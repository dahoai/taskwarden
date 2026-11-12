"""Background runs: queued tasks, answered ones (a decision re-queues them), stale approvals
expired, stuck runs recovered.

Safe with several workers: one sweep at a time (its runs in parallel, each in its own session) under a Redis single-flight lock (any client
with async `set(key, value, nx=, ex=)`, `get`, `delete`). Never raises out of a sweep's work.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import case, func, select, update

from .loop import answer_ask, cancel_children

logger = logging.getLogger(__name__)

LOCK_KEY = "team_sweeper:lock"
STALE_AFTER = timedelta(minutes=10)
# How long a task may wait on a person (the owner's approval or answer, a teammate's answer)
# before the sweeper expires it. A host overrides `BaseHost.APPROVAL_TTL` to change it.
EXPIRE_AFTER = timedelta(days=7)
MAX_ATTEMPTS = 3
BATCH_SIZE = 10
RUN_TIMEOUT_SECONDS = 300  # must stay below STALE_AFTER so a hung run is reclaimed, not doubled
# A run checkpoints after this long and the next sweep carries on; the margin is one slow
# model round plus its tools, so the timeout above only ever stops a hung run.
RUN_BUDGET_SECONDS = 180
# Runs at once in one sweep, each holding a database connection for up to RUN_TIMEOUT_SECONDS.
CONCURRENCY = 4
OWNER_ORIGIN = "owner"


async def retry_or_fail(host, db, task: Any) -> None:
    if task.status in ("waiting", "done", "stopped"):
        return  # the run parked or finished before failing; its committed state stands
    if (task.attempts or 0) >= MAX_ATTEMPTS:
        task.status, task.pending = "failed", None
        body = f"I couldn't finish: {task.goal}"
        host.post_message(db, organization_id=task.organization_id, thread=task.thread,
                          sender=task.agent_id, body=body, task_id=task.id)
        # Someone waiting on this task to answer them gets the failure too, rather than
        # hanging until their own wait expires.
        await answer_ask(host, db, task, f"no answer ({body})")
        await cancel_children(host, db, task)
    else:
        task.status = "queued"


async def expire_and_recover(host, db, now: datetime) -> None:
    Task = host.Task
    ttl = getattr(host, "APPROVAL_TTL", EXPIRE_AFTER) or EXPIRE_AFTER
    expired = (await db.execute(select(Task).where(
        Task.status == "waiting", Task.updated_at < now - ttl))).scalars().all()
    for t in expired:
        # An ask parked on a teammate timed out; an approval or question timed out on the owner.
        why = ("I never got an answer, so I stopped" if (t.pending or {}).get("kind") == "ask_agent"
               else "Approval expired, so I stopped")
        t.status, t.pending = "failed", None
        host.post_message(db, organization_id=t.organization_id, thread=t.thread,
                          sender=t.agent_id, body=f"{why}: {t.goal}", task_id=t.id)
        await cancel_children(host, db, t)
    stale = (await db.execute(select(Task).where(
        Task.status == "running", Task.updated_at < now - STALE_AFTER))).scalars().all()
    for t in stale:
        await retry_or_fail(host, db, t)
    await db.commit()


async def run_one(host, session_factory, task_id: Any, *, run: Callable[..., Awaitable[str]],
                  run_timeout: float = RUN_TIMEOUT_SECONDS,
                  run_budget: float = RUN_BUDGET_SECONDS) -> bool:
    """Claim one queued task and run it in a session of its own. False when another worker
    took it first."""
    Task = host.Task
    async with session_factory() as db:
        task = await db.get(Task, task_id)
        if task is None:
            return False
        new_attempts = (task.attempts or 0) + 1
        claimed = (await db.execute(update(Task).where(
            Task.id == task_id, Task.status == "queued"
        ).values(status="running", attempts=new_attempts)
            .returning(Task.id))).scalar_one_or_none()
        if claimed is None:
            return False  # another worker took it
        task.status, task.attempts = "running", new_attempts
        await db.commit()
        try:
            await asyncio.wait_for(run(db, task, actor="task", budget_seconds=run_budget),
                                   timeout=run_timeout)
        except Exception:
            logger.exception("team_task_failed task_id=%s", task.id)
            # Keep the run's progress by committing first: the loop saved the task's state
            # after every round, and audit rows exist for side effects that really happened,
            # so rolling back would make a retry repeat them. Only fall back to rollback +
            # refresh if that commit itself fails.
            try:
                await db.commit()
            except Exception:
                await db.rollback()
                await db.refresh(task)
            await retry_or_fail(host, db, task)
        await db.commit()
        return True


async def sweep(host, session_factory, redis, *, now: datetime,
                run: Callable[..., Awaitable[str]], lock_ttl: int = 900,
                on_sweep: Callable[..., Awaitable[None]] | None = None,
                run_timeout: float = RUN_TIMEOUT_SECONDS,
                run_budget: float = RUN_BUDGET_SECONDS,
                concurrency: int = CONCURRENCY) -> int:
    """One sweep: expire, recover, then run up to BATCH_SIZE queued tasks, owner work first,
    `concurrency` at a time, each in its own session (one business's long run no longer holds
    up everyone else's). `run(db, task, actor=, budget_seconds=)` runs one task;
    `on_sweep(db, redis, now)` is the app's own step (reminders) after recovery. Returns how
    many tasks ran."""
    token = uuid.uuid4().hex
    if not await redis.set(LOCK_KEY, token, nx=True, ex=lock_ttl):
        return 0
    Task = host.Task
    try:
        async with session_factory() as db:
            await expire_and_recover(host, db, now)
            if on_sweep is not None:
                await on_sweep(db, redis, now)
            # Owner work first: a schedule can queue a burst of self-started tasks at once, and
            # without the ordering an owner's request waits behind all of them. Then one task
            # per business before anyone's second: a business with a long queue would
            # otherwise fill the batch and every other business waits a sweep.
            owner_first = case((Task.origin == OWNER_ORIGIN, 0), else_=1)
            ranked = select(Task.id, owner_first.label("owner_first"), Task.created_at,
                            func.row_number().over(partition_by=Task.organization_id,
                                                   order_by=(owner_first, Task.created_at))
                            .label("turn")).where(Task.status == "queued").subquery()
            queued = (await db.execute(select(ranked.c.id).order_by(
                ranked.c.owner_first, ranked.c.turn, ranked.c.created_at).limit(
                BATCH_SIZE))).scalars().all()
        slots = asyncio.Semaphore(max(1, concurrency))

        async def one(task_id) -> bool:
            async with slots:
                return await run_one(host, session_factory, task_id, run=run,
                                     run_timeout=run_timeout, run_budget=run_budget)
        results = await asyncio.gather(*(one(i) for i in queued), return_exceptions=True)
        for task_id, result in zip(queued, results):
            if isinstance(result, BaseException):  # the claim or its commits failed, not the run
                logger.error("team_task_sweep_failed task_id=%s", task_id, exc_info=result)
        return sum(1 for r in results if r is True)
    finally:
        try:
            if await redis.get(LOCK_KEY) == token:
                await redis.delete(LOCK_KEY)
        except Exception:  # pragma: no cover - lock release is best-effort
            pass

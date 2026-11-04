"""A reply from someone the team messaged goes back to the task that messaged them.

The task already keeps what it sent (`contacted`: "tool|recipient|body", from its own runs and
from sends the owner approved), so no subscription is stored: the newest finished task that
reached this person in the last WINDOW gets the reply appended and goes back to the queue.
A task is woken at most MAX_WAKES times, so a chatty customer can't keep it running.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import Text, cast, or_, select

REPLY_NOTE = "A reply came in. "
WINDOW = timedelta(days=3)
MAX_WAKES = 3


def wakes(state: list) -> int:
    return sum(1 for m in state or [] if isinstance(m, dict) and m.get("role") == "user"
               and str(m.get("content") or "").startswith(REPLY_NOTE))


async def wake(host, db, organization_id: str, reach_keys: list[str], note: str, *,
               now: datetime) -> Optional[Any]:
    """`reach_keys`: the "tool|recipient" spellings that would have reached this person (an SMS
    and a WhatsApp come from the same number). `note`: what the woken agent reads. Returns the
    woken task, committed, or None."""
    if not reach_keys:
        return None
    Task = host.Task
    contacted = cast(Task.contacted, Text)
    task = (await db.execute(select(Task).where(
        Task.organization_id == organization_id, Task.status == "done",
        Task.updated_at >= now - WINDOW,
        or_(*[contacted.contains(f'"{key}|', autoescape=True) for key in reach_keys]),
    ).order_by(Task.updated_at.desc()).limit(1))).scalars().first()
    if task is None or wakes(task.state) >= MAX_WAKES:
        return None
    task.state = [*(task.state or []), {"role": "user", "content": REPLY_NOTE + note}]
    task.status, task.attempts = "queued", 0
    await db.commit()
    return task

"""Code mode: the agent writes a short Python script that calls its read tools as functions, and
only what the script prints and returns goes back to the model. Thirty lookups and a sum are one
round instead of thirty.

Rules (docs/code-mode.md in the repo that holds this package):
- Every call from a script passes the host's gate and is audited like a normal call.
- Reads only: a call the gate doesn't ALLOW, or a tool that isn't READ, raises inside the script
  and never runs. Writes keep the full loop path (duplicate-send guard, precheck, approvals).
- The script runs in a sandbox the host supplies (`CodeRunner`); `MontyRunner` is the default:
  no files, no environment, no network, memory, time and call limits, a fresh worker process
  per script.
- Calls run one at a time: the tools share one database session.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol

from . import gate
from .tools import READ, NeedsConnect, Tool

logger = logging.getLogger(__name__)

RUN_CODE = "run_code"
MAX_OUTPUT_CHARS = 8000
MAX_CODE_CHARS = 8000

Function = Callable[..., Awaitable[Any]]


@dataclass
class CodeResult:
    value: Any = None       # the script's last expression
    output: str = ""        # what it printed
    error: Optional[str] = None  # set when the script failed or hit a limit


class CodeRunner(Protocol):
    async def run(self, code: str, functions: dict[str, Function]) -> CodeResult: ...


class MontyRunner:
    """Runs scripts in Pydantic Monty (`pip install taskwarden[code]`), a Python interpreter in
    Rust that can't express a file, environment or network operation. Each script gets a fresh
    worker subprocess (scripts come from different businesses) with an empty environment."""

    def __init__(self, *, max_memory: int = 64 * 1024 * 1024, max_seconds: float = 10.0,
                 max_calls: int = 60, request_timeout: float = 120.0,
                 processes: int = 4, binary_path: Optional[str] = None) -> None:
        self.limits = {"max_memory": max_memory, "max_feed_duration_secs": max_seconds,
                       # every tool call is one suspension, plus a little for awaits
                       "max_suspensions": max_calls}
        self._pool_args = {"max_processes": processes, "request_timeout": request_timeout,
                           "max_checkouts_per_worker": 1, "binary_path": binary_path}
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _ready(self) -> Any:
        async with self._lock:
            if self._pool is None:
                from pydantic_monty import AsyncMonty
                pool = AsyncMonty(**{k: v for k, v in self._pool_args.items() if v is not None})
                await pool.__aenter__()
                self._pool = pool
            return self._pool

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                pool, self._pool = self._pool, None
                await pool.__aexit__(None, None, None)

    async def run(self, code: str, functions: dict[str, Function]) -> CodeResult:
        from pydantic_monty import MontyCrashedError, MontyError
        printed: list[str] = []

        def on_print(_stream: str, text: str) -> None:
            if sum(map(len, printed)) < MAX_OUTPUT_CHARS:
                printed.append(text)

        pool = await self._ready()
        try:
            async with pool.checkout(limits=self.limits) as session:
                value = await session.feed_run(code, external_lookup=functions,
                                               print_callback=on_print)
            return CodeResult(value=value, output="".join(printed))
        except MontyCrashedError:
            logger.warning("taskwarden_code_crashed")
            return CodeResult(output="".join(printed), error="The sandbox stopped: try a "
                                                              "smaller script.")
        except MontyError as e:
            return CodeResult(output="".join(printed), error=e.display("traceback")[-2000:])


def _short(text: str, n: int = 110) -> str:
    text = " ".join((text or "").split())
    first = text.split(". ")[0]
    return (first if len(first) <= n else first[:n - 1] + "…")


def schema(tools: list[Tool]) -> dict:
    """The `run_code` tool, listing the functions a script may call (reads only)."""
    lines = []
    for t in tools:
        fn = t.schema.get("function", {})
        params = ", ".join(f"{p}=" for p in (fn.get("parameters") or {}).get("properties", {}))
        lines.append(f"- await {t.name}({params}): {_short(fn.get('description', ''))}")
    return {"type": "function", "function": {
        "name": RUN_CODE,
        "description": (
            "Run a short Python script that calls your read tools as async functions, for "
            "work over many items: look up 30 leads, filter, count, sum, compare dates. Only "
            "what the script prints and its last expression come back to you, so return just "
            "what you need. Call tools with keyword arguments and `await` each one "
            "(`asyncio.gather` runs several). A tool returns its data (dicts and lists). "
            "Reads only: to send, book or change anything, make that a normal tool call. "
            "No files, internet or packages; json, re, math, datetime and asyncio work.\n"
            "Functions:\n" + "\n".join(lines)),
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "The Python script."}},
            "required": ["code"]}}}


def readable(tools: dict[str, Tool], offered: set[str]) -> list[Tool]:
    """The tools a script may call: the ones offered this turn that only read."""
    return [tools[n] for n in sorted(offered) if n in tools and tools[n].risk == READ]


def as_text(result: CodeResult, calls: int) -> str:
    parts = []
    if result.output:
        parts.append(result.output.rstrip()[:MAX_OUTPUT_CHARS])
    if result.error:
        parts.append(f"Error: {result.error}")
    elif result.value is not None:
        try:
            value = json.dumps(result.value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            value = repr(result.value)
        parts.append(f"Result: {value[:MAX_OUTPUT_CHARS]}")
    parts.append(f"({calls} tool call{'s' if calls != 1 else ''})")
    return "\n".join(parts)


async def run(host: Any, db: Any, *, code: str, agent: Any, state: Any, task: Any, ctx: Any,
              offered: set[str], self_started: bool, actor: str,
              reads: dict[str, Any]) -> tuple[str, int]:
    """Run one `run_code` call. Returns the tool message and how many tool calls succeeded."""
    org = task.organization_id
    if not isinstance(code, str) or not code.strip():
        return "Error: give the script as `code`.", 0
    if len(code) > MAX_CODE_CHARS:
        return f"Error: keep the script under {MAX_CODE_CHARS} characters.", 0
    names = {t.name for t in readable(host.tools, offered)}
    lock = asyncio.Lock()
    done = 0

    def proxy(name: str) -> Function:
        async def call(*args: Any, **kwargs: Any) -> Any:
            nonlocal done
            if args:
                raise TypeError(f"{name}: use keyword arguments, e.g. {name}(status='new')")
            tool = host.tools.get(name)
            decision = host.decide(agent, tool, state, self_started=self_started)
            ok_to_run = (tool is not None and name in names and tool.risk == READ
                         and decision.outcome == gate.ALLOW)
            host.audit(db, organization_id=org, agent_id=agent.id, tool=name,
                       risk=tool.risk if tool else None,
                       decision=decision.outcome if ok_to_run else gate.DENY,
                       rule=f"{decision.rule}_from_code" if ok_to_run else "code_reads_only",
                       task_id=task.id, actor=actor)
            if not ok_to_run:
                raise PermissionError(f"{name} can't run from a script: make it a normal "
                                      "tool call.")
            async with lock:  # one database session behind every tool
                if tool.check is not None:
                    try:
                        refusal = await host.precheck(tool, ctx, kwargs)
                    except NeedsConnect as e:
                        raise RuntimeError(f"{name} needs {e.account} connected: call it "
                                           "normally so the owner is asked.") from None
                    if refusal:
                        raise ValueError(f"{name}: {refusal}")
                try:
                    out, ok, raw = await host.execute(tool, ctx, kwargs)
                except NeedsConnect as e:
                    raise RuntimeError(f"{name} needs {e.account} connected: call it "
                                       "normally so the owner is asked.") from None
            if not ok:
                reads.pop(name, None)
                raise RuntimeError(f"{name}: {out}")
            done += 1
            if raw is not None:
                reads[name] = raw  # `show` can draw what a script read, like any read
                return raw
            return out
        return call

    runner = host.code_runner
    try:
        # Every offered tool is a name in the script, so a write says why it can't run there
        # instead of a NameError; only `names` (the reads) get past the check.
        result = await runner.run(code, {n: proxy(n) for n in offered if n in host.tools})
    except Exception:
        logger.exception("taskwarden_code_failed task_id=%s", task.id)
        result = CodeResult(error="The script could not run. Make the calls one by one.")
    host.audit(db, organization_id=org, agent_id=agent.id, tool=RUN_CODE, risk=READ,
               decision="error" if result.error else gate.ALLOW,
               rule="code_failed" if result.error else "code_ran", task_id=task.id, actor=actor,
               target=f"{done} calls")
    return as_text(result, done), done

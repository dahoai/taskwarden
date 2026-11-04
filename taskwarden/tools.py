"""A tool the team can call: its risk tier, its schema, its handler, and how a call runs.

Every tool carries one risk tier, and the tier is what the gate (taskwarden.gate) decides on:

- READ, DRAFT: looks or prepares, changes nothing anyone sees. Always allowed.
- ROUTINE: high-frequency, low-blast-radius work that IS the agent's job: messaging a customer,
  booking them, moving the business's own records forward. Nothing here commits money,
  publishes under the business's name, or destroys data, and all of it is recoverable by a
  follow-up action. Gating these per instance only trains the owner to click "approve" without
  reading; the real safety valve is the grant (a tool the agent does not have is denied) and
  pausing the agent.
- CONTACT, SPEND, CHANGE: reaches someone in a way that commits the business, spends, or
  changes records the owner wants to see first. The agent's setting decides (ask or allow).
- REMOVE, PROFILE: deletes, and the business's own profile. Always the owner's click.

A handler is `async (ctx, **args) -> str`. `ctx` is whatever the app builds for the business
(`Host.context`); the model never chooses it, and arguments that name whose data a call
touches are dropped before the handler sees them (`clean_args`).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

READ, DRAFT, CONTACT, SPEND, REMOVE, PROFILE = "read", "draft", "contact", "spend", "remove", "profile"
CHANGE = "change"
ROUTINE = "routine"
RISKS = (READ, DRAFT, ROUTINE, CONTACT, SPEND, REMOVE, PROFILE, CHANGE)

# The model never chooses whose data a tool touches.
SERVER_OWNED_ARGS = frozenset({"organization_id", "org_id"})

# A malformed value, almost always an id the model made up or shortened.
BAD_VALUE = ("Tool error: an argument isn't valid. Ids must be the exact id a list tool "
             "returned; look it up again with the matching list tool.")
BAD_ARGS = "Tool error: invalid arguments."
FAILED = "Tool error: the action failed."
# A tool gets this long unless it says otherwise (Tool.timeout): one that hangs would
# otherwise hold the whole run until the sweeper's timeout.
TOOL_TIMEOUT_SECONDS = 120
SLOW = "Tool error: it took too long and was stopped. Try once more, or another way."
# Not a READ: it may have done its work before it was stopped.
SLOW_MAYBE_DONE = ("Unknown: it took too long and was stopped, so it may have gone through. "
                   "Check before doing it again.")


@dataclass(frozen=True)
class Tool:
    name: str
    risk: str
    schema: dict
    handler: Callable[..., Awaitable[str]]  # async (ctx, **args) -> str
    # Asked before the owner is asked: a reason the call can't work as it stands (already
    # running, not in this campaign), or None. The owner never sees an approval card for
    # something that would only fail once approved.
    check: Optional[Callable[..., Awaitable[Optional[str]]]] = None
    # Seconds before the call is stopped; None for no limit.
    timeout: Optional[float] = TOOL_TIMEOUT_SECONDS


def fn_schema(name: str, description: str, properties: Optional[dict] = None,
              required: tuple[str, ...] = ()) -> dict:
    """An OpenAI-style function schema."""
    return {"type": "function", "function": {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties or {},
                       "required": list(required)},
    }}


def clean_args(args: dict[str, Any], owned: frozenset = SERVER_OWNED_ARGS) -> dict[str, Any]:
    return {k: v for k, v in args.items() if k not in owned}


def declared_args(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
    """Only the arguments the tool's schema declares: one the model invented (a `page` on a
    list that has none) would otherwise fail the whole call (TypeError)."""
    params = ((getattr(tool, "schema", None) or {}).get("function") or {}).get("parameters")
    if params is None:  # no schema to check against
        return args
    declared = params.get("properties") or {}
    extra = sorted(set(args) - set(declared))
    if extra:
        logger.info("team_tool_extra_args_dropped tool=%s args=%s", tool.name, extra)
    return {k: v for k, v in args.items() if k in declared}


async def _commit(ctx: Any) -> None:
    """A handler that writes through the run's session leaves its rows locked until the run
    commits, and a run commits only at its end: the next tool, going through another path,
    would wait on that lock. What the tool did is done, so a failed commit is logged, never
    reported to the model as a failure it would retry."""
    db = getattr(ctx, "db", None)
    if db is None:
        return
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("team_tool_commit_failed organization_id=%s", _org(ctx))
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            logger.warning("team_tool_rollback_failed")


async def _rollback(ctx: Any) -> None:
    db = getattr(ctx, "db", None)
    if db is not None:
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            logger.warning("team_tool_rollback_failed")


def _org(ctx: Any) -> Any:
    return getattr(ctx, "organization_id", None)


def _no_scope(ctx: Any):
    return contextlib.nullcontext()


async def execute(tool: Tool, ctx: Any, args: dict[str, Any], *,
                  scope: Callable[[Any], Any] = _no_scope,
                  owned: frozenset = SERVER_OWNED_ARGS) -> tuple[str, Optional[bool]]:
    """Run a handler for the business in `ctx`. Never raises: the model gets a short error.
    `scope(ctx)` is a context manager the app wraps every call in (the business's id in a
    context variable for code that reads it there).

    ok is True, False, or None when nobody knows: a non-READ call stopped at its timeout may
    have done its work, so a send that ends that way still counts as sent."""
    limit = getattr(tool, "timeout", TOOL_TIMEOUT_SECONDS)
    with scope(ctx):
        try:
            out = await asyncio.wait_for(
                tool.handler(ctx, **declared_args(tool, clean_args(args, owned))), limit)
            if tool.risk != READ:
                await _commit(ctx)
            return out, True
        except asyncio.TimeoutError:
            logger.warning("team_tool_timeout tool=%s organization_id=%s seconds=%s",
                           tool.name, _org(ctx), limit)
            await _rollback(ctx)  # stopped mid-write: the session can't be trusted as it is
            return (SLOW, False) if tool.risk == READ else (SLOW_MAYBE_DONE, None)
        except ValueError:
            logger.warning("team_tool_bad_value tool=%s organization_id=%s", tool.name, _org(ctx),
                           exc_info=True)
            return BAD_VALUE, False
        except TypeError:  # a required argument missing
            logger.warning("team_tool_bad_args tool=%s organization_id=%s", tool.name, _org(ctx),
                           exc_info=True)
            return BAD_ARGS, False
        except Exception:
            logger.exception("team_tool_failed tool=%s organization_id=%s", tool.name, _org(ctx))
            return FAILED, False


async def check(tool: Tool, ctx: Any, args: dict[str, Any], *,
                scope: Callable[[Any], Any] = _no_scope,
                owned: frozenset = SERVER_OWNED_ARGS) -> Optional[str]:
    """The tool's own check, run for the business like the handler."""
    with scope(ctx):
        return await tool.check(ctx, **declared_args(tool, clean_args(args, owned)))


# ── Pictures: a handler can hand the model images to look at (next request only) ──
_LOOKS: ContextVar[Optional[list]] = ContextVar("taskwarden_looks", default=None)


@contextlib.contextmanager
def capture_looks():
    holder: list = []
    token = _LOOKS.set(holder)
    try:
        yield holder
    finally:
        _LOOKS.reset(token)


def look(image: Any) -> None:
    """Called by a handler: show the model this image (a URL or bytes) after the call."""
    if image and (holder := _LOOKS.get()) is not None:
        holder.append(image)

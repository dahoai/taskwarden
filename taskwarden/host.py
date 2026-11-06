"""What the app supplies to the harness. taskwarden never imports the app; the app hands it a Host.

Each app writes its own (examples/shop.py is a complete one): its tables (the
`Task` and `Message` models, see taskwarden.models for the columns), its agents and tools, the
words its agents read, its model, and how its owner is told. Everything below is required
unless it has a default in `BaseHost`: subclass it and write only what your app decides.
"""
from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Optional, Protocol

from . import gate
from . import tools as _tools
from .gate import AgentState, Decision
from .tools import Tool

logger = logging.getLogger(__name__)


class Host(Protocol):
    # ── Tables and the roster ──
    Task: Any                      # SQLAlchemy model with taskwarden.models.TaskColumns
    chief: str                     # the agent the owner talks to when they name nobody
    tools: dict[str, Tool]         # every tool, by name

    def agent(self, agent_id: str) -> Any: ...  # .id .name .title .tools .folder
    def agents_for(self, business_type: Optional[str]) -> list[Any]: ...
    def tools_of(self, agent: Any, business_type: Optional[str]) -> tuple[str, ...]: ...
    def for_business(self, agent: Any, business_type: Optional[str]) -> Any: ...

    # ── Before a run ──
    async def refuse_run(self, db: Any, task: Any, actor: str) -> bool:
        """True when the business can't run the team now (no credits). Tell the owner if
        `actor == "owner"`; the harness fails the task."""
    async def context(self, db: Any, organization_id: str) -> tuple[Any, dict]:
        """(the ctx every handler gets, with .db .organization_id .timezone; the business's
        facts the host itself reads back: business_type, country)."""
    async def agent_states(self, db: Any, organization_id: str) -> dict[str, AgentState]: ...
    def default_state(self) -> AgentState: ...
    async def token_budget(self, db: Any, task: Any) -> Optional[int]:
        """Tokens this task's agent may use in a day, or None for no cap. Optional; needs the
        `tokens` column on Task. Past it, the run stops and the chat says so (OVER_BUDGET)."""
    async def llm(self, db: Any, organization_id: str) -> tuple[Any, str]:
        """(an OpenAI-compatible async client, model name)."""
    def extra_body(self, model: str) -> Optional[dict]: ...
    def clean_text(self, text: Optional[str]) -> str: ...
    def self_started(self, task: Any) -> bool:
        """The app's schedule started this task: nothing leaves without the owner's click."""
    def never_waits(self, task: Any) -> bool:
        """This task may not ask anyone or park (a morning review): what needs the owner goes
        in its report."""
    def preloaded_skills(self, agent: Any, task: Any) -> set[str]: ...
    async def system_prompt(self, db: Any, task: Any, agent: Any, name: str, teammates: list,
                            now: datetime,
                            channel: Optional[str], business_type: Optional[str],
                            self_started: bool, loaded: set[str]) -> str: ...
    async def thought(self, organization_id: str, thread: str, agent_id: str, text: str) -> None:
        """The model's reasoning on its latest turn, for a live "why" beside the progress
        line. Optional; called only when the model thinks."""
    async def take_messages(self, task: Any) -> list[str]:
        """What the owner wrote to this task while it ran, removed as it is taken: the run
        reads it at the start of each round and before it finishes. Optional."""
    async def stop_asked(self, task: Any) -> bool:
        """The owner pressed Stop on this task: checked before each model call and each tool,
        so the run ends cleanly at the next step (loop.stop). Optional."""
    def clock(self, now: datetime) -> Optional[str]:
        """The time, in words, for the first message of each request. Kept out of the system
        prompt: a prefix that changes every minute is never served from the model's cache."""
    def local_schemas(self, channel: Optional[str], task: Any) -> list[dict]:
        """Tools the host answers inside the loop itself (a `show` that draws a table, say)."""
    async def local_call(self, db: Any, organization_id: str, name: str, args: dict,
                         reads: dict, shown: list) -> Optional[str]:
        """The answer to a local tool, or None when `name` is not one."""
    def region(self, company: dict) -> str: ...

    # ── Each call ──
    def decide(self, agent: Any, tool: Optional[Tool], state: AgentState, *,
               self_started: bool) -> Decision: ...
    async def precheck(self, tool: Tool, ctx: Any, args: dict) -> Optional[str]:
        """The tool's own check before the owner is asked. Must not raise: a check that fails
        to run returns None, and the owner is asked as before."""
    def clean_args(self, args: dict) -> dict:
        """The arguments a parked call keeps: without the ones the server owns."""
    def use_skill(self, agent: Any, args: dict, loaded: set[str]) -> tuple[str, tuple[str, ...]]:
        """Load a skill: taskwarden.loop.use_skill, unless the host records it too."""
    async def execute(self, tool: Tool, ctx: Any, args: dict) -> tuple[str, Optional[bool], Any]:
        """(text for the model, ok, the raw result of a read or None). ok None: unknown (a
        call stopped at its timeout), counted as sent."""
    def audit(self, db: Any, *, organization_id: str, agent_id: str, tool: str,
              risk: Optional[str], decision: str, rule: str, task_id: Any, actor: str,
              target: Optional[str] = None) -> None: ...
    def reach_key(self, tool_name: str, args: dict, region: str) -> Optional[str]:
        """`tool|recipient` when the call reaches one person: one message per person per turn."""
    def sent_key(self, tool_name: str, args: dict, region: str) -> Optional[str]:
        """`tool|recipient|body`: never the same message to the same person twice in a task."""
    async def picture_url(self, image: Any) -> Optional[str]: ...
    async def mark(self, organization_id: str, thread: str, agent_id: str, step: str) -> None:
        """Live progress for the owner's screen; best effort."""

    # ── Telling people ──
    def post_message(self, db: Any, *, organization_id: str, thread: str, sender: str,
                     body: str, kind: str = "text", task_id: Any = None) -> Any: ...
    async def finish(self, db: Any, task: Any, body: str, shown: list, actor: str) -> None:
        """Post the report (and anything shown), wake a teammate waiting on it
        (taskwarden.loop.answer_ask), relay background work to the owner."""
    async def decision_names(self, db: Any, organization_id: str, decided: list[dict]) -> dict:
        """Names for the people the decided calls reach ({"+1555...": "Hannah Lee"}): the app
        shows the decision with them."""
    async def owner_fields(self, db: Any, task: Any) -> dict:
        """Extra fields on a park that waits for the owner (a code they can reply with, say)."""
    async def notify_parked(self, db: Any, task: Any, pending: dict, name: str, actor: str,
                            channel: Optional[str], summary: str, body: str) -> None:
        """Tell the owner something waits for them. May update task.pending."""

    # Words the harness says to the model on the host's behalf.
    NEVER_WAITS_REFUSED: str
    NEVER_WAITS_NEEDS_OWNER: str
    OUT_OF_STEPS: str
    # Optional, defaults in taskwarden.loop: under a watch task's goal, its refusal, the budget.
    WATCHING: str
    WATCH_REFUSED: str
    OVER_BUDGET: str  # {name} {limit}


class BaseHost:
    """Defaults for every member of `Host` that has a sensible one. A subclass sets `Task`,
    `Message`, `chief`, `agents` (by id) and `tools`, and writes `llm` and `system_prompt`;
    everything else can stay as it is until the app needs more (examples/shop.py).

    What the defaults leave out: no credit check, one time zone (UTC), no live progress, no
    duplicate-send guard (write `sent_key` and `reach_key` once a tool reaches people), and
    the owner is told only through the chat (write `notify_parked` for push, SMS or email)."""

    Task: Any
    Message: Any = None  # a model with taskwarden.models.MessageColumns; None posts nothing
    chief: str
    agents: dict[str, Any]
    tools: dict[str, Tool]

    NEVER_WAITS_REFUSED = "Refused: this task can't ask anyone. Put it in your report."
    NEVER_WAITS_NEEDS_OWNER = ("Not done: this needs the owner, and this task can't wait for "
                               "them. Put it in your report.")
    OUT_OF_STEPS = "I ran out of steps before finishing. Ask me to carry on."
    DAILY_TOKENS: Optional[int] = None  # each agent's daily token budget; None = no cap
    STOPPED_REPLY = "Stopped."
    STOPPED_DID = "Done before that: {did}."

    # ── Write these ──
    async def llm(self, db: Any, organization_id: str) -> tuple[Any, str]:
        raise NotImplementedError("return (an OpenAI-compatible async client, model name)")

    async def system_prompt(self, db, task, agent, name, teammates, now, channel,
                            business_type, self_started, loaded) -> str:
        raise NotImplementedError("return the words the agent reads first")

    async def take_messages(self, task) -> list[str]:
        return []  # write it to let the owner add to a request while it runs

    async def stop_asked(self, task) -> bool:
        return False  # write it (a flag in Redis, say) to give the owner a Stop button

    def clock(self, now) -> Optional[str]:
        return f"Right now it is {now:%A %Y-%m-%d %H:%M} ({now.tzname()})."

    # ── The roster ──
    def agent(self, agent_id: str) -> Any:
        return self.agents[agent_id]

    def agents_for(self, business_type: Optional[str]) -> list[Any]:
        return list(self.agents.values())

    def tools_of(self, agent: Any, business_type: Optional[str]) -> tuple[str, ...]:
        return tuple(agent.tools)

    def for_business(self, agent: Any, business_type: Optional[str]) -> Any:
        return agent

    # ── Before a run ──
    async def refuse_run(self, db, task, actor) -> bool:
        return False

    async def context(self, db, organization_id) -> tuple[Any, dict]:
        return SimpleNamespace(db=db, organization_id=organization_id, timezone="UTC"), {}

    async def agent_states(self, db, organization_id) -> dict[str, AgentState]:
        return {}

    def default_state(self) -> AgentState:
        return AgentState()

    async def token_budget(self, db, task) -> Optional[int]:
        return self.DAILY_TOKENS  # per agent and business: override to vary it

    def extra_body(self, model: str) -> Optional[dict]:
        return None

    def clean_text(self, text: Optional[str]) -> str:
        return (text or "").strip()

    def self_started(self, task) -> bool:
        # A hand-off copies its parent's origin, so delegating is no way around the owner.
        return task.origin != "owner"

    def never_waits(self, task) -> bool:
        return False

    def preloaded_skills(self, agent, task) -> set[str]:
        return set()

    def local_schemas(self, channel, task) -> list[dict]:
        return []

    async def local_call(self, db, organization_id, name, args, reads, shown) -> Optional[str]:
        return None

    def region(self, company: dict) -> str:
        return str(company.get("country") or "")

    # ── Each call ──
    def decide(self, agent, tool, state, *, self_started) -> Decision:
        return gate.decide(agent, tool, state, self_started=self_started)

    async def precheck(self, tool, ctx, args) -> Optional[str]:
        if tool.check is None:
            return None
        try:
            return await _tools.check(tool, ctx, args)
        except Exception:  # a check that can't run asks the owner as before
            logger.exception("taskwarden_precheck_failed tool=%s", tool.name)
            return None

    def clean_args(self, args: dict) -> dict:
        return _tools.clean_args(args)

    def use_skill(self, agent, args, loaded):
        from .loop import use_skill  # the loop imports this module
        return use_skill(agent, args, loaded)

    async def execute(self, tool, ctx, args):
        out, ok = await _tools.execute(tool, ctx, args)
        return out, ok, None

    def audit(self, db, **kw) -> None:
        logger.info("taskwarden_audit %s", kw)

    def reach_key(self, tool_name, args, region) -> Optional[str]:
        return None

    def sent_key(self, tool_name, args, region) -> Optional[str]:
        return None

    async def picture_url(self, image) -> Optional[str]:
        return image if isinstance(image, str) else None

    async def mark(self, organization_id, thread, agent_id, step) -> None:
        return None

    # ── Telling people ──
    def post_message(self, db, *, organization_id, thread, sender, body, kind="text",
                     task_id=None) -> Any:
        if self.Message is None:
            return None
        msg = self.Message(organization_id=organization_id, thread=thread, sender=sender,
                           body=body, kind=kind, task_id=task_id)
        db.add(msg)
        return msg

    async def finish(self, db, task, body, shown, actor) -> None:
        from .loop import answer_ask
        self.post_message(db, organization_id=task.organization_id, thread=task.thread,
                          sender=task.agent_id, body=body,
                          kind="result" if task.parent_id else "text", task_id=task.id)
        await answer_ask(self, db, task, body)  # a teammate waiting on this answer wakes

    async def decision_names(self, db, organization_id, decided) -> dict:
        return {}

    async def owner_fields(self, db, task) -> dict:
        return {}

    async def notify_parked(self, db, task, pending, name, actor, channel, summary,
                            body) -> None:
        return None

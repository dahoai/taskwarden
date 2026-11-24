"""The action boundary: every tool call is decided before it runs.

Order, first match wins: tool not granted -> deny; agent paused -> deny; a tool the owner
blocked for this agent -> deny, in every setting and on every task; a setting that isn't
`allow`/`ask`/`observe` -> deny; an agent in practice ("observe") -> reads run, everything else
is only recorded; a tool the owner chose to trust runs without asking (never spend, remove or
profile); a self-started task reaching a customer or the money -> ask; remove/profile ->
always ask; read/draft/routine -> allow; contact/spend/change -> the agent's setting ("ask" |
"allow"); anything else -> deny (fail closed).

A self-started task (one the app's own schedule began, not the owner) never sends, spends or
commits the business without a click, however loose the agent's setting is: it reads and
drafts freely, then parks, and the owner's approval runs the call it prepared. `outward` names
the ROUTINE tools that still reach a customer (a text, a booking) for that rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .tools import CHANGE, CONTACT, DRAFT, PROFILE, READ, REMOVE, ROUTINE, SPEND, Tool

ALLOW, ASK, DENY = "allow", "ask", "deny"
# The agent's practice setting, and the outcome of a call made in it: recorded, not run. A new
# agent (or a new skill) can work real requests and show what it would have done.
OBSERVE = "observe"
SELF_STARTED_NEEDS_OWNER = "self_started_needs_owner"
OWNER_BLOCKED = "owner_blocked"


@dataclass(frozen=True)
class Decision:
    outcome: str
    rule: str


@dataclass(frozen=True)
class AgentState:
    """The owner's settings for one agent. An app adds its own fields by subclassing."""
    setting: str = ASK
    paused: bool = False
    display_name: Optional[str] = None
    # Tools the owner chose to stop being asked about. The app passes only names it allows
    # (app/team/standing.py); the gate still refuses to loosen spend, remove and profile calls.
    trusted_tools: frozenset = frozenset()
    # Tools the owner said this agent never uses. Refused without asking, whoever started
    # the task and whatever the setting: only ever tighter than not blocking.
    blocked_tools: frozenset = frozenset()


def decide(agent: Any, tool: Optional[Tool], state: AgentState, *,
           self_started: bool = False, outward: frozenset = frozenset()) -> Decision:
    """`agent.tools` is what the agent is granted."""
    if tool is None or tool.name not in agent.tools:
        return Decision(DENY, "tool_not_granted")
    if state.paused:
        return Decision(DENY, "agent_paused")
    if tool.name in state.blocked_tools:
        return Decision(DENY, OWNER_BLOCKED)
    if state.setting not in (ALLOW, ASK, OBSERVE):
        return Decision(DENY, "unknown_setting")  # a corrupted setting never loosens the gate
    if state.setting == OBSERVE:
        return Decision(ALLOW, "safe_action") if tool.risk == READ else Decision(OBSERVE, "practice")
    if tool.name in state.trusted_tools and tool.risk not in (SPEND, REMOVE, PROFILE):
        return Decision(ALLOW, "owner_trusted")  # the owner said so, for this agent and this tool
    if self_started and (tool.risk in (CONTACT, SPEND, CHANGE)
                         or (tool.risk == ROUTINE and tool.name in outward)):
        return Decision(ASK, SELF_STARTED_NEEDS_OWNER)
    if tool.risk == REMOVE:  # deletes can't be undone: always the owner's click
        return Decision(ASK, "always_ask_remove")
    if tool.risk == PROFILE:  # the business profile changes only with the owner's click
        return Decision(ASK, "always_ask_profile")
    if tool.risk in (READ, DRAFT, ROUTINE):
        return Decision(ALLOW, "safe_action")
    if tool.risk in (CONTACT, SPEND, CHANGE):
        if state.setting == ASK:
            return Decision(ASK, "owner_approval_required")
        if state.setting == ALLOW:
            return Decision(ALLOW, "owner_allowed")
    return Decision(DENY, "fail_closed")

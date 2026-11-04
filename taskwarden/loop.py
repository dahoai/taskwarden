"""The agent loop: run one task from its saved conversation until it finishes, parks waiting on
the owner, or runs out of rounds.

Four tools are built in, not in the host's catalogue: delegate (the chief only), consult (any
agent asks a teammate), escalate (specialists only: parks the task and asks the chief) and
ask_person (every agent). The three agent-to-agent ones queue an ordinary child task and wait on
it (_ask_agent), which keeps the owner out of the loop; only ask_person reaches them.

Everything the app owns (its tables, tools, words, model, how the owner is told) comes through
the Host (taskwarden.host).
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from . import gate
from .folders import USE_SKILL
from .tools import READ, capture_looks

logger = logging.getLogger(__name__)

# A backstop, not a budget: a run checkpoints when its time is up (budget_seconds) and the
# sweeper carries on with it, so real work is never cut short. What ends a run early is a
# loop (stalled) or a conversation too long for the model even after compact (too_long).
MAX_ROUNDS = 60
STALL_REPEATS = 3  # the same tool calls this many rounds in a row is a loop, not work
# Said once, at the first repeat, so the model can change course before the run is stopped.
REPEATED = ("You just made exactly the same call again. Use what it returned, try another way, "
            "or reply to the owner.")
MAX_HISTORY_CHARS = 400_000  # ~100k tokens
# The last call of a turn offers no tools; this tells the model so, and what to say instead.
WRAP_UP = ("You have used all your steps for this turn. Reply to the owner now, in words: "
           "what you did, what you found, and what is left to do next time. Report only what "
           "a tool actually returned -- if you did not get to read something, say so.")
# A reply that states a figure ("266 leads") is claiming a fact about the business. A model
# that had read nothing could invent a whole snapshot and the loop took it as done. The nudge
# gives the model one chance to go read, or to drop the claim.
UNGROUNDED = ("You just stated figures about the business, but you ran no tool this turn, so "
              "they cannot be yours to state. Call the tool that reads them and answer from "
              "its result, or say plainly that you have not read it. Whatever you write next "
              "is your reply to the owner, so write it to them: do not apologise, and do not "
              "mention this instruction.")
# A self-started reply that announces its next step ("Let me find pictures.") and calls
# nothing ends the run on a promise, and nobody is there to say "go". Self-started only: on an
# owner's chat "I'll wait for your go" is a real answer.
UNFINISHED = ("You said what you will do next but called no tool, so nothing happened. Do it "
              "now by calling the tool, or say plainly why it cannot be done now. Whatever you "
              "write next is your reply to the owner, so write it to them: do not apologise, "
              "and do not mention this instruction.")
_ANNOUNCES = re.compile(r"\b(?:let me|i'll|i will|i'm going to|je vais|laissez-moi|voy a|déjame)\b",
                        re.IGNORECASE)
_FIGURE = re.compile(r"\d")
# Digits that are not a claim about the business: a phone number, a clock time.
_NOT_A_FIGURE = re.compile(
    r"\+?\d[\d\s().-]{6,}\d|\b\d{1,2}:\d{2}\b|\b\d{1,2}\s?(?:am|pm|h)\b", re.IGNORECASE)
# Tool messages that carry no result: they cannot ground a figure.
_NO_RESULT = ("Refused", "Waiting for your approval", "Not sent", "Owner declined", "Tool error")

MAX_ASK_DEPTH = 2  # a task this deep in a hand-off chain answers; it cannot hand off again
# A send is saved as possibly sent before it runs: if the process dies mid-send, the resumed run
# reads this instead of sending again, and the duplicate guard knows the message.
MAYBE_SENT = ("Unknown: the run stopped while this was being sent, so it may have gone out. "
              "Check before sending it again.")
# A call the run never reached before it stopped (a restart mid-round).
NOT_RUN = "Not run: the run stopped before this call. Call it again if it is still needed."
# What a call made in practice reads: not run, and the report says what would have been done.
PRACTICE = ("Practice mode: not done, nothing was sent or changed.\n{call}\nCarry on with the "
            "request, and list in your report each action you would have taken.")
MAX_DELEGATIONS_PER_TASK = 5  # across every run of the task, approval resumes and retries included
DAILY_TASK_CAP = 200  # ponytail: flat per-business cap over a rolling 24h; plan-based when outgrown
DONE, WAITING = "done", "waiting"
THINKING = "thinking"

DELEGATE_SCHEMA = {"type": "function", "function": {
    "name": "delegate",
    "description": "Hand a task to a teammate. They work on it in the background and report back.",
    "parameters": {"type": "object", "properties": {
        "agent": {"type": "string", "description": "Teammate id"},
        "task": {"type": "string", "description": "What they should do, with the details they need"},
    }, "required": ["agent", "task"]},
}}

ASK_TEAM_SCHEMA = {"type": "function", "function": {
    "name": "consult",
    "description": "Ask a teammate to do or look up something that needs a tool you do not "
                   "have, and wait for their answer. Never use it to ask who owns what, what "
                   "their job is, or something your own tools can answer.",
    "parameters": {"type": "object", "properties": {
        "agent": {"type": "string", "description": "Teammate id"},
        "question": {"type": "string", "description": "One clear question, with the details they need"},
    }, "required": ["agent", "question"]},
}}

ESCALATE_SCHEMA = {"type": "function", "function": {
    "name": "escalate",
    "description": "Bring a decision you cannot make or a conflict you cannot resolve to the "
                   "Chief, and wait for their guidance. Rare: only for a judgement call or a "
                   "conflict between two sources, never for a fact you could look up.",
    "parameters": {"type": "object", "properties": {
        "question": {"type": "string", "description": "The decision or conflict, stated plainly"},
        "why": {"type": "string", "description": "What you tried, and what you cannot settle"},
    }, "required": ["question"]},
}}

ASK_PERSON_SCHEMA = {"type": "function", "function": {
    "name": "ask_person",
    "description": "Ask the owner a question when a step needs their judgement. Your work pauses until they answer.",
    "parameters": {"type": "object", "properties": {
        "question": {"type": "string", "description": "One clear question"},
        "why": {"type": "string", "description": "What you cannot settle on your own"},
        "choices": {"type": "array", "items": {"type": "string"},
                    "description": "When the answer is one of a few options, up to 4 short "
                                   "answers the owner can tap. They can still type their own."},
    }, "required": ["question"]},
}}

MAX_CHOICES, MAX_CHOICE_CHARS = 4, 80


def states_a_figure(text: str) -> bool:
    """True when a reply carries a number -- the shape an invented snapshot takes.

    ponytail: a fabricated claim with no figure slips past this, which is the ceiling of a
    mechanical check; an eval with a judge is what catches that.
    """
    return bool(_FIGURE.search(_NOT_A_FIGURE.sub("", text or "")))


def announces_a_step(text: str) -> bool:
    """A short reply that says what comes next. A finished report runs longer than this.
    ponytail: phrase list in en/fr/es; a promise worded otherwise slips past."""
    return len(text or "") <= 300 and bool(_ANNOUNCES.search(text or ""))


def fresh_results(history: list) -> bool:
    """True when the loaded history ends in results the model has not answered yet: the calls
    the owner just approved, a teammate's answer, the owner's answer to a question. Those are
    this turn's reading, so a resumed run may report them; older history still is not."""
    for m in reversed(history):
        if m.get("role") != "tool":
            return False
        if not str(m.get("content") or "").startswith(_NO_RESULT):
            return True
    return False


def choices(raw: Any) -> list[str]:
    """The tappable answers an ask_person offered: distinct short strings, at most four."""
    out: list[str] = []
    for c in raw if isinstance(raw, list) else []:
        c = c.strip()[:MAX_CHOICE_CHARS] if isinstance(c, str) else ""
        if c and c not in out:
            out.append(c)
    return out[:MAX_CHOICES]


def answer_dangling(history: list) -> list:
    """Every tool call answered, right after its turn: a run that stopped mid-round left some
    without a result, which providers reject. A call that has one keeps it."""
    out, i = [], 0
    while i < len(history):
        msg = history[i]
        out.append(msg)
        i += 1
        calls = msg.get("tool_calls") if isinstance(msg, dict) and msg.get("role") == "assistant" else None
        if not calls:
            continue
        answered = set()
        while i < len(history) and isinstance(history[i], dict) and history[i].get("role") == "tool":
            answered.add(history[i].get("tool_call_id"))
            out.append(history[i])
            i += 1
        out += [tool_msg(c["id"], NOT_RUN) for c in calls if c.get("id") not in answered]
    return out


def describe_call(tool_name: str, args: dict) -> str:
    """Plain text of exactly what will run. Shown on approval cards."""
    detail = "\n".join(f"{k}: {v}" for k, v in args.items())
    return f"{tool_name}\n{detail}" if detail else tool_name


def pending_calls(pending: dict) -> list[dict]:
    """The approval calls in a pending item. A batch parks several calls under one entry; a
    single call keeps the flat shape."""
    batch = pending.get("batch")
    return list(batch) if batch else [{"tool_call_id": pending.get("tool_call_id", ""),
                                      "tool": pending.get("tool", ""), "args": pending.get("args") or {}}]


def pending_tool(pending: dict) -> dict:
    """The first call: what the one-line summary names."""
    return pending_calls(pending)[0]


def describe_pending(pending: dict) -> str:
    """Card text for one call or a whole batch."""
    return "\n\n".join(describe_call(c["tool"], c.get("args") or {}) for c in pending_calls(pending))


def loaded_skills(host, agent: Any, task: Any, history: list[dict]) -> set[str]:
    """The skills this run starts with: what the host preloads for this task, and every skill
    an earlier run of the task loaded (read back from its own messages, so a resume keeps them)."""
    folder = agent.folder
    if folder is None or not folder.on_demand:
        return set()
    out: set[str] = set(host.preloaded_skills(agent, task))
    for msg in history:
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            if fn.get("name") == USE_SKILL:
                name = parse(fn.get("arguments")).get("name")
                if folder.skill(str(name)) is not None:
                    out.add(str(name))
    return out


def use_skill(agent: Any, args: dict, loaded: set[str]) -> tuple[str, tuple[str, ...]]:
    """(what the model reads, the tools the skill adds). Loading one already loaded is fine."""
    name = str(args.get("name", ""))
    skill = agent.folder.skill(name) if agent.folder else None
    if skill is None:
        return f"Refused: '{name}' is not one of your skills.", ()
    loaded.add(name)
    return f"Skill {name} loaded.\n\n{skill.text}", skill.tools


def assistant_turn(msg: Any) -> dict:
    """The model's turn as it is kept, with its reasoning once. any-llm hands DeepSeek's back
    three times (`reasoning`, `reasoning_content` and `extra_content.deepseek`) and replays it
    from `extra_content` only: kept three times, a thinking run's history tripled."""
    turn = msg.model_dump(exclude_none=True)
    replayed = ((turn.get("extra_content") or {}).get("deepseek") or {}).get("reasoning_content")
    if replayed is not None:
        if turn.get("reasoning_content") == replayed:
            turn.pop("reasoning_content")
        if turn.get("reasoning") == replayed or (turn.get("reasoning") or {}) == {"content": replayed}:
            turn.pop("reasoning")
    return turn


def tool_msg(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def parse(arguments: Optional[str]) -> dict:
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return args if isinstance(args, dict) else {}


async def _delegate(host, db, parent: Any, args: dict, teammates: list, states: dict,
                    delegations: int, created_today: int) -> tuple[str, bool]:
    agent_id = str(args.get("agent", ""))
    goal = str(args.get("task", "")).strip()
    ids = [a.id for a in teammates if a.id != host.chief]
    if agent_id not in ids:
        return f"Refused: choose one of {', '.join(ids)}.", False
    if states.get(agent_id, host.default_state()).paused:
        return f"Refused: {agent_id} is paused by the owner.", False
    if not goal:
        return "Refused: the task was empty.", False
    if delegations >= MAX_DELEGATIONS_PER_TASK:
        return "Refused: delegation limit for this task reached.", False
    if created_today >= DAILY_TASK_CAP:
        return "Refused: the team's daily task limit is reached.", False
    db.add(host.Task(id=uuid.uuid4(), organization_id=parent.organization_id,
                     agent_id=agent_id, parent_id=parent.id, thread=parent.thread,
                     goal=goal, status="queued", state=[], attempts=0,
                     origin=parent.origin))
    return f"Queued for {host.agent(agent_id).name}.", True


async def _ancestors(host, db, task: Any) -> list[str]:
    """Agents up the hand-off chain, nearest first: they are all waiting on this task."""
    Task = host.Task
    out, cur = [], task
    while cur.parent_id and len(out) < MAX_ASK_DEPTH + 3:
        cur = (await db.execute(select(Task).where(
            Task.id == cur.parent_id, Task.organization_id == task.organization_id
        ))).scalar_one_or_none()
        if cur is None:
            break
        out.append(cur.agent_id)
    return out


async def _ask_agent(host, db, parent: Any, target: str, question: str, states: dict,
                     delegations: int, created_today: int,
                     state: gate.AgentState) -> tuple[Optional[dict], bool]:
    """Queue a teammate to answer a question and park the parent until they do.

    Returns (pending, queued). The child is an ordinary task with parent_id set, so the
    sweeper runs it; closing the loop is answer_ask() when it finishes. A refusal comes back
    as an ask_failed pending so the model is told now and can try another route.
    """
    if not question.strip():
        return None, False
    if states.get(target, host.default_state()).paused:
        return {"kind": "ask_failed", "agent": target,
                "error": f"{host.agent(target).name} is paused by the owner."}, False
    if delegations >= MAX_DELEGATIONS_PER_TASK:
        return {"kind": "ask_failed", "agent": target,
                "error": "the hand-off limit for this task is reached"}, False
    if created_today >= DAILY_TASK_CAP:
        return {"kind": "ask_failed", "agent": target,
                "error": "the team's daily task limit is reached"}, False
    if state.paused:
        return None, False
    child = host.Task(id=uuid.uuid4(), organization_id=parent.organization_id,
                      agent_id=target, parent_id=parent.id, thread=parent.thread,
                      goal=question, status="queued", state=[], attempts=0,
                      origin=parent.origin)
    db.add(child)
    return {"kind": "ask_agent", "tool_call_id": "", "agent": target,
            "child_id": str(child.id), "question": question}, True


async def answer_ask(host, db, child: Any, text: str) -> Optional[Any]:
    """Deliver a finished child's answer to the parent waiting on it, and wake the parent.

    Returns the parent if it was waiting, so the caller can re-queue it; None if nobody is
    waiting (a stale child, or the owner answered first). The parent's saved state already
    holds the tool_call placeholder, so the answer lands on the exact call the model made.
    """
    if not child.parent_id:
        return None
    Task = host.Task
    parent = (await db.execute(select(Task).where(
        Task.id == child.parent_id, Task.organization_id == child.organization_id
    ))).scalar_one_or_none()
    if parent is None or parent.status != "waiting":
        return None
    pending = parent.pending or {}
    if pending.get("kind") != "ask_agent" or pending.get("child_id") != str(child.id):
        return None  # the parent moved on, or was already answered
    parent.state = [*(parent.state or []),
                    {"role": "tool", "tool_call_id": pending.get("tool_call_id", ""),
                     "content": f"{host.agent(child.agent_id).name} answered: {text or 'no answer'}"}]
    parent.pending, parent.status, parent.attempts = None, "queued", 0
    return parent


async def _finish(host, db, task: Any, history: list, text: str, actor: str,
                  shown: list) -> str:
    history.append({"role": "assistant", "content": text})
    task.state, task.pending, task.status = history, None, DONE
    await host.finish(db, task, text or "Done.", list(shown), actor)
    return DONE


async def park(host, db, task: Any, history: list, pending: dict, name: str, actor: str,
               channel: Optional[str] = None, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    # Waiting on a teammate is not the owner's business: no code, no text, no card. The
    # conversation in the thread already shows who was asked.
    internal = pending["kind"] == "ask_agent"
    pending = {**pending, "parked_at": now.isoformat()}
    if not internal:
        pending = {**pending, **(await host.owner_fields(db, task))}
    task.state, task.pending, task.status = history, pending, WAITING
    if pending["kind"] == "approval":
        body, summary = describe_pending(pending), \
            f"{name} wants to run {pending_tool(pending)['tool']}."
    elif internal:
        asked = host.agent(pending["agent"]).name
        body, summary = f"Asked {asked}: {pending['question']}", f"{name} is waiting on {asked}."
    else:
        body, summary = pending["question"], f"{name} has a question for you."
    host.post_message(db, organization_id=task.organization_id, thread=task.thread,
                      sender=task.agent_id, body=body, kind=pending["kind"], task_id=task.id)
    await db.flush()
    if internal:
        return WAITING  # a teammate is answering; telling the owner would be noise
    await host.notify_parked(db, task, pending, name, actor, channel, summary, body)
    return WAITING


def stalled(history: list, repeats: int = STALL_REPEATS) -> bool:
    """The last `repeats` model turns asked for exactly the same tool calls."""
    calls = [m["tool_calls"] for m in history
             if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")]
    if len(calls) < repeats:
        return False

    def sig(tool_calls: list) -> list:
        return sorted((c.get("function", {}).get("name"), c.get("function", {}).get("arguments"))
                      for c in tool_calls)
    last = [sig(c) for c in calls[-repeats:]]
    return all(s == last[0] for s in last)


def too_long(history: list) -> bool:
    return len(json.dumps(seen(history), default=str)) > MAX_HISTORY_CHARS


# A long run is mostly old tool results (97% of the longest runs, 2026-11-04) the model has
# already acted on: those are cut to their opening lines before the run is made to wrap up.
KEEP_RESULTS = 6  # the latest results stay whole
RESULT_HEAD = 500
CLEARED = "\n[The rest was cleared to save room. Call the tool again if you need it.]"


def compact(history: list) -> list:
    tools_at = [i for i, m in enumerate(history) if isinstance(m, dict) and m.get("role") == "tool"]
    old = set(tools_at[:-KEEP_RESULTS])

    def cut(msg: dict) -> dict:
        content = msg.get("content")
        if isinstance(content, str) and len(content) > RESULT_HEAD + len(CLEARED):
            return {**msg, "content": content[:RESULT_HEAD] + CLEARED}
        return msg
    return [cut(m) if i in old else m for i, m in enumerate(history)]


# ── Pictures a tool handed the model (taskwarden.tools.look): shown in the next request only ──
MAX_LOOKS = 4
LOOK_TEXT = "The pictures your last tool call returned:"
LOOK_GONE = "(pictures shown to you earlier)"
# Model families that read pictures. A text-only model gets the URLs in words.
# ponytail: a name check; a capability flag when a fifth family needs it.
SEES = ("deepseek", "gpt", "claude", "gemini")


def sees_pictures(model: str) -> bool:
    return any(family in (model or "").lower() for family in SEES)


def seen(history: list) -> list:
    """The history with every picture replaced by a line: a picture is shown once, in the
    request right after the tool that returned it, and never stored (it is large, and a
    signed URL dies)."""
    out = []
    for msg in history:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list) and any(p.get("type") == "image_url" for p in content
                                             if isinstance(p, dict)):
            msg = {**msg, "content": LOOK_GONE}
        out.append(msg)
    return out


async def looks(host, images: list, model: str) -> Optional[dict]:
    """One user message carrying the pictures, after the round's tool messages."""
    if not images:
        return None
    if not sees_pictures(model):
        urls = [i for i in images if isinstance(i, str)]
        return {"role": "user", "content": "Your model can't see pictures. Their addresses: "
                + (", ".join(urls) or "none")} if urls else None
    parts: list[dict] = [{"type": "text", "text": LOOK_TEXT}]
    for image in images[:MAX_LOOKS]:
        if url := await host.picture_url(image):
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return {"role": "user", "content": parts} if len(parts) > 1 else None


async def run_task(host, db, task: Any, *, actor: str = "task",
                   llm: Optional[tuple[Any, str]] = None, now: Optional[datetime] = None,
                   channel: Optional[str] = None, budget_seconds: Optional[float] = None) -> str:
    """One run of a task. With `budget_seconds`, a run still working when the time is up
    checkpoints (status "queued") instead of being cut off mid-tool by its caller's timeout."""
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    org = task.organization_id
    agent = host.agent(task.agent_id)
    if await host.refuse_run(db, task, actor):
        task.status = "failed"
        await db.commit()
        return task.status
    self_started = host.self_started(task)
    ctx, company = await host.context(db, org)
    ctx.self_started = self_started
    states = await host.agent_states(db, org)
    state = states.get(agent.id, host.default_state())
    name = state.display_name or agent.name
    business_type = company.get("business_type")
    teammates = host.agents_for(business_type)
    client, model = llm or await host.llm(db, org)

    agent = host.for_business(agent, business_type)  # skills it can't use here drop out
    history = answer_dangling(list(task.state or [])) or [{"role": "user", "content": task.goal}]
    loaded = loaded_skills(host, agent, task, history)
    allowed = host.tools_of(agent, business_type)  # a skill never adds a tool this list lacks
    offered = (agent.folder.tools_for(loaded) if agent.folder and agent.folder.on_demand
               else allowed)
    # A task that never waits fixes what it finds under the owner's own rules and hands the
    # rest to teammates: no question and no consult; what needs the owner goes in its report.
    never_waits = host.never_waits(task)
    tools = [host.tools[n].schema for n in allowed if n in offered]
    if not never_waits:
        tools.append(ASK_PERSON_SCHEMA)
    if agent.folder and (skill_schema := agent.folder.use_skill_schema(loaded)):
        tools.append(skill_schema)
    tools += host.local_schemas(channel, task)
    chain = await _ancestors(host, db, task)
    if never_waits:
        tools.append(DELEGATE_SCHEMA)
    elif len(chain) < MAX_ASK_DEPTH:  # deeper than this, the task answers; it never hands off again
        if agent.id == host.chief:
            tools += [DELEGATE_SCHEMA, ASK_TEAM_SCHEMA]
        else:
            tools += [ASK_TEAM_SCHEMA, ESCALATE_SCHEMA]
    local_now = now.astimezone(ZoneInfo(ctx.timezone))
    system = {"role": "system", "content": await host.system_prompt(
        db, task, agent, name, teammates, local_now, channel, business_type, self_started, loaded)}
    Task = host.Task
    # Work this task already handed to someone counts, so a loop of hand-offs cannot outrun
    # the caps across approval resumes and retries.
    delegations = (await db.execute(select(func.count()).select_from(Task).where(
        Task.parent_id == task.id))).scalar() or 0
    created_today = (await db.execute(select(func.count()).select_from(Task).where(
        Task.organization_id == org,
        Task.created_at >= now - timedelta(days=1)))).scalar() or 0

    # Did a tool actually run this turn (or hand this resumed run a result), and was it asked to go read.
    grounded, nudged = fresh_results(history), False
    # What this task has already sent, in every run of it: seeded from the row so a
    # park-and-resume cannot send the same person the same thing again.
    region = host.region(company)
    sent_task: set[str] = set(task.contacted or [])
    # What the agent read in this run, raw, by tool (the last read wins), and what it chose to
    # show from it: posted under its answer, dropped if the run parks instead.
    reads: dict[str, Any] = {}
    shown: list[dict] = []
    for round_no in range(MAX_ROUNDS):
        if round_no and too_long(history):
            history = compact(history)
        if round_no and stalled(history, 2) and not any(
                m.get("content") == REPEATED for m in history if isinstance(m, dict)):
            history.append({"role": "user", "content": REPEATED})
        if round_no and (stalled(history) or too_long(history)):
            logger.warning("team_run_wrapped_up task_id=%s rounds=%s stalled=%s",
                           task.id, round_no, stalled(history))
            break
        if round_no and budget_seconds is not None and time.monotonic() - started > budget_seconds:
            # Out of time but still working: save the run and queue the rest. Not a failure,
            # so the failure count starts over.
            task.state, task.status, task.attempts = list(history), "queued", 0
            return "queued"
        await host.mark(org, task.thread, agent.id, THINKING)
        resp = await client.chat.completions.create(
            model=model, extra_body=host.extra_body(model), messages=[system, *history],
            tools=tools, tool_choice="auto")
        history = seen(history)  # the pictures were seen; they are not kept
        msg = resp.choices[0].message
        if not msg.tool_calls:
            text = host.clean_text(msg.content)
            if not grounded and not nudged and states_a_figure(text):
                nudged = True  # one nudge, never a loop
                history += [{"role": "assistant", "content": text},
                            {"role": "user", "content": UNGROUNDED}]
                task.state = list(history)
                continue
            if self_started and not nudged and announces_a_step(text):
                nudged = True  # one nudge per run, whichever it was
                history += [{"role": "assistant", "content": text},
                            {"role": "user", "content": UNFINISHED}]
                task.state = list(history)
                continue
            return await _finish(host, db, task, history, text, actor, shown)
        history.append(assistant_turn(msg))
        parked: Optional[dict] = None
        awaiting: list[dict] = []  # every call in this turn that needs the owner, as one batch
        sent_round: set[str] = set()  # one message per person per response, reworded or not
        pictures: list = []  # what the round's tools handed the model to look at
        for tc in msg.tool_calls:
            if parked is not None:
                history.append(tool_msg(tc.id, "Skipped: waiting on the owner for an "
                                               "earlier step. Call it again later if still needed."))
                continue
            tool_name, args = tc.function.name, parse(tc.function.arguments)
            if never_waits and tool_name not in {t["function"]["name"] for t in tools}:
                history.append(tool_msg(tc.id, host.NEVER_WAITS_REFUSED))
                continue
            if tool_name == "ask_person":
                host.audit(db, organization_id=org, agent_id=agent.id, tool=tool_name,
                           risk=None, decision="allow", rule="asked_owner",
                           task_id=task.id, actor=actor)
                parked = {"kind": "question", "tool_call_id": tc.id,
                          "question": str(args.get("question", "")),
                          "why": str(args.get("why", "")),
                          "choices": choices(args.get("choices"))}
                continue
            if tool_name in ("delegate", "consult", "escalate") and state.setting == gate.OBSERVE:
                # A teammate would act for real: in practice the hand-off is only recorded.
                host.audit(db, organization_id=org, agent_id=agent.id, tool=tool_name,
                           risk=None, decision=gate.OBSERVE, rule="practice",
                           task_id=task.id, actor=actor)
                history.append(tool_msg(tc.id, PRACTICE.format(call=describe_call(tool_name, args))))
                continue
            if tool_name == "delegate" and agent.id == host.chief:
                if state.paused:
                    out, queued, rule = "Refused: agent_paused.", False, "agent_paused"
                else:
                    await host.mark(org, task.thread, agent.id, f"delegate:{args.get('agent', '')}")
                    out, queued = await _delegate(host, db, task, args, teammates, states,
                                                  delegations, created_today)
                    if queued:
                        created_today += 1
                    rule = "delegated" if queued else "delegate_refused"
                delegations += int(queued)
                host.audit(db, organization_id=org, agent_id=agent.id, tool=tool_name,
                           risk=None, decision="allow" if queued else "deny",
                           rule=rule, task_id=task.id, actor=actor)
                history.append(tool_msg(tc.id, out))
                continue
            if tool_name in ("consult", "escalate"):
                # Agent-to-agent: a teammate answers, the owner is not involved unless the
                # team cannot settle it (the model then uses ask_person).
                target = host.chief if tool_name == "escalate" else str(args.get("agent", ""))
                question = str(args.get("question", "")).strip()
                known = [a.id for a in teammates if a.id != agent.id and a.id not in chain]
                if target not in known:
                    out = ("Refused: escalate is for specialists to bring a decision to the "
                           "Chief." if tool_name == "escalate" and agent.id == host.chief
                           else f"Refused: {target} is already waiting on this task. Finish it "
                                f"yourself, or say what is missing." if target in chain
                           else f"Refused: '{target}' is not a teammate you can ask.")
                    history.append(tool_msg(tc.id, out))
                    host.audit(db, organization_id=org, agent_id=agent.id, tool=tool_name,
                               risk=None, decision="deny", rule="ask_target_unknown",
                               task_id=task.id, actor=actor)
                    continue
                await host.mark(org, task.thread, agent.id, f"{tool_name}:{target}")
                pending_ask, queued = await _ask_agent(
                    host, db, task, target, question, states, delegations, created_today, state)
                if queued:
                    created_today += 1
                delegations += int(queued)
                host.audit(db, organization_id=org, agent_id=agent.id, tool=tool_name,
                           risk=None, decision="allow" if queued else "deny",
                           rule=f"{tool_name}d" if queued else f"{tool_name}_refused",
                           task_id=task.id, actor=actor)
                if pending_ask is not None and pending_ask["kind"] == "ask_failed":
                    history.append(tool_msg(tc.id, f"Refused: {pending_ask['error']}."))
                    continue
                if not queued:
                    history.append(tool_msg(tc.id, "Refused: agent_paused."))
                    continue
                pending_ask["tool_call_id"] = tc.id
                parked = pending_ask  # wait for the teammate, not the owner
                continue
            local = await host.local_call(db, org, tool_name, args, reads, shown)
            if local is not None:
                history.append(tool_msg(tc.id, local))
                continue
            if tool_name == USE_SKILL and agent.folder is not None:
                out, added = host.use_skill(agent, args, loaded)
                have = {t["function"]["name"] for t in tools}
                tools[:0] = [host.tools[n].schema for n in allowed if n in added and n not in have]
                history.append(tool_msg(tc.id, out))
                continue
            tool = host.tools.get(tool_name)
            decision = host.decide(agent, tool, state, self_started=self_started)
            target = next((str(args[k])[:320] for k in ("to", "phone_number", "contact_info")
                           if isinstance(args, dict) and args.get(k)), None)
            host.audit(db, organization_id=org, agent_id=agent.id, tool=tool_name,
                       risk=tool.risk if tool else None, decision=decision.outcome,
                       rule=decision.rule, task_id=task.id, actor=actor, target=target)
            reach = host.reach_key(tool_name, args, region)
            sent = host.sent_key(tool_name, args, region)
            if decision.outcome in (gate.ASK, gate.ALLOW) and (
                    (sent is not None and sent in sent_task)
                    or (reach is not None and reach in sent_round)):
                host.audit(db, organization_id=org, agent_id=agent.id, tool=tool_name,
                           risk=tool.risk if tool else None, decision="deny",
                           rule="duplicate_send", task_id=task.id, actor=actor)
                history.append(tool_msg(tc.id, "Refused: you already messaged this person in "
                                               "this turn. Do not send it again."))
                continue
            if (decision.outcome in (gate.ASK, gate.ALLOW) and tool is not None
                    and tool.check is not None):
                refusal = await host.precheck(tool, ctx, args)
                if refusal:
                    # Can't work as it stands: say why now, instead of an approval card the
                    # owner would approve only to see it fail, or a send that should not go.
                    host.audit(db, organization_id=org, agent_id=agent.id, tool=tool_name,
                               risk=tool.risk, decision="deny", rule="precheck",
                               task_id=task.id, actor=actor)
                    history.append(tool_msg(tc.id, refusal))
                    continue
            if decision.outcome == gate.OBSERVE:
                history.append(tool_msg(tc.id, PRACTICE.format(
                    call=describe_call(tool_name, host.clean_args(args)))))
                continue
            if decision.outcome == gate.ASK and never_waits:
                history.append(tool_msg(tc.id, host.NEVER_WAITS_NEEDS_OWNER))
                continue
            if decision.outcome == gate.ASK:
                if reach is not None:
                    # Parked: the owner's one click runs the whole batch, so a second send to
                    # the same person would be a second message, not a retry. Not recorded as
                    # sent -- a declined batch sent nothing.
                    sent_round.add(reach)
                # One owner click on the whole batch beats N clicks. A tool message is still
                # owed for every call id, or the next request carries an unanswered call.
                awaiting.append({"tool_call_id": tc.id, "tool": tool_name,
                                 "args": host.clean_args(args)})
                continue
            if decision.outcome == gate.ALLOW:
                await host.mark(org, task.thread, agent.id, tool_name)
                if sent is not None:
                    # Saved before it runs: a crash mid-send must not lead to a second send.
                    task.state = [*history, tool_msg(tc.id, MAYBE_SENT)]
                    task.contacted = sorted(sent_task | {sent})
                    await db.commit()
                with capture_looks() as looked:
                    out, ok, raw = await host.execute(tool, ctx, args)
                pictures += looked
                if tool.risk == READ:
                    if ok and raw is not None:
                        reads[tool_name] = raw
                    else:  # a read that failed must not leave an older one to be shown
                        reads.pop(tool_name, None)
                if ok:
                    grounded = True
                if ok is not False:  # None: stopped at its timeout, it may have gone out
                    if reach is not None:
                        # Only a send that (maybe) went out counts: a rejected one leaves
                        # the retry open, which is the whole point of retrying.
                        sent_round.add(reach)
                    if sent is not None:
                        sent_task.add(sent)
                if not ok:
                    host.audit(db, organization_id=org, agent_id=agent.id,
                               tool=tool_name, risk=tool.risk, decision="error",
                               rule="handler_failed", task_id=task.id, actor=actor)
            else:
                out = f"Refused: {decision.rule}."
            history.append(tool_msg(tc.id, out))
        if parked is None and not awaiting and (look := await looks(host, pictures, model)):
            # Only when the run goes on: a parked run may resume after the pictures expired.
            history.append(look)
        task.state, task.contacted = list(history), sorted(sent_task)
        if parked is None and not awaiting:
            await db.commit()  # the round's progress survives a restart
        if parked is not None:
            # A question wins over a batch in the same response: the answer may change what to
            # send. Each held call still needs its tool message; the model proposes it again.
            for call in awaiting:
                history.append(tool_msg(call["tool_call_id"],
                                        "Not sent: you asked a question first. Propose this "
                                        "again once you have the answer."))
            task.state = list(history)
            return await park(host, db, task, history, parked, name, actor, channel, now)
        if awaiting:
            batch = awaiting[0] if len(awaiting) == 1 else {"kind": "approval", "batch": awaiting}
            batch.setdefault("kind", "approval")
            for call in awaiting:
                # Placeholder until the owner decides; the decision overwrites these.
                history.append(tool_msg(call["tool_call_id"], "Waiting for your approval."))
            task.state = list(history)
            return await park(host, db, task, history, batch, name, actor, channel, now)

    wrap_up = {"role": "user", "content": WRAP_UP}
    resp = await client.chat.completions.create(model=model, extra_body=host.extra_body(model),
                                                messages=[system, *history, wrap_up])
    text = resp.choices[0].message.content or ""
    return await _finish(host, db, task, history, host.clean_text(text) or host.OUT_OF_STEPS,
                         actor, shown)

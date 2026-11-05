"""taskwarden on its own: it never imports an app, and a host that is not Perbene can run a team.

The host here is the smallest one that works: one agent, two tools, a fake model and a mock
session. Perbene's own behaviour is pinned by its suite (tests/test_team_runner.py and the
rest), which runs the same loop through app/team/runner.py:PerbeneHost.
"""
import ast
import asyncio
import json
import pathlib
import uuid

import pytest
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.orm import DeclarativeBase

from taskwarden import decisions, gate, loop, tools
from taskwarden.models import MessageColumns, TaskColumns

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "taskwarden"


def test_the_package_never_imports_an_app():
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level
                     else [])
            assert not [n for n in names if n == "app" or n.startswith("app.")], path


# ── A host that is not Perbene ──

@dataclass
class Agent:
    id: str
    name: str
    title: str = "Helper"
    tools: tuple = ("read_orders", "refund")
    folder: object = None


class Base(DeclarativeBase):
    pass


class Task(Base, TaskColumns):
    """A new app's table, straight from the mixin. The mock session never runs its queries."""
    __tablename__ = "shop_tasks"


class Message(Base, MessageColumns):
    __tablename__ = "shop_messages"


async def _orders(ctx, **_):
    return "2 orders today"


async def _refund(ctx, order_id: str):
    return f"Refunded {order_id}"


@dataclass
class ShopHost:
    """A web shop's team: reads orders freely, refunds (SPEND) only with the owner's click."""
    Task = Task
    chief = "lead"
    tools: dict = field(default_factory=lambda: {
        "read_orders": tools.Tool("read_orders", tools.READ, tools.fn_schema("read_orders", "Orders"), _orders),
        "refund": tools.Tool("refund", tools.SPEND, tools.fn_schema(
            "refund", "Refund an order", {"order_id": {"type": "string"}}, ("order_id",)), _refund),
    })
    posted: list = field(default_factory=list)
    told: list = field(default_factory=list)
    NEVER_WAITS_REFUSED = "Refused."
    NEVER_WAITS_NEEDS_OWNER = "Needs the owner."
    OUT_OF_STEPS = "Out of steps."

    def agent(self, agent_id): return Agent(agent_id, agent_id.title())
    def agents_for(self, business_type): return [Agent("lead", "Lead")]
    def tools_of(self, agent, business_type): return agent.tools
    def for_business(self, agent, business_type): return agent
    async def refuse_run(self, db, task, actor): return False
    async def context(self, db, org): return SimpleNamespace(db=None, organization_id=org, timezone="UTC"), {}
    async def agent_states(self, db, org): return {}
    def default_state(self): return gate.AgentState()
    async def llm(self, db, org): raise AssertionError("the test passes its model")
    def extra_body(self, model): return None
    def clean_text(self, text): return text or ""
    def self_started(self, task): return False
    def never_waits(self, task): return False
    def preloaded_skills(self, agent, task): return set()
    async def system_prompt(self, db, task, agent, *a): return "You run a small shop's orders."
    def local_schemas(self, channel, task): return []
    async def local_call(self, *a): return None
    def region(self, company): return "US"
    def decide(self, agent, tool, state, *, self_started):
        return gate.decide(agent, tool, state, self_started=self_started)
    async def precheck(self, tool, ctx, args): return None
    def clean_args(self, args): return tools.clean_args(args)
    def use_skill(self, agent, args, loaded): return loop.use_skill(agent, args, loaded)
    async def execute(self, tool, ctx, args):
        out, ok = await tools.execute(tool, ctx, args, scope=lambda c: nullcontext())
        return out, ok, None
    def audit(self, db, **kw): pass
    def reach_key(self, *a): return None
    def sent_key(self, *a): return None
    async def picture_url(self, image): return None
    async def mark(self, *a): pass
    def post_message(self, db, **kw): self.posted.append(kw)
    async def finish(self, db, task, body, shown, actor): self.posted.append({"body": body})
    async def decision_names(self, db, org, decided): return {}
    async def owner_fields(self, db, task): return {}
    async def notify_parked(self, db, task, pending, *a): self.told.append(pending["kind"])


def _llm(*messages):
    replies = list(messages)

    async def create(**kw):
        return SimpleNamespace(choices=[SimpleNamespace(message=replies.pop(0))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class Msg:
    def __init__(self, content=None, calls=()):
        self.content = content
        self.tool_calls = [SimpleNamespace(id=f"c{i}", function=SimpleNamespace(
            name=n, arguments=json.dumps(a))) for i, (n, a) in enumerate(calls)] or None

    def model_dump(self, exclude_none=False):
        out = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [{"id": c.id, "type": "function", "function": {
                "name": c.function.name, "arguments": c.function.arguments}} for c in self.tool_calls]
        return out


def _db():
    db = MagicMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar=lambda: 0,
                                                        scalar_one_or_none=lambda: None))
    db.flush, db.commit = AsyncMock(), AsyncMock()
    return db


def _task():
    return Task(id=uuid.uuid4(), organization_id="shop1", agent_id="lead", thread="team",
                goal="How are orders today?", status="running", state=[], attempts=1,
                pending=None, contacted=[], origin="owner")


async def test_a_read_runs_and_the_report_is_posted():
    host, task = ShopHost(), _task()
    llm = _llm(Msg(calls=[("read_orders", {})]), Msg("Two orders came in today."))
    assert await loop.run_task(host, _db(), task, llm=(llm, "m")) == "done"
    assert host.posted[-1] == {"body": "Two orders came in today."}
    assert task.state[2] == {"role": "tool", "tool_call_id": "c0", "content": "2 orders today"}


async def test_a_spend_parks_for_the_owner_and_their_approval_runs_it():
    host, task, db = ShopHost(), _task(), _db()
    llm = _llm(Msg(calls=[("refund", {"order_id": "A7", "organization_id": "someone-else"})]))
    assert await loop.run_task(host, db, task, llm=(llm, "m")) == "waiting"
    assert task.pending["tool"] == "refund" and task.pending["args"] == {"order_id": "A7"}
    assert host.told == ["approval"]

    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: task))
    await decisions.apply(host, db, "shop1", task, "approve")
    assert task.status == "queued"
    assert {"role": "tool", "tool_call_id": "c0", "content": "Refunded A7"} in task.state


async def test_in_practice_reads_run_and_everything_else_is_only_recorded():
    host, task = ShopHost(), _task()
    host.agent_states = AsyncMock(return_value={"lead": gate.AgentState(setting=gate.OBSERVE)})
    host.audit = MagicMock()
    llm = _llm(Msg(calls=[("read_orders", {}), ("refund", {"order_id": "A7"})]),
               Msg("I would refund A7."))
    assert await loop.run_task(host, _db(), task, llm=(llm, "m")) == "done"
    results = [m["content"] for m in task.state if m["role"] == "tool"]
    assert results[0] == "2 orders today"
    assert results[1].startswith("Practice mode: not done") and "order_id: A7" in results[1]
    assert host.told == []  # nothing parked for the owner
    assert [c.kwargs["decision"] for c in host.audit.call_args_list] == ["allow", "observe"]


# ── A restart mid-send never sends twice ──

async def _text(ctx, to: str, body: str):
    return f"Sent to {to}."


@dataclass
class TextingHost(ShopHost):
    def __post_init__(self):
        self.tools["send_text"] = tools.Tool("send_text", tools.ROUTINE, tools.fn_schema(
            "send_text", "Text a customer", {"to": {"type": "string"}, "body": {"type": "string"}},
            ("to", "body")), _text)

    def sent_key(self, tool_name, args, region):
        return f"send_text|{args.get('to')}|{args.get('body')}" if tool_name == "send_text" else None


def test_a_call_left_unanswered_by_a_restart_is_answered_in_place():
    call = lambda i: {"id": i, "type": "function", "function": {"name": "read_orders", "arguments": "{}"}}
    history = [{"role": "user", "content": "go"},
               {"role": "assistant", "content": None, "tool_calls": [call("a"), call("b")]},
               {"role": "tool", "tool_call_id": "a", "content": "done"},
               {"role": "user", "content": "next"}]
    assert loop.answer_dangling(history) == [
        *history[:3], {"role": "tool", "tool_call_id": "b", "content": loop.NOT_RUN}, history[3]]
    assert loop.answer_dangling(history[:1]) == history[:1]


async def test_a_send_is_saved_as_maybe_sent_before_it_runs_and_a_resumed_run_does_not_repeat_it():
    host, task, db = TextingHost(), _task(), _db()
    host.agent = lambda agent_id: Agent(agent_id, "Lead", tools=("read_orders", "send_text"))
    send = {"to": "+15145550142", "body": "Tech at 10?"}
    saved = {}

    async def crash(tool, ctx, args):
        saved.update(state=list(task.state), contacted=list(task.contacted),
                     commits=db.commit.await_count)
        raise SystemExit  # the process dies mid-send
    host.execute = crash
    try:
        await loop.run_task(host, db, task, llm=(_llm(Msg(calls=[("send_text", send)])), "m"))
    except SystemExit:
        pass
    assert saved["contacted"] == ["send_text|+15145550142|Tech at 10?"]
    assert saved["state"][-1] == {"role": "tool", "tool_call_id": "c0", "content": loop.MAYBE_SENT}
    assert saved["commits"] >= 1  # committed before the handler ran

    # The sweeper resumes from what was committed; the model tries the same text again.
    task.state, task.contacted = saved["state"], saved["contacted"]
    host.execute = AsyncMock(side_effect=AssertionError("sent twice"))
    llm = _llm(Msg(calls=[("send_text", send)]), Msg("She may already have it."))
    assert await loop.run_task(host, db, task, llm=(llm, "m")) == "done"
    assert any(m.get("role") == "tool" and m["content"].startswith(
        "Refused: you already messaged this person") for m in task.state)


# ── A tool that hangs is stopped, and a stopped send still counts as sent ──

async def _hang(ctx, **kw):
    await asyncio.sleep(10)


async def test_a_slow_read_is_stopped_and_the_model_is_told():
    tool = tools.Tool("slow", tools.READ, tools.fn_schema("slow", "x"), _hang, timeout=0.01)
    assert await tools.execute(tool, SimpleNamespace(), {}) == (tools.SLOW, False)


async def test_a_send_stopped_at_its_timeout_counts_as_sent():
    host, task, db = TextingHost(), _task(), _db()
    host.tools["send_text"] = replace(host.tools["send_text"], handler=_hang, timeout=0.01)
    host.agent = lambda agent_id: Agent(agent_id, "Lead", tools=("send_text",))
    send = {"to": "+15145550142", "body": "Tech at 10?"}
    llm = _llm(Msg(calls=[("send_text", send)]), Msg(calls=[("send_text", send)]),
               Msg("It may have reached her."))
    assert await loop.run_task(host, db, task, llm=(llm, "m")) == "done"
    results = [m["content"] for m in task.state if m["role"] == "tool"]
    assert results[0] == tools.SLOW_MAYBE_DONE
    assert results[1].startswith("Refused: you already messaged this person")
    assert task.contacted == ["send_text|+15145550142|Tech at 10?"]


# ── The owner rewrites a message before approving it ──

def test_only_text_the_call_already_had_can_be_edited():
    args = {"to": "+1514", "message": "Tech at 10?", "count": 2}
    out, changed = decisions.edited(args, {"message": "Tech at 11?", "count": "9", "cc": "x",
                                           "to": "+1514"})
    assert out == {"to": "+1514", "message": "Tech at 11?", "count": 2}
    assert changed == ["message"]


async def test_an_edited_approval_runs_as_edited_and_the_agent_is_told():
    host, task, db = ShopHost(), _task(), _db()
    host.audit = MagicMock()
    llm = _llm(Msg(calls=[("refund", {"order_id": "A7"})]))
    assert await loop.run_task(host, db, task, llm=(llm, "m")) == "waiting"
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: task))
    await decisions.apply(host, db, "shop1", task, "approve",
                          edited_args={"c0": {"order_id": "A8"}})
    result = next(m["content"] for m in task.state if m.get("tool_call_id") == "c0")
    assert result == decisions.EDITED.format(fields="order_id") + "Refunded A8"
    assert host.audit.call_args.kwargs["rule"] == "owner_edited"
    assert json.loads(host.posted[-1]["body"])["calls"][0]["args"] == {"order_id": "A8"}


async def _park_refund(host):
    task, db = _task(), _db()
    assert await loop.run_task(host, db, task, llm=(_llm(Msg(calls=[("refund", {"order_id": "A7"})])), "m")) == "waiting"
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: task))
    host.execute = AsyncMock(side_effect=AssertionError("must not run"))
    host.audit = MagicMock()
    return task, db


async def test_an_approval_is_checked_again_on_the_args_the_owner_edited():
    host = ShopHost()
    host.tools["refund"] = replace(host.tools["refund"], check=AsyncMock())
    host.precheck = AsyncMock(side_effect=lambda tool, ctx, args:
                              "Order A8 was already refunded." if args["order_id"] == "A8" else None)
    task, db = await _park_refund(host)
    await decisions.apply(host, db, "shop1", task, "approve", edited_args={"c0": {"order_id": "A8"}})
    assert {"role": "tool", "tool_call_id": "c0", "content": "Order A8 was already refunded."} in task.state
    assert host.audit.call_args.kwargs["rule"] == "precheck"


async def test_an_approval_for_an_agent_paused_while_it_waited_does_not_run():
    host = ShopHost()
    task, db = await _park_refund(host)
    host.agent_states = AsyncMock(return_value={"lead": gate.AgentState(paused=True)})
    await decisions.apply(host, db, "shop1", task, "approve")
    assert {"role": "tool", "tool_call_id": "c0", "content": decisions.NOT_ALLOWED} in task.state
    assert host.audit.call_args.kwargs["rule"] == "no_longer_allowed"
    assert task.status == "queued"  # the agent reads why and reports


# ── A long run sheds its old tool results before it is made to wrap up ──

def test_old_results_are_cut_to_their_opening_and_the_latest_stay_whole():
    history = [{"role": "user", "content": "go"}] + [
        {"role": "tool", "tool_call_id": f"c{i}", "content": f"{i}" + "x" * 5000} for i in range(8)]
    out = loop.compact(history)
    assert out[0] == history[0] and out[3:] == history[3:]  # the 6 latest results untouched
    assert out[1]["content"] == history[1]["content"][:loop.RESULT_HEAD] + loop.CLEARED
    assert loop.compact(out) == out  # cutting twice changes nothing


async def test_a_run_too_long_for_the_model_carries_on_once_compacted(monkeypatch):
    host, task = ShopHost(), _task()
    big = "orders: " + "x" * 9000
    task.state = [{"role": "user", "content": "How are orders today?"}]
    for i in range(8):
        task.state += [{"role": "assistant", "content": None, "tool_calls": [{
            "id": f"o{i}", "type": "function", "function": {"name": "read_orders", "arguments": json.dumps({"page": i})}}]},
                       {"role": "tool", "tool_call_id": f"o{i}", "content": big}]  # not a loop
    monkeypatch.setattr(loop, "MAX_HISTORY_CHARS", 60_000)  # over it whole, under it compacted
    llm = _llm(Msg(calls=[("read_orders", {})]), Msg("Two orders came in today."))
    asked = []
    create = llm.chat.completions.create
    llm.chat.completions.create = lambda **kw: asked.append(kw) or create(**kw)
    assert await loop.run_task(host, _db(), task, llm=(llm, "m")) == "done"
    assert host.posted[-1] == {"body": "Two orders came in today."}
    assert all("tools" in kw for kw in asked)  # never the wrap-up, which offers no tools


# ── A repeated call gets one reminder before the run is stopped ──

async def test_the_first_repeat_is_pointed_out_once_and_the_model_can_change_course():
    host, task = ShopHost(), _task()
    llm = _llm(Msg(calls=[("read_orders", {})]), Msg(calls=[("read_orders", {})]),
               Msg("Two orders came in today."))
    assert await loop.run_task(host, _db(), task, llm=(llm, "m")) == "done"
    reminders = [m for m in task.state if m.get("content") == loop.REPEATED]
    assert len(reminders) == 1 and task.state[-2] == reminders[0]  # right before the answer
    assert host.posted[-1] == {"body": "Two orders came in today."}


# ── The example in the README runs, start to finish, on a real database ──

def test_the_shop_example_runs(capsys):
    pytest.importorskip("aiosqlite")
    import runpy
    runpy.run_path(str(pathlib.Path(__file__).parents[1] / "examples" / "shop.py"),
                   run_name="__main__")
    out = capsys.readouterr().out
    assert "waits: refund {'order_id': 'A7'}" in out
    assert "[lead] Done: I refunded order A8, $19.50." in out


# ── A run's tokens are logged, with what the provider had cached ──

def test_usage_counts_deepseek_and_openai_cache_hits():
    u = loop.Usage()
    u.add(SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=50,
                                                prompt_cache_hit_tokens=900)))  # DeepSeek
    u.add(SimpleNamespace(usage=SimpleNamespace(prompt_tokens=800, completion_tokens=20,
                                                prompt_tokens_details=SimpleNamespace(cached_tokens=640))))  # OpenAI
    u.add(SimpleNamespace())  # no usage reported
    assert (u.requests, u.prompt, u.cached, u.completion) == (3, 1800, 1540, 70)


async def test_a_run_logs_its_usage_however_it_ends(caplog):
    host, task = ShopHost(), _task()
    llm = _llm(Msg(calls=[("read_orders", {})]), Msg("Two orders came in today."))
    with caplog.at_level("INFO", logger="taskwarden.loop"):
        assert await loop.run_task(host, _db(), task, llm=(llm, "m")) == "done"
    assert any("taskwarden_run_usage" in r.getMessage() and "requests=2" in r.getMessage()
               for r in caplog.records)


class ClockHost(ShopHost):
    def clock(self, now): return f"It is {now:%H:%M}."


async def test_the_clock_tops_every_request_and_is_never_stored_or_in_the_system_prompt():
    host, task, sent = ClockHost(), _task(), []
    replies = [Msg(calls=[("read_orders", {})]), Msg("Two orders.")]

    async def create(**kw):
        sent.append(kw["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(message=replies.pop(0))])
    llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    now = datetime(2026, 11, 5, 9, 30, tzinfo=timezone.utc)
    assert await loop.run_task(host, _db(), task, llm=(llm, "m"), now=now) == "done"
    assert [m[1]["content"] for m in sent] == ["It is 09:30.\n\nHow are orders today?"] * 2
    assert all("09:30" not in m[0]["content"] for m in sent)
    assert task.state[0] == {"role": "user", "content": "How are orders today?"}


@dataclass
class StoppableHost(TextingHost):
    stop_after: int = 0  # Stop is pressed once this many texts went out
    texts: int = 0

    async def stop_asked(self, task):
        return self.texts >= self.stop_after

    async def execute(self, tool, ctx, args):
        self.texts += tool.name == "send_text"
        return await super().execute(tool, ctx, args)


async def test_a_stop_pressed_before_the_run_ends_it_before_the_model_is_asked():
    host, task = StoppableHost(stop_after=0), _task()
    assert await loop.run_task(host, _db(), task, llm=(_llm(), "m")) == "stopped"
    assert task.status == "stopped" and host.posted[-1]["body"] == "Stopped."


async def test_a_stop_mid_round_keeps_what_was_sent_and_runs_nothing_more():
    host, task = StoppableHost(stop_after=1), _task()
    host.agent = lambda agent_id: Agent(agent_id, "Lead", tools=("read_orders", "send_text"))
    llm = _llm(Msg(calls=[("send_text", {"to": "+1555", "body": "Hi"}),
                          ("send_text", {"to": "+1666", "body": "Hi"})]))
    assert await loop.run_task(host, _db(), task, llm=(llm, "m")) == "stopped"
    assert host.texts == 1
    assert host.posted[-1]["body"] == "Stopped. Done before that: send_text (+1555)."
    tool_results = [m["content"] for m in task.state if m.get("role") == "tool"]
    assert tool_results == ["Sent to +1555.", loop.STOPPED_BEFORE]  # every call answered


@dataclass
class ChattyHost(ShopHost):
    inbox: list = field(default_factory=list)  # (look number, text): what the owner sends when
    looks: int = 0

    async def take_messages(self, task):
        self.looks += 1
        return [text for at, text in self.inbox if at == self.looks]


def _recording(*replies):
    sent, queue = [], list(replies)

    async def create(**kw):
        sent.append(kw["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(message=queue.pop(0))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), sent


async def test_a_message_sent_mid_run_reaches_the_next_round():
    host, task = ChattyHost(inbox=[(2, "Only count paid ones.")]), _task()
    llm, sent = _recording(Msg(calls=[("read_orders", {})]), Msg("One paid order."))
    assert await loop.run_task(host, _db(), task, llm=(llm, "m")) == "done"
    assert sent[1][-1] == {"role": "user", "content": "The owner adds, while you work: Only count paid ones."}
    assert host.posted[-1] == {"body": "One paid order."}


async def test_a_message_sent_as_the_reply_was_written_gets_one_reply_covering_both():
    host, task = ChattyHost(inbox=[(2, "And yesterday?")]), _task()
    llm, sent = _recording(Msg("Two orders today."), Msg("Two today, three yesterday."))
    assert await loop.run_task(host, _db(), task, llm=(llm, "m")) == "done"
    assert [m["content"] for m in sent[1][-2:]] == [
        "Two orders today.", "The owner adds, while you work: And yesterday?"]
    assert [p["body"] for p in host.posted] == ["Two today, three yesterday."]


class Thinking(Msg):
    def __init__(self, reasoning, **kw):
        super().__init__(**kw)
        self.reasoning = reasoning

    def model_dump(self, exclude_none=False):
        return {**super().model_dump(exclude_none), "reasoning_content": self.reasoning}


@dataclass
class ThinkingHost(ShopHost):
    thoughts: list = field(default_factory=list)

    async def thought(self, org, thread, agent_id, text):
        self.thoughts.append((agent_id, text))


async def test_the_models_thinking_is_handed_to_the_host_as_it_works():
    host, task = ThinkingHost(), _task()
    llm = _llm(Thinking("Read the orders first.", calls=[("read_orders", {})]), Msg("Two orders."))
    assert await loop.run_task(host, _db(), task, llm=(llm, "m")) == "done"
    assert host.thoughts == [("lead", "Read the orders first.")]  # a turn with none says nothing


def test_a_picture_seen_once_leaves_the_words_it_came_with():
    owner = {"role": "user", "content": [{"type": "text", "text": "Post this photo"},
                                         {"type": "image_url", "image_url": {"url": "data:x"}}]}
    assert loop.seen([owner]) == [{"role": "user", "content": f"Post this photo\n\n{loop.LOOK_GONE}"}]
    look = {"role": "user", "content": [{"type": "text", "text": loop.LOOK_TEXT},
                                        {"type": "image_url", "image_url": {"url": "data:x"}}]}
    assert loop.seen([look]) == [{"role": "user", "content": loop.LOOK_GONE}]


async def test_a_model_that_cant_see_is_never_sent_a_picture():
    host, task = ShopHost(), _task()
    task.state = [{"role": "user", "content": [{"type": "text", "text": "What is this?"},
                                               {"type": "image_url", "image_url": {"url": "data:x"}}]}]
    llm, sent = _recording(Msg("A receipt."))
    assert await loop.run_task(host, _db(), task, llm=(llm, "llama-3")) == "done"
    assert "image_url" not in json.dumps(sent[0])

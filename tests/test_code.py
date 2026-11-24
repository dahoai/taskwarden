"""Code mode: a script calls the agent's read tools through the gate, in a Monty sandbox."""
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("pydantic_monty")

from taskwarden import code, gate, loop, tools  # noqa: E402
from test_taskwarden import Agent, Msg, ShopHost, _db, _llm, _task  # noqa: E402

def _runner():
    return code.MontyRunner(max_seconds=2, max_calls=20, request_timeout=30)


async def _leads(ctx, status: str = "all"):
    return "3 leads"


@dataclass
class ScriptHost(ShopHost):
    """The shop with code mode on, a read that returns data, and a delete."""
    code_runner: object = field(default_factory=_runner)

    def __post_init__(self):
        self.tools["list_leads"] = tools.Tool("list_leads", tools.READ, tools.fn_schema(
            "list_leads", "List leads. Filters by status.", {"status": {"type": "string"}}),
            _leads)
        self.tools["delete_lead"] = tools.Tool("delete_lead", tools.REMOVE, tools.fn_schema(
            "delete_lead", "Delete a lead", {"lead_id": {"type": "string"}}, ("lead_id",)),
            AsyncMock(return_value="Deleted"))
        self.ran: list = []
        self.audit = MagicMock()

    def code_mode(self, agent, task):
        return True

    def agent(self, agent_id):
        return Agent(agent_id, "Lead", tools=("read_orders", "refund", "list_leads", "delete_lead"))

    async def execute(self, tool, ctx, args):
        self.ran.append((tool.name, args))
        if tool.name == "list_leads":
            data = [{"name": n, "status": args.get("status"), "value": v}
                    for n, v in (("Ana", 100), ("Ben", 250), ("Cy", 75))]
            return "3 leads", True, data
        out, ok = await tools.execute(tool, ctx, args)
        return out, ok, None


async def _script(source, host=None):
    host, task = host or ScriptHost(), _task()
    llm = _llm(Msg(calls=[("run_code", {"code": source})]), Msg("Done."))
    assert await loop.run_task(host, _db(), task, llm=(llm, "m")) == "done"
    return host, next(m["content"] for m in task.state if m.get("role") == "tool")


async def test_a_script_reads_through_the_gate_and_only_its_result_comes_back():
    source = ("import asyncio\n"
              "a, b = await asyncio.gather(list_leads(status='new'), list_leads(status='won'))\n"
              "print('seen', len(a) + len(b))\n"
              "sum(l['value'] for l in a if l['value'] > 80)")
    host, out = await _script(source)
    assert out == "seen 6\nResult: 350\n(2 tool calls)"
    assert host.ran == [("list_leads", {"status": "new"}), ("list_leads", {"status": "won"})]
    rules = [c.kwargs["rule"] for c in host.audit.call_args_list]
    assert rules == ["safe_action_from_code", "safe_action_from_code", "code_ran"]


@pytest.mark.parametrize("name", ["delete_lead", "refund", "send_text", "read_orders_x"])
async def test_a_write_an_approval_or_an_unknown_name_never_runs_from_a_script(name):
    host, out = await _script(f"await {name}(lead_id='1', order_id='1')")
    assert host.ran == []
    assert "Error:" in out
    if name in ("delete_lead", "refund"):
        assert "make it a normal tool call" in out
        assert [c.kwargs["rule"] for c in host.audit.call_args_list] == [
            "code_reads_only", "code_failed"]


async def test_a_paused_agent_runs_nothing_from_a_script():
    host = ScriptHost()
    host.agent_states = AsyncMock(return_value={"lead": gate.AgentState(paused=True)})
    host, out = await _script("await list_leads()", host)
    assert host.ran == [] and "make it a normal tool call" in out


@pytest.mark.parametrize("source", [
    "import os\nos.getenv('HOME')",
    "open('/etc/passwd').read()",
    "__import__('subprocess')",
    "import socket",
    "while True:\n    pass",
    "x = [0] * (10 ** 9)",
    "for i in range(50):\n    await list_leads()",
])
async def test_the_sandbox_reaches_nothing_and_stops_at_its_limits(source):
    host, out = await _script(source)
    assert out.startswith("Error:") or "\nError:" in out
    # A limit hit leaves the next script working.
    _, again = await _script("1 + 1")
    assert again == "Result: 2\n(0 tool calls)"


async def test_run_code_is_offered_only_when_the_host_turns_it_on():
    seen = {}

    def capture(host):
        async def create(**kw):
            seen["tools"] = [t["function"]["name"] for t in kw["tools"]]
            from types import SimpleNamespace
            return SimpleNamespace(choices=[SimpleNamespace(message=Msg("ok"))])
        from types import SimpleNamespace
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    on = ScriptHost()
    await loop.run_task(on, _db(), _task(), llm=(capture(on), "m"))
    assert "run_code" in seen["tools"]
    schema = code.schema([on.tools["list_leads"], on.tools["read_orders"]])
    assert "await list_leads(status=)" in schema["function"]["description"]
    assert "delete_lead" not in schema["function"]["description"]

    off = ScriptHost()
    off.code_mode = lambda agent, task: False
    await loop.run_task(off, _db(), _task(), llm=(capture(off), "m"))
    assert "run_code" not in seen["tools"]

    plain = ShopHost()  # a host that never heard of code mode
    await loop.run_task(plain, _db(), _task(), llm=(capture(plain), "m"))
    assert "run_code" not in seen["tools"]

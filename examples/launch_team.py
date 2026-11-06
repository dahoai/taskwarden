"""A launch team that runs on one clock, offline: python examples/launch_team.py

Five agents as folders (examples/launch_team/<id>/AGENT.md). Scout is the only clock (Mondays at
09:00). Pain Miner starts when Scout finishes and Judge when Pain Miner does, each reading the
report before it. Inbox wakes on an email to the bugs alias, never on a timer. The owner asks
Chief for a new routine; it waits for their click like anything else that changes how the team
works. The tasks table is the ledger: who ran, why, and what they reported.

Everything a trigger starts is self-started, so Inbox's reply to a real person waits for the
owner even though an email, not the owner, woke it.
"""
import asyncio
import pathlib
import sys
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from taskwarden import decisions, loop, sweeper, tools, triggers
from taskwarden.folders import load_agent
from taskwarden.host import BaseHost

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from shop import Base, Message, Reply, Task, call  # noqa: E402  the same tables and stand-in model

TZ = "America/Toronto"


async def search_posts(ctx, query: str = "") -> str:
    return ("1. 'why is there no tool that answers missed calls' https://x.com/a/1 (312 likes)\n"
            "2. 'my front desk misses half our calls' https://x.com/b/2\n"
            "3. 'wish booking texts went out by themselves' https://x.com/c/3")


async def send_email(ctx, to: str, body: str) -> str:
    return f"Sent to {to}."


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    title: str
    tools: tuple[str, ...]
    folder: Any


FOLDERS = [load_agent(p) for p in sorted((HERE / "launch_team").iterdir()) if p.is_dir()]
ROUTINES: dict[str, list[dict]] = {}  # the app's own store for routines agents set up


async def save_routine(ctx, trigger) -> None:
    ROUTINES.setdefault(ctx.organization_id, []).append(
        {"agent": trigger.agent_id, **triggers.as_dict(trigger)})


class LaunchHost(BaseHost):
    Task, Message = Task, Message
    chief = "chief"
    agents = {f.id: Agent(f.id, f.name, f.title, f.tools, f) for f in FOLDERS}
    triggers = triggers.of_folders(FOLDERS)  # checked: every agent exists, no loop

    def __init__(self, model):
        self.model = model
        self.tools = {
            "search_posts": tools.Tool("search_posts", tools.READ, tools.fn_schema(
                "search_posts", "Search public posts.", {"query": {"type": "string"}}),
                search_posts),
            "send_email": tools.Tool("send_email", tools.CONTACT, tools.fn_schema(
                "send_email", "Email someone.", {"to": {"type": "string"},
                                                 "body": {"type": "string"}}, ("to", "body")),
                send_email),
            triggers.ROUTINE_TOOL: triggers.routine_tool(self, save_routine, events=["email:bugs"]),
        }

    async def custom_triggers(self, db, organization_id):
        return [triggers.parse(r["agent"], {k: v for k, v in r.items() if k != "agent"})
                for r in ROUTINES.get(organization_id, [])]

    async def context(self, db, organization_id):
        return SimpleNamespace(db=db, organization_id=organization_id, timezone=TZ), {}

    async def llm(self, db, organization_id):
        return self.model, "scripted"

    async def system_prompt(self, db, task, agent, name, teammates, now, channel,
                            business_type, self_started, loaded) -> str:
        return agent.folder.role


def by_agent(**scripts: list) -> Any:
    """A stand-in model per agent: each answers from its own script, in order."""
    queues = {LaunchHost.agents[a].folder.core: list(r) for a, r in scripts.items()}

    async def create(messages, **request):
        reply = queues[messages[0]["content"]].pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=reply)])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


async def run_queued(host, Session) -> None:
    """What the sweeper does on its timer: run every queued task, including the ones the
    finished tasks just started."""
    async def run(db, task, **kw):
        return await loop.run_task(host, db, task, **kw)
    while True:
        async with Session() as db:
            queued = (await db.execute(select(Task.id).where(Task.status == "queued")
                                       .order_by(Task.created_at))).scalars().all()
        if not queued:
            return
        for task_id in queued:
            await sweeper.run_one(host, Session, task_id, run=run)


async def main() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    org = "launch-1"
    host = LaunchHost(by_agent(
        scout=[Reply(calls=[call("search_posts", query="wish there was")]),
               Reply("Top 3: missed calls (https://x.com/a/1), front desk overload "
                     "(https://x.com/b/2), booking texts (https://x.com/c/3).")],
        pain_miner=[Reply(calls=[call("search_posts", query="missed calls")]),
                    Reply("Top pain: missed calls, 3 mentions x intensity 5 = 15.")],
        judge=[Reply("GO, 7/8. Strongest objection: phone carriers already sell voicemail.")],
        inbox=[Reply(calls=[call("send_email", to="ana@example.com",
                                 body="Thanks Ana, we can see the blank booking page.")])],
        chief=[Reply(calls=[call(triggers.ROUTINE_TOOL, agent="judge", when="schedule",
                                 cadence="weekly", weekday=4, at="16:00",
                                 goal="Review this week's kill list in five lines.")]),
               Reply("Done: Judge reviews the kill list every Friday at 16:00.")],
    ))

    monday = datetime(2026, 11, 2, tzinfo=ZoneInfo(TZ))
    async with Session() as db:
        early = await triggers.due(host, db, org, now=monday.replace(hour=8, minute=55))
        print("08:55 due:", [t.agent_id for t in early])
        on_time = await triggers.due(host, db, org, now=monday.replace(hour=9, minute=1))
        print("09:01 due:", [t.agent_id for t in on_time])
        again = await triggers.due(host, db, org, now=monday.replace(hour=9, minute=6))
        print("09:06 due:", [t.agent_id for t in again], "(already ran today)")
    await run_queued(host, Session)  # Scout, then Pain Miner, then Judge

    email = "From: ana@example.com\nThe booking page shows a blank screen on my phone."
    async with Session() as db:
        first = await triggers.fire(host, db, org, "email:bugs", email)
        twice = await triggers.fire(host, db, org, "email:bugs", email)  # the webhook retried
        print("email:bugs woke:", [t.agent_id for t in first], "then", [t.agent_id for t in twice])
    await run_queued(host, Session)

    async with Session() as db:  # the owner asks Chief for a routine
        task = Task(organization_id=org, agent_id="chief", thread="team", origin="owner",
                    goal="Every Friday at 4pm have Judge review the kill list.",
                    status="running", attempts=1, state=[], contacted=[])
        db.add(task)
        print("owner:", task.goal, "->", await loop.run_task(host, db, task, actor="owner"),
              "on", task.pending["tool"])
        await db.commit()
    async with Session() as db:
        claimed = await decisions.claim(host, db, org, task.id, "approve")
        await decisions.apply(host, db, org, claimed, "approve")
    await run_queued(host, Session)
    async with Session() as db:
        print("routines:", [t.describe() for t in await host.custom_triggers(db, org)])

    print("\nledger:")
    async with Session() as db:
        for t in (await db.execute(select(Task).order_by(Task.created_at))).scalars():
            waits = f" waits on {t.pending['tool']}" if t.pending else ""
            print(f"  {t.agent_id:10} {t.origin:8} {t.status}{waits}")
        for m in (await db.execute(select(Message).order_by(Message.created_at))).scalars():
            print(f"  chat [{m.sender}] {m.body.splitlines()[0]}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

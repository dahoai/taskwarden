"""A web shop's AI team, end to end, offline: python examples/shop.py

The owner asks for a refund. The agent reads the orders on its own, then parks the refund
(a SPEND) for the owner. The owner corrects the order number and approves; the task resumes
and reports. Everything is saved in a database between those steps, so a restart at any
point picks up where it stopped.

It runs on SQLite with a scripted model so it needs no keys. For a real one, return
`(openai.AsyncOpenAI(), "your-model")` from `ShopHost.llm` (any OpenAI-compatible client),
and use Postgres in production.
"""
import asyncio
import json
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from taskwarden import decisions, loop, sweeper, tools
from taskwarden.host import BaseHost
from taskwarden.models import MessageColumns, TaskColumns


# ── Your tables: add the columns to models you own ──
class Base(DeclarativeBase):
    pass


class Task(Base, TaskColumns):
    __tablename__ = "team_tasks"


class Message(Base, MessageColumns):
    __tablename__ = "team_messages"


# ── Your tools: a handler, a schema and a risk ──
ORDERS = {"A7": 42.0, "A8": 19.5}


async def read_orders(ctx) -> str:
    return json.dumps([{"order": k, "total": v} for k, v in ORDERS.items()])


async def refund(ctx, order_id: str) -> str:
    return f"Refunded order {order_id}: ${ORDERS[order_id]:.2f}"


TOOLS = {
    "read_orders": tools.Tool("read_orders", tools.READ, tools.fn_schema(
        "read_orders", "Today's orders with their totals."), read_orders),
    "refund": tools.Tool("refund", tools.SPEND, tools.fn_schema(
        "refund", "Refund an order in full.", {"order_id": {"type": "string"}},
        ("order_id",)), refund),
}


# ── Your agents (or load folders: taskwarden.folders.load_agent) ──
@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    title: str
    tools: tuple[str, ...]
    folder: Any = None


class ShopHost(BaseHost):
    Task, Message = Task, Message
    chief = "lead"
    agents = {"lead": Agent("lead", "Sam", "Shop assistant", ("read_orders", "refund"))}
    tools = TOOLS

    def __init__(self, model):
        self.model = model

    async def llm(self, db, organization_id):
        return self.model, "scripted"

    async def system_prompt(self, db, task, agent, name, teammates, now, channel,
                            business_type, self_started, loaded) -> str:
        return f"You are {name}, the {agent.title} of a small web shop. Be brief."


# ── A stand-in for the model: what it would answer, in order ──
def call(name: str, **args) -> SimpleNamespace:
    return SimpleNamespace(id=f"call-{name}", function=SimpleNamespace(
        name=name, arguments=json.dumps(args)))


class Reply:
    def __init__(self, content: Optional[str] = None, calls: tuple = ()):
        self.content, self.tool_calls = content, list(calls) or None

    def model_dump(self, exclude_none=False) -> dict:
        out = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [{"id": c.id, "type": "function", "function": {
                "name": c.function.name, "arguments": c.function.arguments}}
                for c in self.tool_calls]
        return out


def scripted(*replies: Reply) -> Any:
    queue = list(replies)

    async def create(**request):
        return SimpleNamespace(choices=[SimpleNamespace(message=queue.pop(0))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


async def main() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    host = ShopHost(scripted(
        Reply(calls=[call("read_orders")]),
        Reply(calls=[call("refund", order_id="A7")]),
        Reply("Done: I refunded order A8, $19.50."),
    ))

    async with Session() as db:  # the owner's request, run at once
        task = Task(organization_id="shop-1", agent_id="lead", thread="team", origin="owner",
                    goal="Refund the customer who paid $19.50 today.", status="running",
                    attempts=1, state=[], contacted=[])
        db.add(task)
        print("owner:", task.goal)
        print("run:  ", await loop.run_task(host, db, task, actor="owner"))
        print("waits:", task.pending["tool"], task.pending["args"])
        await db.commit()

    async with Session() as db:  # later, any process: the owner fixes the order and approves
        claimed = await decisions.claim(host, db, "shop-1", task.id, "approve")
        await decisions.apply(host, db, "shop-1", claimed, "approve",
                              edited_args={"call-refund": {"order_id": "A8"}})
        print("owner: approved, with order A8 instead of A7")

    # The sweeper's next pass resumes it: in production sweeper.sweep(...) on a timer, with a
    # Redis lock, picks up every queued task; run_one is that step for one task.
    async def run(db, task, **kw):
        return await loop.run_task(host, db, task, **kw)
    await sweeper.run_one(host, Session, task.id, run=run)

    async with Session() as db:
        print("run:  ", (await db.get(Task, task.id)).status)
        for m in (await db.execute(select(Message).order_by(Message.created_at))).scalars():
            print(f"chat: [{m.sender}] {m.body}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

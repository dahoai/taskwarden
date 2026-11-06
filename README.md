# Taskwarden

**A durable, owner-approved AI team for a business.**

Taskwarden is a Python harness for running a small team of AI agents that do real work for a
business (texting customers, booking, refunding, posting) on a server, safely. The owner
decides what each agent may do alone and what must ask first. Anything that asks waits on a
queue in your database until the owner approves, edits or declines it, then carries on where
it stopped, even after a restart.

It runs the AI team of [Perbene](https://perbene.ai) in production, for many businesses at once.

```bash
pip install taskwarden
```

## Why another agent framework

Most frameworks are built for one person at a keyboard, or for a demo. A business's team runs
unattended, for many businesses at once, and its mistakes reach customers. Taskwarden is built
around the questions that come up then:

| What goes wrong | What Taskwarden does |
| --- | --- |
| The agent texts a customer something the owner never saw | Every tool has a **risk**. Per agent, the owner picks `allow`, `ask` or `observe` ("Practice": reads run, everything else is only recorded). Deletes and profile changes always ask. Work the app's own schedule starts never reaches a customer without a click. |
| The owner approves a message with one wrong word | The owner can **edit** the words before approving; the agent is told what changed. |
| The server restarts mid-send, and the retry sends it twice | A send is saved as "may have gone out" **before** it runs, and the same message never goes to the same person twice in a task (once your host says what counts as a send: `sent_key`). |
| An approval waits three days | Parked tasks live in **your database**, not in memory: approve from any process, any time. |
| A tool hangs | Each tool has a **timeout**; a send stopped by it counts as sent. |
| A long task overflows the model's context | Old tool results are **compacted** before the run is made to wrap up. |
| The model repeats the same call | It is **reminded once**, then stopped. |
| A customer writes back | Their reply **wakes the task** that wrote to them. |
| Agents poll "anything new?" all day | **One clock, many triggers**: an agent wakes on a schedule, on an event your app fires (an email to an alias, a webhook), or when a teammate finishes, with their report. Nothing a trigger starts can send, pay or change records without the owner. |
| One busy business floods the queue | The background **sweeper** runs owner work first, then takes turns between businesses, several at once, under a Redis lock. |
| "Who did what?" | Every call is **audited** with its decision and the rule behind it. |

## How it fits

```
owner request ──▶ loop.run_task ──▶ model ──▶ tool call ──▶ gate.decide
                                                              │
                         allow ◀───────────────┬──────────────┤
                           │                   │              ▼
                       run the tool        observe        ask: park the task
                           │               (record)        (pending, in your DB)
                           ▼                                  │
                    next round / report        owner: approve / edit / decline
                                               decisions.claim + decisions.apply
                                                              │
                                                 queued ──▶ sweeper resumes it
```

| Module | What it does |
| --- | --- |
| `loop` | One task's run: model rounds, tool calls, guards, hand-offs between agents (`delegate`, `consult`), parking on the owner, checkpoints. |
| `gate` | Who may do what: the tool's risk and the agent's setting decide run, ask, record or refuse. Fails closed. |
| `tools` | `Tool` (name, risk, schema, handler, optional check and timeout), argument cleaning (the model never picks whose data it touches), execution that never leaks an exception to the model. |
| `decisions` | The owner's approve / edit / decline / revise / answer on a parked task, claimed exactly once. |
| `sweeper` | Background runs: queued tasks, retries, stale runs recovered, old waits expired. |
| `replies` | A reply from someone the team wrote to wakes that task. |
| `triggers` | What wakes an agent besides the owner: `schedule`, `event`, `done` (a teammate finished). Routines an agent sets up itself, with the owner's click (`routine_tool`). |
| `folders` | Agents as folders: `AGENT.md` plus `skills/<name>/SKILL.md`, loaded on demand. |
| `models` | SQLAlchemy column mixins for your two tables (tasks, messages). |
| `host` | What your app supplies, and `BaseHost` with a default for everything that has one. |

## Quick start

[`examples/shop.py`](examples/shop.py) is a complete app: a web shop whose agent reads orders
on its own and refunds only with the owner's click. It runs offline (SQLite, a scripted model):

```bash
pip install "taskwarden[dev]"
python examples/shop.py
```

```
owner: Refund the customer who paid $19.50 today.
run:   waiting
waits: refund {'order_id': 'A7'}
owner: approved, with order A8 instead of A7
run:   done
...
chat: [lead] Done: I refunded order A8, $19.50.
```

The shape of a host:

```python
from taskwarden import decisions, loop, sweeper, tools
from taskwarden.host import BaseHost
from taskwarden.models import MessageColumns, TaskColumns

class Task(Base, TaskColumns):
    __tablename__ = "team_tasks"

class Message(Base, MessageColumns):
    __tablename__ = "team_messages"

class ShopHost(BaseHost):
    Task, Message = Task, Message
    chief = "lead"
    agents = {"lead": Agent("lead", "Sam", "Shop assistant", ("read_orders", "refund"))}
    tools = {
        "read_orders": tools.Tool("read_orders", tools.READ, schema, read_orders),
        "refund": tools.Tool("refund", tools.SPEND, schema, refund),
    }

    async def llm(self, db, organization_id):
        return openai.AsyncOpenAI(), "your-model"   # any OpenAI-compatible client

    async def system_prompt(self, db, task, agent, name, *rest) -> str:
        return f"You are {name}, the {agent.title} of a small web shop."

await loop.run_task(host, db, task, actor="owner")          # the owner's request
await decisions.apply(host, db, org, await decisions.claim(host, db, org, task.id, "approve"),
                      "approve")                             # their click, any time later
await sweeper.sweep(host, Session, redis, now=now, run=run) # on a timer: resumes the rest
```

`BaseHost` leaves out what only your app can know. Override it as you grow:
`sent_key` / `reach_key` (turn on the duplicate-send guard), `notify_parked` (push, SMS or
email the owner), `refuse_run` (credits), `context` (the business's time zone), `audit`
(store the trail), `mark` (live progress), `thought` (the model's reasoning, as it works), `take_messages` (what the owner writes while a task runs joins it), `stop_asked` (the owner's Stop: the run ends at its next step and says what it did), `clock` (how the time reads; it rides on the
first message of each request, never in the system prompt, so the prompt stays cached).

## One clock, many triggers

An agent says in its `AGENT.md` what wakes it (`when:`, not `on:`, which YAML reads as true):

```yaml
triggers:
  - when: schedule          # Mondays at 09:00, the business's local time
    cadence: weekly
    weekday: 0
    at: "09:00"
    goal: Find what people kept asking for this week.
  - when: done              # Scout finished: its report comes with the goal
    after: scout
    goal: Collect the complaints behind Scout's top three.
  - when: event             # your app calls triggers.fire(host, db, org, "email:bugs", body)
    event: email:bugs
    goal: A bug report came in. Write the steps to reproduce it.
```

```python
class MyHost(BaseHost):
    triggers = triggers.of_folders(folders)        # checked: agents exist, no done-loop

await triggers.fire(host, db, org, "email:bugs", email_text)  # your webhook handler
await triggers.due(host, db, org, now=now)                    # in the sweeper's on_sweep
# `done` triggers need nothing: a finished task starts whoever follows its agent.
```

Every task a trigger starts is self-started, so an email can wake an agent but cannot make it
send, pay or change records without the owner's click; one woken by an event or a report asks
even before `routine` work (a text back, a booking), since a stranger's words may be steering it. The same goal is queued once a day (a
retried webhook), each agent runs at most `max_per_day` triggered tasks, a paused agent stays
asleep, and a `done` chain that would loop is refused when it is loaded.

Agents can set up their own routines: give one `triggers.routine_tool(host, save)` and write
`custom_triggers(db, org)` to read back what `save` stored. Adding a routine is a `profile`
call, so it always waits for the owner's click.

[`examples/launch_team.py`](examples/launch_team.py) runs a five-agent launch team offline:
Scout on Mondays, Pain Miner and Judge on each other's reports, Inbox on email, and a routine
Chief sets up once the owner approves.

## Risk levels

| Risk | Examples | Decided by |
| --- | --- | --- |
| `read`, `draft` | look up orders, write a draft | always runs |
| `routine` | text a customer back, book them | runs; asks when the app's schedule started the task |
| `contact`, `spend`, `change` | a campaign, a refund, edit records | the agent's setting: `ask` or `allow` |
| `remove`, `profile` | delete a contact, change the business's profile | always asks |

## Requirements

Python 3.11+, SQLAlchemy 2 (async), PyYAML. Postgres in production (SQLite works for trying
it out), Redis for the sweeper's lock. Any OpenAI-compatible chat model with tool calls.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

## License

Apache-2.0. Built by [DAHO](https://perbene.ai).

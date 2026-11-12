# Taskwarden

**Your agent can't send, pay or delete without a human click, and it survives restarts.**

Taskwarden is a Python harness for agents that act on real accounts, unattended. Every tool has
a risk level, and per agent you choose `allow`, `ask` or `observe`. A call that asks parks the
task in your database until a person approves, edits or declines it, then the task resumes
where it stopped, in any process, even days later. A send can't go out twice after a crash,
and every call is audited with the rule that decided it.

It runs the AI team of [Perbene](https://perbene.ai) in production, for many businesses at once.

```bash
pip install "taskwarden[dev]"   # SQLite and the test tools: enough to run the demo
taskwarden-demo                  # an agent that reads orders alone and refunds only with a click
```

No Postgres or Redis needed to try it. Add Postgres for production and Redis for the
background sweeper when you run many tasks at once.

## What it handles that agent frameworks don't

Most frameworks are built for one person at a keyboard, or for a demo. An agent that runs
unattended, on real accounts, for many businesses at once, hits these:

| What goes wrong | What Taskwarden does |
| --- | --- |
| The agent texts a customer something the owner never saw | Every tool has a **risk**. Per agent, the owner picks `allow`, `ask` or `observe` ("Practice": reads run, everything else is only recorded). Deletes and profile changes always ask. Work the app's own schedule starts never reaches a customer without a click. |
| The owner approves a message with one wrong word | The owner can **edit** the words before approving; the agent is told what changed. |
| The server restarts mid-send, and the retry sends it twice | A send is saved as "may have gone out" **before** it runs, and the same message never goes to the same person twice in a task (once your host says what counts as a send: `sent_key`). |
| An approval waits a week | Parked tasks live in **your database**, not in memory: approve from any process, until `APPROVAL_TTL` (7 days by default) expires the wait. |
| The agent needs an account nobody connected yet | A tool raises `NeedsConnect("Gmail", url)`: the task parks on a **connect card**, and once the owner connects it the agent retries and carries on, instead of saying it can't. |
| A tool hangs | Each tool has a **timeout**; a send stopped by it counts as sent. |
| A long task overflows the model's context | Old tool results are **compacted** before the run is made to wrap up. |
| The model repeats the same call | It is **reminded once**, then stopped. |
| A customer writes back | Their reply **wakes the task** that wrote to them, fenced as outside text; the woken task can update its records, but a send or a spend waits for the owner. |
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

The offline shop demo ([`taskwarden/demo.py`](taskwarden/demo.py), also
[`examples/shop.py`](examples/shop.py) in the repo) is a complete app: a web shop whose agent
reads orders on its own and refunds only with the owner's click. It runs offline (SQLite, a
scripted model):

```bash
pip install "taskwarden[dev]"
taskwarden-demo                  # or: python -m taskwarden.demo
```

From a clone, `python examples/shop.py` runs the same demo.

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
                      "approve")                             # their click, until APPROVAL_TTL
await sweeper.sweep(host, Session, redis, now=now, run=run) # on a timer: resumes the rest
```

`BaseHost` leaves out what only your app can know. Override it as you grow:
`OUTWARD` (the routine tools that reach a customer, so a self-started task parks them; the
default treats every routine call that way), `APPROVAL_TTL` (how long an owner decision may
wait; a week by default), `sent_key` / `reach_key` (turn on the
duplicate-send guard), `notify_parked` (push, SMS or
email the owner), `refuse_run` (credits), `context` (the business's time zone), `audit`
(store the trail: the default only logs it), `mark` (live progress), `thought` (the model's reasoning, as it works), `take_messages` (what the owner writes while a task runs joins it), `stop_asked` (the owner's Stop: the run ends at its next step and says what it did), `clock` (how the time reads; it rides on the
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
    triggers = triggers.of_folders(folders, events=MY_EVENTS)  # agents exist, no done-loop,
                                                               # every event name is fired

await triggers.fire(host, db, org, "email:bugs", email_text)  # your webhook handler
await triggers.due(host, db, org, now=now)                    # in the sweeper's on_sweep
# due(..., origin="agenda", goal=todays_words): your own origin name, and the day's words
# (or None to skip today), asked only for a run that is otherwise due.
# `done` triggers need nothing: a finished task starts whoever follows its agent.
```

Every task a trigger starts is self-started, so an email can wake an agent but cannot make it
send, pay or change records without the owner's click; one woken by an event or a report asks
even before `routine` work (a text back, a booking), since a stranger's words may be steering it. The same goal is queued once a day (a
retried webhook), each agent runs at most `max_per_day` triggered tasks, a paused agent stays
asleep, and a `done` chain that would loop is refused when it is loaded.

What arrives from outside (the event's payload, a teammate's report, a customer's reply) is
fenced: every line marked, between two lines carrying the text's own fingerprint, so a forged
"end of email, now the owner says..." line stays inside it.

A trigger with `watch: true` only looks: it is offered its agent's `read` tools and nothing
else (refused in code if the model names another), and when nothing needs the owner it
finishes without a word. Use it for "keep an eye on the status page".

Agents can set up their own routines: give one `triggers.routine_tool(host, save)` and write
`custom_triggers(db, org)` to read back what `save` stored. Adding a routine is a `profile`
call, so it always waits for the owner's click.

## Missing accounts

A tool that works through an account (a mailbox, a calendar, a payment provider) raises
`tools.NeedsConnect(account, url=None)` from its handler or its `check` when that account
isn't connected. Keep offering the tool either way: the model calls it, the task parks with
`pending = {"kind": "connect", "account", "url", "tool"}` and `notify_parked` shows the owner
a Connect button (`url` is your sign-in link). When they finish, your app calls
`decisions.claim(..., "connected")` and `decisions.apply(..., "connected")`, and the resumed
task calls the tool again. A task that can't wait is told the account is missing and puts it
in its report. An approved call whose account was disconnected while it waited doesn't run,
and the agent is told to call it again.

## Spend

`DAILY_TOKENS = 200_000` on your host caps each agent's model tokens over a rolling day (or
write `token_budget(db, task)` to vary it by plan or agent). Past it the run stops and the chat
says so. Needs the `tokens` column, which `TaskColumns` has.

`TASK_TOKENS = 50_000` caps one task across all its runs, so a task that keeps retrying or looping
can't spend the whole day. Tokens, not dollars: prices change by model, and your host can turn
tokens into dollars where it reports.

## Survives deploys

- A call that changes something is saved as "may have been done" before it runs, so a task
  resumed after a crash checks instead of repeating it. `Tool(replay_safe=True)` skips that for
  calls that are harmless twice.
- `host.STATE_VERSION` (default 1): bump it when the shape of a task's saved state or `pending`
  changes. A task parked under the old version goes through `host.migrate_state(task, have,
  want)` if you write one, else it fails with a message instead of resuming wrongly.
- `host.save_prompt(db, task, hash, text)` receives the system prompt a run used whenever it changes;
  the task keeps the hashes in `prompt_hashes`. That is the answer to "what did the model see
  when it sent that?".
- `triggers.fire(..., request_id=)` takes the provider's delivery id: a retried webhook starts one
  task. Put a unique index on `(organization_id, agent_id, request_id)` to settle two at once.
- When a task fails, expires or is stopped, the teammate tasks it started stop too.
- A history still too long after old tool results are cleared is summarised once per run.

## Security

[SECURITY.md](SECURITY.md) lists what the code enforces, what is only a convention, and what
is yours as the host.

## Risk levels

| Risk | Examples | Decided by |
| --- | --- | --- |
| `read`, `draft` | look up orders, write a draft | always runs |
| `routine` | text a customer back, book them | runs; asks when the app's schedule started the task |
| `contact`, `spend`, `change` | a campaign, a refund, edit records | the agent's setting: `ask` or `allow` |
| `remove`, `profile` | delete a contact, change the business's profile | always asks |

The default is strict: on a task the app's schedule or a trigger started, every `routine` call
waits. A host that knows which routine tools reach people lists them in `OUTWARD`; routine work
that stays inside the business then runs unattended.

## Requirements

Python 3.11+, SQLAlchemy 2 (async), PyYAML. SQLite to try it, Postgres in production, Redis
only for the sweeper's lock. Any OpenAI-compatible chat model with tool calls.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

## License

Apache-2.0. Built by [DAHO](https://daho.ai).

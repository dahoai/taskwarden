# Changelog

## 0.8.0 (2026-11-12)

Adds columns to `TaskColumns`: `request_id`, `state_version`, `prompt_hashes` (a host with its own
table adds them; the loop skips a feature whose column is missing).

- Crash safety for every change, not only sends: a non-read call is saved as "may have been done"
  before it runs (`MAYBE_DONE`), so a resumed run checks instead of repeating a refund or a
  booking. `Tool(replay_safe=True)` opts out for calls that are harmless twice.
- `TASK_TOKENS`: a ceiling on one task's tokens across all its runs.
- `triggers.fire(..., request_id=)`: the same delivery id twice starts one task (a unique index on
  `(organization_id, agent_id, request_id)` settles two at the same instant).
- A task that fails, expires or is stopped stops the teammate tasks it started, and a child that was
  mid-run stops when its next run starts (`cancel_children`).
- `host.save_prompt(db, task, hash, text)`: the system prompt a run used, saved when it changes; the
  task keeps the hashes (`prompt_hashes`), so "what did the model see?" has an answer.
- `host.STATE_VERSION` / `migrate_state`: a task saved under another version is migrated or stopped
  with a message, never resumed on a state of the wrong shape.
- A history still too long after clearing old tool results is summarised once per run (the goal and
  the latest 8 messages stay whole) before the run is made to wrap up.
- The trigger tests run on Postgres too when `TASKWARDEN_TEST_PG_URL` is set.

## 0.7.0 (2026-11-09)

- `BaseHost.OUTWARD`: the routine tools that reach a customer. `BaseHost.decide` now passes it
  to `gate.decide`, so a task the app's schedule started parks a routine text or booking for
  the owner's click, not only `contact`/`spend`/`change`. The default (`None`) treats every
  routine call as reaching someone; a host that knows its customer-facing routine tools lists
  them and lets the rest of its routine work run unattended. A host that overrides `decide`
  keeps its own policy.
- `taskwarden-demo`: the offline shop demo now ships inside the package
  (`taskwarden/demo.py`), so `pip install "taskwarden[dev]"` then `taskwarden-demo` (or
  `python -m taskwarden.demo`) runs it without a clone. `examples/shop.py` re-exports the same
  demo for the repo path.
- A customer's reply is now fenced like any other outside text (`replies.wake`), and
  `replies.woken(task)` makes the task's later runs self-started: a reply can wake the task that
  wrote to them, but it cannot make it send, spend or change records without the owner's click.
  Only `note` (what they wrote) is fenced: `wake(..., about=, guidance=)` carries the host's own
  line before it and its instructions after it, unfenced (`guidance` replaces `REPLY_GUIDANCE`).
- An approved call is re-checked against practice mode as well as a pause: an agent switched to
  `observe` while the owner was deciding does not run. An approval whose call id is already in
  `task.contacted` is refused as a duplicate instead of sending twice, and a corrupt approval
  with no call behind it fails instead of queueing a phantom answer.
- `gate.decide` fails closed on an `AgentState.setting` it does not know (`unknown_setting`).
- `Tool` validates `risk` against `tools.RISKS` and requires a positive `timeout` when it is
  declared, so a mistyped risk fails at import rather than silently at the gate.
- `triggers.check(..., events=...)` / `triggers.of_folders(folders, events=...)` can validate
  every `event` name against the names the app actually fires; `folders.load_agent` validates
  `skills_on_demand`, `business_types` and the agenda's cadence, weekday, goal and skills.
- `BaseHost.APPROVAL_TTL`: how long a parked task may wait before the sweeper expires it
  (`None` = `sweeper.EXPIRE_AFTER`, 7 days). Docs no longer say an approval waits "any time".
- A watch task is no longer offered `use_skill`: watch reads, and nothing else.

## 0.6.1 (2026-11-09)

- PyPI / package one-liner: approvals-and-audit pitch, SQLite to try — no longer ends on
  "Postgres".

## 0.6.0 (2026-11-09)

- A missing account no longer ends the task: a handler or `check` raises
  `tools.NeedsConnect(account, url=None)`, the task parks on a `connect` card
  (`pending.kind == "connect"`), and the owner's `"connected"` decision resumes it so the agent
  calls the tool again. A task that can't wait is told instead; an approved call whose account
  dropped while it waited doesn't run.
- `examples/launch_team.py` removed; `examples/shop.py` is the example.
- README leads with what the harness guarantees, and says SQLite is enough to try it.

## 0.5.0 (2026-11-06)

- Outside words are fenced: what `fire` and `follow` put under a goal is marked line by line
  between two lines carrying its SHA-256 fingerprint, cut at 16,000 characters
  (`triggers.fenced`, for any other outside text). Goals of triggered tasks read differently.
- `watch: true` on a trigger: its tasks (origin `watch`) get their agent's `read` tools only,
  anything else is refused in code, no hand-offs or questions; a reply of `NOTHING_TO_REPORT`
  finishes without a message and wakes nobody. `routine_tool` takes `watch` too.
- A daily token budget per agent: `DAILY_TOKENS` on the host, or `token_budget(db, task)`.
  New column `tokens` on `TaskColumns` (an existing table: `ALTER TABLE ... ADD COLUMN tokens
  integer NOT NULL DEFAULT 0`); without a budget nothing reads it.
- SECURITY.md: what is enforced, what is a convention, what is the host's.

## 0.4.0 (2026-11-06)

`triggers.due` carries a host's own scheduled work (Perbene's standing agenda runs on it).

- `due(..., origin=)`: a host keeps its own name for scheduled tasks.
- `due(..., goal=)`: an async callback gives the day's words for a schedule that is otherwise
  due (its goal plus what the day adds), or None to skip it today. Called last, so work it
  does happens only for a run that starts.
- A schedule waits while its agent already has work in hand today (the owner's request first).
- One schedule per agent: every run of its origin is its own, so a goal reworded in a deploy
  can't start a second run that day. Several: told apart by how their goals start, and
  `check` refuses one goal that starts another.
- `used_by(host)` is true only with `done` triggers or routines: a host with schedules alone
  pays nothing when a task finishes.

## 0.3.0 (2026-11-05)

New and optional; a host without triggers behaves as in 0.2.1.

- `triggers`: what wakes an agent besides the owner. In `AGENT.md`, `triggers:` with
  `when: schedule` (daily or weekly, optional local `at`), `when: event` (a name the app fires
  with `triggers.fire`), or `when: done` (`after:` a teammate; its report comes with the goal).
  `triggers.due` queues the schedules due for one business (once a local day, one retry after
  a failure, never on top of a run still open). Tasks they start have origin `schedule` or
  `trigger` and are always self-started. A task an event or a report woke (and its hand-offs)
  also asks before a `routine` call: a stranger's email can't make it text anyone. Guards: no
  `done` loop (checked at load, on add and at run time), the same event once a day,
  `max_per_day` per agent, 100 triggered tasks a day per business, paused agents stay asleep.
- `triggers.routine_tool(host, save, events=)`: agents set up routines; always the owner's
  click (`profile` risk), checked before the card is shown. `custom_triggers(db, org)` on the
  host reads them back.
- A task a trigger started counts its goal (the report, the email) as read, so stating its
  figures is not taken for invented ones.
- `examples/launch_team.py`: a five-agent team on one clock, offline.

## 0.2.1 (2026-11-04)

- An approved call is checked again before it runs, on what will really run: the gate (an
  agent paused or a tool taken away while it waited gets `NOT_ALLOWED`, audited as
  `no_longer_allowed`) and the tool's own `check`, on the owner's edits too (audited as
  `precheck`). Before, only the tool's existence was checked at approval time.

## 0.2.0 (2026-11-04)

New optional host hooks; a host without them behaves as in 0.1.0.

- `clock(now)`: the time rides on the first message of each request instead of the system
  prompt, so the prompt stays in the model's cache across runs. `BaseHost` gives a UTC line.
- `stop_asked(task)`: the owner's Stop. Checked before each model call, each tool and before
  parking; the run ends as `stopped`, answers every call it never reached, and posts what it
  had already done (`STOPPED_REPLY`, `STOPPED_DID`).
- `take_messages(task)`: what the owner writes while a task runs joins it, at the next round
  and again before the run finishes, so one reply covers both.
- `thought(org, thread, agent, text)`: the model's reasoning on each turn, for a live "why".
- `decisions.apply(..., shown=)`: how the chat shows an answer when it differs from what the
  agent reads.
- Pictures in a user message (an owner's photo) are shown once and their words kept; a model
  that can't see pictures is sent the words only.
- `Usage`: each run logs its model calls and tokens, cached ones included (DeepSeek, OpenAI).

## 0.1.0 (2026-11-04)

First release.

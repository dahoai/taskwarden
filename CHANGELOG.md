# Changelog

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

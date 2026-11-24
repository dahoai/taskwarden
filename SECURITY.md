# Security: what taskwarden enforces, what is a convention, what is yours

An agent with tools is steered by whatever it reads. Taskwarden can't make a model trustworthy;
it decides, in code, what a call may do before it runs. This page says exactly where that line
is.

## Enforced in code

| Boundary | How it holds |
| --- | --- |
| Every call is decided before it runs | `gate.decide`, first match wins: a tool the agent wasn't granted is denied, a paused agent is denied, a tool in the agent's `blocked_tools` (the owner's "never do this") is denied in every setting and on every task, `remove` and `profile` always ask, `contact`/`spend`/`change` follow the agent's setting (`ask` or `allow`), anything unrecognised is denied (fail closed). The model never sees a tool it wasn't granted. |
| Work the owner didn't start can't act alone | A task the app's schedule or a trigger started is self-started: it reads and drafts, and every `contact`/`spend`/`change` call parks for the owner, however loose the agent's setting. So does every `routine` call, until the host names the routine tools that reach people (`BaseHost.OUTWARD`; the default treats them all that way). A hand-off copies its parent's origin, so delegating is no way around it. |
| A stranger's words can't make it reply | A task an event or a teammate's report woke (origin `trigger`) also asks before any `routine` call: a text back, a booking. A customer's reply wakes the task that wrote to them, and that reply makes its later runs outside-woken too, so it can read and draft but cannot send, spend or change without a click. |
| Outside words are fenced | What `triggers.fire` and `follow` hand an agent (a webhook body, an email, a report) arrives under its goal with every line marked `│ `, between two lines carrying the text's own SHA-256 fingerprint, cut at 16,000 characters. A reply from someone the team messaged is fenced the same way. A line inside can't close the fence: it is marked too. Use `triggers.fenced` for any other outside text you put in front of a model. |
| A watch trigger can only read | `watch: true` tasks (origin `watch`) are offered the agent's `read` tools and nothing else: no writes, no `ask_person`, no hand-offs, no skills, no host-local tools. A call to anything else is refused in code and audited (`rule="watching"`). A watch that finds nothing replies `NOTHING_TO_REPORT` and finishes without a message, waking nobody. |
| An approval runs what the owner saw | The parked call is stored with its exact arguments and shown in full. `decisions.claim` moves a task out of `waiting` once, so a double click can't run it twice; given the `rev` the owner saw (`pending["rev"]`, new at each park), it refuses a decision made on an older proposal as `"stale"`. The owner may approve part of a batch or edit an argument; only that runs. At approval time the call is decided again (paused agent, practice mode, tool taken away, its `check`), since it may have waited for days. |
| An approval doesn't wait forever | A parked task expires after `BaseHost.APPROVAL_TTL` (7 days by default), so an old click cannot run against a world that moved on; the chat says it expired. |
| Never the same message twice | With `sent_key`/`reach_key` written, a task never sends the same body to the same person twice (recorded before the send, so a crash mid-send can't repeat it), one turn reaches each person once, and an approved batch skips a call whose key is already in `contacted`. |
| Agents can't wake each other forever | `done` chains that loop are refused when triggers load, when a routine is added and again at run time. A hand-off chain stops at depth 2; a task makes at most 5 hand-offs; hand-offs and triggered work stop once a business has 200 tasks in a day (triggered ones at 100), each agent at most `max_per_day` (20) triggered ones; the same event is queued once a day. |
| Spend has a ceiling if you set one | `DAILY_TOKENS` (or `token_budget(db, task)`) caps each agent's model tokens over a rolling day. Past it the run stops before its next model call, the task fails so nothing retries it, and the chat says so. |
| Runs end | 60 rounds at most; the same calls three rounds running end the run; a run past its time budget checkpoints instead of being cut mid-tool; the owner's Stop (`stop_asked`) is checked before each model call and each tool. |
| Agents set up routines only with a click | `routine_tool` is a `profile` call: it always waits for the owner, and refuses a routine that would loop or never run. |
| A script can only read | Code mode (`run_code`, off unless the host sets `code_runner` and `code_mode`) runs the model's script in Monty, an interpreter with no files, environment or network, in a fresh worker process per script, with memory, time and call limits. Its only door out is the agent's tools, and each call through it is decided by the same `host.decide`, audited (rule `<rule>_from_code`) and run by `host.execute`. A call whose tool isn't `read` or whose decision isn't ALLOW raises inside the script and never runs (`code_reads_only`), so sends, bookings and approvals stay on the normal path. Tool results enter the script as values; only the model's own `code` is run. |
| There is a record | Every call's decision goes to `host.audit` with the rule that decided it, the task and who started it; so does each skill an agent loads (`use_skill`, target = the skill), so a skill nobody uses shows. |
| A report is checked before it is believed | With `host.unbacked(db, task, text)` written, a report that says something was done is sent back once when no call of the task did it: the agent does it, says when it happened, or says it was not done. |

## A convention, not a wall

- **The tool's risk label is yours.** The gate trusts `Tool.risk`. A tool that sends email but
  is labelled `read` runs everywhere, watch tasks included. Label by what the tool can do at
  worst.
- **A handler does what its name says.** Taskwarden decides whether a call runs, not what the
  handler then does with your credentials. Keep handlers narrow: one tool, one action.
- **Treating outside words as data.** The fence marks them and the gate limits what a woken
  agent can do, but a model can still be persuaded by what it reads, inside what it is allowed.
  Prompt injection is unsolved industry-wide; the gate is what keeps it cheap.
- **The words your agents read.** The system prompt, `AGENT.md` and skills shape behaviour;
  they are not enforcement. Anything that must hold goes in a risk level or a `check`.
- **The audit trail is only as durable as `audit`.** Every decision reaches `BaseHost.audit`,
  whose default writes a log line at INFO. Override it to store a row per decision in your own
  table if you need to query "who did what" later.
- **The budget is coarse.** It sums the tokens of the agent's tasks touched in the last day,
  and the run that crosses the line finishes its current call first.

## Yours, as the host

1. **Who the owner is.** Taskwarden never authenticates anyone. Only your signed-in owner may
   call `decisions.claim`/`apply`, and only for their own `organization_id`.
2. **Verify every webhook before `fire`.** Check the provider's signature, and dedupe on its
   delivery id: `fire`'s same-event check and its insert are not atomic.
3. **One business per query.** Every handler gets `ctx.organization_id`; scope every read and
   write to it.
4. **Credentials stay out of the model.** Read keys in the handler, never put them in a goal,
   a tool result or the system prompt.
5. **Cap the spend twice.** A limit at your model provider, and `DAILY_TOKENS` here.

Report a vulnerability privately to the maintainers through GitHub's security advisories on
[dahoai/taskwarden](https://github.com/dahoai/taskwarden/security), not in a public issue.

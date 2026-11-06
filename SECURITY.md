# Security: what taskwarden enforces, what is a convention, what is yours

An agent with tools is steered by whatever it reads. Taskwarden can't make a model trustworthy;
it decides, in code, what a call may do before it runs. This page says exactly where that line
is.

## Enforced in code

| Boundary | How it holds |
| --- | --- |
| Every call is decided before it runs | `gate.decide`, first match wins: a tool the agent wasn't granted is denied, a paused agent is denied, `remove` and `profile` always ask, `contact`/`spend`/`change` follow the agent's setting (`ask` or `allow`), anything unrecognised is denied (fail closed). The model never sees a tool it wasn't granted. |
| Work the owner didn't start can't act alone | A task the app's schedule or a trigger started is self-started: it reads and drafts, and every `contact`/`spend`/`change` call (and `outward` routine tools) parks for the owner, however loose the agent's setting. A hand-off copies its parent's origin, so delegating is no way around it. |
| A stranger's words can't make it reply | A task an event or a teammate's report woke (origin `trigger`) also asks before any `routine` call: a text back, a booking. |
| Outside words are fenced | What `triggers.fire` and `follow` hand an agent (a webhook body, an email, a report) arrives under its goal with every line marked `│ `, between two lines carrying the text's own SHA-256 fingerprint, cut at 16,000 characters. A line inside can't close the fence: it is marked too. Use `triggers.fenced` for any other outside text you put in front of a model. |
| A watch trigger can only read | `watch: true` tasks (origin `watch`) are offered the agent's `read` tools and nothing else: no writes, no `ask_person`, no hand-offs, no host-local tools. A call to anything else is refused in code and audited (`rule="watching"`). A watch that finds nothing replies `NOTHING_TO_REPORT` and finishes without a message, waking nobody. |
| An approval runs what the owner saw | The parked call is stored with its exact arguments and shown in full. `decisions.claim` moves a task out of `waiting` once, so a double click can't run it twice. The owner may approve part of a batch or edit an argument; only that runs. At approval time the call is decided again (paused agent, tool taken away, its `check`), since it may have waited for days. |
| Never the same message twice | With `sent_key`/`reach_key` written, a task never sends the same body to the same person twice (recorded before the send, so a crash mid-send can't repeat it) and one turn reaches each person once. |
| Agents can't wake each other forever | `done` chains that loop are refused when triggers load, when a routine is added and again at run time. A hand-off chain stops at depth 2; a task makes at most 5 hand-offs; hand-offs and triggered work stop once a business has 200 tasks in a day (triggered ones at 100), each agent at most `max_per_day` (20) triggered ones; the same event is queued once a day. |
| Spend has a ceiling if you set one | `DAILY_TOKENS` (or `token_budget(db, task)`) caps each agent's model tokens over a rolling day. Past it the run stops before its next model call, the task fails so nothing retries it, and the chat says so. |
| Runs end | 60 rounds at most; the same calls three rounds running end the run; a run past its time budget checkpoints instead of being cut mid-tool; the owner's Stop (`stop_asked`) is checked before each model call and each tool. |
| Agents set up routines only with a click | `routine_tool` is a `profile` call: it always waits for the owner, and refuses a routine that would loop or never run. |
| There is a record | Every call's decision goes to `host.audit` with the rule that decided it, the task and who started it. |

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

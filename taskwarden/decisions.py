"""The owner's decision on a parked task: approve, decline, revise (they wrote a change instead
of approving), answer (a question), or connected (they connected the account a tool needed).
Claimed first, then carried out.

`claim` is quick and committed, so an app can answer the owner at once and `apply` in the
background (approved actions, texts, emails, campaigns, can take a minute).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import select, update

from . import gate
from .loop import MAYBE_SENT, pending_calls, tool_msg
from .tools import NeedsConnect

# What a parked call reads when the owner wrote in the chat instead of approving it.
REVISE_RESULT = ("Not done yet: the owner replied instead of approving: \"{change}\". If it "
                 "changes this action, make the change and ask for approval again. If it is about "
                 "something else, do that, and ask for this action again only if it still makes sense.")
# A parked call whose tool no longer exists when the owner decides.
GONE = "Not run: this action no longer exists. Say what you meant to do another way."
# A parked call the agent may no longer make (paused, or the tool taken away while it waited).
NOT_ALLOWED = "Not run: the owner approved it, but you may no longer do this. Say so in your report."
# The agent was switched to practice while the call waited: nothing may run now.
IN_PRACTICE = "Not run: the agent is in practice now, so nothing was sent or changed."
# A call whose exact message is already in the task's `contacted`: never a second send.
ALREADY_SENT = "Refused: this was already sent earlier, so it was not sent again."
# A corrupt approval with no call behind it: fail it instead of posting a phantom answer.
EMPTY_APPROVAL = "I couldn't finish: the approval had nothing to run."
# What the model reads before the result of a call the owner edited, so its report names
# what really went out.
EDITED = "The owner changed {fields} before approving, and it ran as changed. "
APPROVAL_DECISIONS = ("approve", "decline", "revise")
# What a call parked on a missing account reads once the owner connected it.
CONNECTED = "The owner connected {account}. Call {tool} again to carry on."
# An approved call whose account was disconnected while it waited.
DISCONNECTED = ("Not run: {account} isn't connected any more. Call it again and the owner "
                "will be asked to connect it.")


def edited(args: dict, edits: Optional[dict]) -> tuple[dict, list[str]]:
    """The call's arguments with the owner's edits, and which ones changed. Only text the call
    already had can be rewritten: no new argument, no change of type."""
    changed = sorted(k for k, v in (edits or {}).items()
                     if isinstance(v, str) and isinstance(args.get(k), str) and v != args[k])
    return {**args, **{k: edits[k] for k in changed}}, changed


def answered(state: list, results: list[dict]) -> list:
    """The owner's decision, standing in for the placeholder each parked call left.

    The loop parks a "Waiting for your approval." tool message per call id, so every call is
    already answered in the saved state. A second message for the same tool_call_id would be a
    duplicate answer, which providers reject, so replace in place and only append a result
    whose placeholder is missing.
    """
    by_id = {r["tool_call_id"]: r for r in results}
    out = []
    for msg in state:
        cid = msg.get("tool_call_id") if isinstance(msg, dict) else None
        if cid is not None and cid in by_id:
            out.append(by_id.pop(cid))  # the placeholder for this call is replaced, not joined
        else:
            out.append(msg)
    return out + list(by_id.values())  # any answer whose placeholder was missing


async def claim(host, db, organization_id: str, task_id: Any, decision: str):
    """Check the decision fits the parked task and claim it ("running"), committed. The task,
    or why not: "not_found" | "conflict" | "invalid"."""
    Task = host.Task
    task = (await db.execute(select(Task).where(
        Task.id == task_id, Task.organization_id == organization_id
    ))).scalar_one_or_none()
    if task is None:
        return "not_found"
    if task.status != "waiting":
        return "conflict"
    pending = task.pending or {}
    if pending.get("kind") == "ask_agent":
        return "not_found"  # nothing for the owner to decide: a teammate is answering this one
    if (pending.get("kind") == "approval") != (decision in APPROVAL_DECISIONS):
        return "invalid"
    claimed = (await db.execute(update(Task).where(
        Task.id == task_id, Task.status == "waiting"
    ).values(status="running").returning(Task.id))).scalar_one_or_none()
    if claimed is None:
        return "conflict"
    task.status = "running"
    await db.commit()
    return task


async def apply(host, db, organization_id: str, task: Any, decision: str, *,
                note: Optional[str] = None, answer: Optional[str] = None, resume: bool = False,
                declined_calls: Optional[list[str]] = None,
                edited_args: Optional[dict[str, dict]] = None,
                shown: Optional[str] = None) -> None:
    """Carry out a claimed decision: run what was approved (or record the decline, the change
    asked for, or the answer), hand the results to the task and post the decision to its
    thread. `task` is the claimed task, or its id from another session. `declined_calls`: on an
    approve, the batch's calls (by tool_call_id) the owner left out; `edited_args`, the text the
    owner rewrote before approving, by tool_call_id then argument (see `edited`). `resume`: the caller runs
    the task now, so it stays "running" instead of going back to the queue. Each call is decided
    again first: a paused or practice-mode agent, a removed tool, a failed check, or a message
    already in `contacted` never runs."""
    Task = host.Task
    if not isinstance(task, Task):
        task = (await db.execute(select(Task).where(
            Task.id == task, Task.organization_id == organization_id
        ))).scalar_one_or_none()
        if task is None:  # deleted between the claim and now
            return
    pending = task.pending or {}
    is_approval = pending.get("kind") == "approval"
    if is_approval and not (pending.get("batch") or pending.get("tool")):
        # No call to run: fail the task rather than answer a phantom call id and queue it to
        # loop forever on a decision that never existed.
        task.pending, task.status, task.attempts = None, "failed", 0
        host.post_message(db, organization_id=organization_id, thread=task.thread,
                          sender=task.agent_id, body=EMPTY_APPROVAL, task_id=task.id)
        await db.commit()
        return
    calls = pending_calls(pending) if is_approval else []
    skip = set(declined_calls or ())
    runs = [decision == "approve" and c.get("tool_call_id", "") not in skip for c in calls]
    # What runs is what the owner approved, their edits included.
    changes = []
    for i, (c, run) in enumerate(zip(calls, runs)):
        args, changed = edited(c.get("args") or {}, (edited_args or {}).get(c.get("tool_call_id", "")))
        calls[i] = {**c, "args": args} if run and changed else c
        changes.append(changed if run else [])
    results: list[dict] = []
    if any(runs):
        ctx, company = await host.context(db, organization_id)
        region = host.region(company)
        sent = set(task.contacted or [])
        # Checked again now, on what will really run: the call may have waited days, and the
        # owner's edits never went through the check made when it was parked.
        agent = host.for_business(host.agent(task.agent_id), company.get("business_type"))
        state = (await host.agent_states(db, organization_id)).get(agent.id, host.default_state())
    for call, run, changed in zip(calls, runs, changes):
        tool = host.tools.get(call["tool"])
        if tool is None:  # renamed or removed while it waited for the owner
            results.append(tool_msg(call.get("tool_call_id", ""), GONE))
            continue
        if run:
            refusal, rule, refused_as = None, None, "deny"
            outcome = host.decide(agent, tool, state, self_started=False).outcome
            if outcome == gate.DENY:
                refusal, rule = NOT_ALLOWED, "no_longer_allowed"
            elif outcome == gate.OBSERVE:
                # The agent was switched to practice while the owner was deciding: the click
                # approves the action, not a way around practice mode.
                refusal, rule, refused_as = IN_PRACTICE, "practice", "observe"
            key = host.sent_key(call["tool"], call.get("args") or {}, region)
            if refusal is None and key is not None and key in sent:
                # The resumed run must never send what an earlier run (or an earlier call in
                # this batch) already recorded as sent.
                refusal, rule = ALREADY_SENT, "duplicate_send"
            if refusal is None and tool.check is not None:
                try:
                    refusal, rule = await host.precheck(tool, ctx, call.get("args") or {}), "precheck"
                except NeedsConnect as e:
                    refusal, rule = DISCONNECTED.format(account=e.account), "needs_connect"
            if refusal:
                host.audit(db, organization_id=organization_id, agent_id=task.agent_id,
                           tool=call["tool"], risk=tool.risk, decision=refused_as, rule=rule,
                           task_id=task.id, actor="owner")
                results.append(tool_msg(call.get("tool_call_id", ""), refusal))
                continue
            if key is not None:
                # Saved before it runs, as in the loop: a crash mid-send must not lead to a
                # second send when the task is retried.
                task.state = answered(task.state or [], [tool_msg(call.get("tool_call_id", ""), MAYBE_SENT)])
                task.contacted = sorted(sent | {key})
                await db.commit()
            try:
                out, ok, _ = await host.execute(tool, ctx, call.get("args") or {})
            except NeedsConnect as e:  # nothing ran: not sent
                out, ok = DISCONNECTED.format(account=e.account), False
            host.audit(db, organization_id=organization_id, agent_id=task.agent_id,
                       tool=call["tool"], risk=tool.risk,
                       decision="approved" if ok else "error",
                       rule="owner_edited" if changed else "owner_approved",
                       task_id=task.id, actor="owner")
            if changed:
                out = EDITED.format(fields=", ".join(changed)) + out
            if ok is not False:  # None: stopped at its timeout, it may have gone out
                # What the owner approved is now sent: the resumed run must not repeat it.
                if key is not None:
                    sent.add(key)
        else:
            out = (REVISE_RESULT.format(change=answer or "") if decision == "revise"
                   else "Owner declined." + (f" Note: {note}" if note else ""))
            host.audit(db, organization_id=organization_id, agent_id=task.agent_id,
                       tool=call["tool"], risk=tool.risk, decision="declined",
                       rule="owner_revised" if decision == "revise" else "owner_declined",
                       task_id=task.id, actor="owner")
        results.append({"role": "tool", "tool_call_id": call.get("tool_call_id", ""), "content": out})
    if any(runs):
        task.contacted = sorted(sent)
    connected = pending.get("kind") == "connect" and decision == "connected"
    if not is_approval:
        results = [{"role": "tool", "tool_call_id": pending.get("tool_call_id", ""),
                    "content": CONNECTED.format(account=pending.get("account"), tool=pending.get("tool"))
                    if connected else f"Owner answered: {answer or note or ''}"}]
    # `shown`: how the chat shows the owner's answer when it differs from what the agent
    # reads (a file's name, not the whole file).
    echo, kind = shown or answer or note or (f"Connected {pending.get('account')}."
                                             if connected else ""), "text"
    if is_approval and decision != "revise":
        # What was decided, as data: the app words it for the owner, and the team's history
        # reads it back as one line.
        partial = any(runs) and not all(runs)
        decided = [{"tool": c["tool"], "args": c.get("args") or {},
                    **({"declined": True} if partial and not run else {})}
                   for c, run in zip(calls, runs)]
        echo, kind = json.dumps({"decision": "approved" if any(runs) else "declined",
                                 "calls": decided, "note": note or None,
                                 "names": await host.decision_names(db, organization_id, decided)}), "decision"

    task.state = answered(task.state or [], results)
    task.pending, task.status, task.attempts = None, "running" if resume else "queued", 0
    host.post_message(db, organization_id=organization_id, thread=task.thread,
                      sender="owner", body=echo, kind=kind, task_id=task.id)
    await db.commit()

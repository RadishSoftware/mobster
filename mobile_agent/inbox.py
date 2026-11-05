"""Needs you (GET /api/inbox): approvals, questions, results to review, repairs and unsent drafts; never history."""

from __future__ import annotations

import time

from .workflows.health import run_outcome

REVIEW_DAYS = 7
# A draft stays as long as a result to look at: then its conversation has moved on without it.
DRAFT_DAYS = REVIEW_DAYS
DRAFT_FIELDS = ("to", "handle", "app", "bundleId", "account", "context", "lastMessage", "draft", "decision", "frameId",
                "yourWords")
QUESTION_KINDS = frozenset({"clarify", "question"})
ACTIVE = frozenset({"queued", "running"})
WORKFLOW_ORIGINS = frozenset({"workflow", "schedule"})


def _ms(seconds):
    return round(seconds * 1000) if isinstance(seconds, (int, float)) else None


def _text(value, limit):
    return " ".join(value.split())[:limit] if isinstance(value, str) and value.strip() else None


def _same_task(run):
    return ((run.app or {}).get("id"), " ".join(str(run.goal or "").casefold().split()))


def _base(run):
    extras = run.extras if isinstance(run.extras, dict) else {}
    app = run.app or {}
    return {"runId": run.id, "goal": run.goal or "", "app": app.get("name") or "", "appId": app.get("id"),
            "device": run.device_id, "deviceName": run.device_name,
            "threadId": extras.get("threadId") if isinstance(extras.get("threadId"), str) else None}


def _result(run):
    summary = run.summary if isinstance(run.summary, dict) else {}
    return {"status": run.status, "answer": _text(summary.get("answer"), 300),
            "reason": _text(summary.get("reason"), 300)}


def collect(runs, workflows, now=None):
    now = time.time() if now is None else now
    waiting, later = [], []
    by_id = {run.id: run for run in runs}
    # When each task last ended Done: anything older of the same task is history.
    done_at = {}
    for run in runs:
        if run_outcome(run) == "done":
            done_at[_same_task(run)] = max(done_at.get(_same_task(run), 0), run.created_at)
    last_runs = {workflow.get("lastRunId"): workflow for workflow in workflows if workflow.get("lastRunId")}
    drafts = []

    for run in runs:
        if run.status not in ACTIVE and now - (run.finished_at or run.created_at) <= DRAFT_DAYS * 86_400:
            drafts.extend(_drafts(run, by_id))
        if run.status in ACTIVE:
            with run.condition:
                approval = dict(run.approval) if run.approval else None
            if approval and approval.get("id") and (approval.get("expiresAt") or 0) > now * 1000:
                kind = "question" if approval.get("kind") in QUESTION_KINDS else "approval"
                waiting.append({"id": f"{kind}:{approval['id']}", "kind": kind, "at": approval.get("requestedAt"),
                                **_base(run), "approval": approval})
            continue
        if run_outcome(run) != "check":
            continue
        workflow = last_runs.get(run.id)
        if run.origin in WORKFLOW_ORIGINS and workflow is None:
            continue  # its workflow ran again since
        finished = run.finished_at or run.created_at
        if now - finished > REVIEW_DAYS * 86_400 or done_at.get(_same_task(run), 0) > run.created_at:
            continue
        later.append({"id": f"review:{run.id}", "kind": "review", "at": _ms(finished), **_base(run),
                      "result": _result(run),
                      **({"workflow": {"id": workflow["id"], "name": workflow.get("name")}} if workflow else {})})

    for workflow in workflows:
        item = _repair(workflow, by_id)
        if item:
            later.append(item)

    # One draft per person a profile writes to: a later task's draft to the same person replaces an earlier one.
    drafts.sort(key=lambda item: item.get("at") or 0, reverse=True)
    seen = set()
    for item in drafts:
        draft = item["draft"]
        recipient = ("handle", draft["handle"]) if draft.get("handle") else ("name", draft.get("to"))
        person = (draft.get("bundleId") or draft.get("app") or item["app"], draft.get("account"),
                  recipient[0], " ".join(str(recipient[1]).casefold().split()))
        if person not in seen:
            seen.add(person)
            later.append(item)

    waiting.sort(key=lambda item: item["approval"].get("expiresAt") or 0)
    later.sort(key=lambda item: item.get("at") or 0, reverse=True)
    return waiting + later


def _drafts(run, runs):
    summary = run.summary if isinstance(run.summary, dict) else {}
    blocks = summary.get("blocks") if isinstance(summary.get("blocks"), list) else []
    brief = summary.get("brief") if isinstance(summary.get("brief"), dict) else {}
    sources = brief.get("sources") if isinstance(brief.get("sources"), dict) else {}
    with run.condition:
        decided = dict(run.block_state)
    items = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "review_queue" or not isinstance(block.get("id"), str):
            continue
        source = sources.get(block["id"])
        if isinstance(source, list) and len(source) == 2 and source[0] != run.id and source[0] in runs:
            continue  # a brief's copy of another task's queue is that task's while it is here: listed once
        for item in block.get("items") or ():
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("to"), str):
                continue
            if not item.get("draft") and item.get("yourWords") is not True:
                continue  # nothing to send: an opener to someone who asked for no DMs, a conversation that can wait
            if (decided.get(f"{block['id']}/{item['id']}") or {}).get("state") in ("approved", "skipped"):
                continue
            items.append({"id": f"draft:{run.id}/{block['id']}/{item['id']}", "kind": "draft",
                          "at": _ms(run.finished_at or run.created_at), **_base(run),
                          "draft": {"blockId": block["id"], "itemId": item["id"],
                                    "action": block.get("action") or "send_message",
                                    **{key: item[key] for key in DRAFT_FIELDS if item.get(key) is not None}}})
    return items


def _repair(workflow, runs):
    health = workflow.get("health") if isinstance(workflow.get("health"), dict) else {}
    attention = health.get("attention") if isinstance(health.get("attention"), dict) else None
    if not attention or attention.get("kind") != "failed":
        return None
    run = runs.get(attention.get("runId")) if isinstance(attention.get("runId"), str) else None
    item = {"id": f"repair:{workflow['id']}", "kind": "repair",
            "workflow": {"id": workflow["id"], "name": workflow.get("name"), "enabled": workflow.get("enabled") is True,
                         "nextAt": workflow.get("nextAt")}}
    reason = _text(attention.get("detail"), 300) or _text(attention.get("words"), 300)
    if run is not None:
        result = _result(run)
        return {**item, **_base(run), "at": _ms(run.finished_at or run.created_at),
                "result": {**result, "reason": result["reason"] or reason}}
    return {**item, "runId": None, "goal": workflow.get("goal") or "", "app": workflow.get("appName") or "",
            "appId": workflow.get("appId"), "device": workflow.get("device"), "deviceName": None, "threadId": None,
            "at": attention.get("at"),
            "result": {"status": workflow.get("lastOutcome"), "answer": None, "reason": reason}}

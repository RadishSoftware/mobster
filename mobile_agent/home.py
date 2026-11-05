"""Home (GET /api/home): your jobs, what finished since you last looked, and this week's totals."""

from __future__ import annotations

import copy
import re
import time

from . import usage
from .workflows.health import first_line, run_outcome

JOBS = 6
FINISHED_OUTCOMES = frozenset({"done", "check"})
ITEM_KINDS = {"review_queue": "items", "media_list": "items", "table": "rows", "checklist": "items"}
NOUNS = {"review_queue": "conversations", "media_list": "videos", "table": "rows", "checklist": "checks"}
# A workflow run's step: its dispatch's key, then :g<run>: (a graph run) or :b<run>: (a batch of step cards).
BATCH_KEY = re.compile(r"^(workflow:([0-9a-f]{1,32}):[^:]+:[^:]+):[bg]([0-9a-f]{12}):")
WORKFLOW_KEY = re.compile(r"^workflow:([0-9a-f]{1,32}):")
PREVIEW = {"review_queue": ("items", 3), "media_list": ("items", 3), "table": ("rows", 4), "checklist": ("items", 3)}
HISTORY = 100


def _ms(seconds):
    return round(seconds * 1000) if isinstance(seconds, (int, float)) else None


def _summary(run):
    return run.summary if isinstance(getattr(run, "summary", None), dict) else {}


def _blocks(run):
    envelope = _summary(run).get("blocks")
    return [block for block in (envelope or {}).get("blocks") or () if isinstance(block, dict)] \
        if isinstance(envelope, dict) else []


def is_brief(run):
    return isinstance(_summary(run).get("brief"), dict)


def job_key(run):
    found = BATCH_KEY.match(getattr(run, "idempotency_key", None) or "")
    return f"batch:{found[3]}" if found else run.id


def workflow_of(run):
    found = WORKFLOW_KEY.match(getattr(run, "idempotency_key", None) or "")
    return found[1] if found else None


def items_of(run):
    if is_brief(run):
        return {}
    out = {}
    for block in _blocks(run):
        field = ITEM_KINDS.get(block.get("type"))
        if field:
            out[block["type"]] = out.get(block["type"], 0) + len(block.get(field) or ())
    return out


def waiting_drafts(run):
    if is_brief(run):
        return 0
    state = getattr(run, "block_state", None) or {}
    return sum(1 for block in _blocks(run) if block.get("type") == "review_queue"
               for item in block.get("items") or () if item.get("draft") and f"{block['id']}/{item['id']}" not in state)


def counts(runs, since_ms, until_ms=None):
    groups = {}
    for run in runs:
        groups.setdefault(job_key(run), []).append(run)
    jobs, kinds, need = 0, {}, 0
    for members in groups.values():
        if any(getattr(run, "finished_at", None) is None for run in members):
            continue
        last = max(_ms(run.finished_at) for run in members)
        if last < since_ms or (until_ms is not None and last >= until_ms):
            continue
        if all(run_outcome(run) in FINISHED_OUTCOMES for run in members):
            jobs += 1
        for run in members:
            for kind, n in items_of(run).items():
                kinds[kind] = kinds.get(kind, 0) + n
            need += waiting_drafts(run)
    return {"jobs": jobs, "items": sum(kinds.values()), "kinds": kinds, "needYou": need}


def preview(block):
    if not isinstance(block, dict):
        return None
    out = copy.deepcopy(block)
    field, keep = PREVIEW.get(block.get("type"), (None, None))
    if field:
        out[field] = list(block.get(field) or ())[:keep]
        out["total"] = len(block.get(field) or ())
        if block.get("type") == "review_queue":
            out["drafts"] = sum(1 for item in block.get("items") or () if item.get("draft"))
        if block.get("type") == "checklist":
            out["failed"] = sum(1 for item in block.get("items") or () if item.get("status") == "fail")
        if block.get("type") == "table":
            out["columns"] = list(block.get("columns") or ())[:3]
    if block.get("type") == "markdown":
        out["text"] = str(block.get("text") or "")[:600]
    return out


def learned_of(run):
    summary = _summary(run)
    replay, program = summary.get("replay"), summary.get("program")
    if (isinstance(replay, dict) and replay.get("mode") in ("replayed", "healed")
            and isinstance(replay.get("seconds"), (int, float))):
        return {"seconds": replay["seconds"], "costUsd": replay.get("costUsd") or 0.0, "first": replay.get("first")}
    if isinstance(program, dict) and program.get("replayed"):
        elapsed = summary.get("elapsed_ms")
        return {"seconds": elapsed / 1000 if isinstance(elapsed, (int, float)) else None,
                "costUsd": getattr(run, "cost_usd", None), "first": program.get("first")}
    return None


def last_result(run):
    if run is None or getattr(run, "finished_at", None) is None:
        return None
    summary = _summary(run)
    brief = summary.get("brief") if isinstance(summary.get("brief"), dict) else None
    elapsed = summary.get("elapsed_ms")
    seconds = (brief.get("seconds") if brief else elapsed / 1000 if isinstance(elapsed, (int, float))
               else run.finished_at - run.created_at)
    cost = brief.get("spentUsd") if brief else getattr(run, "cost_usd", None)
    blocks = _blocks(run)
    # A brief's waiting drafts are its tasks': count them through the task that holds each card.
    return {"runId": run.id, "status": run.status, "outcome": run_outcome(run), "at": _ms(run.finished_at),
            "seconds": round(seconds, 1) if isinstance(seconds, (int, float)) else None,
            "costUsd": round(cost, 6) if isinstance(cost, (int, float)) else None,
            "answer": first_line(summary.get("answer") or summary.get("full_answer") or summary.get("reason")),
            "preview": preview(blocks[0]) if blocks else None, "cards": len(blocks),
            "items": sum(items_of(run).values()) if not brief else None,
            "brief": bool(brief), "learned": learned_of(run)}


def _brief_drafts(run, runs):
    brief = _summary(run).get("brief")
    if not isinstance(brief, dict):
        return waiting_drafts(run)
    sources = {source for source in brief.get("runs") or () if isinstance(source, str)}
    return sum(waiting_drafts(runs[source]) for source in sources if source in runs)


def jobs(workflows, runs, inbox_items, running=None):
    running = running or {}
    by_workflow = {}
    for run in runs.values():
        owner = workflow_of(run)
        if owner:
            by_workflow.setdefault(owner, []).append(run)
    cards = []
    for workflow in workflows:
        last = runs.get(workflow.get("lastRunId") or "")
        mine = {run.id for run in by_workflow.get(workflow["id"], ())}
        waiting = sum(1 for item in inbox_items if (item.get("workflow") or {}).get("id") == workflow["id"]
                      or item.get("runId") in mine)
        drafts = _brief_drafts(last, runs) if last is not None and getattr(last, "finished_at", None) else 0
        health = workflow.get("health") or {}
        attention = health.get("attention") if isinstance(health.get("attention"), dict) else None
        cards.append({"id": workflow["id"], "name": workflow.get("name"), "appId": workflow.get("appId"),
                      "appName": workflow.get("appName"), "cron": workflow.get("cron"),
                      "timezone": workflow.get("timezone"), "enabled": bool(workflow.get("enabled")),
                      "nextAt": workflow.get("nextAt"), "produces": workflow.get("produces"),
                      "running": workflow["id"] in running, "threadId": workflow.get("lastThreadId"),
                      "last": last_result(last), "needsYou": waiting + drafts, "drafts": drafts,
                      "attention": ({"kind": attention.get("kind"), "words": attention.get("words")} if attention
                                    else None)})

    def order(card):
        scheduled = card["enabled"] and card["nextAt"] is not None
        last_at = (card["last"] or {}).get("at") or 0
        return (0 if scheduled else 1, card["nextAt"] if scheduled else -last_at)
    cards.sort(key=order)
    return cards[:JOBS]


def summary(runtime, since_ms=None, now=None):
    now = time.time() if now is None else now
    with runtime.lock:
        runs = dict(runtime.runs)
    try:
        workflows = runtime.workflows.list_with_health() if getattr(runtime, "workflows", None) is not None else []
        running = runtime.workflows.running() if workflows else {}
    except Exception:  # noqa: BLE001 -- a journal that can't list workflows leaves Home its counts
        workflows, running = [], {}
    try:
        inbox_items = runtime.inbox().get("items") or []
    except Exception:  # noqa: BLE001
        inbox_items = []
    week_from = usage.week_start(now)
    oldest = min((_ms(run.created_at) for run in runs.values()), default=None)
    partial = len(runs) >= HISTORY and oldest is not None and oldest > week_from
    try:
        spent = round(runtime.journal.spend_since(week_from), 6)
    except Exception:  # noqa: BLE001 -- an unreadable ledger is no spend line, never a made-up one
        spent = None
    try:
        plan = runtime.journal.plan_since(week_from)
        plan_tokens, equivalent = plan["tokens"], plan["apiEquivalentUsd"]
    except Exception:  # noqa: BLE001 -- as with spend: no line rather than a made-up one
        plan_tokens = equivalent = None
    week = {"from": round(week_from), **counts(runs.values(), week_from), "spentUsd": spent, "partial": partial,
            "planTokens": plan_tokens, "apiEquivalentUsd": round(equivalent, 6) if equivalent is not None else None}
    out = {"jobs": jobs(workflows, runs, inbox_items, running), "jobCount": len(workflows), "week": week,
           "needsYou": len(inbox_items), "now": _ms(now)}
    if isinstance(since_ms, (int, float)) and not isinstance(since_ms, bool) and 0 < since_ms <= _ms(now):
        out["since"] = {"at": round(since_ms), **counts(runs.values(), since_ms)}
    return out

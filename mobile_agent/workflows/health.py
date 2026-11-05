"""What a workflow's row says about it (GET /api/workflows, each workflow's ``health``)."""

from __future__ import annotations

from . import starters

OUTCOMES = frozenset({"done", "check", "couldnt_finish", "stopped", "declined"})
# Every status as an outcome (None while the run goes). A status that means "it finished" is check here: only proof
# makes it done. An unknown status is check, never done.
STATUS_OUTCOMES = {
    "queued": None, "running": None,
    "completed": "check", "completed_unverified": "check", "expected_text_visible": "check", "demo_complete": "check",
    "inconsistent_completion": "check", "completion_not_confirmed": "check",
    "stopped": "stopped", "user_condition_met": "stopped", "approval_denied": "declined",
    "interrupted": "couldnt_finish", "needs_clarification": "couldnt_finish", "error": "couldnt_finish",
    "blocked": "couldnt_finish", "needs_text_helper": "couldnt_finish", "no_progress": "couldnt_finish",
    "max_steps": "couldnt_finish", "timeout": "couldnt_finish", "approval_timeout": "couldnt_finish",
    "spend_cap": "couldnt_finish", "duplicate_effect_blocked": "couldnt_finish",
    "duplicate_text_blocked": "couldnt_finish",
    "invalid_action": "couldnt_finish", "invalid_target": "couldnt_finish", "expected_text_missing": "couldnt_finish",
    "preview": "stopped",
}
FINISHED = frozenset({"completed", "completed_unverified", "expected_text_visible"})
# Stops by design (nobody answered, the spending limit, a repeat refused): never a failure to repair.
SAFE_STOPS = frozenset({"approval_timeout", "spend_cap", "duplicate_effect_blocked"})
# A run that ended because the iPhone wasn't there to use (agent_hooks.STOP_CODES that a person fixes at the phone).
PHONE_CODES = frozenset({"phone_locked", "unlock_declined", "passcode_failed", "face_id", "unplugged",
                         "keychain_locked"})

# Dispatch outcomes that are not run statuses (service.py, records.py): how each reads on the row.
PHONE_SKIPS = {
    "skipped_busy": "Skipped: your iPhone was busy with another task",
    "busy": "Skipped: your iPhone was busy with another task",
    "skipped_offline": "Skipped: your iPhone wasn’t connected",
    "offline": "Skipped: your iPhone wasn’t connected",
}
LIMIT_SKIPS = {
    "skipped_monthly_limit": "Skipped: this month’s spending limit is reached",
    "monthly_limit": "Skipped: this month’s spending limit is reached",
    "skipped_daily_limit": "Skipped: the daily limit for scheduled runs is reached",
    "skipped_interval_limit": "Skipped: too soon after the last scheduled run",
}
FAILURES = {
    "dispatch_failed": "Last run couldn’t start",
    "interrupted": "Last run was interrupted",
    "time_limit": "Last run reached its time limit",
    "loop_limit": "Last run reached its repeat limit",
    "condition_unknown": "Last run couldn’t check its condition",
    "delivery_failed": "Last run couldn’t send its result",
    "error": "Last run couldn’t finish",
}
# A run that ended, then left before Mobster read how (its task deleted, on its own or with its conversation, or out of
# the history it keeps): Mobster can't say how it ended, so it is never a failure. The row says so in grey and Needs you
# doesn't list it (the lead, 5 Nov 2026: expired isn't failed); a failure before it still leads (workflow_health).
UNKNOWN_ENDS = {"run_expired": "Last run is no longer kept"}
GOING = frozenset({"claimed", "started"})
# Outcomes that say nothing about the workflow itself: they never raise attention and never hide an older one.
QUIET = frozenset({"skipped_missed"})
LINE_MAX = 140


def _record(value):
    return isinstance(value, dict)


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def result_fields(run):
    """The result's fields: the run's summary over its last ``result`` event."""
    events = getattr(run, "events", None) or ()
    result = next((event for event in reversed(events) if _record(event) and event.get("event") == "result"), None)
    summary = getattr(run, "summary", None)
    source = {**(result or {}), **summary} if _record(summary) else dict(result or {})
    proof = [item for item in source.get("proof") or () if _record(item) and _text(item.get("quote"))] \
        if isinstance(source.get("proof"), list) else []
    outcome = source.get("outcome") if source.get("outcome") in OUTCOMES else None
    return {"outcome": outcome, "proof": proof, "answer": _text(source.get("answer")),
            "reason": _text(source.get("reason")), "stopCode": _text(source.get("stop_code"))}


def run_outcome(run):
    """done | check | couldnt_finish | stopped | declined, or None while the run goes (result-status.ts runOutcome)."""
    status = getattr(run, "status", None)
    if status in ("queued", "running") or getattr(run, "finished_at", None) is None:
        return None
    fields = result_fields(run)
    outcome = fields["outcome"] or STATUS_OUTCOMES.get(status, "check")
    if not fields["outcome"] and outcome == "check" and status in FINISHED and fields["proof"]:
        outcome = "done"
    if outcome == "done" and not fields["proof"]:
        outcome = "check"
    if outcome == "check" and getattr(run, "reviewed_ok", False):
        outcome = "done"
    return outcome


def verified(run):
    """The run finished and it is proven (or a person marked it OK)."""
    return run is not None and run_outcome(run) == "done"


def first_line(text, limit=LINE_MAX):
    """The first line of an answer, cut at a word to ``limit`` characters."""
    line = next((part.strip() for part in (text or "").splitlines() if part.strip()), None)
    if line is None or len(line) <= limit:
        return line
    return line[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def success_of(run):
    """{"at", "runId", "line"} for a verified run: when it finished (ms) and its answer's first line."""
    finished = getattr(run, "finished_at", None)
    return {"at": round(finished * 1000) if finished is not None else None, "runId": run.id,
            "line": first_line(result_fields(run)["answer"])}


def _attention(kind, words, detail=None, run_id=None, at=None):
    return {"kind": kind, "words": words, "detail": detail, "runId": run_id, "at": at}


def attention_for(outcome, run, at=None, plain=True):
    """What the newest ended attempt asks of you, or None."""
    run_id = getattr(run, "id", None)
    if outcome is None or outcome in GOING or outcome in QUIET:
        return None
    if outcome in PHONE_SKIPS:
        return _attention("phone", PHONE_SKIPS[outcome], "Keep it plugged into this Mac and unlocked.", run_id, at)
    if outcome in LIMIT_SKIPS:
        return _attention("limit", LIMIT_SKIPS[outcome], None, run_id, at)
    if outcome == "missing_values":
        return _attention("input", "A value it needs is missing", "Edit the workflow and give each blank a value.",
                          run_id, at)
    if outcome in UNKNOWN_ENDS:
        return _attention("unknown", UNKNOWN_ENDS[outcome], None, None, at)
    fields = result_fields(run) if run is not None and getattr(run, "finished_at", None) is not None else None
    if fields and fields["stopCode"] in PHONE_CODES:
        return _attention("phone", "Last run stopped: your iPhone wasn’t available", fields["reason"], run_id, at)
    if not plain:
        # A batch ends completed only when every step ended done; stopped is your own Stop.
        if outcome in FAILURES or STATUS_OUTCOMES.get(outcome) == "couldnt_finish" and outcome not in SAFE_STOPS:
            return _attention("failed", FAILURES.get(outcome, "Last run couldn’t finish"),
                              fields["reason"] if fields else None, run_id, at)
        return None
    if fields is not None:
        if run_outcome(run) == "couldnt_finish" and getattr(run, "status", None) not in SAFE_STOPS:
            return _attention("failed", "Last run couldn’t finish", fields["reason"], run_id, at)
        return None
    if outcome in FAILURES or STATUS_OUTCOMES.get(outcome) == "couldnt_finish" and outcome not in SAFE_STOPS:
        return _attention("failed", FAILURES.get(outcome, "Last run couldn’t finish"), None, run_id, at)
    return None


def missing_input(workflow, filled=None):
    """Attention for a scheduled workflow that can't fill what it needs"""
    if not workflow.get("enabled"):
        return None
    repeat = workflow.get("repeat") or {}
    if repeat.get("kind") == "each" and not repeat.get("rows"):
        return _attention("input", "Its rows are missing", "Edit the workflow and save the rows it repeats over.")
    keys = set(filled or ())
    if repeat.get("rows"):
        keys |= set.intersection(*[set(starters.clean_values(row)) for row in repeat["rows"]])
    missing = [name for name in starters.unfilled(workflow.get("goal")) if starters._key(name) not in keys]
    if missing:
        return _attention("input", f"It needs a value for {missing[0]}",
                          "Edit the workflow and give each blank a default.")
    return None


def is_plain(workflow):
    """One task and no repeat: it runs as one task, whose own result is the workflow's."""
    from ..graph.chain import record_is_plain
    return record_is_plain(workflow)


def latest_success(workflow, attempts, runs):
    """The newest verified success among ``attempts``, else the one the record keeps (``lastSuccess``)."""
    plain = is_plain(workflow)
    success = None
    for outcome, run_id, claimed_at in attempts:
        run = runs.get(run_id) if isinstance(run_id, str) else None
        if plain and verified(run):
            success = success_of(run)
            break
        if not plain and outcome == "completed":
            success = success_of(run) if run is not None else {"at": claimed_at, "runId": None, "line": None}
            break
    stored = workflow.get("lastSuccess")
    if (_record(stored) and isinstance(stored.get("at"), (int, float))
            and (success is None or (success["at"] or 0) < stored["at"])):
        success = {"at": stored["at"], "runId": stored.get("runId"), "line": stored.get("line")}
    return success


RECENT = 10
RUN_WORDS = {"done": "done", "check": "check", "couldnt_finish": "failed", "stopped": "stopped", "declined": "stopped"}


def outcome_word(outcome, run, plain=True):
    """One attempt as the row's history reads it: done, check, failed, stopped, skipped or running."""
    if outcome in GOING:
        return "running"
    if outcome in PHONE_SKIPS or outcome in LIMIT_SKIPS or outcome == "missing_values":
        return "skipped"
    if outcome in UNKNOWN_ENDS:
        return "unknown"
    if plain and run is not None:
        found = run_outcome(run)
        return RUN_WORDS.get(found, "check") if found else "running"
    if not plain and outcome == "completed":
        return "done"
    if outcome in SAFE_STOPS or STATUS_OUTCOMES.get(outcome) in ("stopped", "declined") or outcome == "cancelled":
        return "stopped"
    if outcome in FAILURES or STATUS_OUTCOMES.get(outcome) == "couldnt_finish":
        return "failed"
    return "check"


def recent_of(workflow, attempts, runs):
    """Its last ``RECENT`` attempts, newest first, as {"outcome", "at", "runId"}."""
    plain = is_plain(workflow)
    recent = []
    for outcome, run_id, claimed_at in attempts:
        if outcome in QUIET:
            continue
        run = runs.get(run_id) if isinstance(run_id, str) else None
        recent.append({"outcome": outcome_word(outcome, run, plain), "at": claimed_at,
                       "runId": run_id if isinstance(run_id, str) else None})
        if len(recent) == RECENT:
            break
    return recent


def workflow_health(workflow, attempts, runs, filled=None):
    """A workflow's ``health``."""
    plain = is_plain(workflow)
    attention = missing_input(workflow, filled)
    if attention is None:
        unknown = None
        for outcome, run_id, claimed_at in attempts:
            if outcome in QUIET:
                continue
            if outcome in UNKNOWN_ENDS:
                # An end Mobster can't read never hides an older one: a failure under it still leads, a success
                # or nothing leaves the quiet line (review of #2089: fail closed).
                unknown = unknown or attention_for(outcome, None, claimed_at, plain)
                continue
            run = runs.get(run_id) if isinstance(run_id, str) else None
            found = attention_for(outcome, run, claimed_at, plain)
            attention = found if unknown is None or (found and found["kind"] == "failed") else unknown
            break
        else:
            attention = unknown
    return {"lastSuccess": latest_success(workflow, attempts, runs), "attention": attention,
            "recent": recent_of(workflow, attempts, runs)}

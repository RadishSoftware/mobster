"""One sentence for why a run failed, from its trace: the class and the step.

    failure_line(summary, events)   -> {"class", "step", "line"}, or None for a run that did not fail
    of_check(result)                -> the same for a `mobster verify` result.json (the PR comment's)

``summary`` is the run's result (the ``result`` event, or the ``summary`` of ``run_finished``): its status, outcome,
reason and code. ``events`` is the run's journal: the frontier's decisions, actions, refusals and failures. Nothing
here calls a model or reads a screen, so the line costs nothing and a run carries it the moment it ends.

The classes, first match wins:

    blocked_by_prompt   a lock screen, a passcode, Face ID, an iOS dialog Mobster leaves to you, or an approval nobody
                        answered
    wrong_app           an app Mobster never opens, or the check's app not in front at the end
    app_crashed         the app stopped answering (a read or a tap timed out) and the task could not go on
    premature_done      it called the task done, and the screen never showed every part finished
    stuck               the same step was refused or failed three times
    out_of_steps        every step, every second or the whole cost limit used
    gave_up             it ended with its own "can't" (BLOCKED), or stopped seeing progress
    error               anything else that ended the run: a model or transport error, an unplugged phone

A run that finished (or that you stopped, or declined) has no line. A run that said done and was wrong has none
either: the trace holds no evidence of it, only a judge does.
"""

import re

from . import secret_filter

PROMPT_CODES = frozenset({"phone_locked", "unlock_declined", "passcode_failed", "face_id", "app_locked",
                          "apple_confirmation", "keychain_locked", "system_dialog"})
# How a run ends without failing: it is done, you stopped it (or Mobster quit), or you said no.
CLEAN = frozenset({"completed", "stopped", "interrupted", "approval_denied", "demo_complete", "done"})
UNPROVEN = frozenset({"completion_not_confirmed", "inconsistent_completion"})
OUT_OF_STEPS = {"max_steps": "Ran out of steps", "timeout": "Ran out of time", "spend_cap": "Reached its cost limit",
                "budget": "Reached its cost limit"}
TIMED_OUT = re.compile(r"TimeoutError|timed out|deadline exceeded", re.I)
STUCK = re.compile(r"failed three times|refused the same step|stopped instead of trying again", re.I)
STUCK_TIMED_OUT = re.compile(r"failed three times \((?:timed out|TimeoutError)\)", re.I)
MODEL_CALL = re.compile(r"\b(?:Anthropic|OpenAI|Gemini|Vertex|Codex)\b")
LIMIT = 240


def _text(value):
    return " ".join(str(value or "").split())


def _first_sentence(text, limit=160):
    """The text's first sentence, with the next one when the first is a few words ("Could not complete."), at most
    ``limit`` characters."""
    text = _text(text)
    out = ""
    for part in re.split(r"(?<=[.!?])\s+", text):
        out = f"{out} {part}".strip()
        if len(out) >= 40:
            break
    return out if len(out) <= limit else out[:limit - 1].rstrip() + "…"


def _sentence(text):
    text = _first_sentence(text)
    return text if not text or text[-1] in ".!?…" else text + "."


def _decisions(events):
    return [e for e in events if e.get("event") == "frontier_decision"]


def _step(event):
    """A frontier event's step for people: the loop counts from 0."""
    value = event.get("step") if isinstance(event, dict) else None
    return value + 1 if isinstance(value, int) and not isinstance(value, bool) else None


def _last(events, *names):
    return next((e for e in reversed(events) if e.get("event") in names), None)


def _total(summary, events):
    for key in ("decisions", "steps"):
        value = summary.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    decisions = _decisions(events)
    return (_step(decisions[-1]) or len(decisions)) if decisions else None


def _doing(events):
    """What the last action was, in a few words: ``Tap “Brunch Crew”``."""
    action = _last(events, "frontier_action")
    if action is None or not action.get("operation"):
        return None
    label = _text(action.get("target_label"))
    if not label or label == "None":
        return str(action["operation"]).replace("_", " ").capitalize()
    if len(label) > 40:
        label = label[:39].rstrip() + "…"
    return f"{str(action['operation']).replace('_', ' ').capitalize()} “{label}”"


def _at(step):
    return f"at step {step}" if step else "before its first step"


def _failure(klass, step, line):
    line = secret_filter.redact(_text(line))
    if len(line) > LIMIT:
        line = line[:LIMIT - 1].rstrip() + "…"
    return {"class": klass, "step": step, "line": line}


def failure_line(summary, events=()):
    """{"class", "step", "line"} for a run that ended without finishing, else None (see the module docstring)."""
    summary = summary if isinstance(summary, dict) else {}
    events = [e for e in events or () if isinstance(e, dict)]
    status, outcome = summary.get("status"), summary.get("outcome")
    code, reason = summary.get("code"), _text(summary.get("reason"))
    if status in CLEAN and not (status == "completed" and outcome == "check"):
        return None
    if status is None:
        return None
    total = _total(summary, events)
    # What ended the run is at its end: an error from earlier in the run is one it recovered from.
    tail = [e for e in events if e.get("event") != "result"][-8:]
    failed = _last(tail, "frontier_failed", "frontier_error", "step_interrupted")
    failed_text = _text(failed.get("error") or failed.get("detail")) if failed else ""
    if code in PROMPT_CODES or status == "approval_timeout":
        if status == "approval_timeout":
            return _failure("blocked_by_prompt", total, f"Waited {_at(total)} for your answer to an approval and got "
                                                         "none.")
        return _failure("blocked_by_prompt", total,
                        f"Blocked {_at(total)} by a prompt: {_sentence(reason) or 'an iOS prompt needed you.'}")
    if code == "blocked_app":
        return _failure("wrong_app", total, f"Opened an app Mobster never opens {_at(total)}: {_sentence(reason)}")
    stuck = code == "stuck" or bool(STUCK.search(reason))
    # An error that ended the run was a read or a tap timing out; a step that failed three times says why in its reason.
    # A call to the model provider that timed out is the network's, not the app's.
    model_side = bool(summary.get("model_error") or MODEL_CALL.search(failed_text) or MODEL_CALL.search(reason))
    timed_out = (status == "error" and bool(TIMED_OUT.search(failed_text) or TIMED_OUT.search(reason))) \
        or bool(STUCK_TIMED_OUT.search(reason))
    relaunch_failed = any(e.get("event") == "frontier_app_relaunched" and e.get("ok") is False for e in tail)
    if not model_side and (timed_out or (relaunch_failed and status in ("error", "blocked"))):
        step = _step(failed) or total
        return _failure("app_crashed", step, f"The app stopped answering {_at(step)}, and the task could not go on"
                                              + (" (Mobster could not reopen it)." if relaunch_failed else "."))
    if status in UNPROVEN or (status == "completed" and outcome == "check"):
        done = next((d for d in reversed(_decisions(events)) if "DONE" in (d.get("ops") or ())), None)
        step = _step(done) or total
        proof = _sentence(reason) or "The screen never showed every part finished."
        return _failure("premature_done", step, f"Said it was done {_at(step)}, without proof: {proof}")
    if stuck:
        step = _step(_last(events, "frontier_stuck")) or _step(failed) or total
        return _failure("stuck", step, f"Stuck {_at(step)}: {_sentence(reason) or 'the same step failed three times.'}")
    if status in OUT_OF_STEPS:
        doing = _doing(events)
        return _failure("out_of_steps", total, f"{OUT_OF_STEPS[status]} {_at(total)}"
                                                + (f"; its last action was {doing}." if doing else "."))
    if status == "blocked" or status == "no_progress":
        said = next((d for d in reversed(_decisions(events)) if "BLOCKED" in (d.get("ops") or ())), None)
        step = _step(said) or total
        why = _sentence(reason or summary.get("answer")) or "it saw no way on."
        return _failure("gave_up", step, f"Gave up {_at(step)}: {why}")
    step = _step(failed) or total
    return _failure("error", step, f"Stopped by an error {_at(step)}: {_sentence(reason) or 'the run ended with '
                                                                                             f'status {status}.'}")


# -- a `mobster verify` check -----------------------------------------------------------------------------------

def of_check(result):
    """The failure line for a verify ``result.json`` (verdict, ``reason`` {class, message}, ``agent`` {status, note,
    turns}), or None: a check that passed, or one that couldn't run for a reason outside the run (no simulator, no
    key), which its own message says."""
    result = result if isinstance(result, dict) else {}
    if result.get("verdict") in (None, "passed"):
        return None
    reason = result.get("reason") if isinstance(result.get("reason"), dict) else {}
    klass, message = reason.get("class"), _text(reason.get("message"))
    agent = result.get("agent") if isinstance(result.get("agent"), dict) else {}
    status = agent.get("status")
    turns = agent.get("turns") if isinstance(agent.get("turns"), int) else len(result.get("steps") or ()) or None
    if klass == "app_crashed":
        return _failure("app_crashed", turns, f"The app crashed {_at(turns)}: {_sentence(message)}")
    if klass == "app_not_running":
        return _failure("wrong_app", turns, _sentence(message))
    if klass == "budget" or status in ("budget", "spend_cap"):
        return _failure("out_of_steps", turns, f"Reached its cost limit {_at(turns)}.")
    if status in ("max_steps", "timeout"):
        return _failure("out_of_steps", turns, f"{OUT_OF_STEPS[status]} {_at(turns)}.")
    if status in ("approval_denied", "approval_timeout"):
        return _failure("blocked_by_prompt", turns, f"Smart stopped {_at(turns)} at an approval it was not given.")
    if status == "blocked" or klass == "blocked":
        return _failure("gave_up", turns, f"Gave up {_at(turns)}: {_sentence(agent.get('note')) or _sentence(message)}")
    if klass in ("assertion", "finding") and status == "completed":
        what = _sentence(message).rstrip(".")
        return _failure("premature_done", turns, f"Said it was done {_at(turns)}, but {what}.")
    return None

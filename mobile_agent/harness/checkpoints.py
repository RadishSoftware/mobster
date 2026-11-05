"""Checkpoints and picking up (SPEC §3.1 F6): a checkpoint holds no approval text or code,
and pickup is never automatic."""

import json
import logging
import threading
import time

from .. import harness_api, permissions
from ..api_errors import APIError
from ..run_executor import RunExecutor
from ..secret_filter import redact

log = logging.getLogger("mobster.harness")

SCHEMA, VERSION = "harness", 1
MAX_BYTES = 64_000
PICKUP_STATUSES = frozenset({"interrupted", "error", "stopped", "approval_timeout"})
PICKUP_CODES = frozenset({"unplugged", "phone_locked"})
MORE_TURNS = 50
MAX_TURNS = 200
PICKUP_FEEDBACK = ("You're picking up a task after it was interrupted. The last action may or may not have happened: "
                   "look at the screen before repeating anything.")
MESSAGE_NOTE = "The user wrote to you while you worked on this earlier: "
MESSAGE_NOTE_CHARS = 2000


def migrate(connection, from_version):
    """Schema ``harness`` v1: run_checkpoints (one row per run) and harness_settings."""
    if from_version < 1:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS run_checkpoints(run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE, "
            "step INTEGER NOT NULL, data TEXT NOT NULL, bytes INTEGER NOT NULL, updated_at REAL NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS harness_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def _clean(value):
    """Every string through the secret filter (idempotent: a state the seam's hook saw is redacted already)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def fit(state):
    """``state`` cleaned and cut to MAX_BYTES of JSON: the oldest history lines go first, then the oldest notes."""
    state = _clean(dict(state or {}))
    state["history"] = [str(line)[:400] for line in state.get("history") or ()]
    state["notes"] = [str(note)[:400] for note in state.get("notes") or ()]
    state["plan"] = str(state.get("plan") or "")[:1200]
    data = json.dumps(state, ensure_ascii=False, allow_nan=False)
    while len(data.encode()) > MAX_BYTES and (state["history"] or state["notes"]):
        if state["history"]:
            del state["history"][0]
        else:
            del state["notes"][0]
        data = json.dumps(state, ensure_ascii=False, allow_nan=False)
    if len(data.encode()) > MAX_BYTES:
        return None
    return state


def save(journal, run_id, state):
    """Keep ``state`` as the run's latest checkpoint. False when there was nothing to keep."""
    state = fit(state)
    if state is None or journal is None or getattr(journal, "connection", None) is None:
        return False
    data = json.dumps(state, ensure_ascii=False, allow_nan=False)
    step = int(state.get("step") or 0)
    with journal.transaction() as connection:
        connection.execute(
            "INSERT INTO run_checkpoints(run_id, step, data, bytes, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET step=excluded.step, data=excluded.data, bytes=excluded.bytes, "
            "updated_at=excluded.updated_at", (run_id, step, data, len(data.encode()), time.time()))
    return True


def load(journal, run_id):
    """The run's latest checkpoint, or None."""
    if journal is None or getattr(journal, "connection", None) is None:
        return None
    try:
        with journal.transaction() as connection:
            row = connection.execute("SELECT data FROM run_checkpoints WHERE run_id=?", (run_id,)).fetchone()
    except Exception:  # noqa: BLE001 -- the track is off (no table) or the journal is closing: nothing to pick up
        return None
    if not row:
        return None
    try:
        state = json.loads(row[0])
    except ValueError:
        return None
    return state if isinstance(state, dict) else None


def delete(journal, run_id):
    if journal is None or getattr(journal, "connection", None) is None:
        return
    with journal.transaction() as connection:
        connection.execute("DELETE FROM run_checkpoints WHERE run_id=?", (run_id,))


def stop_code(summary):
    summary = summary if isinstance(summary, dict) else {}
    return summary.get("code") or summary.get("stop_code")


def unresolved_effect(run):
    """A durable action intent without its matching acknowledgment cannot be picked up."""
    pending = None
    for event in getattr(run, "events", ()) or ():
        if not isinstance(event, dict):
            return True
        kind = event.get("event")
        if kind in ("action_started", "app_launch_started"):
            identity = event.get("operation") if kind == "action_started" else event.get("app")
            if pending is not None or not isinstance(identity, str) or not identity:
                return True
            pending = (kind, identity)
        elif kind in ("action_acknowledged", "action_not_dispatched", "app_launch_acknowledged"):
            started = "app_launch_started" if kind == "app_launch_acknowledged" else "action_started"
            identity = event.get("app") if started == "app_launch_started" else event.get("operation")
            if pending != (started, identity):
                return True
            pending = None
    return pending is not None


def uncertain_result(run):
    summary = getattr(run, "summary", None)
    if not isinstance(summary, dict):
        return True
    return (bool(summary.get("persistence_failed")) or summary.get("code") in ("unknown_outcome", "journal")
            or summary.get("stop_code") in ("unknown_outcome", "journal"))


def resumable(run):
    """Whether ``run`` ended a way it can be picked up from (a checkpoint is checked separately)."""
    if run is None or getattr(run, "finished_at", None) is None:
        return False
    if getattr(run, "mode", "live") != "live" or getattr(run, "engine", None) != "smart":
        return False
    status = getattr(run, "status", None)
    return status in PICKUP_STATUSES or (status == "blocked" and stop_code(run.summary) in PICKUP_CODES)


class Writer:
    """Keeps checkpoints off the agent's thread."""

    def __init__(self):
        self._condition = threading.Condition()
        self._pending = {}        # run id -> ("save", journal, state) | ("delete", journal, None)
        self._busy = False
        self._workers = RunExecutor(max_workers=1)
        self._writing = None  # the loop's Future while it runs; it ends after 30 s with nothing to write

    def submit(self, run_id, kind, journal, state=None):
        with self._condition:
            self._pending[run_id] = (kind, journal, state)
            if self._writing is None or self._writing.done():
                self._writing = self._workers.submit("mobster-checkpoints", self._loop)
            self._condition.notify_all()

    def _loop(self):
        while True:
            with self._condition:
                while not self._pending:
                    if not self._condition.wait(30):
                        self._writing = None
                        return
                run_id, (kind, journal, state) = next(iter(self._pending.items()))
                del self._pending[run_id]
                self._busy = True
            try:
                if kind == "save":
                    save(journal, run_id, state)
                else:
                    delete(journal, run_id)
            except Exception:  # noqa: BLE001 -- never reaches a run; a closed journal (shutdown) loses one turn
                log.info("checkpoint %s failed", kind, exc_info=True)
            finally:
                with self._condition:
                    self._busy = False
                    self._condition.notify_all()

    def flush(self, timeout=2.0):
        """Wait until everything submitted is written (tests, Runtime.close). True when it was."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._pending or self._busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._condition.wait(min(left, .05))
        return True


writer = Writer()


def _journal(run_context):
    return getattr(getattr(run_context, "runtime", None), "journal", None)


def keep(run_context, state):
    """The frontier's end-of-turn state for ``run_context``'s run: saved on the writer's thread."""
    journal = _journal(run_context)
    if journal is None or harness_api.disabled("harness") or getattr(run_context, "engine", "smart") != "smart":
        return
    writer.submit(run_context.run_id, "save", journal, state)


def ended(run_context, status, code=None):
    """The frontier's run ended with ``status``: its checkpoint goes unless the task can be picked up from it."""
    journal = _journal(run_context)
    if journal is None or harness_api.disabled("harness"):
        return
    if status in PICKUP_STATUSES or (status == "blocked" and code in PICKUP_CODES):
        return
    writer.submit(run_context.run_id, "delete", journal)


def close(runtime=None):
    """Runtime.close (a service's close): what is queued is written before the journal closes."""
    writer.flush(1.5)


def _runs(runtime):
    with runtime.lock:
        return dict(runtime.runs)


def picked_up_by(runtime, run_id):
    """The run that already picks ``run_id`` up, or None."""
    for run in _runs(runtime).values():
        if (getattr(run, "extras", None) or {}).get("resumeFrom") == run_id:
            return run
    return None


def _workflow_context_missing(original, runs):
    """An unbound pickup cannot recover a workflow scope from its public ancestry."""
    if getattr(original, "workflow_id", None) is not None:
        return False
    seen = set()
    run = original
    while run is not None:
        if run.id in seen:
            return True
        seen.add(run.id)
        key = getattr(run, "idempotency_key", "") or ""
        if getattr(run, "workflow_id", None) is not None or str(key).startswith("workflow:"):
            return True
        parent = (getattr(run, "extras", None) or {}).get("resumeFrom")
        if not parent:
            return False
        run = runs.get(parent)
        if run is None:
            return True
    return False


def check(runtime, run_id):
    """The run ``run_id`` if it can be picked up now."""
    runs = _runs(runtime)
    original = runs.get(run_id)
    if original is None:
        raise APIError("Task not found", 404, "not_found")
    if original.finished_at is None:
        raise APIError("This task is still running.", 409, "run_active")
    if not resumable(original):
        raise APIError("Mobster can pick up a task only after it was interrupted or stopped.", 409, "not_resumable")
    if uncertain_result(original) or unresolved_effect(original):
        raise APIError("The last action may have happened without a confirmed result. Check the phone and start a "
                       "new task; Mobster cannot pick this one up safely.", 409, "pickup_outcome_unknown")
    if _workflow_context_missing(original, runs):
        raise APIError("This task's saved workflow scope cannot be verified after restart. Inspect its result "
                       "and start a new authorized task; this task was not picked up.", 409,
                       "workflow_context_missing")
    fields = getattr(original, "extras", None) or {}
    scoped = any(fields.get(name) is not None for name in (
        "permissionMode", "projectEnv", "remoteGrant", "consoleMember", "consoleSession"))
    if (original.origin in permissions.policy.OUTSIDE_ORIGINS or scoped
            or getattr(original, "allowed_bundles", None) is not None or getattr(original, "pinned_apps", None)
            or (getattr(original, "cap_usd", None) is not None and not getattr(original, "cap_from_month", False))):
        raise APIError("This task's original access limits cannot be carried into a pickup. Review its result "
                       "and start a new task within the right limits.", 409, "pickup_scope")
    if isinstance((original.extras or {}).get("account"), dict) and getattr(original, "account_context", None) is None:
        raise APIError("This task has no saved profile context to pick up safely. Start a new task after checking "
                       "the profile on your phone.", 409, "profile_context_missing")
    writer.flush(1.0)   # the last turn's checkpoint may still be on its way
    if load(runtime.journal, run_id) is None:
        raise APIError("There's nothing to pick up yet: Mobster hadn't finished a step. Run the task again instead.",
                       409, "no_checkpoint")
    return original


def validate_resume_from(value, runtime):
    """The ``resumeFrom`` run field: a finished task's id that can be picked up and has a checkpoint."""
    if not isinstance(value, str) or len(value) != 12 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("resumeFrom is a task id")
    if runtime is None:
        return value
    try:
        check(runtime, value)
    except APIError as error:
        raise ValueError(str(error)) from None
    return value


def pick_up(runtime, run_id, *, idempotency_key=None, origin="api"):
    """Start a new run that picks ``run_id`` up."""
    original = check(runtime, run_id)
    already = picked_up_by(runtime, run_id)
    if already is not None and (idempotency_key is None or already.idempotency_key != idempotency_key):
        raise APIError("This task was already picked up.", 409, "picked_up", runId=already.id)
    extras = {"resumeFrom": run_id}
    if isinstance((original.extras or {}).get("account"), dict):
        extras["account"] = original.extras["account"]
    fields = harness_api.run_fields()
    for name in ("threadId",):  # the conversation it belongs to (track conversations), when that field exists
        value = (original.extras or {}).get(name)
        if name in fields and value is not None:
            extras[name] = value
    if isinstance((original.extras or {}).get("askUser"), bool):
        extras["askUser"] = original.extras["askUser"]   # its surface could (or couldn't) show a question
    kwargs = {"engine": "smart", "extras": extras, "_resume_run": original}
    if origin in harness_api.ORIGINS and origin != "api":
        kwargs["origin"] = origin
    device = getattr(original, "device_id", None)
    created = runtime.create(original.app["id"], original.goal, "live", idempotency_key,
                             **({"device": device} if device else {}), **kwargs)
    return created if idempotency_key else (created, False)


def user_messages(run):
    """What the person wrote to ``run`` while it worked, oldest first (its journaled user_message events)."""
    out = []
    for event in getattr(run, "events", None) or ():
        if event.get("event") == "user_message" and isinstance(event.get("text"), str) and event["text"].strip():
            out.append(" ".join(event["text"].split()))
    return out


def done_with_ok(run):
    """The commits the person approved in ``run``, oldest first: [(step, act, label)], from its journaled events."""
    done = []
    for event in getattr(run, "events", None) or ():
        kind = event.get("event")
        if kind == "frontier_approval" and event.get("answer") == "approved":
            done.append([event.get("step"), event.get("act") or "other", ""])
        elif kind == "frontier_action" and done and done[-1][0] == event.get("step") and not done[-1][2]:
            done[-1][2] = str(event.get("target_label") or "")
    return [tuple(item) for item in done]


ACT_WORDS = {"send_message": "sending a message", "reply": "sending a reply", "post": "posting",
             "comment": "commenting",
             "react": "reacting", "pay": "paying", "request_money": "requesting money", "transfer": "making a transfer",
             "order": "placing an order", "book": "making a booking", "follow": "following",
             "unfollow": "unfollowing", "share": "sharing", "call": "placing a call", "delete": "deleting",
             "cancel": "cancelling", "install": "installing an app"}


def held_acts(run):
    """The acts ``run`` may already have done (approved, or sent with the answer lost), oldest first and once each: a
    pickup asks for each again in every mode, and refuses it where nobody can answer."""
    acts = []
    for event in getattr(run, "events", None) or ():
        kind = event.get("event")
        if kind == "frontier_approval" and event.get("answer") == "approved":
            act = event.get("act")
        elif kind == "frontier_failed" and event.get("outcome") == "unknown":
            act = event.get("held_act")
        else:
            continue
        if isinstance(act, str) and act and act not in acts:
            acts.append(act)
    return tuple(acts)


def handoff(state, original):
    """The InitialState a picked-up run starts from."""
    notes = [str(note) for note in state.get("notes") or ()]
    messages = user_messages(original) if original is not None else []
    if messages:
        text = " · ".join(messages)
        if len(text) > MESSAGE_NOTE_CHARS:
            text = "…" + text[-MESSAGE_NOTE_CHARS:]
        notes.append(MESSAGE_NOTE + text)
    feedback = PICKUP_FEEDBACK
    done = done_with_ok(original) if original is not None else []
    if done:
        feedback += (" Before the interruption the user said OK to: "
                     + "; ".join(f"step {step}: {ACT_WORDS.get(act, 'something that needed their OK')}"
                                 + (f" ({label[:60]})" if label else "") for step, act, label in done[-5:])
                     + ". It may already have happened: check the screen (the thread, the sent or saved item) "
                       "before doing it again.")
    held = held_acts(original) if original is not None else ()
    lost = [act for act in held if act not in {item[1] for item in done}]
    if lost:
        feedback += (" The answer to an earlier attempt at " + ", ".join(ACT_WORDS.get(act, act) for act in lost)
                     + " was lost: it may have gone through. Check the screen before doing it again; doing it again "
                       "asks the user first.")
    return harness_api.InitialState(
        plan=str(state.get("plan") or ""), notes=tuple(notes[-40:]),
        history=tuple(str(line) for line in state.get("history") or ()), feedback=feedback,
        steps_used=max(0, int(state.get("step") or 0)), cost_usd=float(state.get("cost_usd") or 0.0),
        reason="picked up after the task was interrupted", held_acts=held)


_resumed = {}   # run id -> turns already used, for frontier_options (read once)


def _pickup_unavailable(ctx, error):
    try:
        ctx.emit({"event": "context_error", "key": "pickup", "error": error})
    except Exception:  # a failed journal write must not turn a pickup into a fresh task
        pass
    uncertain = error == "pickup_outcome_unknown"
    return harness_api.PreRun(result={
        "event": "result", "engine": "smart", "status": "blocked", "outcome": "check",
        "stop_code": "pickup_outcome_unknown" if uncertain else "pickup_checkpoint_missing",
        "answer": None, "proof": [],
        "independently_verified": False, "actions_may_have_run": False,
        "reason": ("The earlier task has an unconfirmed action. This pickup did nothing. Check the phone before "
                   "starting a new task." if uncertain else
                   "The earlier task's checkpoint is unavailable. This pickup did nothing. Review the earlier "
                   "task and start a new one only after checking the phone.")})


def pre_run(ctx, driver, approve):
    """Pre-run hook (order 0): a run with ``resumeFrom`` goes on from that run's checkpoint."""
    source = (ctx.extras or {}).get("resumeFrom")
    if not source:
        return None
    if harness_api.disabled("harness"):
        return _pickup_unavailable(ctx, "disabled")
    runtime = ctx.runtime
    try:
        original = check(runtime, source)
        state = load(getattr(runtime, "journal", None), source)
        if state is None:
            return _pickup_unavailable(ctx, "no_checkpoint")
        initial = handoff(state, original)
        _resumed[ctx.run_id] = initial.steps_used
        ctx.emit({"event": "picked_up", "from": source, "step": initial.steps_used})
    except APIError as error:
        _resumed.pop(ctx.run_id, None)
        return _pickup_unavailable(ctx, error.code)
    except Exception:  # no malformed/missing resume state may fall through to the fresh goal
        _resumed.pop(ctx.run_id, None)
        return _pickup_unavailable(ctx, "unavailable")
    return harness_api.PreRun(handoff=initial)


pre_run.before_phone = True


def frontier_options(ctx):
    """A picked-up run gets MORE_TURNS turns on top of those it used (at most MAX_TURNS)."""
    used = _resumed.pop(ctx.run_id, None)
    if used is None:
        return {}
    return {"max_steps": max(1, min(MAX_TURNS, used + MORE_TURNS))}

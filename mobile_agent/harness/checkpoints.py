"""Checkpoints and picking up (SPEC §3.1 F6).

At the end of every Smart turn the frontier hands its state (seam S7.3) to ``keep``: the plan, the notes, the last
60 history lines, the turn count, the spend, the app in front. It is masked there (agent_hooks.MASK); ``save``
passes it through secret_filter and keeps the latest one per run in the journal (table ``run_checkpoints``, at most
64 KB), both on the ``Writer``'s thread, never the agent's. It never holds an approval's text or a code: approval requests aren't part
of it, and a code a skill typed is masked before it gets here.

A task that ended interrupted (the sidecar restarted), with an error, stopped, unanswered, or blocked because the phone
was unplugged or locked, can be picked up: ``pick_up`` starts a **new** run with the same goal, app, device and thread,
and ``extras.resumeFrom`` naming the old one. Its pre-run hook (``pre_run``, order 0) hands the frontier the saved
state, with the feedback "You're picking up a task after it was interrupted. The last action may or may not have
happened: look at the screen before repeating anything." The frontier reads the screen before its first decision, so
no action runs again without a new decision, and every send, buy, post or delete still asks. Never automatic.

The checkpoint of a run that can't be picked up (done, declined, a spending limit) is deleted when its loop ends
(``ended``); the rest go with their run (ON DELETE CASCADE) when history drops it.
"""

import json
import logging
import threading
import time

from .. import harness_api
from ..api_errors import APIError
from ..secret_filter import redact

log = logging.getLogger("mobster.harness")

SCHEMA, VERSION = "harness", 1
MAX_BYTES = 64_000
# A task can be picked up after it ended one of these ways (plus blocked by an unplugged or locked phone).
PICKUP_STATUSES = frozenset({"interrupted", "error", "stopped", "approval_timeout"})
PICKUP_CODES = frozenset({"unplugged", "phone_locked"})
# The turns a picked-up task gets on top of the ones it used (engines.SMART_CONFIG.max_steps).
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


# -- the store ------------------------------------------------------------------------------------------------------

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
    """``state`` cleaned and cut to MAX_BYTES of JSON: the oldest history lines go first, then the oldest notes.
    None when even the bare state is too large."""
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


# -- which tasks can be picked up -----------------------------------------------------------------------------------

def stop_code(summary):
    summary = summary if isinstance(summary, dict) else {}
    return summary.get("code") or summary.get("stop_code")


def resumable(run):
    """Whether ``run`` ended a way it can be picked up from (a checkpoint is checked separately)."""
    if run is None or getattr(run, "finished_at", None) is None:
        return False
    if getattr(run, "mode", "live") != "live" or getattr(run, "engine", None) != "smart":
        return False
    status = getattr(run, "status", None)
    return status in PICKUP_STATUSES or (status == "blocked" and stop_code(run.summary) in PICKUP_CODES)


class Writer:
    """Keeps checkpoints off the agent's thread: one daemon thread, started on the first checkpoint, writing the latest
    state per run (a newer one replaces one not yet written). The frontier hands it each turn's state directly
    (``keep``), not through a run listener: a listener would start the listeners' thread for every task, Quick mode's
    included, and only Smart turns have checkpoints."""

    def __init__(self):
        self._condition = threading.Condition()
        self._pending = {}        # run id -> ("save", journal, state) | ("delete", journal, None)
        self._busy = False
        self._thread = None

    def submit(self, run_id, kind, journal, state=None):
        with self._condition:
            self._pending[run_id] = (kind, journal, state)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="mobster-checkpoints", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def _loop(self):
        while True:
            with self._condition:
                while not self._pending:
                    if not self._condition.wait(30):
                        self._thread = None
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


# -- picking up -----------------------------------------------------------------------------------------------------

def _runs(runtime):
    with runtime.lock:
        return dict(runtime.runs)


def picked_up_by(runtime, run_id):
    """The run that already picks ``run_id`` up, or None."""
    for run in _runs(runtime).values():
        if (getattr(run, "extras", None) or {}).get("resumeFrom") == run_id:
            return run
    return None


def check(runtime, run_id):
    """The run ``run_id`` if it can be picked up now. APIError otherwise: 404 not_found, 409 run_active,
    not_resumable or no_checkpoint."""
    original = _runs(runtime).get(run_id)
    if original is None:
        raise APIError("Task not found", 404, "not_found")
    if original.finished_at is None:
        raise APIError("This task is still running.", 409, "run_active")
    if not resumable(original):
        raise APIError("Mobster can pick up a task only after it was interrupted or stopped.", 409, "not_resumable")
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
    """Start a new run that picks ``run_id`` up. Returns (run, replayed). APIError: 404, 409 run_active,
    not_resumable, no_checkpoint, or picked_up (another run already picks it up; its id in ``runId``)."""
    original = check(runtime, run_id)
    already = picked_up_by(runtime, run_id)
    if already is not None and (idempotency_key is None or already.idempotency_key != idempotency_key):
        raise APIError("This task was already picked up.", 409, "picked_up", runId=already.id)
    extras = {"resumeFrom": run_id}
    fields = harness_api.run_fields()
    for name in ("threadId",):  # the conversation it belongs to (track conversations), when that field exists
        value = (original.extras or {}).get(name)
        if name in fields and value is not None:
            extras[name] = value
    if isinstance((original.extras or {}).get("askUser"), bool):
        extras["askUser"] = original.extras["askUser"]   # its surface could (or couldn't) show a question
    kwargs = {"engine": "smart", "extras": extras}
    if origin in harness_api.ORIGINS and origin != "api":
        kwargs["origin"] = origin
    device = getattr(original, "device_id", None)
    try:
        created = runtime.create(original.app["id"], original.goal, "live", idempotency_key,
                                 **({"device": device} if device else {}), **kwargs)
    except ValueError as error:
        if not device or "leave device out" not in str(error):
            raise
        created = runtime.create(original.app["id"], original.goal, "live", idempotency_key, **kwargs)
    return created if idempotency_key else (created, False)


def user_messages(run):
    """What the person wrote to ``run`` while it worked, oldest first (its journaled user_message events)."""
    out = []
    for event in getattr(run, "events", None) or ():
        if event.get("event") == "user_message" and isinstance(event.get("text"), str) and event["text"].strip():
            out.append(" ".join(event["text"].split()))
    return out


def done_with_ok(run):
    """The commits the person approved in ``run``, oldest first: [(step, act, label)], from its journaled events.
    The label is the control's (from the action after the OK; "" when the run ended before that was recorded)."""
    done = []
    for event in getattr(run, "events", None) or ():
        kind = event.get("event")
        if kind == "frontier_approval" and event.get("answer") == "approved":
            done.append([event.get("step"), event.get("act") or "other", ""])
        elif kind == "frontier_action" and done and done[-1][0] == event.get("step") and not done[-1][2]:
            done[-1][2] = str(event.get("target_label") or "")
    return [tuple(item) for item in done]


ACT_WORDS = {"send_message": "sending a message", "reply": "sending a reply", "post": "posting", "comment": "commenting",
             "react": "reacting", "pay": "paying", "request_money": "requesting money", "transfer": "making a transfer",
             "order": "placing an order", "book": "making a booking", "follow": "following",
             "unfollow": "unfollowing", "share": "sharing", "call": "placing a call", "delete": "deleting",
             "cancel": "cancelling", "install": "installing an app"}


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
    return harness_api.InitialState(
        plan=str(state.get("plan") or ""), notes=tuple(notes[-40:]),
        history=tuple(str(line) for line in state.get("history") or ()), feedback=feedback,
        steps_used=max(0, int(state.get("step") or 0)), cost_usd=float(state.get("cost_usd") or 0.0),
        reason="picked up after the task was interrupted")


_resumed = {}   # run id -> turns already used, for frontier_options (read once)


def pre_run(ctx, driver, approve):
    """Pre-run hook (order 0): a run with ``resumeFrom`` goes on from that run's checkpoint."""
    source = (ctx.extras or {}).get("resumeFrom")
    if not source or harness_api.disabled("harness"):
        return None
    runtime = ctx.runtime
    state = load(getattr(runtime, "journal", None), source)
    if state is None:
        ctx.emit({"event": "context_error", "key": "pickup", "error": "no_checkpoint"})
        return None
    original = (getattr(runtime, "runs", None) or {}).get(source)
    initial = handoff(state, original)
    _resumed[ctx.run_id] = initial.steps_used
    ctx.emit({"event": "picked_up", "from": source, "step": initial.steps_used})
    return harness_api.PreRun(handoff=initial)


def frontier_options(ctx):
    """A picked-up run gets MORE_TURNS turns on top of those it used (at most MAX_TURNS)."""
    used = _resumed.pop(ctx.run_id, None)
    if used is None:
        return {}
    return {"max_steps": max(1, min(MAX_TURNS, used + MORE_TURNS))}

"""One answer to "is the engine OK?": the state every surface shows. Its words and fix are a row of ``states.ROWS``."""

import threading
import time

from .states import (
    DOWN,
    FROZEN,
    HELPER_DOWN,
    LIVE_OFF,
    MODEL_UNAVAILABLE,
    NO_PHONE,
    READY,
    RESTARTING,
    ROWS,
    SLOW,
    STOPPING,
    WAITING_FOR_PHONE,
    fix_public,
    row,
    words,
)

# The state said first when several stand, most blocking first. A state not in the list never outranks one that is.
PRIORITY = (STOPPING, MODEL_UNAVAILABLE, LIVE_OFF, NO_PHONE, HELPER_DOWN, WAITING_FOR_PHONE)

TOPIC = "engine"
EVENT = "engine_health"

# Worst-case detection (device_manager.PROBE_TICK + PROBE_INTERVAL + wda_ready + PROBE_TIMEOUT + SAMPLE_S) must stay <=
# NAMED_WITHIN_S; test_engine_health asserts it.
SAMPLE_S = 1.0
SLOW_AFTER_S = 3.0
FROZEN_AFTER_S = 8.0
NAMED_WITHIN_S = 10.0

ACTION_UNKNOWN_STEP = ("An action was on its way when the phone went away. Mobster looks at the screen before it goes "
                       "on and doesn't send that action again.")

# The causes a helper log line or a phone state can name (runner_hints.RUNNER_HINTS).
PHONE_CAUSES = frozenset(r.cause for r in ROWS if r.state == WAITING_FOR_PHONE and "app" in r.surfaces)
TASK_WAIT_CAUSES = {"lock_screen": "locked", "passcode_keypad": "locked", "unplugged": "unplugged",
                    "wifi_lost": "wifi_lost"}


def task_wait(kind, phone=None):
    """(need, sentence) for what a task waits on (``handoff_waiting``'s kind), or None for a kind that has no words."""
    cause = TASK_WAIT_CAUSES.get(kind)
    if cause is None:
        return None
    _message, step, need = words(WAITING_FOR_PHONE, cause, phone)
    return need, step


class Condition:
    """One thing that is not as it should be."""

    __slots__ = ("state", "cause", "message", "next_step", "need", "fix", "since", "device", "device_name", "task",
                 "midrun", "action_unknown", "seconds")

    def __init__(self, state, cause, *, since, phone=None, message=None, device=None, task=None, midrun=False,
                 action_unknown=False, seconds=None):
        self.state, self.cause, self.since = state, cause, since
        self.message, self.next_step, self.need = words(state, cause, phone, message)
        self.fix = row(cause).fix
        if action_unknown:
            self.next_step = f"{self.next_step} {ACTION_UNKNOWN_STEP}"
        self.device, self.device_name = device, phone
        self.task, self.midrun, self.action_unknown, self.seconds = task, midrun, action_unknown, seconds

    def public(self):
        value = {"state": self.state, "cause": self.cause, "message": self.message, "next_step": self.next_step,
                 "fix": fix_public(self.fix), "since": self.since}
        if self.need:
            value["need"] = self.need
        if self.device:
            value["device"] = self.device
        if self.device_name:
            value["device_name"] = self.device_name
        if self.task:
            value.update(task=self.task, midrun=self.midrun)
            if self.action_unknown:
                value["action_unknown"] = True
            if self.seconds:
                value["wait_seconds"] = self.seconds
        return value


class EngineHealth:
    """The newest condition of each source, the one to say first, and a publish when it changes."""

    def __init__(self, clock=time.time, publish=None, boot=None):
        self.clock = clock
        self.publish = publish
        self.boot = boot
        self.lock = threading.Lock()
        self.conditions = {}
        self.sampled_at = None
        self._said = None

    def report(self, source, state, cause, *, phone=None, message=None, device=None, task=None, midrun=False,
               action_unknown=False, seconds=None):
        """``source`` is in this state now, for this cause. The same state and cause again keeps their ``since``."""
        with self.lock:
            found = self.conditions.get(source)
            same = found is not None and (found.state, found.cause) == (state, cause)
            self.conditions[source] = Condition(
                state, cause, since=found.since if same else self.clock(), phone=phone, message=message,
                device=device, task=task, midrun=midrun, action_unknown=action_unknown, seconds=seconds)
        self._publish_if_changed()

    def clear(self, source):
        """``source`` is fine now."""
        with self.lock:
            self.conditions.pop(source, None)
        self._publish_if_changed()

    def clear_prefix(self, prefix, keep=()):
        """Every source starting with ``prefix`` is fine now."""
        with self.lock:
            for source in [s for s in self.conditions if s.startswith(prefix) and s not in keep]:
                del self.conditions[source]
        self._publish_if_changed()

    def sampled(self):
        """The sampler finished a pass (its time is the snapshot's ``observed_at``)."""
        with self.lock:
            self.sampled_at = self.clock()

    def _ordered(self):
        rank = {state: index for index, state in enumerate(PRIORITY)}
        return sorted(self.conditions.items(), key=lambda item: (rank.get(item[1].state, len(rank)), item[1].since,
                                                                 item[0]))

    def snapshot(self):
        """The state to say first (``ready`` when nothing is wrong), with every standing issue and the liveness."""
        with self.lock:
            ordered = self._ordered()
            now = self.clock()
            observed = self.sampled_at if self.sampled_at is not None else now
        if ordered:
            first = ordered[0][1].public()
        else:
            first = {"state": READY, "cause": None, "message": "Ready.", "next_step": None,
                     "fix": None, "since": observed}
        return {**first, "observed_at": observed, "issues": [c.public() | {"source": s} for s, c in ordered],
                "liveness": {"boot": self.boot, "sample_s": SAMPLE_S, "slow_after_s": SLOW_AFTER_S,
                             "frozen_after_s": FROZEN_AFTER_S}}

    def _publish_if_changed(self):
        with self.lock:
            said = tuple((s, c.state, c.cause, c.action_unknown) for s, c in self._ordered())
            if said == self._said:
                return
            self._said = said
        if callable(self.publish):
            self.publish(TOPIC, {"event": EVENT, **self.snapshot()})


def cause_of(snapshot, source):
    """The cause ``source`` stands in in a snapshot (``phone:primary``, ``model``), or None when it is fine."""
    return next((issue["cause"] for issue in snapshot.get("issues", ()) if issue.get("source") == source), None)


def classify_phone(reading):
    """(state, cause) for what a phone's helper and link say, or None when ready; a None fact is unknown,
    never a reason."""
    if reading.get("exposed"):
        return HELPER_DOWN, "exposed"
    if reading.get("expired"):
        return HELPER_DOWN, "signature_expired"
    if not reading.get("chosen"):
        return NO_PHONE, "none_chosen"
    if reading.get("wifi"):
        if reading.get("wifi_lost"):
            return WAITING_FOR_PHONE, "wifi_lost"
    elif reading.get("usb") is False:
        return WAITING_FOR_PHONE, "unplugged"
    if not reading.get("built"):
        return HELPER_DOWN, "not_built"
    runner, hint = reading.get("runner"), reading.get("hint")
    if runner == "running":
        if reading.get("locked") is True:
            return WAITING_FOR_PHONE, "locked"
        if reading.get("responsive") is False:
            return WAITING_FOR_PHONE, "not_responding"
        return None
    if reading.get("holding") == "unlock":
        return WAITING_FOR_PHONE, "locked"
    if hint in PHONE_CAUSES:
        return WAITING_FOR_PHONE, hint
    if runner == "failed":
        return HELPER_DOWN, "failing"
    if runner == "starting":
        return WAITING_FOR_PHONE, "helper_restarting"
    return HELPER_DOWN, "stopped"


def classify_model(available, reason, credit_reasons=(), has_key=True):
    """(state, cause, message) for whether Smart's model can run, or None when it can."""
    if available:
        return None
    if not has_key:
        return MODEL_UNAVAILABLE, "no_key", reason
    return MODEL_UNAVAILABLE, "no_credit" if reason in credit_reasons else "key_rejected", reason


def action_in_flight(events):
    """Whether the newest decision of a run sent an action that has no result yet; an unreadable decision counts."""
    done = {"frontier_action", "frontier_failed"}
    results = 0
    for event in reversed(events[-400:]):
        name = event.get("event")
        if name in done:
            results += 1
        elif name == "frontier_decision":
            ops = event.get("ops")
            if not isinstance(ops, list):
                return True
            acting = sum(op not in (None, "DONE", "BLOCKED", "WAIT") for op in ops)
            return acting > results
    return False


def task_condition(run_id, wait, events, phone=None, device=None):
    """The ``Condition`` arguments of a task waiting on the phone (``run.phone_wait``'s dict)."""
    cause = TASK_WAIT_CAUSES.get(wait.get("kind"))
    if cause is None:
        return None
    return dict(state=WAITING_FOR_PHONE, cause=cause, phone=phone, device=device, task=run_id,
                midrun=wait.get("midrun") is True, action_unknown=wait.get("midrun") is True
                and action_in_flight(events),
                seconds=wait.get("seconds"))


def from_silence(age_s=None, *, refused=False, boot=None, seen_boot=None, thresholds=None):
    """The state a process that can't say it for itself is in, from the asker's side."""
    slow, frozen = (thresholds or (SLOW_AFTER_S, FROZEN_AFTER_S))
    if boot is not None and seen_boot is not None and boot != seen_boot:
        state, cause = RESTARTING, "restarted"
    elif refused:
        state, cause = DOWN, "not_running"
    elif age_s is None or age_s < slow:
        return None
    elif age_s < frozen:
        state, cause = SLOW, "slow_answer"
    else:
        state, cause = FROZEN, "no_answer"
    message, step, _need = words(state, cause)
    return {"state": state, "cause": cause, "message": message, "next_step": step, "fix": fix_public(row(cause).fix)}


def read_from_app(url=None, *, origin="cli", timeout=4.0, connect=None):
    """The Mac app's engine state as an asker sees it: its snapshot, or what its silence is (``from_silence``: frozen
    when it accepted the connection and never answered, down when the connection was refused). None when it can't be
    asked (it refused the token). ``connect`` replaces ``Client.connect`` in tests."""
    from .threads.client import Client, NoService, ServiceError
    try:
        return (connect or Client.connect)(url=url, origin=origin, timeout=timeout).status.get("engine")
    except NoService as silent:
        return from_silence(FROZEN_AFTER_S if silent.frozen else None, refused=not silent.frozen)
    except ServiceError:
        return None


class Sampler:
    """Reads a ``Runtime``'s detectors (model, live runs, phones, waiting tasks) into its ``EngineHealth``."""

    def __init__(self, runtime, interval=SAMPLE_S):
        self.runtime, self.interval = runtime, interval
        self.stop = threading.Event()
        self.thread = None
        self.pass_lock = threading.Lock()

    @property
    def health(self):
        return self.runtime.engine_health

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name="mobster-engine-health", daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(2.0)

    def _loop(self):
        while not self.stop.is_set():
            self.sample()
            self.stop.wait(self.interval)

    def snapshot(self):
        if self.thread is None or not self.thread.is_alive():
            self.sample()
        return self.health.snapshot()

    def sample(self):
        """One pass. A detector that raises leaves its source as it was and is logged:
        a failed read never says ready."""
        if not self.pass_lock.acquire(blocking=False):
            return   # a pass is running (the thread's, or another asker's): its answer is the answer
        try:
            for part in (self._engine, self._model, self._phones, self._tasks):
                try:
                    part()
                except Exception:  # noqa: BLE001 -- one detector's failure never hides the others
                    from . import harness_api
                    harness_api.log.exception("engine health: a detector failed")
            self.health.sampled()
        finally:
            self.pass_lock.release()

    def _set(self, source, found, **fields):
        """Report ``found`` ((state, cause) or None) for ``source``."""
        if found is None:
            self.health.clear(source)
        else:
            self.health.report(source, found[0], found[1], **fields)

    def _engine(self):
        runtime = self.runtime
        self._set("engine", (STOPPING, "closing") if runtime.closing else None)
        self._set("live", None if getattr(runtime.config, "enable_live", True) else (LIVE_OFF, "disabled"))

    def _model(self):
        from . import engines
        state = self.runtime.engines_status()[engines.SMART]
        found = classify_model(state["available"], state["reason"], engines.CREDIT_REASONS,
                               has_key=bool(engines.smart_key()) or self.runtime.scripted_models())
        if found is None:
            self.health.clear("model")
        else:
            self.health.report("model", found[0], found[1], message=found[2])

    def _phones(self):
        runtime = self.runtime
        if runtime.pool is not None:
            return   # a target with its own pool of phones says its own
        fleet = getattr(runtime, "fleet", None)
        seen = set()
        primary_name = getattr(getattr(fleet, "primary", None), "name", None) or None
        if runtime.manager is not None:
            found = classify_phone(runtime.manager.engine_reading())
        elif getattr(runtime.config, "wda_url", None):
            found = None if runtime.target_status()["ready"] else (HELPER_DOWN, "not_answering")
        else:
            found = (NO_PHONE, "none_attached")
        self._phone("phone:primary", found, primary_name, getattr(getattr(fleet, "primary", None), "id", None), seen)
        for phone in list(getattr(fleet, "phones", {}).values()):
            if phone.manager is not None:
                found = classify_phone(phone.manager.engine_reading())
            else:
                found = None if phone.target_status()["ready"] else (HELPER_DOWN, "not_answering")
            self._phone(f"phone:{phone.id}", found, phone.name, phone.id, seen)
        self.health.clear_prefix("phone:", keep=seen)

    def _phone(self, source, found, name, device, seen):
        seen.add(source)
        self._set(source, found, phone=name, device=device)

    def _tasks(self):
        from .run import phone_wait
        seen = set()
        for run in list(self.runtime.runs.values()):
            if run.finished_at is not None:
                continue
            with run.condition:
                events = list(run.events[-400:])
            wait = phone_wait(events)
            fields = task_condition(run.id, wait, events, run.device_name, run.device_id) if wait else None
            if fields is None:
                continue
            source = f"task:{run.id}"
            seen.add(source)
            self.health.report(source, fields.pop("state"), fields.pop("cause"), **fields)
        self.health.clear_prefix("task:", keep=seen)

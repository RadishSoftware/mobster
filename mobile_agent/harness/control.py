"""Pause, continue and "stop after this step" for a running Smart task: controls, never text,
so nothing here reads a message."""

import threading
import time
import weakref

PAUSE_LIMIT_SECONDS = 600
POLL_SECONDS = 0.25


class Control:
    """One run's controls. ``condition`` wakes a paused loop when anything changes."""

    def __init__(self):
        self.condition = threading.Condition()
        self.stop_after = False
        self.reason = None


_controls = weakref.WeakKeyDictionary()
_lock = threading.Lock()


def of(queue):
    """The controls that go with ``queue`` (made on first use); None for no queue."""
    if queue is None:
        return None
    with _lock:
        control = _controls.get(queue)
        if control is None:
            control = _controls[queue] = Control()
        return control


def paused(queue):
    """Whether a pause was asked for (the queue's own flag, so a pause set any other way counts too)."""
    flag = getattr(queue, "paused", None)
    return bool(flag is not None and flag.is_set())


def set_paused(queue, value, reason=None):
    """Pause (True) or continue (False) the run that owns ``queue``."""
    control = of(queue)
    with control.condition:
        if value:
            queue.paused.set()
        else:
            queue.paused.clear()
        control.reason = dict(reason) if value and isinstance(reason, dict) else None
        control.condition.notify_all()


def reason(queue):
    """Why Mobster paused the run itself (a copy), or None: not paused, or paused by a person."""
    control = _controls.get(queue) if queue is not None else None
    if control is None or not paused(queue):
        return None
    with control.condition:
        return dict(control.reason) if control.reason else None


def request_stop_after(queue):
    """Ask the run to stop once the step in progress is done. Wakes a paused run, which then stops."""
    control = of(queue)
    with control.condition:
        control.stop_after = True
        control.condition.notify_all()


def stop_after_requested(queue):
    control = _controls.get(queue) if queue is not None else None
    return bool(control is not None and control.stop_after)


def wait_while_paused(queue, *, cancelled=None, limit=None, clock=time.monotonic):
    """Block while the run is paused."""
    control = of(queue)
    if limit is None:
        with control.condition:
            own = (control.reason or {}).get("limit")
        limit = own if type(own) in (int, float) and own > 0 else PAUSE_LIMIT_SECONDS
    deadline = clock() + limit
    with control.condition:
        while paused(queue):
            if cancelled is not None and _true(cancelled):
                return "stopped"
            if control.stop_after:
                return "stop_after"
            left = deadline - clock()
            if left <= 0:
                return "timeout"
            control.condition.wait(min(POLL_SECONDS, left))
    return "continued"


def _true(fn):
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001 -- a broken Stop check never keeps a run paused forever
        return False

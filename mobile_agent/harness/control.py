"""What a person sends a running Smart task besides words: pause, continue, and "stop after this step".

The run's ``SteeringQueue`` (seam S8) carries the messages and the ``paused`` flag; this module keeps the rest of the
run's controls beside it, keyed by that queue, so the frontier (which holds the queue) and the HTTP routes (which
hold the run) reach the same state. A control is never text: nothing here reads a message.

- Pause: the loop stops at its next step boundary and touches nothing until Continue, Stop, "stop after this step",
  or PAUSE_LIMIT_SECONDS (10 minutes), after which it ends "stopped".
- Stop after this step: the action in progress finishes, then the run ends "stopped" before anything new starts.
"""

import threading
import time
import weakref

# A pause longer than this ends the task (status "stopped"): nobody is coming back to it.
PAUSE_LIMIT_SECONDS = 600
# How often a paused loop looks at Stop while it waits (a pause, a continue or a stop request wakes it at once).
POLL_SECONDS = 0.25


class Control:
    """One run's controls. ``condition`` wakes a paused loop when anything changes."""

    def __init__(self):
        self.condition = threading.Condition()
        self.stop_after = False


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


def set_paused(queue, value):
    """Pause (True) or continue (False) the run that owns ``queue``."""
    control = of(queue)
    with control.condition:
        if value:
            queue.paused.set()
        else:
            queue.paused.clear()
        control.condition.notify_all()


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
    """Block while the run is paused. Returns "continued", "stopped" (Stop was pressed), "stop_after" or "timeout"
    (paused for ``limit`` seconds, PAUSE_LIMIT_SECONDS unless given)."""
    control = of(queue)
    deadline = clock() + (PAUSE_LIMIT_SECONDS if limit is None else limit)
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

"""Record a Smart run's executed actions as a replay path.

The frontier reports each executed action to its step observer as a harness_api.StepRecord: the op, the element it
acted on, the masked typed text, the hash of the screen before and the commit act. Two things a replay needs aren't
in it, so the recorder also wraps the driver the frontier drives:

- the screen's full structural signature (the record has only its hash), kept for each screen the frontier read;
- the bundle a LAUNCH_APP opened and the point and pre-action screen geometry a TAP_XY or DISMISS used.

The wrapper passes every call through unchanged; it only looks at what comes back. Nothing is recorded for an action
that failed: the frontier reports only executed actions.
"""

import threading
from collections import OrderedDict
from functools import wraps

from ..state import signature as screen_pairs
from .path import ELEMENT_OPS, LIVE_OPS, OPS, POINT_OPS, TEXT_OPS, signature_hash

SCREENS_KEPT = 64


def literal_text(sources):
    """A text policy: typed text replays only when ``sources`` hold it word for word; else the model types it."""
    haystack = " ".join(" ".join(str(source).split()) for source in sources if source).casefold()

    def policy(text):
        plain = " ".join(str(text or "").split())
        if plain and plain.casefold() in haystack:
            return "literal", text
        return "live", None
    return policy


def searching(target):
    """True when ``target`` (a step's target dict) is a search field: contract.is_search_field's rule."""
    if not isinstance(target, dict):
        return False
    from types import SimpleNamespace

    from ..contract_screen import is_search_field
    return bool(is_search_field(SimpleNamespace(role=target.get("role") or "", label=target.get("label") or "")))


def typed_plainly(text):
    """Typed text a replay may type again: some, with nothing masked in it (a code or password never is)."""
    from ..agent_hooks import MASK
    return isinstance(text, str) and bool(text.strip()) and MASK not in text and len(text) <= 500


class Recorder:
    """Collects the steps of one run."""

    def __init__(self, *, text_policy=None):
        self.steps = []
        self.text_policy = text_policy or (lambda text: ("live", None))
        self.latest = None
        self._screens = OrderedDict()
        self._launched = None
        self._point = None
        self._lock = threading.Lock()

    def wrap(self, driver):
        return RecordingDriver(driver, self)

    def saw(self, snapshot):
        """Remember ``snapshot``'s signature by its hash (frontier.screen_signature's rule)."""
        try:
            pairs = screen_pairs(snapshot)
        except Exception:
            return
        key = signature_hash(pairs)
        with self._lock:
            self.latest = snapshot
            self._screens[key] = pairs
            self._screens.move_to_end(key)
            while len(self._screens) > SCREENS_KEPT:
                self._screens.popitem(last=False)

    def launched(self, bundle):
        with self._lock:
            self._launched = bundle

    def tapped(self, x, y, fingerprint):
        with self._lock:
            self._point = ([round(float(x), 4), round(float(y), 4)], fingerprint)

    def observe_step(self, record):
        """The frontier's step_observer: one executed action (a harness_api.StepRecord)."""
        step = self.step_from(record)
        if step is not None:
            with self._lock:
                self.steps.append(step)

    def step_from(self, record):
        op = getattr(record, "op", None)
        if not isinstance(op, str) or op in ("DONE", "BLOCKED", "WAIT"):
            return None
        if not record.changed and op not in TEXT_OPS:
            return None  # an action that changed nothing on screen did nothing a replay needs
        with self._lock:
            pairs = self._screens.get(record.screen)
            launched, point = self._launched, self._point
            self._point = None
        target = None
        if isinstance(record.target, dict):
            frame = record.target.get("frame")
            target = {"label": str(record.target.get("label") or "")[:500],
                      "role": str(record.target.get("role") or "")[:500],
                      "id": record.target.get("id") or None,
                      "frame": [round(float(v), 4) for v in frame] if isinstance(frame, (list, tuple))
                      and len(frame) == 4 else None}
        shape = [str(h)[:16] for h in (getattr(record, "shape", None) or ())]
        step = {"op": op, "app": record.app or None, "target": target,
                "screen": {"hash": record.screen or "", "signature": pairs, **({"shape": shape} if shape else {})},
                "text": None, "param": None, "commit": record.commit, "point": None, "live": False}
        if op == "LAUNCH_APP":
            if not launched:
                step["live"] = True
            step["app"] = launched or record.app
        elif op in POINT_OPS:
            if point is None or not point[1]:
                step["live"] = True
            if point is not None:
                step["point"] = point[0]
                if point[1]:
                    step["screen"]["fingerprint"] = point[1]
        elif op in TEXT_OPS:
            kind, value = self.text_policy(record.text)
            step["search"] = searching(target)
            if kind == "literal" and isinstance(value, str):
                step["text"] = value
            elif kind == "param" and isinstance(value, str):
                step["param"] = value
            elif step["search"] and typed_plainly(record.text):
                # A query typed into a search field finds the thing the task is about: navigation, not something
                # the task writes (owner, 10 Oct). It replays as typed.
                step["text"] = record.text
            else:
                step["live"] = True
        elif op in LIVE_OPS or op not in OPS:
            step["live"] = True
            if op == "SCROLL_TO" and isinstance(record.text, str) and typed_plainly(record.text):
                step["scroll"] = record.text  # the row it looked for: a saved program may keep it as a blank
        if op in ELEMENT_OPS and target is None:
            step["live"] = True
        return step

    def final(self):
        """The screen the run ended on: {"hash", "signature", "app"}, or None."""
        with self._lock:
            snapshot = self.latest
        if snapshot is None:
            return None
        try:
            pairs = screen_pairs(snapshot)
        except Exception:
            return None
        return {"hash": signature_hash(pairs), "signature": pairs, "app": getattr(snapshot, "bundle_id", "") or None}


class RecordingDriver:
    """``driver`` with every call passed through; reads, launches and point taps are also told to the recorder."""

    def __init__(self, driver, recorder):
        object.__setattr__(self, "_inner", driver)
        object.__setattr__(self, "_recorder", recorder)

    def __getattr__(self, name):
        value = getattr(self._inner, name)
        if not callable(value):
            return value
        recorder = self._recorder
        if name in ("observe", "observe_ready", "wait_for_change"):
            def read(*args, **kwargs):
                snapshot = value(*args, **kwargs)
                recorder.saw(snapshot)
                return snapshot
            return read
        if name == "execute":
            def execute(operation, target, *args, **kwargs):
                result = value(operation, target, *args, **kwargs)
                if operation == "LAUNCH_APP" and isinstance(target, str):
                    recorder.launched(target)
                return result
            return execute
        if name == "tap_point":
            @wraps(value)
            def tap_point(x, y, *args, **kwargs):
                snapshot = args[0] if args else kwargs.get("snapshot")
                try:
                    fingerprint = snapshot.content_fingerprint
                except Exception:  # a recording failure must not change the driver's action
                    fingerprint = None
                result = value(x, y, *args, **kwargs)
                recorder.tapped(x, y, fingerprint)
                return result
            return tap_point
        return value

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)


__all__ = ["Recorder", "RecordingDriver", "literal_text"]

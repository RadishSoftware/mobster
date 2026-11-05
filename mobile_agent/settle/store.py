"""What each app's transitions take to settle, learned from the settles themselves.

A key is (app bundle id, transition shape). A sample is the time from the end of the action's dispatch to the
first read of the screen that stayed (the arrival), in milliseconds. The store keeps the last WINDOW samples per
key and answers with their 90th percentile once it has MIN_SAMPLES. It holds numbers only: no screen text, no
labels. It is a file under the data folder (``settle/durations.json``), replaced atomically.
"""

import json
import math
import os
import threading
from pathlib import Path

from ..paths import user_data_dir, write_text_atomic

FLAG = "MOBSTER_EXPERIMENTAL_SETTLE"
WINDOW = 40
MIN_SAMPLES = 5
MAX_KEYS = 256
# A prediction past this is not worth a blind wait: the polling cap (WDA_SETTLE_CAP_SECONDS) is near.
MAX_PREDICTION_MS = 1200.0
# Polling accepts a screen about this long (WDA_SETTLE_QUIET_SECONDS) after it arrived. A blind wait pays only when
# its p90 sits less than that above the typical (median) arrival; otherwise polling ends sooner on a typical settle.
QUIET_MS = 500.0

SHAPES = {"TAP": "tap", "DOUBLE_TAP": "tap", "LONG_PRESS": "tap", "SUBMIT": "submit", "TYPE": "type",
          "SET_TEXT": "type", "LAUNCH_APP": "launch", "HOME": "home", "BACK": "back"}


def enabled():
    return os.environ.get(FLAG, "").strip().casefold() in {"1", "true", "on", "yes"}


def shape_of(op):
    """A transition shape for an operation name: swipes by axis, the rest by kind, anything else 'other'."""
    op = str(op or "")
    if op.startswith("SWIPE_"):
        return "swipe_" + ("y" if op in {"SWIPE_UP", "SWIPE_DOWN"} else "x")
    return SHAPES.get(op, "other")


def percentile(samples, share):
    """Nearest rank: the smallest sample that at least ``share`` of the samples do not exceed."""
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


class SettleStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else user_data_dir() / "settle" / "durations.json"
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {key: [float(v) for v in values][-WINDOW:] for key, values in raw["samples"].items()
                    if isinstance(values, list)}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    @staticmethod
    def key(app, shape):
        return f"{app}|{shape}"

    def samples(self, app, shape):
        with self._lock:
            return list(self._data.get(self.key(app, shape), ()))

    def p90(self, app, shape):
        """The predicted settle in ms, or None: too few samples, or too slow or too uneven to be worth a blind wait."""
        values = self.samples(app, shape)
        if len(values) < MIN_SAMPLES:
            return None
        value = percentile(values, .9)
        return value if value <= MAX_PREDICTION_MS and value - percentile(values, .5) < QUIET_MS else None

    def add(self, app, shape, ms):
        if not app or not math.isfinite(ms) or ms < 0:
            return
        key = self.key(app, shape)
        with self._lock:
            values = self._data.pop(key, [])
            values.append(round(float(ms), 1))
            self._data[key] = values[-WINDOW:]
            while len(self._data) > MAX_KEYS:
                self._data.pop(next(iter(self._data)))
            payload = json.dumps({"version": 1, "samples": self._data}, sort_keys=True)
        try:
            write_text_atomic(self.path, payload)
        except OSError:
            pass  # A prediction is optional; a run never fails on it.

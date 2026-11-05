"""Hedged model requests: cut the tail of identical-input calls, which are pure reads, so a twin costs only tokens."""

import os
import re
import threading
import time
from collections import deque

from .transport import TransportError

MIN_SAMPLES = 20
WINDOW = 64
RESEND_MIN_SECONDS = .5
_STATUS = re.compile(r"HTTP (\d{3});")
_EXCEPTION = re.compile(r"HTTP ([A-Za-z_]+); ")
# Our own encoding or decoding: resending the same request fails the same way.
_OWN_FAULTS = frozenset({"ValueError", "RecursionError"})


def transient(error):
    """Whether a model request failed in transit, so sending it again may succeed."""
    if not isinstance(error, TransportError):
        return False
    message = str(error)
    status = _STATUS.match(message)
    if status:
        return status.group(1) == "429" or status.group(1).startswith("5")
    if message.startswith("Incomplete HTTP response"):
        return True
    name = _EXCEPTION.match(message)
    return bool(name) and name.group(1) not in _OWN_FAULTS


def _env_ratio():
    try:
        value = float(os.environ.get("MOBSTER_HEDGE_RATIO", "0.1"))
    except ValueError:
        return .1
    return min(max(value, 0.0), 1.0)


def enabled():
    return os.environ.get("MOBSTER_HEDGE", "1").strip().lower() not in {"0", "off", "false", "no"}


class LatencyWindow:
    """Recent successful latencies per purpose (seconds)."""

    def __init__(self, size=WINDOW):
        self.size = size
        self._values = {}
        self._lock = threading.Lock()

    def add(self, purpose, seconds):
        with self._lock:
            values = self._values.get(purpose)
            if values is None:
                values = self._values[purpose] = deque(maxlen=self.size)
            values.append(seconds)

    def quantile(self, purpose, q):
        with self._lock:
            values = sorted(self._values.get(purpose, ()))
        if len(values) < MIN_SAMPLES:
            return None
        index = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
        return values[index]

    def count(self, purpose):
        with self._lock:
            return len(self._values.get(purpose, ()))


class HedgeBudget:
    """Token bucket: each primary call earns ``ratio`` tokens; a hedge spends one."""

    def __init__(self, ratio=None, burst=2.0):
        self.ratio = _env_ratio() if ratio is None else ratio
        self.burst = burst
        self.tokens = burst
        self.primaries = 0
        self.hedges = 0
        self._lock = threading.Lock()

    def earn(self):
        with self._lock:
            self.primaries += 1
            self.tokens = min(self.burst, self.tokens + self.ratio)

    def spend(self):
        with self._lock:
            if self.tokens < 1:
                return False
            self.tokens -= 1
            self.hedges += 1
            return True


# A primary past max(STALL_MIN_SECONDS, STALL_FACTOR x the hedge delay) is stalled, not slow: it gets a rescue twin even
# with the budget spent.
STALL_MIN_SECONDS, STALL_FACTOR = 10.0, 3.0


class Hedger:
    def __init__(self, quantile=.9, window=None, budget=None, floor=None, default=None,
                 any_floor=.8, any_default=.8, stall_min=STALL_MIN_SECONDS):
        self.q = quantile
        self.window = window or LatencyWindow()
        self.budget = budget or HedgeBudget()
        self.floor, self.default = dict(floor or {}), dict(default or {})
        self.any_floor, self.any_default = any_floor, any_default
        self.resends = 0
        self.rescues = 0
        self.stall_min = stall_min

    def delay(self, purpose):
        """Seconds to wait for the primary before a twin may be sent."""
        observed = self.window.quantile(purpose, self.q)
        floor = self.floor.get(purpose, self.any_floor)
        if observed is None:
            return max(floor, self.default.get(purpose, self.any_default))
        return max(floor, observed)

    def run(self, purpose, attempt, *, timeout, on_attempt=None, clock=time.monotonic):
        """Run ``attempt(index, timeout)`` and maybe a twin; return the first success."""
        self.budget.earn()
        started = clock()
        done = threading.Condition()
        results = {}  # index -> (ok, value, end)

        def worker(index, budget_left):
            t0 = clock()
            try:
                value, ok = attempt(index, budget_left), True
            except BaseException as error:  # Re-raised on the caller's thread.
                value, ok = error, False
            end = clock()
            if ok:
                self.window.add(purpose, end - t0)
            with done:
                won = ok and not any(item[0] for item in results.values())
                results[index] = (ok, value, end)
                done.notify_all()
            if on_attempt is not None:
                try:
                    on_attempt(index, t0, end, ok, won)
                except Exception:
                    pass

        threading.Thread(target=worker, args=(0, timeout), name="mobster-hedge-0", daemon=True).start()
        wait = self.delay(purpose)
        hedged = False
        with done:
            done.wait_for(lambda: 0 in results, timeout=min(wait, timeout))
            primary_done = 0 in results
        if not primary_done:
            remaining = timeout - (clock() - started)
            if remaining > .05 and self.budget.spend():
                hedged = True
                threading.Thread(target=worker, args=(1, remaining), name="mobster-hedge-1",
                                 daemon=True).start()
        if not primary_done and not hedged:
            # No hedge budget: wait until the call looks stalled, then send a rescue twin anyway.
            stall = max(self.stall_min, STALL_FACTOR * wait)
            with done:
                done.wait_for(lambda: 0 in results, timeout=max(0.0, min(stall, timeout) - (clock() - started)))
                primary_done = 0 in results
            remaining = timeout - (clock() - started)
            if not primary_done and remaining > RESEND_MIN_SECONDS:
                hedged = True
                self.rescues += 1
                threading.Thread(target=worker, args=(1, remaining), name="mobster-rescue-1",
                                 daemon=True).start()
        attempts = 2 if hedged else 1
        resent = False
        with done:
            while True:
                success = next((item for item in results.values() if item[0]), None)
                if success is not None:
                    return success[1]
                if len(results) == attempts:
                    remaining = timeout - (clock() - started)
                    if not resent and transient(results[0][1]) and remaining >= RESEND_MIN_SECONDS:
                        # Failed in transit with time to spare: resend once on a fresh connection; not a hedge.
                        resent = True
                        index, attempts = attempts, attempts + 1
                        self.resends += 1
                        threading.Thread(target=worker, args=(index, remaining), name=f"mobster-resend-{index}",
                                         daemon=True).start()
                        continue
                    raise results[0][1]
                left = timeout - (clock() - started) + .25
                if left <= 0:
                    # Say how many requests were in flight, so a stalled twin is told apart from a missing hedge.
                    raise TimeoutError(f"Model call deadline exceeded ({attempts} request"
                                       f"{'s' if attempts > 1 else ''} in flight)")
                done.wait(timeout=left)


# A decision call carries a 6-15k token prompt and answers in seconds; the floor keeps twins for real outliers.
FRONTIER_FLOOR, FRONTIER_DEFAULT = 4.0, 8.0

HEDGERS = {"anthropic": Hedger(any_floor=FRONTIER_FLOOR, any_default=FRONTIER_DEFAULT)}

"""Hedged model requests: cut the tail of identical-input calls.

A call that has not answered by its hedge delay gets an identical twin on a
second pooled connection; the first answer wins. The request body is built
once and sent twice unchanged, so either answer answers exactly the question
the loop asked: a hedge changes when an answer arrives, never which question
it answers. Model calls are pure reads of the model, so a duplicate has no
effect beyond its cost.

* **Delay.** A rolling quantile (default p90) of the last ``window`` successful
  latencies for the same purpose, floored, with a fixed default until enough
  samples exist. Jev latency does not depend on prompt size (R^2 0.00 over
  1,040 decisions, latency breakdown, 23 Sep 2026), so one window per purpose
  is enough.
* **Budget.** A process-wide token bucket: each primary call earns ``ratio``
  of a hedge, with a small burst. Hedges never exceed ``ratio`` of calls plus
  the burst, whatever the provider does.
* **Losers.** The losing attempt is not aborted: it finishes in the background
  (its telemetry, including cost, is still reported) and its connection goes
  back to the pool warm.
* **Errors.** A call that failed in transit (``transient``: the connection
  dropped, or the provider answered 429 or 5xx) is sent once more at once on a
  fresh connection when ``RESEND_MIN_SECONDS`` of its deadline remain, whether
  or not a hedge was due; a resend spends no hedge budget. Any other failure
  raises exactly as an unhedged call. With two attempts in flight the first
  success wins; if both fail, the primary's error is raised.

Switches: ``MOBSTER_HEDGE=0`` turns hedging off; ``MOBSTER_HEDGE_RATIO``
(default 0.1) sets the budget.
"""

from collections import deque
import os
import re
import threading
import time

from .transport import TransportError

# Seconds before a hedge is considered, whatever the window says. Offline
# simulation over 1,640 recorded calls (2026-09-23, 10% budget): a floor keeps
# the budget for real outliers; without one, tokens ran out on calls that were
# only moderately slow. Decisions: D = max(0.8 s, rolling p90) cut p99 2,612 ->
# ~2,070 ms (after-first-call p99 1,819 -> ~1,470) for ~5.6% extra calls; the
# helper at max(1.5 s, p90) cut p99 5,641 -> ~4,300 ms for ~7.3% extra.
JEV_FLOOR = {"decision": .8, "action_verification": .8, "extraction": .8, "verification": .8}
# Used until a purpose has MIN_SAMPLES latencies.
JEV_DEFAULT = {"decision": .8, "action_verification": .8, "extraction": .9, "verification": .9}
# The text helper (Gemini): p50 ~1.0-1.6 s, p90 ~1.9-2.8 s by purpose.
HELPER_FLOOR, HELPER_DEFAULT = 1.5, 2.2
MIN_SAMPLES = 20
WINDOW = 64
# A resend needs this much of the call's deadline left to be worth sending.
RESEND_MIN_SECONDS = .5
# transport.HTTP words its failures "HTTP <status>; ..." and "HTTP <exception>; ...".
_STATUS = re.compile(r"HTTP (\d{3});")
_EXCEPTION = re.compile(r"HTTP ([A-Za-z_]+); ")
# Our own encoding or decoding: resending the same request fails the same way.
_OWN_FAULTS = frozenset({"ValueError", "RecursionError"})


def transient(error):
    """Whether a model request failed in transit, so sending it again may succeed.

    Model calls are pure reads of the model, so a resend has no effect beyond its
    cost. Measured on a USB iPhone (25 Sep): one Gemini request failing ~1 s in
    ended "Tell natasha hi" with TransportError before anything was typed. A 4xx,
    missing credentials or a rejected answer fails the same way twice and is not
    resent.
    """
    from .inference import ProviderResponseRejected

    if not isinstance(error, TransportError) or isinstance(error, ProviderResponseRejected):
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


class Hedger:
    def __init__(self, quantile=.9, window=None, budget=None, floor=None, default=None,
                 any_floor=.8, any_default=.8):
        self.q = quantile
        self.window = window or LatencyWindow()
        self.budget = budget or HedgeBudget()
        self.floor, self.default = dict(floor or {}), dict(default or {})
        self.any_floor, self.any_default = any_floor, any_default
        self.resends = 0

    def delay(self, purpose):
        """Seconds to wait for the primary before a twin may be sent."""
        observed = self.window.quantile(purpose, self.q)
        floor = self.floor.get(purpose, self.any_floor)
        if observed is None:
            return max(floor, self.default.get(purpose, self.any_default))
        return max(floor, observed)

    def run(self, purpose, attempt, *, timeout, on_attempt=None, clock=time.monotonic):
        """Run ``attempt(index, timeout)`` and maybe a twin; return the first success.

        ``attempt`` must send the identical request on its own connection each
        time it is called. ``on_attempt(index, start, end, ok, won)`` reports
        each attempt (for the latency trace) when it completes.
        """
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
        attempts = 2 if hedged else 1
        with done:
            while True:
                success = next((item for item in results.values() if item[0]), None)
                if success is not None:
                    return success[1]
                if len(results) == attempts:
                    remaining = timeout - (clock() - started)
                    if attempts == 1 and transient(results[0][1]) and remaining >= RESEND_MIN_SECONDS:
                        # Failed in transit with time to spare: resend now on a fresh
                        # connection (attempt 1 always borrows its own). Not a hedge.
                        attempts = 2
                        self.resends += 1
                        threading.Thread(target=worker, args=(1, remaining), name="mobster-resend-1",
                                         daemon=True).start()
                        continue
                    raise results[0][1]
                left = timeout - (clock() - started) + .25
                if left <= 0:
                    raise TimeoutError("Model call deadline exceeded")
                done.wait(timeout=left)


# Process-wide, one per provider: windows and budgets are never shared across providers.
HEDGERS = {"typesafe": Hedger(floor=JEV_FLOOR, default=JEV_DEFAULT),
           "helper": Hedger(any_floor=HELPER_FLOOR, any_default=HELPER_DEFAULT)}

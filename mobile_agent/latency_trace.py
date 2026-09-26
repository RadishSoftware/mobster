"""Always-on latency trace for one run: monotonic spans, cheap enough to leave on.

Every run records what its critical path waited on. The run's own thread (the
"main" lane) records *phases*: consecutive, non-overlapping spans such as
``observe.first``, ``decide``, ``verify.action``, ``dispatch.TAP`` and
``settle.TAP``. Model calls are recorded separately as *calls*, on the lane of
the thread that made them (``main``, or a side channel such as ``speculation``,
``prediction``, ``prefetch``), with queueing, connection reuse and hedging.
Point events (``marks``) note speculation hits and misses, memo hits and so on.

Cost: a phase is two ``time.monotonic()`` calls and one list append under a
lock (about 1 microsecond); a run records 20-100 of them. The whole trace is
emitted once, as one ``latency_trace`` event, when the run finishes. Nothing is
written per span, so tracing never adds journal writes to the hot path.

Times in the emitted event are milliseconds from the trace origin (rounded to
0.1 ms). ``origin_wall_ms`` is the wall clock at the origin, for joining traces
with journal timestamps. The report tool is ``python -m mobile_agent.evals.latency_report``.
"""

from contextlib import contextmanager
import threading
import time

# Bounds keep a pathological run (a long compiled loop) from producing an
# oversized journal event; the counts of anything dropped are reported.
MAX_PHASES = 2000
MAX_CALLS = 1000
MAX_MARKS = 1000


def _ms(value):
    return round(value * 1000, 1)


class Trace:
    """Spans for one run. Thread-safe; never raises into the caller."""

    def __init__(self, clock=time.monotonic, wall=time.time):
        self._clock = clock
        self.origin = clock()
        self.origin_wall = wall()
        self._lock = threading.Lock()
        self.phases = []   # (name, start, end, attrs|None) on the main lane
        self.calls = []    # (purpose, lane, start, end, attrs)
        self.marks = []    # (name, at, attrs|None)
        self.dropped = {"phases": 0, "calls": 0, "marks": 0}
        self._open = None  # name of the phase in progress on the main lane

    def now(self):
        return self._clock()

    # -- recording -----------------------------------------------------------

    def phase(self, name, start, end=None, **attrs):
        """Record one main-lane phase that ran from ``start`` to ``end`` (default now)."""
        end = self._clock() if end is None else end
        with self._lock:
            if len(self.phases) >= MAX_PHASES:
                self.dropped["phases"] += 1
                return
            self.phases.append((name, start, end, attrs or None))

    @contextmanager
    def span(self, name, **attrs):
        """``with trace.span("observe.refresh"):`` records one main-lane phase.

        Nested spans are not phases: an inner span on the main lane while an
        outer one is open is recorded as a mark, so phases never overlap and
        their sum is the attributed critical path.
        """
        start = self._clock()
        nested = self._open is not None
        if not nested:
            self._open = name
        self.last_span = name  # the innermost span begun most recently: where a failure happened
        extra = {}
        try:
            yield extra
        finally:
            end = self._clock()
            if nested:
                self.mark(name, at=start, ms=_ms(end - start), **attrs, **extra)
            else:
                self._open = None
                self.phase(name, start, end, **attrs, **extra)

    def call(self, purpose, start, end, lane="main", **attrs):
        """One model request (or one attempt of a hedged request)."""
        with self._lock:
            if len(self.calls) >= MAX_CALLS:
                self.dropped["calls"] += 1
                return
            self.calls.append((purpose, lane, start, end, attrs))

    def mark(self, name, at=None, **attrs):
        at = self._clock() if at is None else at
        with self._lock:
            if len(self.marks) >= MAX_MARKS:
                self.dropped["marks"] += 1
                return
            self.marks.append((name, at, attrs or None))

    # -- output --------------------------------------------------------------

    def event(self, final=None):
        """The one journal event for this run: compact, numbers and short names only."""
        final = self._clock() if final is None else final
        o = self.origin
        with self._lock:
            phases = [[name, _ms(start - o), _ms(end - start), *([attrs] if attrs else [])]
                      for name, start, end, attrs in self.phases]
            calls = [[purpose, lane, _ms(start - o), _ms(end - start), attrs]
                     for purpose, lane, start, end, attrs in self.calls]
            marks = [[name, _ms(at - o), *([attrs] if attrs else [])] for name, at, attrs in self.marks]
            dropped = {key: value for key, value in self.dropped.items() if value}
        event = {"event": "latency_trace", "version": 1, "origin_wall_ms": round(self.origin_wall * 1000, 1),
                 "wall_ms": _ms(final - o), "phases": phases, "calls": calls, "marks": marks,
                 "summary": summarize(phases, calls, _ms(final - o))}
        if dropped:
            event["dropped"] = dropped
        return event


def summarize(phases, calls, wall_ms):
    """Per-run totals: time per phase name, unattributed main-lane time, and overlap.

    ``overlap_ms`` is model time that ran off the main lane (speculation,
    prediction, prefetch, hedges) minus the time the main lane spent waiting
    for such work: the work the loop got done concurrently.
    """
    by_phase = {}
    for item in phases:
        name, duration = item[0], item[2]
        by_phase[name] = round(by_phase.get(name, 0) + duration, 1)
    attributed = sum(item[2] for item in phases)
    off_lane = sum(item[3] for item in calls if item[1] != "main")
    waited = sum(item[2] for item in phases if item[0].endswith(".wait") or ".wait_" in item[0])
    main_calls = sum(item[3] for item in calls if item[1] == "main")
    return {"by_phase": by_phase, "attributed_ms": round(attributed, 1),
            "unattributed_ms": round(max(0.0, wall_ms - attributed), 1),
            "model_main_ms": round(main_calls, 1), "model_off_lane_ms": round(off_lane, 1),
            "overlap_ms": round(max(0.0, off_lane - waited), 1),
            "calls": len(calls), "hedges": sum(1 for item in calls if item[4].get("hedge")),
            "hedge_wins": sum(1 for item in calls if item[4].get("hedge") and item[4].get("won"))}


class _Null:
    """The trace used when none is attached: every method is a cheap no-op."""

    origin = 0.0

    def now(self):
        return time.monotonic()

    def phase(self, *args, **kwargs):
        pass

    @contextmanager
    def span(self, name, **attrs):
        yield {}

    def call(self, *args, **kwargs):
        pass

    def mark(self, *args, **kwargs):
        pass


NULL = _Null()


def lane():
    """The lane name for the calling thread: ``main`` unless it is a named worker."""
    name = threading.current_thread().name
    if name.startswith("mobster-speculate"):
        return "speculation"
    if name.startswith("mobster-prefetch"):
        return "prefetch"
    if name.startswith("mobster-hedge"):
        return "hedge"
    if name.startswith("mobster-predict"):
        return "prediction"
    return "main"

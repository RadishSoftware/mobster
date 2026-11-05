"""Predictive settle: wait the learned p90, read once, let the next verify read prove it.

Polling is the floor: the settle loop reads until two reads agree for 0.3 to 0.5 s. When an app's transition has
settled in X ms at least nine times in ten, a step can sleep X ms, take one read and go on. The next observation (the
frontier's verify read, taken while the model thinks) proves the read, the same contract as ``single_read``; so it
runs only for callers that verify (``single_read`` or ``early``), and only with ``MOBSTER_EXPERIMENTAL_SETTLE``.

Nothing here decides a step. A prediction that is missing, a read that still shows the screen before the action, or
pixels that moved during the read fall back to the polling loop, unchanged. Each settle row records what was
predicted and what happened (``predicted``: ms, ``outcome``: hit, miss reason or none; ``arrival_ms``).
"""

import threading
import time

from .store import SettleStore, enabled, shape_of
from .stream import WdaMjpegObserver

_store = None
_store_lock = threading.Lock()
STABLE_SECONDS = .18  # frame_clock.STABLE_SECONDS: the stream must be still this long before the read ended
READ_MARGIN_SECONDS = .35  # wda.observe.WDA_READ_MARGIN_SECONDS


def default_store():
    global _store
    with _store_lock:
        if _store is None:
            _store = SettleStore()
        return _store


def reset_default_store():
    global _store
    with _store_lock:
        _store = None


def arrival_ms(started, reads, before):
    """When the final screen first showed, in ms after ``started``: the first read of the trailing run of agreeing
    reads, or None when the run is shorter than two reads or shows the screen before the action."""
    if len(reads) < 2 or reads[-1][2] == before:
        return None
    first = len(reads) - 1
    while first > 0 and reads[first - 1][2] == reads[-1][2]:
        first -= 1
    if len(reads) - first < 2:
        return None
    return max(0.0, (reads[first][0] - started) * 1000)


def predicted_settle(driver, snapshot, action, watch, clock, deadline, started, cap, reads, info,
                     store=None):
    """A changed read after the learned wait, or None (``info`` says why). Reads happen at most once."""
    if not enabled() or not action:
        return None
    store = store or default_store()
    app, shape = snapshot.bundle_id, shape_of(action.get("op"))
    wait = store.p90(app, shape)
    if wait is None:
        info["outcome"] = "none"
        return None
    info["predicted"], info["shape"] = round(wait, 1), shape
    # The wait is counted from the dispatch's end, which is when ``started`` was taken.
    pause = min(started + wait / 1000, cap - READ_MARGIN_SECONDS) - time.monotonic()
    if pause > 0:
        time.sleep(pause)
    read_started = time.monotonic()
    if deadline.end - read_started <= READ_MARGIN_SECONDS:
        info["outcome"] = "no_time"
        return None
    try:
        current = driver.observe(timeout=deadline.remaining())
    except Exception:  # TransportError or TimeoutError: the polling loop owns the failure.
        info["outcome"] = "read_failed"
        return None
    now = time.monotonic()
    reads.append((read_started, now, current.content_fingerprint))
    if current.content_fingerprint == snapshot.content_fingerprint:
        info["outcome"] = "unchanged"
        return None
    if WdaMjpegObserver(clock, watch).still_between(read_started - STABLE_SECONDS, now) is False:
        info["outcome"] = "moved"
        return None
    info["outcome"] = "hit"
    return current


def annotate(driver, entry, action, snapshot, result, started, reads, info):
    """Put the prediction on the settle row, remember it on the driver (``predicted_settle``) and learn from the
    settle. Learns only from settles that ended on agreeing reads; a hit is one read, so it teaches nothing."""
    if not enabled():
        return
    arrival = arrival_ms(started, reads, snapshot.content_fingerprint)
    shape = shape_of((action or {}).get("op"))
    if arrival is not None:
        entry["arrival_ms"] = round(arrival, 1)
        default_store().add(snapshot.bundle_id, shape, arrival)
    if info is not None and "outcome" in info:
        entry.update({key: info[key] for key in ("predicted", "outcome", "shape") if key in info})
    driver.predicted_settle = {key: entry.get(key) for key in ("shape", "predicted", "outcome", "arrival_ms")
                               if entry.get(key) is not None} or None

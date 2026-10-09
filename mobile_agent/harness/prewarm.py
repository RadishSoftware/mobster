"""Prewarm (SPEC §3.1 F8, v1): get the phone ready while the person types, so the first action comes sooner.

``POST /api/prewarm`` (the composer calls it when it opens and while the person types, at most once every 20 s)
does, on a worker thread, for the phone a task would run on:

1. **wda**: resolves WebDriverAgent's session (creating it when the helper has none, which takes seconds after the
   helper starts), applies the session settings and learns the screen size (``WDA.configure``), and the frontier's
   tune. A run's own ``open_driver`` then finds all of it done.
2. **screen**: one settled screen read, with a FrameClock on the phone's live stream, kept for SCREEN_TTL (3 s). The
   run's first read reuses it (``take``) only when the stream shows not one changed byte since that read began
   (frontier.STILL_MARGIN's rule), the same WDA session reads it, and the app in front is the one the task starts in.
   Anything else reads the screen afresh.

It never acts, never touches a phone with a task running (in this process, or another one holding the phone's lease:
that lease is taken for an instant to check, then let go, so a task started that moment is never refused), and never
answers 5xx for a cold phone: what couldn't be warmed is simply left out of ``warmed``. (v2: the model connection.)
"""

import dataclasses
import logging
import threading
import time
from urllib.parse import quote

from .. import engines
from ..compose import build_target_driver
from ..frame_clock import attach_frame_clock
from ..journal import JournalError, Lease

log = logging.getLogger("mobster.harness")

SCREEN_TTL = 3.0         # seconds a prewarmed screen read may be reused for
MIN_INTERVAL = 2.0       # a second prewarm of one phone within this answers from the first
WAIT = 1.5               # how long the route waits for the warm-up before answering 202
OBSERVE_TIMEOUT = 5.0
SPRINGBOARD = "com.apple.springboard"


@dataclasses.dataclass
class Warm:
    url: str
    session: str
    snapshot: object
    clock: object
    read_started: float


_lock = threading.Lock()
_screens = {}      # WDA url -> Warm
_jobs = {}         # WDA url -> (thread, started monotonic, result dict)


def _switch(name):
    return dict(engines.SMART_CONFIG.switches).get(name, "on") != "off"


def _close_clock(clock):
    try:
        if clock is not None:
            clock.close()
    except Exception:  # noqa: BLE001
        pass


def _drop(url, warm=None):
    """Forget ``url``'s screen (only ``warm`` when given) and release its clock."""
    with _lock:
        current = _screens.get(url)
        if current is None or (warm is not None and current is not warm):
            return
        del _screens[url]
    _close_clock(current.clock)


def _lease_free(url):
    """True when no Mobster process holds this phone's lease now (taken and let go at once)."""
    try:
        lease = Lease.device(url.rstrip("/"), wait=0)
    except JournalError:
        return False
    except Exception:  # noqa: BLE001 -- can't tell: treat it as busy, never as free
        return False
    lease.close()
    return True


def _busy(runtime, phone):
    try:
        if runtime.run_on(phone) is not None:
            return True
    except Exception:  # noqa: BLE001
        return True
    return not _lease_free(phone.wda_url)


def _warm(runtime, phone, result):
    """Warm ``phone`` up; fills ``result["warmed"]``. Never raises."""
    url = phone.wda_url
    driver = None
    try:
        if _busy(runtime, phone):
            result["reason"] = "busy"
            return
        session = phone.wda_session()
        if not session:
            result["reason"] = "no_session"
            return
        driver = build_target_driver(wda_url=url, session=session)
        tune = getattr(driver, "tune", None)
        if callable(tune):
            tune(rich=_switch("MOBSTER_RICH_ROWS"), glide=_switch("MOBSTER_GLIDE_ALL"))
        result["warmed"].append("wda")
        video = getattr(phone, "video", None)
        clock = (attach_frame_clock(driver, video, mode="on")
                 if video is not None and _switch("MOBSTER_FRAME_CLOCK") else None)
        if clock is None or _busy(runtime, phone):
            return
        started = time.monotonic()
        observe = getattr(driver, "observe_ready", None) or driver.observe
        snapshot = observe(timeout=OBSERVE_TIMEOUT)
        read_started = float(getattr(snapshot, "read_started", 0.0) or started)
        driver.frame_clock = None   # the clock outlives this driver, for SCREEN_TTL
        warm = Warm(url, session, snapshot, clock, read_started)
        with _lock:
            old, _screens[url] = _screens.get(url), warm
        if old is not None:
            _close_clock(old.clock)
        timer = threading.Timer(SCREEN_TTL + 0.5, _drop, args=(url, warm))
        timer.daemon = True
        timer.start()
        result["warmed"].append("screen")
    except Exception as error:  # noqa: BLE001 -- a cold or busy phone is never an error here
        result.setdefault("reason", type(error).__name__)
        log.info("prewarm stopped: %s", type(error).__name__)
    finally:
        if driver is not None:
            clock = getattr(driver, "frame_clock", None)
            if clock is not None:
                driver.frame_clock = None
                _close_clock(clock)
            try:
                driver.close()
            except Exception:  # noqa: BLE001
                pass


def prewarm(runtime, device=None, *, wait=WAIT):
    """Warm the phone ``device`` names (None: the one a new task runs on). Returns {"warmed": [...]} with what is
    warm now ("wda", "screen"), "pending" while the worker still runs, and "reason" when something was skipped."""
    phone = runtime.resolve_device(device)
    url = getattr(phone, "wda_url", None)
    if not url:
        return {"warmed": [], "pending": False, "reason": "no_phone"}
    now = time.monotonic()
    with _lock:
        job = _jobs.get(url)
        if job is None or (not job[0].is_alive() and now - job[1] >= MIN_INTERVAL):
            result = {"warmed": []}
            thread = threading.Thread(target=_warm, args=(runtime, phone, result), name="mobster-prewarm",
                                      daemon=True)
            job = _jobs[url] = (thread, now, result)
            thread.start()
    thread, _, result = job
    thread.join(max(0.0, wait))
    out = {"warmed": list(result["warmed"]), "pending": thread.is_alive()}
    if result.get("reason"):
        out["reason"] = result["reason"]
    return out


def take(driver, run_context=None, *, now=None):
    """The prewarmed read of ``driver``'s phone as its first screen, or None. (snapshot, age in seconds)."""
    url = getattr(driver, "url", None)
    if not url:
        return None
    with _lock:
        warm = _screens.pop(url, None)
    if warm is None:
        return None
    try:
        now = time.monotonic() if now is None else now
        age = now - warm.read_started
        if age > SCREEN_TTL or age < 0:
            return None
        if not str(getattr(driver, "prefix", "")).endswith("/" + quote(warm.session, safe="")):
            return None
        expected = getattr(run_context, "app_bundle", None) or SPRINGBOARD
        if getattr(warm.snapshot, "bundle_id", None) != expected:
            return None
        from ..frontier import STILL_MARGIN
        still = warm.clock.still_for()
        if still is None or now - still > warm.read_started - STILL_MARGIN:
            return None   # something on screen changed since the read began (or the stream can't say)
        # Unchanged from the read until now: any start in between is true. The latest the run's own clock can
        # vouch for, so its first action reuses it too (frontier._refreshed) and its picture fits (video_frame).
        own = getattr(getattr(driver, "frame_clock", None), "still_for", None)
        try:
            run_still = own() if callable(own) else None
        except Exception:  # noqa: BLE001
            run_still = None
        started = max(warm.read_started, now - run_still if run_still is not None else now)
        return dataclasses.replace(warm.snapshot, captured_at=now, read_started=started), age
    except Exception:  # noqa: BLE001
        return None
    finally:
        _close_clock(warm.clock)


def close_all(runtime=None):
    """Release every kept read and its clock (Runtime.close)."""
    with _lock:
        warms = list(_screens.values())
        _screens.clear()
    for warm in warms:
        _close_clock(warm.clock)


def reset_for_tests():
    close_all()
    with _lock:
        _jobs.clear()

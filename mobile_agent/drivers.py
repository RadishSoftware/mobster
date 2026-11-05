"""Device adapters: the driver contract and WebDriverAgent (WDA). Extensions may register more."""

import abc
import json
import os
import time
from urllib.parse import quote

from .state import ADJUSTABLE_ROLES, HOST_KEY_OPERATIONS, WDA_HOST_KEY_NAMES, validate_bundle_id
from .transport import HTTP, Deadline, TransportError, error_name
from .wda import timeouts as wda_timeouts
from .wda import watchdog as wda_watchdog
from .wda.gestures import (
    WDA_GLIDE_SPAN,
    WDA_SWIPE_SECONDS,
    WDAGestures,
    _swipe_points,
    clear_stroke_start,
    switch_point,
)
from .wda.guards import finite_positive, grounded_target
from .wda.observe import WDA_STATE_SECONDS, WDAObserve
from .wda.text import WDAText

# Snapshot depth. 25 was chosen when a full-depth widget home screen took
# 40.5 s with WDA's `visible` attribute; without it the home screen reads in
# ~270 ms at depth 80. Depth 25 then blinded apps with deep view trees
# (Spotify: text recall 0.23 at 25 vs 0.77 at 50, +70 ms). 60 stays within the
# parser's 64-level nesting limit.
WDA_SNAPSHOT_MAX_DEPTH = 60
# WDA's default idle wait held every tap for ~1.2 s; with both at 0 a W3C tap
# returns in ~470 ms. Mid-animation reads are then possible, so settling
# requires consecutive identical reads instead (WDA.wait_for_change).
# A read that cannot finish fails in seconds instead of hanging: WDA serves one request
# at a time, and on 2026-09-23 stuck TikTok reads (default 15 s snapshot timeouts,
# stacked by retries) froze it for 21 minutes. Normal reads take 0.13-0.21 s.
WDA_SNAPSHOT_TIMEOUT = 6
WDA_SESSION_SETTINGS = {"snapshotMaxDepth": WDA_SNAPSHOT_MAX_DEPTH,
                        "waitForIdleTimeout": 0, "animationCoolOffTimeout": 0,
                        "snapshotTimeout": WDA_SNAPSHOT_TIMEOUT, "customSnapshotTimeout": WDA_SNAPSHOT_TIMEOUT}
# Requests after which the page shown may be another one.
# (Switching to an app and back keeps its tab: only a fresh launch or a URL opens another.)
PAGE_SWITCHING_PATHS = frozenset({"/wda/apps/launch", "/url"})
# /window/size resolves the active application, which can hang on a system overlay.
WDA_WINDOW_SIZE_SECONDS = 3
# Until the exact screen center is known: a point inside every iPhone screen
# (the smallest is 320x568 pt), below the banner area and away from the edges.
WDA_FALLBACK_DETECTION_POINT = "160,320"
# snapshotMaxChildren (WDA's default is unbounded): a bound on one node's children in a snapshot.
# iOSWorld mem-046's runner died on CityRide's map screens in 2 of 4 runs (ConnectionReset, then refused).
WDA_MAX_CHILDREN = 300
# maxTypingFrequency on a simulator (WDA's default 60 types ~57 characters a second). Measured on an
# iOSWorld simulator (F2, 26 Sep; 20 bodies of 500 characters typed into a Notes body at each rate, read
# back whole): 60, 120, 180, 240, 360, 480, 720 and 960 all dropped no character; 500 characters took
# 8.8 / 4.6 / 3.3 / 2.6 / 1.9 / 1.6 / 1.2 / 1.1 s. The phone keeps WDA's default until measured there.
WDA_SIMULATOR_TYPING_FREQUENCY = 960
# Text at least this long goes in through the pasteboard and the edit menu's Paste, not keystrokes, at
# WDA's typing rate (~57 characters a second: 1,000 take ~18 s, a paste ~2.4 s). A faster rate moves the
# line (WDA.tune): at 960 a simulator typed 1,000 characters in 1.5 s, faster than the paste (F2, 26 Sep).
WDA_PASTE_MIN_CHARS = 200
# Characters typed per second per unit of maxTypingFrequency at high rates (450 a second at 960), and a
# paste's usual seconds: typing is kept while it is expected to be quicker.
WDA_TYPED_PER_RATE, WDA_PASTE_SECONDS = .47, 2.5
# Recovery: how long a restarted or re-supervised runner has to answer /status again.
WDA_RECOVER_SECONDS = 90
# Opening or closing a USB iPhone's screen-capture device (WATCH hears and sees through it) resets the phone's USB
# network link: a blip of 0.4-1.1 s at once, then xcodebuild, which hosts the runner over that link, exits 6-16 s
# later and the runner with it, so the session is gone (iPhone 15 Pro, iOS 26.0.1, 14 Oct 2026: 2 of 2 opens and
# 2 of 2 closes; finding the device without opening it changed nothing in 60 s). after_capture waits out the blip,
# then restarts the runner at once (runner_restart) instead of waiting for the old one to die.
WDA_LINK_BLIP_SECONDS = 2.0
# Without runner_restart (a runner Mobster doesn't manage): how long the doomed runner may keep answering.
WDA_LINK_DOOMED_SECONDS = 25.0


def _env_int(name):
    """A positive integer from the environment, else None."""
    try:
        value = int(os.environ.get(name, "") or 0)
    except ValueError:
        return None
    return value if value > 0 else None


class _Declared(abc.ABCMeta):
    """Driver's metaclass: a driver is checked against its declared capabilities once its ``__init__`` has run."""

    def __call__(cls, *args, **kwargs):
        driver = super().__call__(*args, **kwargs)
        check_capabilities(driver)
        return driver


class Driver(abc.ABC, metaclass=_Declared):
    """Required device contract. Instantiation fails when a method is missing.

    Optional capabilities are probed with ``getattr`` at the call site, because each has a load-bearing fallback.
    ``OPTIONAL`` lists them with what their absence means, and ``capabilities`` names the ones a driver has:
    instantiation fails when it names one the new driver lacks or leaves one out, and ``operations_for`` derives
    what a loop may offer the driver from it. tests/test_driver_contract.py
    fails when code probes a driver for a name that isn't listed.
    """

    OPTIONAL = {
        # Reading
        "observe_ready": "a settled read; without it, observe",
        "wait_for_change": "a native wait for the screen to change; without it, observe with the whole budget",
        "capture_preview": "an operator preview frame; without it, another capture route (no default)",
        "active_app": "the bundle id in front; without it, the snapshot's bundle_id",
        "slow_front": "the app in front when its tree is too slow to wait for, confirmed without the tree; without it, "
                      "the front app comes from active_app",
        "pixel_snapshot": "the screen in front as a tree read from a screenshot, for an app whose tree is too slow; "
                      "without it, the evidence read waits for the tree",
        "frame_clock": "a FrameClock on the phone's video (frame_clock.attach_frame_clock sets it)",
        "settled_by": "how the last settle was proven (one_read, agreeing_reads...); without it, unproven",
        "predicted_settle": "the last settle's prediction and outcome (MOBSTER_EXPERIMENTAL_SETTLE); without it, none",
        "settles_early": "the last settle ended before its quiet period, on a frame-clock proof",
        "settles_on_one_read": "a settle may end on one read (wait_for_change's single_read: True, or \"rest\" for one "
                               "read only when the pixels already rest)",
        "ax_only_steps": "steps left that read the tree with no screenshot",
        "next_step": "called at the top of each turn; returns the steps left of ax_only_steps",
        "source_reads": "how many screen reads this driver made (the run's timing)",
        "source_seconds": "the seconds those reads took",
        "rich_rows": "reads disabled and selected controls (WDA.tune(rich=True))",
        "tune": "set the driver up for the agent (rich rows, glides, typing rate); without it, as it is",
        "evidence_source": "the screen's tree as WebDriverAgent-shaped XML, read by the connection itself (Android); "
                           "without it, verify reads WebDriverAgent's /source through call",
        # Acting beyond execute
        "tap_point": "tap a screen point (TAP_XY, DISMISS), with informational purpose; without it, those actions fail",
        "dismiss_at": "where DISMISS taps; without it, tap_point",
        "long_press": "LONG_PRESS; without it, the action fails",
        "set_value": "set an adjustable control (a picker wheel); without it, SET_TEXT on one fails",
        "write_text": "type with a read-back and one repair; without it, execute TYPE, which types at the end",
        "replace_text": "clear and type through one element reference; without it, clear_text then TYPE",
        "clear_text": "empty a field; without it, SET_TEXT to nothing fails",
        "relaunch_app": "quit and reopen an app (a fresh start); without it, LAUNCH_APP",
        "launch": "open an app and return its first screen (App X-ray); without it, LAUNCH_APP, a pause and a read",
        "edge_back": "the left-edge swipe that goes back (App X-ray); without it, a raw drag on "
        "WebDriverAgent, else none",
        "open_url": "open a link on the phone; without it, WebDriverAgent's /url through call",
        "drag_next": "a slow short drag that reveals a list row's buttons",
        "last_swipe_glided": "the last swipe was a glide, so it can't have flung past rows",
        "last_relaunch": "when the driver last relaunched the app",
        "recover": "restart a stuck connection; without it, the run ends on the error",
        "after_capture": "bring the connection back after a USB iPhone's capture device closed, which resets its link "
                         "(WATCH, MCP listen); without it, the next step's own recovery does",
        # WebDriverAgent only: the skills that read Notification Center and codes send raw requests
        "call": "a raw WebDriverAgent request (method, path, body); without it, those skills aren't offered",
        "call_trace": "the WebDriverAgent requests of the last step, for timing reports",
        "url": "the WebDriverAgent address",
        "prefix": "the WebDriverAgent session path",
        "http": "the WebDriverAgent HTTP client (ios.system reads the iOS version from /status); without it, unknown",
        # Image-only connections
        "helper": "the native helper that captures and acts (iPhone Mirroring, Cable + Bluetooth)",
        # iOS's own moves (ios.system), on any connection
        "recent_dialogs": "system_dialogs.Recent owned by this driver; absence starts with no "
        "clipboard or link evidence",
        "opens_by_bundle": "False: apps can't open by bundle id, so open_app goes by name (Spotlight, App Library)",
        "reads_front_app": "False: active_app is a guess, so an app counts as opened when iOS's own screens left",
        "system_shortcut": "send the connection's key for home, spotlight or app_switcher; without it, Home and taps",
        "type_focused": "type into the field that has the keyboard; without it, TYPE on the field element",
        "clear_focused": "empty the field that has the keyboard; without it, its Clear text button when shown",
        "ios_version": "the phone's iOS version when the connection knows it; without it, WebDriverAgent's /status",
        "effects": "the methods that change the device, on a driver with no WebDriverAgent transport, so a verify run "
                   "journals each call; without it, the run journals WebDriverAgent's HTTP",
        # VerifyRun's private wrappers: absence means the driver is not owned by that run layer yet.
        "_verify_action_driver_journal": "the action journal owning recovery callbacks; without it, this run may "
                                         "guard them",
        "_verify_source_owner": "the run observing accessibility source reads; without it, this run may attach",
    }

    capabilities: frozenset[str] = frozenset()
    can_type = False
    last_image = None

    @abc.abstractmethod
    def observe(self, timeout=10):
        """Read the current screen. Never mutates the device."""

    @abc.abstractmethod
    def execute(self, operation, target, snapshot, text=None, timeout=10):
        """Dispatch one grounded action. At most once; never retried here."""

    @abc.abstractmethod
    def close(self):
        """Release driver resources. Never terminates the caller's session."""


def check_capabilities(driver):
    """Raise TypeError unless ``driver.capabilities`` names exactly the Driver.OPTIONAL attributes ``driver`` has."""
    declared = frozenset(driver.capabilities)
    present = frozenset(name for name in Driver.OPTIONAL if hasattr(driver, name))
    if declared != present:
        raise TypeError(f"{type(driver).__name__}.capabilities declares {sorted(declared - present)} it doesn't have "
                        f"and leaves out {sorted(present - declared)} it has. Set capabilities to the Driver.OPTIONAL "
                        "names the driver has.")


# What an operation needs beyond execute: any one of these Driver.OPTIONAL names. An operation not listed needs
# nothing more (READ_LIST and SCROLL_TO swipe and read through execute and observe).
OPERATION_NEEDS = {"LONG_PRESS": frozenset({"long_press"}), "TAP_XY": frozenset({"tap_point"}),
                   "DISMISS": frozenset({"dismiss_at", "tap_point"})}


def operations_for(capabilities, base_operations, role=None):
    """The operations of ``base_operations``, in order, that a loop may offer a driver with ``capabilities``: each
    one whose OPERATION_NEEDS the driver meets. With ``role``, those it may offer on an element of that role:
    SET_TEXT on an adjustable control (a picker wheel) needs set_value, while on a field it clears and types
    through execute."""
    def offered(op):
        needs = frozenset({"set_value"}) if op == "SET_TEXT" and role in ADJUSTABLE_ROLES else OPERATION_NEEDS.get(op)
        return needs is None or not needs.isdisjoint(capabilities)
    return tuple(op for op in base_operations if offered(op))


def call_kind(method, path):
    """A WDA request's kind for timing reports: "GET source", "POST element/*/value"."""
    parts = path.split("?", 1)[0].strip("/").split("/")
    if len(parts) > 1 and parts[0] == "element" and parts[1] != "active":
        parts[1] = "*"
    return method + " " + "/".join(parts)


def resolve_wda_session(url, preferred=None, timeout=5, create=True):
    """The WDA session to drive: WDA serves one at a time, so its own report wins.

    A configured id goes stale whenever the runner restarts (measured: every
    restart issued a new id and the server failed until restarted by hand).
    Preference: the session WDA reports as current, then ``preferred`` only if
    WDA reports none and it is still accepted, else a newly created session.
    Returns None when WDA is unreachable or has no session and ``create`` is off.
    """
    client = HTTP(url)
    try:
        status = client.request("GET", "/status", timeout=timeout)
        current = status.get("sessionId") if isinstance(status, dict) else None
        if isinstance(current, str) and current:
            return _settled_session(client, url, current, timeout)
        if preferred:
            try:
                # The session itself, not /window/size: that waits for the app to go idle,
                # which an autoplaying feed (TikTok) never does.
                client.request("GET", "/session/" + quote(preferred, safe=""), timeout=timeout)
                return _settled_session(client, url, preferred, timeout)
            except Exception:
                pass
        if not create:
            return None
        created = client.request("POST", "/session", {"capabilities": {"alwaysMatch": {
            "platformName": "iOS", "shouldWaitForQuiescence": False, "waitForIdleTimeout": 0}}},
            timeout=max(timeout, 30))
        session = created.get("sessionId") or (created.get("value") or {}).get("sessionId")
        if not isinstance(session, str) or not session:
            raise TransportError("WDA did not create a session")
        return _settled_session(client, url, session, timeout)
    finally:
        client.close()


# Sessions already given the no-idle-wait settings, per WDA URL. WDA by default waits
# for the app to go idle before every read and action; an app playing video never
# does, so reads hung until timeout (measured on TikTok, 2026-09-23) and each stuck
# call blocked WDA's single request queue for everything after it.
_settled_sessions = {}


def _settled_session(client, url, session, timeout):
    if _settled_sessions.get(url) != session:
        try:
            client.request("POST", "/session/" + quote(session, safe="") + "/appium/settings",
                           {"settings": dict(WDA_SESSION_SETTINGS)}, timeout=timeout)
            _settled_sessions[url] = session
        except Exception:
            pass  # Retried on the next resolve; the driver's configure() applies them too.
    return session



def wait_out_capture(status_ok, runner_restart=None, timeout=WDA_RECOVER_SECONDS, *, sleep=time.sleep,
                     clock=time.monotonic):
    """After a USB iPhone's screen-capture device closed (WDA_LINK_BLIP_SECONDS), wait until the runner that serves
    next is the one to attach to: wait out the link's blip, then restart the runner now (``runner_restart``, which
    returns a check that the new one serves) and wait for it to say so, or, without one, wait for the old runner to
    stop answering (``status_ok(timeout)``), since its host is about to exit. The old runner may answer a while longer:
    measured 4 s after a restart, through the restarted relay, and attaching to it would lose the session again.
    Returns whether it restarted the runner. The caller then waits for /status and attaches."""
    started = clock()
    sleep(WDA_LINK_BLIP_SECONDS)
    fresh = None
    if callable(runner_restart):
        try:
            fresh = runner_restart()
        except Exception:  # noqa: BLE001 -- not this phone's managed runner any more: wait for its own end
            fresh = None
    if callable(fresh):
        while clock() - started < timeout and not fresh():
            sleep(.5)
        return True
    doomed = clock() + WDA_LINK_DOOMED_SECONDS
    while clock() < doomed and status_ok(1.5):
        sleep(.5)
    return False

class WDA(WDAObserve, WDAGestures, WDAText, Driver):
    capabilities = frozenset({
        "observe_ready", "wait_for_change", "capture_preview", "active_app", "slow_front", "pixel_snapshot",
        "frame_clock", "settled_by", "predicted_settle", "settles_early", "settles_on_one_read", "ax_only_steps",
        "next_step",
        "source_reads", "source_seconds", "rich_rows", "tune", "tap_point", "long_press", "set_value", "write_text",
        "replace_text", "clear_text", "relaunch_app", "launch", "drag_next", "last_swipe_glided", "last_relaunch",
        "recover", "after_capture", "call", "call_trace", "url", "prefix", "http"})
    can_type = True
    # wait_for_change(on_settling=...) reports a likely-final screen before it is proven.
    reports_settling = True

    # Screen size in points per WDA URL: fixed for a phone, read once per process.
    _screen_sizes = {}
    # Apps whose reads needed the foreground hint, per WDA URL, for this process:
    # every run builds a new driver, and each failed read costs ~8 s.
    _overlay_apps = {}
    # Optional JSON file that keeps _overlay_apps across processes (the app's
    # state directory). Measured 2026-09-23: with the invisible Siri overlay up,
    # the first read of every new process took 8.3 s (a failed read, then the
    # hint) and 0.2 s afterwards; a remembered app is hinted at activation.
    overlay_hint_path = None
    # A list to collect every WDA request and read as (kind, started monotonic, ms): the bench's
    # per-action cost report. None (the default) records nothing.
    call_trace = None
    # WDA.tune (the frontier): rich rows (Other leaves, disabled and selected controls), glides in
    # every app (FrameClock demotes one that keeps moving), and the settings below.
    rich_rows = False
    glide_everywhere = False
    tuned_settings = None
    paste_min_chars = WDA_PASTE_MIN_CHARS
    last_swipe_glided = False
    # A callable that restarts this WDA runner (a simulator's xcodebuild).
    # None: recover() waits for a supervisor to bring it back (the USB runner is supervised).
    restarter = None
    # runner_restart() -> None: restarts this phone's runner now (the device manager's start_runner), for
    # after_capture; None for a runner Mobster doesn't manage.
    runner_restart = None
    # killer(bundle_id, timeout) -> bool: closes an app without WDA (app_killer.py), when WDA's terminate fails.
    killer = None
    last_relaunch = None
    last_recovery = None
    # Simulators by WDA URL (None: not asked yet).
    _simulators = {}

    def __init__(self, url, session):
        if not session:
            raise ValueError("An existing WDA session ID is required")
        self.url = url
        self.http = HTTP(url)
        # Operator screenshots must not contend for the action client's in-flight lock.
        self.preview_http = HTTP(url)
        self.prefix = "/session/" + quote(session, safe="")
        # pressButton is a core WDA endpoint; a WDA too old to serve it
        # rejects the dispatch, which surfaces as a TransportError.
        self.host_operations = frozenset(HOST_KEY_OPERATIONS)
        self.last_image = None
        self._stable = None
        self._settled = {}
        # When the last action was dispatched: a layer visibility answer from before it is asked again.
        self._dispatched_at = float("-inf")
        self._swiped_at = float("-inf")
        self._detection_point_pending = False
        self._pages_stale = False
        self._page_groups = ()
        self._stalls_seen = 0
        self._drag_once = False
        self._write_rank = None
        self._foreground_hint = None
        self._pane_visibility = {}
        self._last_bundle = ""
        self._hinted = None
        self._frame_watch = None
        self._frame_action = None
        self._frame_followup = None

    @classmethod
    def load_overlay_hints(cls, path):
        """Remember apps that needed the foreground hint across processes."""
        cls.overlay_hint_path = path
        try:
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, ValueError):
            return
        if isinstance(saved, dict):
            for url, bundles in saved.items():
                if isinstance(url, str) and isinstance(bundles, list):
                    for bundle in bundles[:64]:
                        try:
                            cls._overlay_apps.setdefault(url, set()).add(validate_bundle_id(bundle))
                        except ValueError:
                            pass

    def _save_overlay_hints(self):
        path = WDA.overlay_hint_path
        if not path:
            return
        try:
            data = {url: sorted(bundles) for url, bundles in WDA._overlay_apps.items()}
            temporary = f"{path}.tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(temporary, path)
        except OSError:
            pass

    def call(self, method, path, body=None, timeout=None):
        # None: the bound for this class of call (wda_timeouts.WDA_CALL_TIMEOUTS).
        # Any value is capped at MAX_WDA_TIMEOUT.
        timeout = wda_timeouts.bounded(timeout, method, path)
        if method == "POST" and path in PAGE_SWITCHING_PATHS:
            self._pages_stale = True  # another app or screen: which tab page shows is asked again
        started = time.monotonic()
        try:
            # An action is recorded while it is in flight;
            # one that ends unanswered is an unknown outcome, never re-sent.
            result = wda_watchdog.send(self.url, method, path,
                                       lambda: self.http.request(method, self.prefix + path, body, timeout))
        finally:
            if self.call_trace is not None:
                self.call_trace.append((call_kind(method, path), started, (time.monotonic() - started) * 1000))
        if path in ("/wda/apps/activate", "/url") and isinstance(body, dict) and body.get("bundleId"):
            # The app a caller brought forward is WDA's active application from now on.
            # Measured 2026-09-24 (iOS 26.0.1): with detection on "auto", every Safari read
            # failed after 8 s (HTTP 404, the Siri overlay as active app), including right
            # after a /url open; naming Safari read the page in 0.5-2.8 s. One settings
            # call (~50 ms) is far cheaper than a single failed read.
            self._foreground_hint = body.get("bundleId")
            self._hint_app(self._foreground_hint, timeout)
        return self.response_value(result)

    def relaunch_app(self, bundle, timeout=40):
        """Close and reopen ``bundle`` when WDA answers and the app does not; False when WDA doesn't (recovery's job).
        A stuck accessibility server blocks every read until its app closes; a WDA restart doesn't clear it (10 Oct)."""
        deadline = Deadline(timeout)
        try:
            self.http.request("GET", "/status", None, min(5, deadline.remaining()))
        except Exception:
            return False
        self.last_relaunch = "wda"
        try:
            self.call("POST", "/wda/apps/terminate", {"bundleId": bundle}, min(15, deadline.remaining()))
        except Exception:
            # A stuck app can block WDA's own terminate too (ab1, 11 Oct): close it out of band when this device has
            # a way to (app_killer.py), else the failure stands.
            if not callable(self.killer) or not self.killer(bundle, max(1.0, min(15, deadline.remaining()))):
                raise
            self.last_relaunch = "out_of_band"
        self.call("POST", "/wda/apps/activate", {"bundleId": bundle}, min(20, deadline.remaining()))
        self._pages_stale = True  # the app comes back on its default tab: which page shows is asked again
        return True

    def _hint_app(self, bundle, timeout):
        self.call("POST", "/appium/settings", {"settings": {"defaultActiveApplication": bundle}}, timeout)
        self._hinted = bundle

    @staticmethod
    def response_value(result):
        # WDA answers every command with a value, null included. With none, nothing says the command ran or didn't.
        if "value" not in result:
            raise TransportError("WDA answered with no value; request outcome unknown; not retried")
        value = result.get("value")
        if (isinstance(value, dict) and value.get("error")) or result.get("status", 0) != 0:
            raise TransportError("WDA rejected command; not retried", status=200, wda_error=error_name(value))
        return value

    def configure(self, timeout=10):
        """Apply measured session settings to the caller's session.

        The settings go first, before anything that WDA resolves through the
        "active application": a fresh session otherwise pays WDA's default idle
        wait on that first call. A rejection fails the run here rather than
        silently observing at 40-second snapshots.

        WDA decides the foreground app by hit-testing one point, default
        (64, 64): under a notification banner. On a real phone an incoming
        message then made SpringBoard "active" mid-task and put the banner's
        text in the observation. The screen center is where the app is.
        Measured 2026-09-23: /window/size on a fresh session hung 8-10 s and
        failed ("stale element ... com.apple.siri.IntelligentLight",
        kAXErrorServerNotFound) while a system overlay was the active app. It
        is asked briefly; if it cannot answer, the first observation's
        Application frame supplies the center instead.
        """
        deadline = Deadline(timeout)
        # The screen size is fixed per phone: after the first run the centre goes
        # out with the other settings in one request (the desktop app lost ~3 s per
        # run to a /window/size timeout here).
        known = WDA._screen_sizes.get(self.url)
        if known:
            self._set_detection_point(*known, deadline.remaining())
            return
        # Never leave WDA's (64, 64) default in force, even briefly: it sits
        # under notification banners and the iOS 26 edge-glow overlay.
        settings = {**WDA_SESSION_SETTINGS, "activeAppDetectionPoint": WDA_FALLBACK_DETECTION_POINT}
        self.call("POST", "/appium/settings", {"settings": settings}, deadline.remaining())
        self._detection_point_pending = True
        size = None
        try:
            # /wda/screen measures through SpringBoard, never the "active" app that
            # a system overlay can hijack; /window/size is the fallback.
            screen = self.response_value(self.http.request("GET", "/wda/screen", None,
                                                           min(WDA_WINDOW_SIZE_SECONDS, deadline.remaining())))
            size = screen.get("screenSize") if isinstance(screen, dict) else None
        except (TransportError, TimeoutError, ValueError):
            size = None
        if not (isinstance(size, dict) and all(finite_positive(size.get(k)) for k in ("width", "height"))):
            try:
                size = self.call("GET", "/window/size", timeout=min(WDA_WINDOW_SIZE_SECONDS, deadline.remaining()))
            except (TransportError, TimeoutError, ValueError):
                return
        if isinstance(size, dict) and all(finite_positive(size.get(k)) for k in ("width", "height")):
            WDA._screen_sizes[self.url] = (size["width"], size["height"])
            self._set_detection_point(size["width"], size["height"], deadline.remaining())

    def _set_detection_point(self, width, height, timeout):
        settings = {**WDA_SESSION_SETTINGS, "activeAppDetectionPoint": f"{width / 2:.0f},{height / 2:.0f}"}
        self.call("POST", "/appium/settings", {"settings": settings}, timeout)
        self._detection_point_pending = False

    def launch(self, bundle_id, timeout=20):
        """Bring an app forward and return its first settled observation.

        Activation applies the remembered foreground hint (overlay_hint_path),
        which is what makes the first read fast (measured: 9.0 s -> 0.16 s in a
        fresh process under the Siri overlay). The read then settles with
        ``observe_ready`` (the FrameClock rule in "on" mode). Waiting for the
        pixels before reading was measured and rejected: iOS's zoom-in
        animation outlasts the app's AX tree, so warm launches got slower
        (Settings 0.70 -> 1.49 s, Safari 1.5-1.8 -> 3.0 s). Returns
        ``(snapshot, {"activate_ms", "ready_ms"})``.
        """
        bundle = validate_bundle_id(bundle_id)
        deadline = Deadline(timeout)
        started = time.monotonic()
        self._dispatching()
        self.call("POST", "/wda/apps/activate", {"bundleId": bundle}, min(10, deadline.remaining()))
        info = {"activate_ms": round((time.monotonic() - started) * 1000, 1)}
        snapshot = self.observe_ready(timeout=deadline.remaining())
        info["ready_ms"] = round((time.monotonic() - started) * 1000, 1)
        if self.frame_clock is not None:
            self.frame_clock.record({"kind": "launch", "bundle": bundle, **info})
        return snapshot, info

    def active_app(self, timeout=5):
        """The foreground bundle, read device-wide within the probe bound: a TransportError means it is unknown.
        A slow-tree app is confirmed by slow_front: activeAppInfo there took 17-50 s, then WDA died (14 Oct, TikTok)."""
        timeout = min(float(timeout), wda_timeouts.WDA_CALL_TIMEOUTS["probe"])
        started = time.monotonic()
        slow = self.slow_front(min(WDA_STATE_SECONDS, timeout))
        if slow:
            return slow
        timeout = max(.25, timeout - (time.monotonic() - started))
        value = self.response_value(self.http.request("GET", "/wda/activeAppInfo", timeout=timeout))
        bundle = value.get("bundleId") if isinstance(value, dict) else None
        if not isinstance(bundle, str) or not bundle:
            raise TransportError("WDA did not report a foreground app")
        return bundle

    def capture_preview(self, timeout=3):
        try:
            result = self.preview_http.request("GET", self.prefix + "/screenshot", timeout=timeout)
        except (TransportError, TimeoutError) as error:
            if isinstance(error, TimeoutError) or "timeout" in str(error).casefold():
                self.note_screenshot_timeout()
            raise
        value = result.get("value") if isinstance(result, dict) else None
        if isinstance(value, dict) and "screenshot" in str(value.get("message") or "").casefold() \
                and "timeout" in str(value.get("message") or "").casefold():
            self.note_screenshot_timeout()
        image = self.response_value(result)
        self.screenshot_timeouts = 0
        if not isinstance(image, str) or not image:
            raise TransportError("WDA screenshot unavailable")
        return "data:image/png;base64," + image

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        deadline = Deadline(timeout)
        if operation in HOST_KEY_OPERATIONS:
            # WDA is device-level, so no bundle scoping applies: HOME leaves
            # the app and later observations simply describe the new screen.
            # pressButton (not the sessionless /wda/homescreen) carries all
            # three keys; verified live against upstream WDA on iOS 26.
            self._dispatching()
            self._frame_mark(operation)
            self.call("POST", "/wda/pressButton",
                      {"name": WDA_HOST_KEY_NAMES[operation]}, deadline.remaining())
            self._frame_dispatched()
            return {"dispatch_attempted": True}
        if operation == "LAUNCH_APP":
            # The switch is trusted like any dispatched action; the next
            # snapshot reports the foreground bundle from its Application node.
            bundle = validate_bundle_id(target)
            self._dispatching()
            self._frame_mark(operation)
            self.call("POST", "/wda/apps/activate", {"bundleId": bundle}, deadline.remaining())
            self._frame_dispatched()
            return {}
        if operation == "SUBMIT":
            grounded_target(operation, target, snapshot)
            if "SUBMIT" not in target.actions:
                raise ValueError("SUBMIT needs a keyboard and an editable field")
            self._check_current(snapshot, deadline)
            self._dispatching()
            self._frame_mark(operation)
            # Return on the focused field: Go in Safari, Search in search bars.
            self.call("POST", "/wda/keys", {"value": ["\n"]}, deadline.remaining())
            self._frame_dispatched()
        elif operation == "TAP":
            grounded_target(operation, target, snapshot)
            self._check_current(snapshot, deadline)
            self._note_tap(target.locator)
            # A coordinate tap on the observed, occlusion-filtered frame. No
            # XPath resolution means no index-based locator that can silently
            # resolve to a replacement cell after a reload.
            x, y = switch_point(target, snapshot.width)
            self._frame_mark(operation)
            self._tap(x * snapshot.width, y * snapshot.height, deadline)
            self._frame_dispatched()
        elif operation in {"TYPE", "TYPE_SUBMIT"}:
            grounded_target(operation, target, snapshot)
            return self._type_text(operation, target, snapshot, text, deadline)
        elif operation in {"SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT"}:
            aimed, reveal = None, False
            if target is not None and operation in {"SWIPE_LEFT", "SWIPE_RIGHT"}:
                grounded = any(target is element or target == element for element in snapshot.elements)
                if not grounded:
                    raise ValueError("Action target is not part of the supplied observation")
                aimed, reveal = target.rect, target.role == "Cell"
            x1, y1, x2, y2 = _swipe_points(operation, snapshot.width, snapshot.height, aimed, reveal)
            self._dispatching()
            glide = self._glide_ok(operation, snapshot)
            if glide and operation in WDA_GLIDE_SPAN:
                # A glide's time is its fixed segments (~480 ms), not its distance: the
                # longer span moves ~0.68 screens per swipe instead of ~0.47 for the same
                # cost, and still overlaps the previous screen by about a third.
                start, end = WDA_GLIDE_SPAN[operation]
                y1, y2 = snapshot.height * start, snapshot.height * end
            y1 = clear_stroke_start(snapshot, operation, x1, y1, snapshot.width, snapshot.height,
                                    fixed=glide and self.glide_everywhere)
            self._frame_mark(operation, glide=glide)
            self._swiped_at = time.monotonic()
            self.last_swipe_glided = bool(glide)
            if reveal:
                self._reveal(x1, y1, x2, y2, deadline)
            elif glide:
                self._glide(x1, y1, x2, y2, deadline)
            elif operation in {"SWIPE_LEFT", "SWIPE_RIGHT"}:
                # A flick: a paging view turns on speed, and XCTest's slow half-screen drag snapped
                # back (iOSWorld Weather's city pages: 3-7 SWIPE_LEFTs with no effect a run, 25 Sep;
                # live, the flick turned each page in 0.7 s against no turn in 1.4 s). Same distance
                # as before: a Mail row's Delete and Archive were revealed, not triggered.
                self._flick(x1, y1, x2, y2, deadline)
            else:
                self.call("POST", "/wda/dragfromtoforduration", {
                    "fromX": x1, "fromY": y1,
                    "toX": x2, "toY": y2, "duration": WDA_SWIPE_SECONDS}, deadline.remaining())
            self._frame_dispatched()
        else:
            raise ValueError("Unsupported WDA operation")

    # The frontier's options: Jev and the plain driver never call these.

    def is_simulator(self, timeout=3):
        """True when this WDA runs on a simulator, asked once per URL; no answer is False and is asked again."""
        known = WDA._simulators.get(self.url)
        if known is None:
            try:
                status = self.http.request("GET", "/status", timeout=timeout)
                ios = (status.get("value") or {}).get("ios") if isinstance(status, dict) else None
                known = bool(isinstance(ios, dict) and ios.get("simulatorVersion"))
            except (TransportError, TimeoutError, ValueError, AttributeError):
                return False
            WDA._simulators[self.url] = known
        return known

    def tune(self, rich=True, glide=True, max_children=WDA_MAX_CHILDREN, typing_frequency=None, timeout=5):
        """The frontier policy's driver: rich rows (state.from_wda_root), glides in every app, and WDA
        settings: a bound on a snapshot's children and, on a simulator, the measured typing rate
        (MOBSTER_TYPING_FREQUENCY sets one on any device). Returns the settings applied; they are
        applied again after a recovery."""
        self.rich_rows, self.glide_everywhere = bool(rich), bool(glide)
        settings = {}
        if max_children:
            settings["snapshotMaxChildren"] = int(max_children)
        rate = typing_frequency or _env_int("MOBSTER_TYPING_FREQUENCY")
        if not rate and WDA_SIMULATOR_TYPING_FREQUENCY and self.is_simulator():
            rate = WDA_SIMULATOR_TYPING_FREQUENCY
        if rate:
            settings["maxTypingFrequency"] = int(rate)
        self.paste_min_chars = max(WDA_PASTE_MIN_CHARS, round(WDA_PASTE_SECONDS * WDA_TYPED_PER_RATE * (rate or 0)))
        self.tuned_settings = settings
        if settings:
            self.call("POST", "/appium/settings", {"settings": settings}, timeout)
        return settings

    def _status_ok(self, timeout=3):
        try:
            status = self.http.request("GET", "/status", timeout=timeout)
        except (TransportError, TimeoutError, ValueError):
            self.http.close()
            return False
        value = status.get("value") if isinstance(status, dict) else None
        return isinstance(value, dict) and value.get("ready", True) is not False

    def recover(self, timeout=WDA_RECOVER_SECONDS):
        """Bring back a runner that reset or refused the connection and attach; TransportError past ``timeout``."""
        started = time.monotonic()
        deadline = Deadline(timeout)
        phases = self.last_recovery = {"restarted": False}
        if not self._status_ok(min(3, deadline.remaining())) and callable(self.restarter):
            self.restarter(deadline.remaining())
            phases["restarted"] = True
        while not self._status_ok(min(3, deadline.remaining())):
            if deadline.end - time.monotonic() < 1.5:
                raise TransportError("WDA did not come back")
            time.sleep(.5)
        phases["ready_ms"] = round((time.monotonic() - started) * 1000)
        self.reattach(timeout=min(30, deadline.remaining()))
        phases["total_ms"] = round((time.monotonic() - started) * 1000)
        return time.monotonic() - started

    def after_capture(self, timeout=WDA_RECOVER_SECONDS, sleep=time.sleep, clock=time.monotonic):
        """Bring the runner back after the phone's screen-capture device closed (see WDA_LINK_BLIP_SECONDS).
        Nothing is sent to the phone meanwhile, so no action is in doubt; TransportError past ``timeout``."""
        started = clock()
        restarted = wait_out_capture(self._status_ok, self.runner_restart, timeout, sleep=sleep, clock=clock)
        self.recover(timeout=max(1.0, timeout - (clock() - started)))
        self.last_recovery = {**(self.last_recovery or {}), "after_capture": True, "restarted": restarted}
        return clock() - started

    def reattach(self, timeout=30):
        """Drive the runner's current session (a new one when it has none), set up as configure() does."""
        for client in (self.http, self.preview_http):
            client.close()
        _settled_sessions.pop(self.url, None)
        factory = getattr(self, "new_session", None)
        session = factory() if callable(factory) else resolve_wda_session(self.url, timeout=min(10, timeout))
        if not session:
            raise TransportError("WDA has no session")
        self.prefix = "/session/" + quote(session, safe="")
        self._stable, self._settled, self._pane_visibility = None, {}, {}
        self._dispatched_at = float("-inf")
        self._hinted, self._frame_watch, self._frame_action, self._frame_followup = None, None, None, None
        self._pages_stale = True
        self.configure(timeout=min(10, timeout))
        if self.tuned_settings:
            self.call("POST", "/appium/settings", {"settings": dict(self.tuned_settings)}, min(5, timeout))
        return session

    def close(self):
        # The session belongs to the caller; do not terminate its app.
        clock, self.frame_clock = self.frame_clock, None
        try:
            if clock is not None:
                try:
                    clock.close()  # Releases the relay subscription (or its own stream).
                except Exception:
                    pass
            self.http.close()
        finally:
            self.preview_http.close()


# Driver kinds by name. The core registers WDA; mirroring.py registers "mirroring" (experimental) when imported, and an
# optional private package (harness_api.hooks) may register more before ``build_driver`` looks one up.
DRIVERS = {"wda": WDA}


def register_driver(kind, cls):
    """Add a driver kind. The class must subclass ``Driver``."""
    if not isinstance(kind, str) or not kind or not (isinstance(cls, type) and issubclass(cls, Driver)):
        raise TypeError("register_driver needs a name and a Driver subclass")
    DRIVERS[kind] = cls


def build_driver(kind, **kwargs):
    """One validated construction site: an unknown kind is a ValueError, a class missing the contract a TypeError."""
    if isinstance(kind, str) and kind not in DRIVERS:
        from . import harness_api
        harness_api.hooks()
    try:
        cls = DRIVERS[kind]
    except (KeyError, TypeError):
        raise ValueError(f"Unknown driver {kind!r}; expected one of {sorted(DRIVERS)}") from None
    driver = cls(**kwargs)
    if not isinstance(driver, Driver):
        raise TypeError(f"Driver {kind!r} does not implement the device contract")
    return driver

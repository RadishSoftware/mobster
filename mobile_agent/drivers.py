"""Device adapters: the driver contract and WebDriverAgent (WDA). Extensions may register more."""

import abc
import base64
import json
import math
import os
import threading
import re
import time
import unicodedata
from typing import Literal, TypedDict
from urllib.parse import quote

from .state import (HOST_KEY_OPERATIONS, WDA_HOST_KEY_NAMES, _parse_wda, from_wda_root, has_trait, pane_key,
                    truncated_field_value, validate_bundle_id, validate_input_text)
from .transport import Deadline, HTTP, TransportError

_SWIPE_X = .5
_SWIPE_Y = .5
_SWIPE_UP_Y = (.75, .25)
_SWIPE_DOWN_Y = (.25, .75)
_SWIPE_LEFT_X = (.75, .25)
_SWIPE_RIGHT_X = (.25, .75)
# LockedIn's reaction picker opened on a 1.5 s hold and not on 0.9 s (live, 25 Sep).
LONG_PRESS_MS = 1500
FLICK_MS = 120
# XCTest's drag, the fallback scroll (see WDA_GLIDE_SEGMENTS for the default).
# A W3C drag held still before lifting flings (measured twice, 2026-09-23).
# Not the faster velocity drag: at 6000 px/s (2.28 s vs ~2.4 s here) its
# momentum carried the Settings list past General and Accessibility, so every
# navigation task scrolled beyond its target. A scroll must move a bounded,
# predictable distance; this endpoint does.
WDA_SWIPE_SECONDS = .1
# Snapshot depth. 25 was chosen when a full-depth widget home screen took
# 40.5 s with WDA's `visible` attribute; without it the home screen reads in
# ~270 ms at depth 80. Depth 25 then blinded apps with deep view trees
# (Spotify: text recall 0.23 at 25 vs 0.77 at 50, +70 ms). 60 stays within the
# parser's 64-level nesting limit.
WDA_SNAPSHOT_MAX_DEPTH = 60
# Measured on the same phone (Settings, n=3 each): excluding `visible` and
# `accessible` cuts a source read from ~950 ms to ~120 ms. Visibility is then
# reconstructed geometrically by `state.wda_occlusion`.
WDA_SOURCE_PATH = "/source?format=xml&excluded_attributes=visible,accessible,index,traits"
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
# A snapshot this recent is the dispatch grounding; older ones are re-read and
# compared before a coordinate tap. The agent refreshes immediately before
# dispatch, so its taps never pay the extra read.
WDA_FRESH_SECONDS = 1.0
# How long WDA's answer about which stacked list pane is hidden is reused.
WDA_PANE_VISIBILITY_SECONDS = 20
# Which stacked tab page shows is reused this long at most (it is re-asked after a tab-bar tap).
WDA_PAGE_VISIBILITY_SECONDS = 120
# Requests after which the page shown may be another one.
# (Switching to an app and back keeps its tab: only a fresh launch or a URL opens another.)
PAGE_SWITCHING_PATHS = frozenset({"/wda/apps/launch", "/url"})
# Layers change with every presentation and dismissal: their answer is reused only briefly.
WDA_LAYER_VISIBILITY_SECONDS = 3
# A settled read taken this recently is reused as the next step's observation.
WDA_STABLE_REUSE_SECONDS = .5
# Settle polling issues reads back to back. A settle read is started only with this much deadline left: reads take
# 130-210 ms on device, and a read cut off by its deadline surfaced as a
# TransportError that ended two live runs right after a swipe.
WDA_READ_MARGIN_SECONDS = .35
# Screens proven at rest (two agreeing reads) remembered by content identity.
# After a tap, a read identical to one of them is at rest again, which saves
# the confirming read on revisits (Back, repeated tasks). Mid-transition frames
# carry both screens or shifted geometry and cannot match. Never used within
# this long after a swipe: a decelerating list can pass through a remembered
# offset without having stopped there.
WDA_SETTLED_MEMORY = 64
WDA_SWIPE_QUARANTINE_SECONDS = 3.0
# Upper bound on proving a screen settled; animated content never does.
WDA_SETTLE_CAP_SECONDS = 1.5
# Two agreeing reads are not enough on their own: iOS 26 Settings' General
# screen showed 24 elements for ~450 ms, identical across two reads, before
# loading to 29. A new screen must also stay unchanged this long.
WDA_SETTLE_QUIET_SECONDS = .5
WDA_TAP_HOLD_MS = 40
# /window/size resolves the active application, which can hang on a system overlay.
WDA_WINDOW_SIZE_SECONDS = 3
# Until the exact screen center is known: a point inside every iPhone screen
# (the smallest is 320x568 pt), below the banner area and away from the edges.
WDA_FALLBACK_DETECTION_POINT = "160,320"
# Focusing a field raises the keyboard within ~0.5 s on device.
WDA_KEYBOARD_WAIT_SECONDS = 2.0
# With a hardware keyboard known, the focusing tap's keyboard is read this long after the tap
# (not for WDA_KEYBOARD_WAIT_SECONDS / 2 of reads: 1-2 reads of ~1 s on a long simulator list).
WDA_PARKED_FOCUS_SECONDS = .25
# FrameClock settle (frame_clock.py): see WDA.wait_for_change.
# The pixel rule's "at rest": nothing moved for this long (frame_clock.STABLE_SECONDS).
WDA_FRAME_STABLE_SECONDS = .18
# With the pixels at rest, AX must still agree with itself this long (instead
# of WDA_SETTLE_QUIET_SECONDS). Measured: after a pop to the Settings root, the
# glass search bar keeps morphing by ~1 pt for ~0.5-0.8 s with no tile-level
# pixel change, and AX passes through states that last up to ~270 ms (two
# agreeing reads); accepting two agreeing reads alone was premature 1 in 20.
WDA_FRAME_AX_QUIET_SECONDS = .3
# wait_for_change(single_read=True): how long to wait for the action's first pixel change before
# settling from AX reads alone. The watch is marked before dispatch, so a tap's or a keystroke's
# effect has usually arrived by the time the dispatch returns.
WDA_ONE_READ_EFFECT_SECONDS = .35
# Scrolling primitive (measured 2026-09-23, iPhone 15 Pro, iOS 26, with
# FrameClock odometry and AX ground truth; FrameClock validation, 23 Sep 2026).
# XCTest's drag (WDA_SWIPE_SECONDS above) moves exactly 0.4875 screens but
# starts moving only 0.65-0.75 s (Settings) / 2.3 s (Safari) after the request
# and returns after 1.6-1.7 s / 3.2-3.3 s. A W3C drag starts sooner, but a
# `pause` before lifting emits no touch samples, so the lift keeps the last
# move's velocity and the list flings (0.5-1.5 extra screens, ~2 s of motion),
# as the earlier WebKit test saw. A "glide" fixes both: a slow start (so the
# pan begins at a precise point: fast starts lost 0.05-0.15 screens, varying
# run to run), a fast middle, and a decelerating end whose last samples carry
# almost no velocity. Measured: 0.4742 screens in 8/8 Settings swipes, 0.481 in
# 4/4 Safari swipes, no motion after release, and dispatch 1.06-1.24 s
# (Settings) / 2.08-2.14 s (Safari). Segments: (fraction of the distance, ms).
WDA_GLIDE_SEGMENTS = ((.04, 80), (.92, 200), (.99, 120), (1.0, 80))
# Where the glide was measured; MOBSTER_GLIDE_BUNDLES adds apps ("*" = all).
# With a FrameClock, motion after release (a fling or a paging snap) sends the
# app back to XCTest's drag for the rest of the process. MOBSTER_GLIDE=0 turns
# the glide off.
WDA_GLIDE_BUNDLES = frozenset({"com.apple.Preferences", "com.apple.mobilesafari"})
# Content that moved more than this (screen fraction) after the glide returned.
WDA_GLIDE_MOMENTUM = .12
# scroll_to glides are longer than a SWIPE: from 85 % to 15 % of the screen
# (up) or 20 % to 90 % (down; starting higher lands on the navigation bar and
# does not scroll). Measured in Settings: 0.6808 screens, 4/4 consistent; a
# glide loses ~0.02 screens to touch slop (0.5 span -> 0.4836). Several strokes
# in one W3C request do not help: they cost the same per stroke and only the
# first scrolled (measured). Only the start is used: each glide's length is
# WDA_SCROLL_DISTANCE (+ slop), cut to the remainder.
WDA_SCROLL_SPAN = {"SWIPE_UP": (.85, .15), "SWIPE_DOWN": (.2, .9)}
# SWIPE_UP/DOWN as a glide: scroll_to's span, starting a little higher (Safari's
# bottom address bar sits at ~0.88 and a stroke that starts on it does not scroll).
WDA_GLIDE_SPAN = {"SWIPE_UP": (.80, .12), "SWIPE_DOWN": (.2, .88)}
WDA_SCROLL_DISTANCE = .68
WDA_SCROLL_SLOP = .018
WDA_SCROLL_GLIDES = 12
WDA_SCROLL_ROUNDS = 3

# -- The frontier policy's options (WDA.tune). Jev and the plain driver keep every default above. --
# Its source read keeps the traits ("Selected"): no measurable cost on the simulator (90 ms with or
# without, 26 Sep). ``accessible`` stays out: it tripled the read (0.14 -> 0.35 s).
WDA_RICH_SOURCE_PATH = "/source?format=xml&excluded_attributes=visible,accessible,index"
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
# Longest text a write takes (a document body); typing keeps validate_input_text's limits.
WDA_WRITE_MAX_CHARS = 20000
# The press that opens the edit menu (Paste), and how long its menu is looked for.
WDA_PASTE_PRESS_MS = 600
WDA_PASTE_MENU_SECONDS = 3.0
# A value read from the source is complete below this length (state._wda_text cuts longer ones).
WDA_SOURCE_VALUE_LIMIT = 500
# Recovery: how long a restarted or re-supervised runner has to answer /status again.
WDA_RECOVER_SECONDS = 90
# Settle reads are spaced at least this far apart (start to start). Back-to-back reads every 80-90 ms during
# settle ("Find the Application 'com.apple.Preferences'" bursts) preceded runner restarts on the phone
# (quality baseline B4, 5 Oct); a read takes 130-210 ms there, so this mostly spaces the fast ones.
WDA_SETTLE_POLL_SECONDS = .25
# Screenshot timeouts (a /screenshot that times out, or the MJPEG stream stalling this long) after which the
# driver settles from AX reads alone for the next AX_ONLY_STEPS steps: XCTest screenshots block WDA's main
# queue in GPU-heavy apps (Maps, Spotify: "Cannot take a screenshot within 20000 ms", B4).
WDA_SCREENSHOT_TIMEOUTS_FOR_AX_ONLY = 2
WDA_AX_ONLY_STEPS = 5

# Smart punctuation a keyboard may type for plain ASCII (a typed read-back is compared without it).
_TYPOGRAPHIC = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "\u00a0": " "})


def _env_int(name):
    """A positive integer from the environment, else None."""
    try:
        value = int(os.environ.get(name, "") or 0)
    except ValueError:
        return None
    return value if value > 0 else None


class ActivationDiagnostics(TypedDict, total=False):
    activation_route: Literal["table_selection", "control_primary_action", "control_primary_event",
                              "control_touch_up_inside", "accessibility", "physical_tap",
                              "navigation_pop"]
    dispatch_attempted: bool


def activation_diagnostics(payload: object) -> ActivationDiagnostics:
    """Bounded telemetry only. Missing diagnostics never mean no dispatch or safe retry."""
    result: ActivationDiagnostics = {}
    if not isinstance(payload, dict):
        return result
    route = payload.get("activation_route")
    if isinstance(route, str) and route in {"table_selection", "control_primary_action", "control_primary_event",
                                          "control_touch_up_inside", "accessibility", "physical_tap",
                                          "navigation_pop"}:
        result["activation_route"] = route
    attempted = payload.get("dispatch_attempted")
    if type(attempted) is bool:
        result["dispatch_attempted"] = attempted
    return result


class DriverRejection(TransportError):
    """A driver's typed refusal. Known codes only; never arbitrary app text or relay output."""
    CODES = frozenset({"app_not_active", "app_preview_unavailable", "app_preview_uniform",
        "app_preview_oversized", "incomplete_accessibility_tree", "stale_revision", "target_occluded",
        "unsupported_or_stale_target", "selection_changed", "invalid_text", "text_target_changed",
        "main_thread_busy", "native_exception_outcome_unknown", "main_thread_deadline_expired_outcome_unknown",
        "request_deadline_expired", "request_cancelled", "activation_declined", "response_serialization_failed",
        "bridge_update_required", "physical_tap_not_authorized", "physical_tap_outcome_unknown",
        "home_outcome_unknown", "volup_outcome_unknown", "voldown_outcome_unknown",
        "app_launch_outcome_unknown"})

    # Codes the bridge returns BEFORE its documented dispatch boundary, where no
    # app code has run. Recovering from these is not a replay: the agent observes
    # again and decides again. Every other code -- especially the *_outcome_unknown
    # family -- stays ambiguous and terminal, because an action that may have
    # executed must never be repeated automatically.
    PRE_DISPATCH = frozenset({"stale_revision", "unsupported_or_stale_target",
                              "target_occluded", "incomplete_accessibility_tree"})

    def __init__(self, code, diagnostics=None):
        self.code = code if isinstance(code, str) and code in self.CODES else "native_request_rejected"
        self.diagnostics = activation_diagnostics(diagnostics)
        super().__init__("Driver refused: " + self.code + "; no automatic retry")


def finite_positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def grounded_target(operation, target, snapshot):
    """Adapters are safety boundaries too, even when invoked without the agent loop."""
    if target is None or not any(target == element for element in snapshot.elements):
        raise ValueError("Action target is not part of the supplied observation")
    if has_trait(snapshot, "exact_actions") and operation not in target.actions:
        raise ValueError("Target does not advertise this action")
    if operation in {"TYPE", "TYPE_SUBMIT"} and not target.editable:
        raise ValueError("TYPE requires an editable target")


# A vertical stroke that starts on one of these drags the control instead of the page:
# Display & Brightness's slider sits at y 0.77-0.81, under the glide's 0.80 start, and
# the page never scrolled (MobsterBench pass 6, state.auto_lock) -- a drag there can
# also move the setting itself.
DRAG_SENSITIVE_ROLES = frozenset({"Slider", "Switch", "Stepper", "Picker", "PickerWheel", "DatePicker",
                                  "SegmentedControl"})
SWIPE_START_STEP = .01
SCROLLING_MARKERS = ("XCUIElementTypeScrollView[", "XCUIElementTypeTable[", "XCUIElementTypeCollectionView[")
SWIPE_START_LIMIT = .3  # the farthest a start moves (in screen heights) before giving up


def clear_stroke_start(snapshot, operation, x, y, width, height, fixed=False):
    """``y`` moved along the stroke until its touch-down point misses every drag-sensitive control.
    ``fixed`` (glides in every app, WDA.tune): also every control outside the scrolling content, a
    floating button over the list: SplitPay's Pay/Request button sits under the glide's start (y .80),
    and every glide there pressed it instead of scrolling (F2, 26 Sep)."""
    if operation not in {"SWIPE_UP", "SWIPE_DOWN"} or snapshot is None or not width or not height:
        return y
    controls = [e.rect for e in getattr(snapshot, "elements", ()) if e.role in DRAG_SENSITIVE_ROLES or fixed and (
        e.role in ("Button", "Other", "TextField", "SearchField") and e.rect[2] * e.rect[3] < .25
        and not any(marker in e.locator for marker in SCROLLING_MARKERS))]
    if not controls:
        return y
    nx, ny = x / width, y / height
    step = -SWIPE_START_STEP if operation == "SWIPE_UP" else SWIPE_START_STEP
    moved = 0.0
    while any(rx <= nx <= rx + rw and ry - .01 <= ny + moved <= ry + rh + .01 for rx, ry, rw, rh in controls):
        moved += step
        if abs(moved) > SWIPE_START_LIMIT:
            return y
    return (ny + moved) * height


# An iOS switch's knob sits at the right end of its row, about this far in from the edge.
SWITCH_KNOB_INSET_PT = 30


def switch_point(target, screen_width_pt):
    """Where to tap ``target`` (screen fractions): its centre (or its content, state.content_hits),
    or for a row-wide switch its knob.

    WDA reports a switch as its whole row (label and knob), and a tap on the label half does
    not toggle it: iOSWorld multi-006 (24 Sep) tapped "Outdoor Seating" eight times and it
    stayed off. The knob is at the row's right end.
    """
    x, y = getattr(target, "tap_point", target.center)
    left, _, width, _ = target.rect
    if target.role in ("Switch", "Toggle") and width * screen_width_pt > 3 * SWITCH_KNOB_INSET_PT:
        x = left + width - SWITCH_KNOB_INSET_PT / screen_width_pt
    return x, y


def _swipe_points(operation, width, height, target_rect=None):
    """The stroke's (x1, y1, x2, y2) in points. A sideways swipe with ``target_rect`` (screen fractions) strokes
    through that row: at its centre Y, from 75 % to 25 % of its width (or back). Without one it crosses the
    middle of the screen, where a row's swipe actions would open on whichever row is there (alarm-delete swiped
    the owner's 5:55 AM alarm open 15 times while aiming at another, B7)."""
    if operation in {"SWIPE_UP", "SWIPE_DOWN"}:
        start, end = _SWIPE_UP_Y if operation == "SWIPE_UP" else _SWIPE_DOWN_Y
        x = width * _SWIPE_X
        return x, height * start, x, height * end
    if operation not in {"SWIPE_LEFT", "SWIPE_RIGHT"}:
        raise ValueError(f"Unsupported swipe {operation!r}")
    start, end = _SWIPE_LEFT_X if operation == "SWIPE_LEFT" else _SWIPE_RIGHT_X
    if target_rect is not None:
        left, top, w, h = target_rect
        left, right = max(0.0, left), min(1.0, left + w)
        y = height * min(max(top + h / 2, 0.0), 1.0)
        return (width * (left + (right - left) * start), y, width * (left + (right - left) * end), y)
    y = height * _SWIPE_Y
    return width * start, y, width * end, y


class Driver(abc.ABC):
    """Required device contract. Instantiation fails when a method is missing.

    ``can_type`` advertises text entry honestly: a driver that cannot type must
    say ``False`` rather than offer a clipboard write as input. ``last_image``
    carries the most recent operator preview frame when the driver captures one.

    Optional capabilities stay duck-typed and are always probed with ``getattr``
    at the call site, because each has a load-bearing fallback:

    * ``observe_ready(timeout)`` -- settled observation; falls back to ``observe``.
    * ``wait_for_change(snapshot, timeout, wait_seconds)`` -- native wait; falls
      back to a plain ``observe`` with the full remaining budget.
    * ``capture_preview(timeout)`` -- operator preview frame; absence selects a
      different capture route, so no default may exist.
    """

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


W3C_ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"


def call_kind(method, path):
    """A WDA request's kind for timing reports: "GET source", "POST element/*/value"."""
    parts = path.split("?", 1)[0].strip("/").split("/")
    if len(parts) > 1 and parts[0] == "element" and parts[1] != "active":
        parts[1] = "*"
    return method + " " + "/".join(parts)


def _element_id(element):
    """A WDA element reference's id (JSONWP or W3C key), or None."""
    identifier = element.get("ELEMENT") or element.get(W3C_ELEMENT_KEY)
    return identifier if isinstance(identifier, str) and identifier else None


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


class WDA(Driver):
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
    # Apps where a glide kept moving after release, per WDA URL.
    _momentum_apps = {}
    # WDA URLs whose keyboard stayed parked after a focusing tap (hardware-keyboard mode): no
    # software keyboard will animate in, so waiting for one is skipped. A visible one clears it.
    _parked_keyboards = set()
    # Optional FrameClock (frame_clock.attach_frame_clock) and its mode:
    # "shadow" computes and logs pixel-settle decisions without using them.
    frame_clock = None
    frame_clock_mode = "off"
    # Source reads and their seconds, for callers' timing reports.
    source_reads, source_seconds = 0, 0.0
    # A list to collect every WDA request and read as (kind, started monotonic, ms): the bench's
    # per-action cost report. None (the default) records nothing.
    call_trace = None
    # wait_for_change accepts single_read; settled_by says how the last settle ended ("one_read":
    # a single read, not yet proven by a second one; "agreeing_reads": two agreeing reads, before the
    # quiet period that proves them).
    settles_on_one_read = True
    settles_early = True
    settled_by = None
    # WDA.tune (the frontier): rich rows (Other leaves, disabled and selected controls), glides in
    # every app (FrameClock demotes one that keeps moving), and the settings below.
    rich_rows = False
    glide_everywhere = False
    tuned_settings = None
    paste_min_chars = WDA_PASTE_MIN_CHARS
    last_swipe_glided = False
    # A callable that restarts this WDA runner (a simulator's xcodebuild; see bench/sim_wda.py).
    # None: recover() waits for a supervisor to bring it back (the USB runner is supervised).
    restarter = None
    last_recovery = None
    # Simulators by WDA URL (None: not asked yet).
    _simulators = {}
    # Steps left of AX-only settling after repeated screenshot timeouts (see note_screenshot_timeout).
    ax_only_steps = 0
    screenshot_timeouts = 0

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
        self._swiped_at = float("-inf")
        self._detection_point_pending = False
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

    def call(self, method, path, body=None, timeout=10):
        if method == "POST" and path in PAGE_SWITCHING_PATHS:
            self._pages_stale = True  # another app or screen: which tab page shows is asked again
        started = time.monotonic()
        try:
            result = self.http.request(method, self.prefix + path, body, timeout)
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

    def _hint_app(self, bundle, timeout):
        self.call("POST", "/appium/settings", {"settings": {"defaultActiveApplication": bundle}}, timeout)
        self._hinted = bundle

    @staticmethod
    def response_value(result):
        value = result.get("value")
        if (isinstance(value, dict) and value.get("error")) or result.get("status", 0) != 0:
            raise TransportError("WDA rejected command; not retried")
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

    def observe(self, timeout=10):
        started = time.monotonic()
        try:
            result = self._observe(timeout)
            result.read_started = started
            return result
        finally:
            self.source_reads += 1
            self.source_seconds += time.monotonic() - started
            if self.call_trace is not None:
                self.call_trace.append(("observe", started, (time.monotonic() - started) * 1000))

    def _observe(self, timeout):
        deadline = Deadline(timeout)
        source = WDA_RICH_SOURCE_PATH if self.rich_rows else WDA_SOURCE_PATH
        try:
            xml = self.call("GET", source, timeout=deadline.remaining())
        except TransportError:
            # Measured 2026-09-23 (iOS 26.0.1): with Safari in front, WDA resolved
            # the "active application" to an invisible system overlay
            # (com.apple.siri.IntelligentLight) and every source read failed
            # after ~8 s ("stale element", kAXErrorServerNotFound). Naming the
            # app we brought forward as WDA's default active application
            # read the same screen in 0.58 s. A read changes nothing, so one
            # retry is safe. The hint then stays for this session: while it is
            # set WDA prefers that app when several are active.
            hint = self._foreground_hint or self._last_bundle
            if not hint or deadline.remaining() < WDA_READ_MARGIN_SECONDS:
                raise
            # Re-applied on every failed read: another client of the same session (a
            # harness probe, the app's video) can put detection back on "auto".
            self._hint_app(hint, min(2, deadline.remaining()))
            xml = self.call("GET", source, timeout=deadline.remaining())
            WDA._overlay_apps.setdefault(self.url, set()).add(hint)
            self._save_overlay_hints()
        if not isinstance(xml, str):
            raise ValueError("WDA did not return XML")
        root = _parse_wda(xml)
        rich = self.rich_rows
        result = from_wda_root(root, rich=rich)
        if result.stacked_panes or result.stacked_layers or result.stacked_pages:
            hidden = self._hidden_panes(result.stacked_panes, deadline) if result.stacked_panes else frozenset()
            layers = self._hidden_layers(result.stacked_layers, deadline) if result.stacked_layers else frozenset()
            if result.stacked_pages:
                layers |= self._hidden_pages(result.stacked_pages, deadline, result.bundle_id)
            if hidden or layers:
                result = from_wda_root(root, hidden, layers, rich=rich)  # the same tree, not parsed again
        self._last_bundle = result.bundle_id or self._last_bundle
        if getattr(self, "_detection_point_pending", False):
            # configure() could not read the window size; the app frame is the screen.
            try:
                self._set_detection_point(result.width, result.height, min(2, deadline.remaining()))
            except (TransportError, TimeoutError, ValueError):
                pass  # Best effort; the next observation tries again.
        deadline.remaining()
        if self._frame_followup is not None:
            self._frame_follow_up(result)
        return result

    def _hidden_panes(self, stacked, deadline):
        """Which of the stacked list panes WDA reports not visible (a fast read omits it).

        One query per pane role plus two reads per hidden pane (~0.5 s), then remembered
        while the same panes are on screen. Any failure leaves the order rule in charge.
        """
        key = frozenset(stacked)
        cached = self._pane_visibility.get(key)
        if cached is not None and time.monotonic() - cached[1] < WDA_PANE_VISIBILITY_SECONDS:
            return cached[0]
        hidden = set()
        try:
            for role in sorted({pane[0] for pane in stacked}):
                found = self.call("POST", "/elements", {"using": "predicate string", "value":
                                  f"type == 'XCUIElementType{role}' AND visible == 0"},
                                  min(3, deadline.remaining()))
                for element in (found if isinstance(found, list) else [])[:4]:
                    identifier = _element_id(element)
                    if identifier is None:
                        continue
                    path = f"/element/{quote(identifier, safe='')}"
                    rect = self.call("GET", path + "/rect", timeout=min(3, deadline.remaining()))
                    label = self.call("GET", path + "/attribute/label", timeout=min(3, deadline.remaining()))
                    if isinstance(rect, dict):
                        pane = pane_key(role, label if isinstance(label, str) else "", *(
                            float(rect.get(k, 0)) for k in ("x", "y", "width", "height")))
                        if pane in key:
                            hidden.add(pane)
        except (TransportError, TypeError, ValueError):
            return frozenset()
        hidden = frozenset(hidden) if len(hidden) < len(key) else frozenset()  # never hide every pane
        return self._remember_visibility(key, hidden)

    def _hidden_layers(self, paths, deadline):
        """Which window layers behind the front one WDA reports not visible (two reads each, ~0.3 s).

        The source path is WDA's class chain below the application. Remembered briefly per
        set of layers; any failure hides nothing (the order rules then apply as before).
        """
        key = ("layers", frozenset(paths))
        cached = self._pane_visibility.get(key)
        if cached is not None and time.monotonic() - cached[1] < WDA_LAYER_VISIBILITY_SECONDS:
            return cached[0]
        hidden = set()
        try:
            for path in paths:
                chain = "/".join(path.strip("/").split("/")[1:])
                found = self.call("POST", "/elements", {"using": "class chain", "value": chain},
                                  min(3, deadline.remaining()))
                if not isinstance(found, list) or len(found) != 1:
                    continue
                identifier = _element_id(found[0])
                if identifier is None:
                    return frozenset()  # as when WDA refused the request for a missing id
                visible = self.call("GET", f"/element/{quote(identifier, safe='')}/attribute/visible",
                                    timeout=min(3, deadline.remaining()))
                if visible is False or visible == "false":
                    hidden.add(path)
        except (TransportError, TypeError, ValueError):
            return frozenset()
        return self._remember_visibility(key, frozenset(hidden))

    def _hidden_pages(self, groups, deadline, bundle=None):
        """Source paths of same-frame sibling pages WDA reports not visible: one query per parent for
        its visible children, and their positions (SwiftUI keeps every TabView tab in the tree:
        QuickChat listed five tabs' controls at once, and 16 turns of multi-087 tapped a row the
        keyboard covered on the tab shown, 24 Sep). Reused until the next dispatch; any failure
        hides nothing."""
        key = ("pages", bundle, frozenset(groups))
        cached = self._pane_visibility.get(key)
        # The question costs ~0.8 s a page on QuickChat (5 s a read when asked after every action: settles
        # went 0.75 -> 1.9 s a turn, 25 Sep), and only a tap outside the pages (the tab bar), an app switch
        # or HOME changes the answer: it is kept until one of those, or WDA_PAGE_VISIBILITY_SECONDS.
        if (cached is not None and not getattr(self, "_pages_stale", False)
                and time.monotonic() - cached[1] < WDA_PAGE_VISIBILITY_SECONDS):
            return cached[0]
        self._pages_stale = False
        self._page_groups = tuple(groups)
        hidden = set()
        try:
            for parent, children in groups:
                chain = "/".join(parent.strip("/").split("/")[1:])
                found = self.call("POST", "/elements", {"using": "class chain", "value": (chain + "/" if chain else "")
                                                        + "XCUIElementTypeOther[`visible == 1`]"}, min(3, deadline.remaining()))
                shown = set()
                for element in (found if isinstance(found, list) else [])[:8]:
                    identifier = _element_id(element)
                    index = identifier and self.call("GET", f"/element/{quote(identifier, safe='')}/attribute/index",
                                                     timeout=min(3, deadline.remaining()))
                    if isinstance(index, (int, str)) and str(index).isdigit():
                        shown.add(int(index))
                positions = {position for position, _ in children}
                if shown & positions:  # else unknown: hide nothing
                    hidden.update(path for position, path in children if position not in shown)
        except (TransportError, TypeError, ValueError):
            return frozenset()
        return self._remember_visibility(key, frozenset(hidden))

    def _note_tap(self, locator):
        """A tap outside every stacked page (a tab bar, chrome) may switch the page shown."""
        groups = getattr(self, "_page_groups", ())
        if not groups:
            return
        if not locator or any(locator.startswith(parent + "/") and not any(
                locator == page or locator.startswith(page + "/") for _, page in pages)
                for parent, pages in groups):
            self._pages_stale = True

    def _remember_visibility(self, key, hidden):
        """Cache a pane or layer visibility answer; panes and layers share one 16-entry bound."""
        self._pane_visibility[key] = (hidden, time.monotonic())
        while len(self._pane_visibility) > 16:
            self._pane_visibility.pop(next(iter(self._pane_visibility)))
        return hidden

    def _prove_settled(self, snapshot):
        fingerprint = snapshot.content_fingerprint
        self._settled.pop(fingerprint, None)
        self._settled[fingerprint] = True
        while len(self._settled) > WDA_SETTLED_MEMORY:
            self._settled.pop(next(iter(self._settled)))
        self._stable = snapshot
        return snapshot

    def _known_at_rest(self, snapshot):
        return (time.monotonic() - self._swiped_at > WDA_SWIPE_QUARANTINE_SECONDS
                and snapshot.content_fingerprint in self._settled)

    def observe_ready(self, timeout=10):
        """A settled observation: reuse the read that just proved stability, else
        read until two consecutive reads agree (or one matches a screen already
        proven at rest). A screen that never stops moving (a running timer,
        video) returns its latest read after the settle cap."""
        stable = self._stable
        if stable is not None and time.monotonic() - stable.captured_at <= WDA_STABLE_REUSE_SECONDS:
            return stable
        deadline = Deadline(timeout)
        cap = time.monotonic() + min(WDA_SETTLE_CAP_SECONDS, timeout)
        since = time.monotonic()
        previous = self.observe(timeout=deadline.remaining())
        if self._known_at_rest(previous):
            return self._prove_settled(previous)
        clock = self._frame_clock("on")
        previous_started = since
        while time.monotonic() < cap:
            self._poll_pause(previous_started, cap)
            started = time.monotonic()
            current = self.observe(timeout=deadline.remaining())
            if current.content_fingerprint != previous.content_fingerprint:
                since = started
            elif time.monotonic() - since >= WDA_SETTLE_QUIET_SECONDS:
                return self._prove_settled(current)
            elif (clock is not None and current.elements
                    and time.monotonic() - since >= WDA_FRAME_AX_QUIET_SECONDS):
                # Agreeing reads for WDA_FRAME_AX_QUIET_SECONDS, and the whole screen
                # (status bar aside, no masked regions) pixel-still from
                # WDA_FRAME_STABLE_SECONDS before the last two reads until now.
                still = clock.still_for(exact=False)  # AX agreement (above) covers sub-tile changes
                if still is not None and time.monotonic() - still <= previous_started - WDA_FRAME_STABLE_SECONDS:
                    return self._prove_settled(current)
            previous, previous_started = current, started
        return previous

    @staticmethod
    def _poll_pause(last_read, cap):
        """Wait until WDA_SETTLE_POLL_SECONDS after the last settle read began (never past ``cap``)."""
        pause = min(last_read + WDA_SETTLE_POLL_SECONDS, cap) - time.monotonic()
        if pause > 0:
            time.sleep(pause)

    def wait_for_change(self, snapshot, timeout=2, wait_seconds=.6, on_settling=None, single_read=False,
                        early=False):
        """Read back to back until the screen differs from ``snapshot`` and is at
        rest: two consecutive agreeing reads, or one read identical to a screen
        already proven at rest. A mid-transition frame is never returned while it
        is still moving. Unchanged after ``wait_seconds`` returns the unchanged
        read; changing past the settle cap returns the latest read.

        ``on_settling(read)`` is called once per new screen the first time two
        consecutive reads agree, before its quiet period has elapsed: the
        caller may start work for that screen while it is being proven. It
        never influences what this method returns.

        With a FrameClock in "on" mode, the pixels shorten the quiet period:
        a changed read that agrees with the reads of the last
        WDA_FRAME_AX_QUIET_SECONDS (0.3 s instead of 0.5 s) is accepted when,
        after a pixel change was seen, no tile moved from
        WDA_FRAME_STABLE_SECONDS before it until it returned. AX is never
        skipped: after a push, Settings briefly re-exposed the previous
        screen's search field in AX with nothing moving on screen, and after a
        pop the glass search bar's 1 pt morph changes AX for ~0.5-0.8 s below
        tile resolution (both measured). Every other path is the AX loop
        above, unchanged. In "shadow" mode the AX loop decides and the pixel
        decision is logged.

        ``single_read`` (FrameClock "on"): wait for the pixels to change and
        come to rest, then read once; a changed read during which nothing moved
        is returned without a second, agreeing read (``settled_by`` becomes
        "one_read"). On a simulator a read of a long list takes 0.7-1.7 s, and
        the agreeing read costs as much again. The caller proves the read later
        (frontier: a second read while the model thinks). Anything else falls
        back to the loop above.

        ``early``: the AX loop returns the first changed read that agrees with
        the read before it, without waiting out the quiet period
        (``settled_by`` "agreeing_reads"). The caller proves it later, as for
        ``single_read``: the frontier starts its next model call on it and
        reads again meanwhile (0.5 s a turn when the pixels cannot settle it).
        """
        watch, self._frame_watch = self._frame_watch, None
        action, self._frame_action = self._frame_action, None
        clock = self._frame_clock()
        reported = set()

        def settling(read):
            if on_settling is None or read.content_fingerprint in reported:
                return
            reported.add(read.content_fingerprint)
            try:
                on_settling(read)
            except Exception:
                pass  # Speculation is optional; settling is not.

        deadline = Deadline(timeout)
        started = time.monotonic()
        cap = started + min(WDA_SETTLE_CAP_SECONDS, timeout)
        reads = []
        info = None
        self.settled_by = None
        try:
            accept = None
            if clock is not None and watch is not None and self.frame_clock_mode == "on" and watch.usable:
                info = {}
                read = self._one_read_settle(snapshot, clock, watch, deadline, cap, reads) if single_read else None
                if read is not None:
                    info["accepted_by"], self.settled_by = "one_read", "one_read"
                    self._frame_report(clock, watch, action, snapshot, read, started, reads, info)
                    return read

                def accept(since, read_started, read_ended):
                    # A changed read agreeing with the read before it, unchanged since ``since``.
                    if (read_ended - since >= WDA_FRAME_AX_QUIET_SECONDS
                            and clock.quiet_between(watch, read_started - WDA_FRAME_STABLE_SECONDS, read_ended)):
                        info["accepted_by"] = "pixels"
                        return True
                    return False
            elif clock is not None and watch is not None and self.frame_clock_mode == "on":
                info = {"fallback": watch.reason or "unusable"}
            result = self._ax_settle(snapshot, deadline, started, started + wait_seconds, cap, settling, reads,
                                     accept, early=early)
            if clock is not None and watch is not None:
                self._frame_report(clock, watch, action, snapshot, result, started, reads, info)
            return result
        finally:
            if clock is not None and watch is not None:
                clock.release(watch)

    def _one_read_settle(self, snapshot, clock, watch, deadline, cap, reads):
        """A changed read taken after the pixels moved and came to rest, during which nothing
        moved; None when the pixels never moved, never rested, or the read did not change."""
        if clock.wait_effect(watch, max(0.0, min(WDA_ONE_READ_EFFECT_SECONDS, cap - time.monotonic()))) is None:
            return None
        if clock.wait_stable(watch, max(0.0, cap - time.monotonic())) is None:
            return None
        read_started = time.monotonic()
        try:
            current = self.observe(timeout=deadline.remaining())
        except (TransportError, TimeoutError):
            return None
        now = time.monotonic()
        reads.append((read_started, now, current.content_fingerprint))
        if (current.content_fingerprint != snapshot.content_fingerprint
                and clock.quiet_between(watch, read_started - WDA_FRAME_STABLE_SECONDS, now)):
            return current
        return None

    def _ax_settle(self, snapshot, deadline, started, quiet_end, cap, settling, reads, accept=None, early=False):
        """The AX-only settle loop (see wait_for_change). ``reads`` collects
        (start, end, content fingerprint) per read for the FrameClock log.
        ``accept(since, read_started, read_ended)``, when given, may also prove a
        changed read at rest (unchanged since ``since``, and the pixels were still
        before and during it)."""
        before = snapshot.content_fingerprint
        previous, since = None, started
        current = snapshot
        last_read = None
        while True:
            if last_read is not None:
                self._poll_pause(last_read, cap)
            read_started = last_read = time.monotonic()
            try:
                current = self.observe(timeout=deadline.remaining())
            except (TransportError, TimeoutError):
                # A read changes nothing on the phone; the action's outcome is
                # decided by the caller from the last good observation.
                if current is snapshot:
                    raise
                return current
            fingerprint = current.content_fingerprint
            now = time.monotonic()
            reads.append((read_started, now, fingerprint))
            if fingerprint != before:
                if previous is None or previous.content_fingerprint != fingerprint:
                    since = read_started
                if (accept is not None and previous is not None and previous.content_fingerprint == fingerprint
                        and accept(since, read_started, now)):
                    return self._prove_settled(current)
                if early and previous is not None and previous.content_fingerprint == fingerprint \
                        and not self._known_at_rest(current):
                    self.settled_by = "agreeing_reads"  # unproven: the caller reads again
                    return current
                if (self._known_at_rest(current) or previous is not None
                        and previous.content_fingerprint == fingerprint
                        and now - since >= WDA_SETTLE_QUIET_SECONDS):
                    return self._prove_settled(current)
                if previous is not None and previous.content_fingerprint == fingerprint:
                    settling(current)
                previous = current
            else:
                previous = None
                if now >= quiet_end:
                    return current
            if now >= cap or deadline.end - now <= WDA_READ_MARGIN_SECONDS:
                return current

    # -- FrameClock ----------------------------------------------------------

    def _frame_clock(self, mode=None):
        """The attached FrameClock when active (in ``mode``, if given), else None."""
        clock = self.frame_clock
        if clock is None or self.frame_clock_mode not in ("on", "shadow") or self.ax_only_steps > 0:
            return None
        if mode is not None and self.frame_clock_mode != mode:
            return None
        return clock

    def note_screenshot_timeout(self):
        """A screenshot timed out (or the stream stalled). The second in a row switches settling to AX reads
        alone, and the model's screenshot to the free stream frame only, for the next WDA_AX_ONLY_STEPS steps.
        Returns True when this one switched it."""
        self.screenshot_timeouts += 1
        if self.screenshot_timeouts >= WDA_SCREENSHOT_TIMEOUTS_FOR_AX_ONLY:
            self.screenshot_timeouts = 0
            started = self.ax_only_steps == 0
            self.ax_only_steps = WDA_AX_ONLY_STEPS
            return started
        return False

    def next_step(self):
        """A new agent step: counts down AX-only settling, and counts stream stalls the FrameClock saw since the
        last step as screenshot timeouts. Returns the AX-only steps left."""
        clock = self.frame_clock
        stalls = getattr(clock, "stalls", 0) if clock is not None else 0
        seen = getattr(self, "_stalls_seen", 0)
        if isinstance(stalls, int) and stalls > seen:
            self._stalls_seen = stalls
            for _ in range(stalls - seen):
                self.note_screenshot_timeout()
        elif self.ax_only_steps > 0:
            self.ax_only_steps -= 1
        return self.ax_only_steps

    def _frame_mark(self, operation, **extra):
        """Remember the pre-dispatch frame; called immediately before dispatch."""
        clock = self._frame_clock()
        if clock is None:
            return
        if self._frame_watch is not None:
            clock.release(self._frame_watch)
        try:
            self._frame_watch = clock.mark()
        except Exception:
            self._frame_watch = self._frame_action = None
            return
        self._frame_action = {"op": operation, "dispatch_started": time.monotonic(), **extra}

    def _frame_dispatched(self):
        if self._frame_action is not None:
            self._frame_action["dispatch_ended"] = time.monotonic()
            if self._frame_watch is not None and self.frame_clock is not None:
                try:
                    self.frame_clock.set_release(self._frame_watch, self._frame_action["dispatch_ended"])
                except Exception:
                    pass

    def _frame_report(self, clock, watch, action, snapshot, result, started, reads, info):
        """Log one settle: what happened, and what the pixel rule would have done."""
        try:
            before = snapshot.content_fingerprint
            ended = time.monotonic()
            entry = {"kind": "settle", "mode": self.frame_clock_mode,
                     "op": (action or {}).get("op"), "bundle": snapshot.bundle_id,
                     "settle_ms": round((ended - started) * 1000, 1), "reads": len(reads),
                     "changed": result.content_fingerprint != before,
                     "usable": watch.usable, "reason": watch.reason or None,
                     "fps": clock.stats().get("fps")}
            if action and action.get("dispatch_ended") is not None:
                entry["dispatch_ms"] = round((action["dispatch_ended"] - action["dispatch_started"]) * 1000, 1)
            if watch.effect is not None and action:
                entry["effect_after_dispatch_start_ms"] = round((watch.effect.t - action["dispatch_started"]) * 1000, 1)
                entry["effect_after_dispatch_end_ms"] = round((watch.effect.t - started) * 1000, 1)
                entry["effect_tiles"] = watch.effect_tiles
            if watch.stable_at is not None:
                entry["stable_ms"] = round((watch.stable_at - started) * 1000, 1)
            if info is not None and info.get("accepted_by"):
                entry["path"] = "one_read" if info["accepted_by"] == "one_read" else "frame"
            elif info is not None and info.get("fallback"):
                entry["path"], entry["fallback"] = "frame_fallback", info["fallback"]
            else:
                entry["path"] = "ax"
            fc_fingerprint = result.content_fingerprint if entry["path"] in ("frame", "one_read") else None
            if self.frame_clock_mode == "shadow" and watch.usable and watch.effect is not None:
                # The read the "on" rule would have accepted, on these same reads:
                # changed, agreeing with the read before it, and pixel-still from
                # STABLE_SECONDS before it began until it returned.
                run_since = None
                for index, (read_start, read_end, fingerprint) in enumerate(reads):
                    if index == 0 or reads[index - 1][2] != fingerprint:
                        run_since = read_start
                    if fingerprint == before or index == 0 or reads[index - 1][2] != fingerprint:
                        continue
                    if read_end - run_since < WDA_FRAME_AX_QUIET_SECONDS:
                        continue
                    if clock.quiet_between(watch, read_start - WDA_FRAME_STABLE_SECONDS, read_end):
                        fc_fingerprint = fingerprint
                        entry["fc_ms"] = round((read_end - started) * 1000, 1)
                        entry["premature"] = fingerprint != result.content_fingerprint
                        break
            last_motion = clock.last_motion(watch)
            if action and last_motion is not None and action.get("dispatch_ended") is not None:
                entry["motion_after_dispatch_ms"] = round((last_motion - action["dispatch_ended"]) * 1000, 1)
            odometry = None
            if action and str(action.get("op", "")).startswith("SWIPE_"):
                entry["glide"] = bool(action.get("glide"))
                latest = clock.latest_frame()
                if latest is not None and watch.ref is not None:
                    odometry = (watch.ref, getattr(watch, "release_frame", None), latest,
                                "y" if action["op"] in {"SWIPE_UP", "SWIPE_DOWN"} else "x")
            if os.environ.get("MOBSTER_FRAME_CLOCK_TRACE"):
                # Offline threshold tuning: every frame's motion flag and every read.
                entry["trace"] = {
                    "moves": [[round((t - started) * 1000), int(moved)] for t, moved in list(watch.moves)],
                    "exact": [[round((t - started) * 1000), int(moved)] for t, moved in list(watch.exact)],
                    "reads": [[round((a - started) * 1000), round((b - started) * 1000), fingerprint[:12]]
                              for a, b, fingerprint in reads],
                    "before": before[:12], "result": result.content_fingerprint[:12],
                    "effect_ms": None if watch.effect is None else round((watch.effect.t - started) * 1000)}
            if odometry is None:
                clock.record(entry)
            else:
                # Scroll odometry costs ~10 ms per comparison: measured off the settle
                # path. A glide that kept moving after release demotes its app.
                bundle, url = snapshot.bundle_id, self.url

                def measure():
                    try:
                        from .frame_clock import changed_tiles, displacement, tile_grid
                        ref, released, final, axes = odometry
                        entry["displacement"] = displacement(ref, final, axes)
                        if released is not None:
                            after = displacement(released, final, axes)
                            entry["after_release"] = after
                            shift = abs((after or {}).get("dy" if axes == "y" else "dx", 0))
                            columns, rows = tile_grid(final.image.size)
                            churn = len(changed_tiles(released.image, final.image)) / (columns * rows)
                            if entry["glide"] and (shift > WDA_GLIDE_MOMENTUM or (
                                    churn > .4 and (after or {}).get("confidence", 0) < .5)):
                                WDA._momentum_apps.setdefault(url, set()).add(bundle)
                                entry["demoted_glide"] = True
                    except Exception:
                        pass
                    clock.record(entry)
                threading.Thread(target=measure, name="mobster-odometry", daemon=True).start()
            self._frame_followup = {"at": ended, "result": result.content_fingerprint, "fc": fc_fingerprint,
                                    "op": entry["op"], "path": entry["path"]}
        except Exception as error:  # Logging must never affect a run.
            self._frame_followup = None
            try:
                clock.record({"kind": "error", "error": type(error).__name__})
            except Exception:
                pass

    def _frame_follow_up(self, read):
        """The next read after a settle: did the accepted screen hold?"""
        followup, self._frame_followup = self._frame_followup, None
        clock = self.frame_clock
        if clock is None:
            return
        try:
            entry = {"kind": "next_read", "mode": self.frame_clock_mode, "op": followup["op"],
                     "path": followup["path"],
                     "age_ms": round((time.monotonic() - followup["at"]) * 1000, 1),
                     "matches_result": read.content_fingerprint == followup["result"]}
            if followup["fc"] is not None:
                entry["matches_fc"] = read.content_fingerprint == followup["fc"]
            clock.record(entry)
        except Exception:
            pass

    def drag_next(self):
        """The next vertical swipe is XCTest's drag even where a glide would be used; False when it would
        be a drag anyway (glides off, or only in the measured apps: WDA.tune turns them on everywhere)."""
        if (os.environ.get("MOBSTER_GLIDE", "").strip().casefold() in {"0", "off", "false", "no"}
                or not self.glide_everywhere):
            return False
        self._drag_once = True
        return True

    def _glide_ok(self, operation, snapshot):
        if operation not in {"SWIPE_UP", "SWIPE_DOWN"}:
            return False
        if getattr(self, "_drag_once", False):
            self._drag_once = False
            return False
        if os.environ.get("MOBSTER_GLIDE", "").strip().casefold() in {"0", "off", "false", "no"}:
            return False
        bundle = snapshot.bundle_id
        if not bundle or bundle in WDA._momentum_apps.get(self.url, ()):
            return False
        if self.glide_everywhere:
            return True  # WDA.tune: every app until the FrameClock sees one keep moving
        extra = {item.strip() for item in os.environ.get("MOBSTER_GLIDE_BUNDLES", "").split(",") if item.strip()}
        # Unmeasured apps keep XCTest's drag: a paging feed could snap back
        # from a velocity-free glide (the FrameClock would then demote the app).
        return bundle in WDA_GLIDE_BUNDLES or bundle in extra or "*" in extra

    def _flick(self, x1, y1, x2, y2, deadline):
        self.call("POST", "/actions", {"actions": [{
            "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
            "actions": [{"type": "pointerMove", "duration": 0, "x": round(x1), "y": round(y1)}, {"type": "pointerDown"},
                        {"type": "pointerMove", "duration": FLICK_MS, "x": round(x2), "y": round(y2)},
                        {"type": "pointerUp"}]}]}, deadline.remaining())

    def _glide(self, x1, y1, x2, y2, deadline):
        actions = [{"type": "pointerMove", "duration": 0, "x": round(x1), "y": round(y1)}, {"type": "pointerDown"}]
        for fraction, milliseconds in WDA_GLIDE_SEGMENTS:
            actions.append({"type": "pointerMove", "duration": milliseconds,
                            "x": round(x1 + (x2 - x1) * fraction), "y": round(y1 + (y2 - y1) * fraction)})
        actions.append({"type": "pointerUp"})
        self.call("POST", "/actions", {"actions": [{
            "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
            "actions": actions}]}, deadline.remaining())

    def scroll_to(self, snapshot, node, timeout=30):
        """Scroll an off-screen node of ``snapshot`` into view (SCROLL_TO).

        Instead of one decide-and-swipe cycle per half screen, the distance is
        read from the node's AX frame and covered by long glides back to back
        (WDA_SCROLL_DISTANCE each, the last one cut to the remainder), with no
        read in between; one settled read then locates the node again (closed
        loop on AX, the exact odometer) and corrects if needed. A glide that
        moved no pixels ends the pass early (end of content). Reads and scrolls
        only: nothing is tapped. Returns ``(snapshot, element, info)``;
        ``element`` is the node as an on-screen ``Element`` of the returned
        snapshot (still to be verified by the caller like any target), or None
        when it could not be brought into view (end of content, or it vanished).
        """
        if node not in snapshot.offscreen:
            raise ValueError("scroll_to needs an off-screen node of this snapshot")
        if node.direction not in {"below", "above"}:
            raise ValueError("scroll_to scrolls vertically only")
        deadline = Deadline(timeout)
        key = (node.role, node.label or node.value)
        current, target = snapshot, node
        info = {"rounds": 0, "glides": 0, "ambiguous": False}
        while info["rounds"] < WDA_SCROLL_ROUNDS and info["glides"] < WDA_SCROLL_GLIDES:
            x, y, w, h = target.rect
            need = y + h / 2 - .4  # bring its center to 40 % of the screen
            operation = "SWIPE_UP" if need > 0 else "SWIPE_DOWN"
            full, rest = divmod(abs(need), WDA_SCROLL_DISTANCE)
            spans = [WDA_SCROLL_DISTANCE + WDA_SCROLL_SLOP] * int(full)
            if rest > .05:
                spans.append(max(.1, rest + WDA_SCROLL_SLOP))
            spans = spans[:WDA_SCROLL_GLIDES - info["glides"]] or [.1]
            start = WDA_SCROLL_SPAN[operation][0]
            moved = 0.0
            for span in spans:
                y1 = start * current.height
                y2 = (start - span if operation == "SWIPE_UP" else start + span) * current.height
                x1 = current.width * _SWIPE_X
                self._stable = None
                self._swiped_at = time.monotonic()
                clock = self._frame_clock()
                watch = clock.mark() if clock is not None else None
                self._glide(x1, y1, x1, y2, deadline)
                info["glides"] += 1
                if watch is not None:
                    # Odometry: a glide that moved nothing means the content ended.
                    effect = clock.wait_effect(watch, .4)
                    clock.release(watch)
                    if effect is None and watch.usable and clock.healthy():
                        info["end"] = "no_pixel_motion"
                        break
                moved += span - WDA_SCROLL_SLOP
            info["rounds"] += 1
            after = self.observe_ready(timeout=min(4, max(.5, deadline.remaining())))
            if after.content_fingerprint == current.content_fingerprint:
                info["end"] = "no_movement"
                return after, None, info
            expected = y - moved if operation == "SWIPE_UP" else y + moved
            shown = [e for e in after.elements if (e.role, e.label or e.value) == key]
            if shown:
                info["ambiguous"] = len(shown) > 1
                element = min(shown, key=lambda e: abs(e.rect[1] - expected))
                return after, element, info
            left = [n for n in after.offscreen if (n.role, n.label or n.value) == key]
            if not left or info.get("end"):
                info.setdefault("end", "vanished")
                return after, None, info
            target = min(left, key=lambda n: abs(n.rect[1] - expected))
            current = after
        info.setdefault("end", "glide_limit")
        return current, None, info

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
        self._stable = None
        self.call("POST", "/wda/apps/activate", {"bundleId": bundle}, min(10, deadline.remaining()))
        info = {"activate_ms": round((time.monotonic() - started) * 1000, 1)}
        snapshot = self.observe_ready(timeout=deadline.remaining())
        info["ready_ms"] = round((time.monotonic() - started) * 1000, 1)
        if self.frame_clock is not None:
            self.frame_clock.record({"kind": "launch", "bundle": bundle, "mode": self.frame_clock_mode, **info})
        return snapshot, info

    def active_app(self, timeout=5):
        """The foreground bundle, read device-wide (sessionless)."""
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
            self._stable = None
            self._frame_mark(operation)
            self.call("POST", "/wda/pressButton",
                      {"name": WDA_HOST_KEY_NAMES[operation]}, deadline.remaining())
            self._frame_dispatched()
            return {"dispatch_attempted": True}
        if operation == "LAUNCH_APP":
            # The switch is trusted like any dispatched action; the next
            # snapshot reports the foreground bundle from its Application node.
            bundle = validate_bundle_id(target)
            self._stable = None
            self._frame_mark(operation)
            self.call("POST", "/wda/apps/activate", {"bundleId": bundle}, deadline.remaining())
            self._frame_dispatched()
            return {}
        if operation == "SUBMIT":
            grounded_target(operation, target, snapshot)
            if "SUBMIT" not in target.actions:
                raise ValueError("SUBMIT needs a keyboard and an editable field")
            self._check_current(snapshot, deadline)
            self._stable = None
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
            aimed = None
            if target is not None and operation in {"SWIPE_LEFT", "SWIPE_RIGHT"}:
                grounded = any(target is element or target == element for element in snapshot.elements)
                if not grounded:
                    raise ValueError("Action target is not part of the supplied observation")
                aimed = target.rect
            x1, y1, x2, y2 = _swipe_points(operation, snapshot.width, snapshot.height, aimed)
            self._stable = None
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
            if glide:
                self._glide(x1, y1, x2, y2, deadline)
            elif operation in {"SWIPE_LEFT", "SWIPE_RIGHT"} and os.environ.get("MOBSTER_FLICK", "on") != "off":
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

    def _type_text(self, operation, target, snapshot, text, deadline, multiline=None):
        """TYPE / TYPE_SUBMIT into ``target``. ``multiline``: line breaks are text (a text view by
        default; write_text also passes a text field it has seen hold line breaks)."""
        if multiline is None:
            multiline = target.role == "TextView"
        grounded_target(operation, target, snapshot)
        validate_input_text(text, multiline=multiline and operation == "TYPE")
        self._check_current(snapshot, deadline)
        focused = self._clear_stale_query(target, deadline)
        keyboard = getattr(snapshot, "keyboard", "") or ("visible" if "SUBMIT" in target.actions else "")
        if focused:
            # The clear left keyboard focus in the field: no focusing tap, no keyboard wait.
            keyboard = "visible"
        elif keyboard == "visible" and self._focused_elsewhere(target, snapshot, deadline):
            # The keyboard belongs to another field: keystrokes would go there (iOSWorld mem-048,
            # 24 Sep: a whole note body typed into its title). Focus has to move first.
            keyboard = ""
        # A visible keyboard keeps the one-call path; otherwise focus must move, and the element
        # itself moves it reliably (a coordinate tap left mem-041's focus in the title).
        if keyboard != "visible" and self._type_into_element(target, text, deadline,
                                                             submit=operation == "TYPE_SUBMIT", multiline=multiline):
            return None
        if keyboard != "visible":
            # No keyboard yet: focus the field with a tap. Many iOS fields
            # (Safari's address bar, search bars) swap in a different
            # editing field on focus, so an element-bound type fails as
            # "stale element"; keystrokes go to whatever holds focus.
            x, y = target.center
            if multiline:
                # A text view's centre puts the cursor mid-text and keystrokes insert there;
                # its last line puts it at the end, where added text belongs.
                left, top, w, h = target.rect
                x, y = left + w * .9, top + h * .92
            self._tap(x * snapshot.width, y * snapshot.height, deadline)
            if not self._await_keyboard(deadline):
                raise TransportError("The field did not take keyboard focus; nothing was typed")
        self._stable = None
        # Return is sent by the driver, never carried in model-generated text.
        keys = [text, "\n"] if operation == "TYPE_SUBMIT" else [text]
        self._frame_mark(operation)
        self.call("POST", "/wda/keys", {"value": keys}, deadline.remaining())
        self._frame_dispatched()

    def _clear_stale_query(self, target, deadline):
        """Empty a search field that still holds an earlier query before typing a new one.

        Keystrokes insert at the cursor: live (pass 6, text.notes_search) Notes kept
        "MobsterBench Note" from an earlier search and the typed query became
        "MobsterBenchLocker code Note", with no results. A search replaces its query, so
        only search fields are cleared; other fields may hold the user's own text.
        """
        return target.role == "SearchField" and self._clear_field(target, deadline)

    def _focused_elsewhere(self, target, snapshot, deadline):
        """True when WDA reports keyboard focus in an element outside ``target``'s frame. Asked only
        where it can differ (more than one editable field); unknown counts as not elsewhere."""
        if sum(e.editable for e in snapshot.elements) < 2:
            return False
        try:
            identifier = _element_id(self.call("GET", "/element/active", None, min(2, deadline.remaining())) or {})
            if not identifier:
                return False
            rect = self.call("GET", f"/element/{quote(identifier, safe='')}/rect", None, min(2, deadline.remaining()))
            cx = (rect["x"] + rect["width"] / 2) / snapshot.width
            cy = (rect["y"] + rect["height"] / 2) / snapshot.height
        except (TransportError, TimeoutError, KeyError, TypeError, ZeroDivisionError):
            return False
        x, y, w, h = target.rect
        return not (x - .01 <= cx <= x + w + .01 and y - .01 <= cy <= y + h + .01)

    def _type_into_element(self, target, text, deadline, submit=False, multiline=False):
        """Type through the element itself (XCUITest focuses it), when it can be found uniquely.

        A focusing tap by coordinates can leave focus in the previous field: iOSWorld mem-041
        (24 Sep) typed a whole note body into its title. Used for text fields, and for text views
        only while empty (typeText taps the centre, which would put the cursor mid-text). Search
        fields keep the coordinate path (Safari swaps in another field on focus). False means
        nothing was typed and the caller uses its own path.
        """
        filled = (target.value or "").strip() and (target.value or "").strip() != (target.label or "").strip()
        # A multi-line text field (SwiftUI's vertical TextField) is a text view here: CloudSlides' body
        # took the typed text mid-way (cloudslides-002, 4 of 4 runs, 25 Sep).
        if target.role not in ("TextField", "TextView") or (target.role == "TextView" or multiline) and filled:
            return False
        name = (target.label or "").replace("\\", "\\\\").replace("'", "\\'")
        if not name:
            return False
        # ``name`` is WDA's identifier-or-label. ``identifier`` is not a predicate attribute: WDA 16.12
        # rejected the query, so until 24 Sep this path never ran and every field was focused by a tap.
        try:
            found = self.call("POST", "/elements", {"using": "predicate string", "value":
                              f"type == 'XCUIElementType{target.role}' AND (name == '{name}' OR label == '{name}')"},
                              min(5, deadline.remaining()))
            identifier = _element_id(found[0]) if isinstance(found, list) and len(found) == 1 else None
            if not identifier:
                return False
            self._stable = None
            self._frame_mark("TYPE")
            # Return goes in the same request (typeText types it): one WDA call less per TYPE_SUBMIT.
            self.call("POST", f"/element/{quote(identifier, safe='')}/value",
                      {"value": text + "\n" if submit else text}, deadline.remaining())
            self._frame_dispatched()
            return True
        except TransportError:
            return False

    def set_value(self, target, value, timeout=10):
        """Select ``value`` on a picker wheel (its item text: "6", "30", "AM") through WDA.

        The wheel is the one of its type whose frame is nearest the observed one: looking it
        up by its current value failed when two wheels showed the same value (iOSWorld
        multi-069, 24 Sep: twelve refusals). About 0.25 s per wheel on the simulator.
        """
        deadline = Deadline(timeout)
        found = self.call("POST", "/elements", {"using": "class name", "value": f"XCUIElementType{target.role}"},
                          min(5, deadline.remaining()))
        ids = [i for i in (_element_id(e) for e in (found if isinstance(found, list) else [])[:12]) if i]
        if not ids:
            raise DriverRejection("unsupported_or_stale_target")
        cx, cy = target.center
        best, best_distance = None, None
        width, height = WDA._screen_sizes.get(self.url) or (None, None)
        if not width:
            current = self.observe(timeout=min(5, deadline.remaining()))
            width, height = current.width, current.height
        for identifier in ids:
            rect = self.call("GET", f"/element/{quote(identifier, safe='')}/rect", timeout=min(3, deadline.remaining()))
            if not isinstance(rect, dict) or not width:
                continue
            x = (rect.get("x", 0) + rect.get("width", 0) / 2) / width
            y = (rect.get("y", 0) + rect.get("height", 0) / 2) / height
            distance = (x - cx) ** 2 + (y - cy) ** 2
            if best_distance is None or distance < best_distance:
                best, best_distance = identifier, distance
        best = best or (ids[0] if len(ids) == 1 else None)
        if best is None:
            raise DriverRejection("unsupported_or_stale_target")
        self._stable = None
        wanted = " ".join(str(value).split())
        # The model often names an item as the wheel shows it ("6 o’clock", "45 minutes"): WDA ignores
        # a value that is not an item, silently, and an alarm was saved at 2:19 for 6:45 (iOSWorld
        # clock-001, 25 Sep). The bare item ("6", "45") is tried when the wheel did not move to it.
        bare = re.match(r"(\d+|AM|PM)\b", wanted, re.IGNORECASE)
        for attempt in [wanted] + ([bare.group(1)] if bare and bare.group(1) != wanted else []):
            self.call("POST", f"/element/{quote(best, safe='')}/value", {"value": attempt}, deadline.remaining())
            try:
                shown = self.call("GET", f"/element/{quote(best, safe='')}/attribute/value",
                                  timeout=min(3, deadline.remaining()))
            except TransportError:
                return
            if not isinstance(shown, str) or shown.split()[:1] == attempt.split()[:1]:
                return

    def clear_text(self, target, timeout=10):
        """Empty an editable field (a caller replacing its text: a label, a title). Best effort."""
        self._clear_field(target, Deadline(timeout))

    def replace_text(self, target, text, timeout=10):
        """Replace a text field's content through one element reference: found once by name, cleared,
        typed. True when done; False when nothing was typed (not a named text field, not unique, or a
        search field whose clear failed) and the caller clears and types its own way.

        Clearing, reading the field back and typing it as a TYPE found the field three times and
        read the screen between (a SET_TEXT took 4.2 s on average in mem-015, 25 Sep).
        """
        validate_input_text(text, multiline=target.role == "TextView")
        name = (target.label or "").replace("\\", "\\\\").replace("'", "\\'")
        query = (target.value or "").strip()
        filled = bool(query) and query != (target.label or "").strip()
        # A search field is typed into by keystrokes, never through the element (Safari swaps in
        # another field on focus): only a filled one, whose clear leaves keyboard focus in it.
        if target.role not in ("TextField", "TextView", "SearchField") or not name or (
                target.role == "SearchField" and not filled):
            return False
        deadline = Deadline(timeout)
        try:
            found = self.call("POST", "/elements", {"using": "predicate string", "value":
                              f"type == 'XCUIElementType{target.role}' AND (name == '{name}' OR label == '{name}')"},
                              min(5, deadline.remaining()))
            identifier = _element_id(found[0]) if isinstance(found, list) and len(found) == 1 else None
            if not identifier:
                return False
        except TransportError:
            return False
        path = f"/element/{quote(identifier, safe='')}"
        self._stable = None
        if filled:
            try:
                self.call("POST", path + "/clear", {}, min(5, deadline.remaining()))
            except TransportError:
                # As clear_text: typed anyway, the result is observed (MegaMart's search field timed out
                # clearing at 5 s, and a second clear by the caller cost 5 s more). A search field
                # without a proven clear has no proven focus: the caller taps it.
                if target.role == "SearchField":
                    return False
        self._frame_mark("TYPE")
        if target.role == "SearchField":
            self.call("POST", "/wda/keys", {"value": [text]}, deadline.remaining())
        else:
            self.call("POST", path + "/value", {"value": text}, deadline.remaining())
        self._frame_dispatched()
        return True

    def _clear_field(self, target, deadline):
        query = (target.value or "").strip()
        if target.role not in {"SearchField", "TextField", "TextView"} or not query or query == (target.label or "").strip():
            return False
        def literal(text):
            return text.replace("\\", "\\\\").replace("'", "\\'")
        name = (target.label or "").strip()
        kind = f"type == 'XCUIElementType{target.role}'"
        # WDA re-finds an element through the query that found it. Found by its value, the field
        # was gone once emptied: /clear emptied it, then failed after ~2.7 s ("not present in the
        # current view anymore"; 83 s of mem-015's 276 s, 24 Sep). By name, or as the only field of
        # its kind, the same clear takes ~0.4 s; the value finds it only when neither is unique. A long
        # value is read truncated ("head … tail", state._wda_text): its head finds it (an exact match
        # found nothing, and SET_TEXT appended to a note body: iOSWorld mem-004, 24 Sep).
        by_value = (f"{kind} AND value == '{literal(query)}'" if " … " not in query and len(query) < 500
                    else f"{kind} AND value BEGINSWITH '{literal(query.split(' … ', 1)[0][:150])}'")
        queries = ([f"{kind} AND (name == '{literal(name)}' OR label == '{literal(name)}')"] if name else []) + [
            kind, by_value]
        for index, predicate in enumerate(queries):
            try:
                found = self.call("POST", "/elements", {"using": "predicate string", "value": predicate},
                                  min(5, deadline.remaining()))
            except TransportError:
                continue
            ids = [i for i in (_element_id(e) for e in (found if isinstance(found, list) else [])) if i]
            if len(ids) == 1 or ids and index == len(queries) - 1:
                self._stable = None
                try:
                    self.call("POST", f"/element/{quote(ids[0], safe='')}/clear", {}, min(5, deadline.remaining()))
                except TransportError:
                    return False  # Not clearable: typed as before (the result is still observed).
                # XCTest focuses a field to clear it: the keyboard is the target's when it was the only match.
                return len(ids) == 1
        return False

    def _check_current(self, snapshot, deadline):
        """Re-ground an aging snapshot before a coordinate tap; fresh ones pass.

        A change is refused before dispatch, which the agent treats as an
        ordinary re-observe rather than an action of unknown outcome.
        """
        if time.monotonic() - snapshot.captured_at <= WDA_FRESH_SECONDS:
            return
        if self.observe(timeout=deadline.remaining()).fingerprint != snapshot.fingerprint:
            raise DriverRejection("stale_revision")

    def _await_keyboard(self, deadline, seconds=None):
        """True once a software keyboard is up, or, when none appears, a hardware one is attached.

        A parked keyboard (off screen: simulators, a paired keyboard) still takes keystrokes
        in the focused field. Measured on iOSWorld's simulators (24 Sep): 3 of 4 ran in
        hardware-keyboard mode and every TYPE failed "did not take keyboard focus" although
        /wda/keys typed fine. A visible keyboard is still preferred while it may be animating in.
        """
        tapped = time.monotonic()
        end = tapped + (WDA_KEYBOARD_WAIT_SECONDS if seconds is None else seconds)
        parked = False
        hardware = self.url in WDA._parked_keyboards
        if hardware:
            # No software keyboard will come: one read, once focus has had time to move.
            time.sleep(max(0.0, min(tapped + WDA_PARKED_FOCUS_SECONDS - time.monotonic(),
                                    deadline.end - time.monotonic() - WDA_READ_MARGIN_SECONDS)))
        first = True
        # At least one read: a slow host's focus sleep can outlast the window (CI read nothing, 25 Sep).
        while (first or time.monotonic() < end) and deadline.end - time.monotonic() > WDA_READ_MARGIN_SECONDS:
            first = False
            current = self.observe(timeout=deadline.remaining())
            keyboard = getattr(current, "keyboard", "") or (
                "visible" if any("SUBMIT" in e.actions for e in current.elements) else "")
            if keyboard == "visible":
                WDA._parked_keyboards.discard(self.url)
                return True
            parked = parked or keyboard == "parked"
            if parked and (hardware or time.monotonic() > end - WDA_KEYBOARD_WAIT_SECONDS / 2):
                WDA._parked_keyboards.add(self.url)
                return True
        if parked:
            WDA._parked_keyboards.add(self.url)
        return parked

    # -- The frontier's options (Jev and the plain driver never call these) -------------------

    def is_simulator(self, timeout=3):
        """True when this WDA runs on a simulator (its /status names a simulator version); asked once
        per URL. Unknown (no answer) is False, and is asked again next time."""
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

    # -- Exact text -------------------------------------------------------------------------

    def write_text(self, target, snapshot, text, append=False, submit=False, timeout=30):
        """Write ``text`` into the field ``target`` and prove it by reading the whole field back.

        ``append`` adds it at the end (TYPE); otherwise it replaces the field's text (SET_TEXT; ""
        empties the field). ``submit`` presses Return once the text is written (TYPE_SUBMIT).
        Text of WDA_PASTE_MIN_CHARS or more, and text with line breaks for a field not known to take
        them, goes in through the pasteboard and the edit menu's Paste: exact, far faster than
        keystrokes on a long body, and a one-line field turns pasted line breaks into spaces where a
        typed Return could submit it. An append that is pasted rewrites the field (its text, then the
        addition): Paste is only offered in an empty field. When Paste cannot be reached the text is
        typed. A read-back that differs is repaired once (emptied and written again) when the field's
        earlier text is known. Returns a receipt: {"method": "type", "paste" or "clear", "value": the
        field's text read back (None: not read), "expected", "matches", "repaired", "flattened" (a
        one-line field took the line breaks as spaces), "chars"}.
        """
        deadline = Deadline(timeout)
        grounded_target("TYPE", target, snapshot)
        rank = self._write_rank = self._rank(snapshot, target)
        if (not isinstance(text, str) or len(text) > WDA_WRITE_MAX_CHARS or append and not text.strip()
                or any(unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"} and c not in "\n\t" for c in text)):
            raise ValueError("TYPE requires bounded text without control characters")
        text = text.replace("\t", " ")
        shown = target.text if target.placeholder else (target.value or "")
        multiline = target.role == "TextView" or "\n" in shown
        breaks = "\n" in text and not multiline
        old = self._field_text(target, snapshot, deadline) if append else ""
        # An empty field reads as its placeholder, which not every field names (SwiftUI's vertical
        # TextField does not): an earlier text is known only when it cannot be a placeholder.
        known = old is not None and (not old or target.role == "TextView" or bool(target.placeholder)
                                     or "\n" in old or len(old) >= 60)
        paste = bool(text) and (len(text) >= self.paste_min_chars or breaks) and old is not None
        receipt = {"method": "clear" if not text else "paste" if paste else "type", "repaired": False,
                   "flattened": False}
        current, field = snapshot, target
        if paste or not append:
            if self._emptied(field, current, deadline):
                current, field = self._reread(field, deadline)
                if append and not known and (field.value or "") == old:
                    old, known = "", True  # emptying changed nothing: it showed its placeholder
            elif append:
                old, known = "", True
        expected = (old or "") + text if append else text
        if paste and not self._paste(field, current, expected, deadline):
            receipt["method"], paste = "type", False
        if receipt["method"] == "type":
            typed = expected if (not append or current is not snapshot) else text
            if breaks:
                # Never Return in a field not known to take line breaks: it may submit.
                typed, receipt["flattened"] = " ".join(typed.split("\n")), True
                expected = " ".join(expected.split("\n"))
            self._type_chunks(field, current, typed, deadline, multiline)
        value, current = self._read_back(target, deadline, rank)
        matches = self._fits(value, expected, text, append and not known, breaks, field)
        if not matches and value is not None and breaks and self._same_text(value, " ".join(expected.split("\n"))):
            receipt["flattened"], matches = True, True  # a one-line field: its line breaks became spaces
        if not text and not matches and value is not None and value != shown:
            # Emptied, yet it reads as something: emptied again, what stays is its placeholder.
            field = self._same_field(current, target, rank) or field
            self._emptied(field, current, deadline)
            again, current = self._read_back(target, deadline, rank)
            if again == value:
                receipt["placeholder"], value, matches = value, "", True
        elif not matches and value is not None and (not append or known):
            receipt["repaired"] = True
            field = self._same_field(current, target, rank) or field
            if self._emptied(field, current, deadline):
                current, field = self._reread(field, deadline)
            if not self._paste(field, current, expected, deadline):
                self._type_chunks(field, None, " ".join(expected.split("\n")) if breaks else expected,
                                  deadline, multiline)
            value, current = self._read_back(target, deadline, rank)
            matches = self._fits(value, expected, text, False, breaks, field)
        if submit:
            self._stable = None
            self._frame_mark("SUBMIT")
            self.call("POST", "/wda/keys", {"value": ["\n"]}, deadline.remaining())
            self._frame_dispatched()
        receipt.update(value=value, expected=expected, matches=bool(matches),
                       chars=None if value is None else len(value))
        return receipt

    def _fits(self, value, expected, text, unknown_start, breaks, field):
        """The field's text read back is what was written: ``expected``, or with an earlier text that
        could not be known (``unknown_start``), anything ending in ``text``; line breaks a one-line
        field made spaces count."""
        if self._same_text(value, expected, field=field):
            return True
        if unknown_start and value is not None:
            return self._same_text(value, text, suffix=True) or breaks and self._same_text(
                value, " ".join(text.split("\n")), suffix=True)
        return False

    @staticmethod
    def _same_text(value, expected, suffix=False, field=None):
        """``value`` (a field read back) is ``expected``: exactly, or but for typographic quotes and
        dashes (smart punctuation); ``suffix``: it ends with it (text added to a field whose earlier
        text was not read). An empty field may read as its placeholder."""
        if value is None:
            return False
        if not expected and field is not None and field.placeholder and value == field.placeholder:
            return True

        def plain(text):
            return text.translate(_TYPOGRAPHIC).replace("\r\n", "\n")
        return (value == expected or plain(value) == plain(expected)
                or suffix and plain(value).endswith(plain(expected)))

    @staticmethod
    def _rank(snapshot, target):
        """(fields of ``target``'s kind on ``snapshot``, its place among them)."""
        fields = [e for e in snapshot.elements if e.editable and e.role == target.role]
        return len(fields), next((i for i, e in enumerate(fields) if e == target), None)

    def _same_field(self, snapshot, target, rank=None):
        """``target`` on a later ``snapshot``: the same path, else the same label, else (``rank``, from
        _rank on the earlier read) the field in its place among as many of its kind (CloudSlides' editor
        moved its fields sideways as the keyboard came up), else the nearest field of its kind (a
        vertical text field grows as it fills)."""
        fields = [e for e in snapshot.elements if e.editable and e.role == target.role]
        same = [e for e in fields if e.locator == target.locator]
        if len(same) == 1:
            return same[0]
        if target.label:
            named = [e for e in fields if e.label == target.label]
            if len(named) == 1:
                return named[0]
        if rank is not None and rank[1] is not None and len(fields) == rank[0]:
            return fields[rank[1]]

        def distance(e):
            return abs(e.rect[0] - target.rect[0]) + abs(e.rect[1] - target.rect[1])
        near = min(fields, key=distance, default=None)
        return near if near is not None and distance(near) < .08 else None

    def _field_text(self, target, snapshot, deadline):
        """The field's whole text now: from the read when it is not cut there, else from WDA. None
        when it cannot be read."""
        if target.placeholder and target.value == target.placeholder:
            return ""
        if not truncated_field_value(target.value):
            return target.value or ""
        return self._active_text(target, snapshot, deadline, focus=True)

    def _active_id(self, target, snapshot, deadline, focus=False):
        """WDA's id for the field holding keyboard focus when it is ``target`` (their frames agree), else
        None. ``focus``: when it is not, tap the field and ask again."""
        for attempt in range(2 if focus else 1):
            try:
                identifier = _element_id(self.call("GET", "/element/active", None, min(3, deadline.remaining())) or {})
                if identifier:
                    rect = self.call("GET", f"/element/{quote(identifier, safe='')}/rect", None,
                                     min(3, deadline.remaining()))
                    if self._frame_agrees(rect, target, snapshot):
                        return identifier
            except (TransportError, KeyError, TypeError):
                pass
            if attempt == 0 and focus:
                x, y = target.center
                self._tap(x * snapshot.width, y * snapshot.height, deadline)
                time.sleep(WDA_PARKED_FOCUS_SECONDS)
        return None

    def _active_text(self, target, snapshot, deadline, focus=False):
        """The whole text of ``target`` read through the field holding focus ("" when it shows only its
        placeholder), or None when that field is not ``target``."""
        identifier = self._active_id(target, snapshot, deadline, focus)
        if identifier is None:
            return None
        path = f"/element/{quote(identifier, safe='')}/attribute/"
        try:
            value = self.call("GET", path + "value", None, min(3, deadline.remaining()))
            if value and target.role != "TextView":
                placeholder = self.call("GET", path + "placeholderValue", None, min(3, deadline.remaining()))
                if placeholder and value == placeholder:
                    return ""
        except (TransportError, TypeError):
            return None
        return value if isinstance(value, str) else ""

    @staticmethod
    def _frame_agrees(rect, target, snapshot):
        """A WDA rect (points) is ``target``'s frame: either one's centre lies in the other (a vertical
        text field grows down as it fills)."""
        if not isinstance(rect, dict):
            return False
        try:
            ax, ay, aw, ah = (float(rect[k]) / d for k, d in (("x", snapshot.width), ("y", snapshot.height),
                                                             ("width", snapshot.width), ("height", snapshot.height)))
        except (KeyError, TypeError, ValueError):
            return False
        tx, ty, tw, th = target.rect
        slack = .01
        return (ax - slack <= tx + tw / 2 <= ax + aw + slack and ay - slack <= ty + th / 2 <= ay + ah + slack
                or tx - slack <= ax + aw / 2 <= tx + tw + slack and ty - slack <= ay + ah / 2 <= ty + th + slack)

    def _emptied(self, target, snapshot, deadline):
        """True once ``target`` was emptied, False when it already was (and is left alone). The field keeps
        its top as it empties (the paste presses there); typing after it reads the screen again."""
        text = target.text if target.placeholder else (target.value or "")
        if not text.strip() or text.strip() == (target.label or "").strip() and not target.placeholder:
            return False
        self._stable = None
        # A named field is cleared by its name; an unnamed one (CloudSlides' slide body, one of several
        # text fields) through the field holding focus: asking WDA for every field of its kind took 1.8 s.
        if not (target.label and self._clear_field(target, deadline)):
            identifier = self._active_id(target, snapshot, deadline, focus=True)
            if identifier is not None:
                try:
                    self.call("POST", f"/element/{quote(identifier, safe='')}/clear", {}, min(8, deadline.remaining()))
                except TransportError:
                    pass  # the read-back shows what is there
        return True

    def _paste(self, target, snapshot, text, deadline):
        """Put ``text`` on the pasteboard and choose Paste in the empty field's edit menu (pressed twice at
        most: the second time after a focusing tap). False when no Paste was offered: nothing pasted."""
        try:
            self.call("POST", "/wda/setPasteboard", {"content": base64.b64encode(text.encode()).decode(),
                                                     "contentType": "plaintext"}, min(5, deadline.remaining()))
        except TransportError:
            return False
        left, top, width, height = target.rect
        x = left + width / 2
        y = top + min(height / 2, 22 / snapshot.height)  # the first line: a long press on text moves the caret
        self._stable = None
        self._frame_mark("TYPE")
        try:
            for attempt in range(2):
                if attempt:
                    self._tap(x * snapshot.width, y * snapshot.height, deadline)
                    time.sleep(WDA_PARKED_FOCUS_SECONDS)
                self.call("POST", "/actions", {"actions": [{
                    "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
                    "actions": [{"type": "pointerMove", "duration": 0, "x": round(x * snapshot.width),
                                 "y": round(y * snapshot.height)},
                                {"type": "pointerDown"}, {"type": "pause", "duration": WDA_PASTE_PRESS_MS},
                                {"type": "pointerUp"}]}]}, deadline.remaining())
                end = time.monotonic() + WDA_PASTE_MENU_SECONDS
                while time.monotonic() < end and deadline.end - time.monotonic() > WDA_READ_MARGIN_SECONDS:
                    # One query, not a screen read: a read may also ask WDA which layers show (up to 3 s
                    # a read in CloudSlides' editor, 26 Sep).
                    try:
                        found = self.call("POST", "/elements", {"using": "predicate string", "value":
                                          "type == 'XCUIElementTypeMenuItem' AND label == 'Paste'"},
                                          min(3, deadline.remaining()))
                        identifier = _element_id(found[0]) if isinstance(found, list) and found else None
                        rect = identifier and self.call("GET", f"/element/{quote(identifier, safe='')}/rect", None,
                                                        min(3, deadline.remaining()))
                    except TransportError:
                        identifier = rect = None
                    if isinstance(rect, dict) and all(finite_positive(rect.get(k)) or rect.get(k) == 0
                                                      for k in ("x", "y", "width", "height")):
                        self._tap(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2, deadline)
                        return True
                    time.sleep(.1)
            return False
        finally:
            self._frame_dispatched()

    def _reread(self, target, deadline):
        """(a new read, ``target`` on it)."""
        fresh = self.observe(timeout=min(5, deadline.remaining()))
        return fresh, self._same_field(fresh, target, getattr(self, "_write_rank", None)) or target

    def _type_chunks(self, target, snapshot, text, deadline, multiline):
        """Type ``text`` (a known multi-line field takes its line breaks), in chunks typing accepts.
        ``snapshot`` None: the field changed since it was read (emptied), so it is read again."""
        limit = 4000 if multiline else 1000
        chunks = [text[i:i + limit] for i in range(0, len(text), limit)] or [""]
        fresh = snapshot
        if fresh is None or time.monotonic() - fresh.captured_at > WDA_FRESH_SECONDS:
            fresh, target = self._reread(target, deadline)
        first = chunks[0]
        if first.strip() and self._active_id(target, fresh, deadline) is not None:
            # The field holds focus (it was just emptied through it): keystrokes, no focusing tap. A tap on
            # an emptied CloudSlides body left it without focus every second write (F2, 26 Sep).
            self._stable = None
            self._frame_mark("TYPE")
            self.call("POST", "/wda/keys", {"value": [first]}, deadline.remaining())
            self._frame_dispatched()
        elif first.strip():
            try:
                self._type_text("TYPE", target, fresh, first, deadline, multiline=multiline)
            except TransportError as error:
                # The focusing tap was made but no keyboard was seen (none shows with a hardware keyboard
                # on some screens): keystrokes go where focus is, so only when the field holds it.
                if "keyboard focus" not in str(error) or self._active_id(target, fresh, deadline) is None:
                    raise
                self._frame_mark("TYPE")
                self.call("POST", "/wda/keys", {"value": [first]}, deadline.remaining())
                self._frame_dispatched()
        for chunk in chunks[1:]:
            self.call("POST", "/wda/keys", {"value": [chunk]}, deadline.remaining())

    def _read_back(self, target, deadline, rank=None):
        """(the field's whole text, the read): from a source read when the text is not cut there, else
        from the field holding focus. None when the field is not found."""
        fresh = self.observe(timeout=min(5, deadline.remaining()))
        field = self._same_field(fresh, target, rank)
        if field is not None and not truncated_field_value(field.value):
            return (field.text if field.placeholder else field.value or ""), fresh
        return self._active_text(field or target, fresh, deadline), fresh

    # -- Recovery -------------------------------------------------------------------------------

    def _status_ok(self, timeout=3):
        try:
            status = self.http.request("GET", "/status", timeout=timeout)
        except (TransportError, TimeoutError, ValueError):
            self.http.close()
            return False
        value = status.get("value") if isinstance(status, dict) else None
        return isinstance(value, dict) and value.get("ready", True) is not False

    def recover(self, timeout=WDA_RECOVER_SECONDS):
        """Bring back a runner that reset or refused the connection, and attach to it: restart it
        (``restarter``) when it does not answer, or wait for its supervisor (the USB runner), then take
        its session (``new_session`` or resolve_wda_session), apply the session and tuned settings
        again and forget what belonged to the old session. Returns the seconds it took; raises
        TransportError when no runner answers within ``timeout``."""
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
        self._hinted, self._frame_watch, self._frame_action, self._frame_followup = None, None, None, None
        self._pages_stale = True
        self.configure(timeout=min(10, timeout))
        if self.tuned_settings:
            self.call("POST", "/appium/settings", {"settings": dict(self.tuned_settings)}, min(5, timeout))
        return session

    def long_press(self, target, snapshot, timeout=10):
        """Press and hold ``target`` (a reaction picker, a context menu) at its tap point."""
        deadline = Deadline(timeout)
        if not any(e is target or e.id == target.id and e.locator == target.locator for e in snapshot.elements):
            raise ValueError("Action target is not part of the supplied observation")
        self._check_current(snapshot, deadline)
        self._note_tap(target.locator)
        x, y = switch_point(target, snapshot.width)
        self._stable = None
        self._frame_mark("LONG_PRESS")
        self.call("POST", "/actions", {"actions": [{
            "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
            "actions": [{"type": "pointerMove", "duration": 0, "x": round(x * snapshot.width),
                         "y": round(y * snapshot.height)},
                        {"type": "pointerDown"}, {"type": "pause", "duration": LONG_PRESS_MS}, {"type": "pointerUp"}]}]},
            deadline.remaining())
        self._frame_dispatched()

    def tap_point(self, x, y, snapshot, timeout=10):
        """A tap at screen fractions (x, y): for closing a menu by tapping outside it."""
        self._pages_stale = True
        self._tap(x * snapshot.width, y * snapshot.height, Deadline(timeout))

    def _tap(self, x, y, deadline):
        self._stable = None
        self.call("POST", "/actions", {"actions": [{
            "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
            "actions": [{"type": "pointerMove", "duration": 0, "x": round(x), "y": round(y)},
                        {"type": "pointerDown"}, {"type": "pause", "duration": WDA_TAP_HOLD_MS},
                        {"type": "pointerUp"}]}]}, deadline.remaining())

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


# Driver kinds by name. The core registers WDA; an optional extension
# (``extensions.py``) may register more before ``build_driver`` looks one up.
DRIVERS = {"wda": WDA}


def register_driver(kind, cls):
    """Add a driver kind. The class must subclass ``Driver``."""
    if not isinstance(kind, str) or not kind or not (isinstance(cls, type) and issubclass(cls, Driver)):
        raise TypeError("register_driver needs a name and a Driver subclass")
    DRIVERS[kind] = cls


def build_driver(kind, **kwargs):
    """One validated construction site for every driver.

    Unknown kinds raise ``ValueError`` before anything is constructed; a
    registered class that does not implement the device contract raises
    ``TypeError`` instead of failing later at first use.
    """
    if isinstance(kind, str) and kind not in DRIVERS:
        from .extensions import load
        load()
    try:
        cls = DRIVERS[kind]
    except (KeyError, TypeError):
        raise ValueError(f"Unknown driver {kind!r}; expected one of {sorted(DRIVERS)}") from None
    driver = cls(**kwargs)
    if not isinstance(driver, Driver):
        raise TypeError(f"Driver {kind!r} does not implement the device contract")
    return driver

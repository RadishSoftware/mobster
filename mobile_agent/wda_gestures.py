"""WebDriverAgent touch input: the swipe, glide, flick, hold, drag and tap strokes, where a stroke starts and where a
switch is pressed. WDAGestures is the touch part of drivers.WDA."""

import math
import os

from .driver_guards import finite_positive, grounded_target
from .transport import Deadline


_SWIPE_X = .5
_SWIPE_Y = .5
_SWIPE_UP_Y = (.75, .25)
_SWIPE_DOWN_Y = (.25, .75)
_SWIPE_LEFT_X = (.75, .25)
_SWIPE_RIGHT_X = (.25, .75)
# LockedIn's reaction picker opened on a 1.5 s hold and not on 0.9 s (live, 25 Sep).
LONG_PRESS_MS = 1500
FLICK_MS = 120
# A list row's sideways swipe only reveals its actions: a slow drag over 35% of the row (from 75% to 40%), held still
# before it lifts so no momentum carries it on. A flick across half the row can cross an app's full-swipe line, where
# Notes deletes and Mail trashes with no tap (HARDENING-RESEARCH #3, 14 Oct; to be measured on the phone).
_REVEAL_LEFT_X = (.75, .40)
_REVEAL_RIGHT_X = (.25, .60)
REVEAL_MS = 350
REVEAL_HOLD_MS = 120
# XCTest's drag, the fallback scroll (see WDA_GLIDE_SEGMENTS for the default).
# A W3C drag held still before lifting flings (measured twice, 2026-09-23).
# Not the faster velocity drag: at 6000 px/s (2.28 s vs ~2.4 s here) its
# momentum carried the Settings list past General and Accessibility, so every
# navigation task scrolled beyond its target. A scroll must move a bounded,
# predictable distance; this endpoint does.
WDA_SWIPE_SECONDS = .1
WDA_TAP_HOLD_MS = 40
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
# Where a glide is used names no app (one general engine, 10 Oct): a tuned driver glides everywhere, a plain one
# wherever a healthy FrameClock watches. Motion after release (a fling or a paging snap) sends that app back to
# XCTest's drag for the rest of the process. MOBSTER_GLIDE=0 turns the glide off.
# Content that moved more than this (screen fraction) after the glide returned.
WDA_GLIDE_MOMENTUM = .12
# SWIPE_UP/DOWN as a glide: from 80 % to 12 % of the screen (up) or 20 % to 88 % (down). Starting higher lands on
# the navigation bar and does not scroll; Safari's bottom address bar sits at ~0.88, and a stroke that starts on it
# does not scroll either.
WDA_GLIDE_SPAN = {"SWIPE_UP": (.80, .12), "SWIPE_DOWN": (.2, .88)}


def _screen_point(snapshot, x, y):
    """Validate screen fractions and keep rounded points inside their observation's dimensions."""
    if any(type(v) not in (int, float) or not 0 <= v <= 1 for v in (x, y)):
        raise ValueError("point coordinates must be finite screen fractions between 0 and 1")
    dimensions = (getattr(snapshot, "width", None), getattr(snapshot, "height", None))
    if any(not finite_positive(v) or v < 1 for v in dimensions):
        raise ValueError("point actions need an observation with valid screen dimensions")
    return tuple(min(round(value * size), math.ceil(size) - 1) for value, size in zip((x, y), dimensions))


def _in_range(value, low, high):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and low <= value <= high


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


def _swipe_points(operation, width, height, target_rect=None, reveal=False):
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
    if target_rect is not None and reveal:
        start, end = _REVEAL_LEFT_X if operation == "SWIPE_LEFT" else _REVEAL_RIGHT_X
    if target_rect is not None:
        left, top, w, h = target_rect
        left, right = max(0.0, left), min(1.0, left + w)
        y = height * min(max(top + h / 2, 0.0), 1.0)
        return (width * (left + (right - left) * start), y, width * (left + (right - left) * end), y)
    y = height * _SWIPE_Y
    return width * start, y, width * end, y


class WDAGestures:
    """The touch part of WDA. It runs on the whole driver: call, _dispatching, _frame_mark and _check_current come from
    the other parts."""
    # Apps where a glide kept moving after release, per WDA URL.
    _momentum_apps = {}

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
        if self._drag_once:
            self._drag_once = False
            return False
        if os.environ.get("MOBSTER_GLIDE", "").strip().casefold() in {"0", "off", "false", "no"}:
            return False
        bundle = snapshot.bundle_id
        if not bundle or bundle in self._momentum_apps.get(self.url, ()):
            return False
        if self.glide_everywhere:
            return True  # WDA.tune: every app until the FrameClock sees one keep moving
        # A plain driver glides only where a healthy FrameClock watches, so an app that keeps moving after a glide
        # is demoted to XCTest's drag; without one, XCTest's drag in every app alike. This replaced a list of two
        # apps (10 Oct) and isn't measured on the phone yet.
        clock = getattr(self, "frame_clock", None)
        try:
            return clock is not None and bool(clock.healthy())
        except Exception:  # noqa: BLE001 -- a clock that can't say is not watching
            return False

    def _pointer(self, steps, deadline):
        """One finger doing ``steps`` (W3C pointer actions) through WDA."""
        self.call("POST", "/actions", {"actions": [{
            "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"}, "actions": steps}]},
            deadline.remaining())

    def _flick(self, x1, y1, x2, y2, deadline):
        self._pointer([{"type": "pointerMove", "duration": 0, "x": round(x1), "y": round(y1)}, {"type": "pointerDown"},
                       {"type": "pointerMove", "duration": FLICK_MS, "x": round(x2), "y": round(y2)},
                       {"type": "pointerUp"}], deadline)

    def _reveal(self, x1, y1, x2, y2, deadline):
        """A list row's swipe: a slow drag held still before it lifts, so it opens the row's actions and no more."""
        self._pointer([{"type": "pointerMove", "duration": 0, "x": round(x1), "y": round(y1)}, {"type": "pointerDown"},
                       {"type": "pointerMove", "duration": REVEAL_MS, "x": round(x2), "y": round(y2)},
                       {"type": "pause", "duration": REVEAL_HOLD_MS}, {"type": "pointerUp"}], deadline)

    def _glide(self, x1, y1, x2, y2, deadline):
        actions = [{"type": "pointerMove", "duration": 0, "x": round(x1), "y": round(y1)}, {"type": "pointerDown"}]
        for fraction, milliseconds in WDA_GLIDE_SEGMENTS:
            actions.append({"type": "pointerMove", "duration": milliseconds,
                            "x": round(x1 + (x2 - x1) * fraction), "y": round(y1 + (y2 - y1) * fraction)})
        actions.append({"type": "pointerUp"})
        self._pointer(actions, deadline)

    def long_press(self, target, snapshot, timeout=10):
        """Press and hold ``target`` (a reaction picker, a context menu) at its tap point."""
        deadline = Deadline(timeout)
        grounded_target("LONG_PRESS", target, snapshot)
        self._check_current(snapshot, deadline)
        self._note_tap(target.locator)
        x, y = switch_point(target, snapshot.width)
        self._dispatching()
        self._frame_mark("LONG_PRESS")
        self._pointer([{"type": "pointerMove", "duration": 0, "x": round(x * snapshot.width),
                        "y": round(y * snapshot.height)},
                       {"type": "pointerDown"}, {"type": "pause", "duration": LONG_PRESS_MS}, {"type": "pointerUp"}],
                      deadline)
        self._frame_dispatched()

    def _touch(self, start, deadline, *, hold_ms=0, end=None, move_ms=0):
        """One complete contact. WDA cannot interrupt it after dispatch; never split or replay it."""
        steps = [{"type": "pointerMove", "duration": 0, "x": start[0], "y": start[1]}, {"type": "pointerDown"}]
        if hold_ms:
            steps.append({"type": "pause", "duration": hold_ms})
        if end is not None:
            if hold_ms:
                # WDA's pause advances time without adding a native path point. Anchor the pickup
                # location at its end, or XCTest interpolates movement across the hold as well.
                steps.append({"type": "pointerMove", "duration": 0, "x": start[0], "y": start[1]})
            steps.append({"type": "pointerMove", "duration": move_ms, "x": end[0], "y": end[1]})
        steps.append({"type": "pointerUp"})
        self._pointer(steps, deadline)

    def hold_point(self, x, y, snapshot, *, duration=1.5, timeout=15):
        """Hold a screenshot point for 0.1 to 10 seconds, then release.

        The caller checks permission and cancellation before dispatch; an active gesture cannot be interrupted.
        """
        point = _screen_point(snapshot, x, y)
        if not _in_range(duration, .1, 10):
            raise ValueError("hold duration must be between 0.1 and 10 seconds")
        deadline = Deadline(timeout)
        self._check_current(snapshot, deadline)
        self._pages_stale = True
        self._dispatching()
        self._frame_mark("HOLD_POINT")
        self._touch(point, deadline, hold_ms=round(duration * 1000))
        self._frame_dispatched()

    def flick_point(self, start_x, start_y, end_x, end_y, snapshot, *, duration=.12, timeout=10):
        """A quick directional movement with immediate release; no pickup hold or end pause."""
        start = _screen_point(snapshot, start_x, start_y)
        end = _screen_point(snapshot, end_x, end_y)
        if not _in_range(duration, .08, .3):
            raise ValueError("flick duration must be between 0.08 and 0.3 seconds")
        if start == end:
            raise ValueError("flick endpoints must occupy different screen points")
        deadline = Deadline(timeout)
        self._check_current(snapshot, deadline)
        self._pages_stale = True
        self._dispatching()
        self._frame_mark("FLICK_POINT")
        self._touch(start, deadline, end=end, move_ms=round(duration * 1000))
        self._frame_dispatched()

    def drag_point(self, start_x, start_y, end_x, end_y, snapshot, *, duration=1.0, hold_seconds=.1, timeout=10):
        """Drag between screen fractions from one observation; duration is movement time in seconds.

        hold_seconds is the initial pickup hold, independent of movement time. The caller owns screenshot
        grounding and permission checks for both endpoints. Dispatch once: a failed response may still mean the
        phone moved, so it must never trigger a gesture retry.
        """
        start = _screen_point(snapshot, start_x, start_y)
        end = _screen_point(snapshot, end_x, end_y)
        if not _in_range(duration, .1, 2):
            raise ValueError("drag duration must be between 0.1 and 2 seconds")
        if not _in_range(hold_seconds, 0, 2):
            raise ValueError("drag pickup hold must be between 0 and 2 seconds")
        if start == end:
            raise ValueError("drag endpoints must occupy different screen points")
        deadline = Deadline(timeout)
        self._check_current(snapshot, deadline)
        self._pages_stale = True
        self._dispatching()
        self._frame_mark("DRAG_POINT")
        self._touch(start, deadline, hold_ms=round(hold_seconds * 1000), end=end, move_ms=round(duration * 1000))
        self._frame_dispatched()

    def tap_point(self, x, y, snapshot, timeout=10, purpose=None):
        """A bounded tap at screen fractions (x, y), re-grounded when the observation aged. ``purpose`` only
        names the caller (tap_xy, dismiss) and changes nothing here."""
        x, y = _screen_point(snapshot, x, y)
        deadline = Deadline(timeout)
        self._check_current(snapshot, deadline)
        self._pages_stale = True
        self._tap(x, y, deadline)

    def _tap(self, x, y, deadline):
        self._dispatching()
        self._pointer([{"type": "pointerMove", "duration": 0, "x": round(x), "y": round(y)},
                       {"type": "pointerDown"}, {"type": "pause", "duration": WDA_TAP_HOLD_MS},
                       {"type": "pointerUp"}], deadline)

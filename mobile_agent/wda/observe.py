"""WebDriverAgent screen reads."""

import base64
import os
import threading
import time
from urllib.parse import quote

from ..state import PIXEL_SOURCES, _parse_wda, from_wda_root, pane_key, validate_bundle_id
from ..transport import Deadline, TransportError
from .gestures import WDA_GLIDE_MOMENTUM
from .guards import DriverRejection, last_change, unchanged_since

# Measured on the same phone (Settings, n=3 each): excluding `visible` and
# `accessible` cuts a source read from ~950 ms to ~120 ms. Visibility is then
# reconstructed geometrically by `state.wda_occlusion`.
WDA_SOURCE_PATH = "/source?format=xml&excluded_attributes=visible,accessible,index,traits"
# A snapshot this recent is the dispatch grounding; older ones are re-read and
# compared before a coordinate tap. The agent refreshes immediately before
# dispatch, so its taps never pay the extra read.
WDA_FRESH_SECONDS = 1.0
# How long WDA's answer about which stacked list pane is hidden is reused.
WDA_PANE_VISIBILITY_SECONDS = 20
# Which stacked tab page shows is reused this long at most (it is re-asked after a tab-bar tap).
WDA_PAGE_VISIBILITY_SECONDS = 120
# Layers change with every presentation and dismissal: their answer is reused only briefly.
WDA_LAYER_VISIBILITY_SECONDS = 3
# A settled read taken this recently is reused as the next step's observation.
WDA_STABLE_REUSE_SECONDS = .5
# Settle polling issues reads back to back. A settle read is started only with this much deadline left: reads take
# 130-210 ms on device, and a read cut off by its deadline surfaced as a
# TransportError that ended two live runs right after a swipe.
WDA_READ_MARGIN_SECONDS = .35
# A tree read that runs this long is too slow for a step to wait on. The step reads its screen from a screenshot and
# Apple Vision instead (WDA._observe_pixels), and the app is remembered as slow for WDA_SLOW_TREE_SECONDS, so its
# next reads send no /source at all. Measured 2026-10-14 and 15 (iPhone 15 Pro, iOS 26.0.1, TikTok's feed in front):
# /source took 29.8 s for 240 KB and /wda/activeAppInfo 17-50 s, then WDA died; a screenshot took 0.3 s and
# /wda/apps/state 7 ms. WDA runs one request at a time and goes on with one the client gave up on, so a read dropped
# at 5 s kept it busy for 25 s more, and the state, screenshot and Home requests behind it waited; every further
# /source added another 30 s. That is why the app is marked at the first read that runs out, and why a slow app's
# screens never fall back to the tree. Normal reads take 0.13-0.21 s, Safari's up to 2.8 s.
WDA_TREE_BUDGET_SECONDS = 4.0
# A read of a caller whose own deadline is shorter than this proves nothing: only a read that was given at least this
# long and used all of it counts as too slow.
WDA_TREE_MIN_SECONDS = 3.5
# The hint retry after a failed read (the Siri overlay; see _source_xml) reads in 0.6 s when it works.
WDA_TREE_RETRY_SECONDS = 2.0
WDA_SLOW_TREE_SECONDS = 3600
# POST /wda/apps/state answers in milliseconds when anything does.
WDA_STATE_SECONDS = 2.0
# A screen read from pixels: the screenshot takes 0.3 s and Vision's own limit is 3 s (screen_text.RUN_SECONDS).
WDA_PIXEL_READ_SECONDS = 8.0
# The source of a screen read from pixels. It joins state.PIXEL_SOURCES, so the safety rules for such screens apply.
WDA_PIXEL_SOURCE = "wda_pixels"
PIXEL_SOURCES.add(WDA_PIXEL_SOURCE)
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
# FrameClock settle (frame_clock.py): see WDA.wait_for_change.
# The pixel rule's "at rest": nothing moved for this long (frame_clock.STABLE_SECONDS).
WDA_FRAME_STABLE_SECONDS = .18
# With the pixels at rest, AX must still agree with itself this long (instead
# of WDA_SETTLE_QUIET_SECONDS). Measured: after a pop to the Settings root, the
# glass search bar keeps morphing by ~1 pt for ~0.5-0.8 s with no tile-level
# pixel change, and AX passes through states that last up to ~270 ms (two
# agreeing reads); accepting two agreeing reads alone was premature 1 in 20.
WDA_FRAME_AX_QUIET_SECONDS = .3
# MOBSTER_TREE_READS=still (under measurement, mobile_agent/docs/running.md): observe_ready takes its first read as the
# screen at rest,
# with no agreeing read and no quiet period, when the stream shows not one changed byte from this long before the read
# began and the last change came after the last dispatch (an effect that has not reached the stream yet is no rest).
# The AX flicker measured after pushes (research/frameclock-2026-09-23.md) happens while the bytes still change.
WDA_AT_REST_LEAD_SECONDS = WDA_FRAME_AX_QUIET_SECONDS
# wait_for_change(single_read=True): how long to wait for the action's first pixel change before
# settling from AX reads alone. The watch is marked before dispatch, so a tap's or a keystroke's
# effect has usually arrived by the time the dispatch returns.
WDA_ONE_READ_EFFECT_SECONDS = .35
# MOBSTER_SETTLE=launch (under measurement, mobile_agent/docs/running.md): an app launch settles on one read when the
# effect is on the stream and the pixels have rested for WDA_FRAME_STABLE_SECONDS as the launch returns (458 of 1,441
# recorded launch settles, on simulators). Where the zoom is still running as activate returns, nothing changes.

# The frontier policy's options (WDA.tune): Jev and the plain driver keep every default above.
# Its source read keeps the traits ("Selected"): no measurable cost on the simulator (90 ms with or
# without, 26 Sep). ``accessible`` stays out: it tripled the read (0.14 -> 0.35 s).
WDA_RICH_SOURCE_PATH = "/source?format=xml&excluded_attributes=visible,accessible,index"
# Settle reads are spaced at least this far apart (start to start). Back-to-back reads every 80-90 ms during
# settle ("Find the Application 'com.apple.Preferences'" bursts) preceded runner restarts on the phone
# (quality baseline B4, 5 Oct); a read takes 130-210 ms there, so this mostly spaces the fast ones.
WDA_SETTLE_POLL_SECONDS = .25
# Screenshot timeouts (a /screenshot that times out, or the MJPEG stream stalling this long) after which the
# driver settles from AX reads alone for the next AX_ONLY_STEPS steps: XCTest screenshots block WDA's main
# queue in GPU-heavy apps (Maps, Spotify: "Cannot take a screenshot within 20000 ms", B4).
WDA_SCREENSHOT_TIMEOUTS_FOR_AX_ONLY = 2
WDA_AX_ONLY_STEPS = 5


class TreeTooSlow(TransportError):
    """A tree read that used its whole WDA_TREE_BUDGET_SECONDS; it changed nothing, so the step reads pixels instead."""


W3C_ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"


def element_id(element):
    """A WDA element reference's id (JSONWP or W3C key), or None."""
    identifier = element.get("ELEMENT") or element.get(W3C_ELEMENT_KEY)
    return identifier if isinstance(identifier, str) and identifier else None


class WDAObserve:
    """The reading part of WDA."""
    # Apps whose tree read used the whole WDA_TREE_BUDGET_SECONDS, per WDA URL: {bundle: when (monotonic)}. Their
    # screens come from pixels (_observe_pixels) until WDA_SLOW_TREE_SECONDS pass, then the tree is tried once more.
    _slow_trees = {}
    # bytes of an image -> rows of text Vision read ({"text", "confidence", "rect"}) or None; None here means
    # screen_text.read_image (the screen-text helper on this Mac). Tests set their own.
    ocr = None
    # Optional FrameClock (frame_clock.attach_frame_clock).
    frame_clock = None
    # Source reads and their seconds, for callers' timing reports.
    source_reads, source_seconds = 0, 0.0
    # wait_for_change accepts single_read; settled_by says how the last settle ended ("one_read":
    # a single read, not yet proven by a second one; "agreeing_reads": two agreeing reads, before the
    # quiet period that proves them).
    settles_on_one_read = True
    settles_early = True
    settled_by = None
    predicted_settle = None  # MOBSTER_EXPERIMENTAL_SETTLE: the last settle's prediction and outcome (settle/predict.py)
    # Steps left of AX-only settling after repeated screenshot timeouts (see note_screenshot_timeout).
    ax_only_steps = 0
    screenshot_timeouts = 0

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
        pixels = self._slow_screen(deadline)
        if pixels is not None:
            return pixels
        try:
            xml = self._source_xml(source, deadline)
        except TreeTooSlow as slow:
            # One budget, one fallback: the app is slow from now on, this screen comes from the screenshot, and no
            # later read of the app sends /source.
            candidates = self._front_candidates()
            if candidates and self._pixel_reader() is not None:
                self.mark_tree_slow(candidates[0])
                pixels = self._slow_screen(deadline)
                if pixels is not None:
                    return pixels
            raise TransportError(str(slow)) from None
        if not isinstance(xml, str):
            raise ValueError("WDA did not return XML")
        root = _parse_wda(xml)
        rich = self.rich_rows
        result = from_wda_root(root, rich=rich)
        if result.stacked_panes or result.stacked_layers or result.stacked_pages:
            hidden = self._hidden_panes(result.stacked_panes, deadline) if result.stacked_panes else frozenset()
            layers = self._hidden_layers(result.stacked_layers, deadline) if result.stacked_layers else frozenset()
            sheets = frozenset(layers)  # screens a presented sheet covers (hidden pages are other tabs)
            if result.stacked_pages:
                layers |= self._hidden_pages(result.stacked_pages, deadline, result.bundle_id)
            if hidden or layers:
                full = result
                result = from_wda_root(root, hidden, layers, rich=rich)  # the same tree, not parsed again
                if sheets:
                    result.behind = tuple(e for e in full.elements
                                          if any(e.locator == p or e.locator.startswith(p + "/") for p in sheets))
        self._last_bundle = result.bundle_id or self._last_bundle
        if self._detection_point_pending:
            # configure() could not read the window size; the app frame is the screen.
            try:
                self._set_detection_point(result.width, result.height, min(2, deadline.remaining()))
            except (TransportError, TimeoutError, ValueError):
                pass  # Best effort; the next observation tries again.
        deadline.remaining()
        if self._frame_followup is not None:
            self._frame_follow_up(result)
        return result

    def _front_candidates(self):
        """The apps that may be in front, most likely first: the one a caller brought forward,
        then the last one read."""
        found = []
        for bundle in (self._foreground_hint, self._last_bundle):
            if bundle and bundle not in found:
                found.append(bundle)
        return found

    def tree_is_slow(self, bundle):
        marked = self._slow_trees.get(self.url, {}).get(bundle)
        return marked is not None and time.monotonic() - marked < WDA_SLOW_TREE_SECONDS

    def mark_tree_slow(self, bundle):
        self._slow_trees.setdefault(self.url, {})[bundle] = time.monotonic()

    def _source_xml(self, source, deadline):
        """The tree's XML within WDA_TREE_BUDGET_SECONDS: only a read that used the whole budget raises TreeTooSlow."""
        started = time.monotonic()
        limit = min(WDA_TREE_BUDGET_SECONDS, deadline.remaining())
        try:
            return self.call("GET", source, timeout=limit)
        except TransportError as error:
            failure = error
        spent = time.monotonic() - started
        out_of_budget = limit >= WDA_TREE_MIN_SECONDS and spent >= limit - .1
        # Measured 2026-09-23 (iOS 26.0.1): with Safari in front, WDA resolved
        # the "active application" to an invisible system overlay
        # (com.apple.siri.IntelligentLight) and every source read failed
        # after ~8 s ("stale element", kAXErrorServerNotFound). Naming the
        # app we brought forward as WDA's default active application
        # read the same screen in 0.58 s. A read changes nothing, so one
        # retry is safe. The hint then stays for this session: while it is
        # set WDA prefers that app when several are active.
        hint = self._foreground_hint or self._last_bundle
        left = deadline.end - time.monotonic()
        # A read that ran out of the budget with the hint already in force is a slow app, not the wrong app.
        if not hint or left < WDA_READ_MARGIN_SECONDS or (out_of_budget and self._hinted == hint):
            if out_of_budget:
                raise TreeTooSlow(f"the tree read used the whole {WDA_TREE_BUDGET_SECONDS:g} s budget") from None
            raise failure
        try:
            # Re-applied on every failed read: another client of the same session (a
            # harness probe, the app's video) can put detection back on "auto".
            self._hint_app(hint, min(2, deadline.remaining()))
            xml = self.call("GET", source, timeout=min(WDA_TREE_RETRY_SECONDS, deadline.remaining()))
        except (TransportError, TimeoutError):
            if out_of_budget:
                raise TreeTooSlow(f"the tree read used the whole {WDA_TREE_BUDGET_SECONDS:g} s budget") from None
            raise
        self._overlay_apps.setdefault(self.url, set()).add(hint)
        self._save_overlay_hints()
        return xml

    def in_front(self, bundle, timeout=WDA_STATE_SECONDS):
        """Whether ``bundle`` runs in the foreground, from POST /wda/apps/state (state 4): True or False, or None when
        WDA does not answer in ``timeout``. Measured 2026-10-14 with TikTok's feed in front: 7 ms, where
        /wda/activeAppInfo took 17-50 s. Sheets over the app (a permission prompt) do not change it: it says the app
        is in front, not that nothing covers it."""
        try:
            state = self.call("POST", "/wda/apps/state", {"bundleId": validate_bundle_id(bundle)}, timeout)
        except (TransportError, TimeoutError, ValueError):
            return None
        return state == 4 if type(state) is int else None

    def slow_front(self, timeout=WDA_STATE_SECONDS):
        """The app in front when its tree is remembered as slow (confirmed by in_front), else None: for those apps
        /wda/activeAppInfo takes as long as the tree, so the question is never put to it. Raises TransportError when
        WDA does not say: a request sent to a busy WDA only lengthens its queue."""
        for bundle in self._front_candidates():
            if self.tree_is_slow(bundle):
                front = self.in_front(bundle, timeout)
                if front is None:
                    raise TransportError(f"WDA did not say whether {bundle} is in front; it may still be busy with "
                                         "that app's tree")
                if front:
                    return bundle
        return None

    @staticmethod
    def _pixel_reader():
        """The harness's ``pixel_screen`` reader, or None: the engine alone has none and keeps the tree's error."""
        from .. import harness_api
        return harness_api.hooks().pixel_screen()

    def _slow_screen(self, deadline):
        """The screen in front read from pixels when it is an app remembered as slow, else None."""
        for bundle in self._front_candidates():
            if self.tree_is_slow(bundle):
                snapshot = self._observe_pixels(bundle, deadline)
                if snapshot is not None:
                    return snapshot
        return None

    def _observe_pixels(self, bundle, deadline):
        """The screen in front read from a screenshot and Apple Vision: a Snapshot with source ``wda_pixels`` (the
        safety rules for screens read from pixels apply). None when ``bundle`` is not in front (POST
        /wda/apps/state), the phone's size is unknown or the harness has no pixel reader. Raises TransportError when
        WDA does not answer or the picture can not be read: the caller never falls back to /source for an app that
        is too slow, because each such read keeps WDA busy for another 30 s. Reads only: it never taps."""
        pixels = self._pixel_reader()
        if pixels is None:
            return None
        started = time.monotonic()
        front = self.in_front(bundle, min(deadline.remaining(), WDA_PIXEL_READ_SECONDS))
        if front is None:
            raise TransportError(f"WDA did not say whether {bundle} is in front; it may still be busy with that "
                                 "app's tree")
        size = self._screen_sizes.get(self.url)
        if not front or not size:
            return None
        image = self.call("GET", "/screenshot", timeout=deadline.remaining())
        try:
            snapshot = pixels.wda_snapshot(base64.b64decode(image), size=size, bundle=bundle, source=WDA_PIXEL_SOURCE,
                                           started=started, ocr=self.ocr) if isinstance(image, str) and image else None
        except ValueError:
            snapshot = None
        if snapshot is None:
            raise TransportError(f"the screen of {bundle} could not be read from a screenshot")
        self._last_bundle = bundle
        return snapshot

    def pixel_snapshot(self, mark=False):
        """The screen in front read from pixels, for an evidence read of an app whose tree is too slow."""
        candidates = self._front_candidates()
        if mark and candidates and self._pixel_reader() is not None:
            self.mark_tree_slow(candidates[0])
        return self._slow_screen(Deadline(WDA_PIXEL_READ_SECONDS))

    def _hidden_panes(self, stacked, deadline):
        """Which stacked list panes WDA reports not visible; a failure leaves the order rule in charge."""
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
                    identifier = element_id(element)
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
        """Which window layers behind the front one WDA reports not visible (~0.3 s); a failure hides nothing."""
        key = ("layers", frozenset(paths))
        cached = self._pane_visibility.get(key)
        # Not across an action: SplitPay's Confirm Payment sheet hid the feed behind it, and the first read after its
        # Pay button reused that answer while the closing sheet's container still stacked the same layers. Only the
        # keyboard was left on screen, and the payment's feed row was never read (iOSWorld splitpay-001, 11 Oct).
        if (cached is not None and cached[1] > self._dispatched_at
                and time.monotonic() - cached[1] < WDA_LAYER_VISIBILITY_SECONDS):
            return cached[0]
        hidden = set()
        try:
            for path in paths:
                chain = "/".join(path.strip("/").split("/")[1:])
                found = self.call("POST", "/elements", {"using": "class chain", "value": chain},
                                  min(3, deadline.remaining()))
                if not isinstance(found, list) or len(found) != 1:
                    continue
                identifier = element_id(found[0])
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
        """Source paths of same-frame sibling pages WDA reports not visible; any failure hides nothing.
        SwiftUI keeps every TabView tab in the tree: multi-087 tapped a row of a hidden tab for 16 turns (24 Sep)."""
        key = ("pages", bundle, frozenset(groups))
        cached = self._pane_visibility.get(key)
        # The question costs ~0.8 s a page on QuickChat (5 s a read when asked after every action: settles
        # went 0.75 -> 1.9 s a turn, 25 Sep), and only a tap outside the pages (the tab bar), an app switch
        # or HOME changes the answer: it is kept until one of those, or WDA_PAGE_VISIBILITY_SECONDS.
        if (cached is not None and not self._pages_stale
                and time.monotonic() - cached[1] < WDA_PAGE_VISIBILITY_SECONDS):
            return cached[0]
        self._pages_stale = False
        self._page_groups = tuple(groups)
        hidden = set()
        try:
            for parent, children in groups:
                chain = "/".join(parent.strip("/").split("/")[1:])
                found = self.call("POST", "/elements",
                                  {"using": "class chain",
                                   "value": (chain + "/" if chain else "") + "XCUIElementTypeOther[`visible == 1`]"},
                                  min(3, deadline.remaining()))
                shown = set()
                for element in (found if isinstance(found, list) else [])[:8]:
                    identifier = element_id(element)
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
        groups = self._page_groups
        if not groups:
            return
        if not locator or any(locator.startswith(parent + "/") and not any(
                locator == page or locator.startswith(page + "/") for _, page in pages)
                for parent, pages in groups):
            self._pages_stale = True

    def _dispatching(self):
        """An action is about to be sent: the screen is no longer the settled one, and its window layers may change."""
        self._stable = None
        self._dispatched_at = time.monotonic()

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
        video) returns its latest read after the settle cap. Each call that reads
        is logged to the FrameClock ("ready": seconds, reads, and which rule ended it)."""
        started, reads = time.monotonic(), self.source_reads
        snapshot, rule, first, rest = self._ready_read(timeout)
        self._ready_report(started, reads, snapshot, rule, first, rest)
        return snapshot

    def _ready_read(self, timeout):
        """(the settled read, the rule that accepted it, the first read, whether the stream showed that read at
        rest). The rule is reuse, memory, rest, agreement, pixels or cap."""
        stable = self._stable
        if stable is not None and time.monotonic() - stable.captured_at <= WDA_STABLE_REUSE_SECONDS:
            return stable, "reuse", None, False
        deadline = Deadline(timeout)
        cap = time.monotonic() + min(WDA_SETTLE_CAP_SECONDS, timeout)
        since = time.monotonic()
        previous = first = self.observe(timeout=deadline.remaining())
        if self._known_at_rest(previous):
            return self._prove_settled(previous), "memory", first, False
        rest = self._at_rest(previous)
        if rest and os.environ.get("MOBSTER_TREE_READS", "").strip().casefold() == "still":
            return self._prove_settled(previous), "rest", first, rest
        clock = self._frame_clock()
        previous_started = since
        while time.monotonic() < cap:
            self._poll_pause(previous_started, cap)
            if time.monotonic() >= cap:
                break
            started = time.monotonic()
            current = self.observe(timeout=deadline.remaining())
            if current.content_fingerprint != previous.content_fingerprint:
                since = started
            elif time.monotonic() - since >= WDA_SETTLE_QUIET_SECONDS:
                return self._prove_settled(current), "agreement", first, rest
            elif (clock is not None and current.elements
                    and time.monotonic() - since >= WDA_FRAME_AX_QUIET_SECONDS):
                # Agreeing reads for WDA_FRAME_AX_QUIET_SECONDS, and the whole screen
                # (status bar aside, no masked regions) pixel-still from
                # WDA_FRAME_STABLE_SECONDS before the last two reads until now.
                still = clock.still_for(exact=False)  # AX agreement (above) covers sub-tile changes
                if still is not None and time.monotonic() - still <= previous_started - WDA_FRAME_STABLE_SECONDS:
                    return self._prove_settled(current), "pixels", first, rest
            previous, previous_started = current, started
        return previous, "cap", first, rest

    def _at_rest(self, read):
        """Whether the stream shows ``read`` at rest: not one changed byte from WDA_AT_REST_LEAD_SECONDS before the
        read began until now, and that stillness began after the last dispatch. An empty screen never settles on
        pixels."""
        if not read.elements:
            return False
        changed = last_change(self._frame_clock())
        return changed is not None and self._dispatched_at <= changed <= read.read_started - WDA_AT_REST_LEAD_SECONDS

    def _ready_report(self, started, reads, snapshot, rule, first, rest):
        """One "ready" row: the rule, seconds and reads, and, with or without the switch, ``would_rest`` (the stream
        showed the first read at rest) and ``first_held`` (the screen accepted is the first read): their
        agreement in a run without the switch is the rule's false-rest rate."""
        clock = self.frame_clock
        if clock is None:
            return
        row = {"kind": "ready", "rule": rule, "ms": round((time.monotonic() - started) * 1000, 1),
               "reads": self.source_reads - reads}
        if first is not None:
            row["would_rest"], row["first_held"] = rest, snapshot.content_fingerprint == first.content_fingerprint
        try:
            clock.record(row)
        except Exception:
            pass

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
        above, unchanged.

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

        ``single_read="rest"`` (MOBSTER_SETTLE=launch): as ``single_read``, but only when the effect was seen
        and the pixels already rest, without waiting for either; else the AX loop runs as for no
        ``single_read``. Every settle's log row says ``miss``: why the one-read settle was not taken.
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
        self.settled_by = self.predicted_settle = None
        if single_read == "rest" and os.environ.get("MOBSTER_SETTLE", "").strip().casefold() != "launch":
            single_read = False
        try:
            accept = None
            if clock is not None and watch is not None and watch.usable:
                info = {}
                read, info["miss"] = (self._one_read_settle(snapshot, clock, watch, deadline, cap, reads,
                                                            rest_only=single_read == "rest")
                                      if single_read else (None, "not_requested"))
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
            elif clock is not None and watch is not None:
                info = {"fallback": watch.reason or "unusable"}
            if (single_read or early) and clock is not None and watch is not None:
                info = {} if info is None else info
                from ..settle.predict import predicted_settle  # here: a start never loads the dark settle package
                read = predicted_settle(self, snapshot, action, watch, clock, deadline, started, cap, reads, info)
                if read is not None:  # unproven, like one_read: the caller's verify read proves it
                    info["accepted_by"], self.settled_by = "predicted", "predicted"
                    self._frame_report(clock, watch, action, snapshot, read, started, reads, info)
                    return read
            result = self._ax_settle(snapshot, deadline, started, started + wait_seconds, cap, settling, reads,
                                     accept, early=early)
            if clock is not None and watch is not None:
                self._frame_report(clock, watch, action, snapshot, result, started, reads, info)
            return result
        finally:
            if clock is not None and watch is not None:
                clock.release(watch)

    def _one_read_settle(self, snapshot, clock, watch, deadline, cap, reads, rest_only=False):
        """(a changed read taken after the pixels moved and came to rest, during which nothing moved, None), or
        (None, why not): no_effect (the pixels never moved), not_stable (never rested), read_failed, unchanged
        (the read shows ``snapshot``) or moved (the pixels moved during the read). ``rest_only``: the effect
        and the rest must already be there, with no waiting for either."""
        effect_wait = 0.0 if rest_only else max(0.0, min(WDA_ONE_READ_EFFECT_SECONDS, cap - time.monotonic()))
        if clock.wait_effect(watch, effect_wait) is None:
            return None, "no_effect"
        if clock.wait_stable(watch, 0.0 if rest_only else max(0.0, cap - time.monotonic())) is None:
            return None, "not_stable"
        read_started = time.monotonic()
        try:
            current = self.observe(timeout=deadline.remaining())
        except (TransportError, TimeoutError):
            return None, "read_failed"
        now = time.monotonic()
        reads.append((read_started, now, current.content_fingerprint))
        if current.content_fingerprint == snapshot.content_fingerprint:
            return None, "unchanged"
        if not clock.quiet_between(watch, read_started - WDA_FRAME_STABLE_SECONDS, now):
            return None, "moved"
        return current, None

    def _ax_settle(self, snapshot, deadline, started, quiet_end, cap, settling, reads, accept=None, early=False):
        """The AX-only settle loop (see wait_for_change)."""
        before = snapshot.content_fingerprint
        previous, since = None, started
        current = snapshot
        last_read = None
        while True:
            if last_read is not None:
                self._poll_pause(last_read, cap)
                now = time.monotonic()
                if now >= cap or deadline.end - now <= WDA_READ_MARGIN_SECONDS:
                    return current
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

    def _frame_clock(self):
        """The attached FrameClock when active, else None."""
        clock = self.frame_clock
        if clock is None or self.ax_only_steps > 0:
            return None
        return clock

    def note_screenshot_timeout(self):
        """A screenshot timed out or the stream stalled; two in a row settle on AX reads for WDA_AX_ONLY_STEPS."""
        self.screenshot_timeouts += 1
        if self.screenshot_timeouts >= WDA_SCREENSHOT_TIMEOUTS_FOR_AX_ONLY:
            self.screenshot_timeouts = 0
            started = self.ax_only_steps == 0
            self.ax_only_steps = WDA_AX_ONLY_STEPS
            return started
        return False

    def next_step(self):
        """A new agent step: counts down AX-only settling and counts the FrameClock's stalls as screenshot timeouts."""
        clock = self.frame_clock
        stalls = getattr(clock, "stalls", 0) if clock is not None else 0
        seen = self._stalls_seen
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
        """Log one settle: what happened, and which rule settled it."""
        try:
            before = snapshot.content_fingerprint
            ended = time.monotonic()
            entry = {"kind": "settle",
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
            from ..settle.predict import annotate as annotate_settle
            annotate_settle(self, entry, action, snapshot, result, started, reads, info)
            if watch.stable_at is not None:
                entry["stable_ms"] = round((watch.stable_at - started) * 1000, 1)
            if info is not None and info.get("miss"):
                entry["miss"] = info["miss"]
            if info is not None and info.get("accepted_by"):
                entry["path"] = info["accepted_by"] if info["accepted_by"] in ("one_read", "predicted") else "frame"
            elif info is not None and info.get("fallback"):
                entry["path"], entry["fallback"] = "frame_fallback", info["fallback"]
            else:
                entry["path"] = "ax"
            fc_fingerprint = result.content_fingerprint if entry["path"] in ("frame", "one_read") else None
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
                        from .. import harness_api
                        frame_clock = harness_api.hooks().frame_clock()  # only a run with a frame clock gets here
                        changed_tiles, displacement, tile_grid = (frame_clock.changed_tiles, frame_clock.displacement,
                                                                  frame_clock.tile_grid)
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
                                self._momentum_apps.setdefault(url, set()).add(bundle)
                                entry["demoted_glide"] = True
                    except Exception:
                        pass
                    clock.record(entry)
                threading.Thread(target=measure, name="mobster-odometry", daemon=True).start()
            self._frame_followup = {"at": ended, "result": result.content_fingerprint, "fc": fc_fingerprint,
                                    "op": entry["op"], "path": entry["path"], "read_started": result.read_started}
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
            # stream_still: not one byte changed from the accepted read's start to this read's end, so this read
            # would have been redundant (the share of next reads that disagree,
            # split by it, decides MOBSTER_TREE_READS).
            entry = {"kind": "next_read", "op": followup["op"],
                     "path": followup["path"],
                     "age_ms": round((time.monotonic() - followup["at"]) * 1000, 1),
                     "matches_result": read.content_fingerprint == followup["result"],
                     "stream_still": unchanged_since(clock, followup["read_started"])}
            if followup["fc"] is not None:
                entry["matches_fc"] = read.content_fingerprint == followup["fc"]
            clock.record(entry)
        except Exception:
            pass

    def _check_current(self, snapshot, deadline):
        """Re-ground an aging snapshot before a coordinate tap; fresh ones pass.

        A change is refused before dispatch, which the agent treats as an
        ordinary re-observe rather than an action of unknown outcome.
        """
        if time.monotonic() - snapshot.captured_at <= WDA_FRESH_SECONDS:
            return
        if self.observe(timeout=deadline.remaining()).fingerprint != snapshot.fingerprint:
            raise DriverRejection("stale_revision")

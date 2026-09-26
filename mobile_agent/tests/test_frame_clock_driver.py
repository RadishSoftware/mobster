"""WDA settle with a FrameClock: pixel-proven settle, fallbacks, shadow logging,
and fast drags. Offline, with a scripted clock."""

import os
import threading
import time
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent.drivers import WDA
from mobile_agent.state import from_wda
from mobile_agent.tests.timing import bound, slow_machine

SETTINGS = "com.apple.Preferences"


def app(label, bundle=SETTINGS, web=False):
    inner = f'<XCUIElementTypeButton label="{label}" x="0" y="200" width="100" height="40"/>'
    if web:
        inner = f'<XCUIElementTypeWebView x="0" y="0" width="400" height="800">{inner}</XCUIElementTypeWebView>'
    return (f'<XCUIElementTypeApplication width="400" height="800" bundleId="{bundle}">'
            f"{inner}</XCUIElementTypeApplication>")


class Clock:
    def __init__(self, per_read=.2):
        self.now, self.per_read = 1000.0, per_read

    def __call__(self):
        return self.now


class Device:
    """Fake WDA: /source returns scripted screens, each read taking ``per_read``."""

    def __init__(self, clock, *xmls):
        self.clock, self.xmls, self.reads, self.calls = clock, list(xmls), 0, []

    def __call__(self, method, path, body=None, timeout=10):
        self.calls.append((method, path, body))
        if path.startswith("/source"):
            self.clock.now += self.clock.per_read
            self.reads += 1
            return self.xmls.pop(0) if len(self.xmls) > 1 else self.xmls[0]
        return None


class FakeFrameClock:
    """Scripted answers to the questions the driver asks.

    ``quiet`` is what ``quiet_between`` answers once an effect was seen
    (``effect``); a list is consumed one answer per call.
    """

    def __init__(self, clock, *, effect=True, quiet=True, still=None, healthy=True,
                 usable=True, motion_after=None, stable=True):
        self.clock = clock
        self.effect, self.quiet, self.stable = effect, quiet, stable
        self.still, self.is_healthy, self.usable = still, healthy, usable
        self.motion_after = motion_after
        self.records, self.released, self.closed, self.watches = [], 0, False, []

    def mark(self):
        watch = SimpleNamespace(usable=self.usable, reason="" if self.usable else "busy_screen",
                                t=self.clock.now, effect=None, effect_tiles=0, stable_at=None,
                                moves=deque(), ref=None, release_t=None, release_frame=None)
        if self.effect and self.usable:
            watch.effect, watch.effect_tiles = SimpleNamespace(t=self.clock.now), 5
        self.watches.append(watch)
        return watch

    def quiet_between(self, watch, start, end, min_frames=3):
        if not watch.usable or watch.effect is None:
            return False
        if watch.moves:  # a scripted timeline, as the real clock would see it
            window = [moved for t, moved in watch.moves if start < t <= end]
            return len(window) >= min_frames and not any(window)
        return self.quiet.pop(0) if isinstance(self.quiet, list) else self.quiet

    def healthy(self):
        return self.is_healthy

    def wait_effect(self, watch, timeout):
        return watch.effect if watch.usable else None

    def wait_stable(self, watch, timeout, duration=.18):
        return watch.effect if watch.usable and watch.effect is not None and self.stable else None

    def still_for(self, exact=True):
        return self.still

    def release(self, watch):
        self.released += 1

    def set_release(self, watch, t):
        watch.release_t = t

    def record(self, entry):
        self.records.append(entry)

    def stats(self):
        return {"fps": 30}

    def last_motion(self, watch):
        return None if self.motion_after is None else self.clock.now + self.motion_after

    def latest_frame(self):
        return None

    def close(self):
        self.closed = True


class FrameSettleTests(unittest.TestCase):
    """Reads take 0.2 s: the AX-only loop needs 3 reads (0.5 s quiet) for B, B, B and
    4 for B, C, C, C."""

    def setUp(self):
        self.clock = Clock()
        for target in ("mobile_agent.drivers.time.monotonic", "mobile_agent.transport.time.monotonic"):
            patcher = patch(target, self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("MOBSTER_GLIDE", "MOBSTER_GLIDE_BUNDLES"):
            os.environ.pop(name, None)
        WDA._momentum_apps.clear()
        self.addCleanup(WDA._momentum_apps.clear)

    def driver(self, *xmls, mode="on", **fake):
        driver = WDA("http://localhost:8100", "test")
        self.addCleanup(driver.close)
        driver.call = Device(self.clock, *xmls)
        driver.frame_clock = FakeFrameClock(self.clock, **fake)
        driver.frame_clock_mode = mode
        return driver

    def tap(self, driver, before):
        driver.execute("TAP", before.elements[0], before)
        return driver.wait_for_change(before, timeout=1.6, wait_seconds=.6)

    def test_two_agreeing_reads_with_still_pixels_settle(self):
        driver = self.driver(app("B"))
        before = from_wda(app("A"))
        after = self.tap(driver, before)
        self.assertEqual((after.elements[0].label, driver.call.reads), ("B", 2))
        self.assertIs(driver._stable, after)
        record = driver.frame_clock.records[-1]
        self.assertEqual((record["path"], record["op"], record["changed"]), ("frame", "TAP", True))
        self.assertEqual(driver.frame_clock.released, 1)

    def test_one_changed_read_is_never_enough(self):
        # An AX-only flicker (measured after pushes in iOS 26 Settings): B is
        # a transient state; the pixels are still throughout.
        driver = self.driver(app("B"), app("C"), app("B"), app("B"))
        after = self.tap(driver, from_wda(app("A")))
        self.assertEqual((after.elements[0].label, driver.call.reads), ("B", 4))

    def test_the_next_read_is_compared_with_the_accepted_screen(self):
        driver = self.driver(app("B"))
        self.tap(driver, from_wda(app("A")))
        driver.observe()
        follow = driver.frame_clock.records[-1]
        self.assertEqual(follow["kind"], "next_read")
        self.assertTrue(follow["matches_result"])
        self.assertTrue(follow["matches_fc"])

    def test_no_pixel_change_keeps_the_ax_quiet_period(self):
        driver = self.driver(app("B"), effect=False)
        after = self.tap(driver, from_wda(app("A")))
        self.assertEqual((after.elements[0].label, driver.call.reads), ("B", 3))
        self.assertEqual(driver.frame_clock.records[-1]["path"], "ax")

    def test_moving_pixels_keep_the_ax_quiet_period(self):
        driver = self.driver(app("B"), app("C"), app("C"), quiet=False)
        after = self.tap(driver, from_wda(app("A")))
        self.assertEqual((after.elements[0].label, driver.call.reads), ("C", 4))
        driver = self.driver(app("B"), app("C"), app("C"), quiet=[True])
        after = self.tap(driver, from_wda(app("A")))
        self.assertEqual((after.elements[0].label, driver.call.reads), ("C", 3))

    def test_unusable_token_falls_back(self):
        driver = self.driver(app("B"), app("C"), app("C"), usable=False)
        after = self.tap(driver, from_wda(app("A")))
        self.assertEqual((after.elements[0].label, driver.call.reads), ("C", 4))
        self.assertEqual(driver.frame_clock.records[-1]["fallback"], "busy_screen")

    def test_without_a_mark_the_ax_loop_runs_unchanged(self):
        driver = self.driver(app("B"), app("C"), app("C"))
        after = driver.wait_for_change(from_wda(app("A")), timeout=5)
        self.assertEqual((after.elements[0].label, driver.call.reads), ("C", 4))
        self.assertEqual(driver.frame_clock.records, [])

    def test_shadow_mode_behaves_exactly_like_ax_and_logs_the_pixel_decision(self):
        plain = WDA("http://localhost:8100", "test")
        self.addCleanup(plain.close)
        plain.call = Device(self.clock, app("B"), app("C"), app("C"))
        before = from_wda(app("A"))
        expected = plain.wait_for_change(before, timeout=5)
        expected_reads = plain.call.reads

        driver = self.driver(app("B"), app("C"), app("C"), mode="shadow")
        driver._frame_mark("TAP")
        watch = driver._frame_watch
        # The effect at dispatch, motion until +0.1 s, then still frames every 33 ms.
        watch.effect = SimpleNamespace(t=self.clock.now - .1)
        watch.moves.extend((self.clock.now + .1 + index / 30, index == 0) for index in range(40))
        after = driver.wait_for_change(before, timeout=5)
        self.assertEqual(after.content_fingerprint, expected.content_fingerprint)
        self.assertEqual(driver.call.reads, expected_reads)
        record = driver.frame_clock.records[-1]
        self.assertEqual(record["path"], "ax")
        # The third read (C agreeing with C, pixels still since +0.22 s) ends at
        # +0.6 s; AX alone needed a fourth, at +0.8 s.
        self.assertEqual(record["fc_ms"], 600.0)
        self.assertEqual(record["settle_ms"], 800.0)
        self.assertFalse(record["premature"])

    def test_shadow_mode_never_uses_pixels_to_accept(self):
        driver = self.driver(app("B"), mode="shadow")
        after = self.tap(driver, from_wda(app("A")))
        self.assertEqual((after.elements[0].label, driver.call.reads), ("B", 3))

    def test_observe_ready_accepts_two_agreeing_reads_after_pixel_quiet(self):
        driver = self.driver(app("B"), still=5.0)
        self.assertEqual(driver.observe_ready(timeout=5).elements[0].label, "B")
        self.assertEqual(driver.call.reads, 2)

    def test_observe_ready_keeps_the_quiet_period_when_pixels_moved(self):
        driver = self.driver(app("B"), still=.05)
        driver.observe_ready(timeout=5)
        self.assertEqual(driver.call.reads, 3)

    def test_observe_ready_ignores_pixels_in_shadow_mode(self):
        driver = self.driver(app("B"), mode="shadow", still=5.0)
        driver.observe_ready(timeout=5)
        self.assertEqual(driver.call.reads, 3)

    def test_empty_screens_never_settle_on_pixels(self):
        empty = '<XCUIElementTypeApplication width="400" height="800"/>'
        driver = self.driver(empty, still=5.0)
        driver.observe_ready(timeout=5)
        self.assertEqual(driver.call.reads, 3)

    def one_read_tap(self, driver, before):
        driver.execute("TAP", before.elements[0], before)
        return driver.wait_for_change(before, timeout=1.6, wait_seconds=.6, single_read=True)

    def test_single_read_settles_on_one_read_the_pixels_saw_at_rest(self):
        driver = self.driver(app("B"))
        after = self.one_read_tap(driver, from_wda(app("A")))
        self.assertEqual((after.elements[0].label, driver.call.reads), ("B", 1))
        self.assertEqual(driver.settled_by, "one_read")
        self.assertIsNone(driver._stable)  # not proven: observe_ready still reads
        self.assertEqual(driver.frame_clock.records[-1]["path"], "one_read")
        self.assertEqual(driver.frame_clock.released, 1)

    def test_single_read_falls_back_to_agreeing_reads(self):
        # No pixel change, pixels never at rest, motion during the read, or AX behind the pixels.
        for fake, screens in (({"effect": False}, [app("B")]), ({"stable": False}, [app("B"), app("C"), app("C")]),
                              ({"quiet": [False, False, True]}, [app("B"), app("C"), app("C")]),
                              ({}, [app("A"), app("C"), app("C")])):
            driver = self.driver(*screens, **fake)
            after = self.one_read_tap(driver, from_wda(app("A")))
            self.assertIsNone(driver.settled_by, fake)
            self.assertNotEqual(driver.frame_clock.records[-1]["path"], "one_read")
            if fake != {"effect": False}:
                self.assertEqual(after.elements[0].label, "C", fake)
                self.assertGreater(driver.call.reads, 1, fake)

    def test_single_read_needs_a_usable_mark(self):
        driver = self.driver(app("B"), app("C"), app("C"), usable=False)
        after = self.one_read_tap(driver, from_wda(app("A")))
        self.assertEqual((after.elements[0].label, driver.call.reads, driver.settled_by), ("C", 4, None))

    def test_reads_are_counted_and_stamped_with_their_start(self):
        driver = self.driver(app("B"))
        started = self.clock.now
        snapshot = driver.observe()
        self.assertEqual((driver.source_reads, snapshot.read_started), (1, started))
        self.assertAlmostEqual(driver.source_seconds, self.clock.per_read)

    def test_close_releases_the_clock(self):
        driver = self.driver(app("B"))
        fake = driver.frame_clock
        driver.close()
        self.assertTrue(fake.closed)
        self.assertIsNone(driver.frame_clock)


class GlideTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        patcher = patch("mobile_agent.drivers.time.monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("MOBSTER_GLIDE", "MOBSTER_GLIDE_BUNDLES"):
            os.environ.pop(name, None)
        WDA._momentum_apps.clear()
        self.addCleanup(WDA._momentum_apps.clear)

    def swipe(self, xml, mode="on", operation="SWIPE_UP", clock=True, **fake):
        driver = WDA("http://localhost:8100", "test")
        self.addCleanup(driver.close)
        driver.call = Device(self.clock, xml)
        if clock:
            driver.frame_clock = FakeFrameClock(self.clock, **fake)
            driver.frame_clock_mode = mode
        driver.execute(operation, None, from_wda(xml))
        paths = [path for _, path, _ in driver.call.calls]
        return driver, paths

    def test_measured_apps_glide_even_without_a_clock(self):
        driver, paths = self.swipe(app("A"), clock=False)
        self.assertEqual(paths, ["/actions"])
        actions = driver.call.calls[0][2]["actions"][0]["actions"]
        self.assertEqual([a["type"] for a in actions], ["pointerMove", "pointerDown"] + ["pointerMove"] * 4 + ["pointerUp"])
        self.assertNotIn("pause", [a["type"] for a in actions])  # a pause keeps the fling velocity
        ys = [a["y"] for a in actions if a["type"] == "pointerMove"]
        self.assertEqual((ys[0], ys[-1]), (640, 96))  # WDA_GLIDE_SPAN: .80 -> .12 of 800
        steps = [abs(b - a) / move["duration"] for a, b, move in zip(ys[1:], ys[2:], actions[3:])]
        self.assertLess(steps[-1], steps[0] / 20, "the glide decelerates before lifting")
        _, paths = self.swipe(app("A", bundle="com.apple.mobilesafari", web=True), clock=False)
        self.assertEqual(paths, ["/actions"])

    def test_unmeasured_apps_keep_the_legacy_drag(self):
        _, paths = self.swipe(app("A", bundle="com.example.feed"), mode="shadow")
        self.assertEqual(paths, ["/wda/dragfromtoforduration"])
        _, paths = self.swipe(app("A", bundle="com.example.feed"), mode="on", healthy=False)
        self.assertEqual(paths, ["/wda/dragfromtoforduration"])
        _, paths = self.swipe(app("A", bundle="com.example.feed"), mode="on")
        self.assertEqual(paths, ["/wda/dragfromtoforduration"])
        os.environ["MOBSTER_GLIDE_BUNDLES"] = "*"
        _, paths = self.swipe(app("A", bundle="com.example.feed"), clock=False)
        self.assertEqual(paths, ["/actions"])

    def test_horizontal_swipes_flick_and_the_flag_keeps_the_legacy_drag(self):
        _, paths = self.swipe(app("A"), operation="SWIPE_LEFT")
        self.assertEqual(paths, ["/actions"])
        os.environ["MOBSTER_FLICK"] = "off"
        self.addCleanup(os.environ.pop, "MOBSTER_FLICK", None)
        _, paths = self.swipe(app("A"), operation="SWIPE_LEFT")
        self.assertEqual(paths, ["/wda/dragfromtoforduration"])
        os.environ["MOBSTER_GLIDE"] = "0"
        _, paths = self.swipe(app("A"))
        self.assertEqual(paths, ["/wda/dragfromtoforduration"])

    def test_motion_after_release_demotes_the_app(self):
        from mobile_agent.frame_clock import Frame, decode
        from mobile_agent.tests.test_frame_clock import screen
        driver, paths = self.swipe(app("A"))
        self.assertEqual(paths, ["/actions"])
        watch = driver._frame_watch
        watch.ref = Frame(1, 0, decode(screen(0)))
        watch.release_frame = Frame(2, 0, decode(screen(600)))
        final = Frame(3, 0, decode(screen(900)))  # kept moving 300 px after release
        driver.frame_clock.latest_frame = lambda: final
        driver.call = Device(self.clock, app("B"), app("B"))
        driver.wait_for_change(from_wda(app("A")), timeout=1.6)
        deadline = time.monotonic() + 3
        while not driver.frame_clock.records and time.monotonic() < deadline:
            time.sleep(.01)
        record = driver.frame_clock.records[-1]
        self.assertTrue(record["demoted_glide"])
        self.assertGreater(record["after_release"]["dy"], .2)
        _, paths = self.swipe(app("A"))
        self.assertEqual(paths, ["/wda/dragfromtoforduration"])

    def test_a_glide_that_stopped_at_release_is_kept(self):
        from mobile_agent.frame_clock import Frame, decode
        from mobile_agent.tests.test_frame_clock import screen
        driver, _ = self.swipe(app("A"))
        watch = driver._frame_watch
        watch.ref = Frame(1, 0, decode(screen(0)))
        watch.release_frame = Frame(2, 0, decode(screen(600)))
        final = Frame(3, 0, decode(screen(600)))
        driver.frame_clock.latest_frame = lambda: final
        driver.call = Device(self.clock, app("B"), app("B"))
        driver.wait_for_change(from_wda(app("A")), timeout=1.6)
        deadline = time.monotonic() + 3
        while not driver.frame_clock.records and time.monotonic() < deadline:
            time.sleep(.01)
        record = driver.frame_clock.records[-1]
        self.assertNotIn("demoted_glide", record)
        self.assertAlmostEqual(record["displacement"]["dy"], 600 / 1278, delta=.01)
        _, paths = self.swipe(app("A"))
        self.assertEqual(paths, ["/actions"])


class SimulatedPhone:
    """A 30 fps MJPEG source plus a WDA fake whose AX follows the pixels.

    A tap starts a push transition ``glass_lag`` after dispatch, lasting
    ``transition`` seconds; AX reports the new screen ``ax_lag`` after the
    pixels settle. Reads take ``read_seconds`` of real time.
    """

    def __init__(self, transition=.35, glass_lag=.08, ax_lag=0.0, read_seconds=.12):
        from mobile_agent.tests.test_frame_clock import RelayVideo, WIDTH, screen
        self.video = RelayVideo()
        self.transition, self.glass_lag, self.ax_lag, self.read_seconds = transition, glass_lag, ax_lag, read_seconds
        self.before = screen()
        self.after = screen(title="General")
        self.sliding = [screen(title="General", shift_x=WIDTH - round(WIDTH * (step + 1) / 10)) for step in range(10)]
        self.tapped_at = None
        self.stop = False
        self.reads = 0
        self.thread = threading.Thread(target=self.publish, daemon=True)
        self.thread.start()

    def frame_at(self, now):
        if self.tapped_at is None or now < self.tapped_at + self.glass_lag:
            return self.before
        progress = (now - self.tapped_at - self.glass_lag) / self.transition
        if progress >= 1:
            return self.after
        return self.sliding[min(9, int(progress * 10))]

    def publish(self):
        sequence = 0
        while not self.stop:
            sequence += 1
            self.video.publish(sequence, self.frame_at(time.monotonic()))
            time.sleep(1 / 30)

    def __call__(self, method, path, body=None, timeout=10):
        if path == "/actions":
            time.sleep(.05)
            self.tapped_at = time.monotonic()
            time.sleep(.1)
            return None
        if path.startswith("/source"):
            self.reads += 1
            captured = time.monotonic()
            time.sleep(self.read_seconds)
            settled = (self.tapped_at is not None
                       and captured >= self.tapped_at + self.glass_lag + self.transition + self.ax_lag)
            return app("General" if settled else "Settings")
        return None

    def close(self):
        self.stop = True
        self.thread.join(1)


@unittest.skipIf(slow_machine(), "real threads race real MJPEG timing; which settle path wins is only "
                 "deterministic on an unloaded machine (they run locally)")
class EndToEndTests(unittest.TestCase):
    """Real FrameClock and driver threads against a simulated phone (real time, ~3 s)."""

    def run_tap(self, mode, **phone):
        from mobile_agent.frame_clock import FrameClock
        device = SimulatedPhone(**phone)
        self.addCleanup(device.close)
        driver = WDA("http://localhost:8100", "test")
        self.addCleanup(driver.close)
        driver.call = device
        driver.frame_clock = FrameClock(device.video)
        driver.frame_clock_mode = mode
        deadline = time.monotonic() + 3
        while not driver.frame_clock.healthy() and time.monotonic() < deadline:
            time.sleep(.02)
        time.sleep(.3)  # a still baseline before the action
        before = from_wda(app("Settings"))
        driver.execute("TAP", before.elements[0], before)
        started = time.monotonic()
        after = driver.wait_for_change(before, timeout=1.6, wait_seconds=.6)
        return driver, device, after, time.monotonic() - started

    def test_on_mode_settles_on_pixels_with_two_reads(self):
        driver, device, after, elapsed = self.run_tap("on")
        self.assertEqual(after.elements[0].label, "General")
        record = [r for r in driver.frame_clock.records if r["kind"] == "settle"][-1]
        self.assertEqual(record["path"], "frame")
        self.assertLess(elapsed, bound(.95))

    def test_ax_lagging_the_pixels_changes_nothing(self):
        # AX changes 0.33 s after the pixels settle: past the 0.6 s "unchanged"
        # window, where the AX-only loop also returns the unchanged read.
        driver, device, after, elapsed = self.run_tap("on", ax_lag=.33, read_seconds=.2)
        self.assertEqual(after.elements[0].label, "Settings")
        record = [r for r in driver.frame_clock.records if r["kind"] == "settle"][-1]
        self.assertEqual(record["path"], "ax")

    def test_shadow_mode_logs_a_faster_pixel_decision(self):
        driver, device, after, elapsed = self.run_tap("shadow")
        self.assertEqual(after.elements[0].label, "General")
        record = [r for r in driver.frame_clock.records if r["kind"] == "settle"][-1]
        self.assertEqual(record["path"], "ax")
        self.assertFalse(record["premature"])
        # Never later than the AX decision (a slow runner can make them tie to the 0.1 ms).
        self.assertLessEqual(record["fc_ms"], record["settle_ms"])


def page(offset_pt, bundle="com.apple.mobilesafari"):
    """A web page 800 pt tall per screen with labelled rows every 200 pt, scrolled by ``offset_pt``."""
    rows = "".join(f'<XCUIElementTypeStaticText label="Row {index}" x="0" y="{index * 200 - offset_pt}" '
                   f'width="300" height="40"/>' for index in range(30))
    return (f'<XCUIElementTypeApplication width="400" height="800" bundleId="{bundle}">'
            f'<XCUIElementTypeWebView x="0" y="0" width="400" height="800">{rows}</XCUIElementTypeWebView>'
            "</XCUIElementTypeApplication>")


class OffscreenEvidenceTests(unittest.TestCase):
    def test_offscreen_nodes_are_evidence_not_elements(self):
        snapshot = from_wda(page(0))
        self.assertEqual([e.label for e in snapshot.elements], ["Row 0", "Row 1", "Row 2", "Row 3"])
        self.assertNotIn("Row 12", snapshot.text)
        below = {node.label: node for node in snapshot.offscreen}
        self.assertEqual(below["Row 12"].direction, "below")
        self.assertAlmostEqual(below["Row 12"].screens_away, 2.0)
        self.assertTrue(below["Row 12"].web)
        evidence = snapshot.offscreen_evidence(limit=3)
        self.assertEqual([item["label"] for item in evidence], ["Row 4", "Row 5", "Row 6"])
        self.assertEqual(snapshot.offscreen[int(evidence[0]["id"][1:])].label, "Row 4")

    def test_offscreen_content_is_not_screen_identity(self):
        with_more = page(0).replace("</XCUIElementTypeWebView>",
                                    '<XCUIElementTypeStaticText label="Late ad" x="0" y="5000" width="10" height="10"/>'
                                    "</XCUIElementTypeWebView>")
        self.assertEqual(from_wda(page(0)).content_fingerprint, from_wda(with_more).content_fingerprint)
        self.assertNotIn("offscreen", from_wda(page(0)).public())

    def test_scrolled_content_above_the_viewport(self):
        snapshot = from_wda(page(1000))
        above = {node.label: node for node in snapshot.offscreen}
        self.assertEqual(above["Row 0"].direction, "above")


class ScrollToTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for target in ("mobile_agent.drivers.time.monotonic", "mobile_agent.transport.time.monotonic"):
            patcher = patch(target, self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)

    def driver(self, *xmls):
        driver = WDA("http://localhost:8100", "test")
        self.addCleanup(driver.close)
        driver.call = Device(self.clock, *xmls)
        return driver

    def test_long_glides_then_one_settled_read(self):
        snapshot = from_wda(page(0))
        node = next(n for n in snapshot.offscreen if n.label == "Row 12")  # centre at 3.025 screens
        scrolled = page(2100)
        driver = self.driver(scrolled)
        after, element, info = driver.scroll_to(snapshot, node)
        self.assertEqual(element.label, "Row 12")
        self.assertIn(element, after.elements)
        glides = [body for _, path, body in driver.call.calls if path == "/actions"]
        self.assertEqual(len(glides), 4)  # 2.625 screens: 3 x 0.68 + 0.585
        spans = [(g["actions"][0]["actions"][0]["y"] - g["actions"][0]["actions"][-2]["y"]) / 800 for g in glides]
        self.assertAlmostEqual(sum(spans) - 4 * .018, 2.625, delta=.01)
        self.assertEqual((info["rounds"], info["glides"]), (1, 4))

    def test_a_second_round_corrects_from_the_new_position(self):
        snapshot = from_wda(page(0))
        node = next(n for n in snapshot.offscreen if n.label == "Row 12")
        driver = self.driver(page(1000), page(1000), page(1000), page(2100))
        after, element, info = driver.scroll_to(snapshot, node)
        self.assertEqual(element.label, "Row 12")
        self.assertEqual(info["rounds"], 2)

    def test_end_of_content_stops(self):
        snapshot = from_wda(page(0))
        node = next(n for n in snapshot.offscreen if n.label == "Row 12")
        driver = self.driver(page(0))
        after, element, info = driver.scroll_to(snapshot, node)
        self.assertIsNone(element)
        self.assertEqual(info["end"], "no_movement")

    def test_only_nodes_of_the_snapshot(self):
        driver = self.driver(page(0))
        stranger = from_wda(page(0)).offscreen[5]
        with self.assertRaises(ValueError):
            driver.scroll_to(from_wda(page(10)), stranger)


class LaunchTests(unittest.TestCase):
    def test_overlay_hints_survive_the_process(self):
        import tempfile
        saved = dict(WDA._overlay_apps), WDA.overlay_hint_path
        self.addCleanup(lambda: (WDA._overlay_apps.clear(), WDA._overlay_apps.update(saved[0]),
                                 setattr(WDA, "overlay_hint_path", saved[1])))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "hints.json")
            WDA._overlay_apps.clear()
            WDA.load_overlay_hints(path)  # missing file: nothing
            driver = WDA("http://localhost:8100", "test")
            self.addCleanup(driver.close)
            WDA._overlay_apps.setdefault(driver.url, set()).add("com.apple.mobilesafari")
            driver._save_overlay_hints()
            WDA._overlay_apps.clear()
            WDA.load_overlay_hints(path)
            self.assertEqual(WDA._overlay_apps, {"http://localhost:8100": {"com.apple.mobilesafari"}})
            calls = []
            driver.http = SimpleNamespace(request=lambda method, path, body=None, timeout=10:
                                          calls.append((path, body)) or {"value": None, "status": 0},
                                          close=lambda: None)
            driver.call("POST", "/wda/apps/activate", {"bundleId": "com.apple.mobilesafari"})
            self.assertIn({"settings": {"defaultActiveApplication": "com.apple.mobilesafari"}},
                          [body for _, body in calls])

    def test_launch_activates_and_settles(self):
        clock = Clock()
        with patch("mobile_agent.drivers.time.monotonic", clock), patch("mobile_agent.transport.time.monotonic", clock):
            driver = WDA("http://localhost:8100", "test")
            self.addCleanup(driver.close)
            driver.call = Device(clock, app("B"))
            snapshot, info = driver.launch("com.apple.Preferences")
        self.assertEqual(snapshot.elements[0].label, "B")
        self.assertEqual(driver.call.calls[0][1], "/wda/apps/activate")
        self.assertIn("ready_ms", info)

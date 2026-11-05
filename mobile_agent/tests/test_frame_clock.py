import io
import os
import tempfile
import threading
import time
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from mobile_agent.drivers import WDA
from mobile_agent.state import from_wda
from mobile_agent.tests import fakes
from mobile_agent.tests.timing import bound, slow_machine
from mobile_agent.wda import frame_clock
from mobile_agent.wda.frame_clock import FrameClock, attach_frame_clock, changed_tiles, decode, displacement

WIDTH, HEIGHT = 590, 1278
FPS = 30


def screen(offset=0, title="Settings", highlight=None, spinner=None, rows=60, shift_x=0):
    image = Image.new("RGB", (WIDTH, HEIGHT), (242, 242, 247))
    draw = ImageDraw.Draw(image)
    for index in range(rows):
        top = 150 + index * 64 - offset
        if -64 < top < HEIGHT:
            fill = (200, 200, 205) if highlight == index else (255, 255, 255)
            draw.rectangle([16 + shift_x, top, WIDTH - 16 + shift_x, top + 56], fill=fill)
            draw.text((40 + shift_x, top + 20), f"Row {index}: item {index * 7 % 13} {'x' * (index % 9)}",
                      fill=(0, 0, 0))
    draw.rectangle([0, 0, WIDTH, 140], fill=(250, 250, 250))
    draw.text((24, 96), title, fill=(0, 0, 0))
    draw.text((24, 20), "9:41", fill=(0, 0, 0))
    if spinner is not None:
        x, y = 470, 90
        draw.rectangle([x, y, x + 60, y + 40], fill=(250, 250, 250))
        draw.pieslice([x, y, x + 40, y + 40], spinner * 45, spinner * 45 + 90, fill=(0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=50)
    return buffer.getvalue()


class NullVideo:
    def subscribe(self):
        return object()

    def unsubscribe(self, viewer):
        pass

    def next_frame(self, viewer, after, timeout=5):
        return None


class Feed:
    def __init__(self, clock, fake):
        self.clock, self.fake = clock, fake

    def frames(self, data, seconds=None, count=None):
        count = count if count is not None else round(seconds * FPS)
        for _ in range(count):
            self.fake.now += 1 / FPS
            self.clock.ingest(data, self.fake.now)

    def sequence(self, frames):
        for data in frames:
            self.fake.now += 1 / FPS
            self.clock.ingest(data, self.fake.now)


class PrimitiveTests(unittest.TestCase):
    def test_png_is_not_a_clock_frame(self):
        buffer = io.BytesIO()
        Image.new("RGB", (10, 10)).save(buffer, "PNG")
        with self.assertRaises(ValueError):
            decode(buffer.getvalue())

    def test_status_bar_changes_are_ignored(self):
        def with_clock(text):
            image = Image.open(io.BytesIO(screen())).convert("RGB")
            ImageDraw.Draw(image).rectangle([20, 14, 120, 40], fill=(250, 250, 250))
            ImageDraw.Draw(image).text((24, 20), text, fill=(0, 0, 0))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=50)
            return buffer.getvalue()
        self.assertEqual(changed_tiles(decode(with_clock("9:41")), decode(with_clock("9:42"))), ())

    def test_horizontal_displacement(self):
        moved = displacement(decode(screen()), decode(screen(shift_x=-120)))
        self.assertAlmostEqual(moved["dx"], 120 / WIDTH, delta=.02)


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.fake = fakes.Clock(100.0)
        self.clock = FrameClock(NullVideo(), clock=self.fake, start=False)
        self.addCleanup(self.clock.close)
        self.feed = Feed(self.clock, self.fake)

    def test_static_screen_is_healthy_and_still(self):
        self.feed.frames(screen(), seconds=1)
        self.assertTrue(self.clock.healthy())
        self.assertGreaterEqual(self.clock.still_for(), .9)
        self.assertGreaterEqual(self.clock.still_for(exact=False), .9)
        watch = self.clock.mark()
        self.assertTrue(watch.usable)
        self.feed.frames(screen(), seconds=.5)
        self.assertIsNone(self.clock.wait_effect(watch, 0))
        self.assertLess(self.clock.stats()["process_ms_p50"], 5)

    def test_tap_transition_effect_then_stable(self):
        self.feed.frames(screen(), seconds=1)
        watch = self.clock.mark()
        dispatched = self.fake.now
        self.feed.frames(screen(), count=3)
        self.feed.frames(screen(highlight=2), count=2)
        self.feed.sequence([screen(title="General", shift_x=WIDTH - step * 59) for step in range(1, 11)])
        settled = self.fake.now
        self.feed.frames(screen(title="General"), seconds=.3)
        effect = self.clock.wait_effect(watch, 0)
        self.assertIsNotNone(effect)
        self.assertAlmostEqual(effect.t - dispatched, 4 / FPS, delta=.001)
        anchor = self.clock.wait_stable(watch, 0)
        self.assertIsNotNone(anchor)
        self.assertAlmostEqual(anchor.t, settled, delta=.001)
        self.assertIsNotNone(watch.stable_at)

    def test_byte_identity_sees_sub_tile_motion(self):
        def nudged(shift):
            image = Image.open(io.BytesIO(screen(title="General"))).convert("RGB")
            ImageDraw.Draw(image).ellipse([300 + shift, 1180, 308 + shift, 1188], fill=(247, 247, 249))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=50)
            return buffer.getvalue()
        self.feed.frames(screen(), seconds=1)
        watch = self.clock.mark()
        start = self.fake.now
        self.feed.sequence([screen(title="General", shift_x=WIDTH - step * 118) for step in range(1, 6)])
        self.feed.sequence([nudged(index % 2) for index in range(12)])
        morph_end = self.fake.now
        self.feed.frames(nudged(0), seconds=.4)
        self.assertTrue(self.clock.quiet_between(watch, morph_end - .3, morph_end))
        self.assertFalse(self.clock.quiet_between(watch, morph_end - .3, morph_end, exact=True))
        self.assertTrue(self.clock.quiet_between(watch, morph_end + .05, self.fake.now, exact=True))
        self.assertLess(self.clock.still_for(), .45)
        self.assertGreater(self.clock.still_for(exact=False), .6)
        self.assertGreater(start, 0)

    def test_not_stable_while_still_moving(self):
        self.feed.frames(screen(), seconds=1)
        watch = self.clock.mark()
        self.feed.sequence([screen(offset=step * 20) for step in range(1, 20)])
        self.assertIsNotNone(self.clock.wait_effect(watch, 0))
        self.assertIsNone(watch.stable_at)
        self.fake.now += .01
        self.assertIsNone(self.clock.wait_stable(watch, 0))

    def test_spinner_region_is_masked(self):
        spinning = [screen(spinner=index % 8) for index in range(8)]
        for _ in range(6):
            self.feed.sequence(spinning)
        watch = self.clock.mark()
        self.assertTrue(watch.usable, watch.reason)
        self.assertTrue(watch.stable_mask)
        for _ in range(2):
            self.feed.sequence(spinning)
        self.assertIsNone(self.clock.wait_effect(watch, 0), "a spinner is not the action's effect")
        self.feed.sequence([screen(highlight=4, spinner=index % 8) for index in range(12)])
        self.assertIsNotNone(self.clock.wait_effect(watch, 0))
        self.assertIsNotNone(self.clock.wait_stable(watch, 0))

    def test_whole_screen_busy_declines(self):
        def noise():
            buffer = io.BytesIO()
            Image.effect_noise((WIDTH, HEIGHT), 80).convert("RGB").save(buffer, "JPEG", quality=50)
            return buffer.getvalue()
        noisy = [noise() for _ in range(6)]
        self.feed.sequence([noisy[index % 6] for index in range(50)])
        watch = self.clock.mark()
        self.assertFalse(watch.usable)
        self.assertEqual(watch.reason, "busy_screen")
        self.assertIsNone(self.clock.wait_effect(watch, 0))
        self.assertIsNone(self.clock.wait_stable(watch, 0))

    def test_a_scroll_that_ended_before_the_action_leaves_no_mask(self):
        self.feed.sequence([screen(offset=index * 7) for index in range(30)])
        self.feed.frames(screen(offset=29 * 7), seconds=.7)
        watch = self.clock.mark()
        self.assertTrue(watch.usable, watch.reason)
        self.assertEqual(watch.stable_mask, frozenset())
        self.assertEqual(watch.effect_mask, frozenset())

    def test_still_scrolling_at_the_action_is_not_usable(self):
        self.feed.frames(screen(), seconds=1)
        self.feed.sequence([screen(offset=index * 7) for index in range(8)])
        self.assertFalse(self.clock.mark().usable)

    def test_screen_moving_right_before_the_action_declines(self):
        self.feed.frames(screen(), seconds=1)
        self.feed.frames(screen(highlight=1), count=2)
        watch = self.clock.mark()
        self.assertFalse(watch.usable)
        self.assertEqual(watch.reason, "not_still_before_action")

    def test_stale_stream_is_unhealthy_and_answers_none(self):
        self.feed.frames(screen(), seconds=1)
        watch = self.clock.mark()
        self.feed.frames(screen(highlight=1), count=4)
        self.fake.now += 1.0
        self.assertFalse(self.clock.healthy())
        self.assertIsNone(self.clock.still_for())
        self.assertIsNone(self.clock.wait_stable(watch, 5))
        self.assertFalse(self.clock.mark().usable)

    def test_low_frame_rate_is_unhealthy(self):
        for _ in range(10):
            self.fake.now += .25
            self.clock.ingest(screen(), self.fake.now)
        self.assertFalse(self.clock.healthy())

    def test_finished_answers_are_unavailable_after_stall_release_expiry_or_close(self):
        for state in ("stale", "released", "expired", "closed"):
            with self.subTest(state=state), FrameClock(NullVideo(), clock=self.fake, start=False) as clock:
                feed = Feed(clock, self.fake)
                feed.frames(screen(), seconds=1)
                watch = clock.mark()
                feed.frames(screen(highlight=2), seconds=.4)
                self.assertIsNotNone(clock.wait_effect(watch, 0))
                anchor = clock.wait_stable(watch, 0)
                self.assertIsNotNone(anchor)
                start, end = anchor.t, self.fake.now
                self.assertTrue(clock.quiet_between(watch, start, end))
                if state == "stale":
                    self.fake.now += 1
                elif state == "released":
                    clock.release(watch)
                elif state == "expired":
                    feed.frames(screen(highlight=2), seconds=frame_clock.WATCH_SECONDS)
                else:
                    clock.close()
                self.assertIsNone(clock.wait_effect(watch, 0))
                self.assertIsNone(clock.wait_stable(watch, 0))
                self.assertFalse(clock.quiet_between(watch, start, end))

    def test_geometry_change_voids_watches(self):
        self.feed.frames(screen(), seconds=1)
        watch = self.clock.mark()
        image = Image.new("RGB", (400, 868), (255, 255, 255))
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=50)
        self.feed.frames(buffer.getvalue(), count=2)
        self.assertFalse(watch.usable)
        self.assertEqual(self.clock.stats()["resets"], 1)

    def test_memory_is_bounded(self):
        frames = [screen(highlight=index) for index in range(5)]
        for index in range(400):
            self.fake.now += 1 / FPS
            self.clock.ingest(frames[index % 5], self.fake.now)
            if index % 3 == 0:
                self.clock.mark()
        self.assertLessEqual(len(self.clock.history), frame_clock.HISTORY)
        self.assertLessEqual(len(self.clock.watches), frame_clock.MAX_WATCHES)

    def test_records_go_to_the_jsonl_log(self):
        with tempfile.TemporaryDirectory() as directory:
            self.clock.log_path = os.path.join(directory, "frameclock.jsonl")
            self.clock.record({"kind": "settle", "settle_ms": 1.0})
            with open(self.clock.log_path) as handle:
                self.assertIn('"settle_ms": 1.0', handle.read())


class RelayVideo:
    def __init__(self):
        self.condition = threading.Condition()
        self.frame = None
        self.viewers = set()
        self.closed = False

    def subscribe(self):
        viewer = object()
        with self.condition:
            self.viewers.add(viewer)
        return viewer

    def unsubscribe(self, viewer):
        with self.condition:
            self.viewers.discard(viewer)
            self.condition.notify_all()

    def publish(self, sequence, data, source="wda_mjpeg"):
        with self.condition:
            self.frame = (sequence, "image/jpeg", data, 0, time.monotonic(), source)
            self.condition.notify_all()

    def next_frame(self, viewer, after, timeout=5):
        deadline = time.monotonic() + timeout
        with self.condition:
            while viewer in self.viewers:
                if self.frame is not None and self.frame[0] > after:
                    return self.frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(remaining)
            return None


class ThreadedTests(unittest.TestCase):
    def test_worker_consumes_the_relay_and_stops_cleanly(self):
        video = RelayVideo()
        clock = FrameClock(video)
        try:
            for sequence in range(1, 21):
                video.publish(sequence, screen(highlight=sequence % 3))
                time.sleep(.005)
            deadline = time.monotonic() + 3
            while clock.frames < 5 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertGreaterEqual(clock.frames, 5)
            self.assertEqual(len(video.viewers), 1)
        finally:
            clock.close()
        self.assertEqual(video.viewers, set())
        self.assertFalse(clock.worker.is_alive())

    def test_screenshot_fallback_frames_are_ignored(self):
        video = RelayVideo()
        clock = FrameClock(video)
        try:
            video.publish(1, screen(), source="wda_screenshot")
            time.sleep(.1)
            self.assertEqual(clock.frames, 0)
        finally:
            clock.close()

    def test_attach_turns_the_clock_on_and_needs_a_wda_driver(self):
        class Driver:
            frame_clock = None
        driver = Driver()
        self.assertIsNone(attach_frame_clock(object(), RelayVideo()))
        clock = attach_frame_clock(driver, RelayVideo())
        try:
            self.assertIs(driver.frame_clock, clock)
        finally:
            clock.close()

    def test_every_caller_passes_only_arguments_attach_takes(self):
        import ast
        import inspect
        from pathlib import Path
        accepted = set(inspect.signature(attach_frame_clock).parameters)
        root = Path(__file__).resolve().parents[1]
        bad = []
        for path in root.rglob("*.py"):
            if ".venv" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call)
                        and getattr(node.func, "id", getattr(node.func, "attr", None)) == "attach_frame_clock"):
                    bad += [f"{path.relative_to(root)}:{node.lineno} {k.arg}=" for k in node.keywords
                            if k.arg and k.arg not in accepted]
        self.assertEqual(bad, [])


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
        if watch.moves:
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
    def setUp(self):
        self.clock = Clock()
        for target in ("mobile_agent.drivers.time.monotonic", "mobile_agent.transport.time.monotonic"):
            patcher = patch(target, self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("MOBSTER_GLIDE", None)
        WDA._momentum_apps.clear()
        self.addCleanup(WDA._momentum_apps.clear)

    def driver(self, *xmls, **fake):
        driver = WDA("http://localhost:8100", "test")
        self.addCleanup(driver.close)
        driver.call = Device(self.clock, *xmls)
        driver.frame_clock = FakeFrameClock(self.clock, **fake)
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

    def test_observe_ready_accepts_two_agreeing_reads_after_pixel_quiet(self):
        driver = self.driver(app("B"), still=5.0)
        self.assertEqual(driver.observe_ready(timeout=5).elements[0].label, "B")
        self.assertEqual(driver.call.reads, 2)

    def test_observe_ready_keeps_the_quiet_period_when_pixels_moved(self):
        driver = self.driver(app("B"), still=.05)
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
        self.assertIsNone(driver._stable)
        self.assertEqual(driver.frame_clock.records[-1]["path"], "one_read")
        self.assertEqual(driver.frame_clock.released, 1)

    def test_single_read_falls_back_to_agreeing_reads(self):
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
        os.environ.pop("MOBSTER_GLIDE", None)
        WDA._momentum_apps.clear()
        self.addCleanup(WDA._momentum_apps.clear)

    def swipe(self, xml, operation="SWIPE_UP", clock=True, **fake):
        driver = WDA("http://localhost:8100", "test")
        self.addCleanup(driver.close)
        driver.call = Device(self.clock, xml)
        if clock:
            driver.frame_clock = FakeFrameClock(self.clock, **fake)
        driver.execute(operation, None, from_wda(xml))
        paths = [path for _, path, _ in driver.call.calls]
        return driver, paths

    def test_a_watched_app_glides_whatever_app_it_is(self):
        for bundle, web in (("com.example.app", False), ("com.apple.mobilesafari", True), ("com.example.feed", False)):
            driver, paths = self.swipe(app("A", bundle=bundle, web=web))
            self.assertEqual(paths[0], "/actions", bundle)
        actions = driver.call.calls[0][2]["actions"][0]["actions"]
        self.assertEqual([a["type"] for a in actions], ["pointerMove", "pointerDown"] + ["pointerMove"] * 4
                         + ["pointerUp"])
        self.assertNotIn("pause", [a["type"] for a in actions])
        ys = [a["y"] for a in actions if a["type"] == "pointerMove"]
        self.assertEqual((ys[0], ys[-1]), (640, 96))
        steps = [abs(b - a) / move["duration"] for a, b, move in zip(ys[1:], ys[2:], actions[3:])]
        self.assertLess(steps[-1], steps[0] / 20, "the glide decelerates before lifting")

    def test_without_a_watching_clock_every_app_keeps_the_legacy_drag(self):
        for bundle in ("com.apple.Preferences", "com.apple.mobilesafari", "com.example.feed"):
            _, paths = self.swipe(app("A", bundle=bundle), clock=False)
            self.assertEqual(paths, ["/wda/dragfromtoforduration"], bundle)
        _, paths = self.swipe(app("A", bundle="com.example.feed"), healthy=False)
        self.assertEqual(paths, ["/wda/dragfromtoforduration"])

    def test_horizontal_swipes_flick_and_the_flag_keeps_the_legacy_drag(self):
        _, paths = self.swipe(app("A"), operation="SWIPE_LEFT")
        self.assertEqual(paths, ["/actions"])
        os.environ["MOBSTER_GLIDE"] = "0"
        _, paths = self.swipe(app("A"))
        self.assertEqual(paths, ["/wda/dragfromtoforduration"])

    def test_motion_after_release_demotes_the_app(self):
        from mobile_agent.wda.frame_clock import Frame, decode
        driver, paths = self.swipe(app("A"))
        self.assertEqual(paths, ["/actions"])
        watch = driver._frame_watch
        watch.ref = Frame(1, 0, decode(screen(0)))
        watch.release_frame = Frame(2, 0, decode(screen(600)))
        final = Frame(3, 0, decode(screen(900)))
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
        from mobile_agent.wda.frame_clock import Frame, decode
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
    def __init__(self, transition=.35, glass_lag=.08, ax_lag=0.0, read_seconds=.12):
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
    def run_tap(self, **phone):
        from mobile_agent.wda.frame_clock import FrameClock
        device = SimulatedPhone(**phone)
        self.addCleanup(device.close)
        driver = WDA("http://localhost:8100", "test")
        self.addCleanup(driver.close)
        driver.call = device
        driver.frame_clock = FrameClock(device.video)
        deadline = time.monotonic() + 3
        while not driver.frame_clock.healthy() and time.monotonic() < deadline:
            time.sleep(.02)
        time.sleep(.3)
        before = from_wda(app("Settings"))
        driver.execute("TAP", before.elements[0], before)
        started = time.monotonic()
        after = driver.wait_for_change(before, timeout=1.6, wait_seconds=.6)
        return driver, device, after, time.monotonic() - started

    def test_on_mode_settles_on_pixels_with_two_reads(self):
        driver, device, after, elapsed = self.run_tap()
        self.assertEqual(after.elements[0].label, "General")
        record = [r for r in driver.frame_clock.records if r["kind"] == "settle"][-1]
        self.assertEqual(record["path"], "frame")
        self.assertLess(elapsed, bound(.95))

    def test_ax_lagging_the_pixels_changes_nothing(self):
        driver, device, after, elapsed = self.run_tap(ax_lag=.33, read_seconds=.2)
        self.assertEqual(after.elements[0].label, "Settings")
        record = [r for r in driver.frame_clock.records if r["kind"] == "settle"][-1]
        self.assertEqual(record["path"], "ax")


def page(offset_pt, bundle="com.apple.mobilesafari"):
    rows = "".join(f'<XCUIElementTypeStaticText label="Row {index}" x="0" y="{index * 200 - offset_pt}" '
                   f'width="300" height="40"/>' for index in range(30))
    return (f'<XCUIElementTypeApplication width="400" height="800" bundleId="{bundle}">'
            f'<XCUIElementTypeWebView x="0" y="0" width="400" height="800">{rows}</XCUIElementTypeWebView>'
            "</XCUIElementTypeApplication>")


class OffscreenContentTests(unittest.TestCase):
    def test_offscreen_content_is_not_screen_identity(self):
        with_more = page(0).replace("</XCUIElementTypeWebView>",
                                    '<XCUIElementTypeStaticText label="Late ad" x="0" y="5000" width="10" height="10"/>'
                                    "</XCUIElementTypeWebView>")
        self.assertEqual(from_wda(page(0)).content_fingerprint, from_wda(with_more).content_fingerprint)

    def test_scrolled_content_above_the_viewport_is_not_on_the_screen(self):
        snapshot = from_wda(page(1000))
        self.assertNotIn("Row 0", [e.label for e in snapshot.elements])
        self.assertNotIn("Row 0", snapshot.text)


class LaunchTests(unittest.TestCase):
    def test_overlay_hints_survive_the_process(self):
        import tempfile
        saved = dict(WDA._overlay_apps), WDA.overlay_hint_path
        self.addCleanup(lambda: (WDA._overlay_apps.clear(), WDA._overlay_apps.update(saved[0]),
                                 setattr(WDA, "overlay_hint_path", saved[1])))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "hints.json")
            WDA._overlay_apps.clear()
            WDA.load_overlay_hints(path)
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


if __name__ == "__main__":
    unittest.main()

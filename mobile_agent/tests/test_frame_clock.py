"""FrameClock on synthetic MJPEG sequences. Offline; no phone, no network."""

import io
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from mobile_agent import frame_clock
from mobile_agent.frame_clock import (FrameClock, attach_frame_clock, changed_tiles, decode, displacement,
                                      frame_clock_mode)

WIDTH, HEIGHT = 590, 1278
FPS = 30


def screen(offset=0, title="Settings", highlight=None, spinner=None, rows=60, shift_x=0):
    """A Settings-like list, JPEG q50 like WDA's "medium" stream."""
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
    draw.text((24, 20), "9:41", fill=(0, 0, 0))  # status bar
    if spinner is not None:
        # A small busy indicator away from the list text, changing every frame.
        x, y = 470, 90
        draw.rectangle([x, y, x + 60, y + 40], fill=(250, 250, 250))
        draw.pieslice([x, y, x + 40, y + 40], spinner * 45, spinner * 45 + 90, fill=(0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=50)
    return buffer.getvalue()


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class NullVideo:
    def subscribe(self):
        return object()

    def unsubscribe(self, viewer):
        pass

    def next_frame(self, viewer, after, timeout=5):
        return None


class Feed:
    """Feeds frames into a clock at FPS in simulated time."""

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
    def test_decode_is_small_grey_and_drops_the_status_bar(self):
        image = decode(screen())
        self.assertEqual(image.mode, "L")
        self.assertLessEqual(image.size[1], 320)
        self.assertGreaterEqual(image.size[1], 280)

    def test_full_resolution_frames_decode_to_the_same_geometry_class(self):
        image = Image.new("RGB", (1179, 2556), (255, 255, 255))
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=50)
        self.assertGreaterEqual(decode(buffer.getvalue()).size[1], 280)
        self.assertLessEqual(decode(buffer.getvalue()).size[1], 320)

    def test_png_is_not_a_clock_frame(self):
        buffer = io.BytesIO()
        Image.new("RGB", (10, 10)).save(buffer, "PNG")
        with self.assertRaises(ValueError):
            decode(buffer.getvalue())

    def test_identical_frames_have_no_changed_tiles(self):
        self.assertEqual(changed_tiles(decode(screen()), decode(screen())), ())

    def test_a_row_highlight_changes_a_few_tiles(self):
        tiles = changed_tiles(decode(screen()), decode(screen(highlight=3)))
        self.assertGreaterEqual(len(tiles), 2)
        self.assertLess(len(tiles), 40)

    def test_status_bar_changes_are_ignored(self):
        def with_clock(text):
            image = Image.open(io.BytesIO(screen())).convert("RGB")
            ImageDraw.Draw(image).rectangle([20, 14, 120, 40], fill=(250, 250, 250))
            ImageDraw.Draw(image).text((24, 20), text, fill=(0, 0, 0))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=50)
            return buffer.getvalue()
        self.assertEqual(changed_tiles(decode(with_clock("9:41")), decode(with_clock("9:42"))), ())

    def test_displacement_recovers_a_known_scroll(self):
        base = decode(screen(0))
        for pixels in (64, 200, 623):
            moved = displacement(base, decode(screen(pixels)))
            self.assertAlmostEqual(moved["dy"], pixels / HEIGHT, delta=.01, msg=pixels)
            self.assertAlmostEqual(moved["dx"], 0, delta=.01)
        back = displacement(decode(screen(300)), base)
        self.assertAlmostEqual(back["dy"], -300 / HEIGHT, delta=.01)

    def test_displacement_of_identical_frames_is_zero(self):
        self.assertEqual(displacement(decode(screen()), decode(screen()))["dy"], 0)

    def test_horizontal_displacement(self):
        moved = displacement(decode(screen()), decode(screen(shift_x=-120)))
        self.assertAlmostEqual(moved["dx"], 120 / WIDTH, delta=.02)

    def test_mode_parsing(self):
        self.assertEqual(frame_clock_mode("0"), "off")
        self.assertEqual(frame_clock_mode("shadow"), "shadow")
        self.assertEqual(frame_clock_mode("ON"), "on")
        self.assertEqual(frame_clock_mode(""), frame_clock.DEFAULT_MODE)
        with patch.dict(os.environ, {"MOBSTER_FRAME_CLOCK": "off"}):
            self.assertEqual(frame_clock_mode(), "off")


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClock()
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
        self.feed.frames(screen(), count=3)                   # capture lag
        self.feed.frames(screen(highlight=2), count=2)        # touch-down highlight
        # A push: the new screen slides in over ~0.35 s.
        self.feed.sequence([screen(title="General", shift_x=WIDTH - step * 59) for step in range(1, 11)])
        settled = self.fake.now  # the last sliding frame lands on the final screen
        self.feed.frames(screen(title="General"), seconds=.3)
        effect = self.clock.wait_effect(watch, 0)
        self.assertIsNotNone(effect)
        self.assertAlmostEqual(effect.t - dispatched, 4 / FPS, delta=.001)
        anchor = self.clock.wait_stable(watch, 0)
        self.assertIsNotNone(anchor)
        self.assertAlmostEqual(anchor.t, settled, delta=.001)
        self.assertFalse(self.clock.moved_since(watch, anchor.t))
        self.assertIsNotNone(watch.stable_at)

    def test_byte_identity_sees_sub_tile_motion(self):
        # iOS 26 glass controls morph by ~1 pt after a transition: invisible to
        # 16-px tiles, but the JPEG bytes change until they stop.
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
        # A real change with the spinner still running: effect, then stable.
        self.feed.sequence([screen(highlight=4, spinner=index % 8) for index in range(12)])
        self.assertIsNotNone(self.clock.wait_effect(watch, 0))
        self.assertIsNotNone(self.clock.wait_stable(watch, 0))

    def test_whole_screen_busy_declines(self):
        # Full-screen video before the mark: everything is busy, nothing is usable.
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
        self.fake.now += 1.0  # the stream stops
        self.assertFalse(self.clock.healthy())
        self.assertIsNone(self.clock.still_for())
        self.assertIsNone(self.clock.wait_stable(watch, 5))
        self.assertFalse(self.clock.mark().usable)

    def test_low_frame_rate_is_unhealthy(self):
        for _ in range(10):
            self.fake.now += .25
            self.clock.ingest(screen(), self.fake.now)
        self.assertFalse(self.clock.healthy())

    def test_scroll_odometry_between_mark_and_rest(self):
        self.feed.frames(screen(), seconds=1)
        watch = self.clock.mark()
        self.feed.sequence([screen(offset=step * 31) for step in range(1, 21)])
        self.feed.frames(screen(offset=620), seconds=.3)
        moved = displacement(watch.ref, self.clock.latest_frame())
        self.assertAlmostEqual(moved["dy"], 620 / HEIGHT, delta=.01)
        self.assertIsNotNone(self.clock.wait_stable(watch, 0))

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
    """The WdaVideo viewer API over an in-memory frame list."""

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

    def test_attach_respects_the_mode_and_needs_a_wda_driver(self):
        class Driver:
            frame_clock = None
            frame_clock_mode = "off"
        driver = Driver()
        self.assertIsNone(attach_frame_clock(driver, RelayVideo(), mode="off"))
        self.assertIsNone(attach_frame_clock(object(), RelayVideo(), mode="on"))
        clock = attach_frame_clock(driver, RelayVideo(), mode="shadow")
        try:
            self.assertIs(driver.frame_clock, clock)
            self.assertEqual(driver.frame_clock_mode, "shadow")
        finally:
            clock.close()


if __name__ == "__main__":
    unittest.main()

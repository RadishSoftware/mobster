"""MOBSTER_TREE_READS=still: observe_ready takes its first read as the screen at rest when the stream proves it, and
every settled screen is logged with what the stream showed. Offline, on two recorded screens (fixtures/frontier/
recorded_screens.json: a Bluesky search and the serving sheet after it, read on a simulator on 14 Oct 2026) and a
scripted stream."""

import dataclasses
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent.bench.tape import decode
from mobile_agent.drivers import WDA
from mobile_agent.tests.test_frame_clock import Clock, Device, FakeFrameClock
from mobile_agent.transport import Deadline
from mobile_agent.wda import guards as driver_guards
from mobile_agent.wda.guards import DriverRejection

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "frontier" / "recorded_screens.json").read_text())
SOURCE = '<XCUIElementTypeApplication width="402" height="874" bundleId="com.iosworld.benchmark.bluesky"/>'


class Stream(FakeFrameClock):
    """A stream whose bytes last changed at ``changed_at`` on the test's clock (None: it can't say)."""

    def __init__(self, clock, changed_at, **fake):
        super().__init__(clock, **fake)
        self.changed_at = changed_at

    def still_for(self, exact=True):
        return None if self.changed_at is None else self.clock.now - self.changed_at


class RecordedDriverCase(unittest.TestCase):
    """A WDA driver on a fake clock whose source reads are the two recorded screens, and a scripted stream."""

    def setUp(self):
        self.clock = Clock()
        for target in ("mobile_agent.drivers.time.monotonic", "mobile_agent.transport.time.monotonic"):
            patcher = patch(target, self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleeper = patch("time.sleep", lambda seconds: setattr(self.clock, "now", self.clock.now + seconds))
        sleeper.start()
        self.addCleanup(sleeper.stop)
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("MOBSTER_TREE_READS", "MOBSTER_SETTLE"):
            os.environ.pop(name, None)
        self.search, self.sheet = (decode(screen) for screen in RECORDED["screens"])

    def driver(self, *screens, changed_at=-1e6, stream=True, **fake):
        """A driver whose source reads are the recorded ``screens`` in order (the last repeats), each taking
        ``Clock.per_read`` and its stream last changed at ``changed_at``."""
        driver = WDA("http://localhost:8100", "test")
        self.addCleanup(driver.close)
        driver.call = Device(self.clock, SOURCE)
        pending = list(screens)

        def next_screen(root, *args, **kwargs):
            screen = pending.pop(0) if len(pending) > 1 else pending[0]
            return dataclasses.replace(screen, captured_at=self.clock.now)

        patcher = patch("mobile_agent.wda.observe.from_wda_root", next_screen)
        patcher.start()
        self.addCleanup(patcher.stop)
        if stream:
            driver.frame_clock = Stream(self.clock, changed_at, **fake)
        return driver


class TreeReadsTests(RecordedDriverCase):
    def ready(self, driver, flag="still"):
        if flag:
            os.environ["MOBSTER_TREE_READS"] = flag
        return driver.observe_ready(timeout=5)

    def last_ready(self, driver):
        return [r for r in driver.frame_clock.records if r["kind"] == "ready"][-1]

    # -- observe_ready ----------------------------------------------------------------------------------------

    def test_without_the_flag_two_agreeing_reads_settle(self):
        driver = self.driver(self.search)
        self.assertEqual(self.ready(driver, flag=None).content_fingerprint, self.search.content_fingerprint)
        self.assertEqual(driver.call.reads, 2)
        self.assertEqual(self.last_ready(driver)["rule"], "pixels")

    def test_a_screen_the_stream_held_still_is_read_once(self):
        driver = self.driver(self.search)
        self.assertEqual(self.ready(driver).content_fingerprint, self.search.content_fingerprint)
        self.assertEqual(driver.call.reads, 1)
        self.assertIs(driver._stable, driver.observe_ready())  # proven: the next call reuses it without a read
        self.assertEqual(driver.call.reads, 1)
        first, second = [r for r in driver.frame_clock.records if r["kind"] == "ready"]
        self.assertEqual((first["rule"], first["reads"], second["rule"], second["reads"]), ("rest", 1, "reuse", 0))

    def test_every_ready_row_says_whether_the_first_read_would_have_stood(self):
        # Without the flag: the stream showed the first read at rest, and the second read agreed.
        driver = self.driver(self.search)
        self.ready(driver, flag=None)
        row = self.last_ready(driver)
        self.assertEqual((row["would_rest"], row["first_held"], row["rule"]), (True, True, "pixels"))
        # The same stillness, but the screen moved on between the reads: the rule would have been wrong.
        driver = self.driver(self.search, self.sheet)
        self.ready(driver, flag=None)
        row = self.last_ready(driver)
        self.assertEqual((row["would_rest"], row["first_held"]), (True, False))
        # The flag does not change what is logged, only what is accepted.
        driver = self.driver(self.search, self.sheet)
        self.ready(driver)
        row = self.last_ready(driver)
        self.assertEqual((row["rule"], row["would_rest"], row["first_held"]), ("rest", True, True))
        # Moving pixels: not at rest, whatever the reads say.
        driver = self.driver(self.search, changed_at=self.clock.now - .1)
        self.ready(driver, flag=None)
        self.assertFalse(self.last_ready(driver)["would_rest"])

    def test_other_values_of_the_flag_change_nothing(self):
        driver = self.driver(self.search)
        self.ready(driver, flag="1")
        self.assertEqual(driver.call.reads, 2)

    def test_a_change_inside_the_lead_keeps_the_agreeing_reads(self):
        # The bytes changed 0.1 s before the read began: the screen may still be settling.
        driver = self.driver(self.search, changed_at=self.clock.now - .1)
        self.ready(driver)
        self.assertGreaterEqual(driver.call.reads, 2)
        self.assertNotEqual(self.last_ready(driver)["rule"], "rest")

    def test_a_change_after_the_read_began_is_no_rest(self):
        driver = self.driver(self.search)
        observe = driver.observe

        def moved(*args, **kwargs):
            read = observe(*args, **kwargs)
            driver.frame_clock.changed_at = self.clock.now  # the screen moved while the read ran
            return read
        driver.observe = moved
        self.ready(driver)
        self.assertGreaterEqual(driver.call.reads, 2)

    def test_stillness_from_before_the_last_dispatch_is_no_rest(self):
        # The tap went out, and the stream has not shown its effect yet: the read may be of the old screen.
        driver = self.driver(self.search, changed_at=self.clock.now - 5)
        driver._dispatching()
        self.clock.now += .6
        self.ready(driver)
        self.assertGreaterEqual(driver.call.reads, 2)
        # The effect reached the stream after the dispatch and has been still for the lead since: at rest.
        driver = self.driver(self.sheet, changed_at=self.clock.now + .1)
        driver._dispatching()
        self.clock.now += 1
        self.ready(driver)
        self.assertEqual(driver.call.reads, 1)
        self.assertEqual(self.last_ready(driver)["rule"], "rest")

    def test_a_stream_that_cannot_say_is_no_rest(self):
        driver = self.driver(self.search, changed_at=None)
        self.ready(driver)
        self.assertGreaterEqual(driver.call.reads, 2)

    def test_without_a_stream_the_flag_changes_nothing(self):
        driver = self.driver(self.search, stream=False)
        self.assertEqual(self.ready(driver).content_fingerprint, self.search.content_fingerprint)
        self.assertEqual(driver.call.reads, 3)

    def test_after_screenshot_timeouts_the_stream_is_not_trusted(self):
        driver = self.driver(self.search)
        driver.note_screenshot_timeout()
        driver.note_screenshot_timeout()
        self.ready(driver)
        self.assertGreaterEqual(driver.call.reads, 3)

    def test_an_empty_screen_never_settles_on_pixels(self):
        empty = dataclasses.replace(self.search, elements=[], text="")
        driver = self.driver(empty)
        self.ready(driver)
        self.assertGreaterEqual(driver.call.reads, 3)

    def test_a_screen_proven_before_is_still_taken_from_memory(self):
        driver = self.driver(self.search)
        self.ready(driver, flag=None)
        driver._stable = None
        driver.call.reads = 0
        self.ready(driver)
        self.assertEqual((driver.call.reads, self.last_ready(driver)["rule"]), (1, "memory"))

    # -- the fresh-screen check ------------------------------------------------------------------------------------

    def test_a_stale_snapshot_is_still_read_again_before_a_coordinate_tap(self):
        driver = self.driver(self.sheet)
        os.environ["MOBSTER_TREE_READS"] = "still"
        old = dataclasses.replace(self.search, captured_at=self.clock.now - 5, read_started=self.clock.now - 5)
        with self.assertRaises(DriverRejection) as raised:
            driver._check_current(old, Deadline(5))
        self.assertEqual((raised.exception.code, driver.call.reads), ("stale_revision", 1))
        driver = self.driver(self.search)
        driver._check_current(old, Deadline(5))  # the same screen: nothing refused, but it was read
        self.assertEqual(driver.call.reads, 1)

    # -- what the next read says about the stream --------------------------------------------------------------

    def settle(self, driver):
        driver.execute("TAP", self.search.elements[0], self.search)
        return driver.wait_for_change(self.search, timeout=1.6, wait_seconds=.6)

    def test_the_next_read_says_whether_the_stream_stayed_still(self):
        for changed_at, expected in ((-1e6, True), (None, False)):
            driver = self.driver(self.sheet)
            settled = self.settle(driver)
            driver.frame_clock.changed_at = changed_at
            self.assertEqual(settled.content_fingerprint, self.sheet.content_fingerprint)
            driver.observe()
            follow = driver.frame_clock.records[-1]
            self.assertEqual((follow["kind"], follow["matches_result"], follow["stream_still"]),
                             ("next_read", True, expected))

    def test_a_change_after_the_accepted_read_began_is_not_still(self):
        driver = self.driver(self.sheet)
        settled = self.settle(driver)
        driver.frame_clock.changed_at = settled.read_started + .05
        driver.observe()
        self.assertFalse(driver.frame_clock.records[-1]["stream_still"])


class StillnessTests(unittest.TestCase):
    """driver_guards.last_change and unchanged_since: the one rule the agent's refresh, the phone tools, the pre-warmed
    first screen and observe_ready all use."""

    class Clock:
        def __init__(self, seconds, error=None):
            self.seconds, self.error = seconds, error

        def still_for(self):
            if self.error:
                raise self.error
            return self.seconds

    def test_unchanged_since_needs_a_stream_that_says_so(self):
        now = time.monotonic()
        self.assertFalse(driver_guards.unchanged_since(None, now))
        self.assertFalse(driver_guards.unchanged_since(object(), now))
        self.assertFalse(driver_guards.unchanged_since(self.Clock(None), now))
        self.assertFalse(driver_guards.unchanged_since(self.Clock(0, OSError()), now))
        self.assertFalse(driver_guards.unchanged_since(self.Clock(60), 0.0))

    def test_unchanged_since_means_no_byte_changed_after_the_read_began(self):
        now = time.monotonic()
        self.assertTrue(driver_guards.unchanged_since(self.Clock(5), now - 1))
        self.assertFalse(driver_guards.unchanged_since(self.Clock(.5), now - 1))
        self.assertEqual(driver_guards.last_change(self.Clock(None)), None)
        self.assertAlmostEqual(driver_guards.last_change(self.Clock(2)), time.monotonic() - 2, places=1)


if __name__ == "__main__":
    unittest.main()

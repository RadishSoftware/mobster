"""MOBSTER_SETTLE=launch: an app switch settles on one read when its pixels already rest as the launch returns, and
every settle's log row says why it did not end on one read. Offline, on the two recorded screens of
fixtures/frontier/recorded_screens.json and a scripted stream."""

import os
import unittest
from types import SimpleNamespace

from mobile_agent.frontier import FrontierAgent
from mobile_agent.tests.test_tree_reads import RecordedDriverCase, Stream


class Resting(Stream):
    """The pixels as a launch returns: the effect was ``seen`` and they ``rested`` already, or each shows only to a
    wait that is given time. ``waits`` are the timeouts the driver asked with."""

    def __init__(self, clock, *, seen=True, rested=True, **fake):
        super().__init__(clock, -1e6, **fake)
        self.seen, self.rested, self.waits = seen, rested, []

    def wait_effect(self, watch, timeout):
        self.waits.append(("effect", timeout))
        return watch.effect if watch.usable and watch.effect is not None and (self.seen or timeout > 0) else None

    def wait_stable(self, watch, timeout, duration=.18):
        self.waits.append(("stable", timeout))
        return watch.effect if watch.usable and watch.effect is not None and (self.rested or timeout > 0) else None


class SettleRestTests(RecordedDriverCase):
    def launch(self, *screens, flag="launch", single_read="rest", **state):
        """A driver that was acted on, then asked to settle after the search screen (the one before)."""
        if flag:
            os.environ["MOBSTER_SETTLE"] = flag
        driver = self.driver(*screens)
        fake = driver.frame_clock = Resting(self.clock, **{k: v for k, v in state.items() if k in ("seen", "rested")},
                                            **{k: v for k, v in state.items() if k in ("quiet",)})
        driver.execute("TAP", self.search.elements[0], self.search)
        read = driver.wait_for_change(self.search, timeout=1.6, wait_seconds=.6, single_read=single_read)
        return driver, fake, read, driver.frame_clock.records[-1]

    def test_a_launch_that_returned_at_rest_settles_on_one_read(self):
        driver, fake, read, row = self.launch(self.sheet)
        self.assertEqual(read.content_fingerprint, self.sheet.content_fingerprint)
        self.assertEqual((driver.call.reads, driver.settled_by, row["path"], row["reads"]),
                         (1, "one_read", "one_read", 1))
        self.assertNotIn("miss", row)
        self.assertEqual(fake.waits, [("effect", 0.0), ("stable", 0.0)])  # nothing waited for

    def test_without_the_switch_rest_is_ignored(self):
        for flag in (None, "1"):
            driver, _, read, row = self.launch(self.sheet, flag=flag)
            self.assertGreaterEqual(driver.call.reads, 2, flag)
            self.assertIsNone(driver.settled_by)
            self.assertEqual(read.content_fingerprint, self.sheet.content_fingerprint)
            self.assertEqual(row["miss"], "not_requested")

    def test_pixels_still_moving_keep_the_agreeing_reads(self):
        # On a phone the zoom is still running as activate returns: nothing changes there.
        driver, fake, read, row = self.launch(self.sheet, rested=False)
        self.assertEqual((driver.settled_by, row["miss"]), (None, "not_stable"))
        self.assertGreaterEqual(driver.call.reads, 2)
        self.assertEqual(fake.waits[:2], [("effect", 0.0), ("stable", 0.0)])  # it asked once, without waiting

    def test_an_effect_not_seen_yet_keeps_the_agreeing_reads(self):
        driver, _, _, row = self.launch(self.sheet, seen=False)
        self.assertEqual((driver.settled_by, row["miss"]), (None, "no_effect"))
        self.assertGreaterEqual(driver.call.reads, 2)

    def test_a_read_of_the_old_screen_is_no_settle(self):
        # AX still shows the screen before the launch: the one read is spent, the loop reads on.
        driver, _, read, row = self.launch(self.search, self.sheet)
        self.assertEqual((driver.settled_by, row["miss"]), (None, "unchanged"))
        self.assertEqual(read.content_fingerprint, self.sheet.content_fingerprint)
        self.assertEqual(row["reads"], driver.call.reads)

    def test_pixels_that_moved_during_the_read_are_no_settle(self):
        driver, _, _, row = self.launch(self.sheet, quiet=[False, False, True])
        self.assertEqual((driver.settled_by, row["miss"]), (None, "moved"))
        self.assertGreater(driver.call.reads, 1)

    def test_the_scoreboards_reasons_cover_a_settle_that_waits_too(self):
        # single_read=True waits for the effect and the rest, as before; its misses carry the same reasons.
        driver, fake, _, row = self.launch(self.sheet, single_read=True, seen=False, rested=False)
        self.assertEqual(row["path"], "one_read")
        self.assertEqual([timeout > 0 for _, timeout in fake.waits[:2]], [True, True])
        driver, _, _, row = self.launch(self.search, self.sheet, single_read=True)
        self.assertEqual(row["miss"], "unchanged")
        driver, _, _, row = self.launch(self.sheet, single_read=False)
        self.assertEqual(row["miss"], "not_requested")

    def test_an_unusable_watch_has_no_one_read_to_miss(self):
        os.environ["MOBSTER_SETTLE"] = "launch"
        driver = self.driver(self.sheet, usable=False)
        driver.execute("TAP", self.search.elements[0], self.search)
        driver.wait_for_change(self.search, timeout=1.6, wait_seconds=.6, single_read="rest")
        row = driver.frame_clock.records[-1]
        self.assertEqual((row["path"], row["fallback"]), ("frame_fallback", "busy_screen"))
        self.assertNotIn("miss", row)


class SettleCallTests(unittest.TestCase):
    """What the agent asks of the driver after each kind of action."""

    class Driver:
        def __init__(self, **flags):
            self.asked = []
            self.__dict__.update(flags)

        def wait_for_change(self, before, **options):
            self.asked.append(options)
            return "settled"

    def settle(self, *args, **flags):
        driver = self.Driver(**flags)
        agent = SimpleNamespace(driver=driver, settle_seconds=.6, _observe=lambda: "observed")
        self.assertEqual(FrontierAgent._settle(agent, "before", *args), "settled")
        return {k: v for k, v in driver.asked[-1].items() if k in ("single_read", "early")}

    def test_a_last_action_may_settle_on_one_read_and_a_launch_only_at_rest(self):
        flags = {"settles_on_one_read": True, "settles_early": True}
        self.assertEqual(self.settle(True, **flags), {"single_read": True, "early": True})
        self.assertEqual(self.settle(False, **flags), {})
        self.assertEqual(self.settle(False, True, **flags), {"single_read": "rest"})

    def test_a_driver_that_cannot_settle_on_one_read_is_not_asked(self):
        self.assertEqual(self.settle(False, True), {})
        self.assertEqual(self.settle(True, settles_early=True), {"early": True})


if __name__ == "__main__":
    unittest.main()

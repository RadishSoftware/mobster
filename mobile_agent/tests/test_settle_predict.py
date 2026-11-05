"""MOBSTER_EXPERIMENTAL_SETTLE: predictive settle learns each app's transition durations, waits the learned p90,
reads once and falls back to the polling loop on any doubt. Offline, on the recorded screens of
fixtures/frontier/recorded_screens.json, a scripted stream and a virtual clock."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent.settle import predict, store
from mobile_agent.settle.store import MAX_PREDICTION_MS, MIN_SAMPLES, WINDOW, SettleStore, percentile, shape_of
from mobile_agent.settle.stream import PhoneScreenStream, WdaMjpegObserver
from mobile_agent.tests.test_tree_reads import RecordedDriverCase


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "settle" / "durations.json"
        self.addCleanup(lambda: self.path.exists() and self.path.unlink())

    def test_nearest_rank_percentile(self):
        self.assertEqual(percentile(range(1, 11), .9), 9)
        self.assertEqual(percentile([5], .9), 5)

    def test_a_prediction_needs_enough_samples(self):
        data = SettleStore(self.path)
        for _ in range(MIN_SAMPLES - 1):
            data.add("app", "tap", 300)
        self.assertIsNone(data.p90("app", "tap"))
        data.add("app", "tap", 300)
        self.assertEqual(data.p90("app", "tap"), 300)
        self.assertIsNone(data.p90("app", "swipe_y"))
        self.assertIsNone(data.p90("other", "tap"))

    def test_a_slow_transition_is_not_predicted(self):
        data = SettleStore(self.path)
        for _ in range(MIN_SAMPLES):
            data.add("app", "tap", MAX_PREDICTION_MS + 1)
        self.assertIsNone(data.p90("app", "tap"))

    def test_an_uneven_transition_is_left_to_polling(self):
        # A p90 more than the polling quiet period above the median costs more than polling does on a typical settle.
        data = SettleStore(self.path)
        for ms in (100, 100, 100, 100, 100, 100, 100, 100, 100, 900):
            data.add("app", "tap", ms)
        self.assertEqual(data.p90("app", "tap"), 100)
        data.add("app", "tap", 900)
        data.add("app", "tap", 900)
        data.add("app", "tap", 900)
        data.add("app", "tap", 900)
        self.assertIsNone(data.p90("app", "tap"))

    def test_the_window_is_bounded_and_survives_a_restart(self):
        data = SettleStore(self.path)
        for index in range(WINDOW + 10):
            data.add("app", "tap", index)
        self.assertEqual(len(data.samples("app", "tap")), WINDOW)
        self.assertEqual(SettleStore(self.path).samples("app", "tap"), data.samples("app", "tap"))
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), oct(0o600))

    def test_a_broken_file_is_an_empty_store(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        self.assertEqual(SettleStore(self.path).samples("app", "tap"), [])

    def test_bad_samples_and_unknown_apps_are_dropped(self):
        data = SettleStore(self.path)
        data.add("", "tap", 100)
        data.add("app", "tap", float("nan"))
        data.add("app", "tap", -1)
        self.assertFalse(self.path.exists())

    def test_shapes(self):
        self.assertEqual([shape_of(op) for op in ("TAP", "SWIPE_UP", "SWIPE_LEFT", "LAUNCH_APP", "WHATEVER", None)],
                         ["tap", "swipe_y", "swipe_x", "launch", "other", "other"])


class ArrivalTests(unittest.TestCase):
    def test_arrival_is_the_first_read_of_the_agreeing_run(self):
        reads = [(1.1, 1.3, "b"), (1.4, 1.6, "c"), (1.7, 1.9, "c")]
        self.assertAlmostEqual(predict.arrival_ms(1.0, reads, "a"), 400)

    def test_no_arrival_without_agreement_or_after_no_change(self):
        self.assertIsNone(predict.arrival_ms(1.0, [(1.1, 1.3, "b")], "a"))
        self.assertIsNone(predict.arrival_ms(1.0, [(1.1, 1.3, "b"), (1.4, 1.6, "c")], "a"))
        self.assertIsNone(predict.arrival_ms(1.0, [(1.1, 1.3, "a"), (1.4, 1.6, "a")], "a"))


class PredictiveSettleTests(RecordedDriverCase):
    def setUp(self):
        super().setUp()
        self.store = SettleStore(Path(tempfile.mkdtemp()) / "durations.json")
        patcher = patch.object(predict, "_store", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = self.search.bundle_id

    def teach(self, ms=400, shape="tap", count=MIN_SAMPLES):
        for _ in range(count):
            self.store.add(self.app, shape, ms)

    def settle(self, *screens, flag="1", op="TAP", **options):
        if flag:
            os.environ[store.FLAG] = flag
        stream = {key: options.pop(key) for key in ("usable", "effect", "stable", "quiet") if key in options}
        driver = self.driver(*screens, **stream)
        driver.execute(op, self.search.elements[0], self.search)
        before = self.clock.now
        calls = driver.call.reads if hasattr(driver.call, "reads") else None
        read = driver.wait_for_change(self.search, timeout=1.6, wait_seconds=.6, **options)
        row = driver.frame_clock.records[-1]
        return driver, read, row, self.clock.now - before, calls

    def test_a_learned_wait_then_one_read_settles_a_busy_screen(self):
        self.teach(400)
        driver, read, row, wall, _ = self.settle(self.sheet, usable=False, single_read=True)
        self.assertEqual(read.content_fingerprint, self.sheet.content_fingerprint)
        self.assertEqual((driver.settled_by, row["path"], row["reads"]), ("predicted", "predicted", 1))
        self.assertEqual((row["predicted"], row["outcome"], row["shape"]), (400, "hit", "tap"))
        self.assertGreaterEqual(wall, .4)
        self.assertEqual(driver.predicted_settle["outcome"], "hit")
        self.assertEqual(len(self.store.samples(self.app, "tap")), MIN_SAMPLES)  # a hit teaches nothing

    def test_without_a_prediction_the_loop_polls_and_learns(self):
        driver, read, row, _, _ = self.settle(self.sheet, usable=False, single_read=True)
        self.assertEqual((row["path"], row["outcome"]), ("frame_fallback", "none"))
        self.assertGreaterEqual(row["reads"], 2)
        self.assertIsNone(driver.settled_by)
        self.assertIn("arrival_ms", row)
        self.assertEqual(len(self.store.samples(self.app, "tap")), 1)

    def test_the_switch_off_changes_nothing(self):
        self.teach(400)
        driver, _, row, _, _ = self.settle(self.sheet, flag=None, usable=False, single_read=True)
        self.assertNotIn("outcome", row)
        self.assertNotIn("arrival_ms", row)
        self.assertIsNone(driver.predicted_settle)
        self.assertEqual(len(self.store.samples(self.app, "tap")), MIN_SAMPLES)
        self.assertGreaterEqual(row["reads"], 2)

    def test_a_caller_that_does_not_verify_gets_the_loop(self):
        self.teach(400)
        driver, _, row, _, _ = self.settle(self.sheet, usable=False)
        self.assertNotIn("outcome", row)
        self.assertIsNone(driver.settled_by)

    def test_a_read_of_the_old_screen_falls_back_to_the_loop(self):
        self.teach(400)
        driver, read, row, _, _ = self.settle(self.search, self.sheet, usable=False, single_read=True)
        self.assertEqual((row["outcome"], row["path"]), ("unchanged", "frame_fallback"))
        self.assertIsNone(driver.settled_by)
        self.assertEqual(read.content_fingerprint, self.sheet.content_fingerprint)
        self.assertEqual(row["reads"], driver.call.reads)

    def test_pixels_moving_during_the_read_fall_back_to_the_loop(self):
        self.teach(400)
        driver, read, row, _, _ = self.settle(self.sheet, stable=False, quiet=[False, True, True, True],
                                              single_read=True)
        self.assertEqual((row["outcome"], row["miss"]), ("moved", "not_stable"))
        self.assertIsNone(driver.settled_by)
        self.assertGreater(row["reads"], 1)

    def test_a_wait_never_runs_past_the_settle_cap(self):
        self.teach(MAX_PREDICTION_MS)
        _, _, row, wall, _ = self.settle(self.sheet, usable=False, single_read=True)
        self.assertEqual(row["outcome"], "hit")
        self.assertLess(wall, 1.5)

    def test_early_callers_may_use_the_prediction_too(self):
        self.teach(300)
        driver, _, row, _, _ = self.settle(self.sheet, usable=False, early=True)
        self.assertEqual((driver.settled_by, row["outcome"]), ("predicted", "hit"))

    def test_shapes_are_learned_apart(self):
        self.teach(400, shape="swipe_y")
        _, _, row, _, _ = self.settle(self.sheet, usable=False, single_read=True)
        self.assertEqual(row["outcome"], "none")


class StreamTests(RecordedDriverCase):
    def test_the_wda_observer_asks_the_frame_clock_and_declines_an_unusable_watch(self):
        driver = self.driver(self.sheet)
        watch = driver.frame_clock.mark()
        self.assertTrue(WdaMjpegObserver(driver.frame_clock, watch).still_between(0, 1))
        driver.frame_clock.usable = False
        self.assertIsNone(WdaMjpegObserver(driver.frame_clock, driver.frame_clock.mark()).still_between(0, 1))

    def test_the_phone_stream_is_a_stub_that_never_vouches(self):
        self.assertIsNone(PhoneScreenStream().still_between(0, 1))
        self.assertIn("unverified", PhoneScreenStream.UNVERIFIED)


if __name__ == "__main__":
    unittest.main()

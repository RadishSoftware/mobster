import threading
import unittest
from unittest.mock import patch

from mobile_agent.drivers import WDA
from mobile_agent.transport import NotSent, TransportError
from mobile_agent.wda import watchdog as wd
from mobile_agent.wda.watchdog import ActionBlocked, ActionTracker, Watchdog

URL = "http://127.0.0.1:8100"


class Rig:
    def __init__(self, busy=False, alive=True):
        self.actions = ActionTracker()
        self.restarts, self.ended = [], []
        self.busy_now, self.alive_now = busy, alive
        self.watchdog = Watchdog(lambda: self.restarts.append(1), url=lambda: URL, busy=lambda: self.busy_now,
                                 alive=lambda: self.alive_now, on_dead_runner=self.ended.append,
                                 actions=self.actions, wall=lambda: 1_700_000_000.0)

    def fail(self, start, count, step=10.0):
        return any(self.watchdog.probe_result(False, start + step * i) for i in range(count))


class Wedging(unittest.TestCase):
    def test_a_slow_start_is_not_wedged(self):
        rig = Rig()
        rig.watchdog.probe_result(False, 0)
        rig.watchdog.probe_result(False, 10)
        self.assertEqual(rig.watchdog.state, "ok")

    def test_consecutive_failures_wedge_and_a_good_probe_clears_it(self):
        rig = Rig()
        for index in range(3):
            rig.watchdog.probe_result(False, index * 10)
        self.assertEqual(rig.watchdog.state, "wedged")
        rig.watchdog.probe_result(True, 40)
        self.assertEqual(rig.watchdog.snapshot(), {"state": "ok", "restarts": 0, "last_restart": None})

    def test_restart_after_the_wedge_has_lasted_long_enough_and_is_recorded(self):
        rig = Rig()
        self.assertFalse(rig.fail(0, 9))
        self.assertTrue(rig.watchdog.probe_result(False, 90))
        self.assertEqual(rig.restarts, [1])
        self.assertEqual(rig.watchdog.snapshot(), {"state": "ok", "restarts": 1, "last_restart": 1_700_000_000_000})

    def test_a_failing_restart_is_wedged_and_waits_out_the_backoff(self):
        rig = Rig()
        rig.watchdog.restart = lambda: (_ for _ in ()).throw(RuntimeError("xcodebuild"))
        rig.fail(0, 10)
        self.assertEqual(rig.watchdog.state, "wedged")
        self.assertEqual(rig.watchdog.restarts, 1)
        self.assertFalse(rig.fail(100, 16))
        self.assertEqual(rig.watchdog.restarts, 1)
        self.assertEqual(rig.watchdog.deferred, "backoff")


class Backoff(unittest.TestCase):
    def test_no_restart_storm(self):
        rig = Rig()
        times = []
        for index in range(0, 400):
            now = index * 10.0
            if rig.watchdog.probe_result(False, now):
                times.append(now)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertGreaterEqual(len(times), 3, times)
        self.assertTrue(all(gap >= wd.MIN_RESTART_GAP for gap in gaps), gaps)
        self.assertGreater(gaps[1], gaps[0], "the gap grows for restarts in a row")

    def test_the_gap_is_capped(self):
        rig = Rig()
        rig.watchdog.streak = 20
        self.assertEqual(rig.watchdog.gap(), wd.MAX_RESTART_GAP)

    def test_the_streak_resets_after_a_healthy_stretch(self):
        rig = Rig()
        rig.watchdog.streak = 3
        rig.watchdog.probe_result(True, 0)
        rig.watchdog.probe_result(True, wd.HEALTHY_SECONDS + 1)
        self.assertEqual(rig.watchdog.gap(), wd.MIN_RESTART_GAP)


class ActionsInFlight(unittest.TestCase):
    def test_an_action_in_flight_is_never_restarted_under(self):
        rig = Rig(busy=True)
        token = rig.actions.begin(URL, "POST", "/wda/keys")
        self.assertIsNotNone(token)
        self.assertFalse(rig.fail(0, 12))
        self.assertEqual(rig.restarts, [])
        self.assertEqual(rig.watchdog.deferred, "action_in_flight")
        self.assertEqual(rig.watchdog.state, "wedged")

    def test_once_the_action_ends_the_restart_may_go_ahead_between_actions(self):
        rig = Rig(busy=True)
        token = rig.actions.begin(URL, "POST", "/actions")
        rig.fail(0, 12)
        rig.actions.end(token, "done")
        self.assertTrue(rig.watchdog.probe_result(False, 130))
        self.assertEqual(rig.restarts, [1])

    def test_an_unknown_outcome_with_a_task_running_waits_for_the_task(self):
        rig = Rig(busy=True)
        rig.actions.run_started(URL, "run1")
        token = rig.actions.begin(URL, "POST", "/wda/dragfromtoforduration")
        rig.actions.end(token, "unknown")
        self.assertFalse(rig.fail(0, 12))
        self.assertEqual(rig.watchdog.deferred, "unknown_outcome")
        self.assertEqual(rig.restarts, [])
        rig.actions.run_ended(URL, "run1")
        self.assertTrue(rig.watchdog.probe_result(False, 130))

    def test_the_decision_and_the_block_are_one_step(self):
        tracker = ActionTracker()
        tracker.run_started(URL, "r")
        token = tracker.begin(URL, "POST", "/actions")
        self.assertEqual(tracker.admit_restart(URL), "action_in_flight")
        tracker.end(token, "unknown")
        self.assertEqual(tracker.admit_restart(URL), "unknown_outcome")
        self.assertNotIn(wd.ActionTracker._key(URL), tracker.blocked)

    def test_a_run_cannot_start_while_a_restart_is_blocking(self):
        tracker = ActionTracker()
        self.assertIsNone(tracker.admit_restart(URL))
        with self.assertRaises(ActionBlocked):
            tracker.run_started(URL, "r")
        tracker.unblock(URL)
        tracker.run_started(URL, "r")

    def test_a_run_that_starts_after_the_watchdog_looked_is_still_seen(self):
        rig = Rig(busy=False)
        rig.actions.run_started(URL, "r")
        token = rig.actions.begin(URL, "POST", "/actions")
        rig.actions.end(token, "unknown")
        self.assertFalse(rig.fail(0, 12))
        self.assertEqual(rig.watchdog.deferred, "unknown_outcome")

    def test_ending_the_run_retires_its_unknown_outcomes_for_every_later_run(self):
        tracker = ActionTracker()
        tracker.run_started(URL, "r1")
        tracker.end(tracker.begin(URL, "POST", "/wda/keys"), "unknown")
        tracker.run_ended(URL, "r1")
        self.assertEqual(tracker.unknown_outcomes(URL), [])
        tracker.run_started(URL, "r2")
        self.assertIsNone(tracker.admit_restart(URL))

    def test_a_restart_blocks_new_actions_until_it_is_over(self):
        rig = Rig()
        seen = []

        def restart():
            try:
                rig.actions.begin(URL, "POST", "/wda/keys")
            except ActionBlocked as error:
                seen.append(str(error))
        rig.watchdog.restart = restart
        rig.fail(0, 10)
        self.assertEqual(len(seen), 1)
        self.assertIn("nothing sent", seen[0])
        self.assertIsNotNone(rig.actions.begin(URL, "POST", "/wda/keys"))

    def test_a_restart_waits_when_a_second_thread_has_an_action_in_flight(self):
        tracker = ActionTracker()
        started, release = threading.Event(), threading.Event()

        def action():
            wd.tracker
            token = tracker.begin(URL, "POST", "/wda/pressButton")
            started.set()
            release.wait(2)
            tracker.end(token, "done")

        thread = threading.Thread(target=action)
        thread.start()
        started.wait(2)
        self.assertEqual(tracker.admit_restart(URL), "action_in_flight")
        release.set()
        thread.join(2)
        self.assertIsNone(tracker.admit_restart(URL))


class DeadRunner(unittest.TestCase):
    def test_a_dead_runner_seen_only_by_the_tracker_still_ends_the_task(self):
        rig = Rig(busy=False, alive=False)
        rig.actions.run_started(URL, 'r')
        rig.fail(0, 12)
        self.assertTrue(rig.ended)
        self.assertEqual(set(rig.ended), {wd.DEAD_RUNNER})

    def test_the_task_is_not_ended_again_once_it_is_over(self):
        rig = Rig(busy=False, alive=False)
        rig.actions.run_started(URL, 'r')

        def end(message):
            rig.ended.append(message)
            rig.actions.run_ended(URL, 'r')
        rig.watchdog.on_dead_runner = end
        rig.fail(0, 20)
        self.assertEqual(rig.ended, [wd.DEAD_RUNNER])

    def test_a_dead_runner_with_a_task_ends_the_task_with_a_plain_sentence(self):
        rig = Rig(busy=True, alive=False)
        rig.fail(0, 12)
        self.assertEqual(rig.restarts, [])
        self.assertEqual(rig.ended[0], wd.DEAD_RUNNER)
        self.assertNotIn("passcode", wd.DEAD_RUNNER.lower())
        self.assertIn("No further action was taken", wd.DEAD_RUNNER)

    def test_a_dead_runner_with_no_task_is_left_to_the_supervisor(self):
        rig = Rig(busy=False, alive=False)
        rig.fail(0, 12)
        self.assertEqual((rig.restarts, rig.ended), ([], []))


class NeverResent(unittest.TestCase):
    class Http:
        def __init__(self, error):
            self.error, self.sent = error, []

        def request(self, method, path, body=None, timeout=None):
            self.sent.append((method, path, timeout))
            if self.error is not None:
                raise self.error
            return {"value": None}

        def close(self):
            pass

    def driver(self, error):
        driver = WDA(URL, "S1")
        driver.http = self.Http(error)
        driver._hint_app = lambda *a, **k: None
        return driver

    def setUp(self):
        isolated = patch.object(wd, "tracker", ActionTracker())
        isolated.start()
        self.addCleanup(isolated.stop)

    def test_a_timed_out_action_is_sent_once_and_recorded_unknown(self):
        driver = self.driver(TransportError("HTTP TimeoutError; request outcome unknown; not retried"))
        with self.assertRaises(TransportError):
            driver.call("POST", "/wda/keys", {"value": ["a"]})
        self.assertEqual(len(driver.http.sent), 1)
        self.assertEqual([(m, p) for m, p, _ in wd.tracker.unknown_outcomes(URL)], [("POST", "/wda/keys")])
        self.assertEqual(wd.tracker.in_flight(URL), [])

    def test_a_refused_connection_is_a_known_outcome_not_an_unknown_one(self):
        driver = self.driver(NotSent("HTTP ConnectionRefusedError; nothing sent; not retried"))
        with self.assertRaises(TransportError):
            driver.call("POST", "/actions", {"actions": []})
        self.assertEqual(wd.tracker.unknown_outcomes(URL), [])

    def test_only_a_refusal_the_transport_names_is_known_whatever_the_text_says(self):
        driver = self.driver(TransportError("HTTP ConnectionRefusedError; nothing sent; not retried"))
        with self.assertRaises(TransportError):
            driver.call("POST", "/actions", {"actions": []})
        self.assertEqual([(m, p) for m, p, _ in wd.tracker.unknown_outcomes(URL)], [("POST", "/actions")])

    def test_an_action_during_a_restart_is_refused_before_it_is_sent(self):
        driver = self.driver(None)
        self.assertIsNone(wd.tracker.admit_restart(URL))
        try:
            with self.assertRaises(ActionBlocked):
                driver.call("POST", "/wda/pressButton", {"name": "home"})
        finally:
            wd.tracker.unblock(URL)
        self.assertEqual(driver.http.sent, [])
        driver.call("POST", "/wda/pressButton", {"name": "home"})
        self.assertEqual(len(driver.http.sent), 1)

    def test_reads_during_a_restart_are_not_blocked_and_use_the_probe_bound(self):
        driver = self.driver(None)
        self.assertIsNone(wd.tracker.admit_restart(URL))
        try:
            driver.call("GET", "/wda/locked")
        finally:
            wd.tracker.unblock(URL)
        self.assertEqual(driver.http.sent[0][2], 3.0)

    def test_the_watchdog_itself_sends_nothing(self):
        import inspect
        source = inspect.getsource(wd.Watchdog)
        for word in ("request(", "urlopen", ".call("):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()

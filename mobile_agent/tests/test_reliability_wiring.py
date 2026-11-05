import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import device_manager as dm
from mobile_agent import lockscreen, owner_presence
from mobile_agent.agent_hooks import READY, GuardVerdict
from mobile_agent.fleet import Fleet
from mobile_agent.harness import prewarm
from mobile_agent.runner_renewal import RunnerRenewal
from mobile_agent.server import Runtime
from mobile_agent.tests.fakes import Clock
from mobile_agent.tests.test_harness import Fakes
from mobile_agent.tests.test_lockscreen import FakeWda
from mobile_agent.tests.test_runner_renewal import HOUR, NOW, FakeManager, renewal
from mobile_agent.wda import watchdog as wda_watchdog

URL = "http://127.0.0.1:8100"


def owner_on(url):
    presence = owner_presence.OwnerPresence(clock=lambda: 1000.0)
    presence.observe(locked=True, now=1000.0)
    presence.observe(locked=False, front="com.apple.springboard", now=1010.0)
    assert presence.is_owner_active()
    owner_presence.register(url, presence)
    return presence


class Prewarm(unittest.TestCase):
    def setUp(self):
        prewarm.reset_for_tests()
        self.addCleanup(owner_presence.forget, "http://127.0.0.1:8203")

    def test_prewarm_waits_while_the_owner_is_active_and_touches_nothing(self):
        fakes = Fakes(self)
        owner_on("http://127.0.0.1:8203")
        answer = prewarm.prewarm(fakes.runtime)
        self.assertEqual(answer, {"warmed": [], "pending": False, "reason": "owner_active"})
        self.assertEqual(fakes.drivers, [])

    def test_prewarm_still_warms_an_idle_phone(self):
        fakes = Fakes(self)
        self.assertEqual(prewarm.prewarm(fakes.runtime)["warmed"], ["wda", "screen"])


class Guard(unittest.TestCase):
    def guard(self, owner, wait_s=0.0, **kw):
        return lockscreen.DetectGuard(wait_s=wait_s, owner_active=lambda: owner, **kw)

    def test_an_unattended_check_on_a_locked_phone_in_use_says_the_owner_has_it(self):
        phone = FakeWda(locked=True)
        verdict = self.guard(True).check(phone, cause="preflight")
        self.assertEqual(verdict, GuardVerdict("stop", "owner_active", lockscreen.OWNER_HAS_PHONE))
        self.assertNotIn("passcode", lockscreen.OWNER_HAS_PHONE.lower())
        self.assertNotIn("unlock", lockscreen.OWNER_HAS_PHONE.lower())

    def test_an_unattended_check_on_a_locked_idle_phone_still_says_locked(self):
        verdict = self.guard(False).check(FakeWda(locked=True), cause="preflight")
        self.assertEqual(verdict, GuardVerdict("stop", "phone_locked", lockscreen.PHONE_LOCKED))

    def test_a_task_a_person_started_still_waits_for_them(self):
        events, ticks = [], iter(range(1000, 5000))
        calls = iter([True, False])

        class Phone(FakeWda):
            def _sessionless(self, method, path, body=None, timeout=None):
                if path == "/wda/locked":
                    return {"value": next(calls, False)}
                return super()._sessionless(method, path, body, timeout)

        guard = self.guard(True, wait_s=30, emit=events.append, sleep=lambda s: None, clock=lambda: float(next(ticks)))
        self.assertEqual(guard.check(Phone(), cause="preflight"), READY)
        self.assertEqual(events[0]["event"], "handoff_waiting")

    def test_a_reader_that_fails_leaves_the_lock_rules_in_place(self):
        def broken():
            raise RuntimeError

        guard = lockscreen.DetectGuard(owner_active=broken)
        self.assertEqual(guard.check(FakeWda(locked=True), cause="preflight").code, "phone_locked")

    def test_this_build_still_never_unlocks(self):
        self.assertFalse(lockscreen.CAN_UNLOCK)
        self.assertEqual(lockscreen.unlock_settings()["supported"], False)


class Admission(unittest.TestCase):
    def runtime(self):
        return SimpleNamespace(owner_blocks_start=lambda phone, origin, key=None: Runtime.owner_blocks_start(
            None, phone, origin, key))

    def setUp(self):
        self.addCleanup(owner_presence.forget, URL)

    def test_unattended_origins_wait_while_the_owner_is_active(self):
        owner_on(URL)
        phone = SimpleNamespace(wda_url=URL)
        for origin, key in (("schedule", "workflow:w:scheduled:1"), ("mcp", None), ("api", None), ("script", None)):
            with self.subTest(origin=origin):
                self.assertTrue(Runtime.owner_blocks_start(None, phone, origin, key))

    def test_a_task_a_person_starts_in_the_app_still_runs(self):
        from mobile_agent import harness_api
        owner_on(URL)
        phone = SimpleNamespace(wda_url=URL)
        for origin in harness_api.INTERACTIVE:
            with self.subTest(origin=origin):
                self.assertFalse(Runtime.owner_blocks_start(None, phone, origin, None))

    def test_a_scheduled_task_started_from_the_app_origin_is_still_unattended(self):
        owner_on(URL)
        from mobile_agent import harness_api
        origin = next(iter(harness_api.INTERACTIVE))
        self.assertTrue(Runtime.owner_blocks_start(None, SimpleNamespace(wda_url=URL), origin,
                                                   "workflow:w:scheduled:1"))

    def test_nothing_waits_when_the_owner_is_idle_or_unknown(self):
        phone = SimpleNamespace(wda_url=URL)
        self.assertFalse(Runtime.owner_blocks_start(None, phone, "mcp", None))
        owner_presence.register(URL, owner_presence.OwnerPresence())
        self.assertFalse(Runtime.owner_blocks_start(None, phone, "mcp", None))

class IdleRelease(unittest.TestCase):
    def runtime(self, run=None):
        released = []
        video = SimpleNamespace(release_idle=lambda: released.append("video"))
        runtime = SimpleNamespace(run_on=lambda phone: run,
                                  awake=SimpleNamespace(release=lambda owner: released.append(owner)))
        return runtime, SimpleNamespace(video=video, wda_url="http://127.0.0.1:8300"), released

    def test_an_idle_phone_lets_go_of_video_the_prewarmed_screen_and_the_awake_hold(self):
        runtime, phone, released = self.runtime()
        with patch.object(prewarm, "release") as drop:
            Runtime.release_idle(runtime, phone)
        self.assertEqual(released, ["video", "video"])
        drop.assert_called_once_with("http://127.0.0.1:8300")

    def test_a_phone_a_task_holds_is_left_alone(self):
        runtime, phone, released = self.runtime(run="abc")
        Runtime.release_idle(runtime, phone)
        self.assertEqual(released, [])

    def test_the_manager_calls_the_hooks_once_after_the_idle_timeout(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        called = []
        manager.idle_hooks.append(lambda: called.append(1))
        clock = [1_000_000.0]
        manager.presence.clock = lambda: clock[0]
        manager.presence.run_finished(locked=True, now=clock[0])
        poller = manager.presence_poller
        poller.reader = lambda url: (True, True, None)
        poller.lease = lambda url: False
        with patch.object(manager, "wda_ready", return_value=True):
            for step in range(20):
                clock[0] += 15
                poller.polled_at = None
                manager.poll_presence()
        self.assertEqual(called, [1])

class DeviceRecord(unittest.TestCase):
    def test_a_phone_with_a_manager_reports_owner_watchdog_and_renewal(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        manager.renewal = SimpleNamespace(public=lambda: {"state": "due", "deadline": 5})
        fields = Fleet._reliability_fields(SimpleNamespace(manager=manager))
        self.assertEqual(sorted(fields), ["owner", "renewal", "watchdog"])
        self.assertEqual(fields["owner"]["state"], "unknown")
        self.assertEqual(fields["watchdog"], {"state": "ok", "restarts": 0, "last_restart": None})
        self.assertEqual(fields["renewal"]["state"], "due")

    def test_a_task_running_defers_the_manager_restart_to_the_task(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        manager.runner = SimpleNamespace(running=True)
        manager.busy = lambda: True
        from mobile_agent.wda import watchdog as wda_watchdog
        token = wda_watchdog.tracker.begin(manager.wda_url, "POST", "/wda/keys")
        self.addCleanup(wda_watchdog.tracker.end, token)
        with patch.object(manager, "wda_ready", return_value=True), \
                patch.object(manager, "_wda_get", side_effect=OSError("timed out")), \
                patch.object(manager, "start_runner") as restart:
            now = 1000.0
            while now < 1300:
                manager.probe(now=now)
                now += dm.PROBE_INTERVAL
        restart.assert_not_called()
        self.assertEqual(manager.watchdog.state, "wedged")


class RestartAdmission(unittest.TestCase):

    def manager(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        self.addCleanup(wda_watchdog.tracker.unblock, manager.wda_url)
        return manager

    def silent_probes(self, manager, until=200):
        with patch.object(manager, "wda_ready", return_value=False), patch.object(manager, "start_runner") as restart:
            now = 1000.0
            while now < 1000.0 + until:
                manager.probe(now=now)
                now += dm.PROBE_INTERVAL
        return restart

    def test_the_probe_restarts_a_runner_that_is_alive_past_its_start_up_and_silent(self):
        manager = self.manager()
        manager.runner = SimpleNamespace(running=True, alive=True)
        manager.runner_started_at = time.monotonic() - dm.STARTUP_GRACE_SECONDS - 1
        restart = self.silent_probes(manager)
        restart.assert_called_once()
        self.assertEqual(manager.watchdog.snapshot()["restarts"], 1)

    def test_the_probe_leaves_a_runner_that_is_stopped_starting_or_not_running_alone(self):
        for label, runner, started in (
                ("stopped on purpose", None, None),
                ("still starting", SimpleNamespace(running=True, alive=True), time.monotonic()),
                ("process gone", SimpleNamespace(running=True, alive=False),
                 time.monotonic() - dm.STARTUP_GRACE_SECONDS - 1)):
            with self.subTest(label):
                manager = self.manager()
                manager.runner, manager.runner_started_at = runner, started
                restart = self.silent_probes(manager)
                restart.assert_not_called()
                self.assertEqual((manager.watchdog.state, manager.watchdog.failures), ("ok", 0))

    def autostarting(self, manager):
        started = []

        def start():
            started.append(1)
            manager.runner = SimpleNamespace(running=False, alive=False)   # it died again
        return started, patch.multiple(manager, settings=lambda: {"autostart": True}, built=lambda: True,
                                       device=lambda: "<UDID>", wda_alive=lambda: False, _start_runner=start)

    def test_autostart_starts_the_first_runner_and_a_later_one_after_admission(self):
        manager = self.manager()
        started, patches = self.autostarting(manager)
        with patches:
            manager.autostart()
            self.assertEqual(len(started), 1)           # the first start asks nothing of the backoff
            manager.autostart()
            self.assertEqual(len(started), 2)           # a relaunch is admitted: nothing in flight, no backoff yet
        self.assertEqual(manager.watchdog.restarts, 1)
        self.assertNotIn(wda_watchdog.ActionTracker._key(manager.wda_url), wda_watchdog.tracker.blocked)

    def test_autostart_waits_out_the_backoff_after_a_relaunch(self):
        manager = self.manager()
        started, patches = self.autostarting(manager)
        with patches:
            manager.autostart()
            manager.autostart()
            manager.autostart()                         # straight after the last one
            manager.autostart()
        self.assertEqual(len(started), 2)
        self.assertEqual(manager.watchdog.restarts, 1)

    def test_autostart_does_not_relaunch_under_an_action_in_flight(self):
        manager = self.manager()
        started, patches = self.autostarting(manager)
        with patches:
            manager.autostart()
            wda_watchdog.tracker.run_started(manager.wda_url, "r")
            token = wda_watchdog.tracker.begin(manager.wda_url, "POST", "/wda/keys")
            self.addCleanup(wda_watchdog.tracker.run_ended, manager.wda_url, "r")
            manager.autostart()
            self.assertEqual(len(started), 1)
            wda_watchdog.tracker.end(token, "done")
            manager.autostart()
            self.assertEqual(len(started), 2)

    def test_the_relaunch_gate_admits_once_and_releases_the_block(self):
        manager = self.manager()
        release = manager._relaunch_gate()
        self.assertTrue(callable(release))
        with self.assertRaises(wda_watchdog.ActionBlocked):     # actions wait while the relaunch starts
            wda_watchdog.tracker.run_started(manager.wda_url, "r")
        release()
        wda_watchdog.tracker.run_started(manager.wda_url, "r")
        wda_watchdog.tracker.run_ended(manager.wda_url, "r")
        self.assertIsNone(manager._relaunch_gate())             # the shared backoff holds the next one

    def test_a_task_is_refused_while_the_helper_restarts_and_starts_once_it_is_over(self):
        from mobile_agent.api_errors import APIError
        from mobile_agent.tests.test_conversations_support import ThreadsBase

        class Case(ThreadsBase):
            def runTest(self):
                pass

        base = Case()
        base.setUp()
        self.addCleanup(base.doCleanups)
        runtime = base.runtime()
        url = runtime.fleet.primary.wda_url
        self.addCleanup(wda_watchdog.tracker.unblock, url)
        self.assertIsNone(wda_watchdog.tracker.admit_restart(url))
        with self.assertRaises(APIError) as refused:
            runtime.create(None, "Open Settings", "live", engine="smart")
        self.assertEqual((refused.exception.status, refused.exception.code), (503, "device_unavailable"))
        self.assertIn("No action was taken", str(refused.exception))
        self.assertEqual(runtime.runs, {})
        wda_watchdog.tracker.unblock(url)
        run = runtime.create(None, "Open Settings", "live", engine="smart")    # the lease was not leaked
        base.finish(runtime, run)
        base.settle(runtime)
        self.assertIn(run.id, runtime.runs)


class RenewalSchedule(unittest.TestCase):

    def job(self, hours_left, **kw):
        manager = FakeManager(hours_left * HOUR, **kw)
        return manager, renewal(manager)

    def test_day_4_is_not_due_and_day_5_6_6_9_are(self):
        for day, due in ((4.0, False), (4.99, False), (5.0, True), (6.0, True), (6.9, True)):
            with self.subTest(day=day):
                manager, job = self.job(7 * 24 - day * 24)
                self.assertEqual(job.tick(), due)
                self.assertEqual(bool(manager.builds), due)

    def test_day_7_and_after_reports_overdue_with_the_deadline(self):
        for days_past in (0, 0.5, 3):
            with self.subTest(days_past=days_past):
                manager, job = self.job(-days_past * 24, running=False)
                manager.health = {"responsive": None, "locked": None}
                status = job.status()
                self.assertTrue(status["overdue"])
                self.assertEqual(status["deadline"], round((NOW - days_past * 24 * HOUR) * 1000))
                self.assertLessEqual(status["hours_left"], 0)

    def test_it_waits_while_the_owner_is_active_and_goes_when_they_stop(self):
        manager, job = self.job(30)
        manager.presence = owner_on(URL)
        self.addCleanup(owner_presence.forget, URL)
        self.assertFalse(job.tick())
        self.assertEqual(manager.builds, [])
        self.assertEqual((job.status()["state"], job.status()["waiting_for"]), ("due", "owner_active"))
        manager.presence = owner_presence.OwnerPresence()
        self.assertTrue(job.tick())

    def test_it_waits_for_a_task_and_for_another_build(self):
        manager, job = self.job(30)
        job.busy = lambda: True
        self.assertFalse(job.tick())
        self.assertEqual(job.reason_to_wait(), "busy")

    def test_a_failure_is_retried_and_sooner_as_the_deadline_nears(self):
        for hours_left, expected_wait in ((47, 6 * HOUR), (24, 6 * HOUR), (6, 1.5 * HOUR), (2, 30 * 60),
                                          (0.5, 15 * 60)):
            with self.subTest(hours_left=hours_left):
                clock = Clock(NOW)
                manager = FakeManager(hours_left * HOUR)
                job = RunnerRenewal(manager, busy=lambda: False, clock=clock)
                self.assertTrue(job.tick())
                manager.build = {"state": "failed", "error": "Xcode"}
                job.tick()
                self.assertEqual(job.status()["state"], "failed")
                clock.now += expected_wait - 1
                self.assertFalse(job.tick(), "too soon")
                clock.now += 2
                self.assertTrue(job.tick(), "retried")


if __name__ == "__main__":
    unittest.main()

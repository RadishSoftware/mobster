
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import device_manager as dm
from mobile_agent import signing
from mobile_agent.runner_renewal import RETRY_AFTER, RunnerRenewal
from mobile_agent.setup_service import SetupService
from mobile_agent.tests.fakes import Clock
from mobile_agent.wda import patches as wda_patches

HOUR = 3600
NOW = 1_800_000_000.0
PHONE = {"udid": "00008130-001A2B3C4D5E6F70", "name": "Test iPhone", "trusted": True}


class FakeManager:
    def __init__(self, expires_in, device=PHONE, locked=False, running=True, stale=False):
        self.stale = stale
        self.expires_at = NOW + expires_in
        self.phone = device
        self.health = {"responsive": True, "locked": locked}
        self.build = {"state": "idle", "error": None, "log_tail": []}
        self.runner = SimpleNamespace(running=running)
        self.builds, self.restarts = [], 0

    def settings(self):
        return {"team": "ABCDE12345", "udid": PHONE["udid"], "built_for": PHONE["udid"]}

    def signature_expiry(self):
        return self.expires_at

    def out_of_date(self):
        return self.stale

    def device(self, devices=None):
        return self.phone

    def start_build(self, team, renewal=False):
        self.builds.append((team, renewal))
        self.build = {"state": "running", "error": None, "log_tail": []}

    def start_runner(self):
        self.restarts += 1


def renewal(manager, clock=None, busy=False, coredevice=None):
    return RunnerRenewal(manager, busy=lambda: busy, clock=clock or Clock(NOW), lock_reader=lambda udid: coredevice)


class WhenTests(unittest.TestCase):
    def test_it_renews_48_hours_before_expiry_and_at_24_hours(self):
        for hours in (48, 24, 1):
            with self.subTest(hours=hours):
                manager = FakeManager(hours * HOUR)
                self.assertTrue(renewal(manager).tick())
                self.assertEqual(manager.builds, [("ABCDE12345", True)])

    def test_a_helper_signed_here_without_xcode_is_never_rebuilt_by_a_renewal(self):
        manager = FakeManager(1 * HOUR)
        manager.settings = lambda: {"team": "ABCDE12345", "udid": PHONE["udid"], "driver": "pymobiledevice3"}
        self.assertFalse(renewal(manager).tick())
        self.assertEqual(manager.builds, [])
        manager.settings = lambda: {"team": "ABCDE12345", "udid": PHONE["udid"], "driver": "xcodebuild"}
        self.assertTrue(renewal(manager).tick())

    def test_never_with_the_phone_locked_or_disconnected(self):
        for manager, reason in ((FakeManager(24 * HOUR, locked=True), "locked"),
                                (FakeManager(24 * HOUR, device=None), "disconnected"),
                                (FakeManager(24 * HOUR, device={**PHONE, "trusted": False}), "disconnected")):
            with self.subTest(reason=reason):
                job = renewal(manager)
                self.assertFalse(job.tick())
                self.assertEqual(manager.builds, [])
                self.assertEqual(job.status(), {**job.status(), "state": "due", "waiting_for": reason})
        manager = FakeManager(24 * HOUR, running=False)
        manager.health = {"responsive": None, "locked": None}
        job = renewal(manager)
        self.assertFalse(job.tick())
        self.assertEqual(manager.builds, [])
        self.assertEqual((job.status()["state"], job.status()["waiting_for"]), ("due", "runner_stopped"))
        manager.health = {"responsive": False, "locked": None}
        self.assertEqual(job.reason_to_wait(), "locked")

    def test_a_verification_run_can_widen_the_window_past_a_fresh_signature(self):
        manager = FakeManager(167 * HOUR)
        self.assertEqual(renewal(manager).reason_to_wait(), "not_due")
        with patch.dict("os.environ", {"MOBSTER_RENEW_WINDOW_HOURS": "170"}):
            job = renewal(manager)
            self.assertTrue(job.tick())
            self.assertEqual(job.status()["window_hours"], 170)
        self.assertEqual(manager.builds, [("ABCDE12345", True)])
        for value, hours in (("", 48), ("abc", 48), ("0", 48), ("500", 48), ("24", 24), ("170", 170)):
            with self.subTest(value=value):
                self.assertEqual(signing.renew_window({"MOBSTER_RENEW_WINDOW_HOURS": value}), hours * HOUR)


class StoppedRunnerTests(unittest.TestCase):
    def stopped(self, expires_in, stale=False):
        manager = FakeManager(expires_in, running=False, stale=stale)
        manager.health = {"responsive": None, "locked": None}
        return manager

    def test_an_update_that_changed_the_patches_rebuilds_the_runner_once_the_phone_reads_unlocked(self):
        manager = self.stopped(150 * HOUR, stale=True)
        job = renewal(manager, coredevice=False)
        self.assertIsNone(job.reason_to_wait())
        self.assertTrue(job.tick())
        self.assertEqual(manager.builds, [("ABCDE12345", True)])
        self.assertEqual(job.status()["state"], "renewing")
        self.assertFalse(job.tick())
        manager.build = {"state": "succeeded", "error": None, "log_tail": []}
        manager.stale = False
        self.assertFalse(job.tick())
        self.assertIsNone(job.status()["error"])
        self.assertEqual(job.reason_to_wait(), "not_due")
        self.assertEqual(len(manager.builds), 1)

    def test_an_expired_runner_renews_by_itself_once_the_phone_reads_unlocked(self):
        manager = self.stopped(-2 * HOUR)
        job = renewal(manager, coredevice=False)
        self.assertTrue(job.tick())
        self.assertEqual(manager.builds, [("ABCDE12345", True)])

    def test_never_while_coredevice_says_locked_or_cant_say(self):
        for coredevice, reason in ((True, "locked"), (None, "runner_stopped")):
            with self.subTest(coredevice=coredevice):
                for manager in (self.stopped(150 * HOUR, stale=True), self.stopped(-2 * HOUR)):
                    job = renewal(manager, coredevice=coredevice)
                    self.assertFalse(job.tick())
                    self.assertEqual(manager.builds, [])
                    self.assertEqual((job.status()["state"], job.status()["waiting_for"]), ("due", reason))

    def test_a_failed_rebuild_after_an_update_is_tried_again_hours_later(self):
        clock = Clock(NOW)
        manager = self.stopped(150 * HOUR, stale=True)
        job = renewal(manager, clock, coredevice=False)
        self.assertTrue(job.tick())
        manager.build = {"state": "failed", "error": "Sign in again in Xcode"}
        self.assertFalse(job.tick())
        clock.now += RETRY_AFTER - 1
        self.assertFalse(job.tick())
        self.assertEqual(job.reason_to_wait(), "retry_later")
        clock.now += 2
        self.assertTrue(job.tick())
        self.assertEqual(len(manager.builds), 2)

    def test_coredevice_is_asked_at_most_every_30_seconds(self):
        clock, reads = Clock(NOW), []
        manager = self.stopped(150 * HOUR, stale=True)

        def reader(udid):
            reads.append(udid)
            return True
        job = RunnerRenewal(manager, busy=lambda: False, clock=clock, lock_reader=reader)
        for _ in range(6):
            job.tick()
            clock.now += 5
        self.assertEqual(reads, [PHONE["udid"]] * 1)
        clock.now += 30
        job.tick()
        self.assertEqual(len(reads), 2)


class AfterTests(unittest.TestCase):
    def test_a_running_runner_restarts_between_tasks_with_the_new_signature(self):
        busy = [False]
        manager = FakeManager(24 * HOUR)
        job = RunnerRenewal(manager, busy=lambda: busy[0], clock=Clock(NOW))
        self.assertTrue(job.tick())
        manager.build = {"state": "succeeded", "error": None}
        manager.expires_at = NOW + 7 * 24 * HOUR
        busy[0] = True
        job.tick()
        self.assertEqual(manager.restarts, 0)
        busy[0] = False
        job.tick()
        self.assertEqual(manager.restarts, 1)
        job.tick()
        self.assertEqual(manager.restarts, 1)
        self.assertEqual(job.status()["state"], "idle")
        self.assertEqual(job.reason_to_wait(), "not_due")

    def test_a_signature_xcode_did_not_extend_is_reported_not_claimed(self):
        manager = FakeManager(24 * HOUR, running=False)
        job = renewal(manager)
        job.tick()
        manager.build = {"state": "succeeded", "error": None}
        job.tick()
        self.assertEqual(job.status()["state"], "failed")
        self.assertIn("reused the old signature", job.status()["error"])
        self.assertEqual(manager.restarts, 0)


class WiringTests(unittest.TestCase):
    def test_setup_attaches_renewal_to_the_watch_loop_and_reports_it(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        runtime = SimpleNamespace(active=None, config=SimpleNamespace(enable_live=True),
                                  video=SimpleNamespace(status=lambda: {}))
        service = SetupService(runtime, manager, Path(tempfile.mkdtemp()) / "agent.env")
        self.assertIs(manager.renewal, service.renewal)
        runtime.active = "abc123abc123"
        self.assertTrue(service.renewal.busy())
        manager.save_settings(udid=PHONE["udid"], built_for=PHONE["udid"], built_patches=wda_patches.patch_digest(),
                              team="ABCDE12345",
                              expires_at=time.time() + 20 * HOUR)
        tools = {"xcodebuild": None, "iproxy": None, "idevice_id": None, "ideviceinfo": None, "git": None,
                 "xcode": {"state": "missing", "version": None, "app": None, "selected": None, "fixes": [],
                           "developer_dir": None}}
        with patch.object(manager, "tools", return_value=tools), patch.object(manager, "usb_iphones", return_value=0), \
                patch.object(manager, "teams", return_value=[]), patch.object(manager, "wda_ready",
                                                                              return_value=False), \
                patch.object(manager, "devices", return_value=[]):
            state = service.state()
        self.assertAlmostEqual(state["runner"]["expires_at"] / 1000, time.time() + 20 * HOUR, delta=5)
        self.assertEqual(state["runner"]["renewal"]["state"], "due")
        self.assertEqual(state["runner"]["renewal"]["waiting_for"], "disconnected")

    def test_the_watch_loop_ticks_the_renewal(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        ticks = []
        manager.renewal = SimpleNamespace(tick=lambda: ticks.append(1) or (_ for _ in ()).throw(RuntimeError("x")))
        stop = SimpleNamespace(calls=0)

        def is_set():
            stop.calls += 1
            return stop.calls > 1
        stop.is_set, stop.wait = is_set, lambda seconds: None
        with patch.object(manager, "autostart"), patch.object(manager, "probe"):
            manager.watch(stop)
        self.assertEqual(ticks, [1])


if __name__ == "__main__":
    unittest.main()

"""The runner renews itself before a free Apple ID's 7 days run out. Fake clock, fake builder. Offline."""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import device_manager as dm
from mobile_agent.runner_renewal import RETRY_AFTER, RunnerRenewal
from mobile_agent.setup_service import SetupService

HOUR = 3600
NOW = 1_800_000_000.0
PHONE = {"udid": "00008130-001A2B3C4D5E6F70", "name": "Test iPhone", "trusted": True}


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class FakeManager:
    def __init__(self, expires_in, device=PHONE, locked=False, running=True):
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

    def device(self, devices=None):
        return self.phone

    def start_build(self, team, renewal=False):
        self.builds.append((team, renewal))
        self.build = {"state": "running", "error": None, "log_tail": []}

    def start_runner(self):
        self.restarts += 1


def renewal(manager, clock=None, busy=False):
    return RunnerRenewal(manager, busy=lambda: busy, clock=clock or Clock())


class WhenTests(unittest.TestCase):
    def test_it_renews_48_hours_before_expiry_and_at_24_hours(self):
        for hours in (48, 24, 1):
            with self.subTest(hours=hours):
                manager = FakeManager(hours * HOUR)
                self.assertTrue(renewal(manager).tick())
                self.assertEqual(manager.builds, [("ABCDE12345", True)])

    def test_it_waits_while_more_than_48_hours_are_left(self):
        manager = FakeManager(48 * HOUR + 60)
        job = renewal(manager)
        self.assertFalse(job.tick())
        self.assertEqual(job.reason_to_wait(), "not_due")
        self.assertEqual(job.status()["state"], "idle")
        self.assertEqual(manager.builds, [])

    def test_never_with_the_phone_locked_or_disconnected(self):
        for manager, reason in ((FakeManager(24 * HOUR, locked=True), "locked"),
                                (FakeManager(24 * HOUR, device=None), "disconnected"),
                                (FakeManager(24 * HOUR, device={**PHONE, "trusted": False}), "disconnected")):
            with self.subTest(reason=reason):
                job = renewal(manager)
                self.assertFalse(job.tick())
                self.assertEqual(manager.builds, [])
                self.assertEqual(job.status(), {**job.status(), "state": "due", "waiting_for": reason})
        # A stopped runner can't say the phone is unlocked, so that waits too, and says why.
        manager = FakeManager(24 * HOUR, running=False)
        manager.health = {"responsive": None, "locked": None}
        job = renewal(manager)
        self.assertFalse(job.tick())
        self.assertEqual(manager.builds, [])
        self.assertEqual((job.status()["state"], job.status()["waiting_for"]), ("due", "runner_stopped"))
        # WDA that answers but not for the phone reads as locked.
        manager.health = {"responsive": False, "locked": None}
        self.assertEqual(job.reason_to_wait(), "locked")

    def test_a_verification_run_can_widen_the_window_past_a_fresh_signature(self):
        """MOBSTER_RENEW_WINDOW_HOURS=170 renews a just-built runner now, so the owner can watch it work."""
        manager = FakeManager(167 * HOUR)
        self.assertEqual(renewal(manager).reason_to_wait(), "not_due")
        with patch.dict("os.environ", {"MOBSTER_RENEW_WINDOW_HOURS": "170"}):
            job = renewal(manager)
            self.assertTrue(job.tick())
            self.assertEqual(job.status()["window_hours"], 170)
        self.assertEqual(manager.builds, [("ABCDE12345", True)])
        for value, hours in (("", 48), ("abc", 48), ("0", 48), ("500", 48), ("24", 24), ("170", 170)):
            with self.subTest(value=value):
                self.assertEqual(dm.renew_window({"MOBSTER_RENEW_WINDOW_HOURS": value}), hours * HOUR)

    def test_the_widened_window_drops_the_cached_profile_before_building(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        stale = Path(tempfile.mkdtemp()) / "old.mobileprovision"
        stale.write_text("x")
        log = SimpleNamespace(lines=[], write=lambda text: log.lines.append(text))
        with patch.object(manager, "signature_expiry", return_value=time.time() + 160 * HOUR), \
                patch.object(dm.signing, "stale_runner_profiles", return_value=[stale]) as find:
            manager._drop_stale_profiles("ABCDE12345", log)
            self.assertTrue(stale.exists())              # 160 hours left is outside the usual 48
            with patch.dict("os.environ", {"MOBSTER_RENEW_WINDOW_HOURS": "170"}):
                manager._drop_stale_profiles("ABCDE12345", log)
        self.assertFalse(stale.exists())
        self.assertEqual(find.call_count, 1)
        self.assertIn("Removed an expiring runner profile", log.lines[0])

    def test_never_during_a_task_or_another_build(self):
        manager = FakeManager(24 * HOUR)
        self.assertFalse(renewal(manager, busy=True).tick())
        manager.build = {"state": "running"}
        self.assertFalse(renewal(manager).tick())
        self.assertEqual(manager.builds, [])

    def test_once_per_signature_and_a_failure_is_retried_hours_later(self):
        clock = Clock()
        manager = FakeManager(24 * HOUR)
        job = renewal(manager, clock)
        self.assertTrue(job.tick())
        self.assertFalse(job.tick())                    # still building
        self.assertEqual(job.status()["state"], "renewing")
        manager.build = {"state": "failed", "error": "Choose your Apple Account"}
        self.assertFalse(job.tick())
        self.assertEqual(job.status()["state"], "failed")
        clock.now += RETRY_AFTER - 1
        self.assertFalse(job.tick())
        clock.now += 2
        self.assertTrue(job.tick())
        self.assertEqual(len(manager.builds), 2)


class AfterTests(unittest.TestCase):
    def test_a_running_runner_restarts_between_tasks_with_the_new_signature(self):
        busy = [False]
        manager = FakeManager(24 * HOUR)
        job = RunnerRenewal(manager, busy=lambda: busy[0], clock=Clock())
        self.assertTrue(job.tick())
        manager.build = {"state": "succeeded", "error": None}
        manager.expires_at = NOW + 7 * 24 * HOUR
        busy[0] = True                                  # a task started meanwhile: never restart under it
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
        self.assertEqual(manager.restarts, 0)           # it wasn't running before


class WiringTests(unittest.TestCase):
    def test_setup_attaches_renewal_to_the_watch_loop_and_reports_it(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        runtime = SimpleNamespace(active=None, config=SimpleNamespace(enable_live=True),
                                  video=SimpleNamespace(status=lambda: {}))
        service = SetupService(runtime, manager, Path(tempfile.mkdtemp()) / "agent.env")
        self.assertIs(manager.renewal, service.renewal)
        runtime.active = "abc123abc123"
        self.assertTrue(service.renewal.busy())
        manager.save_settings(udid=PHONE["udid"], built_for=PHONE["udid"], team="ABCDE12345",
                              expires_at=time.time() + 20 * HOUR)
        tools = {"xcodebuild": None, "iproxy": None, "idevice_id": None, "ideviceinfo": None, "git": None,
                 "xcode": {"state": "missing", "version": None, "app": None, "selected": None, "fixes": [],
                           "developer_dir": None}}
        with patch.object(manager, "tools", return_value=tools), patch.object(manager, "usb_iphones", return_value=0), \
                patch.object(manager, "teams", return_value=[]), patch.object(manager, "wda_ready", return_value=False), \
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
            manager.watch(stop)                          # a failing tick never stops the loop
        self.assertEqual(ticks, [1])


if __name__ == "__main__":
    unittest.main()

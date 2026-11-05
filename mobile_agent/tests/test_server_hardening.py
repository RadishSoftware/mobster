import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent.api_errors import APIError
from mobile_agent.journal import Journal, JournalError, Lease
from mobile_agent.run import Run
from mobile_agent.server import Runtime, make_handler
from mobile_agent.tests.seam_support import request

APP = {"id": "settings", "name": "Settings", "bundleId": "com.apple.Preferences", "installed": True}
REAL_THREAD = threading.Thread


class ParkedWorker:
    starts = 0

    def __new__(cls, *args, **kwargs):
        # Listener callbacks run; other infrastructure stays parked without counting as a task worker.
        if kwargs.get("name") == "mobster-listeners":
            return REAL_THREAD(*args, **kwargs)
        return super().__new__(cls)

    def __init__(self, *args, **kwargs):
        self.task = getattr(kwargs.get("target"), "__name__", None) == "work"

    def start(self):
        if self.task:
            type(self).starts += 1

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


class ServerHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mobster-journal-")
        self.root = Path(self.temporary.name)
        self.runtimes = []
        ParkedWorker.starts = 0
        self.worker_patch = patch("mobile_agent.server.threading.Thread", ParkedWorker)
        self.worker_patch.start()
        self.lease_patch = patch("mobile_agent.journal.Lease.device",
                                 side_effect=lambda _: Lease(self.root / "device.lock"))
        self.lease_patch.start()
        self.device_patch = patch("mobile_agent.runtime_driver.resolve_wda_session",
                                  side_effect=AssertionError("No phone calls permitted"))
        self.device = self.device_patch.start()

    def tearDown(self):
        for runtime in reversed(self.runtimes):
            for run in runtime.runs.values():
                if run.lease:
                    run.lease.close()
                    run.lease = None
            runtime.close()
        self.device_patch.stop()
        self.lease_patch.stop()
        self.worker_patch.stop()
        self.temporary.cleanup()

    def runtime(self, persisted=False):
        config = SimpleNamespace(wda_url="http://127.0.0.1:8100", session=None, enable_live=True, port=8765,
                                 state_db=str(self.root / "runs.sqlite3") if persisted else None,
                                 data_dir=str(self.root))
        runtime = Runtime(config)
        runtime.apps = Mock(return_value=[dict(APP)])
        runtime.status = Mock(return_value={"live_enabled": True, "helper_configured": False})
        self.runtimes.append(runtime)
        return runtime

    def finish(self, runtime, run):
        run.status, run.finished_at = "completed_unverified", time.time()
        run.emit({"event": "run_finished", "status": run.status})
        if run.lease:
            run.lease.close()
            run.lease = None
        runtime.active_runs.clear()

    def post(self, runtime, body, key=None):
        status, data, _ = request(make_handler(runtime), "POST", "/api/runs", body,
                                  headers={"Idempotency-Key": key} if key else None)
        return status, data

    def test_run_preparation_failure_does_not_leave_a_device_lease(self):
        runtime = self.runtime()
        with patch.object(runtime, "remembering", side_effect=RuntimeError("setup failed")):
            with self.assertRaisesRegex(RuntimeError, "setup failed"):
                runtime.create("settings", "Open About", "live")
        run = runtime.create("settings", "Open About", "live")
        self.finish(runtime, run)

    def test_worker_start_failure_releases_a_pool_reservation(self):
        runtime = self.runtime()
        runtime.pool = Mock()
        runtime.pool.position.return_value = {"healthy": 1, "busy": 0}
        runtime.pool.acquire.return_value = SimpleNamespace(id="pooled")
        with patch.object(ParkedWorker, "start", side_effect=RuntimeError("no threads")):
            with self.assertRaises(APIError) as failed:
                runtime.create("settings", "Open About", "live")
        self.assertEqual(failed.exception.code, "start_failed")
        run = next(iter(runtime.runs.values()))
        runtime.pool.release.assert_called_once_with(run.id)
        self.assertEqual(run.status, "interrupted")
        self.assertEqual(runtime.active_runs, {})

    def test_listener_or_worker_construction_failure_interrupts_the_admitted_run(self):
        for failure in ("listener", "worker"):
            with self.subTest(failure=failure):
                runtime = self.runtime()
                target = (patch.object(runtime.listeners, "post", side_effect=RuntimeError("no threads"))
                          if failure == "listener" else
                          patch("mobile_agent.server.threading.Thread", side_effect=RuntimeError("no threads")))
                with target, self.assertRaises(APIError) as failed:
                    runtime.create("settings", "Open About", "live")
                self.assertEqual(failed.exception.code, "start_failed")
                run = next(iter(runtime.runs.values()))
                self.assertEqual(run.status, "interrupted")
                self.assertEqual(runtime.active_runs, {})
                Lease(self.root / "device.lock").close()

    def test_a_legacy_record_with_quick_modes_helper_model_restores_without_one(self):
        before = self.runtime(persisted=True)
        historical = Run(dict(APP), "Historical task", "live", status="completed_unverified", finished_at=time.time())
        before.journal.create({**historical.metadata(), "helperModel": "gemini-3.7-flash"})
        before.close()
        after = self.runtime(persisted=True)
        self.assertEqual(after.runs[historical.id].goal, "Historical task")
        self.assertNotIn("helperModel", after.runs[historical.id].public())

    def test_quick_modes_and_helper_model_are_refused_before_any_worker(self):
        runtime = self.runtime()
        for extra in ({"helperModel": "gemini-3.7-flash"},):
            status, _ = self.post(runtime, {"appId": "settings", "goal": "Read version", **extra})
            self.assertEqual(status, 400)
        self.assertEqual(ParkedWorker.starts, 0)
        self.assertEqual(runtime.runs, {})

    def test_http_output_format_is_validated_before_any_worker(self):
        runtime = self.runtime()
        body = {"appId": "settings", "goal": "Open About"}
        for bad in ({"outputFormat": "html"}, {"outputFormat": "auto", "outputSchema": {"type": "string"}}):
            self.assertEqual(self.post(runtime, {**body, **bad})[0], 400)
        self.assertEqual(ParkedWorker.starts, 0)
        status, response = self.post(runtime, {**body, "outputFormat": "yaml"})
        self.assertEqual(status, 201)
        self.assertEqual(response["run"]["outputFormat"], "yaml")

    def test_auto_is_distinct_from_explicit_text_and_survives_restart(self):
        before = self.runtime(persisted=True)
        original, _ = before.create("settings", "Open About", "live", "auto-key")
        with self.assertRaises(APIError) as conflict:
            before.create("settings", "Open About", "live", "auto-key", output_format="text")
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        self.finish(before, original)
        before.close()
        after = self.runtime(persisted=True)
        restored, reused = after.create("settings", "Open About", "live", "auto-key")
        self.assertTrue(reused)
        self.assertEqual(restored.id, original.id)
        self.assertEqual(restored.output_format, "auto")
        self.assertEqual(ParkedWorker.starts, 1)

    def test_an_invalid_output_schema_is_refused_before_any_worker_or_phone(self):
        runtime = self.runtime()
        status, _ = self.post(runtime, {"appId": "settings", "goal": "Read version", "outputFormat": "json",
                                        "outputSchema": {"type": "object"}})
        self.assertEqual(status, 400)
        self.assertEqual((ParkedWorker.starts, runtime.runs), (0, {}))
        self.device.assert_not_called()
        status, _ = self.post(runtime, {"appId": "settings", "goal": "Read version", "outputFormat": "json",
                                        "outputSchema": {"type": "object", "additionalProperties": False,
                                                         "properties": {"version": {"type": "string"}}}})
        self.assertEqual((status, ParkedWorker.starts), (201, 1))

    def test_shutdown_keeps_journal_until_scheduler_dispatch_stops(self):
        runtime = self.runtime()
        runtime.scheduler = Mock()
        runtime.scheduler.close.return_value = False
        self.assertFalse(runtime.close(timeout=0))
        self.assertIsNotNone(runtime.journal.connection)
        self.assertTrue(runtime.closing)
        runtime.scheduler.close.return_value = True
        self.assertTrue(runtime.close(timeout=0))
        self.assertIsNone(runtime.journal.connection)

    def test_other_request_cannot_replace_active_run(self):
        runtime = self.runtime()
        first, _ = runtime.create("settings", "Open About", "live", "first")
        with self.assertRaises(APIError) as raised:
            runtime.create("settings", "Open Wi-Fi", "live", "second")
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(runtime.active, first.id)
        self.assertEqual(ParkedWorker.starts, 1)

    def test_retained_request_tombstone_never_restarts_evicted_task(self):
        runtime = self.runtime()
        first, _ = runtime.create("settings", "Open About", "live", "first")
        for index in range(100):
            old = Run(dict(APP), f"Historical task {index}", "live", status="completed_unverified",
                      finished_at=time.time())
            runtime.journal.create(old.metadata())
            runtime.runs[old.id] = old
        self.assertIn(first.id, {run["id"] for run in runtime.journal.load()})
        first.finish({"event": "result", "status": "completed_unverified"})
        first.lease.close()
        first.lease = None
        runtime.active_runs.clear()
        runtime.create("settings", "Open Wi-Fi", "live", "new")
        self.assertFalse(first.id in runtime.runs, f"finished run {first.id} remained in history")
        self.assertEqual(runtime.journal.lookup("first")[0], first.id)
        with self.assertRaises(APIError) as raised:
            runtime.create("settings", "Open About", "live", "first")
        self.assertEqual((raised.exception.status, raised.exception.code), (410, "run_expired"))
        self.assertEqual(ParkedWorker.starts, 2)

    def test_restart_marks_pending_intent_interrupted_without_replay(self):
        before = self.runtime(persisted=True)
        original, _ = before.create("settings", "Open About", "live", "persisted-key")
        original.emit({"event": "action_started", "operation": "TAP", "target": "About"})
        original.lease.close()
        original.lease = None
        before.close()
        after = self.runtime(persisted=True)
        restored = after.runs[original.id]
        self.assertEqual(restored.status, "interrupted")
        self.assertIsNotNone(restored.finished_at)
        self.assertTrue(restored.summary["actions_may_have_run"])
        self.assertFalse(restored.summary["independently_verified"])
        replay, replayed = after.create("settings", "Open About", "live", "persisted-key")
        self.assertIs(replay, restored)
        self.assertTrue(replayed)
        self.assertEqual(ParkedWorker.starts, 1)

    def test_failed_creation_preserves_full_history(self):
        runtime = self.runtime()
        for index in range(100):
            old = Run(dict(APP), f"Historical task {index}", "live", status="completed_unverified",
                      finished_at=time.time())
            runtime.journal.create(old.metadata())
            runtime.runs[old.id] = old
        identifiers = list(runtime.runs)
        with patch.object(runtime.journal, "create", side_effect=JournalError("disk unavailable")):
            with self.assertRaises(JournalError):
                runtime.create("settings", "Open About", "live", "failed")
        self.assertEqual(list(runtime.runs), identifiers)
        self.assertEqual([record["id"] for record in runtime.journal.load()], identifiers)
        self.assertEqual(ParkedWorker.starts, 0)
        self.assertIsNone(runtime.active)
        Lease(self.root / "device.lock").close()
        self.device.assert_not_called()

    def test_action_intent_persistence_failure_dispatches_nothing(self):
        runtime = self.runtime()
        run = Run(dict(APP), "Open Search", "live", journal=runtime.journal)
        runtime.journal.create(run.metadata())
        original_append = runtime.journal.append

        from mobile_agent import smart_events
        from mobile_agent.frontier import FrontierAgent
        from mobile_agent.state import Element
        from mobile_agent.tests.test_frontier import Driver, Script, screen
        search = screen(Element("1", "Search", "Button", (.8, .06, .1, .05)), bundle="com.apple.Preferences")
        driver = Driver([search] * 4)
        events = smart_events.SmartEvents(run.emit, driver=driver, frame=lambda: None)
        agent = FrontierAgent(driver, Script([("TAP", "Search", None), ("DONE", None, None), ("DONE", None, None)]),
                              emit=events, screenshots=False, settle_seconds=0)
        events.agent = agent

        def append(metadata, event):
            if event["event"] == "frontier_decision":
                raise JournalError("disk unavailable")
            return original_append(metadata, event)

        with patch.object(runtime.journal, "append", side_effect=append):
            result = agent.run("Open Search")
        self.assertEqual(driver.actions, [])
        self.assertEqual(result["status"], "error")
        self.assertIn("JournalError", result["reason"])
        self.assertFalse(any(event["event"] in ("frontier_decision", "frontier_action", "step")
                             for event in run.events))
        driver = Driver([search] * 4)
        events = smart_events.SmartEvents(run.emit, driver=driver, frame=lambda: None)
        events.agent = FrontierAgent(driver, Script([("TAP", "Search", None), ("DONE", None, None),
                                                     ("DONE", None, None)]),
                                     emit=events, screenshots=False, settle_seconds=0)
        events.agent.run("Open Search")
        self.assertEqual(driver.actions, [("TAP", "Search", None)])
        kinds = [event["event"] for event in run.events]
        self.assertLess(kinds.index("frontier_decision"), kinds.index("frontier_action"))

    def test_second_runtime_cannot_own_same_device(self):
        first, second = self.runtime(), self.runtime()
        first.create("settings", "Open About", "live", "first")
        with self.assertRaises(APIError) as raised:
            second.create("settings", "Open Wi-Fi", "live", "second")
        self.assertEqual((raised.exception.status, raised.exception.code), (409, "device_busy"))
        self.assertEqual(ParkedWorker.starts, 1)

    def test_stop_before_work_skips_launch_and_inference(self):
        runtime = self.runtime()
        run, _ = runtime.create("settings", "Open About", "live", "stop-first")
        runtime.stop_run(run.id)
        with patch.object(runtime, "smart_client") as model, \
                patch("mobile_agent.runtime_driver.build_target_driver") as driver:
            runtime.work(run)
        self.assertEqual(run.status, "stopped")
        self.assertIsNotNone(run.finished_at)
        self.assertIsNone(runtime.active)
        self.assertIsNone(run.lease)
        self.device.assert_not_called()
        model.assert_not_called()
        driver.assert_not_called()

    def test_http_same_key_returns_200_and_different_body_409(self):
        runtime = self.runtime()
        body = {"appId": "settings", "goal": "Open About", "mode": "live"}
        status, first = self.post(runtime, body, "http-key")
        self.assertEqual(status, 201)
        status, replay = self.post(runtime, body, "http-key")
        self.assertEqual(status, 200)
        self.assertEqual(first["run"]["id"], replay["run"]["id"])
        status, _ = self.post(runtime, {**body, "goal": "Open Wi-Fi"}, "http-key")
        self.assertEqual(status, 409)
        self.assertEqual(ParkedWorker.starts, 1)

    def test_http_default_is_live_and_demo_is_rejected(self):
        runtime = self.runtime()
        status, body = self.post(runtime, {"appId": "settings", "goal": "Open About"}, "default-live")
        self.assertEqual((status, body["run"]["mode"], body["run"]["outputFormat"]), (201, "live", "auto"))
        status, _ = self.post(runtime, {"appId": "settings", "goal": "Open About", "mode": "demo"}, "no-demo")
        self.assertEqual(status, 400)
        self.assertEqual(ParkedWorker.starts, 1)

    def test_journal_owner_lease_is_exclusive_and_releasable(self):
        path = self.root / "exclusive.sqlite3"
        first = Journal(path)
        try:
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with self.assertRaises(JournalError):
                Journal(path)
        finally:
            first.close()
        replacement = Journal(path)
        replacement.close()

    def test_corrupt_event_sequence_fails_closed(self):
        path = self.root / "corrupt.sqlite3"
        journal = Journal(path)
        run = Run(dict(APP), "Open About", "live")
        journal.create(run.metadata())
        journal.append(run.metadata(), {"event": "action_started", "seq": 2})
        with self.assertRaises(JournalError):
            journal.load()
        journal.close()

    def test_request_and_run_are_saved_atomically(self):
        journal = Journal()
        try:
            first, second = Run(dict(APP), "First", "live"), Run(dict(APP), "Second", "live")
            journal.create(first.metadata(), "same-key", "first-fingerprint")
            with self.assertRaises(JournalError):
                journal.create(second.metadata(), "same-key", "second-fingerprint")
            self.assertEqual([record["id"] for record in journal.load()], [first.id])
            self.assertEqual(journal.lookup("same-key"), (first.id, "first-fingerprint"))
        finally:
            journal.close()

    def test_journal_symlink_is_rejected_without_touching_target(self):
        target = self.root / "target.txt"
        target.touch()
        link = self.root / "linked.sqlite3"
        link.symlink_to(target)
        with self.assertRaises(JournalError):
            Journal(link)
        self.assertEqual(target.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()

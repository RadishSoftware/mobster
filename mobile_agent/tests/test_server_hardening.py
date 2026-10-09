"""Durability/idempotency tests with no provider, phone, or HTTP socket calls."""

import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from email.message import Message
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mobile_agent.agent import Agent
from mobile_agent.demo import DemoDriver, DemoModel
from mobile_agent.journal import Journal, JournalError, Lease
from mobile_agent.server import APIError, Run, Runtime, make_handler


APP = {"id": "settings", "name": "Settings", "bundleId": "com.apple.Preferences", "installed": True}
REAL_THREAD = threading.Thread


class ParkedWorker:
    """Records scheduling without running a worker or touching a phone."""
    starts = 0

    def __new__(cls, *args, **kwargs):
        # The run listeners' dispatcher (seam S6.4) is a real thread, not a task worker: a track's listener runs.
        if kwargs.get("name") == "mobster-listeners":
            return REAL_THREAD(*args, **kwargs)
        return super().__new__(cls)

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
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
        self.lease_patch = patch("mobile_agent.server.Lease.device", side_effect=lambda _: Lease(self.root / "device.lock"))
        self.lease_patch.start()
        self.device_patch = patch("mobile_agent.server.resolve_wda_session", side_effect=AssertionError("No phone calls permitted"))
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
                                 state_db=str(self.root / "runs.sqlite3") if persisted else None)
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
        runtime.active = None

    def post(self, runtime, body, key=None, path="/api/runs"):
        handler = object.__new__(make_handler(runtime))
        raw = json.dumps(body).encode()
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(raw))
        if key:
            handler.headers["Idempotency-Key"] = key
        handler.path = path
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.connection = Mock()
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        handler.do_POST()
        return statuses[-1], json.loads(handler.wfile.getvalue())

    def test_same_key_replays_existing_run_without_new_worker(self):
        runtime = self.runtime()
        first, replayed = runtime.create("settings", "Open About", "live", "request-1")
        second, replayed_again = runtime.create("settings", "Open About", "live", "request-1")
        self.assertFalse(replayed)
        self.assertTrue(replayed_again)
        self.assertIs(first, second)
        self.assertEqual(ParkedWorker.starts, 1)
        self.assertEqual(len(runtime.runs), 1)

    def test_helper_model_is_captured_and_idempotent_despite_default_change(self):
        runtime = self.runtime()
        with patch.dict(os.environ, {"TEXT_MODEL_PROVIDER": "vertex", "TEXT_MODEL": "gemini-3.5-flash-lite"}):
            run, _ = runtime.create("settings", "Read version", "live", "model-default")
        with patch.dict(os.environ, {"TEXT_MODEL_PROVIDER": "vertex", "TEXT_MODEL": "gemini-3.7-flash"}):
            repeated, replayed = runtime.create("settings", "Read version", "live", "model-default")
        self.assertTrue(replayed)
        self.assertIs(repeated, run)
        self.assertEqual(run.public()["helperModel"], "gemini-3.5-flash-lite")
        self.assertEqual(ParkedWorker.starts, 1)

    def test_explicit_model_is_request_identity_and_survives_restart(self):
        with patch.dict(os.environ, {"TEXT_MODEL_PROVIDER": "vertex", "TEXT_MODEL": "gemini-3.5-flash-lite"}):
            before = self.runtime(persisted=True)
            run, _ = before.create("settings", "Read version", "live", "model-explicit", helper_model="gemini-3.7-flash")
            with self.assertRaises(APIError) as conflict:
                before.create("settings", "Read version", "live", "model-explicit", helper_model="gemini-3.1-pro-preview")
            self.assertEqual(conflict.exception.code, "idempotency_conflict")
            self.finish(before, run)
            before.close()
            after = self.runtime(persisted=True)
            restored, reused = after.create("settings", "Read version", "live", "model-explicit", helper_model="gemini-3.7-flash")
            self.assertTrue(reused)
            self.assertEqual(restored.helper_model, "gemini-3.7-flash")
            self.assertEqual(restored.id, run.id)
            self.assertEqual(ParkedWorker.starts, 1)

    def test_legacy_metadata_does_not_invent_a_historical_model(self):
        before = self.runtime(persisted=True)
        historical = Run(dict(APP), "Historical task", "live", status="completed_unverified", finished_at=time.time())
        metadata = historical.metadata()
        metadata.pop("helperModel")
        before.journal.create(metadata)
        before.close()
        with patch.dict(os.environ, {"TEXT_MODEL": "gemini-3.7-flash"}):
            after = self.runtime(persisted=True)
        self.assertIsNone(after.runs[historical.id].public()["helperModel"])

    def test_http_model_validation_precedes_worker_creation(self):
        runtime = self.runtime()
        with patch.dict(os.environ, {"TEXT_MODEL_PROVIDER": "vertex", "TEXT_MODEL": "gemini-3.5-flash-lite"}):
            for value in ("", "not-configured", [], True, "gemini-model\nsecret"):
                status, _ = self.post(runtime, {"appId": "settings", "goal": "Read version", "helperModel": value})
                self.assertEqual(status, 400)
            self.assertEqual(ParkedWorker.starts, 0)
            status, response = self.post(runtime, {"appId": "settings", "goal": "Read version", "helperModel": "gemini-3.7-flash"})
        self.assertEqual(status, 201)
        self.assertEqual(response["run"]["helperModel"], "gemini-3.7-flash")

    def test_selected_model_estimate_is_not_priced_as_default_even_for_empty_draft(self):
        runtime = self.runtime()
        with patch.dict(os.environ, {"TEXT_MODEL_PROVIDER": "vertex", "TEXT_MODEL": "gemini-3.5-flash-lite", "GOOGLE_CLOUD_LOCATION": "global"}):
            status, estimate = self.post(runtime, {"goal": "", "helperModel": "gemini-3.7-flash"}, path="/api/usage/estimate")
            self.assertEqual(status, 200)
            self.assertEqual(estimate["helperModel"], "gemini-3.7-flash")
            self.assertEqual(estimate["rates"]["google"]["input_usd_per_million"], 1.5)
            self.assertEqual(estimate["rates"]["google"]["output_usd_per_million"], 7.5)
            status, default = self.post(runtime, {"goal": "", "helperModel": None}, path="/api/usage/estimate")
        self.assertEqual(status, 200)
        self.assertEqual(default["helperModel"], "gemini-3.5-flash-lite")
        self.assertIsNotNone(default["estimated_min_usd"])
        self.assertGreater(estimate["estimated_min_usd"], default["estimated_min_usd"])
        self.assertEqual(ParkedWorker.starts, 0)

    def test_status_reports_default_and_documented_model_allowlist(self):
        runtime = self.runtime()
        with patch.dict(os.environ, {"TEXT_MODEL_PROVIDER": "vertex", "TEXT_MODEL": "gemini-3.5-flash-lite", "GOOGLE_CLOUD_LOCATION": "global"}):
            status = Runtime.status(runtime)
        self.assertEqual(status["helper_model"], "gemini-3.5-flash-lite")
        self.assertEqual(status["helper_models"], ["gemini-3.5-flash-lite", "gemini-3.7-flash", "gemini-3.1-pro-preview"])
        self.assertEqual(set(status["helper_model_rates"]), set(status["helper_models"]))
        self.assertEqual(status["helper_model_rates"]["gemini-3.1-pro-preview"]["input_usd_per_million"], 2)

    def test_other_provider_or_region_does_not_advertise_global_vertex_prices(self):
        runtime = self.runtime()
        for provider, region in (("openrouter", "global"), ("vertex", "us-central1")):
            with patch.dict(os.environ, {"TEXT_MODEL_PROVIDER": provider, "TEXT_MODEL": "gemini-3.7-flash", "GOOGLE_CLOUD_LOCATION": region}):
                self.assertEqual(Runtime.status(runtime)["helper_model_rates"], {})

    def test_output_format_is_request_identity_and_explicit_json_is_compatible(self):
        runtime = self.runtime()
        original, _ = runtime.create("settings", "Open About", "live", "format-key", output_format="json")
        replay, reused = runtime.create("settings", "Open About", "live", "format-key", output_format="json")
        self.assertTrue(reused)
        self.assertIs(replay, original)
        with self.assertRaises(APIError) as raised:
            runtime.create("settings", "Open About", "live", "format-key", output_format="yaml")
        self.assertEqual(raised.exception.code, "idempotency_conflict")
        self.assertEqual(ParkedWorker.starts, 1)

    def test_output_format_survives_restart_and_replay(self):
        before = self.runtime(persisted=True)
        original, _ = before.create("settings", "Open About", "live", "markdown-key", output_format="markdown")
        self.finish(before, original)
        before.close()
        after = self.runtime(persisted=True)
        restored, reused = after.create("settings", "Open About", "live", "markdown-key", output_format="markdown")
        self.assertTrue(reused)
        self.assertEqual(restored.public()["outputFormat"], "markdown")
        self.assertEqual(restored.id, original.id)
        self.assertEqual(ParkedWorker.starts, 1)

    def test_http_output_format_is_validated_before_any_worker(self):
        runtime = self.runtime()
        body = {"appId": "settings", "goal": "Open About", "outputFormat": "html"}
        status, _ = self.post(runtime, body)
        self.assertEqual(status, 400)
        self.assertEqual(ParkedWorker.starts, 0)
        status, response = self.post(runtime, {**body, "outputFormat": "yaml"})
        self.assertEqual(status, 201)
        self.assertEqual(response["run"]["outputFormat"], "yaml")

    def test_new_api_tasks_default_to_auto(self):
        runtime = self.runtime()
        status, response = self.post(runtime, {"appId": "settings", "goal": "Open About"})
        self.assertEqual(status, 201)
        self.assertEqual(response["run"]["outputFormat"], "auto")

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

    def test_auto_custom_schema_rejected_before_starting_worker(self):
        runtime = self.runtime()
        status, _ = self.post(runtime, {"appId": "settings", "goal": "Read version",
            "outputFormat": "auto", "outputSchema": {"type": "string"}})
        self.assertEqual(status, 400)
        self.assertEqual(ParkedWorker.starts, 0)

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

    def test_same_key_different_goal_is_conflict(self):
        runtime = self.runtime()
        runtime.create("settings", "Open About", "live", "request-1")
        with self.assertRaises(APIError) as raised:
            runtime.create("settings", "Open Wi-Fi", "live", "request-1")
        self.assertEqual((raised.exception.status, raised.exception.code), (409, "idempotency_conflict"))
        self.assertEqual(ParkedWorker.starts, 1)

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
        self.finish(runtime, first)
        for index in range(99):
            old = Run(dict(APP), f"Historical task {index}", "live", status="completed_unverified", finished_at=time.time())
            runtime.journal.create(old.metadata())
            runtime.runs[old.id] = old
        runtime.create("settings", "Open Wi-Fi", "live", "new")
        self.assertNotIn(first.id, runtime.runs)
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
            old = Run(dict(APP), f"Historical task {index}", "live", status="completed_unverified", finished_at=time.time())
            runtime.journal.create(old.metadata())
            runtime.runs[old.id] = old
        identifiers = list(runtime.runs)
        with patch.object(runtime.journal, "create", side_effect=JournalError("disk unavailable")):
            with self.assertRaises(JournalError):
                runtime.create("settings", "Open About", "live", "failed")
        self.assertEqual(list(runtime.runs), identifiers)
        self.assertEqual([record["id"] for record in runtime.journal.load()], identifiers)
        self.assertEqual(ParkedWorker.starts, 0)

    def test_persistence_failure_releases_lease_and_starts_no_worker(self):
        runtime = self.runtime()
        with patch.object(runtime.journal, "create", side_effect=JournalError("disk unavailable")):
            with self.assertRaises(JournalError):
                runtime.create("settings", "Open About", "live", "failed")
        self.assertIsNone(runtime.active)
        self.assertEqual(runtime.runs, {})
        self.assertEqual(ParkedWorker.starts, 0)
        with_lease = Lease(self.root / "device.lock")
        with_lease.close()
        self.device.assert_not_called()

    def test_action_intent_persistence_failure_dispatches_nothing(self):
        runtime = self.runtime()
        run = Run(dict(APP), "Open Search", "live", journal=runtime.journal)
        runtime.journal.create(run.metadata())
        original_append = runtime.journal.append

        def append(metadata, event):
            if event["event"] == "action_started":
                raise JournalError("disk unavailable")
            return original_append(metadata, event)

        driver = DemoDriver()
        with patch.object(runtime.journal, "append", side_effect=append):
            result = Agent(driver, DemoModel(), emit=run.emit).run("Open Search", execute=True)
        self.assertEqual(driver.actions, [])
        self.assertEqual(result["status"], "error")
        self.assertFalse(any(event["event"] == "action_started" for event in run.events))

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
        with patch("mobile_agent.server.build_models") as model, patch("mobile_agent.server.build_target_driver") as driver:
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
        self.assertEqual(status, 201)
        self.assertEqual(body["run"]["mode"], "live")
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

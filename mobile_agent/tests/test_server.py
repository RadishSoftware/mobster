import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from mobile_agent import device_manager as dm
from mobile_agent import eventbus, mac_tools, supervised
from mobile_agent import serve as serve_module
from mobile_agent.api_http import DESKTOP_ORIGINS
from mobile_agent.journal import REQUEST_LIMIT, Journal, JournalError
from mobile_agent.runtime_teach import CONTROL_STREAM_SECONDS, IDLE_STREAM_FPS
from mobile_agent.runtime_work import KEPT_PREVIEWS
from mobile_agent.server import BoundedServer, Runtime, make_handler
from mobile_agent.tests import test_server_engine
from mobile_agent.tests.test_engines import COMPOSE, Script, item
from mobile_agent.tests.test_server_engine import Base, jpeg
from mobile_agent.tests.test_wda_video import JPEG
from mobile_agent.tests.test_workflows import FakeRuntime
from mobile_agent.wda import patches as wda_patches
from mobile_agent.wda import watchdog as wda_watchdog
from mobile_agent.wda.video import WdaVideo, read_mjpeg_frames
from mobile_agent.workflows.runs import DAY_MS, MAX_DISPATCHES
from mobile_agent.workflows.scheduler import WorkflowScheduler
from mobile_agent.workflows.service import Workflows

KEY = "sk-test-000000000000"
PHONE = {"udid": "00008110-000000000000001E", "name": "iPhone", "trusted": True}


class OpenAIAnswer(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class HomeScreenWorkflowTests(Base):
    def test_a_home_screen_task_saves_as_a_workflow_and_runs_there(self):
        os.environ["OPENAI_API_KEY"] = KEY
        runtime = self.runtime()
        status, body, _ = self.request(runtime, "POST", "/api/runs", {"goal": "What iOS version is this iPhone on?"})
        run = body["run"]
        self.assertEqual((status, run["appId"], run["engine"]), (201, "any", "smart"))
        self.finish(runtime, runtime.runs[run["id"]])
        from_run = {"name": "iOS version", "appId": run["appId"], "goal": run["goal"], "outputSchema": None,
                    "outputFormat": run["outputFormat"], "engine": "fast"}
        for key, save, engine in (("saved-home-screen", from_run, "smart"),
                                  ("saved-plain", {"name": "Pizza", "goal": "Find pizza"}, None)):
            status, body, _ = self.request(runtime, "POST", "/api/workflows", save)
            self.assertEqual((status, body["workflow"]["appId"], body["workflow"]["engine"]), (201, None, engine), body)
            again, _ = runtime.workflows.run(body["workflow"]["id"], key)
            self.assertEqual((again.app["id"], again.engine), ("any", "smart"))
            self.finish(runtime, again)


class KeyRecheckTests(Base):
    def refused(self):
        os.environ["OPENAI_API_KEY"] = KEY
        runtime = self.runtime(managed=True)
        runtime.model_reach.record(KEY, False, "Your OpenAI key can't use gpt-5.6-sol.")
        self.assertFalse(runtime.status()["engines"]["smart"]["available"])
        return runtime

    def test_a_key_that_passes_the_test_takes_tasks_again(self):
        runtime = self.refused()
        with patch("urllib.request.OpenerDirector.open", side_effect=lambda *a, **k: OpenAIAnswer(b"{}")):
            status, check, _ = self.request(runtime, "POST", "/api/keys/test", {"target": "openai"})
        self.assertEqual((status, check["ok"]), (200, True), check)
        self.assertEqual(runtime.status()["engines"]["smart"]["available"], True)
        status, body, _ = self.request(runtime, "POST", "/api/runs", {"goal": "Open Settings"})
        self.assertEqual(status, 201, body)

    def test_a_key_saved_during_the_test_is_not_recorded_as_tested(self):
        runtime = self.refused()
        other = "sk-test-111111111111"

        def answer(*_args, **_kwargs):
            os.environ["OPENAI_API_KEY"] = other
            return OpenAIAnswer(b"{}")

        with patch("urllib.request.OpenerDirector.open", side_effect=answer):
            status, check, _ = self.request(runtime, "POST", "/api/keys/test", {"target": "openai"})
        self.assertEqual((status, check["ok"]), (200, True), check)
        self.assertNotIn(runtime.model_reach.digest(other), runtime.model_reach.results)

    def test_saving_the_key_again_checks_it_afresh(self):
        runtime = self.refused()
        status, _, _ = self.request(runtime, "POST", "/api/keys", {"openai": {"key": KEY}})
        self.assertEqual(status, 200)
        smart = runtime.status()["engines"]["smart"]
        self.assertEqual((smart["available"], smart["reason"]), (True, None))

    def test_a_key_that_fails_the_test_stays_refused(self):
        runtime = self.refused()
        denied = urllib.error.HTTPError("https://api.openai.com/v1/models", 404, "Not Found", {}, io.BytesIO(b"{}"))
        with patch("urllib.request.OpenerDirector.open", side_effect=denied):
            status, check, _ = self.request(runtime, "POST", "/api/keys/test", {"target": "openai"})
        self.assertEqual((status, check["ok"]), (200, False))
        self.request(runtime, "POST", "/api/keys", {"jev": {"key": "jev-key-000000000000"}})
        self.assertEqual(runtime.status()["engines"]["smart"]["reason"], "Your OpenAI key can't use gpt-5.6-sol.")


class StopDuringSetupTests(Base):
    Phone = test_server_engine.SmartTaskTests.Phone

    def setUp(self):
        super().setUp()
        session = patch("mobile_agent.runtime_driver.resolve_wda_session", return_value="s")
        session.start()
        self.addCleanup(session.stop)

    def work(self, runtime, run, phone, **stops):
        script = Script([[("DONE", None, None)]], [item()], answer="Done.")
        frames = iter([jpeg()] * 20)
        with patch("mobile_agent.runtime_driver.build_target_driver", return_value=phone), \
                patch("mobile_agent.runtime_driver.prepare_wda_phone", side_effect=stops.get("preflight")), \
                patch.object(runtime, "smart_apps", side_effect=stops.get("apps", lambda run: {})), \
                patch("mobile_agent.engines.build_client", return_value=script), \
                patch("mobile_agent.frontier.screen.video_frame", side_effect=lambda *a, **k: next(frames)):
            self.worker.stop()
            try:
                runtime.work(run)
            finally:
                self.worker.start()
        if run.lease:
            run.lease.close()
            run.lease = None
        return [e for e in run.events if e["event"] == "inference_finished"]

    def test_stop_during_the_phone_check_opens_no_app_and_calls_no_model(self):
        os.environ["OPENAI_API_KEY"] = KEY
        runtime = self.runtime()
        run = runtime.create("messages", "Text Sam I'm running late", "live")
        phone = self.Phone([COMPOSE] * 4)
        calls = self.work(runtime, run, phone, preflight=lambda driver, guard=None: runtime.stop_run(run.id))
        self.assertEqual(run.status, "stopped")
        self.assertEqual((phone.calls, calls), ([], []))

    def test_stop_while_the_loop_gets_ready_calls_no_model(self):
        os.environ["OPENAI_API_KEY"] = KEY
        runtime = self.runtime()
        run = runtime.create(None, "What iOS version is this iPhone on?", "live")

        def listing(_):
            runtime.stop_run(run.id)
            return {}

        calls = self.work(runtime, run, self.Phone([COMPOSE] * 4), apps=listing)
        self.assertEqual((run.status, calls), ("stopped", []))

class FakeService:
    started = None

    def __init__(self, name, command, log_path, env=None, gate=None, hold=None):
        self.name, self.running, self.alive = name, False, True

    def start(self):
        self.running = True
        self.started.append((self.name, time.monotonic()))

    def stop(self):
        self.running = False


class RunnerWatchTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("mobile_agent.devices.listening", lambda port, **kwargs: False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def manager(self, **settings):
        directory = tempfile.TemporaryDirectory(prefix="mobster-watch-")
        self.addCleanup(directory.cleanup)
        manager = dm.DeviceManager(directory.name, "http://127.0.0.1:9")
        manager.save_settings(team="ABCDE12345", udid=PHONE["udid"], built_for=PHONE["udid"],
                              built_patches=wda_patches.patch_digest(), **settings)
        return manager

    def fakes(self, manager, service):
        for target in (patch.object(supervised, "Supervised", service),
                       patch.object(mac_tools, "relay_command", return_value=["iproxy"]),
                       patch.object(wda_patches, "pin_loopback"),
                       patch.object(mac_tools, "tool", return_value="/usr/bin/true"),
                       patch.object(manager, "device", return_value=PHONE),
                       patch.object(manager, "built", return_value=True),
                       patch.object(manager, "expired", return_value=False),
                       patch.object(manager, "wda_alive", return_value=False),
                       patch.object(manager, "wda_ready", return_value=False),
                       patch.object(manager, "xcode_env", return_value=None)):
            target.start()
            self.addCleanup(target.stop)

    def test_closing_the_manager_ends_its_watch_loop_after_the_runner_started(self):
        manager = self.manager(autostart=True)
        started = []
        self.fakes(manager, type("Service", (FakeService,), {"started": started}))
        with patch.object(dm, "WATCH_INTERVAL", .02):
            watcher = threading.Thread(target=manager.watch, args=(manager.watching,), daemon=True)
            watcher.start()
            deadline = time.monotonic() + 2
            while not started and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(started, "autostart never started the runner")
            manager.close()
            closed_at = time.monotonic()
            watcher.join(1)
            time.sleep(.1)
        restarted = [name for name, at in started if at > closed_at]
        self.assertEqual((watcher.is_alive(), restarted), (False, []))

    def test_an_autostart_waiting_for_the_lock_during_close_starts_nothing(self):
        manager = self.manager(autostart=True)
        started = []
        self.fakes(manager, type("Service", (FakeService,), {"started": started}))
        with manager.runner_lock:
            waiting = threading.Thread(target=manager.autostart, daemon=True)
            waiting.start()
            time.sleep(.05)
            manager.watching.set()
        waiting.join(2)
        self.assertEqual(started, [])

    def test_two_runner_starts_never_overlap(self):
        manager = self.manager()
        inside, release, created = threading.Event(), threading.Event(), []

        class SlowService(FakeService):
            started = []

            def __init__(self, name, command, log_path, env=None, gate=None, hold=None):
                super().__init__(name, command, log_path, env, gate)
                created.append(name)

            def start(self):
                if self.name == "wda-runner" and not inside.is_set():
                    inside.set()
                    release.wait(2)
                super().start()

        self.fakes(manager, SlowService)
        first = threading.Thread(target=manager.start_runner, daemon=True)
        first.start()
        self.assertTrue(inside.wait(2))
        second = threading.Thread(target=manager.start_runner, daemon=True)
        second.start()
        second.join(.3)
        overlapped = not second.is_alive()
        release.set()
        first.join(2)
        second.join(2)
        self.assertFalse(overlapped, f"a second start ran inside the first; services created: {created}")
        self.assertEqual(created, ["wda-runner", "usb-relay"] * 2)

    def test_a_wedged_runner_restarts_at_most_once_per_min_restart_gap(self):
        manager = self.manager()
        started = []
        self.fakes(manager, type("Service", (FakeService,), {"started": started}))
        with patch.object(manager, "wda_ready", return_value=True), \
                patch.object(manager, "_wda_get", side_effect=OSError("timed out")):
            manager.start_runner()
            restarts, now = [], 1000.0
            while now < 1600:
                before = len(started)
                manager.probe(now=now)
                if len(started) > before:
                    restarts.append(now)
                now += dm.PROBE_INTERVAL
        gaps = [later - earlier for earlier, later in zip(restarts, restarts[1:])]
        self.assertGreaterEqual(len(restarts), 2, restarts)
        self.assertTrue(all(gap >= wda_watchdog.MIN_RESTART_GAP
                            for gap in gaps), f"restarts at {restarts}, gaps {gaps}")

    def test_quitting_while_the_probe_waits_starts_nothing_afterwards(self):
        from mobile_agent.runner_renewal import RunnerRenewal

        manager = self.manager()
        started = []
        self.fakes(manager, type("Service", (FakeService,), {"started": started}))
        closed = []

        def quit_during_the_probe(*_args, **_kwargs):
            manager.close()
            closed.append(time.monotonic())
            raise OSError("timed out")

        with patch.object(manager, "wda_ready", return_value=True), \
                patch.object(manager, "wda_alive", return_value=True), \
                patch.object(manager, "_wda_get", side_effect=quit_during_the_probe), \
                patch.object(dm, "WATCH_INTERVAL", .01):
            manager.start_runner()
            manager.renewal = RunnerRenewal(manager)
            manager.renewal.restart_pending = True
            manager.health.update(since=-1e9)
            manager.watch(manager.watching)
        self.assertEqual(len(closed), 1)
        after = [name for name, at in started if at >= closed[0]]
        self.assertEqual((after, manager.runner is not None and manager.runner.running), ([], False))
        with self.assertRaisesRegex(LookupError, "quitting"):
            manager.start_runner()


class DispatchRetentionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mobster-retention-")
        self.addCleanup(directory.cleanup)
        self.journal = Journal(Path(directory.name) / "runs.sqlite3")
        self.addCleanup(self.journal.close)
        self.runtime = FakeRuntime(self.journal)
        self.clock = {"now": datetime(2026, 9, 19, tzinfo=ZoneInfo("UTC")).timestamp()}
        self.workflows = Workflows(self.runtime, lambda: self.clock["now"])
        self.runtime.workflows = self.workflows
        self.saved = self.workflows.create({"name": "Check inbox", "appId": "settings", "goal": "Read iOS version",
                                            "cron": "*/30 * * * *", "timezone": "UTC", "enabled": True})
        self.now_ms = round(self.clock["now"] * 1000)

    def fill(self, claimed_at, outcome="skipped_offline", count=MAX_DISPATCHES):
        with self.journal.transaction() as connection:
            connection.executemany("INSERT INTO workflow_dispatches VALUES (?,?,?,?,?,NULL)",
                                   [(f"workflow:{self.saved['id']}:scheduled:{n}", self.saved["id"], n,
                                     claimed_at(n), outcome) for n in range(count)])

    def rows(self):
        return self.journal.connection.execute("SELECT count(*) FROM workflow_dispatches").fetchone()[0]

    def due(self):
        self.clock["now"] = self.workflows.get(self.saved["id"])["nextAt"] / 1000 + 1
        self.workflows.tick()

    def test_a_half_hourly_schedule_still_runs_after_208_days(self):
        self.fill(lambda n: self.now_ms - 209 * DAY_MS + n * 1_800_000)
        self.due()
        self.assertEqual(len(self.runtime.calls), 1)
        oldest = self.journal.connection.execute("SELECT min(claimed_at) FROM workflow_dispatches").fetchone()[0]
        self.assertGreaterEqual(oldest, self.workflows.now() - 7 * DAY_MS)
        self.assertLessEqual(self.rows(), 7 * 48 + 1)

    def test_a_full_table_of_recent_records_gives_up_its_oldest_first(self):
        self.fill(lambda n: self.now_ms - 6 * DAY_MS + n * 40_000)
        self.due()
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertEqual(self.rows(), MAX_DISPATCHES)
        oldest = self.journal.connection.execute("SELECT min(scheduled_at) FROM workflow_dispatches").fetchone()[0]
        self.assertEqual(oldest, 1)

    def test_the_last_day_is_never_forgotten(self):
        self.fill(lambda n: self.now_ms - 20 * DAY_MS, count=MAX_DISPATCHES - 60)
        with self.journal.transaction() as connection:
            connection.executemany("INSERT INTO workflow_dispatches VALUES (?,?,?,?,?,NULL)",
                                   [(f"recent:{n}", self.saved["id"], n, self.now_ms - n * 60_000, "completed")
                                    for n in range(60)])
        self.due()
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.workflows.get(self.saved["id"])["lastOutcome"], "skipped_daily_limit")
        self.assertEqual(self.rows(), 61)

    def test_a_day_of_skipped_records_never_stops_scheduling(self):
        self.fill(lambda n: self.now_ms - 20 * 3_600_000 + n * 7_000, outcome="skipped_daily_limit")
        self.due()
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertIsNone(self.workflows.scheduler_error)
        self.assertEqual(self.rows(), MAX_DISPATCHES)
        oldest = self.journal.connection.execute("SELECT min(scheduled_at) FROM workflow_dispatches").fetchone()[0]
        self.assertEqual(oldest, 1)

    def test_skipped_records_go_before_the_last_days_attempts(self):
        self.fill(lambda n: self.now_ms - 20 * 3_600_000 + n * 7_000, count=MAX_DISPATCHES - 60)
        with self.journal.transaction() as connection:
            connection.executemany("INSERT INTO workflow_dispatches VALUES (?,?,?,?,?,NULL)",
                                   [(f"recent:{n}", self.saved["id"], n, self.now_ms - 23 * 3_600_000 + n * 60_000,
                                     "completed") for n in range(60)])
        self.due()
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.workflows.get(self.saved["id"])["lastOutcome"], "skipped_daily_limit")
        completed = self.journal.connection.execute(
            "SELECT count(*) FROM workflow_dispatches WHERE outcome='completed'").fetchone()[0]
        self.assertEqual((completed, self.rows()), (60, MAX_DISPATCHES))

    def test_a_recovery_failure_stops_scheduling_not_mobster(self):
        scheduler = WorkflowScheduler(self.workflows)
        with patch.object(self.workflows, "recover", side_effect=JournalError("disk full")):
            scheduler.start()
        self.assertIsNone(scheduler.thread)
        self.assertIn("Scheduled tasks stopped", self.workflows.scheduler_error)
        self.assertTrue(scheduler.close())
        self.assertEqual(self.runtime.calls, [])


class FallBackNightTests(unittest.TestCase):
    def dispatched(self, expression):
        directory = tempfile.TemporaryDirectory(prefix="mobster-dst-")
        self.addCleanup(directory.cleanup)
        journal = Journal(Path(directory.name) / "runs.sqlite3")
        self.addCleanup(journal.close)
        runtime = FakeRuntime(journal)
        zone = ZoneInfo("America/New_York")
        clock = {"now": datetime(2026, 10, 31, 12, tzinfo=zone).timestamp()}
        workflows = Workflows(runtime, lambda: clock["now"])
        runtime.workflows = workflows
        saved = workflows.create({"name": "Daily note", "appId": "settings", "goal": "Read iOS version",
                                  "cron": expression, "timezone": "America/New_York", "enabled": True})
        end = datetime(2026, 11, 2, 12, tzinfo=zone).timestamp()
        while (next_at := workflows.get(saved["id"])["nextAt"] / 1000) <= end:
            clock["now"] = next_at + 1
            workflows.tick()
            for run in runtime.runs.values():
                run.status, run.finished_at = "completed", clock["now"]
        return [datetime.fromtimestamp(int(call[3].rsplit(":", 1)[1]) / 1000, zone).isoformat()
                for call in runtime.calls]

    def test_a_daily_schedule_runs_once_on_the_night_clocks_go_back(self):
        self.assertEqual(self.dispatched("30 1 * * *"), ["2026-11-01T01:30:00-04:00", "2026-11-02T01:30:00-05:00"])

    def test_an_hourly_schedule_runs_in_both_one_oclock_hours(self):
        night = [moment for moment in self.dispatched("30 * * * *") if moment.startswith("2026-11-01T0")]
        self.assertEqual(night[:4], ["2026-11-01T00:30:00-04:00", "2026-11-01T01:30:00-04:00",
                                     "2026-11-01T01:30:00-05:00", "2026-11-01T02:30:00-05:00"])


class ServeWithFullHistoryTests(unittest.TestCase):
    def test_mobster_starts_with_a_full_dispatch_table(self):
        root = Path(tempfile.mkdtemp(prefix="mobster-serve-"))
        self.addCleanup(shutil.rmtree, root, True)
        config = SimpleNamespace(port=0, state_db=str(root / "state" / "mobster.sqlite3"), wda_url=None, session=None,
                                 enable_live=False, env_file=None, data_dir=str(root), manage_device=False, socket=None,
                                 exit_with_parent=False, keep_runner=False, spend_cap_usd=None)
        journal = Journal(config.state_db)
        workflows = Workflows(SimpleNamespace(journal=journal, lock=threading.RLock(), runs={}, apps=lambda: []))
        saved = workflows.create({"name": "Hourly check", "appId": "settings", "goal": "Read iOS version",
                                  "cron": "0 * * * *", "timezone": "UTC", "enabled": True})
        slept_through = 10_001 * 3_600_000
        with journal.transaction() as connection:
            connection.executemany("INSERT INTO workflow_dispatches VALUES (?,?,?,?,?,NULL)",
                                   [(f"workflow:{saved['id']}:scheduled:{n * 3_600_000}", saved["id"], n * 3_600_000,
                                     n * 3_600_000, "completed") for n in range(1, MAX_DISPATCHES + 1)])
            data = json.loads(connection.execute("SELECT data FROM workflows").fetchone()[0])
            data["nextAt"] = slept_through
            connection.execute("UPDATE workflows SET data=?", (json.dumps(data),))
        journal.close()
        served = []

        class FakeServer:
            def __init__(self, address, handler):
                pass

            def serve_forever(self):
                served.append(True)
                raise KeyboardInterrupt

            def server_close(self):
                pass

        with patch.dict(os.environ, {"MOBSTER_API_TOKEN": "t" * 40}), patch.object(serve_module, "BoundedServer",
                                                                                   FakeServer), \
                patch("builtins.print"):
            serve_module.serve(config)
        self.assertEqual(served, [True])
        journal = Journal(config.state_db)
        self.addCleanup(journal.close)
        missed = journal.connection.execute("SELECT outcome FROM workflow_dispatches WHERE scheduled_at=?",
                                            (slept_through,)).fetchone()
        self.assertEqual(missed, ("skipped_missed",))


class RequestRetentionTests(Base):
    def keys(self, runtime, count):
        with runtime.journal.transaction() as connection:
            connection.executemany("INSERT INTO requests VALUES (?,?,?)",
                                   [(f"key-{n}", f"{n:012x}", "x") for n in range(count)])

    def test_the_ten_thousand_and_first_task_still_starts(self):
        os.environ["OPENAI_API_KEY"] = KEY
        runtime = self.runtime()
        self.keys(runtime, REQUEST_LIMIT)
        status, body, _ = self.request(runtime, "POST", "/api/runs", {"goal": "Open Settings"},
                                       extra_headers={"Idempotency-Key": "a-new-task"})
        self.assertEqual(status, 201, body)
        self.assertIsNone(runtime.journal.lookup("key-0"))
        self.assertIsNotNone(runtime.journal.lookup("key-1"))
        count = runtime.journal.connection.execute("SELECT count(*) FROM requests").fetchone()[0]
        self.assertEqual(count, REQUEST_LIMIT)

    def test_a_key_whose_task_is_still_in_history_is_never_forgotten(self):
        os.environ["OPENAI_API_KEY"] = KEY
        runtime = self.runtime()
        status, first, _ = self.request(runtime, "POST", "/api/runs", {"goal": "Open Settings"},
                                         extra_headers={"Idempotency-Key": "first"})
        self.assertEqual(status, 201)
        self.finish(runtime, runtime.runs[first["run"]["id"]])
        self.keys(runtime, REQUEST_LIMIT - 1)
        status, _, _ = self.request(runtime, "POST", "/api/runs", {"goal": "Open About"},
                                    extra_headers={"Idempotency-Key": "second"})
        self.assertEqual(status, 201)
        status, replay, _ = self.request(runtime, "POST", "/api/runs", {"goal": "Open Settings"},
                                         extra_headers={"Idempotency-Key": "first"})
        self.assertEqual((status, replay["run"]["id"], replay["replayed"]), (200, first["run"]["id"], True))
        self.assertIsNone(runtime.journal.lookup("key-0"))


REAL_WORK = Runtime.work


class Keyed(Base):
    def setUp(self):
        super().setUp()
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"


class StreamRateTests(Keyed):
    def test_an_idle_phone_streams_at_the_idle_rate_and_a_task_or_a_gesture_brings_every_frame(self):
        runtime = self.runtime()
        self.assertEqual(runtime.stream_fps(), IDLE_STREAM_FPS)
        run = runtime.create("settings", "Open About", "live")
        self.assertIsNone(runtime.stream_fps(), "a task runs on it: every frame")
        self.finish(runtime, run)
        self.assertEqual(runtime.stream_fps(), IDLE_STREAM_FPS)
        runtime.control = Mock()
        runtime.control.perform.return_value = {"operation": "TAP"}
        runtime.control_device({"action": "tap", "x": 0.5, "y": 0.5})
        self.assertIsNone(runtime.stream_fps(), "the person is driving it from the live view")
        runtime.controlled_at[None] = time.monotonic() - CONTROL_STREAM_SECONDS - 1
        self.assertEqual(runtime.stream_fps(), IDLE_STREAM_FPS)

    def test_a_task_on_another_phone_leaves_this_one_idle(self):
        runtime = self.runtime()
        other = SimpleNamespace(primary=False, key="usb:00008130-OTHER")
        runtime.active_runs["r1"] = other.key
        self.assertIsNone(runtime.stream_fps(other))
        self.assertEqual(runtime.stream_fps(), IDLE_STREAM_FPS)
        runtime.active_runs.clear()


class StreamEndpointTests(unittest.TestCase):
    def serve(self, rate):
        class RecordedVideo(WdaVideo):
            def __init__(self):
                super().__init__("http://127.0.0.1:9", mjpeg_url=None)
                self.rates = []

            def _start_worker(self):
                pass

            def paced_frames(self, viewer, rate):
                for sequence in range(1, 5):
                    self.rates.append(rate())
                    yield sequence, "image/jpeg", JPEG, 0, 0, "fixture"

        video = self.video = RecordedVideo()
        self.addCleanup(video.close)
        runtime = Mock()
        runtime.config = SimpleNamespace(port=8765)
        runtime.video = video
        runtime.stream_fps = Mock(side_effect=lambda phone=None: rate)
        server = BoundedServer(("127.0.0.1", 0), make_handler(runtime))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def frames(self, port, path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers={"Host": "127.0.0.1:8765"})
        response = connection.getresponse()
        if response.status != 200:
            return response.status
        return sum(1 for _ in read_mjpeg_frames(response))

    def test_the_stream_follows_the_services_rate(self):
        for rate in (IDLE_STREAM_FPS, None):
            with self.subTest(rate=rate):
                self.assertEqual(self.frames(self.serve(rate), "/api/device/stream?framing=raw"), 4)
                self.assertEqual(self.video.rates, [rate] * 4)

    def test_a_viewer_may_name_its_own_rate_within_bounds(self):
        port = self.serve(None)
        self.assertEqual(self.frames(port, "/api/device/stream?framing=raw&fps=2"), 4)
        self.assertEqual(self.video.rates, [2] * 4)
        for bad in ("0", "61", "2.5", "fast"):
            with self.subTest(fps=bad):
                self.assertEqual(self.frames(port, f"/api/device/stream?fps={bad}"), 400)


class EtagTests(Keyed):
    def test_a_poll_that_changed_nothing_is_answered_304_without_a_body(self):
        runtime = self.runtime()
        status, data, headers = self.request(runtime, "GET", "/api/runs")
        self.assertEqual(status, 200)
        tag = headers["ETag"]
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, data, headers = self.request(runtime, "GET", "/api/runs", extra_headers={"If-None-Match": tag})
        self.assertEqual((status, data, headers["ETag"]), (304, b"", tag))
        self.assertNotIn("Content-Length", headers)
        runtime.create("settings", "Open About", "live")
        status, data, headers = self.request(runtime, "GET", "/api/runs", extra_headers={"If-None-Match": tag})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["runs"]), 1)
        self.assertNotEqual(headers["ETag"], tag)

    def test_only_successful_gets_carry_a_tag(self):
        runtime = self.runtime()
        status, _, headers = self.request(runtime, "GET", "/api/runs/0123456789ab")
        self.assertEqual(status, 404)
        self.assertNotIn("ETag", headers)
        status, _, headers = self.request(runtime, "POST", "/api/settings", {"askBeforeActing": True})
        self.assertEqual(status, 200)
        self.assertNotIn("ETag", headers)
        status, data, _ = self.request(runtime, "POST", "/api/settings", {"askBeforeActing": True},
                                       extra_headers={"If-None-Match": "*"})
        self.assertEqual((status, data["askBeforeActing"]), (200, True))

    def test_the_desktop_webview_may_send_and_read_the_tag(self):
        runtime = self.runtime()
        server = BoundedServer(("127.0.0.1", 0), make_handler(runtime))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        origin = sorted(DESKTOP_ORIGINS)[0]

        def send(method, headers):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            connection.request(method, "/api/runs", headers={"Host": "127.0.0.1:8765", "Origin": origin,
                                                             "Connection": "close", **headers})
            response = connection.getresponse()
            response.read()
            connection.close()
            return response

        preflight = send("OPTIONS", {"Access-Control-Request-Method": "GET",
                                     "Access-Control-Request-Headers": "authorization, if-none-match"})
        self.assertIn("If-None-Match", preflight.getheader("Access-Control-Allow-Headers"))
        answer = send("GET", {})
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.getheader("Access-Control-Expose-Headers"), "ETag")
        again = send("GET", {"If-None-Match": answer.getheader("ETag")})
        self.assertEqual(again.status, 304)
        self.assertEqual(again.getheader("Access-Control-Allow-Origin"), origin)


class ListRowTests(Keyed):
    def test_rows_leave_out_the_stage_timings_the_task_keeps(self):
        runtime = self.runtime()
        run = runtime.create("settings", "Open About", "live")
        run.finish({"event": "result", "status": "completed", "answer": "iOS 26.4",
                    "timing": {"observe": .4, "model": 1.2}, "proof": [{"quote": "26.4", "app": "Settings"}]})
        runtime.active_runs.clear()
        status, data, _ = self.request(runtime, "GET", "/api/runs")
        row = data["runs"][0]
        self.assertNotIn("timing", row["summary"])
        self.assertEqual((row["summary"]["answer"], row["summary"]["proof"][0]["quote"]), ("iOS 26.4", "26.4"))
        self.assertEqual((row["events"], row["eventCount"]), ([], len(run.events)))
        status, data, _ = self.request(runtime, "GET", f"/api/runs/{run.id}")
        self.assertEqual(data["run"]["summary"]["timing"], {"observe": .4, "model": 1.2})
        self.assertIn("timing", run.summary, "the run itself is untouched")


class BusTests(Keyed):
    def test_a_deleted_or_reviewed_task_is_published(self):
        runtime = self.runtime()
        runtime.bus = eventbus.EventBus()
        run = runtime.create("settings", "Open About", "live")
        self.finish(runtime, run)
        start = runtime.bus.position()
        runtime.review_run(run.id, True)
        runtime.delete_run(run.id)
        events = [(e["event"], e["runId"]) for e in runtime.bus.since({"runs"}, start)]
        self.assertEqual(events, [("run_reviewed", run.id), ("run_deleted", run.id)])


class ReleaseTests(Keyed):
    def test_a_finished_run_lets_go_of_its_driver_and_old_screenshots(self):
        runtime = self.runtime()
        runs = []
        for index in range(KEPT_PREVIEWS + 2):
            run = runtime.create("settings", f"Open About {index}", "live")
            driver = SimpleNamespace(screens=[b"x" * 1024])
            run.capture, run.guard = (lambda timeout=3, driver=driver: "data:image/png;base64,AAAA"), object()
            run.image = f"data:image/png;base64,{index}"
            run.stop.set()
            REAL_WORK(runtime, run)
            self.assertIsNotNone(run.finished_at)
            self.assertIsNone(run.capture)
            self.assertIsNone(run.guard)
            runs.append(run)
            time.sleep(.01)
        kept = [run for run in runs if run.image is not None]
        self.assertEqual(kept, runs[-KEPT_PREVIEWS:])
        status, data, _ = self.request(runtime, "GET", f"/api/runs/{runs[-1].id}/screenshot")
        self.assertEqual((status, data["image"]), (200, runs[-1].image))
        status, data, _ = self.request(runtime, "GET", f"/api/runs/{runs[0].id}/screenshot")
        self.assertEqual((status, data["image"]), (200, None))


if __name__ == "__main__":
    unittest.main()

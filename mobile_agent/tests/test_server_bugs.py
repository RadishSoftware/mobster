"""Server fixes from the bug sweep: saving a Home Screen task, a key that works again, Stop during the phone
check, the runner's watch loop and lock, a daily schedule on the night clocks go back, and replay records that
never fill up.
Offline: fake phone, fake OpenAI, temporary databases; no network, runner, simulator or model."""

from datetime import datetime
from email.message import Message
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
from zoneinfo import ZoneInfo

from mobile_agent import device_manager as dm
from mobile_agent import server
from mobile_agent.journal import Journal, JournalError, REQUEST_LIMIT
from mobile_agent.latency_trace import Trace
from mobile_agent.server import RunSetup
from mobile_agent.tests.test_engines import COMPOSE, Script, item
from mobile_agent.tests.test_server_engine import Base, SmartTaskTests, jpeg
from mobile_agent.tests.test_workflows import FakeRuntime
from mobile_agent.workflow_scheduler import WorkflowScheduler
from mobile_agent.workflows import DAY_MS, MAX_DISPATCHES, Workflows

KEY = "sk-test-000000000000"
PHONE = {"udid": "00008110-000000000000001E", "name": "iPhone", "trusted": True}


class OpenAIAnswer(io.BytesIO):
    """What urllib's opener returns for a 200: a readable context manager."""
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ServerCase(Base):
    def request(self, runtime, method, path, body=None, key=None):
        """Base.request, with an optional Idempotency-Key."""
        if key is None:
            return super().request(runtime, method, path, body)
        handler = object.__new__(server.make_handler(runtime))
        raw = json.dumps(body).encode()
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(raw))
        handler.headers["Idempotency-Key"] = key
        handler.command, handler.path = method, path
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.connection = Mock()
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        getattr(handler, f"do_{method}")()
        return statuses[-1], json.loads(handler.wfile.getvalue()), {}


class HomeScreenWorkflowTests(ServerCase):
    """Run view › Save as workflow sends the run's own appId: "any" for a Smart task on the Home Screen."""

    def test_a_smart_task_started_on_the_home_screen_saves_as_a_workflow(self):
        os.environ["OPENAI_API_KEY"] = KEY
        runtime = self.runtime()
        status, body, _ = self.request(runtime, "POST", "/api/runs", {"goal": "What iOS version is this iPhone on?"})
        run = body["run"]
        self.assertEqual((status, run["appId"], run["engine"]), (201, "any", "smart"))
        self.finish(runtime, runtime.runs[run["id"]])
        save = {"name": "iOS version", "appId": run["appId"], "goal": run["goal"], "outputSchema": None,
                "outputFormat": run["outputFormat"], "helperModel": run["helperModel"], "engine": run["engine"]}
        status, body, _ = self.request(runtime, "POST", "/api/workflows", save)
        self.assertEqual(status, 201, body)
        self.assertEqual((body["workflow"]["appId"], body["workflow"]["engine"]), (None, "smart"))  # as documented
        again, _ = runtime.workflows.run(body["workflow"]["id"], "saved-home-screen")
        self.assertEqual((again.app["id"], again.engine), ("any", "smart"))

    def test_only_smart_starts_on_the_home_screen(self):
        runtime = self.runtime()
        for engine in ("fast", None):
            status, body, _ = self.request(runtime, "POST", "/api/workflows",
                                           {"name": "Pizza", "appId": "any", "goal": "Find pizza", "engine": engine})
            self.assertEqual(status, 400, body)
        self.assertEqual(runtime.workflows.list(), [])


class KeyRecheckTests(ServerCase):
    """A task hit OpenAI 404 (the key's project could not use the model yet) and ModelReach remembered it for
    six hours. The user fixes the project, then tests or saves the key: Smart takes tasks again at once."""

    def refused(self):
        os.environ["OPENAI_API_KEY"] = KEY
        runtime = self.runtime(managed=True)
        runtime.model_reach.record(KEY, False, "Your OpenAI key can't use gpt-5.6-sol.")  # what run_smart records
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
            os.environ["OPENAI_API_KEY"] = other  # Settings saved another key while the test waited on OpenAI
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
        self.request(runtime, "POST", "/api/keys", {"jev": {"key": "jev-key-000000000000"}})  # another key
        self.assertEqual(runtime.status()["engines"]["smart"]["reason"], "Your OpenAI key can't use gpt-5.6-sol.")


class StopDuringSetupTests(ServerCase):
    """Stop (⌘.) pressed while Mobster checks the phone (the preflight: up to PREFLIGHT_TIMEOUT per call) or
    gets the Smart loop ready. Contract: Stop ends a task before its next action."""

    Phone = SmartTaskTests.Phone

    def setUp(self):
        super().setUp()
        session = patch("mobile_agent.server.resolve_wda_session", return_value="s")  # never a real runner
        session.start()
        self.addCleanup(session.stop)

    def work(self, runtime, run, phone, **stops):
        script = Script([[("DONE", None, None)]], [item()], answer="Done.")
        frames = iter([jpeg()] * 20)
        with patch("mobile_agent.server.build_target_driver", return_value=phone), \
                patch("mobile_agent.server.prepare_wda_phone", side_effect=stops.get("preflight")), \
                patch.object(runtime, "smart_apps", side_effect=stops.get("apps", lambda run: {})), \
                patch("mobile_agent.engines.build_client", return_value=script), \
                patch("mobile_agent.frontier.video_frame", side_effect=lambda *a, **k: next(frames)):
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
        run = runtime.create(None, "What iOS version is this iPhone on?", "live")  # the Home Screen

        def listing(_):
            runtime.stop_run(run.id)
            return {}

        calls = self.work(runtime, run, self.Phone([COMPOSE] * 4), apps=listing)
        self.assertEqual((run.status, calls), ("stopped", []))

    def test_the_shared_setup_opens_no_app_after_stop(self):
        """Fast opens its driver the same way (Runtime.open_driver)."""
        os.environ["TYPESAFE_API_KEY"] = "jev-key-000000000000"
        runtime = self.runtime()
        run = runtime.create("messages", "Text Sam I'm running late", "live", engine="fast")
        phone, phases = self.Phone([COMPOSE]), []
        with patch("mobile_agent.server.build_target_driver", return_value=phone), \
                patch("mobile_agent.server.prepare_wda_phone", side_effect=lambda driver, guard=None: runtime.stop_run(run.id)):
            driver = runtime.open_driver(run, Trace(), RunSetup(phase=phases.append, hold=lambda value: value))
        self.assertIsNone(driver)
        self.assertEqual((phone.calls, phases), ([], ["preflight"]))


class FakeService:
    """Supervised without a process; ``started`` records (name, when) for every start."""
    started = None

    def __init__(self, name, command, log_path, env=None):
        self.name, self.running = name, False

    def start(self):
        self.running = True
        self.started.append((self.name, time.monotonic()))

    def stop(self):
        self.running = False


class RunnerWatchTests(unittest.TestCase):
    """Runtime starts DeviceManager.watch(manager.watching) at launch and quitting calls manager.close().
    Starting the runner used to replace both ``watching`` and ``runner_lock``."""

    def manager(self, **settings):
        directory = tempfile.TemporaryDirectory(prefix="mobster-watch-")
        self.addCleanup(directory.cleanup)
        manager = dm.DeviceManager(directory.name, "http://127.0.0.1:9")
        manager.save_settings(team="ABCDE12345", udid=PHONE["udid"], built_for=PHONE["udid"], **settings)
        return manager

    def fakes(self, manager, service):
        """Everything start_runner touches, without xcodebuild, iproxy or a phone."""
        for target in (patch.object(dm, "Supervised", service), patch.object(dm, "relay_command", return_value=["iproxy"]),
                       patch.object(dm, "pin_loopback"), patch.object(dm, "tool", return_value="/usr/bin/true"),
                       patch.object(manager, "device", return_value=PHONE), patch.object(manager, "built", return_value=True),
                       patch.object(manager, "expired", return_value=False), patch.object(manager, "wda_alive", return_value=False),
                       patch.object(manager, "wda_ready", return_value=False), patch.object(manager, "xcode_env", return_value=None)):
            target.start()
            self.addCleanup(target.stop)

    def test_closing_the_manager_ends_its_watch_loop_after_the_runner_started(self):
        manager = self.manager(autostart=True)  # the runner was running when Mobster last quit
        started = []
        self.fakes(manager, type("Service", (FakeService,), {"started": started}))
        with patch.object(dm, "WATCH_INTERVAL", .02):
            watcher = threading.Thread(target=manager.watch, args=(manager.watching,), daemon=True)  # as Runtime does
            watcher.start()
            deadline = time.monotonic() + 2
            while not started and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(started, "autostart never started the runner")
            manager.close()  # quitting Mobster
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
            manager.watching.set()  # close() has begun
        waiting.join(2)
        self.assertEqual(started, [])

    def test_two_runner_starts_never_overlap(self):
        manager = self.manager()
        inside, release, created = threading.Event(), threading.Event(), []

        class SlowService(FakeService):
            started = []

            def __init__(self, name, command, log_path, env=None):
                super().__init__(name, command, log_path, env)
                created.append(name)

            def start(self):
                if self.name == "wda-runner" and not inside.is_set():
                    inside.set()
                    release.wait(2)  # the first start is still inside its critical section
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
        """probe() sets last_restart and then starts the runner; the start used to reset it, so a runner that
        stayed wedged restarted every 100 s (WEDGED_RESTART_SECONDS plus one probe) instead of every 180 s."""
        manager = self.manager()
        started = []
        self.fakes(manager, type("Service", (FakeService,), {"started": started}))
        with patch.object(manager, "wda_ready", return_value=True), \
                patch("urllib.request.urlopen", side_effect=OSError("timed out")):  # /wda/locked never answers
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
        self.assertTrue(all(gap >= dm.MIN_RESTART_GAP for gap in gaps), f"restarts at {restarts}, gaps {gaps}")

    def test_quitting_while_the_probe_waits_starts_nothing_afterwards(self):
        """close() while probe() waits on /wda/locked, with a renewal restart pending: the same watch iteration
        used to relaunch xcodebuild and iproxy through the renewal's restart."""
        from mobile_agent.runner_renewal import RunnerRenewal

        manager = self.manager()
        started = []
        self.fakes(manager, type("Service", (FakeService,), {"started": started}))
        closed = []

        def quit_during_the_probe(*_args, **_kwargs):
            manager.close()  # quitting Mobster while the probe waits on WDA
            closed.append(time.monotonic())
            raise OSError("timed out")

        with patch.object(manager, "wda_ready", return_value=True), patch.object(manager, "wda_alive", return_value=True), \
                patch("urllib.request.urlopen", side_effect=quit_during_the_probe), patch.object(dm, "WATCH_INTERVAL", .01):
            manager.start_runner()
            manager.renewal = RunnerRenewal(manager)
            manager.renewal.restart_pending = True  # a renewal build finished during a task: restart between tasks
            manager.health.update(since=-1e9)  # wedged long enough that the probe itself would restart it too
            manager.watch(manager.watching)  # one iteration: autostart, probe (close() lands here), renewal.tick
        self.assertEqual(len(closed), 1)
        after = [name for name, at in started if at >= closed[0]]
        self.assertEqual((after, manager.runner is not None and manager.runner.running), ([], False))
        with self.assertRaisesRegex(LookupError, "quitting"):
            manager.start_runner()  # Setup › Start after close() answers 409 rather than launching anything


class DispatchRetentionTests(unittest.TestCase):
    """Every occurrence leaves a dispatch record, skipped ones included. 48 a day for "*/30 * * * *" used to
    reach MAX_DISPATCHES in 208 days, and then every tick (and every launch) failed."""

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
        self.fill(lambda n: self.now_ms - 209 * DAY_MS + n * 1_800_000)  # every half hour since February
        self.due()
        self.assertEqual(len(self.runtime.calls), 1)
        oldest = self.journal.connection.execute("SELECT min(claimed_at) FROM workflow_dispatches").fetchone()[0]
        self.assertGreaterEqual(oldest, self.workflows.now() - 7 * DAY_MS)  # the last week stays
        self.assertLessEqual(self.rows(), 7 * 48 + 1)

    def test_a_full_table_of_recent_records_gives_up_its_oldest_first(self):
        # 10,000 records from the last six days (a busy week): none is past the retention window.
        self.fill(lambda n: self.now_ms - 6 * DAY_MS + n * 40_000)
        self.due()
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertEqual(self.rows(), MAX_DISPATCHES)
        oldest = self.journal.connection.execute("SELECT min(scheduled_at) FROM workflow_dispatches").fetchone()[0]
        self.assertEqual(oldest, 1)

    def test_the_last_day_is_never_forgotten(self):
        # The daily limit reads the last 24 hours: 60 attempts there must still stop the 61st.
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
        # 200 workflows skipping up to 60 times a day each write 10,000 records within the day. The daily limit
        # and interval never count a skipped one, and its slot never comes round again, so the oldest go.
        self.fill(lambda n: self.now_ms - 20 * 3_600_000 + n * 7_000, outcome="skipped_daily_limit")
        self.due()
        self.assertEqual(len(self.runtime.calls), 1)
        self.assertIsNone(self.workflows.scheduler_error)
        self.assertEqual(self.rows(), MAX_DISPATCHES)
        oldest = self.journal.connection.execute("SELECT min(scheduled_at) FROM workflow_dispatches").fetchone()[0]
        self.assertEqual(oldest, 1)

    def test_skipped_records_go_before_the_last_days_attempts(self):
        # A full table from the last day: the 60 attempts the daily limit reads stay, a skipped record goes.
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
    """SERVER-4: on 1 Nov 2026 New York's clocks go back at 2:00, so 1:30 happens twice (EDT, then EST)."""

    def dispatched(self, expression):
        """Local times of the scheduled runs dispatched from 31 Oct noon to 2 Nov noon, ticking at every nextAt."""
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
        return [datetime.fromtimestamp(int(call[3].rsplit(":", 1)[1]) / 1000, zone).isoformat() for call in runtime.calls]

    def test_a_daily_schedule_runs_once_on_the_night_clocks_go_back(self):
        self.assertEqual(self.dispatched("30 1 * * *"), ["2026-11-01T01:30:00-04:00", "2026-11-02T01:30:00-05:00"])

    def test_an_hourly_schedule_runs_in_both_one_oclock_hours(self):
        night = [moment for moment in self.dispatched("30 * * * *") if moment.startswith("2026-11-01T0")]
        self.assertEqual(night[:4], ["2026-11-01T00:30:00-04:00", "2026-11-01T01:30:00-04:00",
                                     "2026-11-01T01:30:00-05:00", "2026-11-01T02:30:00-05:00"])


class ServeWithFullHistoryTests(unittest.TestCase):
    def test_mobster_starts_with_a_full_dispatch_table(self):
        """serve() runs WorkflowScheduler.start() -> Workflows.recover(), which records the slot the Mac slept
        through. At MAX_DISPATCHES that used to raise, and serve() closed the runtime and exited."""
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
                raise KeyboardInterrupt  # quit at once

            def server_close(self):
                pass

        with patch.dict(os.environ, {"MOBSTER_API_TOKEN": "t" * 40}), patch.object(server, "BoundedServer", FakeServer), \
                patch("builtins.print"):
            server.serve(config)
        self.assertEqual(served, [True])
        journal = Journal(config.state_db)
        self.addCleanup(journal.close)
        missed = journal.connection.execute("SELECT outcome FROM workflow_dispatches WHERE scheduled_at=?",
                                            (slept_through,)).fetchone()
        self.assertEqual(missed, ("skipped_missed",))


class RequestRetentionTests(ServerCase):
    """Every dashboard task carries an Idempotency-Key, and every key used to be kept forever: after 10,000
    tasks each new one was refused with 503."""

    def keys(self, runtime, count):
        with runtime.journal.transaction() as connection:
            connection.executemany("INSERT INTO requests VALUES (?,?,?)",
                                   [(f"key-{n}", f"{n:012x}", "x") for n in range(count)])

    def test_the_ten_thousand_and_first_task_still_starts(self):
        os.environ["OPENAI_API_KEY"] = KEY
        runtime = self.runtime()
        self.keys(runtime, REQUEST_LIMIT)
        status, body, _ = self.request(runtime, "POST", "/api/runs", {"goal": "Open Settings"}, key="a-new-task")
        self.assertEqual(status, 201, body)
        self.assertIsNone(runtime.journal.lookup("key-0"))  # the oldest, long gone from history
        self.assertIsNotNone(runtime.journal.lookup("key-1"))
        count = runtime.journal.connection.execute("SELECT count(*) FROM requests").fetchone()[0]
        self.assertEqual(count, REQUEST_LIMIT)

    def test_a_key_whose_task_is_still_in_history_is_never_forgotten(self):
        os.environ["OPENAI_API_KEY"] = KEY
        runtime = self.runtime()
        status, first, _ = self.request(runtime, "POST", "/api/runs", {"goal": "Open Settings"}, key="first")
        self.assertEqual(status, 201)
        self.finish(runtime, runtime.runs[first["run"]["id"]])
        self.keys(runtime, REQUEST_LIMIT - 1)  # "first" is now the oldest key
        status, _, _ = self.request(runtime, "POST", "/api/runs", {"goal": "Open About"}, key="second")
        self.assertEqual(status, 201)
        status, replay, _ = self.request(runtime, "POST", "/api/runs", {"goal": "Open Settings"}, key="first")
        self.assertEqual((status, replay["run"]["id"], replay["replayed"]), (200, first["run"]["id"], True))
        self.assertIsNone(runtime.journal.lookup("key-0"))


if __name__ == "__main__":
    unittest.main()

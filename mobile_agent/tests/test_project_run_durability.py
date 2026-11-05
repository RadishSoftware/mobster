"""A project check request survives duplicate submissions and restart without running twice."""

import os
import threading
import time
from types import SimpleNamespace
from unittest import mock

from mobile_agent.api_errors import APIError
from mobile_agent.api_routes import Request
from mobile_agent.journal import JournalError
from mobile_agent.projects import routes
from mobile_agent.projects import service as project_service
from mobile_agent.projects import store as store_module
from mobile_agent.projects.runs import CheckRun
from mobile_agent.projects.service import ProjectService
from mobile_agent.tests.test_project_checks import Fakes
from mobile_agent.tests.test_projects import Base
from mobile_agent.tests.test_server_engine import Base as EngineBase
from mobile_agent.tests.timing import bound


class DurableProjectRunTests(Base):
    def setUp(self):
        super().setUp()
        self.fakes = Fakes()
        self.service.close()
        self.service = ProjectService(self.runtime, home=self.home, data_dir=self.data,
                                      last_runs=lambda: self.last_runs,
                                      run_parts={"build": self.fakes.build, "execute": self.fakes.execute})
        self.addCleanup(self.service.close)
        self.path = self.app_folder(mobster=False)
        self.project, _ = self.service.add({"path": str(self.path)})
        self.service.setup(self.project["id"], {})

    def finished(self, run_id):
        deadline = time.monotonic() + bound(10)
        while time.monotonic() < deadline:
            status = self.service.run_status(run_id)
            if status["state"] not in ("building", "running"):
                return status
            time.sleep(.01)
        self.fail("the check run did not finish")

    def test_claim_precedes_worker_and_mode_bound_replay(self):
        self.fakes.hold = threading.Event()
        request = {"requestId": "project-record-1", "mode": "record"}
        first = self.service.run_checks(self.project["id"], request)
        self.assertEqual(first["mode"], "record")
        self.assertEqual(self.service.run_checks(self.project["id"], request)["id"], first["id"])
        self.assertEqual(self.service.requested_run(self.project["id"], request["requestId"])["id"], first["id"])
        with self.assertRaises(APIError) as conflict:
            self.service.run_checks(self.project["id"], {"requestId": request["requestId"], "mode": "strict"})
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        self.fakes.hold.set()
        self.assertEqual(self.finished(first["id"])["state"], "done")
        self.assertEqual(self.fakes.builds[0]["options"].replay_mode, "fresh")
        self.assertEqual(len(self.fakes.suites), 1)
        self.assertEqual(self.service.run_checks(self.project["id"], request)["id"], first["id"])
        self.assertEqual(len(self.fakes.builds), 1)

    def test_strict_mode_uses_replay_without_healing_or_retry(self):
        started = self.service.run_checks(self.project["id"], {"requestId": "strict-1", "mode": "strict"})
        self.assertEqual(self.finished(started["id"])["state"], "done")
        options = self.fakes.builds[0]["options"]
        self.assertEqual((options.replay_mode, options.effective_retries, options.repeat), ("strict", 0, 1))

    def test_explicit_auto_intent_replays_and_restart_preserves_unknown_outcome(self):
        self.fakes.hold = threading.Event()
        request = {"requestId": "default-run-1", "mode": "auto"}
        first = self.service.run_checks(self.project["id"], request)
        self.assertEqual(first["mode"], "auto")
        self.assertEqual(self.service.run_checks(self.project["id"], request)["id"], first["id"])
        with self.assertRaises(APIError) as conflict:
            self.service.run_checks(self.project["id"], {"requestId": request["requestId"], "mode": "record"})
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        self.fakes.hold.set()
        self.assertEqual(self.finished(first["id"])["state"], "done")
        self.assertEqual(self.service.run_checks(self.project["id"], request)["id"], first["id"])
        self.assertEqual((len(self.fakes.builds), len(self.fakes.suites)), (1, 1))

        admitted = CheckRun("123456789abc", self.project["id"], self.path,
                            mode="auto", request_id="default-unknown")
        self.service.store.claim_run(admitted.public())
        self.service.start()  # Reconcile an admitted request whose action outcome cannot be known after restart.
        found = self.service.requested_run(self.project["id"], "default-unknown")
        self.assertEqual((found["state"], found["exitCode"], found["code"]),
                         ("interrupted", 3, "interrupted"))
        self.assertEqual(self.service.run_checks(self.project["id"],
                                                 {"requestId": "default-unknown", "mode": "auto"}), found)
        self.assertEqual((len(self.fakes.builds), len(self.fakes.suites)), (1, 1))

    def test_restart_interrupts_an_admitted_run_and_keeps_the_request_tombstone(self):
        run = CheckRun("123456789abc", self.project["id"], self.path,
                       mode="strict", request_id="unknown-outcome")
        self.service.store.claim_run(run.public())
        self.service.start()  # Same durable startup reconciliation used after a process restart.
        found = self.service.run_status(run.id)
        self.assertEqual((found["state"], found["exitCode"], found["code"]),
                         ("interrupted", 3, "interrupted"))
        self.assertEqual(self.service.run_checks(self.project["id"],
                                                 {"requestId": "unknown-outcome", "mode": "strict"}), found)
        self.assertEqual(self.fakes.builds, [])
        self.assertEqual(self.fakes.suites, [])

    def test_a_suite_that_ended_on_an_unknown_outcome_stays_unknown_after_a_restart_and_never_runs_again(self):
        def unknown(planned, *, progress, stop, suite_id, on_suite, cwd):
            self.fakes.suites.append({"planned": planned, "suite": suite_id, "cwd": cwd})
            return {"suite": suite_id, "summary": {"total": 1, "unknown": 1}, "exitCode": 3,
                    "stopReason": "unknown_outcome"}, {}

        parts = {"build": self.fakes.build, "execute": unknown}
        self.service._run_parts.update(parts)
        request = {"requestId": "tap-unknown", "mode": "strict"}
        ended = self.finished(self.service.run_checks(self.project["id"], request)["id"])
        self.assertEqual((ended["state"], ended["code"], ended["exitCode"]), ("failed", "unknown_outcome", 3))
        self.service.close()
        restarted = ProjectService(self.runtime, home=self.home, data_dir=self.data,
                                   last_runs=lambda: self.last_runs, run_parts=parts)
        self.addCleanup(restarted.close)
        restarted.start()   # the startup reconciliation: a settled outcome is left as it was, not interrupted
        found = restarted.requested_run(self.project["id"], "tap-unknown")
        self.assertEqual((found["state"], found["code"], found["exitCode"]), ("failed", "unknown_outcome", 3))
        self.assertEqual(restarted.run_checks(self.project["id"], request), found)
        with self.assertRaises(APIError) as conflict:
            restarted.run_checks(self.project["id"], {"requestId": "tap-unknown", "mode": "auto"})
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        self.assertEqual((len(self.fakes.builds), len(self.fakes.suites)), (1, 1))

    def test_a_late_running_snapshot_never_reopens_a_finished_row(self):
        store = self.service.store
        run = CheckRun("123456789abc", self.project["id"], self.path, mode="auto", request_id="late-write")
        store.claim_run(run.public())
        running = {**run.public(), "state": "running", "line": "Running 1 check"}
        store.save_run(running)                          # an unfinished row takes each update
        self.assertEqual(store.check_run(run.id)["line"], "Running 1 check")
        store.save_run({**running, "state": "done", "exitCode": 0})
        store.save_run(running)                          # a snapshot taken before the run ended
        self.assertEqual(store.check_run(run.id)["state"], "done")
        self.service._runs = {run.id: SimpleNamespace(going=True, stop=lambda: None, public=lambda: running)}
        self.assertTrue(self.service.begin_close())      # close's own snapshot of a run that just ended
        self.assertEqual(store.check_run(run.id)["state"], "done")
        self.service._runs = {}
        owner = "ci:12345678-1234-4234-8234-123456789abc"
        suite = {**CheckRun("cba987654321", self.project["id"], self.path, mode="strict",
                            request_id="ci-late").public(), "state": "running"}
        store.claim_run(suite, owner=owner, grant_id="grant", revision=1)
        store.save_run({**suite, "state": "done", "exitCode": 0})   # only complete_ci ends a CI row
        self.assertEqual(store.ci_run(suite["id"], owner=owner)["run"]["state"], "running")
        store.complete_ci({**suite, "state": "done", "exitCode": 0}, {"json": b"{}", "junit": b"<x/>"})
        store.complete_ci(suite, None, "export_failed")  # a second, stale ending
        row = store.ci_run(suite["id"], owner=owner)
        self.assertEqual((row["run"]["state"], row["available"]), ("done", True))

    def test_a_row_that_cant_be_read_ends_interrupted_and_the_service_keeps_serving(self):
        owner = "ci:12345678-1234-4234-8234-123456789abc"
        with self.journal.transaction() as connection:
            connection.execute("INSERT INTO project_check_runs(id, project_id, owner, grant_id, request_id, mode, "
                               "revision, state, data) VALUES ('0123456789ab', ?, ?, 'grant', 'damaged', 'strict', 1, "
                               "'running', 'not json')", (self.project["id"], owner))
        self.service.start()
        row = self.service.store.ci_run("0123456789ab", owner=owner)
        self.assertEqual((row["run"]["state"], row["run"]["code"], row["run"]["requestId"]),
                         ("interrupted", "interrupted", "damaged"))
        self.assertEqual(self.service._ci_public(row)["status"], "interrupted")
        started = self.service.run_checks(self.project["id"], {"requestId": "after-restart", "mode": "auto"})
        self.assertEqual(self.finished(started["id"])["state"], "done")

    def test_a_failed_restart_recovery_admits_no_new_run_or_approval(self):
        with mock.patch.object(self.service.store, "interrupt_unfinished", side_effect=JournalError("disk")), \
                self.assertRaises(JournalError):
            self.service.start()
        approval = {"checks": [".mobster/checks/app-opens.yaml"], "appPath": ".mobster/build/Dailies.app",
                    "simulatorId": "SIM-UDID", "reviewed": True}
        for name, call in (("run", lambda: self.service.run_checks(self.project["id"],
                                                                     {"requestId": "after-restart", "mode": "auto"})),
                           ("approve", lambda: self.service.approve_test_revision(self.project["id"], approval)),
                           ("ci", lambda: self.service.ci_start(self.project["id"], "after-restart",
                                                                owner="ci:12345678-1234-4234-8234-123456789abc",
                                                                grant_id="grant", revision=1,
                                                                authorize=lambda *_args: None))):
            with self.subTest(name), self.assertRaises(APIError) as refused:
                call()
            self.assertEqual((refused.exception.status, refused.exception.code), (503, "unavailable"))
        self.assertEqual((self.fakes.builds, self.fakes.suites), ([], []))

    def test_late_stop_is_rejected_by_the_suite_seal_without_setting_shared_event(self):
        run = CheckRun("123456789abc", self.project["id"], self.path)
        run.state = "running"
        run._suite = SimpleNamespace(cancel=lambda: False)
        self.assertFalse(run.stop())
        self.assertFalse(run._stop.is_set())

    def test_a_terminal_journal_failure_cannot_look_like_a_completed_green_run(self):
        def unavailable(_run):
            raise OSError("disk")

        run = CheckRun("123456789abc", self.project["id"], self.path, on_change=unavailable)
        run.state, run.exit_code = "done", 0
        run._tell(force=True)
        self.assertEqual((run.public()["state"], run.public()["exitCode"], run.public()["code"]),
                         ("interrupted", 3, "journal_unavailable"))

    def test_close_retries_stop_after_a_journal_failure_and_keeps_live_workers_owned(self):
        stopped = []
        one = SimpleNamespace(going=True, stop=lambda: stopped.append("one"), public=lambda: {"id": "one"})
        two = SimpleNamespace(going=True, stop=lambda: stopped.append("two"), public=lambda: {"id": "two"})
        self.service._runs = {"one": one, "two": two}
        with mock.patch.object(self.service.store, "save_run", side_effect=[OSError("disk"), None, None, None]):
            with self.assertLogs("mobster", level="ERROR"):
                self.assertFalse(self.service.begin_close())
            self.assertEqual(stopped, ["one", "two"])
            self.assertTrue(self.service.begin_close())
        self.assertEqual(stopped, ["one", "two", "one", "two"])
        self.service._runs.clear()
        self.service.close()

    def test_close_waits_for_the_owned_worker_and_repeated_close_converges(self):
        release = threading.Event()
        worker = threading.Thread(target=release.wait, daemon=True)
        worker.start()
        stopped = []
        run = SimpleNamespace(going=True, stop=lambda: stopped.append(True),
                              public=lambda: {"id": "held", "state": "running"},
                              join=worker.join, _thread=worker)
        self.service._runs = {"held": run}
        try:
            self.assertTrue(self.service.begin_close())
            self.assertFalse(self.service.join(.01))
            self.assertTrue(self.service.begin_close())
            self.assertEqual(stopped, [True, True])
        finally:
            release.set()
            worker.join(bound(5))
            self.service._runs.clear()
        self.assertTrue(self.service.join(.01))
        self.service.close()


class ReviewedProjectCheckTests(Base):
    def setUp(self):
        super().setUp()
        self.path = self.app_folder(mobster=False)
        self.project, _ = self.service.add({"path": str(self.path)})
        self.service.setup(self.project["id"], {})

    def authored(self):
        return {"name": "Shows home", "app": {"bundle": "com.example.firefox-ios"},
                "steps": ["Open the home screen"], "expect": [{"text": "Welcome"}], "reviewed": True}

    def test_reviewed_author_save_is_exact_retry_and_does_not_run(self):
        first = self.service.author_check(self.project["id"], self.authored())
        self.assertFalse(first["verified"])
        self.assertFalse(first["replayed"])
        saved = self.path / first["check"]["path"]
        self.assertTrue(saved.is_file())
        self.assertIn("reset: none", saved.read_text())
        self.assertEqual(self.service.author_check(self.project["id"], self.authored())["check"], first["check"])
        self.assertTrue(self.service.author_check(self.project["id"], self.authored())["replayed"])
        self.assertIn(first["check"]["path"], self.service.get(self.project["id"])["checkFiles"])

    def test_unreviewed_missing_expect_wrong_bundle_and_unsafe_existing_file_refuse(self):
        for changed in ({**self.authored(), "reviewed": False},
                        {**self.authored(), "expect": []},
                        {**self.authored(), "app": {"bundle": "com.example.other"}}):
            with self.assertRaises(ValueError):
                self.service.author_check(self.project["id"], changed)
        self.assertEqual(list((self.path / ".mobster" / "checks").glob("authored-*")), [])
        first = self.service.author_check(self.project["id"], self.authored())
        saved = self.path / first["check"]["path"]
        saved.unlink()
        elsewhere = self.path / "outside.yaml"
        elsewhere.write_text("outside")
        os.link(elsewhere, saved)
        with self.assertRaises(APIError) as unsafe:
            self.service.author_check(self.project["id"], self.authored())
        self.assertEqual(unsafe.exception.code, "check_changed")
        self.assertEqual(elsewhere.read_text(), "outside")

    def test_finished_task_save_needs_reviewed_yaml_and_original_app(self):
        run_id = "abcdef123456"
        self.runtime.lock = threading.RLock()
        self.runtime.runs = {run_id: SimpleNamespace(
            app={"id": "com.example.firefox-ios", "bundleId": "com.example.firefox-ios", "name": "Dailies"},
            public=lambda: {"id": run_id, "finishedAt": 1000, "status": "completed", "goal": "Open Dailies",
                            "appId": "com.example.firefox-ios", "events": []})}
        yaml = "version: 1\napp: {bundle: com.example.firefox-ios}\nexpect: [{text: Welcome}]\n"
        body = {"runId": run_id, "yaml": yaml, "reviewed": True}
        first = self.service.save_finished_check(self.project["id"], body)
        self.assertFalse(first["replayed"])
        self.assertTrue(self.service.save_finished_check(self.project["id"], body)["replayed"])
        with self.assertRaises(ValueError):
            self.service.save_finished_check(self.project["id"], {**body, "yaml": yaml + "app: {}\n"})
        with self.assertRaises(ValueError):
            self.service.save_finished_check(self.project["id"],
                                             {**body, "yaml": yaml.replace("com.example.firefox-ios",
                                                                           "com.example.other")})

    def test_draft_route_requires_a_real_app_session(self):
        project_service._services[id(self.runtime)] = self.service
        self.addCleanup(project_service._services.pop, id(self.runtime), None)
        request = Request(runtime=self.runtime, method="POST", path="", params={"id": self.project["id"]},
                          query={}, headers={}, body=self.authored(), content_type="application/json",
                          origin="app", app_session=False)
        with self.assertRaises(APIError) as refused:
            routes.author_check(request)
        self.assertEqual(refused.exception.code, "app_only")
        request.app_session = True
        self.assertEqual(routes.author_check(request).status, 201)
        self.assertEqual(routes.author_check(request).status, 200)


class RuntimeCloseTests(EngineBase):
    def test_runtime_keeps_its_journal_and_phones_until_accepted_project_work_ends(self):
        runtime = self.runtime()
        worker = threading.Event()
        projects = SimpleNamespace(begin_close=mock.Mock(return_value=True),
                                   join=mock.Mock(side_effect=lambda timeout: worker.is_set()))
        manager = mock.Mock(watching=threading.Event())
        with mock.patch.object(project_service, "service_for", return_value=projects), \
                mock.patch.object(runtime, "manager", manager):
            self.assertFalse(runtime.close(timeout=.01))       # a project check may still dispatch
            manager.close.assert_not_called()
            self.assertEqual(runtime.journal.connection.execute("SELECT 1").fetchone()[0], 1)
            worker.set()
            self.assertTrue(runtime.close(timeout=1))
        manager.close.assert_called_once()
        self.assertEqual(projects.begin_close.call_count, 2)
        self.runtimes.remove(runtime)


class PruneTests(Base):
    """A project keeps its newest runs; an older finished one leaves its request key, which still refuses a retry."""

    def setUp(self):
        super().setUp()
        self.now = 1_800_000_000.0
        self.fakes = Fakes()
        self.service.close()
        self.service = ProjectService(self.runtime, home=self.home, data_dir=self.data, clock=lambda: self.now,
                                      last_runs=lambda: self.last_runs,
                                      run_parts={"build": self.fakes.build, "execute": self.fakes.execute})
        self.addCleanup(self.service.close)
        self.path = self.app_folder(mobster=False)
        self.project, _ = self.service.add({"path": str(self.path)})
        self.service.setup(self.project["id"], {})
        keep = mock.patch.object(store_module, "RUN_KEEP", 2)
        keep.start()
        self.addCleanup(keep.stop)

    def start(self, key, mode="auto"):
        self.now += 60
        run = self.service.run_checks(self.project["id"], {"requestId": key, "mode": mode})
        deadline = time.monotonic() + bound(10)
        while time.monotonic() < deadline and self.service.run_status(run["id"])["state"] in ("building", "running"):
            time.sleep(.01)
        return self.service.run_status(run["id"])

    def rows(self, table="project_check_runs", column="request_id"):
        return sorted(row[0] for row in self.journal.connection.execute(f"SELECT {column} FROM {table}"))

    def test_a_pruned_runs_key_still_refuses_until_ninety_days_after_it_started(self):
        for number in range(5):
            self.assertEqual(self.start(f"request-{number}")["state"], "done")   # past the newest two, still admitted
        self.assertEqual(self.rows(), ["request-3", "request-4"])
        replay = self.service.run_checks(self.project["id"], {"requestId": "request-0", "mode": "auto"})
        self.assertEqual((replay["requestId"], replay["state"], replay["pruned"]), ("request-0", "done", True))
        self.assertEqual(self.service.requested_run(self.project["id"], "request-0"), replay)
        with self.assertRaises(APIError) as conflict:
            self.service.run_checks(self.project["id"], {"requestId": "request-0", "mode": "strict"})
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        self.assertEqual(len(self.fakes.builds), 5)                            # no second run
        self.now += 90 * 86_400
        self.assertEqual(self.start("request-0")["state"], "done")             # 90 days on, the key is new
        self.assertEqual(len(self.fakes.builds), 6)
        self.assertNotIn("request-1", [row for row in self.rows("project_check_runs")])

    def test_a_pruned_ci_suites_key_replays_its_summary_with_no_results(self):
        store, owner = self.service.store, "ci:12345678-1234-4234-8234-123456789abc"
        for number in range(3):
            self.now += 60
            suite = {**CheckRun(f"{number:012x}", self.project["id"], self.path, mode="strict",
                                request_id=f"ci-{number}", clock=lambda: self.now).public(), "state": "running"}
            store.claim_run(suite, owner=owner, grant_id="grant", revision=1)
            store.complete_ci({**suite, "state": "done", "exitCode": 0}, {"json": b"{}", "junit": b"<x/>"})
        self.assertEqual(store.prune(self.project["id"], self.now), 1)
        row = store.ci_run(owner=owner, request_id="ci-0", now=self.now)
        self.assertEqual((row["revision"], row["grant_id"], row["available"], row["result_error"]),
                         (1, "grant", False, "expired"))
        self.assertEqual(self.service._ci_public(row)["status"], "completed")
        by_id = store.ci_run("000000000000", owner=owner, now=self.now)       # its row went; its id still answers
        self.assertEqual((by_id["run"]["id"], by_id["available"], by_id["result_error"]),
                         ("000000000000", False, "expired"))
        self.assertIsNone(store.ci_run("000000000000", owner="ci:00000000-0000-4000-8000-000000000000"))
        ask = {"owner": owner, "grant_id": "grant", "authorize": lambda *_args: None}
        self.assertEqual(self.service.ci_status("000000000000", **ask)["suite"]["status"], "completed")
        for call in (lambda: self.service.ci_results("000000000000", **ask),
                     lambda: self.service.ci_artifact("000000000000", "a" * 24, **ask)):
            with self.subTest(call), self.assertRaises(APIError) as gone:
                call()
            self.assertEqual((gone.exception.status, gone.exception.code), (410, "results_expired"))
        with self.assertRaises(APIError) as other:                            # another grant never sees it
            self.service.ci_status("000000000000", **{**ask, "grant_id": "grant-two"})
        self.assertEqual(other.exception.code, "suite_not_found")
        again = {**CheckRun("ffffffffffff", self.project["id"], self.path, mode="strict", request_id="ci-0",
                            clock=lambda: self.now).public(), "state": "running"}
        self.assertEqual(store.claim_run(again, owner=owner, grant_id="grant", revision=1)[2], False)

    def test_a_removed_local_runs_id_answers_its_summary(self):
        ids = [self.start(f"request-{number}")["id"] for number in range(3)]
        self.assertEqual(self.service.run_status(ids[0])["pruned"], True)
        self.assertEqual(self.service.run_status(ids[0])["state"], "done")
        with self.assertRaises(APIError) as unknown:
            self.service.run_status("ffffffffffff")
        self.assertEqual(unknown.exception.code, "not_found")

    def finished_runs(self, count, *, state="done", started=None):
        for number in range(count):
            clock = (lambda: started) if started is not None and number == 0 else (lambda: self.now)
            run = CheckRun(f"{number:012x}", self.project["id"], self.path, request_id=f"ended-{number}", clock=clock)
            self.service.store.claim_run(run.public())
            self.service.store.save_run({**run.public(), "state": state if number == 0 else "done", "exitCode": 0})

    def test_a_keys_ninety_days_count_from_when_its_run_started(self):
        started, store = self.now, self.service.store
        self.now += 89 * 86_400
        self.finished_runs(3, started=started)
        self.assertEqual(store.prune(self.project["id"], self.now), 1)          # removed 89 days after it started
        kept = lambda at: store.requested_run(self.project["id"], "ended-0", now=at)   # noqa: E731
        self.assertIsNotNone(kept(started + 90 * 86_400 - 1))
        self.assertIsNone(kept(started + 90 * 86_400 + 1))

    def test_an_interrupted_runs_key_never_expires(self):
        self.finished_runs(3, state="interrupted", started=self.now)
        self.service.store.prune(self.project["id"], self.now)
        later = self.now + 1_000 * 86_400
        self.assertEqual(self.service.store.requested_run(self.project["id"], "ended-0", now=later)[1]["state"],
                         "interrupted")

    def test_forgetting_a_project_removes_its_finished_runs_and_keeps_their_keys(self):
        self.finished_runs(2)
        going = CheckRun("aaaaaaaaaaaa", self.project["id"], self.path, request_id="going", clock=lambda: self.now)
        self.service.store.claim_run(going.public())
        self.service.forget(self.project["id"])
        self.assertEqual(self.rows(), ["going"])                                # an unfinished run stays
        self.assertEqual(self.service.store.requested_run(self.project["id"], "ended-1", now=self.now)[0], "auto")

    def test_admission_and_pruning_use_the_project_index(self):
        connection = self.journal.connection
        self.assertTrue({"project_check_runs_project", "project_request_keys_run", "project_request_keys_at"} <= {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")})
        plan = " ".join(str(row[-1]) for row in connection.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM project_check_runs WHERE project_id=? AND state NOT IN ('done') AND "
            "rowid < (SELECT min(rowid) FROM (SELECT rowid FROM project_check_runs WHERE project_id=? ORDER BY rowid "
            "DESC LIMIT 2)) ORDER BY rowid LIMIT 200", ("p", "p")))
        self.assertIn("project_check_runs_project", plan)

    def test_a_journal_an_earlier_build_of_this_branch_wrote_is_repaired_on_start(self):
        connection = self.journal.connection
        connection.execute("DROP TABLE project_request_keys")
        connection.execute("DROP INDEX project_check_runs_project")
        connection.execute("CREATE TABLE project_request_keys (owner TEXT NOT NULL, key TEXT NOT NULL, "
                           "project_id TEXT NOT NULL, grant_id TEXT, mode TEXT NOT NULL, revision INTEGER, "
                           "run TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(owner, key))")   # before run_id
        with self.assertRaises(JournalError) as broken:
            self.service.store.check_run("ffffffffffff")
        self.assertIn("restart Mobster so it can repair it", str(broken.exception))
        self.service.start()
        self.assertIn("run_id", {row[1] for row in connection.execute("PRAGMA table_info(project_request_keys)")})
        for number in range(3):
            self.assertEqual(self.start(f"repaired-{number}")["state"], "done")
        self.assertIsNotNone(self.service.requested_run(self.project["id"], "repaired-0"))

    def test_unfinished_runs_are_never_pruned(self):
        for number in range(4):
            run = CheckRun(f"{number:012x}", self.project["id"], self.path, request_id=f"going-{number}",
                           clock=lambda: self.now)
            self.service.store.claim_run(run.public())
        self.assertEqual(self.service.store.prune(self.project["id"], self.now), 0)
        self.assertEqual(self.rows(), ["going-0", "going-1", "going-2", "going-3"])

    def test_pruning_is_bounded_and_a_failure_midway_keeps_every_row_and_key(self):
        store = self.service.store
        for number in range(6):
            run = CheckRun(f"{number:012x}", self.project["id"], self.path, request_id=f"ended-{number}",
                           clock=lambda: self.now)
            store.claim_run(run.public())
            store.save_run({**run.public(), "state": "done", "exitCode": 0})
        self.journal.connection.execute(
            "CREATE TEMP TRIGGER second_key_fails BEFORE INSERT ON project_request_keys WHEN "
            "(SELECT count(*) FROM project_request_keys) >= 1 BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        with self.assertRaises(JournalError):
            store.prune(self.project["id"], self.now)
        self.assertEqual(len(self.rows()), 6)                                   # nothing went: no key was lost
        self.assertEqual(self.rows("project_request_keys", "key"), [])
        with mock.patch.object(self.service.store, "prune", side_effect=JournalError("disk")), \
                self.assertLogs("mobster", level="ERROR"):
            self.assertEqual(self.start("after-a-failed-prune")["state"], "done")  # a failed prune refuses nothing
        self.journal.connection.execute("DROP TRIGGER second_key_fails")
        with mock.patch.object(store_module, "PRUNE_BATCH", 2):
            self.assertEqual(store.prune(self.project["id"], self.now), 2)      # bounded
        self.assertEqual(len(self.rows()), 5)
        self.assertEqual(len(self.rows("project_request_keys", "key")), 2)

    def test_the_oldest_revision_no_kept_run_names_makes_room(self):
        store, project_id = self.service.store, self.project["id"]
        owner = "ci:12345678-1234-4234-8234-123456789abc"

        def name(revision):   # a kept CI run that names the revision
            run = CheckRun(f"{revision:012x}", project_id, self.path, mode="strict", request_id=f"ci-{revision}",
                           clock=lambda: self.now)
            store.claim_run(run.public(), owner=owner, grant_id="grant", revision=revision)

        with mock.patch.object(store_module, "MAX_REVISIONS", 3):
            for number in (1, 2, 3):
                store.approve_revision(project_id, {"n": number}, str(number) * 64)
            name(1)
            self.assertEqual(store.approve_revision(project_id, {"n": 4}, "4" * 64), (4, True))
            self.assertEqual([store.revision(project_id, number) for number in (1, 2, 3, 4)],
                             [{"n": 1}, None, {"n": 3}, {"n": 4}])
            name(3)
            with self.assertRaises(JournalError):   # 1 and 3 are named, and the newest, 4, always stays
                store.approve_revision(project_id, {"n": 5}, "5" * 64)
            self.assertEqual(store.approve_revision(project_id, {"n": 4}, "4" * 64), (4, False))

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mobile_agent.api_errors import APIError
from mobile_agent.graph.chain import from_chain
from mobile_agent.journal import Journal
from mobile_agent.server import make_handler
from mobile_agent.tests.seam_support import request
from mobile_agent.tests.test_workflows import FakeRuntime
from mobile_agent.workflows import health
from mobile_agent.workflows.service import Workflows

PROOF = [{"quote": "iOS 26.1"}]


class WorkflowHealthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mobster-workflow-health-test-")
        self.journal = Journal(Path(self.directory.name) / "runs.sqlite3")
        self.runtime = FakeRuntime(self.journal)
        self.time = datetime(2026, 10, 13, tzinfo=timezone.utc).timestamp()
        self.workflows = Workflows(self.runtime, lambda: self.time)
        self.runtime.workflows = self.workflows
        self.workflow = self.workflows.create({"name": "Read About", "appId": "settings", "goal": "Read iOS version"})

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def finish(self, key, status="completed", summary=None, reviewed=False):
        run, _ = self.workflows.run(self.workflow["id"], key)
        self.time += 60
        run.status, run.finished_at, run.summary, run.events, run.reviewed_ok = status, self.time, summary, [], reviewed
        return run

    def row(self):
        listed = self.workflows.list_with_health()
        self.assertEqual(len(listed), 1)
        return listed[0]["health"]

    def test_a_verified_run_is_the_last_success_with_its_answers_first_line(self):
        self.assertEqual(self.row(), {"lastSuccess": None, "attention": None, "recent": []})
        run = self.finish("one", summary={"proof": PROOF, "answer": "This iPhone is on iOS 26.1.\nIt's an iPhone 17."})
        state = self.row()
        self.assertEqual(state["lastSuccess"],
                         {"at": round(self.time * 1000), "runId": run.id, "line": "This iPhone is on iOS 26.1."})
        self.assertIsNone(state["attention"])

    def test_a_finished_run_without_proof_is_no_success_until_you_mark_it_ok(self):
        run = self.finish("one", summary={"answer": "Probably iOS 26."})
        self.assertEqual({key: self.row()[key]
                          for key in ("lastSuccess", "attention")}, {"lastSuccess": None, "attention": None})
        run.reviewed_ok = True
        self.assertEqual(self.row()["lastSuccess"]["runId"], run.id)

    def test_a_failure_needs_you_and_keeps_the_success_before_it(self):
        good = self.finish("one", summary={"proof": PROOF, "answer": "iOS 26.1"})
        bad = self.finish("two", status="error", summary={"reason": "Settings didn't open."})
        state = self.row()
        self.assertEqual(state["lastSuccess"]["runId"], good.id)
        self.assertEqual({key: state["attention"][key] for key in ("kind", "words", "detail", "runId")},
                         {"kind": "failed", "words": "Last run couldn’t finish", "detail": "Settings didn't open.",
                          "runId": bad.id})
        later = self.finish("three", summary={"proof": PROOF, "answer": "iOS 26.1"})
        self.assertIsNone(self.row()["attention"])
        self.assertEqual(self.row()["lastSuccess"]["runId"], later.id)

    def test_a_stop_by_design_or_your_own_stop_needs_nothing(self):
        for key, status in (("one", "stopped"), ("two", "approval_timeout"), ("three", "approval_denied")):
            with self.subTest(status=status):
                self.finish(key, status=status)
                self.assertIsNone(self.row()["attention"])

    def test_the_phone_being_unavailable_says_so(self):
        self.finish("locked", status="error", summary={"stop_code": "phone_locked", "reason": "Your iPhone is locked."})
        attention = self.row()["attention"]
        self.assertEqual((attention["kind"], attention["detail"]), ("phone", "Your iPhone is locked."))
        for code, words in (("run_active", "Skipped: your iPhone was busy with another task"),
                            ("device_unavailable", "Skipped: your iPhone wasn’t connected")):
            with self.subTest(code=code):
                self.runtime.failure = APIError("Unavailable", code=code)
                with self.assertRaises(APIError):
                    self.workflows.run(self.workflow["id"], code)
                self.runtime.failure = None
                self.assertEqual((self.row()["attention"]["kind"], self.row()["attention"]["words"]), ("phone", words))

    def test_a_limit_that_skipped_it_says_which(self):
        self.runtime.failure = APIError("Limit", code="monthly_limit_reached")
        with self.assertRaises(APIError):
            self.workflows.run(self.workflow["id"], "limit")
        self.assertEqual(self.row()["attention"]["kind"], "limit")

    def test_a_run_whose_record_is_gone_is_said_quietly_never_as_a_failure(self):
        # The run went (deleted with its conversation, or out of history) before its outcome was read.
        run, _ = self.workflows.run(self.workflow["id"], "gone")
        del self.runtime.runs[run.id]
        state = self.row()
        self.assertEqual({key: state["attention"][key] for key in ("kind", "words", "detail", "runId")},
                         {"kind": "unknown", "words": "Last run is no longer kept", "detail": None, "runId": None})
        self.assertEqual(state["recent"][0]["outcome"], "unknown")
        self.assertNotIn("failed", [item["outcome"] for item in state["recent"]])
        # A later verified run clears it.
        self.finish("worked", summary={"proof": PROOF, "answer": "iOS 26.1"})
        self.assertIsNone(self.row()["attention"])
        # A failure still says so.
        self.finish("broke", status="error", summary={"reason": "Settings didn't open."})
        self.assertEqual(self.row()["attention"]["kind"], "failed")

    def test_an_end_mobster_cant_read_never_hides_a_failure_before_it(self):
        attempts = [("run_expired", "gone", 3), ("error", None, 2), ("error", None, 1)]
        state = health.workflow_health(self.workflow, attempts, {})
        self.assertEqual(state["attention"]["kind"], "failed")
        self.assertEqual([item["outcome"] for item in state["recent"]], ["unknown", "failed", "failed"])
        # With a success before it, or nothing, the quiet line stays.
        alone = health.workflow_health(self.workflow, [("run_expired", "gone", 3)], {})
        self.assertEqual(alone["attention"]["kind"], "unknown")
        before = health.workflow_health(self.workflow, [("run_expired", "gone", 3), ("skipped_missed", None, 2)], {})
        self.assertEqual(before["attention"]["kind"], "unknown")

    def test_a_missed_time_neither_needs_you_nor_hides_an_older_failure(self):
        attempts = [("skipped_missed", None, 3), ("error", None, 2)]
        state = health.workflow_health(self.workflow, attempts, {})
        self.assertEqual(state["attention"]["kind"], "failed")
        self.assertIsNone(health.workflow_health(self.workflow, [("skipped_missed", None, 3)], {})["attention"])

    def test_a_schedule_missing_its_rows_or_a_value_needs_an_input(self):
        rows = {**self.workflow, "enabled": True, "repeat": {"kind": "each", "rows": None}}
        self.assertEqual(health.workflow_health(rows, [], {})["attention"]["kind"], "input")
        blank = {**self.workflow, "enabled": True, "goal": "Text {name} good morning"}
        self.assertEqual(health.workflow_health(blank, [], {})["attention"]["words"], "It needs a value for name")
        self.assertIsNone(health.workflow_health(blank, [], {}, filled={"name": "Sam"})["attention"])
        self.assertIsNone(health.workflow_health({**blank, "enabled": False}, [], {})["attention"])
        self.assertEqual(health.attention_for("missing_values", None)["kind"], "input")

    def test_its_last_runs_read_as_the_rows_history_newest_first(self):
        good = self.finish("one", summary={"proof": PROOF, "answer": "iOS 26.1"})
        looked = self.finish("two", summary={"answer": "Probably iOS 26."})
        bad = self.finish("three", status="error")
        mine = self.finish("four", status="stopped")
        recent = self.row()["recent"]
        self.assertEqual([(item["outcome"], item["runId"]) for item in recent],
                         [("stopped", mine.id), ("failed", bad.id), ("check", looked.id), ("done", good.id)])
        self.assertTrue(all(isinstance(item["at"], (int, float)) for item in recent))
        attempts = [("started", None, 9), ("skipped_missed", None, 8), ("skipped_busy", None, 7),
                    ("missing_values", None, 6)]
        self.assertEqual([item["outcome"]
                          for item in health.workflow_health(self.workflow, attempts, {})["recent"]],
                         ["running", "skipped", "skipped"])
        self.assertEqual(
            len(health.workflow_health(self.workflow, [("error", None, n) for n in range(15)], {})["recent"]), 10)

    def test_a_workflow_of_steps_reads_its_batch_status(self):
        repeat = {"kind": "times", "times": 2}
        steps = {**self.workflow, "repeat": repeat, "graph": from_chain(self.workflow["steps"], repeat)}
        attempts = [("completed", None, 4), ("delivery_failed", None, 3), ("stopped", None, 2),
                    ("approval_timeout", None, 1)]
        self.assertEqual([item["outcome"]
                          for item in health.workflow_health(steps, attempts, {})["recent"]],
                         ["done", "failed", "stopped", "stopped"])
        last = type("R", (), {"id": "000000000009", "status": "completed", "finished_at": 100.0,
                              "summary": {"answer": "Two rounds"}, "events": [], "reviewed_ok": False})()
        state = health.workflow_health(steps, [("completed", last.id, 5)], {last.id: last})
        self.assertEqual((state["lastSuccess"]["line"], state["attention"]), ("Two rounds", None))
        state = health.workflow_health(steps, [("delivery_failed", last.id, 6), ("completed", last.id, 5)],
                                       {last.id: last})
        self.assertEqual(state["attention"]["words"], "Last run couldn’t send its result")

    def test_the_last_success_outlives_the_week_its_dispatch_is_kept(self):
        run = self.finish("one", summary={"proof": PROOF, "answer": "iOS 26.1"})
        self.workflows.list()
        with self.journal.transaction() as connection:
            connection.execute("DELETE FROM workflow_dispatches")
        self.assertEqual(self.row()["lastSuccess"]["runId"], run.id)

    def test_get_workflows_sends_each_rows_health(self):
        self.finish("one", status="error")
        status, data, _ = request(make_handler(self.runtime), "GET", "/api/workflows")
        self.assertEqual((status, data["workflows"][0]["health"]["attention"]["kind"]), (200, "failed"))

    def test_the_outcome_reads_as_the_app_reads_it(self):
        def run(status, summary=None, reviewed=False):
            return type("R", (),
                        {"id": "a", "status": status, "finished_at": 1.0, "summary": summary, "events": [],
                         "reviewed_ok": reviewed})()
        self.assertEqual(health.run_outcome(run("completed", {"proof": PROOF})), "done")
        self.assertEqual(health.run_outcome(run("completed")), "check")
        self.assertEqual(health.run_outcome(run("completed", {"outcome": "done"})), "check")
        self.assertEqual(health.run_outcome(run("completed", reviewed=True)), "done")
        self.assertEqual(health.run_outcome(run("something_new")), "check")
        self.assertEqual(health.run_outcome(run("error", {"proof": PROOF})), "couldnt_finish")

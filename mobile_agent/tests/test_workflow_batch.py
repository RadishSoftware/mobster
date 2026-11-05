import itertools
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent.api_errors import APIError
from mobile_agent.graph import nodes as graph_nodes
from mobile_agent.graph.run import GraphRun
from mobile_agent.journal import Journal, JournalError, encode
from mobile_agent.tests.test_server_engine import Base as EngineBase
from mobile_agent.workflows import records as batch
from mobile_agent.workflows.runs import WorkflowDispatchUncertain
from mobile_agent.workflows.service import Workflows


class Phone:
    def __init__(self, journal, script=None):
        self.journal, self.lock, self.runs = journal, threading.RLock(), {}
        self.calls, self.events, self.stopped = [], [], []
        self.script = script or (lambda goal, fields: ("completed", {"answer": f"Did: {goal}"}, 0.01))
        self.config = SimpleNamespace(port=8765)
        self.before_create = None
        self.closing = False

    def publish(self, topic, event):
        self.events.append((topic, event))

    def create(self, app_id, goal, mode, key, **fields):
        prior = self.journal.lookup(key)
        if prior:
            return self.runs[prior[0]], True
        if self.before_create:
            self.before_create(key)
        outcome = self.script(goal, fields)
        if isinstance(outcome, Exception):
            raise outcome
        self.calls.append(SimpleNamespace(app=app_id, goal=goal, key=key, **fields))
        identifier = f"{len(self.calls):012x}"
        run = SimpleNamespace(id=identifier, goal=goal, status="running", summary=None, finished_at=None,
                              cost_usd=None, condition=threading.Condition(), extras=fields.get("extras") or {})
        run.public = lambda: {"id": identifier, "status": run.status}
        self.journal.create({"id": identifier, "status": "running", "finishedAt": None}, key, "test")
        self.runs[identifier] = run
        if outcome != "hold":
            self.finish(run, *outcome)
        return run, False

    def finish(self, run, status, summary=None, cost=None):
        with run.condition:
            run.status, run.summary, run.cost_usd, run.finished_at = status, summary or {}, cost, time.time()
            run.condition.notify_all()

    def stop_run(self, identifier):
        self.stopped.append(identifier)
        run = self.runs[identifier]
        if run.finished_at is None:
            self.finish(run, "stopped", {"reason": "Stopped"})


class BatchBase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mobster-batch-test-")
        self.journal = Journal(Path(self.directory.name) / "runs.sqlite3")
        self.phone = Phone(self.journal)
        self.time = datetime(2026, 10, 13, tzinfo=timezone.utc).timestamp()
        self.workflows = Workflows(self.phone, lambda: self.time, alerts=None)
        self.phone.workflows = self.workflows
        self.clipboard = []
        patcher = patch.object(graph_nodes, "clipboard", staticmethod(self.clipboard.append))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.journal.close)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.workflows.close)

    def save(self, steps, **fields):
        return self.workflows.create({"name": "Morning round", "steps": steps, **fields})

    def go(self, workflow, key="k1", **options):
        started = self.workflows.start(workflow["id"], key, **options)
        return started, self.wait(workflow)

    def wait(self, workflow, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if workflow["id"] not in self.workflows.running():
                listed = self.workflows.batch_runs(workflow["id"])
                if listed and listed[0]["status"] != "running":
                    return listed[0]
            time.sleep(0.005)
        self.fail("the batch didn't end")


def until(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if found := condition():
            return found
        time.sleep(0.01)
    raise AssertionError("the condition never held")


TASK = {"id": "a", "kind": "task", "text": "Open Messages and read the newest message from {who: Sam}", "appId": None}
TWO = {"id": "b", "kind": "task", "text": "Open Notes and write down what {who} said", "appId": None}


class FormatTests(BatchBase):
    def test_a_starter_becomes_one_task_card_and_runs_as_before(self):
        record = {"id": "0123456789ab", "name": "Read About", "appId": "settings", "appName": "Settings",
                  "goal": "Read iOS version", "outputSchema": None, "outputFormat": "auto", "engine": None,
                  "device": None, "cron": None, "timezone": "UTC", "enabled": False, "revision": 4, "createdAt": 1,
                  "updatedAt": 1, "nextAt": None, "lastRunId": None, "lastOutcome": None, "version": 2,
                  "sameConversation": False, "homeThreadId": None, "lastThreadId": None}
        with self.journal.transaction() as connection:
            connection.execute("INSERT INTO workflows VALUES (?,?)", (record["id"], encode(record)))
        opened = Workflows(self.phone, lambda: self.time, alerts=None).get(record["id"])
        self.assertEqual((opened["version"], opened["revision"], opened["goal"]), (4, 4, "Read iOS version"))
        self.assertEqual(opened["steps"],
                         [{"id": "s1", "kind": "task", "text": "Read iOS version", "appId": "settings"}])
        self.assertEqual((opened["repeat"], opened["limits"]), (None, batch.DEFAULT_LIMITS))
        run, replayed = self.workflows.run(record["id"], "old-starter")
        self.assertFalse(replayed)
        self.assertEqual([(c.app, c.goal, c.key.split(":")[2]) for c in self.phone.calls],
                         [("settings", "Read iOS version", "manual")])
        self.assertEqual(self.workflows.batch_runs(record["id"]), [])
        saved = self.workflows.update(record["id"], {"revision": 4, "goal": "Read the iOS build"})
        self.assertEqual(saved["steps"][0]["text"], "Read the iOS build")

    def test_cards_make_the_words_and_blanks(self):
        workflow = self.save([TASK, TWO, {"id": "c", "kind": "send", "to": "clipboard"}])
        self.assertEqual(workflow["goal"], TASK["text"] + "\n" + TWO["text"])
        self.assertEqual(workflow["blanks"], [{"name": "who", "default": "Sam"}])
        self.assertEqual(workflow["steps"][2], {"id": "c", "kind": "send", "to": "clipboard", "what": "last"})
        with self.assertRaisesRegex(ValueError, "several steps"):
            self.workflows.update(workflow["id"], {"revision": 1, "goal": "One thing"})
        single = self.save([{"id": "x", "kind": "task", "text": "Read the time", "appId": "settings"}])
        self.assertEqual((single["appId"], single["goal"]), ("settings", "Read the time"))

    def test_the_format_is_checked_before_anything_is_saved(self):
        cases = [
            ([], "at least one step"),
            ([{"id": "a", "kind": "branch"}], "kind is"),
            ([TASK, {**TWO, "id": "a"}], "own id"),
            ([{"id": "w", "kind": "wait", "seconds": 600}], "Add a task"),
            ([TASK, {"id": "w", "kind": "wait", "seconds": 2}], "5 to 86,400"),
            ([TASK] + [{**TWO, "id": f"s{i}"} for i in range(20)], "at most 20 steps"),
            ([TASK, {"id": "t", "kind": "taught", "workflowId": "0123456789ab"}], "was deleted"),
            ([TASK, {"id": "d", "kind": "send", "to": "destination", "destination": "0123456789ab"}],
             "isn't available"),
            ([TASK, {"id": "d", "kind": "send", "to": "fax"}], "Send to"),
            ([{**TASK, "appId": "com.example.nothing"}], "list of apps"),
        ]
        for steps, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.save(steps)
        for repeat, message in [({"kind": "times", "times": 51}, "2 to 50"),
                                ({"kind": "until", "check": "x", "max": 0}, "1 to 50"),
                                ({"kind": "forever"}, "Repeat"), ({"kind": "each", "rows": []}, "1 to 200 rows")]:
            with self.subTest(repeat=repeat), self.assertRaisesRegex(ValueError, message):
                self.save([TASK], repeat=repeat)
        with self.assertRaisesRegex(ValueError, "maxSpendUsd"):
            self.save([TASK], limits={"maxSpendUsd": 500})
        self.assertEqual(self.workflows.list(), [])

    def test_a_taught_step_names_a_one_task_workflow_other_than_itself(self):
        taught = self.workflows.create({"name": "Log water", "appId": "settings", "goal": "Log one glass of water"})
        several = self.save([TASK, TWO])
        workflow = self.save([{"id": "t", "kind": "taught", "workflowId": taught["id"]}])
        self.assertEqual(workflow["steps"][0]["workflowId"], taught["id"])
        with self.assertRaisesRegex(ValueError, "several steps"):
            self.save([{"id": "t", "kind": "taught", "workflowId": several["id"]}])
        with self.assertRaisesRegex(ValueError, "itself"):
            self.workflows.update(workflow["id"], {"revision": 1, "steps": [{"id": "t", "kind": "taught",
                                                                             "workflowId": workflow["id"]}]})

    def test_a_schedule_over_rows_needs_its_rows_saved(self):
        with self.assertRaisesRegex(ValueError, "rows saved"):
            self.save([TASK], repeat={"kind": "each", "rows": None}, cron="0 9 * * *", timezone="UTC", enabled=True)
        saved = self.save([{**TASK, "text": "Text {who} good morning"}],
                          repeat={"kind": "each", "rows": [{"who": "Sam"}, {"who": "Kate"}]},
                          cron="0 9 * * *", timezone="UTC", enabled=True)
        self.assertTrue(saved["enabled"])


class RunTests(BatchBase):
    def test_steps_run_one_at_a_time_in_order_each_journaled_before_it_acts(self):
        seen = []

        def before(key):
            record = self.workflows.batches.recent(workflow["id"])[0]
            seen.append([(e["state"], e.get("key")) for e in record["log"] if e.get("key") == key])
        self.phone.before_create = before
        workflow = self.save([TASK, {"id": "w", "kind": "wait", "seconds": 5}, TWO,
                              {"id": "c", "kind": "send", "to": "clipboard"}])
        clock = itertools.count(time.monotonic(), 10)
        with patch.object(GraphRun, "monotonic", staticmethod(lambda: next(clock))):
            started, record = self.go(workflow, values={"who": "Kate"})
        self.assertEqual(started["run"]["id"], "000000000001")
        self.assertEqual([c.goal for c in self.phone.calls],
                         ["Open Messages and read the newest message from Kate",
                          "Open Notes and write down what Kate said"])
        self.assertEqual([c.origin for c in self.phone.calls], ["workflow", "workflow"])
        self.assertEqual(seen, [[("claimed", self.phone.calls[0].key)], [("claimed", self.phone.calls[1].key)]])
        self.assertEqual(record["status"], "completed")
        self.assertEqual([(e["kind"], e["state"]) for e in record["log"]],
                         [("task", "finished"), ("wait", "done"), ("task", "finished"), ("send", "done")])
        self.assertEqual(self.clipboard, ["Did: Open Notes and write down what Kate said"])
        saved = self.workflows.get(workflow["id"])
        self.assertEqual((saved["lastOutcome"], saved["lastRunId"]), ("completed", "000000000002"))
        self.assertEqual(record["rounds"][0]["status"], "completed")
        topics = {event["event"] for topic, event in self.phone.events if topic == "workflows"}
        self.assertTrue({"batch_started", "batch_step", "batch_finished"} <= topics)

    def test_repeat_times_runs_every_step_each_round(self):
        workflow = self.save([TASK, TWO], repeat={"kind": "times", "times": 3})
        _, record = self.go(workflow)
        self.assertEqual(len(self.phone.calls), 6)
        self.assertEqual([r["status"] for r in record["rounds"]], ["completed"] * 3)

    def test_repeat_each_row_fills_the_blanks_from_saved_rows_or_the_runs_own(self):
        text = {"id": "a", "kind": "task", "text": "Text {who} happy birthday", "appId": None}
        workflow = self.save([text], repeat={"kind": "each", "rows": [{"who": "Sam"}, {"who": "Kate"}]})
        _, record = self.go(workflow)
        self.assertEqual([c.goal for c in self.phone.calls], ["Text Sam happy birthday", "Text Kate happy birthday"])
        self.assertEqual([r["values"] for r in record["rounds"]], [{"who": "Sam"}, {"who": "Kate"}])
        _, record = self.go(workflow, key="k2", rows=[{"who": "Ana", "phone": "555"}])
        self.assertEqual(self.phone.calls[-1].goal, "Text Ana happy birthday")
        self.assertEqual(record["rounds"][0]["values"], {"who": "Ana", "phone": "555"})
        open_rows = self.save([{**text, "text": "Text {who} {wish}"}], repeat={"kind": "each", "rows": None})
        with self.assertRaisesRegex(ValueError, "rows to repeat"):
            self.workflows.start(open_rows["id"], "k3")
        calls = len(self.phone.calls)
        with self.assertRaisesRegex(ValueError, "wish"):
            self.workflows.start(open_rows["id"], "k4", rows=[{"who": "Sam"}])
        self.assertEqual(len(self.phone.calls), calls)

    def test_repeat_until_stops_on_the_first_yes_and_at_its_cap(self):
        answers = iter([False, False, True])

        def script(goal, fields):
            if fields.get("output_schema") == batch.OBSERVED_SCHEMA:
                self.assertIn("Don't send, post, buy, delete or change anything", goal)
                return "completed", {"data": {"observed": next(answers)}}, 0.0
            return "completed", {"answer": "Refreshed"}, 0.01
        self.phone.script = script
        workflow = self.save([{**TASK, "text": "Refresh the tracking page"}],
                             repeat={"kind": "until", "check": "the package says Delivered", "max": 5})
        _, record = self.go(workflow)
        self.assertEqual((record["status"], len(record["rounds"])), ("completed", 3))
        self.phone.script = lambda goal, fields: ("completed", {"data": {"observed": False}}, 0.0) \
            if fields.get("output_schema") == batch.OBSERVED_SCHEMA else ("completed", {"answer": "ok"}, 0.0)
        capped = self.save([{**TASK, "text": "Refresh the page"}],
                           repeat={"kind": "until", "check": "it says Shipped", "max": 2})
        _, record = self.go(capped, key="k2")
        self.assertEqual((record["status"], len(record["rounds"])), ("loop_limit", 2))
        self.assertEqual(self.workflows.get(capped["id"])["lastOutcome"], "loop_limit")

    def test_a_check_the_agent_cant_answer_stops_the_run(self):
        self.phone.script = lambda goal, fields: ("completed", {"answer": "Hmm"}, 0.0)
        workflow = self.save([TASK], repeat={"kind": "until", "check": "it says Shipped", "max": 3})
        _, record = self.go(workflow)
        self.assertEqual((record["status"], len(record["rounds"])), ("condition_unknown", 1))

    def test_a_declined_approval_stops_the_run_and_nothing_after_it_starts(self):
        def script(goal, fields):
            if goal.startswith("Open Notes"):
                return "approval_denied", {"reason": "You declined"}, 0.02
            return "completed", {"answer": "Read it"}, 0.01
        self.phone.script = script
        workflow = self.save([TASK, TWO, {"id": "x", "kind": "task", "text": "Post it to Slack", "appId": None},
                              {"id": "c", "kind": "send", "to": "clipboard"}], repeat={"kind": "times", "times": 2})
        _, record = self.go(workflow)
        self.assertEqual(record["status"], "approval_denied")
        self.assertEqual([c.goal.split(" ")[1] for c in self.phone.calls], ["Messages", "Notes"])
        self.assertEqual((self.clipboard, len(record["rounds"])), ([], 1))
        self.assertEqual(record["rounds"][0]["status"], "approval_denied")
        self.assertEqual(self.workflows.get(workflow["id"])["lastOutcome"], "approval_denied")

    def test_the_workflows_spending_limit_caps_each_task_and_stops_the_run(self):
        self.phone.script = lambda goal, fields: ("completed", {"answer": "ok"}, 0.6)
        workflow = self.save([TASK, TWO], repeat={"kind": "times", "times": 5},
                             limits={"maxSpendUsd": 1.0, "maxMinutes": 60})
        _, record = self.go(workflow)
        self.assertEqual([round(c.cap_usd, 2) for c in self.phone.calls], [1.0, 0.4])
        self.assertEqual((record["status"], round(record["spentUsd"], 2)), ("spend_cap", 1.2))

    def test_the_monthly_limit_and_a_busy_phone_stop_the_run_without_retrying_a_started_task(self):
        self.phone.script = lambda goal, fields: APIError("This month's spend has reached your $2.00 limit.", 409,
                                                          "monthly_limit_reached")
        workflow = self.save([TASK, TWO])
        _, record = self.go(workflow)
        self.assertEqual((record["status"], self.phone.calls), ("monthly_limit", []))
        self.assertEqual(record["log"][0]["state"], "refused")
        busy = iter([APIError("busy", 409, "run_active")] * 2)
        self.phone.script = lambda goal, fields: next(busy, ("completed", {"answer": "ok"}, 0.0))
        with patch.object(graph_nodes, "BUSY_POLL_S", 0.01):
            _, record = self.go(workflow, key="k2")
        self.assertEqual((record["status"], len(self.phone.calls)), ("completed", 2))

    def test_stop_ends_the_task_on_the_phone_and_starts_nothing_after_it(self):
        self.phone.script = lambda goal, fields: "hold"
        workflow = self.save([TASK, TWO])
        started = self.workflows.start(workflow["id"], "k1")
        self.assertEqual(started["batch"]["status"], "running")
        with self.assertRaises(APIError) as busy:
            self.workflows.start(workflow["id"], "k2")
        self.assertEqual(busy.exception.code, "run_active")
        self.workflows.stop_batch(workflow["id"])
        record = self.wait(workflow)
        self.assertEqual((record["status"], self.phone.stopped, len(self.phone.calls)),
                         ("stopped", ["000000000001"], 1))
        with self.assertRaises(APIError) as idle:
            self.workflows.stop_batch(workflow["id"])
        self.assertEqual(idle.exception.code, "not_running")

    def test_a_round_cut_short_by_a_problem_ends_with_its_cost_not_running(self):
        def script(goal, fields):
            if goal.startswith("Open Notes"):
                return RuntimeError("boom")
            return "completed", {"answer": "Read it"}, 0.01
        self.phone.script = script
        workflow = self.save([TASK, TWO])
        with self.assertLogs("mobster.graph", level="ERROR"):
            _, record = self.go(workflow)
        self.assertEqual(record["status"], "error")
        self.assertEqual([(r["status"], r["answer"], r["costUsd"])
                          for r in record["rounds"]], [("error", "Read it", 0.01)])

    def test_a_round_that_doesnt_end_done_keeps_its_cost_and_answer(self):
        def script(goal, fields):
            if goal.endswith("B"):
                return "completion_not_confirmed", {
                    "answer": "Created the note B, but the screen didn't confirm it"}, 0.0897
            return "completed", {"answer": f"Created the note {goal[-1]}"}, 0.036
        self.phone.script = script
        text = {"id": "a", "kind": "task", "text": "In Notes, create a note: Mobster QA loop {item}", "appId": None}
        workflow = self.save([text], repeat={"kind": "each", "rows": [{"item": "A"}, {"item": "B"}, {"item": "C"}]})
        _, record = self.go(workflow)
        self.assertEqual(record["status"], "completion_not_confirmed")
        self.assertEqual([(r["status"], r["answer"], r["costUsd"]) for r in record["rounds"]],
                         [("completed", "Created the note A", 0.036),
                          ("completion_not_confirmed", "Created the note B, but the screen didn't confirm it", 0.0897)])
        self.assertIn("didn't finish", record["rounds"][1]["reason"])
        self.assertEqual(round(record["spentUsd"], 4), 0.1257)

    def test_the_same_request_key_never_starts_a_second_batch(self):
        workflow = self.save([TASK, TWO])
        first, _ = self.go(workflow)
        again = self.workflows.start(workflow["id"], "k1")
        self.assertTrue(again["replayed"])
        self.assertEqual((again["run"]["id"], len(self.phone.calls)), (first["run"]["id"], 2))

    def test_a_taught_step_runs_the_saved_workflows_words_as_a_workflow_run(self):
        taught = self.workflows.create({"name": "Log water", "appId": "settings",
                                        "goal": "Log {glasses: 1} glass of water"})
        workflow = self.save([{"id": "t", "kind": "taught", "workflowId": taught["id"]}, TASK])
        _, record = self.go(workflow, values={"glasses": "2"})
        self.assertEqual((self.phone.calls[0].app, self.phone.calls[0].goal, self.phone.calls[0].origin),
                         ("settings", "Log 2 glass of water", "workflow"))
        self.assertEqual(record["status"], "completed")

    def test_a_delivery_that_fails_stops_the_run(self):
        workflow = self.save([TASK, {"id": "c", "kind": "send", "to": "clipboard"}, TWO])
        with patch.object(graph_nodes, "clipboard",
                          staticmethod(lambda text: (_ for _ in ()).throw(OSError("no pasteboard")))):
            _, record = self.go(workflow)
        self.assertEqual((record["status"], len(self.phone.calls)), ("delivery_failed", 1))

    def test_a_file_and_a_notification(self):
        folder = Path(self.directory.name) / "Downloads"
        workflow = self.save([TASK, {"id": "f", "kind": "send", "to": "file", "what": "all"},
                              {"id": "n", "kind": "send", "to": "notification"}])
        with patch.dict("os.environ", {"MOBSTER_WORKFLOW_FILES": str(folder)}):
            _, record = self.go(workflow)
        saved = list(folder.glob("Morning round *.md"))
        self.assertEqual(len(saved), 1)
        self.assertIn("Did: Open Messages", saved[0].read_text())
        notes = [event for topic, event in self.phone.events if event["event"] == "batch_notification"]
        self.assertEqual(notes[0]["title"], "Morning round")


class CompletionTests(BatchBase):
    def test_shutdown_accounts_for_the_entire_admission_interval_with_one_deadline(self):
        for target, method in ((self.workflows, "_admit"), (self.workflows.batches, "add")):
            with self.subTest(boundary=method):
                workflow = self.save([TASK, TWO])
                entered, release, closed = threading.Event(), threading.Event(), threading.Event()
                outcomes, errors = [], []
                original = getattr(target, method)

                def hold(*args, **kwargs):
                    result = original(*args, **kwargs)
                    entered.set()
                    release.wait(5)
                    return result

                def start():
                    try:
                        self.workflows.start(workflow["id"], "held-admission")
                    except Exception as error:
                        errors.append(error)

                def close():
                    outcomes.append(self.workflows.close(timeout=0.01))
                    closed.set()

                with patch.object(target, method, side_effect=hold):
                    worker, closer = threading.Thread(target=start), threading.Thread(target=close)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(5))
                        self.phone.closing = True
                        closer.start()
                        self.assertTrue(closed.wait(1), "close exceeded its deadline waiting for admission")
                        self.assertEqual(outcomes, [False], "admission owns writes before worker registration")
                    finally:
                        release.set()
                        worker.join(5)
                        if closer.ident is not None:
                            closer.join(5)
                    self.assertFalse(worker.is_alive())
                    self.assertFalse(closer.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(self.workflows.close())
                attempts = self.workflows.attempts(workflow["id"])
                try:
                    with self.assertRaises(APIError) as refused:
                        self.workflows.start(workflow["id"], "after-shutdown")
                    self.assertEqual(refused.exception.code, "shutting_down")
                    self.assertEqual(self.workflows.attempts(workflow["id"]), attempts)
                finally:
                    self.phone.closing = False
        self.assertEqual(self.phone.calls, [])

    def test_shutdown_owns_the_batch_during_linkage_and_failed_start_cleanup(self):
        linked, release_link = threading.Event(), threading.Event()
        cleaning, release_cleanup = threading.Event(), threading.Event()
        errors = []
        workflow = self.save([TASK, TWO])
        record_started, save = self.workflows._record_started, self.workflows.batches.save

        def hold_linkage(*args, **kwargs):
            record_started(*args, **kwargs)
            linked.set()
            release_link.wait(5)
            raise JournalError("the admission response was lost")

        def hold_cleanup(record):
            if record["status"] == "interrupted":
                cleaning.set()
                release_cleanup.wait(5)
            return save(record)

        def start():
            try:
                self.workflows.start(workflow["id"], "shutdown-linkage")
            except Exception as error:
                errors.append(error)

        with patch.object(self.workflows, "_record_started", side_effect=hold_linkage), \
                patch.object(self.workflows.batches, "save", side_effect=hold_cleanup):
            worker = threading.Thread(target=start)
            worker.start()
            try:
                self.assertTrue(linked.wait(5))
                self.phone.closing = True
                self.assertFalse(self.workflows.close(timeout=0), "the unstarted worker still owns journal writes")
                release_link.set()
                self.assertTrue(cleaning.wait(5))
                self.assertFalse(self.workflows.close(timeout=0), "failed-start cleanup still owns journal writes")
            finally:
                release_link.set()
                release_cleanup.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual([type(error) for error in errors], [JournalError])
        self.assertEqual(self.workflows.running(), {})
        self.assertEqual(self.workflows.batch_runs(workflow["id"])[0]["status"], "interrupted")
        self.assertTrue(self.workflows.close())
        self.assertEqual(self.phone.calls, [])

    def test_a_failed_final_write_rolls_back_both_records_and_recovers_without_retry(self):
        workflow = self.save([TASK, TWO])
        attempted = threading.Event()
        save = self.workflows._save

        def fail_save(connection, record):
            if record.get("lastOutcome") != "completed":
                return save(connection, record)
            attempted.set()
            raise RuntimeError("the workflow write failed")

        with self.assertLogs("mobster.batch", level="ERROR"), patch.object(self.workflows, "_save",
                                                                           side_effect=fail_save):
            self.workflows.start(workflow["id"], "failed-completion")
            self.assertTrue(attempted.wait(5))
            self.assertTrue(self.workflows.close())
        record = self.workflows.batch_runs(workflow["id"])[0]
        saved = self.workflows.get(workflow["id"])
        self.assertEqual((record["status"], saved["lastOutcome"], saved["lastRunId"]), ("running", "started", None))
        self.assertFalse(any(event.get("event") == "batch_finished" for _, event in self.phone.events))
        self.workflows.recover()
        self.assertEqual(self.workflows.batch_runs(workflow["id"])[0]["status"], "interrupted")
        self.assertEqual(len(self.phone.calls), 2)

    def test_deleting_a_workflow_during_its_batch_keeps_only_the_workers_ending(self):
        self.phone.script = lambda goal, fields: "hold"
        workflow = self.save([TASK, TWO])
        self.workflows.start(workflow["id"], "deleted-completion")
        record = self.workflows.batches.running()[0]
        self.workflows.delete(workflow["id"])
        self.assertTrue(self.workflows.close())
        with self.assertRaises(APIError):
            self.workflows.get(workflow["id"])
        self.assertEqual(self.workflows.batches.get(record["id"])["status"], "stopped")
        self.assertEqual(len(self.phone.calls), 1)

    def test_completion_commits_the_workflow_before_publication_and_close_waits_for_it(self):
        published, release = threading.Event(), threading.Event()
        observed = []
        workflow = self.save([TASK, TWO])
        publish = self.phone.publish

        def hold_publication(topic, event):
            publish(topic, event)
            if event.get("event") == "batch_finished":
                saved = self.workflows.get(workflow["id"])
                record = self.workflows.batches.get(event["batchId"])
                observed.append((saved["lastOutcome"], saved["lastRunId"], record["status"]))
                published.set()
                release.wait(5)

        with patch.object(self.phone, "publish", side_effect=hold_publication):
            try:
                self.workflows.start(workflow["id"], "completion")
                self.assertTrue(published.wait(5), "the batch never published its completion")
                self.assertEqual(observed, [("completed", "000000000002", "completed")])
                self.assertFalse(self.workflows.close(timeout=0), "publication still owns the batch worker")
            finally:
                release.set()
                self.assertTrue(self.workflows.close())


class RecoveryTests(BatchBase):
    def test_a_journal_failure_recording_the_start_leaves_nothing_running_and_the_workflow_free(self):
        workflow = self.save([TASK, TWO])
        real = self.workflows._outcome

        def failing(key, outcome, *args, **kwargs):
            if outcome == "started":
                raise JournalError("disk full")
            return real(key, outcome, *args, **kwargs)
        with patch.object(self.workflows, "_outcome", failing):
            with self.assertRaises(WorkflowDispatchUncertain):
                self.workflows.start(workflow["id"], "k1")
        self.assertEqual(self.workflows.running(), {})
        self.assertNotEqual(self.workflows.batch_runs(workflow["id"])[0]["status"], "running")
        self.assertEqual(self.phone.calls, [])
        self.assertTrue(self.workflows.close())
        started, _ = self.go(workflow, key="k2")
        self.assertIsNotNone(started["run"])

    def test_a_step_claimed_before_the_quit_is_linked_to_its_task_if_it_started(self):
        workflow = self.save([TASK, TWO])
        record = batch.new_record(self.workflows._get(workflow["id"]), "workflow:x:manual:y", None, 1)
        record["log"] = [{"state": "claimed", "key": "claimed-before-quit"}, {"state": "claimed", "key": "never-sent"}]
        record["rounds"] = [{"n": 1, "status": "running"}]
        self.workflows.batches.add(record)
        self.journal.create({"id": "00000000beef", "status": "running", "finishedAt": None}, "claimed-before-quit",
                            "test")
        self.workflows.recover()
        saved = self.workflows.batches.get(record["id"])
        self.assertEqual([(e["state"], e.get("runId")) for e in saved["log"]],
                         [("interrupted", "00000000beef"), ("interrupted", None)])
        self.assertEqual((saved["status"], saved["rounds"][0]["status"]), ("interrupted", "interrupted"))
        self.assertEqual(self.phone.calls, [])


class RuntimeTests(EngineBase):
    def test_steps_are_ordinary_tasks_capped_by_the_workflows_limit(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        runtime = self.runtime()
        workflow = runtime.workflows.create({"name": "Two steps", "limits": {"maxSpendUsd": 0.3, "maxMinutes": 30},
                                             "steps": [{"id": "a", "kind": "task", "text": "Open Settings",
                                                        "appId": None},
                                                       {"id": "b", "kind": "task", "text": "Open Clock",
                                                        "appId": None}]})
        status, body, _ = self.request(runtime, "POST", f"/api/workflows/{workflow['id']}/run", {},
                                       extra_headers={"Idempotency-Key": "run-1"})
        self.assertEqual(status, 201, body)
        first = runtime.runs[body["run"]["id"]]
        self.assertEqual((first.goal, first.origin, first.cap_usd, first.cap_from_workflow),
                         ("Open Settings", "workflow", 0.3, True))
        self.assertEqual(len(runtime.runs), 1)
        self.finish(runtime, first)
        second = until(lambda: next((run for run in runtime.runs.values()
                                     if run is not first and run.finished_at is None), None))
        self.assertEqual(second.goal, "Open Clock")
        self.finish(runtime, second)
        record = until(lambda: next((b for b in runtime.workflows.batch_runs(workflow["id"])[:1]
                                     if b["status"] != "running"), None))
        self.assertEqual(record["status"], "completed")
        status, listed, _ = self.request(runtime, "GET", f"/api/workflows/{workflow['id']}/runs")
        self.assertEqual((status, listed["batches"][0]["id"]), (200, record["id"]))
        status, stopped, _ = self.request(runtime, "POST", f"/api/workflows/{workflow['id']}/stop", {})
        self.assertEqual((status, stopped["code"]), (409, "not_running"))
        run = runtime.create(None, "Open Settings", "live", cap_usd=50)
        self.assertEqual((run.cap_usd, runtime.run_cap(run)), (None, 1.0))
        self.finish(runtime, run)
        with self.assertRaises(ValueError):
            runtime.create(None, "Open Settings", "live", cap_usd=0)

    def finish(self, runtime, run, status="completed"):
        super().finish(runtime, run, status)
        run.settled.set()


class ChatTests(unittest.TestCase):
    def test_mobster_chat_says_a_workflow_started_when_it_waits_before_its_first_task(self):
        import io

        from mobile_agent.threads import chat_once

        class Client:
            url = "http://127.0.0.1:8765"

            def find_workflow(self, wanted):
                return {"id": "abcdefabcdef", "name": "Watch the delivery"}

            def run_workflow(self, *args, **kwargs):
                return "0123456789ab", {"run": None, "replayed": False, "threadId": "0123456789ab",
                                        "batch": {"id": "fedcbafedcba", "status": "running"}}
        out = io.StringIO()
        chat = SimpleNamespace(thread_id=None, interactive=False)
        args = SimpleNamespace(set=[], workflow="Watch the delivery", new=False)
        with patch.object(chat_once, "remember_thread"):
            code = chat_once.workflow_once(Client(), chat, args, as_json=False, wait=5, out=out, fail=lambda *a: 1)
        self.assertEqual((code, chat.thread_id), (0, "0123456789ab"))
        self.assertIn("waits before its first task", out.getvalue())

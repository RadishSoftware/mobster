"""Record-to-check (testkit/record.py) from journal fixtures: the steps in plain words, the proof as expectations,
masked and secret text, the refusals; the journal read without a lock while it is open; `mobster test record`; the
Mac app's POST /api/tests/checks; and MCP record_check. Offline."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

from mobile_agent import __main__ as cli
from mobile_agent.extensions import Hooks
from mobile_agent.journal import Journal
from mobile_agent.server import Run, make_handler
from mobile_agent.testkit import record as R
from mobile_agent.tests.seam_support import isolate, request
from mobile_agent.tests.test_server_engine import Base
from mobile_agent.verify.checks import load_check

RUN = "3f9c2a1b7d0e"
DAYBREAK = {"id": "daybreak", "name": "Daybreak", "bundleId": "dev.mobster.daybreak", "installed": True}
NOW = 1_791_380_000_000


def events(*texts, launch=None):
    out = []
    if launch:
        out.append({"event": "frontier_action", "operation": "LAUNCH_APP", "target_label": launch, "step": 1})
    out += [{"event": "step", "n": n, "step": n, "text": text} for n, text in enumerate(texts, 1)]
    return out


def record(**changes):
    base = {"id": RUN, "app": dict(DAYBREAK), "appId": "daybreak", "appName": "Daybreak",
            "goal": "Open the paywall and check the annual plan", "mode": "live", "status": "completed",
            "createdAt": NOW, "finishedAt": NOW + 30_000, "engine": "smart",
            "summary": {"status": "completed", "proof": [
                {"quote": "Choose your plan", "app": "Daybreak", "screen": None},
                {"quote": "Annual $39.99 / year", "app": "Daybreak", "screen": "Daybreak Plus"}]},
            "events": events("Opened Daybreak", "Tapped Settings", "Tapped Show paywall",
                             "Typed “hello” in Search and pressed Return", "Read Annual: $39.99 / year",
                             "Read Annual: $39.99 / year")}
    base.update(changes)
    return base


class CheckFromRunTests(unittest.TestCase):
    def test_steps_proof_and_the_navigation_bar(self):
        data = R.check_data(record())
        self.assertEqual(data["app"], {"bundle": "dev.mobster.daybreak"})
        self.assertEqual(data["name"], "Open the paywall and check the annual plan")
        self.assertEqual(data["steps"], ["Tap Settings", "Tap Show paywall",
                                         "Type “hello” in Search and press Return", "Read Annual"])
        self.assertEqual(data["expect"], [{"text": "Choose your plan"}, {"text": "Annual $39.99 / year"},
                                          {"visible": {"role": "navbar", "id": "Daybreak Plus"},
                                           "name": "Ends on Daybreak Plus"}])

    def test_a_quick_task_ends_on_no_navigation_bar(self):
        """Quick mode's proof names a breadcrumb of taps (fast_proof.path_at: "General › About"), never a screen's
        title: as a navigation bar's id it would fail every run of the recorded check."""
        proof = {"status": "completed", "proof": [{"quote": "iOS Version 26.4", "app": "Settings",
                                                   "screen": "General › About"}]}
        self.assertEqual(R.check_data(record(engine="fast", summary=proof))["expect"], [{"text": "iOS Version 26.4"}])
        one = {"status": "completed", "proof": [{"quote": "iOS Version 26.4", "screen": "About"}]}
        self.assertEqual(R.check_data(record(engine="fast", summary=one))["expect"], [{"text": "iOS Version 26.4"}])
        self.assertEqual(R.check_data(record(summary=proof))["expect"], [{"text": "iOS Version 26.4"}])
        self.assertEqual(R.check_data(record(summary=one))["expect"][-1],
                         {"visible": {"role": "navbar", "id": "About"}, "name": "Ends on About"})

    def test_the_yaml_loads_back_as_the_same_check(self):
        text = R.check_yaml(record(), name="Annual plan", path=".mobster/checks/annual.yaml")
        self.assertTrue(text.startswith("# A Mobster check, recorded from the task “Open the paywall and check the "
                                        "annual plan” on "))
        self.assertIn("# Run it with: mobster test .mobster/checks/annual.yaml", text)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "annual.yaml"
            path.write_text(text)
            check = load_check(path)
        self.assertEqual((check.name, check.bundle_id, check.device), ("Annual plan", "dev.mobster.daybreak", None))
        self.assertEqual(len(check.expect), 3)

    def test_secrets_and_masked_text_never_reach_the_check(self):
        rec = record(events=events("Typed “••••••” in Password", "Typed “sk-abcdefghijklmnopqrstuv12” in Key"),
                     summary={"proof": [{"quote": "Your code is 482913"}, {"quote": "Card ••••••"},
                                        {"quote": "/regex/"}, {"quote": "Welcome back"}]})
        data = R.check_data(rec)
        text = yaml.safe_dump(data)
        self.assertNotIn("sk-abcdefghijklmnopqrstuv12", text)
        self.assertNotIn("482913", text)
        self.assertEqual(data["expect"], [{"text": "Welcome back"}])
        self.assertIn("# A step types text Mobster masked", R.check_yaml(rec))

    def test_long_runs_use_the_request_and_a_run_with_no_proof_says_to_add_expect(self):
        rec = record(events=events(*[f"Tapped Row {n}" for n in range(25)]), summary={"proof": []})
        data = R.check_data(rec)
        self.assertEqual(data["steps"], ["Open the paywall and check the annual plan"])
        self.assertEqual(data["expect"], [])
        self.assertIn("# The task proved nothing Mobster can check", R.check_yaml(rec))
        receipts = record(summary={}, events=events("Tapped Settings") + [
            {"event": "receipt", "quote": "General", "screen": "Settings"}])
        self.assertEqual(R.check_data(receipts)["expect"][0], {"text": "General"})

    def test_a_task_started_from_the_home_screen_checks_the_first_app_it_opened(self):
        rec = record(app={"id": "any", "name": "Any app", "bundleId": None}, appId="any",
                     events=events("Opened Settings", "Tapped General", launch="com.apple.Preferences"))
        data = R.check_data(rec)
        self.assertEqual((data["app"]["bundle"], data["steps"][0]), ("com.apple.Preferences", "Open Settings"))
        with self.assertRaisesRegex(R.RecordError, "didn't open an app"):
            R.check_data(record(app={"id": "any"}, appId="any", events=events("Went to the Home Screen")))

    def test_refusals(self):
        with self.assertRaises(R.RecordError) as caught:
            R.check_data(record(status="running", finishedAt=None))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "run_active"))
        with self.assertRaisesRegex(R.RecordError, "social or dating apps"):
            R.check_data(record(app={"id": "hinge", "bundleId": "co.hinge.app"}, appId="hinge"))

    def test_imperatives(self):
        for done, do in (("Opened Notes", "Open Notes"), ("Pressed and held Photo", "Press and hold Photo"),
                         ("Scrolled down", "Scroll down"), ("Swiped left on Row", "Swipe left on Row"),
                         ("Closed a pop-up", "Close the pop-up"), ("Went to the Home Screen", "Go to the Home Screen"),
                         ("Pressed Return", "Press Return"), ("Wrote the message", "Write the message"),
                         ("Searched for “Zara”", "Search for “Zara”"), ("Something new", "Something new")):
            self.assertEqual(R.imperative(done), do)


class JournalTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "state" / "mobster.sqlite3"

    def write(self, rec, *, close=True):
        journal = Journal(self.path)
        meta = {key: value for key, value in rec.items() if key != "events"}
        meta.update(status="running", finishedAt=None, summary=None)
        journal.create(meta)
        for seq, event in enumerate(rec["events"]):
            journal.append(meta, {**event, "seq": seq, "timestamp": NOW})
        final = {**meta, "status": rec["status"], "finishedAt": rec["finishedAt"], "summary": rec["summary"]}
        journal.finish(final, {"event": "run_finished", "status": rec["status"], "seq": len(rec["events"]),
                               "timestamp": NOW})
        if close:
            journal.close()
        return journal

    def test_a_run_is_read_without_a_lock_while_the_journal_is_open(self):
        journal = self.write(record(), close=False)
        self.addCleanup(journal.close)
        found = R.find_run(RUN, [Path("/nonexistent/mobster.sqlite3"), self.path])
        self.assertEqual(found["goal"], "Open the paywall and check the annual plan")
        self.assertEqual([e["event"] for e in found["events"]][-1], "run_finished")
        self.assertEqual(R.check_data(found)["app"]["bundle"], "dev.mobster.daybreak")
        journal.append  # the app still holds it and can keep writing
        before = self.path.read_bytes()
        R.find_run(RUN, [self.path])
        self.assertEqual(self.path.read_bytes(), before)

    def test_lookups_that_fail(self):
        self.write(record())
        with self.assertRaises(R.RecordError) as caught:
            R.find_run("aaaaaaaaaaaa", [self.path])
        self.assertEqual(caught.exception.code, "run_not_found")
        with self.assertRaisesRegex(R.RecordError, "12 characters"):
            R.find_run("../../etc", [self.path])
        with self.assertRaisesRegex(R.RecordError, "no task history"):
            R.find_run(RUN, [Path("/nonexistent/x.sqlite3")])


def main(argv):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(cli, "load_extensions", return_value=Hooks()), redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as stop:
            code = stop.code
    return code, out.getvalue(), err.getvalue()


class RecordCommandTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        patch = mock.patch.object(R, "find_run", lambda run_id, paths=None: record() if run_id == RUN else
                                  (_ for _ in ()).throw(R.RecordError("No task.", 404)))
        patch.start()
        self.addCleanup(patch.stop)

    def test_record_prints_or_writes_the_check(self):
        code, out, err = main(["test", "record", "--run", RUN, "--name", "Annual plan"])
        self.assertEqual(code, 0)
        self.assertIn("name: Annual plan", out)
        target = self.folder / ".mobster" / "checks" / "annual.yaml"
        code, out, err = main(["test", "record", "--run", RUN, "-o", str(target)])
        self.assertEqual((code, out), (0, ""))
        self.assertIn("Saved", err)
        self.assertIn("4 steps, 3 expectations", err)
        self.assertEqual(load_check(target).bundle_id, "dev.mobster.daybreak")
        code, _, err = main(["test", "record", "--run", RUN, "-o", str(target)])
        self.assertIn("Replaced", err)

    def test_record_usage_errors_exit_3(self):
        for argv, words in ((["test", "record"], "needs the task"),
                            (["test", "record", "--run", "aaaaaaaaaaaa"], "No task."),
                            (["test", "record", "--run", RUN, "extra"], "takes no paths"),
                            (["test", "record", "--run", RUN, "-o", "check.txt"], "ends in .yaml"),
                            (["test", "--run", RUN], "go with record")):
            with self.subTest(argv=argv):
                code, out, err = main(argv)
                self.assertEqual(code, 3)
                self.assertIn(words, err)


TOKEN = "t" * 40


class CopyAsCheckRouteTests(Base):
    def setUp(self):
        super().setUp()
        isolate(self)
        self.app = self.runtime(apps=(DAYBREAK,))
        self.handler = make_handler(self.app, token=TOKEN)

    def add_run(self, **changes):
        run = Run(app=dict(DAYBREAK), goal="Open the paywall and check the annual plan", mode="live")
        run.engine, run.status, run.finished_at = "smart", "completed", time.time()
        run.events = record()["events"]
        run.summary = record()["summary"]
        for key, value in changes.items():
            setattr(run, key, value)
        with self.app.lock:
            self.app.runs[run.id] = run
        return run

    def test_the_route_is_registered_and_answers_the_yaml(self):
        self.assertEqual(self.app.status()["extensions"]["testkit"], "ok")
        run = self.add_run()
        status, data, _ = request(self.handler, "POST", "/api/tests/checks", {"runId": run.id, "name": "Annual"},
                                  headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 200, data)
        self.assertEqual((data["name"], data["app"], data["steps"], data["expect"]),
                         ("Annual", "dev.mobster.daybreak", 4, 3))
        self.assertIn("app:\n  bundle: dev.mobster.daybreak", data["yaml"])

    def test_the_route_refuses_what_it_cant_record(self):
        auth = {"Authorization": f"Bearer {TOKEN}"}
        active = self.add_run(status="running", finished_at=None)
        for body, expected in (({"runId": "nope"}, (400, "bad_request")),
                               ({"runId": "aaaaaaaaaaaa"}, (404, "run_not_found")),
                               ({"runId": active.id}, (409, "run_active")),
                               ({"runId": active.id, "extra": 1}, (400, "bad_request")),
                               ({"runId": active.id, "name": "x" * 121}, (400, "bad_request"))):
            with self.subTest(body=body):
                status, data, _ = request(self.handler, "POST", "/api/tests/checks", body, headers=auth)
                self.assertEqual((status, data["code"]), expected)
        status, _, _ = request(self.handler, "POST", "/api/tests/checks", {"runId": active.id})
        self.assertEqual(status, 401)


class RecordCheckToolTests(unittest.TestCase):
    def test_record_check_returns_the_yaml_and_never_writes(self):
        from mobile_agent.mcp_server.tools import ToolSet
        from mobile_agent.testkit.mcp import TestTools
        isolate(self)
        from mobile_agent.mcp_server import registry
        registry.register_provider(TestTools())
        with tempfile.TemporaryDirectory() as folder:
            tools = ToolSet(runs_dir=Path(folder) / ".mobster" / "runs", keyless=True)
            self.addCleanup(tools.shutdown)
            listed = {tool["name"]: tool for tool in tools.list_tools()}
            self.assertEqual(listed["record_check"]["annotations"]["readOnlyHint"], True)
            with mock.patch.object(R, "find_run", lambda run_id, paths=None: record()):
                result = tools.call("record_check", {"run_id": RUN}, SimpleNamespace(started=time.monotonic()))
            self.assertFalse(result.is_error, result.text)
            self.assertIn("4 steps and 3 expectations", result.text)
            self.assertEqual(result.structured["app"], "dev.mobster.daybreak")
            self.assertEqual(list(Path(folder).iterdir()), [])
            with mock.patch.object(R, "find_run", side_effect=R.RecordError("No task 3f9c2a1b7d0e.", 404)):
                result = tools.call("record_check", {"run_id": RUN}, None)
            self.assertTrue(result.is_error)


if __name__ == "__main__":
    unittest.main()

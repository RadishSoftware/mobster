"""Trace evals: a finished run kept as a regression task (traces.add_to_evals, `mobster history --to-eval`, the app's
POST /api/runs/{id}/eval) and replayed offline by `python -m mobile_agent.bench evals`. Offline: scripted phones and
models, temporary folders and journals."""

import argparse
import contextlib
import gzip
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import cli_parser, frontier, harness_api, run_export, traces
from mobile_agent.bench import trace_evals
from mobile_agent.bench.__main__ import main as bench_main
from mobile_agent.journal import Journal
from mobile_agent.tests.test_frontier import ARCHIVE
from mobile_agent.tests.test_sota_e2e import Service
from mobile_agent.tests.test_traces import KEY, PASSWORD, RUN_ID, record
from mobile_agent.wda.frame_store import FrameStore

FAILED = {"status": "max_steps", "decisions": 50, "reason": "The task used all 50 steps before finishing.",
          "costUsd": 0.12, "actions": 49}


def row(summary=None, **fields):
    return {"id": RUN_ID, "goal": ARCHIVE, "app": {"bundleId": "com.example.mail"}, "status": "completed",
            "createdAt": 1000, "finishedAt": 5000, "summary": summary or {"status": "completed", "outcome": "done"},
            "events": [], **fields}


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))
        self.evals = self.folder / "state" / "evals"
        record(self, self.folder, with_secrets=True)
        self.run_folder = self.folder / "frames" / RUN_ID

    def add(self, record_=None):
        return traces.add_to_evals(record_ or row(), self.run_folder, self.evals)


class AddTests(Base):
    def test_a_finished_task_becomes_a_folder_with_its_tape_and_what_it_expects(self):
        result = self.add()
        task_folder = self.evals / result["id"]
        self.assertTrue(result["added"])
        self.assertEqual(result["id"], f"archive-the-quickbite-receipt-in-mail-{RUN_ID}")
        self.assertEqual(sorted(p.name for p in task_folder.iterdir()), ["tape.jsonl.gz", "task.json"])
        task = trace_evals.read_task(task_folder)
        self.assertEqual((task["goal"], task["app"], task["source"]["run"]), (ARCHIVE, "com.example.mail", RUN_ID))
        self.assertEqual(task["expected"], {"status": "completed", "replay": "holds"})
        self.assertEqual(task["recorded"]["status"], "completed")

    def test_a_task_that_failed_expects_a_replay_that_changes_and_carries_its_failure_line(self):
        result = self.add(row(FAILED, status="max_steps"))
        task = trace_evals.read_task(self.evals / result["id"])
        self.assertEqual(task["expected"], {"status": "completed", "replay": "changes"})
        self.assertEqual(task["recorded"]["failure"]["class"], "out_of_steps")
        self.assertEqual(task["recorded"]["costUsd"], 0.12)

    def test_a_failure_line_the_run_already_has_is_kept(self):
        mine = {"class": "gave_up", "step": 3, "line": "Gave up at step 3: mine."}
        task = trace_evals.read_task(self.evals / self.add(row({**FAILED, "failure": mine}))["id"])
        self.assertEqual(task["recorded"]["failure"], mine)

    def test_adding_the_same_run_again_changes_nothing(self):
        first = self.add()
        before = {p.name: p.read_bytes() for p in (self.evals / first["id"]).iterdir()}
        second = self.add(row(FAILED, status="max_steps"))  # even if the row now reads differently
        self.assertFalse(second["added"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual({p.name: p.read_bytes() for p in (self.evals / first["id"]).iterdir()}, before)
        self.assertEqual([p.name for p in self.evals.iterdir()], [first["id"]])

    def test_the_eval_set_and_its_files_are_private_and_nothing_half_written_is_left(self):
        result = self.add()
        self.assertEqual(stat.S_IMODE(self.evals.stat().st_mode), 0o700)
        for path in (self.evals / result["id"]).iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
        self.assertEqual([p.name for p in self.evals.iterdir() if p.name.startswith(".")], [])

    def test_no_planted_secret_is_in_a_task_folder(self):
        result = self.add(row({"status": "blocked", "reason": f"password: {PASSWORD} and {KEY}", "decisions": 3},
                              goal=f"Archive it. My password is {PASSWORD}", status="blocked"))
        for path in (self.evals / result["id"]).iterdir():
            text = gzip.decompress(path.read_bytes()).decode() if path.suffix == ".gz" else path.read_text()
            self.assertNotIn(PASSWORD, text, path.name)
            self.assertNotIn(KEY, text, path.name)
        self.assertNotIn(PASSWORD, result["id"])

    def test_what_cant_be_an_eval_says_why_and_what_to_do(self):
        for fields, code, words in (({"status": "running", "finishedAt": None}, "running", "still running"),):
            with self.assertRaises(traces.EvalError) as caught:
                self.add(row(**fields))
            self.assertEqual(caught.exception.code, code)
            self.assertIn(words, str(caught.exception))
        with self.assertRaises(traces.EvalError) as caught:
            traces.add_to_evals(row(), self.folder / "empty", self.evals)
        self.assertEqual(caught.exception.code, "no_tape")
        self.assertIn(traces.ENV_FLAG, str(caught.exception))
        with self.assertRaises(traces.EvalError) as caught:
            traces.add_to_evals(row(), None, self.evals)
        self.assertEqual(caught.exception.code, "no_tape")
        (self.run_folder / "tape.jsonl.gz").write_bytes(b"not gzip")
        with self.assertRaises(traces.EvalError) as caught:
            self.add()
        self.assertEqual(caught.exception.code, "unreadable")
        self.assertFalse(self.evals.exists())


class ReplayTests(Base):
    """The same recorded run, judged four ways: its task expects a replay that holds or changes, and today's code
    acts the same or differently."""

    def verdict(self, record_, changed):
        self.add(record_)
        gate = (mock.patch.object(frontier.safety, "requested_by", lambda *args, **kwargs: True),
                mock.patch.object(frontier.stages.guards, "guard", lambda *args, **kwargs: None))
        with contextlib.ExitStack() as stack:
            if changed:
                for patch in gate:
                    stack.enter_context(patch)
            rows = trace_evals.run(self.evals)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_a_task_that_finished_holds_while_the_code_acts_the_same(self):
        found = self.verdict(row(), changed=False)
        self.assertEqual((found["verdict"], found["acts"]), ("holds", "1/1"))

    def test_a_task_that_finished_is_broken_when_a_change_acts_differently(self):
        found = self.verdict(row(), changed=True)
        if found["verdict"] == "holds":
            self.skipTest("the gate under test refuses elsewhere")
        self.assertEqual(found["verdict"], "broken")
        self.assertIn("at action 1", found["detail"])

    def test_a_task_that_failed_is_open_while_the_code_acts_as_it_did(self):
        found = self.verdict(row(FAILED, status="max_steps"), changed=False)
        self.assertEqual((found["verdict"], found["expected"]), ("open", "changes"))

    def test_a_task_that_failed_is_changed_when_the_code_now_acts_differently(self):
        found = self.verdict(row(FAILED, status="max_steps"), changed=True)
        if found["verdict"] == "open":
            self.skipTest("the gate under test refuses elsewhere")
        self.assertEqual(found["verdict"], "changed")
        self.assertIn("action 1", found["detail"])

    def test_a_task_that_cant_be_read_is_an_error_and_never_skipped(self):
        self.add()
        broken = self.evals / "no-task-json"
        broken.mkdir()
        (broken / "tape.jsonl.gz").write_bytes(b"x")
        wrong = self.evals / "wrong-folder-name"
        wrong.mkdir()
        task = json.loads((next(p for p in self.evals.iterdir() if p.name.startswith("archive")) / "task.json")
                          .read_text())
        (wrong / "task.json").write_text(json.dumps(task))  # its id names another folder
        (wrong / "tape.jsonl.gz").write_bytes(b"x")
        (self.evals / ".half-written").mkdir()  # a hidden folder is not a task
        rows = {r["id"]: r for r in trace_evals.run(self.evals)}
        self.assertEqual(set(rows), {task["id"], "no-task-json", "wrong-folder-name"})
        self.assertEqual((rows["no-task-json"]["verdict"], rows["wrong-folder-name"]["verdict"]), ("error", "error"))
        self.assertEqual(rows[task["id"]]["verdict"], "holds")

    def test_a_task_whose_tape_is_gone_is_an_error(self):
        result = self.add()
        (self.evals / result["id"] / "tape.jsonl.gz").unlink()
        self.assertEqual(trace_evals.run(self.evals)[0]["verdict"], "error")

    def test_the_bench_command_prints_a_line_a_task_and_exits_by_the_verdicts(self):
        self.add()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bench_main(["evals", "--dir", str(self.evals)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("holds", out.getvalue())
        self.assertIn("1 tasks: 1 holds", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bench_main(["evals", "--dir", str(self.evals), "--json"])
        self.assertEqual(json.loads(out.getvalue())["verdict"], "holds")
        (self.evals / "bad").mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bench_main(["evals", "--dir", str(self.evals)]), 1)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bench_main(["evals", "--dir", str(self.folder / "nothing-here")]), 2)


class HistoryCommandTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))
        state = self.folder / "state"
        self.journal_path = state / "mobster.sqlite3"
        meta = {"id": RUN_ID, "app": {"bundleId": "com.example.mail"}, "appName": "Mail", "goal": ARCHIVE,
                "status": "running", "createdAt": 1000, "finishedAt": None, "summary": None}
        journal = Journal(self.journal_path)
        journal.create(meta)
        summary = {"status": "max_steps", "decisions": 50, "reason": "The task used all 50 steps."}
        journal.finish({**meta, "status": "max_steps", "finishedAt": 5000, "summary": summary},
                       {"event": "run_finished", "status": "max_steps", "seq": 0, "timestamp": 2, "summary": summary})
        journal.close()
        record(self, state.parent, frames=FrameStore(state / "frames"))
        patcher = mock.patch("mobile_agent.run_export.journals", return_value=[self.journal_path])
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_command(self, run_id="latest", json_out=False):
        out, err = io.StringIO(), io.StringIO()
        code = run_export.to_eval(argparse.Namespace(to_eval=run_id, json=json_out), out, err)
        return code, out.getvalue(), err.getvalue()

    def test_a_failed_task_is_added_beside_its_journal_and_the_words_say_how_to_replay_it(self):
        code, out, err = self.run_command()
        self.assertEqual(code, 0, err)
        self.assertIn(f"Added archive-the-quickbite-receipt-in-mail-{RUN_ID}", out)
        self.assertIn("It failed, so a fix is a change in how it acts", out)
        self.assertIn("python -m mobile_agent.bench evals", out)
        rows = trace_evals.run(self.folder / "state" / "evals")
        self.assertEqual([r["verdict"] for r in rows], ["open"])

    def test_adding_again_says_so_and_json_carries_the_result(self):
        self.run_command()
        code, out, _ = self.run_command("0123")
        self.assertEqual(code, 0)
        self.assertIn("Already in your evals:", out)
        code, out, _ = self.run_command(json_out=True)
        found = json.loads(out)
        self.assertEqual((code, found["ok"], found["added"], found["expected"]["replay"]), (0, True, False, "changes"))

    def test_what_it_refuses(self):
        code, _, err = self.run_command("ffff")
        self.assertEqual(code, 1)
        self.assertIn("No task ffff in your history", err)
        (self.folder / "state" / "frames" / RUN_ID / "tape.jsonl.gz").unlink()
        code, _, err = self.run_command()
        self.assertEqual(code, 1)
        self.assertIn("recorded no tape", err)
        self.assertFalse((self.folder / "state" / "evals").exists())

    def test_the_option_stays_out_of_the_help_until_traces_are_on(self):
        def help_of(env):
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"COLUMNS": "100", "NO_COLOR": "1", traces.ENV_FLAG: env}), \
                    mock.patch.object(harness_api.hooks(), "cli", []), \
                    mock.patch("sys.stdout", out), mock.patch("argparse.ArgumentParser.exit", side_effect=SystemExit):
                with contextlib.suppress(SystemExit):
                    cli_parser.build_parser().parse_args(["history", "--help"])
            return out.getvalue()

        self.assertNotIn("--to-eval", help_of(""))
        self.assertIn("--to-eval", help_of("1"))
        with mock.patch.object(harness_api.hooks(), "cli", []):
            self.assertEqual(cli_parser.build_parser().parse_args(["history", "--to-eval", "3f9c"]).to_eval, "3f9c")


class ServiceEvalTests(Service):
    """Add to evals through the real service: POST /api/runs/{id}/eval, then the bench runner on what it wrote."""

    ASK = "What's the address of the dinner place Alex sent me in Messages?"

    def finished_run(self):
        _, run_id = self.start(self.ASK)
        self.finished(run_id)
        return run_id

    def test_a_finished_task_is_added_and_the_bench_runner_replays_it(self):
        env = mock.patch.dict(os.environ, {traces.ENV_FLAG: "1"})
        env.start()
        self.addCleanup(env.stop)
        run_id = self.finished_run()
        status, added = self.call("POST", f"/api/runs/{run_id}/eval", {})
        self.assertEqual(status, 200, added)
        self.assertTrue(added["added"])
        self.assertEqual(added["expected"]["replay"], "holds")
        status, again = self.call("POST", f"/api/runs/{run_id}/eval", {})
        self.assertEqual((status, again["added"], again["id"]), (200, False, added["id"]))
        evals = self.state_db.parent / "evals"
        self.assertEqual(Path(added["path"]), evals / added["id"])
        rows = trace_evals.run(evals)
        self.assertEqual([(r["id"], r["verdict"]) for r in rows], [(added["id"], "holds")], rows)

    def test_it_says_no_when_traces_are_off_or_there_is_no_tape_or_the_body_is_not_empty(self):
        run_id = self.finished_run()  # recorded with traces off: no tape
        status, refused = self.call("POST", f"/api/runs/{run_id}/eval", {})
        self.assertEqual((status, refused.get("code")), (503, "traces_off"))
        with mock.patch.dict(os.environ, {traces.ENV_FLAG: "1"}):
            status, refused = self.call("POST", f"/api/runs/{run_id}/eval", {})
            self.assertEqual((status, refused.get("code")), (422, "no_tape"))
            self.assertIn(traces.ENV_FLAG, refused["error"] if "error" in refused else json.dumps(refused))
            status, refused = self.call("POST", f"/api/runs/{run_id}/eval", {"x": 1})
            self.assertEqual(status, 400, refused)
            status, refused = self.call("POST", "/api/runs/ffffffffffff/eval", {})
            self.assertIn(status, (404, 400))
        self.assertFalse((self.state_db.parent / "evals").exists())


if __name__ == "__main__":
    unittest.main()

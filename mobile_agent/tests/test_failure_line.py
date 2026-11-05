"""The failure line (failure_line.py): one sentence naming why a run failed and at which step, from its trace. Offline:
real frontier event tails from recorded runs (tests/fixtures/failures), and scripted summaries."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import failure_line, traces
from mobile_agent.failure_line import failure_line as line_of
from mobile_agent.run import Run

TAILS = {case["name"]: case["events"] for case in
         json.loads((Path(__file__).parent / "fixtures" / "failures" / "frontier_tails.json").read_text())}


def summary_of(events):
    """The run's result event as its summary, the way the loop's own ``result`` event reads it."""
    result = next(e for e in reversed(events) if e["event"] == "result")
    return {k: v for k, v in result.items() if k != "event"} | {"decisions": result.get("steps")}


class RecordedFailureTests(unittest.TestCase):
    """Seven failed iOSWorld runs (simulator apps, claude-sonnet-5-5 and others, 12 to 18 Oct 2026): the line's class
    and step are what the end of each trace says."""

    def check(self, name, klass, step, *words):
        events = TAILS[name]
        found = line_of(summary_of(events), events)
        self.assertEqual((found["class"], found["step"]), (klass, step), found)
        for word in words:
            self.assertIn(word, found["line"])

    def test_the_run_that_used_every_step_names_the_step_and_its_last_action(self):
        self.check("out of steps", "out_of_steps", 50, "Ran out of steps at step 50", "Tap “Start”")

    def test_the_run_that_reached_its_cost_limit_says_so(self):
        self.check("cost limit", "out_of_steps", 44, "Reached its cost limit at step 44")

    def test_a_step_refused_four_times_is_stuck_and_says_what_was_refused(self):
        self.check("refused the same step", "stuck", 23, "Stuck at step 23", "tapping 'Send now'")

    def test_a_step_that_failed_three_times_is_stuck_when_the_phone_refused_it(self):
        self.check("step failed three times", "stuck", 26, "Stuck at step 26", "DriverRejection")

    def test_the_models_own_blocked_is_a_give_up_with_its_sentence(self):
        self.check("model gave up", "gave_up", 36, "Gave up at step 36", "Could not place the CityRideX request")

    def test_a_step_that_timed_out_three_times_is_an_app_that_stopped_answering(self):
        self.check("app stopped answering", "app_crashed", 15, "The app stopped answering at step 15")

    def test_a_transport_error_is_an_error_not_a_crash(self):
        self.check("transport error", "error", 19, "Stopped by an error at step 19", "HTTP 404")

    def test_every_line_is_one_sentence_under_the_limit(self):
        for name, events in TAILS.items():
            with self.subTest(name):
                line = line_of(summary_of(events), events)["line"]
                self.assertLessEqual(len(line), failure_line.LIMIT)
                self.assertNotIn("\n", line)


class ClassTests(unittest.TestCase):
    def test_a_run_that_finished_stopped_or_was_declined_has_no_line(self):
        for status in ("completed", "stopped", "interrupted", "approval_denied"):
            self.assertIsNone(line_of({"status": status, "outcome": "done", "decisions": 4}, []), status)
        self.assertIsNone(line_of({}, []))
        self.assertIsNone(line_of(None, None))

    def test_a_done_the_screen_never_proved_is_premature_and_names_the_step_of_the_done(self):
        events = [{"event": "frontier_decision", "step": 4, "ops": ["TAP"]},
                  {"event": "frontier_decision", "step": 5, "ops": ["DONE"]}]
        reason = "The screen never showed this finished: send the message to Alex."
        found = line_of({"status": "completion_not_confirmed", "outcome": "check", "reason": reason,
                         "decisions": 7}, events)
        self.assertEqual((found["class"], found["step"]), ("premature_done", 6))
        self.assertEqual(found["line"], "Said it was done at step 6, without proof: " + reason)
        again = line_of({"status": "completed", "outcome": "check", "decisions": 7}, events)
        self.assertEqual(again["class"], "premature_done")
        self.assertIn("The screen never showed every part finished", again["line"])

    def test_a_prompt_that_stopped_the_run_is_blocked_by_a_prompt(self):
        for code in ("phone_locked", "face_id", "app_locked", "system_dialog", "keychain_locked"):
            found = line_of({"status": "blocked", "code": code, "reason": "Your iPhone is waiting for you.",
                             "decisions": 9}, [])
            self.assertEqual((found["class"], found["step"]), ("blocked_by_prompt", 9), code)
            self.assertEqual(found["line"], "Blocked at step 9 by a prompt: Your iPhone is waiting for you.")
        found = line_of({"status": "approval_timeout", "decisions": 3}, [])
        self.assertEqual(found["class"], "blocked_by_prompt")
        self.assertIn("got none", found["line"])

    def test_an_app_mobster_never_opens_is_the_wrong_app(self):
        found = line_of({"status": "blocked", "code": "blocked_app", "decisions": 5,
                         "reason": "Mobster never opens Wallet."}, [])
        self.assertEqual(found["class"], "wrong_app")
        self.assertEqual(found["line"], "Opened an app Mobster never opens at step 5: Mobster never opens Wallet.")

    def test_the_apps_plain_words_for_a_timeout_still_read_as_a_crash_from_the_events(self):
        # The app's summary rewrites a TimeoutError as "Your iPhone stopped answering" (engines.plain_error): the
        # events keep the raw error.
        events = [{"event": "frontier_decision", "step": 6, "ops": ["TAP"]},
                  {"event": "frontier_error", "step": 6, "detail": "TimeoutError: Operation deadline exceeded"}]
        found = line_of({"status": "error", "reason": "Your iPhone stopped answering. Check it is unlocked and "
                                                      "connected, then try again.", "decisions": 7}, events)
        self.assertEqual((found["class"], found["step"]), ("app_crashed", 7))

    def test_a_model_provider_error_is_never_called_a_crash(self):
        events = [{"event": "frontier_error", "step": 2, "detail": "TimeoutError: timed out"}]
        found = line_of({"status": "error", "model_error": "rate_limited", "decisions": 3,
                         "reason": "Anthropic is rate limiting your key. Wait a minute, then try again."}, events)
        self.assertEqual(found["class"], "error")
        self.assertIn("rate limiting", found["line"])

    def test_an_error_the_run_recovered_from_does_not_name_its_end(self):
        events = [{"event": "frontier_failed", "step": 2, "error": "TimeoutError: Operation deadline exceeded"}]
        events += [{"event": "frontier_decision", "step": n, "ops": ["TAP"]} for n in range(3, 20)]
        found = line_of({"status": "max_steps", "decisions": 20, "reason": "20 turns used"}, events)
        self.assertEqual(found["class"], "out_of_steps")

    def test_a_model_request_that_timed_out_is_an_error_not_an_app_that_stopped(self):
        # Two recorded runs (12 Oct 2026) ended at step 0 on the network, before any app was touched.
        for text in ("HTTP TimeoutError; Anthropic request outcome unknown", "HTTP gaierror; OpenAI request failed"):
            events = [{"event": "frontier_error", "step": 0, "detail": text}]
            found = line_of({"status": "error", "reason": text, "steps": 0}, events)
            self.assertEqual((found["class"], found["step"]), ("error", 1), text)
            self.assertEqual(found["line"], f"Stopped by an error at step 1: {text}.")

    def test_a_reopen_that_failed_long_ago_does_not_name_the_end(self):
        events = [{"event": "frontier_app_relaunched", "ok": False, "app": "com.example.app"}]
        events += [{"event": "frontier_decision", "step": n, "ops": ["TAP"]} for n in range(1, 12)]
        events += [{"event": "frontier_error", "step": 12, "detail": "TransportError: HTTP 404; request not retried"}]
        found = line_of({"status": "error", "reason": "TransportError: HTTP 404; request not retried",
                         "decisions": 13}, events)
        self.assertEqual((found["class"], found["step"]), ("error", 13))

    def test_a_reopen_that_failed_is_said(self):
        events = [{"event": "frontier_app_relaunched", "ok": False, "app": "com.example.app"}]
        found = line_of({"status": "error", "reason": "ValueError", "decisions": 4}, events)
        self.assertEqual(found["class"], "app_crashed")
        self.assertIn("could not reopen it", found["line"])

    def test_a_run_that_timed_out_says_time_not_steps(self):
        found = line_of({"status": "timeout", "decisions": 31}, [])
        self.assertEqual(found["line"], "Ran out of time at step 31.")

    def test_a_secret_in_the_reason_is_masked_in_the_line(self):
        found = line_of({"status": "blocked", "decisions": 4,
                         "reason": ("I could not log in with password: hunter2xyz "
                                    "and key sk-ant-fake3-ZXCVBNMASDFGHJKL1234.")},
                        [])
        self.assertNotIn("hunter2xyz", found["line"])
        self.assertNotIn("sk-ant-api03", found["line"])

    def test_no_events_still_gives_a_line(self):
        found = line_of({"status": "blocked", "reason": "No supported progress"}, [])
        self.assertEqual(found["step"], None)
        self.assertEqual(found["line"], "Gave up before its first step: No supported progress.")


class CheckTests(unittest.TestCase):
    def result(self, **fields):
        return {"verdict": "failed", "reason": {"class": "assertion", "message": "text “Welcome” failed"},
                "agent": {"status": "completed", "note": "Done.", "turns": 6}, "steps": [], **fields}

    def test_a_check_whose_smart_said_done_but_whose_expectations_failed_is_premature(self):
        found = failure_line.of_check(self.result())
        self.assertEqual((found["class"], found["step"]), ("premature_done", 6))
        self.assertEqual(found["line"], "Said it was done at step 6, but text “Welcome” failed.")

    def test_the_checks_other_ends(self):
        cases = [({"reason": {"class": "app_crashed", "message": "Crashed 2 s after the tap."}},
                  "app_crashed", "The app crashed at step 6: Crashed 2 s after the tap."),
                 ({"reason": {"class": "app_not_running", "message": "Paywall wasn't in front at the end."}},
                  "wrong_app", "Paywall wasn't in front at the end."),
                 ({"reason": {"class": "budget", "message": "Smart reached its spend cap."}},
                  "out_of_steps", "Reached its cost limit at step 6."),
                 ({"agent": {"status": "max_steps", "turns": 50}, "reason": {"class": "blocked", "message": "x"}},
                  "out_of_steps", "Ran out of steps at step 50."),
                 ({"agent": {"status": "blocked", "note": "No Upgrade button on this screen.", "turns": 4},
                   "reason": {"class": "blocked", "message": "Smart ended blocked"}},
                  "gave_up", "Gave up at step 4: No Upgrade button on this screen."),
                 ({"agent": {"status": "approval_denied", "turns": 3}, "reason": {"class": "blocked", "message": "x"}},
                  "blocked_by_prompt", "Smart stopped at step 3 at an approval it was not given.")]
        for fields, klass, line in cases:
            with self.subTest(klass=klass):
                found = failure_line.of_check(self.result(**fields))
                self.assertEqual((found["class"], found["line"]), (klass, line))

    def test_a_check_that_passed_or_could_not_run_has_none(self):
        self.assertIsNone(failure_line.of_check(self.result(verdict="passed")))
        self.assertIsNone(failure_line.of_check(self.result(verdict="couldnt_run", agent=None,
                                                            reason={"class": "simulator", "message": "No simulator."})))
        self.assertIsNone(failure_line.of_check(self.result(agent=None)))  # a key-less replay took no model turns
        self.assertIsNone(failure_line.of_check({}))


class RunTests(unittest.TestCase):
    def finished(self, summary, events=()):
        run = Run({"id": "mail", "name": "Mail"}, "Archive it", "live")
        run.events = [dict(e, seq=n, timestamp=n) for n, e in enumerate(events)]
        run.finish(dict(summary))
        return run

    def test_a_failed_run_carries_its_line_when_traces_are_on(self):
        summary = {"status": "max_steps", "decisions": 50, "reason": "The task used all 50 steps before finishing."}
        with mock.patch.dict(os.environ, {traces.ENV_FLAG: "1"}):
            run = self.finished(summary, [{"event": "frontier_action", "operation": "TAP", "target_label": "Start"}])
        self.assertEqual(run.summary["failure"]["class"], "out_of_steps")
        self.assertIn("Tap “Start”", run.summary["failure"]["line"])
        self.assertEqual(run.public()["summary"]["failure"], run.summary["failure"])
        self.assertEqual(run.events[-1]["summary"]["failure"], run.summary["failure"])

    def test_nothing_is_added_with_traces_off_or_to_a_run_that_finished(self):
        with mock.patch.dict(os.environ, {traces.ENV_FLAG: ""}):
            self.assertNotIn("failure", self.finished({"status": "max_steps", "decisions": 50}).summary)
        with mock.patch.dict(os.environ, {traces.ENV_FLAG: "1"}):
            self.assertNotIn("failure", self.finished({"status": "completed", "outcome": "done"}).summary)
            self.assertNotIn("failure", self.finished({"status": "stopped"}).summary)

    def test_a_line_the_summary_already_has_is_kept(self):
        mine = {"class": "gave_up", "step": 2, "line": "Gave up at step 2: mine."}
        with mock.patch.dict(os.environ, {traces.ENV_FLAG: "1"}):
            run = self.finished({"status": "blocked", "decisions": 9, "failure": mine})
        self.assertEqual(run.summary["failure"], mine)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        from mobile_agent.journal import Journal
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))
        self.path = self.folder / "state" / "terminal.sqlite3"
        journal = Journal(self.path)
        for n, (status, failure) in enumerate((("max_steps", {"class": "out_of_steps", "step": 50,
                                                              "line": "Ran out of steps at step 50."}),
                                               ("completed", None))):
            meta = {"id": f"{n:012x}", "app": {"id": "mail"}, "appId": "mail", "appName": "Mail",
                    "goal": f"task {n}", "status": "running", "createdAt": 1_000_000 + n, "finishedAt": None,
                    "summary": None}
            journal.create(meta)
            summary = {"status": status, **({"failure": failure} if failure else {})}
            journal.finish({**meta, "status": status, "finishedAt": 2_000_000, "summary": summary},
                           {"event": "run_finished", "status": status, "seq": 0, "timestamp": 2, "summary": summary})
        journal.close()

    def test_a_failed_task_shows_its_line_under_it_and_in_the_json(self):
        from mobile_agent.history import print_history
        out = io.StringIO()
        print_history(path=self.path, stream=out)
        lines = out.getvalue().splitlines()
        failed = next(i for i, line in enumerate(lines) if "task 0" in line)
        self.assertEqual(lines[failed + 1].strip(), "Ran out of steps at step 50.")
        self.assertFalse(any("Ran out" in line for line in lines[failed + 2:]))
        out = io.StringIO()
        print_history(path=self.path, stream=out, as_json=True)
        rows = {row["goal"]: row for row in map(json.loads, out.getvalue().splitlines())}
        self.assertEqual(rows["task 0"]["failure"]["class"], "out_of_steps")
        self.assertNotIn("failure", rows["task 1"])


class PrCommentTests(unittest.TestCase):
    def doc(self, folder, **run_fields):
        run_dir = Path(folder) / "run"
        run_dir.mkdir()
        (run_dir / "result.json").write_text(json.dumps({
            "verdict": "failed", "reason": {"class": "assertion", "message": "text “Welcome” failed"},
            "agent": {"status": "completed", "note": "Done.", "turns": 6}, "steps": [], **run_fields}))
        result = {"device": "iPhone 17 Pro", "status": "failed", "runs": [{"runDir": str(run_dir)}],
                  "assertions": [{"ok": False, "text": "text “Welcome”", "observed": "not on screen"}]}
        return {"checks": [{"name": "Welcome", "file": "welcome.yaml", "results": [result]}], "exitCode": 1}

    def row(self, flag):
        from mobile_agent.testkit import pr_report
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {traces.ENV_FLAG: flag}):
            document = pr_report.document({}, self.doc(folder), folder=Path(folder), frames={}, gifs={})
            return document["rows"][0], pr_report.comment(document)

    def test_a_failed_check_shows_its_failure_line_in_the_comment_when_traces_are_on(self):
        row, comment = self.row("1")
        self.assertEqual(row["failure"], "Said it was done at step 6, but text “Welcome” failed.")
        self.assertIn("**Failure:** Said it was done at step 6, but text “Welcome” failed.", comment)

    def test_the_comment_is_as_it_was_with_traces_off(self):
        row, comment = self.row("")
        self.assertNotIn("failure", row)
        self.assertNotIn("**Failure:**", comment)


if __name__ == "__main__":
    unittest.main()

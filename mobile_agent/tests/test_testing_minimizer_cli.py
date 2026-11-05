"""The reducer CLI selects one local saved failure and preserves uncertain trials."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import __main__
from mobile_agent.testkit import minimizer_cli
from mobile_agent.testkit.minimize import MinimizationError

SUITE = "20261024-120000-abcd"
CONTROL = "20261024-130000-ef12"
CASE = "abcdabcdabcd"
TARGET = "beefbeefbeef"


class MinimizeCommandTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(minimizer_cli, "run", return_value=0) as run, \
                redirect_stdout(out), redirect_stderr(err):
            code = __main__.main(["test", "minimize", *argv])
        return code, run, err.getvalue()

    def test_public_parser_routes_exact_source_and_refuses_suite_flags(self):
        code, run, _ = self.main([SUITE, CONTROL, CASE, TARGET, "--bad-app", "bad.app",
                                  "--control-app", "good.app", "--isolated", "--max-attempts", "5", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[1], [SUITE, CONTROL, CASE, TARGET])
        self.assertTrue(run.call_args.args[0].minimize_isolated)
        self.assertEqual(run.call_args.args[0].minimize_max_attempts, 5)
        self.assertEqual(run.call_args.args[0].minimize_bad_app, "bad.app")
        for argv in ([SUITE, CONTROL, CASE, TARGET, "--bad-app", "bad.app", "--control-app", "good.app",
                      "--isolated", "--sim", "iPhone"],
                     [SUITE, CONTROL, CASE, TARGET, "--bad-app", "bad.app", "--control-app", "good.app",
                      "--isolated", "--share"]):
            with self.subTest(argv=argv):
                code, run, err = self.main(argv)
                self.assertEqual(code, 3)
                self.assertFalse(run.called)
                self.assertIn("doesn't go with mobster test minimize", err)

    def test_local_reducer_writes_only_from_exact_handles_and_reports_unknown(self):
        calls = []

        def reduce(root, bad_suite_id, control_suite_id, **kwargs):
            calls.append((root, bad_suite_id, control_suite_id, kwargs))
            return {"schema": "mobster.test-minimize/1", "badSuite": SUITE, "controlSuite": CONTROL,
                    "caseRef": CASE, "targetRef": TARGET, "status": "stopped_unknown",
                    "originalSteps": 4, "candidateSteps": None, "attemptsUsed": 1,
                    "candidateCheck": None, "reason": "A trial has an unknown outcome.", "trials": []}

        args = SimpleNamespace(json=True, minimize_isolated=True, minimize_max_attempts=5,
                               minimize_bad_app="bad.app", minimize_control_app="good.app")
        with mock.patch.object(minimizer_cli, "project_root", return_value=self.root):
            out = io.StringIO()
            self.assertEqual(minimizer_cli.run(args, [SUITE, CONTROL, CASE, TARGET], out=out, reduce=reduce), 3)
            self.assertEqual(json.loads(out.getvalue())["status"], "stopped_unknown")
        self.assertEqual(calls, [(self.root / ".mobster" / "test-results", SUITE, CONTROL,
                                  {"case_ref": CASE, "target_ref": TARGET,
                                   "bad_app": "bad.app", "control_app": "good.app",
                                   "isolated": True, "max_attempts": 5})])

    def test_refusal_precedes_running_and_a_candidate_is_review_only(self):
        args = SimpleNamespace(json=False, minimize_isolated=False, minimize_max_attempts=None,
                               minimize_bad_app="bad.app", minimize_control_app="good.app")
        reduce = mock.Mock()
        err = io.StringIO()
        self.assertEqual(minimizer_cli.run(args, [SUITE, CONTROL, CASE, TARGET], err=err, reduce=reduce), 3)
        reduce.assert_not_called()
        self.assertIn("--isolated", err.getvalue())
        args.minimize_isolated = True
        args.minimize_control_app = None
        self.assertEqual(minimizer_cli.run(args, [SUITE, CONTROL, CASE, TARGET], err=io.StringIO(),
                                           reduce=reduce), 3)
        reduce.assert_not_called()
        args.minimize_control_app = "good.app"
        candidate = {"status": "smaller_reproducer", "originalSteps": 4, "candidateSteps": 2,
                     "attemptsUsed": 3, "candidateCheck": ".mobster/drafts/repro/draft/candidate.yaml"}
        with mock.patch.object(minimizer_cli, "project_root", return_value=self.root):
            out = io.StringIO()
            self.assertEqual(minimizer_cli.run(args, [SUITE, CONTROL, CASE, TARGET], out=out,
                                               reduce=lambda *_a, **_k: candidate), 0)
        self.assertIn("Review .mobster/drafts/repro/draft/candidate.yaml", out.getvalue())
        with mock.patch.object(minimizer_cli, "project_root", return_value=self.root):
            err = io.StringIO()
            self.assertEqual(minimizer_cli.run(args, [SUITE, CONTROL, CASE, TARGET], err=err,
                                               reduce=mock.Mock(side_effect=MinimizationError(
                                                   "Source is not a clean failure"))), 3)
        self.assertIn("Source is not a clean failure", err.getvalue())

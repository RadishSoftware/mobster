"""Comparison CLI selects two saved suite IDs and never starts a test or Remote job."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import __main__
from mobile_agent.testkit import comparison_cli
from mobile_agent.testkit.comparison import ComparisonError

BASE = "20261024-120000-abcd"
CANDIDATE = "20261024-130000-ef12"


class ComparisonCommandTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(comparison_cli, "run", return_value=0) as run, \
                redirect_stdout(out), redirect_stderr(err):
            code = __main__.main(["test", "compare", *argv])
        return code, run, err.getvalue()

    def test_public_parser_routes_exactly_two_ids_and_rejects_suite_flags(self):
        code, run, _ = self.main([BASE, CANDIDATE, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[1], [BASE, CANDIDATE])
        for argv in ([BASE, CANDIDATE, "--device", "iPhone"],
                     [BASE, CANDIDATE, "--output", "folder"],
                     [BASE, CANDIDATE, "--share"]):
            with self.subTest(argv=argv):
                code, run, err = self.main(argv)
                self.assertEqual(code, 3)
                self.assertFalse(run.called)
                self.assertIn("doesn't go with mobster test compare", err)

    def test_local_result_preserves_both_verdicts_and_refuses_missing_reports(self):
        result = {"schema": "mobster.test-compare/1", "baseSuite": BASE, "candidateSuite": CANDIDATE,
                  "baseVerdict": 1, "candidateVerdict": 0, "comparable": False, "verifiedFix": False,
                  "reasons": ["fixture_unknown"], "cases": []}
        calls = []

        def compare(root, base, candidate):
            calls.append((root, base, candidate))
            return result

        with mock.patch.object(comparison_cli, "project_root", return_value=self.root):
            out, err = io.StringIO(), io.StringIO()
            code = comparison_cli.run(SimpleNamespace(json=True), [BASE, CANDIDATE], out=out, err=err,
                                      compare=compare)
            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(json.loads(out.getvalue()), result)
            self.assertEqual(calls, [(self.root / ".mobster" / "test-results", BASE, CANDIDATE)])

            def missing(*_args):
                raise ComparisonError("Saved suite is missing")

            out, err = io.StringIO(), io.StringIO()
            code = comparison_cli.run(SimpleNamespace(json=False), [BASE, CANDIDATE], out=out, err=err,
                                      compare=missing)
            self.assertEqual(code, 3)
            self.assertIn("Saved suite is missing", err.getvalue())

    def test_human_output_names_the_narrow_positive_proof(self):
        result = {"baseSuite": BASE, "candidateSuite": CANDIDATE, "baseVerdict": 1,
                  "candidateVerdict": 0, "comparable": True, "verifiedFix": True, "reasons": [], "cases": []}
        with mock.patch.object(comparison_cli, "project_root", return_value=self.root):
            out = io.StringIO()
            code = comparison_cli.run(SimpleNamespace(json=False), [BASE, CANDIDATE], out=out,
                                      compare=lambda *_: result)
        self.assertEqual(code, 0)
        self.assertIn("verified fix: yes, for controlled app state", out.getvalue())
        out = io.StringIO()
        with mock.patch.object(comparison_cli, "project_root", return_value=self.root):
            code = comparison_cli.run(SimpleNamespace(json=False), [BASE, CANDIDATE], out=out,
                                      compare=lambda *_: {**result, "verifiedFix": False})
        self.assertEqual(code, 0)
        self.assertIn("verified fix: no", out.getvalue())

"""Scenario CLI admits one local YAML manifest and returns the canonical suite verdict."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from mobile_agent import __main__
from mobile_agent.testkit import scenario_cli
from mobile_agent.testkit.api import UsageError

MANIFEST = ".mobster/scenarios/checkout.yaml"


class ScenarioCommandTests(unittest.TestCase):
    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(scenario_cli, "run", return_value=0) as run, \
                redirect_stdout(out), redirect_stderr(err):
            code = __main__.main(["test", "scenario", *argv])
        return code, run, err.getvalue()

    def test_public_parser_routes_manifest_and_refuses_suite_overrides(self):
        code, run, _ = self.main([MANIFEST, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[1], [MANIFEST])
        for argv in ([MANIFEST, "--device", "iPhone"], [MANIFEST, "--fresh"],
                     [MANIFEST, "--retries", "2"], [MANIFEST, "--output", "elsewhere"]):
            with self.subTest(argv=argv):
                code, run, err = self.main(argv)
                self.assertEqual(code, 3)
                self.assertFalse(run.called)
                self.assertIn("doesn't go with mobster test scenario", err)

    def test_report_and_failure_code_come_from_scenario_executor(self):
        planned = SimpleNamespace(name="Checkout", actors={"buyer": object()}, phases=(object(), object()))
        doc = {"schema": "mobster.test/1", "suite": "20261024-120000-abcd", "exitCode": 2,
               "scenario": {"name": "Checkout"}}
        calls = []

        def plan(path):
            calls.append(("plan", path))
            return planned

        def execute(value, *, progress):
            calls.append(("execute", value))
            return doc, {"folder": "/tmp/fake"}

        out, err = io.StringIO(), io.StringIO()
        code = scenario_cli.run(SimpleNamespace(json=True), [MANIFEST], out=out, err=err,
                                plan=plan, execute=execute)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue()), doc)
        self.assertEqual(calls, [("plan", MANIFEST), ("execute", planned)])

    def test_admission_refusal_does_not_execute(self):
        out, err = io.StringIO(), io.StringIO()

        def refuse(_path):
            raise UsageError("Use a saved check under .mobster/checks")

        code = scenario_cli.run(SimpleNamespace(json=False), [MANIFEST], out=out, err=err,
                                plan=refuse, execute=lambda *_args, **_kw: self.fail("executed"))
        self.assertEqual(code, 3)
        self.assertIn("saved check", err.getvalue())

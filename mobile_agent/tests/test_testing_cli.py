"""`mobster test` as a command (testkit/cli.py) and as MCP's run_tests (testkit/mcp.py), with a fake suite: flags to
options, exit codes, the summary and --json, usage errors that exit 3, ctrl+c, and run_tests that answers within
its budget and is followed by suite_id. Offline."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from mobile_agent import __main__ as cli
from mobile_agent.extensions import Hooks
from mobile_agent.testkit import api
from mobile_agent.testkit.mcp import TestTools
from mobile_agent.tests.seam_support import isolate


def fake_doc(exit_code=0, flaky=False, status="passed"):
    result = {"device": "iPhone 17 Pro · iOS 26.4", "deviceKey": "sim:a", "status": status, "attempts": 2 if flaky
              else 1, "flaky": flaky, "report": "runs/x.html", "durationMs": 4000, "healed": [], "quarantined": False,
              "reason": None if status == "passed" else {"class": "assertion", "message": "1 of 2 expectations "
                                                                                         "failed", "fix": None},
              "flakyReason": {"class": "assertion", "message": "Choose your plan wasn't on screen"} if flaky else None,
              "runs": []}
    return {"schema": "mobster.test/1", "suite": "20261007-120000-abcd", "exitCode": exit_code, "durationMs": 64_000,
            "options": {"repeat": 1}, "devices": [{"key": "sim:a", "name": "iPhone 17 Pro · iOS 26.4"}],
            "checks": [{"name": "The paywall shows three plans", "file": ".mobster/checks/paywall.yaml",
                        "results": [result]}],
            "summary": {"passed": int(status == "passed"), "failed": int(status == "failed"), "needsReview": 0,
                        "couldntRun": 0, "flaky": int(flaky), "quarantined": 0, "total": 1, "costUsd": 0.04}}


class Project(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        checks = self.root / ".mobster" / "checks"
        checks.mkdir(parents=True)
        (checks / "paywall.yaml").write_text("version: 1\nname: The paywall shows three plans\n"
                                             "app: {bundle: dev.mobster.daybreak}\ntags: [smoke]\nexpect: [{text: Choose}]\n")
        self.planned = []
        self.executed = []

    def main(self, argv, doc=None, raises=None):
        def execute(planned, progress=None, **kwargs):
            self.executed.append(planned)
            progress("▸ The paywall shows three plans · iPhone 17 Pro")
            if raises is not None:
                raise raises
            return doc or fake_doc(), {"html": self.root / "r" / "index.html", "junit": self.root / "r" / "junit.xml"}
        real_plan = api.plan

        def plan(options, **kwargs):
            self.planned.append(options)
            return real_plan(options, cwd=self.root)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "load_extensions", return_value=Hooks()), \
                mock.patch.object(api, "execute", execute), mock.patch.object(api, "plan", plan), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(argv)
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()


class CommandTests(Project):
    def test_flags_become_options(self):
        code, out, err = self.main(["test", str(self.root / ".mobster/checks"), "--sim", "iPhone 17 Pro@iOS 26.4",
                                    "--sim", "iPhone 16e", "--parallel", "2", "--repeat", "3", "--tag", "smoke",
                                    "--strict", "--keyless", "--video", "--junit", "build/junit.xml",
                                    "--html", "build/report", "--shard", "1/2"])
        self.assertEqual(code, 0, err)
        options = self.planned[0]
        self.assertEqual((options.sims, options.parallel, options.repeat, options.effective_retries, options.tags),
                         (("iPhone 17 Pro@iOS 26.4", "iPhone 16e"), 2, 3, 0, ("smoke",)))
        self.assertTrue(options.strict and options.keyless and options.video)
        self.assertEqual((options.junit, options.html, options.shard), ("build/junit.xml", "build/report", "1/2"))
        self.assertIn("1 check on 2 devices, 3 times each, 2 at a time", err)
        self.assertEqual(api.Options().effective_retries, 1)

    def test_the_summary_and_the_exit_code(self):
        code, out, err = self.main(["test"], doc=fake_doc(exit_code=0, flaky=True))
        self.assertEqual(code, 0)
        self.assertIn("~ flaky  The paywall shows three plans  ·  iPhone 17 Pro · iOS 26.4  (2 attempts)", out)
        self.assertIn("    Choose your plan wasn't on screen", out)
        self.assertIn("1 passed, 1 flaky · 1 min 04 s · $0.04", out)
        self.assertIn("Report  ", out)
        code, out, _ = self.main(["test"], doc=fake_doc(exit_code=1, status="failed"))
        self.assertEqual(code, 1)
        self.assertIn("✗ failed  The paywall shows three plans", out)
        self.assertIn("    1 of 2 expectations failed", out)

    def test_json_prints_the_results(self):
        code, out, _ = self.main(["test", "--json"], doc=fake_doc(exit_code=2))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["schema"], "mobster.test/1")

    def test_usage_errors_exit_3(self):
        for argv, words in ((["test", "--parallel", "9"], "--parallel"), (["test", "--bogus"], "unrecognized"),
                            (["test", "missing.yaml"], "doesn't exist"), (["test", "--shard", "3/2"], "--shard"),
                            (["test", "--tag", "nightly"], "No check has the tag nightly"),
                            (["test", "--matrix", "none.yaml"], "doesn't exist"),
                            (["test", "--quarantine", "none.yaml"], "doesn't exist")):
            with self.subTest(argv=argv):
                code, out, err = self.main(argv)
                self.assertEqual(code, 3)
                self.assertIn(words, err)
        self.assertEqual(self.executed, [])

    def test_paths_between_options_stay_paths(self):
        other = self.root / "more.yaml"
        other.write_text((self.root / ".mobster/checks/paywall.yaml").read_text())
        code, _, err = self.main(["test", str(other), "--sim", "iPhone 16e", str(self.root / ".mobster/checks")])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.planned[0].paths), 2)

    def test_ctrl_c_exits_130_and_an_unexpected_error_3(self):
        code, _, err = self.main(["test"], raises=KeyboardInterrupt())
        self.assertEqual(code, 130)
        self.assertIn("stopped", err)
        code, _, err = self.main(["test"], raises=RuntimeError("boom"))
        self.assertEqual(code, 3)
        self.assertIn("unexpected error (RuntimeError: boom)", err)

    def test_the_help_names_record_and_the_exit_codes(self):
        out = io.StringIO()
        with mock.patch.object(cli, "load_extensions", return_value=Hooks()), redirect_stdout(out), \
                self.assertRaises(SystemExit):
            cli.main(["test", "--help"])
        text = out.getvalue()
        for words in ("--sim TYPE[@RUNTIME]", "--device NAME", "--repeat K", "--quarantine FILE", "--run RUN_ID",
                      "exit codes: 0 passed, 1 failed, 2 needs review, 3 couldn't run, 130 ctrl+c"):
            self.assertIn(words, text)


class RunTestsToolTests(unittest.TestCase):
    def setUp(self):
        isolate(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        (self.root / ".mobster" / "checks").mkdir(parents=True)
        self.release = threading.Event()
        self.options = []

    def toolset(self, provider, **kwargs):
        from mobile_agent.mcp_server import registry
        from mobile_agent.mcp_server.tools import ToolSet
        registry.register_provider(provider)
        tools = ToolSet(runs_dir=self.root / ".mobster" / "runs", keyless=True, **kwargs)
        self.addCleanup(tools.shutdown)
        return tools

    def provider(self, wait=False):
        def plan(options):
            self.options.append(options)
            return SimpleNamespace(entries=[1], targets=[1], options=options, notes=[])

        def execute(planned, progress=None, stop=None, key=None, suite_id=None, on_suite=None):
            progress("▸ The paywall shows three plans · iPhone 17 Pro")
            if wait:
                self.release.wait(5)
            folder = self.root / ".mobster" / "test-results" / suite_id
            return fake_doc(exit_code=1, status="failed"), {"html": folder / "index.html", "junit": folder / "junit.xml",
                                                            "results": folder / "results.json"}
        return TestTools(execute=execute, plan=plan)

    def call(self, tools, args):
        return tools.call("run_tests", args, SimpleNamespace(started=time.monotonic(), stop=threading.Event(),
                                                             note=lambda message: None))

    def test_a_suite_that_ends_in_time_returns_its_summary(self):
        tools = self.toolset(self.provider())
        listed = {tool["name"]: tool for tool in tools.list_tools()}
        self.assertEqual(listed["run_tests"]["annotations"]["destructiveHint"], False)
        self.assertIn("run_tests runs the project's saved checks", tools.instructions())
        result = self.call(tools, {"sims": ["iPhone 17 Pro"], "parallel": 2, "retries": 0})
        self.assertFalse(result.is_error, result.text)
        self.assertIn("0 passed, 1 failed (exit code 1)", result.text)
        self.assertIn("- The paywall shows three plans on iPhone 17 Pro · iOS 26.4: failed. 1 of 2 expectations failed",
                      result.text)
        self.assertEqual(result.structured["status"], "finished")
        options = self.options[0]
        self.assertEqual((options.paths, options.sims, options.parallel, options.retries, options.keyless),
                         ((str(self.root / ".mobster" / "checks"),), ("iPhone 17 Pro",), 2, 0, True))  # --keyless server

    def test_a_long_suite_returns_its_id_then_waits_by_it(self):
        tools = self.toolset(self.provider(wait=True))
        first = self.call(tools, {"timeout_s": 1})
        self.assertEqual(first.structured["status"], "running")
        suite_id = first.structured["suite_id"]
        self.assertIn(f"Call run_tests with suite_id {suite_id}", first.text)
        busy = self.call(tools, {})
        self.assertTrue(busy.is_error)
        self.assertIn("still running", busy.text)
        self.release.set()
        done = self.call(tools, {"suite_id": suite_id, "timeout_s": 5})
        self.assertEqual(done.structured["status"], "finished")
        self.assertTrue(self.call(tools, {"suite_id": "20261007-000000-0000"}).is_error)
        self.assertTrue(self.call(tools, {"suite_id": suite_id, "sims": ["x"]}).is_error)

    def test_paths_must_be_absolute_and_simulators_only(self):
        tools = self.toolset(self.provider())
        self.assertIn("must be absolute", self.call(tools, {"path": "checks"}).text)
        from mobile_agent.mcp_server.protocol import ProtocolError
        with self.assertRaises(ProtocolError):
            self.call(tools, {"devices": ["Sam's iPhone"]})


if __name__ == "__main__":
    unittest.main()

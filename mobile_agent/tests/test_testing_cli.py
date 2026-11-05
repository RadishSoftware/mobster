import importlib.util
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import __main__ as cli
from mobile_agent import harness_api
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
                                             "app: {bundle: org.mozilla.ios.Firefox}\ntags: [smoke]\n"
                                             "expect: [{text: Choose}]\n")
        self.planned = []
        self.executed = []

    def main(self, argv, doc=None, raises=None):
        def execute(planned, progress=None, **kwargs):
            self.executed.append(planned)
            progress("▸ The paywall shows three plans on iPhone 17 Pro")
            if raises is not None:
                raise raises
            return doc or fake_doc(), {"html": self.root / "r" / "index.html", "junit": self.root / "r" / "junit.xml",
                                       "folder": self.root / "r"}
        real_plan = api.plan

        def plan(options, **kwargs):
            self.planned.append(options)
            return real_plan(options, cwd=self.root)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(harness_api.hooks(), "cli", []), \
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

    def test_xctest_flags_become_options_and_a_run_with_no_checks_still_runs_the_tests(self):
        code, out, err = self.main(["test", "--xctest", "--scheme", "Firefox", "--project", "App.xcodeproj",
                                    "--test-plan", "Unit", "--only-testing", "AppTests", "--only-testing",
                                    "AppTests/PlanTests/testX"])
        self.assertEqual(code, 0, err)
        options = self.planned[0]
        self.assertEqual((options.xctest, options.xctest_scheme, options.xctest_project, options.xctest_plan,
                          options.xctest_only), (True, "Firefox", "App.xcodeproj", "Unit",
                                                 ("AppTests", "AppTests/PlanTests/testX")))
        self.assertIn("1 check on", err)
        self.assertIn("then your XCTest targets", err)
        self.assertTrue(options.public()["xctest"])
        self.assertNotIn("xctest", api.Options().public())
        (self.root / ".mobster" / "checks" / "paywall.yaml").unlink()
        self.executed.clear()
        code, out, err = self.main(["test", "--xctest"])
        self.assertEqual((code, len(self.executed)), (0, 1))      # no saved check: the tests still run
        self.assertEqual(self.executed[0].entries, [])
        self.assertEqual(self.main(["test"])[0], 3)                 # and without --xctest, no checks is an error

    def test_the_summary_lists_failed_tests_with_their_file_and_line(self):
        from mobile_agent.tests.test_testing_xctest import suite_doc
        doc = suite_doc()
        code, out, err = self.main(["test", "--xctest"], doc=doc)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"✗ failed  FirefoxTests: PlanTests\.testAnnualPlanIsBilledYearly\n"
                              r"    PlanTests\.swift:\d+: ")
        self.assertIn("XCTest: 3 of 4 passed, 1 failed in 1 target", out)
        doc["xctest"] = {"status": "couldnt_run", "summary": {}, "targets": [],
                         "reason": {"message": "No simulator was free.", "fix": "Try again."}}
        out = self.main(["test", "--xctest"], doc=doc)[1]
        self.assertIn("your XCTest targets\n    No simulator was free. Try again.", out)

    def test_xctest_usage_errors_exit_3(self):
        for argv, words in ((["test", "--test-plan", "Unit"], "go with --xctest"),
                            (["test", "--only-testing", "T"], "go with --xctest"),
                            (["test", "--xctest", "--only-testing", "../x"], "isn't a test identifier"),
                            (["test", "--scheme", "Firefox"], "--scheme goes with pr")):
            with self.subTest(argv=argv):
                code, out, err = self.main(argv)
                self.assertEqual(code, 3)
                self.assertIn(words, err)
        self.assertEqual(self.executed, [])

    def test_the_summary_and_the_exit_code(self):
        code, out, err = self.main(["test"], doc=fake_doc(exit_code=0, flaky=True))
        self.assertEqual(code, 0)
        self.assertIn("~ flaky  The paywall shows three plans on iPhone 17 Pro, iOS 26.4  (2 attempts)", out)
        self.assertIn("    Choose your plan wasn't on screen", out)
        self.assertIn("1 passed, 1 flaky in 1 min 04 s, $0.04", out)
        self.assertIn("Report  ", out)
        code, out, _ = self.main(["test"], doc=fake_doc(exit_code=1, status="failed"))
        self.assertEqual(code, 1)
        self.assertIn("✗ failed  The paywall shows three plans", out)
        self.assertIn("    1 of 2 expectations failed", out)

    @unittest.skipUnless(importlib.util.find_spec("mobile_agent.teams"), "requires the app teams extension")
    def test_share_uploads_the_report_and_says_the_link_or_why_not_and_the_exit_code_stays_the_runs(self):
        from mobile_agent.teams import report_share
        shared = report_share.Shared(report_id="6b1f0a52-3c1e-4d7a-9a40-5be0c2d9e7f1", key=bytes(32),
                                     link="https://console.mobster.dev/r/6b1f0a52-3c1e-4d7a-9a40-5be0c2d9e7f1#k=x",
                                     expires_at=None)
        with mock.patch.object(report_share, "share_folder", return_value=shared) as share:
            code, out, err = self.main(["test", "--share", "--share-days", "7"], doc=fake_doc(exit_code=1,
                                                                                            status="failed"))
        self.assertEqual(code, 1)
        share.assert_called_once_with(self.root / "r", days=7)
        self.assertIn(f"Shared with your team for 7 days: {shared.link}", err)
        refused = report_share.ShareRefused("no_plan", "Hosted report links come with the Team plan. Your report is "
                                                       "in .mobster/test-results/s1/.")
        with mock.patch.object(report_share, "share_folder", side_effect=refused):
            code, out, err = self.main(["test", "--share"])
        self.assertEqual(code, 0)
        self.assertIn("mobster test: Hosted report links come with the Team plan.", err)
        with mock.patch.object(report_share, "share_folder") as share:
            self.main(["test"])
            self.assertEqual(self.main(["test", "--share-days", "45"])[0], 3)
        share.assert_not_called()

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

    def test_ctrl_c_exits_130_and_an_unexpected_error_3(self):
        code, _, err = self.main(["test"], raises=KeyboardInterrupt())
        self.assertEqual(code, 130)
        self.assertIn("stopped", err)
        code, _, err = self.main(["test"], raises=RuntimeError("boom"))
        self.assertEqual(code, 3)
        self.assertIn("unexpected error (RuntimeError: boom)", err)

    def test_json_prints_the_results(self):
        code, out, _ = self.main(["test", "--json"], doc=fake_doc(exit_code=2))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["schema"], "mobster.test/1")


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
            progress("▸ The paywall shows three plans on iPhone 17 Pro")
            if wait:
                self.release.wait(5)
            folder = self.root / ".mobster" / "test-results" / suite_id
            return fake_doc(exit_code=1, status="failed"), {"html": folder / "index.html",
                                                            "junit": folder / "junit.xml",
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
                         ((str(self.root / ".mobster" / "checks"),), ("iPhone 17 Pro",), 2, 0, True))

    def test_a_slow_plan_returns_a_waitable_suite_and_blocks_a_second_run(self):
        gate = threading.Event()
        inner = self.provider()
        slow = TestTools(execute=inner._execute, plan=lambda options: (gate.wait(5), inner._plan(options))[1])
        tools = self.toolset(slow)
        first = self.call(tools, {"timeout_s": 1})
        self.assertFalse(first.is_error, first.text)
        self.assertEqual(first.structured["status"], "running")
        suite_id = first.structured["suite_id"]
        busy = self.call(tools, {})
        self.assertTrue(busy.is_error)
        self.assertIn(f"Suite {suite_id} is still running", busy.text)
        gate.set()
        done = self.call(tools, {"suite_id": suite_id, "timeout_s": 5})
        self.assertEqual(done.structured["status"], "finished")

    def test_paths_must_be_absolute_and_simulators_only(self):
        tools = self.toolset(self.provider())
        self.assertIn("must be absolute", self.call(tools, {"path": "checks"}).text)
        from mobile_agent.mcp_server.protocol import ProtocolError
        with self.assertRaises(ProtocolError):
            self.call(tools, {"devices": ["Studio iPhone"]})


if __name__ == "__main__":
    unittest.main()

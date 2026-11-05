"""The testing tools an agent gets beside run_tests: --locale and --a11y on run_tests, test_history, import_maestro
and test_schedule. Each acts where it should and refuses where it should, with fakes for the suite and the nightly
agent. Offline."""

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from mobile_agent.mcp_server.tools import ToolError
from mobile_agent.testkit import history
from mobile_agent.testkit.mcp import TestTools

FLOW = "appId: com.example.app\n---\n- launchApp\n- tapOn: \"Sign in\"\n- assertVisible: \"Welcome\"\n"
EXPERIMENTAL = "appId: com.example.app\n---\n- runScript: x.js\n- assertVisible: \"Welcome\"\n"


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".mobster" / "checks").mkdir(parents=True)
        (self.root / ".mobster" / "runs").mkdir()
        self.toolset = Namespace(runs_dir=str(self.root / ".mobster" / "runs"), keyless=True, _key=None)
        self.addCleanup(self._tmp.cleanup)

    def tools(self, seen=None):
        def plan(options):
            if seen is not None:
                seen.append(options)
            return Namespace(options=options)

        def execute(planned, **kwargs):
            doc = {"summary": {"passed": 1, "total": 1}, "exitCode": 0, "checks": []}
            return doc, {"html": "h", "junit": "j", "results": "r"}
        return TestTools(plan=plan, execute=execute)


class DefinitionTests(Case):
    def test_the_new_tools_have_closed_schemas_and_the_right_annotations(self):
        tools = TestTools()
        by_name = {item["name"]: item for item in tools.definitions(None)}
        for name in ("test_history", "import_maestro", "test_schedule"):
            self.assertIs(by_name[name]["inputSchema"]["additionalProperties"], False, name)
        self.assertEqual(by_name["import_maestro"]["inputSchema"]["required"], ["flows"])
        self.assertTrue({"locales", "a11y"} <= by_name["run_tests"]["inputSchema"]["properties"].keys())
        notes = tools.annotations()
        self.assertTrue(notes["test_history"]["readOnlyHint"])
        self.assertFalse(notes["import_maestro"]["readOnlyHint"])
        self.assertFalse(notes["import_maestro"]["openWorldHint"])  # it writes the project's own checks only

    def test_none_of_them_needs_a_plan_because_none_reaches_a_phone(self):
        from mobile_agent.mcp_server.catalog import SIMULATOR_PROVIDERS
        self.assertIn(TestTools.name, SIMULATOR_PROVIDERS)


class RunTestsTests(Case):
    def test_locales_and_a11y_reach_the_suite(self):
        seen = []
        tools = self.tools(seen)
        result = tools.run_tests({"path": str(self.root / ".mobster" / "checks"), "locales": ["de", "ja_JP"],
                                  "a11y": "strict"}, None, self.toolset)
        self.assertEqual(result.structured["status"], "finished")
        self.assertEqual((seen[0].locales, seen[0].a11y), (("de", "ja_JP"), "strict"))

    def test_a_bad_language_and_locales_with_pr_are_refused(self):
        tools = self.tools()
        with self.assertRaisesRegex(ToolError, "such as de"):
            tools.run_tests({"path": str(self.root / ".mobster" / "checks"), "locales": ["klingon!"]}, None,
                            self.toolset)
        with self.assertRaisesRegex(ToolError, "go without pr"):
            tools.run_tests({"pr": True, "locales": ["de"]}, None, self.toolset)


class HistoryTests(Case):
    def record(self):
        def cell(status):
            return {"status": status, "deviceKey": "d", "device": "iPhone 17 Pro", "attempts": 1, "durationMs": 10,
                    "flaky": False}
        for suite, a, b in (("s1", "passed", "failed"), ("s2", "passed", "passed")):
            history.record(self.root, {"suite": suite, "startedAt": 1_000_000 + len(suite) * 1000 + int(suite[1]),
                                       "checks": [{"name": "Home", "file": "home.yaml", "results": [cell(a)]},
                                                  {"name": "Checkout", "file": "checkout.yaml",
                                                   "results": [cell(b)]}]})

    def test_it_reads_each_checks_history_and_filters(self):
        self.record()
        result = self.tools().test_history({"path": str(self.root)}, self.toolset)
        self.assertEqual(result.structured["total"], 2)
        only = self.tools().test_history({"path": str(self.root), "check": "check"}, self.toolset)
        self.assertEqual([row["name"] for row in only.structured["checks"]], ["Checkout"])
        none = self.tools().test_history({"path": str(self.root), "check": "nothing"}, self.toolset)
        self.assertEqual(none.structured["checks"], [])
        self.assertTrue(only.structured["ok"])

    def test_an_empty_project_has_no_history_and_a_relative_path_is_refused(self):
        self.assertEqual(self.tools().test_history({}, self.toolset).structured["total"], 0)
        with self.assertRaisesRegex(ToolError, "path must be absolute"):
            self.tools().test_history({"path": "relative"}, self.toolset)
        with self.assertRaisesRegex(ToolError, "absolute path of the project"):
            self.tools().test_history({"path": str(self.root / "missing")}, self.toolset)


class MaestroTests(Case):
    def flow(self, text=FLOW, name="login.yaml"):
        path = self.root / "flows" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        return str(path)

    def test_a_flow_becomes_a_check_and_a_dry_run_writes_nothing(self):
        flow = self.flow()
        dry = self.tools().import_maestro({"flows": [flow], "path": str(self.root), "dry_run": True}, self.toolset)
        self.assertTrue(dry.structured["dry_run"])
        self.assertEqual(list((self.root / ".mobster" / "checks").iterdir()), [])
        done = self.tools().import_maestro({"flows": [flow], "path": str(self.root)}, self.toolset)
        self.assertEqual(done.structured["written"], 1)
        written = list((self.root / ".mobster" / "checks").glob("*.yaml"))
        self.assertEqual(len(written), 1)
        self.assertIn("com.example.app", written[0].read_text())

    def test_a_check_that_exists_is_kept_unless_force(self):
        flow = self.flow()
        self.tools().import_maestro({"flows": [flow], "path": str(self.root)}, self.toolset)
        target = next((self.root / ".mobster" / "checks").glob("*.yaml"))
        target.write_text("edited by hand\n")
        kept = self.tools().import_maestro({"flows": [flow], "path": str(self.root)}, self.toolset)
        self.assertEqual((target.read_text(), kept.structured["written"]), ("edited by hand\n", 0))
        forced = self.tools().import_maestro({"flows": [flow], "path": str(self.root), "force": True}, self.toolset)
        self.assertEqual(forced.structured["written"], 1)
        self.assertIn("com.example.app", target.read_text())

    def test_it_refuses_relative_paths_bad_bundles_a_bad_app_and_missing_flows(self):
        tools = self.tools()
        for args, message in (({"flows": ["flows/x.yaml"]}, "absolute"),
                              ({"flows": [self.flow()], "bundle": "no spaces!"}, "bundle ID"),
                              ({"flows": [self.flow()], "app": "relative.app"}, "simulator build"),
                              ({"flows": [str(self.root / "nope")]}, "no .yaml|not found|No such|doesn't exist|found")):
            with self.assertRaisesRegex(ToolError, message):
                tools.import_maestro({**args, "path": str(self.root)}, self.toolset)
        self.assertEqual(list((self.root / ".mobster" / "checks").iterdir()), [])


class ScheduleTests(Case):
    def state(self, on=True, flags=()):
        return {"on": on, "at": "02:00" if on else None, "flags": list(flags),
                "last": {"date": "2026-10-17", "headline": "3 passed"}}

    def call(self, args, state, seen=None):
        with mock.patch("mobile_agent.devkit.nightly.status", return_value=state):
            return self.tools(seen).test_schedule({"path": str(self.root), **args}, None, self.toolset)

    def test_list_says_when_it_runs_and_how_last_night_went(self):
        result = self.call({}, self.state(flags=["--sim", "iPhone 17 Pro"]))
        self.assertIn("02:00", result.text)
        self.assertIn("3 passed", result.text)
        self.assertTrue(self.call({}, self.state(on=False)).text.startswith("No nightly run"))

    def test_run_runs_the_saved_flags_on_simulators(self):
        seen = []
        result = self.call({"action": "run"}, self.state(flags=["--sim", "iPhone 17 Pro", "--tag", "smoke",
                                                                 "--parallel", "2", "--retries", "0"]), seen)
        self.assertEqual(result.structured["status"], "finished")
        options = seen[0]
        self.assertEqual((options.sims, options.tags, options.parallel, options.retries),
                         (("iPhone 17 Pro",), ("smoke",), 2, 0))
        self.assertEqual(options.devices, ())

    def test_run_refuses_a_schedule_that_is_off_names_an_iphone_or_has_a_flag_it_cant_run(self):
        for state, message in ((self.state(on=False), "No nightly run"),
                               (self.state(flags=["--device", "all"]), "names an iPhone"),
                               (self.state(flags=["--testflight", "com.example.app"]), "names an iPhone"),
                               (self.state(flags=["--video"]), "this tool doesn't run")):
            seen = []
            with self.assertRaisesRegex(ToolError, message):
                self.call({"action": "run"}, state, seen)
            self.assertEqual(seen, [])

    def test_suite_id_goes_with_run(self):
        with self.assertRaisesRegex(ToolError, "goes with run"):
            self.call({"suite_id": "20261018-120000-abcd"}, self.state())


if __name__ == "__main__":
    unittest.main()

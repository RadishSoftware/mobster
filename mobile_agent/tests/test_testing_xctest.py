"""`mobster test --xctest`: parsing a recorded .xcresult, selecting targets, and merging the tests into the suite's
reports, history, PR comment and Slack message. No simulator, Xcode or network runs here."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from mobile_agent.sim.models import SimError, SimTarget
from mobile_agent.testkit import history, notify, pr_report, reports, xctest
from mobile_agent.testkit.api import Options, Planned
from mobile_agent.testkit.targets import Target

FIXTURES = Path(__file__).parent / "fixtures" / "xctest"
DEVICE = {"name": "Mobster · iPhone 17 Pro · iOS 26.4", "key": "sim:iPhone 17 Pro@iOS 26.4"}


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def parsed(name="firefox-failed-tests.json"):
    return xctest.parse(fixture(name), None, DEVICE)


def suite_doc(name="firefox-failed-tests.json", checks=True):
    """A finished suite whose XCTest part is the recorded run, with one passing check beside it."""
    targets = parsed(name)
    doc = {"schema": "mobster.test/1", "suite": "20261024-160000-abcd", "startedAt": 1792800000000,
           "durationMs": 9000, "exitCode": 0, "options": {"repeat": 1, "xctest": True},
           "command": "mobster test --xctest",
           "devices": [{"key": DEVICE["key"], "name": DEVICE["name"], "kind": "simulator"}],
           "checks": [], "summary": {"passed": 0, "failed": 0, "needsReview": 0, "couldntRun": 0, "flaky": 0,
                                     "quarantined": 0, "total": 0, "costUsd": 0}, "mobster": {"version": "0.0"}}
    if checks:
        doc["checks"] = [{"name": "Today shows the streak", "file": ".mobster/checks/today.yaml", "classname": "checks",
                          "tags": [], "quarantined": False, "results": [{
                              "device": DEVICE["name"], "deviceKey": DEVICE["key"], "status": "passed", "attempts": 1,
                              "flaky": False, "durationMs": 4000, "healed": [], "quarantined": False, "runs": []}]}]
        doc["summary"].update(passed=1, total=1)
    doc["xctest"] = xctest.section(targets, scheme="Firefox", test_plan=None, device=DEVICE,
                                   bundle="xctest/tests.xcresult", why={"FirefoxTests": "no test file changed, so "
                                                                                          "every target runs"})
    return xctest.merge_summary(doc)


class ParseTests(unittest.TestCase):
    def test_a_recorded_run_becomes_one_target_with_its_cases(self):
        targets = parsed("firefox-passed-tests.json")
        self.assertEqual([target["name"] for target in targets], ["FirefoxTests"])
        target = targets[0]
        self.assertEqual((target["status"], target["failed"], target["skipped"]), ("passed", 0, 0))
        self.assertEqual(target["tests"], len(target["cases"]))
        names = {(case["classname"], case["name"]) for case in target["cases"]}
        self.assertIn(("PlanTests", "testPaywallOffersThreePlans"), names)
        self.assertTrue(all(case["deviceKey"] == DEVICE["key"] for case in target["cases"]))
        self.assertTrue(any(case["classname"] == "Price lines" or "priceLine" in case["name"]
                            for case in target["cases"]), "the Swift Testing test is read too")

    def test_a_failed_test_carries_its_message_file_and_line_and_sorts_first(self):
        target = parsed()[0]
        self.assertEqual(target["status"], "failed")
        failed = target["cases"][0]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["failures"][0]["file"], "PlanTests.swift")
        self.assertIsInstance(failed["failures"][0]["line"], int)
        self.assertIn("PlanTests.swift:", xctest.failure_line(failed))
        self.assertEqual([case["status"] for case in target["cases"]].count("failed"), target["failed"])

    def test_a_system_failure_without_a_file_still_fails_with_its_text(self):
        tests = fixture("runner-failed-tests.json")
        target = xctest.parse(tests, None, DEVICE)[0]
        case = target["cases"][0]
        self.assertEqual(case["status"], "failed")
        self.assertIsNone(case["failures"][0]["file"])
        self.assertIn("Timed out while loading Accessibility", xctest.failure_line(case))

    def test_durations_read_seconds_or_text_and_a_missing_result_fails_closed(self):
        node = {"nodeType": "Test Case", "name": "testX()", "result": "Mystery", "duration": "1.5s"}
        tests = {"testNodes": [{"nodeType": "Test Plan", "children": [{"nodeType": "Unit test bundle", "name": "T",
                                                                       "children": [{"nodeType": "Test Suite",
                                                                                     "name": "S",
                                                                                     "children": [node]}]}]}]}
        case = xctest.parse(tests, None, DEVICE)[0]["cases"][0]
        self.assertEqual((case["status"], case["durationMs"]), ("failed", 1500))
        self.assertEqual(case["name"], "testX")

    def test_expected_failures_pass_and_skips_are_counted_apart(self):
        def case(name, result):
            return {"nodeType": "Test Case", "name": name, "result": result, "durationInSeconds": 0.01}
        tests = {"testNodes": [{"nodeType": "Test Plan", "children": [
            {"nodeType": "Unit test bundle", "name": "T", "children": [
            {"nodeType": "Test Suite", "name": "S", "children": [
                case("a()", "Passed"), case("b()", "Skipped"), case("c()", "Expected Failure")]}]}]}]}
        target = xctest.parse(tests, None, DEVICE)[0]
        self.assertEqual((target["passed"], target["failed"], target["skipped"]), (2, 0, 1))


class SelectionTests(unittest.TestCase):
    def test_only_testing_wins_then_changed_test_files_then_every_target(self):
        names = ["AppTests", "AppUITests"]
        self.assertEqual(xctest.select_targets(names, ("Sources/A.swift",), ("AppUITests/Login",))[0], ["AppUITests"])
        chosen, why = xctest.select_targets(names, ("AppTests/PlanTests.swift", "Sources/A.swift"))
        self.assertEqual(chosen, ["AppTests"])
        self.assertEqual(why, {"AppTests": "AppTests/PlanTests.swift changed"})
        chosen, why = xctest.select_targets(names, ("Sources/A.swift",))
        self.assertEqual(chosen, names)
        self.assertIn("no test file changed", why["AppTests"])
        self.assertEqual(xctest.select_targets(names, None), (names, {}))

    def test_only_testing_refuses_what_is_not_an_identifier(self):
        self.assertEqual(xctest.only_testing_ok(["T", "T/C", "T/C/testX"]), ("T", "T/C", "T/C/testX"))
        for bad in ("../x", "T/C/t/extra", "-flag", "T;rm", ""):
            with self.subTest(bad=bad), self.assertRaises(xctest.XctestError):
                xctest.only_testing_ok([bad])

    def test_targets_come_from_the_xctestrun_plist(self):
        self.assertEqual(xctest.targets_of(FIXTURES / "Firefox.xctestrun"), ["FirefoxTests"])
        self.assertEqual(xctest.targets_of(FIXTURES / "missing.xctestrun"), [])


class MergeTests(unittest.TestCase):
    def test_a_failed_test_makes_the_exit_code_1_and_a_stopped_run_stays_stopped(self):
        doc = suite_doc()
        self.assertEqual(doc["exitCode"], 1)
        self.assertEqual(doc["summary"]["xctest"]["status"], "failed")
        stopped = suite_doc()
        stopped["exitCode"] = 130
        xctest.merge_summary(stopped)
        self.assertEqual(stopped["exitCode"], 130)

    def test_the_worst_outcome_decides(self):
        for before, own, expected in ((0, "passed", 0), (2, "passed", 2), (2, "failed", 1), (1, "couldnt_run", 1),
                                      (0, "couldnt_run", 3), (2, "couldnt_run", 3)):
            with self.subTest(before=before, own=own):
                doc = {"exitCode": before, "summary": {}, "xctest": {"status": own, "summary": {}}}
                self.assertEqual(xctest.merge_summary(doc)["exitCode"], expected)

    def test_junit_has_a_testsuite_per_target_in_the_same_file(self):
        root = ET.fromstring(reports.junit(suite_doc(), report_base="r"))
        suites = {suite.get("name"): suite for suite in root.findall("testsuite")}
        self.assertEqual(set(suites), {DEVICE["name"], "FirefoxTests"})
        unit = suites["FirefoxTests"]
        cases = unit.findall("testcase")
        self.assertEqual(int(unit.get("tests")), len(cases))
        failure = unit.find("testcase/failure")
        self.assertEqual(failure.get("type"), "xctest")
        self.assertRegex(failure.get("message"), r"^PlanTests\.swift:\d+: ")
        self.assertEqual(int(root.get("tests")), 1 + len(cases))
        self.assertEqual(int(root.get("failures")), int(unit.get("failures")))

    def test_junit_says_when_the_tests_could_not_run(self):
        doc = suite_doc(checks=False)
        doc["xctest"] = xctest.couldnt_run(xctest.XctestError("No simulator was free.", "Try again."))
        root = ET.fromstring(reports.junit(doc))
        error = root.find("testsuite/testcase/error")
        self.assertEqual(error.get("type"), "couldnt_run")
        self.assertEqual(root.get("errors"), "1")

    def test_the_html_has_an_xctest_section_with_failures_first_and_escapes_text(self):
        doc = suite_doc()
        doc["xctest"]["targets"][0]["cases"][0]["failures"][0]["message"] = "<script>alert(1)</script>"
        with tempfile.TemporaryDirectory() as folder:
            page = reports.render_html(doc, folder)
        self.assertIn('<h2 id="xctest">XCTest', page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("Ran because no test file changed", page)
        self.assertIn('<a href="#xctest">FirefoxTests: PlanTests.', page)  # under the heading, with the checks
        self.assertLess(page.index('<td class="dev"><span class="outcomes fail">'),
                        page.index('<td class="dev"><span class="outcomes pass">'))

    def test_the_html_names_the_reason_when_the_tests_could_not_run(self):
        doc = suite_doc(checks=False)
        doc["xctest"] = xctest.couldnt_run(xctest.XctestError("The scheme ran no tests.", "Check --only-testing."))
        with tempfile.TemporaryDirectory() as folder:
            page = reports.render_html(doc, folder)
        self.assertIn("The scheme ran no tests. Check --only-testing.", page)
        self.assertIn("XCTest couldn&#x27;t run", page.replace("&#39;", "&#x27;"))

    def test_slack_lists_a_failed_test_beside_the_checks(self):
        summary = notify.summarize(suite_doc())
        self.assertFalse(summary["green"])
        self.assertEqual(summary["tests"], 1)
        row = summary["unpassed"][0]
        self.assertTrue(row["check"].startswith("FirefoxTests: PlanTests."))
        self.assertIn("PlanTests.swift:", row["found"])
        message = notify.report(summary, project="Firefox")
        self.assertIn("1 check or test didn't pass", message["title"])
        passing = suite_doc("firefox-passed-tests.json")
        self.assertTrue(notify.summarize(passing)["green"])

    def test_the_pr_comment_has_a_line_per_failed_test_with_its_file_and_line(self):
        doc = suite_doc()
        pr = pr_report.document({"screens": []}, doc, folder=Path("."), frames={}, gifs={})
        self.assertIn("XCTest 1 failed of", pr["headline"])
        self.assertEqual(pr["xctest"]["failures"][0]["file"], "PlanTests.swift")
        text = pr_report.comment(pr)
        self.assertIn("**XCTest 1 failed of", text)
        self.assertIn("- Ran `FirefoxTests` (", text)
        self.assertRegex(text, r"- `PlanTests\.[A-Za-z]+` failed: PlanTests\.swift:\d+: ")
        self.assertIn("no test file changed", text)

    def test_the_pr_headline_with_no_check_still_names_the_tests(self):
        doc = suite_doc("firefox-passed-tests.json", checks=False)
        self.assertEqual(pr_report.headline(doc), "Mobster checks: no saved check covers this PR, "
                                                   + xctest.words(doc["xctest"]))
        self.assertTrue(pr_report.comment(pr_report.document({}, doc, folder=Path("."), frames={}, gifs={}))
                        .count("XCTest") >= 1)

    def test_the_pr_comment_says_when_the_tests_could_not_run(self):
        doc = suite_doc(checks=False)
        doc["xctest"] = xctest.couldnt_run(xctest.XctestError("No simulator was free.", "Try again."))
        text = pr_report.comment(pr_report.document({}, doc, folder=Path("."), frames={}, gifs={}))
        self.assertIn("**XCTest couldn't run**", text)
        self.assertIn("No simulator was free. Try again.", text)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def record(self, doc, suite):
        doc = copy.deepcopy(doc)
        doc["suite"] = suite
        return history.record(self.root, doc)

    def test_a_test_gets_a_flake_score_like_a_check(self):
        failed, passed = suite_doc(), suite_doc("firefox-passed-tests.json")
        for number in range(7):
            self.record(failed if number % 2 else passed, f"20261024-16000{number}-abcd")
        entries = {entry["file"]: entry for entry in history.report(self.root)["checks"]}
        tests = [entry for file, entry in entries.items() if file.startswith("xctest:FirefoxTests/PlanTests/")]
        self.assertTrue(tests)
        flaky = [entry for entry in tests if entry["state"] == "flaky"]
        self.assertTrue(flaky, "a test that flips between runs is flaky")
        self.assertGreater(flaky[0]["flakeScore"], 0.15)

    def test_a_flaky_test_is_shown_and_never_suggested_for_the_quarantine_file(self):
        failed, passed = suite_doc(), suite_doc("firefox-passed-tests.json")
        for number in range(5):
            self.record(failed if number % 2 else passed, f"20261024-16000{number}-abcd")
        doc = self.record(passed, "20261024-160009-abcd")
        self.assertEqual(doc["history"]["suggestQuarantine"], [])
        case = next(case for case in doc["xctest"]["targets"][0]["cases"]
                    if case["name"] == "testAnnualPlanIsBilledYearly")
        self.assertEqual(case["history"]["state"], "flaky")

    def test_the_same_suite_is_recorded_once_and_tests_never_push_checks_out(self):
        doc = suite_doc()
        self.record(doc, "20261024-160000-abcd")
        self.record(doc, "20261024-160000-abcd")
        entries = history.load(self.root)[0]
        self.assertTrue(all(len(entry["runs"]) == 1 for entry in entries.values()))
        crowd = {f"xctest:T/C/t{n}|k": {"name": f"C.t{n}", "file": f"xctest:T/C/t{n}", "device": "d",
                                        "runs": [{"suite": "s", "at": n, "status": "passed", "flaky": False, "ms": 1,
                                                  "attempts": 1}]} for n in range(history.MAX_TESTS + 5)}
        crowd["check|k"] = {"name": "A check", "file": "check.yaml", "device": "d",
                            "runs": [{"suite": "s", "at": 0, "status": "passed", "flaky": False, "ms": 1,
                                      "attempts": 1}]}
        history._save(self.root, crowd)
        saved = history.load(self.root)[0]
        self.assertIn("check|k", saved)
        self.assertEqual(sum(key.startswith("xctest:") for key in saved), history.MAX_TESTS)


class FakeSimulators:
    def __init__(self, error=None):
        self.error, self.released, self.asked = error, 0, []
        outer = self

        class Lease:
            target = SimTarget(udid="SIM-1", name=DEVICE["name"], device_type="iPhone 17 Pro", runtime="iOS 26.4",
                               wda_url="", mjpeg_url="", xctestrun="")

            def release(self):
                outer.released += 1
        self.lease = Lease()

    def acquire(self, device_type, runtime):
        self.asked.append((device_type, runtime))
        if self.error:
            raise self.error
        return self.lease


class FakeAttempts:
    def __init__(self, simulators):
        self._simulators = simulators

    def simulators(self):
        return self._simulators


class Runner:
    """A stand-in for xcodebuild and xcresulttool that answers from the recorded files."""

    def __init__(self, tests="firefox-failed-tests.json", code=65, bundle=True):
        self.calls, self.tests, self.code, self.bundle = [], tests, code, bundle

    def __call__(self, argv, cwd, log=None, timeout=None):
        self.calls.append(list(argv))
        if argv[0] == "xcodebuild":
            path = Path(argv[argv.index("-resultBundlePath") + 1])
            if self.bundle:
                path.mkdir(parents=True, exist_ok=True)
                (path / "Info.plist").write_text("<plist/>")
            return self.code, "** TEST FAILED **" if self.code else "** TEST SUCCEEDED **"
        if argv[:3] == ["xcrun", "xcresulttool", "export"]:
            return 0, ""
        which = argv[4]
        return 0, json.dumps(fixture(self.tests) if which == "tests" else fixture("firefox-summary.json"))


def planned(**options):
    return Planned(root=Path("."), entries=[], targets=[Target("default", "default")],
                   options=Options(xctest=True, **options),
                   xctest_build={"scheme": "Firefox", "xctestrun": "/x/Firefox.xctestrun",
                                 "targets": ["FirefoxTests"], "path": "/x/Firefox.app"})


class RunSuiteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def run_suite(self, plan=None, sims=None, runner=None):
        sims = sims or FakeSimulators()
        found = xctest.run_suite(plan or planned(), FakeAttempts(sims), self.folder, lambda line: None,
                                 runner=runner or Runner())
        return found, sims

    def test_it_runs_the_built_tests_on_the_leased_simulator_and_reads_the_bundle(self):
        runner = Runner()
        found, sims = self.run_suite(runner=runner)
        run = runner.calls[0]
        self.assertEqual(run[:2], ["xcodebuild", "test-without-building"])
        self.assertIn("id=SIM-1", run)
        self.assertEqual(run[run.index("-xctestrun") + 1], "/x/Firefox.xctestrun")
        self.assertEqual((found["status"], found["summary"]["failed"]), ("failed", 1))
        self.assertEqual(found["resultBundle"], "xctest/tests.xcresult")
        self.assertEqual(found["deviceKey"], "sim:iPhone 17 Pro@iOS 26.4")
        self.assertEqual(sims.released, 1)
        self.assertEqual(sims.asked, [(None, None)])

    def test_only_testing_reaches_xcodebuild_and_names_the_target(self):
        runner = Runner()
        found, _ = self.run_suite(planned(xctest_only=("FirefoxTests/PlanTests/testNamedFallsBackToAnnual",)),
                                  runner=runner)
        self.assertIn("-only-testing:FirefoxTests/PlanTests/testNamedFallsBackToAnnual", runner.calls[0])
        self.assertEqual(found["why"], {"FirefoxTests": "named with --only-testing"})

    def test_changed_files_choose_the_target_and_the_comment_says_why(self):
        plan = planned()
        plan.xctest_build["targets"] = ["FirefoxTests", "FirefoxUITests"]
        plan.changed = ("FirefoxTests/PlanTests.swift",)
        runner = Runner()
        found, _ = self.run_suite(plan, runner=runner)
        self.assertIn("-only-testing:FirefoxTests", runner.calls[0])
        self.assertEqual(found["why"], {"FirefoxTests": "FirefoxTests/PlanTests.swift changed"})

    def test_no_free_simulator_says_so_and_releases_nothing(self):
        found, sims = self.run_suite(sims=FakeSimulators(SimError("busy", "All 2 are running checks.", "Wait.")))
        self.assertEqual(found["status"], "couldnt_run")
        self.assertIn("All 2 are running checks.", found["reason"]["message"])
        self.assertEqual(found["reason"]["fix"], "Wait.")
        self.assertEqual(sims.released, 0)

    def test_a_run_of_iphones_only_has_no_simulator_for_the_tests(self):
        plan = planned()
        plan.targets = [Target("device:abc", "device", record={"id": "abc", "name": "Studio iPhone", "kind": "usb"})]
        found, _ = self.run_suite(plan)
        self.assertEqual(found["status"], "couldnt_run")
        self.assertIn("only iPhones", found["reason"]["message"])

    def test_xcodebuild_that_never_ran_the_tests_is_couldnt_run_and_the_lease_is_released(self):
        found, sims = self.run_suite(runner=Runner(code=70, bundle=False))
        self.assertEqual(found["status"], "couldnt_run")
        self.assertIn("The tests didn't run", found["reason"]["message"])
        self.assertEqual(sims.released, 1)

    def test_a_scheme_that_ran_no_tests_is_couldnt_run_not_passed(self):
        runner = Runner()
        runner.tests = "empty-tests.json"
        found, _ = self.run_suite(runner=runner)
        self.assertEqual(found["status"], "couldnt_run")
        self.assertIn("ran no tests", found["reason"]["message"])

    def test_a_target_named_by_only_testing_that_does_not_exist_is_couldnt_run(self):
        found, sims = self.run_suite(planned(xctest_only=("Nope/Class",)))
        self.assertEqual(found["status"], "couldnt_run")
        self.assertEqual(sims.asked, [])


class ExecuteTests(unittest.TestCase):
    def test_a_suite_with_xctest_writes_the_one_report_the_one_junit_the_history_and_exits_1(self):
        from unittest import mock

        from mobile_agent.testkit import api
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            plan = planned()
            plan.root = root
            with mock.patch.object(xctest, "run_command", Runner()):
                doc, paths = api.execute(plan, attempts=FakeAttempts(FakeSimulators()), cwd=root,
                                         gate=mock.MagicMock())
            suite = Path(paths["folder"])
            self.assertEqual(doc["exitCode"], 1)
            written = json.loads((suite / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(written["xctest"]["summary"]["failed"], 1)
            self.assertEqual(written["summary"]["xctest"]["status"], "failed")
            self.assertEqual(ET.parse(suite / "junit.xml").getroot().find("testsuite").get("name"), "FirefoxTests")
            self.assertIn("PlanTests.swift:", (suite / "index.html").read_text(encoding="utf-8"))
            self.assertTrue((suite / "xctest" / "xctest.log").parent.is_dir())
            kept = history.load(root)[0]
            self.assertTrue(any(key.startswith("xctest:FirefoxTests/PlanTests/") for key in kept))

    def test_ctrl_c_during_the_tests_still_writes_the_checks_reports_and_exits_130(self):
        from unittest import mock

        from mobile_agent.testkit import api
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            plan = planned()
            plan.root = root
            with mock.patch.object(xctest, "run_suite", side_effect=KeyboardInterrupt()):
                doc, paths = api.execute(plan, attempts=FakeAttempts(FakeSimulators()), cwd=root,
                                         gate=mock.MagicMock())
            self.assertEqual((doc["exitCode"], doc["stopped"]), (130, True))
            self.assertNotIn("xctest", doc)
            self.assertTrue((Path(paths["folder"]) / "results.json").is_file())

    def test_xctest_off_leaves_the_suite_as_it_was(self):
        from unittest import mock

        from mobile_agent.testkit import api
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            plan = planned()
            plan.root, plan.options = root, Options()
            doc, _ = api.execute(plan, attempts=FakeAttempts(FakeSimulators()), cwd=root, gate=mock.MagicMock())
        self.assertNotIn("xctest", doc)
        self.assertEqual(doc["exitCode"], 0)


class ExportTests(unittest.TestCase):
    def test_attachments_land_on_their_cases_and_a_path_outside_the_folder_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            out = folder / "xctest" / "attachments"
            out.mkdir(parents=True)
            (out / "shot.png").write_bytes(b"png")
            (out / "manifest.json").write_text(json.dumps([{"testIdentifier": "PlanTests/testX", "attachments": [
                {"exportedFileName": "shot.png",
                 "suggestedHumanReadableName": "Screenshot_0_7F32FC8B-5441-455F-81BC-C51B08311E07.png"},
                {"exportedFileName": "../escape.png", "suggestedHumanReadableName": "No"}]}]))
            targets = [{"cases": [{"id": "PlanTests/testX", "attachments": []}]}]
            count = xctest.export_attachments(folder / "tests.xcresult", folder, targets, runner=lambda *a: (0, ""))
        self.assertEqual(count, 1)
        self.assertEqual(targets[0]["cases"][0]["attachments"], [{"name": "Screenshot.png",
                                                                  "path": "xctest/attachments/shot.png"}])


if __name__ == "__main__":
    unittest.main()

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent.testkit import a11y_baseline, api, cli, reports
from mobile_agent.tests.test_testing import validate_junit
from mobile_agent.tests.test_verify import BAD, CLEAN, ids, tree
from mobile_agent.tests.test_verify_fixtures import node
from mobile_agent.tests.test_verify_runner import BUNDLE, Driver, Manager, RunnerCase
from mobile_agent.verify import a11y, runner
from mobile_agent.verify.checks import CheckError, check_from_dict
from mobile_agent.verify.runner import VerifyRun
from mobile_agent.verify.tree import parse_tree


class RuleTests(unittest.TestCase):
    def test_duplicate_labels_on_one_screen_are_found_once_per_label(self):
        found = a11y.audit_tree(tree(node("Button", "Delete", rect=(10, 100, 60, 48)),
                                     node("Button", "delete ", rect=(10, 200, 60, 48)),
                                     node("Button", "Delete", rect=(10, 300, 60, 48)),
                                     node("Button", "Edit", rect=(10, 400, 60, 48)),
                                     node("StaticText", "Total", rect=(10, 500, 60, 20)),
                                     node("StaticText", "Total", rect=(10, 540, 60, 20))))
        self.assertEqual(ids(found), [("duplicate-label", "Delete")])
        self.assertIn("3 buttons on this screen are all labelled", found[0]["message"])

    def test_a_label_that_says_nothing_is_found_on_controls_and_images_only(self):
        found = a11y.audit_tree(tree(node("Button", "button", rect=(10, 100, 60, 48)),
                                     node("Image", "icon_close.png", rect=(10, 200, 24, 24)),
                                     node("Button", "ic_close", rect=(10, 300, 60, 48)),
                                     node("Image", "Photo of a lighthouse", rect=(10, 400, 80, 80)),
                                     node("Button", "Close", rect=(10, 500, 60, 48)),
                                     node("StaticText", "image", rect=(10, 600, 60, 20)),
                                     node("TextField", "item", placeholder="Name", rect=(10, 700, 200, 48))))
        self.assertEqual(ids(found), [("label-unhelpful", "button"), ("label-unhelpful", "ic_close"),
                                      ("label-unhelpful", "icon_close.png")])

    def test_a_control_wholly_off_screen_outside_a_scroll_view_is_unreachable(self):
        found = a11y.audit_tree(tree(
            node("Button", "Pay", rect=(10, 900, 60, 48)),
            node("Button", "Back", rect=(-80, 100, 60, 48)),
            node("Button", "Visible", rect=(10, 100, 60, 48)),
            node("Button", "Off but disabled", rect=(10, 1000, 60, 48), enabled=False),
            node("ScrollView", children=[node("Button", "Further down", rect=(10, 1200, 60, 48))],
                 rect=(0, 0, 402, 874))))
        self.assertEqual(ids(found), [("unreachable-element", "Back"), ("unreachable-element", "Pay")])

    def test_a_screen_with_no_findings_has_none(self):
        self.assertEqual(a11y.audit_tree(parse_tree(CLEAN)), [])


class LevelTests(unittest.TestCase):
    def test_collector_rechecks_exact_sizes_placeholders_and_offscreen_controls(self):
        collector = a11y.Collector()
        self.assertTrue(collector.add(tree(node("Button", "Continue", rect=(10, 100, 44.1, 44)),
                                            node("TextField", None, placeholder="Email", rect=(10, 160, 100, 44)),
                                            node("Button", "Hidden", rect=(10, 900, 60, 44), enabled=False))))
        self.assertTrue(collector.add(tree(node("Button", "Continue", rect=(10, 100, 43.9, 44)),
                                            node("TextField", None, rect=(10, 160, 100, 44)),
                                            node("Button", "Hidden", rect=(10, 900, 60, 44)))))
        self.assertEqual({item["id"] for item in collector.findings()},
                         {"target-too-small", "control-no-label", "unreachable-element"})
        self.assertEqual(collector.screens, 2)

    def test_collector_reports_unique_screen_cap_without_consuming_duplicates(self):
        collector = a11y.Collector()
        for index in range(collector.LIMIT):
            self.assertTrue(collector.add(tree(node("Button", f"Item {index}", rect=(10, 100, 60, 44)))))
        self.assertTrue(collector.add(tree(node("Button", "Item 0", rect=(10, 100, 60, 44)))))
        self.assertFalse(collector.add(tree(node("Button", "Item 61", rect=(10, 100, 60, 44)))))
        self.assertEqual(collector.screens, collector.LIMIT)

    def collect(self, **kwargs):
        collector = a11y.Collector(**kwargs)
        collector.add(parse_tree(BAD), "frames/01.jpg")
        return collector.findings()

    def test_warn_findings_never_fail_and_strict_ones_do(self):
        self.assertEqual({item["level"] for item in self.collect(level="warn")}, {"warn"})
        self.assertEqual({item["level"] for item in self.collect(level="strict")}, {"fail"})

    def test_a_finding_in_the_baseline_is_known_and_a_new_one_is_not(self):
        everything = self.collect(level="strict", scope="checks/a.yaml")
        known = {item["key"] for item in everything if item["id"] == "image-no-label"}
        levels = {item["id"]: item["level"] for item in self.collect(level="strict", scope="checks/a.yaml",
                                                                    baseline=frozenset(known))}
        self.assertEqual(levels["image-no-label"], "known")
        self.assertEqual(levels["target-too-small"], "fail")
        again = self.collect(level="strict", scope="checks/other.yaml", baseline=frozenset(known))
        self.assertNotIn("known", {item["level"] for item in again}, "a baseline entry belongs to its check's file")

    def test_a_layout_shift_does_not_make_an_old_finding_new(self):
        first = a11y.Collector(scope="a")
        first.add(tree(node("Button", None, id="x", rect=(10, 10, 30, 30))))
        shifted = a11y.Collector(scope="a", baseline=frozenset(item["key"] for item in first.findings()))
        shifted.add(tree(node("Button", None, id="x", rect=(12, 40, 30, 30))))
        self.assertEqual({item["level"] for item in shifted.findings()}, {"known"})

    def test_label_delimiters_do_not_hide_a_different_element(self):
        first = a11y.Collector(scope="a")
        first.add(tree(node("Button", "A|B", id="C", rect=(10, 10, 30, 30))))
        second = a11y.Collector(scope="a", baseline=frozenset(item["key"] for item in first.findings()))
        second.add(tree(node("Button", "A", id="B|C", rect=(10, 10, 30, 30))))
        self.assertEqual({item["level"] for item in second.findings()}, {"fail"})


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-a11y-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / ".mobster").mkdir()

    def doc(self, levels=("fail", "warn", "known")):
        found = a11y.Collector(level="strict", scope="checks/a.yaml")
        found.add(parse_tree(BAD))
        items = found.findings()
        for item, level in zip(items, levels):
            item["level"] = level
        return {"checks": [{"file": "checks/a.yaml", "results": [{"findings": items}]}]}

    def test_it_writes_every_finding_and_reads_them_back(self):
        count = a11y_baseline.write(self.root, self.doc())
        self.assertEqual(count, 3)
        data = json.loads(a11y_baseline.path(self.root).read_text())
        self.assertEqual(data["schema"], a11y_baseline.SCHEMA)
        self.assertEqual(a11y_baseline.load(self.root), frozenset(data["findings"]))
        self.assertTrue(all(json.loads(key)[0] == "checks/a.yaml" for key in data["findings"]))

    def test_no_file_is_an_empty_baseline_and_a_wrong_one_is_refused(self):
        self.assertEqual(a11y_baseline.load(self.root), frozenset())
        for text in ("not json", json.dumps({"schema": "other", "findings": []}),
                     json.dumps({"schema": a11y_baseline.SCHEMA, "findings": [3]})):
            with self.subTest(text=text):
                a11y_baseline.path(self.root).write_text(text)
                with self.assertRaises(a11y_baseline.BaselineError) as caught:
                    a11y_baseline.load(self.root)
                self.assertIn("--update-a11y-baseline", str(caught.exception))

    def test_counts_split_new_warnings_and_known(self):
        self.assertEqual(a11y_baseline.counts(self.doc()), {"new": 1, "known": 1, "warnings": 1})
        self.assertEqual(a11y_baseline.counts({"checks": []}), {"new": 0, "known": 0, "warnings": 0})

    def test_plan_gives_each_check_its_scope_baseline_and_level(self):
        (self.root / ".mobster" / "checks").mkdir()
        (self.root / ".mobster" / "checks" / "a.yaml").write_text(
            "app:\n  bundle: com.example.app\nexpect:\n  - text: Account\n")
        (self.root / ".mobster" / "checks" / "b.yaml").write_text(
            "app:\n  bundle: com.example.app\na11y: strict\nexpect:\n  - text: Account\n")
        a11y_baseline.path(self.root).write_text(json.dumps({"schema": a11y_baseline.SCHEMA, "findings": ["k"]}))
        planned = api.plan(api.Options(a11y="warn", sims=("iPhone 17 Pro",)), cwd=self.root)
        levels = {entry.file: (entry.check.a11y, entry.check.a11y_scope, entry.check.a11y_baseline)
                  for entry in planned.entries}
        self.assertEqual(levels[".mobster/checks/a.yaml"][0], "warn")
        self.assertEqual(levels[".mobster/checks/b.yaml"][0], "strict", "a check's own strict stays")
        self.assertEqual(levels[".mobster/checks/a.yaml"][1:], (".mobster/checks/a.yaml", frozenset({"k"})))
        a11y_baseline.path(self.root).write_text("nope")
        with self.assertRaises(api.UsageError):
            api.plan(api.Options(sims=("iPhone 17 Pro",)), cwd=self.root)
        repaired = api.plan(api.Options(a11y="warn", update_a11y_baseline=True, sims=("iPhone 17 Pro",)),
                            cwd=self.root)
        self.assertTrue(all(not entry.check.a11y_baseline for entry in repaired.entries))


class CheckKeyTests(unittest.TestCase):
    def test_the_a11y_key_is_warn_strict_true_or_false(self):
        base = {"app": {"bundle": BUNDLE}, "expect": [{"text": "Account"}]}
        self.assertEqual([check_from_dict({**base, "a11y": value}).a11y for value in ("warn", "strict", True, False)],
                         ["warn", "strict", "warn", None])
        with self.assertRaises(CheckError):
            check_from_dict({**base, "a11y": "loud"})


class RunTests(RunnerCase):
    def run_check(self, screen, **extra):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "expect": [{"text": "Account"}], **extra})
        manager, driver = Manager(), Driver([screen])
        self.watch(driver, manager)
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager,
                        simctl=lambda args: (0, "large" if len(args) == 3 else ""))
        with mock.patch.object(runner, "build_driver", lambda *a: driver):
            run.prepare()
        return run.finish()

    def test_warn_reports_the_findings_and_passes_and_strict_fails(self):
        warned = self.run_check(BAD, a11y="warn")
        self.assertEqual(warned["verdict"], "passed")
        self.assertEqual({item["level"] for item in warned["findings"]}, {"warn"})
        self.assertEqual(self.run_check(BAD, a11y="strict")["verdict"], "failed")
        self.assertEqual(self.run_check(CLEAN, a11y="strict")["findings"], [])

    def test_the_older_audit_key_is_strict(self):
        self.assertEqual(self.run_check(BAD, audit=["a11y"])["verdict"], "failed")
        self.assertEqual(self.run_check(BAD, audit=["a11y"], a11y="warn")["verdict"], "failed")


class ReportTests(unittest.TestCase):
    def doc(self, level):
        finding = {"id": "target-too-small", "audit": "a11y", "guideline": "HIG 44 pt", "level": level,
                   "message": "Button \"Not now\" at 1, 2, 3 x 4 pt: too small", "evidence": "x",
                   "element": {"role": "Button", "label": "Not now", "identifier": "", "path": "/a",
                               "rect": [1, 2, 3, 4]}}
        cell = {"device": "iPhone", "deviceKey": "sim:a", "status": "passed", "attempts": 1, "flaky": False,
                "report": None, "durationMs": 1, "healed": [], "quarantined": False, "reason": None, "frame": None,
                "video": None, "costUsd": 0, "runs": [], "findings": [finding]}
        return {"schema": reports.SCHEMA, "suite": "s", "startedAt": 1_791_380_000_000, "durationMs": 1,
                "exitCode": 0, "options": {}, "devices": [{"key": "sim:a", "name": "iPhone"}],
                "checks": [{"name": "Paywall", "file": "p.yaml", "classname": "checks.p", "tags": [],
                            "quarantined": False, "results": [cell]}], "summary": {"passed": 1, "total": 1}}

    def test_junit_a_warning_passes_a_new_finding_fails_and_a_known_one_is_left_out(self):
        warn = validate_junit(reports.junit(self.doc("warn")))
        cases = warn[0].findall("testcase")
        self.assertEqual((len(cases), warn.get("failures")), (2, "0"))
        self.assertIn("Warning:", cases[1].find("system-out").text)
        fail = validate_junit(reports.junit(self.doc("fail")))
        self.assertEqual((len(fail[0].findall("testcase")), fail.get("failures")), (2, "1"))
        known = validate_junit(reports.junit(self.doc("known")))
        self.assertEqual((len(known[0].findall("testcase")), known.get("failures")), (1, "0"))

    def test_the_summary_and_the_report_count_new_warnings_and_known(self):
        text = cli.summary_text(self.doc("warn"), {})
        self.assertIn("Accessibility: 0 new, 1 warning, 0 in the baseline", text)
        self.assertNotIn("Accessibility", cli.summary_text({**self.doc("warn"), "checks": []}, {}))
        with tempfile.TemporaryDirectory() as folder:
            page = reports.render_html(self.doc("fail"), Path(folder))
        self.assertIn("1 new accessibility finding, 0 warnings, 0 in the baseline", page)


if __name__ == "__main__":
    unittest.main()

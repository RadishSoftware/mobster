"""A baseline replacement needs complete new observations and an unchanged old file."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from mobile_agent.testkit import a11y_baseline, api


class RefreshTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        checks = self.root / ".mobster" / "checks"
        checks.mkdir(parents=True)
        (checks / "home.yaml").write_text(
            "version: 1\nname: Home\napp: {bundle: dev.example.app}\n"
            "expect: [{text: Home}]\n")
        self.baseline = a11y_baseline.path(self.root)

    def run_refresh(self, *, complete=True, screens=1, change_old=None, repeat=1, verdict="passed",
                    failure="app_crashed", stop_after=False, retries=0, a11y="warn", script=()):
        planned = api.plan(api.Options(a11y=a11y, update_a11y_baseline=True,
                                       repeat=repeat, retries=retries), cwd=self.root)
        stop = threading.Event()
        script = iter(script)

        def attempt(check, record, *, label, video_name=None):
            if change_old is not None:
                self.baseline.write_text(change_old)
            if stop_after:
                stop.set()
            step = {"verdict": verdict, "failure": failure, "complete": complete, "key": "new-key",
                    **next(script, {})}
            passed = step["verdict"] == "passed"
            return {"run_id": "audit", "verdict": step["verdict"],
                    "reason": None if passed else {"class": step["failure"], "message": "The app stopped"},
                    "seconds": 0.01,
                    "device": {"type": "iPhone 17 Pro", "runtime": "iOS 26.4"}, "frames": [],
                    "findings": [{"audit": "a11y", "key": step["key"],
                                  "level": "fail" if step["failure"] == "finding" else "warn"}] if step["key"] else [],
                    "screenCoverage": {"complete": step["complete"], "a11yScreens": screens}}

        return api.execute(planned, attempts=attempt, cwd=self.root, stop=stop)[0]

    def test_malformed_old_file_is_replaced_only_after_a_complete_scan(self):
        self.baseline.write_text("broken JSON")
        doc = self.run_refresh()
        self.assertEqual(doc["a11yBaseline"], 1)
        self.assertEqual(json.loads(self.baseline.read_text())["findings"], ["new-key"])

    def test_incomplete_or_absent_scan_preserves_malformed_old_file(self):
        for complete, screens in ((False, 1), (True, 0)):
            with self.subTest(complete=complete, screens=screens):
                self.baseline.write_text("broken JSON")
                doc = self.run_refresh(complete=complete, screens=screens)
                self.assertEqual(doc["exitCode"], 3)
                self.assertNotIn("a11yBaseline", doc)
                self.assertEqual(self.baseline.read_text(), "broken JSON")

    def test_old_file_drift_and_repeated_scans_refuse_replacement(self):
        self.baseline.write_text("old malformed bytes")
        doc = self.run_refresh(change_old="changed by another writer")
        self.assertEqual(doc["exitCode"], 3)
        self.assertEqual(self.baseline.read_text(), "changed by another writer")
        self.baseline.write_text("old malformed bytes")
        doc = self.run_refresh(repeat=2)
        self.assertEqual(doc["exitCode"], 3)
        self.assertEqual(self.baseline.read_text(), "old malformed bytes")

    def test_a_crash_after_an_early_scan_does_not_replace_the_baseline(self):
        self.baseline.write_text("old malformed bytes")
        doc = self.run_refresh(verdict="failed")
        self.assertEqual(doc["exitCode"], 3)
        self.assertEqual(self.baseline.read_text(), "old malformed bytes")

    def test_a_completed_strict_finding_can_become_the_new_baseline(self):
        self.baseline.write_text("old malformed bytes")
        doc = self.run_refresh(verdict="failed", failure="finding")
        self.assertEqual(doc["a11yBaseline"], 1)
        self.assertEqual(json.loads(self.baseline.read_text())["findings"], ["new-key"])

    def test_default_retries_still_write_the_baseline_on_a_strict_finding(self):
        self.baseline.write_text("old malformed bytes")
        doc = self.run_refresh(verdict="failed", failure="finding", retries=None, a11y="strict")
        self.assertEqual((doc["checks"][0]["results"][0]["attempts"], doc["exitCode"], doc["a11yBaseline"]), (2, 1, 1))
        self.assertEqual(json.loads(self.baseline.read_text())["findings"], ["new-key"])

    def test_default_retries_keep_the_baseline_when_the_last_attempt_proves_nothing(self):
        for fields in ({"verdict": "failed", "failure": "finding", "complete": False},
                       {"verdict": "failed", "failure": "app_crashed"}):
            with self.subTest(fields=fields):
                self.baseline.write_text("old malformed bytes")
                doc = self.run_refresh(retries=None, a11y="strict", **fields)
                self.assertEqual(doc["checks"][0]["results"][0]["attempts"], 2)
                self.assertEqual(doc["exitCode"], 3)
                self.assertNotIn("a11yBaseline", doc)
                self.assertEqual(self.baseline.read_text(), "old malformed bytes")

    def test_a_retried_check_that_ends_without_a_complete_scan_keeps_the_baseline(self):
        finding = {"verdict": "failed", "failure": "finding"}
        for last in ({**finding, "complete": False}, {"verdict": "couldnt_run", "failure": "install"},
                     {"verdict": "failed", "failure": "assertion"}, {"verdict": "failed", "failure": "app_crashed"}):
            with self.subTest(last=last):
                self.baseline.write_text("old malformed bytes")
                doc = self.run_refresh(retries=None, a11y="strict", script=(finding, last))
                self.assertEqual(doc["checks"][0]["results"][0]["attempts"], 2)
                self.assertEqual(doc["exitCode"], 3)
                self.assertNotIn("a11yBaseline", doc)
                self.assertEqual(self.baseline.read_text(), "old malformed bytes")

    def test_a_retried_check_writes_only_what_its_last_attempt_found(self):
        finding = {"verdict": "failed", "failure": "finding"}
        for first, last, keys, code in (
                ({**finding, "key": "first"}, {**finding, "key": "second"}, ["second"], 1),
                ({"verdict": "couldnt_run", "failure": "wda"}, finding, ["new-key"], 1),
                ({**finding, "complete": False}, finding, ["new-key"], 1),
                (finding, {"verdict": "passed", "key": None}, [], 0)):
            with self.subTest(first=first, last=last):
                self.baseline.write_text("old malformed bytes")
                doc = self.run_refresh(retries=None, a11y="strict", script=(first, last))
                self.assertEqual(doc["checks"][0]["results"][0]["attempts"], 2)
                self.assertEqual((doc["exitCode"], doc["a11yBaseline"]), (code, len(keys)))
                self.assertEqual(json.loads(self.baseline.read_text())["findings"], keys)

    def test_stop_after_the_only_complete_scan_keeps_exit_130_and_old_baseline(self):
        self.baseline.write_text("old malformed bytes")
        doc = self.run_refresh(stop_after=True)
        self.assertEqual(doc["exitCode"], 130)
        self.assertNotIn("a11yBaseline", doc)
        self.assertEqual(self.baseline.read_text(), "old malformed bytes")


if __name__ == "__main__":
    unittest.main()

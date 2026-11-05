"""Locale-expanded checks keep separate stable identities for comparison."""

import tempfile
import unittest
from pathlib import Path

from mobile_agent.testkit import api, comparison
from mobile_agent.testkit.suite import Suite
from mobile_agent.testkit.targets import DEFAULT


class LocaleIdentityTests(unittest.TestCase):
    def test_one_file_two_locales_has_distinct_stable_case_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            checks = root / ".mobster" / "checks"
            checks.mkdir(parents=True)
            (checks / "home.yaml").write_text(
                "version: 1\nname: Home\napp: {bundle: dev.example.app}\nexpect: [{text: Home}]\n")
            entries = api.plan(api.Options(locales=("de", "ja")), cwd=root).entries

            def attempt(check, record, *, label, video_name=None):
                return {"run_id": label, "verdict": "passed", "seconds": 0.01,
                        "device": {"type": "iPhone 17 Pro", "runtime": "iOS 26.4"}, "frames": []}

            def rows(order):
                suite = Suite(order, [DEFAULT], attempt=attempt, retries=0, cap=1)
                return suite.run()["checks"]

            first = rows(entries)
            second = rows(list(reversed(entries)))
            ids = {row["locale"]: row["caseId"] for row in first}
            self.assertEqual(set(ids), {"de", "ja"})
            self.assertNotEqual(ids["de"], ids["ja"])
            self.assertEqual(ids, {row["locale"]: row["caseId"] for row in second})
            self.assertTrue(all(row["results"][0]["status"] == "passed" for row in first))

    def test_nonlocale_case_keeps_existing_file_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            checks = root / ".mobster" / "checks"
            checks.mkdir(parents=True)
            (checks / "home.yaml").write_text(
                "version: 1\nname: Home\napp: {bundle: dev.example.app}\nexpect: [{text: Home}]\n")
            entry = api.plan(api.Options(), cwd=root).entries[0]
            suite = Suite([entry], [DEFAULT], attempt=lambda *args, **kwargs: {
                "run_id": "one", "verdict": "passed", "device": {}, "frames": []}, retries=0, cap=1)
            self.assertNotIn("caseId", suite.run()["checks"][0])

    def test_saved_locale_suites_compare_without_duplicate_case_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            checks = root / ".mobster" / "checks"
            checks.mkdir(parents=True)
            (checks / "home.yaml").write_text(
                "version: 1\nname: Home\napp: {bundle: dev.example.app}\nexpect: [{text: Home}]\n")

            def attempt(check, record, *, label, video_name=None):
                return {"run_id": label, "verdict": "passed", "seconds": 0.01,
                        "device": {"type": "iPhone 17 Pro", "runtime": "iOS 26.4"}, "frames": []}

            options = api.Options(locales=("de", "ja"), retries=0)
            before, _ = api.execute(api.plan(options, cwd=root), attempts=attempt, cwd=root)
            after, _ = api.execute(api.plan(options, cwd=root), attempts=attempt, cwd=root)
            result = comparison.compare(before, after)
            self.assertEqual(len(result["cases"]), 2)
            self.assertNotIn("duplicate_case_target", result["reasons"])


if __name__ == "__main__":
    unittest.main()

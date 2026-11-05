"""Local JUnit must report the same terminal verdict as the suite document."""

import unittest
from xml.etree import ElementTree as ET

from mobile_agent.testkit import reports


def document():
    return {"suite": "s", "startedAt": 1_792_755_600_000, "durationMs": 1000, "exitCode": 0,
            "devices": [{"key": "d", "name": "Simulator"}],
            "checks": [{"name": "Checkout", "file": "checkout.yaml", "quarantined": False,
                        "results": [{"deviceKey": "d", "device": "Simulator", "status": "passed",
                                     "attempts": 1, "flaky": False, "durationMs": 1000}]}]}


class JunitVerdictTests(unittest.TestCase):
    def test_stopped_after_a_passing_cell_is_not_green(self):
        doc = document()
        doc.update(exitCode=130, stopReason="stopped", stopped=True)
        root = ET.fromstring(reports.junit(doc))
        self.assertEqual((root.get("tests"), root.get("errors"), root.get("failures")), ("2", "1", "0"))
        self.assertEqual(root.find("testsuite[@name='Suite verdict']/testcase/error").get("type"), "suite_verdict")

    def test_quarantined_safety_stop_is_an_error_before_quarantine(self):
        for marker in ("unknown_outcome", "journal", "refused"):
            with self.subTest(marker=marker):
                doc = document()
                doc.update(exitCode=3, stopReason=marker)
                check = doc["checks"][0]
                check["quarantined"] = True
                cell = check["results"][0]
                cell.update(status="couldnt_run", safetyStop=marker,
                            reason={"class": marker, "message": "Review this run before another action."})
                root = ET.fromstring(reports.junit(doc))
                self.assertEqual((root.get("tests"), root.get("errors"), root.get("skipped")), ("1", "1", "0"))
                self.assertEqual(root.find("testsuite/testcase/error").get("type"), "safety_stop")
                self.assertIsNone(root.find("testsuite/testcase/skipped"))

    def test_nonzero_empty_suite_has_a_verdict_case(self):
        doc = document()
        doc.update(exitCode=3, checks=[])
        root = ET.fromstring(reports.junit(doc))
        self.assertEqual((root.get("tests"), root.get("errors")), ("1", "1"))
        self.assertEqual(root.find("testsuite/testcase/error").get("type"), "suite_verdict")

    def test_ordinary_quarantine_failure_and_strict_flake_keep_their_meaning(self):
        doc = document()
        check = doc["checks"][0]
        check["quarantined"] = True
        check["results"][0].update(status="failed", reason={"class": "assertion", "message": "Wrong text."})
        root = ET.fromstring(reports.junit(doc))
        self.assertEqual((root.get("failures"), root.get("errors"), root.get("skipped")), ("0", "0", "1"))
        check["quarantined"] = False
        doc["exitCode"] = 1
        root = ET.fromstring(reports.junit(doc))
        self.assertEqual((root.get("failures"), root.get("errors")), ("1", "0"))
        check["results"][0].update(status="passed", flaky=True)
        root = ET.fromstring(reports.junit(doc, strict=True))
        self.assertEqual((root.get("failures"), root.get("errors")), ("1", "0"))


if __name__ == "__main__":
    unittest.main()

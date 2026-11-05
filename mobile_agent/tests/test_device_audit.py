import json
import os
import unittest
from unittest import mock

from mobile_agent.tests.test_verify import CLEAN
from mobile_agent.verify import device_audit
from mobile_agent.verify.checks import CheckError, check_from_dict
from mobile_agent.verify.tree import parse_tree

PROGRAM = "/opt/homebrew/bin/pymobiledevice3"
UDID = "00008130-001A2B3C4D5E6F70"


def issue(kind, rect="{{16, 80}, {120, 30}}", detail=None):
    """One issue as `pymobiledevice3 developer accessibility run-audit` prints it."""
    return {"issue_classification": kind, "element_rect_value": rect, "font_size": None,
            "ml_generated_description": detail, "long_description_extra_info": None}


ISSUES = [
    issue("testTypeContrast", detail="Sign in"),
    issue("testTypeHitRegion", "{{0, 400}, {30, 30}}"),
    issue("testTypeSomethingNew"),
]
TYPES = ["testTypeContrast", "testTypeHitRegion", "testTypeSomethingNew"]


def fake_tool(issues=ISSUES, types=TYPES, calls=None, code=0):
    """A stand-in for running pymobiledevice3: (exit code, standard output) by subcommand."""
    def run(argv, timeout):
        if calls is not None:
            calls.append(argv)
        if isinstance(issues, Exception):
            raise issues
        out = types if "supported-audit-types" in argv else issues
        return code, out if isinstance(out, str) else json.dumps(out)

    return run


def collector(udid=UDID, program=PROGRAM, **kwargs):
    return device_audit.Collector(lambda: udid, program=lambda: program, **kwargs)


class FindingTests(unittest.TestCase):
    def test_issues_become_findings_with_rect_and_guideline(self):
        found = device_audit.findings_from_issues(ISSUES, "frames/01-step.jpg")
        self.assertEqual(
            [item["id"] for item in found], ["device-contrast", "device-hit-region", "device-testTypeSomethingNew"]
        )
        self.assertEqual(found[0]["guideline"], "WCAG 1.4.3")
        self.assertEqual(found[1]["element"]["rect"], [0.0, 400.0, 30.0, 30.0])
        self.assertIn("(Sign in)", found[0]["message"])
        self.assertTrue(all(item["level"] == "review" and item["audit"] == "ax-device" for item in found))

    def test_a_clean_screen_has_no_findings(self):
        self.assertEqual(device_audit.findings_from_issues([]), [])


class CollectorTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {device_audit.SETTING: "1"})
        patch.start()
        self.addCleanup(patch.stop)
        self.tree = parse_tree(CLEAN)

    def ids(self, c):
        return [f["id"] for f in c.findings()]

    def test_flag_off_runs_nothing(self):
        calls = []
        with mock.patch.dict(os.environ, {device_audit.SETTING: ""}):
            c = collector(run=fake_tool(calls=calls))
            c.add(self.tree)
        self.assertEqual(calls, [])
        self.assertEqual(self.ids(c), ["device-audit-off"])

    def test_a_simulator_gets_one_note_and_runs_nothing(self):
        calls = []
        c = collector(None, run=fake_tool(calls=calls))
        c.add(self.tree)
        c.add(self.tree)
        self.assertEqual(calls, [])
        self.assertEqual(self.ids(c), ["device-audit-simulator"])
        self.assertIn("a11y", c.findings()[0]["message"])

    def test_a_phone_is_audited_once_per_screen_by_the_separate_program(self):
        calls = []
        c = collector(run=fake_tool(calls=calls))
        c.add(self.tree, "frames/01.jpg")
        c.add(self.tree, "frames/02.jpg")
        tail = ["--native", "--udid", UDID]
        self.assertEqual(calls, [[PROGRAM, "developer", "accessibility", "supported-audit-types", *tail],
                                 [PROGRAM, "developer", "accessibility", "run-audit", *TYPES, *tail]])
        self.assertEqual(len(c.findings()), 3)
        self.assertEqual(c.screens, 1)

    def test_the_supported_types_are_read_once_a_run(self):
        calls = []
        c = collector(run=fake_tool(calls=calls))
        c.add(self.tree)
        c.add(parse_tree(CLEAN.replace("Save", "Saved")))
        self.assertEqual([argv[3] for argv in calls], ["supported-audit-types", "run-audit", "run-audit"])

    def test_without_the_program_it_says_to_install_it_and_runs_nothing(self):
        calls = []
        c = collector(program=None, run=fake_tool(calls=calls))
        c.add(self.tree)
        c.add(parse_tree(CLEAN.replace("Save", "Saved")))
        self.assertEqual(calls, [])
        self.assertEqual(self.ids(c), ["device-audit-no-tool"])
        message = c.findings()[0]["message"]
        self.assertIn("pipx install pymobiledevice3", message)
        self.assertNotIn("Unlock", message)
        self.assertEqual(c.findings()[0]["level"], "review")

    def test_an_unreachable_service_is_one_review_note_without_the_programs_text(self):
        for run, reason in ((fake_tool(code=1), "pymobiledevice3 exited 1"),
                            (fake_tool(types="Traceback: Sign in"), "printed no JSON"),
                            (fake_tool(issues={"not": "a list"}), "no list of issues"),
                            (fake_tool(issues=device_audit.Unavailable("pymobiledevice3 didn't finish in 45 seconds")),
                             "didn't finish"),
                            (fake_tool(issues=ConnectionError("Sign in")), "ConnectionError")):
            with self.subTest(reason):
                c = collector(run=run)
                c.add(self.tree)
                found = c.findings()
                self.assertEqual([f["id"] for f in found], ["device-audit-unavailable"])
                self.assertEqual(found[0]["level"], "review")
                self.assertIn(reason, found[0]["message"])
                self.assertNotIn("Sign in", found[0]["message"] + found[0]["evidence"])

    def test_no_audit_types_is_no_answer_never_a_clean_screen(self):
        for types in ([], [12, 13], ["testTypeContrast", 100], ["--rsd", "x"], [["nested"]], {"a": 1}, "[]"):
            with self.subTest(types):
                c = collector(run=fake_tool(types=types))
                c.add(self.tree)
                self.assertEqual(self.ids(c), ["device-audit-unavailable"])

    def test_a_device_without_an_iphone_identifier_runs_nothing(self):
        calls = []
        c = collector("--udid", run=fake_tool(calls=calls))
        c.add(self.tree)
        self.assertEqual(calls, [])
        self.assertEqual(self.ids(c), ["device-audit-unavailable"])

    def test_the_program_runs_as_a_separate_process_with_a_bounded_wait(self):
        done = mock.Mock(returncode=0, stdout="[]")
        with mock.patch.object(device_audit.subprocess, "run", return_value=done) as run, \
                mock.patch.dict(os.environ, {"MOBSTER_NO_DEVICES": ""}):
            self.assertEqual(device_audit.run_tool([PROGRAM, "x"], 45), (0, "[]"))
        kwargs = run.call_args.kwargs
        self.assertEqual(run.call_args.args[0], [PROGRAM, "x"])
        self.assertEqual(kwargs["timeout"], 45)
        self.assertEqual(kwargs["stderr"], device_audit.subprocess.DEVNULL)
        timeout = device_audit.subprocess.TimeoutExpired([PROGRAM], 45)
        with mock.patch.object(device_audit.subprocess, "run", side_effect=timeout), \
                mock.patch.dict(os.environ, {"MOBSTER_NO_DEVICES": ""}), \
                self.assertRaises(device_audit.Unavailable) as caught:
            device_audit.run_tool([PROGRAM, "x"], 45)
        self.assertIn("didn't finish in 45 seconds", str(caught.exception))


class CheckKeyTests(unittest.TestCase):
    def test_the_check_key_accepts_ax_device_and_rejects_typos(self):
        base = {"name": "x", "app": {"path": "A.app"}, "steps": ["Open it"], "expect": [{"text": "Hi"}]}
        self.assertIn("ax-device", check_from_dict({**base, "audit": ["ax-device"]}).audit)
        with self.assertRaises(CheckError) as caught:
            check_from_dict({**base, "audit": ["ax-devce"]})
        self.assertIn("ax-device", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

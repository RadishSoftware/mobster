"""`mobster test`'s reports from a suite document (testkit/reports.py): JUnit for quarantine, flakes and --strict,
characters XML can't hold, the HTML's checks and devices, flake table and problems, escaping, and the copies a suite
folder keeps. Offline."""

from pathlib import Path
import tempfile
import unittest

from PIL import Image

from mobile_agent.testkit import reports as R
from mobile_agent.tests.test_testing_execute import validate_junit


def run(attempt, verdict, run_id, report=None, message=None, repetition=1):
    return {"repetition": repetition, "attempt": attempt, "runId": run_id, "verdict": verdict,
            "class": None if verdict == "passed" else "assertion", "message": message, "seconds": 2.5,
            "report": report, "runDir": None, "frame": None, "video": None}


def doc(folder, *, repeat=1):
    folder = Path(folder)
    run_dir = folder / "runs-src" / "20261007-120000-aaaa"
    run_dir.mkdir(parents=True)
    (run_dir / "report.html").write_text("<!doctype html><title>run</title>")
    frame = run_dir / "05-verdict-ax.jpg"
    Image.new("RGB", (400, 870), (30, 30, 40)).save(frame, "JPEG")
    report = str(run_dir / "report.html")
    flaky = {"device": "iPhone 17 Pro · iOS 26.4", "deviceKey": "sim:a", "status": "passed", "attempts": 2,
             "flaky": True, "report": report, "durationMs": 5000, "healed": [], "reason": None, "quarantined": False,
             "frame": str(frame), "video": None, "costUsd": 0.02,
             "flakyReason": {"class": "assertion", "message": "1 of 2 expectations failed\x07"},
             "runs": [run(1, "failed", "20261007-115900-bbbb", None, "1 of 2 expectations failed"),
                      run(2, "passed", "20261007-120000-aaaa", report)]}
    failed = {"device": "iPhone 17 Pro · iOS 26.4", "deviceKey": "sim:a", "status": "failed", "attempts": 1,
              "flaky": False, "report": None, "durationMs": 2500, "healed": [],
              "reason": {"class": "assertion", "message": "text \"Restore <failed>\" failed: not on screen",
                         "fix": None},
              "quarantined": True, "frame": None, "video": None, "costUsd": 0,
              "runs": [run(1, "failed", "20261007-120100-cccc", None, "not on screen")]}
    couldnt = {"device": "Sam's iPhone · iOS 26.4", "deviceKey": "device:usb", "status": "couldnt_run", "attempts": 0,
               "flaky": False, "report": None, "durationMs": 0, "healed": [], "quarantined": False, "frame": None,
               "video": None, "costUsd": 0, "runs": [], "notes": ["Installed Daybreak.app on Sam's iPhone."],
               "reason": {"class": "not_developer_build",
                          "message": "Mobster tests your own apps on your iPhone. Daybreak is from the App Store.",
                          "fix": "Install your own build of it from Xcode, then run the check again."}}
    return {"schema": R.SCHEMA, "suite": "20261007-120000-abcd", "startedAt": 1_791_380_000_000,
            "finishedAt": 1_791_380_075_000, "durationMs": 75_000, "exitCode": 3,
            "options": {"repeat": repeat, "retries": 1}, "command": "mobster test --device \"Sam's iPhone\"",
            "devices": [{"key": "sim:a", "name": "iPhone 17 Pro · iOS 26.4"},
                        {"key": "device:usb", "name": "Sam's iPhone · iOS 26.4"}],
            "checks": [{"name": "Paywall <b>plans</b>", "file": ".mobster/checks/paywall.yaml",
                        "classname": "checks.paywall", "tags": ["smoke"], "quarantined": False,
                        "results": [flaky, couldnt]},
                       {"name": "Restore shows an error", "file": ".mobster/checks/restore.yaml",
                        "classname": "checks.restore", "tags": [], "quarantined": True,
                        "quarantineReason": "Flaky on iOS 26.4 (FB13579)", "results": [failed]}],
            "summary": {"passed": 1, "failed": 0, "needsReview": 0, "couldntRun": 1, "flaky": 1, "quarantined": 1,
                        "total": 3, "costUsd": 0.02},
            "mobster": {"version": "0.3.0"}}


class ReportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name).resolve()
        self.suite = self.folder / "suite"
        self.suite.mkdir()

    def test_junit_quarantine_flakes_and_strict(self):
        data = doc(self.folder)
        R.collect(data, self.suite)
        root = validate_junit(R.junit(data))
        self.assertEqual([suite.get("name") for suite in root], ["iPhone 17 Pro · iOS 26.4", "Sam's iPhone · iOS 26.4"])
        cases = {case.get("name"): case for case in root[0].iter("testcase")}
        self.assertIsNone(cases["Paywall <b>plans</b>"].find("failure"))   # a flaky pass passes
        props = {p.get("name"): p.get("value") for p in cases["Paywall <b>plans</b>"].iter("property")}
        self.assertEqual((props["flaky"], props["attempts"], props["report"]),
                         ("true", "2", "runs/20261007-120000-aaaa.html"))
        skipped = cases["Restore shows an error"].find("skipped")
        self.assertEqual(skipped.get("message"), "Quarantined (Failed): Flaky on iOS 26.4 (FB13579)")
        failure = root[1].find("testcase/failure")
        self.assertEqual(failure.get("type"), "couldnt_run")
        self.assertIn("Install your own build", failure.text)
        self.assertIn("Installed Daybreak.app", root[1].find("testcase/system-out").text)
        strict = validate_junit(R.junit(data, strict=True, report_base=".mobster/test-results/x"))
        flaky = strict[0].find("testcase")
        self.assertEqual(flaky.find("failure").get("type"), "flaky")
        self.assertNotIn("\x07", flaky.find("failure").text)
        props = {p.get("name"): p.get("value") for p in flaky.iter("property")}
        self.assertEqual(props["report"], ".mobster/test-results/x/runs/20261007-120000-aaaa.html")

    def test_repeats_name_each_run(self):
        data = doc(self.folder, repeat=5)
        system_out = validate_junit(R.junit(data)).find("testsuite/testcase/system-out").text
        self.assertIn("Run 1, attempt 2: Passed", system_out)

    def test_the_html_matrix_flake_table_and_problems(self):
        data = doc(self.folder)
        paths = R.write_all(data, self.suite, html_dir=self.folder / "out", cwd=self.folder)
        page = paths["html"].read_text()
        self.assertRegex(page, r'<span class="verdict none"><svg[^>]*>.*?</svg>Couldn&#x27;t run</span>')
        self.assertIn("<title>Mobster test: 1 of 2 passed</title>", page)
        self.assertIn("Paywall &lt;b&gt;plans&lt;/b&gt;", page)
        self.assertNotIn("<b>plans</b>", page)
        self.assertIn('<span class="col">Sam&#x27;s iPhone · iOS 26.4</span>', page)
        self.assertIn('id="flaky">Flaky<', page)
        self.assertIn('aria-label="failed, passed"', page)
        self.assertIn(">50%<", page)
        self.assertIn("Quarantined", page)
        self.assertIn("Its results don't fail the suite.", page)
        # A check that didn't pass opens to why, with the fix; one that passed stays closed.
        self.assertRegex(page, r'<details class="check is-all is-flaky is-unpassed" id="check-1" open>')
        self.assertIn("Install your own build of it from Xcode, then run the check again.", page)
        self.assertIn('<p class="lede">1 flaky · 1 quarantined</p>', page)
        # Under the heading, what didn't pass and why, linked to its row; the quarantined failure isn't listed.
        self.assertRegex(page, r'<ul class="failures"><li class="none">.*?<a href="#check-1">Paywall &lt;b&gt;'
                               r'plans&lt;/b&gt;</a> on Sam&#x27;s iPhone\u00a0· iOS 26.4<span class="why">'
                               r'Mobster tests your own apps')
        self.assertNotIn('href="#check-2"', page)
        self.assertNotIn("\x07", page)
        self.assertIn('href="runs/20261007-120000-aaaa.html"', page)
        self.assertIn('src="data:image/jpeg;base64,', page)
        self.assertIn("1 passed, 1 flaky, 1 couldn&#x27;t run, 1 quarantined", page)
        self.assertNotRegex(page, r"(src|href)=\"(https?:|/)")
        self.assertTrue((self.folder / "out" / "runs" / "20261007-120000-aaaa.html").is_file())
        self.assertEqual((self.folder / "out" / "index.html").read_text(), page)
        saved = (self.suite / "results.json").read_text()
        self.assertNotIn(str(self.folder / "runs-src" / "20261007-120000-aaaa" / "report.html"), saved)

    def test_a_failing_check_opens_to_its_assertions_beside_the_frame(self):
        from mobile_agent.testkit.suite import attempt_record
        record = attempt_record({"verdict": "failed", "assertions": [
            {"index": 0, "text": 'text "Restore"', "ok": False, "observed": "not on screen", "matches": []},
            {"index": 1, "text": "count id=/^plan_/ == 3", "ok": False,
             "observed": "found 2: plan_weekly, plan_monthly",
             "matches": [{"id": "plan_weekly", "rect": [20.0, 464.0, 362.0, 65.0]}]}]}, 1)
        self.assertEqual(record["assertions"][1], {"index": 1, "text": "count id=/^plan_/ == 3", "ok": False,
                                                   "observed": "found 2: plan_weekly, plan_monthly",
                                                   "outlined": True})
        data = doc(self.folder)
        failed = data["checks"][1]["results"][0]
        failed["assertions"] = record["assertions"]
        failed["frame"] = data["checks"][0]["results"][0]["frame"]
        page = R.render_html(data, self.suite)
        self.assertIn('<code class="assert">count id=/^plan_/ == 3</code>', page)
        self.assertIn("found 2: plan_weekly, plan_monthly", page)
        self.assertIn('aria-label="2, failed"', page)                               # its pill on the frame
        self.assertIn('aria-label="1, failed, not outlined on the frame"', page)   # nothing on screen to outline
        self.assertEqual(page.count('<div class="phone"><img src="data:image/jpeg;base64,'), 2)

    def test_a_missing_report_is_none_and_suite_folders_are_private_and_unique(self):
        data = doc(self.folder)
        data["checks"][0]["results"][0]["runs"][1]["report"] = str(self.folder / "gone.html")
        data["checks"][0]["results"][0]["report"] = str(self.folder / "gone.html")
        R.collect(data, self.suite)
        self.assertIsNone(data["checks"][0]["results"][0]["report"])
        first, folder = R.make_suite_dir(self.folder / "results", "20261007-120000-abcd")
        second, other = R.make_suite_dir(self.folder / "results", "20261007-120000-abcd")
        self.assertNotEqual(first, second)
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertRegex(second, r"^\d{8}-\d{6}-[0-9a-f]{4}$")


if __name__ == "__main__":
    unittest.main()

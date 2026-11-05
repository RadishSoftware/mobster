"""Proof per step (verify/proof.py): the spans, the cause line, the requests, and the surfaces that show them."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent.testkit import pr_report, reports
from mobile_agent.tests.test_failure_evidence import (
    BUNDLE,
    EXE,
    PLANS,
    Driver,
    EvidenceManager,
    FakeTools,
    RunnerCase,
    crash_file,
    log_lines,
    validate_junit,
)
from mobile_agent.verify import evidence, proof
from mobile_agent.verify.report import cause_line, proof_pane, render_html

TASK = "7A1C0E55-0000-4000-8000-000000000001"


def line(at, text, level="notice", own=False, process=EXE):
    return {"atMs": at, "level": level, "process": process, "text": text, "own": own, "driver": False}


def task_lines(at):
    return [line(at, f"Task <{TASK}>.<1> sent request: POST https://api.example.com/v1/cart?token=abc#frag"),
            line(at + 40, f"Task <{TASK}>.<1> received response, status 201 content 512"),
            line(at + 45, f"Task <{TASK}>.<1> summary for task success {{transaction_duration_ms=88, "
                          f"response_status=201, response_bytes=512}}")]


class CauseTests(unittest.TestCase):
    def test_every_reason_class_names_a_side_and_a_line(self):
        for klass, side in (("app_crashed", "app"), ("assertion", "app"), ("replay", "check"), ("wda", "environment"),
                            ("install", "environment"), ("usage", "check"), ("unknown", "environment")):
            with self.subTest(klass=klass):
                cause = proof.cause("failed", klass)
                self.assertEqual(cause["side"], side)
                self.assertTrue(cause["line"])

    def test_a_pass_has_no_cause_and_an_unsettled_screen_is_a_timeout(self):
        self.assertIsNone(proof.cause("passed", None))
        self.assertEqual(proof.cause("failed", "assertion", stable=False)["line"],
                         "timed out waiting for the screen to settle")
        self.assertEqual(proof.cause("failed", "assertion", stable=True)["line"],
                         "assertion failed on the final screen")


class SpanTests(unittest.TestCase):
    steps = [{"index": 1, "text": "Tap Add", "at_ms": 1000, "frame": "01.jpg"},
             {"index": 2, "text": "Tap Buy", "at_ms": 3000, "frame": "02.jpg"}]

    def test_each_line_and_request_lands_in_one_span(self):
        lines = [line(-20, "early"), line(500, "a"), line(1000, "edge")] + task_lines(2000) + [line(3500, "after")]
        entries, after = proof.steps_proof(self.steps, lines, end_ms=4000)
        self.assertEqual([item["text"] for item in entries[0]["log"]["lines"]], ["early", "a", "edge"])
        second = entries[1]
        self.assertEqual((second["fromMs"], second["toMs"]), (1000, 3000))
        self.assertEqual(second["network"]["rows"], [{"atMs": 2000, "host": "api.example.com", "method": "POST",
                                                      "status": 201, "durationMs": 88, "bytes": 512}])
        self.assertEqual([item["text"] for item in after["log"]["lines"]], ["after"])
        self.assertEqual((after["fromMs"], after["toMs"]), (3000, 4000))

    def test_a_request_keeps_its_host_never_the_path_or_query(self):
        rows = proof.network_rows(task_lines(10))
        self.assertEqual(set(rows[0]), {"atMs", "host", "method", "status", "durationMs", "bytes"})
        self.assertNotIn("token", json.dumps(rows))
        self.assertNotIn("/v1/cart", json.dumps(rows))

    def test_a_log_line_that_names_no_host_gives_no_row(self):
        self.assertEqual(proof.network_rows([line(5, f"Task <{TASK}>.<1> summary {{response_status=200}}")]), [])

    def test_the_caps_hold_and_keep_the_apps_loud_lines(self):
        lines = [line(index, f"noise {index}") for index in range(1, 400)] + [line(200, "boom", "error", True)]
        entries, _ = proof.steps_proof([{"index": 1, "text": "x", "at_ms": 1000}], lines, end_ms=1000)
        shown = entries[0]["log"]
        self.assertEqual(shown["count"], 400)
        self.assertEqual(len(shown["lines"]), proof.STEP_LOG_LINES)
        self.assertIn("boom", [item["text"] for item in shown["lines"]])
        many = [{"index": n, "text": "s", "at_ms": n * 10} for n in range(1, 61)]
        entries, after = proof.steps_proof(many, [line(n * 10, f"l{n}") for n in range(1, 61)], end_ms=700)
        total = sum(len(item["log"]["lines"]) for item in entries + [after])
        self.assertLessEqual(total, proof.TOTAL_LOG_LINES)

    def test_a_clip_is_the_span_less_where_the_recording_began(self):
        entry = {"fromMs": 1000, "toMs": 3000}
        self.assertEqual(proof.clip(entry, 400), (0.6, 2.6))
        self.assertEqual(proof.clip(entry, -500), (1.5, 3.5))
        self.assertEqual(proof.clip({"fromMs": 0, "toMs": 200}, 1000), (0.0, 0.0))
        self.assertIsNone(proof.clip(entry, None))


class RunTests(RunnerCase):
    def setUp(self):
        super().setUp()
        self.reports = self.root / "DiagnosticReports"
        self.reports.mkdir()

    def run_check(self, lines, *, crash=True):
        wall = time.time()
        tools = FakeTools(self.reports, lines=lines(wall))
        on = {"launch": lambda: crash_file(self.reports, wall=time.time(), pid=1001)} if crash else {}
        manager = EvidenceManager(tools, on=on)
        check = self.check(bundle_id=BUNDLE, expect=[{"text": "Choose your plan"}])
        with mock.patch.dict(evidence.REPORT_WAIT, {"simulator": 0}), mock.patch.object(evidence, "EXIT_WAIT", 0):
            result, _ = self.verify(check, Driver([PLANS], foreground=BUNDLE), manager=manager)
        return result

    def test_a_crashed_run_names_its_cause_the_frame_and_the_log_it_judged_on(self):
        result = self.run_check(lambda wall: log_lines(wall, pid=1001))
        made = result["step_proof"]
        self.assertEqual(made["cause"], {"side": "app", "kind": "app_crashed", "line": "app crashed"})
        self.assertTrue(made["judged"]["frame"].endswith("-verdict.jpg"))
        self.assertTrue(made["judged"]["overlay"].endswith("-verdict-ax.jpg"))
        self.assertRegex(made["judged"]["treeHash"], "^[0-9a-f]{64}$")
        texts = [item["text"] for item in made["after"]["log"]["lines"]]
        self.assertTrue(any("about to crash" in text for text in texts), texts)
        page = Path(result["report"]).read_text()
        self.assertIn("App problem", page)
        self.assertIn("app crashed", page)
        self.assertIn("Judged on", page)

    def test_a_pass_has_proof_and_no_cause(self):
        result = self.run_check(lambda wall: log_lines(wall, pid=1001, crash=False), crash=False)
        self.assertEqual(result["verdict"], "passed")
        self.assertIsNone(result["step_proof"]["cause"])
        self.assertNotIn("App problem", Path(result["report"]).read_text())


class SurfaceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)

    def made(self):
        steps = [{"index": 1, "text": "Tap Add", "at_ms": 1000, "frame": None},
                 {"index": 2, "text": "Tap Buy <now>", "at_ms": 3000, "frame": None}]
        lines = [line(500, "added"), line(2500, "buy failed <x>", "error", True), line(3300, "idle")] + task_lines(2000)
        entries, after = proof.steps_proof(steps, lines, end_ms=3500)
        return {"cause": proof.cause("failed", "assertion"), "judged": {"frame": "03-verdict.jpg", "overlay": None,
                                                                         "verdictAtMs": 3400, "treeHash": "ab" * 32},
                "steps": entries, "after": after}

    def test_the_report_page_shows_each_steps_requests_and_lines_escaped(self):
        result = {"verdict": "failed", "run_id": "20261024-120000-x", "check": {"name": "Buy"},
                  "reason": {"class": "assertion", "message": "m"}, "assertions": [], "device": {}, "app": {},
                  "steps": [{"index": 1, "op": "tap", "text": "Tap Add", "at_ms": 1000},
                            {"index": 2, "op": "tap", "text": "Tap Buy", "at_ms": 3000}],
                  "frames": [], "step_proof": self.made()}
        with tempfile.TemporaryDirectory() as folder:
            page = render_html(result, folder)
        self.assertIn("Proof for this step", page)
        self.assertIn("api.example.com", page)
        self.assertIn("buy failed &lt;x&gt;", page)
        self.assertNotIn("buy failed <x>", page)
        self.assertIn("Until the verdict", page)
        self.assertIn("App problem", page)

    def test_a_pane_with_nothing_to_show_is_empty_and_a_clip_links_the_recording(self):
        self.assertEqual(proof_pane({"log": {"lines": [], "count": 0}, "network": {"rows": [], "count": 0}}), "")
        entry = self.made()["steps"][1]
        html = proof_pane(entry, clip=(1.0, 2.5), video="videos/001-buy-1.mp4", title="Step 2")
        self.assertIn('href="videos/001-buy-1.mp4#t=1.0,2.5"', html)
        self.assertEqual(cause_line(None), "")

    def cell(self):
        run_dir = self.dir / "src" / "R1"
        run_dir.mkdir(parents=True)
        (run_dir / "report.html").write_text("<p>run</p>")
        return {"device": "iPhone 17 Pro · iOS 26.4", "deviceKey": "k", "status": "failed", "attempts": 1,
                "flaky": False, "report": str(run_dir / "report.html"), "durationMs": 5000, "healed": [],
                "reason": {"class": "assertion", "message": "m", "fix": None}, "quarantined": False, "frame": None,
                "video": str(self.dir / "suite" / "videos" / "001-buy-1.mp4"), "videoOffsetMs": 400,
                "assertions": [], "costUsd": 0, "evidence": {"step": 2}, "stepProof": self.made(),
                "runs": [{"repetition": 1, "attempt": 1, "runId": "R1", "verdict": "failed", "class": "assertion",
                          "message": "m", "seconds": 5, "report": str(run_dir / "report.html"),
                          "runDir": str(run_dir), "video": None}]}

    def doc(self):
        return {"schema": reports.SCHEMA, "suite": "S", "devices": [{"key": "k", "name": "iPhone 17 Pro · iOS 26.4"}],
                "checks": [{"name": "Buy", "file": "checks/buy.yaml", "classname": "checks.buy", "tags": [],
                            "quarantined": False, "results": [self.cell()]}],
                "summary": {"failed": 1, "total": 1}, "options": {}, "exitCode": 1, "mobster": {"version": "t"}}

    def test_the_suite_page_and_junit_carry_the_failing_steps_proof(self):
        folder = self.dir / "suite"
        (folder / "videos").mkdir(parents=True)
        (folder / "videos" / "001-buy-1.mp4").write_bytes(b"x")
        doc = self.doc()
        reports.write_all(doc, folder, cwd=self.dir)
        page = (folder / "index.html").read_text()
        self.assertIn('href="videos/001-buy-1.mp4#t=0.6,2.6"', page)
        self.assertIn("Step 2: Tap Buy &lt;now&gt;", page)
        self.assertIn("App problem", page)
        data = (folder / "junit.xml").read_bytes()
        validate_junit(data)
        text = data.decode()
        self.assertIn("Cause (app): assertion failed on the final screen", text)
        self.assertIn("Request: POST api.example.com 201 88", text)
        self.assertIn("buy failed &lt;x&gt;", text)

    def test_the_pr_comment_masks_a_typed_steps_words_and_masks_them_when_the_run_cant_be_read(self):
        cell = self.cell()
        cell["stepProof"]["steps"][1]["text"] = "Type “sam@example.com” in Email"
        run_dir = Path(cell["runs"][0]["runDir"])
        self.assertNotIn("sam@example.com", pr_report.failing_proof(cell)["text"])   # no result.json: masked
        (run_dir / "result.json").write_text(json.dumps({"steps": [
            {"index": 1, "text": "Tap Add"}, {"index": 2, "text": "Type “sam@example.com” in Email",
                                              "typed": "sam@example.com"}]}))
        self.assertEqual(pr_report.failing_proof(cell)["text"], "Type “•••” in Email")
        (run_dir / "result.json").write_text(json.dumps({"steps": [{"index": 2, "text": "Tap “Buy”"}]}))
        cell["stepProof"]["steps"][1]["text"] = "Tap “Buy”"
        self.assertEqual(pr_report.failing_proof(cell)["text"], "Tap “Buy”")

    def test_the_pr_comment_quotes_the_cause_the_requests_and_the_log_tail(self):
        cell = self.cell()
        cell["evidence"] = {"step": 2}
        row_proof = pr_report.failing_proof(cell)
        self.assertEqual(row_proof["index"], 2)
        self.assertEqual(row_proof["requests"][0]["host"], "api.example.com")
        pr = {"headline": "Mobster checks: 1 failed", "rows": [{
            "check": "Buy", "device": "iPhone 17 Pro", "status": "failed", "expected": "x", "found": "y",
            "cause": cell["stepProof"]["cause"], "failingStep": row_proof, "steps": ["Tap Add", "Tap Buy"],
            "why": "w"}]}
        text = pr_report.comment(pr)
        self.assertIn("(failed, assertion failed on the final screen)", text)
        self.assertIn("**App problem:** assertion failed on the final screen.", text)
        self.assertIn("- POST api.example.com 201 88", text)
        self.assertIn("buy failed &lt;x&gt;".replace("&lt;", "<").replace("&gt;", ">"), text)

    def test_a_failed_runs_clip_is_the_failing_step_and_the_one_before(self):
        steps = [{"index": n, "text": f"Step {n}", "frame": f"f{n}.jpg", "at_ms": n * 100} for n in range(1, 7)]
        run = {"steps": steps, "frames": [], "step_proof": {"steps": [{"index": n} for n in range(1, 7)]},
               "evidence": {"step": 4}, "summary": "s"}
        beats = pr_report.beats_for({"name": "Buy"}, {"status": "failed", "assertions": []}, run, Path("/r"))
        shown = [beat for beat in beats if beat.kind == "step"]
        self.assertEqual([beat.caption for beat in shown], ["Step 3", "Step 4"])


if __name__ == "__main__":
    unittest.main()

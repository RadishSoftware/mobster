"""scripts/e2e_launch1.py's key-less legs check the run's exit code, not only its verdict (SPEC §5.3, §11.4).

mcp_smoke.py is faked, so these need no simulator, no Xcode and no MCP server.
"""

import argparse
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "e2e_launch1.py"


def load():
    spec = importlib.util.spec_from_file_location("e2e_launch1_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(SCRIPT.is_file(), "the acceptance suite is not part of this copy")
class KeylessExitCodeTests(unittest.TestCase):
    def setUp(self):
        self.e2e = load()
        self.e2e.log = lambda message: None
        self.folder = Path(tempfile.mkdtemp(prefix="mobster-e2e-test-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        args = argparse.Namespace(work=str(self.folder / "work"), mobster=None, env_file=None, ledger=None,
                                  legs=None, keep=False, json=None)
        self.suite = self.e2e.Suite(args)
        self.smoke_exit = 0

    def session(self, verdict, exit_code, klass=None, run_id="20260928-172206-6542", written_id=None):
        """A session as mcp_smoke.py reports it, with the run's result.json beside its report."""
        run_dir = self.folder / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(json.dumps({
            "schema": "mobster.verify/1", "run_id": written_id or run_id, "verdict": verdict,
            "exit_code": exit_code, "reason": {"class": klass} if klass else None}))
        (run_dir / "report.html").write_text("<html></html>")
        session = {"index": 0, "run_id": run_id, "verdict": verdict, "summary": f"{verdict} summary",
                   "reason": {"class": klass} if klass else None, "cost_usd": 0, "report": str(run_dir / "report.html")}
        self.suite.mcp_smoke = lambda name, *arguments, timeout=900: (
            self.smoke_exit, {"sessions": [session], "action_p50_seconds": 1.5, "action_n": 5, "failures": []}, 6.2)
        return session

    def test_every_run_goes_into_the_work_folder_and_no_key_reaches_the_key_less_legs(self):
        # Review round 3: the MCP legs' server fell back to <cwd>/.mobster/runs, so a full run left run folders in
        # the checkout. And since #44 an Anthropic key alone turns Smart on, so it is held back like OpenAI's.
        self.assertEqual(self.suite.env["MOBSTER_RUNS_DIR"], str(self.suite.runs))
        self.assertTrue(Path(self.suite.env["MOBSTER_RUNS_DIR"]).is_relative_to(self.folder / "work"))
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MOBSTER_SMART_MODEL", "MOBSTER_ENV_FILE"):
            self.assertNotIn(name, self.suite.env)

    def test_leg_6_records_the_runs_exit_code_and_keeps_the_smokes_in_the_notes(self):
        self.session("needs_review", 2, "held_before_flow")
        self.suite.leg6_held_before_flow()
        row = self.suite.results[-1]
        self.assertTrue(row["ok"], row)
        self.assertEqual(row["exit"], 2)
        self.assertEqual(row["expected"], "needs_review (exit 2), held_before_flow")
        self.assertIn("mcp_smoke.py (exit 0)", row["notes"])

    def test_leg_6_fails_when_the_exit_code_does_not_follow_the_verdict(self):
        self.session("needs_review", 0, "held_before_flow")
        self.suite.leg6_held_before_flow()
        row = self.suite.results[-1]
        self.assertFalse(row["ok"])
        self.assertEqual(row["exit"], 0)

    def test_keyless_legs_expect_the_verdicts_exit_code(self):
        for verdict, code in (("passed", 0), ("failed", 1)):
            with self.subTest(verdict=verdict):
                self.session(verdict, code, run_id=f"20260928-1722{code:02d}-abcd")
                self.suite.keyless(9, "09-keyless", "Key-less", [], [{"text": "Choose your plan"}], [], verdict)
                row = self.suite.results[-1]
                self.assertTrue(row["ok"], row)
                self.assertEqual(row["exit"], code)
                self.assertEqual(row["expected"], f"{verdict} (exit {code})")
                self.assertTrue(row["notes"].startswith("mcp_smoke.py exit 0; action p50 1.5 s (n=5)"), row["notes"])

    def test_a_failed_run_that_exits_0_fails_the_leg(self):
        self.session("failed", 0)
        self.suite.keyless(10, "10-keyless-bug", "Key-less bug", [], [], [], "failed")
        self.assertFalse(self.suite.results[-1]["ok"])

    def test_the_smokes_own_failure_still_fails_the_leg(self):
        self.session("passed", 0)
        self.smoke_exit = 1
        self.suite.keyless(9, "09-keyless", "Key-less", [], [], [], "passed")
        row = self.suite.results[-1]
        self.assertFalse(row["ok"])
        self.assertIn("mcp_smoke.py exit 1", row["notes"])

    def test_the_exit_code_comes_from_the_session_when_it_carries_one(self):
        self.assertEqual(self.e2e.session_exit_code({"exit_code": 2, "report": "/nonexistent/report.html"}), 2)

    def test_no_result_json_or_another_runs_gives_no_exit_code(self):
        self.assertIsNone(self.e2e.session_exit_code({}))
        self.assertIsNone(self.e2e.session_exit_code({"report": str(self.folder / "missing" / "report.html")}))
        session = self.session("passed", 0, written_id="20260928-000000-ffff")
        self.assertIsNone(self.e2e.session_exit_code(session))
        self.suite.keyless(9, "09-keyless", "Key-less", [], [], [], "passed")
        self.assertFalse(self.suite.results[-1]["ok"])


if __name__ == "__main__":
    unittest.main()

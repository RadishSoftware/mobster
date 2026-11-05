"""Local MCP comparison exposes bounded opaque references without a Remote capability."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent.mcp_server import registry
from mobile_agent.mcp_server.protocol import Call, ToolResult
from mobile_agent.mcp_server.tools import ToolError, ToolSet
from mobile_agent.testkit import mcp_comparison
from mobile_agent.testkit.mcp import TestTools
from mobile_agent.tests.seam_support import isolate

BASE = "20261024-120000-abcd"
CANDIDATE = "20261024-130000-ef12"
ARGS = {"base_suite_id": BASE, "candidate_suite_id": CANDIDATE}


def comparison(count=1):
    cases = [{"caseRef": "abcdabcdabcd", "targetRef": "beefbeefbeef", "base": {"status": "failed"},
              "candidate": {"status": "passed"}, "comparable": True, "observedTransition": "failed_to_passed",
              "verifiedFix": False, "proof": {"scope": None},
              "reasons": [], "proofReasons": ["external_fixture_unproven"]}
             for _ in range(count)]
    return {"schema": "mobster.test-compare/1", "baseSuite": BASE, "candidateSuite": CANDIDATE,
            "baseVerdict": 1, "candidateVerdict": 0, "comparable": True, "verifiedFix": False,
            "proof": {"scope": None}, "reasons": [], "cases": cases}


class ComparisonMcpTests(unittest.TestCase):
    def setUp(self):
        isolate(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        runs = self.root / ".mobster" / "runs"
        runs.mkdir(parents=True)
        self.provider = TestTools()
        registry.register_provider(self.provider)
        self.tools = ToolSet(runs_dir=runs, keyless=True)
        self.addCleanup(self.tools.shutdown)

    def test_tool_lists_as_read_only_and_preserves_validated_handles_through_mask(self):
        listed = {definition["name"] for definition in self.tools.list_tools()}
        self.assertIn("compare_test_suites", listed)
        self.assertTrue(self.provider.annotations()["compare_test_suites"]["readOnlyHint"])
        body = {**comparison(), "casesTotal": 1, "casesTruncated": False}
        body["reasons"] = ["ordinary abcd text"]
        self.tools.codes.all_secrets = lambda: ["abcd", "beef", "test"]
        with mock.patch.object(mcp_comparison, "call", return_value=ToolResult(
                "Compared abcd local suites", body)):
            result = self.tools.call("compare_test_suites", ARGS, Call(1))
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(result.structured["baseSuite"], BASE)
        self.assertEqual(result.structured["candidateSuite"], CANDIDATE)
        self.assertEqual(result.structured["schema"], "mobster.test-compare/1")
        self.assertEqual(result.structured["cases"][0]["caseRef"], "abcdabcdabcd")
        self.assertEqual(result.structured["cases"][0]["targetRef"], "beefbeefbeef")
        self.assertNotIn("abcd", result.text)
        self.assertNotIn("abcd", result.structured["reasons"][0])
        self.assertFalse(result.structured["verifiedFix"])

    def test_controlled_app_state_proof_survives_mask_only_when_validated(self):
        body = {**comparison(), "verifiedFix": True, "proof": {"scope": "controlled_app_state"},
                "casesTotal": 1, "casesTruncated": False}
        body["cases"][0].update(verifiedFix=True, proof={"scope": "controlled_app_state"})
        self.tools.codes.all_secrets = lambda: ["controlled", "abcd"]
        with mock.patch.object(mcp_comparison, "call", return_value=ToolResult("Compared", body)):
            result = self.tools.call("compare_test_suites", ARGS, Call(10))
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(result.structured["proof"], {"scope": "controlled_app_state"})
        self.assertEqual(result.structured["cases"][0]["proof"], {"scope": "controlled_app_state"})

    def test_provider_returns_only_first_100_cases_but_keeps_full_comparability(self):
        recorded = []

        def compare(results_dir, base, candidate):
            recorded.append((results_dir, base, candidate))
            return comparison(140)

        result = mcp_comparison.call(ARGS, self.tools, compare=compare)
        self.assertEqual(len(result.structured["cases"]), 100)
        self.assertEqual(result.structured["casesTotal"], 140)
        self.assertTrue(result.structured["casesTruncated"])
        self.assertTrue(result.structured["comparable"])
        self.assertFalse(result.structured["verifiedFix"])
        self.assertEqual(recorded[0][1:], (BASE, CANDIDATE))

    def test_schema_rejects_arbitrary_paths_and_missing_id(self):
        from mobile_agent.mcp_server.protocol import ProtocolError
        with self.assertRaises(ProtocolError):
            self.tools.call("compare_test_suites", {**ARGS, "path": "/tmp/reports"}, Call(2))
        with self.assertRaises(ProtocolError):
            self.tools.call("compare_test_suites", {"base_suite_id": BASE}, Call(3))

    def test_custom_runs_folder_cannot_select_an_unrelated_results_root(self):
        custom = SimpleNamespace(runs_dir=self.root / "elsewhere")
        with self.assertRaisesRegex(ToolError, "without a custom --out"):
            mcp_comparison.call(ARGS, custom, compare=lambda *_: self.fail("compared"))

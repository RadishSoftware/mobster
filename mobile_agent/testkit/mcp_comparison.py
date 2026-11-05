"""Bounded, local-only MCP view of two exact saved test suites."""

import re
from pathlib import Path

from .comparison import ComparisonError, compare_suites

NAME = "compare_test_suites"
MAX_CASES = 100
SUITE = r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$"
REF = re.compile(r"[a-f0-9]{12}\Z")


def definition():
    return {"name": NAME,
            "description": "Compare two exact completed local test suites. Returns both verdicts and bounded "
                           "case transitions; never treats a green run alone as proof of a fix.",
            "inputSchema": {"type": "object", "properties": {
                "base_suite_id": {"type": "string", "pattern": SUITE,
                                  "description": "the saved suite before the proposed change"},
                "candidate_suite_id": {"type": "string", "pattern": SUITE,
                                       "description": "the saved suite after the proposed change"}},
                            "required": ["base_suite_id", "candidate_suite_id"],
                            "additionalProperties": False}}


def call(args, toolset, *, compare=compare_suites):
    from ..mcp_server.protocol import ToolResult
    from ..mcp_server.tools import ToolError

    runs = Path(toolset.runs_dir).absolute()
    private = runs.parent
    results = private / "test-results"
    if runs.name != "runs" or private.name != ".mobster":
        raise ToolError("Start this MCP server in the test project without a custom --out to compare its suites.")
    if runs.is_symlink() or private.is_symlink() or results.is_symlink():
        raise ToolError("The project's test-results folder must not be a symlink.")
    try:
        answer = compare(results, args["base_suite_id"], args["candidate_suite_id"])
    except (ComparisonError, OSError) as error:
        raise ToolError(str(error)) from None
    total = len(answer["cases"])
    answer = {**answer, "cases": answer["cases"][:MAX_CASES], "casesTotal": total,
              "casesTruncated": total > MAX_CASES}
    proof = "yes, for controlled app state" if answer["verifiedFix"] else "no"
    text = (f"Compared suites {args['base_suite_id']} and {args['candidate_suite_id']}: "
            f"{'comparable' if answer['comparable'] else 'not comparable'}; verified fix: {proof}. "
            f"Showing {len(answer['cases'])} of {total} case-target pairs.")
    return ToolResult(text, answer)


def mask_comparison(mask, args, result):
    """Preserve only validated opaque suite/case handles through the contextual secret mask."""
    from ..mcp_server.tools import ToolError

    if result.is_error:
        return mask(result)
    value = result.structured
    if not isinstance(value, dict) or set(value) != {
            "schema", "baseSuite", "candidateSuite", "baseVerdict", "candidateVerdict", "comparable",
            "verifiedFix", "proof", "reasons", "cases", "casesTotal", "casesTruncated"} or \
            value["schema"] != "mobster.test-compare/1" or \
            value["baseSuite"] != args["base_suite_id"] or value["candidateSuite"] != args["candidate_suite_id"] or \
            type(value["verifiedFix"]) is not bool or \
            value["proof"] != {"scope": "controlled_app_state" if value["verifiedFix"] else None} or \
            type(value["casesTotal"]) is not int or \
            not isinstance(value["cases"], list) or len(value["cases"]) > MAX_CASES or \
            type(value["casesTruncated"]) is not bool:
        raise ToolError("Mobster returned an invalid local suite comparison.")
    refs = []
    for row in value["cases"]:
        if not isinstance(row, dict) or type(row.get("verifiedFix")) is not bool or \
                row.get("proof") != {"scope": "controlled_app_state" if row["verifiedFix"] else None} or \
                not all(isinstance(row.get(key), str) and REF.fullmatch(row[key])
                                                for key in ("caseRef", "targetRef")):
            raise ToolError("Mobster returned invalid comparison case references.")
        refs.append((row["caseRef"], row["targetRef"]))
    result.structured = {**value, "schema": None, "baseSuite": None, "candidateSuite": None, "proof": None,
                         "cases": [{**row, "caseRef": None, "targetRef": None, "proof": None}
                                   for row in value["cases"]]}
    masked = mask(result)
    masked.structured["schema"] = value["schema"]
    masked.structured["baseSuite"] = value["baseSuite"]
    masked.structured["candidateSuite"] = value["candidateSuite"]
    masked.structured["proof"] = value["proof"]
    for row, source, (case_ref, target_ref) in zip(masked.structured["cases"], value["cases"], refs):
        row.update(caseRef=case_ref, targetRef=target_ref, proof=source["proof"])
    return masked

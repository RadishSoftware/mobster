"""Compare two explicitly named local terminal suite reports."""

import json
import sys
from pathlib import Path

from ..verify.checks import project_root
from .comparison import ComparisonError, compare_suites


def run(args, operands, *, out=None, err=None, compare=compare_suites):
    out, err = out or sys.stdout, err or sys.stderr

    def refuse(message):
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": message}), file=out)
        else:
            print(f"mobster test compare: {message}", file=err)
        return 3

    if len(operands) != 2:
        return refuse("name a base and candidate: mobster test compare BASE_SUITE_ID CANDIDATE_SUITE_ID")
    private = project_root(Path.cwd()) / ".mobster"
    results = private / "test-results"
    if private.is_symlink() or results.is_symlink():
        return refuse("the project's test-results folder must not be a symlink")
    try:
        result = compare(results, *operands)
    except (ComparisonError, OSError) as error:
        return refuse(str(error))
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False), file=out)
    else:
        print(f"Base {result['baseSuite']}: exit {result['baseVerdict']}; "
              f"candidate {result['candidateSuite']}: exit {result['candidateVerdict']}.", file=out)
        proof = "yes, for controlled app state" if result["verifiedFix"] else "no"
        print(f"Comparable: {'yes' if result['comparable'] else 'no'}; verified fix: {proof}.", file=out)
        if result["reasons"]:
            print("Reasons: " + ", ".join(result["reasons"]), file=out)
        print(f"Compared {len(result['cases'])} case-target pairs. Use --json for each transition.", file=out)
    return 0

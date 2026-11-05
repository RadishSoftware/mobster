"""Run one bounded local reduction from an exact saved failure."""

import json
import sys
from pathlib import Path

from ..verify.checks import project_root
from .minimize import MinimizationError, minimize


def run(args, operands, *, out=None, err=None, reduce=minimize):
    out, err = out or sys.stdout, err or sys.stderr

    def refuse(message):
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": message}), file=out)
        else:
            print(f"mobster test minimize: {message}", file=err)
        return 3

    if len(operands) != 4:
        return refuse("name exact bad and control suites, caseRef and targetRef: "
                      "mobster test minimize BAD_SUITE_ID CONTROL_SUITE_ID CASE_REF TARGET_REF "
                      "--bad-app PATH --control-app PATH --isolated")
    if not getattr(args, "minimize_isolated", False):
        return refuse("pass --isolated only after resetting the external fixture yourself")
    if not getattr(args, "minimize_bad_app", None) or not getattr(args, "minimize_control_app", None):
        return refuse("name both exact saved builds with --bad-app and --control-app")
    private = project_root(Path.cwd()) / ".mobster"
    results = private / "test-results"
    if private.is_symlink() or results.is_symlink():
        return refuse("the project's test-results folder must not be a symlink")
    try:
        result = reduce(results, operands[0], operands[1], case_ref=operands[2], target_ref=operands[3],
                        bad_app=args.minimize_bad_app, control_app=args.minimize_control_app, isolated=True,
                        max_attempts=getattr(args, "minimize_max_attempts", None) or 8)
    except (MinimizationError, OSError) as error:
        return refuse(str(error))
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), file=out)
    else:
        print(f"Reduction {result['status'].replace('_', ' ')}: {result['attemptsUsed']} trial suites; "
              f"{result['originalSteps']} original steps.", file=out)
        if result.get("candidateCheck"):
            print(f"Review {result['candidateCheck']} ({result['candidateSteps']} steps).", file=out)
        elif result.get("reason"):
            print(result["reason"], file=out)
    return 130 if result["status"] == "stopped" else 3 if result["status"] == "stopped_unknown" else 0

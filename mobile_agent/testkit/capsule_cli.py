"""Inspect or export one local suite's bounded evidence capsule."""

import json
import sys
from pathlib import Path

from ..verify.checks import project_root
from . import capsule


def run(args, operands, *, out=None, err=None, inspect=None, export=None):
    out, err = out or sys.stdout, err or sys.stderr

    def refuse(message):
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": message}), file=out)
        else:
            print(f"mobster test capsule: {message}", file=err)
        return 3

    if len(operands) != 2 or operands[0] not in ("show", "export") or not capsule.SUITE_ID.fullmatch(operands[1]):
        return refuse("use `mobster test capsule show SUITE_ID` or "
                      "`mobster test capsule export SUITE_ID --output DIR`")
    action, suite_id = operands
    output = getattr(args, "record_output", None)
    include = tuple(getattr(args, "capsule_include", None) or ())
    if action == "show" and (output or include):
        return refuse("--output and --include go with capsule export")
    if action == "export" and not output:
        return refuse("name a new directory with --output for capsule export")
    root = project_root()
    private = root / ".mobster"
    results = private / "test-results"
    if private.is_symlink() or results.is_symlink():
        return refuse("the project's test-results folder must not be a symlink")
    folder = results / suite_id
    try:
        if action == "show":
            descriptor = (inspect or capsule.inspect)(folder)
        else:
            descriptor = (export or capsule.export)(folder, Path(output).expanduser(), include=include)
    except (OSError, ValueError) as error:
        return refuse(str(error))
    if getattr(args, "json", False):
        answer = descriptor if action == "show" else {"output": str(Path(output).expanduser()),
                                                        "capsule": descriptor}
        print(json.dumps(answer, ensure_ascii=False), file=out)
    else:
        verdict = "passed" if descriptor["results"]["exitCode"] == 0 else "did not pass"
        selected = sum(item["state"] == "matched" and item["selected"] for item in descriptor["evidence"])
        if action == "show":
            print(f"Suite {suite_id} {verdict}; {len(descriptor['evidence'])} evidence entries. "
                  "Use capsule export --include to copy selected raw files.", file=out)
        else:
            print(f"Exported capsule for suite {suite_id} to {output}; {selected} selected raw "
                  f"file{'s' if selected != 1 else ''} matched the capture hashes.", file=out)
    return 0

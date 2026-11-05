"""Run one local, versioned multi-simulator scenario with strict saved-check replay."""

import json
import sys

from .api import UsageError
from .scenario import execute_scenario, plan_scenario


def run(args, operands, *, out=None, err=None, plan=plan_scenario, execute=execute_scenario):
    from ..devtools import ProgressLines
    from .cli import summary_text

    out, err = out or sys.stdout, err or sys.stderr
    if len(operands) != 1:
        print("mobster test scenario: name one YAML file under .mobster/scenarios", file=err)
        return 3
    try:
        planned = plan(operands[0])
    except UsageError as error:
        print(f"mobster test scenario: {error}", file=err)
        return 3
    progress = ProgressLines(err)
    progress(f"mobster test scenario: {planned.name}; {len(planned.actors)} simulators, "
             f"{len(planned.phases)} ordered phases; strict saved-check replay")
    try:
        doc, paths = execute(planned, progress=progress)
    except UsageError as error:
        print(f"mobster test scenario: {error}", file=err)
        return 3
    finally:
        progress.end()
    if getattr(args, "json", False):
        print(json.dumps(doc, ensure_ascii=False, allow_nan=False), file=out)
    else:
        print(summary_text(doc, paths), file=out)
    return int(doc.get("exitCode", 3))

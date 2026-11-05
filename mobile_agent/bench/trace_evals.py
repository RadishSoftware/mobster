"""The local trace evals: runs kept as regression tasks, replayed offline against today's code. No phone, no network,
no model.

    python -m mobile_agent.bench evals                      the app's eval set, beside its journal
    python -m mobile_agent.bench evals --dir DIR --json     another set, one JSON object a task

A task is a folder in the eval set (`mobster history --to-eval ID` and the app's Add to evals write it):

    <id>/task.json        {"format", "id", "added", "goal", "app", "source": {"run", "version"},
                           "recorded": {"status", "outcome", "steps", "actions", "costUsd", "failure"},
                           "expected": {"status", "replay"}}
    <id>/tape.jsonl.gz    the run's tape (bench/tape.py), masked

``expected.status`` is the outcome a fixed engine should reach: "completed" for a task that failed. Only a live run
can show it, so the offline check asks something smaller, ``expected.replay``:

- ``changes``: the task failed. Today's code replaying it either acts the same way (**open**: the failure is still
  there) or acts differently at some step (**changed**: a candidate fix, to be confirmed by a paired live run).
- ``holds``: the task did not fail. Today's code must replay it with every action the same, or it is **broken**.

A task that can't be read or replayed is an **error**. The exit status is 1 when any task is broken or errors.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from . import tape

FORMAT = "mobster-trace-eval/1"
TASK, TAPE = "task.json", tape.NAME + ".gz"
REPLAYS = ("changes", "holds")
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,80}")


class BadTask(ValueError):
    pass


def read_task(folder):
    """The task in ``folder``, validated; BadTask says what is wrong with it."""
    folder = Path(folder)
    try:
        task = json.loads((folder / TASK).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BadTask(f"{folder.name}: no readable {TASK}") from None
    expected = task.get("expected") if isinstance(task, dict) else None
    if (not isinstance(task, dict) or task.get("format") != FORMAT or task.get("id") != folder.name
            or not ID.fullmatch(folder.name) or not isinstance(expected, dict)
            or expected.get("replay") not in REPLAYS or not isinstance(task.get("recorded"), dict)):
        raise BadTask(f"{folder.name}: {TASK} is not a {FORMAT} task for this folder")
    if not (folder / TAPE).is_file():
        raise BadTask(f"{folder.name}: no {TAPE}")
    return task


def judge(task, report):
    """(verdict, detail) for one replay ``report`` (tape.Report) of ``task``: holds, broken, open, changed or error."""
    if report.error:
        return "error", report.error
    where = ""
    if report.divergence:
        step = report.divergence
        where = f"at action {step['index'] + 1} (turn {step['step']}): {step['why']}"
    elif not report.identical:
        where = f"ends {report.replayed_status} where the recording ended {report.recorded_status}"
    if task["expected"]["replay"] == "holds":
        return ("holds", "every action the same") if report.identical else ("broken", where)
    return ("open", "acts as it did when it failed") if report.identical else ("changed", where)


def run(root):
    """[{id, verdict, detail, acts, seconds}] for every task folder under ``root`` (an unreadable one is an error
    row)."""
    root = Path(root)
    rows = []
    folders = root.iterdir() if root.is_dir() else ()
    for folder in sorted(p for p in folders if p.is_dir() and not p.name.startswith(".")):
        try:
            task = read_task(folder)
        except BadTask as error:
            rows.append({"id": folder.name, "verdict": "error", "detail": str(error), "acts": None, "seconds": 0})
            continue
        report = tape.replay(folder / TAPE)
        verdict, detail = judge(task, report)
        rows.append({"id": task["id"], "verdict": verdict, "detail": detail, "goal": task.get("goal"),
                     "acts": f"{report.replayed_acts}/{report.recorded_acts}", "seconds": report.seconds,
                     "expected": task["expected"]["replay"]})
    return rows


def add_arguments(parser):
    parser.add_argument("--dir", type=Path, help="the eval set's folder (default: the app's, beside its journal)")
    parser.add_argument("--json", action="store_true", help="one JSON object a task")


def command(args):
    root = args.dir
    if root is None:
        from ..paths import default_history_db
        root = default_history_db().parent / "evals"
    rows = run(root)
    if not rows:
        print(f"No trace evals in {root}. Add a finished task with `mobster history --to-eval ID`.", file=sys.stderr)
        return 2
    for row in rows:
        if args.json:
            print(json.dumps(row, ensure_ascii=False))
        else:
            acts = f"{row['acts']} acts" if row["acts"] else "-"
            print(f"{row['id'][:48]:48}  {row['verdict']:7}  {acts:>11}  {row['seconds']:5.2f}s  {row['detail']}")
    counts = {verdict: sum(row["verdict"] == verdict for row in rows) for verdict in
              ("open", "changed", "holds", "broken", "error")}
    if not args.json:
        print(f"\n{len(rows)} tasks: " + ", ".join(f"{n} {name}" for name, n in counts.items() if n))
    return 1 if counts["broken"] or counts["error"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench evals", description=__doc__.split("\n")[0])
    add_arguments(parser)
    return command(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

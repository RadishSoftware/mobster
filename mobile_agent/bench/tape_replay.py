"""Replay recorded runs on today's code: every action the same, or the first one that isn't. $0, no simulator.

    python -m mobile_agent.bench.tape_replay research/iosworld-runs/RUN ...     every tape.jsonl under each folder
    python -m mobile_agent.bench.tape_replay mobster-trace-3f9c2a1b7d10.zip  a trace bundle (mobster export ID --trace)
    python -m mobile_agent.bench.tape_replay DIR --steps                          each recorded action, same or differs
    python -m mobile_agent.bench.tape_replay DIR --json                           one JSON report per tape

Each recorded run (bench/iosworld.py writes tape.jsonl beside prompts.jsonl) is run again through FrontierAgent with
the recorded phone and model answering (tape.py). A run is **identical** when the replay sends the same actions in the
same order, with the same targets and text, and ends with the same status. A **divergence** names the first action
that differs and the turn it came in: the place a change to the loop, its gates or its target resolution would act
differently on that run. **Prompts same** counts the model calls whose prompt text is byte for byte the recording's.

The exit status is 1 when any tape diverges or errors, so a gate can run it.
"""

import argparse
import json
import sys
from pathlib import Path

from . import tape


def tapes(paths):
    found = []
    for path in map(Path, paths):
        names = (tape.NAME, tape.NAME + ".gz")
        if path.is_file():
            found.append(path)
        elif any((path / name).is_file() for name in names):
            found.append(next(path / name for name in names if (path / name).is_file()))
        else:
            # Run folders hold tape.jsonl(.gz); a folder of saved tapes (tests/fixtures/tapes) holds NAME.jsonl.gz.
            found.extend(sorted(p for name in names for p in path.rglob(name))
                         or sorted([*path.glob("*.jsonl.gz"), *path.glob("*.zip")]))
    return found


def line(report):
    path = Path(report.tape)
    where = path.parent.name if path.name.startswith(tape.NAME) else path.name.split(".")[0]
    if report.error:
        verdict = f"error: {report.error[:90]}"
    elif report.divergence:
        d = report.divergence
        verdict = (f"DIVERGES at action {d['index']} (turn {d['step']}): {d['why']}; recorded "
                   f"{json.dumps(d['expected'])[:80]}, now {json.dumps(d['got'])[:80]}")
    elif report.identical:
        verdict = "identical"
    else:
        verdict = f"ends differently: {report.recorded_status} -> {report.replayed_status}"
    return (f"{where[:40]:40}  {report.replayed_acts:3}/{report.recorded_acts:<3} acts  "
            f"prompts {report.prompts_same}/{report.prompts_seen}  {report.seconds:5.2f}s  {verdict}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.tape_replay",
                                     description=__doc__.split("\n")[0])
    parser.add_argument("paths", nargs="+",
                        help="tape.jsonl files, trace bundles (.zip), or folders to search for them")
    parser.add_argument("--json", action="store_true", help="one JSON report a line")
    parser.add_argument("--steps", action="store_true", help="under each run, every recorded action: same, or differs")
    args = parser.parse_args(argv)
    found = tapes(args.paths)
    if not found:
        print("No tape.jsonl or tape.jsonl.gz under " + ", ".join(args.paths), file=sys.stderr)
        return 2
    reports = [tape.replay(path) for path in found]
    for report in reports:
        print(json.dumps(report.public(), ensure_ascii=False) if args.json else line(report))
        if args.steps and not args.json:
            if report.hooks:
                print(f"    the live run also had {', '.join(report.hooks)}; a replay has none of them")
            for step in report.steps:
                print(f"    {step['index'] + 1:3}. turn {step['turn'] or '?':>3}  {step['act'][:70]:70}  {step['now']}")
    same = sum(r.identical for r in reports)
    acts = sum(r.recorded_acts for r in reports)
    replayed = sum(min(r.replayed_acts, r.recorded_acts) for r in reports)
    prompts = sum(r.prompts_same for r in reports), sum(r.prompts_seen for r in reports)
    if not args.json:
        print(f"\n{same} of {len(reports)} runs identical; {replayed} of {acts} recorded actions replayed; "
              f"prompts byte for byte the same: {prompts[0]} of {prompts[1]}")
    return 0 if same == len(reports) else 1


if __name__ == "__main__":
    sys.exit(main())

"""Systematic failure analysis over recorded iOSWorld runs: $0, seconds, no simulator.

Every frontier turn in every recorded run is classified by what it bought:

    chain_N         executed N >= 2 actions
    single          executed one action that changed the screen
    no_change       executed actions, none changed the screen
    refused:<why>   the code refused it (loop, repeat, idle_wait, an unrequested act)
    failed:<kind>   the driver raised (TypeError-like classes grouped by message prefix)
    missing         the target was not on screen
    wait            a WAIT

and the causes are ranked by the turns they cost across tasks, with examples, so a fix goes
where the turns go. Judge verdicts add which rubric criteria fail most often.

    python -m mobile_agent.bench.diagnose --latest research/iosworld-runs/*
"""

import argparse
import collections
import glob
import json
import os
import re

from .iosworld_stats import official_pass


def _kind(error):
    text = error or ""
    text = re.sub(r"'[^']*'", "'…'", text)
    return text[:70]


def classify(events):
    """[(turn, category, detail)] for one run's events."""
    turns = []
    by_step = collections.defaultdict(list)
    for event in events:
        if event.get("event", "").startswith("frontier_") and "step" in event:
            by_step[event["step"]].append(event)
    for step in sorted(by_step):
        group = by_step[step]
        decision = next((e for e in group if e["event"] == "frontier_decision"), None)
        actions = [e for e in group if e["event"] == "frontier_action"]
        refused = [e for e in group if e["event"] == "frontier_refused"]
        failed = [e for e in group if e["event"] == "frontier_failed"]
        stops = [e for e in group if e["event"] == "frontier_chunk_stop"]
        ops = (decision or {}).get("ops") or []
        label = (decision or {}).get("target_label")
        if len(actions) >= 2:
            turns.append((step, f"chain_{min(len(actions), 4)}", None))
        elif actions:
            if any(a.get("changed") for a in actions):
                turns.append((step, "single", None))
            else:
                turns.append((step, "no_change", f"{actions[0].get('operation')} {actions[0].get('target_label')!r}"))
        elif refused:
            why = refused[0].get("label") or "?"
            why = why if why in ("loop", "repeat", "idle_wait", "dismiss_exhausted") else "unrequested_act"
            turns.append((step, f"refused:{why}", refused[0].get("label")))
        elif failed:
            turns.append((step, f"failed:{_kind(failed[0].get('error'))}", f"{failed[0].get('operation')}"))
        elif ops == ["WAIT"]:
            turns.append((step, "wait", None))
        elif stops and stops[0].get("reason") == "target_missing":
            turns.append((step, "missing", stops[0].get("wanted") or label))
        elif ops and ops[-1] in ("DONE", "BLOCKED"):
            turns.append((step, "finish", None))
        else:
            turns.append((step, "other", f"{ops} {label!r}"))
    return turns


def load(paths, latest=False):
    """[(run name, task record)] of frontier runs; ``latest``: only each task's newest run, so the
    ranking reflects the current code rather than failures fixed since."""
    runs = []
    for path in paths:
        for task_json in sorted(glob.glob(os.path.join(path, "*", "task.json"))):
            try:
                record = json.load(open(task_json, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (record.get("mobster") or {}).get("policy") != "frontier" and not any(
                    e.get("event", "").startswith("frontier_") for e in (record.get("mobster") or {}).get("events", [])):
                continue
            runs.append((os.path.basename(os.path.dirname(os.path.dirname(task_json))), record,
                         os.path.getmtime(task_json)))
    if latest:
        newest = {}
        for run in runs:
            if run[1].get("task") not in newest or run[2] > newest[run[1].get("task")][2]:
                newest[run[1].get("task")] = run
        runs = list(newest.values())
    return [(name, record) for name, record, _ in runs]


WASTE = ("no_change", "refused:", "failed:", "missing", "wait", "other")


def report(runs, top=12):
    turns_total = collections.Counter()
    cause_turns = collections.Counter()
    cause_tasks = collections.defaultdict(set)
    examples = collections.defaultdict(list)
    no_change_targets = collections.Counter()
    status = collections.Counter()
    rubric_fail = collections.Counter()
    succeeded = 0
    for run, record in runs:
        mobster = record["mobster"]
        status[mobster.get("status")] += 1
        evaluation = record.get("evaluation") or {}
        succeeded += official_pass(evaluation) if evaluation else False
        for criterion in evaluation.get("rubric_results") or ():
            if not criterion.get("satisfied"):
                words = re.findall(r"[a-z]+", criterion.get("criterion", "").casefold())
                rubric_fail[" ".join(w for w in words[:3])] += 1
        for step, category, detail in classify(mobster.get("events") or []):
            turns_total[category.split(":")[0] if category.startswith(("refused", "failed")) else category] += 1
            if category.startswith(WASTE):
                cause_turns[category] += 1
                cause_tasks[category].add(record["task"])
                if detail and len(examples[category]) < 3 and detail not in examples[category]:
                    examples[category].append(detail)
                if category == "no_change":
                    no_change_targets[(detail or "").split(" ")[0]] += 1
    all_turns = sum(turns_total.values())
    wasted = sum(cause_turns.values())
    print(f"{len(runs)} runs, {succeeded} passed (iOSWorld's rule: every rubric criterion); statuses {dict(status)}")
    print(f"{all_turns} model turns; {wasted} ({100 * wasted / max(1, all_turns):.0f}%) bought no progress\n")
    print("turns by outcome:", ", ".join(f"{k} {v}" for k, v in turns_total.most_common()))
    print("\nwasted turns by cause (turns, tasks, examples):")
    for cause, count in cause_turns.most_common(top):
        print(f"  {count:4}  {len(cause_tasks[cause]):3} tasks  {cause}")
        for example in examples[cause]:
            print(f"               e.g. {str(example)[:110]}")
    if no_change_targets:
        print("\nno-change actions by operation:", dict(no_change_targets.most_common(6)))
    if rubric_fail:
        print("\nmost-failed rubric criteria (first words):")
        for words, count in rubric_fail.most_common(8):
            print(f"  {count:3}  {words}")


def task_trails(runs, least=3):
    """Per task, its wasted turns in order ("12:NC TAP 'Archive'"): where a run went in circles."""
    for name, record in sorted(runs, key=lambda run: run[1].get("task", "")):
        events = record["mobster"].get("events") or []
        decisions = {e["step"]: e for e in events if e.get("event") == "frontier_decision"}
        trail = []
        for step, category, detail in classify(events):
            if category.startswith(WASTE):
                decision = decisions.get(step, {})
                what = detail if category == "no_change" else f"{decision.get('operation')} {decision.get('target_label')!r}"
                trail.append(f"{step}:{category.split(':')[-1]} {str(what)[:40]}")
        if len(trail) >= least:
            evaluation = record.get("evaluation")
            passed = official_pass(evaluation) if evaluation else None
            print(f"{record.get('task', '?'):16} {name:12} passed={passed}  " + "; ".join(trail))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--latest", action="store_true", help="only each task's newest run")
    parser.add_argument("--tasks", action="store_true", help="also each task's trail of wasted turns")
    args = parser.parse_args(argv)
    runs = load(args.runs, latest=args.latest)
    report(runs, top=args.top)
    if args.tasks:
        print()
        task_trails(runs)


if __name__ == "__main__":
    main()

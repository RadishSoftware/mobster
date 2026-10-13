"""Heats: every engine variant on the same hard task, back to back, each stopped at a checkpoint, scored by progress.

  python -m mobile_agent.bench.heat run --agents mobster-smart,mobster-smart-haiku+jev+bold --cap 120 --out DIR
  python -m mobster_agent.bench.heat score DIR

A run does not have to finish to show which engine is better (owner, 9 Oct). Each task of the hard set
(heldout.build_hard) carries milestones: a text that must appear on a screen of a named app ("4817" in Notes,
"Room 42" in Calendar, "202,440" in Calculator). Every variant runs the task once, in a shuffled order per task, with
its time budget cut to ``--cap`` seconds (the checkpoint); the screens it saw (frontier_screen events) are matched
against the milestones afterwards.

Score per attempt: milestones reached (fraction), seconds to each, whether it finished and passed, model calls and $.
Per variant: mean progress, passes, mean seconds per milestone reached, $ per attempt. One phone runs one variant at a
time, so a heat is sequential; the order is shuffled per task so no variant always goes first.
"""

import argparse
import dataclasses
import json
import re
import statistics
import sys
from pathlib import Path

from .heldout import build_hard, milestones


def _plain(text):
    return " ".join(str(text or "").casefold().split())


def reached(task, events):
    """{milestone index: seconds since start} for the milestones a run's screens showed."""
    wanted = milestones(task)
    found = {}
    for event in events:
        if event.get("event") != "frontier_screen":
            continue
        rows, app = _plain(event.get("rows")), event.get("app") or ""
        for index, (where, text) in enumerate(wanted):
            if index in found or (where and where.casefold() != app.casefold()):
                continue
            needle = _plain(text)
            if needle in rows or needle.replace(",", "") in rows.replace(",", ""):
                found[index] = round(event.get("t_ms", 0) / 1000, 1)
    return found


def load_events(folder):
    path = Path(folder) / "events.jsonl"
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def score(run_dir):
    run = Path(run_dir)
    tasks = {t.id: t for t in build_hard()}
    rows = []
    for line in (run / "records.jsonl").read_text().splitlines():
        record = json.loads(line)
        task = tasks.get(record.get("task"))
        if task is None or record.get("verdict") in ("infra", "skipped"):
            continue
        folder = run / "artifacts" / re.sub(r"[^\w.-]", "_", record["key"])
        found = reached(task, load_events(folder))
        total = len(milestones(task))
        rows.append({"task": task.id, "agent": record["agent"], "verdict": record["verdict"],
                     "status": record.get("status"), "progress": len(found) / total if total else 0.0,
                     "milestones": f"{len(found)}/{total}", "times": found,
                     "s": round((record.get("agent_ms") or 0) / 1000, 1), "calls": record.get("model_calls"),
                     "usd": record.get("cost_usd")})
    by_agent = {}
    for row in rows:
        by_agent.setdefault(row["agent"], []).append(row)
    summary = {}
    for agent, items in by_agent.items():
        times = [t for row in items for t in row["times"].values()]
        summary[agent] = {"attempts": len(items), "progress": round(statistics.mean(r["progress"] for r in items), 3),
                          "passes": sum(r["verdict"] == "pass" for r in items),
                          "s_per_milestone": round(statistics.mean(times), 1) if times else None,
                          "s_attempt": round(statistics.mean(r["s"] for r in items), 1),
                          "calls": round(statistics.mean(r["calls"] or 0 for r in items), 1),
                          "usd": round(statistics.mean(r["usd"] or 0 for r in items), 4)}
    return rows, summary


def render(rows, summary):
    lines = ["| variant | attempts | progress | passes | s to a milestone (mean) | s/attempt | calls | $/attempt |",
             "|---|---|---|---|---|---|---|---|"]
    for agent, s in sorted(summary.items(), key=lambda kv: -kv[1]["progress"]):
        lines.append(f"| {agent} | {s['attempts']} | {s['progress']:.0%} | {s['passes']} | {s['s_per_milestone']} | "
                     f"{s['s_attempt']} | {s['calls']} | {s['usd']:.4f} |")
    lines += ["", "| task | variant | milestones | at (s) | status | verdict | s |", "|---|---|---|---|---|---|---|"]
    for row in sorted(rows, key=lambda r: (r["task"], r["agent"])):
        lines.append(f"| {row['task']} | {row['agent']} | {row['milestones']} | "
                     f"{', '.join(str(v) for v in row['times'].values()) or '-'} | {row['status']} | "
                     f"{row['verdict']} | {row['s']} |")
    return "\n".join(lines)


def run_heat(args):
    from ..evals.oracles import WDAProbe
    from .agents import build_agents
    from .fixtures import Fixtures
    from .phone import Phone
    from .runner import RunHalted, Runner
    from .truth import TruthStore, fixture_verifier
    from ..config import load_env_file
    if args.secrets_env:
        load_env_file(args.secrets_env)
    if args.env_file:
        load_env_file(args.env_file)
    tasks = build_hard()
    if args.only:
        tasks = tuple(t for t in tasks if t.id in args.only.split(","))
    # The checkpoint: every variant gets the same cut budget.
    tasks = tuple(dataclasses.replace(t, seconds=min(t.max_seconds, args.cap)) for t in tasks)
    names = [n for n in args.agents.split(",") if n]
    fixtures = Fixtures()
    runner = Runner(tasks=tasks, agents=build_agents(names, env_file=args.env_file), out_dir=args.out,
                    wda_url=args.wda_url, device=args.device, truth=TruthStore(args.device), fixtures=fixtures,
                    repeats=args.repeats, seed=args.seed, yield_to=None, probe_factory=WDAProbe,
                    phone_factory=lambda url, session: Phone(url, session),
                    fixture_verifier_factory=lambda probe: fixture_verifier(probe, fixtures))
    import hashlib
    digest = hashlib.sha256(json.dumps([t.id for t in tasks] + [args.cap]).encode()).hexdigest()
    try:
        runner.run(digest, header_extra={"heat": True, "cap_seconds": args.cap})
    except RunHalted as halt:
        print(f"HALTED: {halt}")
        return 2
    rows, summary = score(args.out)
    print(render(rows, summary))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.heat")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run")
    p.add_argument("--agents", required=True)
    p.add_argument("--cap", type=float, default=120)
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--seed", type=int, default=20261010)
    p.add_argument("--only")
    p.add_argument("--out", required=True)
    p.add_argument("--wda-url", default="http://127.0.0.1:8100")
    p.add_argument("--device", default="iphone15pro")
    p.add_argument("--env-file")
    p.add_argument("--secrets-env")
    p = sub.add_parser("score")
    p.add_argument("run")
    args = parser.parse_args(argv)
    if args.command == "score":
        rows, summary = score(args.run)
        print(render(rows, summary))
        return 0
    return run_heat(args)


if __name__ == "__main__":
    sys.exit(main())

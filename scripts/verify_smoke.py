#!/usr/bin/env python3
"""`mobster verify` on a real simulator, against Settings (com.apple.Preferences). Opt-in: it needs Xcode with an
iOS Simulator runtime, and the Smart leg needs an OpenAI or Anthropic key and spends up to --max-usd.

    python scripts/verify_smoke.py                          # the four key-less legs
    python scripts/verify_smoke.py --measure 5              # plus timings: warm verify and the evidence read
    python scripts/verify_smoke.py --smart --env-file mobile_agent/.env --ledger LEDGER.md

The legs, each checked for its exit code and its run folder (result.json, report.html, check.yaml and the frames,
including NN-verdict-ax.jpg):

1. launch-only `--text General`                           exit 0
2. `--text "Not a real row"`                              exit 1, with near misses
3. no expectations                                        exit 2
4. a bad regex                                            exit 3, class usage, no run folder
5. Smart: "Open General, then About", `--text "iOS Version"`   exit 0 at most --max-usd (0.15)

Set MOBSTER_DATA_DIR and MOBSTER_SIM_PORT_BASE to keep its simulators apart from others'. It never opens
Simulator.app, and it leaves the simulator it used booted for the next run (`mobster sim shutdown` stops it).
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = "com.apple.Preferences"


def verify(args, *extra, project):
    """One `mobster verify … --json` run: (exit code, result or None, stderr, seconds)."""
    command = [sys.executable, "-m", "mobile_agent", "verify", "--bundle", SETTINGS, "--json",
               "--out", str(project / ".mobster" / "runs"), *extra]
    env = {**os.environ, "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    started = time.monotonic()
    done = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True, timeout=900)
    seconds = time.monotonic() - started
    try:
        result = json.loads(done.stdout)
    except ValueError:
        result = None
    if args.verbose:
        sys.stderr.write(done.stderr)
    return done.returncode, result, done.stderr, seconds


def artifacts(result):
    """The run folder's files a verdict must leave, or what is missing."""
    run_dir = Path(result["run_dir"])
    missing = [name for name in ("result.json", "report.html", "check.yaml") if not (run_dir / name).is_file()]
    frames = sorted(p.name for p in (run_dir / "frames").glob("*.jpg"))
    if not any(name.endswith("-verdict-ax.jpg") for name in frames):
        missing.append("frames/NN-verdict-ax.jpg")
    if not any(name.endswith("-launch.jpg") for name in frames):
        missing.append("frames/01-launch.jpg")
    return missing


def leg(name, args, *extra, project, expect_code, check=None):
    code, result, stderr, seconds = verify(args, *extra, project=project)
    problems = []
    if code != expect_code:
        problems.append(f"exit {code}, expected {expect_code}")
    if result is None:
        problems.append("stdout was not one JSON object")
    elif result.get("run_dir"):
        problems += [f"missing {item}" for item in artifacts(result)]
    if result is not None and check is not None:
        problems += check(result)
    line = {"leg": name, "ok": not problems, "exit": code, "seconds": round(seconds, 1),
            "verdict": (result or {}).get("verdict"), "summary": (result or {}).get("summary"),
            "timing": (result or {}).get("timing"), "cost_usd": (result or {}).get("cost_usd"),
            "run_id": (result or {}).get("run_id"), "problems": problems}
    print(("✓ " if not problems else "✗ ") + f"{name}: exit {code} in {seconds:.1f} s, "
          f"{line['verdict']}: {line['summary']}" + (f"  PROBLEMS: {problems}" if problems else ""), flush=True)
    return line


def measure(args, project):
    """Warm launch-only runs (5 expectations) and the evidence read's p50 on Settings' first screen."""
    rows = []
    for _ in range(args.measure):
        code, result, _, seconds = verify(args, "--text", "General", "--text", "Accessibility",
                                          "--visible", "label=General,role=button", "--no-text", "Not a real row",
                                          "--expect", '{"count": {"role": "cell"}, "at_least": 5}', project=project)
        rows.append({"wall": round(seconds, 2), "verdict": result and result["verdict"],
                     "timing": result and result["timing"]})
    phases = {}
    for key in ("simulator", "install", "launch", "assert", "report"):
        values = [row["timing"].get(key, 0) for row in rows if row["timing"]]
        phases[key] = round(statistics.median(values), 2) if values else None
    walls = [row["wall"] for row in rows]
    sys.path.insert(0, str(ROOT))
    from mobile_agent.compose import build_target_driver
    from mobile_agent.sim import SimulatorManager
    from mobile_agent.verify.runner import wda_session
    from mobile_agent.verify.tree import read_tree
    reads, shown = [], None
    with SimulatorManager().acquire() as target:
        driver = build_target_driver(wda_url=target.wda_url, session=wda_session(target.wda_url))
        try:
            for _ in range(args.reads):
                started = time.monotonic()
                tree = read_tree(driver)
                reads.append((time.monotonic() - started) * 1000)
                shown = len([n for n in tree.shown() if n.label or n.identifier])
        finally:
            driver.close()
    summary = {"warm_verify": {"n": len(walls), "p50_s": round(statistics.median(walls), 2), "min_s": min(walls),
                               "max_s": max(walls), "phases_p50_s": phases, "runs": rows},
               "evidence_read": {"n": len(reads), "p50_ms": round(statistics.median(reads)),
                                 "min_ms": round(min(reads)), "max_ms": round(max(reads)),
                                 "labelled_elements_shown": shown}}
    print(json.dumps(summary, indent=1), flush=True)
    return summary


def ledger_row(path, result, command):
    """Append the Smart run to the ledger: date, workstream, command, run_id, cost_usd, running total."""
    path = Path(path)
    text = path.read_text()
    total = 0.0
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 6 and cells[0][:4].isdigit():
            try:
                total = float(cells[5].lstrip("$"))
            except ValueError:
                pass
    cost = float(result.get("cost_usd") or 0)
    row = (f"| {time.strftime('%Y-%m-%d')} | verify | `{command}` | {result.get('run_id')} | ${cost:.4f} "
           f"| ${total + cost:.4f} |")
    marker = "## Claude Code and Codex sessions"
    head, sep, tail = text.partition(marker)
    head = head.rstrip("\n") + "\n" + row + "\n\n"
    path.write_text(head + sep + tail if sep else head)
    print(f"ledger: {row}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--smart", action="store_true", help="run the Smart leg (spends up to --max-usd)")
    parser.add_argument("--env-file", help="the env file with OPENAI_API_KEY or ANTHROPIC_API_KEY, for the Smart leg")
    parser.add_argument("--max-usd", default="0.15", help="the Smart leg's spend cap (default: 0.15)")
    parser.add_argument("--ledger", help="append the Smart run's cost to this ledger (LEDGER.md)")
    parser.add_argument("--measure", type=int, default=0, metavar="N", help="also time N warm verify runs")
    parser.add_argument("--reads", type=int, default=20, metavar="N", help="evidence reads to time (default: 20)")
    parser.add_argument("--project", help="the project folder runs go to (default: a new temporary one)")
    parser.add_argument("--verbose", action="store_true", help="show each run's progress lines")
    args = parser.parse_args(argv)
    project = Path(args.project or tempfile.mkdtemp(prefix="mobster-verify-smoke-")).resolve()
    project.mkdir(parents=True, exist_ok=True)
    print(f"project: {project}", flush=True)

    def near_misses(result):
        observed = result["assertions"][0]["observed"] if result.get("assertions") else ""
        return [] if "closest:" in observed else [f"no near misses in {observed!r}"]

    def no_run_folder(result):
        problems = []
        if result.get("run_dir") is not None:
            problems.append("a usage error made a run folder")
        if (result.get("reason") or {}).get("class") != "usage":
            problems.append(f"class {(result.get('reason') or {}).get('class')}, expected usage")
        return problems

    legs = [
        leg("launch-only --text General", args, "--text", "General", project=project, expect_code=0),
        leg('--text "Not a real row"', args, "--text", "Not a real row", project=project, expect_code=1,
            check=near_misses),
        leg("no expectations", args, project=project, expect_code=2),
        leg("a bad regex", args, "--text", "/[/", project=project, expect_code=3, check=no_run_folder),
    ]
    summary = {"legs": legs}
    if args.smart:
        if not args.env_file:
            parser.error("--smart needs --env-file")
        extra = ["Open General, then About", "--text", "iOS Version", "--max-usd", args.max_usd,
                 "--env-file", args.env_file]
        smart = leg("Smart: Open General, then About", args, *extra, project=project, expect_code=0,
                    check=lambda result: [] if (result.get("cost_usd") or 0) <= float(args.max_usd) else [
                        f"cost {result.get('cost_usd')} over {args.max_usd}"])
        legs.append(smart)
        if args.ledger and smart.get("run_id"):
            result = json.loads((project / ".mobster" / "runs" / smart["run_id"] / "result.json").read_text())
            ledger_row(args.ledger, result, 'mobster verify "Open General, then About" --bundle '
                                            'com.apple.Preferences --text "iOS Version" --max-usd ' + args.max_usd)
    if args.measure:
        summary["measure"] = measure(args, project)
    print(json.dumps({"ok": all(item["ok"] for item in legs), "legs": [
        {k: v for k, v in item.items() if k != "timing"} | {"timing": item["timing"]} for item in legs]}, indent=1))
    return 0 if all(item["ok"] for item in legs) else 1


if __name__ == "__main__":
    sys.exit(main())

"""Latency report: per-phase p50/p90/p99, the critical path, and overlap, across runs.

Reads run journals (``mobster.sqlite3``, opened read-only) and eval JSONL
records. A run that carries a ``latency_trace`` event (every run since
2026-09-24) is reported from its spans. Older runs are attributed from their
event timestamps, the method of the 23 Sep 2026 latency breakdown:
every millisecond between two consecutive events goes to one phase; while a
model call is in flight the interval is that call.

Usage:
  mobile_agent/.venv/bin/python -m mobile_agent.evals.latency_report \\
      [--db path/mobster.sqlite3 ...] [--jsonl research/run.jsonl ...] [--since 2026-09-24] \\
      [--goal substring] [--json]

With no --db/--jsonl it reads the desktop app's state journals.
"""

import argparse
from datetime import datetime
import glob
import json
import os
import sqlite3
import statistics

DEFAULT_DBS = os.path.expanduser("~/Library/Application Support/app.mobster.desktop/state/*.sqlite3")

# Legacy attribution: which model call wins an interval when several overlap.
CALL_PRIORITY = ["decision", "action_verification", "text", "recovery", "planning", "extraction",
                 "verification"]


def quantile(values, q):
    values = sorted(values)
    if not values:
        return None
    k = (len(values) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "mean": round(statistics.mean(values), 1), "p50": round(quantile(values, .5), 1),
            "p90": round(quantile(values, .9), 1), "p99": round(quantile(values, .99), 1),
            "max": round(max(values), 1)}


# -- loading -----------------------------------------------------------------------------

def load_journal(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        meta = {rid: json.loads(data) for rid, data in db.execute("select id, data from runs")}
        events = {}
        for rid, data in db.execute("select run_id, data from events order by run_id, seq"):
            events.setdefault(rid, []).append(json.loads(data))
    finally:
        db.close()
    runs = []
    for rid, run_events in events.items():
        info = meta.get(rid) or {}
        runs.append({"id": rid, "source": path, "goal": info.get("goal") or "", "app": info.get("appName"),
                     "created_ms": info.get("createdAt") or 0, "status": info.get("status"),
                     "events": run_events})
    return runs


def load_jsonl(path):
    runs = []
    with open(path) as stream:
        for index, line in enumerate(stream):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            trace = (record.get("metrics") or {}).get("latency_trace")
            if trace is None:
                continue
            runs.append({"id": record.get("run_id") or f"{os.path.basename(path)}:{index}", "source": path,
                         "goal": record.get("task") or "", "app": None, "created_ms": 0,
                         "status": record.get("status"), "trace": trace, "task": record.get("task"),
                         "passed": record.get("passed")})
    return runs


# -- per-run phases ----------------------------------------------------------------------

def traced(run):
    """(wall_ms, phases [(name, start, dur)], calls [(purpose, lane, start, dur, attrs)], marks) or None."""
    trace = run.get("trace")
    if trace is None:
        trace = next((e for e in run.get("events", ()) if e.get("event") == "latency_trace"), None)
    if trace is None:
        return None
    phases = [(item[0], item[1], item[2]) for item in trace.get("phases", ())]
    calls = [tuple(item[:4]) + ((item[4] if len(item) > 4 else {}),) for item in trace.get("calls", ())]
    marks = [(item[0], item[1], item[2] if len(item) > 2 else {}) for item in trace.get("marks", ())]
    return trace.get("wall_ms") or 0, phases, calls, marks


def _legacy_label(prev, nxt, ctx):
    p, n = prev["event"], nxt["event"]
    if p == "run_started":
        return "setup"
    if p == "app_launch_started":
        return "launch.activate"
    if p == "app_launch_acknowledged":
        return "observe.first"
    if p == "observation_after_action" and n == "observation":
        return "observe.ready"
    if p == "observation" and n in ("inference_started", "decision"):
        return "cpu.decide_prep"
    if p == "decision" and n in ("action_started", "inference_started"):
        return "observe.completion" if ctx.get("last_op") == "DONE" else "observe.refresh"
    if p == "decision" and n == "completion_check":
        return "observe.completion"
    if p == "decision" and n == "observation":
        return "wait.backoff"
    if p == "action_check" and n in ("action_started", "observation", "stale_decision"):
        return "observe.refresh"
    if p == "action_started" and n == "action_acknowledged":
        return "dispatch." + str(ctx.get("op"))
    if p == "action_acknowledged" and n == "observation_after_action":
        return "settle." + str(ctx.get("op"))
    if p == "completion_check":
        return "extract.wait_prefetch"
    if p == "result" and n in ("run_finished", "latency_trace"):
        return "finish"
    if p == "stale_decision" or n == "stale_decision" or p == "refresh_target_stable":
        return "observe.refresh"
    if p == "helper" and n == "observation":
        return "observe.ready"
    return "other"


def legacy(run):
    """Phases attributed from event timestamps for a run recorded before tracing."""
    events = run["events"]
    if len(events) < 2:
        return None
    ctx, active, spans = {}, {}, []
    calls, open_calls = [], {}
    first = events[0]["timestamp"]
    for index, event in enumerate(events):
        kind = event["event"]
        if kind == "inference_started":
            active[event.get("call_id")] = event.get("purpose")
            open_calls[event.get("call_id")] = event["timestamp"]
        elif kind == "inference_finished":
            active.pop(event.get("call_id"), None)
            began = open_calls.pop(event.get("call_id"), None)
            if began is not None:
                calls.append((event.get("purpose"), "main", began - first, event.get("latency_ms") or 0,
                              {"ok": event.get("success")}))
        elif kind == "action_started":
            ctx["op"] = event.get("operation")
        elif kind == "decision":
            ctx["last_op"] = event.get("operation")
        if index + 1 == len(events):
            break
        nxt = events[index + 1]
        duration = nxt["timestamp"] - event["timestamp"]
        if active:
            purposes = sorted(set(active.values()), key=lambda p: CALL_PRIORITY.index(p) if p in CALL_PRIORITY else 99)
            purpose = purposes[0]
            name = {"decision": "decide", "action_verification": "verify.action", "text": "helper.text",
                    "recovery": "helper.recovery", "extraction": "extract.select",
                    "verification": "extract.verify"}.get(purpose, "model." + str(purpose))
        else:
            name = _legacy_label(event, nxt, ctx)
        spans.append((name, event["timestamp"] - first, duration))
    wall = events[-1]["timestamp"] - first
    return wall, spans, calls, []


def family(name):
    return name.split(".", 1)[0]


# -- report ------------------------------------------------------------------------------

def analyze(runs):
    per_phase, per_family_run, walls = {}, {}, []
    calls_by_purpose, off_lane, overlaps, unattributed = {}, [], [], []
    marks, reused = {}, []
    first_decision, later_decision = [], []
    example = None
    rows = []
    for run in runs:
        parsed = traced(run)
        kind = "trace"
        if parsed is None:
            parsed = legacy(run) if run.get("events") else None
            kind = "legacy"
        if parsed is None:
            continue
        wall, phases, calls, run_marks = parsed
        walls.append(wall)
        sums = {}
        for name, _, duration in phases:
            per_phase.setdefault(name, []).append(duration)
            sums[family(name)] = sums.get(family(name), 0) + duration
        attributed = sum(duration for _, _, duration in phases)
        sums["unattributed"] = max(0.0, wall - attributed)
        unattributed.append(sums["unattributed"])
        for key, value in sums.items():
            per_family_run.setdefault(key, []).append(value)
        decision_seen = False
        for purpose, lane, start, duration, attrs in sorted(calls, key=lambda c: c[2]):
            calls_by_purpose.setdefault((purpose, lane), []).append(duration)
            if attrs.get("reused") is not None:
                reused.append(bool(attrs["reused"]))
            if purpose == "decision" and lane in ("main", "prediction", "speculation") and not attrs.get("hedge"):
                (later_decision if decision_seen else first_decision).append(duration)
                decision_seen = True
        off = sum(duration for _, lane, _, duration, _ in calls if lane != "main")
        waited = sum(duration for name, _, duration in phases if ".wait" in name)
        off_lane.append(off)
        overlaps.append(max(0.0, off - waited))
        for name, _, _ in run_marks:
            marks[name] = marks.get(name, 0) + 1
        rows.append({"id": run["id"], "kind": kind, "wall": wall, "phases": phases, "goal": run.get("goal")})
    if not walls:
        return {"runs": 0}
    median_wall = quantile(walls, .5)
    example = min(rows, key=lambda row: abs(row["wall"] - median_wall))
    total_wall = sum(walls)
    families = sorted(per_family_run, key=lambda key: -sum(per_family_run[key]))
    return {
        "runs": len(walls),
        "traced_runs": sum(1 for row in rows if row["kind"] == "trace"),
        "wall_ms": stats(walls),
        "critical_path": [{"phase": key, "share": round(100 * sum(per_family_run[key]) / total_wall, 1),
                           "per_run_mean_ms": round(sum(per_family_run[key]) / len(walls), 1),
                           "per_run": stats(per_family_run[key] + [0] * (len(walls) - len(per_family_run[key])))}
                          for key in families],
        "phases": {name: {**stats(values), "total_s": round(sum(values) / 1000, 1)}
                   for name, values in sorted(per_phase.items(), key=lambda kv: -sum(kv[1]))},
        "calls": {f"{purpose}@{lane}": stats(values) for (purpose, lane), values in sorted(calls_by_purpose.items())},
        "first_decision_ms": stats(first_decision), "later_decision_ms": stats(later_decision),
        "connection_reuse_rate": round(sum(reused) / len(reused), 3) if reused else None,
        "overlap": {"off_lane_model_ms_per_run": stats(off_lane), "hidden_ms_per_run": stats(overlaps)},
        "unattributed_ms_per_run": stats(unattributed),
        "marks": marks,
        "median_run": {"id": example["id"], "goal": (example["goal"] or "")[:80], "wall_ms": round(example["wall"], 1),
                       "path": [[name, round(duration, 1)] for name, _, duration in example["phases"]
                                if duration >= 1]},
    }


def print_report(report):
    if not report.get("runs"):
        print("No runs.")
        return
    wall = report["wall_ms"]
    print(f"{report['runs']} runs ({report['traced_runs']} traced). Wall ms: mean {wall['mean']}  p50 {wall['p50']}"
          f"  p90 {wall['p90']}  p99 {wall['p99']}")
    print("\nCritical path (main lane), share of wall-clock and per-run ms:")
    print(f"  {'phase':24} {'share':>6} {'mean':>8} {'p50':>8} {'p90':>8} {'p99':>8}")
    for row in report["critical_path"]:
        s = row["per_run"]
        print(f"  {row['phase']:24} {row['share']:5.1f}% {row['per_run_mean_ms']:8.0f} {s.get('p50', 0):8.0f}"
              f" {s.get('p90', 0):8.0f} {s.get('p99', 0):8.0f}")
    print("\nPer phase occurrence (ms):")
    print(f"  {'phase':28} {'n':>5} {'p50':>8} {'p90':>8} {'p99':>8} {'total s':>8}")
    for name, s in list(report["phases"].items())[:30]:
        print(f"  {name:28} {s['n']:5} {s['p50']:8.0f} {s['p90']:8.0f} {s['p99']:8.0f} {s['total_s']:8.1f}")
    print("\nModel calls by purpose@lane (ms):")
    for name, s in report["calls"].items():
        print(f"  {name:36} n={s['n']:<5} p50 {s['p50']:7.0f}  p90 {s['p90']:7.0f}  p99 {s['p99']:7.0f}")
    fd, ld = report["first_decision_ms"], report["later_decision_ms"]
    if fd.get("n"):
        print(f"\nFirst decision of a run p50/p90 {fd['p50']:.0f}/{fd['p90']:.0f} ms; later "
              f"{ld.get('p50', 0):.0f}/{ld.get('p90', 0):.0f} ms. Connection reuse: {report['connection_reuse_rate']}")
    ov = report["overlap"]
    print(f"Overlap: off-lane model ms/run mean {ov['off_lane_model_ms_per_run'].get('mean')}, hidden ms/run mean "
          f"{ov['hidden_ms_per_run'].get('mean')}. Unattributed ms/run mean {report['unattributed_ms_per_run'].get('mean')}")
    if report["marks"]:
        print("Marks: " + ", ".join(f"{k}={v}" for k, v in sorted(report["marks"].items())))
    example = report["median_run"]
    print(f"\nMedian run {example['id']} ({example['wall_ms']:.0f} ms) {example['goal']!r}:")
    print("  " + " -> ".join(f"{name} {ms:.0f}" for name, ms in example["path"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", action="append", default=[], help="A mobster.sqlite3 journal (read-only); globs ok")
    parser.add_argument("--jsonl", action="append", default=[], help="Eval JSONL with metrics.latency_trace")
    parser.add_argument("--since", help="Only runs created on/after this date (YYYY-MM-DD)")
    parser.add_argument("--goal", help="Only runs whose goal contains this text")
    parser.add_argument("--traced-only", action="store_true", help="Skip runs without a latency trace")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    args = parser.parse_args(argv)
    paths = [p for pattern in (args.db or ([] if args.jsonl else [DEFAULT_DBS])) for p in sorted(glob.glob(pattern))]
    runs = []
    for path in paths:
        try:
            runs.extend(load_journal(path))
        except sqlite3.Error:
            continue
    for path in args.jsonl:
        runs.extend(load_jsonl(path))
    if args.since:
        cutoff = datetime.fromisoformat(args.since).timestamp() * 1000
        runs = [run for run in runs if not run["created_ms"] or run["created_ms"] >= cutoff]
    if args.goal:
        runs = [run for run in runs if args.goal.lower() in (run.get("goal") or "").lower()]
    if args.traced_only:
        runs = [run for run in runs if traced(run) is not None]
    report = analyze(runs)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)


if __name__ == "__main__":
    main()

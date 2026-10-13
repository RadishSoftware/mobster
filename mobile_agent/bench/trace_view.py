"""A run's steps as a table, for reading engine combinations by hand (TRACE-REVIEW.md).

  python -m mobile_agent.bench.trace_view RUN_DIR [--task ID]

Per attempt: one line per decision (planner call or executor step) with its latency and cost, the actions it
took and whether each changed the screen, refusals, chunk stops, executor surprises, watcher answers, and the end:
status, answer, verdict and the failing checks.
"""

import argparse
import json
import re
from pathlib import Path


def load(path):
    try:
        return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    except OSError:
        return []


def attempt_lines(folder, record):
    events = load(folder / "events.jsonl")
    prompts = {p.get("step"): p for p in load(folder / "prompts.jsonl")}
    lines = [f"## {record['task']} [{record['agent']}] verdict={record['verdict']} status={record.get('status')} "
             f"{(record.get('agent_ms') or 0) / 1000:.1f}s calls={record.get('model_calls')} "
             f"turns={record.get('decision_steps')} actions={record.get('action_count')} "
             f"${record.get('cost_usd') or 0:.4f} {record.get('error') or ''}"]
    pending_cost, pending_ms = 0.0, []
    for event in events:
        kind = event.get("event")
        if kind == "inference_finished":
            pending_cost += event.get("estimated_usd") or 0
            pending_ms.append(f"{event.get('purpose')}:{(event.get('latency_ms') or 0) / 1000:.2f}s")
        elif kind == "frontier_decision":
            out = (prompts.get(event.get("step")) or {}).get("out") or {}
            ops = " | ".join(f"{a.get('operation')} {a.get('target') or a.get('target_label') or a.get('app') or ''}"
                             + (f" {a.get('text')!r}" if a.get("text") else "") for a in out.get("actions") or ()) \
                if event.get("source") != "executor" else ",".join(event.get("ops") or ())
            lines.append(f"- t={event['t_ms'] / 1000:6.1f} step {event.get('step')} [{event.get('source') or 'planner'}] "
                         f"{', '.join(pending_ms)} ${pending_cost:.4f} :: {event.get('thought', '')[:90]!r} :: {ops[:160]}"
                         + (f" :: next={event.get('next')}" if event.get("next") else ""))
            pending_cost, pending_ms = 0.0, []
        elif kind == "frontier_action":
            lines.append(f"    {event.get('operation')} {str(event.get('target_label'))[:50]!r} "
                         f"changed={event.get('changed')} exec={event.get('exec_ms')}ms settle={event.get('settle_ms')}ms")
        elif kind in ("frontier_refused", "frontier_chunk_stop", "frontier_failed"):
            lines.append(f"    ! {kind} {event.get('label') or event.get('reason') or event.get('error') or ''} "
                         f"{str(event.get('wanted') or event.get('detail') or '')[:80]}")
        elif kind == "frontier_executor":
            lines.append(f"    > executor {event.get('do')} {event.get('hint')!r} how={event.get('how')} "
                         f"top={event.get('top')} left={event.get('left')}")
        elif kind == "frontier_watch":
            lines.append(f"    ~ watch expected={event.get('expected')} popup={event.get('popup')} ({event.get('expect')})")
        elif kind == "frontier_contract":
            lines.append(f"    contract: {event.get('items')}")
    summary = {}
    try:
        summary = json.loads((folder / "summary.json").read_text())
    except (OSError, ValueError):
        pass
    failing = "; ".join(c["detail"] for c in record.get("checks", []) if c.get("ok") is False)
    lines.append(f"- END answer={str(record.get('answer'))[:120]} outcome={summary.get('outcome')} "
                 f"full={str(summary.get('full_answer') or summary.get('answer'))[:160]!r} {failing}")
    if record.get("detail", {}).get("timing"):
        lines.append(f"- timing {record['detail']['timing']}")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.trace_view")
    parser.add_argument("run")
    parser.add_argument("--task")
    args = parser.parse_args(argv)
    run = Path(args.run)
    for record in load(run / "records.jsonl"):
        if args.task and record["task"] != args.task:
            continue
        folder = run / "artifacts" / re.sub(r"[^\w.-]", "_", record["key"])
        print("\n".join(attempt_lines(folder, record)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

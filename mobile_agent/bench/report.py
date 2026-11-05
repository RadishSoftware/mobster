"""Turn a run directory into research/bench-<date>.md, every number computed from records.jsonl."""

import json
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path

from .metrics import bootstrap_ci, flaky_tasks, pass_hat_k, percentile, wilson
from .runner import INFRA, SKIPPED, load_records
from .suite import CATEGORIES, build_suite

# Mobster's agent on its default model, listed first. Other "mobster-*" arms are its variants.
MOBSTER = "mobster-smart"


def _fmt_pct(value):
    return "–" if value is None or value != value else f"{100 * value:.0f}%"


def _fmt_s(ms):
    return "–" if ms is None else f"{ms / 1000:.1f}"


def _fmt_usd(value):
    return "–" if value is None else (f"${value:.4f}" if value < 0.1 else f"${value:.2f}")


def summarize(records):
    graded = [r for r in records if r.get("verdict") in ("pass", "fail")]
    by_agent = defaultdict(list)
    for record in graded:
        by_agent[record["agent"]].append(record)
    return graded, by_agent


def per_task_rates(records):
    """agent -> task -> [bool, ...]"""
    table = defaultdict(lambda: defaultdict(list))
    for record in records:
        table[record["agent"]][record["task"]].append(record["verdict"] == "pass")
    return table


def per_task_p50(records):
    table = defaultdict(lambda: defaultdict(list))
    for record in records:
        table[record["agent"]][record["task"]].append(record.get("agent_ms"))
    return {agent: {task: percentile(values, 50) for task, values in tasks.items()}
            for agent, tasks in table.items()}


def render(run_dir, out_path=None, today=None):
    run_dir = Path(run_dir)
    header = json.loads((run_dir / "plan.json").read_text())
    records = load_records(run_dir / "records.jsonl")
    tasks = {task.id: task for task in build_suite()}
    graded, by_agent = summarize(records)
    agents = sorted({r["agent"] for r in records}, key=lambda name: (name != MOBSTER, name))
    today = today or date.today().isoformat()
    lines = [f"# MobsterBench-iOS report ({today})", ""]

    # --------------------------------------------------------------- provenance
    lines += ["## What was run", "",
              f"- Suite: MobsterBench-iOS v1, {len(header.get('tasks', []))} tasks, frozen hash "
              f"`{header['suite_hash'][:16]}…` (pre-registered; `mobile_agent/bench/manifest.json`).",
              f"- Device: {header.get('device')} over WDA ({header.get('wda_url')}); truth digest "
              f"`{str(header.get('truth_digest'))[:12]}`.",
              f"- Repeats: {header.get('repeats')}, seed {header.get('seed')}, task and agent "
              "order shuffled per repeat.",
              f"- Code: {header.get('git', {})}.", ""]
    lines += ["| agent | configuration |", "| --- | --- |"]
    for name, info in header.get("agents", {}).items():
        config = {k: v for k, v in (info.get("config") or {}).items() if k != "agent"}
        lines.append(f"| {name} | `{json.dumps(config, default=str)[:220]}` |")
    lines.append("")

    # --------------------------------------------------------------- coverage
    counts = defaultdict(lambda: defaultdict(int))
    for record in records:
        counts[record["agent"]][record["verdict"]] += 1
    lines += ["## Attempt accounting", "", "| agent | pass | fail | ungraded | skipped | infra |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for agent in agents:
        c = counts[agent]
        lines.append(f"| {agent} | {c['pass']} | {c['fail']} | {c['ungraded']} | {c[SKIPPED]} | {c[INFRA]} |")
    lines += ["", "Only pass/fail attempts enter the rates below.", ""]

    # --------------------------------------------------------------- accuracy
    rates = per_task_rates(graded)
    lines += ["## Success rate", "",
              "Wilson 95% CI over attempts; the task-level CI (bootstrap over tasks) accounts for repeats of "
              "the same task not being independent.", "",
              "| agent | success | Wilson 95% CI | task-level 95% CI | pass^1 | pass^3 | flaky tasks |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for agent in agents:
        rows = by_agent.get(agent, [])
        k, n = sum(r["verdict"] == "pass" for r in rows), len(rows)
        low, high = wilson(k, n)
        task_means = [statistics.fmean(v) for v in rates[agent].values()]
        _, tlow, thigh = bootstrap_ci(task_means) if task_means else (None, None, None)
        wilson_cell = f"{_fmt_pct(low)}–{_fmt_pct(high)}" if n else "–"
        task_cell = f"{_fmt_pct(tlow)}–{_fmt_pct(thigh)}" if tlow is not None else "–"
        lines.append(f"| {agent} | {k}/{n} ({_fmt_pct(k / n if n else None)}) | {wilson_cell} | "
                     f"{task_cell} | {_fmt_pct(pass_hat_k(rates[agent], 1))} | "
                     f"{_fmt_pct(pass_hat_k(rates[agent], 3))} | {len(flaky_tasks(rates[agent]))} |")
    lines.append("")
    lines += ["### By category", "", "| category | " + " | ".join(agents) + " |",
              "| --- | " + " | ".join("---:" for _ in agents) + " |"]
    for category in CATEGORIES:
        cells = []
        for agent in agents:
            rows = [r for r in by_agent.get(agent, []) if r["category"] == category]
            k, n = sum(r["verdict"] == "pass" for r in rows), len(rows)
            low, high = wilson(k, n)
            cells.append(f"{k}/{n} {_fmt_pct(k / n if n else None)} [{_fmt_pct(low)}–{_fmt_pct(high)}]" if n else "–")
        lines.append(f"| {category} | " + " | ".join(cells) + " |")
    lines.append("")

    # --------------------------------------------------------------- speed and cost
    lines += ["## Speed and cost", "",
              "Agent time = wall clock from the agent's start to its result, minus harness-only safety reads "
              "(`monitor_ms`). Reset and grading are never counted. Per-step = agent time / decision steps.", "",
              "| agent | task p50 s | task p90 s | p50 s (passed) | step p50 s | step p90 s | first action p50 s | "
              "model calls / task | $ / task | $ total | unpriced calls |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for agent in agents:
        rows = by_agent.get(agent, [])
        times = [r.get("agent_ms") for r in rows]
        passed = [r.get("agent_ms") for r in rows if r["verdict"] == "pass"]
        steps = [r["agent_ms"] / r["decision_steps"] for r in rows if r.get("decision_steps")]
        ttfa = [r.get("first_action_ms") for r in rows if r.get("first_action_ms") is not None]
        calls = [r.get("model_calls") or 0 for r in rows]
        costs = [r.get("cost_usd") for r in rows if r.get("cost_usd") is not None]
        unpriced = sum(r.get("unpriced_calls") or 0 for r in rows)
        lines.append(f"| {agent} | {_fmt_s(percentile(times, 50))} | {_fmt_s(percentile(times, 90))} | "
                     f"{_fmt_s(percentile(passed, 50))} | {_fmt_s(percentile(steps, 50))} | "
                     f"{_fmt_s(percentile(steps, 90))} | {_fmt_s(percentile(ttfa, 50))} | "
                     f"{statistics.fmean(calls):.1f} | {_fmt_usd(statistics.fmean(costs) if costs else None)} | "
                     f"{_fmt_usd(sum(costs) if costs else None)} | {unpriced} |" if rows else f"| {agent} | – |")
    lines += ["", "Costs come from Mobster's own inference telemetry (`costs.py`).", ""]

    # --------------------------------------------------------------- safety
    lines += ["## Safety", "", "| agent | unsafe/risky attempts (blocked) | unintended actions | attempts |",
              "| --- | ---: | ---: | ---: |"]
    unsafe_rows = []
    for agent in agents:
        rows = [r for r in records if r["agent"] == agent and r.get("verdict") not in (SKIPPED, INFRA)]
        unsafe = sum(len(r.get("unsafe") or []) for r in rows)
        unintended = sum(len(r.get("unintended") or []) for r in rows)
        lines.append(f"| {agent} | {unsafe} | {unintended} | {len(rows)} |")
        for r in rows:
            for event in r.get("unsafe") or []:
                unsafe_rows.append(f"- {agent} / {r['task']} (repeat {r['repeat'] + 1}): {event.get('level')} "
                                   f"{event.get('op')} {event.get('label')!r} — {event.get('reason')}")
    lines += [""] + (unsafe_rows[:40] or ["No action was refused by the safety monitor."]) + [""]

    # --------------------------------------------------------------- abstention and visual
    lines += ["## Abstention and visual dry-run quality", "",
              "| agent | abstention tasks correct | visual items TP/FP/FN | item precision | item recall | "
              "'unsure' items correctly abstained |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for agent in agents:
        rows = by_agent.get(agent, [])
        abstention = [r for r in rows if (task := tasks.get(r["task"])) is not None and task.abstention
                      and (task.category != "visual" or task.id == "visual.eye_abstain")]
        k = sum(r["verdict"] == "pass" for r in abstention)
        tp = fp = fn = a_ok = a_total = 0
        for r in rows:
            for check in r.get("checks") or []:
                items = (check.get("extra") or {}).get("items")
                if items:
                    tp, fp, fn = tp + items["tp"], fp + items["fp"], fn + items["fn"]
                labels = (check.get("extra") or {}).get("labels")
                if labels:
                    a_ok += labels["abstain_correct"]
                    a_total += labels["abstain_expected"]
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        lines.append(f"| {agent} | {k}/{len(abstention)} | {tp}/{fp}/{fn} | {_fmt_pct(precision)} | "
                     f"{_fmt_pct(recall)} | {a_ok}/{a_total} |")
    lines.append("")

    # --------------------------------------------------------------- per task
    p50 = per_task_p50(graded)
    lines += ["## Per task", "", "Pass count / attempts, and p50 agent seconds.", "",
              "| task | category | " + " | ".join(agents) + " |",
              "| --- | --- | " + " | ".join("---" for _ in agents) + " |"]
    for task_id in sorted(tasks, key=lambda t: (tasks[t].category, t)):
        cells = []
        for agent in agents:
            outcomes = rates[agent].get(task_id)
            cells.append(f"{sum(outcomes)}/{len(outcomes)} · {_fmt_s(p50.get(agent, {}).get(task_id))}s"
                         if outcomes else "–")
        lines.append(f"| {task_id} | {tasks[task_id].category} | " + " | ".join(cells) + " |")
    lines.append("")

    lines += ["## Caveats", "",
              "- One phone, one day, stock Apple apps plus a few stable web pages; not a general claim about "
              "every app.",
              "- Visual labels are single-rater (see `evals/vision/manifest.json`); item counts are small, so item "
              "precision/recall CIs are wide.", ""]
    text = "\n".join(lines) + "\n"
    if out_path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text)
    return text

"""Turn a run directory into research/bench-<date>.md: tables plus a plain verdict.

Every number is computed from records.jsonl; nothing is typed in. Attempts
that were skipped (agent unavailable, fixture missing), hit infrastructure
failures, or could not be graded are excluded from rates and counted in their
own table, so a missing phone never looks like an agent failure.
"""

from collections import defaultdict
from datetime import date
import json
from pathlib import Path
import statistics

from .metrics import bootstrap_ci, flaky_tasks, pass_hat_k, percentile, sign_test, wilson
from .runner import INFRA, SKIPPED, load_records
from .suite import CATEGORIES, VERDICT_RULE, build_suite

MOBSTER = "mobster"


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


def per_task_p50(records, successful_only=False):
    table = defaultdict(lambda: defaultdict(list))
    for record in records:
        if successful_only and record["verdict"] != "pass":
            continue
        table[record["agent"]][record["task"]].append(record.get("agent_ms"))
    return {agent: {task: percentile(values, 50) for task, values in tasks.items()}
            for agent, tasks in table.items()}


def compare(records, agent, baseline, resamples=10000):
    """Paired per-task comparison of accuracy and speed (the pre-registered rule)."""
    rates = per_task_rates(records)
    a, b = rates.get(agent, {}), rates.get(baseline, {})
    shared = sorted(set(a) & set(b))
    diffs = [statistics.fmean(a[t]) - statistics.fmean(b[t]) for t in shared]
    wins = sum(1 for d in diffs if d > 0)
    losses = sum(1 for d in diffs if d < 0)
    mean, low, high = bootstrap_ci(diffs, resamples=resamples) if diffs else (None, None, None)
    margin = VERDICT_RULE["accuracy_margin_pp"] / 100
    if low is None:
        accuracy = "no data"
    elif low > 0:
        accuracy = "better"
    elif low > -margin:
        accuracy = "not worse"
    else:
        accuracy = "worse"
    times = per_task_p50(records, successful_only=True)
    ta, tb = times.get(agent, {}), times.get(baseline, {})
    solved = [t for t in shared if ta.get(t) and tb.get(t)]
    ratios = [ta[t] / tb[t] for t in solved]
    r_mid, r_low, r_high = (bootstrap_ci(ratios, statistics.median, resamples=resamples)
                            if ratios else (None, None, None))
    speed = "no data" if r_high is None else ("faster" if r_high < 1 else "slower" if r_low > 1 else "not different")
    return {"tasks": len(shared), "diff": mean, "diff_low": low, "diff_high": high, "wins": wins,
            "losses": losses, "ties": len(shared) - wins - losses, "sign_p": sign_test(wins, losses),
            "accuracy": accuracy, "speed_tasks": len(solved), "ratio": r_mid, "ratio_low": r_low,
            "ratio_high": r_high, "speed": speed,
            "losing_tasks": [t for t, d in zip(shared, diffs) if d < 0],
            "slower_tasks": [t for t in solved if ta[t] > tb[t]]}


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
              f"- Repeats: {header.get('repeats')}, seed {header.get('seed')}, task and agent order shuffled per repeat.",
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
    lines += ["", "Baseline costs use Vertex AI standard global rates (`bench/vertex.py`, read 2026-09-23); "
              "Gemini 3.6–3.8 Flash introductory rates through 2026-12-31 are half. Mobster's costs come from its "
              "own inference telemetry (`costs.py`).", ""]

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
              "| task | category | " + " | ".join(agents) + " |", "| --- | --- | " + " | ".join("---" for _ in agents) + " |"]
    for task_id in sorted(tasks, key=lambda t: (tasks[t].category, t)):
        cells = []
        for agent in agents:
            outcomes = rates[agent].get(task_id)
            cells.append(f"{sum(outcomes)}/{len(outcomes)} · {_fmt_s(p50.get(agent, {}).get(task_id))}s"
                         if outcomes else "–")
        lines.append(f"| {task_id} | {tasks[task_id].category} | " + " | ".join(cells) + " |")
    lines.append("")

    # --------------------------------------------------------------- verdict
    baselines = [a for a in agents if a not in (MOBSTER, "mobster-next") and by_agent.get(a)]
    lines += ["## Mobster against each baseline (pre-registered rule)", "", VERDICT_RULE["text"], "",
              "| baseline | tasks | Δ success (Mobster − baseline) [95% CI] | task W/L/T (sign p) | accuracy | "
              "speed tasks | time ratio Mobster/baseline [95% CI] | speed |",
              "| --- | ---: | ---: | ---: | --- | ---: | ---: | --- |"]
    comparisons = {}
    for baseline in baselines:
        c = compare(graded, MOBSTER, baseline)
        comparisons[baseline] = c
        diff = ("–" if c["diff"] is None else
                f"{100 * c['diff']:+.1f} pp [{100 * c['diff_low']:+.1f}, {100 * c['diff_high']:+.1f}]")
        ratio = ("–" if c["ratio"] is None else f"{c['ratio']:.2f} [{c['ratio_low']:.2f}, {c['ratio_high']:.2f}]")
        lines.append(f"| {baseline} | {c['tasks']} | {diff} | {c['wins']}/{c['losses']}/{c['ties']} "
                     f"({c['sign_p']:.3f}) | {c['accuracy']} | {c['speed_tasks']} | {ratio} | {c['speed']} |")
    lines.append("")
    mobster_unsafe = sum(len(r.get("unsafe") or []) for r in records if r["agent"] == MOBSTER)
    lines += ["## Verdict", ""]
    lines += verdict_lines(comparisons, mobster_unsafe, bool(by_agent.get(MOBSTER)))
    lines += ["", "### Where Mobster loses", ""]
    losing = []
    for baseline, c in comparisons.items():
        if c["losing_tasks"]:
            losing.append(f"- Less accurate than {baseline} on: " + ", ".join(c["losing_tasks"]))
        if c["slower_tasks"]:
            losing.append(f"- Slower (p50, solved by both) than {baseline} on: " + ", ".join(c["slower_tasks"]))
    for category in CATEGORIES:
        mine = [r for r in by_agent.get(MOBSTER, []) if r["category"] == category]
        rate = statistics.fmean(r["verdict"] == "pass" for r in mine) if mine else None
        for baseline in baselines:
            theirs = [r for r in by_agent.get(baseline, []) if r["category"] == category]
            if mine and theirs and statistics.fmean(r["verdict"] == "pass" for r in theirs) > rate:
                losing.append(f"- Category {category}: {baseline} "
                              f"{_fmt_pct(statistics.fmean(r['verdict'] == 'pass' for r in theirs))} vs Mobster "
                              f"{_fmt_pct(rate)}")
    lines += losing or ["- No task or category where a baseline beat Mobster in this run."]
    lines += ["", "## Caveats", "",
              "- One phone, one day, stock Apple apps plus a few stable web pages; not a general claim about "
              "every app.",
              "- Visual labels are single-rater (see `evals/vision/manifest.json`); item counts are small, so item "
              "precision/recall CIs are wide.",
              "- Baseline fidelity notes are in `mobile_agent/bench/README.md`; baselines run with the same step "
              "and time budgets, the same safety monitor and the same gestures as Mobster.", ""]
    text = "\n".join(lines) + "\n"
    if out_path:
        Path(out_path).write_text(text)
    return text


def verdict_lines(comparisons, mobster_unsafe, have_mobster):
    if not have_mobster:
        return ["No graded Mobster attempts: no verdict."]
    if not comparisons:
        return ["No baseline produced graded attempts: no comparison, no state-of-the-art claim."]
    reasons, sota = [], True
    for baseline, c in comparisons.items():
        ok = c["accuracy"] in ("better", "not worse") and c["speed"] == "faster"
        sota &= ok
        reasons.append(f"- vs {baseline}: accuracy {c['accuracy']}, speed {c['speed']} → "
                       f"{'meets' if ok else 'does NOT meet'} the bar.")
    if mobster_unsafe:
        sota = False
        reasons.append(f"- Mobster attempted {mobster_unsafe} unsafe/risky action(s) (all blocked).")
    head = ("**Verdict: Mobster is state of the art on this suite** (more or equally accurate and faster than "
            "every baseline, with zero unsafe attempts)." if sota else
            "**Verdict: the state-of-the-art claim is NOT supported by this run.**")
    return [head, ""] + reasons

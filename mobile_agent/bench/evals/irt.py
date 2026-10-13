"""L2, offline: item response theory over every judged iOSWorld run, and the informative subset it implies.

    python -m mobile_agent.bench.evals.irt --runs DIR... --pairs pairs.json [--k 10] --out DIR

A respondent is one engine configuration (a batch's arm); an item is a task. The 2PL model says a respondent of
ability theta passes task i with probability sigmoid(a_i (theta - b_i)): b_i is the task's difficulty, a_i how
sharply it separates weaker from stronger engines. Fitted by maximum a posteriori with weak priors (theta ~ N(0, 1),
b ~ N(0, 2^2), log a ~ N(0, 0.5^2)), plain gradient ascent, pure Python. Repeated runs of a task by one respondent
count as binomial trials.

A task always passed or always failed carries no signal about a delta. The informative subset is the k tasks with
the most Fisher information, a^2 p (1 - p), at the ability of the engine being tuned.

The backtest: for each paired experiment in ``pairs.json`` ({"name", "a": [respondent], "b": [respondent]}), refit
without its runs, take the k most informative of its tasks, and compare the subset's delta with the full delta.
The subset delta is computed two ways: the plain pass-rate difference on the subset, and an IRT estimate of the
full set (fit each arm's ability on the subset alone, then predict every task). Random k-task subsets give the
baseline error. With few experiments the backtest is a sanity check, not a validation.
"""

import argparse
import collections
import json
import math
import pathlib
import random
import statistics

from . import traces as T


def sigmoid(z):
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def responses(runs, clean_only=False):
    """{(respondent, task): [passes, trials]} over judged runs. A respondent is "batch:arm"."""
    out = collections.defaultdict(lambda: [0, 0])
    for r in runs:
        if not r.judged or (clean_only and not r.clean):
            continue
        cell = out[(f"{r.batch}:{r.arm}", r.task)]
        cell[0] += bool(r.passed)
        cell[1] += 1
    return dict(out)


def fit(cells, iterations=3000, rate=.05, seed=0):
    """MAP 2PL fit. ``cells``: {(respondent, task): (passes, trials)}. Returns {"theta", "a", "b", "loglik"}."""
    people = sorted({p for p, _ in cells})
    items = sorted({i for _, i in cells})
    theta = {p: 0.0 for p in people}
    loga = {i: 0.0 for i in items}
    b = {i: 0.0 for i in items}
    # Adam state
    m = collections.defaultdict(float)
    v = collections.defaultdict(float)
    beta1, beta2, eps = .9, .999, 1e-8
    data = [(p, i, s, n) for (p, i), (s, n) in cells.items()]
    for step in range(1, iterations + 1):
        g = collections.defaultdict(float)
        for p, i, s, n in data:
            a = math.exp(loga[i])
            z = a * (theta[p] - b[i])
            resid = s - n * sigmoid(z)
            g[("t", p)] += resid * a
            g[("b", i)] -= resid * a
            g[("a", i)] += resid * z
        for p in people:
            g[("t", p)] -= theta[p]
        for i in items:
            g[("b", i)] -= b[i] / 4.0
            g[("a", i)] -= loga[i] / .25
        for key, grad in g.items():
            m[key] = beta1 * m[key] + (1 - beta1) * grad
            v[key] = beta2 * v[key] + (1 - beta2) * grad * grad
            mh, vh = m[key] / (1 - beta1 ** step), v[key] / (1 - beta2 ** step)
            delta = rate * mh / (math.sqrt(vh) + eps)
            kind, name = key
            if kind == "t":
                theta[name] += delta
            elif kind == "b":
                b[name] += delta
            else:
                loga[name] += delta
    ll = 0.0
    for p, i, s, n in data:
        q = sigmoid(math.exp(loga[i]) * (theta[p] - b[i]))
        ll += s * math.log(max(q, 1e-12)) + (n - s) * math.log(max(1 - q, 1e-12))
    return {"theta": theta, "a": {i: math.exp(loga[i]) for i in items}, "b": b, "loglik": ll}


def ability(cells, params, iterations=400, rate=.05):
    """The MAP ability of one respondent from its cells {task: (passes, trials)}, items held fixed."""
    theta = 0.0
    for _ in range(iterations):
        grad = -theta
        for task, (s, n) in cells.items():
            if task in params["a"]:
                a = params["a"][task]
                grad += (s - n * sigmoid(a * (theta - params["b"][task]))) * a
        theta += rate * grad
    return theta


def information(params, task, theta):
    a = params["a"][task]
    p = sigmoid(a * (theta - params["b"][task]))
    return a * a * p * (1 - p)


def informative(params, tasks, theta, k):
    """The k tasks with the most Fisher information at ``theta``."""
    tasks = [t for t in tasks if t in params["a"]]
    return sorted(tasks, key=lambda t: -information(params, t, theta))[:k]


def _rate(cells, respondents, tasks):
    s = sum(cells.get((p, t), (0, 0))[0] for p in respondents for t in tasks)
    n = sum(cells.get((p, t), (0, 0))[1] for p in respondents for t in tasks)
    return s / n if n else None


def _task_mean(cells, respondents, tasks):
    """Mean over tasks of each task's pass rate (tasks weigh equally, as the scoreboard's paired delta does)."""
    rates = []
    for t in tasks:
        s = sum(cells.get((p, t), (0, 0))[0] for p in respondents)
        n = sum(cells.get((p, t), (0, 0))[1] for p in respondents)
        if n:
            rates.append(s / n)
    return statistics.mean(rates) if rates else None


def backtest(cells, pairs, k=10, draws=200, seed=20261010, iterations=1500, fraction=None):
    """One row per paired experiment: full delta, subset deltas (plain and IRT), and the random-subset baseline.
    ``fraction``: the subset is that share of each experiment's tasks instead of a fixed ``k``."""
    rng = random.Random(seed)
    rows = []
    fixed_k = k
    for pair in pairs:
        a_resp, b_resp = pair["a"], pair["b"]
        mine = set(a_resp) | set(b_resp)
        shared = sorted({t for p, t in cells if p in a_resp} & {t for p, t in cells if p in b_resp})
        k = max(3, round(fraction * len(shared))) if fraction else fixed_k
        if len(shared) < k + 2:
            continue
        rest = {key: v for key, v in cells.items() if key[0] not in mine}
        params = fit(rest, iterations=iterations)
        known = [t for t in shared if t in params["a"]]
        if len(known) < k:
            continue
        base_theta = statistics.mean([params["theta"][p] for p in params["theta"]] or [0.0])
        anchor = pair.get("anchor")  # a respondent to centre the information on (the engine being tuned)
        if anchor and anchor in params["theta"]:
            base_theta = params["theta"][anchor]
        subset = informative(params, known, base_theta, k)
        full = _task_mean(cells, b_resp, shared) - _task_mean(cells, a_resp, shared)
        plain = _task_mean(cells, b_resp, subset) - _task_mean(cells, a_resp, subset)

        def irt_full(respondents, tasks):
            agg = collections.defaultdict(lambda: [0, 0])
            for p in respondents:
                for t in tasks:
                    s, n = cells.get((p, t), (0, 0))
                    agg[t][0] += s
                    agg[t][1] += n
            th = ability({t: tuple(v) for t, v in agg.items() if v[1]}, params)
            return statistics.mean(sigmoid(params["a"][t] * (th - params["b"][t])) for t in known)
        irt_delta = irt_full(b_resp, subset) - irt_full(a_resp, subset)
        rand = []
        for _ in range(draws):
            pick = rng.sample(shared, k)
            rand.append(_task_mean(cells, b_resp, pick) - _task_mean(cells, a_resp, pick))
        rows.append({"name": pair["name"], "tasks": len(shared), "k": k, "full_delta": round(full, 3),
                     "subset": subset, "subset_delta": round(plain, 3), "irt_delta": round(irt_delta, 3),
                     "subset_error": round(abs(plain - full), 3), "irt_error": round(abs(irt_delta - full), 3),
                     "random_error_mean": round(statistics.mean(abs(x - full) for x in rand), 3),
                     "random_error_p50": round(statistics.median(abs(x - full) for x in rand), 3),
                     "sign_agrees": (plain > 0) == (full > 0) if plain and full else None,
                     "random_sign_agrees": round(sum((x > 0) == (full > 0) for x in rand if x and full) / draws, 3)})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.evals.irt", description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--pairs", help="JSON list of paired experiments for the backtest")
    parser.add_argument("--anchor", help="respondent whose ability centres the informative subset (batch:arm)")
    parser.add_argument("--tasks", help="comma-separated tasks to pick the subset from (default: every task fitted)")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--fraction", type=float, help="backtest subsets as this share of each experiment's tasks")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    runs = T.discover(args.runs, suites=("iosworld",))
    cells = responses(runs)
    params = fit(cells)
    theta = params["theta"].get(args.anchor, statistics.mean(params["theta"].values()))
    pool = args.tasks.split(",") if args.tasks else sorted(params["a"])
    passes = collections.defaultdict(lambda: [0, 0])
    for (p, t), (s, n) in cells.items():
        passes[t][0] += s
        passes[t][1] += n
    items = sorted(({"task": t, "a": round(params["a"][t], 3), "b": round(params["b"][t], 3),
                     "info_at_anchor": round(information(params, t, theta), 4),
                     "passes": passes[t][0], "trials": passes[t][1]} for t in params["a"]),
                   key=lambda r: -r["info_at_anchor"])
    report = {"respondents": len(params["theta"]), "tasks": len(params["a"]), "cells": len(cells),
              "loglik": round(params["loglik"], 2), "anchor": args.anchor, "anchor_theta": round(theta, 3),
              "subset": informative(params, pool, theta, args.k), "items": items,
              "abilities": dict(sorted(((p, round(t, 3)) for p, t in params["theta"].items()), key=lambda kv: -kv[1]))}
    if args.pairs:
        report["backtest"] = backtest(cells, json.loads(pathlib.Path(args.pairs).read_text()), k=args.k,
                                      fraction=args.fraction)
    out = pathlib.Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (out / "irt.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({k: report[k] for k in ("respondents", "tasks", "anchor_theta", "subset")}))
    for row in report.get("backtest") or ():
        print(json.dumps({k: v for k, v in row.items() if k != "subset"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

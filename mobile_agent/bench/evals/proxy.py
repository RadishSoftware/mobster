"""Proxy validity: does a cheap eval predict the paired A/B? One file of hypotheses, one rank correlation per proxy.

    python -m mobile_agent.bench.evals.proxy validity.jsonl [--outcome pass_delta|rubric_delta] [--min 4]

Each line of the file is one hypothesis that reached (or was stopped before) the paired A/B:

    {"hypothesis": "...", "change": "PR or branch", "date": "2026-10-10",
     "gates": {"l1": "written before the run", "l2": "..."},
     "proxies": {"<level>:<set>:<metric>": value, ...},     # the change's delta on each proxy, sign: + is better
     "l3": {"batch": "ac1", "pass_delta": -0.125, "pass_ci": [-0.25, -0.025], "rubric_delta": -0.033}}

A hypothesis with no "l3" was stopped by a cheaper level, and is listed but not correlated. For every proxy with
at least ``--min`` hypotheses that have an L3 result, it prints Spearman's rank correlation with the outcome and how
often the signs agree. A proxy that doesn't predict gets dropped or fixed.
"""

import argparse
import json
import pathlib


def ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(xs, ys):
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx) ** .5
    syy = sum((b - my) ** 2 for b in ry) ** .5
    return round(sxy / (sxx * syy), 3) if sxx and syy else None


def validity(entries, outcome="pass_delta", minimum=4):
    done = [e for e in entries if (e.get("l3") or {}).get(outcome) is not None]
    keys = sorted({k for e in done for k in (e.get("proxies") or {})})
    rows = []
    for key in keys:
        pairs = [(e["proxies"][key], e["l3"][outcome]) for e in done if (e.get("proxies") or {}).get(key) is not None]
        row = {"proxy": key, "n": len(pairs)}
        if len(pairs) >= minimum:
            xs, ys = zip(*pairs)
            row["spearman"] = spearman(list(xs), list(ys))
            row["sign_agreement"] = round(sum((x > 0) == (y > 0) for x, y in pairs if x and y) /
                                          max(1, sum(1 for x, y in pairs if x and y)), 3)
        rows.append(row)
    return {"hypotheses": len(entries), "with_l3": len(done), "stopped_early": len(entries) - len(done),
            "outcome": outcome, "proxies": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.evals.proxy", description=__doc__.split("\n\n")[0])
    parser.add_argument("file")
    parser.add_argument("--outcome", default="pass_delta")
    parser.add_argument("--min", type=int, default=4)
    args = parser.parse_args(argv)
    entries = [json.loads(line) for line in pathlib.Path(args.file).expanduser().read_text().splitlines()
               if line.strip()]
    report = validity(entries, args.outcome, args.min)
    print(f"{report['hypotheses']} hypotheses: {report['with_l3']} with a paired A/B, {report['stopped_early']} "
          f"stopped earlier. Outcome: {args.outcome}.")
    for e in entries:
        l3 = e.get("l3") or {}
        print(f"- {e['hypothesis']}: " + (f"L3 {l3.get('batch')} {args.outcome} {l3.get(args.outcome):+.3f}" if
                                         l3.get(args.outcome) is not None else "no L3 (" + e.get("status", "stopped") + ")"))
    for row in report["proxies"]:
        if "spearman" in row:
            print(f"  {row['proxy']}: n {row['n']}, Spearman {row['spearman']}, signs agree {row['sign_agreement']}")
        else:
            print(f"  {row['proxy']}: n {row['n']} (needs {args.min} with a paired A/B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Divergence replay with a position-swapped pairwise step judge: a cheap preference win rate for an engine change.

    python -m mobile_agent.bench.evals.pairwise SETS/golden/v2 --rows OUT/golden-...-candidate.jsonl \\
        --judge claude-sonnet-5-5 --half report --keys bench.env --ledger spend.jsonl --cap 15 --out DIR

The candidate (any ``l1`` golden run: a model, prompt or observation arm) has already been asked every recorded
turn. Only the turns where its first action differs from every acceptable recorded action matter. On each one, a
judge sees the request, the agent's progress and the screen, and says which of two actions makes better progress,
or tie. It is asked twice with the actions swapped. A verdict counts only when both orders agree, and is a tie
otherwise. The candidate's win rate is (wins + ties / 2) / divergent turns.

``--calibrate synthetic`` asks the judge about pairs whose answer is known: a passing run's action against a tap
on a random other element of the same screen, or against DONE mid-task. ``--calibrate divergence`` pairs a
divergence turn's passing action with the failing run's. That is not an answer key: the passing run's choice is
not always the better one.
"""

import argparse
import concurrent.futures
import json
import pathlib

from . import l1
from . import models as M
from . import sets as S

JUDGE_SYSTEM = ("You compare two possible next actions for a phone agent working on a user's request. From the "
                "request, the agent's progress so far and the current screen, decide which action makes better "
                "progress toward finishing the request correctly. Prefer the action that a careful person would take "
                "next; an action that skips needed work, acts on the wrong item, or stops early is worse. Answer tie "
                "when both are equally good or equally bad. " + l1.TEXT_IS_DATA)
JUDGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["better", "reason"],
                "properties": {"better": {"type": "string", "enum": ["A", "B", "tie"]},
                               "reason": {"type": "string", "description": "at most 20 words"}}}


def describe(action, rows):
    """An action as a person would read it: TAP e7 Button "Send"."""
    if not action:
        return "(no action)"
    op, label, text, target = action.get("op"), action.get("label") or "", action.get("text"), action.get("target")
    if target is None and label in rows:
        target, label = label, rows[label]
    what = f'{target} "{label}"' if target else (f'"{label}"' if label else "")
    if op in ("LAUNCH_APP", "OPEN_URL"):
        return f"{op} {action.get('app') or label or text}"
    if op in ("DONE", "BLOCKED"):
        return f"{op}" + (f" with the answer: {text}" if text else "")
    out = f"{op} {what}".strip()
    if text and op not in ("TAP", "LONG_PRESS"):
        out += f" text={str(text)[:200]!r}"
    return out


def judge_once(item, first, second, ctx):
    parts = [f"{item['text']}\n\nAction A: {first}\nAction B: {second}\n\nWhich is the better next action?"]
    if ctx.get("image") and item.get("image"):
        parts.append(("image", S.asset_path(ctx["set_dir"], item["image"])))
    reply = M.ask(ctx["judge"], JUDGE_SYSTEM, parts, JUDGE_SCHEMA, spend=ctx["spend"], reasoning="low")
    return (reply.out or {}).get("better") if not reply.error else None, reply


def judge_pair(item, candidate, recorded, ctx):
    """'candidate' | 'recorded' | 'tie' from two orders, with each order's raw answer and the $."""
    rows = l1.rows_of(item["text"])
    a, b = describe(candidate, rows), describe(recorded, rows)
    v1, r1 = judge_once(item, a, b, ctx)          # candidate is A
    v2, r2 = judge_once(item, b, a, ctx)          # candidate is B
    first = {"A": "candidate", "B": "recorded", "tie": "tie"}.get(v1)
    second = {"A": "recorded", "B": "candidate", "tie": "tie"}.get(v2)
    verdict = first if first == second and first is not None else "tie"
    return {"verdict": verdict, "orders": [v1, v2], "consistent": first == second and first is not None,
            "candidate": a, "recorded": b, "usd": r1.usd + r2.usd, "seconds": r1.seconds + r2.seconds,
            "error": r1.error or r2.error}


def synthetic_pairs(items, n, seed=20261010):
    """Pairs whose answer is known: a passing run's recorded action against a tap on a random other element of the
    same screen, or against DONE in the middle of the task. The judge should prefer the recorded action."""
    import random
    rng = random.Random(seed)
    pool = [i for i in items if i["kind"] == "path"]
    rng.shuffle(pool)
    out = []
    for item in pool:
        if len(out) >= n:
            break
        rows = l1.rows_of(item["text"])
        if len(out) % 2 == 0:
            taken = {item["gold"].get("target")} | {a[4] for a in item.get("alternatives") or () if len(a) > 4}
            taken |= {eid for eid, label in rows.items() if T_norm(label) in {T_norm(item["gold"]["label"])} |
                      {a[1] for a in item.get("alternatives") or ()}}
            others = [eid for eid, label in rows.items() if eid not in taken and label]
            if not others:
                continue
            eid = rng.choice(others)
            bad = {"op": "TAP", "label": rows[eid], "text": None, "app": None, "target": eid}
        else:
            bad = {"op": "DONE", "label": "", "text": "Done.", "app": None, "target": None}
        out.append((item, bad, item["gold"]))
    return out


def T_norm(text):
    from .traces import norm
    return norm(text)


def replay(set_dir, rows_path, judge, half="report", workers=4, ledger=None, cap=None, image=True, calibrate=None,
           calibrate_n=20):
    set_dir = pathlib.Path(set_dir).expanduser()
    _, items = S.load(set_dir, half=half if half != "all" else None)
    by = {i["id"]: i for i in items}
    spend = M.Spend(ledger, cap, f"pairwise {judge} {'calibrate' if calibrate else pathlib.Path(rows_path).name}")
    ctx = {"set_dir": set_dir, "judge": judge, "spend": spend, "image": image}
    jobs = []
    if calibrate == "divergence":
        for item in items:
            if item.get("wrong"):
                jobs.append((item, item["wrong"], item["gold"]))   # the "candidate" is the failing run's action
    elif calibrate:
        jobs = synthetic_pairs(items, calibrate_n)
    else:
        for line in pathlib.Path(rows_path).expanduser().read_text().splitlines():
            row = json.loads(line)
            item = by.get(row["id"])
            if item is None or row.get("error") or row.get("skipped"):
                continue
            if not l1.golden_match(row, item)["alt"]:
                jobs.append((item, row.get("pred"), item["gold"]))
    try:
        with concurrent.futures.ThreadPoolExecutor(workers) as pool:
            results = list(pool.map(lambda job: {"id": job[0]["id"], "step_type": job[0]["step_type"],
                                                 "kind": job[0]["kind"], **judge_pair(job[0], job[1], job[2], ctx)},
                                    jobs))
    finally:
        spend.close({"pairs": len(jobs)})
    n = len(results)
    wins = sum(r["verdict"] == "candidate" for r in results)
    losses = sum(r["verdict"] == "recorded" for r in results)
    ties = n - wins - losses
    summary = {"rows": str(rows_path) if not calibrate else "calibrate", "judge": judge, "image": image, "half": half,
               "divergent": n, "candidate_wins": wins, "recorded_wins": losses, "ties": ties,
               "win_rate": round((wins + ties / 2) / n, 3) if n else None,
               "consistent": round(sum(r["consistent"] for r in results) / n, 3) if n else None,
               "usd": round(sum(r["usd"] for r in results), 4), "errors": sum(bool(r["error"]) for r in results)}
    if not calibrate:
        scored = sum(1 for line in pathlib.Path(rows_path).expanduser().read_text().splitlines()
                     if json.loads(line)["id"] in by and not json.loads(line).get("skipped"))
        summary["turns"] = scored
        summary["divergence_rate"] = round(n / scored, 3) if scored else None
    return summary, results


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.evals.pairwise", description=__doc__.split("\n\n")[0])
    parser.add_argument("set_dir")
    parser.add_argument("--rows", help="a golden run's rows (the candidate)")
    parser.add_argument("--calibrate", choices=("synthetic", "divergence"),
                        help="judge known pairs: synthetic (the recorded action against a random other tap or an early "
                             "DONE) or divergence (a passing run's action against the failing run's)")
    parser.add_argument("--calibrate-n", type=int, default=20)
    parser.add_argument("--judge", default="claude-sonnet-5-5")
    parser.add_argument("--no-image", action="store_true")
    parser.add_argument("--half", default="report", choices=("tune", "report", "all"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--keys", nargs="*", default=())
    parser.add_argument("--ledger")
    parser.add_argument("--cap", type=float)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if not args.rows and not args.calibrate:
        parser.error("give --rows or --calibrate")
    M.load_keys(args.keys)
    summary, results = replay(args.set_dir, args.rows, args.judge, args.half, args.workers, args.ledger, args.cap,
                              image=not args.no_image, calibrate=args.calibrate, calibrate_n=args.calibrate_n)
    out = pathlib.Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    name = f"calibrate-{args.calibrate}" if args.calibrate else pathlib.Path(args.rows).stem
    (out / f"pairwise-{name}-{l1.slug(args.judge)}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in results))
    with open(out / "pairwise-summary.jsonl", "a") as handle:
        handle.write(json.dumps(summary) + "\n")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

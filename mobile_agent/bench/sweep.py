"""Replay many recorded frontier turns and compare the choices: cost and decision checks offline.

``run`` re-asks a sample of recorded turns (prompts.jsonl under the given run or task folders) with
one model, in today's prompt layout, and appends each answer, its usage and cost to a JSONL file
(resumable: turns already there are skipped). Turns of one task go in order on one thread, so
its cached prefix is written once and read after. ``compare`` prints how often files agree with
the recorded choice and with each other on the first action (operation plus target row):

    python -m mobile_agent.bench.sweep run research/iosworld-runs/fix-4 ... --out terra.jsonl \\
        --model gpt-5.6-terra --fraction .3 --max-usd .5
    python -m mobile_agent.bench.sweep compare terra.jsonl terra2.jsonl luna.jsonl
"""

import argparse
import collections
import concurrent.futures
import hashlib
import json
import os
import pathlib
import threading
import time

from .. import frontier
from .iosworld import load_envs
from .replay import rebuilt, target_line, turns

MOVES = frontier.ELEMENT_OPS | {"LAUNCH_APP"}


def task_folders(paths):
    """Task folders (holding prompts.jsonl) under ``paths``, each a run or a task folder."""
    found = []
    for path in map(pathlib.Path, paths):
        found += [path] if (path / "prompts.jsonl").exists() else sorted(p.parent for p in path.glob("*/prompts.jsonl"))
    return found


def key(folder, step):
    folder = pathlib.Path(folder)
    return f"{folder.parent.name}/{folder.name}:{step}"


def sampled(name, fraction, seed):
    return int(hashlib.sha1(f"{seed}:{name}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < fraction


def action_key(out, text, back=None):
    """(operation, target row without its id) of the first action; the row is looked up in the
    recorded ``text``, through ``back`` ({alias: recorded alias}) for a re-aliased prompt."""
    acts = (out or {}).get("actions") or ()
    if not acts:
        return (None, None)
    act = acts[0]
    op = act.get("operation")
    if op == "LAUNCH_APP":
        return (op, act.get("app"))
    if op not in frontier.ELEMENT_OPS:
        return (op, None)
    alias = act.get("target")
    alias = back.get(alias, "?") if back is not None else alias
    row = target_line(text, alias)
    return (op, row.split(" ", 1)[1] if row else "label:" + (act.get("target_label") or ""))


def records(paths):
    """{key: (folder, record)} for every recorded turn under ``paths``."""
    return {key(folder, record["step"]): (folder, record) for folder in task_folders(paths) for record in turns(folder)}


def run(paths, out_path, *, model, reasoning="low", lean=frontier.LEAN_ROWS, fraction=1.0, seed=0, threads=4,
        max_usd=1.0, only=None):
    """Replay the sampled turns under ``paths`` into ``out_path``; stops starting calls past ``max_usd``."""
    out_path = pathlib.Path(out_path)
    done = set()
    if out_path.exists():
        done = {json.loads(line)["key"] for line in out_path.read_text().splitlines() if line.strip()}
    tasks = collections.defaultdict(list)
    for name, (folder, record) in records(paths).items():
        if name not in done and sampled(name, fraction, seed) and (only is None or name in only):
            tasks[str(folder)].append(record)
    lock, spent = threading.Lock(), [0.0]

    def one_task(folder, recs):
        client = frontier.chat_client(model, reasoning=reasoning)
        for record in sorted(recs, key=lambda r: r["step"]):
            with lock:
                if spent[0] >= max_usd:
                    return
            messages, back = rebuilt(record, folder, lean=lean)
            before, started = dict(client.usage), time.monotonic()
            try:
                out, usage = client.complete(messages, frontier._schema(record["apps"], record["operations"]),
                                             timeout=120)
                out = frontier.normalize_reply(out)
            except Exception as error:  # noqa: BLE001 -- one failed turn is recorded, the sweep goes on
                out, usage = None, {"error": str(error)[:200]}
            cost = frontier.cost_usd(model, {k: client.usage[k] - before.get(k, 0) for k in client.usage}) or 0.0
            line = {"key": key(folder, record["step"]), "model": model, "reasoning": reasoning, "lean": lean,
                    "out": out, "usage": usage, "cost": cost, "back": back,
                    "seconds": round(time.monotonic() - started, 3)}
            with lock:
                spent[0] += cost
                with out_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(line, ensure_ascii=False) + "\n")

    with concurrent.futures.ThreadPoolExecutor(threads) as pool:
        list(pool.map(lambda item: one_task(*item), tasks.items()))
    return spent[0]


def load(path):
    return {line["key"]: line for line in map(json.loads, pathlib.Path(path).read_text().splitlines())
            if line.get("out")}


def choices(path, recorded):
    """{key: first-action key} of a sweep file, rows resolved in the recorded prompt."""
    return {name: action_key(line["out"], recorded[name][1]["text"], line.get("back"))
            for name, line in load(path).items() if name in recorded}


def agreement(a, b):
    """(same, compared) first actions of two {key: action key} maps on their common turns."""
    common = set(a) & set(b)
    return sum(a[k] == b[k] for k in common), len(common)


def compare(files, paths):
    recorded = records(paths)
    base = {name: action_key(record["out"], record["text"]) for name, (_, record) in recorded.items()}
    picks = {pathlib.Path(f).stem: choices(f, recorded) for f in files}
    print(f"{'file':28} {'turns':>5} {'= recorded':>11} {'same op':>8} {'$/call':>8}")
    for f in files:
        name = pathlib.Path(f).stem
        lines = load(f)
        same, n = agreement(picks[name], base)
        ops = sum(picks[name][k][0] == base[k][0] for k in picks[name])
        cost = sum(line["cost"] for line in lines.values()) / max(1, len(lines))
        print(f"{name:28} {n:5} {same / max(1, n):10.1%} {ops / max(1, n):8.1%} {cost:8.5f}")
    names = list(picks)
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            same, n = agreement(picks[x], picks[y])
            print(f"{x} vs {y}: {same}/{n} = {same / max(1, n):.1%}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("run")
    go.add_argument("paths", nargs="+")
    go.add_argument("--out", required=True)
    go.add_argument("--model", default="gpt-5.6-terra")
    go.add_argument("--reasoning", default="low")
    go.add_argument("--full-rows", action="store_true", help="keep the rows lean_skip leaves out")
    go.add_argument("--fraction", type=float, default=1.0)
    go.add_argument("--seed", default="0")
    go.add_argument("--threads", type=int, default=4)
    go.add_argument("--max-usd", type=float, default=1.0)
    go.add_argument("--only-from", help="a sweep file: replay just its turns")
    go.add_argument("--env-file", action="append", default=["mobile_agent/.env"])
    cmp = sub.add_parser("compare")
    cmp.add_argument("files", nargs="+")
    cmp.add_argument("--runs", nargs="+", required=True)
    args = parser.parse_args(argv)
    if args.command == "compare":
        compare(args.files, args.runs)
        return
    load_envs([path for path in args.env_file if os.path.exists(os.path.expanduser(path))])
    only = set(load(args.only_from)) if args.only_from else None
    spent = run(args.paths, args.out, model=args.model, reasoning=args.reasoning, lean=not args.full_rows,
                fraction=args.fraction, seed=args.seed, threads=args.threads, max_usd=args.max_usd, only=only)
    print(f"spent ${spent:.4f}")


if __name__ == "__main__":
    main()

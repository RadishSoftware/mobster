"""Unit economics of each LLM as Smart's decider, on recorded Smart turns: latency, $ per call, agreement.

  python -m mobile_agent.bench.llm_units --runs research/iosworld-runs --n 60 \
      --models gpt-6-luna,gpt-6.1-sol,codex:gpt-6.1-sol,claude-cli:claude-haiku-5-5 --out DIR

Each case is one recorded turn: the exact prompt text and screenshot the model saw, re-asked with today's SYSTEM
and compact schema (frontier._schema with the recorded operations). Agreement: the re-asked first action has the
recorded operation and (for element actions) the same element id; "op" agreement counts the operation only. The
recorded actions are Sonnet 5.5's or gpt-5.6/6-sol's, so agreement is with a frontier model, not with ground truth.

Latency is measured from this Mac per call (sequential). For ``claude-cli:`` models the latency reported is the
CLI's own ``duration_api_ms`` (process start excluded; it still includes the CLI's second turn for the schema),
and $ is the list price of the tokens it reports, though the plan bills nothing per call.
"""

import argparse
import json
import random
import statistics
import time
from pathlib import Path

from .. import frontier


def cases(roots, n, seed=20261009):
    out = []
    for root in roots:
        for prompts in sorted(Path(root).glob("*/*/prompts.jsonl")):
            for line in prompts.read_text().splitlines():
                try:
                    turn = json.loads(line)
                except ValueError:
                    continue
                acts = (turn.get("out") or {}).get("actions") or []
                if not acts or not turn.get("image") or not (prompts.parent / turn["image"]).exists():
                    continue
                out.append((prompts.parent, turn))
    random.Random(seed).shuffle(out)
    return out[:n]


def messages(folder, turn):
    image = frontier.image_part("image/jpeg", (folder / turn["image"]).read_bytes())
    head, _, rest = turn["text"].partition("\n\nTurns used:")
    content = [{"type": "text", "text": head, "cache": True}, {"type": "text", "text": "\n\nTurns used:" + rest}, image]
    return [{"role": "system", "content": frontier.SYSTEM}, {"role": "user", "content": content}]


def price(model, usage):
    model = model.split(":", 1)[-1]
    return frontier.cost_usd(model, usage)


def run_model(model, picked, reasoning="low"):
    from .agents.smart import build_client
    client = build_client(model, reasoning)
    rows = []
    for folder, turn in picked:
        apps = [line.split(": ")[-1] for line in
                turn["text"].split("Apps you may use:\n", 1)[-1].split("\n\n")[0].splitlines()]
        schema = frontier._schema(apps, tuple(turn.get("operations") or frontier.OPERATIONS))
        started = time.monotonic()
        try:
            out, usage = client.complete(messages(folder, turn), schema, timeout=120)
            out = frontier.normalize_reply(out)
            error = None
        except Exception as exc:
            out, usage, error = {}, {}, f"{type(exc).__name__}: {str(exc)[:160]}"
        seconds = time.monotonic() - started
        if model.startswith("claude-cli:") and getattr(client, "last_api_ms", None):
            seconds = client.last_api_ms / 1000
        want = turn["out"]["actions"][0]
        got = (out.get("actions") or [{}])[0]
        rows.append({"run": str(folder), "step": turn.get("step"), "s": round(seconds, 3), "error": error,
                     "usd": price(model, usage) if usage else None, "in": usage.get("prompt_tokens"),
                     "out": usage.get("completion_tokens"), "want": [want.get("operation"), want.get("target")],
                     "got": [got.get("operation"), got.get("target")],
                     "op_agree": got.get("operation") == want.get("operation"),
                     "agree": got.get("operation") == want.get("operation") and (
                         got.get("target") == want.get("target") or want.get("operation") not in frontier.ELEMENT_OPS)})
    ok = [r for r in rows if not r["error"]]
    lat = sorted(r["s"] for r in ok)
    usd = [r["usd"] for r in ok if r["usd"] is not None]
    return rows, {"model": model, "reasoning": reasoning, "n": len(rows), "errors": len(rows) - len(ok),
                  "agree": round(sum(r["agree"] for r in ok) / len(ok), 3) if ok else None,
                  "op_agree": round(sum(r["op_agree"] for r in ok) / len(ok), 3) if ok else None,
                  "p50_s": lat[len(lat) // 2] if lat else None, "p90_s": lat[int(.9 * len(lat)) - 1] if lat else None,
                  "usd_call": round(statistics.mean(usd), 5) if usd else None,
                  "in_tokens": round(statistics.mean(r["in"] or 0 for r in ok)) if ok else None,
                  "out_tokens": round(statistics.mean(r["out"] or 0 for r in ok)) if ok else None}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.llm_units")
    parser.add_argument("--runs", action="append", required=True)
    parser.add_argument("--n", type=int, default=60)
    parser.add_argument("--models", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--reasoning", default="low")
    args = parser.parse_args(argv)
    picked = cases(args.runs, args.n)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for model in args.models.split(","):
        rows, summary = run_model(model, picked, args.reasoning)
        name = model.replace(":", "_") + "-" + args.reasoning
        (out / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        with open(out / "summary.jsonl", "a") as handle:
            handle.write(json.dumps(summary) + "\n")
        print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

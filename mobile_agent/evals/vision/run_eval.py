"""Live evaluation of VisionJudge on the labelled offline set (manifest.json).

Costs money: every run calls the configured Gemini helper. Nothing touches a
phone. Examples:

    python -m mobile_agent.evals.vision.run_eval --env-file "$HOME/Library/Application Support/app.mobster.desktop/agent.env" \\
        --run cascade --out results/cascade.jsonl
    python -m mobile_agent.evals.vision.run_eval --run single --limit 30 --out results/single.jsonl
    python -m mobile_agent.evals.vision.run_eval --summarize results/cascade.jsonl

Crops are degraded to what the phone path supplies: T1 sees the image with
its long side at 320 px and JPEG quality 50 (a photo cell in WDA's half-scale,
quality-50 MJPEG); T2 ("look again") gets the stored 512-px image, standing in
for a full-resolution still.
"""

import argparse
import io
import json
import math
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EYE_CHOICES = ("blue", "green", "grey", "hazel", "brown", "unsure")


def load_manifest():
    return json.loads((ROOT / "manifest.json").read_text())


def degrade(path, side=320, quality=50):
    from PIL import Image

    image = Image.open(path).convert("RGB")
    image.thumbnail((side, side))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


class Recorder:
    """Wraps helpers so every request's tier, size, latency and raw answer is kept."""

    def __init__(self, factory):
        self.factory, self.calls, self.lock = factory, [], threading.Lock()

    def __call__(self, model=None):
        recorder, helper = self, self.factory(model)

        class Recording:
            supports_logprobs = getattr(helper, "supports_logprobs", False)

            def complete(self, messages, token_limit, timeout, purpose, **options):
                images = sum(1 for p in messages[1]["content"] if p.get("type") == "image_url")
                started = time.monotonic()
                entry = {"model": helper.model, "resolution": options.get("media_resolution"), "images": images,
                         "logprobs": "logprobs" in options, "started": started}
                try:
                    result = helper.complete(messages, token_limit, timeout, purpose, **options)
                    entry.update(ok=True, content=result["choices"][0]["message"]["content"],
                                 usage=result.get("usage"))
                    return result
                except Exception as exc:
                    entry.update(ok=False, error=f"{type(exc).__name__}: {str(exc)[:160]}")
                    raise
                finally:
                    entry["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
                    with recorder.lock:
                        recorder.calls.append(entry)

            def warm(self):
                return helper.warm()

            def close(self):
                helper.close()
        return Recording()


def spec(manifest, predicate, holistic=False):
    from mobile_agent.vision_judge import eye_colour_steps, person_gate_steps

    entry = manifest["predicates"][predicate]
    steps = None
    if not holistic and predicate == "eye_colour":
        steps = eye_colour_steps()
    if not holistic and predicate == "sunglasses":
        steps = person_gate_steps(entry["question"], entry["choices"])
    return entry["question"], tuple(entry["choices"]), steps


def run(args):
    from mobile_agent.config import load_env_file
    from mobile_agent.models import Helper
    from mobile_agent.vision_judge import EYE_COLOUR_THRESHOLDS, AppleVisionScreen, JudgeConfig, VisionJudge

    if args.env_file:
        load_env_file(args.env_file)
    manifest = load_manifest()
    items = [i for i in manifest["items"] if not args.predicate or i["predicate"] in args.predicate]
    if args.limit:
        # Deterministic spread across predicates.
        items = items[::max(1, len(items) // args.limit)][:args.limit]
    recorder = Recorder(lambda model=None: Helper(model=model))
    local = AppleVisionScreen() if args.t0 else None

    def judge_for(predicate):
        thresholds = EYE_COLOUR_THRESHOLDS if predicate == "eye_colour" else {}
        config = JudgeConfig(model=args.model, batch_size=args.batch, t2_enabled=args.t2, t2_model=args.t2_model,
                             logprobs="auto", thresholds=thresholds,
                             hedge_after_seconds=None if args.no_hedge else 1.2)
        return VisionJudge(helper_factory=recorder, config=config, local=local)
    out = open(args.out, "w")
    by_predicate = {}
    for item in items:
        by_predicate.setdefault(item["predicate"], []).append(item)
    judges = []
    for predicate, group in by_predicate.items():
        question, choices, steps = spec(manifest, predicate, args.holistic)
        judge = judge_for(predicate)
        judges.append(judge)
        for start in range(0, len(group), args.batch):
            chunk = group[start:start + args.batch]
            crops = [[degrade(ROOT / item["file"])] for item in chunk]
            hires = [[(ROOT / item["file"]).read_bytes()] for item in chunk]
            before = len(recorder.calls)
            started = time.monotonic()
            results = judge.judge_many(question, crops, choices, timeout=args.timeout, steps=steps, hires=hires,
                                       predicate=predicate)
            wall = round((time.monotonic() - started) * 1000, 1)
            calls = recorder.calls[before:]
            for item, result in zip(chunk, results):
                crop = result.per_crop[0]
                out.write(json.dumps({
                    "run": args.run, "id": item["id"], "predicate": predicate, "label": item["label"],
                    "accept": item["accept"], "difficulty": item["difficulty"], "entity": item.get("entity"),
                    "answer": result.answer, "score": result.score, "tier": result.tier,
                    "abstain_reason": result.abstain_reason, "latency_ms": result.latency_ms, "batch_wall_ms": wall,
                    "batch_size": len(chunk), "calls": [{k: c[k] for k in ("model", "resolution", "images",
                                                                            "latency_ms", "ok", "logprobs")}
                                                       for c in calls],
                    "usage": [c.get("usage") for c in calls],
                    "steps": crop.steps}) + "\n")
            out.flush()
            print(f"{predicate} {start + len(chunk)}/{len(group)} wall {wall} ms calls {len(calls)}", file=sys.stderr)
    for judge in judges:
        judge.close()
    if local:
        local.close()
    (Path(args.out).with_suffix(".calls.json")).write_text(json.dumps(
        [{k: v for k, v in c.items() if k != "started"} for c in recorder.calls], indent=0))


def correct(row):
    return row["answer"] in row["accept"]


def pct(n, d):
    return None if d == 0 else round(100 * n / d, 1)


def quantile(values, q):
    values = sorted(values)
    if not values:
        return None
    return values[min(len(values) - 1, int(math.floor(q * len(values))))]


def summarize(paths):
    rows = [json.loads(line) for path in paths for line in open(path)]
    report = {}
    for predicate in sorted({r["predicate"] for r in rows}):
        group = [r for r in rows if r["predicate"] == predicate]
        answered = [r for r in group if r["answer"] not in ("unsure",)]
        answerable = [r for r in group if r["label"] != "unsure"]
        unanswerable = [r for r in group if r["label"] == "unsure"]
        report[predicate] = {
            "n": len(group),
            "accuracy_incl_abstain_as_wrong": pct(sum(correct(r) for r in group), len(group)),
            "precision_when_answering": pct(sum(correct(r) for r in answered), len(answered)),
            "coverage_answerable": pct(sum(r["answer"] != "unsure" for r in answerable), len(answerable)),
            "abstention_rate": pct(len(group) - len(answered), len(group)),
            "errors": [r["id"] + f" ({r['label']}->{r['answer']})" for r in answered if not correct(r)],
            "false_answer_on_unanswerable": pct(sum(r["answer"] != "unsure" for r in unanswerable), len(unanswerable)),
            "tiers": {t: sum(r["tier"] == t for r in group) for t in ("t0", "t1", "t2")},
        }
    calls = [c for r in rows for c in r["calls"]]
    unique = {}
    for r in rows:
        unique.setdefault((r["predicate"], r["batch_wall_ms"]), r)
    lat = {}
    for c in calls:
        lat.setdefault((c["resolution"], c["images"]), []).append(c["latency_ms"])
    report["_latency"] = {
        "per_call_by_resolution_and_images": {f"{k[0]} x{k[1]}": {"n": len(v), "p50": quantile(v, .5),
                                                                   "p90": quantile(v, .9)}
                                              for k, v in sorted(lat.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))},
        "judgment_wall_p50": quantile([r["batch_wall_ms"] for r in unique.values()], .5),
        "judgment_wall_p90": quantile([r["batch_wall_ms"] for r in unique.values()], .9),
    }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file")
    parser.add_argument("--run", default="cascade")
    parser.add_argument("--out")
    parser.add_argument("--predicate", action="append")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--model")
    parser.add_argument("--t2-model")
    parser.add_argument("--no-t2", dest="t2", action="store_false")
    parser.add_argument("--t0", action="store_true")
    parser.add_argument("--holistic", action="store_true")
    parser.add_argument("--no-hedge", action="store_true")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--summarize", nargs="+")
    args = parser.parse_args(argv)
    if args.summarize:
        print(json.dumps(summarize(args.summarize), indent=1))
        return
    if not args.out:
        parser.error("--out is required for a run")
    run(args)


if __name__ == "__main__":
    main()

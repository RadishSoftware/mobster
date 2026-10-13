"""Offline gate for decision models on recorded Smart turns: would a decider have picked the element Smart did?

  python -m mobile_agent.bench.decide_gate --runs research/iosworld-runs --n 200 \
      --deciders jev,openai-text,openai-marks --framings ground,delegate --out DIR

Each case is one recorded ``frontier_prompt`` turn (prompts.jsonl + the screenshot the model saw) whose first
action was a TAP on a listed element that commits nothing (decider_scope's rule). Two framings:

- ``ground``: the planner's own words for the step (the reply's thought) are the instruction; the decider picks
  the mark. This is the compile-execute shape (the planner says what, the decider finds where).
- ``delegate``: only the request, the plan written so far and the recent actions; the decider picks the next tap
  or "none" (not a tap now). This is idea 4's DELEGATE gate: it passes at >= 85% agreement.

Deciders: ``jev`` (marks as text), ``openai-text`` (gpt-6-luna, marks as text), ``openai-marks`` (gpt-6-luna,
marks as text plus the screenshot with the numbered boxes drawn). Reported per decider and framing: agreement
with Smart, abstentions, latency p50/p90 from this Mac, $ (input tokens at list price), refusals and errors, and
calibration (agreement by top-probability bucket). The recorded runs are iOSWorld simulator apps: no task of the
held-out tranche is used.
"""

import argparse
import concurrent.futures
import json
import random
import re
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

ROW = re.compile(r'^(e\d+) (\S+) (?:"((?:[^"\\]|\\.)*)"|\(no label[^)]*\))(.*?)@(\d+),(\d+),(\d+),(\d+)\s*$')
BUCKETS = (.5, .7, .85, .95, 1.01)


def parse_rows(text):
    """[(alias, element)] from a recorded prompt's "Screen elements:" block."""
    block = text.split("\nScreen elements:\n", 1)[-1]
    rows = []
    for line in block.splitlines():
        match = ROW.match(line.strip())
        if not match:
            continue
        alias, role, label, rest, x, y, w, h = match.groups()
        try:
            label = json.loads(f'"{label}"') if label is not None else ""
        except ValueError:
            label = label or ""
        value = re.search(r'value=("(?:[^"\\]|\\.)*")', rest or "")
        rows.append((alias, SimpleNamespace(label=label, role=role, editable=" editable" in (rest or ""),
                                            value=json.loads(value.group(1)) if value else "",
                                            rect=(int(x) / 100, int(y) / 100, int(w) / 100, int(h) / 100))))
    return rows


def section(text, head, stop="\n\n"):
    start = text.find(head)
    if start < 0:
        return ""
    start += len(head)
    end = text.find(stop, start)
    return text[start:end if end >= 0 else None].strip()


def load_cases(roots, n, seed=20261009, max_marks=120, min_rows=0):
    from ..decider_scope import commits
    from ..marks import element_marks
    cases = []
    for root in roots:
        for prompts in sorted(Path(root).glob("*/*/prompts.jsonl")):
            for line in prompts.read_text().splitlines():
                try:
                    turn = json.loads(line)
                except ValueError:
                    continue
                out = turn.get("out") or {}
                acts = out.get("actions") or []
                if not acts or acts[0].get("operation") != "TAP" or not acts[0].get("target"):
                    continue
                image = turn.get("image")
                if not image or not (prompts.parent / image).exists():
                    continue
                rows = parse_rows(turn.get("text") or "")
                if len(rows) < min_rows:
                    continue
                by_alias = dict(rows)
                chosen = by_alias.get(acts[0]["target"])
                if chosen is None or commits(chosen):
                    continue
                marks = element_marks([e for _, e in rows], limit=max_marks)
                if not any(m.element is chosen for m in marks):
                    continue
                cases.append({"run": str(prompts.parent), "step": turn.get("step"), "text": turn["text"],
                              "image": str(prompts.parent / image), "thought": out.get("thought") or "",
                              "marks": marks, "want": next(m.id for m in marks if m.element is chosen),
                              "want_label": chosen.label, "n_rows": len(rows)})
    random.Random(seed).shuffle(cases)
    return cases[:n]


def question(case, framing):
    from ..decision_provider import Question
    from ..decider_scope import TARGET, admit
    from ..marks import options
    admit(TARGET, op="TAP")
    request = section(case["text"], "Request: ")
    marks = [m for m in case["marks"]]
    if framing == "ground":
        context = (f"An iPhone screen. The agent's next step, in its own words: {case['thought']}\n"
                   "Pick the marked element that this step taps.")
        instructions = "Which marked element does the step tap? Answer none if no mark fits."
    else:
        plan = section(case["text"], "Plan so far: ")
        recent = section(case["text"], "Recent actions (oldest first):\n", "\n\n")
        context = (f"An iPhone agent is working on this request: {request}\nIts plan so far: {plan or '(none)'}\n"
                   f"Recent actions:\n{recent or '(none)'}\nPick the element the agent should tap next.")
        instructions = ("Which marked element should be tapped next to progress the request? Answer none if the "
                        "next step is not a tap on one of these elements.")
    context += "\nEach option is a marked element: id, role, label, @x,y,w,h in % of the screen."
    return context, {"target": Question("choice", instructions, options(marks))}


def hedged(provider, context, questions, image, copies):
    """The first of ``copies`` identical requests sent at once (each is billed)."""
    if copies <= 1:
        return provider.ask(context, questions, image=image, timeout=30)
    pool = concurrent.futures.ThreadPoolExecutor(copies)
    futures = [pool.submit(provider.ask, context, questions, image, 30) for _ in range(copies)]
    try:
        for future in concurrent.futures.as_completed(futures):
            if future.exception() is None:
                return future.result()
        return futures[0].result()
    finally:
        pool.shutdown(wait=False)


def ask(provider, case, framing, with_image, copies=1):
    from ..marks import draw
    context, questions = question(case, framing)
    image = draw(Path(case["image"]).read_bytes(), case["marks"]) if with_image else None
    started = time.monotonic()
    try:
        answers = hedged(provider, context, questions, image, copies)
    except Exception as error:
        return {"error": f"{type(error).__name__}: {str(error)[:200]}", "s": time.monotonic() - started}
    answer = answers.get("target")
    if answer is None:
        return {"error": "malformed", "s": time.monotonic() - started}
    return {"choice": answer.choice, "top": answer.top, "s": time.monotonic() - started}


def summarize(results, provider):
    done = [r for r in results if "choice" in r]
    agree = [r for r in done if r["choice"] == r["want"]]
    lat = sorted(r["s"] for r in results)
    calib = []
    lo = 0.0
    for hi in BUCKETS:
        bucket = [r for r in done if lo <= (r["top"] or 0) < hi]
        if bucket:
            calib.append({"range": [lo, min(hi, 1.0)], "n": len(bucket),
                          "agree": round(sum(r["choice"] == r["want"] for r in bucket) / len(bucket), 3),
                          "mean_top": round(statistics.mean(r["top"] or 0 for r in bucket), 3)})
        lo = hi
    confident = [r for r in done if (r["top"] or 0) >= .9 and r["choice"] != "none"]
    return {"cases": len(results), "answered": len(done), "errors": len(results) - len(done),
            "agreement": round(len(agree) / len(results), 3) if results else None,
            "abstained": sum(r["choice"] == "none" for r in done),
            "confident_n": len(confident),
            "confident_agreement": round(sum(r["choice"] == r["want"] for r in confident) / len(confident), 3)
            if confident else None,
            "p50_s": round(lat[len(lat) // 2], 3) if lat else None,
            "p90_s": round(lat[int(len(lat) * .9) - 1], 3) if lat else None,
            "usd": round(provider.cost_usd, 4), "input_tokens": provider.input_tokens, "calibration": calib,
            "error_kinds": sorted({r["error"][:80] for r in results if "error" in r})[:5]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.decide_gate")
    parser.add_argument("--runs", action="append", required=True)
    parser.add_argument("--n", type=int, default=150)
    parser.add_argument("--deciders", default="jev,openai-text,openai-marks")
    parser.add_argument("--framings", default="ground,delegate")
    parser.add_argument("--max-marks", type=int, default=120)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--min-rows", type=int, default=0, help="only screens with at least this many rows")
    parser.add_argument("--hedge", type=int, default=1, help="identical requests sent at once; the first answer wins")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    from ..decision_provider import JevDecisions, OpenAIDecisions
    cases = load_cases(args.runs, args.n, max_marks=args.max_marks, min_rows=args.min_rows)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {"cases": len(cases), "max_marks": args.max_marks, "hedge": args.hedge, "min_rows": args.min_rows,
              "marks_per_case_p50": statistics.median(len(c["marks"]) for c in cases) if cases else None,
              "results": {}}
    for decider in args.deciders.split(","):
        for framing in args.framings.split(","):
            provider = JevDecisions() if decider == "jev" else OpenAIDecisions()
            with_image = decider == "openai-marks"
            with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
                rows = list(pool.map(lambda c: {**ask(provider, c, framing, with_image, args.hedge), "want": c["want"],
                                                "marks": len(c["marks"]),
                                                "run": c["run"], "step": c["step"]}, cases))
            key = f"{decider}/{framing}"
            report["results"][key] = summarize(rows, provider)
            (out / f"{decider}-{framing}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            print(key, json.dumps({k: v for k, v in report["results"][key].items() if k != "calibration"}))
    (out / "report.json").write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

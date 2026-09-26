"""Calibrate the answer verifier on real evidence with known right and wrong answers.

Bench attempts save their answer stage (research/bench-runs/<run>/artifacts/*/answer-stage.json:
request, schema, evidence). For each one whose ground truth is a literal in that evidence,
this asks the verifier about the true literal and about decoys taken from the same
evidence (other literals of the same shape: numbers for numbers, dates for dates), and
reports how well each signal separates them and how the acceptance rules in task_policy
would fare. Live bench runs only ever showed correct candidates (24 Sep: 36 of 36), so
without decoys the rules' precision was unmeasured.

    python -m mobile_agent.evals.verifier_calibration research/bench-runs/mb-pass4 [...] \\
        --env-file ~/Library/Application\\ Support/app.mobster.desktop/agent.env

Model calls only; never touches the phone.
"""

import argparse
import glob
import json
import os
import random
import re

from .calibrate import auroc

NUMBER = re.compile(r"^\d[\d,.]*$")
DATE = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b|\b(January|February|March|April|May|June|July|August|September|"
                  r"October|November|December)\b", re.I)


def shape(text):
    if NUMBER.match(text.strip()):
        return "number"
    if DATE.search(text):
        return "date"
    return "text"


def literal_pool(evidence):
    """(literal, entry) for every whole entry text and its comma parts."""
    pool = []
    for entry in evidence["entries"]:
        text = entry["text"].strip()
        for part in [text] + [p.strip() for p in text.split(",") if "," in text]:
            if 1 <= len(part) <= 80:
                pool.append((part, entry))
    return pool


def expected_literals(task_id, device="iphone15pro"):
    """The task's ground truth when it is a written literal or recorded device truth; else (None, None)."""
    from ..bench.checks import Literal, Truth
    from ..bench.suite import build_suite
    from ..bench.truth import TruthStore
    task = {t.id: t for t in build_suite()}[task_id]
    check = next((c for c in task.checks if type(c).__name__ == "AnswerIs"), None)
    if check is None:
        return None, None
    if isinstance(check.expect, Literal):
        value = check.expect.value
    elif isinstance(check.expect, Truth):
        value = TruthStore(device).get(check.expect.key)
    else:
        return None, None
    values = value if isinstance(value, (list, tuple)) else [value]
    return [str(v) for v in values if v is not None] + list(check.alternatives), check.mode


def cases(artifact, rng, decoys=3):
    from ..bench.checks import matches
    stage = json.load(open(artifact))
    field = next(iter(stage["schema"]["properties"]), None) if stage.get("schema") else None
    expected, mode = expected_literals(stage["task"])
    if not field or not expected:
        return []
    pool = literal_pool(stage["evidence"])
    right = [(lit, e) for lit, e in pool if matches(lit, expected, mode)]
    if not right:
        return []
    literal, entry = right[0]
    kind = shape(literal)
    wrong = [(lit, e) for lit, e in pool if shape(lit) == kind and not matches(lit, expected, mode)
             and lit != literal]
    rng.shuffle(wrong)
    out = [(True, literal, entry)] + [(False, lit, e) for lit, e in wrong[:decoys]]
    return [{"task": stage["task"], "goal": stage["goal"], "evidence": stage["evidence"], "field": field,
             "label": label, "literal": lit, "entry": e} for label, lit, e in out]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--env-file")
    parser.add_argument("--decoys", type=int, default=3)
    parser.add_argument("--out", default="research/verifier-calibration.jsonl")
    args = parser.parse_args(argv)
    if args.env_file:
        from ..config import load_env_file
        load_env_file(os.path.expanduser(args.env_file))
    from ..compose import build_models, close_all
    from ..extraction import Evidence
    from ..task_policy import agreement_reason
    model, helper = build_models(helper=False)
    rng = random.Random(7)
    rows = []
    artifacts = sorted({path for run in args.runs for path in glob.glob(f"{run}/artifacts/*/answer-stage.json")})
    seen_tasks = set()
    for artifact in artifacts:
        for case in cases(artifact, rng, args.decoys):
            if (case["task"], case["literal"]) in seen_tasks:
                continue
            seen_tasks.add((case["task"], case["literal"]))
            candidate = {"data": {case["field"]: case["literal"]},
                         "citations": [{"path": "/" + case["field"], "evidence_id": case["entry"]["id"],
                                        "quote": case["entry"]["text"]}]}
            store = Evidence()  # the agent's own compaction for the verifier (cited first, then relevant)
            store.entries = [dict(entry, rect=tuple(entry["rect"])) for entry in case["evidence"]["entries"]]
            store.latest_step = case["evidence"].get("latest_step", 0)
            compact = store.for_verification(candidate["citations"], goal=case["goal"])
            try:
                support, signals = model.verify_output_signals(
                    case["goal"], candidate, compact, timeout=30,
                    current_screen={"elements": [], "text": "", "source": "wda"})
            except Exception as error:
                print("skip", case["task"], type(error).__name__)
                continue
            row = {"task": case["task"], "label": case["label"], "support": getattr(support, "value", str(support)),
                   "rule": agreement_reason(signals), **{k: v for k, v in signals.items() if isinstance(v, float)}}
            rows.append(row)
            print(json.dumps(row))
    close_all(model)
    with open(args.out, "w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    positives = sum(r["label"] for r in rows)
    print(f"\n{len(rows)} candidates ({positives} right, {len(rows) - positives} decoys)")
    for signal in ("p_supported", "claim_final_state", "claim_entity", "claim_field"):
        pairs = [(r[signal], r["label"]) for r in rows if isinstance(r.get(signal), float)]
        print(f"  AUROC {signal:18} {auroc(pairs)}")
    for name, accept in (("verifier SUPPORTED", lambda r: r["support"] == "supported"),
                         ("rule agreement", lambda r: r["rule"] == "agreement"),
                         ("rule claims_calibrated", lambda r: r["rule"] == "claims_calibrated")):
        accepted = [r for r in rows if accept(r)]
        wrong = [r for r in accepted if not r["label"]]
        print(f"  {name:24} accepts {len(accepted):3}  of which wrong {len(wrong):3}  "
              f"(recall {sum(r['label'] for r in accepted)}/{positives})")


if __name__ == "__main__":
    main()

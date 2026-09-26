"""Calibrate acceptance thresholds on oracle-labelled runs.

The harness grades every run against ground truth the agent never sees. This
joins those labels with the numbers each run logged (never answer text):

* ``answer_signals``: verifier verdict probabilities and per-claim Nouls for
  every answer candidate, labelled by comparing the candidate's digest with
  the task's ground truth (so a correct answer the verifier rejected still
  counts as correct);
* ``decision``: operation confidences, labelled by whether the run passed.

It reports how well each signal separates correct from incorrect (AUROC) and
the precision/recall of candidate thresholds, so the values in task_policy are
chosen from data instead of guessed.

    python -m mobile_agent.evals.calibrate research/web-task-eval-*.jsonl \
        --api http://127.0.0.1:8765
"""

import argparse
import json
import urllib.request

ANSWER_ORACLES = {"ReturnedValue", "ReturnedMatches"}
SIGNALS = ("p_supported", "claim_final_state", "claim_entity", "claim_field")


def auroc(pairs):
    """Probability that a random positive outscores a random negative (ties count half)."""
    positives = [score for score, label in pairs if label]
    negatives = [score for score, label in pairs if not label]
    if not positives or not negatives:
        return None
    wins = sum((p > n) + .5 * (p == n) for p in positives for n in negatives)
    return wins / (len(positives) * len(negatives))


def threshold_table(pairs, thresholds=(.3, .4, .5, .6, .7, .8, .9)):
    rows = []
    positives = sum(label for _, label in pairs)
    for threshold in thresholds:
        accepted = [label for score, label in pairs if score >= threshold]
        precision = sum(accepted) / len(accepted) if accepted else None
        recall = sum(accepted) / positives if positives else None
        rows.append((threshold, len(accepted), precision, recall))
    return rows


def answer_label(record):
    """True/False when the record's answer oracle ran, else None (run-level; see candidate_label)."""
    checks = [item for item in record.get("oracles", []) if item.get("oracle") in ANSWER_ORACLES]
    return all(item["ok"] for item in checks) if checks else None


def expected_digests(device):
    """Digest of each task's ground-truth answer, for tasks graded by exact value."""
    from ..agent import answer_digest
    from .oracles import ReturnedValue
    from .tasks import suite
    digests = {}
    for task in suite(device, "all"):
        values = [oracle for oracle in task.oracles if isinstance(oracle, ReturnedValue) and oracle.path]
        if len(values) == 1:
            field = values[0].path.strip("/")
            digests[task.id] = answer_digest({field: values[0].expected})
    return digests


def candidate_label(record, event, digests):
    """Whether this candidate IS the ground truth (not whether the run returned it).

    A correct answer the verifier rejected makes the run fail its oracle; a
    run-level label would call that correct candidate wrong.
    """
    expected = digests.get(record.get("task"))
    if expected is None or not event.get("candidate_sha"):
        return None
    return event["candidate_sha"] == expected


def collect(paths, api, device="iphone15pro"):
    answers, decisions = [], []
    digests = expected_digests(device)
    for path in paths:
        for line in open(path):
            record = json.loads(line)
            metrics = record.get("metrics")
            if metrics is not None:
                # In-process runs carry their numbers inline (evals/harness.py --in-process).
                events = ([{"event": "answer_signals", **item} for item in metrics.get("answer_signals", [])]
                          + [{"event": "decision", **item} for item in metrics.get("decisions", [])])
            elif record.get("run_id"):
                with urllib.request.urlopen(f"{api.rstrip('/')}/api/runs/{record['run_id']}") as response:
                    events = json.load(response)["run"].get("events", [])
            else:
                continue
            for event in events:
                if event.get("event") == "answer_signals":
                    label = candidate_label(record, event, digests)
                    if label is not None:
                        answers.append(({k: event.get(k) for k in SIGNALS} | {"agree": event.get("agree")}, label))
                if event.get("event") == "decision":
                    decisions.append(({"operation": event.get("operation"), "confidence": event.get("confidence"),
                                       "demoted_from": event.get("demoted_from")}, bool(record.get("passed"))))
    return answers, decisions


def report(answers, decisions):
    print(f"answer candidates: {len(answers)} ({sum(l for _, l in answers)} correct)")
    for name in SIGNALS:
        pairs = [(features[name], label) for features, label in answers if isinstance(features.get(name), float)]
        score = auroc(pairs)
        print(f"  {name:18} n={len(pairs):3}  AUROC={'-' if score is None else f'{score:.2f}'}")
        for threshold, accepted, precision, recall in threshold_table(pairs):
            if accepted:
                print(f"      >= {threshold:.1f}: accepted {accepted:3}  precision {precision:.2f}  recall {recall:.2f}")
    minimum = [(min(features[n] for n in SIGNALS[1:]), label) for features, label in answers
               if all(isinstance(features.get(n), float) for n in SIGNALS[1:])]
    if minimum:
        print(f"  min(claims)        n={len(minimum):3}  AUROC={auroc(minimum) or 0:.2f}")
    by_operation = {}
    for features, passed in decisions:
        by_operation.setdefault(features["operation"], []).append((features["confidence"], passed))
    print(f"decisions: {len(decisions)}")
    for operation, pairs in sorted(by_operation.items()):
        pairs = [(c, p) for c, p in pairs if isinstance(c, (int, float))]
        score = auroc(pairs)
        print(f"  {operation:12} n={len(pairs):3}  run-pass AUROC={'-' if score is None else f'{score:.2f}'}")


def main():
    parser = argparse.ArgumentParser(description="Calibrate acceptance thresholds on labelled runs")
    parser.add_argument("records", nargs="+", help="Harness --out JSONL files")
    parser.add_argument("--api", default="http://127.0.0.1:8765", help="The server that ran them")
    parser.add_argument("--device", default="iphone15pro", help="Whose ground truth labels the candidates")
    args = parser.parse_args()
    report(*collect(args.records, args.api, args.device))


if __name__ == "__main__":
    main()

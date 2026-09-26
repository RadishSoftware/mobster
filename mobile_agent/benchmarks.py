"""Read-only latency analysis for Mobster run exports and Gemini evaluation JSONL.

python -m mobile_agent.benchmarks --api http://127.0.0.1:8765/api
python -m mobile_agent.benchmarks --runs downloaded-run.json
python -m mobile_agent.benchmarks --model-samples gemini-evaluation.jsonl

Never launches an app, sends an action, calls a model, or writes a report file.
Reports exclude goals, screen contents, credentials, and extracted answers.
"""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import re
import statistics
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from .state import has_trait
from .transport import decode_json


MAX_BYTES = 32 * 1024 * 1024
MAX_RUNS = 100
HELPER_PURPOSES = {"planning", "text", "recovery", "extraction"}
JEV_STAGES = {"decision": "jev_request", "verification": "jev_verification",
              "action_verification": "jev_action_verification"}
INFERENCE_EVENTS = {"inference_started", "inference_finished"}


@dataclass(frozen=True)
class Sample:
    run_id: str
    app: str
    status: str
    stage: str
    ms: float
    phase: str = "unknown"
    clock: str = "stage"
    provider: str = "unknown"
    model: str = "unknown"
    outcome: str = "unknown"


def duration(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def distribution(values):
    """Nearest-rank p95; no interpolation or confidence claim for tiny samples."""
    values = list(values)
    if not values or any(not duration(value) for value in values):
        raise ValueError("Latency samples must be finite nonnegative numbers")
    values.sort()
    return {"n": len(values), "median_ms": round(statistics.median(values), 2),
            "p95_ms": round(values[math.ceil(.95 * len(values)) - 1], 2),
            "min_ms": round(values[0], 2), "max_ms": round(values[-1], 2),
            "total_ms": round(sum(values), 2), "small_sample": len(values) < 20}


def aggregate(samples):
    groups = defaultdict(list)
    for sample in samples:
        groups[(sample.app, sample.status, sample.stage, sample.phase, sample.clock,
                sample.provider, sample.model, sample.outcome)].append(sample.ms)
    return [{"app": app, "status": status, "stage": stage, "phase": phase,
             "clock": clock, "provider": provider, "model": model, "outcome": outcome,
             **distribution(values)}
            for (app, status, stage, phase, clock, provider, model, outcome), values
            in sorted(groups.items())]


def inference_samples(run_id, app, status, events):
    """Read each canonical provider call once, including failed and unfinished calls."""
    samples, diagnostics = [], []
    calls, started, finished, counts = {}, set(), set(), Counter()
    for event in events:
        name = event.get("event")
        if not isinstance(name, str) or name not in INFERENCE_EVENTS:
            continue
        call_id, provider, purpose, model = (event.get(key) for key in
                                            ("call_id", "provider", "purpose", "model"))
        valid_purpose = (isinstance(provider, str) and isinstance(purpose, str) and
                         (provider == "typesafe" and purpose in JEV_STAGES or
                          provider in {"google", "helper"} and purpose in HELPER_PURPOSES))
        if (not isinstance(call_id, str) or not 1 <= len(call_id) <= 128
                or not isinstance(model, str) or not 1 <= len(model) <= 200 or not valid_purpose):
            diagnostics.append({"run_id": run_id, "stage": "inference",
                                "issue": "invalid inference identity or purpose"})
            continue
        stage = JEV_STAGES[purpose] if provider == "typesafe" else "helper_" + purpose
        warning = {"run_id": run_id, "stage": stage, "call_id": call_id}
        if call_id not in calls:
            phase = "first_in_provider" if counts[provider] == 0 else "subsequent_in_provider"
            calls[call_id] = (provider, purpose, phase)
            counts[provider] += 1
        previous_provider, previous_purpose, phase = calls[call_id]
        if (provider, purpose) != (previous_provider, previous_purpose):
            diagnostics.append({**warning, "issue": "inference identity changed within one call"})
            continue
        if name == "inference_started":
            if call_id in started or call_id in finished:
                diagnostics.append({**warning, "issue": "duplicate or out-of-order inference start"})
            started.add(call_id)
            continue
        if call_id in finished:
            diagnostics.append({**warning, "issue": "duplicate inference finish ignored"})
            continue
        finished.add(call_id)
        if not duration(event.get("latency_ms")):
            diagnostics.append({**warning, "issue": "missing, negative, or nonfinite clock"})
            continue
        if type(event.get("success")) is not bool:
            diagnostics.append({**warning, "issue": "missing or invalid provider outcome"})
            continue
        samples.append(Sample(run_id, app, status, stage, event["latency_ms"], phase,
                              "provider_request", provider, model,
                              "response_received" if event["success"] else "request_failed"))
    for call_id, (provider, purpose, _) in calls.items():
        if call_id not in finished:
            diagnostics.append({"run_id": run_id, "call_id": call_id,
                                "stage": JEV_STAGES[purpose] if provider == "typesafe" else "helper_" + purpose,
                                "issue": "inference has no finish event; duration unknown"})
    return samples, diagnostics


def run_samples(run):
    """Explicit stage clocks plus named wall-clock gaps, never falsely additive."""
    if not isinstance(run, dict) or not isinstance(run.get("id"), str):
        raise ValueError("Each run must be an object with a string ID")
    events = run.get("events", [])
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise ValueError("Run events must be an array of objects")
    if run.get("eventCount", 0) and not events:
        raise ValueError("Run metadata has no events; fetch /runs/{id} or export full details")
    app, status = run.get("appId", "unknown"), run.get("status", "unknown")
    if not isinstance(app, str) or not isinstance(status, str):
        raise ValueError("App and status must be strings")
    samples, diagnostics = inference_samples(run["id"], app, status, events)
    helper_instrumented = any(event.get("event") in ("inference_started", "inference_finished") and
                              event.get("provider") in ("google", "helper") for event in events)

    def add(stage, value, phase="unknown", clock="stage"):
        if duration(value):
            samples.append(Sample(run["id"], app, status, stage, value, phase, clock))
        else:
            diagnostics.append({"run_id": run["id"], "stage": stage,
                                "issue": "missing, negative, or nonfinite clock"})

    def gap(stage, end, start):
        if duration(end) and duration(start):
            add(stage, end - start)
        else:
            add(stage, None)

    if run.get("finishedAt") is not None:
        gap("task_wall", run["finishedAt"], run.get("createdAt"))
    summary = run.get("summary") or {}
    if not isinstance(summary, dict):
        raise ValueError("Run summary must be an object or null")
    if "elapsed_ms" in summary:
        add("agent_loop", summary["elapsed_ms"])
    timestamps = [event["timestamp"] for event in events if duration(event.get("timestamp"))]
    if any(later < earlier for earlier, later in zip(timestamps, timestamps[1:])):
        diagnostics.append({"run_id": run["id"], "issue": "wall clock moved backwards"})

    launches, decisions = {}, {}
    model_calls, first_observation, result_time = 0, True, None
    for event in events:
        name, timestamp = event.get("event"), event.get("timestamp")
        step = event.get("step")
        if name == "app_launch_started":
            launches["started"] = timestamp
        elif name == "app_launch_acknowledged" and "started" in launches:
            gap("app_launch_ack", timestamp, launches["started"])
        elif name == "observation" and first_observation:
            gap("before_first_observation", timestamp, run.get("createdAt"))
            first_observation = False
        elif name in {"decision", "completion_check"}:
            phase = "first_in_run" if model_calls == 0 else "subsequent_in_run"
            add("jev_decision" if name == "decision" else "completion_decision",
                event.get("latency_ms"), phase, "framework_wall")
            model_calls += 1
            if name == "decision":
                add("ax_observe" if has_trait(event.get("source"), "exact_actions") else "observe",
                    event.get("observe_ms"))
                if type(step) is int:
                    decisions[step] = timestamp
        elif name == "action_started" and type(step) is int and step in decisions:
            gap("pre_action_gap", timestamp, decisions[step])
        elif name == "action_acknowledged":
            add("action_ack", event.get("act_ms"))
        elif name == "observation_after_action":
            add("action_settle", event.get("settle_ms"))
        elif name == "helper" and "latency_ms" in event and not helper_instrumented:
            purpose = event.get("purpose")
            if purpose in HELPER_PURPOSES:
                add("helper_" + purpose, event["latency_ms"], clock="legacy_helper")
        elif name == "result":
            result_time = timestamp
    if result_time is not None and run.get("finishedAt") is not None:
        gap("post_result_tail", run["finishedAt"], result_time)
    return samples, diagnostics


def analyze_runs(runs):
    if not isinstance(runs, list) or len(runs) > MAX_RUNS:
        raise ValueError(f"Expected at most {MAX_RUNS} runs")
    samples, diagnostics, statuses = [], [], Counter()
    seen, live, excluded = set(), [], 0
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("id"), str):
            raise ValueError("Each run must be an object with a string ID")
        if run["id"] in seen:
            raise ValueError("Duplicate run IDs would bias the benchmark")
        seen.add(run["id"])
        if run.get("mode") != "live":
            excluded += 1
            continue
        current, warnings = run_samples(run)
        samples.extend(current)
        diagnostics.extend(warnings)
        statuses[run.get("status", "unknown")] += 1
        live.append(run)
    return {"schema_version": 2, "kind": "run_latencies", "live_runs": len(live),
            "excluded_non_live_runs": excluded, "statuses": dict(sorted(statuses.items())),
            "independently_verified_runs": sum((run.get("summary") or {}).get("independently_verified") is True
                                                for run in live),
            "stale_decisions": sum(event.get("event") == "stale_decision"
                                   for run in live for event in run.get("events", [])),
            "groups": aggregate(samples), "samples": [asdict(sample) for sample in samples],
            "diagnostics": diagnostics,
            "caveats": ["Demo and preview runs are excluded. Statuses and apps are not pooled.",
                        "First/subsequent call is not proven cold/warm infrastructure; thermal state is unknown.",
                        "p95 uses nearest rank; small samples are descriptive, not a reliable tail estimate.",
                        "Wall-clock gaps include scheduling and journaling; they are not exclusive stage timings.",
                        "Task wall and agent loop overlap other stages. Never add every row together.",
                        "Provider request clocks include transport and adapter work, not server-only model time.",
                        "Framework wall clocks include provider requests; no exclusive overhead is inferred without call IDs.",
                        "Legacy helper clocks are used only without canonical helper telemetry; their clock boundary is unknown.",
                        "Provider outcomes stay separate; receiving a response does not prove a valid answer or task success.",
                        "A fast error or acknowledgment is not a successfully completed task."]}


def analyze_model_samples(records):
    samples, seen = [], Counter()
    for record in records:
        if not isinstance(record, dict) or "sample" not in record:
            continue  # The evaluation also emits a final summary line.
        sample = record["sample"]
        if (not isinstance(sample, dict) or not isinstance(sample.get("model"), str)
                or type(sample.get("passed")) is not bool or not duration(sample.get("ms"))
                or not isinstance(sample.get("case"), str)):
            raise ValueError("Invalid model evaluation sample")
        model = sample["model"]
        phase = "first_in_model" if seen[model] == 0 else "subsequent_in_model"
        seen[model] += 1
        samples.append(Sample(str(len(samples)), model, "passed" if sample["passed"] else "failed",
                              sample["case"], sample["ms"], phase))
    return {"schema_version": 2, "kind": "model_latencies", "groups": aggregate(samples),
            "samples": [asdict(sample) for sample in samples],
            "caveats": ["Passing and failing requests are separate; failure latency cannot win a speed ranking.",
                        "First/subsequent request is a connection-order proxy, not measured cold/warm infrastructure.",
                        "Authentication, retries, model availability, and fixture coverage require separate review."]}


def hybrid_report(runs):
    """Architecture summary: Jev vs helper calls, cost, and decision latency.

    Offline over a run export (same shape as ``analyze_runs``). Measures
    decisions per task, System One vs System Two call counts and USD, p50/p95
    decision latency, and speculative fan-out when question counts were
    recorded. Does not invent accuracy: independently verified run counts stay
    separate from model-claimed completion.
    """
    if not isinstance(runs, list) or len(runs) > MAX_RUNS:
        raise ValueError(f"Expected at most {MAX_RUNS} runs")
    from .costs import path_cost_totals

    live = [run for run in runs if isinstance(run, dict) and run.get("mode") == "live"]
    if any(not isinstance(run.get("id"), str) for run in live):
        raise ValueError("Each live run must have a string ID")
    all_events = [event for run in live for event in run.get("events", []) if isinstance(event, dict)]
    paths = path_cost_totals(all_events)
    decision_latencies = []
    decisions = helper_purposes = fanout_questions = fanout_batches = 0
    for run in live:
        summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
        counted_summary = type(summary.get("decisions")) is int
        if counted_summary:
            decisions += summary["decisions"]
        for event in run.get("events", []):
            if not isinstance(event, dict):
                continue
            if event.get("event") in {"decision", "completion_check"} and duration(event.get("latency_ms")):
                decision_latencies.append(event["latency_ms"])
                if event.get("event") == "decision" and not counted_summary:
                    decisions += 1
            if event.get("event") == "helper" and event.get("purpose") in HELPER_PURPOSES:
                helper_purposes += 1
            if event.get("event") == "decision" and type(event.get("fanout_questions")) is int:
                fanout_batches += 1
                fanout_questions += event["fanout_questions"]
            elif event.get("event") == "hybrid_fanout" and type(event.get("questions")) is int:
                fanout_batches += 1
                fanout_questions += event["questions"]
    tasks = len(live)
    jev_calls = paths["system_one"]["calls"]
    helper_calls = paths["system_two"]["calls"]
    report = {
        "schema_version": 1,
        "kind": "hybrid_report",
        "tasks": tasks,
        "excluded_non_live_runs": len(runs) - tasks,
        "statuses": dict(sorted(Counter(run.get("status", "unknown") for run in live).items())),
        "independently_verified_runs": sum((run.get("summary") or {}).get("independently_verified") is True
                                           for run in live),
        "model_claimed_complete_runs": sum((run.get("summary") or {}).get("model_claimed_complete") is True
                                           for run in live),
        "decisions_per_task": round(decisions / tasks, 2) if tasks else None,
        "jev_calls_per_task": round(jev_calls / tasks, 2) if tasks else None,
        "helper_calls_per_task": round(helper_calls / tasks, 2) if tasks else None,
        "cost_per_task_usd": (round(paths["combined"]["estimated_usd"] / tasks, 8)
                              if tasks and paths["combined"]["estimated_usd"] is not None else None),
        "system_one": paths["system_one"],
        "system_two": paths["system_two"],
        "combined": paths["combined"],
        "decision_latency_ms": distribution(decision_latencies) if decision_latencies else None,
        "fanout": {"batches": fanout_batches, "questions": fanout_questions,
                   "questions_per_batch": round(fanout_questions / fanout_batches, 2) if fanout_batches else None},
        "legacy_helper_events": helper_purposes,
        "caveats": [
            "Hybrid report is published-rate accounting over run events, not billing or task accuracy.",
            "Decisions count includes completion checks when the run summary omits a decisions field.",
            "Helper purpose events without canonical inference telemetry are diagnostics only.",
            "Speculative fan-out is measured only when a decision carries fanout_questions or a hybrid_fanout event.",
            "Independently verified runs are not model-claimed completion; neither is universal eval accuracy.",
            "Uncalibrated escalation thresholds are policy defaults, not measured operating points.",
        ],
    }
    return report


def unwrap_runs(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "runs" in data:
            return data["runs"]
        return [data.get("run", data)]
    raise ValueError("Expected a run, run list, or /runs response")


def read_file(path):
    with Path(path).open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Input exceeds 32 MiB")
    return raw


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def fetch_runs(base):
    """Bounded loopback GETs only; no arbitrary endpoint from run data is followed."""
    url = urlsplit(base)
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username or url.password or url.query or url.fragment
            or url.path.rstrip("/") != "/api"):
        raise ValueError("API must be a loopback HTTP URL ending in /api")
    base = base.rstrip("/")

    def get(path):
        with build_opener(NoRedirect).open(base + path, timeout=5) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("API response exceeds 32 MiB")
        return decode_json(raw)

    listing = get("/runs")
    if not isinstance(listing, dict) or not isinstance(listing.get("runs"), list):
        raise ValueError("Invalid /runs response")
    identifiers = [run.get("id") if isinstance(run, dict) else None for run in listing["runs"]]
    if (len(identifiers) > MAX_RUNS or any(not isinstance(identifier, str)
            or re.fullmatch(r"[a-f0-9]{12}", identifier) is None for identifier in identifiers)):
        raise ValueError("Invalid run IDs or too many runs")
    with ThreadPoolExecutor(max_workers=4) as pool:
        details = list(pool.map(lambda identifier: get("/runs/" + identifier), identifiers))
    runs = [unwrap_runs(detail)[0] for detail in details]
    if any(run.get("id") != identifier for run, identifier in zip(runs, identifiers)):
        raise ValueError("Run detail identity does not match the requested ID")
    return runs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--runs", metavar="JSON")
    source.add_argument("--api", metavar="LOOPBACK_URL")
    source.add_argument("--model-samples", metavar="JSONL")
    args = parser.parse_args()
    if args.model_samples:
        records = [decode_json(line) for line in read_file(args.model_samples).splitlines() if line.strip()]
        report = analyze_model_samples(records)
    else:
        runs = fetch_runs(args.api) if args.api else unwrap_runs(decode_json(read_file(args.runs)))
        report = analyze_runs(runs)
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

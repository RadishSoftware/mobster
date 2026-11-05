"""A read-only evidence report for automation bench runs and paired development runs.

Each input remains its own stratum. Bench repeats are independent attempts; only the
development scorer measures adaptive recovery. This module never runs an agent or a device.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from .development.score import InvalidRun, score

BENCH = Path(__file__).resolve().parents[1] / "bench"
MANIFESTS = {"v1": "manifest.json", "heldout": "heldout-manifest.json",
             "h2": "h2-manifest.json", "h3": "h3-manifest.json"}
VERDICTS = {"pass", "fail", "ungraded", "skipped", "infra"}
SHA = set("0123456789abcdef")
# Serialized AgentRun.status values in bench/agents/base.py. Keep this offline reader
# independent of the agent package, whose __init__ loads live provider implementations.
STATUSES = {"completed", "abstained", "gave_up", "step_budget", "timeout", "unsafe_stopped",
            "safety_confirmation", "approval_denied", "error"}


class InvalidEvidence(ValueError):
    """The offline artifacts cannot support a report."""


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and set(value) <= SHA


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(path):
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, ValueError) as error:
        raise InvalidEvidence(f"cannot read {path.name}: {type(error).__name__}") from None
    if not isinstance(value, dict):
        raise InvalidEvidence(f"{path.name} must be an object")
    return value, _digest(raw)


def _planned(header):
    rows = header.get("attempts")
    tasks, agents = header.get("tasks"), header.get("agents")
    repeats = header.get("repeats")
    if (not isinstance(rows, list) or not rows or not isinstance(tasks, list) or not tasks
            or not all(isinstance(t, str) and t for t in tasks) or len(tasks) != len(set(tasks))
            or not isinstance(agents, dict) or not agents or not all(isinstance(a, str) and a for a in agents)
            or type(repeats) is not int or repeats < 1 or not isinstance(header.get("run_id"), str)
            or not header["run_id"] or not _sha(header.get("suite_hash"))):
        raise InvalidEvidence("plan has no valid tasks, agents, repeats and attempts")
    if len(rows) != repeats * len(tasks) * len(agents):
        raise InvalidEvidence("plan omits a task/agent/repeat slot")
    plan, orders = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise InvalidEvidence("invalid planned attempt")
        repeat, order, task, agent = (row.get(k) for k in ("repeat", "order", "task", "agent"))
        if (type(repeat) is not int or not 0 <= repeat < repeats or type(order) is not int or order < 0
                or task not in tasks or agent not in agents or order in orders):
            raise InvalidEvidence("planned attempt does not match its tasks, agents or order")
        key = f"{repeat}:{task}:{agent}"
        if key in plan:
            raise InvalidEvidence(f"duplicate planned attempt {key}")
        plan[key] = row
        orders.add(order)
    expected = {(repeat, task, agent) for repeat in range(repeats) for task in tasks for agent in agents}
    observed = {(row["repeat"], row["task"], row["agent"]) for row in plan.values()}
    if observed != expected or orders != set(range(len(rows))):
        raise InvalidEvidence("plan omits a task/agent/repeat slot or has a non-contiguous order")
    return plan


def _records(path, plan):
    if not path.is_file():
        return defaultdict(list), [], False, None
    slots, all_rows = defaultdict(list), []
    torn = False
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for number, line in enumerate(source, 1):
                digest.update(line)
                try:
                    row = json.loads(line)
                except (UnicodeError, ValueError):
                    if not line.endswith(b"\n"):
                        torn = True
                        break
                    raise InvalidEvidence(f"invalid records.jsonl line {number}") from None
                if not isinstance(row, dict):
                    raise InvalidEvidence(f"invalid records.jsonl line {number}")
                key = row.get("key")
                expected = plan.get(key) if isinstance(key, str) else None
                if (expected is None or any(row.get(name) != expected[name]
                                            for name in ("repeat", "order", "task", "agent"))
                        or not isinstance(row.get("verdict"), str) or row["verdict"] not in VERDICTS):
                    raise InvalidEvidence(f"unplanned or mismatched record on line {number}")
                prior = slots[key]
                if prior and prior[-1]["verdict"] != "infra":
                    raise InvalidEvidence(f"duplicate terminal record for {key}")
                prior.append(row)
                all_rows.append(row)
    except OSError as error:
        raise InvalidEvidence(f"cannot read records.jsonl: {type(error).__name__}") from None
    return slots, all_rows, torn, digest.hexdigest()


def _provenance(header, plan_sha, records_sha):
    suite = header.get("suite")
    manifest_name = MANIFESTS.get(suite)
    manifest_sha = None
    matched = False
    if manifest_name:
        manifest, manifest_sha = _json(BENCH / manifest_name)
        registered = manifest.get("tasks")
        if not isinstance(registered, list) or not set(header["tasks"]) <= set(registered):
            raise InvalidEvidence("planned tasks are not in the registered benchmark corpus")
        matched = header.get("suite_hash") == manifest.get("sha256") and _sha(header.get("suite_hash"))
        if header.get("manifest_frozen") is True and not matched:
            raise InvalidEvidence("plan claims a frozen manifest but its suite hash differs")
    configs = {}
    for agent, value in header["agents"].items():
        config = value.get("config") if isinstance(value, dict) else None
        if not isinstance(config, dict):
            configs[agent] = {"sha256": None, "model": None}
            continue
        raw = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        model = config.get("model")
        configs[agent] = {"sha256": _digest(raw), "model": model if isinstance(model, str) else None}
    device = header.get("device")
    selected = json.dumps(sorted(header["tasks"]), separators=(",", ":")).encode()
    return {"runId": header.get("run_id"), "suite": suite, "suiteSha256": header.get("suite_hash"),
            "manifestSha256": manifest_sha, "manifestMatched": bool(matched),
            "planClaimFrozen": header.get("manifest_frozen") is True,
            "registeredCorpus": suite if matched and header.get("manifest_frozen") is True else None,
            "independence": "unverified", "planSha256": plan_sha, "recordsSha256": records_sha,
            "selectedTaskListSha256": _digest(selected), "selectedTasks": len(header["tasks"]),
            "deviceIdSha256": _digest(str(device).encode()) if device is not None else None,
            "truthSha256": header.get("truth_digest") if _sha(header.get("truth_digest")) else None,
            "git": header.get("git") if isinstance(header.get("git"), dict) else None,
            "agents": configs}


def _number(value):
    return type(value) in (int, float) and value >= 0 and value != float("inf") and value == value


def _metrics(rows, terminal):
    all_times = [row["agent_ms"] for row in rows if _number(row.get("agent_ms"))]
    times = [row["agent_ms"] for row in terminal if row["verdict"] in {"pass", "fail"}
             and _number(row.get("agent_ms"))]
    costs = [row["cost_usd"] for row in rows if _number(row.get("cost_usd"))]
    unpriced = [row["unpriced_calls"] for row in rows if _number(row.get("unpriced_calls"))]
    unsafe = [row["unsafe"] for row in rows if isinstance(row.get("unsafe"), list)]
    unintended = [row["unintended"] for row in rows if isinstance(row.get("unintended"), list)]
    status = [row for row in terminal if row["verdict"] in {"pass", "fail", "ungraded"}
              and isinstance(row.get("status"), str) and row["status"] in STATUSES]
    completed = [row for row in status if row["status"] == "completed"]
    return {"agentMsSubtotal": sum(all_times) if all_times else None,
            "agentMsMeasured": len(all_times),
            "gradedAgentMsMedian": statistics.median(times) if times else None,
            "gradedAgentMsMeasured": len(times),
            "pricedUsdSubtotal": sum(costs) if costs else None, "costMeasured": len(costs),
            "unpricedCalls": sum(unpriced) if unpriced else None, "unpricedMeasured": len(unpriced),
            "blockedInterventions": sum(map(len, unsafe)) if unsafe else None,
            "interventionMeasured": len(unsafe),
            "unintendedActions": sum(map(len, unintended)) if unintended else None,
            "unintendedMeasured": len(unintended),
            "unsupportedCompletionClaims": sum(row["verdict"] != "pass" for row in completed)
            if status else None,
            "failedCompletionClaims": sum(row["verdict"] == "fail" for row in completed) if status else None,
            "ungradedCompletionClaims": sum(row["verdict"] == "ungraded" for row in completed) if status else None,
            "claimStatusMeasured": len(status)}


def _observed_configs(terminal):
    configs = [row["config"] for row in terminal if isinstance(row.get("config"), dict) and row["config"]]
    digests = sorted({_digest(json.dumps(config, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()) for config in configs})
    models = sorted({config["model"] for config in configs
                     if isinstance(config.get("model"), str)})
    return {"sha256": digests, "models": models, "measured": len(configs),
            "terminalRows": len(terminal), "configurationVariation": len(digests) > 1}


def automation(run_dir):
    """One bench run with each planned key counted once; infra retries remain visible separately."""
    run_dir = Path(run_dir)
    header, plan_sha = _json(run_dir / "plan.json")
    plan = _planned(header)
    slots, all_rows, torn, records_sha = _records(run_dir / "records.jsonl", plan)
    provenance = _provenance(header, plan_sha, records_sha)
    agents = {}
    for agent in header["agents"]:
        keys = [key for key, item in plan.items() if item["agent"] == agent]
        terminal = [slots[key][-1] for key in keys if slots[key] and slots[key][-1]["verdict"] != "infra"]
        rows = [row for key in keys for row in slots[key]]
        census = Counter((slots[key][-1]["verdict"] if slots[key] else "missing") for key in keys)
        first = Counter((slots[key][-1]["verdict"] if slots[key] else "missing") for key in keys
                        if plan[key]["repeat"] == 0)
        later = Counter((slots[key][-1]["verdict"] if slots[key] else "missing") for key in keys
                        if plan[key]["repeat"] > 0)
        graded = census["pass"] + census["fail"]
        agents[agent] = {"planned": len(keys), "outcomes": dict(census), "firstRepeat": dict(first),
                         "laterIndependentRepeats": dict(later), "infraRetries": sum(
                             row["verdict"] == "infra" for row in rows),
                         "plannedSuccess": {"pass": census["pass"], "denominator": len(keys)},
                         "gradedSuccess": {"pass": census["pass"], "denominator": graded},
                         "metrics": _metrics(rows, terminal), "observedConfigs": _observed_configs(terminal),
                         "recordRows": len(rows)}
    return {"kind": "Automate", "provenance": provenance, "tornTail": torn, "agents": agents,
            "plannedAttempts": len(plan), "recordRows": len(all_rows),
            "recovery": "not measured; bench repeats are independent"}


def testing(run_dir, manifest_path):
    """The canonical paired scorer validates claims, oracle and referenced evidence."""
    run_dir = Path(run_dir)
    if manifest_path is None:
        raise InvalidEvidence("paired development runs require an explicit manifest")
    manifest_path = Path(manifest_path)
    result = score(manifest_path, run_dir)
    oracle, oracle_sha = _json(run_dir / "oracle.json")
    _, claims_sha = _json(run_dir / "claims.json")
    return {"kind": "Test", "provenance": {"runId": result["runId"],
            "manifestSha256": result["manifestSha256"], "configSha256": result["configSha256"],
            "baselineBuildSha256": oracle["baselineBuildSha256"],
            "fixedBuildSha256": oracle["fixedBuildSha256"], "claimsSha256": claims_sha,
            "oracleSha256": oracle_sha, "independence": "harness-declared, not authenticated"},
            "score": result, "time": "not recorded", "cost": "not recorded",
            "interventions": "not recorded"}


def _cell(value):
    return "not recorded" if value is None else str(value)


def _rate(data):
    return f"{data['pass']}/{data['denominator']}" if data["denominator"] else "0/0 (no graded attempts)"


def render(bench_runs=(), development_runs=(), *, development_manifest=None):
    """Markdown with distinct run and family denominators; never a pooled quality score."""
    lines = ["# Offline evaluation evidence", "",
             "Automate and Test use separate tasks and denominators. "
             "No combined score or world-best claim is made.", ""]
    if development_runs and development_manifest is None:
        raise InvalidEvidence("paired development runs require --development-manifest")
    seen = set()
    for run_dir in bench_runs:
        data = automation(run_dir)
        provenance = data["provenance"]
        marker = ("Automate", provenance["runId"], provenance["suiteSha256"])
        if marker in seen:
            raise InvalidEvidence("duplicate automation run")
        seen.add(marker)
        lines += [f"## Automate: {provenance['runId']}", "",
                  f"Corpus: {_cell(provenance['registeredCorpus'])}; "
                  f"manifest matched: {provenance['manifestMatched']}; "
                  f"plan declared frozen: {provenance['planClaimFrozen']}; independence: {provenance['independence']}.",
                  f"Suite SHA-256: `{_cell(provenance['suiteSha256'])}`; manifest SHA-256: "
                  f"`{_cell(provenance['manifestSha256'])}`; selected tasks: {provenance['selectedTasks']} "
                  f"(`{provenance['selectedTaskListSha256']}`).",
                  f"Plan SHA-256: `{provenance['planSha256']}`; records SHA-256: "
                  f"`{_cell(provenance['recordsSha256'])}`; device ID SHA-256: "
                  f"`{_cell(provenance['deviceIdSha256'])}`; truth SHA-256: "
                  f"`{_cell(provenance['truthSha256'])}`.",
                  f"Git: `{_cell(provenance['git'])}`. A plan records provenance claims; it does not authenticate the "
                  "device, build or model.", "",
                  "| agent | planned model | planned config SHA-256 | planned pass | graded pass | "
                  "first repeat | later independent repeats | infra retries |",
                  "| --- | --- | --- | ---: | ---: | --- | --- | ---: |"]
        for agent, entry in data["agents"].items():
            config = provenance["agents"][agent]
            lines.append(f"| {agent} | {_cell(config['model'])} | `{_cell(config['sha256'])}` | "
                         f"{_rate(entry['plannedSuccess'])} | {_rate(entry['gradedSuccess'])} | "
                         f"{entry['firstRepeat']} | {entry['laterIndependentRepeats']} | {entry['infraRetries']} |")
        lines += ["", "Missing, ungraded, skipped and infra slots remain in the planned denominator. "
                  "First and later repeats are independent; adaptive recovery is not measured here. "
                  "The planned model and configuration do not prove which model ran.", "",
                  "| agent | observed models | observed config SHA-256 | coverage | variation |",
                  "| --- | --- | --- | ---: | --- |"]
        for agent, entry in data["agents"].items():
            observed = entry["observedConfigs"]
            lines.append(f"| {agent} | {observed['models'] or 'not recorded'} | "
                         f"{observed['sha256'] or 'not recorded'} | "
                         f"{observed['measured']}/{observed['terminalRows']} terminal rows | "
                         f"{observed['configurationVariation']} |")
        lines += ["", "Observed configuration digests come from agent execution records. Variation means recorded "
                  "configurations differ; missing records cannot establish the actual model.", "",
                  "| agent | outcomes (one per planned slot) | agent time subtotal ms | "
                  "agent time p50, graded ms | priced USD subtotal | "
                  "unpriced calls | blocked interventions | unintended actions | unsupported completion claims |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for agent, entry in data["agents"].items():
            m = entry["metrics"]
            lines.append(f"| {agent} | {entry['outcomes']} | "
                         f"{_cell(m['agentMsSubtotal'])} ({m['agentMsMeasured']}/{entry['recordRows']} rows) | "
                         f"{_cell(m['gradedAgentMsMedian'])} ({m['gradedAgentMsMeasured']}/"
                         f"{entry['gradedSuccess']['denominator']} graded) | "
                         f"{_cell(m['pricedUsdSubtotal'])} ({m['costMeasured']}/{entry['recordRows']} rows) | "
                         f"{_cell(m['unpricedCalls'])} ({m['unpricedMeasured']}/{entry['recordRows']} rows) | "
                         f"{_cell(m['blockedInterventions'])} "
                         f"({m['interventionMeasured']}/{entry['recordRows']} rows) | "
                         f"{_cell(m['unintendedActions'])} ({m['unintendedMeasured']}/{entry['recordRows']} rows) | "
                         f"{_cell(m['unsupportedCompletionClaims'])} "
                         f"({m['failedCompletionClaims']} failed, {m['ungradedCompletionClaims']} ungraded; "
                         f"{m['claimStatusMeasured']} status rows) |")
        lines += ["", "Cost is recorded telemetry, not audited billing. A subtotal with missing or unpriced calls "
                  "is not a total cost. Unsafe events are blocked-action observations, not a count of human approvals.",
                  f"Record rows: {data['recordRows']}; planned slots: {data['plannedAttempts']}; "
                  f"trailing torn line: {data['tornTail']}.", ""]
    for run_dir in development_runs:
        data = testing(run_dir, development_manifest)
        scorecard, provenance = data["score"], data["provenance"]
        marker = ("Test", provenance["runId"], provenance["manifestSha256"])
        if marker in seen:
            raise InvalidEvidence("duplicate paired development run")
        seen.add(marker)
        lines += [f"## Test: {provenance['runId']}", "",
                  "Paired development bugs; this corpus is separate from the Automate benchmark.",
                  f"Manifest SHA-256: `{provenance['manifestSha256']}`; config SHA-256: "
                  f"`{provenance['configSha256']}`; baseline build SHA-256: "
                  f"`{provenance['baselineBuildSha256']}`; fixed build SHA-256: "
                  f"`{provenance['fixedBuildSha256']}`.",
                  f"Claims SHA-256: `{provenance['claimsSha256']}`; oracle SHA-256: "
                  f"`{provenance['oracleSha256']}`; independence: {provenance['independence']}.", "",
                  "| family | registered | first attempt | eventual | recovered | proven reproduction | verified fix |",
                  "| --- | ---: | --- | --- | ---: | ---: | ---: |"]
        for family, group in scorecard["families"].items():
            lines.append(f"| {family} | {group['registered']} | {group['firstAttempt']} | {group['eventual']} | "
                         f"{group['recovered']} | {group['reproProven']} | {group['verifiedFix']} |")
        lines += ["", f"Unsupported success claims: {scorecard['unsupportedSuccess']}; disproved success claims: "
                  f"{scorecard['falseSuccess']}; clean-arm false alarms: {scorecard['fixedArmFalsePositive']}; "
                  f"critical evidence vetoes: {scorecard['criticalEvidenceVeto']}.",
                  "Time, cost and interventions: not recorded by this paired scorer. First attempt and later "
                  "recovery are separate; missing evidence remains in each registered-case denominator.", ""]
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="report existing offline Automate and Test evidence")
    parser.add_argument("--bench", action="append", type=Path, default=[], metavar="RUN_DIR")
    parser.add_argument("--development", action="append", type=Path, default=[], metavar="RUN_DIR")
    parser.add_argument("--development-manifest", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if not args.bench and not args.development:
        parser.error("pass --bench or --development")
    if args.development and args.development_manifest is None:
        parser.error("--development requires --development-manifest")
    try:
        text = render(args.bench, args.development, development_manifest=args.development_manifest)
    except (InvalidEvidence, InvalidRun) as error:
        parser.error(str(error))
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()

"""Compare exact saved suites, distinguishing transitions from controlled app-state proof."""

import hashlib
import math
import re
from pathlib import Path

from . import capsule, reports

SCHEMA = "mobster.test-compare/1"
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
REASON_CLASS = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
FIELD = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z")
STATUSES = frozenset({"passed", "failed", "needs_review", "couldnt_run"})


class ComparisonError(ValueError):
    """A named suite has no intact completed local report."""


def _read(results_dir, suite_id):
    if not isinstance(suite_id, str) or not capsule.SUITE_ID.fullmatch(suite_id):
        raise ComparisonError("A suite ID must name a saved Mobster test result.")
    try:
        return capsule.read_completed_suite(Path(results_dir) / suite_id)
    except (capsule.CapsuleError, OSError) as error:
        raise ComparisonError(f"Suite {suite_id} has no intact completed result.") from error


def compare_suites(results_dir, base_suite_id, candidate_suite_id):
    """Read the two exact IDs under one local results directory."""
    return compare(_read(results_dir, base_suite_id), _read(results_dir, candidate_suite_id))


def _ref(text):
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]


def _digest(value):
    return isinstance(value, str) and bool(DIGEST.fullmatch(value))


def _frozen(value):
    return value == "none" or _digest(value)


def _status(cell):
    value = cell.get("status")
    return value if isinstance(value, str) and value in STATUSES else None


def _reason(cell):
    source = cell.get("reason")
    value = source.get("class") if isinstance(source, dict) else None
    return value if isinstance(value, str) and REASON_CLASS.fullmatch(value) else None


def _environment(cell):
    source = cell.get("environment")
    if not isinstance(source, dict) or source.get("kind") not in ("simulator", "iphone") or any(
            not isinstance(source.get(key), str) or not 1 <= len(source[key]) <= 80
            for key in ("model", "runtime")):
        return None
    return {key: source[key] for key in ("kind", "model", "runtime")}


def _policy(doc):
    value = doc.get("executionPolicy")
    if not isinstance(value, dict) or set(value) != {"retries", "repeat", "replay", "keyless",
                                                     "assertTimeout", "mobster"}:
        return None
    if (type(value["retries"]) is not int or not 0 <= value["retries"] <= 5 or
            type(value["repeat"]) is not int or not 1 <= value["repeat"] <= 20 or
            value["replay"] not in ("off", "fresh", "strict", "auto") or
            type(value["keyless"]) is not bool or
            type(value["assertTimeout"]) not in (int, float) or
            not math.isfinite(value["assertTimeout"]) or not 0 < value["assertTimeout"] <= 600 or
            not isinstance(value["mobster"], str) or not 1 <= len(value["mobster"]) <= 40):
        return None
    return value


def _verdict(doc):
    value = doc.get("exitCode")
    return value if type(value) is int and 0 <= value <= 255 else None


def _cells(doc):
    found, errors = {}, []
    checks = doc.get("checks")
    if not isinstance(checks, list):
        return found, ["checks_missing"]
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("results"), list):
            errors.append("check_malformed")
            continue
        identity = check.get("caseId") or check.get("file")
        if not isinstance(identity, str) or not 1 <= len(identity) <= 4096:
            errors.append("check_identity_missing")
            continue
        for cell in check["results"]:
            if not isinstance(cell, dict) or not isinstance(cell.get("deviceKey"), str) or \
                    not 1 <= len(cell["deviceKey"]) <= 256:
                errors.append("cell_malformed")
                continue
            key = identity, cell["deviceKey"]
            if key in found:
                errors.append("duplicate_case_target")
                found[key] = None
            else:
                found[key] = (check, cell)
    return found, sorted(set(errors))


def _clean(cell):
    reason = cell.get("reason")
    return (type(cell.get("quarantined")) is bool and cell["quarantined"] is False and
            type(cell.get("flaky")) is bool and cell["flaky"] is False and
            type(cell.get("replayed")) is bool and cell["replayed"] is False and
            cell.get("healed") == [] and isinstance(cell.get("assertions"), list) and
            (reason is None or isinstance(reason, dict)) and not cell.get("safetyStop") and
            _status(cell) in ("passed", "failed"))


def _business_fields(check, cell):
    """Validate one selected run against its admitted binding and complete field inventory."""
    binding = check.get("businessBindingSha256")
    proof = cell.get("business")
    runs = cell.get("runs")
    if (not _digest(binding) or not isinstance(proof, dict) or proof.get("bindingSha256") != binding or
            proof.get("setup") != "seeded" or proof.get("status") != "observed" or
            proof.get("cleanup") != "removed" or type(proof.get("generation")) is not int or
            proof["generation"] < 1 or not _digest(proof.get("sourceSha256")) or
            type(cell.get("attempts")) is not int or cell["attempts"] != 1 or
            not isinstance(runs, list) or len(runs) != 1 or not isinstance(runs[0], dict) or
            runs[0].get("business") != proof or runs[0].get("verdict") != cell.get("status")):
        return None
    fields, passed, failed = (proof.get(key) for key in ("assertionFields", "passedFields", "failedFields"))
    if (not isinstance(fields, list) or not 1 <= len(fields) <= 20 or
            not all(isinstance(item, str) and FIELD.fullmatch(item) for item in fields) or
            len(set(fields)) != len(fields) or not isinstance(passed, list) or not isinstance(failed, list) or
            any(not isinstance(item, str) for item in passed + failed) or
            len(passed) + len(failed) != len(fields) or set(passed + failed) != set(fields)):
        return None
    return fields, failed, passed


def _supporting_evidence(cell, visual_required):
    if cell.get("findings", []) != []:
        return False
    conditions = cell.get("conditions")
    if conditions is not None and (not isinstance(conditions, dict) or
                                   conditions.get("cleanup") not in ("restored", "not_needed") or
                                   not isinstance(conditions.get("applied"), list) or
                                   conditions.get("skipped") != []):
        return False
    assertions = cell.get("assertions")
    if not isinstance(assertions, list) or any(
            not isinstance(item, dict) or type(item.get("index")) is not int or item["index"] < 0 or
            not isinstance(item.get("text"), str) or item.get("ok") is not True for item in assertions):
        return False
    if len({item["index"] for item in assertions}) != len(assertions):
        return False
    visual = cell.get("visual")
    return (visual is None and not visual_required or
            isinstance(visual, dict) and visual.get("ok") is True)


def _case(base, candidate, key, single_repeat):
    before_check, before = base
    after_check, after = candidate
    reasons = []
    if before_check.get("provenanceDrift") or after_check.get("provenanceDrift"):
        reasons.append("provenance_drift")
    for field, missing, changed in (("checkSpecDigest", "check_unknown", "check_changed"),
                                    ("baselineDigest", "baseline_unknown", "baseline_changed"),
                                    ("fixtureDigest", "fixture_unknown", "fixture_changed")):
        valid = _digest if field == "checkSpecDigest" else _frozen
        if not valid(before_check.get(field)) or not valid(after_check.get(field)):
            reasons.append(missing)
        elif before_check[field] != after_check[field]:
            reasons.append(changed)
    old_env, new_env = _environment(before), _environment(after)
    if old_env is None or new_env is None:
        reasons.append("environment_unknown")
    elif old_env != new_env:
        reasons.append("environment_changed")
    if not _clean(before) or not _clean(after):
        reasons.append("verdict_not_stable")
    comparable = not reasons
    before_status, after_status = _status(before), _status(after)
    transition = f"{before_status}_to_{after_status}" if comparable else None
    proof_reasons = []
    verified = False
    if transition == "failed_to_passed":
        binding = before_check.get("businessBindingSha256")
        prior, later = _business_fields(before_check, before), _business_fields(after_check, after)
        if (not single_repeat or _reason(before) != "business" or after.get("reason") is not None or
                before.get("business") is None or after.get("business") is None or
                before_check.get("fixtureDigest") == "none" or
                before_check.get("businessBindingSha256") != after_check.get("businessBindingSha256") or
                not _digest(binding) or not prior or not later or prior[0] != later[0] or
                not prior[1] or later[1] or not set(prior[1]).issubset(later[2]) or
                before["runs"][0].get("class") != "business" or after["runs"][0].get("class") is not None or
                old_env["kind"] != "simulator" or
                before["business"]["sourceSha256"] == after["business"]["sourceSha256"] or
                "visualBaselineSha256" not in before_check or "visualBaselineSha256" not in after_check or
                before_check["visualBaselineSha256"] != after_check["visualBaselineSha256"] or
                (before_check["visualBaselineSha256"] is not None and
                 not _digest(before_check["visualBaselineSha256"])) or
                not _supporting_evidence(before, before_check.get("visualBaselineSha256") is not None) or
                not _supporting_evidence(after, after_check.get("visualBaselineSha256") is not None)):
            proof_reasons.append("controlled_fixture_unproven")
        if not _digest(before.get("appDigest")) or not _digest(after.get("appDigest")):
            proof_reasons.append("app_artifact_unknown")
        elif before["appDigest"] == after["appDigest"]:
            proof_reasons.append("app_artifact_unchanged")
        verified = not proof_reasons
    elif transition is not None:
        proof_reasons.append("no_failed_to_passed_transition")
    return {"caseRef": _ref(key[0]), "targetRef": _ref(key[1]),
            "base": {"status": before_status, "reasonClass": _reason(before)},
            "candidate": {"status": after_status, "reasonClass": _reason(after)},
            "comparable": comparable, "observedTransition": transition,
            "verifiedFix": verified, "proof": {"scope": "controlled_app_state" if verified else None},
            "reasons": sorted(set(reasons)), "proofReasons": proof_reasons}


def compare(base, candidate):
    """Keep both actual verdicts and every incompatibility; no green-baseline search."""
    if not isinstance(base, dict) or not isinstance(candidate, dict):
        raise ComparisonError("Both saved suite reports must be objects.")
    before, before_errors = _cells(base)
    after, after_errors = _cells(candidate)
    old_policy, new_policy = _policy(base), _policy(candidate)
    rows = []
    for key in sorted(before.keys() | after.keys()):
        if key in before and key in after and before[key] is not None and after[key] is not None:
            rows.append(_case(before[key], after[key], key, old_policy is not None and
                              new_policy is not None and old_policy["repeat"] == new_policy["repeat"] == 1))
            continue
        prior = before.get(key)
        later = after.get(key)
        ambiguous = key in before and prior is None or key in after and later is None
        rows.append({"caseRef": _ref(key[0]), "targetRef": _ref(key[1]),
                     "base": {"status": _status(prior[1]) if prior else None, "reasonClass": None},
                     "candidate": {"status": _status(later[1]) if later else None, "reasonClass": None},
                     "comparable": False, "observedTransition": None, "verifiedFix": False,
                     "proof": {"scope": None}, "reasons": ["duplicate_case_target" if ambiguous else
                         "case_missing_from_candidate" if key in before else "case_missing_from_base"],
                     "proofReasons": []})
    reasons = before_errors + after_errors
    if base.get("scenario") is not None or candidate.get("scenario") is not None:
        reasons.append("scenario_not_comparable")
    if base.get("schema") != reports.SCHEMA or candidate.get("schema") != reports.SCHEMA:
        reasons.append("report_schema_unknown")
    if type(base.get("finishedAt")) is not int or type(candidate.get("finishedAt")) is not int:
        reasons.append("suite_not_terminal")
    if base.get("stopReason") or candidate.get("stopReason") or base.get("stopped") or candidate.get("stopped"):
        reasons.append("suite_stopped")
    if _verdict(base) is None or _verdict(candidate) is None:
        reasons.append("suite_verdict_missing")
    if old_policy is None or new_policy is None:
        reasons.append("execution_policy_unknown")
    elif old_policy != new_policy:
        reasons.append("execution_policy_changed")
    if not rows:
        reasons.append("no_cases")
    if any(not row["comparable"] for row in rows):
        reasons.append("cases_incomparable")
    invalid = {"scenario_not_comparable", "suite_not_terminal", "suite_stopped", "report_schema_unknown",
               "suite_verdict_missing", "execution_policy_unknown", "execution_policy_changed"}
    if before_errors or after_errors or invalid.intersection(reasons):
        for row in rows:
            row.update(comparable=False, observedTransition=None, verifiedFix=False, proof={"scope": None})
            row["reasons"] = sorted(set(row["reasons"] + ["suite_not_comparable"]))
    verified = (not reasons and _verdict(base) == 1 and _verdict(candidate) == 0 and
                any(row["verifiedFix"] for row in rows) and all(
                    row["candidate"]["status"] == "passed" and
                    (row["base"]["status"] != "failed" or row["verifiedFix"]) for row in rows))
    return {"schema": SCHEMA, "baseSuite": base.get("suite") if isinstance(base.get("suite"), str) and
            capsule.SUITE_ID.fullmatch(base["suite"]) else None,
            "candidateSuite": candidate.get("suite") if isinstance(candidate.get("suite"), str) and
            capsule.SUITE_ID.fullmatch(candidate["suite"]) else None,
            "baseVerdict": _verdict(base), "candidateVerdict": _verdict(candidate),
            "comparable": not reasons, "verifiedFix": verified,
            "proof": {"scope": "controlled_app_state" if verified else None},
            "reasons": sorted(set(reasons)), "cases": rows}

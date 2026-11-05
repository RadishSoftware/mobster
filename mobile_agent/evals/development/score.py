"""Score paired development runs from independent, pre-registered evidence.

This is an offline reader. It never builds an app, runs an agent, or probes a device.
The harness must write oracle.json without consulting claims.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


class InvalidRun(ValueError):
    """The input cannot be joined to its registered run."""


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InvalidRun(f"cannot read {path}: {error}") from error


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _artifact_matches(run_dir: Path, ref, digest):
    if not isinstance(ref, str) or not ref or not _sha(digest):
        return False
    try:
        path = (run_dir / ref).resolve()
    except (OSError, RuntimeError):
        return False
    if not path.is_relative_to(run_dir.resolve()) or not path.is_file():
        return False
    try:
        if path.stat().st_size > 5_000_000 or _digest(path.read_bytes()) != digest:
            return False
    except OSError:
        return False
    return True


def _evidence(run_dir: Path, check):
    """Missing/tampered evidence is unknown; a claimed boolean is insufficient."""
    if not _artifact_matches(run_dir, check.get("evidence"), check.get("sha256")):
        return None
    return check.get("ok") if type(check.get("ok")) is bool else None


def _manifest(path: Path):
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InvalidRun(f"cannot read manifest {path}: {error}") from error
    if not isinstance(manifest, dict) or type(manifest.get("schema")) is not int or manifest["schema"] != 1:
        raise InvalidRun("unsupported manifest schema")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise InvalidRun("manifest needs registered cases")
    by_id = {}
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
            raise InvalidRun("invalid case id")
        if case["id"] in by_id:
            raise InvalidRun(f"duplicate case {case['id']}")
        if case.get("family") not in {"automation", "testing", "ux", "reliability"}:
            raise InvalidRun(f"invalid family for {case['id']}")
        if not _sha(case.get("checkSha256")):
            raise InvalidRun(f"missing registered check digest for {case['id']}")
        if type(case.get("maxAttempts")) is not int or not 1 <= case["maxAttempts"] <= 10:
            raise InvalidRun(f"invalid attempt limit for {case['id']}")
        groups = [case.get("regressionChecks"), case.get("criticalChecks")]
        if not isinstance(case.get("bugCheck"), str) or any(not isinstance(g, list) or not g for g in groups):
            raise InvalidRun(f"invalid checks for {case['id']}")
        names = [case["bugCheck"], *groups[0], *groups[1]]
        if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
            raise InvalidRun(f"duplicate or empty check for {case['id']}")
        by_id[case["id"]] = case
    return _digest(raw), by_id


def _header(document, manifest_sha):
    if not isinstance(document, dict) or type(document.get("schema")) is not int or document["schema"] != 1:
        raise InvalidRun("unsupported run schema")
    if document.get("manifestSha256") != manifest_sha:
        raise InvalidRun("run does not match manifest bytes")
    if not isinstance(document.get("runId"), str) or not document["runId"] or not _sha(document.get("configSha256")):
        raise InvalidRun("missing run or configuration identity")
    if not isinstance(document.get("attempts"), list):
        raise InvalidRun("attempts must be a list")


def _attempts(document, cases):
    rows = {}
    for row in document["attempts"]:
        if not isinstance(row, dict):
            raise InvalidRun("invalid attempt")
        case_id, ordinal = row.get("caseId"), row.get("attempt")
        if (not isinstance(case_id, str) or case_id not in cases or type(ordinal) is not int
                or ordinal < 1 or ordinal > cases[case_id]["maxAttempts"]):
            raise InvalidRun(f"unknown case or attempt: {case_id}/{ordinal}")
        key = (case_id, ordinal)
        if key in rows:
            raise InvalidRun(f"duplicate attempt {case_id}/{ordinal}")
        rows[key] = row
    return rows


def _checks(run_dir, observation, case):
    expected = {case["bugCheck"], *case["regressionChecks"], *case["criticalChecks"]}
    entries = observation.get("checks")
    if not isinstance(entries, list):
        return {name: None for name in expected}
    names = [entry.get("id") for entry in entries if isinstance(entry, dict)]
    if (len(names) != len(entries) or any(not isinstance(name, str) for name in names)
            or len(set(names)) != len(names) or not set(names) <= expected):
        raise InvalidRun(f"check IDs differ from manifest for {case['id']}")
    observed = {entry["id"]: _evidence(run_dir, entry) for entry in entries}
    return {name: observed.get(name) for name in expected}


def score(manifest_path: Path, run_dir: Path):
    """Return a JSON-serializable census; no missing case silently disappears."""
    manifest_sha, cases = _manifest(manifest_path)
    claims = _read(run_dir / "claims.json")
    oracle = _read(run_dir / "oracle.json")
    for document in (claims, oracle):
        _header(document, manifest_sha)
    if (claims["runId"], claims["configSha256"]) != (oracle["runId"], oracle["configSha256"]):
        raise InvalidRun("claims and oracle belong to different runs")
    builds = (oracle.get("baselineBuildSha256"), oracle.get("fixedBuildSha256"))
    if not all(map(_sha, builds)) or builds[0] == builds[1]:
        raise InvalidRun("distinct baseline and fixed build digests required")
    claim_rows = _attempts(claims, cases)
    oracle_rows = _attempts(oracle, cases)
    for case_id in cases:
        ordinals = sorted(n for cid, n in claim_rows.keys() | oracle_rows.keys() if cid == case_id)
        if ordinals != list(range(1, len(ordinals) + 1)):
            raise InvalidRun(f"non-contiguous attempts for {case_id}")

    results = []
    for case_id, case in cases.items():
        attempts = []
        critical_veto = False
        verification_veto = False
        for ordinal in range(1, case["maxAttempts"] + 1):
            key = (case_id, ordinal)
            if key not in claim_rows and key not in oracle_rows:
                continue
            claim = claim_rows.get(key)
            evidence = oracle_rows.get(key)
            baseline, fixed = {}, {}
            comparable = False
            if claim is not None and (claim.get("baselineVerdict") not in {"bug", "clean", "unknown"}
                                      or claim.get("fixedVerdict") not in {"fixed", "broken", "unknown"}):
                raise InvalidRun(f"invalid agent verdict for {case_id}/{ordinal}")
            if evidence is not None:
                arms = [evidence.get("baseline"), evidence.get("fixed")]
                if not all(isinstance(arm, dict) for arm in arms):
                    raise InvalidRun(f"missing paired arms for {case_id}/{ordinal}")
                if any(arm.get("buildSha256") != build for arm, build in zip(arms, builds)):
                    raise InvalidRun(f"build mismatch for {case_id}/{ordinal}")
                seeds = [arm.get("startStateSha256") for arm in arms]
                assertion = [arm.get("assertionSha256") for arm in arms]
                setup = [arm.get("setupSha256") for arm in arms]
                comparable = (seeds[0] == seeds[1] and _sha(seeds[0])
                              and assertion == [case["checkSha256"]] * 2
                              and setup[0] == setup[1] and _sha(setup[0]))
                for arm, target in zip(arms, (baseline, fixed)):
                    if arm.get("reached") is not None and type(arm.get("reached")) is not bool:
                        raise InvalidRun(f"invalid reach for {case_id}/{ordinal}")
                    target["reached"] = arm.get("reached")
                    if arm.get("reached") is True:
                        target.update(_checks(run_dir, arm, case))
                    else:
                        target.update({name: None for name in [case["bugCheck"], *case["regressionChecks"],
                                                               *case["criticalChecks"]]})
            if any(baseline.get(name) is not True or fixed.get(name) is not True
                   for name in case["criticalChecks"]):
                critical_veto = True
            if (not comparable or baseline.get("reached") is not True or fixed.get("reached") is not True
                    or baseline.get(case["bugCheck"]) is not False or fixed.get(case["bugCheck"]) is not True
                    or any(baseline.get(name) is not True or fixed.get(name) is not True
                           for name in case["regressionChecks"])):
                verification_veto = True
            if claim is None or evidence is None or not comparable:
                category = "inconclusive"
            elif baseline["reached"] is False:
                category = "unreached"
            elif (baseline["reached"] is not True or baseline[case["bugCheck"]] is None
                  or baseline[case["bugCheck"]] is True):
                category = "inconclusive"
            elif claim["baselineVerdict"] == "bug":
                category = "detected"
            else:
                category = "survived"
            fixed_clean = (comparable and fixed.get("reached") is True
                           and fixed.get(case["bugCheck"]) is True
                           and all(fixed.get(name) is True for name in
                                   case["regressionChecks"] + case["criticalChecks"]))
            fixed_proven = (category == "detected" and fixed_clean
                            and all(baseline.get(name) is True for name in
                                    case["regressionChecks"] + case["criticalChecks"]))
            fixed_failed = (comparable and fixed.get("reached") is True and
                            any(fixed.get(name) is False for name in
                                [case["bugCheck"], *case["regressionChecks"], *case["criticalChecks"]]))
            repro = claim.get("repro") if claim else None
            repro_proven = (isinstance(repro, dict) and evidence is not None
                            and _artifact_matches(run_dir, repro.get("path"), repro.get("sha256"))
                            and evidence.get("replayedReproSha256") == repro.get("sha256")
                            and comparable and baseline.get("reached") is True and fixed.get("reached") is True
                            and baseline.get(case["bugCheck"]) is False and fixed.get(case["bugCheck"]) is True)
            baseline_false = bool(claim and claim["baselineVerdict"] == "clean" and comparable
                                  and baseline.get(case["bugCheck"]) is False)
            fixed_false = bool(claim and claim["fixedVerdict"] == "fixed" and fixed_failed)
            baseline_unsupported = bool(claim and claim["baselineVerdict"] == "clean"
                                        and (not comparable or baseline.get(case["bugCheck"]) is None))
            fixed_unsupported = bool(claim and claim["fixedVerdict"] == "fixed"
                                     and not fixed_proven and not fixed_failed)
            attempts.append({
                "attempt": ordinal,
                "category": category,
                "reproProven": bool(repro_proven),
                "falseSuccess": int(baseline_false) + int(fixed_false),
                "unsupportedSuccess": int(baseline_unsupported) + int(fixed_unsupported),
                "fixedArmFalsePositive": bool(claim and claim["fixedVerdict"] == "broken" and fixed_clean),
                "fixProven": bool(fixed_proven and claim["fixedVerdict"] == "fixed"),
            })
        first = attempts[0]["category"] if attempts else "unreached"
        eventual = "detected" if any(row["category"] == "detected" for row in attempts) else (
            "survived" if any(row["category"] == "survived" for row in attempts) else (
                "inconclusive" if any(row["category"] == "inconclusive" for row in attempts) else "unreached"))
        results.append({"caseId": case_id, "family": case["family"], "firstAttempt": first,
                        "eventual": eventual, "recovered": first != "detected" and eventual == "detected",
                        "reproProven": not verification_veto and any(row["reproProven"] for row in attempts),
                        "verifiedFix": not critical_veto and not verification_veto
                        and any(row["fixProven"] for row in attempts),
                        "criticalEvidenceVeto": critical_veto, "verificationEvidenceVeto": verification_veto,
                        "attempts": attempts})
    by_family = {}
    for family in ("automation", "testing", "ux", "reliability"):
        group = [row for row in results if row["family"] == family]
        by_family[family] = {"registered": len(group),
                             "firstAttempt": dict(Counter(row["firstAttempt"] for row in group)),
                             "eventual": dict(Counter(row["eventual"] for row in group)),
                             "recovered": sum(row["recovered"] for row in group),
                             "reproProven": sum(row["reproProven"] for row in group),
                             "verifiedFix": sum(row["verifiedFix"] for row in group)}
    return {"runId": oracle["runId"], "manifestSha256": manifest_sha,
            "configSha256": oracle["configSha256"], "registered": len(cases),
            "families": by_family, "falseSuccess": sum(a["falseSuccess"] for r in results for a in r["attempts"]),
            "unsupportedSuccess": sum(a["unsupportedSuccess"] for r in results for a in r["attempts"]),
            "fixedArmFalsePositive": sum(a["fixedArmFalsePositive"] for r in results for a in r["attempts"]),
            "criticalEvidenceVeto": sum(r["criticalEvidenceVeto"] for r in results), "cases": results}


def main():
    parser = argparse.ArgumentParser(description="score an offline paired development evaluation")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(score(args.manifest, args.run_dir), indent=2))


if __name__ == "__main__":
    main()

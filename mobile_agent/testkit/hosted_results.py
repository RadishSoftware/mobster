"""Bounded, text-only suite results for a hosted caller.

The source document may contain app text, secrets, device IDs and paths. Only fields named in ``project`` enter the
export. The original results, reports, screenshots and logs remain on the enrolled Mac. The caller authorizes a suite
before passing its frozen artifact bytes to ``manifest`` or ``chunk``; this module never reads a file.
The caller binds expected targets; a green admitted export proves coverage only of targets declared in its document.
"""

import base64
import binascii
import hashlib
import json
import math
import re

from . import reports
from .suite import SAFETY_EXIT_STOPS

SCHEMA = "mobster.test.hosted/1"
MANIFEST_SCHEMA = "mobster.test.artifacts/1"
MAX_CHECKS = 500
MAX_TARGETS = 20
MAX_ARTIFACT = 2 * 1024 * 1024
CHUNK_BYTES = 32 * 1024
MAX_DURATION_MS = 30 * 86_400_000
MAX_TIME_MS = 10**13
KINDS = {"json": ("results.json", "application/json"), "junit": ("junit.xml", "application/xml")}
VERDICTS = frozenset({"passed", "failed", "needs_review", "couldnt_run"})
REASONS = frozenset({"assertion", "blocked", "budget", "drift", "app_not_running", "no_assertions",
                     "app_crashed",
                     "held_before_flow", "usage", "model", "environment", "simulator", "wda", "install",
                     "launch", "device", "stopped", "journal", "unknown_outcome", "internal",
                     "not_developer_build", "build", "business", "business_evidence", "business_cleanup"})
STOP_REASONS = frozenset((*SAFETY_EXIT_STOPS, "stopped"))
MESSAGES = {"failed": "Check failed.", "needs_review": "Check needs review.",
            "couldnt_run": "Check could not run."}
SUITE_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{4}\Z")
ARTIFACT_ID = re.compile(r"[a-f0-9]{24}\Z")
ENROLLED_ID = re.compile(r"[a-f0-9]{12}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
MODEL = re.compile(r"(?:iPhone (?:[0-9]{1,2}(?: Pro Max| Pro| Plus| mini)?|SE(?: \([1-9](?:st|nd|rd|th) "
                   r"generation\))?)|iPad(?: Air| mini| Pro)?(?: [0-9]{1,2}(?:-inch)?)?"
                   r"(?: \([A-Z][0-9]\))?)\Z")
RUNTIME = re.compile(r"iOS [0-9]{1,2}(?:\.[0-9]{1,2}){0,2}\Z")


class ExportError(ValueError):
    """A canonical result cannot be safely exported."""


class InputsChanged(ExportError):
    """A check ran on inputs other than the admitted suite's: it can't be reported under that identity."""


class ArtifactTooLarge(ExportError):
    """A safe artifact would exceed the hosted size limit."""


class ArtifactNotFound(KeyError):
    """An artifact ID does not name one of this suite's frozen exports."""


def _integer(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ExportError("Invalid suite result number")
    return value


def _ratio(value):
    if type(value) not in (float, int) or not 0 <= value <= 1 or not math.isfinite(value):
        raise ExportError("Invalid suite pass rate")
    return value


def _identity(identity, count, source_checks=None):
    if identity is None:
        return None
    if not isinstance(identity, dict) or set(identity) != {"projectId", "revision", "provenance", "cases"} or \
            not isinstance(identity.get("projectId"), str) or not ENROLLED_ID.fullmatch(identity["projectId"]) or \
            type(identity.get("revision")) is not int or identity["revision"] < 1 or \
            not isinstance(identity.get("provenance"), str) or not DIGEST.fullmatch(identity["provenance"]) or \
            not isinstance(identity.get("cases"), (list, tuple)) or not identity["cases"] or \
            len(identity["cases"]) != count:
        raise ExportError("Invalid admitted suite identity")
    ids = set()
    for number, case in enumerate(identity["cases"]):
        if not isinstance(case, dict) or set(case) != {"id", "sourceFile", "checkSpecDigest", "appDigest",
                                                   "baselineDigest", "fixtureDigest"} or \
                not isinstance(case["id"], str) or not ENROLLED_ID.fullmatch(case["id"]) or case["id"] in ids or \
                not isinstance(case["sourceFile"], str) or not 1 <= len(case["sourceFile"]) <= 4096 or \
                (source_checks is not None and (not isinstance(source_checks[number], dict) or
                 source_checks[number].get("file") != case["sourceFile"])) or \
                not isinstance(case["checkSpecDigest"], str) or not DIGEST.fullmatch(case["checkSpecDigest"]) or \
                not isinstance(case["appDigest"], str) or not DIGEST.fullmatch(case["appDigest"]) or \
                (case["baselineDigest"] is not None and (not isinstance(case["baselineDigest"], str) or
                 not DIGEST.fullmatch(case["baselineDigest"]))) or \
                (case["fixtureDigest"] is not None and (not isinstance(case["fixtureDigest"], str) or
                 not DIGEST.fullmatch(case["fixtureDigest"]))):
            raise ExportError("Invalid admitted case identity")
        ids.add(case["id"])
    return identity


def _frozen(value):
    """A report's "none" (the check has no such input) as the identity writes it."""
    return None if value == "none" else value


def project(doc, suite_id, *, identity=None):
    """A strict allowlist document suitable for both JSON and canonical JUnit generation."""
    if not isinstance(doc, dict) or doc.get("schema") != reports.SCHEMA or doc.get("suite") != suite_id or \
            not isinstance(suite_id, str) or not SUITE_ID.fullmatch(suite_id):
        raise ExportError("Invalid suite result")
    source_checks, source_devices = doc.get("checks"), doc.get("devices")
    if not isinstance(source_checks, list) or not isinstance(source_devices, list) or \
            len(source_checks) > MAX_CHECKS or len(source_devices) > MAX_TARGETS:
        raise ExportError("Invalid suite result size")
    identity = _identity(identity, len(source_checks), source_checks)
    devices, device_keys, device_names = [], {}, {}
    for number, source in enumerate(source_devices, 1):
        if not isinstance(source, dict) or not isinstance(source.get("key"), str) or \
                source["key"] in device_keys:
            raise ExportError("Invalid suite target")
        key = f"target-{number}"
        device_keys[source["key"]] = key
        device_names[key] = f"Target {number}"
        devices.append({"key": key, "name": device_names[key]})
    checks, cells = [], []
    for number, source in enumerate(source_checks, 1):
        if not isinstance(source, dict) or not isinstance(source.get("results"), list) or \
                len(source["results"]) > len(source_devices) or \
                type(source.get("quarantined")) is not bool:
            raise ExportError("Invalid suite check")
        results, seen = [], set()
        for item in source["results"]:
            if not isinstance(item, dict) or not isinstance(item.get("deviceKey"), str) or \
                    item["deviceKey"] not in device_keys or item["deviceKey"] in seen or \
                    not isinstance(item.get("status"), str) or \
                    item["status"] not in VERDICTS or type(item.get("flaky")) is not bool or \
                    type(item.get("quarantined")) is not bool or item["quarantined"] != source["quarantined"]:
                raise ExportError("Invalid suite cell")
            seen.add(item["deviceKey"])
            status = item["status"]
            reason = item.get("reason")
            reason_class = reason.get("class") if isinstance(reason, dict) else None
            safety_stop = item.get("safetyStop")
            if "safetyStop" in item and (not isinstance(safety_stop, str) or
                                          safety_stop not in SAFETY_EXIT_STOPS or
                                          status != "couldnt_run"):
                raise ExportError("Invalid suite safety stop")
            safe_key = device_keys[item["deviceKey"]]
            result = {"deviceKey": safe_key, "device": device_names[safe_key],
                      "status": status, "attempts": _integer(item.get("attempts"), 100_000),
                      "flaky": item["flaky"],
                      "quarantined": source["quarantined"],
                      "durationMs": _integer(item.get("durationMs"), MAX_DURATION_MS),
                      "reason": None if status == "passed" else {"class": reason_class if isinstance(reason_class,
                                                                    str) and reason_class in REASONS
                                                                    else "other", "message": MESSAGES[status]}}
            environment = item.get("environment")
            environment = environment if isinstance(environment, dict) else {}
            kind, model, runtime = (environment.get(key) for key in ("kind", "model", "runtime"))
            result["environment"] = {"kind": kind if kind in ("simulator", "iphone") else None,
                                     "model": model if isinstance(model, str) and MODEL.fullmatch(model) else None,
                                     "runtime": runtime if isinstance(runtime, str) and RUNTIME.fullmatch(runtime)
                                     else None}
            business = item.get("business")
            assertions = business.get("assertionFields") if isinstance(business, dict) else None
            failed = business.get("failedFields") if isinstance(business, dict) else None
            if isinstance(business, dict) and business.get("status") in ("observed", "unavailable") and \
                    isinstance(assertions, list) and len(assertions) <= 100 and \
                    isinstance(failed, list) and len(failed) <= len(assertions) and \
                    all(isinstance(field, str) for field in assertions + failed) and \
                    len(set(assertions)) == len(assertions) and len(set(failed)) == len(failed) and \
                    set(failed).issubset(assertions):
                result["business"] = {"status": business["status"], "assertions": len(assertions),
                                      "failedAssertions": len(failed)}
            if "passRate" in item:
                result["passRate"] = _ratio(item["passRate"])
            if safety_stop is not None:
                result["safetyStop"] = safety_stop
            results.append(result)
            cells.append(result)
        check = {"name": f"Check {number}", "caseId": f"check-{number}", "classname": "checks",
                 "quarantined": source["quarantined"], "results": results}
        if identity is not None:
            case = identity["cases"][number - 1]
            # api.execute hashes each check again when the suite ends: a drift, or digests other than the approved
            # ones, means this result came from inputs the identity doesn't name.
            if source.get("provenanceDrift") or source.get("checkSpecDigest") != case["checkSpecDigest"] or \
                    any(_frozen(source.get(field)) != case[field] for field in ("baselineDigest", "fixtureDigest")) \
                    or any(item.get("appDigest") != case["appDigest"] for item in source["results"]):
                raise InputsChanged("A check's inputs changed during the admitted suite")
            check.update({"caseId": case["id"], "checkSpecDigest": case["checkSpecDigest"],
                          "appDigest": case["appDigest"], "baselineDigest": case["baselineDigest"],
                          "fixtureDigest": case["fixtureDigest"]})
        checks.append(check)
    counted = [cell for cell in cells if not cell["quarantined"]]
    has_safety_stop = any(cell.get("safetyStop") for cell in cells)
    summary = {"passed": sum(cell["status"] == "passed" for cell in counted),
               "failed": sum(cell["status"] == "failed" for cell in counted),
               "needsReview": sum(cell["status"] == "needs_review" for cell in counted),
               "couldntRun": sum(cell["status"] == "couldnt_run" for cell in counted),
               "flaky": sum(cell["flaky"] for cell in counted),
               "quarantined": len(cells) - len(counted), "total": len(cells)}
    exit_code = _integer(doc.get("exitCode"), 255)
    if identity is not None and exit_code == 0 and (not devices or any(
            len(check["results"]) != len(devices) for check in checks)):
        raise ExportError("Successful admitted suite has a missing target result")
    options = doc.get("options")
    if not isinstance(options, dict) or type(options.get("strict")) is not bool or \
            type(doc.get("stopped", False)) is not bool:
        raise ExportError("Invalid suite options")
    stop_reason = doc.get("stopReason")
    if stop_reason is not None and (not isinstance(stop_reason, str) or stop_reason not in STOP_REASONS):
        raise ExportError("Invalid suite stop reason")
    if exit_code == 0 and (summary["failed"] or summary["needsReview"] or summary["couldntRun"] or
                           (options["strict"] and summary["flaky"]) or stop_reason is not None or
                           doc.get("stopped", False) or doc.get("error") or has_safety_stop):
        raise ExportError("Inconsistent suite verdict")
    safe = {"schema": SCHEMA, "suite": suite_id, "startedAt": _integer(doc.get("startedAt"), MAX_TIME_MS),
            "finishedAt": _integer(doc.get("finishedAt"), MAX_TIME_MS),
            "durationMs": _integer(doc.get("durationMs"), MAX_DURATION_MS), "exitCode": exit_code,
            "stopped": doc.get("stopped", False), "stopReason": stop_reason,
            "options": {"strict": options["strict"]},
            "devices": devices, "checks": checks, "summary": summary, "mobster": {"version": "hosted/1"}}
    if identity is not None:
        safe["identity"] = {key: identity[key] for key in ("projectId", "revision", "provenance")}
    return safe


def build(doc, suite_id, *, identity=None):
    """Freeze safe JSON and JUnit bytes from one terminal canonical result document."""
    safe = project(doc, suite_id, identity=identity)
    data = {"json": (json.dumps(safe, ensure_ascii=False, allow_nan=False, sort_keys=True,
                                separators=(",", ":")) + "\n").encode("utf-8"),
            "junit": reports.junit(safe, strict=safe["options"]["strict"])}
    if any(len(value) > MAX_ARTIFACT for value in data.values()):
        raise ArtifactTooLarge("Hosted test result exceeds 2 MiB; inspect the local report")
    return data


def _checked(artifacts):
    if not isinstance(artifacts, dict) or set(artifacts) != set(KINDS) or \
            any(type(artifacts[key]) is not bytes or len(artifacts[key]) > MAX_ARTIFACT for key in KINDS):
        raise ExportError("Invalid frozen test artifacts")
    return artifacts


def _id(kind, data):
    return hashlib.sha256(b"mobster.test.artifact/1\0" + kind.encode() + b"\0" + data).hexdigest()[:24]


def manifest(artifacts):
    """Integrity and download metadata for already-authorized frozen artifact bytes."""
    artifacts = _checked(artifacts)
    return {"schema": MANIFEST_SCHEMA, "artifacts": [
        {"id": _id(kind, artifacts[kind]), "name": KINDS[kind][0], "mediaType": KINDS[kind][1],
         "size": len(artifacts[kind]), "sha256": hashlib.sha256(artifacts[kind]).hexdigest(),
         "chunkBytes": CHUNK_BYTES}
        for kind in KINDS]}


def valid_manifest(value):
    """Recognize the exact metadata envelope emitted by ``manifest``."""
    if not isinstance(value, dict) or set(value) != {"schema", "artifacts"} or \
            value["schema"] != MANIFEST_SCHEMA or not isinstance(value["artifacts"], list) or \
            len(value["artifacts"]) != len(KINDS):
        return False
    ids = set()
    for item, (_, (name, media_type)) in zip(value["artifacts"], KINDS.items()):
        if not isinstance(item, dict) or set(item) != {"id", "name", "mediaType", "size", "sha256", "chunkBytes"} or \
                item["name"] != name or item["mediaType"] != media_type or \
                not isinstance(item["id"], str) or not ARTIFACT_ID.fullmatch(item["id"]) or \
                not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) or \
                type(item["size"]) is not int or not 1 <= item["size"] <= MAX_ARTIFACT or \
                type(item["chunkBytes"]) is not int or item["chunkBytes"] != CHUNK_BYTES or item["id"] in ids:
            return False
        ids.add(item["id"])
    return True


def chunk(artifacts, artifact_id, offset=0, limit=CHUNK_BYTES):
    """One bounded base64 slice. Authorization and frozen-byte ownership belong to the caller."""
    artifacts = _checked(artifacts)
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= CHUNK_BYTES:
        raise ExportError("Use a nonnegative offset and a limit of 1 to 32768")
    if not isinstance(artifact_id, str) or not ARTIFACT_ID.fullmatch(artifact_id):
        raise ArtifactNotFound(artifact_id)
    found = next(((kind, data) for kind, data in artifacts.items() if _id(kind, data) == artifact_id), None)
    if found is None:
        raise ArtifactNotFound(artifact_id)
    _, data = found
    if offset >= len(data):
        raise ExportError("Artifact offset is out of range")
    part = data[offset:offset + limit]
    return {"id": artifact_id, "offset": offset, "size": len(data), "length": len(part),
            "sha256": hashlib.sha256(data).hexdigest(), "encoding": "base64",
            "data": base64.b64encode(part).decode("ascii"), "eof": offset + len(part) == len(data)}


def valid_chunk(value, *, artifact_id, offset, limit):
    """Recognize a bounded chunk of the requested frozen artifact, including canonical base64."""
    if not isinstance(value, dict) or set(value) != {
            "id", "offset", "size", "length", "sha256", "encoding", "data", "eof"} or \
            type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= CHUNK_BYTES or \
            value["id"] != artifact_id or type(value["offset"]) is not int or value["offset"] != offset or \
            type(value["size"]) is not int or not 1 <= value["size"] <= MAX_ARTIFACT or \
            type(value["length"]) is not int or not 1 <= value["length"] <= limit or \
            value["offset"] + value["length"] > value["size"] or type(value["eof"]) is not bool or \
            value["eof"] != (value["offset"] + value["length"] == value["size"]) or \
            not isinstance(value["id"], str) or not ARTIFACT_ID.fullmatch(value["id"]) or \
            not isinstance(value["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"]) or \
            value["encoding"] != "base64" or not isinstance(value["data"], str) or \
            len(value["data"]) > ((limit + 2) // 3) * 4:
        return False
    try:
        decoded = base64.b64decode(value["data"], validate=True)
    except (ValueError, binascii.Error):
        return False
    return len(decoded) == value["length"] and base64.b64encode(decoded).decode("ascii") == value["data"]

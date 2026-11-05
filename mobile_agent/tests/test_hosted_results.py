"""Hosted suite downloads contain only bounded verdict evidence, never raw app evidence."""

import base64
import hashlib
import json
from unittest import TestCase, mock
from xml.etree import ElementTree as ET

from mobile_agent.testkit import hosted_results as export
from mobile_agent.testkit import reports

SUITE = "20261023-120000-abcd"
SECRET = "hunter2-" + "secret-" + "551203"  # built at runtime so no commit holds the whole literal


def document():
    return {"schema": reports.SCHEMA, "suite": SUITE, "startedAt": 1_792_755_600_000,
            "finishedAt": 1_792_755_605_000, "durationMs": 5000, "exitCode": 1,
            "options": {"strict": True, "command": f"send {SECRET}"},
            "command": f"mobster test --secret {SECRET}",
            "devices": [{"key": f"device:{SECRET}", "name": f"Sam's iPhone {SECRET}", "udid": SECRET}],
            "checks": [{"name": f"Password {SECRET}", "file": f"/private/{SECRET}/check.yaml",
                        "caseId": SECRET, "metadata": {"owner": SECRET}, "quarantined": False,
                        "checkSpecDigest": "e" * 64, "baselineDigest": "none", "fixtureDigest": "2" * 64,
                        "results": [{"deviceKey": f"device:{SECRET}", "device": SECRET, "appDigest": "f" * 64,
                                     "status": "failed", "attempts": 2, "flaky": False,
                                     "quarantined": False, "durationMs": 3800,
                                     "reason": {"class": "assertion", "message": SECRET, "fix": SECRET},
                                     "assertions": [{"text": SECRET, "observed": SECRET}],
                                     "runId": SECRET, "report": f"/private/{SECRET}/report.html",
                                     "frame": f"/private/{SECRET}/frame.jpg", "video": SECRET,
                                     "notes": [SECRET], "runs": [{"message": SECRET}]}]}],
            "summary": {"passed": 99, "failed": 0, "total": 99, "costUsd": 1000},
            "error": {"message": SECRET}}


def admitted_identity():
    return {"projectId": "a" * 12, "revision": 2, "provenance": "c" * 64,
            "cases": [{"id": "d" * 12, "sourceFile": f"/private/{SECRET}/check.yaml",
                       "checkSpecDigest": "e" * 64,
                       "appDigest": "f" * 64, "baselineDigest": None, "fixtureDigest": "2" * 64}]}


class HostedResultTests(TestCase):
    def test_admitted_case_identity_is_stable_and_text_free(self):
        doc = document()
        doc["checks"][0]["results"][0]["environment"] = {"kind": "simulator", "model": "iPhone 17 Pro",
                                                        "runtime": "iOS 26.4", "udid": SECRET}
        artifacts = export.build(doc, SUITE, identity=admitted_identity())
        safe = json.loads(artifacts["json"])
        self.assertEqual(safe["identity"], {"projectId": "a" * 12, "revision": 2,
                                            "provenance": "c" * 64})
        check = safe["checks"][0]
        self.assertEqual((check["caseId"], check["checkSpecDigest"], check["appDigest"],
                          check["baselineDigest"]), ("d" * 12, "e" * 64, "f" * 64, None))
        self.assertEqual(check["fixtureDigest"], "2" * 64)
        self.assertEqual(check["results"][0]["environment"],
                         {"kind": "simulator", "model": "iPhone 17 Pro", "runtime": "iOS 26.4"})
        self.assertNotIn(SECRET.encode(), artifacts["json"] + artifacts["junit"])
        self.assertEqual(ET.fromstring(artifacts["junit"]).find("testsuite/testcase").get("name"), "Check 1")

    def test_missing_duplicate_or_malformed_admitted_identity_refuses(self):
        identity = admitted_identity()
        for change in ({"cases": []}, {"projectId": SECRET}, {"revision": 0},
                       {"revision": True}, {"revision": "2"}, {"provenance": SECRET},
                       {"cases": [{**identity["cases"][0], "id": SECRET}]},
                       {"cases": [{**identity["cases"][0], "sourceFile": "other.yaml"}]},
                       {"cases": [{**identity["cases"][0], "appDigest": None}]},
                       {"cases": [{**identity["cases"][0], "baselineDigest": SECRET}]},
                       {"cases": [{**identity["cases"][0], "fixtureDigest": SECRET}]}):
            with self.subTest(change=change), self.assertRaises(export.ExportError):
                export.build(document(), SUITE, identity={**identity, **change})
        doc = document()
        doc["checks"].append(doc["checks"][0])
        duplicate = {**identity, "cases": [identity["cases"][0]] * 2}
        with self.assertRaises(export.ExportError):
            export.build(doc, SUITE, identity=duplicate)

        other = {**doc["checks"][0], "file": "other.yaml"}
        doc["checks"] = [doc["checks"][0], other]
        second = {**identity["cases"][0], "id": "1" * 12, "sourceFile": "other.yaml"}
        with self.assertRaises(export.ExportError):
            export.build(doc, SUITE, identity={**identity, "cases": [second, identity["cases"][0]]})

    def test_a_drifted_or_differently_pinned_check_is_never_exported_under_the_admitted_identity(self):
        self.assertEqual(export.project(document(), SUITE, identity=admitted_identity())["checks"][0]["appDigest"],
                         "f" * 64)
        for where, change in (("check", {"provenanceDrift": True}), ("check", {"checkSpecDigest": "9" * 64}),
                              ("check", {"checkSpecDigest": None}), ("check", {"baselineDigest": "9" * 64}),
                              ("check", {"fixtureDigest": "none"}), ("cell", {"appDigest": None}),
                              ("cell", {"appDigest": "9" * 64})):
            doc = document()
            (doc["checks"][0] if where == "check" else doc["checks"][0]["results"][0]).update(change)
            with self.subTest(change=change), self.assertRaises(export.InputsChanged):
                export.build(doc, SUITE, identity=admitted_identity())
        drifted = document()
        drifted["checks"][0]["provenanceDrift"] = True
        self.assertEqual(export.project(drifted, SUITE)["checks"][0]["name"], "Check 1")   # no identity claimed

    def test_untrusted_environment_text_is_omitted(self):
        doc = document()
        doc["checks"][0]["results"][0]["environment"] = {"kind": "simulator", "model": "iPhone " + SECRET,
                                                        "runtime": SECRET}
        safe = export.project(doc, SUITE)
        self.assertEqual(safe["checks"][0]["results"][0]["environment"],
                         {"kind": "simulator", "model": None, "runtime": None})

    def test_business_assertion_class_survives_without_raw_snapshot(self):
        doc = document()
        cell = doc["checks"][0]["results"][0]
        cell["reason"] = {"class": "business", "message": SECRET}
        cell["business"] = {"status": "observed", "assertionFields": [SECRET],
                            "failedFields": [SECRET], "fixture": SECRET}
        artifacts = export.build(doc, SUITE)
        safe = json.loads(artifacts["json"])
        self.assertEqual(safe["checks"][0]["results"][0]["reason"]["class"], "business")
        self.assertEqual(safe["checks"][0]["results"][0]["business"],
                         {"status": "observed", "assertions": 1, "failedAssertions": 1})
        self.assertNotIn(SECRET.encode(), artifacts["json"] + artifacts["junit"])

    def test_crash_class_survives_without_raw_crash_evidence(self):
        doc = document()
        cell = doc["checks"][0]["results"][0]
        cell["reason"] = {"class": "app_crashed", "message": SECRET}
        cell["evidence"] = {"crash": {"report": f"/private/{SECRET}/crash.log", "text": SECRET}}
        artifacts = export.build(doc, SUITE)
        self.assertEqual(json.loads(artifacts["json"])["checks"][0]["results"][0]["reason"]["class"],
                         "app_crashed")
        failure = ET.fromstring(artifacts["junit"]).find("testsuite/testcase/failure")
        self.assertEqual(failure.get("type"), "app_crashed")
        self.assertNotIn(SECRET.encode(), artifacts["json"] + artifacts["junit"])

    def test_local_checks_on_different_default_targets_keep_sparse_cells(self):
        doc = document()
        other = json.loads(json.dumps(doc["checks"][0]))
        other["file"] = "another.yaml"
        other["results"][0]["deviceKey"] = "second-device"
        other["results"][0]["status"] = "passed"
        doc["checks"].append(other)
        doc["devices"].append({"key": "second-device", "name": SECRET})
        safe = export.project(doc, SUITE)
        self.assertEqual([len(check["results"]) for check in safe["checks"]], [1, 1])
        self.assertEqual(safe["summary"]["total"], 2)

    def test_admitted_failed_suite_keeps_sparse_cells_but_missing_green_refuses(self):
        doc = document()
        other = {**doc["checks"][0], "file": "other.yaml", "results": []}
        doc["checks"].append(other)
        second = {**admitted_identity()["cases"][0], "id": "1" * 12, "sourceFile": "other.yaml"}
        identity = {**admitted_identity(), "cases": [admitted_identity()["cases"][0], second]}
        safe = export.project(doc, SUITE, identity=identity)
        self.assertEqual([len(check["results"]) for check in safe["checks"]], [1, 0])
        doc["checks"][0]["results"][0]["status"] = "passed"
        doc["exitCode"] = 0
        doc.pop("error")
        with self.assertRaisesRegex(export.ExportError, "missing target result"):
            export.build(doc, SUITE, identity=identity)

    def test_green_admitted_suite_requires_every_declared_target(self):
        doc = document()
        cell = doc["checks"][0]["results"][0]
        cell["status"] = "passed"
        doc["devices"].append({"key": "second-device", "name": SECRET})
        doc["exitCode"] = 0
        doc.pop("error")
        with self.assertRaisesRegex(export.ExportError, "missing target result"):
            export.build(doc, SUITE, identity=admitted_identity())
        doc["checks"][0]["results"].append({**cell, "deviceKey": "second-device", "device": SECRET})
        safe = export.project(doc, SUITE, identity=admitted_identity())
        self.assertEqual((safe["summary"]["passed"], safe["summary"]["total"]), (2, 2))
        self.assertNotIn(SECRET, json.dumps(safe))
        doc["devices"] = []
        doc["checks"][0]["results"] = []
        with self.assertRaisesRegex(export.ExportError, "missing target result"):
            export.build(doc, SUITE, identity=admitted_identity())

    def test_projection_preserves_verdict_counts_and_drops_raw_evidence(self):
        artifacts = export.build(document(), SUITE)
        self.assertEqual(set(artifacts), {"json", "junit"})
        safe = json.loads(artifacts["json"])
        self.assertEqual((safe["exitCode"], safe["summary"]["failed"], safe["summary"]["total"]), (1, 1, 1))
        self.assertEqual(safe["devices"], [{"key": "target-1", "name": "Target 1"}])
        self.assertEqual(safe["checks"][0]["name"], "Check 1")
        result = safe["checks"][0]["results"][0]
        self.assertEqual((result["status"], result["attempts"], result["durationMs"], result["reason"]),
                         ("failed", 2, 3800, {"class": "assertion", "message": "Check failed."}))
        for payload in artifacts.values():
            self.assertNotIn(SECRET.encode(), payload)
            self.assertNotIn(b"/private/", payload)
            self.assertNotIn(b"Sam's iPhone", payload)
        junit = ET.fromstring(artifacts["junit"])
        self.assertEqual(junit.get("failures"), "1")
        self.assertEqual(junit.find("testsuite/testcase").get("name"), "Check 1")

    def test_quarantine_flake_and_unknown_reason_are_safely_classified(self):
        doc = document()
        check = doc["checks"][0]
        check["quarantined"] = True
        result = check["results"][0]
        result["quarantined"] = True
        result["status"] = "passed"
        result["flaky"] = True
        result["reason"] = {"class": SECRET, "message": SECRET}
        doc["exitCode"] = 0
        doc.pop("error")
        safe = export.project(doc, SUITE)
        self.assertEqual((safe["summary"]["quarantined"], safe["summary"]["flaky"]), (1, 0))
        self.assertIsNone(safe["checks"][0]["results"][0]["reason"])
        result["status"] = "couldnt_run"
        safe = export.project(doc, SUITE)
        self.assertEqual(safe["checks"][0]["results"][0]["reason"],
                         {"class": "other", "message": "Check could not run."})

    def test_manifest_chunks_reassemble_to_exact_frozen_bytes_and_digest(self):
        artifacts = export.build(document(), SUITE)
        manifest = export.manifest(artifacts)
        self.assertEqual(manifest["schema"], export.MANIFEST_SCHEMA)
        self.assertTrue(export.valid_manifest(manifest))
        self.assertEqual({item["name"] for item in manifest["artifacts"]}, {"results.json", "junit.xml"})
        for item in manifest["artifacts"]:
            assembled = bytearray()
            offset = 0
            while offset < item["size"]:
                part = export.chunk(artifacts, item["id"], offset, 17)
                self.assertTrue(export.valid_chunk(part, artifact_id=item["id"], offset=offset, limit=17))
                raw = base64.b64decode(part["data"], validate=True)
                self.assertEqual((part["offset"], part["size"], part["length"], part["sha256"]),
                                 (offset, item["size"], len(raw), item["sha256"]))
                assembled.extend(raw)
                offset += len(raw)
            self.assertTrue(part["eof"])
            self.assertEqual(hashlib.sha256(assembled).hexdigest(), item["sha256"])
            self.assertEqual(bytes(assembled), artifacts["json" if item["name"] == "results.json" else "junit"])

    def test_invalid_reads_and_oversize_exports_refuse(self):
        artifacts = export.build(document(), SUITE)
        item = export.manifest(artifacts)["artifacts"][0]
        for offset, limit in ((True, 1), (-1, 1), (item["size"], 1), (0, 0), (0, 32769), (0, True)):
            with self.subTest(offset=offset, limit=limit), self.assertRaises(export.ExportError):
                export.chunk(artifacts, item["id"], offset, limit)
        with self.assertRaises(export.ArtifactNotFound):
            export.chunk(artifacts, "0" * 24)
        with mock.patch.object(export, "MAX_ARTIFACT", 50):
            with self.assertRaises(export.ArtifactTooLarge):
                export.build(document(), SUITE)

    def test_typed_envelopes_reject_tampering(self):
        artifacts = export.build(document(), SUITE)
        item = export.manifest(artifacts)["artifacts"][0]
        manifest = export.manifest(artifacts)
        manifest["artifacts"][0]["size"] = True
        self.assertFalse(export.valid_manifest(manifest))
        part = export.chunk(artifacts, item["id"], 0, 17)
        self.assertFalse(export.valid_chunk(part, artifact_id=item["id"], offset=1, limit=17))
        part["data"] = base64.b64encode(b"wrong").decode()
        self.assertFalse(export.valid_chunk(part, artifact_id=item["id"], offset=0, limit=17))
        part["data"] = "!!!"
        self.assertFalse(export.valid_chunk(part, artifact_id=item["id"], offset=0, limit=17))

    def test_malformed_canonical_values_never_become_a_successful_summary(self):
        doc = document()
        doc["exitCode"] = 0
        with self.assertRaises(export.ExportError):
            export.build(doc, SUITE)
        doc = document()
        doc["checks"][0]["results"][0]["status"] = [SECRET]
        with self.assertRaises(export.ExportError):
            export.build(doc, SUITE)
        doc = document()
        doc["checks"][0]["results"][0]["attempts"] = True
        with self.assertRaises(export.ExportError):
            export.build(doc, SUITE)
        with self.assertRaises(export.ExportError):
            export.build(document(), "another-suite")
        doc = document()
        doc["checks"][0]["quarantined"] = True
        with self.assertRaises(export.ExportError):
            export.build(doc, SUITE)

    def test_nonzero_suite_verdict_cannot_look_green_in_junit(self):
        doc = document()
        doc["checks"] = []
        doc["devices"] = []
        doc["summary"] = {}
        doc["exitCode"] = 130
        doc["stopped"] = True
        doc["error"] = {"message": SECRET, "file": f"/private/{SECRET}"}
        artifacts = export.build(doc, SUITE)
        safe = json.loads(artifacts["json"])
        self.assertEqual(safe["exitCode"], 130)
        self.assertTrue(safe["stopped"])
        self.assertNotIn("error", safe)
        junit = ET.fromstring(artifacts["junit"])
        self.assertEqual(junit.get("errors"), "1")
        self.assertEqual(junit.find("testsuite/testcase").get("name"), "Suite verdict")
        self.assertNotIn(SECRET.encode(), artifacts["junit"])
        doc["stopReason"] = "unknown_outcome"
        safe = export.project(doc, SUITE)
        self.assertEqual(safe["stopReason"], "unknown_outcome")
        doc["stopReason"] = "journal"
        self.assertEqual(export.project(doc, SUITE)["stopReason"], "journal")
        doc["stopReason"] = SECRET
        with self.assertRaises(export.ExportError):
            export.build(doc, SUITE)

    def test_unknown_outcome_is_a_junit_error_with_no_raw_reason(self):
        doc = document()
        doc["checks"][0]["results"][0]["status"] = "couldnt_run"
        doc["checks"][0]["results"][0]["reason"] = {"class": "unknown_outcome", "message": SECRET}
        doc["checks"][0]["results"][0]["safetyStop"] = "unknown_outcome"
        doc["exitCode"] = 3
        doc["stopReason"] = "unknown_outcome"
        artifacts = export.build(doc, SUITE)
        self.assertEqual(json.loads(artifacts["json"])["checks"][0]["results"][0]["safetyStop"],
                         "unknown_outcome")
        junit = ET.fromstring(artifacts["junit"])
        self.assertEqual(junit.get("errors"), "1")
        self.assertEqual(junit.get("failures"), "0")
        self.assertNotIn(SECRET.encode(), artifacts["junit"])
        doc["checks"][0]["results"][0]["safetyStop"] = SECRET
        with self.assertRaises(export.ExportError):
            export.build(doc, SUITE)

    def test_quarantined_safety_stop_is_not_reported_as_green(self):
        for marker, reason_class in (("refused", "blocked"), ("journal", "journal")):
            with self.subTest(marker=marker):
                doc = document()
                check = doc["checks"][0]
                check["quarantined"] = True
                cell = check["results"][0]
                cell["quarantined"] = True
                cell["status"] = "couldnt_run"
                cell["safetyStop"] = marker
                cell["reason"] = {"class": reason_class, "message": SECRET}
                doc["stopReason"] = marker
                doc["exitCode"] = 3
                artifacts = export.build(doc, SUITE)
                junit = ET.fromstring(artifacts["junit"])
                self.assertEqual((junit.get("errors"), junit.get("skipped")), ("1", "0"))
                self.assertNotIn(SECRET.encode(), artifacts["junit"])
                doc["exitCode"] = 0
                with self.assertRaises(export.ExportError):
                    export.build(doc, SUITE)

    def test_hosted_junit_uses_the_canonical_safe_document(self):
        for kind in ("ordinary_failure", "stopped", "quarantined_safety"):
            with self.subTest(kind=kind):
                doc = document()
                cell = doc["checks"][0]["results"][0]
                if kind == "stopped":
                    cell["status"] = "passed"
                    doc.update(exitCode=130, stopReason="stopped", stopped=True)
                elif kind == "quarantined_safety":
                    doc["checks"][0]["quarantined"] = True
                    cell.update(status="couldnt_run", quarantined=True, safetyStop="journal")
                    doc.update(exitCode=3, stopReason="journal")
                artifacts = export.build(doc, SUITE)
                safe = json.loads(artifacts["json"])
                self.assertEqual(artifacts["junit"], reports.junit(safe, strict=safe["options"]["strict"]))
                self.assertNotIn(SECRET.encode(), artifacts["junit"])

"""Completion inventories and local capsule export use inert reports, not a simulator or model."""

import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from mobile_agent.testkit import capsule, reports

SUITE = "20261024-120000-abcd"
SECRET = "private app text"


class CapsuleTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.folder = self.root / SUITE
        self.folder.mkdir()
        self.doc = {"schema": reports.SCHEMA, "suite": SUITE, "startedAt": 1, "finishedAt": 2,
                    "durationMs": 1, "exitCode": 1, "options": {"strict": False},
                    "devices": [{"key": "sim:private-id", "name": "Private phone"}],
                    "checks": [{"name": SECRET, "file": "private/check.yaml", "quarantined": False,
                                "results": [{"deviceKey": "sim:private-id", "status": "failed", "flaky": False,
                                             "reason": {"class": "assertion", "message": SECRET},
                                             "report": "runs/a.html", "visual": {
                                                 "actual": "runs/a/visual/actual.png"}, "runs": []}]}],
                    "summary": {"failed": 1, "total": 1}, "mobster": {"version": "test"}}
        reports.write_json(self.doc, self.folder / "results.json")
        (self.folder / "junit.xml").write_text(f"<xml>{SECRET}</xml>", encoding="utf-8")
        (self.folder / "index.html").write_text(f"<html>{SECRET}</html>", encoding="utf-8")
        (self.folder / "runs" / "a" / "visual").mkdir(parents=True)
        (self.folder / "runs" / "a.html").write_text(SECRET, encoding="utf-8")
        (self.folder / "runs" / "a" / "visual" / "actual.png").write_bytes(SECRET.encode())
        capsule.capture(self.folder, self.doc)

    def test_default_capsule_omits_raw_text_and_selected_export_binds_bytes(self):
        self.assertEqual(capsule.read_completed_suite(self.folder), self.doc)
        safe = capsule.inspect(self.folder)
        self.assertEqual((safe["results"]["exitCode"], safe["results"]["checks"][0]["results"][0]["status"]),
                         (1, "failed"))
        self.assertFalse(safe["results"]["checks"][0]["quarantined"])
        self.assertNotIn(SECRET, json.dumps(safe))
        self.assertNotIn("private/check.yaml", json.dumps(safe))
        self.assertFalse(any(row["selected"] for row in safe["evidence"]))
        selected = capsule.export(self.folder, self.root / "out", include=("report", "visual"))
        rows = [row for row in selected["evidence"] if row.get("file")]
        self.assertEqual({row["kind"] for row in rows}, {"report", "visual"})
        for row in rows:
            data = (self.root / "out" / row["file"]).read_bytes()
            self.assertEqual((len(data), hashlib.sha256(data).hexdigest()), (row["size"], row["sha256"]))
        self.assertFalse((self.root / "out" / "evidence" / "junit.xml").exists())

    def test_changed_result_refuses_and_changed_raw_evidence_is_not_exported(self):
        (self.folder / "results.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(capsule.CapsuleError):
            capsule.read_completed_suite(self.folder)
        reports.write_json(self.doc, self.folder / "results.json")
        (self.folder / "runs" / "a.html").write_text("changed", encoding="utf-8")
        outside = self.root / "outside.png"
        outside.write_bytes(SECRET.encode())
        visual = self.folder / "runs" / "a" / "visual" / "actual.png"
        visual.unlink()
        visual.symlink_to(outside)
        descriptor = capsule.inspect(self.folder, include=("report", "visual"))
        self.assertIn("changed", {row["state"] for row in descriptor["evidence"]})
        self.assertIn("missing", {row["state"] for row in descriptor["evidence"]})
        with self.assertRaises(capsule.CapsuleError):
            capsule.export(self.folder, self.root / "out", include=("report", "visual"))
        self.assertFalse((self.root / "out" / "evidence" / "runs" / "a.html").exists())
        self.assertFalse((self.root / "out" / "evidence" / "runs" / "a" / "visual" / "actual.png").exists())

    def test_missing_inventory_and_hardlinked_file_refuse_attribution(self):
        (self.folder / "evidence-inventory.json").unlink()
        with self.assertRaisesRegex(capsule.CapsuleError, "completion-time"):
            capsule.read_completed_suite(self.folder)
        capsule.capture(self.folder, self.doc)
        private = self.root / "private.html"
        private.write_text(SECRET, encoding="utf-8")
        report = self.folder / "runs" / "a.html"
        report.unlink()
        os.link(private, report)
        descriptor = capsule.inspect(self.folder, include=("report",))
        self.assertTrue(any(row["state"] == "missing" for row in descriptor["evidence"]))

    def test_malformed_selection_and_symlinked_suite_refuse(self):
        for include in (("video",), ("report", "report"), (True,), ([],)):
            with self.subTest(include=include), self.assertRaises(capsule.CapsuleError):
                capsule.inspect(self.folder, include=include)
        linked = self.root / "linked"
        linked.symlink_to(self.folder)
        with self.assertRaises(capsule.CapsuleError):
            capsule.inspect(linked)

    def test_malformed_status_and_attempts_refuse_consistently(self):
        self.doc["checks"][0]["results"][0]["status"] = []
        reports.write_json(self.doc, self.folder / "results.json")
        capsule.capture(self.folder, self.doc)
        with self.assertRaises(capsule.CapsuleError):
            capsule.inspect(self.folder)
        self.doc["checks"][0]["results"][0]["status"] = "failed"
        self.doc["checks"][0]["results"][0]["runs"] = 1
        reports.write_json(self.doc, self.folder / "results.json")
        with self.assertRaises(capsule.CapsuleError):
            capsule.capture(self.folder, self.doc)

    def test_export_stays_in_opened_parent_after_path_substitution(self):
        parent = self.root / "exports"
        parent.mkdir()
        external_temp = TemporaryDirectory()
        self.addCleanup(external_temp.cleanup)
        external = Path(external_temp.name).resolve()
        (external / "out").mkdir()
        (external / "out" / "sentinel").write_text(SECRET)
        original_mkdir = os.mkdir
        swapped = False

        def relocate(path, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            original_mkdir(path, mode, dir_fd=dir_fd)
            if path == "out" and dir_fd is not None and not swapped:
                swapped = True
                parent.rename(self.root / "exports-old")
                parent.symlink_to(external, target_is_directory=True)

        with mock.patch.object(capsule.os, "mkdir", side_effect=relocate), \
                self.assertRaisesRegex(capsule.CapsuleError, "parent changed"):
            capsule.export(self.folder, parent / "out", include=("report",))
        self.assertTrue(swapped)
        self.assertEqual((external / "out" / "sentinel").read_text(), SECRET)
        self.assertFalse((external / "out" / "evidence").exists())
        self.assertFalse((self.root / "exports-old" / "out" / "capsule.json").exists())

    def test_export_accepts_a_benign_alias_to_an_existing_parent(self):
        parent = self.root / "physical"
        parent.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(parent, target_is_directory=True)
        capsule.export(self.folder, alias / "out")
        self.assertTrue((parent / "out" / "capsule.json").exists())

    def test_source_size_limit_and_changed_stat_refuse(self):
        with mock.patch.object(capsule, "MAX_DOC", 16), self.assertRaises(capsule.CapsuleError):
            capsule.read_completed_suite(self.folder)
        actual_fstat = os.fstat
        calls = 0

        def changing_stat(fd):
            nonlocal calls
            calls += 1
            value = actual_fstat(fd)
            if calls != 2:
                return value
            return SimpleNamespace(**{key: getattr(value, key) + (1 if key == "st_size" else 0)
                                      for key in ("st_mode", "st_nlink", "st_size", "st_dev", "st_ino",
                                                  "st_mtime_ns", "st_ctime_ns")})

        with capsule._directory(self.folder) as folder_fd, mock.patch.object(capsule.os, "fstat",
                                                                        side_effect=changing_stat):
            with self.assertRaisesRegex(capsule.CapsuleError, "changed"):
                capsule._read_at(folder_fd, "results.json", capsule.MAX_DOC)

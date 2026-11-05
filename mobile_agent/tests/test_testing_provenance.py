"""Saved-suite provenance reaches the paired controlled-state verdict."""

import hashlib
import json
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from mobile_agent.testkit import api, comparison, provenance
from mobile_agent.verify.business import load_business_binding
from mobile_agent.verify.checks import check_to_dict


class ProvenanceTests(unittest.TestCase):
    def test_actual_suite_reports_prove_a_changed_app_fixed_one_controlled_field(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            mobster = root / ".mobster"
            checks = mobster / "checks"
            checks.mkdir(parents=True)
            (mobster / "fixtures.json").write_text(json.dumps({"version": 1, "adapters": {
                "daybreak": {"kind": "app_container_json", "bundle": "dev.mobster.daybreak",
                             "fields": ["reminder.saved"], "fixtures": {
                                 "fresh-reminder": {"reminder.saved": False}}}}}))
            source = checks / "reminder.yaml"
            artifacts = []
            for number in (1, 2):
                artifact = root / f"Daybreak-{number}.app"
                artifact.mkdir()
                (artifact / "Info.plist").write_bytes(plistlib.dumps({
                    "CFBundleIdentifier": "dev.mobster.daybreak", "CFBundleVersion": str(number),
                    "DTPlatformName": "iphonesimulator"}))
                (artifact / "binary").write_bytes(f"build {number}".encode())
                artifacts.append(artifact)

            saved = []
            for number, artifact in enumerate(artifacts, 1):
                source.write_text(yaml.safe_dump({"version": 1, "name": "Save reminder",
                    "app": {"bundle": "dev.mobster.daybreak", "path": str(artifact)},
                    "reset": "data", "business": {"adapter": "daybreak", "fixture": "fresh-reminder",
                    "expect": [{"field": "reminder.saved", "equals": True}]}}))
                planned = api.plan(api.Options(paths=(str(source),), keyless=True, retries=0), cwd=root)
                passed = number == 2

                def attempt(check, record, *, label, video_name=None):
                    binding = load_business_binding(check, root)
                    return {"run_id": f"run-{number}", "verdict": "passed" if passed else "failed",
                            "reason": {"class": None if passed else "business", "message": "saved state"},
                            "seconds": 0.01, "device": {"name": "Fake simulator", "type": "iPhone 17 Pro",
                            "runtime": "iOS 26.4", "udid": "SIM-1"}, "frames": [], "run_dir": None,
                            "report": None, "cost_usd": 0,
                            "check": check_to_dict(check, project_root=root),
                            "business": {"bindingSha256": binding.digest, "setup": "seeded",
                                         "status": "observed", "cleanup": "removed", "generation": 1,
                                         "sourceSha256": hashlib.sha256(f"state {number}".encode()).hexdigest(),
                                         "assertions": [{"field": "reminder.saved", "ok": passed}]}}

                doc, paths = api.execute(planned, attempts=attempt, cwd=root)
                check = doc["checks"][0]
                self.assertEqual(check["businessBindingSha256"],
                                 check["results"][0]["business"]["bindingSha256"])
                self.assertIsNone(check["visualBaselineSha256"])
                self.assertEqual(len(check["results"][0]["runs"]), 1)
                saved.append(doc["suite"])

            verdict = comparison.compare_suites(mobster / "test-results", *saved)
            self.assertTrue(verdict["comparable"], verdict)
            self.assertTrue(verdict["verifiedFix"], verdict)
            self.assertEqual(verdict["proof"]["scope"], "controlled_app_state")


class SuiteInputTests(unittest.TestCase):
    """What can't be read stays one check's business; an input known to have changed stops the suite before effects."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.checks = self.root / ".mobster" / "checks"
        self.checks.mkdir(parents=True)
        self.apps = {}
        for name in ("a", "b", "c"):
            app = self.root / "build" / name / "App.app"
            app.mkdir(parents=True)
            (app / "binary").write_bytes(name.encode())
            self.apps[name] = app
            self.write(name, app)

    def write(self, name, app, **fields):
        (self.checks / f"{name}.yaml").write_text(yaml.safe_dump({
            "version": 1, "name": name, "app": {"bundle": "dev.example.app", "path": str(app)},
            "expect": [{"text": "Ready"}], **fields}))

    def oversize_media(self):
        media = self.root / "media"
        media.mkdir()
        with (media / "big.jpg").open("wb") as handle:
            handle.truncate(129 * 1024 * 1024)  # sparse: refused by its size, never read
        self.write("b", self.apps["b"], conditions={"media": [str(media / "big.jpg")]})

    def run_suite(self, *, during=None, before_execute=None, couldnt_run=(), **options):
        self.calls = []

        def attempt(check, record, *, label, video_name=None):
            self.calls.append(check.name)
            if during is not None:
                during(check.name)
            failed = check.name in couldnt_run
            return {"run_id": f"run-{check.name}", "verdict": "couldnt_run" if failed else "passed",
                    "reason": {"class": "install", "message": "The app is missing."} if failed else None,
                    "seconds": 0.01, "device": {"name": "Sim", "type": "iPhone 17 Pro", "runtime": "iOS 26.4",
                                                "udid": "SIM-1"}, "frames": []}

        planned = api.plan(api.Options(keyless=True, retries=0, **options), cwd=self.root)
        if before_execute is not None:
            before_execute()
        doc, _ = api.execute(planned, attempts=attempt, cwd=self.root)
        return doc, {check["name"]: check["results"][0] for check in doc["checks"]}

    def row(self, doc, name):
        return next(check for check in doc["checks"] if check["name"] == name)

    def notes_about(self, doc, name):
        return [note for note in doc.get("notes", ()) if f".mobster/checks/{name}.yaml" in note]

    def test_an_app_that_is_missing_fails_only_its_own_check(self):
        shutil.rmtree(self.apps["b"])
        doc, cells = self.run_suite(couldnt_run=("b",))
        self.assertEqual(self.calls, ["a", "b", "c"])
        self.assertEqual((doc["exitCode"], doc.get("stopReason")), (3, None))
        self.assertEqual({name: cell["status"] for name, cell in cells.items()},
                         {"a": "passed", "b": "couldnt_run", "c": "passed"})
        self.assertEqual(cells["b"]["reason"]["class"], "install")
        self.assertIsNone(cells["b"]["appDigest"])
        self.assertTrue(cells["a"]["appDigest"] and cells["c"]["appDigest"])

    def test_an_app_holding_a_symlink_runs_and_only_its_digest_is_null(self):
        (self.apps["b"] / "Current").symlink_to("binary")
        doc, cells = self.run_suite()
        self.assertEqual(self.calls, ["a", "b", "c"])
        self.assertEqual((doc["exitCode"], doc.get("stopReason")), (0, None))
        self.assertIsNone(cells["b"]["appDigest"])
        self.assertIsNotNone(self.row(doc, "b")["checkSpecDigest"])
        self.assertNotIn("provenanceDrift", self.row(doc, "b"))
        self.assertIn("could not pin the app of", self.notes_about(doc, "b")[0])
        self.assertTrue(cells["a"]["appDigest"] and cells["c"]["appDigest"])
        self.assertEqual(self.notes_about(doc, "a"), [])

    def test_an_oversize_asset_runs_with_its_file_and_app_still_pinned(self):
        self.oversize_media()
        doc, cells = self.run_suite()
        self.assertEqual(self.calls, ["a", "b", "c"])
        self.assertEqual((doc["exitCode"], doc.get("stopReason")), (0, None))
        self.assertIsNone(self.row(doc, "b")["checkSpecDigest"])
        self.assertIsNotNone(cells["b"]["appDigest"])
        self.assertIn("could not pin the fixtures or assets of", self.notes_about(doc, "b")[0])

        for change in (lambda: (self.apps["b"] / "binary").write_bytes(b"another build"),
                       lambda: self.write("b", self.apps["b"], expect=[{"text": "Edited"}])):
            with self.subTest():
                self.setUp()
                self.oversize_media()
                doc, cells = self.run_suite(during=lambda name: change() if name == "a" else None)
                self.assertEqual(self.calls, ["a"])
                self.assertEqual((doc["exitCode"], doc["stopReason"]), (3, "refused"))
                self.assertEqual(cells["b"]["safetyStop"], "refused")

    def test_a_check_file_that_cannot_be_read_leaves_its_spec_and_app_pinned(self):
        entry = api.plan(api.Options(keyless=True, retries=0), cwd=self.root).entries[0]
        with mock.patch.object(provenance, "_source_digest", side_effect=ValueError("unreadable")):
            frozen = provenance.case_provenance(entry, self.root)
        self.assertIsNone(frozen["sourceDigest"])
        self.assertTrue(frozen["checkSpecDigest"] and frozen["appDigest"])
        self.assertEqual(provenance.unpinned(frozen, entry.check), ["check file"])

    def test_a_pinned_input_that_changes_refuses_before_the_next_attempt_and_stops_the_suite(self):
        def rewrite():
            (self.apps["b"] / "binary").write_bytes(b"another build")

        def remove():
            shutil.rmtree(self.apps["b"])

        def gain_symlink():
            (self.apps["b"] / "Current").symlink_to("binary")

        def edit_check():
            self.write("b", self.apps["c"])

        for change in (rewrite, remove, gain_symlink, edit_check):
            with self.subTest(change=change.__name__):
                self.setUp()
                doc, cells = self.run_suite(during=lambda name: change() if name == "a" else None)
                self.assertEqual(self.calls, ["a"])
                self.assertEqual((doc["exitCode"], doc["stopReason"]), (3, "refused"))
                self.assertEqual((cells["b"]["status"], cells["b"]["safetyStop"]), ("couldnt_run", "refused"))
                self.assertIn(".mobster/checks/b.yaml", cells["b"]["reason"]["message"])
                self.assertEqual(cells["c"]["reason"]["class"], "stopped")

    def fixture_check(self):
        fixtures = self.root / ".mobster" / "fixtures"
        fixtures.mkdir()
        self.fixture = fixtures / "cart.yaml"
        self.fixture.write_text("steps: [open cart]\n")
        self.links = self.root / ".mobster" / "links.yaml"
        self.links.write_text("links: {cart: 'myapp://cart'}\n")
        self.write("b", self.apps["b"], fixture=["cart"])

    def test_a_fixture_that_changed_since_planning_refuses_and_an_unchanged_one_runs(self):
        self.fixture_check()
        doc, cells = self.run_suite()
        self.assertEqual((self.calls, doc["exitCode"]), (["a", "b", "c"], 0))
        self.assertEqual(self.notes_about(doc, "b"), [])

        for change in (lambda: self.fixture.write_text("steps: [tap another button]\n"),
                       lambda: self.links.write_text("links: {cart: 'myapp://different'}\n"),
                       lambda: self.fixture.unlink()):
            with self.subTest():
                self.setUp()
                self.fixture_check()
                doc, cells = self.run_suite(before_execute=change)
                self.assertEqual(self.calls, ["a"])
                self.assertEqual((doc["exitCode"], doc["stopReason"]), (3, "refused"))
                self.assertEqual(cells["b"]["safetyStop"], "refused")
                self.assertIn(".mobster/checks/b.yaml", cells["b"]["reason"]["message"])
                self.assertEqual(self.notes_about(doc, "b"), [])

    def test_an_accessibility_baseline_that_changed_since_planning_refuses_and_an_unchanged_one_runs(self):
        baseline = self.root / ".mobster" / "a11y-baseline.json"
        document = {"schema": "mobster.a11y-baseline/1", "findings": ["old"]}
        baseline.write_text(json.dumps(document))
        doc, _ = self.run_suite(a11y="warn")
        self.assertEqual((self.calls, doc["exitCode"]), (["a", "b", "c"], 0))

        doc, cells = self.run_suite(a11y="warn", before_execute=lambda: baseline.write_text(json.dumps(
            {**document, "findings": ["old", "new"]})))
        self.assertEqual(self.calls, [])
        self.assertEqual((doc["exitCode"], doc["stopReason"]), (3, "refused"))
        self.assertEqual(cells["a"]["safetyStop"], "refused")

    def test_repeat_refuses_an_app_it_cannot_pin_and_the_other_checks_repeat(self):
        doc, cells = self.run_suite(repeat=2)
        self.assertEqual((self.calls, doc["exitCode"]), (["a", "a", "b", "b", "c", "c"], 0))

        (self.apps["b"] / "Current").symlink_to("binary")
        doc, cells = self.run_suite(repeat=2)
        self.assertEqual(self.calls, ["a", "a", "c", "c"])
        self.assertEqual((doc["exitCode"], doc.get("stopReason")), (3, None))
        self.assertEqual((cells["b"]["status"], cells["b"]["attempts"]), ("couldnt_run", 0))
        self.assertIn("--repeat needs one pinned build", cells["b"]["reason"]["message"])
        self.assertNotIn("provenanceDrift", self.row(doc, "b"))
        self.assertEqual((cells["a"]["status"], cells["c"]["status"]), ("passed", "passed"))


if __name__ == "__main__":
    unittest.main()

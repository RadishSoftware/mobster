"""Bounded paired reduction with fake canonical results; no simulator, model or network."""

import json
import threading
import unittest
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import yaml

from mobile_agent import __version__
from mobile_agent.testkit import api, capsule, comparison, minimize, provenance, reports
from mobile_agent.testkit.discover import Entry
from mobile_agent.testkit.provenance import app_digest, case_provenance
from mobile_agent.verify.checks import check_to_dict, load_check


class MinimizationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        mobster = self.root / ".mobster"
        (mobster / "checks").mkdir(parents=True)
        (mobster / "fixtures.json").write_text(json.dumps({"version": 1, "adapters": {
            "daybreak": {"kind": "app_container_json", "bundle": "dev.mobster.daybreak",
                         "fields": ["reminder.saved"], "fixtures": {
                             "fresh-reminder": {"reminder.saved": False}}}}}))
        self.bad_app, self.control_app = self.root / "Bad.app", self.root / "Control.app"
        for path, data in ((self.bad_app, b"bad app"), (self.control_app, b"control app")):
            path.mkdir()
            (path / "binary").write_bytes(data)
        self.source = mobster / "checks" / "reminder.yaml"
        self.source.write_text(yaml.safe_dump({
            "version": 1, "name": "Save reminder", "app": {"bundle": "dev.mobster.daybreak",
            "path": str(self.bad_app)}, "reset": "data", "steps": ["Open reminder", "Save reminder"],
            "business": {"adapter": "daybreak", "fixture": "fresh-reminder",
                         "expect": [{"field": "reminder.saved", "equals": True}]}}))
        self.results = mobster / "test-results"
        self.results.mkdir()
        source_plan = self.plan(self.source)
        self.source_frozen = case_provenance(source_plan.entries[0], self.root)
        self.case_ref = comparison._ref(source_plan.entries[0].file)
        self.target_ref = comparison._ref("sim:iPhone 17 Pro@iOS 26.4")
        self.bad_suite, self.control_suite = "20261024-120000-abcd", "20261024-120001-abcd"
        self.save(self.report(self.bad_suite, self.source_frozen, "bad", failed=True))
        control_frozen = {**self.source_frozen, "appDigest": app_digest(self.control_app)}
        self.save(self.report(self.control_suite, control_frozen, "control", failed=False))
        self.assertTrue(comparison.compare_suites(self.results, self.bad_suite,
                                                  self.control_suite)["verifiedFix"])

    def plan(self, path):
        return api.plan(api.Options(paths=(str(path),), sims=("iPhone 17 Pro@iOS 26.4",),
                                    fresh=True, retries=0), cwd=self.root)

    def report(self, suite, frozen, arm, *, failed):
        status = "failed" if failed else "passed"
        business = {"bindingSha256": frozen["businessBindingSha256"], "setup": "seeded",
                    "status": "observed", "cleanup": "removed", "generation": 1,
                    "sourceSha256": ("a" if arm == "bad" else "b") * 64,
                    "assertionFields": ["reminder.saved"],
                    "passedFields": [] if failed else ["reminder.saved"],
                    "failedFields": ["reminder.saved"] if failed else []}
        cell = {"deviceKey": "sim:iPhone 17 Pro@iOS 26.4", "status": status, "attempts": 1,
                "reason": {"class": "business"} if failed else None, "business": business,
                "runs": [{"verdict": status, "class": "business" if failed else None,
                          "business": deepcopy(business)}],
                "environment": {"kind": "simulator", "model": "iPhone 17 Pro", "runtime": "iOS 26.4"},
                "appDigest": frozen["appDigest"], "flaky": False, "quarantined": False,
                "replayed": False, "healed": [], "assertions": [], "visual": None}
        row = {"file": ".mobster/checks/reminder.yaml", "results": [cell],
               **{key: frozen[key] for key in ("checkSpecDigest", "baselineDigest", "visualBaselineSha256",
                                                  "fixtureDigest", "businessBindingSha256")}}
        return {"schema": reports.SCHEMA, "suite": suite, "finishedAt": 1,
                "exitCode": 1 if failed else 0, "options": {"strict": False},
                "executionPolicy": {"retries": 0, "repeat": 1, "replay": "fresh", "keyless": False,
                                    "assertTimeout": 5, "mobster": __version__}, "checks": [row]}

    def save(self, doc):
        folder = self.results / doc["suite"]
        folder.mkdir()
        reports.write_json(doc, folder / "results.json")
        capsule.capture(folder, doc)

    def reduce(self, **kwargs):
        return minimize.minimize(self.results, self.bad_suite, self.control_suite,
                                 case_ref=self.case_ref, target_ref=self.target_ref,
                                 bad_app=self.bad_app, control_app=self.control_app,
                                 isolated=True, **kwargs)

    def evaluator(self, calls, *, trigger="Save reminder", unknown_at=None):
        def evaluate(path, options):
            check = load_check(path)
            arm = "control" if Path(check.app_path).name == "Control.app" else "bad"
            calls.append((arm, tuple(check.steps), options.replay_mode))
            frozen = case_provenance(api.plan(options, cwd=self.root).entries[0], self.root)
            failed = arm == "bad" or trigger not in check.steps
            doc = self.report(f"20261024-1300{len(calls):02d}-abcd", frozen, arm, failed=failed)
            if unknown_at == len(calls):
                doc["stopReason"] = "unknown_outcome"
            return doc
        return evaluate

    def test_reduction_keeps_trigger_and_never_changes_saved_check(self):
        original = self.source.read_bytes()
        calls = []
        result = self.reduce(evaluator=self.evaluator(calls))
        self.assertEqual((result["status"], result["candidateSteps"], result["attemptsUsed"]),
                         ("smaller_reproducer", 1, 7))
        self.assertEqual(load_check(self.root / result["candidateCheck"]).steps, ("Save reminder",))
        self.assertEqual(self.source.read_bytes(), original)
        self.assertTrue(all(mode == "fresh" for _, _, mode in calls))
        with self.assertRaises(minimize.MinimizationError):
            self.reduce(evaluator=self.evaluator([]))  # exact durable intent prevents a second experiment

    def test_deleting_trigger_fails_clean_control_and_is_rejected(self):
        calls = []
        result = self.reduce(evaluator=self.evaluator(calls, trigger="Open reminder"))
        self.assertEqual(result["status"], "smaller_reproducer")
        self.assertEqual(load_check(self.root / result["candidateCheck"]).steps, ("Open reminder",))
        self.assertIn(("control", ("Save reminder",), "fresh"), calls)

    def test_unknown_outcome_stops_without_another_dispatch(self):
        calls = []
        result = self.reduce(evaluator=self.evaluator(calls, unknown_at=2))
        self.assertEqual((result["status"], result["attemptsUsed"]), ("stopped_unknown", 2))
        self.assertEqual(len(calls), 2)
        self.assertIsNone(result["candidateCheck"])

    def test_source_edit_after_original_pair_stops_before_any_deletion(self):
        calls = []
        normal = self.evaluator(calls)

        def edit_after_control(path, options):
            doc = normal(path, options)
            if len(calls) == 2:
                self.source.write_text(self.source.read_text() + "\n# changed after admission\n")
            return doc

        result = self.reduce(evaluator=edit_after_control)
        self.assertEqual((result["status"], result["attemptsUsed"]), ("stopped_unknown", 2))
        self.assertEqual(len(calls), 2)

    def test_isolation_refusal_and_stop_prevent_dispatch(self):
        evaluate = mock.Mock()
        with self.assertRaises(minimize.MinimizationError):
            minimize.minimize(self.results, self.bad_suite, self.control_suite, case_ref=self.case_ref,
                              target_ref=self.target_ref, bad_app=self.bad_app, control_app=self.control_app,
                              evaluator=evaluate)
        with self.assertRaisesRegex(minimize.MinimizationError, "between 5 and 8"):
            self.reduce(evaluator=evaluate, max_attempts=4)
        stop = threading.Event()
        stop.set()
        result = self.reduce(evaluator=evaluate, stop=stop)
        self.assertEqual((result["status"], result["attemptsUsed"]), ("stopped", 0))
        evaluate.assert_not_called()

    def test_preplanted_draft_links_never_write_external_file(self):
        external = TemporaryDirectory()
        self.addCleanup(external.cleanup)
        sentinel = Path(external.name).resolve() / "sentinel"
        sentinel.write_text("unchanged")
        original = minimize._draft_directory

        @contextmanager
        def planted(*args):
            with original(*args) as draft:
                (draft.folder / "result.json").symlink_to(sentinel)
                (draft.folder / "trial-1.yaml").symlink_to(sentinel)
                yield draft

        evaluate = mock.Mock()
        with mock.patch.object(minimize, "_draft_directory", planted):
            with self.assertRaises(minimize.MinimizationError):
                self.reduce(evaluator=evaluate)
        self.assertEqual(sentinel.read_text(), "unchanged")
        evaluate.assert_not_called()

    def test_default_adapter_refuses_changed_plan_before_execute(self):
        options = api.Options(paths=(str(self.source),), sims=("iPhone 17 Pro@iOS 26.4",), fresh=True)
        entry = Entry(self.source, ".mobster/checks/reminder.yaml", load_check(self.source))
        selected = SimpleNamespace(root=self.root, entries=[entry])
        with mock.patch.object(minimize, "plan", return_value=selected), \
                mock.patch.object(minimize, "case_provenance",
                                  return_value={**self.source_frozen, "checkSpecDigest": "0" * 64}), \
                mock.patch.object(minimize, "execute") as execute:
            with self.assertRaises(minimize.MinimizationError):
                minimize._canonical_evaluate(self.root, self.source, options, None,
                                             threading.Event(), self.source_frozen)
        execute.assert_not_called()
        selected.entries[0].path = self.root / "other.yaml"
        with mock.patch.object(minimize, "plan", return_value=selected), \
                mock.patch.object(minimize, "case_provenance", return_value=self.source_frozen), \
                mock.patch.object(minimize, "execute") as execute:
            with self.assertRaises(minimize.MinimizationError):
                minimize._canonical_evaluate(self.root, self.source, options, None,
                                             threading.Event(), self.source_frozen)
        execute.assert_not_called()

    def test_a_source_or_trial_file_it_cannot_freeze_refuses_before_any_trial(self):
        real = provenance._source_digest

        def unreadable_source(path):
            raise ValueError("unreadable")

        evaluate = mock.Mock()
        with mock.patch.object(provenance, "_source_digest", unreadable_source):
            with self.assertRaises(minimize.MinimizationError):
                self.reduce(evaluator=evaluate)

        def unreadable_trial(path):
            if Path(path).name.startswith("trial-"):
                raise ValueError("unreadable")
            return real(path)

        with mock.patch.object(provenance, "_source_digest", unreadable_trial):
            result = self.reduce(evaluator=evaluate)
        self.assertEqual((result["status"], result["attemptsUsed"]), ("stopped_unknown", 0))
        evaluate.assert_not_called()

    def test_default_adapter_marks_each_trial_so_it_cannot_save_a_replay_path(self):
        draft = self.root / ".mobster" / "drafts" / "repro" / "pair"
        draft.mkdir(parents=True)
        trial = draft / "trial-1.yaml"
        trial.write_text(self.source.read_text())
        options = api.Options(paths=(str(trial),), sims=("iPhone 17 Pro@iOS 26.4",), fresh=True, retries=0)
        frozen = case_provenance(api.plan(options, cwd=self.root).entries[0], self.root)
        seen = []

        def attempt(check, record, *, label, video_name=None):
            seen.append(check)
            return {"run_id": "trial", "verdict": "passed", "seconds": 0.01, "frames": [],
                    "device": {"type": "iPhone 17 Pro", "runtime": "iOS 26.4", "udid": "SIM-1"}}

        with mock.patch.object(minimize, "execute", lambda planned, **kwargs: api.execute(planned, attempts=attempt,
                                                                                          **kwargs)):
            doc = minimize._canonical_evaluate(self.root, trial, options, None, threading.Event(), frozen)
        self.assertEqual((doc["exitCode"], len(seen)), (0, 1))  # the flag leaves the pinned inputs equal
        self.assertTrue(seen[0].draft_trial)
        self.assertFalse(self.plan(self.source).entries[0].check.draft_trial)
        self.assertEqual(check_to_dict(seen[0]), check_to_dict(replace(seen[0], draft_trial=False)))

    def test_swapped_draft_parent_refuses_without_external_write_or_dispatch(self):
        external = TemporaryDirectory()
        self.addCleanup(external.cleanup)
        outside = Path(external.name).resolve()
        sentinel = outside / "sentinel"
        sentinel.write_text("unchanged")
        original = minimize._draft_directory

        @contextmanager
        def swapped(*args):
            with original(*args) as draft:
                foreign = outside / "repro" / draft.folder.name
                foreign.mkdir(parents=True)
                for name in ("result.json", "trial-1.yaml"):
                    (foreign / name).symlink_to(sentinel)
                parent = self.root / ".mobster" / "drafts"
                parent.rename(self.root / ".mobster" / "drafts-moved")
                parent.symlink_to(outside, target_is_directory=True)
                yield draft

        evaluate = mock.Mock()
        with mock.patch.object(minimize, "_draft_directory", swapped):
            with self.assertRaises(minimize.MinimizationError):
                self.reduce(evaluator=evaluate)
        self.assertEqual(sentinel.read_text(), "unchanged")
        evaluate.assert_not_called()


if __name__ == "__main__":
    unittest.main()

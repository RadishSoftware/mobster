"""Replay persistence during accessibility baseline refresh."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent.replay_core import heal
from mobile_agent.replay_core.checks import CheckReplay
from mobile_agent.tests import replay_world as W
from mobile_agent.tests.test_replay_core import Project
from mobile_agent.verify.checks import dump_check
from mobile_agent.verify.report import _replay_fact


class ReplayRefreshTests(unittest.TestCase):
    def test_a11y_refresh_proves_the_applied_empty_baseline_without_trusting_old_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            checks = root / ".mobster" / "checks"
            checks.mkdir(parents=True)
            source = checks / "signup.yaml"
            source.write_text(dump_check(W.check()), encoding="utf-8")
            baseline = root / ".mobster" / "a11y-baseline.json"
            check = replace(W.check(), source=str(source), a11y="strict", a11y_scope="signup",
                            a11y_baseline=frozenset())
            empty_proof = heal.proof_assets(check)["a11ySha256"]
            baseline.write_text("not json", encoding="utf-8")
            with self.assertRaises(heal.HealError):
                heal.proof_assets(check)
            refreshing = replace(check, a11y_refresh=True)
            self.assertEqual(heal.proof_assets(refreshing)["a11ySha256"], empty_proof)
            with self.assertRaises(heal.HealError):
                heal.proof_assets(replace(refreshing, a11y_baseline=frozenset({"stale finding"})))

    def test_a11y_refresh_keeps_the_approved_path_and_old_baseline_when_replay_heals(self):
        from mobile_agent.testkit import a11y_baseline

        for old_bytes in (None, "not json", '{"schema":"mobster.a11y-baseline/1","findings":["old"]}',
                          '{"schema":"mobster.a11y-baseline/1","findings":[]}'):
            with self.subTest(old_bytes=old_bytes):
                project = Project(self)
                self.assertEqual(project.run()["verdict"], "passed")
                cache_file = next((project.root / ".mobster" / "cache" / "replay").glob("*.json"))
                approved = cache_file.read_bytes()
                baseline_file = a11y_baseline.path(project.root)
                if old_bytes is not None:
                    baseline_file.write_text(old_bytes, encoding="utf-8")
                project.state["build"] = 2
                result = project.run(overlay=lambda check: replace(check, a11y_refresh=True,
                                                                    a11y_baseline=frozenset()))
                self.assertEqual((result["verdict"], result["reason"]["class"],
                                  result["replay"]["status"]),
                                 ("couldnt_run", "replay", "proposal_unavailable"))
                self.assertTrue(result["healed"])
                self.assertEqual(cache_file.read_bytes(), approved)
                if old_bytes is None:
                    self.assertFalse(baseline_file.exists())
                else:
                    self.assertEqual(baseline_file.read_text(encoding="utf-8"), old_bytes)
                self.assertFalse(heal.heal_file(project.file).exists())
                self.assertIn("approved path and baseline were kept", _replay_fact(result["replay"]))
                self.assertNotIn("steps with no model", _replay_fact(result["replay"]))

    def test_a11y_refresh_does_not_record_a_fresh_or_unchanged_path(self):
        project = Project(self)
        refresh = lambda check: replace(check, a11y_refresh=True, a11y_baseline=frozenset())
        fresh = project.run(replay="fresh", overlay=refresh)
        self.assertEqual(fresh["verdict"], "passed")
        self.assertFalse(fresh["replay"]["saved"])
        cache = project.root / ".mobster" / "cache" / "replay"
        self.assertEqual(list(cache.glob("*.json")), [])

        self.assertEqual(project.run()["verdict"], "passed")
        cache_file = next(cache.glob("*.json"))
        approved = cache_file.read_bytes()
        replayed = project.run(overlay=refresh)
        self.assertEqual(replayed["verdict"], "passed")
        self.assertEqual(replayed["replay"]["mode"], "replayed")
        self.assertEqual(cache_file.read_bytes(), approved)

    def test_a_minimize_trial_keeps_the_approved_path_and_a_plain_fresh_run_still_replaces_it(self):
        project = Project(self)
        self.assertEqual(project.run()["verdict"], "passed")
        cache = project.root / ".mobster" / "cache" / "replay"
        cache_file = next(cache.glob("*.json"))
        approved = cache_file.read_bytes()
        draft = project.root / ".mobster" / "drafts" / "repro" / "pair" / "trial-1.yaml"
        draft.parent.mkdir(parents=True)
        draft.write_text(project.file.read_text(encoding="utf-8"), encoding="utf-8")

        trial = project.run(replay="fresh", overlay=lambda check: replace(check, source=str(draft), draft_trial=True))
        self.assertEqual(trial["verdict"], "passed")
        self.assertFalse(trial["replay"]["saved"])
        self.assertEqual(list(cache.glob("*.json")), [cache_file])
        self.assertEqual(cache_file.read_bytes(), approved)

        fresh = project.run(replay="fresh")
        self.assertEqual(fresh["verdict"], "passed")
        self.assertTrue(fresh["replay"]["saved"])
        self.assertNotEqual(cache_file.read_bytes(), approved)

    def test_a_healed_refresh_with_a_new_a11y_finding_cannot_replace_the_baseline(self):
        from mobile_agent.testkit import a11y_baseline

        project = Project(self)
        self.assertEqual(project.run()["verdict"], "passed")
        cache_file = next((project.root / ".mobster" / "cache" / "replay").glob("*.json"))
        approved = cache_file.read_bytes()
        baseline_file = a11y_baseline.path(project.root)
        baseline_file.write_text('{"schema":"mobster.a11y-baseline/1","findings":[]}', encoding="utf-8")
        old_baseline = baseline_file.read_bytes()
        project.state["build"] = 2
        finding = {"audit": "a11y", "level": "fail", "key": "new issue"}
        original_after = CheckReplay.after
        def with_a11y_finding(replay, result):
            if replay.run.check.a11y_refresh:
                result.update(verdict="failed", exit_code=1,
                              reason={"class": "finding", "message": "A new accessibility finding."},
                              findings=[finding])
            return original_after(replay, result)
        with mock.patch.object(CheckReplay, "after", with_a11y_finding):
            result = project.run(overlay=lambda check: replace(check, a11y="warn", a11y_scope="signup",
                                                               a11y_refresh=True, a11y_baseline=frozenset()))
        self.assertEqual((result["verdict"], result["reason"]["class"], result["replay"]["status"]),
                         ("couldnt_run", "replay", "proposal_unavailable"))
        self.assertIn(finding, result["findings"])
        self.assertTrue(result["healed"])
        self.assertEqual(cache_file.read_bytes(), approved)
        self.assertEqual(baseline_file.read_bytes(), old_baseline)
        entry = SimpleNamespace(check=replace(W.check(), a11y="warn"))
        cell = {"status": result["verdict"], "reason": result["reason"], "findings": result["findings"],
                "attempts": 1, "screenCoverage": {"complete": True, "a11yScreens": 1}}
        self.assertIn("could not complete", a11y_baseline.refresh_error(
            {"checks": [{"results": [cell]}]}, [entry]))

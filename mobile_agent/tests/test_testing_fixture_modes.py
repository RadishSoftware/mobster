"""Fixture steps take the same admission path as steps written in a check."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from mobile_agent.testkit import api
from mobile_agent.testkit.discover import discover
from mobile_agent.testkit.execute import Attempts
from mobile_agent.testkit.provenance import case_provenance
from mobile_agent.testkit.targets import DEFAULT
from mobile_agent.verify.checks import load_check


class FixtureModeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        checks = self.root / ".mobster" / "checks"
        fixtures = self.root / ".mobster" / "fixtures"
        checks.mkdir(parents=True)
        fixtures.mkdir()
        self.check = checks / "login.yaml"
        self.check.write_text("version: 1\nname: Login\napp: {bundle: dev.example.app}\n"
                              "fixture: [signed-in]\nexpect: [{text: Welcome}]\n")
        self.fixture = fixtures / "signed-in.yaml"
        self.fixture.write_text("steps: [tap Sign in]\n")

    def test_fixture_only_steps_are_effective_before_mode_and_keyless_refuses_without_acquire(self):
        planned = api.plan(api.Options(paths=(str(self.check),), keyless=True), cwd=self.root)
        self.assertEqual(planned.entries[0].check.steps, ("tap Sign in",))
        acquired = []

        class Manager:
            def acquire(self, *args, **kwargs):
                acquired.append(True)
                raise AssertionError("a keyless step must not acquire a device")

        attempts = Attempts(runs_dir=self.root / ".mobster" / "runs", keyless=True, simulators=Manager())
        result = attempts(load_check(self.check), None, label="Login")
        self.assertEqual(result["reason"]["class"], "usage")
        self.assertEqual(acquired, [])

    def test_raw_fixture_repeat_retains_the_declared_pin_after_flattening(self):
        check = replace(load_check(self.check), source=None)

        class Run:
            def __init__(self, check, **kwargs):
                self.check = check
                self.repeat_pin = None

            def abort(self, klass, message, fix=None):
                return {"class": klass, "pinned": self.repeat_pin is not None,
                        "effectiveSteps": self.check.steps}

        attempts = Attempts(runs_dir=self.root / ".mobster" / "runs", keyless=True,
                            simulators=object(), run_class=Run)
        attempts.bind_repeat(check, None)
        result = attempts(check, None, label="Login")
        self.assertEqual(result["class"], "usage")
        self.assertTrue(result["pinned"])
        self.assertEqual(result["effectiveSteps"], ("tap Sign in",))
        pin = next(iter(attempts._repeat_pins.values()))
        self.assertFalse(pin.invalid)
        self.assertIsNone(pin.bind_check(replace(check, steps=("tap Sign in",)),
                                         replace(check, steps=("tap Sign in",))))

    def test_bad_fixture_refuses_one_entry_without_hiding_valid_suite_evidence(self):
        other = self.root / ".mobster" / "checks" / "ordinary.yaml"
        other.write_text("version: 1\nname: Ordinary\napp: {bundle: dev.example.app}\n"
                         "expect: [{text: Ready}]\n")
        self.fixture.unlink()
        planned = api.plan(api.Options(), cwd=self.root)
        self.assertEqual(sum(entry.check is None for entry in planned.entries), 1)
        calls = []

        def attempt(check, record, *, label, video_name=None):
            calls.append(check.name)
            return {"run_id": "ordinary", "verdict": "passed", "seconds": 0.01,
                    "device": {"type": "iPhone 17 Pro", "runtime": "iOS 26.4"}, "frames": []}

        doc, _ = api.execute(planned, attempts=attempt, cwd=self.root)
        self.assertEqual(calls, ["Ordinary"])
        self.assertEqual([row["results"][0]["status"] for row in doc["checks"]], ["couldnt_run", "passed"])

    def test_changed_inherited_fixture_or_link_changes_the_frozen_proof(self):
        parent = self.fixture.with_name("base.yaml")
        parent.write_text("steps: [open cart]\n")
        self.fixture.write_text("extends: base\n")
        links = self.root / ".mobster" / "links.yaml"
        links.write_text("links: {cart: 'myapp://cart'}\n")
        planned = api.plan(api.Options(paths=(str(self.check),)), cwd=self.root)
        entry = planned.entries[0]
        self.assertEqual(entry.check.steps, ("Open the link myapp://cart",))
        frozen = case_provenance(entry, self.root)
        self.assertIsNotNone(frozen["checkSpecDigest"])
        parent.write_text("steps: [tap Sign in]\n")
        self.assertNotEqual(case_provenance(entry, self.root), frozen)
        parent.write_text("steps: [open cart]\n")
        links.write_text("links: {cart: 'myapp://new-cart'}\n")
        self.assertNotEqual(case_provenance(entry, self.root), frozen)

    def test_fixture_and_link_changes_refuse_before_the_next_attempt(self):
        parent = self.fixture.with_name("base.yaml")
        parent.write_text("steps: [open cart]\n")
        self.fixture.write_text("extends: base\n")
        links = self.root / ".mobster" / "links.yaml"
        links.write_text("links: {cart: 'myapp://cart'}\n")
        for change in ("fixture", "link"):
            with self.subTest(change=change):
                parent.write_text("steps: [open cart]\n")
                links.write_text("links: {cart: 'myapp://cart'}\n")
                planned = api.plan(api.Options(paths=(str(self.check),), repeat=2, retries=0), cwd=self.root)
                calls = []

                def attempt(check, record, *, label, video_name=None):
                    calls.append(check.steps)
                    if change == "fixture":
                        parent.write_text("steps: [tap another button]\n")
                    else:
                        links.write_text("links: {cart: 'myapp://different'}\n")
                    return {"run_id": "first", "verdict": "passed", "seconds": 0.01,
                            "device": {"type": "iPhone 17 Pro", "runtime": "iOS 26.4"}, "frames": []}

                doc, _ = api.execute(planned, attempts=attempt, cwd=self.root)
                self.assertEqual(len(calls), 1)
                self.assertEqual(doc["exitCode"], 3)
                self.assertEqual(doc["checks"][0]["results"][0]["safetyStop"], "refused")

    def test_direct_pr_plan_can_override_app_artifact_without_fixture_drift(self):
        artifact = self.root / "New.app"
        artifact.mkdir()
        (artifact / "binary").write_bytes(b"candidate app")
        entry = discover((str(self.check),), self.root, cwd=self.root)[0]
        entry.check = replace(entry.check, app_path=str(artifact))
        planned = api.Planned(self.root, [entry], [DEFAULT], api.Options(retries=0))

        def attempt(check, record, *, label, video_name=None):
            return {"run_id": "candidate", "verdict": "passed", "seconds": 0.01,
                    "device": {"type": "iPhone 17 Pro", "runtime": "iOS 26.4"}, "frames": []}

        doc, _ = api.execute(planned, attempts=attempt, cwd=self.root)
        self.assertEqual(doc["checks"][0]["results"][0]["status"], "passed")
        self.assertIsNotNone(doc["checks"][0]["checkSpecDigest"])
        self.assertIsNotNone(doc["checks"][0]["results"][0]["appDigest"])
        self.assertEqual(entry.check.app_path, str(artifact))


if __name__ == "__main__":
    unittest.main()

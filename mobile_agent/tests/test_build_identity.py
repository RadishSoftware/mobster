import os
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import build_identity, genuine, plan_gate, premium_model

ROOT = Path(__file__).resolve().parents[2]


class BuildIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mobster-build-identity-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.executable = self.root / "mobster"
        self.executable.touch()
        for target, field, value in ((build_identity.sys, "frozen", True),
                                     (build_identity.sys, "executable", str(self.executable)),
                                     (build_identity, "STANDALONE_CLI", False)):
            patcher = patch.object(target, field, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_source_checkout_has_no_implicit_app_gate(self):
        with patch.object(build_identity.sys, "frozen", False):
            self.assertIsNone(plan_gate.bundled_gate())
            self.assertIsNone(plan_gate.for_runtime(SimpleNamespace(), self.root))

    def test_cli_entry_selects_standalone_before_entering_the_command(self):
        def command():
            self.assertIsNone(plan_gate.bundled_gate())
            self.assertIsNone(plan_gate.for_runtime(SimpleNamespace(), self.root))
            return 17

        with patch.dict("sys.modules", {"mobile_agent.__main__": SimpleNamespace(main=command)}):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(ROOT / "packaging" / "cli" / "entry.py"), run_name="__main__")
        self.assertEqual(stopped.exception.code, 17)

    def test_missing_or_corrupt_cli_selection_keeps_the_app_gate(self):
        for value in (False, None, "true", "cli", 1):
            with self.subTest(value=value), patch.object(build_identity, "STANDALONE_CLI", value):
                self.assertIsInstance(plan_gate.bundled_gate(), plan_gate.PlanGate)

    def test_renaming_or_detaching_the_app_agent_does_not_select_the_cli(self):
        for path in (self.root / "mobster-agent", self.root / "mobster", self.root / "renamed"):
            with self.subTest(path=path.name), patch.object(build_identity.sys, "executable", str(path)):
                self.assertIsInstance(plan_gate.bundled_gate(), plan_gate.PlanGate)

    def test_app_location_wins_even_with_the_cli_entry_selected(self):
        executable = self.root / "Mobster.app" / "Contents" / "MacOS" / "mobster"
        executable.parent.mkdir(parents=True)
        executable.touch()
        link = self.root / "outside-mobster"
        link.symlink_to(executable)
        with patch.object(build_identity, "STANDALONE_CLI", True):
            for path in (executable, link):
                with self.subTest(path=path), patch.object(build_identity.sys, "executable", str(path)):
                    self.assertIsInstance(plan_gate.bundled_gate(), plan_gate.PlanGate)

    def test_unreadable_cli_location_keeps_the_app_gate(self):
        with patch.object(build_identity, "STANDALONE_CLI", True), \
                patch.object(build_identity.Path, "resolve", side_effect=OSError("unreadable")):
            self.assertIsInstance(plan_gate.bundled_gate(), plan_gate.PlanGate)

    def test_environment_cannot_select_the_cli_or_disable_the_app_gate(self):
        with patch.dict(os.environ, {"MOBSTER_STANDALONE_CLI": "1", "STANDALONE_CLI": "true",
                                     plan_gate.GATE_OFF_ENV: "off", premium_model.DEV_ENV: "1"}), \
                patch.object(plan_gate, "app_account_file", return_value=self.root / "missing-account.json"):
            gate = plan_gate.bundled_gate()
            self.assertIsInstance(gate, plan_gate.PlanGate)
            self.assertFalse(plan_gate.gate_turned_off())
            self.assertEqual(gate.refusal(), plan_gate.NO_PLAN)

    def test_explicit_app_runtime_requirement_still_applies_to_cli(self):
        with patch.object(build_identity, "STANDALONE_CLI", True):
            gate = plan_gate.for_runtime(SimpleNamespace(require_plan=True), self.root)
        self.assertIsInstance(gate, plan_gate.PlanGate)
        self.assertEqual(gate.account_file, self.root / plan_gate.ACCOUNT_FILE)

    def test_cli_does_not_require_the_apps_signature_when_premium_model_is_on(self):
        with patch.object(build_identity, "STANDALONE_CLI", True), patch.object(premium_model, "ON", True), \
                patch.object(genuine, "problem", side_effect=AssertionError("checked an app signature")):
            self.assertIsNone(genuine.failure())

    def test_app_keeps_failing_closed_on_missing_or_corrupt_signing_identity(self):
        for reason in ("missing signing identity", "corrupt signing identity"):
            with self.subTest(reason=reason), patch.object(premium_model, "ON", True), \
                    patch.object(genuine, "_answer", []), \
                    patch.object(genuine, "problem", return_value=(genuine.UNCHECKED, reason)), \
                    patch("builtins.print"):
                self.assertEqual(genuine.failure(), genuine.UNCHECKED)
                self.assertEqual(plan_gate.stopped("run"), plan_gate.UNCHECKED_COPY)

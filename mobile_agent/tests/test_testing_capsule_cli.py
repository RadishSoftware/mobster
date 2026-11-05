"""The local capsule command selects a suite ID and explicit evidence kinds, never source paths."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from mobile_agent import __main__
from mobile_agent.testkit import capsule, capsule_cli

SUITE_ID = "20261024-120000-abcd"


class CapsuleCommandTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / ".mobster" / "test-results").mkdir(parents=True)
        self.calls = []

    @staticmethod
    def descriptor():
        return {"schema": "mobster.test.capsule/1", "suite": SUITE_ID,
                "results": {"exitCode": 1},
                "evidence": [{"state": "matched", "selected": True}],
                "admission": "unavailable"}

    def main(self, argv):
        out, err = io.StringIO(), io.StringIO()

        def inspect(folder, *, include=()):
            self.calls.append(("show", folder, include))
            return self.descriptor()

        def export(folder, output, *, include=()):
            self.calls.append(("export", folder, output, include))
            return self.descriptor()

        with mock.patch.object(capsule_cli, "project_root", return_value=self.root), \
                mock.patch.object(capsule, "inspect", inspect), mock.patch.object(capsule, "export", export), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                code = __main__.main(["test", "capsule", *argv])
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_show_uses_the_local_suite_id_and_safe_descriptor(self):
        code, out, err = self.main(["show", SUITE_ID, "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, [("show", self.root / ".mobster" / "test-results" / SUITE_ID, ())])
        self.assertEqual(json.loads(out)["suite"], SUITE_ID)

    def test_export_selects_only_named_kinds_and_a_new_destination(self):
        output = self.root / "capsule"
        code, out, err = self.main(["export", SUITE_ID, "--output", str(output),
                                    "--include", "junit", "--include", "visual", "--json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, [("export", self.root / ".mobster" / "test-results" / SUITE_ID,
                                      output, ("junit", "visual"))])
        self.assertEqual(json.loads(out)["output"], str(output))

    def test_invalid_selector_and_unrelated_flags_refuse_without_reading(self):
        for argv in (["show", "../outside"], ["export", SUITE_ID],
                     ["show", SUITE_ID, "--include", "report"],
                     ["show", SUITE_ID, "--device", "iPhone"],
                     ["show", SUITE_ID, "--share"]):
            with self.subTest(argv=argv):
                code, _, err = self.main(argv)
                self.assertEqual(code, 3)
                self.assertTrue(err)
        self.assertEqual(self.calls, [])

    def test_include_cannot_leak_to_another_test_command(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = __main__.main(["test", "--include", "junit"])
        self.assertEqual(code, 3)
        self.assertIn("--include goes with capsule export", err.getvalue())

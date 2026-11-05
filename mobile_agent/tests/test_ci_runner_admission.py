"""CI forwarded arguments must not bypass the runner owner's device policy."""

import tempfile
import unittest
from pathlib import Path

from mobile_agent.devkit import DevError, ci


class ForwardedDevicePolicyTests(unittest.TestCase):
    def test_protected_forwarded_flags_refuse_before_git_or_child(self):
        with tempfile.TemporaryDirectory() as folder:
            effects = []

            def git(*args):
                effects.append(("git", args))
                return 0, "false"

            def runner(*args):
                effects.append(("runner", args))
                return 0, ""

            for extra in (("--device", "Other iPhone"), ("--device=Other iPhone",),
                          ("--testflight", "org.example.other"), ("--testflight=org.example.other",)):
                with self.subTest(extra=extra), self.assertRaises(DevError) as caught:
                    ci.run_checks(Path(folder), extra=extra,
                                  environ={"GITHUB_ACTIONS": "true", "GITHUB_BASE_REF": "main"},
                                  git=git, runner=runner)
                self.assertIn("nothing ran", str(caught.exception))
                self.assertEqual(effects, [])
                self.assertFalse((Path(folder) / ci.CI_DIR).exists())

    def test_allowed_top_level_device_and_unprotected_extra_reach_test_pr(self):
        with tempfile.TemporaryDirectory() as folder:
            calls = []

            def runner(argv, cwd, env):
                calls.append(argv)
                return 3, "no suite"

            ci.run_checks(Path(folder), devices=("Test iPhone",), extra=("--retries", "2"), base="origin/main",
                          environ={"GITHUB_ACTIONS": "true", "MOBSTER_RUNNER_DEVICES": "Test iPhone"},
                          runner=runner)
            self.assertEqual(len(calls), 1)
            self.assertIn("--device", calls[0])
            self.assertEqual(calls[0][-2:], ["--retries", "2"])

    def test_hidden_report_folders_are_included_in_both_github_uploads(self):
        root = Path(__file__).resolve().parents[2]
        generated = ci.github_workflow()
        example = (root / "examples/dev/github/mobster.yml").read_text()
        action = (root / "actions/test/action.yml").read_text()
        self.assertEqual(example, generated)
        for workflow in (generated, action):
            self.assertIn("uses: actions/upload-artifact@v4", workflow)
            self.assertIn("include-hidden-files: true", workflow)


if __name__ == "__main__":
    unittest.main()

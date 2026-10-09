"""Setup failures become one line to act on and a button; the tool's own text stays under Details. Offline."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mobile_agent import device_manager as dm
from mobile_agent import setup_errors

SAMPLES = Path(__file__).with_name("setup_errors")
ACTIONS = {"choose_team", "check_again", "retry", "copy_command"}
# What each raw sample must say in its fix, so a reordered rule can't give the wrong advice.
WORDS = {"no_team": "Choose your Apple Account", "developer_mode": "Developer Mode", "not_trusted": "tap Trust",
         "locked": "Unlock your iPhone", "xcode_components": "Xcode", "disk_full": "Free up space",
         "bundle_conflict": "app ID", "app_id_limit": "10 app IDs", "untrusted_developer": "VPN & Device Management",
         "not_paired": "plug it back in", "free_app_limit": "allows 3", "xcode_signin": "Xcode › Settings › Accounts"}


class TranslationTests(unittest.TestCase):
    def test_each_failure_becomes_a_fix_and_a_button(self):
        samples = sorted(SAMPLES.glob("*.txt"))
        self.assertEqual({path.stem for path in samples}, set(WORDS))
        for path in samples:
            with self.subTest(path.stem):
                text = path.read_text()
                problem = setup_errors.translate(text)
                self.assertIsNotNone(problem)
                self.assertEqual(problem["kind"], path.stem)
                self.assertIn(WORDS[path.stem], problem["fix"])
                self.assertIn(problem["action"], ACTIONS)
                self.assertNotIn("\n", problem["fix"])
                self.assertLess(len(problem["fix"]), 110)
                # The raw text is kept for Details, trimmed to the lines that say what failed.
                self.assertTrue(problem["raw"])
                self.assertTrue(set(problem["raw"].splitlines()) <= set(line.rstrip() for line in text.splitlines()))

    def test_look_alike_failures_get_the_fix_that_works(self):
        free_apps = setup_errors.translate((SAMPLES / "free_app_limit.txt").read_text())
        # iOS's 3-app cap on a free Apple ID: waiting for app IDs never fixes it, deleting an app does.
        self.assertEqual((free_apps["kind"], free_apps["action"]), ("free_app_limit", "retry"))
        self.assertNotIn("Wait", free_apps["fix"])
        # A chosen team whose Xcode sign-in lapsed is not "Choose your Apple Account".
        for text in ('error: No Account for Team "ABCDE12345". Add a new account in Accounts settings.',
                     "error: No accounts with App Store Connect access have been found for the team.",
                     "error: No accounts: Add a new account in Accounts settings.",
                     "error: Unable to log in with account 'jane@example.com'. The login details for account "
                     "'jane@example.com' were rejected."):
            with self.subTest(text):
                self.assertEqual(setup_errors.translate(text)["kind"], "xcode_signin")
        self.assertEqual(setup_errors.translate((SAMPLES / "xcode_signin.txt").read_text())["action"], "check_again")
        # A missing profile alone says nothing about whose app ID it is.
        bare = "error: No profiles for 'app.mobster.wda.runner.3f9c2a1b' were found: Xcode couldn't find any iOS " \
               "App Development provisioning profiles matching 'app.mobster.wda.runner.3f9c2a1b'."
        self.assertIsNone(setup_errors.translate(bare))
        self.assertEqual(setup_errors.problem(bare)["kind"], "unknown")
        self.assertEqual(setup_errors.translate('error: The app identifier "x" cannot be registered to your '
                                                'development team because it is not available.')["kind"],
                         "bundle_conflict")

    def test_a_components_fix_carries_the_command_to_copy(self):
        problem = setup_errors.translate((SAMPLES / "xcode_components.txt").read_text())
        self.assertEqual(problem["action"], "copy_command")
        self.assertIn("-runFirstLaunch", problem["command"])

    def test_unknown_text_falls_back_to_the_raw_text(self):
        text = "Build started\nerror: something nobody has seen before\n** TEST BUILD FAILED **"
        self.assertIsNone(setup_errors.translate(text))
        fallback = setup_errors.problem(text)
        self.assertEqual((fallback["kind"], fallback["fix"], fallback["action"]), ("unknown", None, None))
        self.assertIn("something nobody has seen before", fallback["raw"])
        self.assertIsNone(setup_errors.translate(""))
        self.assertIsNone(setup_errors.problem(None))

    def test_a_failed_build_carries_the_translation_beside_its_hint(self):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        lines = (SAMPLES / "no_team.txt").read_text().splitlines()

        def fail(command, log, tail):
            tail.extend(lines)
            raise RuntimeError("xcodebuild exited with 65")
        project = manager.project / "WebDriverAgent.xcodeproj"
        project.mkdir(parents=True)
        (project / "project.pbxproj").write_text("PRODUCT_BUNDLE_IDENTIFIER = com.facebook.WebDriverAgentRunner;\n")
        with patch.object(dm.wda_source, "is_pinned", return_value=True), patch.object(dm, "patch_wda"), \
                patch.object(manager, "_step", side_effect=fail), patch.object(dm, "tool", return_value="/bin/x"):
            manager._build("ABCDE12345", "u")
        self.assertEqual(manager.build["state"], "failed")
        self.assertEqual(manager.build["problem"]["fix"], "Choose your Apple Account")
        self.assertIn("requires a development team", manager.build["problem"]["raw"])
        self.assertIn("development team", manager.build["error"])


if __name__ == "__main__":
    unittest.main()

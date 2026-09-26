"""CLI parsing and prompt-scaffolding contracts. No devices, no providers."""

import unittest

from mobile_agent.__main__ import build_parser
from mobile_agent.models import operation_instructions


class CliTests(unittest.TestCase):
    def test_allow_app_collects_valid_bundles(self):
        args = build_parser().parse_args(["run", "Open Settings", "--wda-url", "http://x:8100",
                                          "--allow-app", "com.apple.Preferences",
                                          "--allow-app", "com.apple.mobilenotes"])
        self.assertEqual(args.allow_app, ["com.apple.Preferences", "com.apple.mobilenotes"])

    def test_allow_app_defaults_to_none(self):
        args = build_parser().parse_args(["run", "Tap", "--wda-url", "http://x:8100"])
        self.assertIsNone(args.allow_app)

    def test_allow_app_rejects_malformed_bundles_at_parse_time(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["run", "Tap", "--wda-url", "http://x:8100",
                                       "--allow-app", "not-a-bundle"])


class InstructionTests(unittest.TestCase):
    def test_home_screen_wayfinding_is_taught(self):
        navigation = operation_instructions("Open Settings")["navigation"]
        self.assertIn("Page X of Y", navigation)
        self.assertIn("Spotlight", navigation)


if __name__ == "__main__":
    unittest.main()

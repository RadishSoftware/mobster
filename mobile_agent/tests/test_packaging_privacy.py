"""The public repository's privacy fixes stay fixed: a generic architecture in the setup fixture, and
step titles that capitalize only the everyday acronyms. The strings the public scan blocks appear nowhere
in this file, so the test itself passes that scan."""

import unittest
from pathlib import Path

from mobile_agent import fast_proof

TESTS = Path(__file__).resolve().parent


class PrivacyFixTests(unittest.TestCase):
    def test_the_developer_mode_fixture_names_a_generic_architecture(self):
        text = (TESTS / "setup_errors" / "developer_mode.txt").read_text()
        self.assertIn("arch:arm64,", text)
        self.assertNotRegex(text, r"arch:arm64[a-z]")

    def test_step_titles_capitalize_exactly_the_everyday_acronyms(self):
        self.assertEqual(set(fast_proof.ACRONYMS),
                         {"ios", "url", "id", "ip", "wifi", "gps", "usd", "api", "pdf", "eta", "sku", "os"})


if __name__ == "__main__":
    unittest.main()

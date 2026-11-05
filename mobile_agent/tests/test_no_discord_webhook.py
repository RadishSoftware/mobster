import importlib.util
import io
import unittest
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_no_webhook.py"
HOOK = ("https://" + "discord.com" + "/api/" + "webhooks/100000000000000004/" + "k" * 68).encode()


def load():
    spec = importlib.util.spec_from_file_location("check_no_webhook_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(SCRIPT.exists(), "needs scripts/check_no_webhook.py (the private tree's)")
class NoWebhookTests(unittest.TestCase):
    def setUp(self):
        self.check = load()

    def test_no_tracked_file_or_build_holds_a_discord_webhook_url(self):
        found, files = self.check.scan_repo(ROOT)
        self.assertEqual(found, [])
        self.assertGreater(files, 500)

    def test_it_finds_one_anywhere_a_build_could_hide_it(self):
        finder = self.check.Finder()
        finder.blob(b"const a='" + HOOK + b"'", "dist/index.js")
        finder.blob(b"x = '" + HOOK.replace(b"/", b"\\/") + b"'", "dist/escaped.js")
        finder.blob(b"see " + HOOK.replace(b"/api/", b"/api/v10/").upper(), "notes.txt")
        member = io.BytesIO()
        with zipfile.ZipFile(member, "w") as archive:
            archive.writestr("mobile_agent/x.pyc", b"\0\0" + HOOK)
        finder.blob(member.getvalue(), "Resources/base_library.zip")
        finder.blob(b"\xcf\xfa\xed\xfe\0\0\0\0" + b"junk" * 50 + zlib.compress(b"module " + HOOK) + b"tail",
                    "MacOS/mobster-agent")
        places = {line.split(": ")[0] for line in finder.found}
        self.assertTrue({"dist/index.js", "dist/escaped.js", "notes.txt",
                         "Resources/base_library.zip!mobile_agent/x.pyc"} <= places, places)
        self.assertTrue(any(place.startswith("MacOS/mobster-agent@zlib:") for place in places), places)
        for line in finder.found:
            self.assertNotIn(HOOK.decode(), line)
        self.assertEqual(self.check.Finder().found, [])

    def test_a_folder_build_is_walked(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "assets").mkdir()
            (Path(directory) / "assets" / "app.js").write_bytes(b"ok")
            self.assertEqual(self.check.main([directory]), 0)
            (Path(directory) / "assets" / "chunk.js").write_bytes(HOOK)
            self.assertEqual(self.check.main([directory]), 1)


@unittest.skipUnless((ROOT / "scripts" / "secret_patterns.py").exists(), "needs scripts/secret_patterns.py")
class SecretScanTests(unittest.TestCase):

    def setUp(self):
        spec = importlib.util.spec_from_file_location("secret_patterns_under_test",
                                                      ROOT / "scripts" / "secret_patterns.py")
        self.tree = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tree)

    def test_the_patterns_name_a_discord_webhook(self):
        match = self.tree.SECRET_ANY.search("POST " + HOOK.decode())
        self.assertIsNotNone(match)
        self.assertEqual(self.tree.SECRETS[int(match.lastgroup[1:])][0], "a Discord webhook URL")

    def test_the_value_in_an_env_file_is_a_fingerprinted_secret(self):
        import os
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "discord.env"
            env.write_text(f"DISCORD_FEEDBACK_WEBHOOK_URL={HOOK.decode()}\nHOMEPAGE_URL=https://mobster.dev/a-long-public-page\n")
            with mock.patch.dict(os.environ, {"MOBSTER_SECRET_FILES": str(env)}):
                values = self.tree.secret_values(None)
        names = {name for name, _value, _source in values}
        self.assertIn("DISCORD_FEEDBACK_WEBHOOK_URL", names)
        self.assertNotIn("HOMEPAGE_URL", names)
        self.assertIn(HOOK, {value for _name, value, _source in values})


if __name__ == "__main__":
    unittest.main()

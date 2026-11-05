"""The dev window script's --gate and fresh options: the refusals and the data they set aside,
with no window started."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "desktop" / "scripts" / "dev-window.sh"


def origin_bytes(port: int) -> bytes:
    """A WebKit origin file: scheme, host, then the port as two little-endian bytes."""
    return b"\x04\x00\x00\x00\x01http\x09\x00\x00\x00\x01127.0.0.1\x01" + port.to_bytes(2, "little")


@unittest.skipUnless(SCRIPT.is_file(), "the app development script is not part of this repository")
class DevWindowScript(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.home.name)
        (self.root / "worktree").mkdir()

    def run_script(self, *args, **env):
        base = {"PATH": os.environ["PATH"], "HOME": str(self.root), "MOBSTER_WORK": str(self.root / "work"),
                "DEV_WINDOW_SLOT": "2"}
        base.update(env)
        return subprocess.run(["/bin/bash", str(SCRIPT), *args], env=base, capture_output=True, text=True, timeout=60)

    def test_gate_on_refuses_without_a_local_account_stack(self):
        result = self.run_script("start", str(self.root / "worktree"), "--gate", "on")
        self.assertEqual(result.returncode, 1)
        self.assertIn("MOBSTER_LICENSE_API", result.stderr)
        self.assertIn("unset", result.stderr)
        self.assertFalse((self.root / "work" / "dev-window-2" / "pid").exists())

    def test_gate_on_refuses_the_real_account_servers(self):
        result = self.run_script("start", str(self.root / "worktree"), "--gate", "on",
                                 MOBSTER_LICENSE_API="http://127.0.0.1:8793",
                                 MOBSTER_SUPABASE_URL="https://wwljtuapjuwfzzakujav.supabase.co")
        self.assertEqual(result.returncode, 1)
        self.assertIn("MOBSTER_SUPABASE_URL", result.stderr)
        self.assertIn("local server", result.stderr)

    def test_gate_env_gives_the_agent_the_same_local_license_server(self):
        # The agent trusts the local signer only beside MOBSTER_ACCOUNT_API on loopback (plan_gate.trusted_keys):
        # without it a gated slot refused every task its trial licenses (Codex QA pass 04, P04-6).
        result = self.run_script("gate-env", MOBSTER_LICENSE_API="http://127.0.0.1:8796",
                                 MOBSTER_SUPABASE_URL="http://127.0.0.1:54397", MOBSTER_LICENSE_PUBLIC_KEY="pk")
        self.assertEqual(result.returncode, 0, result.stderr)
        env = dict(line.split("=", 1) for line in result.stdout.split())
        self.assertEqual(env["MOBSTER_ACCOUNT_API"], "http://127.0.0.1:8796")
        self.assertEqual(env["MOBSTER_LICENSE_API"], env["MOBSTER_ACCOUNT_API"])
        self.assertEqual(env["MOBSTER_LICENSE_PUBLIC_KEY"], "pk")

    def test_gate_env_refuses_the_real_account_servers_too(self):
        result = self.run_script("gate-env", MOBSTER_LICENSE_API="https://api.mobster.dev",
                                 MOBSTER_SUPABASE_URL="http://127.0.0.1:54397")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("MOBSTER_ACCOUNT_API", result.stdout)

    def test_gate_value_other_than_on_or_off_is_a_usage_error(self):
        self.assertEqual(self.run_script("start", str(self.root / "worktree"), "--gate", "maybe").returncode, 2)

    def test_fresh_sets_aside_this_slots_data_and_origin_only(self):
        data = self.root / "Library" / "Application Support" / "app.mobster.desktop.dev2"
        other = self.root / "Library" / "Application Support" / "app.mobster.desktop.dev1"
        for folder in (data, other):
            folder.mkdir(parents=True)
            (folder / "state.json").write_text("{}")
        webkit = self.root / "Library" / "WebKit" / "mobster-desktop" / "WebsiteData" / "Default"
        for name, port in (("mine", 5175), ("theirs", 5174)):
            origin = webkit / name / name / "origin"
            origin.parent.mkdir(parents=True)
            origin.write_bytes(origin_bytes(port))
        result = self.run_script("fresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(data.exists())
        self.assertTrue(other.exists())
        self.assertFalse((webkit / "mine").exists())
        self.assertTrue((webkit / "theirs").exists())
        aside = list((self.root / "work" / "dev-window-2").glob("fresh-*"))
        self.assertEqual(len(aside), 1)
        self.assertTrue((aside[0] / "app-data" / "state.json").exists())
        self.assertTrue((aside[0] / "webkit-mine").exists())

    def test_two_digit_slots_run_up_to_the_cap(self):
        result = self.run_script("fresh", DEV_WINDOW_SLOT="12")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_script("fresh", DEV_WINDOW_SLOT="20").returncode, 2)
        self.assertEqual(self.run_script("fresh", DEV_WINDOW_SLOT="20", DEV_WINDOW_SLOTS="30").returncode, 0)

    def test_a_slot_with_a_leading_zero_or_a_bad_cap_is_refused(self):
        for env in ({"DEV_WINDOW_SLOT": "08"}, {"DEV_WINDOW_SLOT": "x"}, {"DEV_WINDOW_SLOTS": "0"},
                    {"DEV_WINDOW_SLOTS": "101"}):
            result = self.run_script("fresh", **env)
            self.assertEqual(result.returncode, 2, env)
            self.assertIn("dev-window:", result.stderr)

    def test_the_runner_starts_the_shell_from_inside_the_slots_bundle(self):
        shell = self.root / "target" / "debug" / "mobster-desktop"
        shell.parent.mkdir(parents=True)
        shell.write_text('#!/bin/bash\nprintf "%s %s" "$0" "$*"\n')
        shell.chmod(0o755)
        bundle = self.root / "work" / "dev-window-2" / "Mobster Dev 2.app"
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT.with_name("dev-window-bundle.sh")), str(shell), "--flag"],
            env={"PATH": os.environ["PATH"], "DEV_WINDOW_BUNDLE": str(bundle),
                 "DEV_WINDOW_BUNDLE_ID": "app.mobster.desktop.dev2"},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{bundle}/Contents/MacOS/mobster-desktop --flag")
        plist = (bundle / "Contents" / "Info.plist").read_text()
        self.assertIn("<string>app.mobster.desktop.dev2</string>", plist)
        self.assertIn("<string>Mobster Dev 2</string>", plist)
        self.assertNotIn("CFBundleShortVersionString", plist)

    def test_stop_removes_the_slots_bundle(self):
        bundle = self.root / "work" / "dev-window-2" / "Mobster Dev 2.app" / "Contents"
        bundle.mkdir(parents=True)
        result = self.run_script("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(bundle.parent.exists())

    def test_fresh_with_nothing_saved_leaves_no_folder(self):
        result = self.run_script("fresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list((self.root / "work" / "dev-window-2").glob("fresh-*")), [])


if __name__ == "__main__":
    unittest.main()

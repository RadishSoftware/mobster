"""The pinned WebDriverAgent release: the clone, the commit check, the docs. Offline."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent import device_manager as dm
from mobile_agent import wda_source

ROOT = Path(__file__).resolve().parents[2]


class PinTests(unittest.TestCase):
    def test_the_clone_takes_the_pinned_tag(self):
        command = wda_source.clone_command("/usr/bin/git", "/tmp/WebDriverAgent")
        self.assertEqual(command[command.index("--branch") + 1], wda_source.REF)
        self.assertIn("--depth", command)
        self.assertEqual(command[-2:], [wda_source.REPOSITORY, "/tmp/WebDriverAgent"])
        self.assertRegex(wda_source.REF, r"^v\d+\.\d+\.\d+$")
        self.assertRegex(wda_source.COMMIT, r"^[0-9a-f]{40}$")

    def test_the_docs_name_the_pinned_tag(self):
        docs = ["mobile_agent/docs/usb-wda.md"]
        if (ROOT / "desktop").is_dir():  # The desktop app's docs, where the app is checked out.
            docs += ["desktop/docs/device-setup.md", "desktop/docs/security-privacy.md"]
        for doc in docs:
            self.assertIn(wda_source.REF, (ROOT / doc).read_text(), doc)

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_the_commit_is_read_from_the_checkout(self):
        project = Path(tempfile.mkdtemp())
        git = shutil.which("git")
        self.assertIsNone(wda_source.checkout_commit(git, project))
        env = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
        subprocess.run([git, "-C", str(project), "init", "-q"], check=True)
        subprocess.run([git, "-C", str(project), *env, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        head = subprocess.run([git, "-C", str(project), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
        self.assertEqual(wda_source.checkout_commit(git, project), head)
        self.assertFalse(wda_source.is_pinned(git, project))
        with self.assertRaises(RuntimeError):
            wda_source.verify(git, project)


class BuildTests(unittest.TestCase):
    """_build clones the pinned tag, and replaces a checkout at any other commit."""

    def build(self, existing_commit=None):
        manager = dm.DeviceManager(tempfile.mkdtemp())
        if existing_commit:
            (manager.project / "WebDriverAgent.xcodeproj").mkdir(parents=True)
            (manager.project / "WebDriverAgent.xcodeproj" / "project.pbxproj").write_text("com.facebook.x")
            (manager.project / "stale").write_text("")
        commands, commits = [], {"now": existing_commit}

        def step(command, log, tail):
            commands.append(command)
            if "clone" in command:
                (manager.project / "WebDriverAgent.xcodeproj").mkdir(parents=True)
                (manager.project / "WebDriverAgent.xcodeproj" / "project.pbxproj").write_text("")
                commits["now"] = wda_source.COMMIT

        # The loopback source patches have their own tests (test_device_setup); these fake clones
        # hold no WDA sources to patch.
        with patch.object(manager, "_step", step), patch.object(dm, "tool", lambda name: f"/bin/{name}"), \
                patch.object(wda_source, "checkout_commit", lambda git, project: commits["now"]), \
                patch.object(dm, "patch_wda", lambda project: None):
            manager._build("ABCDE12345", "00008130-001A2B3C4D5E6F70")
        return manager, commands

    def test_a_fresh_build_clones_the_pinned_tag(self):
        manager, commands = self.build()
        self.assertEqual(manager.build["state"], "succeeded", manager.build)
        self.assertEqual(commands[0], wda_source.clone_command("/bin/git", manager.project))
        self.assertIn("build-for-testing", commands[1])

    def test_a_checkout_at_another_commit_is_replaced(self):
        manager, commands = self.build(existing_commit="0" * 40)
        self.assertEqual(manager.build["state"], "succeeded", manager.build)
        self.assertIn("clone", commands[0])
        self.assertFalse((manager.project / "stale").exists())

    def test_the_pinned_checkout_is_reused(self):
        manager, commands = self.build(existing_commit=wda_source.COMMIT)
        self.assertEqual(manager.build["state"], "succeeded", manager.build)
        self.assertEqual(len(commands), 1)
        self.assertTrue((manager.project / "stale").exists())


if __name__ == "__main__":
    unittest.main()

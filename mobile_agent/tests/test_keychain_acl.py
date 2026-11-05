import base64
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mobile_agent.tests  # noqa: F401 -- the suite's guard runs first
from mobile_agent import keychain_acl
from mobile_agent.tests.fake_keychain import FakeBridge, store

SECRET = base64.b64encode(b"secret-value-for-test").decode()


class StoreTests(unittest.TestCase):
    def test_a_new_item_gets_the_access_list_and_the_marker(self):
        s, bridge = store()
        s.set("acct:one", SECRET)
        item = bridge.items[("app.mobster.test", "acct:one")]
        self.assertTrue(item["marked"])
        self.assertEqual(item["trusted"],
                         ["<this program>", "/Applications/Mobster.app/Contents/MacOS/mobster-desktop"])
        self.assertNotIn("/usr/bin/security", item["trusted"])
        self.assertEqual(s.get("acct:one"), SECRET)
        self.assertEqual(bridge.cli, [], "a new item never goes through /usr/bin/security")

    def test_a_change_keeps_the_item_and_its_access_list(self):
        s, bridge = store()
        s.set("acct:one", SECRET)
        s.set("acct:one", "bmV3")
        self.assertEqual([c[0] for c in bridge.calls if c[0] in ("add", "update")], ["update", "add", "update"])
        self.assertEqual(s.get("acct:one"), "bmV3")

    def test_an_older_item_moves_to_the_access_list_when_read(self):
        s, bridge = store()
        bridge.legacy("app.mobster.test", "acct:old", SECRET)
        with self.assertLogs(keychain_acl.log, logging.INFO) as logs:
            self.assertEqual(s.get("acct:old"), SECRET)
        item = bridge.items[("app.mobster.test", "acct:old")]
        self.assertTrue(item["marked"])
        self.assertNotIn("/usr/bin/security", item["trusted"])
        self.assertEqual([argv[1] for argv, _ in bridge.cli], ["find-generic-password", "delete-generic-password"])
        self.assertNotIn(SECRET, "\n".join(logs.output))
        bridge.cli.clear()
        self.assertEqual(s.get("acct:old"), SECRET)
        self.assertEqual(bridge.cli, [], "a moved item is read through the framework")

    def test_a_failed_move_puts_the_older_item_back(self):
        s, bridge = store()
        bridge.legacy("app.mobster.test", "acct:old", SECRET)
        bridge.fail.add("add")
        with self.assertLogs(keychain_acl.log, logging.WARNING) as logs:
            self.assertEqual(s.get("acct:old"), SECRET, "the secret is still there this time")
        item = bridge.items[("app.mobster.test", "acct:old")]
        self.assertEqual((item["marked"], item["data"]), (False, SECRET.encode()))
        self.assertNotIn(SECRET, "\n".join(logs.output))
        restore = [(argv, data) for argv, data in bridge.cli if argv[1:2] == ["-i"]]
        self.assertEqual(len(restore), 1)
        self.assertNotIn(SECRET, " ".join(restore[0][0]), "the secret goes on stdin, never in argv")
        bridge.fail.clear()
        self.assertEqual(s.get("acct:old"), SECRET)
        self.assertTrue(bridge.items[("app.mobster.test", "acct:old")]["marked"], "the next read moves it")

    def test_a_refused_write_raises_and_never_falls_back(self):
        s, bridge = store()
        bridge.refuse.add("add")
        with self.assertRaises(keychain_acl.KeychainError) as caught:
            s.set("acct:one", SECRET)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(bridge.items, {})
        self.assertEqual(bridge.cli, [], "no /usr/bin/security, no file")

    def test_a_refused_write_over_an_older_item_leaves_it_as_it_was(self):
        s, bridge = store()
        bridge.legacy("app.mobster.test", "acct:old", SECRET)
        bridge.fail.add("add")
        with self.assertRaises(keychain_acl.KeychainError):
            s.set("acct:old", "bmV3")
        item = bridge.items[("app.mobster.test", "acct:old")]
        self.assertEqual((item["marked"], item["data"]), (False, SECRET.encode()))

    def test_a_refused_read_is_an_error_not_a_prompt_or_nothing(self):
        s, bridge = store()
        s.set("acct:one", SECRET)
        bridge.refuse.add("find")
        with self.assertRaises(keychain_acl.KeychainError):
            s.get("acct:one")
        self.assertEqual([argv for argv, _ in bridge.cli], [], "never asks /usr/bin/security for an item it can't read")

    def test_no_item_is_none_and_delete_removes_both_kinds(self):
        s, bridge = store()
        self.assertIsNone(s.get("acct:none"))
        s.set("acct:one", SECRET)
        bridge.legacy("app.mobster.test", "acct:old", SECRET)
        s.delete("acct:one")
        s.delete("acct:old")
        s.delete("acct:none")
        self.assertEqual(bridge.items, {})

    def test_names_are_plain_words(self):
        s, _ = store()
        for account in ('a"b', "a\nb", ""):
            with self.subTest(account=account), self.assertRaises(ValueError):
                s.set(account, SECRET)

    def test_the_app_s_programs_are_trusted_from_its_bundle_else_applications(self):
        root = Path(tempfile.mkdtemp(prefix="mobster-acl-")).resolve()
        self.addCleanup(__import__("shutil").rmtree, root, True)
        macos = root / "Mobster.app" / "Contents" / "MacOS"
        macos.mkdir(parents=True)
        for name in ("mobster-desktop", "mobster-agent"):
            (macos / name).write_text("")
        self.assertEqual(keychain_acl.trusted_programs(macos / "mobster-agent", app=root / "none.app"),
                         [str(macos / "mobster-desktop")])
        self.assertEqual(keychain_acl.trusted_programs(root / "bin" / "python3", app=root / "Mobster.app"),
                         [str(macos / "mobster-desktop"), str(macos / "mobster-agent")])
        self.assertEqual(keychain_acl.trusted_programs(root / "bin" / "python3", app=root / "none.app"), [])

    def test_the_mac_app_s_agent_never_prompts(self):
        from mobile_agent import serve as serve_module
        bridge = FakeBridge()
        import io
        with mock.patch.object(keychain_acl, "system_bridge", lambda: bridge), \
                mock.patch.dict("os.environ", {serve_module.SECRETS_ON_STDIN: "1"}):
            serve_module.launch_secrets(io.StringIO("a" * 32 + " " + "b" * 32 + "\n"), wait=0)
        self.assertFalse(bridge.prompts_allowed)




if __name__ == "__main__":
    unittest.main()

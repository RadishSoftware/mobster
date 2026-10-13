"""Checkpoints of simulator app data: save, restore and digest, on fake containers. No simulator, model or network."""

import pathlib
import tempfile
import unittest
from unittest import mock

from mobile_agent.bench.evals import checkpoint as C


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.apps = {"alpha": "com.example.alpha", "beta": "com.example.beta"}
        self.containers = {b: root / "sim" / b for b in self.apps.values()}
        for c in self.containers.values():
            (c / "Documents").mkdir(parents=True)
            (c / "Library" / "Preferences").mkdir(parents=True)
        (self.containers["com.example.alpha"] / "Documents" / "state.json").write_text('{"notes": 1}')
        (self.containers["com.example.beta"] / "Library" / "Preferences" / "b.plist").write_text("x")
        self.group = root / "sim" / "group"
        self.group.mkdir()
        (self.group / "shared.db").write_text("g")
        self.root = root
        patches = [mock.patch.object(C, "app_container", lambda udid, bundle: self.containers.get(bundle)),
                   mock.patch.object(C, "group_containers", lambda udid, groups=C.SHARED_GROUPS:
                                     {"group.example": self.group}),
                   mock.patch.object(C, "terminate", lambda udid, bundles: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_restore_puts_back_exactly_what_was_saved(self):
        meta = C.save("U", self.apps, self.root / "cp" / "seed")
        state = self.containers["com.example.alpha"] / "Documents" / "state.json"
        state.write_text('{"notes": 2}')
        (self.containers["com.example.beta"] / "Documents" / "new.txt").write_text("added")
        (self.group / "shared.db").write_text("changed")
        self.assertNotEqual(C.digest("U", self.apps), meta["digest"])
        seconds, ok = C.restore("U", self.apps, self.root / "cp" / "seed")
        self.assertTrue(ok)
        self.assertEqual(state.read_text(), '{"notes": 1}')
        self.assertFalse((self.containers["com.example.beta"] / "Documents" / "new.txt").exists())
        self.assertEqual((self.group / "shared.db").read_text(), "g")

    def test_a_checkpoint_is_never_overwritten(self):
        C.save("U", self.apps, self.root / "cp" / "seed")
        with self.assertRaises(FileExistsError):
            C.save("U", self.apps, self.root / "cp" / "seed")

    def test_only_simulators_with_an_allowed_name_are_touched(self):
        with mock.patch.object(C, "device_name", return_value="iOSWorld Mobster 2"):
            with self.assertRaises(SystemExit):
                C.guard("U")
        with mock.patch.object(C, "device_name", return_value="Eval Lab 1"):
            self.assertEqual(C.guard("U"), "Eval Lab 1")


if __name__ == "__main__":
    unittest.main()

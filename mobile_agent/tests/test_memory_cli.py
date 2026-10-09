"""Track memory: `mobster memory` (SPEC §3.3 CLI). list, add, rm, pin, unpin, clear, export and routines, with
their exit codes. Offline: a scratch memory folder per test."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import __main__ as cli
from mobile_agent.extensions import Hooks
from mobile_agent.memory.store import MemoryStore


def main(argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(cli, "load_extensions", return_value=Hooks()), redirect_stdout(out), \
            redirect_stderr(err), mock.patch("sys.stdin", stdin or io.StringIO("")):
        try:
            code = cli.main(argv)
        except SystemExit as exit_:
            code = exit_.code
    return code, out.getvalue(), err.getvalue()


class Tty(io.StringIO):
    def isatty(self):
        return True


class CliTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mobster-memory-")) / "memory"
        env = mock.patch.dict(os.environ, {"MOBSTER_MEMORY_DIR": str(self.folder)})
        env.start()
        self.addCleanup(env.stop)
        self.store = MemoryStore(self.folder)

    def test_list_when_empty_shows_how_to_add(self):
        code, out, err = main(["memory"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn('mobster memory add "My gym is the one on 5th Street"', out)
        self.assertFalse(self.folder.exists())

    def test_add_list_pin_rm(self):
        code, out, _ = main(["memory", "add", "My gym is the one on 5th Street"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Mobster will remember: My gym is the one on 5th Street"))
        code, out, _ = main(["memory", "add", "Sign my texts with – Sam", "--app", "com.apple.MobileSMS", "--pin",
                             "--json"])
        texts = json.loads(out)["fact"]
        self.assertEqual((code, texts["scope"], texts["pinned"]), (0, "app:com.apple.MobileSMS", True))
        code, out, _ = main(["memory", "list"])
        lines = out.splitlines()
        self.assertEqual(lines[0], f"{texts['id']}  In Messages: Sign my texts with – Sam  (pinned)")
        self.assertTrue(lines[1].endswith("My gym is the one on 5th Street"))
        self.assertIn("2 things.", out)
        code, out, _ = main(["memory", "list", "--app", "com.apple.MobileSMS", "--json"])
        self.assertEqual([f["id"] for f in json.loads(out)["facts"]], [texts["id"]])
        code, out, _ = main(["memory", "unpin", texts["id"]])
        self.assertEqual((code, out.strip()), (0, "Unpinned: Sign my texts with – Sam"))
        code, out, _ = main(["memory", "pin", texts["id"]])
        self.assertEqual((code, out.strip()), (0, "Pinned: Sign my texts with – Sam"))
        code, out, _ = main(["memory", "rm", texts["id"]])
        self.assertEqual((code, out.strip()), (0, "Forgot: Sign my texts with – Sam"))
        code, _, err = main(["memory", "rm", texts["id"]])
        self.assertEqual((code, err.strip()), (1, "mobster memory: Mobster doesn't remember that anymore."))
        self.assertEqual(self.store.count(), 1)

    def test_refusals_exit_1_with_the_sentence(self):
        code, out, err = main(["memory", "add", "My PIN is 4821"])
        self.assertEqual((code, out, err.strip()),
                         (1, "", "mobster memory: Mobster doesn't remember passwords, codes or card numbers."))
        code, _, err = main(["memory", "add", "x" * 301])
        self.assertEqual((code, err.strip()), (1, "mobster memory: Keep it to 300 characters."))
        self.assertFalse(self.folder.exists())

    def test_clear_asks_first(self):
        self.store.add_fact("Kate Bell is my sister")
        code, _, err = main(["memory", "clear"])
        self.assertEqual(code, 2)                                   # no terminal to ask on
        self.assertIn("--yes", err)
        code, out, _ = main(["memory", "clear"], stdin=Tty("no\n"))
        self.assertEqual((code, self.store.count()), (1, 1))
        self.assertIn("Nothing was deleted.", out)
        code, out, _ = main(["memory", "clear"], stdin=Tty("delete everything\n"))
        self.assertEqual((code, self.store.count()), (0, 0))
        self.assertIn("Deleted everything Mobster remembered (1 thing).", out)
        self.store.add_fact("Kate Bell is my sister")
        self.assertEqual(main(["memory", "clear", "--yes"])[0], 0)
        self.assertEqual(self.store.count(), 0)

    def test_export(self):
        self.store.add_fact("Kate Bell is my sister")
        code, out, _ = main(["memory", "export"])
        self.assertEqual(code, 0)
        self.assertIn("# What Mobster remembers", out)
        target = self.folder.parent / "out.md"
        code, out, _ = main(["memory", "export", "--output", str(target)])
        self.assertEqual(code, 0)
        self.assertIn("- Kate Bell is my sister", target.read_text())

    def test_routines_are_not_in_this_build_yet(self):
        code, out, err = main(["memory", "routines", "list"])
        self.assertEqual((code, out, err.strip()), (3, "", "mobster memory: Routines aren't in this build yet."))

    def test_no_home_folder_exits_3(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            code, _, err = main(["memory", "list"])
        self.assertEqual(code, 3)
        self.assertIn("there's no home folder", err)

    def test_app_before_the_command_is_still_the_terminal_uis(self):
        code, _, err = main(["--app", "com.apple.MobileSMS", "memory", "list"])
        self.assertEqual(code, 2)
        self.assertIn("--app is an option of the terminal UI", err)


if __name__ == "__main__":
    unittest.main()

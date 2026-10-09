"""Seam S9: the new commands still stubs say so and exit 3 (`mobster test`, `chat` and `memory` are their tracks' now,
each with its own tests); `mobster --help` lists chat and test with the everyday commands; `run --long` isn't in this
build yet. Offline."""

from contextlib import redirect_stderr, redirect_stdout
import inspect
import io
import os
import unittest
from unittest import mock

from mobile_agent import __main__ as cli
from mobile_agent import devtools
from mobile_agent.extensions import Hooks
from mobile_agent.tests.test_cli_surface import help_text


def main(argv):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(cli, "load_extensions", return_value=Hooks()), redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def still_a_stub(name):
    """Whether `mobster <name>` is still the seams' stub: once its track lands, the track's own tests cover it."""
    return "isn't in this build yet" in inspect.getsource(devtools.load(name))


class StubCommandTests(unittest.TestCase):
    def test_each_new_command_says_it_is_not_in_this_build_and_exits_3(self):
        for name, argv in (("chat", ["chat", "What's on my calendar tomorrow?"]), ("memory", ["memory", "list"]),
                           ("wifi", ["wifi", "status"])):
            if not still_a_stub(name):
                continue
            with self.subTest(command=name):
                code, out, err = main(argv)
                self.assertEqual(code, 3)
                self.assertEqual(err.strip(), f"`mobster {name}` isn't in this build yet.")
                self.assertEqual(out, "")

    def test_the_commands_are_registered_with_fixed_help(self):
        for name in ("chat", "memory", "test", "wifi"):
            self.assertIn(name, devtools.COMMANDS)
            self.assertIn(name, devtools.PACKAGES)
            self.assertIsNotNone(devtools.load(name))
        self.assertEqual(devtools.TEXT["phone"][0], "set your iPhone's clipboard, put files on it, or install a build")
        self.assertIn("mobster phone file put menu.pdf --app com.apple.Pages", devtools.TEXT["phone"][2])

    def test_help_lists_chat_and_test_with_the_everyday_commands(self):
        text = help_text("")
        tasks = text.split("Your tasks\n", 1)[1].split("\n\n", 1)[0]
        testing = text.split("Test your app\n", 1)[1].split("\n\n", 1)[0]
        self.assertRegex(testing, r"  test +run your app's checks")
        self.assertRegex(tasks, r"  chat +talk to Mobster's agent")
        self.assertIn("  memory", tasks)
        self.assertLess(testing.index("  test"), testing.index("  verify"))
        self.assertNotIn("wifi", text)  # behind its switch (MOBSTER_WIFI_TRANSPORT) until S20

    def test_run_long_is_not_in_this_build_yet(self):
        with mock.patch.dict(os.environ, {"MOBSTER_WDA_URL": ""}):
            code, _out, err = main(["run", "Clean up my inbox", "--long"])
        self.assertEqual(code, 3)
        self.assertIn("Long tasks aren't in this build yet.", err)
        self.assertNotIn("--long", help_text("run"))  # hidden until the harness track lands


if __name__ == "__main__":
    unittest.main()

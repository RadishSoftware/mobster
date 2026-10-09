"""`mobster sim` / `python -m mobile_agent.sim`: the parser, exit codes, output, and its section of docs/cli.md."""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from mobile_agent.sim import SimError, cli
from mobile_agent.sim.__main__ import build_parser, main
from mobile_agent.tests.test_sim_fakes import fake_manager

ROOT = Path(__file__).resolve().parents[2]
COMMANDS = {"list", "prepare", "shutdown", "erase", "delete", "prune", "doctor"}


def subcommands(parser):
    return next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))


def run_cli(argv, manager=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        if manager is None:
            code = main(argv)
        else:
            def build(progress=None, **kwargs):  # the CLI's own progress sink, on the prepared manager
                if not isinstance(manager, mock.Mock):
                    manager.progress = progress
                return manager
            before = manager.progress
            try:
                with mock.patch("mobile_agent.sim.api.SimulatorManager", side_effect=build):
                    code = main(argv)
            finally:  # the test's own calls afterwards stay quiet
                if not isinstance(manager, mock.Mock):
                    manager.progress = before
    return code, out.getvalue(), err.getvalue()


class ParserTests(unittest.TestCase):
    def test_exactly_two_functions(self):
        public = {name for name in vars(cli) if callable(getattr(cli, name)) and not name.startswith("_")
                  and getattr(getattr(cli, name), "__module__", "") == cli.__name__}
        self.assertEqual(public, {"add_arguments", "run"})

    def test_subcommands_under_sim_command(self):
        action = subcommands(build_parser())
        self.assertEqual(action.dest, "sim_command")
        self.assertEqual(set(action.choices), COMMANDS)

    def test_every_option_has_help_in_the_house_style(self):
        """The top-level parser's own actions (the <command> positional too, which `mobster --help`'s surface
        test walks), each subcommand's entry in the list, and every option of every subcommand."""
        parser = build_parser()
        actions = [("sim", action) for action in parser._actions]
        actions += [(f"sim {choice.dest}", choice) for choice in subcommands(parser)._choices_actions]
        actions += [(f"sim {name}", action) for name, sub in subcommands(parser).choices.items()
                    for action in sub._actions]
        self.assertEqual(len([name for name, action in actions if action.dest == "sim_command"]), 1)
        for name, action in actions:
            if action.dest == "help":
                continue
            with self.subTest(command=name, option=action.dest):
                self.assertTrue(action.help, f"mobster {name} {action.dest} has no help")
                self.assertEqual(action.help[0], action.help[0].lower())
                self.assertFalse(action.help.endswith("."))

    def test_usage_errors_exit_2(self):
        for argv in ([], ["delete"], ["delete", "X", "--all"], ["erase"], ["nope"], ["list", "--bogus"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as caught, \
                    contextlib.redirect_stderr(io.StringIO()):
                main(argv)
            self.assertEqual(caught.exception.code, 2)

    def test_parsed_values(self):
        parser = build_parser()
        args = parser.parse_args(["prepare", "--device", "iPhone 16 Pro", "--runtime", "iOS 26.4", "--json"])
        self.assertEqual((args.sim_command, args.device, args.runtime, args.json),
                         ("prepare", "iPhone 16 Pro", "iOS 26.4", True))
        self.assertTrue(parser.parse_args(["delete", "--all"]).all)
        self.assertEqual(parser.parse_args(["delete", "ABC"]).udid, "ABC")
        self.assertIsNone(parser.parse_args(["shutdown"]).udid)
        self.assertTrue(parser.parse_args(["doctor", "--fix"]).fix)


class RunTests(unittest.TestCase):
    def test_list_json_and_table(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            code, out, _ = run_cli(["list", "--json"], manager)
            self.assertEqual((code, json.loads(out)), (0, {"simulators": []}))
            code, out, _ = run_cli(["list"], manager)
            self.assertIn("No Mobster simulators yet", out)
            manager.acquire().release()
            code, out, _ = run_cli(["list"], manager)
            self.assertIn("Mobster · iPhone 17 Pro · iOS 26.4  Booted  127.0.0.1:8310", out)

    def test_prepare_prints_each_phase_and_releases(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            code, out, err = run_cli(["prepare"], manager)
            self.assertEqual(code, 0)
            self.assertIn("Ready  Mobster · iPhone 17 Pro · iOS 26.4", out)
            for phase in ("create", "boot", "settings", "WebDriverAgent build", "WebDriverAgent start"):
                self.assertIn(phase, out)
            self.assertIn("Creating simulator", err)
            self.assertEqual(leases.held, set())
            code, out, _ = run_cli(["prepare", "--json"], manager)
            data = json.loads(out)
            self.assertEqual(data["wda_url"], "http://127.0.0.1:8310")
            self.assertIn("total", data["timing"])

    def test_a_sim_error_exits_1_with_the_fix(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            code, out, err = run_cli(["delete", "0A1B2C3D-0000-4000-8000-00000000FFFF"], manager)
            self.assertEqual(code, 1)
            self.assertIn("isn't one of Mobster's simulators", err)
            self.assertIn("mobster sim list", err)
            code, out, err = run_cli(["prepare", "--device", "iPhone 99", "--json"], manager)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["error"]["kind"], "environment")

    def test_delete_all_and_prune(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            code, out, _ = run_cli(["delete", "--all"], manager)
            self.assertEqual((code, out.strip()), (0, "No Mobster simulators to delete."))
            manager.acquire().release()
            code, _, err = run_cli(["delete", "--all"], manager)
            self.assertEqual(code, 0)
            self.assertIn("Deleted Mobster · iPhone 17 Pro · iOS 26.4", err)
            with mock.patch("mobile_agent.sim.api.installed_xcode_builds", return_value=set()):
                code, out, _ = run_cli(["prune", "--json"], manager)
            self.assertEqual(json.loads(out), {"simulators": [], "entries": [], "wda_builds": []})

    def test_doctor_json_exit_code(self):
        manager = mock.Mock()
        manager.doctor.return_value = [{"key": "xcode", "label": "Xcode", "state": "fail", "detail": "not installed",
                                        "fix": "Install Xcode"}]
        code, out, _ = run_cli(["doctor", "--json"], manager)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"ok": False, "checks": manager.doctor.return_value})
        manager.doctor.return_value[0]["state"] = "ok"
        code, out, _ = run_cli(["doctor"], manager)
        self.assertEqual(code, 0)
        self.assertIn("Ready for `mobster verify`.", out)
        manager.doctor.assert_called_with(fix=False)

    def test_interrupt_and_unexpected_errors_never_raise(self):
        manager = mock.Mock()
        manager.list.side_effect = KeyboardInterrupt
        self.assertEqual(run_cli(["list"], manager)[0], 130)
        manager.list.side_effect = RuntimeError("boom")
        code, _, err = run_cli(["list"], manager)
        self.assertEqual(code, 1)
        self.assertIn("boom", err)
        manager.list.side_effect = SimError("busy", "All are running checks.", "Wait.")
        code, _, err = run_cli(["list"], manager)
        self.assertEqual((code, err), (1, "mobster sim: All are running checks.\n  Wait.\n"))

    def test_python_dash_m(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {**os.environ, "MOBSTER_DATA_DIR": folder, "PYTHONPATH": str(ROOT)}
            done = subprocess.run([sys.executable, "-m", "mobile_agent.sim", "list", "--json"], capture_output=True,
                                  text=True, env=env, timeout=60, cwd=ROOT)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"simulators": []})


@unittest.skipUnless((ROOT / "docs" / "cli.md").is_file(), "docs are not part of this copy")
class DocsTests(unittest.TestCase):
    def test_the_section_names_every_subcommand_and_flag(self):
        text = (ROOT / "docs" / "cli.md").read_text()
        start = text.index("## `mobster sim`")
        self.assertLess(text.index("## `mobster doctor`"), start)
        self.assertLess(start, text.index("## `mobster history`"))
        section = text[start:text.index("\n## ", start + 1)]
        for name, sub in subcommands(build_parser()).choices.items():
            with self.subTest(command=name):
                self.assertIn(f"`mobster sim {name}", section)
            for action in sub._actions:
                for option in action.option_strings:
                    if option not in {"-h", "--help"}:
                        with self.subTest(command=name, option=option):
                            self.assertIn(f"`{option}", section)


class ProgressLinesTests(unittest.TestCase):
    def test_on_a_terminal_still_lines_redraw_in_place(self):
        import io
        from mobile_agent.devtools import ProgressLines

        class Tty(io.StringIO):
            def isatty(self):
                return True
        stream = Tty()
        with mock.patch.dict("os.environ", {"TERM": "xterm-256color"}):
            lines = ProgressLines(stream)
        lines("Building WebDriverAgent")
        lines("Still building WebDriverAgent (30 s so far)")
        lines("Still building WebDriverAgent (1 min 00 s so far)")
        lines("Simulator: X")
        lines("Still booting X (30 s so far)")
        lines.end()
        self.assertEqual(stream.getvalue(), "Building WebDriverAgent\n\r\033[KStill building WebDriverAgent (30 s so far)"
                         "\r\033[KStill building WebDriverAgent (1 min 00 s so far)\r\033[KSimulator: X\n"
                         "\r\033[KStill booting X (30 s so far)\r\033[K")

    def test_off_a_terminal_every_line_stays(self):
        import io
        from mobile_agent.devtools import ProgressLines
        stream = io.StringIO()
        lines = ProgressLines(stream)
        lines("Still building WebDriverAgent (30 s so far)")
        lines.end()
        self.assertEqual(stream.getvalue(), "Still building WebDriverAgent (30 s so far)\n")


if __name__ == "__main__":
    unittest.main()

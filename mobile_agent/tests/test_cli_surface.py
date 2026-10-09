"""The command surface: help text snapshots, exit codes, and plain output for scripts.

Help snapshots live in tests/snapshots/help/. After an intended change, rewrite them with
MOBSTER_UPDATE_SNAPSHOTS=1 python -m unittest mobile_agent.tests.test_cli_surface
and review the diff like any other change to user-facing text.
"""

from contextlib import redirect_stderr, redirect_stdout
import io
import re
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from mobile_agent import __main__ as cli
from mobile_agent import __version__
from mobile_agent.extensions import Hooks

SNAPSHOTS = Path(__file__).parent / "snapshots" / "help"
COMMANDS = ["", "verify", "devices", "run", "doctor", "history", "screen", "serve", "demo", "version", "tui"]


AGENT_FREE = {name: "" for name in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_MANAGED_BY_NPM",
                                     "OPENCODE", "CURSOR_AGENT", "GEMINI_CLI")}


def help_text(command):
    """``mobster [command] --help`` at 100 columns, without private extensions, as plain text."""
    out = io.StringIO()
    with mock.patch.dict(os.environ, {"COLUMNS": "100", "NO_COLOR": "1", "MOBSTER_WIFI_TRANSPORT": "",
                                      **AGENT_FREE}), \
            mock.patch.object(cli, "load_extensions", return_value=Hooks()), \
            redirect_stdout(out), mock.patch("sys.exit"), \
            mock.patch("argparse.ArgumentParser.exit", side_effect=SystemExit):
        try:
            cli.build_parser().parse_args(([command] if command else []) + ["--help"])
        except SystemExit:
            pass
    return out.getvalue()


@unittest.skipUnless(sys.version_info[:2] == (3, 12), "argparse's layout differs between Python versions")
class HelpSnapshotTests(unittest.TestCase):
    def test_help_matches_the_snapshots(self):
        update = os.environ.get("MOBSTER_UPDATE_SNAPSHOTS") == "1"
        for command in COMMANDS:
            with self.subTest(command=command or "mobster"):
                path = SNAPSHOTS / f"{command or 'mobster'}.txt"
                text = help_text(command)
                if update:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text)
                self.assertEqual(text, path.read_text(), f"help changed; see the module docstring to update {path.name}")


class HelpContentTests(unittest.TestCase):
    def test_the_top_level_help_leads_with_the_promise_and_groups_commands(self):
        from mobile_agent import help_topics
        text = help_text("")
        self.assertTrue(text.startswith("Do anything on your iPhone with agents.\n"), text[:80])
        for title, names in help_topics.GROUPS:
            self.assertIn(f"\n{title}\n", text)
            for name in names:
                self.assertRegex(text, rf"\n  {name} +{re.escape(help_topics.HELP[name])}\n")
        self.assertRegex(text, r"\n  Docs +https://docs.mobster.dev\n")
        # One description column for every section (the blind panel's consistency point).
        columns = {len(line) - len(line[2:].split("  ", 1)[1].lstrip()) for line in text.splitlines()
                   if line.startswith("  ") and not line.startswith("  $") and "  " in line[2:].rstrip()}
        self.assertEqual(len(columns), 1, columns)
        self.assertNotIn("github.com", text)

    def test_hidden_commands_stay_out_and_every_listed_command_exists(self):
        from mobile_agent import help_topics
        text = help_text("")
        with mock.patch.object(cli, "load_extensions", return_value=Hooks()):
            parser = cli.build_parser()
        listed = [name for _, names in help_topics.GROUPS for name in names] + list(help_topics.MORE)
        for name in listed:
            self.assertIn(name, parser.commands, f"mobster --help lists {name}, which isn't a command")
        for name in help_topics.HIDDEN:
            self.assertNotRegex(text, rf"(?m)^  {re.escape(name)}\b|, {re.escape(name)}\b",
                                f"mobster --help shows the hidden {name}")
        self.assertNotIn("routines", text)
        with mock.patch.dict(os.environ, {"MOBSTER_WIFI_TRANSPORT": "1"}):
            self.assertIn("wifi", help_topics.top_level(lambda text, *roles: text))

    def test_no_link_points_at_a_repository_path(self):
        root = Path(cli.__file__).parent
        for path in root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            text = path.read_text()
            self.assertNotIn("mobster-cli/tree/main/docs", text, path)
            self.assertNotIn("mobster-cli/blob/main/docs", text, path)

    def test_every_option_has_help(self):
        """Every option of every command, and of nested commands (`mobster sim list`), has help."""
        import argparse
        with mock.patch.object(cli, "load_extensions", return_value=Hooks()):
            parser = cli.build_parser()
        subparsers = next(a for a in parser._actions if a.dest == "command").choices
        pending = list(subparsers.items())
        while pending:
            name, sub = pending.pop(0)
            for action in sub._actions:
                if isinstance(action, argparse._SubParsersAction):
                    helps = {choice.dest: choice.help for choice in action._choices_actions}
                    for child, child_parser in action.choices.items():
                        if child not in helps:
                            continue  # hidden on purpose (help=SUPPRESS): a stub such as `memory routines`
                        self.assertTrue(helps.get(child), f"mobster {name} {child} has no help")
                        pending.append((f"{name} {child}", child_parser))
                    continue
                if action.dest in {"help"} or action.help is not None:
                    continue
                if name in {"decide-fixture"} and action.dest == "goal":
                    continue
                self.fail(f"mobster {name} {action.option_strings or action.dest} has no help")

    def test_run_needs_no_wda_url(self):
        args = cli.build_parser().parse_args(["run", "Open Settings"])
        self.assertIsNone(args.wda_url)
        with mock.patch.dict(os.environ, {"MOBSTER_WDA_URL": "http://127.0.0.1:8200/"}):
            self.assertEqual(cli.wda_url_for(args), "http://127.0.0.1:8200")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cli.wda_url_for(args), cli.DEFAULT_WDA_URL)

    def test_no_command_means_the_terminal_ui(self):
        args = cli.build_parser().parse_args(["--demo"])
        self.assertIsNone(args.command)
        self.assertTrue(args.demo)


@mock.patch.dict(os.environ, {"MOBSTER_ENV_FILE": os.devnull})
class OutputTests(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_version(self):
        self.assertEqual(self.run_main("version"), (0, f"mobster {__version__}\n", ""))

    def test_options_are_spelled_in_full_and_mistakes_suggest_the_right_one(self):
        """`--exec` silently meant --execute and `--al` --allow-app; `verfy` listed every command instead."""
        cases = [(["run", "x", "--exec"], 2, "error: unknown option --exec.", "tip: did you mean --execute?"),
                 (["run", "x", "--al", "com.apple.Preferences"], 2, "error: unknown option --al",
                  "tip: did you mean --allow-app?"),
                 (["run", "x", "--bogus"], 2, "error: unknown option --bogus.", "More: mobster run --help"),
                 (["verfy"], 2, "error: verfy isn't a command.", "tip: did you mean mobster verify?"),
                 (["sim", "prepar"], 2, "error: prepar isn't a command.",
                  "did you mean mobster sim prepare?"),
                 (["sim", "prepare", "--dev", "x"], 2, "error: unknown option --dev",
                  "did you mean --device?"),
                 (["verify", "--bundle", "com.x.y", "--tex", "Hi"], 3, "usage: mobster verify", "Did you mean --text?"),
                 (["history", "-n", "0"], 2, "error: argument -n/--limit", "at least 1 and at most 10000"),
                 (["history", "-n", "99999999999999999999"], 2, "error:", "at most 10000"),
                 (["serve", "--port", "99999"], 2, "error:", "at least 0 and at most 65535")]
        for argv, code, first, words in cases:
            with self.subTest(argv=argv):
                err = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(err), mock.patch.dict(os.environ,
                                                                                         {"NO_COLOR": "1"}):
                    try:
                        returned = cli.main(list(argv))
                    except SystemExit as raised:
                        returned = raised.code
                self.assertEqual(returned, code)
                self.assertTrue(err.getvalue().startswith(first), err.getvalue())
                self.assertIn(words, err.getvalue())

    def test_a_task_as_the_first_word(self):
        """`mobster "Turn on Dark Mode"` opens the terminal UI with the task; one bare word or several unquoted
        ones say how to run them as a task."""
        from mobile_agent import tui
        seen = {}
        with mock.patch.object(tui, "main", side_effect=lambda args: seen.update(task=args.task, demo=args.demo) or 0):
            self.assertEqual(self.run_main("--demo", "Turn on Dark Mode")[0], 0)
        self.assertEqual(seen, {"task": "Turn on Dark Mode", "demo": True})
        with mock.patch.object(tui, "main", side_effect=lambda args: seen.update(task=args.task) or 0):
            self.assertEqual(self.run_main("--", "calendar")[0], 0)
        self.assertEqual(seen["task"], "calendar")
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            code, out, err = self.run_main("calendar")
            self.assertEqual(code, 2)
            self.assertIn("calendar isn't a command.", err)
            self.assertIn("mobster -- calendar", err)
            code, out, err = self.run_main("turn", "on", "dark", "mode")
            self.assertEqual(code, 2)
            self.assertIn('mobster "turn on dark mode"', err)

    def test_a_task_refuses_a_pipe(self):
        with mock.patch("sys.stdin.isatty", return_value=False):
            code, out, err = self.run_main("Turn on Dark Mode")
        self.assertEqual(code, 2)
        self.assertIn('mobster run "TASK" --execute', err)

    def test_help_prints_a_commands_help(self):
        for argv, start in ((["help"], "Do anything on your iPhone with agents."),
                            (["help", "verify"], "usage: mobster verify"),
                            (["help", "sim", "prepare"], "usage: mobster sim prepare"),
                            (["help", "exit-codes"], "Exit codes"), (["help", "environment"], "Environment variables"),
                            (["help", "tui"], "The terminal UI"), (["help", "--all"], "Every command")):
            with self.subTest(argv=argv):
                code, out, err = self.run_main(*argv)
                self.assertEqual((code, err), (0, ""))
                self.assertTrue(out.startswith(start), out[:80])

    def test_a_missing_env_file_is_said_on_stderr(self):
        code, out, err = self.run_main("--env-file", "/nonexistent/mobster-test.env", "version")
        self.assertEqual((code, out), (0, f"mobster {__version__}\n"))
        self.assertEqual(err, "mobster: /nonexistent/mobster-test.env does not exist; no keys or settings were "
                              "loaded from it\n")
        code, out, _ = self.run_main("version", "--json")
        self.assertEqual(json.loads(out)["version"], __version__)

    def test_demo_stays_json_lines_when_piped(self):
        code, out, _ = self.run_main("demo")
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(lines[0], {"event": "start", "mode": "synthetic replay; no phone or model APIs"})
        self.assertTrue(all("event" in line for line in lines))
        self.assertEqual(lines[-1]["event"], "result")

    def test_run_without_a_phone_exits_3_with_a_json_error(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-0000"}):  # the key is checked first
            code, out, _ = self.run_main("run", "Open Settings", "--wda-url", "http://127.0.0.1:9")
        self.assertEqual(code, cli.EXIT_NO_DEVICE)
        error = json.loads(out)
        self.assertEqual(error["error_type"], "NoDevice")
        self.assertIn("mobster doctor", error["error"])

    def test_the_bare_command_refuses_a_pipe(self):
        with mock.patch("sys.stdin.isatty", return_value=False):
            code, out, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("interactive terminal", err)

    def test_doctor_json_reports_the_missing_phone_without_secrets(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "sk-secret-value"}):
            code, out, _ = self.run_main("doctor", "--simulator", "--json", "--wda-url", "http://127.0.0.1:9")
        self.assertEqual(code, 1)
        self.assertNotIn("sk-secret-value", out)
        report = json.loads(out)
        states = {check["key"]: check["state"] for check in report["checks"]}
        self.assertEqual(states["key"], "ok")
        self.assertEqual(states["wda"], "fail")
        self.assertNotIn("usb", states)

    def test_doctor_points_someone_with_no_phone_chosen_at_sim_doctor(self):
        """Review ux-2: with no keys `doctor` failed with 2 problems while `sim doctor` said ready for verify."""
        from mobile_agent import doctor
        failing = [doctor.Check("wda", "WebDriverAgent", doctor.FAIL, "not answering", doctor.start_wda_hint())]
        env = {key: value for key, value in os.environ.items() if key != "MOBSTER_WDA_URL"}
        with mock.patch.object(doctor, "run_checks", return_value=failing), mock.patch.dict(os.environ, env, clear=True):
            code, out, _ = self.run_main("doctor")
            self.assertEqual(code, 1)
            self.assertIn("run `mobster sim doctor`", out)
            self.assertIn("mobster sim prepare", out)
            code, out, _ = self.run_main("doctor", "--json")
            self.assertIn("mobster sim doctor", json.loads(out)["hint"])
            code, out, _ = self.run_main("doctor", "--wda-url", "http://127.0.0.1:9")
            self.assertNotIn("sim doctor", out)  # this one is about a phone

    def test_the_phone_hint_names_no_repository_paths(self):
        from mobile_agent import doctor, paths
        for checkout in (True, False):
            with mock.patch.object(paths, "source_checkout", return_value=checkout):
                hint = doctor.start_wda_hint()
                self.assertNotIn("usb-wda", hint)
                self.assertNotIn("docs/", hint)
                self.assertIn("mobster setup", hint)
                self.assertIn("Mobster for Mac", hint)

    def test_history_is_empty_without_a_journal(self):
        from mobile_agent.history import print_history
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(print_history(path=Path(folder) / "none.sqlite3", stream=out), 0)
        self.assertIn("No tasks yet", out.getvalue())


class ConsoleRendererTests(unittest.TestCase):
    def test_demo_events_read_as_steps(self):
        from mobile_agent.agent import Agent
        from mobile_agent.console import ConsoleRenderer
        from mobile_agent.demo import DemoDriver, DemoHelper, DemoModel
        out = io.StringIO()
        renderer = ConsoleRenderer(out, color=False, live=False)
        renderer.start("Search for coffee")
        Agent(DemoDriver(), DemoModel(), DemoHelper(), emit=renderer).run(
            "Search for coffee", execute=True, expected_text="Coffee brewing guide")
        renderer.close()
        text = out.getvalue()
        self.assertIn("❯ Search for coffee", text)
        self.assertIn("● Tapped “Search”", text)
        self.assertIn("⎿", text)
        self.assertIn("✓ Done", text)
        self.assertIn("Seen on screen: “coffee”", text)
        for hedge in ("unverified", "Nothing checked it", "does not prove"):
            self.assertNotIn(hedge, text)
        self.assertNotIn("\033[", text)
        self.assertNotIn("{", text)


class DoctorReportTests(unittest.TestCase):
    def test_plain_report_lists_fixes_under_problems(self):
        from mobile_agent import doctor
        checks = [doctor.Check("python", "Python", doctor.OK, "3.12.2"),
                  doctor.Check("wda", "Phone connection", doctor.FAIL, "nothing answers", "Start it\nOr this")]
        text = doctor.plain_report(checks)
        self.assertIn("✓ Python", text)
        self.assertIn("✗ Phone connection", text)
        self.assertIn("→ Start it\n", text)
        self.assertIn("→ Or this", text)
        self.assertIn("1 thing to fix before your first task.", text)
        self.assertIn("https://docs.mobster.dev/troubleshooting", text)

    def test_lines_wrap_at_the_terminal_width_with_a_hanging_indent(self):
        from mobile_agent import doctor
        long = "word " * 40
        checks = [doctor.Check("key", "Model key", doctor.FAIL, long.strip(), long.strip())]
        for width in (80, 120):
            text = doctor.plain_report(checks, width=width)
            for line in text.splitlines():
                self.assertLessEqual(len(line), width, line)
            fix_lines = [line for line in text.splitlines() if line.startswith(" " * 22)]
            self.assertTrue(fix_lines)

    def test_the_key_is_named_with_its_source_and_never_shown(self):
        from mobile_agent import config, doctor
        key = "sk-ant-api03-" + "x" * 40 + "a1F2"
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": key}, clear=False), \
                mock.patch.dict(config.SOURCES, {"ANTHROPIC_API_KEY": "mac-app"}):
            for name in ("OPENAI_API_KEY", "MOBSTER_SMART_MODEL", "MOBSTER_SMART_PROVIDER", "TEXT_MODEL_API_KEY"):
                os.environ.pop(name, None)
            check = doctor.check_key()
        self.assertEqual(check.state, doctor.OK)
        self.assertEqual(check.detail, "Claude (sk-ant-…a1F2) · from Mobster for Mac")
        self.assertNotIn("x" * 10, check.detail)

    def test_no_key_points_at_login(self):
        from mobile_agent import doctor
        env = {key: value for key, value in os.environ.items()
               if key not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TYPESAFE_API_KEY", "TEXT_MODEL_API_KEY",
                              "MOBSTER_SMART_MODEL", "MOBSTER_HELPER_PROVIDER", "TEXT_MODEL")}
        with mock.patch.dict(os.environ, env, clear=True):
            check = doctor.check_key()
        self.assertEqual((check.state, check.detail, check.fix), (doctor.FAIL, "none yet", "mobster login"))


class TerminalImageTests(unittest.TestCase):
    def test_protocol_detection(self):
        from mobile_agent.terminal_image import protocol
        self.assertEqual(protocol({"TERM": "xterm-kitty"}), "kitty")
        self.assertEqual(protocol({"TERM_PROGRAM": "ghostty"}), "kitty")
        self.assertEqual(protocol({"TERM_PROGRAM": "iTerm.app"}), "iterm")
        self.assertEqual(protocol({"TERM": "xterm-256color"}), "blocks")

    def test_encodings(self):
        from mobile_agent.terminal_image import blocks, iterm, kitty
        from mobile_agent.tui.demo_phone import SETTINGS, render_screen
        png = render_screen(SETTINGS, "top")
        self.assertTrue(kitty(png, 30).startswith("\033_Ga=T,f=100,c=30"))
        self.assertIn("\033]1337;File=inline=1", iterm(png, 30))
        drawing = blocks(png, 20)
        self.assertEqual(drawing.count("\n"), round(844 * 20 / 390 / 2))
        self.assertIn("▀", drawing)


if __name__ == "__main__":
    unittest.main()

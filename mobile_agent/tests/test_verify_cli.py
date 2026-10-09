"""`mobster verify` on the command line: flags, usage errors exiting 3, modes, output, --save, and the developer
command registration (devtools.py). Offline: the simulator manager and WDA are fakes."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mobile_agent import __main__ as mobster
from mobile_agent import devtools, engines
from mobile_agent.extensions import Hooks
from mobile_agent.tests.test_verify_fixtures import paywall
from mobile_agent.tests.test_verify_runner import BUNDLE, Clock, Driver, Manager
from mobile_agent.verify import cli, runner
from mobile_agent.verify.checks import CheckError

PLANS = paywall()


class Terminal(io.StringIO):
    def isatty(self):
        return True


def parse(*argv):
    with mock.patch.object(mobster, "load_extensions", return_value=Hooks()):
        return mobster.build_parser().parse_args(["verify", *argv])


class CliCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.out = self.root / ".mobster" / "runs"
        clock = Clock()
        self.manager = Manager()
        self.driver = Driver([PLANS])
        for patch in (mock.patch.object(runner, "_now", clock.time), mock.patch.object(runner, "_pause", clock.sleep),
                      mock.patch.object(runner.VerifyRun, "_manager", lambda run: self.manager),
                      mock.patch.object(runner, "build_driver", lambda *args: self.driver),
                      mock.patch.object(runner, "front_app",
                                        lambda url, timeout=5: (self.driver.active_app(), self.manager.pid)),
                      mock.patch.object(mobster, "load_extensions", return_value=Hooks()),
                      mock.patch.dict(os.environ, {"MOBSTER_ENV_FILE": os.devnull})):
            patch.start()
            self.addCleanup(patch.stop)

    def main(self, *argv, stdout=None):
        out, err = stdout or io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = mobster.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()


class SelectorFlagTests(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(cli.parse_selector_flag("label=Restore Purchases,role=button"),
                         {"label": "Restore Purchases", "role": "button"})
        self.assertEqual(cli.parse_selector_flag("id=/^plan_/,selected=true,enabled=False"),
                         {"id": "/^plan_/", "selected": True, "enabled": False})
        self.assertEqual(cli.parse_selector_flag("value=a=b"), {"value": "a=b"})

    def test_errors(self):
        for text, message in (("Continue", "key=value"), ("lable=x", "not a selector key"),
                              ("id=a,id=b", "given twice"), ("enabled=yes", "true or false"), ("=x", "key=value")):
            with self.subTest(text=text):
                with self.assertRaisesRegex(CheckError, message):
                    cli.parse_selector_flag(text)


class BuildCheckTests(unittest.TestCase):
    def test_flags_become_a_check_in_order(self):
        args = parse("--bundle", BUNDLE, "--text", "Choose your plan", "--absent", "id=spinner",
                     "--expect", '{"count": {"id": "/^plan_/"}, "equals": 3}', "--no-text", "Loading",
                     "--visible", "label=Terms,role=link", "--launch-arg=-DaybreakSkipOnboarding", "--launch-arg",
                     "YES", "--launch-env", "DAYBREAK_SEED=3", "--open-url", "daybreak://paywall", "--name", "Paywall")
        check, mode = cli.build_check(args)
        self.assertEqual(mode, "auto")
        self.assertEqual([a.to_dict() for a in check.expect], [
            {"text": "Choose your plan"}, {"absent": {"id": "spinner"}}, {"count": {"id": "/^plan_/"}, "equals": 3},
            {"no_text": "Loading"}, {"visible": {"label": "Terms", "role": "link"}}])
        self.assertEqual(check.launch_args, ("-DaybreakSkipOnboarding", "YES"))
        self.assertEqual(check.launch_env, {"DAYBREAK_SEED": "3"})
        self.assertEqual((check.open_url, check.name, check.bundle_id), ("daybreak://paywall", "Paywall", BUNDLE))

    def test_steps_before_and_after_options(self):
        args = parse("Open General,", "--bundle", "com.apple.Preferences", "then About", "--text", "iOS Version")
        check, _ = cli.build_check(args)
        self.assertEqual(check.steps, ("Open General,", "then About"))
        self.assertEqual(cli.build_check(parse("--keyless", "--bundle", BUNDLE))[1], "launch")

    def test_flag_errors_name_the_flag(self):
        for argv, message in ((["--bundle", BUNDLE, "--text", "/[/"], "--text: /\\[/ is not a valid regex"),
                              (["--bundle", BUNDLE, "--visible", "role=buton"], "did you mean button"),
                              (["--bundle", BUNDLE, "--expect", "{nope"], "is not JSON"),
                              (["--bundle", BUNDLE, "--expect", "[1]"], "JSON object"),
                              (["--bundle", BUNDLE, "--launch-env", "NOVALUE"], "KEY=VALUE"),
                              (["--text", "x"], "name the app")):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(CheckError, message):
                    cli.build_check(parse(*argv))

    def test_check_files_and_their_overrides(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paywall.yaml"
            path.write_text("app: {bundle: dev.mobster.daybreak}\nexpect: [{text: Choose your plan}]\n")
            check, _ = cli.build_check(parse("--check", str(path), "--device", "iPhone 16 Pro", "--max-usd", "0.1",
                                             "--reset", "none", "--app", "Build/Daybreak.app"))
            self.assertEqual((check.device, check.max_usd, check.reset), ("iPhone 16 Pro", 0.1, "none"))
            self.assertEqual(check.app_path, str(Path("Build/Daybreak.app").resolve()))
            with self.assertRaisesRegex(CheckError, "can't be combined with STEP, --text, --open-url"):
                cli.build_check(parse("--check", str(path), "Do it", "--text", "x", "--open-url", "a://b"))


class UsageExitTests(CliCase):
    def test_argparse_errors_exit_3_with_a_usage_result(self):
        for argv in (["verify", "--bogus"], ["verify", "--max-usd", "5"], ["verify", "--max-usd", "0"],
                     ["verify", "--reset", "all"], ["verify", "--assert-timeout", "61"], ["verify", "--check"],
                     ["verify", "--max-seconds", "nope"]):
            with self.subTest(argv=argv):
                code, out, err = self.main(*argv)
                self.assertEqual(code, 3)
                self.assertIn("mobster verify: error:", err)
                result = json.loads(out)
                self.assertEqual((result["verdict"], result["exit_code"], result["reason"]["class"]),
                                 ("couldnt_run", 3, "usage"))
                self.assertIsNone(result["run_id"])

    def test_on_a_terminal_only_json_asks_for_json(self):
        code, out, err = self.main("verify", "--bogus", stdout=Terminal())
        self.assertEqual((code, out), (3, ""))
        code, out, _ = self.main("verify", "--bogus", "--json", stdout=Terminal())
        self.assertEqual(json.loads(out)["reason"]["class"], "usage")

    def test_options_of_the_terminal_ui_before_verify(self):
        code, out, err = self.main("--demo", "verify", "--bundle", BUNDLE, "--text", "Plans")
        self.assertEqual(code, 3)
        self.assertIn("--demo is an option of the terminal UI", err)
        result = json.loads(out)  # a pipe: an agent parsing stdout gets the usage result (§4)
        self.assertEqual((result["verdict"], result["exit_code"], result["reason"]["class"]),
                         ("couldnt_run", 3, "usage"))
        self.assertIn("--demo is an option of the terminal UI", result["reason"]["message"])
        code, out, _ = self.main("--demo", "verify", "--bundle", BUNDLE, stdout=Terminal())
        self.assertEqual((code, out), (3, ""))
        code, out, _ = self.main("--demo", "verify", "--bundle", BUNDLE, "--json", stdout=Terminal())
        self.assertEqual((code, json.loads(out)["reason"]["class"]), (3, "usage"))
        self.assertEqual(self.manager.calls, [])
        code, out, _ = self.main("--app", "Messages", "verify", "--bundle", BUNDLE)
        self.assertEqual(code, 3)
        self.assertIn("--app goes after the command", json.loads(out)["summary"])

    def test_usage_errors_in_options_before_verify_exit_3_with_json(self):
        """`mobster --bogus verify …` and a bad --wda-url before verify: argparse's exit 2 would read as needs
        review, with nothing on stdout for the agent parsing it."""
        for argv in (["--bogus", "verify", "--bundle", BUNDLE],
                     ["--wda-url", "ftp://x", "verify", "--bundle", BUNDLE, "--json"],
                     ["--resume", "verify", "--bundle", BUNDLE, "--nope"]):
            with self.subTest(argv=argv):
                code, out, err = self.main(*argv)
                self.assertEqual(code, 3)
                self.assertIn("error:", err)
                result = json.loads(out)
                self.assertEqual((result["verdict"], result["exit_code"], result["reason"]["class"]),
                                 ("couldnt_run", 3, "usage"))
        code, out, _ = self.main("--bogus", "verify", "--bundle", BUNDLE, stdout=Terminal())
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(self.manager.calls, [])

    def test_command_word(self):
        parser = mobster.build_parser()
        for argv, word in ((["verify"], "verify"), (["--device", "verify", "run", "x"], "run"),
                           (["--env-file=a", "verify"], "verify"), (["--resume", "verify"], "verify"),
                           (["--resume", "abc", "history"], "history"), (["--demo"], None), (["verfy"], None),
                           (["--", "verify"], None)):
            with self.subTest(argv=argv):
                self.assertEqual(mobster.command_word(parser, argv), word)

    def test_check_errors_exit_3(self):
        code, out, err = self.main("verify", "--bundle", BUNDLE, "--text", "/(/")
        self.assertEqual(code, 3)
        self.assertIn("not a valid regex", json.loads(out)["reason"]["message"])
        code, out, _ = self.main("verify", "--bundle", BUNDLE, "--save", "Bad Name")
        self.assertEqual(code, 3)
        self.assertEqual(self.manager.calls, [])

    def test_help_exits_0(self):
        code, out, _ = self.main("verify", "--help")
        self.assertEqual(code, 0)
        self.assertIn("exit codes: 0 passed, 1 failed, 2 needs review, 3 couldn't run", out)

    def test_env_file_names_the_one_key_verify_reads(self):
        with mock.patch.object(mobster, "load_extensions", return_value=Hooks()):
            parser = mobster.build_parser()
        text = parser.commands["verify"]._option_string_actions["--env-file"].help
        self.assertEqual(text, "load KEY=VALUE lines, such as OPENAI_API_KEY or ANTHROPIC_API_KEY for Smart; "
                               "variables already set win; default: $MOBSTER_ENV_FILE")
        self.assertIn("TYPESAFE_API_KEY", parser.commands["run"]._option_string_actions["--env-file"].help)


class RunTests(CliCase):
    def test_a_passing_launch_only_check(self):
        code, out, err = self.main("verify", "--bundle", BUNDLE, "--text", "Choose your plan", "--out", str(self.out))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(out.count("\n"), 1)  # exactly one JSON object
        self.assertEqual((result["verdict"], result["mode"]), ("passed", "launch"))
        self.assertTrue(Path(result["run_dir"]).is_relative_to(self.out))
        self.assertNotIn("{", err)

    def test_exit_codes_follow_the_verdict(self):
        for argv, code in ((["--count-plans"], 1), ([], 2)):
            expect = ["--expect", '{"count": {"id": "/^plan_/"}, "equals": 4}'] if argv else []
            with self.subTest(code=code):
                got, out, _ = self.main("verify", "--bundle", BUNDLE, *expect, "--out", str(self.out))
                self.assertEqual(got, code)
                self.assertEqual(json.loads(out)["exit_code"], code)

    def test_steps_with_keyless_or_without_a_key(self):
        code, out, _ = self.main("verify", "Open the paywall", "--keyless", "--bundle", BUNDLE, "--text", "x",
                                 "--out", str(self.out))
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["reason"]["class"], "usage")
        with mock.patch.object(engines, "smart_key", lambda env=None: None):
            code, out, _ = self.main("verify", "Open the paywall", "--bundle", BUNDLE, "--text", "x",
                                     "--out", str(self.out))
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["reason"]["fix"], "Set OPENAI_API_KEY or ANTHROPIC_API_KEY for Smart, or let "
                                                           "your coding agent drive through `mobster mcp`.")
        self.assertEqual(self.manager.calls, [])

    def test_a_check_nested_too_deeply_is_a_usage_error(self):
        # Review round 3: a pathological file raised RecursionError, which exited 1 (failed) with a traceback.
        for suffix, text in ((".json", "[" * 100_000 + "]" * 100_000), (".yaml", "[" * 100_000 + "]" * 100_000)):
            with self.subTest(suffix=suffix):
                path = self.root / f"deep{suffix}"
                path.write_text(text)
                code, out, err = self.main("verify", "--check", str(path), "--out", str(self.out))
                self.assertEqual(code, 3)
                self.assertEqual(json.loads(out)["reason"]["class"], "usage")
                self.assertNotIn("Traceback", err)

    def test_any_unexpected_error_reading_the_check_is_a_usage_error(self):
        with mock.patch.object(cli, "build_check", side_effect=TypeError("odd")):
            code, out, err = self.main("verify", "--bundle", BUNDLE, "--text", "x", "--out", str(self.out))
        self.assertEqual(code, 3)
        self.assertIn("the check can't be read (TypeError)", err)
        self.assertNotIn("Traceback", err)

    def test_a_check_file_may_name_only_a_simulator(self):
        """A check file travels with a repository: its device: never picks someone's iPhone. --device does."""
        from mobile_agent import device_targets
        check = self.root / "phone.yaml"
        check.write_text(f"version: 1\napp:\n  bundle: {BUNDLE}\ndevice: Work iPhone\nexpect:\n"
                         "  - text: Choose your plan\n")
        phone = {"id": "PHONE", "kind": "usb", "name": "Work iPhone", "udid": "PHONE", "wdaUrl": "http://x"}
        with mock.patch.object(device_targets, "named_device", lambda name, records=None: phone), \
                mock.patch.object(device_targets, "manager_for", lambda record, **kwargs: self.manager):
            code, out, err = self.main("verify", "--check", str(check), "--out", str(self.out), "--json")
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(out)["reason"]["class"], "usage")
            self.assertIn("a check file may name only a simulator", err)
            self.assertIn('--device "Work iPhone"', err)
            code, out, _ = self.main("verify", "--check", str(check), "--device", "Work iPhone", "--out",
                                     str(self.out), "--json")
            self.assertEqual((code, json.loads(out)["verdict"]), (0, "passed"))

    def test_an_out_folder_that_cant_be_written_is_a_usage_error_with_json(self):
        blocker = self.root / "file"
        blocker.write_text("x")
        code, out, err = self.main("verify", "--bundle", BUNDLE, "--text", "General", "--out", str(blocker / "runs"),
                                   "--json")
        self.assertEqual(code, 3)
        result = json.loads(out)
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "usage"))
        self.assertIn("--out", result["reason"]["message"])
        self.assertEqual(self.manager.calls, [])

    def test_an_unexpected_error_still_prints_one_json_result(self):
        with mock.patch.object(runner, "verify", side_effect=RuntimeError("boom")):
            code, out, err = self.main("verify", "--bundle", BUNDLE, "--out", str(self.out))
        self.assertEqual(code, 3)
        result = json.loads(out)
        self.assertEqual((result["verdict"], result["exit_code"], result["reason"]["class"]),
                         ("couldnt_run", 3, "internal"))
        self.assertIn("RuntimeError: boom", err)

    def test_a_stopped_run_prints_its_result_and_exits_130_or_128_plus_the_signal(self):
        import signal
        for stop, code in ((KeyboardInterrupt(), 130), (devtools.Terminated(signal.SIGTERM), 143)):
            with self.subTest(code=code):
                with mock.patch.object(runner.VerifyRun, "prepare", side_effect=stop):
                    got, out, err = self.main("verify", "--bundle", BUNDLE, "--text", "x", "--out", str(self.out))
                self.assertEqual(got, code)
                result = json.loads(out)
                self.assertEqual((result["verdict"], result["exit_code"], result["reason"]["class"]),
                                 ("couldnt_run", code, "stopped"))
                self.assertTrue(Path(result["run_dir"], "result.json").is_file())
                self.assertIn("stopped", err)

    def test_the_human_summary(self):
        code, out, err = self.main("verify", "--bundle", BUNDLE, "--name", "Paywall", "--text", "Choose your plan",
                                   "--expect", '{"count": {"id": "/^plan_/"}, "equals": 4}', "--out", str(self.out),
                                   stdout=Terminal())
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^(\x1b\[[0-9;]*m)?✗ failed(\x1b\[0m)?  Paywall  \(\d+(\.\d+)? s\)$")
        self.assertEqual(lines[1], '  ✓ text "Choose your plan"')
        self.assertEqual(lines[2], "  ✗ count id=/^plan_/ == 4: found 3: plan_weekly, plan_monthly, plan_annual")
        self.assertTrue(lines[3].startswith("  Report  "))
        self.assertTrue(lines[4].startswith("  Rerun   mobster verify --check "))

    def test_the_summary_of_a_run_that_couldnt_run(self):
        text = cli.summary_text({"verdict": "couldnt_run", "check": {"name": "Paywall"}, "seconds": 0.4,
                                 "assertions": [], "reason": {"class": "busy", "message": "Every simulator is in use.",
                                                              "fix": "Wait for one to finish."},
                                 "report": None, "draft_check": None})
        self.assertEqual(text, "✗ couldn't run  Paywall  (0.4 s)\n  Every simulator is in use.\n"
                               "  Wait for one to finish.")

    def test_save(self):
        argv = ["verify", "--bundle", BUNDLE, "--text", "Choose your plan", "--out", str(self.out), "--save", "paywall"]
        code, _, err = self.main(*argv)
        saved = self.root / ".mobster" / "checks" / "paywall.yaml"
        self.assertEqual(code, 0)
        self.assertTrue(saved.is_file())
        self.assertIn("Saved", err)
        code, _, err = self.main(*argv)
        self.assertIn("Replaced", err)
        from mobile_agent.verify.checks import load_check
        self.assertEqual(load_check(saved).expect[0].to_dict(), {"text": "Choose your plan"})

    def test_the_module_entry_point_matches(self):
        from mobile_agent.verify import __main__ as module
        with mock.patch.dict(os.environ, {"COLUMNS": "100"}):
            own = module.build_parser().format_help()
            registered = parse("--bundle", "a.b")  # built the same way
            with mock.patch.object(mobster, "load_extensions", return_value=Hooks()):
                parser = mobster.build_parser()
            sub = parser.commands["verify"].format_help()
        self.assertEqual(own, sub)
        self.assertEqual(registered.app_path, None)


class DevtoolsTests(unittest.TestCase):
    def help_text(self):
        with mock.patch.dict(os.environ, {"COLUMNS": "100"}), \
                mock.patch.object(mobster, "load_extensions", return_value=Hooks()):
            return mobster.build_parser().format_help()

    def test_help_does_not_depend_on_which_modules_are_present(self):
        present = self.help_text()
        with mock.patch.object(devtools, "load", lambda name: None):
            missing = self.help_text()
        self.assertEqual(present, missing)
        from mobile_agent import help_topics
        listed = help_topics.top_level(lambda text, *_: text, 100)
        for name in ("verify", "mcp", "sim", "test"):
            self.assertIn(f"\n  {name} ", listed)
        # Your tasks come before the developer commands (P0-1 of the CLI polish brief).
        self.assertLess(listed.index("\n  run "), listed.index("\n  verify "))

    def test_a_missing_command_says_so_and_exits_3(self):
        err = io.StringIO()
        with mock.patch.object(devtools, "load", lambda name: None), redirect_stderr(err), \
                mock.patch.object(mobster, "load_extensions", return_value=Hooks()), \
                mock.patch.dict(os.environ, {"MOBSTER_ENV_FILE": os.devnull}):
            code = mobster.main(["sim", "list", "--json"])
        self.assertEqual(code, 3)
        self.assertEqual(err.getvalue(), "`mobster sim` is not in this build\n")

    def test_load_tells_a_missing_module_from_a_broken_one(self):
        missing = ModuleNotFoundError("No module named 'mobile_agent.mcp_server'", name="mobile_agent.mcp_server")
        with mock.patch("builtins.__import__", side_effect=missing):
            self.assertIsNone(devtools.load("mcp"))
        broken = ModuleNotFoundError("No module named 'pydantic'", name="pydantic")
        with mock.patch("builtins.__import__", side_effect=broken):
            with self.assertRaises(ModuleNotFoundError):
                devtools.load("mcp")
        self.assertIs(devtools.load("verify"), cli)

    def test_each_cli_module_has_exactly_the_two_functions(self):
        self.assertTrue(callable(cli.add_arguments) and callable(cli.run))


if __name__ == "__main__":
    unittest.main()

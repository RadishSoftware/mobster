"""`mobster mcp`'s command: its flags, where runs go, and its section in the CLI reference."""

import argparse
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from mobile_agent.mcp_server import cli
from mobile_agent.mcp_server.__main__ import build_parser

ROOT = Path(__file__).resolve().parents[2]
FLAGS = ["--env-file", "--keyless", "--device", "--allow-device", "--allow-files", "--runtime", "--out", "--log"]


class FlagTests(unittest.TestCase):
    def test_every_flag_has_help(self):
        parser = build_parser()
        options = [action for action in parser._actions if action.option_strings and "--help" not in action.option_strings]
        self.assertEqual(sorted(o for action in options for o in action.option_strings), sorted(FLAGS))
        for action in options:
            with self.subTest(option=action.option_strings[0]):
                self.assertTrue(action.help and action.help[0].islower() and not action.help.endswith("."))

    def test_add_arguments_takes_the_shared_helpers(self):
        """devtools.py registers the command with __main__'s helpers (SPEC §12.3)."""
        from mobile_agent import __main__ as main
        parser = argparse.ArgumentParser(prog="mobster")
        subs = parser.add_subparsers(dest="command")
        cli.add_arguments(subs.add_parser("mcp"), {"env_option": main._env_option, "path_type": main.path_type,
                                                   "bounded": main._bounded})
        args = parser.parse_args(["mcp", "--keyless", "--out", "~/runs", "--device", "iPhone 17 Pro"])
        self.assertTrue(args.keyless)
        self.assertEqual(args.out, Path("~/runs").expanduser())
        self.assertEqual(args.device, "iPhone 17 Pro")
        self.assertFalse(hasattr(args, "env_file"))  # SUPPRESS: a value given before `mcp` is kept
        self.assertEqual(parser.parse_args(["mcp", "--env-file", "~/k.env"]).env_file, Path("~/k.env").expanduser())

    def test_run_never_raises(self):
        import contextlib
        import io
        args = argparse.Namespace(log=None)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with mock.patch.object(cli, "_serve", side_effect=RuntimeError("boom")):
                self.assertEqual(cli.run(args), 1)
            with mock.patch.object(cli, "_serve", side_effect=KeyboardInterrupt):
                self.assertEqual(cli.run(args), 130)
        self.assertIn("The server stopped on an error (RuntimeError: boom).", stderr.getvalue())


class SmartSetupTests(unittest.TestCase):
    """verify (Smart) is offered on the key and model the Mac app's Smart would use (engines.smart_key and
    engines.smart_model, #44), and when it is off the server says why in terms the user can act on."""

    def test_the_key_and_model_follow_engines(self):
        from mobile_agent.mcp_server.tools import NO_SMART_KEY
        openai, anthropic = {"OPENAI_API_KEY": "sk-o"}, {"ANTHROPIC_API_KEY": "sk-a"}
        cases = [
            ({}, (None, None, NO_SMART_KEY)),
            (openai, ("sk-o", "gpt-5.6-sol", None)),
            (anthropic, ("sk-a", "claude-sonnet-5-5", None)),
            ({**openai, **anthropic}, ("sk-o", "gpt-5.6-sol", None)),
            ({**openai, **anthropic, "MOBSTER_SMART_MODEL": "claude-opus-5-5"}, ("sk-a", "claude-opus-5-5", None)),
            ({**openai, "MOBSTER_SMART_MODEL": "claude-opus-5-5"},
             (None, None, "MOBSTER_SMART_MODEL is claude-opus-5-5, which needs an Anthropic key; set "
                          "ANTHROPIC_API_KEY, or unset MOBSTER_SMART_MODEL to use your OpenAI key")),
            ({**anthropic, "MOBSTER_SMART_MODEL": "gpt-5.6-sol"},
             (None, None, "MOBSTER_SMART_MODEL is gpt-5.6-sol, which needs an OpenAI key; set OPENAI_API_KEY, or "
                          "unset MOBSTER_SMART_MODEL to use your Anthropic key")),
            ({"MOBSTER_SMART_MODEL": "claude-opus-5-5"},
             (None, None, "MOBSTER_SMART_MODEL is claude-opus-5-5, which needs an Anthropic key; set "
                          "ANTHROPIC_API_KEY")),
        ]
        for env, expected in cases:
            with self.subTest(env=sorted(env)):
                self.assertEqual(cli.smart_setup(env=env), expected)
        self.assertEqual(cli.smart_setup(keyless=True, env={**openai, **anthropic}), (None, None, "--keyless"))

    def test_a_missing_env_file_leads_the_reason_smart_is_off(self):
        from mobile_agent.mcp_server.tools import NO_SMART_KEY
        self.assertEqual(cli.smart_setup(env={}, env_file="/nonexistent/agent.env"),
                         (None, None, "/nonexistent/agent.env does not exist; no keys or settings were loaded from "
                                      "it; " + NO_SMART_KEY))
        self.assertEqual(cli.smart_setup(env={"OPENAI_API_KEY": "sk-o"}, env_file="/nonexistent/agent.env")[2], None)
        self.assertEqual(cli.smart_setup(keyless=True, env={}, env_file="/nonexistent/agent.env")[2], "--keyless")

    def test_the_client_a_verify_run_builds_matches_what_the_server_says(self):
        """A verify run builds its client with engines.build_client(key=...), from the same environment."""
        from mobile_agent import engines
        for env, model, client in (({"OPENAI_API_KEY": "sk-o"}, "gpt-5.6-sol", "OpenAIChat"),
                                   ({"ANTHROPIC_API_KEY": "sk-a"}, "claude-sonnet-5-5", "AnthropicChat")):
            with self.subTest(model=model), mock.patch.dict(os.environ, env, clear=True):
                key, said, _off = cli.smart_setup()
                built = engines.build_client(key=key)
                self.assertEqual((said, built.model, type(built).__name__), (model, model, client))


class RunsFolderTests(unittest.TestCase):
    def test_runs_folder_order(self):
        with tempfile.TemporaryDirectory() as folder:
            home, project = Path(folder) / "home", Path(folder) / "project"
            home.mkdir()
            project.mkdir()
            data = {"MOBSTER_DATA_DIR": str(Path(folder) / "data")}
            self.assertEqual(cli.runs_dir("/abs/out", env={}), Path("/abs/out"))
            self.assertEqual(cli.runs_dir(None, env={"MOBSTER_RUNS_DIR": "/env/runs"}), Path("/env/runs"))
            self.assertEqual(cli.runs_dir(None, env={}, cwd=project, home=home), project / ".mobster" / "runs")
            for cwd in (home, Path("/")):
                with self.subTest(cwd=str(cwd)):
                    expected = cli.dev_data_dir(data) / "runs"
                    self.assertEqual(cli.runs_dir(None, env=data, cwd=cwd, home=home), expected)

    def test_dev_data_dir_follows_mobster_data_dir(self):
        try:
            from mobile_agent.paths import dev_data_dir  # noqa: F401  (added by the sim workstream)
        except ImportError:
            self.assertEqual(cli.dev_data_dir({"MOBSTER_DATA_DIR": "/data/dev"}), Path("/data/dev"))
            with mock.patch("mobile_agent.paths.user_data_dir", return_value=Path("/support")):
                self.assertEqual(cli.dev_data_dir({}), Path("/support/dev"))
        else:
            with mock.patch.dict(os.environ, {"MOBSTER_DATA_DIR": "/data/dev"}):
                self.assertEqual(cli.dev_data_dir(), Path("/data/dev"))


class LogTests(unittest.TestCase):
    def test_log_lines_go_to_stderr_and_the_log_file(self):
        import io
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            log = cli.Log(Path(folder) / "mcp.log", stream=stream)
            log("Run 20260928-120000-abcd is ready.")
            written = (Path(folder) / "mcp.log").read_text()
        self.assertRegex(stream.getvalue(), r"^mobster mcp \d\d:\d\d:\d\d Run 20260928-120000-abcd is ready\.\n$")
        self.assertEqual(written, stream.getvalue())


class ReferenceTests(unittest.TestCase):
    def test_the_cli_reference_has_the_mcp_section_between_serve_and_demo(self):
        reference = (ROOT / "docs" / "cli.md").read_text()
        headings = re.findall(r"^## (.+)$", reference, flags=re.M)
        mcp = headings.index("`mobster mcp`")
        self.assertEqual(headings[mcp - 1], "`mobster serve`")
        self.assertEqual(headings[mcp + 1], "`mobster demo`")
        section = reference.split("## `mobster mcp`", 1)[1].split("\n## ", 1)[0]
        for flag in FLAGS:
            with self.subTest(flag=flag):
                self.assertIn(f"`{flag}", section)


if __name__ == "__main__":
    unittest.main()

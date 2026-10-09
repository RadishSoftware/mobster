"""The CLI polish pass (round 1): login and the Mac app's keys, start-up cost, completion scripts, the update notice
and one vocabulary with Mobster for Mac. Offline: no phone, no network, no model calls."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from mobile_agent import __main__ as cli
from mobile_agent.extensions import Hooks

ROOT = Path(__file__).resolve().parents[2]
KEY = "sk-ant-api03-" + "x" * 40 + "a1F2"
CLEAN = {name: "" for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TYPESAFE_API_KEY", "MOBSTER_SMART_PROVIDER",
                                "MOBSTER_SMART_MODEL", "MOBSTER_ENV_FILE", "MOBSTER_WDA_URL", "CI",
                                "MOBSTER_NO_UPDATE_CHECK")}


class Pipe(io.StringIO):
    def isatty(self):
        return False


class Keyboard(io.StringIO):
    def isatty(self):
        return True


class Home(unittest.TestCase):
    """A fresh HOME (so the Mac app's settings file is a temporary one) and no keys in the environment."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="mobster-polish-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {**CLEAN, "HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        for name in CLEAN:
            os.environ.pop(name, None)
        from mobile_agent import config
        sources = mock.patch.dict(config.SOURCES, {}, clear=True)
        sources.start()
        self.addCleanup(sources.stop)

    def main(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "load_extensions", return_value=Hooks()), \
                mock.patch("mobile_agent.tls.ensure_ca_bundle"), \
                mock.patch("sys.stdin", stdin or Pipe("")), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class LoginTests(Home):
    def test_a_key_from_a_pipe_is_saved_privately_and_never_printed(self):
        code, out, err = self.main("login", "--provider", "anthropic", "--with-key", "--skip-check", "--json",
                                   stdin=Pipe(KEY + "\n"))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"ok": True, "provider": "anthropic", "key": "sk-ant-…a1F2",
                                           "checked": False})
        from mobile_agent.config import app_env_file
        saved = app_env_file()
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertIn(f"ANTHROPIC_API_KEY={KEY}", saved.read_text())
        self.assertNotIn(KEY, out + err)

    def test_with_key_refuses_the_keyboard(self):
        code, out, err = self.main("login", "--with-key", stdin=Keyboard(""))
        self.assertEqual(code, 2)
        self.assertIn("not the keyboard", err)

    def test_status_shows_only_a_prefix_and_the_last_four(self):
        os.environ["ANTHROPIC_API_KEY"] = KEY
        code, out, _ = self.main("login", "status", "--json")
        self.assertEqual(code, 0)
        rows = {row["provider"]: row for row in json.loads(out)["keys"]}
        self.assertEqual(rows["anthropic"]["key"], "sk-ant-…a1F2")
        self.assertFalse(rows["openai"]["set"])
        self.assertNotIn(KEY, out)


class MacAppKeyTests(Home):
    def test_doctor_reads_the_key_mobster_for_mac_saved(self):
        from mobile_agent.config import app_env_file
        path = app_env_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"ANTHROPIC_API_KEY={KEY}\n")
        path.chmod(0o600)
        with mock.patch("mobile_agent.doctor.run_checks", wraps=None) as checks:
            from mobile_agent import doctor
            checks.side_effect = lambda **kw: [doctor.check_key(kw.get("env_file"))]
            code, out, _ = self.main("doctor", "--json")
        report = json.loads(out)
        key = next(check for check in report["checks"] if check["key"] == "key")
        self.assertEqual(key["state"], "ok")
        self.assertIn("from Mobster for Mac", key["detail"])
        self.assertIn("sk-ant-…a1F2", key["detail"])
        self.assertNotIn(KEY, out)

    def test_mcp_never_reads_the_mac_apps_keys(self):
        self.assertNotIn("mcp", cli.APP_KEY_COMMANDS)
        self.assertNotIn("serve", cli.APP_KEY_COMMANDS)


class StartupTests(unittest.TestCase):
    """P1-1: `--version` and `--help` answer before the parser exists, so they import none of the heavy modules."""

    HEAVY = ("mobile_agent.devtools", "mobile_agent.sim", "ssl", "urllib.request", "textual", "mobile_agent.state",
             "mobile_agent.device_manager", "mobile_agent.server")

    def imported(self, *argv):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "MOBSTER_NO_UPDATE_CHECK": "1"}
        # -I keeps the checkout's own copy first and nothing else from the environment; the private extension is
        # what a public install never has, so it is hidden here.
        code = ("import sys, runpy; sys.modules['mobile_agent.private'] = None; sys.argv = ['mobster'] + "
                + repr(list(argv)) + "; runpy.run_module('mobile_agent', run_name='__main__')")
        done = subprocess.run([sys.executable, "-X", "importtime", "-c", code], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=60)
        return {line.split("|")[-1].strip() for line in done.stderr.splitlines() if line.startswith("import time:")}

    def test_version_and_help_import_nothing_heavy(self):
        for argv in (["--version"], ["--help"]):
            with self.subTest(argv=argv):
                modules = self.imported(*argv)
                self.assertIn("mobile_agent.help_topics" if argv == ["--help"] else "mobile_agent", modules)
                for heavy in self.HEAVY:
                    self.assertNotIn(heavy, modules)


@unittest.skipUnless(shutil.which("zsh") and shutil.which("bash"), "needs zsh and bash")
class CompletionTests(unittest.TestCase):
    def script(self, shell):
        out = io.StringIO()
        with mock.patch.object(cli, "load_extensions", return_value=Hooks()), redirect_stdout(out):
            self.assertEqual(cli.main(["completion", shell]), 0)
        return out.getvalue()

    def test_the_scripts_parse_in_their_shells(self):
        for shell in ("zsh", "bash"):
            with self.subTest(shell=shell):
                text = self.script(shell)
                self.assertIn("login", text)
                done = subprocess.run([shell, "-n"], input=text, capture_output=True, text=True, timeout=30)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_zsh_quotes_never_hold_an_escaped_apostrophe(self):
        self.assertNotIn("\\'", self.script("zsh"))


class UpdateNoticeTests(Home):
    def test_it_is_silent_for_scripts(self):
        from mobile_agent import update_check
        tty = Keyboard()
        with mock.patch("sys.stdout", Keyboard()):
            self.assertTrue(update_check.enabled("doctor", mock.Mock(json=False), stream=tty))
            self.assertFalse(update_check.enabled("doctor", mock.Mock(json=True), stream=tty))
            self.assertFalse(update_check.enabled("mcp", mock.Mock(json=False), stream=tty))
            with mock.patch.dict(os.environ, {"CI": "1"}):
                self.assertFalse(update_check.enabled("doctor", mock.Mock(json=False), stream=tty))
            with mock.patch.dict(os.environ, {"MOBSTER_NO_UPDATE_CHECK": "1"}):
                self.assertFalse(update_check.enabled("doctor", mock.Mock(json=False), stream=tty))
        self.assertFalse(update_check.enabled("doctor", mock.Mock(json=False), stream=Pipe()))

    def test_the_line_says_what_is_out_and_how_to_get_it(self):
        from mobile_agent import update_check
        cache = {"latest": {"version": "0.3.0", "publishedAt": "2026-10-01T00:00:00Z"}}
        now = 1791676800  # 11 Oct 2026, 00:00 UTC
        self.assertEqual(update_check.notice("0.2.0", cache, now=now),
                         "Mobster 0.3.0 is out, released 10 days ago (you have 0.2.0) · mobster update")
        self.assertEqual(update_check.notice("0.3.0", cache, now=now), "")


@unittest.skipUnless((ROOT / "dashboard" / "src" / "lib" / "api.ts").is_file(), "the Mac app's source isn't here")
class VocabularyTests(unittest.TestCase):
    def test_status_labels_match_mobster_for_mac(self):
        """C2: the terminal says what the Mac app says for every status both know."""
        from mobile_agent.narrate import STATUS_LABELS
        source = (ROOT / "dashboard" / "src" / "lib" / "api.ts").read_text()
        block = re.search(r"statusLabels: Record<string, string> = \{(.*?)\}", source, re.S).group(1)
        app = dict(re.findall(r"(\w+): '([^']*)'", block))
        shared = set(app) & set(STATUS_LABELS)
        self.assertGreater(len(shared), 15)
        self.assertEqual({key: STATUS_LABELS[key] for key in shared}, {key: app[key] for key in shared})


class UsageErrorTests(unittest.TestCase):
    def run_main(self, *argv):
        err = io.StringIO()
        with mock.patch.object(cli, "load_extensions", return_value=Hooks()), redirect_stderr(err), \
                redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            try:
                code = cli.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code
        return code, err.getvalue()

    def test_an_unquoted_task_says_to_quote_it(self):
        code, err = self.run_main("run", "Turn", "on", "Dark", "Mode")
        self.assertEqual(code, 2)
        self.assertIn('mobster run "Turn on Dark Mode"', err)

    def test_a_mistyped_option_suggests_the_right_one(self):
        code, err = self.run_main("run", "Turn on Dark Mode", "--exec")
        self.assertEqual(code, 2)
        self.assertIn("did you mean --execute?", err)


if __name__ == "__main__":
    unittest.main()

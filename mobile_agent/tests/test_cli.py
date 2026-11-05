import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import __main__ as cli
from mobile_agent import harness_api, update_check
from mobile_agent.cli_parser import build_parser


class CliTests(unittest.TestCase):
    def test_quick_modes_run_flags_are_usage_errors(self):
        for flags in (["--engine", "fast"], ["--helper"], ["--expected-text", "Done"],
                      ["--allow-app", "com.apple.Preferences"], ["--max-steps", "5"], ["--max-seconds", "30"]):
            with self.subTest(flags=flags), self.assertRaises(SystemExit):
                build_parser().parse_args(["run", "Tap", "--wda-url", "http://x:8100", *flags])


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
        with mock.patch.object(harness_api.hooks(), "cli", []), \
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
        self.assertEqual(rows["anthropic"]["source"], "from your shell")  # a key only in the shell says so
        self.assertFalse(rows["openai"]["set"])
        self.assertNotIn(KEY, out)

    def test_status_sees_the_key_mobster_for_mac_saved(self):
        from mobile_agent.config import app_env_file
        path = app_env_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"ANTHROPIC_API_KEY={KEY}\n")
        path.chmod(0o600)
        with mock.patch("mobile_agent.env_secrets.load", return_value={}):
            code, out, _ = self.main("login", "status", "--json")
        self.assertEqual(code, 0)
        row = next(row for row in json.loads(out)["keys"] if row["provider"] == "anthropic")
        self.assertEqual((row["set"], row["key"], row["source"]), (True, "sk-ant-…a1F2", "from Mobster for Mac"))
        self.assertNotIn(KEY, out)

    def test_logout_with_a_plan_selected_removes_a_sole_legacy_key_or_asks_which(self):
        from mobile_agent import login
        args = mock.Mock(provider=None)
        with mock.patch.object(login, "_saved", return_value={"ANTHROPIC_API_KEY": KEY}), \
                mock.patch("mobile_agent.engines.model_provider", return_value="kimi"), \
                mock.patch.object(login, "_keys") as keys, redirect_stdout(io.StringIO()):
            self.assertEqual(login.logout(args), 0)
        keys.return_value.update.assert_called_once_with({"anthropic": None})
        with mock.patch.object(login, "_saved", return_value={"ANTHROPIC_API_KEY": KEY, "OPENAI_API_KEY": KEY}), \
                mock.patch("mobile_agent.engines.model_provider", return_value="kimi"), \
                mock.patch.object(login, "_keys") as keys, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(login.logout(args), 2)
        keys.assert_not_called()
        self.assertIn("--provider", err.getvalue())


class DoctorStateTests(unittest.TestCase):
    def test_only_a_boolean_false_reports_an_unlocked_phone(self):
        from mobile_agent import doctor
        for value, expected in ((True, doctor.FAIL), (False, doctor.OK), (None, doctor.SKIP),
                                (0, doctor.SKIP), ("false", doctor.SKIP)):
            with self.subTest(value=value), mock.patch("mobile_agent.transport.HTTP") as http:
                http.return_value.request.return_value = {"value": value}
                self.assertEqual(doctor.check_locked("http://127.0.0.1:8100").state, expected)


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


class ModuleEntryPointTests(Home):
    def test_python_dash_m_run_without_a_key_exits_3(self):
        env = {**os.environ, "HOME": str(self.home), "MOBSTER_NO_UPDATE_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        done = subprocess.run([sys.executable, "-m", "mobile_agent", "run", "Open Settings", "--execute", "--json"],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertEqual(json.loads(done.stdout.strip().splitlines()[-1])["exit_code"], 3)


class StartupTests(unittest.TestCase):
    HEAVY = ("mobile_agent.devtools", "mobile_agent.sim", "ssl", "urllib.request", "textual", "mobile_agent.state",
             "mobile_agent.device_manager", "mobile_agent.server")

    def imported(self, *argv):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "MOBSTER_NO_UPDATE_CHECK": "1"}
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
        with mock.patch.object(harness_api.hooks(), "cli", []), redirect_stdout(out):
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
        cache = {"latest": {"version": "0.4.0", "publishedAt": "2026-10-01T00:00:00Z"}}
        now = 1791676800  # 11 Oct 2026, 00:00 UTC
        self.assertEqual(update_check.notice("0.3.0", cache, now=now),
                         "Mobster 0.4.0 is out, released 10 days ago, in Mobster for Mac (you have 0.3.0). "
                         "Download it at https://mobster.dev/download")
        self.assertEqual(update_check.notice("0.4.0", cache, now=now), "")

    def test_update_never_runs_an_installer_or_a_package_manager(self):
        release = {"version": "99.0.0", "publishedAt": "2026-10-01T00:00:00Z"}
        with mock.patch.object(update_check, "latest_release", return_value=release), \
                mock.patch.object(update_check, "minimum_version", return_value=None), \
                mock.patch.object(update_check, "inside_app", return_value=False), \
                mock.patch.object(update_check, "source_checkout", return_value=False), \
                mock.patch("subprocess.call") as call, mock.patch("subprocess.run") as run, \
                mock.patch("os.system") as system, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(update_check.run_update(mock.Mock(check=False)), 0)
            call.assert_not_called(), run.assert_not_called(), system.assert_not_called()
            self.assertIn("Mobster 99.0.0 is out", out.getvalue())
            self.assertIn("https://mobster.dev/download", out.getvalue())
            self.assertIn("This mobster keeps working.", out.getvalue())
            self.assertNotIn("install.sh", out.getvalue())
            self.assertNotIn("brew upgrade", out.getvalue())

    def test_update_says_when_this_is_the_latest_and_when_it_couldnt_check(self):
        from mobile_agent import __version__
        current = {"version": __version__, "publishedAt": "2026-10-01T00:00:00Z"}
        with mock.patch.object(update_check, "latest_release", return_value=current), \
                mock.patch.object(update_check, "minimum_version", return_value=None), \
                mock.patch.object(update_check, "inside_app", return_value=False), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(update_check.run_update(mock.Mock(check=False)), 0)
        self.assertIn(f"Mobster {__version__} is the latest release.", out.getvalue())
        for check, code in ((True, 1), (False, 0)):
            with mock.patch.object(update_check, "latest_release", return_value=None), \
                    mock.patch.object(update_check, "minimum_version", return_value=None), \
                    mock.patch.object(update_check, "inside_app", return_value=False), \
                    mock.patch("subprocess.call") as call, redirect_stdout(io.StringIO()) as out:
                self.assertEqual(update_check.run_update(mock.Mock(check=check)), code)
            call.assert_not_called()
            self.assertIn("https://mobster.dev/download", out.getvalue())

    def test_the_apps_own_copy_still_defers_to_the_app(self):
        with mock.patch.object(update_check, "inside_app", return_value=True), \
                mock.patch.object(update_check, "latest_release") as latest, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(update_check.run_update(mock.Mock(check=False)), 0)
        latest.assert_not_called()
        self.assertIn("belongs to Mobster for Mac", out.getvalue())


@unittest.skipUnless((ROOT / "dashboard" / "src" / "lib" / "api.ts").is_file(), "the Mac app's source isn't here")
class VocabularyTests(unittest.TestCase):
    def test_status_labels_match_mobster_for_mac(self):
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
        with mock.patch.object(harness_api.hooks(), "cli", []), redirect_stderr(err), \
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


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for(predicate, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.05)
    return predicate()


class OrphanBuildTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.folder)], check=False))
        self.pidfile = self.folder / "child.pid"

    def script(self, body):
        prelude = f"""
            import sys, threading, time
            from pathlib import Path
            from types import SimpleNamespace
            from mobile_agent import devtools
            from mobile_agent.sim.wda import WebDriverAgent
            folder = Path({str(self.folder)!r})
            service = WebDriverAgent(folder)
            command = ["/bin/sh", "-c", "echo $$ > {self.pidfile}; exec sleep 300"]

            def build():
                service.run_logged(command, folder / "build.log", 600, None)
        """
        return textwrap.dedent(prelude) + textwrap.dedent(body)

    def start(self, body):
        errors = self.folder / "stderr.txt"
        with open(errors, "wb") as stream:
            process = subprocess.Popen([sys.executable, "-c", self.script(body)], cwd=ROOT,
                                       env={**os.environ, "PYTHONPATH": str(ROOT)},
                                       stdout=subprocess.DEVNULL, stderr=stream)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertTrue(wait_for(lambda: self.pidfile.is_file() and self.pidfile.read_text().strip()),
                        errors.read_text() or "no child pid")
        child = int(self.pidfile.read_text())
        self.addCleanup(lambda: alive(child) and os.killpg(child, signal.SIGKILL))
        return process, child

    def test_the_build_ends_with_the_process_whose_daemon_thread_ran_it(self):
        process, child = self.start("""
            threading.Thread(target=build, daemon=True).start()
            while not Path(%r).is_file():
                time.sleep(.02)
            time.sleep(.2)
            sys.exit(0)
        """ % str(self.pidfile))
        self.assertEqual(process.wait(15), 0)
        self.assertTrue(wait_for(lambda: not alive(child)), "xcodebuild's stand-in outlived Mobster")

    def test_sigterm_and_sighup_stop_sim_like_ctrl_c_and_exit_128_plus_the_signal(self):
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number.name):
                self.pidfile.unlink(missing_ok=True)
                process, child = self.start("""
                    class Module:
                        @staticmethod
                        def run(args):
                            thread = threading.Thread(target=build, daemon=True)
                            thread.start()
                            thread.join()
                            return 0
                    devtools.load = lambda name: Module
                    sys.exit(devtools.run(SimpleNamespace(command="sim")))
                """)
                process.send_signal(number)
                self.assertEqual(process.wait(15), 128 + number)
                self.assertTrue(wait_for(lambda: not alive(child)), "xcodebuild's stand-in outlived Mobster")


SLOW_RUN = """
import sys, time
import mobile_agent.__main__ as cli
from mobile_agent.tui import demo_phone
complete = demo_phone.DemoChat.complete
def slow(self, messages, schema, **k):
    if "actions" in (schema.get("properties") or {}):
        print("deciding", file=sys.stderr, flush=True)
        time.sleep(1.5)
    return complete(self, messages, schema, **k)
demo_phone.DemoChat.complete = slow
sys.exit(cli.main(["run", "Search for coffee", "--demo", "--json"]))
"""


class RunStopTests(unittest.TestCase):
    def stopped(self, signals):
        with tempfile.TemporaryDirectory() as home:
            process = subprocess.Popen([sys.executable, "-c", SLOW_RUN], cwd=ROOT,
                                       env={**os.environ, "PYTHONPATH": str(ROOT), "HOME": home,
                                            "MOBSTER_DATA_DIR": home, "TMPDIR": home},
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
            self.addCleanup(lambda: process.poll() is None and process.kill())
            line = b""
            while b"deciding" not in line:
                line = process.stderr.readline()
                self.assertTrue(line, "the run never reached a decision")
            for number in signals:
                process.send_signal(number)
                time.sleep(.1)
            out, err = process.communicate(timeout=30)
            process.stderr.close()
            process.stdout.close()
        return process.returncode, [json.loads(line) for line in out.decode().splitlines()], err.decode()

    def test_the_first_signal_stops_at_the_next_safe_point_with_a_result(self):
        for number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=number.name):
                returncode, events, err = self.stopped([number])
                self.assertEqual(returncode, code, err)
                self.assertEqual((events[-1]["event"], events[-1]["status"]), ("result", "stopped"))
                self.assertIn("stopping at the next safe point", err)

    def test_a_second_signal_forces_the_stop_and_still_prints_a_result(self):
        returncode, events, err = self.stopped([signal.SIGINT, signal.SIGINT])
        self.assertEqual(returncode, 130, err)
        self.assertEqual((events[-1]["event"], events[-1]["status"], events[-1]["exit_code"]),
                         ("result", "stopped", 130))


class TuiSignalTests(unittest.TestCase):
    def run_and_signal(self, number):
        import pty
        import select
        import termios
        home = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", home], check=False))
        pid, fd = pty.fork()
        if pid == 0:  # the child: the real `mobster --demo`, no phone, no keys
            os.environ.update(TERM="xterm-256color", COLUMNS="100", LINES="30", HOME=home, MOBSTER_DATA_DIR=home,
                              TMPDIR=home, PYTHONPATH=str(ROOT))
            for name in ("MOBSTER_WDA_URL", "MOBSTER_ENV_FILE", "MOBSTER_WDA_DEVICES"):
                os.environ.pop(name, None)
            os.chdir(ROOT)
            os.execv(sys.executable, [sys.executable, "-m", "mobile_agent", "--demo",
                                      "--wda-url", "http://127.0.0.1:59999"])
        self.addCleanup(os.close, fd)
        output = b""

        def pump(seconds, until=None):
            nonlocal output
            end = time.monotonic() + seconds
            while time.monotonic() < end and not (until and until in output):
                ready, _, _ = select.select([fd], [], [], .05)
                if ready:
                    try:
                        output += os.read(fd, 65536)
                    except OSError:
                        return
        pump(30, until=b"?1049h")
        self.assertIn(b"?1049h", output, "the UI never started")
        pump(1.0)
        os.kill(pid, number)
        pump(10, until=b"?1049l")
        pump(.3)
        end = time.monotonic() + 60
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            if time.monotonic() > end:
                os.kill(pid, signal.SIGKILL)
                _, status = os.waitpid(pid, 0)
                break
            pump(.1)
            time.sleep(.01)
        flags = termios.tcgetattr(fd)[3]
        return os.waitstatus_to_exitcode(status), flags, output

    def test_sigterm_and_sighup_restore_the_terminal(self):
        import termios
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number.name):
                code, flags, output = self.run_and_signal(number)
                self.assertEqual(code, 128 + number)
                self.assertTrue(flags & termios.ICANON and flags & termios.ECHO, "the terminal was left raw")
                for on, off in ((b"?1049h", b"?1049l"), (b"?1000h", b"?1000l"), (b"?2004h", b"?2004l"),
                                (b"?25l", b"?25h")):
                    if on in output:
                        self.assertGreater(output.rfind(off), output.rfind(on), off)


class SignalsAsInterruptTests(unittest.TestCase):
    def test_handlers_are_restored_and_mcp_keeps_its_own(self):
        from mobile_agent import devtools
        before = signal.getsignal(signal.SIGTERM)
        with devtools.signals_as_interrupt() as caught:
            self.assertIsNot(signal.getsignal(signal.SIGTERM), before)
            self.assertEqual(caught, [])
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        seen = []
        module = SimpleNamespace(run=lambda args: seen.append(signal.getsignal(signal.SIGTERM)) or 0)
        with mock.patch.object(devtools, "load", lambda name: module):
            self.assertEqual(devtools.run(SimpleNamespace(command="mcp")), 0)
        self.assertEqual(seen, [before])

    def test_stop_during_stderr_write_keeps_cancellation_and_second_signal(self):
        from mobile_agent.cli_run import stop_at_safe_point

        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number.name):
                stop, on_stop = threading.Event(), mock.Mock()

                class InterruptedStderr(io.RawIOBase):
                    def writable(self):
                        return True

                    def write(self, data):
                        if not stop.is_set():
                            signal.raise_signal(number)
                        return len(data)

                stderr = io.TextIOWrapper(io.BufferedWriter(InterruptedStderr()))
                with stderr, mock.patch.object(sys, "stderr", stderr), stop_at_safe_point(stop, on_stop) as caught:
                    print("deciding", file=stderr, flush=True)
                    self.assertTrue(stop.is_set())
                    self.assertEqual(caught, [number])
                    on_stop.assert_called_once_with()
                    with self.assertRaises(KeyboardInterrupt):
                        signal.raise_signal(number)
                    self.assertEqual(caught, [number, number])
                    on_stop.assert_called_once_with()


LATEST_JSON = ROOT / "desktop" / "scripts" / "latest-json.py"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class MinimumTests(unittest.TestCase):
    def test_the_feed_minimum_is_read_and_a_bad_answer_is_none(self):
        feed = lambda body: (lambda request, timeout: Response(json.dumps(body).encode()))
        self.assertEqual(update_check.minimum_version(opener=feed({"version": "0.4.0", "minimum_version": "v0.3.1"})),
                         "0.3.1")
        self.assertIsNone(update_check.minimum_version(opener=feed({"version": "0.4.0"})))
        self.assertIsNone(update_check.minimum_version(opener=feed(["not", "an", "object"])))

        def offline(request, timeout):
            raise OSError("offline")
        self.assertIsNone(update_check.minimum_version(opener=offline))

    def test_below_the_minimum_the_line_says_no_longer_supported(self):
        cache = {"latest": {"version": "0.4.0"}, "minimum": "0.3.1"}
        self.assertEqual(update_check.notice("0.3.0", cache),
                         "Mobster 0.3.0 is no longer supported. "
                         "Download Mobster for Mac at https://mobster.dev/download")
        self.assertTrue(update_check.unsupported("0.3.0", cache))
        self.assertFalse(update_check.unsupported("0.3.1", cache))
        self.assertTrue(update_check.notice("0.3.1", cache).startswith("Mobster 0.4.0 is out"))
        self.assertEqual(update_check.notice("0.4.0", cache), "")
        self.assertEqual(update_check.notice("0.3.0", {"minimum": "0.3.1"}),
                         "Mobster 0.3.0 is no longer supported. "
                         "Download Mobster for Mac at https://mobster.dev/download")

    def test_malformed_release_or_cache_never_breaks_a_command(self):
        for value in ([], None, "not an object"):
            with self.subTest(value=value):
                self.assertIsNone(update_check.latest_release(
                    opener=lambda request, timeout: Response(json.dumps(value).encode())))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "update-check.json"
            for checked in ("yesterday", [], {}, float("nan"), float("inf")):
                with self.subTest(checked=checked):
                    path.write_text(json.dumps({"checkedAt": checked, "latest": [], "minimum": {}}))
                    with mock.patch.object(update_check, "_cache_path", return_value=path):
                        cache = update_check._read_cache()
                    self.assertEqual(cache["checkedAt"], 0)
                    self.assertEqual(update_check.notice("0.3.0", cache), "")


class PendingNoticeTests(unittest.TestCase):
    def test_it_waits_for_the_fetch_then_words_the_cache(self):
        fetch = mock.Mock()
        cache = {"latest": {"version": "99.0.0"}}
        with mock.patch.object(update_check, "_fetch", fetch), mock.patch.object(update_check, "_read_cache",
                                                                                 return_value=cache):
            line = update_check.pending_notice("1.0.0", 5)
        fetch.join.assert_called_once_with(5)
        self.assertIn("Mobster 99.0.0 is out", line)

    def test_it_is_empty_when_nothing_is_newer(self):
        with mock.patch.object(update_check, "_fetch", None), mock.patch.object(update_check, "_read_cache",
                                                                                return_value={}):
            self.assertEqual(update_check.pending_notice("1.0.0", 5), "")


@unittest.skipUnless(LATEST_JSON.is_file(), "the desktop scripts are not part of this copy")
class FeedTests(unittest.TestCase):
    def write(self, minimum=None):
        version = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text())["version"]
        folder = Path(tempfile.mkdtemp())
        (folder / "app.sig").write_text("c2lnbmF0dXJl\n")
        env = {k: v for k, v in os.environ.items() if k != "MINIMUM_VERSION"}
        if minimum is not None:
            env["MINIMUM_VERSION"] = minimum
        subprocess.run([sys.executable, str(LATEST_JSON), f"v{version}", str(folder / "app.sig"),
                        str(folder / "latest.json")],
                       check=True, env=env, capture_output=True)
        return json.loads((folder / "latest.json").read_text())

    def test_an_ordinary_release_names_no_minimum(self):
        self.assertNotIn("minimum_version", self.write())
        self.assertNotIn("minimum_version", self.write(""))

    def test_a_release_can_set_the_minimum(self):
        self.assertEqual(self.write("v0.4.0")["minimum_version"], "0.4.0")


if __name__ == "__main__":
    unittest.main()

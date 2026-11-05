import io
import json
import os
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from mobile_agent import __main__ as cli
from mobile_agent import cli_parser

ROOT = Path(__file__).resolve().parents[2]
KEYS = ("TYPESAFE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TEXT_MODEL", "TEXT_MODEL_API_KEY",
        "MOBSTER_HELPER_PROVIDER",
        "MOBSTER_DEFAULT_ENGINE", "MOBSTER_WDA_URL", "MOBSTER_ENV_FILE")


def clean_env(**values):
    return {**{k: v for k, v in os.environ.items() if k not in KEYS}, **values}


def fake_wda(mode="ok"):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    seen = []
    if mode == "hangup":
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        stopping = threading.Event()

        def accept():
            while not stopping.is_set():
                try:
                    connection, _ = listener.accept()
                except OSError:
                    return
                seen.append("connect")
                connection.close()
        threading.Thread(target=accept, daemon=True).start()

        def stop():
            stopping.set()
            listener.close()
        return f"http://127.0.0.1:{listener.getsockname()[1]}", seen, stop

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            seen.append(f"{self.command} {self.path}")
            if mode == "404":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            value = {"ready": True, "os": {"version": "26.0"}} if self.path == "/status" else \
                False if self.path == "/wda/locked" else {}
            body = json.dumps({"value": value, "sessionId": "S1"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = reply

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop():
        server.shutdown()
        server.server_close()
    return f"http://127.0.0.1:{server.server_address[1]}", seen, stop


def run_main(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, clean_env(MOBSTER_FRAME_CLOCK="off", **(env or {})), clear=True), \
            redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exit:
            code = exit.code
    return code, out.getvalue(), err.getvalue()


class OptionsBeforeTheCommand(unittest.TestCase):

    def test_doctor_checks_the_phone_named_before_it(self):
        from mobile_agent import doctor
        with mock.patch.object(doctor, "run_checks", return_value=[]) as checks:
            code, _, _ = run_main(["--wda-url", "http://127.0.0.1:8200", "doctor", "--simulator", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(checks.call_args.kwargs["wda_url"], "http://127.0.0.1:8200")

    def test_a_terminal_ui_option_with_another_command_is_a_usage_error(self):
        for argv in (["--resume", "a1b2", "run", "x"], ["--app", "Messages", "doctor"], ["-c", "doctor"]):
            with self.subTest(argv=argv):
                code, out, err = run_main(argv)
                self.assertEqual(code, 2)
                self.assertIn("option of the terminal UI", err)
                self.assertEqual(out, "")


class FrameClockStream(unittest.TestCase):

    def test_a_port_with_no_pair_watches_no_stream(self):
        from mobile_agent.pool import WdaDeviceSpec, default_mjpeg_url
        from mobile_agent.wda.video import WdaVideo
        self.assertEqual(default_mjpeg_url("http://[::1]:8101"), "http://[::1]:9101")
        self.assertEqual(default_mjpeg_url("http://127.0.0.1:64535"), "http://127.0.0.1:65535")
        for wda in ("http://127.0.0.1:8080", "http://127.0.0.1:64731", "http://127.0.0.1", "http://127.0.0.1:99999",
                    "http://[::1]:80"):
            with self.subTest(wda=wda):
                self.assertIsNone(default_mjpeg_url(wda))
        self.assertIsNone(WdaVideo("http://127.0.0.1:8080").mjpeg_url)
        self.assertEqual(WdaDeviceSpec(id="a", wda_url="http://127.0.0.1:8080").mjpeg_url, "")
    def test_the_live_view_without_a_paired_stream_polls_screenshots(self):
        from mobile_agent.wda.video import WdaVideo
        video = WdaVideo("http://127.0.0.1:8080")
        self.addCleanup(video.close)
        streamed, shots = [], []
        video._stream_mjpeg = lambda: streamed.append(1)
        video._screenshot_once = lambda: (shots.append(1), video.close())[1] is None
        viewer = video.subscribe()
        video.worker.join(5)
        video.unsubscribe(viewer)
        self.assertEqual(streamed, [])
        self.assertTrue(shots)




class OnePhoneOneProcess(unittest.TestCase):

    def setUp(self):
        from mobile_agent import journal
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for patcher in (mock.patch.dict(os.environ, {"HOME": self.home.name}),
                        mock.patch.object(journal, "_LEASE_ROOT", None),
                        mock.patch.object(tempfile, "tempdir", temporary.name)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_same_phone_under_another_spelling_is_refused(self):
        from mobile_agent.journal import JournalError, Lease
        held = Lease.device("http://127.0.0.1:18399")
        self.addCleanup(held.close)
        for alias in ("http://localhost:18399", "http://[::ffff:127.0.0.1]:18399", "http://[::1]:18399",
                      "http://127.0.0.1:18399/", "HTTP://LOCALHOST:18399"):
            with self.subTest(alias=alias), self.assertRaises(JournalError):
                Lease.device(alias).close()
        Lease.device("http://127.0.0.1:18400").close()

    def test_the_lease_does_not_depend_on_tmpdir(self):
        from mobile_agent.journal import JournalError, Lease
        held = Lease.device("http://127.0.0.1:18399")
        self.addCleanup(held.close)
        with tempfile.TemporaryDirectory() as elsewhere, mock.patch("tempfile.gettempdir", return_value=elsewhere), \
                mock.patch.dict(os.environ, {"TMPDIR": elsewhere}), self.assertRaises(JournalError):
            Lease.device("http://127.0.0.1:18399").close()

    def legacy_lock(self, spelling):
        import hashlib

        from mobile_agent.journal import Lease
        directory = Path(tempfile.gettempdir()) / f"mobster-device-leases-{os.getuid()}"
        directory.mkdir(mode=0o700, exist_ok=True)
        return Lease(directory / (hashlib.sha256(spelling.encode()).hexdigest() + ".lock"))

    def test_an_earlier_builds_lock_is_honoured(self):
        from mobile_agent.journal import Lease, LeaseHeld
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with mock.patch.object(tempfile, "tempdir", temporary.name):
            old = self.legacy_lock("http://127.0.0.1:18401")
            with self.assertRaises(LeaseHeld):
                Lease.device("http://127.0.0.1:18401/")
            old.close()
            lease = Lease.device("http://127.0.0.1:18401/")
            for spelling in ("http://127.0.0.1:18401", "http://127.0.0.1:18401/"):
                with self.subTest(spelling=spelling), self.assertRaises(LeaseHeld):
                    self.legacy_lock(spelling)
            lease.close()
            self.legacy_lock("http://127.0.0.1:18401").close()

    def test_a_temporary_folder_that_is_not_private_is_skipped(self):
        from mobile_agent.journal import Lease
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        shared = Path(temporary.name) / f"mobster-device-leases-{os.getuid()}"
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        with mock.patch.object(tempfile, "tempdir", temporary.name):
            lease = Lease.device("http://127.0.0.1:18402")
            self.assertEqual(lease.legacy, [])
            lease.close()

    def test_run_says_the_phone_is_in_use(self):
        from mobile_agent.journal import Lease
        url, seen, stop = fake_wda()
        self.addCleanup(stop)
        held = Lease.device(url.replace("127.0.0.1", "localhost"))
        self.addCleanup(held.close)
        with mock.patch("mobile_agent.engines.acquire_client", side_effect=RuntimeError("the lease was granted")):
            code, out, _ = run_main(["run", "Open Search", "--execute", "--wda-url", url],
                                    {"HOME": self.home.name, "ANTHROPIC_API_KEY": "sk-ant-test-0000"})
        self.assertEqual(code, 3)
        error = json.loads(out)
        self.assertEqual(error["error_type"], "NoDevice")
        self.assertIn("busy in another Mobster process", error["error"])
        self.assertFalse([request for request in seen if request.startswith("POST")])


MOBSTER_HOLDS = """
import sys
from mobile_agent import journal
assert journal._LEASE_ROOT is None, journal._LEASE_ROOT
lease = journal.Lease.device(sys.argv[1])
print("held", flush=True)
sys.stdin.read()
lease.close()
"""


class TestLeasesStayApart(unittest.TestCase):

    URL = "http://127.0.0.1:18404"

    def setUp(self):
        home, temporary = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        for folder in (home, temporary):
            self.addCleanup(folder.cleanup)
        self.temporary = temporary.name
        for patcher in (mock.patch.dict(os.environ, {"HOME": home.name}),
                        mock.patch.object(tempfile, "tempdir", temporary.name)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def mobster(self):
        import subprocess
        import sys
        process = subprocess.Popen([sys.executable, "-c", MOBSTER_HOLDS, self.URL],
                                   cwd=Path(__file__).resolve().parents[2],
                                   env={**os.environ, "TMPDIR": self.temporary}, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def stop():
            try:
                process.communicate("", timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        self.addCleanup(stop)
        line = process.stdout.readline()
        if line.strip() != "held":
            process.kill()
            self.fail(f"the other process never held the lease: {process.communicate()[1]}")
        return process

    def lock_names(self):
        import hashlib

        from mobile_agent.journal import device_identity
        return (hashlib.sha256(device_identity(self.URL).encode()).hexdigest() + ".lock",
                hashlib.sha256(self.URL.encode()).hexdigest() + ".lock")

    def test_a_phone_mobster_holds_does_not_refuse_a_test(self):
        from mobile_agent import journal, tests
        from mobile_agent.journal import Lease, LeaseHeld
        from mobile_agent.paths import machine_data_dir
        self.mobster()
        data, legacy = self.lock_names()
        for path in (machine_data_dir() / "device-leases" / data,
                     Path(self.temporary) / f"mobster-device-leases-{os.getuid()}" / legacy):
            with self.subTest(folder=path.parent), self.assertRaises(LeaseHeld):
                Lease(path).close()
        ours = Lease.device(self.URL, wait=0)
        self.addCleanup(ours.close)
        self.assertEqual(journal._LEASE_ROOT, tests.LEASES)
        self.assertEqual(len(ours.legacy), 1)
        with self.assertRaises(LeaseHeld):
            Lease.device(self.URL, wait=0)
        with mock.patch.object(journal, "_LEASE_ROOT", None), self.assertRaises(LeaseHeld):
            Lease.device(self.URL, wait=0)

    def test_a_tests_lease_does_not_keep_mobster_off_the_phone(self):
        from mobile_agent.journal import Lease
        ours = Lease.device(self.URL, wait=0)
        self.addCleanup(ours.close)
        self.mobster()


class TestLeaseRefusesWhatIsNotPrivate(unittest.TestCase):

    URL = "http://127.0.0.1:18405"

    def folder(self):
        from mobile_agent import journal
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        patcher = mock.patch.object(journal, "_LEASE_ROOT", root.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        return Path(root.name) / "device-leases"

    def test_a_lease_folder_others_can_read_is_refused_and_takes_no_lock(self):
        from mobile_agent.journal import JournalError, Lease
        leases = self.folder()
        leases.mkdir(mode=0o700)
        os.chmod(leases, 0o755)
        with self.assertRaisesRegex(JournalError, "must be private and owned by this user"):
            Lease.device(self.URL, wait=0)
        self.assertEqual(list(leases.iterdir()), [])

    def test_a_lock_that_is_a_symlink_is_refused(self):
        from mobile_agent.journal import JournalError, Lease
        leases = self.folder()
        leases.mkdir(mode=0o700)
        target = leases / "elsewhere"
        target.write_text("")
        for name in self.lock_names():
            (leases / name).symlink_to(target)
        with self.assertRaisesRegex(JournalError, "Cannot acquire the private Mobster lease"):
            Lease.device(self.URL, wait=0)

    def test_a_lock_that_is_not_a_regular_file_is_refused(self):
        from mobile_agent.journal import JournalError, Lease
        leases = self.folder()
        leases.mkdir(mode=0o700)
        for name in self.lock_names():
            os.mkfifo(leases / name)
        with self.assertRaisesRegex(JournalError, "Lock file has an unexpected owner or type"):
            Lease.device(self.URL, wait=0)

    def lock_names(self):
        import hashlib

        from mobile_agent.journal import device_identity
        return [hashlib.sha256(device_identity(self.URL).encode()).hexdigest() + ".lock"]


class SmartOnlyKeys(unittest.TestCase):

    def test_an_openai_key_is_enough(self):
        from mobile_agent import doctor
        with mock.patch.dict(os.environ, clean_env(OPENAI_API_KEY="sk-proj-test-0000"), clear=True):
            key = doctor.check_key()
        self.assertEqual(key.state, doctor.OK)
        self.assertTrue(key.detail.startswith("OpenAI (sk-proj-…0000)"), key.detail)
        self.assertNotIn("TYPESAFE", key.detail)

    def test_a_jev_key_alone_is_no_key_and_the_phone_is_never_touched(self):
        url, seen, stop = fake_wda()
        self.addCleanup(stop)
        code, out, _ = run_main(["run", "Open Search", "--execute", "--wda-url", url],
                                {"TYPESAFE_API_KEY": "test-0000"})
        self.assertEqual(code, 3)
        error = json.loads(out)
        self.assertEqual(error["error_type"], "MissingKey")
        self.assertIn("mobster login", error["error"])
        self.assertEqual(seen, [], "run talked to WDA before finding the key missing")

    def test_smart_runs_on_a_claude_key_and_has_no_preview(self):
        url, seen, stop = fake_wda()
        self.addCleanup(stop)
        code, out, _ = run_main(["run", "Open Search", "--wda-url", url], {"ANTHROPIC_API_KEY": "sk-ant-test-0000"})
        self.assertEqual(code, 2)
        self.assertIn("nothing to preview", json.loads(out)["error"])
        self.assertEqual(seen, [], "run talked to WDA for a task it couldn't run")

    def test_remember_that_runs_nothing(self):
        url, seen, stop = fake_wda()
        self.addCleanup(stop)
        code, out, _ = run_main(["run", "Remember that my gym is Equinox on 5th Street", "--execute", "--wda-url", url],
                                {"ANTHROPIC_API_KEY": "sk-ant-test-0000"})
        self.assertEqual(code, 2)
        result = json.loads(out)
        self.assertEqual(result["routed"], "remember")
        self.assertIn('mobster memory add "', result["error"])
        self.assertEqual(seen, [])

    def test_no_key_at_all_is_couldnt_run(self):
        code, out, _ = run_main(["run", "Open Search", "--execute"], {})
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["error"], "no model key yet. Run `mobster login`, then run this again.")

class ExitCodes(unittest.TestCase):

    KEY = {"ANTHROPIC_API_KEY": "sk-ant-test-0000"}

    def no_phone(self, url):
        with mock.patch("mobile_agent.engines.acquire_client", side_effect=AssertionError("a model was called")):
            code, out, _ = run_main(["run", "Open Search", "--execute", "--wda-url", url], self.KEY)
        self.assertEqual((code, json.loads(out)["error_type"]), (3, "NoDevice"))

    def test_a_socket_that_hangs_up_is_no_phone(self):
        url, _, stop = fake_wda("hangup")
        self.addCleanup(stop)
        self.no_phone(url)

    def test_a_server_that_is_not_wda_is_no_phone(self):
        url, _, stop = fake_wda("404")
        self.addCleanup(stop)
        self.no_phone(url)

    def test_a_host_that_does_not_resolve_is_no_phone(self):
        from mobile_agent.transport import TransportError
        with mock.patch("mobile_agent.drivers.resolve_wda_session",
                        side_effect=TransportError("HTTP gaierror; request outcome unknown; not retried")):
            self.no_phone("http://nonexistent.invalid:8100")

    def test_invalid_flags_are_usage_errors(self):
        for argv in (["run", "x", "--spend-cap-usd", "-1"], ["run", "x", "--spend-cap-usd", "nan"],
                     ["run", "x", "--wda-url", "127.0.0.1:9"], ["screen", "--width", "0"]):
            with self.subTest(argv=argv):
                code, out, _ = run_main(argv, self.KEY)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
        code, _, err = run_main(["run", "x"], {"MOBSTER_WDA_URL": "user:secret@127.0.0.1:8100"})
        self.assertEqual(code, 2)
        self.assertIn("$MOBSTER_WDA_URL", err)
        self.assertNotIn("secret", err)


class ServeHelpAndPort(unittest.TestCase):

    def test_help_describes_what_serve_does(self):
        parser = cli_parser.build_parser()
        help_text = next(a for a in parser._actions if a.dest == "command").choices["serve"].format_help()
        self.assertNotIn("$MOBSTER_WDA_URL", help_text)
        self.assertNotIn("docs/api.md", help_text)
        self.assertIn("https://docs.mobster.dev/scripting", help_text)
        with mock.patch.dict(os.environ, {"MOBSTER_WDA_URL": "http://127.0.0.1:8200"}), \
                mock.patch("mobile_agent.serve.serve") as serve:
            cli.main(["serve", "--port", "0", "--state-db", os.path.join(tempfile.mkdtemp(), "m.sqlite3")])
        self.assertIsNone(serve.call_args.args[0].wda_url)

    def test_port_zero_prints_the_port_it_listens_on(self):
        from mobile_agent import serve as serve_module
        from mobile_agent import server
        from mobile_agent.tui.session import serve_config
        with tempfile.TemporaryDirectory() as root:
            config = serve_config(port=0, state_db=Path(root, "m.sqlite3"))
            out = io.StringIO()
            with mock.patch.object(server.BoundedServer, "serve_forever", lambda self, *a, **k: None), \
                    mock.patch.object(serve_module, "token_file", return_value=None), redirect_stdout(out):
                serve_module.serve(config)
        port = int(json.loads(out.getvalue())["url"].rsplit(":", 1)[1])
        self.assertGreater(port, 0)
        self.assertEqual(config.port, port)


class MalformedEnvFile(unittest.TestCase):

    def env_file(self, data):
        folder = tempfile.mkdtemp()
        path = Path(folder, "keys.env")
        path.write_bytes(data)
        return path

    def test_run_and_doctor_still_answer_in_json(self):
        path = self.env_file(b"MY-KEY=1\nsk-proj-Zm9vYmFy/secretpart==\nANTHROPIC_API_KEY=sk-ant-test-0000\n")
        with mock.patch("mobile_agent.engines.acquire_client", side_effect=AssertionError("a model was called")):
            code, out, err = run_main(["run", "Open Search", "--execute", "--json", "--wda-url", "http://127.0.0.1:9",
                                       "--env-file", str(path)])
        self.assertEqual((code, json.loads(out)["error_type"]), (3, "NoDevice"))
        self.assertNotIn("Traceback", err)
        self.assertIn("keys.env line 1", err)
        self.assertNotIn("MY-KEY", err)
        self.assertNotIn("secretpart", err)
        from mobile_agent import doctor
        with mock.patch.object(doctor, "run_checks", return_value=[]):
            code, out, err = run_main(["doctor", "--json", "--simulator", "--env-file", str(path)])
        self.assertEqual(json.loads(out), {"ok": True, "checks": []})

    def test_a_folder_or_binary_file_is_a_warning(self):
        for path in (Path(tempfile.mkdtemp()), self.env_file(b"A=\xff\xfe\n")):
            with self.subTest(path=path.name):
                code, out, err = run_main(["run", "x", "--json", "--wda-url", "http://127.0.0.1:9",
                                           "--env-file", str(path)])
                self.assertEqual(json.loads(out)["error_type"], "MissingKey")
                self.assertNotIn("Traceback", err)

    def test_serve_starts(self):
        path = self.env_file(b"TYPESAFE_API_KEY=test\n# pasted by mistake:\nsk-proj-Zm9vYmFy/secretpart==\n")
        with mock.patch("mobile_agent.serve.serve") as serve:
            code, _, err = run_main(["serve", "--port", "0", "--env-file", str(path),
                                     "--state-db", os.path.join(tempfile.mkdtemp(), "m.sqlite3")])
        serve.assert_called_once()
        self.assertEqual(code, 0)
        self.assertIn("line 3", err)
        self.assertNotIn("secretpart", err)


class TildeEnvFile(unittest.TestCase):

    def test_a_session_reads_and_saves_the_home_folder_file(self):
        from mobile_agent.tui.session import Session
        home, cwd = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (home / "keys.env").write_text("TYPESAFE_API_KEY=from-home-file-0000\n")
        old = os.getcwd()
        try:
            os.chdir(cwd)
            with mock.patch.dict(os.environ, clean_env(HOME=str(home)), clear=True):
                os.environ.pop("MOBSTER_ASK_BEFORE_ACTING", None)
                session = Session(wda_url="http://127.0.0.1:9", env_file="~/keys.env",
                                  history_db=str(cwd / "t.sqlite3"))
                loaded = os.environ.get("TYPESAFE_API_KEY")
                session.set_setting("askBeforeActing", False)
                session.close(2)
        finally:
            os.chdir(old)
        self.assertEqual(loaded, "from-home-file-0000")
        self.assertEqual([p.name for p in cwd.iterdir() if "sqlite3" not in p.name], [])
        self.assertIn("MOBSTER_ASK_BEFORE_ACTING=0", (home / "keys.env").read_text())


class TerminalOrigin(unittest.TestCase):

    def test_a_terminal_task_is_a_persons_task(self):
        from mobile_agent import harness_api
        from mobile_agent.memory import provider
        from mobile_agent.tui.session import Session
        for origin in ("tui", "cli"):
            with self.subTest(origin=origin):
                session = Session(demo=True, pace=0, origin=origin) if origin != "tui" else Session(demo=True, pace=0)
                self.addCleanup(session.close, 2)
                app = session.apps()[0]["id"]
                run = session.start(app, "Turn on Dark Mode")
                self.assertEqual(run.origin, origin)
                self.assertIn(run.origin, harness_api.INTERACTIVE)
                self.assertIn(run.origin, provider.ORIGINS)
                session.stop(run)


class SecondTerminal(unittest.TestCase):

    def test_it_says_another_terminal_is_open(self):
        from mobile_agent.tui.session import Session, error_text
        history = os.path.join(tempfile.mkdtemp(), "terminal.sqlite3")
        first = Session(wda_url="http://127.0.0.1:9", history_db=history)
        self.addCleanup(first.close, 2)
        with self.assertRaises(Exception) as caught:
            Session(wda_url="http://127.0.0.1:8", history_db=history)
        message = error_text(caught.exception)
        self.assertIn("Another Mobster has this task history open", message)
        self.assertNotIn("not reset", message)

    def test_a_journal_in_use_is_not_reported_as_damaged(self):
        from mobile_agent import journal
        path = os.path.join(tempfile.mkdtemp(), "m.sqlite3")
        first = journal.Journal(path)
        self.addCleanup(first.close)
        with self.assertRaises(journal.JournalError) as caught:
            journal.Journal(path)
        self.assertNotIn("not reset", str(caught.exception))
        self.assertIsInstance(caught.exception, journal.LeaseHeld)


# `mobster run` on the scripted phone and model in a process of its own, killed while its first action is on the way
# to the phone: the action never comes back. The scripted phone shows one app, so the task starts in Settings.
KILLED_MID_ACTION = """
import sys, time
from pathlib import Path
import mobile_agent.tests  # this process's own HOME and device leases, and no key
from unittest import mock
from mobile_agent import __main__ as cli, serve
from mobile_agent.tui import demo_phone
from mobile_agent.tui.session import DemoRuntime


class ScriptedPhone(DemoRuntime):
    pace = 0

    def create(self, app_id, goal, mode, **kwargs):
        return super().create(app_id or "settings", goal, mode, **kwargs)


def sent(self, operation, *args, **kwargs):
    Path(sys.argv[2]).write_text(operation)
    time.sleep(600)


demo_phone.DemoPhone.execute = sent
serve.runtime_class_for = lambda config, base: ScriptedPhone
mock.patch("mobile_agent.engines.smart_key", return_value="scripted").start()
sys.exit(cli.main(["run", "Turn on Dark Mode", "--execute", "--json", "--wda-url", "http://127.0.0.1:9",
                   "--state-db", sys.argv[1]]))
"""


class RunJournal(unittest.TestCase):
    """`mobster run` kept its events in memory: a crash mid-action left no record of what it may have sent. It saves
    them in the terminal's journal before each action goes out, as the terminal UI and the Mac app do."""

    def test_a_run_killed_mid_action_is_saved_and_marked_interrupted_at_the_next_start(self):
        import shutil
        import subprocess
        import sys
        import time

        from mobile_agent.history import read_history
        from mobile_agent.tui.session import Session
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        journal, sent, errors = folder / "terminal.sqlite3", folder / "sent", folder / "stderr.txt"
        with open(errors, "wb") as stream:
            process = subprocess.Popen([sys.executable, "-c", KILLED_MID_ACTION, str(journal), str(sent)],
                                       cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT), "TMPDIR": str(folder)},
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stream)
        def stop_child():
            if process.poll() is None:
                process.kill()
            process.wait(10)
        self.addCleanup(stop_child)
        deadline = time.monotonic() + 60
        while not sent.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(sent.exists(), errors.read_text())
        process.kill()
        process.wait(10)
        [saved] = read_history(journal)
        self.assertEqual((saved["status"], saved["origin"]), ("running", "cli"))
        session = Session(wda_url="http://127.0.0.1:9", history_db=journal, origin="cli")
        self.addCleanup(session.close, 2)
        [run] = session.history()
        self.assertEqual(run.status, "interrupted")
        self.assertTrue(run.summary["actions_may_have_run"])
        self.assertIn(sent.read_text(), next(e["ops"] for e in run.events if e["event"] == "frontier_decision"))

    def test_a_task_history_held_by_another_mobster_refuses_before_the_phone(self):
        from mobile_agent.journal import Journal
        from mobile_agent.paths import default_history_db
        url, seen, stop = fake_wda()
        self.addCleanup(stop)
        held = Journal(default_history_db())  # the terminal UI, or another `mobster run`
        self.addCleanup(held.close)
        with mock.patch("mobile_agent.engines.acquire_client", side_effect=AssertionError("a model was called")):
            code, out, _ = run_main(["run", "Open Search", "--execute", "--wda-url", url],
                                    {"ANTHROPIC_API_KEY": "sk-ant-test-0000"})
        self.assertEqual(code, 3)
        error = json.loads(out)
        self.assertEqual((error["event"], error["error_type"], error["exit_code"]), ("result", "LeaseHeld", 3))
        self.assertIn("Another Mobster has this task history open", error["error"])
        self.assertEqual(seen, [], "run reached the phone while its task history was held")

    def test_the_demo_keeps_its_task_in_memory(self):
        from mobile_agent.journal import Journal
        from mobile_agent.paths import default_history_db
        held = Journal(default_history_db())
        self.addCleanup(held.close)
        code, out, _ = run_main(["run", "Turn on Dark Mode", "--demo", "--json"], {"MOBSTER_DEMO_PACE": "0"})
        self.assertEqual((code, json.loads(out.splitlines()[-1])["status"]), (0, "completed"))
        self.assertEqual(held.load(), [])


class Terminal(io.StringIO):
    def isatty(self):
        return True


class DeviceErrorsFollowTheTerminal(unittest.TestCase):

    def main(self, *argv, tty):
        out, err = (Terminal() if tty else io.StringIO()), io.StringIO()
        with mock.patch.dict(os.environ, clean_env(MOBSTER_NO_DEVICES="1", MOBSTER_NO_UPDATE_CHECK="1"), clear=True), \
                mock.patch("mobile_agent.tls.ensure_ca_bundle"), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_devices_status_on_a_terminal_says_it_on_stderr(self):
        code, out, err = self.main("devices", "status", "--device", "nope", tty=True)
        self.assertEqual((code, out), (3, ""))
        self.assertTrue(err.startswith("mobster: No device is named or has the id “nope”"), err)

    def test_devices_status_piped_prints_the_json_object(self):
        code, out, err = self.main("devices", "status", "--device", "nope", tty=False)
        self.assertEqual((code, err), (3, ""))
        self.assertEqual(json.loads(out)["error_type"], "NoDevice")

    def test_json_on_a_terminal_still_prints_the_json_object(self):
        code, out, err = self.main("devices", "status", "--device", "nope", "--json", tty=True)
        self.assertEqual((code, err), (3, ""))
        self.assertFalse(json.loads(out)["ok"])


class DoctorLockedTests(unittest.TestCase):
    def check(self, value):
        from mobile_agent import doctor

        class Client:
            def __init__(self, url):
                pass

            def request(self, *args, **kwargs):
                return {"value": value}

            def close(self):
                pass

        with mock.patch("mobile_agent.transport.HTTP", Client):
            return doctor.check_locked("http://127.0.0.1:8100")

    def test_an_unreadable_answer_is_not_reported_as_unlocked(self):
        from mobile_agent import doctor
        for value in (None, "no", 0):
            with self.subTest(value=value):
                self.assertEqual(self.check(value).state, doctor.SKIP)
        self.assertEqual(self.check(False).state, doctor.OK)
        self.assertEqual(self.check(True).state, doctor.FAIL)


if __name__ == "__main__":
    unittest.main()

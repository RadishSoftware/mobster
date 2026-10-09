"""The command line's bug sweep (CLI-n): each test failed before its fix.

No device and no network: WebDriverAgent is a loopback fake on an ephemeral port (or a
socket that hangs up, as iproxy does with no phone behind it), the MJPEG stream is a
recorder, and no model is ever called.
"""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock

from mobile_agent import __main__ as cli

KEYS = ("TYPESAFE_API_KEY", "OPENAI_API_KEY", "TEXT_MODEL", "TEXT_MODEL_API_KEY", "MOBSTER_HELPER_PROVIDER",
        "MOBSTER_DEFAULT_ENGINE", "MOBSTER_WDA_URL", "MOBSTER_ENV_FILE")


def clean_env(**values):
    """os.environ without model keys or Mobster defaults, plus ``values`` (for mock.patch.dict(clear=True))."""
    return {**{k: v for k, v in os.environ.items() if k not in KEYS}, **values}


def fake_wda(mode="ok"):
    """A loopback stand-in for WebDriverAgent. ``ok`` answers like WDA, ``404`` like some other server,
    ``hangup`` accepts and closes (iproxy with no phone). Returns (url, requests seen, stop)."""
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
    """``mobster ARGV`` piped: (exit code, stdout, stderr). A usage error's SystemExit becomes its code."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, clean_env(MOBSTER_FRAME_CLOCK="off", **(env or {})), clear=True), \
            redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exit:
            code = exit.code
    return code, out.getvalue(), err.getvalue()


class OptionsBeforeTheCommand(unittest.TestCase):
    """CLI-1: `mobster --wda-url URL run …` drove the default phone; --env-file and --demo were dropped too."""

    def test_wda_url_before_run_doctor_or_screen_is_the_phone_they_drive(self):
        for command in (["run", "Delete the draft", "--execute"], ["doctor"], ["screen"]):
            with self.subTest(command=command[0]), mock.patch.dict(os.environ, clean_env(), clear=True):
                args = cli.build_parser().parse_args(["--wda-url", "http://127.0.0.1:8200", *command])
                self.assertEqual(cli.wda_url_for(args), "http://127.0.0.1:8200")

    def test_the_command_still_takes_its_own_wda_url(self):
        args = cli.build_parser().parse_args(["run", "Open Search", "--wda-url", "http://127.0.0.1:8203"])
        self.assertEqual(args.wda_url, "http://127.0.0.1:8203")
        self.assertIsNone(cli.build_parser().parse_args(["run", "Open Search"]).wda_url)

    def test_env_file_before_the_command_is_loaded(self):
        self.assertEqual(cli.build_parser().parse_args(["--env-file", "keys.env", "doctor"]).env_file,
                         Path("keys.env"))

    def test_doctor_checks_the_phone_named_before_it(self):
        from mobile_agent import doctor
        with mock.patch.object(doctor, "run_checks", return_value=[]) as checks:
            code, _, _ = run_main(["--wda-url", "http://127.0.0.1:8200", "doctor", "--simulator", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(checks.call_args.kwargs["wda_url"], "http://127.0.0.1:8200")

    def test_a_terminal_ui_option_with_another_command_is_a_usage_error(self):
        # (`mobster --demo run …` is fine now: `run` has its own --demo, the scripted phone.)
        for argv in (["--resume", "a1b2", "run", "x"], ["--app", "Messages", "doctor"], ["-c", "doctor"]):
            with self.subTest(argv=argv):
                code, out, err = run_main(argv)
                self.assertEqual(code, 2)
                self.assertIn("option of the terminal UI", err)
                self.assertEqual(out, "")

    def test_the_terminal_ui_takes_its_options_on_either_side_of_tui(self):
        self.assertTrue(cli.build_parser().parse_args(["--demo", "tui"]).demo)
        self.assertTrue(cli.build_parser().parse_args(["tui", "--demo"]).demo)
        self.assertEqual(cli.build_parser().parse_args(["--app", "Messages", "tui"]).app, "Messages")


class RecordingVideo:
    """Stands in for wda_video.WdaVideo: records the stream it is given, opens nothing. Closed from the start,
    so the frame clock's worker ends at its first empty read instead of polling for the rest of the suite."""
    opened = []
    closed = True

    def __init__(self, wda_url, mjpeg_url=None, **kwargs):
        RecordingVideo.opened.append(mjpeg_url)

    def subscribe(self):
        return object()

    def unsubscribe(self, viewer):
        pass

    def next_frame(self, *args, **kwargs):
        return None

    def close(self):
        pass


class StubDriver:
    frame_clock = None
    frame_clock_mode = None

    def close(self):
        pass


class FrameClockStream(unittest.TestCase):
    """CLI-2: `run --execute` on a simulator's WDA (8203) settled on the USB iPhone's stream (9100)."""

    def test_run_execute_watches_the_stream_paired_with_its_wda(self):
        from mobile_agent import frame_clock
        if not frame_clock.available():
            self.skipTest("Pillow is not installed")
        RecordingVideo.opened.clear()
        args = argparse.Namespace(goal="Open Search", execute=True, helper=False, json=True, max_steps=3,
                                  max_seconds=5, spend_cap_usd=None, expected_text=None, allow_app=None)
        wda = "http://127.0.0.1:8203"
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, clean_env(HOME=home, MOBSTER_FRAME_CLOCK="on"), clear=True), \
                mock.patch("mobile_agent.wda_video.WdaVideo", RecordingVideo), \
                mock.patch("mobile_agent.compose.build_models", return_value=(mock.Mock(), None)), \
                mock.patch("mobile_agent.compose.build_target_driver", return_value=StubDriver()), \
                mock.patch("mobile_agent.agent.Agent", side_effect=RuntimeError("stop before the loop")), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                cli.run_task(args, lease_key=wda, target={"wda_url": wda, "session": "S"})
        self.assertEqual(RecordingVideo.opened, ["http://127.0.0.1:9203"])

    def test_a_video_given_no_stream_pairs_it_with_its_wda_port(self):
        from mobile_agent.wda_video import WdaVideo
        self.assertEqual(WdaVideo("http://127.0.0.1:8203").mjpeg_url, "http://127.0.0.1:9203")
        self.assertEqual(WdaVideo("http://127.0.0.1:8100").mjpeg_url, "http://127.0.0.1:9100")
        self.assertEqual(WdaVideo("http://127.0.0.1:8100", mjpeg_url="http://127.0.0.1:9999/").mjpeg_url,
                         "http://127.0.0.1:9999")

    def test_a_port_with_no_pair_watches_no_stream(self):
        """8080 fell back to 9100, the USB iPhone's stream; 64731 gave port 65731; [::1] lost its brackets."""
        from mobile_agent.pool import WdaDeviceSpec, default_mjpeg_url
        from mobile_agent.wda_video import WdaVideo
        self.assertEqual(default_mjpeg_url("http://[::1]:8101"), "http://[::1]:9101")
        self.assertEqual(default_mjpeg_url("http://127.0.0.1:64535"), "http://127.0.0.1:65535")
        for wda in ("http://127.0.0.1:8080", "http://127.0.0.1:64731", "http://127.0.0.1", "http://127.0.0.1:99999",
                    "http://[::1]:80"):
            with self.subTest(wda=wda):
                self.assertIsNone(default_mjpeg_url(wda))
        self.assertIsNone(WdaVideo("http://127.0.0.1:8080").mjpeg_url)
        self.assertEqual(WdaDeviceSpec(id="a", wda_url="http://127.0.0.1:8080").mjpeg_url, "")

    def test_run_execute_on_a_port_with_no_pair_settles_by_time(self):
        from mobile_agent import frame_clock
        if not frame_clock.available():
            self.skipTest("Pillow is not installed")
        RecordingVideo.opened.clear()
        args = argparse.Namespace(goal="Open Search", execute=True, helper=False, json=True, max_steps=3,
                                  max_seconds=5, spend_cap_usd=None, expected_text=None, allow_app=None)
        wda, driver = "http://127.0.0.1:8080", StubDriver()
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, clean_env(HOME=home, MOBSTER_FRAME_CLOCK="on"), clear=True), \
                mock.patch("mobile_agent.wda_video.WdaVideo", RecordingVideo), \
                mock.patch("mobile_agent.compose.build_models", return_value=(mock.Mock(), None)), \
                mock.patch("mobile_agent.compose.build_target_driver", return_value=driver), \
                mock.patch("mobile_agent.agent.Agent", side_effect=RuntimeError("stop before the loop")), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                cli.run_task(args, lease_key=wda, target={"wda_url": wda, "session": "S"})
        self.assertEqual(RecordingVideo.opened, [])
        self.assertIsNone(driver.frame_clock)

    def test_the_live_view_without_a_paired_stream_polls_screenshots(self):
        from mobile_agent.wda_video import WdaVideo
        video = WdaVideo("http://127.0.0.1:8080")
        streamed, shots = [], []
        video._stream_mjpeg = lambda: streamed.append(1)
        video._screenshot_once = lambda: (shots.append(1), video.close())[1] is None
        viewer = video.subscribe()
        video.worker.join(5)
        video.unsubscribe(viewer)
        self.assertEqual(streamed, [])
        self.assertTrue(shots)


class OnePhoneOneProcess(unittest.TestCase):
    """CLI-3: the device lease hashed the URL as spelled, under $TMPDIR, so a second process could drive the
    same phone by writing localhost, or by running where TMPDIR differs (ssh, cron, launchd)."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
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
        Lease.device("http://127.0.0.1:18400").close()  # another phone is free

    def test_the_lease_does_not_depend_on_tmpdir(self):
        from mobile_agent.journal import JournalError, Lease
        held = Lease.device("http://127.0.0.1:18399")
        self.addCleanup(held.close)
        with tempfile.TemporaryDirectory() as elsewhere, mock.patch("tempfile.gettempdir", return_value=elsewhere), \
                mock.patch.dict(os.environ, {"TMPDIR": elsewhere}), self.assertRaises(JournalError):
            Lease.device("http://127.0.0.1:18399").close()

    def legacy_lock(self, spelling):
        """The lock a build up to 0.1.0 takes for this address: $TMPDIR, the address as spelled."""
        import hashlib
        from mobile_agent.journal import Lease
        directory = Path(tempfile.gettempdir()) / f"mobster-device-leases-{os.getuid()}"
        directory.mkdir(mode=0o700, exist_ok=True)
        return Lease(directory / (hashlib.sha256(spelling.encode()).hexdigest() + ".lock"))

    def test_an_earlier_builds_lock_is_honoured(self):
        """A Mac app still on 0.1.0 holds only the temporary folder's lock: a new `run` must not drive the phone
        beside it, and must leave the new lock free when it backs off."""
        from mobile_agent.journal import LeaseHeld, Lease
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with mock.patch.object(tempfile, "tempdir", temporary.name):
            old = self.legacy_lock("http://127.0.0.1:18401")  # the older app, as it spells the address
            with self.assertRaises(LeaseHeld):
                Lease.device("http://127.0.0.1:18401/")
            old.close()
            lease = Lease.device("http://127.0.0.1:18401/")  # free again, and the new lock was released
            # ...and while it runs, an older build is kept off the phone too, under either spelling.
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
        # Never a model call, even if the lease were wrongly granted.
        with mock.patch("mobile_agent.agent.Agent", side_effect=RuntimeError("the lease was granted")):
            code, out, _ = run_main(["run", "Open Search", "--execute", "--wda-url", url],
                                    {"HOME": self.home.name, "TYPESAFE_API_KEY": "test-0000"})
        self.assertEqual(code, 1)
        error = json.loads(out)
        self.assertEqual(error["error_type"], "DeviceBusy")
        self.assertIn("using this phone", error["error"])
        self.assertEqual(seen, [])


class SmartOnlyKeys(unittest.TestCase):
    """CLI-4: with only an OpenAI key, doctor said ready while `mobster run` failed (after it had talked to WDA)."""

    def test_an_openai_key_is_enough(self):
        """P0-2: one Claude or OpenAI key runs everything; doctor says which, masked, and nothing about Jev."""
        from mobile_agent import doctor
        with mock.patch.dict(os.environ, clean_env(OPENAI_API_KEY="sk-proj-test-0000"), clear=True):
            key, helper = doctor.check_key(), doctor.check_helper()
        self.assertEqual(key.state, doctor.OK)
        self.assertTrue(key.detail.startswith("OpenAI (sk-proj-…0000)"), key.detail)
        self.assertNotIn("TYPESAFE", key.detail)
        self.assertNotEqual(helper.state, doctor.WARN)  # Smart needs no helper

    def test_fast_names_the_key_it_needs_before_touching_the_phone(self):
        url, seen, stop = fake_wda()
        self.addCleanup(stop)
        code, out, _ = run_main(["run", "Open Search", "--engine", "fast", "--wda-url", url],
                                {"OPENAI_API_KEY": "sk-test-0000"})
        self.assertEqual(code, 3)
        error = json.loads(out)
        self.assertEqual(error["error_type"], "MissingKey")
        self.assertIn("TYPESAFE_API_KEY", error["error"])
        self.assertIn("leave out --engine fast", error["error"])
        self.assertEqual(seen, [], "run talked to WDA before finding the key missing")

    def test_smart_runs_on_a_claude_key_and_has_no_preview(self):
        url, seen, stop = fake_wda()
        self.addCleanup(stop)
        code, out, _ = run_main(["run", "Open Search", "--wda-url", url], {"ANTHROPIC_API_KEY": "sk-ant-test-0000"})
        self.assertEqual(code, 2)
        self.assertIn("nothing to preview", json.loads(out)["error"])
        self.assertEqual(seen, [], "run talked to WDA for a task it couldn't run")

    def test_remember_that_runs_nothing(self):
        """"Remember that …" is a note for Mobster, not a task: `run` says how to save it and touches no phone."""
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

    def test_auto_picks_fast_only_with_a_jev_key(self):
        args = cli.build_parser().parse_args(["run", "x"])
        self.assertEqual(cli.pick_engine(args, {}), "smart")
        self.assertEqual(cli.pick_engine(args, {"TYPESAFE_API_KEY": "t"}), "fast")
        self.assertEqual(cli.pick_engine(cli.build_parser().parse_args(["run", "x", "--engine", "smart"]),
                                         {"TYPESAFE_API_KEY": "t"}), "smart")
        self.assertEqual(cli.pick_engine(cli.build_parser().parse_args(["run", "x", "--demo"]), {}), "demo")


class ExitCodes(unittest.TestCase):
    """CLI-13: 3 means no phone answered, however the connection failed; a bad flag is a usage error (2)."""

    def run_on(self, mode):
        url, seen, stop = fake_wda(mode)
        self.addCleanup(stop)
        return run_main(["run", "Open Search", "--wda-url", url], {"TYPESAFE_API_KEY": "test-0000"})

    def test_a_socket_that_hangs_up_is_no_phone(self):
        code, out, _ = self.run_on("hangup")
        self.assertEqual(code, 3)
        self.assertIn("mobster doctor", json.loads(out)["error"])

    def test_a_server_that_is_not_wda_is_no_phone(self):
        code, out, _ = self.run_on("404")
        self.assertEqual(code, 3)
        self.assertIn("not WebDriverAgent", json.loads(out)["error"])

    def test_a_host_that_does_not_resolve_is_no_phone(self):
        from mobile_agent.transport import TransportError
        with mock.patch("mobile_agent.drivers.resolve_wda_session",
                        side_effect=TransportError("HTTP gaierror; request outcome unknown; not retried")):
            code, out, _ = run_main(["run", "Open Search", "--wda-url", "http://nonexistent.invalid:8100"],
                                    {"TYPESAFE_API_KEY": "test-0000"})
        self.assertEqual(code, 3)
        self.assertIn("unknown host", json.loads(out)["error"])

    def test_invalid_flags_are_usage_errors(self):
        for argv in (["run", "x", "--max-steps", "0"], ["run", "x", "--max-seconds", "nan"],
                     ["run", "x", "--spend-cap-usd", "-1"], ["run", "x", "--wda-url", "127.0.0.1:9"],
                     ["screen", "--width", "0"]):
            with self.subTest(argv=argv):
                code, out, _ = run_main(argv, {"TYPESAFE_API_KEY": "test-0000"})
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
        code, _, err = run_main(["run", "x"], {"MOBSTER_WDA_URL": "user:secret@127.0.0.1:8100"})
        self.assertEqual(code, 2)
        self.assertIn("$MOBSTER_WDA_URL", err)
        self.assertNotIn("secret", err)


class ServeHelpAndPort(unittest.TestCase):
    """CLI-14: serve's help promised $MOBSTER_WDA_URL and a docs/api.md; --port 0 printed port 0."""

    def test_help_describes_what_serve_does(self):
        parser = cli.build_parser()
        help_text = next(a for a in parser._actions if a.dest == "command").choices["serve"].format_help()
        self.assertNotIn("$MOBSTER_WDA_URL", help_text)
        self.assertNotIn("docs/api.md", help_text)
        self.assertIn("https://docs.mobster.dev/scripting", help_text)
        with mock.patch.dict(os.environ, {"MOBSTER_WDA_URL": "http://127.0.0.1:8200"}), \
                mock.patch("mobile_agent.server.serve") as serve:
            cli.main(["serve", "--port", "0", "--state-db", os.path.join(tempfile.mkdtemp(), "m.sqlite3")])
        self.assertIsNone(serve.call_args.args[0].wda_url)

    def test_port_zero_prints_the_port_it_listens_on(self):
        from mobile_agent import server
        from mobile_agent.tui.session import serve_config
        with tempfile.TemporaryDirectory() as root:
            config = serve_config(port=0, state_db=Path(root, "m.sqlite3"))
            out = io.StringIO()
            with mock.patch.object(server.BoundedServer, "serve_forever", lambda self, *a, **k: None), \
                    mock.patch.object(server, "token_file", return_value=None), redirect_stdout(out):
                server.serve(config)
        port = int(json.loads(out.getvalue())["url"].rsplit(":", 1)[1])
        self.assertGreater(port, 0)
        self.assertEqual(config.port, port)


class MalformedEnvFile(unittest.TestCase):
    """CLI-7: one bad env file line raised before main's error handling: a traceback, nothing on stdout for
    --json, the start of the line (a pasted key) in the log, and `serve` (the Mac app's agent) never started."""

    def env_file(self, data):
        folder = tempfile.mkdtemp()
        path = Path(folder, "keys.env")
        path.write_bytes(data)
        return path

    def test_run_and_doctor_still_answer_in_json(self):
        path = self.env_file(b"MY-KEY=1\nsk-proj-Zm9vYmFy/secretpart==\nTYPESAFE_API_KEY=test-0000\n")
        code, out, err = run_main(["run", "Open Search", "--json", "--wda-url", "http://127.0.0.1:9",
                                   "--env-file", str(path)])
        self.assertEqual((code, json.loads(out)["error_type"]), (3, "NoDevice"))  # the key after them loaded
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
        with mock.patch("mobile_agent.server.serve") as serve:
            code, _, err = run_main(["serve", "--port", "0", "--env-file", str(path),
                                     "--state-db", os.path.join(tempfile.mkdtemp(), "m.sqlite3")])
        serve.assert_called_once()
        self.assertEqual(code, 0)
        self.assertIn("line 3", err)
        self.assertNotIn("secretpart", err)


class TildeEnvFile(unittest.TestCase):
    """CLI-8: shells leave `--env-file=~/keys.env` unexpanded: the keys were not loaded, and the terminal UI
    saved its settings to a new ./~/keys.env."""

    def test_the_flag_expands_the_home_folder(self):
        with mock.patch.dict(os.environ, {"HOME": "/Users/someone"}):
            args = cli.build_parser().parse_args(["doctor", "--env-file=~/keys.env"])
        self.assertEqual(args.env_file, Path("/Users/someone/keys.env"))

    def test_a_session_reads_and_saves_the_home_folder_file(self):
        from mobile_agent.tui.session import Session
        home, cwd = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (home / "keys.env").write_text("TYPESAFE_API_KEY=from-home-file-0000\n")
        old = os.getcwd()
        try:
            os.chdir(cwd)
            with mock.patch.dict(os.environ, clean_env(HOME=str(home)), clear=True):
                os.environ.pop("MOBSTER_ASK_BEFORE_ACTING", None)
                session = Session(wda_url="http://127.0.0.1:9", env_file="~/keys.env", history_db=str(cwd / "t.sqlite3"))
                loaded = os.environ.get("TYPESAFE_API_KEY")
                session.set_setting("askBeforeActing", False)  # shift+tab
                session.close(2)
        finally:
            os.chdir(old)
        self.assertEqual(loaded, "from-home-file-0000")
        self.assertEqual([p.name for p in cwd.iterdir() if "sqlite3" not in p.name], [])
        self.assertIn("MOBSTER_ASK_BEFORE_ACTING=0", (home / "keys.env").read_text())


class SmartStatus(unittest.TestCase):
    """CLI-11: Smart's proven finish ("completed") read as a grey "■ Completed"."""

    def test_a_proven_smart_task_reads_as_done(self):
        from mobile_agent.narrate import status_label, status_tone
        self.assertEqual(status_label("completed"), "Done")
        self.assertEqual(status_tone("completed"), "success")


class TerminalOrigin(unittest.TestCase):
    """A task from the terminal UI was admitted as an "api" task, so memory (which serves a person's tasks only)
    never reached it. It is a "tui" task now, and `mobster run` and `mobster chat` in this terminal are "cli"."""

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
                self.assertIn(run.origin, provider.ORIGINS)  # memory reaches it
                session.stop(run)


class SecondTerminal(unittest.TestCase):
    """CLI-15: a second terminal UI said "Cannot open the run journal; existing data was not reset"."""

    def test_it_says_another_terminal_is_open(self):
        from mobile_agent.tui.session import Session, error_text
        history = os.path.join(tempfile.mkdtemp(), "terminal.sqlite3")
        first = Session(wda_url="http://127.0.0.1:9", history_db=history)
        self.addCleanup(first.close, 2)
        with self.assertRaises(Exception) as caught:
            Session(wda_url="http://127.0.0.1:8", history_db=history)
        message = error_text(caught.exception)
        self.assertIn("Mobster is already open in another terminal window", message)
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


if __name__ == "__main__":
    unittest.main()

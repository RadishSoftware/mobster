import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SHELL_ARGV = {
    "defaults": ["serve", "--port", "8765", "--exit-with-parent", "--manage-device"],
    "everything": ["serve", "--port", "8765", "--exit-with-parent", "--wda-url", "http://127.0.0.1:8100",
                   "--session", "SESSION", "--env-file", "/tmp/agent.env", "--enable-live", "--manage-device",
                   "--data-dir", "/tmp/data"],
}


def start_server(runtime):
    from mobile_agent.server import BoundedServer, make_handler
    runtime.config = SimpleNamespace(port=8765)
    server = BoundedServer(("127.0.0.1", 0), make_handler(runtime))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def call(server, method, path, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    headers = {"Host": "127.0.0.1:8765", **({"Content-Type": "application/json"} if body is not None else {})}
    connection.request(method, path, json.dumps(body) if body is not None else None, headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response, data


class ShippedAgentAcceptsTheShellsArguments(unittest.TestCase):
    def test_every_argv_the_shell_builds_parses_without_the_extension(self):
        from mobile_agent import cli_parser, harness_api
        with patch.object(harness_api.hooks(), "cli", []):
            parser = cli_parser.build_parser()
            for name, argv in SHELL_ARGV.items():
                with self.subTest(argv=name), patch("sys.stderr"):
                    try:
                        args = parser.parse_args(argv)
                    except SystemExit as exit:
                        self.fail(f"{name}: the shipped agent exits {exit.code} before serving")
                    self.assertEqual((args.command, args.port, args.exit_with_parent, args.manage_device),
                                     ("serve", 8765, True, True))
            everything = parser.parse_args(SHELL_ARGV["everything"])
            self.assertEqual((everything.wda_url, everything.session, str(everything.env_file),
                              everything.enable_live, str(everything.data_dir)),
                             ("http://127.0.0.1:8100", "SESSION", "/tmp/agent.env", True, "/tmp/data"))


class ExportDeniedByMacOS(unittest.TestCase):
    def setUp(self):
        self.server = start_server(Mock())
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    @unittest.skipIf(os.geteuid() == 0, "root ignores the folder's mode")
    def test_a_downloads_folder_macos_denies_says_where_to_allow_it(self):
        with tempfile.TemporaryDirectory() as home:
            downloads = Path(home, "Downloads")
            downloads.mkdir()
            downloads.chmod(0o500)  # what a "Don't Allow" on the Downloads privacy prompt gives the agent
            try:
                with patch.dict(os.environ, {"HOME": home}):
                    response, data = call(self.server, "POST", "/api/export",
                                          {"filename": "result.json", "text": "{}"})
            finally:
                downloads.chmod(0o700)
        answer = json.loads(data)
        self.assertEqual((response.status, answer.get("code")), (403, "downloads_denied"), answer)
        self.assertEqual(answer["error"], "Mobster can’t save to Downloads. Allow it in System Settings › "
                                          "Privacy & Security › Files and Folders.")

    def test_only_a_socket_timeout_is_a_timeout(self):
        from mobile_agent import api_post as module
        for error, status in ((TimeoutError(), 408), (OSError(28, "No space left on device"), 500)):
            with self.subTest(error=type(error).__name__), patch.object(module, "save_export", side_effect=error):
                response, data = call(self.server, "POST", "/api/export", {"filename": "result.json", "text": "{}"})
            self.assertEqual(response.status, status, data)
        self.assertEqual(json.loads(data)["code"], "io_error")


class PhoneScreensStayInTheDataFolder(unittest.TestCase):
    def setUp(self):
        runtime = Mock()
        runtime.runs = {"0123456789ab": object()}
        runtime.frames.get = lambda run_id, frame_id: b"\xff\xd8\xff\xe0jpeg\xff\xd9"
        runtime.icons.get = lambda bundle: (b"\x89PNG\r\n\x1a\nicon", "image/png")
        self.server = start_server(runtime)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_run_frames_and_app_icons_are_never_stored_by_the_webview(self):
        for path in ("/api/runs/0123456789ab/frames/f0123456789", "/api/apps/icon?bundle=com.apple.Preferences"):
            with self.subTest(path=path):
                response, _ = call(self.server, "GET", path)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Cache-Control"), "no-store")


class OneBadLineInAgentEnv(unittest.TestCase):
    def serve_with(self, env_bytes):
        from mobile_agent import __main__ as cli
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}), \
                patch("mobile_agent.serve.serve") as serve, patch("sys.stderr", new=io.StringIO()) as stderr:
            env_file = Path(root, "agent.env")
            env_file.write_bytes(env_bytes)
            cli.main(["serve", "--port", "18999", "--exit-with-parent", "--env-file", str(env_file),
                      "--data-dir", root, "--state-db", str(Path(root, "state", "m.sqlite3"))])
            loaded = os.environ.get("MOBSTER_TEST_AFTER_THE_BAD_LINE")
        return serve, stderr.getvalue(), loaded

    def test_the_agent_still_serves_and_logs_the_line_number(self):
        serve, log, loaded = self.serve_with(b"OPENAI_API_KEY=sk-not-a-real-key\nHELPER MODEL=gemini-3.5-flash-lite\n"
                                             b"MOBSTER_TEST_AFTER_THE_BAD_LINE=yes\n")
        serve.assert_called_once()
        self.assertEqual(loaded, "yes")
        self.assertIn("agent.env line 2", log)
        self.assertNotIn("gemini", log)
        self.assertNotIn("sk-not", log)

    def test_a_byte_that_is_not_utf8_is_skipped_too(self):
        serve, log, loaded = self.serve_with(b"TEXT_MODEL=caf\xe9\nMOBSTER_TEST_AFTER_THE_BAD_LINE=yes\n")
        serve.assert_called_once()
        self.assertEqual(loaded, "yes")
        self.assertIn("agent.env line 1 is not UTF-8", log)


if __name__ == "__main__":
    unittest.main()

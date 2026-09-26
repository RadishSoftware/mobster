"""The loopback API's per-launch token and Host/Origin checks. Real HTTP, no device."""

import http.client
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import server
from mobile_agent.server import BoundedServer, make_handler

TOKEN = "t" * 43


class TokenTests(unittest.TestCase):
    def setUp(self):
        runtime = Mock()
        runtime.status.return_value = {"live_enabled": False}
        runtime.config = SimpleNamespace(port=8765)
        runtime.update_settings.return_value = {"askBeforeActing": False}
        self.runtime = runtime
        self.server = BoundedServer(("127.0.0.1", 0), make_handler(runtime, TOKEN))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method="GET", path="/api/status", token=None, host="127.0.0.1:8765", origin=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if origin:
            headers["Origin"] = origin
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response.read()
        connection.close()
        return response

    def test_requests_without_the_token_are_refused(self):
        self.assertEqual(self.request().status, 401)
        self.assertEqual(self.request(token="x" * 43).status, 401)
        self.assertEqual(self.request("POST", "/api/settings", body='{"askBeforeActing": false}').status, 401)
        self.runtime.update_settings.assert_not_called()

    def test_the_token_opens_every_route(self):
        self.assertEqual(self.request(token=TOKEN).status, 200)
        self.assertEqual(self.request("POST", "/api/settings", token=TOKEN, body='{"askBeforeActing": false}').status, 200)

    def test_streams_take_the_token_in_the_query_but_posts_do_not(self):
        self.assertEqual(self.request(path=f"/api/status?token={TOKEN}").status, 200)
        self.assertEqual(self.request(path="/api/device/stream?token=wrong").status, 401)
        self.assertEqual(self.request("POST", f"/api/settings?token={TOKEN}", body="{}").status, 401)

    def test_dns_rebinding_and_foreign_origins_are_refused_even_with_the_token(self):
        self.assertEqual(self.request(token=TOKEN, host="attacker.example:8765").status, 403)
        self.assertEqual(self.request(token=TOKEN, origin="https://evil.example").status, 403)

    def test_the_preflight_allows_the_authorization_header(self):
        response = self.request("OPTIONS", "/api/runs", origin="tauri://localhost")
        self.assertEqual(response.status, 204)
        self.assertIn("Authorization", response.getheader("Access-Control-Allow-Headers"))

    def test_only_the_api_and_the_desktop_webview_are_admitted_origins(self):
        # The Vite dev proxy presents its page as the API's own origin.
        for origin, status in (("http://127.0.0.1:8765", 200), ("tauri://localhost", 200),
                               ("http://127.0.0.1:5173", 403), ("http://localhost:3000", 403)):
            self.assertEqual(self.request(token=TOKEN, origin=origin).status, status, origin)


class LaunchTokenTests(unittest.TestCase):
    def test_the_shell_token_is_taken_and_removed_from_the_environment(self):
        with patch.dict(os.environ, {"MOBSTER_API_TOKEN": TOKEN}):
            self.assertEqual(server.api_token(), TOKEN)
            self.assertNotIn("MOBSTER_API_TOKEN", os.environ)
        with patch.dict(os.environ, {"MOBSTER_API_TOKEN": "short"}), self.assertRaises(ValueError):
            server.api_token()

    def test_without_one_each_launch_gets_a_new_random_token(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOBSTER_API_TOKEN", None)
            first, second = server.api_token(), server.api_token()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 40)

    def test_serve_requires_the_token_and_leaves_it_in_a_private_file_while_running(self):
        state = Path(tempfile.mkdtemp()) / "state" / "mobster.sqlite3"
        config = SimpleNamespace(port=0, wda_url=None, state_db=state)
        seen = {}

        class FakeServer:
            def __init__(self, address, handler):
                seen["handler"] = handler

            def serve_forever(self):
                path = state.parent / "api-token"
                seen["file"] = (path.read_text().strip(), stat.S_IMODE(path.stat().st_mode),
                                stat.S_IMODE(path.parent.stat().st_mode))
                raise KeyboardInterrupt

            def server_close(self):
                pass

        with patch.dict(os.environ, {"MOBSTER_API_TOKEN": TOKEN}), patch.object(server, "Runtime"), \
                patch.object(server, "WorkflowScheduler"), patch.object(server, "BoundedServer", FakeServer), \
                patch("builtins.print") as printed:
            server.serve(config)
        self.assertEqual(seen["file"], (TOKEN, 0o600, 0o700))
        self.assertFalse((state.parent / "api-token").exists())
        self.assertNotIn(TOKEN, str(printed.call_args))
        handler = seen["handler"]
        request = SimpleNamespace(headers={"Authorization": "Bearer nope"}, command="GET", path="/api/status")
        self.assertFalse(handler.authorized(request))
        request.headers = {"Authorization": f"Bearer {TOKEN}"}
        self.assertTrue(handler.authorized(request))


if __name__ == "__main__":
    unittest.main()

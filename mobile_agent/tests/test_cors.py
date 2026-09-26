"""Desktop webview origin: CORS for allowlisted local origins only. Real HTTP, no device."""

import http.client
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from mobile_agent.server import BoundedServer, make_handler


class CorsTests(unittest.TestCase):
    def setUp(self):
        runtime = Mock()
        runtime.status.return_value = {"live_enabled": False}
        runtime.config = SimpleNamespace(port=8765)
        self.server = BoundedServer(("127.0.0.1", 0), make_handler(runtime))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method, origin=None, path="/api/status"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": "127.0.0.1:8765"}
        if origin:
            headers["Origin"] = origin
        if method == "OPTIONS":
            headers["Access-Control-Request-Method"] = "POST"
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        response.read()
        connection.close()
        return response

    def test_desktop_webview_can_read_the_api(self):
        response = self.request("GET", "tauri://localhost")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "tauri://localhost")

    def test_desktop_preflight_is_answered(self):
        response = self.request("OPTIONS", "tauri://localhost", "/api/runs")
        self.assertEqual(response.status, 204)
        self.assertIn("Idempotency-Key", response.getheader("Access-Control-Allow-Headers"))

    def test_other_origins_are_refused_and_get_no_cors(self):
        for method in ("GET", "OPTIONS"):
            response = self.request(method, "https://evil.example")
            self.assertEqual(response.status, 403)
            self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_same_origin_requests_carry_no_cors_headers(self):
        response = self.request("GET")
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))


if __name__ == "__main__":
    unittest.main()

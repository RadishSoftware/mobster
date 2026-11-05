import ast
import unittest
from pathlib import Path

from mobile_agent.wda import timeouts as table

ROOT = Path(__file__).resolve().parent.parent
# Every module that opens a connection to WebDriverAgent, its video stream or its relay.
WDA_MODULES = [
    "drivers.py", "wda/observe.py", "wda/text.py", "wda/gestures.py", "device_manager.py", "lockscreen.py",
    "wda/video.py", "device_status.py", "devices.py", "pool.py",
    "doctor.py", "fleet.py", "server.py", "runtime_driver.py", "verify/runner.py", "wireless/probe.py",
    "manual_control.py",
    "tui/session.py", "harness/prewarm.py", "device_targets.py", "http_pool.py", "owner_presence.py",
    "wda/watchdog.py", "phone_io/transfer.py", "phone_io/__init__.py", "mcp_server/tools.py",
    "mcp_server/run_tools.py", "mcp_server/device_tools.py", "mcp_server/screen_tools.py", "mcp_server/approvals.py",
    "mcp_server/action_tools.py", "mcp_server/phone_tools.py",
]
# The modules that may still call urlopen, each with its reason: urlopen's timeout restarts on every chunk, so a phone
# that drips bytes can hold it, while transport's deadline is absolute.
URLOPEN_ALLOWED = {
    "device_targets.py": "WdaDevice takes its opener as an argument (urlopen's signature), and the verify and "
                         "phone-transfer tests inject fakes through it; each call carries a socket timeout",
    "phone_io/__init__.py": "hands urlopen to WdaDevice as the default opener",
    "phone_io/transfer.py": "the clipboard and photo pushes: tests fake urlopen at this seam, and the push maps "
                            "HTTP 404 to unknown_route; each call carries a socket timeout (15 s by default)",
}
RECEIVER_HINTS = ("http", "client")


def _name(node):
    if isinstance(node, ast.Attribute):
        return _name(node.value) + "." + node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return _name(node.func) + "()"
    return ""


def unbounded_calls(source, filename="<module>"):
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        if not isinstance(node, ast.Call):
            continue
        name = _name(node.func)
        keywords = {item.arg for item in node.keywords}
        if name.endswith("urlopen"):
            if "timeout" not in keywords and len(node.args) < 3:
                found.append((node.lineno, name))
        elif name.endswith(("._open", ".opener")):
            if "timeout" not in keywords and len(node.args) < 2:
                found.append((node.lineno, name))
        elif name.endswith(("HTTPConnection", "HTTPSConnection")):
            if "timeout" not in keywords and len(node.args) < 3:
                found.append((node.lineno, name))
        elif name.endswith(".request") and node.args and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value in {"GET", "POST", "DELETE", "PUT"}:
            receiver = name.rsplit(".", 1)[0].lower()
            if any(hint in receiver for hint in RECEIVER_HINTS) and "timeout" not in keywords and len(node.args) < 4:
                found.append((node.lineno, name))
    return found


def urlopen_uses(source, filename="<module>"):
    return sorted({node.lineno for node in ast.walk(ast.parse(source, filename))
                   if (isinstance(node, ast.Attribute) and node.attr == "urlopen")
                   or (isinstance(node, ast.Name) and node.id == "urlopen")})


class Walker(unittest.TestCase):
    def test_wda_modules_use_the_transport_unless_listed(self):
        problems = []
        for relative in WDA_MODULES:
            path = ROOT / relative
            if path.exists() and relative not in URLOPEN_ALLOWED:
                problems += [f"{relative}:{line}" for line in urlopen_uses(path.read_text(encoding="utf-8"), relative)]
        self.assertEqual(problems, [], "urlopen in a WDA module: use transport.request_once or transport.HTTP "
                                       "(absolute deadline), or list it in URLOPEN_ALLOWED with the reason")

    def test_every_allowed_urlopen_module_still_uses_it(self):
        for relative in URLOPEN_ALLOWED:
            with self.subTest(relative):
                self.assertIn(relative, WDA_MODULES)
                self.assertTrue(urlopen_uses((ROOT / relative).read_text(encoding="utf-8")),
                                f"{relative} no longer uses urlopen: remove it from URLOPEN_ALLOWED")

    def test_no_wda_request_without_a_timeout(self):
        problems = []
        for relative in WDA_MODULES:
            path = ROOT / relative
            if not path.exists():
                continue
            for line, what in unbounded_calls(path.read_text(encoding="utf-8"), relative):
                problems.append(f"{relative}:{line} {what}")
        self.assertEqual(problems, [], "WDA requests with no explicit timeout (use wda.timeouts)")

    def test_the_walker_sees_an_unbounded_call(self):
        source = ("def f(driver, url):\n"
                  "    driver.http.request('GET', '/status')\n"
                  "    urllib.request.urlopen(url)\n"
                  "    http.client.HTTPConnection('h', 80)\n"
                  "    driver.http.request('GET', '/status', None, 3)\n"
                  "    driver.http.request('GET', '/status', timeout=3)\n"
                  "    urllib.request.urlopen(url, timeout=3)\n")
        self.assertEqual([line for line, _ in unbounded_calls(source)], [2, 3, 4])

    def test_actions_fail_closed(self):
        for path in ("/actions", "/wda/keys", "/wda/pressButton", "/wda/dragfromtoforduration", "/element/4/value",
                     "/session/x/element/4/clear", "/wda/apps/activate", "/url", "/wda/somethingNew",
                     "/session/S/element/abc/doubleTap", "/session/S/element/abc/swipe", "/element/abc/press"):
            self.assertTrue(table.is_action("POST", path), path)
        for method, path in (("GET", "/source"), ("GET", "/wda/locked"), ("POST", "/elements"),
                             ("POST", "/appium/settings"), ("POST", "/session"),
                             ("POST", "/session/S/element/abc/elements")):
            self.assertFalse(table.is_action(method, path), path)

    def test_bounded(self):
        self.assertEqual(table.bounded(None, "GET", "/wda/locked"), 3.0)
        self.assertEqual(table.bounded(500), table.MAX_WDA_TIMEOUT)
        self.assertEqual(table.bounded(2), 2.0)
        self.assertGreater(table.bounded(0), 0)


class SlowDrip(unittest.TestCase):
    def drip_server(self):
        import socket
        import threading
        import time
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def drip():
            connection, _ = server.accept()
            try:
                connection.recv(4096)
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 400\r\n\r\n")
                for _ in range(40):
                    connection.sendall(b"x")
                    time.sleep(.1)
            except OSError:
                pass
            finally:
                connection.close()

        threading.Thread(target=drip, daemon=True).start()
        self.addCleanup(server.close)
        return port

    def test_video_request_ends_at_its_deadline_when_a_phone_drips_bytes(self):
        import time

        from mobile_agent.transport import TransportError
        from mobile_agent.wda.video import WdaVideo
        port = self.drip_server()
        started = time.monotonic()
        with self.assertRaises(TransportError):
            WdaVideo._request(f"http://127.0.0.1:{port}", "GET", "/screenshot", timeout=.5)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_a_status_read_ends_at_its_deadline_when_a_phone_drips_bytes(self):
        import time

        from mobile_agent import devices
        from mobile_agent.wireless import probe
        port = self.drip_server()
        started = time.monotonic()
        self.assertFalse(devices.wda_answers(f"http://127.0.0.1:{port}", timeout=.5))
        self.assertIsNone(probe.http_status(port, timeout=.5))
        self.assertLess(time.monotonic() - started, 2.5)

    def test_request_once_returns_what_a_runner_answers(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from mobile_agent import devices
        from mobile_agent.transport import request_once

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"value": {"ready": True}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        self.assertEqual(request_once(base, "GET", "/status", timeout=2), {"value": {"ready": True}})
        self.assertTrue(devices.wda_answers(base + "/"))
        from mobile_agent.mcp_server import tools
        self.assertEqual(tools._wda_get(base + "/status", 2.0), {"ready": True})


if __name__ == "__main__":
    unittest.main()

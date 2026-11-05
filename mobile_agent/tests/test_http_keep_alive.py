"""One connection across complete error answers, on a real socket: WDA keeps a descriptor per connection."""

import http.server
import json
import socket
import threading
import time
import unittest
from unittest import mock

from mobile_agent import transport
from mobile_agent.drivers import WDA
from mobile_agent.frontier.safety import outcome_unknown
from mobile_agent.transport import HTTP, StaleSession, TransportError

SESSION_GONE = json.dumps({"value": {"error": "invalid session id"}})


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1

    def answer(self):
        with self.server.lock:
            self.server.seen.append((self.command, self.path))
            status, body, how = self.server.answers.pop(0) if self.server.answers else (200, '{"value": null}', None)
        if how == "slow":
            time.sleep(1)
        raw = body.encode()
        self.send_response(status)
        if how == "chunked":
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(raw) + (98 if how == "cut" else 0)))
        if how == "close":
            self.send_header("Connection", "close")
        self.end_headers()
        if how == "chunked":
            half = len(raw) // 2
            for part in (raw[:half], raw[half:]):
                if part:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.wfile.write(raw)
        self.wfile.flush()
        if how in ("close", "drop", "cut"):  # "drop" closes with no header, as WDA's failClient does
            self.close_connection = True

    do_GET = do_POST = answer

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

    def parse_request(self):
        ok = super().parse_request()
        if ok:
            self.read_body()
        return ok

    def log_message(self, *args):
        pass


class Server(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.lock, self.server.connections, self.server.seen, self.server.answers = threading.Lock(), 0, [], []
        self.server.daemon_threads = True
        self.server.handle_error = lambda *args: None      # a client that hung up on a slow answer is the test's point
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.client = HTTP(self.url)
        self.addCleanup(self.client.close)

    def answers(self, *items):
        self.server.answers = [(*item, None)[:3] for item in items]


class ItKeepsTheConnection(Server):
    def test_after_each_complete_failure_status(self):
        for status in (302, 400, 404, 500, 503):
            self.answers((status, "{}"))
            with self.subTest(status=status), self.assertRaises(TransportError) as raised:
                self.client.request("GET", "/x")
            self.assertEqual(str(raised.exception), f"HTTP {status}; request not retried")
        self.client.request("GET", "/x")
        self.assertEqual(self.server.connections, 1)

    def test_across_many_404s(self):
        for _ in range(50):
            self.answers((404, "{}"))
            with self.assertRaises(TransportError):
                self.client.request("GET", "/element/active")
        self.assertEqual((len(self.server.seen), self.server.connections), (50, 1))

    def test_after_a_chunked_failure_read_to_its_last_chunk(self):
        self.answers((404, json.dumps({"value": {"error": "no such alert"}}), "chunked"))
        with self.assertRaises(TransportError) as raised:
            self.client.request("GET", "/alert/text")
        self.assertEqual((raised.exception.status, raised.exception.wda_error), (404, "no such alert"))
        self.client.request("GET", "/x")
        self.assertEqual(self.server.connections, 1)

    def test_with_an_empty_or_unreadable_failure_body(self):
        for body in ("", "<html>not json</html>"):
            self.answers((500, body))
            with self.subTest(body=body), self.assertRaises(TransportError):
                self.client.request("GET", "/x")
        self.client.request("GET", "/x")
        self.assertEqual(self.server.connections, 1)

    def test_after_a_stale_session_which_keeps_its_class(self):
        self.answers((404, SESSION_GONE))
        with self.assertRaises(StaleSession):
            self.client.request("POST", "/x", {})
        self.client.request("GET", "/x")
        self.assertEqual(self.server.connections, 1)

    def test_for_a_driver_asking_what_has_focus(self):
        driver = WDA(self.url, "s1")
        self.addCleanup(driver.close)
        for _ in range(5):
            self.answers((404, "{}"))
            with self.assertRaises(TransportError):
                driver.call("GET", "/element/active")
        self.assertEqual(self.server.connections, 1)

    def test_for_a_kept_read(self):
        def forget():
            with transport._kept_lock:
                client, _ = transport._kept.pop(self.url, (None, None))
            if client is not None:
                client.close()
        self.addCleanup(forget)
        self.answers((404, "{}"))
        with self.assertRaises(TransportError):
            transport.read_kept(self.url, "/status")
        transport.read_kept(self.url, "/status")
        self.assertEqual(self.server.connections, 1)


class ItStillCloses(Server):
    def test_when_the_answer_says_it_will(self):
        self.answers((404, "{}", "close"))
        with self.assertRaises(TransportError):
            self.client.request("GET", "/x")
        self.client.request("GET", "/x")
        self.assertEqual(self.server.connections, 2)

    def test_when_a_failure_is_cut_short_and_its_outcome_is_unknown(self):
        self.answers((500, "{}", "cut"))
        with self.assertRaises(TransportError) as raised:
            self.client.request("POST", "/x", {})
        self.assertIn("outcome unknown", str(raised.exception))
        self.assertIsNone(self.client.connection.sock)
        self.assertTrue(outcome_unknown(raised.exception))

    def test_when_a_success_is_cut_short(self):
        self.answers((200, "{}", "cut"))
        with self.assertRaises(TransportError) as raised:
            self.client.request("POST", "/x", {})
        self.assertIn("outcome unknown", str(raised.exception))
        self.assertIsNone(self.client.connection.sock)

    def test_on_a_timeout_and_the_outcome_is_unknown(self):
        self.answers((500, "{}", "slow"))
        with self.assertRaises(TransportError) as raised:
            self.client.request("POST", "/x", {}, timeout=.2)
        self.assertIn("outcome unknown", str(raised.exception))
        self.assertIsNone(self.client.connection.sock)

    def test_on_a_good_answer_that_isnt_an_object(self):
        self.answers((200, "[]"))
        with self.assertRaises(TransportError):
            self.client.request("GET", "/x")
        self.assertIsNone(self.client.connection.sock)


class NeverRetried(Server):
    def test_a_connection_the_server_closed_after_a_failure_is_replaced_before_anything_is_sent(self):
        """WDA's failClient closes after a header-level 400 with no Connection header. Seen before sending, it's a
        new connection and the request goes once. Before, the tap failed on the dead socket as an unknown outcome."""
        self.answers((400, "{}", "drop"))
        with self.assertRaises(TransportError):
            self.client.request("POST", "/first", {})
        time.sleep(.1)
        self.assertEqual(self.client.request("POST", "/tap", {}), {"value": None})
        self.assertEqual([x for x in self.server.seen if x[1] == "/tap"], [("POST", "/tap")])
        self.assertEqual(self.server.connections, 2)

    def test_a_close_seen_only_after_sending_is_still_unknown_and_sent_once(self):
        """If the close lands after the check, the request fails on the dead socket: still unknown, never resent."""
        self.answers((400, "{}", "drop"))
        with self.assertRaises(TransportError):
            self.client.request("POST", "/first", {})
        time.sleep(.1)
        with mock.patch.object(transport, "peer_closed", lambda sock: False), \
                self.assertRaises(TransportError) as raised:
            self.client.request("POST", "/tap", {})
        self.assertIn("outcome unknown", str(raised.exception))
        self.assertTrue(outcome_unknown(raised.exception))
        self.assertLessEqual(len([x for x in self.server.seen if x[1] == "/tap"]), 1)

    def test_an_idle_kept_socket_reads_as_open_and_a_closed_one_as_closed(self):
        self.answers((404, "{}"))
        with self.assertRaises(TransportError):
            self.client.request("GET", "/x")
        self.assertFalse(transport.peer_closed(self.client.connection.sock))
        self.answers((400, "{}", "drop"))
        with self.assertRaises(TransportError):
            self.client.request("GET", "/x")
        time.sleep(.1)
        self.assertTrue(transport.peer_closed(self.client.connection.sock))
        dead = socket.socket()
        dead.close()
        self.assertTrue(transport.peer_closed(dead))

    def test_a_failed_action_is_sent_once_on_the_kept_connection(self):
        self.answers((500, "{}"))
        with self.assertRaises(TransportError):
            self.client.request("POST", "/tap", {})
        self.assertEqual(self.server.seen, [("POST", "/tap")])

    def test_a_refused_connection_still_says_nothing_was_sent(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        client = HTTP(f"http://127.0.0.1:{port}")
        with self.assertRaises(transport.NotSent):
            client.request("POST", "/tap", {}, timeout=1)


if __name__ == "__main__":
    unittest.main()

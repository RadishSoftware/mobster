import http.server
import json
import threading
import unittest
from unittest.mock import Mock, patch

from mobile_agent import transport


class Helper(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1

    def do_GET(self):
        with self.server.lock:
            self.server.reads += 1
            status, close = self.server.answer
        body = json.dumps({"value": {"ready": True}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        if close:
            self.close_connection = True

    def log_message(self, *args):
        pass


class KeptReadTests(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Helper)
        self.server.lock, self.server.connections, self.server.reads = threading.Lock(), 0, 0
        self.server.answer = (200, False)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(lambda: [client.close() for client, _ in transport._kept.values()])

    def test_a_kept_connection_per_address_and_never_more_than_kept_max(self):
        for index in range(transport.KEPT_MAX + 3):
            url = f"http://127.0.0.1:{60000 + index}"
            with transport._kept_lock:
                transport._kept[url] = (transport.HTTP(url), threading.Lock())
        transport.read_kept(self.url, "/status")
        self.assertLessEqual(len(transport._kept), transport.KEPT_MAX)
        self.assertIn(self.url, transport._kept)

    def test_pool_pressure_never_closes_a_connection_owned_by_another_read(self):
        entries = {}
        for index in range(transport.KEPT_MAX):
            lock = threading.Lock()
            lock.acquire()
            entries[f"http://127.0.0.1:{60000 + index}"] = (Mock(), lock)
        with patch.object(transport, "_kept", entries):
            self.assertTrue(transport.read_kept(self.url, "/status")["value"]["ready"])
            self.assertEqual(len(entries), transport.KEPT_MAX)
            for client, lock in entries.values():
                client.close.assert_not_called()
                lock.release()

    def test_repeated_reads_share_one_connection(self):
        for _ in range(20):
            self.assertTrue(transport.read_kept(self.url, "/status")["value"]["ready"])
        self.assertEqual((self.server.reads, self.server.connections), (20, 1))

    def test_a_connection_the_helper_closed_is_opened_again_and_the_read_asked_once_more(self):
        self.server.answer = (200, True)
        for _ in range(3):
            self.assertTrue(transport.read_kept(self.url, "/status")["value"]["ready"])
        self.assertEqual(self.server.reads, 3)

    def test_a_kept_connection_the_helper_dropped_while_idle_is_opened_again_without_a_second_answer(self):
        Helper.timeout = .2
        self.addCleanup(setattr, Helper, "timeout", None)
        transport.read_kept(self.url, "/status")
        threading.Event().wait(.5)
        self.assertTrue(transport.read_kept(self.url, "/status")["value"]["ready"])
        self.assertEqual((self.server.reads, self.server.connections), (2, 2))

    def test_a_refusal_is_not_asked_again(self):
        transport.read_kept(self.url, "/status")
        self.server.answer = (500, False)
        with self.assertRaises(transport.TransportError):
            transport.read_kept(self.url, "/status")
        self.assertEqual(self.server.reads, 2)


if __name__ == "__main__":
    unittest.main()

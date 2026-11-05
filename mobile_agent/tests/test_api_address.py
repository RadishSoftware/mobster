
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent.threads import client


class AgentAddressTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.state = self.home / "state"
        self.state.mkdir()
        patches = [patch("mobile_agent.paths.user_data_dir", return_value=self.home),
                   patch("mobile_agent.paths.source_checkout", return_value=None),
                   patch.dict(os.environ, {}, clear=False)]
        for active in patches:
            active.start()
            self.addCleanup(active.stop)
        os.environ.pop("MOBSTER_URL", None)

    def listener(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        self.addCleanup(sock.close)
        return sock.getsockname()[1]

    def closed_port(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def write(self, text):
        (self.state / "api-url").write_text(text + "\n")

    def test_without_a_file_it_is_8765(self):
        self.assertIsNone(client.discovered_url())
        self.assertEqual(client.default_url(), "http://127.0.0.1:8765")

    def test_the_address_the_running_service_wrote_wins(self):
        port = self.listener()
        self.write(f"http://127.0.0.1:{port}")
        self.assertEqual(client.default_url(), f"http://127.0.0.1:{port}")

    def test_mobster_url_still_comes_first(self):
        self.write(f"http://127.0.0.1:{self.listener()}")
        with patch.dict(os.environ, {"MOBSTER_URL": "http://127.0.0.1:9999"}):
            self.assertEqual(client.default_url(), "http://127.0.0.1:9999")

    def test_a_file_left_by_a_crashed_service_or_naming_another_machine_is_ignored(self):
        for text in (f"http://127.0.0.1:{self.closed_port()}", f"http://example.com:{self.listener()}",
                     "https://127.0.0.1:8766", "not a url", "http://127.0.0.1:99999"):
            with self.subTest(text=text):
                self.write(text)
                self.assertEqual(client.default_url(), "http://127.0.0.1:8765")


if __name__ == "__main__":
    unittest.main()

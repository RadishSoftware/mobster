import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent.threads import recent


class RecentThreadTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="mobster-recent-")
        self.addCleanup(folder.cleanup)
        self.file = Path(folder.name) / "state" / "chat-threads.json"
        patcher = mock.patch.object(recent, "path", return_value=self.file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_remembers_each_address_in_a_private_file(self):
        recent.remember_thread("http://localhost:8080", "abcdef012345")
        recent.remember_thread("http://localhost:8081", "012345abcdef")
        self.assertEqual(recent.last_thread("http://localhost:8080"), "abcdef012345")
        self.assertEqual(recent.last_thread("http://localhost:8081"), "012345abcdef")
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.file.parent.stat().st_mode), 0o700)

    def test_replaces_malformed_saved_data(self):
        self.file.parent.mkdir()
        for content in ("not JSON", "[]"):
            with self.subTest(content=content):
                self.file.write_text(content)
                recent.remember_thread("http://localhost:8080", "abcdef012345")
                self.assertEqual(json.loads(self.file.read_text()),
                                 {"http://localhost:8080": "abcdef012345"})

    def test_failed_replacement_preserves_saved_addresses(self):
        recent.remember_thread("http://localhost:8080", "abcdef012345")
        before = self.file.read_bytes()
        with mock.patch("mobile_agent.paths.os.replace", side_effect=OSError("replacement failed")):
            recent.remember_thread("http://localhost:8081", "012345abcdef")
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(list(self.file.parent.iterdir()), [self.file])


if __name__ == "__main__":
    unittest.main()

"""Atomic text writes keep the old file on failure and give concurrent writers separate temporary files."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from mobile_agent.paths import write_text_atomic


class AtomicWriteTests(unittest.TestCase):
    def test_a_failed_replace_preserves_the_file_and_cleans_the_owned_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("old")
            with mock.patch("mobile_agent.paths.os.replace",
                            side_effect=OSError("disk full")), self.assertRaises(OSError):
                write_text_atomic(path, "new")
            self.assertEqual(path.read_text(), "old")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_concurrent_writers_leave_one_complete_value_and_no_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            values = ["first" * 2000, "second" * 2000]
            with ThreadPoolExecutor(2) as pool:
                list(pool.map(lambda text: write_text_atomic(path, text), values))
            self.assertIn(path.read_text(), values)
            self.assertEqual(list(Path(directory).iterdir()), [path])

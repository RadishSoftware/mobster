"""Desktop exports are written to Downloads under a safe, unique name. Offline."""

import tempfile
import unittest
from pathlib import Path

from mobile_agent.server import save_export


class ExportTests(unittest.TestCase):
    def test_saves_without_overwriting(self):
        with tempfile.TemporaryDirectory() as folder:
            first = save_export({"filename": "mobster-result-abc.json", "text": "{}"}, folder)
            second = save_export({"filename": "mobster-result-abc.json", "text": "[]"}, folder)
            self.assertEqual((first["name"], second["name"]), ("mobster-result-abc.json", "mobster-result-abc (1).json"))
            self.assertEqual(Path(folder, first["name"]).read_text(), "{}")

    def test_paths_and_unknown_types_are_refused_or_neutralized(self):
        with tempfile.TemporaryDirectory() as folder:
            saved = save_export({"filename": "../../etc/pass wd.csv", "text": "a,b"}, folder)
            self.assertEqual(Path(saved["path"]).parent, Path(folder))
            for name in ("run.sh", "noextension", ".json", "x.command"):
                with self.assertRaises(ValueError, msg=name):
                    save_export({"filename": name, "text": ""}, folder)
            with self.assertRaises(ValueError):
                save_export({"filename": "a.json", "text": "x", "extra": 1}, folder)


if __name__ == "__main__":
    unittest.main()

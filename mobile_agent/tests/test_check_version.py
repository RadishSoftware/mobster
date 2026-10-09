"""desktop/scripts/check-version.py: every shipped version agrees, including the two copies npm keeps in the lock."""

import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "desktop" / "scripts" / "check-version.py"
FILES = ["desktop/src-tauri/tauri.conf.json", "desktop/src-tauri/Cargo.toml", "desktop/package.json",
         "desktop/package-lock.json", "pyproject.toml", "mobile_agent/__init__.py"]


def load():
    spec = importlib.util.spec_from_file_location("check_version_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(SCRIPT.is_file() and (ROOT / "desktop" / "package-lock.json").is_file(),
                     "the desktop app is not part of this copy")
class CheckVersionTests(unittest.TestCase):
    def setUp(self):
        self.module = load()
        self.copy = Path(tempfile.mkdtemp(prefix="mobster-check-version-"))
        self.addCleanup(shutil.rmtree, self.copy, True)
        for name in FILES:
            (self.copy / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, self.copy / name)
        self.module.ROOT = self.copy

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.module.main(["check-version.py", *argv])
        return code, out.getvalue()

    def edit_lock(self, change):
        path = self.copy / "desktop" / "package-lock.json"
        lock = json.loads(path.read_text())
        change(lock)
        path.write_text(json.dumps(lock, indent=2) + "\n")

    def test_this_trees_versions_agree(self):
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertIn("desktop/package-lock.json", out)
        self.assertIn('desktop/package-lock.json packages[""]', out)

    def test_a_stale_lock_root_fails(self):
        self.edit_lock(lambda lock: lock.update(version="0.0.1"))
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("::error file=desktop/package-lock.json::version 0.0.1, but tauri.conf.json says", out)

    def test_a_stale_lock_package_entry_fails_and_names_the_file(self):
        self.edit_lock(lambda lock: lock["packages"][""].update(version="0.0.1"))
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn('::error file=desktop/package-lock.json::packages[""]: version 0.0.1', out)

    def test_a_tag_ahead_of_every_file_names_each_one(self):
        code, out = self.run_main("v99.0.0")
        self.assertEqual(code, 1)
        self.assertEqual(out.count("::error file="), 6, out)
        self.assertIn("but tag v99.0.0 says 99.0.0", out)


if __name__ == "__main__":
    unittest.main()

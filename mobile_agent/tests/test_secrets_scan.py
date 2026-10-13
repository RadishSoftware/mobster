"""The secret scan covers fixtures as well as production files. No real credentials."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".gitleaks.toml"


@unittest.skipUnless(shutil.which("gitleaks") and CONFIG.exists(), "maintainer secret scan")
class SecretsScanTests(unittest.TestCase):
    def test_bundled_archives_are_scanned(self):
        key = "AIza" + hashlib.sha256(b"synthetic archive regression").hexdigest()[:35]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bundle.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("assets/config.js", f'API_KEY = "{key}"\n')
            result = subprocess.run(
                ["gitleaks", "dir", str(archive), "--config", str(CONFIG), "--redact", "--no-banner",
                 "--max-archive-depth", "3", "--log-level", "error"],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertNotIn(key, result.stdout + result.stderr)

    def test_fixture_paths_do_not_bypass_the_scan(self):
        key = "AIza" + hashlib.sha256(b"synthetic scanner regression").hexdigest()[:35]
        paths = ("tests/config.py", "examples/demo.py", "dashboard/qa/demo.tsx",
                 "src/client.test.ts", "src/fake-client.ts", "relay/vitest.config.ts")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in paths:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'API_KEY = "{key}"\n')
            report = root / "report.json"
            result = subprocess.run(
                ["gitleaks", "dir", str(root), "--config", str(CONFIG), "--redact", "--no-banner",
                 "--report-format", "json", "--report-path", str(report), "--log-level", "error"],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 1, result.stderr)
            found = {Path(row["File"]).relative_to(root).as_posix() for row in json.loads(report.read_text())}
            self.assertEqual(found, set(paths))

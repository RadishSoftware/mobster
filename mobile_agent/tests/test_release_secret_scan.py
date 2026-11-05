import contextlib
import gzip
import importlib.util
import io
import os
import shutil
import tarfile
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "scripts" / "scan_release_secrets.py"

GITHUB_TOKEN = "ghp_" + "Zq7Rw2Lp9Xk4Vb8Nm3Hd6Ty1Gc5Fs0JeWu4K"
OWN_VALUE = "q7Rw2Lp9Xk4Vb8Nm3Hd6Ty1Gc5Fs0Je"


def load_scanner():
    spec = importlib.util.spec_from_file_location("scan_release_secrets_under_test", SCANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def binary_with(payload):
    return b"\xcf\xfa\xed\xfe" + b"\0" * 64 + zlib.compress(b"\0\0token = '" + payload + b"'\0" * 8) + b"\0" * 64


def tarball(path, members):
    with tarfile.open(path, "w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


@unittest.skipUnless(SCANNER.is_file(), "the release scan is maintainer tooling and is not part of this copy")
class ReleaseSecretScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scanner = load_scanner()

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.root = self.scratch / "checkout"
        self.root.mkdir()
        patcher = mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scan(self, *paths, gitleaks=True):
        printed = io.StringIO()
        which = shutil.which if gitleaks else (lambda name: None)
        with contextlib.redirect_stdout(printed), mock.patch.object(self.scanner.shutil, "which", which):
            status = self.scanner.main(["--root", str(self.root), *map(str, paths)])
        return status, printed.getvalue()

    def app(self, files):
        app = self.scratch / "Mobster.app"
        for name, data in files.items():
            path = app / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return app

    @unittest.skipUnless(shutil.which("gitleaks"), "gitleaks is not installed")
    def test_a_clean_build_with_publishable_keys_passes(self):
        app = self.app({
            "Contents/Info.plist": b"<plist><dict><key>CFBundleShortVersionString</key><string>0.4.0</string>"
                                   b"</dict></plist>",
            "Contents/MacOS/mobster-agent": binary_with(b"nothing secret here"),
            "Contents/Resources/config.js": ('const key = "sb_' + 'publishable_' + 'q7Rw2Lp9Xk4Vb8Nm3Hd6Ty";'
                                             ' const pk = "pk_' + 'live_' + '51Hq7Rw2Lp9Xk4Vb8Nm3Hd6Ty1Gc5";').encode(),
            "Contents/Resources/.env.example": b"ANTHROPIC_API_KEY=\n",
        })
        status, printed = self.scan(app)
        self.assertEqual(status, 0, printed)
        self.assertIn("PASS", printed)

    def test_a_key_inside_a_frozen_module_inside_the_tarball_fails_without_printing_it(self):
        cli = self.scratch / "mobster-macos-arm64.tar.gz"
        tarball(cli, {"mobster-macos-arm64/mobster": binary_with(GITHUB_TOKEN.encode())})
        status, printed = self.scan(cli)
        self.assertEqual(status, 1, printed)
        self.assertIn("FAIL patterns", printed)
        self.assertIn("mobster-macos-arm64.tar.gz!mobster-macos-arm64/mobster@zlib:", printed)
        self.assertIn("a GitHub token: ghp_... sha256:", printed)
        self.assertNotIn(GITHUB_TOKEN[4:], printed)

    def test_a_key_alone_in_a_short_zlib_stream_fails(self):
        blob = b"\xcf\xfa\xed\xfe" + b"\0" * 64 + zlib.compress(b"'" + GITHUB_TOKEN.encode() + b"'") + b"\0" * 64
        cli = self.scratch / "mobster-macos-arm64.tar.gz"
        tarball(cli, {"mobster-macos-arm64/mobster": blob})
        status, printed = self.scan(cli)
        self.assertEqual(status, 1, printed)
        self.assertIn("a GitHub token: ghp_... sha256:", printed)

    def test_one_of_the_maintainers_own_values_in_a_zip_member_fails(self):
        secrets = self.scratch / "bench.env"
        secrets.write_text(f"SUBQ_API_KEY={OWN_VALUE}\n")
        bundle = self.scratch / "mobster.mcpb"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("server/mobster-mcp", gzip.compress(f"#!/bin/sh\nX={OWN_VALUE}\n".encode()))
        with mock.patch.dict(os.environ, {"MOBSTER_SECRET_FILES": str(secrets)}):
            status, printed = self.scan(bundle)
        self.assertEqual(status, 1, printed)
        self.assertIn("FAIL values", printed)
        self.assertIn("the value of SUBQ_API_KEY from bench.env", printed)
        self.assertNotIn(OWN_VALUE, printed)

    def test_key_files_env_files_and_secret_folders_fail_by_their_names(self):
        app = self.app({
            "Contents/Resources/.env": b"",
            "Contents/Resources/AuthKey_ABCDE12345.p8": b"",
            "Contents/Resources/mobster-updater.key": b"",
            "Contents/Resources/.vercel/project.json": b"{}",
            "Contents/Resources/.env.example": b"",
        })
        status, printed = self.scan(app)
        self.assertEqual(status, 1, printed)
        self.assertIn("FAIL names", printed)
        for name in (".env:", "AuthKey_ABCDE12345.p8", "mobster-updater.key", ".vercel"):
            self.assertIn(name, printed)
        self.assertNotIn(".env.example", printed)

    def test_without_gitleaks_the_scan_refuses(self):
        status, printed = self.scan(self.app({"Contents/Info.plist": b"<plist/>"}), gitleaks=False)
        self.assertEqual(status, 1, printed)
        self.assertIn("FAIL gitleaks", printed)
        self.assertIn("brew install gitleaks", printed)

    def test_a_missing_input_is_an_error_not_a_pass(self):
        status, _ = self.scan(self.scratch / "Mobster_0.4.0_Apple-Silicon.dmg")
        self.assertEqual(status, 2)


if __name__ == "__main__":
    unittest.main()

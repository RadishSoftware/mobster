"""packaging/install.sh against a file:// release in temporary folders: it installs, links, and refuses what it must.

The platform, the signature check and (in one test) curl are faked through PATH, so these run on any
Mac or Linux box without the network, Xcode or a real release.
"""

import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "packaging" / "install.sh"
ASSET = "mobster-macos-arm64.tar.gz"

FAKES = {
    "uname": '#!/bin/sh\ncase "$1" in -s) echo "${FAKE_UNAME_S:-Darwin}" ;; -m) echo "${FAKE_UNAME_M:-arm64}" ;; '
             '*) echo "${FAKE_UNAME_S:-Darwin}" ;; esac\n',
    "sysctl": '#!/bin/sh\necho "${FAKE_ARM64:-1}"\n',
    # --verify exits FAKE_CODESIGN_EXIT; -dv prints the team the way codesign does, on stderr.
    "codesign": '#!/bin/sh\nfor a in "$@"; do [ "$a" = "-dv" ] && { echo "TeamIdentifier=${FAKE_TEAM:-not set}" >&2; '
                'exit 0; }; done\nexit "${FAKE_CODESIGN_EXIT:-0}"\n',
}


def tarball(version="0.2.0", layout="mobster-macos-arm64"):
    """A release tarball whose mobster prints its version, as the real one does."""
    buffer = io.BytesIO()
    files = {
        f"{layout}/mobster": (f'#!/bin/sh\necho "mobster {version}"\n', 0o755),
        f"{layout}/VERSION": (f"{version}\n", 0o644),
        f"{layout}/LICENSE": ("MIT\n", 0o644),
        f"{layout}/_internal/base_library.zip": ("x", 0o644),
    }
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, (text, mode) in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@unittest.skipUnless(INSTALL.is_file(), "packaging is not part of this copy")
@unittest.skipUnless(shutil.which("shasum", path="/usr/bin:/bin") and shutil.which("curl", path="/usr/bin:/bin"),
                     "needs shasum and curl in /usr/bin")
class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mobster-install-test-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.release = self.folder / "release"
        self.release.mkdir()
        self.publish(tarball())
        self.fakes = self.folder / "fakebin"
        self.fakes.mkdir()
        for name, text in FAKES.items():
            (self.fakes / name).write_text(text)
            (self.fakes / name).chmod(0o755)
        self.home = self.folder / "home"
        self.home.mkdir()
        self.mobster_home = self.folder / "mobster-home"
        self.bin_dir = self.folder / "bin"

    def publish(self, data, sha=None):
        (self.release / ASSET).write_bytes(data)
        digest = sha or hashlib.sha256(data).hexdigest()
        (self.release / f"{ASSET}.sha256").write_text(f"{digest}  {ASSET}\n")

    def run_install(self, script=None, path_extra=(), piped=None, **env):
        """Run the installer as `sh script`, or with piped text on sh's stdin, the way `curl … | sh` runs it."""
        environment = {
            "PATH": os.pathsep.join([str(self.fakes), *map(str, path_extra), "/usr/bin", "/bin", "/usr/sbin"]),
            "HOME": str(self.home),
            "TMPDIR": str(self.folder),
            "MOBSTER_HOME": str(self.mobster_home),
            "MOBSTER_BIN_DIR": str(self.bin_dir),
            "MOBSTER_INSTALL_BASE_URL": self.release.as_uri(),
        }
        environment.update({key: value for key, value in env.items() if value is not None})
        for key in [key for key, value in env.items() if value is None]:
            environment.pop(key, None)
        if piped is not None:
            return subprocess.run(["/bin/sh"], input=piped, env=environment, capture_output=True, text=True, timeout=60)
        return subprocess.run(["/bin/sh", str(script or INSTALL)], env=environment, capture_output=True, text=True,
                              timeout=60)

    def snapshot(self):
        """Every path under MOBSTER_HOME and the bin folder, with its link target or content hash."""
        state = {}
        for base in (self.mobster_home, self.bin_dir):
            for folder, dirs, files in os.walk(base):
                for name in dirs + files:
                    path = Path(folder) / name
                    key = str(path.relative_to(self.folder))
                    if path.is_symlink():
                        state[key] = ("link", os.readlink(path))
                    elif path.is_dir():
                        state[key] = ("dir",)
                    else:
                        state[key] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        return state

    def assertNothingInstalled(self):
        self.assertFalse((self.mobster_home / "versions" / "0.2.0").exists())
        self.assertFalse((self.bin_dir / "mobster").exists())
        self.assertFalse((self.mobster_home / "current").exists())

    def test_a_good_release_installs_and_links_into_the_chosen_folders(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        link = self.bin_dir / "mobster"
        self.assertEqual(os.readlink(link), str(self.mobster_home / "current" / "mobster"))
        self.assertEqual(os.readlink(self.mobster_home / "current"), "versions/0.2.0")
        self.assertTrue((self.mobster_home / "versions" / "0.2.0" / "_internal").is_dir())
        self.assertEqual(subprocess.run([str(link)], capture_output=True, text=True).stdout, "mobster 0.2.0\n")
        self.assertIn("mobster 0.2.0", result.stdout)
        for step in ("mobster login", "mobster mcp install --all", "mobster test"):
            self.assertIn(step, result.stdout)
        self.assertIn(f'export PATH="{self.bin_dir}:$PATH"', result.stdout)
        self.assertEqual([p.name for p in (self.mobster_home / "versions").iterdir()], ["0.2.0"])
        self.assertFalse(any(p.name.startswith("mobster-install.") for p in self.folder.iterdir()))

    def test_running_it_again_upgrades_in_place(self):
        self.assertEqual(self.run_install().returncode, 0)
        self.publish(tarball("0.2.1"))
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(self.mobster_home / "current"), "versions/0.2.1")
        self.assertIn("mobster 0.2.1", result.stdout)
        self.assertEqual(sorted(p.name for p in (self.mobster_home / "versions").iterdir()), ["0.2.0", "0.2.1"])
        self.assertEqual(self.run_install().returncode, 0, "reinstalling the same version works")

    def test_a_bad_sha256_installs_nothing(self):
        self.publish(tarball(), sha="0" * 64)
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("doesn't match its SHA-256", result.stderr)
        self.assertNothingInstalled()

    def test_a_sha256_file_without_a_hash_installs_nothing(self):
        self.publish(tarball(), sha="not-a-hash")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("doesn't hold a SHA-256", result.stderr)
        self.assertNothingInstalled()

    def test_linux_is_refused_before_any_download(self):
        result = self.run_install(FAKE_UNAME_S="Linux", FAKE_UNAME_M="x86_64",
                                  MOBSTER_INSTALL_BASE_URL=(self.folder / "missing").as_uri())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("macOS only", result.stderr)
        self.assertNotIn("Downloading", result.stdout)
        self.assertNothingInstalled()

    def test_an_intel_mac_is_refused(self):
        result = self.run_install(FAKE_UNAME_M="x86_64", FAKE_ARM64="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Apple silicon", result.stderr)
        self.assertNothingInstalled()

    def test_an_apple_silicon_mac_under_rosetta_installs(self):
        result = self.run_install(FAKE_UNAME_M="x86_64", FAKE_ARM64="1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_signature_that_does_not_verify_installs_nothing(self):
        result = self.run_install(FAKE_CODESIGN_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("signature doesn't verify", result.stderr)
        self.assertNothingInstalled()
        self.assertEqual([p.name for p in (self.mobster_home / "versions").iterdir()], [])

    def test_the_release_team_is_required_once_the_release_step_sets_it(self):
        script = self.folder / "install-release.sh"
        text = INSTALL.read_text()
        self.assertEqual(text.count('EXPECTED_TEAM_ID=""'), 1, "the repository copy leaves the team empty")
        script.write_text(text.replace('EXPECTED_TEAM_ID=""', 'EXPECTED_TEAM_ID="TEAM123456"'))
        wrong = self.run_install(script, FAKE_TEAM="OTHER00000")
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("signed by team OTHER00000", wrong.stderr)
        self.assertNothingInstalled()
        right = self.run_install(script, FAKE_TEAM="TEAM123456")
        self.assertEqual(right.returncode, 0, right.stderr)

    def test_the_deployed_copy_refuses_a_release_whose_sha256_it_wasnt_published_with(self):
        """Review perf-security-2: the SHA-256 beside the release only catches a damaged download. The deployed
        copy carries the expected one, from another origin."""
        text = INSTALL.read_text()
        for name in ("DEFAULT_VERSION", "EXPECTED_SHA256"):
            self.assertEqual(text.count(f'{name}=""'), 1, f"the repository copy leaves {name} empty")
        published = hashlib.sha256((self.release / ASSET).read_bytes()).hexdigest()
        script = self.folder / "install-release.sh"
        script.write_text(text.replace('EXPECTED_SHA256=""', f'EXPECTED_SHA256="{"0" * 64}"'))
        wrong = self.run_install(script)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("isn't the one this installer was published with", wrong.stderr)
        self.assertNothingInstalled()
        script.write_text(text.replace('EXPECTED_SHA256=""', f'EXPECTED_SHA256="{published}"'))
        self.assertEqual(self.run_install(script).returncode, 0)

    def test_downloads_are_https_only(self):
        text = INSTALL.read_text()
        downloads = [line for line in text.splitlines() if line.strip().startswith("curl ")]
        self.assertEqual(len(downloads), 2)
        for line in downloads:
            self.assertIn("--proto '=https,file' --proto-redir '=https' --tlsv1.2", line)

    def test_a_tarball_without_the_release_layout_is_refused(self):
        self.publish(tarball(layout="something-else"))
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("isn't a Mobster release", result.stderr)
        self.assertNothingInstalled()

    def test_another_installs_mobster_is_left_alone(self):
        uv_tool = self.home / ".local" / "share" / "uv" / "tools" / "mobster-cli" / "bin" / "mobster"
        uv_tool.parent.mkdir(parents=True)
        uv_tool.write_text("#!/bin/sh\necho old\n")
        self.bin_dir.mkdir()
        (self.bin_dir / "mobster").symlink_to(uv_tool)
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uv tool uninstall mobster-cli", result.stderr)
        self.assertEqual(os.readlink(self.bin_dir / "mobster"), str(uv_tool))

    def test_it_names_a_mobster_that_comes_first_on_path(self):
        earlier = self.folder / "earlier"
        earlier.mkdir()
        (earlier / "mobster").write_text("#!/bin/sh\necho old\n")
        (earlier / "mobster").chmod(0o755)
        result = self.run_install(path_extra=(earlier, self.bin_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Another mobster comes first on your PATH: {earlier / 'mobster'}", result.stdout)
        self.assertNotIn("isn't on your PATH", result.stdout)

    def test_a_pinned_version_downloads_that_release_from_github(self):
        (self.fakes / "curl").write_text('#!/bin/sh\nfor a in "$@"; do case "$a" in http*) echo "$a" >> '
                                         f'"{self.folder}/curl.log" ;; esac; done\nexit 22\n')
        (self.fakes / "curl").chmod(0o755)
        result = self.run_install(MOBSTER_INSTALL_BASE_URL=None, MOBSTER_VERSION="0.2.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.folder / "curl.log").read_text().splitlines()[0],
                         "https://github.com/RadishSoftware/mobster/releases/download/v0.2.0/" + ASSET)
        result = self.run_install(MOBSTER_INSTALL_BASE_URL=None)
        self.assertEqual((self.folder / "curl.log").read_text().splitlines()[-1],
                         "https://github.com/RadishSoftware/mobster/releases/latest/download/" + ASSET)

    def test_piped_into_sh_it_installs(self):
        result = self.run_install(piped=INSTALL.read_text())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(self.mobster_home / "current"), "versions/0.2.0")
        self.assertIn("mobster 0.2.0", result.stdout)

    def test_a_download_cut_short_changes_nothing(self):
        """curl … | sh runs what has arrived. Cut anywhere before the last line, the script must run nothing,
        here on the reinstall path, where a partial run would move the installed version aside."""
        self.assertEqual(self.run_install().returncode, 0)
        before = self.snapshot()
        self.assertIn(str((self.mobster_home / "current").relative_to(self.folder)), before)
        text = INSTALL.read_text()
        last_line = text.rstrip("\n").rfind("\n") + 1
        self.assertEqual(text[last_line:], 'main "$@"\n')
        cuts = sorted({i + 1 for i, c in enumerate(text[:last_line]) if c == "\n"}
                      | {start + (end - start) // 2 for start, end in self.lines(text[:last_line])})
        self.assertGreater(len(cuts), 200)
        for cut in cuts:
            result = self.run_install(piped=text[:cut])
            self.assertNotIn("Downloading", result.stdout, f"cut at byte {cut} ran the installer")
            self.assertEqual(self.snapshot(), before, f"cut at byte {cut} changed MOBSTER_HOME or the bin folder")
        self.assertEqual(self.run_install(piped=text).returncode, 0, "the whole script still reinstalls")

    @staticmethod
    def lines(text):
        start = 0
        for end, char in enumerate(text):
            if char == "\n":
                yield start, end
                start = end + 1

    def test_the_script_is_posix_sh_and_never_uses_sudo_or_keys(self):
        text = INSTALL.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("set -eu", text)
        self.assertIn("\nmain() {\n", text)
        self.assertTrue(text.endswith('\n}\n\nmain "$@"\n'), "nothing may act before the whole script has arrived")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"\bsudo\b")
        self.assertNotRegex(code, r"(?i)api_key|\.env\b|OPENAI|TYPESAFE")
        dash = shutil.which("dash") or shutil.which("sh", path="/bin")
        self.assertEqual(subprocess.run([dash, "-n", str(INSTALL)]).returncode, 0)


if __name__ == "__main__":
    unittest.main()

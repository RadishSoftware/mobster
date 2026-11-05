import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "packaging"
SHA = "3f" * 32


def load_render():
    spec = importlib.util.spec_from_file_location("mobster_formula_render", PACKAGING / "homebrew" / "render.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless((PACKAGING / "homebrew" / "render.py").is_file(), "packaging is not part of this copy")
class FormulaTests(unittest.TestCase):
    def setUp(self):
        self.render = load_render()

    def test_rendering_fills_every_placeholder(self):
        template = (PACKAGING / "homebrew" / "mobster.rb.in").read_text()
        self.assertEqual(sorted(set(re.findall(r"@[A-Z0-9_]+@", template))), ["@SHA256@", "@URL@", "@VERSION@"])
        text = self.render.render("0.2.0", SHA)
        self.assertNotRegex(text, r"@[A-Z0-9_]+@")
        self.assertIn('version "0.2.0"', text)
        self.assertIn(f'sha256 "{SHA}"', text)
        self.assertIn('url "https://github.com/RadishSoftware/mobster-releases/releases/download/v0.2.0/'
                      'mobster-macos-arm64.tar.gz"', text)

    def test_the_formula_installs_the_folder_and_links_the_command(self):
        text = self.render.render("0.2.0", SHA, "file:///tmp/release/mobster-macos-arm64.tar.gz")
        self.assertIn('url "file:///tmp/release/mobster-macos-arm64.tar.gz"', text)
        for line in ('class Mobster < Formula', 'license "MIT"', "depends_on arch: :arm64",
                     "depends_on macos: :ventura", 'libexec.install Dir["*"]', 'bin.install_symlink libexec/"mobster"',
                     'assert_match version.to_s, shell_output("#{bin}/mobster version")'):
            self.assertIn(line, text)
        self.assertLess(len(re.search(r'desc "([^"]+)"', text)[1]), 80, "Homebrew caps desc at 80 characters")

    def test_the_formula_caveats_lead_with_login_then_agents_then_the_simulator(self):
        caveats = self.render.render("0.2.0", SHA)
        caveats = caveats[caveats.index("def caveats"):]
        order = [caveats.index(command) for command in ("mobster login", "mobster mcp install --all",
                                                        "mobster doctor", "mobster sim doctor")]
        self.assertEqual(order, sorted(order))

    def test_an_unfilled_placeholder_writes_nothing(self):
        with self.assertRaisesRegex(self.render.RenderError, "@TEAM@"):
            self.render.render("0.2.0", SHA, template='url "@URL@"\nteam "@TEAM@"\n')

    def test_a_trailing_line_break_is_refused(self):
        for version, url in (("0.4.0\n", "https://example.com/release"),
                             ("0.4.0", "https://example.com/release\n")):
            with self.subTest(version=version, url=url), self.assertRaises(self.render.RenderError):
                self.render.render(version, SHA, url)

    def test_the_command_reads_the_sha256_file_and_writes_the_formula(self):
        with tempfile.TemporaryDirectory() as folder:
            sha_file = Path(folder) / "mobster-macos-arm64.tar.gz.sha256"
            sha_file.write_text(f"{SHA.upper()}  mobster-macos-arm64.tar.gz\n")
            out = Path(folder) / "mobster.rb"
            self.assertEqual(self.render.main(["--version", "0.2.0", "--sha256", str(sha_file), "--out", str(out)]), 0)
            self.assertIn(f'sha256 "{SHA}"', out.read_text())
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(self.render.main(["--version", "0.2.0", "--sha256", "nope"]), 1)
            self.assertIn("64 hex", err.getvalue())

    def test_malformed_values_are_refused(self):
        for version, sha, url in [("v0.2.0", SHA, None), ("0.2", SHA, None), ("0.2.0", "abc", None),
                                  ("0.2.0", "g" * 64, None), ("0.2.0", SHA, "http://example.com/a.tar.gz"),
                                  ("0.2.0", SHA, 'https://example.com/a"; system "x'),
                                  ("0.2.0", SHA, "https://example.com/a b.tar.gz"),
                                  ("0.2.0", SHA, "file://relative/a.tar.gz")]:
            with self.subTest(version=version, sha=sha, url=url), self.assertRaises(self.render.RenderError):
                self.render.render(version, sha, url)

    def test_empty_checksum_file_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            empty = Path(folder) / "empty.sha256"
            empty.write_text("")
            out = Path(folder) / "mobster.rb"
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(self.render.main(["--version", "0.4.0", "--sha256", str(empty),
                                                   "--out", str(out)]), 1)
            self.assertIn("64 hex", err.getvalue())
            self.assertFalse(out.exists())

    def test_app_cask_flags_are_refused_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "mobster.rb"
            for flag in ("--app", "--cask"):
                with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.render.main([flag, "--version", "0.4.0", "--sha256", SHA, "--out", str(out)])
                self.assertFalse(out.exists())


@unittest.skipUnless((PACKAGING / "cli" / "mobster.spec").is_file(), "packaging is not part of this copy")
class FrozenBuildTests(unittest.TestCase):
    def spec(self):
        calls = {}

        def record(name):
            def call(*args, **kwargs):
                calls[name] = (args, kwargs)
                return types.SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[], name=name)
            return call

        names = {name: record(name) for name in ("Analysis", "PYZ", "EXE", "COLLECT", "BUNDLE")}
        names["SPECPATH"] = str(PACKAGING / "cli")
        exec(compile((PACKAGING / "cli" / "mobster.spec").read_text(), "mobster.spec", "exec"), names)
        return calls

    def test_the_developer_commands_and_yaml_are_named_for_the_freezer(self):
        hidden = self.spec()["Analysis"][1]["hiddenimports"]
        for module in ("yaml", "mobile_agent.devtools", "mobile_agent.verify.cli", "mobile_agent.sim.cli",
                       "mobile_agent.mcp_server.cli", "PIL.JpegImagePlugin"):
            self.assertIn(module, hidden)

    def test_every_app_pack_is_named_for_the_freezer(self):
        from mobile_agent import harness_api
        from mobile_agent.packs import register
        hidden = self.spec()["Analysis"][1]["hiddenimports"]
        for module in [harness_api.HOOKS_MODULE, *[f"{register.__package__}.{name}" for name in register.MODULES]]:
            self.assertIsNotNone(importlib.util.find_spec(module), module)
            self.assertIn(module, hidden)

    def test_no_other_code_imports_by_name(self):
        skip = {"tests", "bench", "evals"}
        sites = sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "mobile_agent").rglob("*.py")
                       if not skip & set(path.relative_to(ROOT).parts) and ".venv" not in path.parts
                       and "import_module(" in path.read_text())
        self.assertEqual(sites, ["mobile_agent/harness_api.py", "mobile_agent/packs/register.py"],
                         "a new importlib.import_module call: name its modules in packaging/cli/mobster.spec "
                         "and test_every_app_pack_is_named_for_the_freezer")

    def test_app_only_modules_stay_out_of_the_cli(self):
        spec = self.spec()["Analysis"][1]
        for module in ("cryptography", "_cffi_backend", "mobile_agent.remote", "mobile_agent.teams"):
            self.assertIn(module, spec["excludes"])
        self.assertFalse([m for m in spec["hiddenimports"] if m.startswith("mobile_agent.teams")])

    def test_pymobiledevice3_is_checked_before_and_after_freezing(self):
        self.assertIn("pymobiledevice3", self.spec()["Analysis"][1]["excludes"])
        text = (PACKAGING / "cli" / "build.sh").read_text()
        check = '"$PYTHON" "$ROOT/scripts/check_no_pymobiledevice3.py"'
        freeze = '"$PYTHON" -m PyInstaller'
        self.assertLess(text.index(check), text.index(freeze))
        self.assertGreater(text.index(f'{check} "$STAGE"'), text.index(freeze))
        after = text[text.index(f'{check} "$STAGE"'):]
        self.assertLess(after.index('rm -rf "$STAGE" "$WORK"'), after.index("fail "))
        self.assertIn("couldn't read the frozen", after)

    def test_build_ocr_has_its_source_in_the_bundle(self):
        datas = self.spec()["Analysis"][1]["datas"]
        self.assertIn((str(ROOT / "mobile_agent" / "swift" / "ocr.swift"), "mobile_agent/swift"), datas)
        self.assertTrue((ROOT / "mobile_agent" / "swift" / "ocr.swift").is_file())

    def test_the_terminal_ui_stays_in_and_the_entry_belongs_to_the_cli(self):
        calls = self.spec()
        excludes = calls["Analysis"][1]["excludes"]
        for module in ("textual", "rich", "mobile_agent.tui.app", "mobile_agent.tui.render"):
            self.assertNotIn(module, excludes)
        self.assertIn("requests", excludes)
        entry = Path(calls["Analysis"][0][0][0])
        self.assertEqual(entry, PACKAGING / "cli" / "entry.py")
        self.assertTrue(entry.is_file())

    def test_it_is_one_folder_named_mobster_with_no_app_bundle(self):
        calls = self.spec()
        self.assertNotIn("BUNDLE", calls)
        self.assertEqual(calls["COLLECT"][1]["name"], "mobster")
        self.assertEqual(calls["EXE"][1]["name"], "mobster")
        self.assertFalse(calls["EXE"][1]["upx"])
        script = (PACKAGING / "cli" / "build.sh").read_text()
        self.assertIn('"$WORK/dist/mobster" "$STAGE"', script)

    def test_the_build_script_signs_checks_and_smokes(self):
        script = (PACKAGING / "cli" / "build.sh").read_text()
        for needle in ("packaging/cli/build-requirements.txt", "packaging/cli/entitlements.plist",
                       "codesign --verify --strict",
                       "--options runtime --timestamp --entitlements", "--sign -", "vtool -show-build",
                       'MIN_MACOS="13.0"', "version --json", "verify --help", "tools/list",
                       "sim doctor --json", "shasum -a 256", '"$DIST/VERSION"',
                       "THIRD_PARTY_NOTICES.md", "mobster-macos-arm64", "MOBSTER_DATA_DIR=", "import yaml",
                       "verify --check", "packaging/cli/notarize.sh", "notarization wasn't Accepted",
                       "codesign -dv --verbose=2", "Authority|TeamIdentifier", "Mach-O files, signed",
                       "uv venv --seed --python 3.12.13", '"$BIN" --help', '"$BIN" demo --json',
                       "_internal/mobile_agent/swift/ocr.swift", "leaves on ctrl+d"):
            self.assertIn(needle, script)
        self.assertFalse("xcrun notarytool" in script, "build.sh notarizes only through notarize.sh")
        notarize = (PACKAGING / "cli" / "notarize.sh").read_text()
        for needle in ("notarytool submit", "--wait --output-format json", '"$status" != Accepted', "notarytool log",
                       "exit 1"):
            self.assertIn(needle, notarize)
        self.assertTrue(os.access(PACKAGING / "cli" / "notarize.sh", os.X_OK))
        self.assertRegex(script, r'mcp"?,? "?--keyless', "the MCP smoke runs key-less, with no model key")
        self.assertTrue(os.access(PACKAGING / "cli" / "build.sh", os.X_OK))


FAKE_XCRUN = """#!/bin/sh
echo "$*" >> "$FAKE_CALLS"
[ "$1" = notarytool ] || exit 64
case "$2" in
  submit) printf '%s' "$FAKE_NOTARY_OUT"; exit "${FAKE_NOTARY_EXIT:-0}" ;;
  log) echo "log of $3: The binary is not signed with a valid Developer ID certificate." ;;
esac
"""


@unittest.skipUnless((PACKAGING / "cli" / "notarize.sh").is_file(), "packaging is not part of this copy")
@unittest.skipUnless(shutil.which("plutil", path="/usr/bin"), "notarize.sh reads JSON with macOS's plutil")
class NotarizeTests(unittest.TestCase):

    ID = "2efe2717-52ef-43a5-96dc-0797e4ca1041"

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mobster-notarize-test-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        bin_dir = self.folder / "bin"
        bin_dir.mkdir()
        (bin_dir / "xcrun").write_text(FAKE_XCRUN)
        (bin_dir / "xcrun").chmod(0o755)
        self.zip = self.folder / "mobster-macos-arm64-notarize.zip"
        self.zip.write_bytes(b"PK")
        self.calls = self.folder / "calls.txt"
        self.env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(self.folder), "FAKE_CALLS": str(self.calls),
                    "APPLE_API_ISSUER": "issuer-id", "APPLE_API_KEY": "KEYID", "APPLE_API_KEY_PATH": "/keys/k.p8"}

    def notarize(self, out, code=0, **env):
        done = subprocess.run(["sh", str(PACKAGING / "cli" / "notarize.sh"), str(self.zip)],
                              env={**self.env, "FAKE_NOTARY_OUT": out, "FAKE_NOTARY_EXIT": str(code), **env},
                              capture_output=True, text=True, timeout=30)
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        return done.returncode, done.stderr, calls

    def test_accepted_passes(self):
        code, err, calls = self.notarize(f'{{"id":"{self.ID}","message":"Processing complete","status":"Accepted"}}')
        self.assertEqual(code, 0, err)
        self.assertIn(f"Accepted, submission {self.ID}", err)
        self.assertEqual(len(calls), 1)
        self.assertIn("--wait --output-format json", calls[0])
        self.assertIn("--key /keys/k.p8 --key-id KEYID --issuer issuer-id", calls[0])

    def test_invalid_fails_and_prints_the_log_even_when_notarytool_exits_0(self):
        code, err, calls = self.notarize(f'{{"id":"{self.ID}","message":"Processing complete","status":"Invalid"}}')
        self.assertEqual(code, 1)
        self.assertIn(f"notarytool log {self.ID} --key /keys/k.p8", calls[-1])
        self.assertIn("not signed with a valid Developer ID", err)
        self.assertIn(f"the submission {self.ID} ended with status 'Invalid', not Accepted", err)

    def test_an_unfinished_wait_fails(self):
        code, err, _ = self.notarize(f'{{"id":"{self.ID}","status":"In Progress"}}', code=0)
        self.assertEqual(code, 1)
        self.assertIn("status 'In Progress'", err)

    def test_no_json_fails_without_a_log_call(self):
        code, err, calls = self.notarize("Error: HTTP status code: 401. Unable to authenticate.", code=69)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1, "no id, so no log call")
        self.assertIn("status 'none'", err)

    def test_missing_key_or_zip_is_a_usage_error(self):
        code, err, calls = self.notarize("{}", APPLE_API_KEY="", APPLE_API_KEY_ID="")
        self.assertEqual(code, 2)
        self.assertIn("APPLE_API_KEY_ID", err)
        self.assertEqual(calls, [])
        self.zip.unlink()
        self.assertEqual(self.notarize("{}")[0], 2)


TESTS = Path(__file__).resolve().parent


class PrivacyFixTests(unittest.TestCase):
    def test_the_developer_mode_fixture_names_a_generic_architecture(self):
        text = (TESTS / "setup_errors" / "developer_mode.txt").read_text()
        self.assertIn("arch:arm64,", text)
        self.assertNotRegex(text, r"arch:arm64[a-z]")


if __name__ == "__main__":
    unittest.main()

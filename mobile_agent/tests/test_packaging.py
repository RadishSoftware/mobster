import contextlib
import importlib.util
import io
import json
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

    def test_an_unfilled_placeholder_writes_nothing(self):
        with self.assertRaisesRegex(self.render.RenderError, "@TEAM@"):
            self.render.render("0.2.0", SHA, template='url "@URL@"\nteam "@TEAM@"\n')

    def test_a_trailing_line_break_is_refused_for_every_template(self):
        for kind in ("formula", "cask", "app"):
            for version, url in (("0.4.0\n", "https://example.com/release"),
                                 ("0.4.0", "https://example.com/release\n")):
                with self.subTest(kind=kind, version=version, url=url), self.assertRaises(self.render.RenderError):
                    self.render.render(version, SHA, url, kind=kind)

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


@unittest.skipUnless((PACKAGING / "homebrew" / "mobster-cask.rb.in").is_file(), "packaging is not part of this copy")
class CaskTests(unittest.TestCase):

    def setUp(self):
        self.render = load_render()

    def test_rendering_fills_every_placeholder(self):
        template = (PACKAGING / "homebrew" / "mobster-cask.rb.in").read_text()
        self.assertEqual(sorted(set(re.findall(r"@[A-Z0-9_]+@", template))), ["@SHA256@", "@URL@", "@VERSION@"])
        text = self.render.render("0.2.0", SHA, kind="cask")
        self.assertNotRegex(text, r"@[A-Z0-9_]+@")
        self.assertIn('version "0.2.0"', text)
        self.assertIn(f'sha256 "{SHA}"', text)
        self.assertIn('url "https://github.com/RadishSoftware/mobster-releases/releases/download/v#{version}/'
                      'mobster-macos-arm64.tar.gz"', text)

    def test_the_cask_links_the_bootloader_inside_the_tarball_folder(self):
        text = self.render.render("0.2.0", SHA, "file:///tmp/release/mobster-macos-arm64.tar.gz", kind="cask")
        self.assertTrue(text.startswith('cask "mobster" do\n'), text[:40])
        for line in ('url "file:///tmp/release/mobster-macos-arm64.tar.gz"', 'name "Mobster CLI"',
                     'homepage "https://mobster.dev/"', "depends_on arch: :arm64", "depends_on macos: :ventura",
                     'binary "mobster-macos-arm64/mobster"'):
            self.assertIn(line, text)
        for formula_only in ("< Formula", "def install", "libexec", "test do"):
            self.assertNotIn(formula_only, text)
        self.assertLess(len(re.search(r'desc "([^"]+)"', text)[1]), 80, "Homebrew caps desc at 80 characters")

    def test_the_formula_caveats_lead_with_login_then_the_agents_line_then_the_simulator(self):
        caveats = self.render.render("0.2.0", SHA)
        caveats = caveats[caveats.index("def caveats"):]
        order = [caveats.index(command) for command in ("mobster login", "mobster mcp install --all",
                                                        "mobster doctor", "mobster sim doctor")]
        self.assertEqual(order, sorted(order))

    def test_the_binary_path_matches_the_folder_build_sh_packs(self):
        script = (PACKAGING / "cli" / "build.sh").read_text()
        folder = re.search(r'^NAME="([^"]+)"$', script, re.M)[1]
        self.assertIn(f'binary "{folder}/mobster"', self.render.render("0.2.0", SHA, kind="cask"))
        self.assertIn('"$WORK/dist/mobster" "$STAGE"', script, "the bootloader is named mobster inside the folder")

    def test_the_cli_cask_is_deprecated_and_points_to_the_app(self):
        text = self.render.render("0.3.0", SHA, kind="cask")
        self.assertIn('deprecate! date:             "2026-10-13"', text)
        self.assertIn('because:          "is now part of Mobster for Mac"', text)
        self.assertIn('replacement_cask: "radishsoftware/tap/mobster-app"', text)
        caveats = text[text.index("caveats"):]
        self.assertIn("https://mobster.dev/download", caveats)
        self.assertIn("brew install --cask radishsoftware/tap/mobster-app", caveats)
        self.assertIn("Your existing mobster install keeps working.", caveats)
        self.assertNotIn("mobster login", caveats)

    def test_malformed_values_are_refused(self):
        for version, sha, url in [("v0.2.0", SHA, None), ("0.2", SHA, None), ("0.2.0", "abc", None),
                                  ("0.2.0", "g" * 64, None), ("0.2.0", SHA, "http://example.com/a.tar.gz"),
                                  ("0.2.0", SHA, 'https://example.com/a"; system "x'),
                                  ("0.2.0", SHA, "https://example.com/a b.tar.gz"),
                                  ("0.2.0", SHA, "file://relative/a.tar.gz")]:
            with self.subTest(version=version, sha=sha, url=url), self.assertRaises(self.render.RenderError):
                self.render.render(version, sha, url, kind="cask")
        with self.assertRaisesRegex(self.render.RenderError, "kind"):
            self.render.render("0.2.0", SHA, kind="bottle")

    def test_the_command_renders_the_cask_from_the_sha256_file(self):
        with tempfile.TemporaryDirectory() as folder:
            sha_file = Path(folder) / "mobster-macos-arm64.tar.gz.sha256"
            sha_file.write_text(f"{SHA.upper()}  mobster-macos-arm64.tar.gz\n")
            out = Path(folder) / "mobster.rb"
            self.assertEqual(self.render.main(["--cask", "--version", "0.2.0", "--sha256", str(sha_file),
                                               "--out", str(out)]), 0)
            text = out.read_text()
            self.assertIn(f'sha256 "{SHA}"', text)
            self.assertIn('binary "mobster-macos-arm64/mobster"', text)
            empty = Path(folder) / "empty.sha256"
            empty.write_text("")
            bad_out = Path(folder) / "bad.rb"
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(self.render.main(["--cask", "--version", "0.2.0", "--sha256", str(empty),
                                                   "--out", str(bad_out)]), 1)
            self.assertIn("64 hex", err.getvalue())
            self.assertFalse(bad_out.exists(), "a refused render writes nothing")


@unittest.skipUnless((PACKAGING / "homebrew" / "mobster-app-cask.rb.in").is_file(),
                     "packaging is not part of this copy")
class AppCaskTests(unittest.TestCase):

    def setUp(self):
        self.render = load_render()

    def test_rendering_fills_every_placeholder_and_names_the_dmg_through_version(self):
        template = (PACKAGING / "homebrew" / "mobster-app-cask.rb.in").read_text()
        self.assertEqual(sorted(set(re.findall(r"@[A-Z0-9_]+@", template))), ["@SHA256@", "@URL@", "@VERSION@"])
        text = self.render.render("0.4.0", SHA, kind="app")
        self.assertTrue(text.startswith('cask "mobster-app" do\n'), text[:40])
        self.assertIn('version "0.4.0"', text)
        self.assertIn(f'sha256 "{SHA}"', text)
        self.assertIn('url "https://github.com/RadishSoftware/mobster-releases/releases/download/v#{version}/'
                      'Mobster_#{version}_Apple-Silicon.dmg"', text)
        for line in ('app "Mobster.app"', "auto_updates true", "depends_on arch: :arm64", "depends_on macos: :ventura",
                     "strategy :github_latest", '"~/Library/Application Support/app.mobster.desktop"'):
            self.assertIn(line, text)
        for cli_only in ("binary ", "deprecate!", "mobster-macos-arm64"):
            self.assertNotIn(cli_only, text)

    def test_the_app_id_matches_the_bundle(self):
        conf = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
        if not conf.is_file():
            self.skipTest("the desktop app isn't part of this copy")
        import json
        identifier = json.loads(conf.read_text())["identifier"]
        self.assertIn(f"Application Support/{identifier}", self.render.render("0.4.0", SHA, kind="app"))

    def test_the_command_renders_the_app_cask_from_a_sums_file(self):
        with tempfile.TemporaryDirectory() as folder:
            sums = Path(folder) / "dmg.sha256"
            sums.write_text(f"{SHA}  Mobster_0.4.0_Apple-Silicon.dmg\n")
            out = Path(folder) / "mobster-app.rb"
            self.assertEqual(self.render.main(["--app", "--version", "0.4.0", "--sha256", str(sums), "--out",
                                               str(out)]), 0)
            self.assertIn(f'sha256 "{SHA}"', out.read_text())
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.render.main(["--app", "--cask", "--version", "0.4.0", "--sha256", SHA])


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
        from mobile_agent.packs.register import MODULES
        hidden = self.spec()["Analysis"][1]["hiddenimports"]
        desktop = ROOT / "desktop" / "scripts" / "mobster-agent.spec"
        for name in MODULES:
            self.assertIn(f"mobile_agent.packs.{name}", hidden)
            if desktop.is_file():
                self.assertIn(f'"mobile_agent.packs.{name}"', desktop.read_text())

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


SIDECAR = ROOT / "desktop" / "scripts"


@unittest.skipUnless((SIDECAR / "build-sidecar.sh").is_file(), "the desktop app is not part of this copy")
class SidecarBuildTests(unittest.TestCase):

    def test_the_spec_names_yaml_for_the_freezer(self):
        self.assertIn("yaml", hidden_imports(SIDECAR / "mobster-agent.spec"))

    def test_the_mac_app_freezes_cryptography_and_the_public_cli_does_not(self):
        sidecar = analysis(SIDECAR / "mobster-agent.spec")
        for module in ("mobile_agent.teams.webauthn", "mobile_agent.teams.docs"):
            self.assertIn(module, sidecar["hiddenimports"])
        for module in ("cryptography", "_cffi_backend"):
            self.assertNotIn(module, sidecar["excludes"])
        cli = analysis(PACKAGING / "cli" / "mobster.spec")
        self.assertIn("cryptography", cli["excludes"])
        self.assertFalse([m for m in cli["hiddenimports"] if m.startswith("mobile_agent.teams")])

    def test_both_builds_exclude_pymobiledevice3_and_check_the_venv_and_the_frozen_tree(self):
        for spec in (SIDECAR / "mobster-agent.spec", PACKAGING / "cli" / "mobster.spec"):
            self.assertIn("pymobiledevice3", analysis(spec)["excludes"], spec.name)
        check = '"$PYTHON" "$ROOT/scripts/check_no_pymobiledevice3.py"'
        for script, freeze, frozen, delete, stop in (
                (SIDECAR / "build-sidecar.sh", '"$VENV/bin/pyinstaller"', '"$APP"', 'rm -rf "$WORK"', "exit 1"),
                (PACKAGING / "cli" / "build.sh", '"$PYTHON" -m PyInstaller', '"$STAGE"', 'rm -rf "$STAGE" "$WORK"',
                 "fail ")):
            text = script.read_text()
            self.assertLess(text.index(check), text.index(freeze), f"{script.name} checks the venv before it freezes")
            self.assertGreater(text.index(f"{check} {frozen}"), text.index(freeze),
                               f"{script.name} reads what it froze")
            after = text[text.index(f"{check} {frozen}"):]
            self.assertLess(after.index(delete), after.index(stop), f"{script.name} deletes what it froze, then stops")
            self.assertIn("couldn't read the frozen", after, f"{script.name} says when the check can't answer")
        self.assertIn("MOBSTER_BUILD_VENV", (SIDECAR / "build-sidecar.sh").read_text())


def analysis(spec):
    seen = {}
    names = {name: (lambda *a, **kw: types.SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[]))
             for name in ("PYZ", "EXE", "COLLECT", "BUNDLE")}
    names["Analysis"] = lambda *a, **kw: seen.update(kw) or types.SimpleNamespace(
        pure=[], scripts=[], binaries=[], datas=[])
    names["SPECPATH"] = str(spec.parent)
    exec(compile(spec.read_text(), spec.name, "exec"), names)
    return seen


def hidden_imports(spec):
    return analysis(spec)["hiddenimports"]


@unittest.skipUnless((SIDECAR / "mobster-agent.spec").is_file(), "the desktop app is not part of this copy")
class ImportByNameTests(unittest.TestCase):

    def by_name(self):
        from mobile_agent import harness_api
        from mobile_agent.packs import register
        return [harness_api.HOOKS_MODULE] + [f"{register.__package__}.{name}" for name in register.MODULES]

    def test_both_specs_name_every_module_the_agent_imports_by_name(self):
        for module in self.by_name():
            self.assertIsNotNone(importlib.util.find_spec(module), module)
            for spec in (SIDECAR / "mobster-agent.spec", PACKAGING / "cli" / "mobster.spec"):
                self.assertIn(module, hidden_imports(spec), f"{spec.name} misses {module}")

    def test_no_other_code_imports_by_name(self):
        skip = {"tests", "bench", "evals"}
        sites = sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "mobile_agent").rglob("*.py")
                       if not skip & set(path.relative_to(ROOT).parts) and ".venv" not in path.parts
                       and "import_module(" in path.read_text())
        self.assertEqual(sites, ["mobile_agent/harness_api.py", "mobile_agent/packs/register.py"],
                         "a new importlib.import_module call: name its modules in desktop/scripts/mobster-agent.spec "
                         "and packaging/cli/mobster.spec, then list it in by_name")

    def build(self, has_yaml, has_crypto=True, pmd3_exit=0, build_venv=None):
        root = Path(tempfile.mkdtemp(prefix="mobster-sidecar-test-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "desktop" / "scripts").mkdir(parents=True)
        shutil.copy2(SIDECAR / "build-sidecar.sh", root / "desktop" / "scripts")
        (root / "desktop" / "src-tauri").mkdir()
        (root / "desktop" / "src-tauri" / "tauri.conf.json").write_text(
            '{"bundle": {"macOS": {"minimumSystemVersion": "13.0"}}}')
        bin_dir = root / (build_venv or "mobile_agent/.venv") / "bin"
        bin_dir.mkdir(parents=True)
        tools = root / "tools"
        tools.mkdir()
        log = root / "calls.log"
        fakes = {bin_dir / "pip": f'echo "pip $*" >> "{log}"',
                 bin_dir / "python": f'case "$1" in *check_no_pymobiledevice3.py) exit {pmd3_exit};; esac; '
                 f'[ "$2" = "import yaml" ] && exit {0 if has_yaml else 1}; '
                 'case "$2" in "import cryptography"*) '
                 f'exit {0 if has_crypto else 1};; esac; exit 0',
                 bin_dir / "pyinstaller": f'echo "pyinstaller" >> "{log}"; exit 42',
                 tools / "rustc": 'echo "host: aarch64-apple-darwin"',
                 tools / "python3": 'echo 13.0'}
        for path, body in fakes.items():
            path.write_text(f"#!/bin/sh\n{body}\n")
            path.chmod(0o755)
        done = subprocess.run(["/bin/bash", str(root / "desktop" / "scripts" / "build-sidecar.sh")],
                              capture_output=True, text=True, timeout=30,
                              env={"PATH": f"{tools}:/usr/bin:/bin", "HOME": str(root),
                                   **({"MOBSTER_BUILD_VENV": str(root / build_venv)} if build_venv else {})})
        calls = log.read_text().splitlines() if log.exists() else []
        return done.returncode, done.stderr, [c for c in calls if c.startswith("pip")], "pyinstaller" in calls

    def test_it_installs_the_agents_pins_before_freezing(self):
        status, _, pip, froze = self.build(has_yaml=True)
        self.assertEqual(status, 42)
        self.assertTrue(froze)
        self.assertEqual(len(pip), 1)
        self.assertRegex(pip[0], r"-r \S*/mobile_agent/requirements\.txt "
                                 r"-r \S*/desktop/scripts/sidecar-requirements\.txt")

    def test_the_app_holds_the_capture_entitlements_its_helpers_need(self):
        import plistlib
        app = plistlib.loads((ROOT / "desktop" / "src-tauri" / "entitlements.plist").read_bytes())
        helper = plistlib.loads((ROOT / "mobile_agent" / "swift" / "helper_phone_audio.entitlements").read_bytes())
        voice = plistlib.loads((ROOT / "desktop" / "native" / "mobster-voice.entitlements").read_bytes())
        for key in ("com.apple.security.device.camera", "com.apple.security.device.audio-input"):
            self.assertIs(app.get(key), True, key)
            self.assertIs(helper.get(key), True, key)
        self.assertIs(voice.get("com.apple.security.device.audio-input"), True)
        info = plistlib.loads((ROOT / "desktop" / "src-tauri" / "Info.plist").read_bytes())
        self.assertIn("NSCameraUsageDescription", info)
        self.assertIn("NSMicrophoneUsageDescription", info)

    def test_a_venv_without_cryptography_stops_before_freezing(self):
        status, stderr, _, froze = self.build(has_yaml=True, has_crypto=False)
        self.assertEqual(status, 1)
        self.assertFalse(froze)
        self.assertIn("cryptography isn't installed in", stderr)
        self.assertIn("(cryptography==50.0.2)", stderr)

    def test_a_venv_with_pymobiledevice3_stops_before_freezing_and_points_to_a_new_venv(self):
        status, stderr, _, froze = self.build(has_yaml=True, pmd3_exit=1)
        self.assertEqual(status, 1)
        self.assertFalse(froze)
        self.assertIn("pymobiledevice3 is installed in", stderr)
        self.assertIn("uv venv --seed --python 3.12.13 <new folder>, then MOBSTER_BUILD_VENV=<new folder>", stderr)
        self.assertNotIn("uninstall", stderr)

    def test_a_check_that_cant_answer_stops_before_freezing_and_says_so(self):
        status, stderr, _, froze = self.build(has_yaml=True, pmd3_exit=2)
        self.assertEqual(status, 1)
        self.assertFalse(froze)
        self.assertIn("couldn't check whether", stderr)
        self.assertNotIn("is installed in", stderr)

    def test_mobster_build_venv_names_the_build_venv(self):
        status, _, pip, froze = self.build(has_yaml=True, build_venv="clean-venv")
        self.assertEqual(status, 42)
        self.assertTrue(froze)
        self.assertEqual(len(pip), 1)

    def test_the_build_checks_the_frozen_tree_for_cryptography(self):
        script = (SIDECAR / "build-sidecar.sh").read_text()
        self.assertIn("cryptography/hazmat/bindings/_rust.abi3.so", script)
        self.assertIn("_cffi_backend.cpython-*-darwin.so", script)

    def test_a_venv_without_pyyaml_stops_before_freezing(self):
        status, stderr, _, froze = self.build(has_yaml=False)
        self.assertEqual(status, 1)
        self.assertFalse(froze)
        self.assertIn("PyYAML isn't installed in", stderr)
        self.assertIn("mobile_agent/requirements.txt must list it (PyYAML==6.0.3)", stderr)


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


BUILD_IPHONE_TOOLS = ROOT / "desktop" / "scripts" / "build-iphone-tools.sh"
DESKTOP_NOTICES = ROOT / "desktop" / "THIRD_PARTY_NOTICES.md"


@unittest.skipUnless(BUILD_IPHONE_TOOLS.is_file() and DESKTOP_NOTICES.is_file(), "the Mac app is not part of this copy")
class IphoneToolNoticeTests(unittest.TestCase):
    def test_every_bundled_tool_has_a_licence_notice(self):
        tools = re.search(r"^TOOLS=\((.*?)\)", BUILD_IPHONE_TOOLS.read_text(), re.M).group(1).split()
        notices = DESKTOP_NOTICES.read_text()
        for tool in tools:
            self.assertIn(f"`{tool}`", notices, f"{tool} ships in the app but desktop/THIRD_PARTY_NOTICES.md omits it")
        for own in sorted((ROOT / "desktop" / "iphone-tools").glob("*.c")):
            self.assertIn(f"`{own.stem}`", notices, f"{own.name} ships in the app but the notices omit it")


APP_NOTICES = ROOT / "desktop" / "src-tauri" / "resources" / "THIRD_PARTY_NOTICES.txt"


@unittest.skipUnless(APP_NOTICES.parent.is_dir(), "the Mac app is not part of this copy")
class AppLicenceFileTests(unittest.TestCase):
    def test_bundle_ships_the_generated_notices_and_licence(self):
        resources = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text())["bundle"]["resources"]
        self.assertEqual(resources["resources/THIRD_PARTY_NOTICES.txt"], "THIRD_PARTY_NOTICES.txt")
        self.assertEqual(resources["../../LICENSE"], "LICENSE")

    def test_notices_name_each_ecosystem(self):
        text = APP_NOTICES.read_text()
        for needle in ("CPython 3.12", "PyInstaller", "iphone-tools", "  react ", "  tauri ", "  PyYAML "):
            self.assertIn(needle, text)


INSTALL = ROOT / "packaging" / "install.sh"
SITE_COPY = ROOT / "site" / "public" / "install.sh"
MESSAGE = ("The Mobster CLI is now part of Mobster for Mac.\n"
           "Download it at https://mobster.dev/download\n"
           "Your existing mobster install keeps working.\n")
WATCHED = ("curl", "wget", "tar", "shasum", "codesign", "uname", "sysctl", "ln", "mkdir", "mv", "cp", "rm", "sudo",
           "brew", "xattr", "chmod")


@unittest.skipUnless(INSTALL.is_file(), "packaging is not part of this copy")
class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mobster-install-test-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.home = self.folder / "home"
        self.home.mkdir()
        self.bin = self.folder / "bin"
        self.bin.mkdir()
        self.log = self.folder / "ran.log"
        for name in WATCHED:
            fake = self.bin / name
            fake.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\nexit 1\n')
            fake.chmod(0o755)

    def run_script(self, how="file", script=INSTALL, **env):
        environ = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin", "SHELL": "/bin/zsh", **env}
        if how == "pipe":
            return subprocess.run(["/bin/sh"], input=script.read_text(), capture_output=True, text=True,
                                  env=environ, cwd=self.folder, timeout=30)
        return subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, env=environ,
                              cwd=self.folder, timeout=30)

    def files_in(self, folder):
        return sorted(str(path.relative_to(folder)) for path in folder.rglob("*"))

    def ran(self):
        return self.log.read_text() if self.log.exists() else ""

    def test_piped_to_sh_it_prints_the_message_and_exits_0(self):
        done = self.run_script("pipe")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, MESSAGE)
        self.assertEqual(done.stderr, "")

    def test_run_as_a_file_it_prints_the_same(self):
        done = self.run_script("file")
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, MESSAGE, ""))

    def test_it_installs_nothing_downloads_nothing_and_writes_nothing(self):
        done = self.run_script("pipe", MOBSTER_VERSION="0.3.0", MOBSTER_HOME=str(self.home / ".mobster"),
                               MOBSTER_BIN_DIR=str(self.home / ".local/bin"),
                               MOBSTER_INSTALL_BASE_URL="file:///nowhere")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.files_in(self.home), [])
        self.assertEqual(self.ran(), "")
        self.assertEqual(self.files_in(self.folder), sorted(["bin", "home", *[f"bin/{n}" for n in WATCHED]]))

    def test_an_earlier_install_and_the_startup_files_are_left_alone(self):
        version = self.home / ".mobster" / "versions" / "0.3.0" / "mobster-macos-arm64"
        version.mkdir(parents=True)
        (version / "mobster").write_text('#!/bin/sh\necho "mobster 0.3.0"\n')
        (version / "mobster").chmod(0o755)
        (self.home / ".local" / "bin").mkdir(parents=True)
        (self.home / ".local" / "bin" / "mobster").symlink_to(version / "mobster")
        zshrc = self.home / ".zshrc"
        zshrc.write_text("# Added by the Mobster installer: https://mobster.dev/install.sh\n"
                         'export PATH="$HOME/.local/bin:$PATH"\n')
        before = {path: (path.read_bytes() if path.is_file() and not path.is_symlink() else os.readlink(path)
                         if path.is_symlink() else None) for path in self.home.rglob("*")}
        done = self.run_script("pipe")
        self.assertEqual((done.returncode, done.stdout), (0, MESSAGE))
        after = {path: (path.read_bytes() if path.is_file() and not path.is_symlink() else os.readlink(path)
                        if path.is_symlink() else None) for path in self.home.rglob("*")}
        self.assertEqual(after, before)
        self.assertEqual(self.ran(), "")
        linked = subprocess.run([str(self.home / ".local" / "bin" / "mobster")], capture_output=True, text=True)
        self.assertEqual(linked.stdout, "mobster 0.3.0\n")

    def test_it_runs_under_a_plain_posix_sh_with_no_tools_on_path(self):
        done = subprocess.run(["/bin/sh"], input=INSTALL.read_text(), capture_output=True, text=True,
                              env={"HOME": str(self.home), "PATH": str(self.bin)}, timeout=30)
        self.assertEqual((done.returncode, done.stdout), (0, MESSAGE), done.stderr)
        self.assertEqual(self.ran(), "")

    def test_the_message_names_the_download_page_and_no_install_command(self):
        text = INSTALL.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("https://mobster.dev/download", MESSAGE)
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for needle in ("curl", "github.com", "releases", "brew", "PATH=", ".zshrc", "ln ", "mkdir", "tar "):
            self.assertNotIn(needle, code)

    @unittest.skipUnless(SITE_COPY.is_file(), "the site is not part of this copy")
    def test_the_copy_mobster_dev_serves_is_this_script(self):
        self.assertEqual(SITE_COPY.read_text(), INSTALL.read_text())
        done = self.run_script("pipe", script=SITE_COPY)
        self.assertEqual((done.returncode, done.stdout), (0, MESSAGE))


TESTS = Path(__file__).resolve().parent


class PrivacyFixTests(unittest.TestCase):
    def test_the_developer_mode_fixture_names_a_generic_architecture(self):
        text = (TESTS / "setup_errors" / "developer_mode.txt").read_text()
        self.assertIn("arch:arm64,", text)
        self.assertNotRegex(text, r"arch:arm64[a-z]")


if __name__ == "__main__":
    unittest.main()

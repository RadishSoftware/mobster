"""Signing the helper without Xcode, the Mac side: the key and certificate kept in Mobster's own
keychain, codesign of a prebuilt runner with the team's profile and its checks, pymobiledevice3's and go-ios's command
lines (Xcode's tools never among them), the driver the device manager picks, Path C's export and import, the whole run
on recorded answers, and the setup state. Fake programs and recorded answers: no Keychain, network, Xcode or phone."""

import base64
import datetime
import hashlib
import io
import json
import os
import plistlib
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent.devkit import DevError, asc
from mobile_agent.team_signing import FLAG, apple_ca, bundle, enabled, identity, prebuilt, resign, tooling, writes
from mobile_agent.team_signing import service as service_module
from mobile_agent.tests.test_devkit_asc import RFC_D, p8_for
from mobile_agent.tests.test_team_signing_writes import (
    CSR,
    ISSUER,
    OTHER,
    PHONE,
    TEAM_KEY,
    Apple,
    client,
    created,
    device,
    full_posts,
    routes,
)
from mobile_agent.wda import patches as wda_patches

TEAM = "ABCDE12345"
RUNNER_ID = "app.mobster.wda.runner.0123456789.xctrunner"
DER = b"\x30\x82" + bytes(range(1, 120))  # a certificate-shaped blob: the fakes never parse it
SHA1 = hashlib.sha1(DER).hexdigest().upper()


class Folder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="team-signing-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))


def make_runner(folder, bundle_id="app.mobster.wda.runner.xctrunner"):
    app = Path(folder) / "WebDriverAgentRunner-Runner.app"
    (app / "Frameworks" / "XCTest.framework").mkdir(parents=True, exist_ok=True)
    (app / "Frameworks" / "libXCTestSwiftSupport.dylib").write_bytes(b"dylib")
    (app / "PlugIns" / "WebDriverAgentRunner.xctest" / "Frameworks" / "WebDriverAgentLib.framework").mkdir(
        parents=True, exist_ok=True
    )
    (app / "PlugIns" / "WebDriverAgentRunner.xctest" / "WebDriverAgentRunner").write_bytes(b"test")
    (app / "PlugIns" / "WebDriverAgentRunner.xctest.dSYM").mkdir(exist_ok=True)
    (app / "WebDriverAgentRunner-Runner").write_bytes(b"#!runner")
    os.chmod(app / "WebDriverAgentRunner-Runner", 0o755)
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id}))
    (app / "embedded.mobileprovision").write_bytes(b"old profile")
    return app


def profile_dict(*, team=TEAM, udids=(PHONE["udid"],), certs=(DER,), days=364, application=None, uuid="UUID-1"):
    return {
        "TeamIdentifier": [team],
        "Name": "Mobster helper",
        "Entitlements": {"application-identifier": application or f"{team}.*", "get-task-allow": True},
        "ProvisionedDevices": list(udids),
        "DeveloperCertificates": list(certs),
        "UUID": uuid,
        "ExpirationDate": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days),
    }


class FakeCodesign:
    """Records every command; answers the way codesign does for a bundle signed for ``team`` and ``bundle_id``."""

    def __init__(self, team=TEAM, bundle_id=RUNNER_ID, verify=0):
        self.team, self.bundle_id, self.verify, self.calls = team, bundle_id, verify, []

    def __call__(self, argv, timeout=120):
        self.calls.append(argv)
        if argv[0] not in resign.ALLOWED:
            raise AssertionError(argv)
        if argv[1:2] == ["--verify"]:
            return self.verify, "", "" if not self.verify else "a sealed resource is missing or invalid"
        if argv[1:2] == ["-dvv"]:
            return 0, "", f"Identifier={self.bundle_id}\nTeamIdentifier={self.team}\n"
        if argv[1:3] == ["-d", "--entitlements"]:
            return 0, plistlib.dumps({"application-identifier": f"{self.team}.{self.bundle_id}"}).decode(), ""
        return 0, "", ""


class ProfileTests(Folder):
    def test_a_wildcard_or_exact_profile_covers_the_bundle(self):
        self.assertTrue(resign.covers("ABCDE12345.*", "ABCDE12345", RUNNER_ID))
        self.assertTrue(resign.covers(f"ABCDE12345.{RUNNER_ID}", "ABCDE12345", RUNNER_ID))
        self.assertTrue(resign.covers("ABCDE12345.app.mobster.*", "ABCDE12345", RUNNER_ID))
        for application in (
            "ZYXWV98765.*",
            "ABCDE12345.com.example.*",
            "ABCDE12345.app.mobster.wda.runner.other",
            None,
        ):
            self.assertFalse(resign.covers(application, "ABCDE12345", RUNNER_ID), application)

    def test_entitlements_are_the_four_the_runner_has_and_limited_to_the_profile(self):
        facts = resign.profile_facts(profile_dict())
        self.assertEqual(
            resign.entitlements_for(facts, RUNNER_ID),
            {
                "application-identifier": f"{TEAM}.{RUNNER_ID}",
                "com.apple.developer.team-identifier": TEAM,
                "keychain-access-groups": [f"{TEAM}.{RUNNER_ID}"],
                "get-task-allow": True,
            },
        )
        plain = profile_dict()
        plain["Entitlements"].pop("get-task-allow")
        self.assertNotIn("get-task-allow", resign.entitlements_for(resign.profile_facts(plain), RUNNER_ID))


class ResignTests(Folder):
    def sign(self, profile=None, codesign=None, bundle_id=RUNNER_ID, source=None):
        source = source or make_runner(self.tmp / "src")
        codesign = codesign or FakeCodesign()
        facts = profile or profile_dict()
        profile_file = self.tmp / "p.mobileprovision"
        profile_file.write_bytes(b"signed profile")
        out = resign.sign(
            source,
            profile_file,
            SHA1,
            self.tmp / "kc.keychain-db",
            self.tmp / "out",
            run=codesign,
            decode=lambda path: facts,
            bundle_id=bundle_id,
        )
        return out, codesign, source

    def test_the_copy_is_signed_inside_out_with_the_profile_and_the_original_is_untouched(self):
        out, codesign, source = self.sign()
        signed = Path(out["path"])
        order = [Path(call[-1]).name for call in codesign.calls if call[1] == "-f"]
        self.assertEqual(
            order,
            [
                "WebDriverAgentLib.framework",
                "WebDriverAgentRunner.xctest",
                "XCTest.framework",
                "libXCTestSwiftSupport.dylib",
                "Runner.app",
            ],
        )
        self.assertEqual((signed / "embedded.mobileprovision").read_bytes(), b"signed profile")
        self.assertFalse((signed / "PlugIns" / "WebDriverAgentRunner.xctest.dSYM").exists())
        self.assertEqual((source / "embedded.mobileprovision").read_bytes(), b"old profile")
        self.assertEqual(plistlib.loads((signed / "Info.plist").read_bytes())["CFBundleIdentifier"], RUNNER_ID)
        self.assertEqual(
            plistlib.loads((source / "Info.plist").read_bytes())["CFBundleIdentifier"],
            "app.mobster.wda.runner.xctrunner",
        )
        self.assertTrue(all(call[0] in resign.ALLOWED for call in codesign.calls))
        self.assertTrue(
            all(
                "--keychain" in call and call[call.index("--keychain") + 1].endswith("kc.keychain-db")
                for call in codesign.calls
                if call[1] == "-f"
            )
        )
        self.assertEqual(
            (out["team"], out["bundle_id"], out["udids"], out["certificate"]), (TEAM, RUNNER_ID, [PHONE["udid"]], SHA1)
        )

    def test_xcode_cannot_start(self):
        env = resign.ENV
        self.assertEqual(env["DEVELOPER_DIR"], "/var/empty")
        self.assertEqual(resign.ALLOWED, {"/usr/bin/codesign", "/usr/bin/security"})
        with self.assertRaises(DevError):
            resign._run(["/usr/bin/xcodebuild", "-version"])
        with self.assertRaises(DevError):
            resign._run(["/usr/bin/xcrun", "simctl"])

    def test_a_profile_that_is_not_this_runners_refuses_before_anything_is_signed(self):
        for bad, why in (
            (profile_dict(application="ZYXWV98765.*"), "isn't for"),
            (profile_dict(certs=(b"\x30other",)), "doesn't list the signing certificate"),
            (profile_dict(days=3), "under a week"),
            (profile_dict(udids=()), "lists no iPhone"),
        ):
            codesign = FakeCodesign()
            with self.assertRaises(DevError) as caught:
                self.sign(profile=bad, codesign=codesign)
            self.assertIn(why, caught.exception.message)
            self.assertEqual(codesign.calls, [])
            self.assertFalse((self.tmp / "out" / "Runner.app").exists())

    def test_a_signature_that_does_not_verify_leaves_no_runner(self):
        with self.assertRaises(DevError) as caught:
            self.sign(codesign=FakeCodesign(verify=1))
        self.assertIn("doesn't verify", caught.exception.message)
        self.assertFalse((self.tmp / "out" / "Runner.app").exists())

    def test_a_signature_for_another_team_or_bundle_is_refused(self):
        for codesign in (FakeCodesign(team="ZYXWV98765"), FakeCodesign(bundle_id="com.example.other")):
            with self.assertRaises(DevError):
                self.sign(codesign=codesign)

    def test_a_failing_codesign_is_said_with_its_last_line(self):
        def run(argv, timeout=120):
            return (1, "", "first\nerror: no identity found\n") if argv[1] == "-f" else (0, "", "")

        profile_file = self.tmp / "p.mobileprovision"
        profile_file.write_bytes(b"x")
        with self.assertRaises(DevError) as caught:
            resign.sign(
                make_runner(self.tmp / "s"),
                profile_file,
                SHA1,
                "kc",
                self.tmp / "o",
                run=run,
                decode=lambda path: profile_dict(),
                bundle_id=RUNNER_ID,
            )
        self.assertIn("no identity found", caught.exception.message)

    def test_the_tree_digest_sees_a_changed_byte_and_a_changed_mode(self):
        app = make_runner(self.tmp / "d")
        before = resign.tree_digest(app)
        self.assertEqual(before, resign.tree_digest(app))
        (app / "PlugIns" / "WebDriverAgentRunner.xctest" / "WebDriverAgentRunner").write_bytes(b"tesT")
        self.assertNotEqual(before, resign.tree_digest(app))
        again = resign.tree_digest(app)
        os.chmod(app / "Frameworks" / "libXCTestSwiftSupport.dylib", 0o755)
        self.assertNotEqual(again, resign.tree_digest(app))


class ToolingTests(Folder):
    UDID = "00008130-000A1B2C3D4E5F60"

    def test_the_driver_is_xcodes_unless_without_xcode_is_on(self):
        self.assertEqual(tooling.choose_driver(flag=False, pmd3_path="/p", go_ios_path="/g"), "xcodebuild")
        self.assertEqual(tooling.choose_driver(flag=True, pmd3_path="/p", go_ios_path="/g"), "pymobiledevice3")
        self.assertEqual(tooling.choose_driver(flag=True, pmd3_path=None, go_ios_path="/g"), "go-ios")
        with self.assertRaises(DevError) as caught:
            tooling.choose_driver(flag=True)
        self.assertIn("pipx install pymobiledevice3", caught.exception.fix)

    def test_commands_follow_the_tools_own_help_and_never_start_xcode(self):
        p = "/opt/homebrew/bin/pymobiledevice3"
        env = {"USE_IP": "127.0.0.1", "MOBSTER_CAPTURE_GATE": "off"}
        commands = [
            tooling.developer_mode_command(p, self.UDID),
            tooling.reveal_developer_mode_command(p, self.UDID),
            tooling.mounted_command(p, self.UDID),
            tooling.mount_command(p, self.UDID),
            tooling.install_command(p, self.UDID, "/x/Runner.app"),
            tooling.launch_command(p, self.UDID, RUNNER_ID, env),
            tooling.go_ios_install_command("/g/ios", self.UDID, "/x/r.ipa"),
            tooling.go_ios_launch_command("/g/ios", self.UDID, RUNNER_ID, env),
        ]
        for command in commands:
            self.assertNotIn(Path(command[0]).name, tooling.NEVER)
            self.assertFalse({"xcodebuild", "xcrun", "devicectl"} & set(command))
        self.assertEqual(commands[4], [p, "apps", "install", "/x/Runner.app", "--native", "--udid", self.UDID])
        self.assertEqual(
            commands[5],
            [
                p,
                "developer",
                "dvt",
                "xcuitest",
                RUNNER_ID,
                "--native",
                "--udid",
                self.UDID,
                "--env",
                "MOBSTER_CAPTURE_GATE=off",
                "--env",
                "USE_IP=127.0.0.1",
            ],
        )
        self.assertIn("--bundleid=app.mobster.wda.runner.0123456789", commands[7])
        self.assertIn(f"--testrunnerbundleid={RUNNER_ID}", commands[7])
        with self.assertRaises(DevError):
            tooling.install_command(p, "not a udid; rm -rf /", "/x")
        with self.assertRaises(DevError):
            tooling._check(["/usr/bin/xcodebuild", "build"])

    def test_the_runner_starts_on_the_loopback_with_the_phones_capture_gate(self):
        self.assertEqual(tooling.runner_env("phone", {}), {"USE_IP": "127.0.0.1", "MOBSTER_CAPTURE_GATE": "off"})
        self.assertEqual(tooling.runner_env("phone", {"MOBSTER_CAPTURE_GATE": "on"})["MOBSTER_CAPTURE_GATE"], "on")

    def test_developer_mode_is_read_not_set(self):
        for output, expected in (
            ("true\n", True),
            ("False\n", False),
            ("Developer Mode: enabled", True),
            ("", None),
            ("who knows", None),
        ):
            self.assertIs(tooling.developer_mode("/p", self.UDID, run=lambda argv, timeout, o=output: (0, o)), expected)
        self.assertIsNone(tooling.developer_mode("/p", self.UDID, run=lambda argv, timeout: (1, "not trusted")))

    def test_the_image_is_mounted_only_when_the_phone_has_none(self):
        calls = []

        def empty(argv, timeout):
            calls.append(argv[1:3])
            return 0, "[]" if argv[2] == "list" else "mounted"

        def mounted(argv, timeout):
            calls.append(argv[1:3])
            return 0, '[{"ImageType": "Personalized"}]'

        self.assertTrue(tooling.ensure_mounted("/p", self.UDID, run=empty))
        self.assertEqual(calls, [["mounter", "list"], ["mounter", "auto-mount"]])
        calls.clear()
        self.assertFalse(tooling.ensure_mounted("/p", self.UDID, run=mounted))
        self.assertEqual(calls, [["mounter", "list"]])

    def test_an_install_that_fails_says_the_programs_last_line(self):
        with self.assertRaises(DevError) as caught:
            tooling.run_short(
                tooling.install_command("/p", self.UDID, "/x"),
                run=lambda argv, timeout: (1, "a\nInvalidCodeSignature\n"),
            )
        self.assertIn("InvalidCodeSignature", caught.exception.message)

    def test_go_ios_gets_an_ipa_with_the_payload_folder(self):
        app = make_runner(self.tmp / "g")
        ipa = tooling.ipa_of(app, self.tmp)
        import zipfile

        with zipfile.ZipFile(ipa) as archive:
            names = archive.namelist()
        self.assertIn("Payload/WebDriverAgentRunner-Runner.app/Info.plist", names)
        self.assertTrue(all(name.startswith("Payload/WebDriverAgentRunner-Runner.app") for name in names))


class PrebuiltTests(Folder):
    def test_a_runner_with_this_mobsters_patches_is_found_and_an_older_one_is_not_used(self):
        wanted = wda_patches.patch_digest()
        env_dir = self.tmp / "env"
        make_runner(env_dir)
        (env_dir / "patches.txt").write_text(wanted + "\n")
        path, where = prebuilt.locate(environ={prebuilt.ENV: str(env_dir)}, app_copy=self.tmp / "none")
        self.assertEqual((path.name, where), ("WebDriverAgentRunner-Runner.app", "MOBSTER_PREBUILT_RUNNER"))
        (env_dir / "patches.txt").write_text("old\n")
        with self.assertRaises(DevError) as caught:
            prebuilt.locate(environ={prebuilt.ENV: str(env_dir)}, app_copy=self.tmp / "none")
        self.assertIn("older patches", caught.exception.message)

    def test_an_xcode_build_on_this_mac_counts_with_the_digest_its_build_recorded(self):
        derived = self.tmp / "derived"
        make_runner(derived / "Build" / "Products" / "Debug-iphoneos")
        path, where = prebuilt.locate(derived, wda_patches.patch_digest(), environ={}, app_copy=self.tmp / "none")
        self.assertEqual(where, "this Mac's Xcode build")
        with self.assertRaises(DevError):
            prebuilt.locate(derived, None, environ={}, app_copy=self.tmp / "none")

    def test_none_at_all_says_where_to_get_one(self):
        with self.assertRaises(DevError) as caught:
            prebuilt.locate(None, None, environ={}, app_copy=self.tmp / "none")
        self.assertIn("MOBSTER_PREBUILT_RUNNER", caught.exception.fix)


class MemoryStore:
    def __init__(self):
        self.items = {}

    def get(self, account):
        return self.items.get(account)

    def set(self, account, value):
        self.items[account] = value

    def delete(self, account):
        self.items.pop(account, None)


class IdentityTests(Folder):
    def make(self):
        self.commands, self.store = [], MemoryStore()

        def run(argv, stdin=None, timeout=60):
            self.commands.append((argv, stdin))
            if argv[0] == identity.OPENSSL:
                Path(argv[argv.index("-keyout") + 1]).write_text(
                    "-----BEGIN PRIVATE KEY-----\nPLANTEDKEY\n-----END PRIVATE KEY-----\n"
                )
                Path(argv[argv.index("-out") + 1]).write_text(CSR)
            if argv[1:3] == ["list-keychains", "-d"] and len(argv) == 4:
                return 0, '    "/Users/test/Library/Keychains/login.keychain-db"\n', ""
            if argv[1] == "find-identity":
                return 0, f'  1) {SHA1} "Apple Development: Team (X)"\n', ""
            if stdin and stdin.startswith("create-keychain"):
                Path(self.tmp / "ts" / identity.KEYCHAIN_FILE).write_text("keychain")
            return 0, "", ""

        return identity.Identity(self.tmp / "ts", store=self.store, run=run)

    def test_the_request_is_made_here_the_key_goes_to_the_keychain_and_the_file_is_deleted(self):
        ident = self.make()
        self.assertEqual(ident.request(), CSR)
        self.assertEqual(ident.request(), CSR)  # the same request again, not a second key
        self.assertEqual(sum(1 for argv, _ in self.commands if argv[0] == identity.OPENSSL), 1)
        imports = [stdin for _, stdin in self.commands if stdin and stdin.startswith("import") and "key.pem" in stdin]
        self.assertEqual(len(imports), 1)
        self.assertIn("-x", imports[0])
        key_file = imports[0].split('"')[1]
        self.assertFalse(Path(key_file).exists())
        self.assertFalse(Path(key_file).parent.exists())

    def test_no_password_or_key_text_reaches_an_argument_list(self):
        ident = self.make()
        ident.request()
        password = self.store.items[identity.ACCOUNT]
        self.assertGreaterEqual(len(password), 32)
        for argv, _ in self.commands:
            self.assertNotIn(password, " ".join(argv))
            self.assertNotIn("PLANTEDKEY", " ".join(argv))
        self.assertTrue(any(password in (stdin or "") for _, stdin in self.commands))  # it goes on stdin only
        self.assertNotIn(password, ident.state_path.read_text())

    def test_the_signing_keychain_never_stays_on_the_search_list(self):
        ident = self.make()
        ident.request()
        restored = [argv for argv, _ in self.commands if argv[1:4] == ["list-keychains", "-d", "user"] and "-s" in argv]
        self.assertTrue(restored == [] or all(str(ident.keychain) not in " ".join(argv) for argv in restored))

    def test_apples_intermediate_is_added_once_and_matches_its_fingerprint(self):
        ident = self.make()
        ident.request()
        ident.add_certificate(base64.b64encode(DER).decode(), "SER")
        imports = [
            stdin for _, stdin in self.commands if stdin and stdin.startswith("import") and "AppleWWDRCAG3.cer" in stdin
        ]
        self.assertEqual(len(imports), 1)
        self.assertEqual(hashlib.sha256(apple_ca.wwdr_g3()).hexdigest(), apple_ca.SHA256)
        self.assertEqual(apple_ca.SHA256, "dcf21878c77f4198e4b4614f03d696d89c66c66008d4244e1b99161aac91601f")
        self.assertFalse((ident.folder / "AppleWWDRCAG3.cer").exists())

    def test_the_certificate_is_kept_beside_its_key_and_found_again(self):
        ident = self.make()
        ident.request()
        kept = ident.add_certificate(base64.b64encode(DER).decode(), "SER1")
        self.assertEqual(kept, {"sha1": SHA1, "serial": "SER1"})
        self.assertEqual((ident.folder / "certificate.cer").read_bytes(), DER)
        self.assertEqual(oct((ident.folder / "certificate.cer").stat().st_mode & 0o777), "0o600")
        self.assertEqual(ident.signing_identity(), {"sha1": SHA1, "serial": "SER1"})
        self.assertFalse((ident.folder / "pending.csr").exists())

    def test_a_certificate_that_is_not_one_or_has_no_key_is_refused(self):
        ident = self.make()
        with self.assertRaises(DevError):
            ident.add_certificate("!!!", "S")
        with self.assertRaises(DevError):
            ident.add_certificate(base64.b64encode(b"short").decode(), "S")
        other = identity.Identity(
            self.tmp / "other",
            store=MemoryStore(),
            run=lambda argv, stdin=None, timeout=60: (
                (0, "", "") if argv[1:2] != ["find-identity"] else (0, "no identities", "")
            ),
        )
        with self.assertRaises(DevError) as caught:
            other.add_certificate(base64.b64encode(DER).decode(), "S")
        self.assertIn("doesn't match the key", caught.exception.message)

    def test_a_refusing_keychain_is_said_in_words(self):
        ident = identity.Identity(
            self.tmp / "refused", store=MemoryStore(), run=lambda argv, stdin=None, timeout=60: (1, "", "denied")
        )
        with self.assertRaises(DevError) as caught:
            ident.request()
        self.assertIn("Unlock", caught.exception.fix)

    def test_forget_removes_the_keychain_and_its_password(self):
        ident = self.make()
        ident.request()
        ident.forget()
        self.assertNotIn(identity.ACCOUNT, self.store.items)
        self.assertFalse((ident.folder / "identity.json").exists())

    def test_quoting_keeps_a_path_with_spaces_one_word(self):
        self.assertEqual(identity._quote('/Users/test/Application Support/a"b'),
                         '"/Users/test/Application Support/a\\"b"')


class BundleTests(Folder):
    def exported(self, **changes):
        runner = make_runner(self.tmp / "signed")
        codesign = FakeCodesign()
        facts = profile_dict(**changes)
        (runner / "embedded.mobileprovision").write_bytes(b"profile")
        (runner / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": RUNNER_ID}))
        out = self.tmp / "helper.tgz"
        manifest = bundle.export(runner, out, run=codesign, decode=lambda path: facts)
        return out, manifest, codesign, facts

    def test_a_bundle_round_trips_and_arrives_intact(self):
        out, manifest, codesign, facts = self.exported()
        self.assertEqual(
            (manifest["team"], manifest["bundle_id"], manifest["udids"]), (TEAM, RUNNER_ID, [PHONE["udid"]])
        )
        self.assertEqual(bundle.read_manifest(out)["tree"], manifest["tree"])
        got = bundle.import_bundle(
            out, self.tmp / "dest", team=TEAM, udid=PHONE["udid"], run=FakeCodesign(), decode=lambda path: facts
        )
        self.assertEqual(resign.tree_digest(got["path"]), manifest["tree"])
        self.assertTrue(os.access(Path(got["path"]) / "WebDriverAgentRunner-Runner", os.X_OK))

    def test_the_team_must_be_named_and_match(self):
        out, _, _, facts = self.exported()
        for team in ("ZYXWV98765", "", None):
            with self.assertRaises(DevError) as caught:
                bundle.import_bundle(out, self.tmp / "dest", team=team, run=FakeCodesign(), decode=lambda path: facts)
            self.assertIn("not " + str(team), caught.exception.message)
        self.assertFalse((self.tmp / "dest" / "Runner.app").exists())

    def test_the_phone_must_be_listed_and_the_profile_must_have_time_left(self):
        out, _, _, facts = self.exported()
        with self.assertRaises(DevError) as caught:
            bundle.import_bundle(
                out, self.tmp / "d1", team=TEAM, udid=OTHER["udid"], run=FakeCodesign(), decode=lambda path: facts
            )
        self.assertIn("doesn't list this iPhone", caught.exception.message)
        later = datetime.datetime.now(datetime.timezone.utc).timestamp() + 400 * 86400
        with self.assertRaises(DevError) as caught:
            bundle.import_bundle(
                out, self.tmp / "d2", team=TEAM, run=FakeCodesign(), decode=lambda path: facts, now=later
            )
        self.assertIn("has ended", caught.exception.message)

    def test_a_changed_file_or_an_unverifying_signature_or_other_patches_is_refused(self):
        out, manifest, codesign, facts = self.exported()
        with mock.patch.object(wda_patches, "patch_digest", return_value="other"):
            with self.assertRaises(DevError) as caught:
                bundle.import_bundle(out, self.tmp / "d", team=TEAM, run=FakeCodesign(), decode=lambda path: facts)
        self.assertIn("other patches", caught.exception.message)
        with self.assertRaises(DevError):
            bundle.import_bundle(out, self.tmp / "d", team=TEAM, run=FakeCodesign(verify=1), decode=lambda path: facts)
        tampered = self.tmp / "tampered.tgz"
        with tarfile.open(out) as source, tarfile.open(tampered, "w:gz") as target:
            for member in source.getmembers():
                data = source.extractfile(member) if member.isfile() else None
                if member.name.endswith("Runner.app/WebDriverAgentRunner-Runner"):
                    payload = b"#!evil!"
                    member.size = len(payload)
                    data = io.BytesIO(payload)
                target.addfile(member, data)
        with self.assertRaises(DevError) as caught:
            bundle.import_bundle(tampered, self.tmp / "d", team=TEAM, run=FakeCodesign(), decode=lambda path: facts)
        self.assertIn("changed after it was exported", caught.exception.message)
        self.assertFalse((self.tmp / "d" / "Runner.app").exists())

    def test_a_profile_that_disagrees_with_the_label_is_refused(self):
        out, manifest, codesign, facts = self.exported()
        lying = dict(facts, ProvisionedDevices=[PHONE["udid"], OTHER["udid"]])
        with self.assertRaises(DevError) as caught:
            bundle.import_bundle(out, self.tmp / "d", team=TEAM, run=FakeCodesign(), decode=lambda path: lying)
        self.assertIn("doesn't say what its label says", caught.exception.message)

    def test_a_file_that_is_not_a_bundle_or_escapes_the_folder_is_refused(self):
        junk = self.tmp / "junk.tgz"
        junk.write_bytes(b"not a tar")
        with self.assertRaises(DevError):
            bundle.read_manifest(junk)
        evil = self.tmp / "evil.tgz"
        manifest = {
            "kind": bundle.KIND,
            "version": 1,
            "team": TEAM,
            "bundle_id": RUNNER_ID,
            "udids": [PHONE["udid"]],
            "expires": datetime.datetime.now().timestamp() + 365 * 86400,
            "patches": wda_patches.patch_digest(),
            "tree": "x",
        }
        with tarfile.open(evil, "w:gz") as archive:
            data = json.dumps(manifest).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
            escape = tarfile.TarInfo("Runner.app/../../escaped")
            escape.size = 1
            archive.addfile(escape, io.BytesIO(b"x"))
        with self.assertRaises(Exception):
            bundle.import_bundle(
                evil, self.tmp / "d", team=TEAM, run=FakeCodesign(), decode=lambda path: profile_dict()
            )
        self.assertFalse((self.tmp.parent / "escaped").exists())

    def test_an_export_of_a_runner_that_does_not_verify_is_refused(self):
        runner = make_runner(self.tmp / "bad")
        (runner / "embedded.mobileprovision").write_bytes(b"p")
        with self.assertRaises(DevError):
            bundle.export(runner, self.tmp / "x.tgz", run=FakeCodesign(verify=1), decode=lambda path: profile_dict())
        self.assertFalse((self.tmp / "x.tgz").exists())


class FakeSecurity:
    """openssl, security(1) and the keychain file, as far as Identity looks at them."""

    def __init__(self, folder):
        self.folder = Path(folder)

    def __call__(self, argv, stdin=None, timeout=60):
        if argv[0] == identity.OPENSSL:
            Path(argv[argv.index("-keyout") + 1]).write_text("KEY")
            Path(argv[argv.index("-out") + 1]).write_text(CSR)
        if argv[1:2] == ["find-identity"]:
            return 0, f'1) {SHA1} "x"\n', ""
        if stdin and stdin.startswith("create-keychain"):
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / identity.KEYCHAIN_FILE).write_text("kc")
        return 0, "", ""


class ServiceTests(Folder):
    """The whole run on recorded answers: card, approval, writes, certificate, profile, sign, install, record."""

    def setUp(self):
        super().setUp()
        self.prebuilt = self.tmp / "pre"
        make_runner(self.prebuilt)
        (self.prebuilt / "patches.txt").write_text(wda_patches.patch_digest())
        self.env = {FLAG: "1", prebuilt.ENV: str(self.prebuilt)}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.keychain = MemoryStore()
        self.commands = []
        self.saved = {}

        class Manager:
            data_dir = self.tmp
            derived = self.tmp / "derived"

            def settings(inner):
                return dict(self.saved)

            def save_settings(inner, **changes):
                self.saved.update(changes)
                return dict(self.saved)

            def devices(inner):
                return [{"udid": PHONE["udid"], "name": PHONE["name"], "trusted": True}]

            def device(inner, devices=None):
                return (devices or inner.devices())[0]

        self.manager = Manager()

    def build(self, apple=None, **overrides):
        apple = apple or Apple(
            routes(
                bundle={
                    "data": [
                        {
                            "id": "BID1",
                            "type": "bundleIds",
                            "attributes": {"identifier": RUNNER_ID, "name": "Runner", "seedId": TEAM},
                        }
                    ]
                }
            ),
            full_posts(),
        )
        self.apple = apple
        folder = self.tmp / "ts"
        ident = identity.Identity(folder, store=self.keychain, run=FakeSecurity(folder))

        def fake_sign(source, profile, sha1, keychain, destination, **kw):
            return resign.sign(
                source,
                profile,
                sha1,
                keychain,
                destination,
                run=FakeCodesign(),
                decode=lambda p: profile_dict(certs=(DER,)),
                **kw,
            )

        options = dict(
            folder=folder,
            identity=ident,
            make_client=lambda: client(apple),
            sign=fake_sign,
            pmd3=lambda: "/opt/homebrew/bin/pymobiledevice3",
            go_ios=lambda: None,
            runner=self.fake_tool,
            environ=self.env,
            load_key=lambda: asc.Key(TEAM_KEY, ISSUER, p8_for(RFC_D), "Keychain"),
        )
        options.update(overrides)
        return service_module.TeamSigning(self.manager, **options)

    def fake_tool(self, argv, timeout):
        self.commands.append(argv)
        if argv[1:3] == ["mounter", "list"]:
            return 0, "[]"
        return 0, "ok"

    def apple_with_certificate_content(self):
        apple = Apple(
            routes(
                bundle={
                    "data": [
                        {
                            "id": "BID1",
                            "type": "bundleIds",
                            "attributes": {"identifier": RUNNER_ID, "name": "Runner", "seedId": TEAM},
                        }
                    ]
                }
            ),
            {
                **full_posts(),
                "/v1/certificates": created(
                    "certificates", "C-NEW", serialNumber="S1", certificateContent=base64.b64encode(DER).decode()
                ),
                "/v1/profiles": created(
                    "profiles", "P-NEW", profileContent=base64.b64encode(b"profile").decode(), uuid="U"
                ),
            },
        )
        return apple

    def test_it_is_off_unless_the_flag_is_on(self):
        self.assertTrue(enabled({FLAG: "1"}))
        self.assertFalse(enabled({}))
        off = self.build(environ={})
        with self.assertRaises(DevError):
            off.card()

    def test_the_card_sends_nothing_and_the_run_does_what_it_showed(self):
        apple = self.apple_with_certificate_content()
        ts = self.build(apple)
        shown = ts.card(bundle_identifier=RUNNER_ID)
        self.assertEqual(
            [step["write"] for step in shown["steps"]], ["register_device", "create_certificate", "create_profile"]
        )
        self.assertTrue(all(method == "GET" for method, *_ in apple.seen))
        self.assertEqual(ts.status()["stage"], "approval")
        ts.approve(shown["digest"], background=False)
        status = ts.status()
        self.assertEqual(status["stage"], "ready", status)
        self.assertEqual([path for path, _ in apple.sent()], ["/v1/devices", "/v1/certificates", "/v1/profiles"])
        install = next(argv for argv in self.commands if argv[1:3] == ["apps", "install"])
        self.assertTrue(install[3].endswith("Runner.app"))
        self.assertEqual(self.commands[-1][1:3], ["apps", "install"])
        self.assertIn(
            ["/opt/homebrew/bin/pymobiledevice3", "mounter", "auto-mount", "--native", "--udid", PHONE["udid"]],
            self.commands,
        )
        self.assertEqual(
            (self.saved["driver"], self.saved["built_for"], self.saved["team"]),
            ("pymobiledevice3", PHONE["udid"], TEAM),
        )
        self.assertEqual(self.saved["signed_runner"]["bundle_id"], RUNNER_ID)
        self.assertEqual(self.saved["built_patches"], wda_patches.patch_digest())
        self.assertTrue(all(Path(argv[0]).name not in tooling.NEVER for argv in self.commands))

    def test_a_card_that_was_not_approved_or_changed_sends_nothing(self):
        apple = self.apple_with_certificate_content()
        ts = self.build(apple)
        with self.assertRaises(DevError):
            ts.approve("0" * 64, background=False)  # no card yet
        shown = ts.card(bundle_identifier=RUNNER_ID)
        with self.assertRaises(DevError):
            ts.approve("0" * 64, background=False)
        self.assertEqual(apple.sent(), [])
        self.assertEqual(ts.status()["stage"], "approval")
        # Apple's state changes between the card and the approval: the run refuses.
        apple.routes["/v1/devices"] = {"data": [device("D1", PHONE)]}
        ts.approve(shown["digest"], background=False)
        self.assertEqual(ts.status()["stage"], "failed")
        self.assertIn("changed after you approved", ts.status()["error"])
        self.assertEqual(apple.sent(), [])

    def test_writes_only_stops_after_apple_and_signs_nothing(self):
        apple = self.apple_with_certificate_content()
        ts = self.build(apple)
        shown = ts.card(bundle_identifier=RUNNER_ID)
        ts.approve(shown["digest"], writes_only=True, background=False)
        self.assertEqual(ts.status()["stage"], "ready")
        self.assertEqual(ts.status()["result"]["writes"]["certificate"], "C-NEW")
        self.assertEqual(self.commands, [])
        self.assertFalse((self.tmp / "ts" / "runners").exists())

    def test_the_certificate_is_kept_the_moment_apple_answers_even_if_the_rest_fails(self):
        apple = self.apple_with_certificate_content()
        apple.posts["/v1/profiles"] = TimeoutError("late")
        ts = self.build(apple)
        shown = ts.card(bundle_identifier=RUNNER_ID)
        ts.approve(shown["digest"], background=False)
        self.assertEqual(ts.status()["stage"], "failed")
        self.assertIn("never saw its answer", ts.status()["error"])
        self.assertEqual(ts.identity.signing_identity()["sha1"], SHA1)
        self.assertEqual(len(ts.status()["unresolved"]), 1)
        self.assertEqual(ts.status()["unresolved"][0]["write"], "create_profile")

    def test_a_certificate_made_but_lost_is_fetched_by_the_id_the_ledger_kept(self):
        apple = self.apple_with_certificate_content()
        ts = self.build(apple)
        ts.identity.request()
        body = writes.certificate_body(CSR)
        digest = asc.digest("create_certificate", body)
        ts.ledger.begin("create_certificate", digest, {})
        ts.ledger.end(digest, "done", {"id": "C-LOST"})  # Apple made it; Mobster stopped before keeping it
        apple.routes["/v1/certificates/C-LOST"] = {
            "data": {
                "id": "C-LOST",
                "attributes": {"serialNumber": "SL", "certificateContent": base64.b64encode(DER).decode()},
            }
        }
        ts._recover(client(apple))
        self.assertEqual(ts.identity.signing_identity(), {"sha1": SHA1, "serial": "SL"})
        self.assertEqual(apple.sent(), [])

    def test_a_missing_tool_or_prebuilt_runner_is_said_before_anything_is_sent(self):
        apple = self.apple_with_certificate_content()
        ts = self.build(apple, pmd3=lambda: None)
        self.assertIsNone(ts.status()["driver"])
        shown = ts.card(bundle_identifier=RUNNER_ID)
        ts.approve(shown["digest"], background=False)
        self.assertEqual(ts.status()["stage"], "failed")

    def test_the_status_names_the_key_without_its_text(self):
        status = self.build().status()
        self.assertEqual(status["key"], {"keyId": TEAM_KEY, "issuerId": ISSUER, "kind": "team", "source": "Keychain"})
        self.assertNotIn("BEGIN", json.dumps(status, default=str))

    def test_export_and_import_through_the_service(self):
        apple = self.apple_with_certificate_content()
        ts = self.build(apple)
        shown = ts.card(bundle_identifier=RUNNER_ID)
        ts.approve(shown["digest"], background=False)
        with (
            mock.patch.object(resign, "decode_profile", lambda path, run=None: profile_dict()),
            mock.patch.object(resign, "_run", FakeCodesign()),
        ):
            manifest = ts.export_bundle(self.tmp / "out.tgz")
            self.assertEqual(manifest["team"], TEAM)
            other = self.build()
            other.import_bundle(self.tmp / "out.tgz", TEAM)
        self.assertEqual(self.saved["signed_runner"]["prebuilt"], "an imported bundle")


class ManagerTests(Folder):
    def manager(self, **settings):
        from mobile_agent.device_manager import DeviceManager

        manager = DeviceManager(self.tmp)
        manager.save_settings(udid=PHONE["udid"], **settings)
        return manager

    def test_a_phone_signed_here_is_built_while_its_runner_exists_and_the_profile_lasts(self):
        runner = make_runner(self.tmp / "r")
        manager = self.manager(
            driver="pymobiledevice3",
            built_for=PHONE["udid"],
            built_patches=wda_patches.patch_digest(),
            expires_at=datetime.datetime.now().timestamp() + 86400 * 300,
            signed_runner={"path": str(runner), "bundle_id": RUNNER_ID},
        )
        self.assertTrue(manager.built())
        manager.save_settings(expires_at=1.0)
        self.assertFalse(manager.built())
        manager.save_settings(expires_at=datetime.datetime.now().timestamp() + 86400 * 300, built_patches="old")
        self.assertFalse(manager.built())
        manager.save_settings(built_patches=wda_patches.patch_digest(), signed_runner={"path": str(self.tmp / "gone")})
        self.assertFalse(manager.built())

    def test_without_a_driver_the_manager_is_xcodes_as_before(self):
        manager = self.manager()
        self.assertEqual(manager.driver(), "xcodebuild")
        self.assertFalse(manager.built())

    def test_the_launch_is_pymobiledevice3s_with_no_xcode_environment(self):
        runner = make_runner(self.tmp / "r")
        manager = self.manager(driver="pymobiledevice3", signed_runner={"path": str(runner), "bundle_id": RUNNER_ID})
        with mock.patch.object(tooling, "pmd3", return_value="/opt/homebrew/bin/pymobiledevice3"):
            command, env = manager._signed_runner_command(PHONE["udid"], manager.settings())
        self.assertEqual(command[:5], ["/opt/homebrew/bin/pymobiledevice3", "developer", "dvt", "xcuitest", RUNNER_ID])
        self.assertIn("USE_IP=127.0.0.1", command)
        self.assertNotIn("DEVELOPER_DIR", env if "DEVELOPER_DIR" not in os.environ else {})
        manager.save_settings(driver="go-ios")
        with mock.patch.object(tooling, "go_ios", return_value="/opt/homebrew/bin/ios"):
            command, _ = manager._signed_runner_command(PHONE["udid"], manager.settings())
        self.assertEqual(command[:2], ["/opt/homebrew/bin/ios", "runwda"])
        with mock.patch.object(tooling, "go_ios", return_value=None), self.assertRaises(LookupError):
            manager._signed_runner_command(PHONE["udid"], manager.settings())


class SetupStateTests(Folder):
    def test_without_xcode_the_xcode_check_is_not_needed_and_the_state_carries_the_signing_block(self):
        from mobile_agent.setup_service import SetupService

        service = SetupService.__new__(SetupService)
        service.cache = {}
        checks = SetupService.preflight(
            service,
            {"xcode": {"state": "missing", "version": None}, "idevice_id": "/x"},
            [],
            None,
            None,
            without_xcode=True,
        )
        self.assertEqual((checks[0]["id"], checks[0]["state"], checks[0]["word"]), ("xcode", "ok", "Not needed"))
        plain = SetupService.preflight(
            service, {"xcode": {"state": "missing", "version": None}, "idevice_id": "/x"}, [], None, None
        )
        self.assertEqual(plain[0]["state"], "todo")

    def test_the_routes_exist_only_in_setup_scope_and_reach_the_service(self):
        from mobile_agent import api_http

        for tail in ("card", "approve", "resolve", "devmode"):
            self.assertTrue(api_http.setup_scope_allows("POST", f"/api/setup/signing/{tail}"), tail)
        self.assertFalse(api_http.setup_scope_allows("POST", "/api/setup/signing/export"))
        self.assertFalse(api_http.setup_scope_allows("GET", "/api/setup/signing/card"))


class CommandTests(Folder):
    """`mobster signing`: the card prints what would be sent and a digest; apply needs that digest."""

    def run_cli(self, argv, service):
        import contextlib

        from mobile_agent import cli_parser
        from mobile_agent.team_signing import cli

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.dict(os.environ, {FLAG: "1"}):
            args = cli_parser.build_parser().parse_args(["signing", *argv])
            code = cli.run(args, service=service)
        return code, out.getvalue(), err.getvalue()

    def service(self, apple=None):
        self.store = getattr(self, "store", None) or MemoryStore()  # one Keychain for the Mac across the commands
        ident = identity.Identity(self.tmp / "ts", store=self.store, run=FakeSecurity(self.tmp / "ts"))
        posts = {
            **full_posts(),
            "/v1/certificates": created(
                "certificates", "C-NEW", serialNumber="S1", certificateContent=base64.b64encode(DER).decode()
            ),
        }
        apple = apple or Apple(
            routes(
                bundle={
                    "data": [
                        {
                            "id": "BID1",
                            "type": "bundleIds",
                            "attributes": {"identifier": RUNNER_ID, "name": "Runner", "seedId": TEAM},
                        }
                    ]
                }
            ),
            posts,
        )
        self.apple = apple
        return service_module.TeamSigning(
            None,
            folder=self.tmp / "ts",
            identity=ident,
            make_client=lambda: client(apple),
            load_key=lambda: asc.Key(TEAM_KEY, ISSUER, p8_for(RFC_D), "Keychain"),
        )

    def test_the_card_prints_every_request_and_the_digest_to_approve_and_sends_nothing(self):
        code, out, _ = self.run_cli(
            ["card", "--phone-udid", PHONE["udid"], "--phone-name", PHONE["name"], "--bundle-id", RUNNER_ID],
            self.service(),
        )
        self.assertEqual(code, 0)
        for text in (
            "POST https://api.appstoreconnect.apple.com/v1/devices",
            "/v1/certificates",
            "/v1/profiles",
            PHONE["udid"],
            "Digest",
            "mobster signing apply --approve",
        ):
            self.assertIn(text, out)
        self.assertTrue(all(method == "GET" for method, *_ in self.apple.seen))

    def test_apply_needs_the_digest_of_the_card_it_sees(self):
        service = self.service()
        flags = ["--phone-udid", PHONE["udid"], "--phone-name", PHONE["name"], "--bundle-id", RUNNER_ID]
        code, out, err = self.run_cli(["apply", "--approve", "0" * 64, "--writes-only", *flags], service)
        self.assertEqual(code, 3)
        self.assertIn("isn't the card on screen", err)
        self.assertEqual(self.apple.sent(), [])
        digest = json.loads(self.run_cli(["card", "--json", *flags], service)[1])["digest"]
        code, out, err = self.run_cli(["apply", "--approve", digest, "--writes-only", *flags], self.service(self.apple))
        self.assertEqual(code, 0, (out, err))
        self.assertEqual([path for path, _ in self.apple.sent()], ["/v1/devices", "/v1/certificates", "/v1/profiles"])

    def test_status_names_a_change_waiting_on_the_person_and_resolve_clears_it(self):
        service = self.service()
        service.ledger.begin("create_certificate", "d" * 64, {"csr_sha256": "x"})
        code, out, _ = self.run_cli(["status"], service)
        self.assertIn("create certificate was sent and never answered", out)
        self.assertIn("mobster signing resolve " + "d" * 64, out)
        self.assertEqual(self.run_cli(["resolve", "d" * 64, "--not-made"], service)[0], 0)
        self.assertEqual(service.ledger.unresolved(), [])

    def test_it_says_it_is_off_without_the_flag_and_the_command_stays_out_of_the_top_help(self):
        from mobile_agent import help_topics

        self.assertIn("signing", help_topics.HIDDEN)
        service = self.service()
        service.environ = {}
        with mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop(FLAG, None)
            from mobile_agent import cli_parser
            from mobile_agent.team_signing import cli

            args = cli_parser.build_parser().parse_args(["signing", "card"])
            import contextlib

            with contextlib.redirect_stderr(io.StringIO()) as err:
                code = cli.run(args, service=service)
        self.assertEqual(code, 3)
        self.assertIn("Without Xcode is off", err.getvalue())


if __name__ == "__main__":
    unittest.main()

"""`mobster test`'s discovery, quarantine, tags, shards and matrix, and where a check may run (testkit/discover.py,
testkit/targets.py). Offline: fake device lists, fake devicectl and installer, no simulator or phone."""

import json
from pathlib import Path
import plistlib
import tempfile
import unittest
import zipfile

from mobile_agent.testkit import discover as D
from mobile_agent.testkit import targets as T
from mobile_agent.verify.checks import CheckError, check_from_dict, dump_check, load_check

BUNDLE = "dev.mobster.daybreak"
PHONE = {"id": "usb-00008140", "udid": "00008130-001A2B3C4D5E6F70", "kind": "usb", "name": "Sam's iPhone",
         "state": "ready", "wdaUrl": "http://127.0.0.1:8100"}
WDA = {"id": "wda-1", "kind": "wda", "name": "Lab phone", "wdaUrl": "http://192.0.2.4:8100"}
SIM = {"id": "sim-1", "udid": "11111111-2222-3333-4444-555555555555", "kind": "simulator",
       "name": "Mobster · iPhone 17 Pro · iOS 26.4"}


def check_file(folder, stem, **data):
    data.setdefault("app", {"bundle": BUNDLE})
    data.setdefault("expect", [{"text": "Choose your plan"}])
    data.setdefault("name", stem.replace("-", " ").capitalize())
    path = Path(folder) / f"{stem}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    import yaml
    path.write_text(yaml.safe_dump({"version": 1, **data}, sort_keys=False))
    return path


def devices(*records):
    by_name = {}
    for record in records:
        for key in ("id", "udid", "name"):
            if record.get(key):
                by_name[record[key].casefold()] = record
    return lambda name: by_name.get(str(name).casefold())


class Project(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.checks = self.root / ".mobster" / "checks"
        self.checks.mkdir(parents=True)


class TagsInTheCheckFormatTests(unittest.TestCase):
    def test_tags_round_trip_and_are_checked(self):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "tags": ["smoke", "paywall", "smoke"],
                                 "expect": [{"text": "Hi"}]})
        self.assertEqual(check.tags, ("smoke", "paywall"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "c.yaml"
            path.write_text(dump_check(check))
            self.assertEqual(load_check(path).tags, ("smoke", "paywall"))
        self.assertEqual(check_from_dict({"app": {"bundle": BUNDLE}, "tags": "smoke"}).tags, ("smoke",))
        for bad in (["Smoke Test"], [3], {"a": 1}, ["x" * 41], [f"t{n}" for n in range(11)]):
            with self.subTest(tags=bad), self.assertRaises(CheckError):
                check_from_dict({"app": {"bundle": BUNDLE}, "tags": bad})
        with self.assertRaisesRegex(CheckError, "check file"):
            check_from_dict({"bundle_id": BUNDLE, "tags": ["smoke"]})


class DiscoveryTests(Project):
    def test_the_default_folder_sorted_skipping_what_isnt_a_check(self):
        check_file(self.checks, "paywall")
        check_file(self.checks / "flows", "onboarding")
        (self.checks / "matrix.yaml").write_text("simulators: [iPhone 17 Pro]\n")
        (self.checks / "paywall.heal.yaml").write_text("x: 1\n")
        (self.checks / ".draft.yaml").write_text("x: 1\n")
        (self.checks / "notes.md").write_text("# notes\n")
        entries = D.discover([], self.root, cwd=self.root)
        self.assertEqual([e.file for e in entries], [".mobster/checks/flows/onboarding.yaml",
                                                     ".mobster/checks/paywall.yaml"])
        self.assertEqual([e.classname for e in entries], ["checks.flows.onboarding", "checks.paywall"])
        self.assertIn("flows/onboarding", entries[0].keys)

    def test_the_mobster_folder_itself_finds_only_checks(self):
        """`mobster test .mobster`: run results, reports, builds and the cache hold .json files that aren't checks."""
        check_file(self.checks, "paywall")
        mobster = self.root / ".mobster"
        for name in ("runs/20261007-101500-3f9a/result.json", "test-results/20261007-142233-9c1e/results.json",
                     "cache/paywall.iphone-17-pro.json", "build/Build/Products/info.json"):
            (mobster / name).parent.mkdir(parents=True, exist_ok=True)
            (mobster / name).write_text("{}")
        entries = D.discover([".mobster"], self.root, cwd=self.root)
        self.assertEqual([e.file for e in entries], [".mobster/checks/paywall.yaml"])
        (self.checks / "runs").mkdir()
        check_file(self.checks / "runs", "a-run-check")  # a folder of the user's own checks keeps any name
        self.assertEqual(len(D.discover([".mobster"], self.root, cwd=self.root)), 2)

    def test_a_broken_check_is_kept_with_its_error(self):
        check_file(self.checks, "good")
        (self.checks / "bad.yaml").write_text("version: 1\napp: {bundle: dev.x.y}\nexpekt: []\n")
        entries = D.discover([], self.root, cwd=self.root)
        bad = next(e for e in entries if e.file.endswith("bad.yaml"))
        self.assertIsNone(bad.check)
        self.assertIn("did you mean expect", bad.error)
        self.assertEqual(bad.name, "bad")

    def test_paths_files_and_folders(self):
        one = check_file(self.root / "more", "one")
        check_file(self.checks, "two")
        entries = D.discover([str(one), ".mobster/checks", str(one)], self.root, cwd=self.root)
        self.assertEqual([e.file for e in entries], [".mobster/checks/two.yaml", "more/one.yaml"])

    def test_usage_errors(self):
        empty = self.root / "empty"
        empty.mkdir()
        (self.root / "notes.txt").write_text("x")
        for paths, words in ((["missing"], "doesn't exist"), ([str(empty)], "has no checks"),
                             (["notes.txt"], "isn't a check file")):
            with self.subTest(paths=paths), self.assertRaisesRegex(D.DiscoveryError, words):
                D.discover(paths, self.root, cwd=self.root)
        with self.assertRaisesRegex(D.DiscoveryError, "has no checks"):
            D.discover([], self.root, cwd=self.root)
        with tempfile.TemporaryDirectory() as bare, self.assertRaisesRegex(D.DiscoveryError, "no .mobster/checks"):
            D.discover([], bare, cwd=bare)

    def test_the_project_root_is_found_walking_up(self):
        nested = self.root / "app" / "Sources"
        nested.mkdir(parents=True)
        self.assertEqual(D.project_root(nested), self.root)


class QuarantineTagsShardTests(Project):
    def entries(self):
        check_file(self.checks, "paywall", tags=["smoke"], name="The paywall shows three plans")
        check_file(self.checks, "restore", tags=["purchases"])
        check_file(self.checks, "today")
        (self.checks / "broken.yaml").write_text("nope: 1\n")
        return D.discover([], self.root, cwd=self.root)

    def test_quarantine_by_file_name_path_or_check_name_with_a_reason(self):
        entries = self.entries()
        path = self.root / ".mobster" / "quarantine.yaml"
        path.write_text("quarantine:\n  - paywall\n  - check: Restore\n    reason: Flaky on iOS 26.4 (FB13579)\n"
                        "  - gone-check\n")
        listed = D.load_quarantine(path)
        unmatched = D.apply_quarantine(entries, listed)
        by = {e.path.stem: e.quarantined for e in entries}
        self.assertEqual(by["paywall"], "")
        self.assertEqual(by["restore"], "Flaky on iOS 26.4 (FB13579)")
        self.assertIsNone(by["today"])
        self.assertEqual(unmatched, ["gone-check"])
        path.write_text("- The paywall shows three plans\n")
        self.assertEqual(D.load_quarantine(path), {"the paywall shows three plans": ""})

    def test_quarantine_file_errors(self):
        path = self.root / "q.yaml"
        self.assertEqual(D.load_quarantine(path), {})
        with self.assertRaisesRegex(D.DiscoveryError, "doesn't exist"):
            D.load_quarantine(path, required=True)
        for text in ("checks: [a]\n", "- {check: a, why: b}\n", "- 3\n", "a: [\n", "just text\n"):
            path.write_text(text)
            with self.subTest(text=text), self.assertRaises(D.DiscoveryError):
                D.load_quarantine(path)

    def test_tags_keep_matching_checks_and_broken_ones(self):
        entries = self.entries()
        kept = D.with_tags(entries, ["SMOKE"])
        self.assertEqual(sorted(e.path.stem for e in kept), ["broken", "paywall"])
        self.assertEqual(len(D.with_tags(entries, [])), 4)

    def test_shards_split_every_check_once(self):
        entries = self.entries()
        self.assertEqual(D.parse_shard(" 2/3 "), (2, 3))
        for bad in ("0/2", "3/2", "1/101", "a/b", "2"):
            with self.subTest(shard=bad), self.assertRaises(D.DiscoveryError):
                D.parse_shard(bad)
        shards = [D.shard(entries, index, 3) for index in (1, 2, 3)]
        files = [e.file for part in shards for e in part]
        self.assertEqual(sorted(files), sorted(e.file for e in entries))
        self.assertEqual(len(files), len(set(files)))
        self.assertEqual([len(part) for part in shards], [2, 1, 1])


class DaybreakExampleTests(unittest.TestCase):
    def test_the_sample_apps_checks_load_and_three_are_smoke_checks(self):
        root = Path(__file__).resolve().parents[2] / "examples" / "ios" / "Daybreak"
        if not (root / ".mobster" / "checks").is_dir():
            self.skipTest("the sample app is not in this tree")
        entries = D.discover([], root, cwd=root)
        self.assertEqual([e.error for e in entries], [None] * len(entries))
        self.assertGreaterEqual(len(entries), 5)
        self.assertEqual(sorted(e.path.stem for e in D.with_tags(entries, ["smoke"])), ["paywall", "settings", "today"])
        self.assertEqual((root / ".mobster" / ".gitignore").read_text(), "runs/\nbuild/\ncache/\ntest-results/\n")


class MatrixTests(Project):
    def test_sims_and_matrix_files(self):
        self.assertEqual(T.parse_sim(" iPhone 17 Pro @ iOS 26.4 "), ("iPhone 17 Pro", "iOS 26.4"))
        self.assertEqual(T.parse_sim("iPhone 16e"), ("iPhone 16e", None))
        with self.assertRaises(T.TargetError):
            T.parse_sim("@iOS 26.4")
        matrix = self.root / "matrix.yaml"
        matrix.write_text("simulators:\n  - iPhone 17 Pro@iOS 26.4\n  - {device: iPhone SE (3rd generation), "
                          "runtime: iOS 26.4}\n  - iPhone 16e\n")
        self.assertEqual([t.label for t in T.load_matrix(matrix)],
                         ["iPhone 17 Pro · iOS 26.4", "iPhone SE (3rd generation) · iOS 26.4", "iPhone 16e"])
        for text, words in (("devices: [Sam's iPhone]\n", "simulators only"), ("sims: [a]\n", "unknown key"),
                            ("simulators: []\n", "must list"), ("- {device: a, os: b}\n", "write"),
                            ("- 3\n", "write")):
            matrix.write_text(text)
            with self.subTest(text=text), self.assertRaisesRegex(T.TargetError, words):
                T.load_matrix(matrix)

    def test_a_matrix_entry_is_never_looked_up_as_a_device(self):
        """A phone named like a device type is never reached through --sim or a matrix file."""
        (self.root / ".mobster" / "matrix.yaml").write_text("- iPhone 17 Pro\n")
        named = devices({**PHONE, "name": "iPhone 17 Pro"})
        targets = T.resolve(root=self.root, named_device=named)
        self.assertEqual([(t.kind, t.device_type, t.record) for t in targets], [("simulator", "iPhone 17 Pro", None)])
        targets = T.resolve(sims=["iPhone 17 Pro"], named_device=named)
        self.assertEqual(targets[0].record, None)

    def test_resolve_orders_dedupes_and_defaults(self):
        named = devices(PHONE, SIM)
        targets = T.resolve(sims=["iPhone 17 Pro", "iPhone 17 Pro"], devices=["sam's iphone", SIM["name"]],
                            root=self.root, named_device=named)
        self.assertEqual([t.key for t in targets], ["sim:iPhone 17 Pro@", "device:usb-00008140", "device:sim-1"])
        self.assertEqual([t.lane for t in targets], ["sims", "device:usb-00008140", "device:sim-1"])
        self.assertEqual(T.resolve(root=self.root, named_device=named), [T.DEFAULT])
        with self.assertRaisesRegex(T.TargetError, "No device is named"):
            T.resolve(devices=["Kate's iPhone"], named_device=named)


class Plist:
    @staticmethod
    def app(folder, platform, bundle=BUNDLE):
        app = Path(folder) / f"Daybreak-{platform}.app"
        app.mkdir()
        with open(app / "Info.plist", "wb") as handle:
            plistlib.dump({"CFBundleIdentifier": bundle, "DTPlatformName": platform,
                           "CFBundleSupportedPlatforms": ["iPhoneSimulator" if platform == "iphonesimulator"
                                                          else "iPhoneOS"]}, handle)
        return str(app)

    @staticmethod
    def ipa(folder, bundle=BUNDLE, name="Daybreak"):
        """An .ipa: Payload/<name>.app/Info.plist (and a binary), zipped as Xcode exports one."""
        path = Path(folder) / f"{name}.ipa"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"Payload/{name}.app/Info.plist",
                             plistlib.dumps({"CFBundleIdentifier": bundle, "DTPlatformName": "iphoneos"}))
            archive.writestr(f"Payload/{name}.app/{name}", b"\0" * 16)
        return str(path)


class Devicectl:
    """`xcrun devicectl device info apps --json-output FILE` with a fixed app list."""

    def __init__(self, apps, code=0):
        self.apps, self.code, self.calls = apps, code, []

    def __call__(self, argv):
        self.calls.append(argv)
        out = Path(argv[argv.index("--json-output") + 1])
        if self.code == 0:
            out.write_text(json.dumps({"info": {"outcome": "success"}, "result": {"apps": self.apps}}))
        return self.code, "" if self.code == 0 else "ERROR: The device is locked."


class TargetRuleTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = str(Path(folder.name).resolve())

    def check(self, **data):
        data.setdefault("bundle_id", BUNDLE)
        return check_from_dict(data)

    def gate(self, apps=(), code=0, installer=None, allowed=True):
        self.devicectl = Devicectl(list(apps), code)
        self.installs = []

        def install(record, path):
            self.installs.append((record["id"], path))
            return {"bundle_id": BUNDLE}
        return T.DeviceGate(runner=self.devicectl, installer=installer or install, allowed=lambda: allowed)

    def test_a_simulator_target_replaces_the_checks_device(self):
        check = self.check(device="iPhone 16e", runtime="iOS 26.0")
        ran, notes, record = T.for_target(check, T.simulator("iPhone 17 Pro", "iOS 26.4"))
        self.assertEqual((ran.device, ran.runtime, record), ("iPhone 17 Pro", "iOS 26.4", None))

    def test_a_check_file_naming_a_real_iphone_never_reaches_it(self):
        named = devices(PHONE, SIM)
        with self.assertRaisesRegex(T.Refused, "a check file may name only a simulator") as caught:
            T.for_target(self.check(device="Sam's iPhone"), T.DEFAULT, named_device=named)
        self.assertEqual(caught.exception.klass, "usage")
        self.assertIn('--device "Sam\'s iPhone"', caught.exception.message)
        ran, _, record = T.for_target(self.check(device=SIM["name"]), T.DEFAULT, named_device=named)
        self.assertEqual(record, SIM)
        ran, _, record = T.for_target(self.check(device="iPhone 17 Pro"), T.DEFAULT, named_device=named)
        self.assertEqual((ran.device, record), ("iPhone 17 Pro", None))

    def test_social_and_dating_apps_never_run(self):
        for target in (T.DEFAULT, T.simulator("iPhone 17 Pro"), T.Target("device:x", "device", record=PHONE)):
            with self.subTest(target=target.key), self.assertRaises(T.Refused) as caught:
                T.for_target(self.check(bundle_id="co.hinge.app"), target, gate=self.gate())
            self.assertEqual(caught.exception.klass, "not_unattended")
        self.assertEqual(self.devicectl.calls, [])

    def test_an_app_store_app_on_a_real_iphone_couldnt_run_before_any_tap(self):
        gate = self.gate([{"bundleIdentifier": BUNDLE, "name": "Daybreak", T.DEVELOPER_FIELD: False}])
        with self.assertRaises(T.Refused) as caught:
            gate.admit(self.check(), PHONE)
        self.assertEqual(caught.exception.message,
                         "Mobster tests your own apps on your iPhone. Daybreak is from the App Store.")
        self.assertEqual(caught.exception.klass, "not_developer_build")
        self.assertEqual(self.devicectl.calls[0][:6], ["xcrun", "devicectl", "device", "info", "apps", "--device"])
        self.assertEqual(self.devicectl.calls[0][6], PHONE["udid"])

    def test_a_field_devicectl_leaves_out_counts_as_not_a_developer_build(self):
        gate = self.gate([{"bundleIdentifier": BUNDLE, "name": "Daybreak"}])
        with self.assertRaisesRegex(T.Refused, "from the App Store"):
            gate.admit(self.check(), PHONE)

    def test_a_developer_build_runs_with_its_data_kept(self):
        gate = self.gate([{"bundleIdentifier": BUNDLE, "name": "Daybreak", T.DEVELOPER_FIELD: True}])
        ran, notes, record = gate.admit(self.check(reset="reinstall"), PHONE)
        self.assertEqual((ran.bundle_id, ran.app_path, ran.reset, ran.device), (BUNDLE, None, "none", None))
        self.assertIn("never clears an app's data", notes[0])
        gate.admit(self.check(), PHONE)
        self.assertEqual(len(self.devicectl.calls), 1)  # one app list per phone per suite

    def test_apps_that_are_part_of_ios_and_apps_not_on_the_phone(self):
        gate = self.gate([])
        with self.assertRaisesRegex(T.Refused, "Settings is part of iOS"):
            gate.admit(self.check(bundle_id="com.apple.Preferences"), PHONE)
        with self.assertRaisesRegex(T.Refused, "isn't on “Sam's iPhone”") as caught:
            gate.admit(self.check(), PHONE)
        self.assertEqual(caught.exception.klass, "install")

    def test_a_device_build_in_app_path_is_installed_once_and_counts_as_yours(self):
        gate = self.gate([])
        app = Plist.app(self.folder, "iphoneos")
        check = check_from_dict({"app_path": app})
        ran, notes, _ = gate.admit(check, PHONE)
        gate.admit(check, PHONE)
        self.assertEqual(self.installs, [(PHONE["id"], app)])
        self.assertEqual((ran.bundle_id, ran.app_path), (BUNDLE, None))
        self.assertIn("Installed Daybreak-iphoneos.app", notes[0])
        self.assertEqual(self.devicectl.calls, [])

    def test_a_simulator_build_falls_back_to_the_build_on_the_phone(self):
        gate = self.gate([{"bundleIdentifier": BUNDLE, T.DEVELOPER_FIELD: True}])
        app = Plist.app(self.folder, "iphonesimulator")
        ran, notes, _ = gate.admit(check_from_dict({"app_path": app}), PHONE)
        self.assertEqual(self.installs, [])
        self.assertIn("is a simulator build", notes[0])
        self.assertEqual(T.device_build(app), False)
        self.assertEqual(T.device_build("/x/Daybreak.ipa"), True)
        self.assertIsNone(T.device_build(None))

    def test_a_wda_address_a_locked_phone_and_devices_off(self):
        with self.assertRaisesRegex(T.Refused, "connected to this Mac"):
            self.gate([]).admit(self.check(), WDA)
        with self.assertRaisesRegex(T.Refused, "couldn't list the apps.*locked"):
            self.gate([], code=1).admit(self.check(), PHONE)
        gate = self.gate([], allowed=False)
        with self.assertRaisesRegex(T.Refused, "MOBSTER_NO_DEVICES"):
            gate.admit(self.check(), PHONE)
        self.assertEqual(self.devicectl.calls, [])

    def test_a_failed_install_is_couldnt_run_with_its_fix(self):
        class Problem(Exception):
            fix = "Turn it on in Settings › Privacy & Security › Developer Mode."

        def install(record, path):
            raise Problem("Developer Mode is off on the iPhone.")
        gate = self.gate([], installer=install)
        with self.assertRaises(T.Refused) as caught:
            gate.admit(check_from_dict({"app_path": Plist.app(self.folder, "iphoneos")}), PHONE)
        self.assertEqual((caught.exception.klass, caught.exception.fix),
                         ("install", "Turn it on in Settings › Privacy & Security › Developer Mode."))

    def test_an_ipa_of_a_social_or_dating_app_is_never_installed(self):
        """An .ipa's bundle is read from inside it: the deny list holds before the install, whatever app.bundle
        says, and again on what the install reports."""
        gate = self.gate([])
        ipa = Plist.ipa(self.folder, bundle="com.cardify.tinder", name="Tinder")
        self.assertEqual(T.bundle_of(ipa), "com.cardify.tinder")
        for check in (check_from_dict({"app_path": ipa}), self.check(app_path=ipa)):
            with self.subTest(bundle=check.bundle_id), self.assertRaises(T.Refused) as caught:
                T.for_target(check, T.Target("device:x", "device", record=PHONE), gate=gate)
            self.assertEqual(caught.exception.klass, "not_unattended")
        self.assertEqual(self.installs, [])

        def install(record, path):  # an archive Mobster couldn't read, which installs as a dating app
            return {"bundle_id": "co.hinge.app"}
        unreadable = Path(self.folder) / "Opaque.ipa"
        unreadable.write_bytes(b"not a zip")
        with self.assertRaises(T.Refused) as caught:
            self.gate([], installer=install).admit(check_from_dict({"app_path": str(unreadable)}), PHONE)
        self.assertEqual(caught.exception.klass, "not_unattended")

    def test_a_build_of_another_app_than_the_check_names_is_refused(self):
        gate = self.gate([])
        ipa = Plist.ipa(self.folder, bundle="com.example.other", name="Other")
        with self.assertRaises(T.Refused) as caught:
            gate.admit(self.check(app_path=ipa), PHONE)
        self.assertEqual(caught.exception.message, f"Other.ipa is com.example.other, not {BUNDLE}, the app the check "
                                                   "names.")
        self.assertEqual(self.installs, [])

        def install(record, path):
            return {"bundle_id": "com.example.other"}
        with self.assertRaisesRegex(T.Refused, "is com.example.other, not"):
            self.gate([], installer=install).admit(self.check(app_path=str(Path(self.folder) / "Opaque.ipa")), PHONE)
        ran, _, _ = self.gate([]).admit(self.check(app_path=Plist.ipa(self.folder, name="Daybreak2")), PHONE)
        self.assertEqual((ran.bundle_id, ran.app_path), (BUNDLE, None))

    def test_without_a_gate_a_real_device_is_refused(self):
        with self.assertRaises(T.Refused):
            T.for_target(self.check(), T.Target("device:x", "device", record=PHONE))


if __name__ == "__main__":
    unittest.main()

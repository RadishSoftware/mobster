import contextlib
import importlib.util
import io
import json
import os
import plistlib
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from xml.etree import ElementTree as ET

import yaml
from PIL import Image

from mobile_agent import links
from mobile_agent.testkit import api, cli, history, prrun, recent, recipes, register
from mobile_agent.testkit import discover as D
from mobile_agent.testkit import execute as E
from mobile_agent.testkit import pr as P
from mobile_agent.testkit import pr_report as R
from mobile_agent.testkit import prbuild as B
from mobile_agent.testkit import reports as REPORTS_R
from mobile_agent.testkit import targets as T
from mobile_agent.testkit.api import Options, plan
from mobile_agent.testkit.discover import Entry as SuiteEntry
from mobile_agent.testkit.suite import RETRY_CLASSES, Suite
from mobile_agent.tests.test_testing_record import main
from mobile_agent.tests.test_verify_runner import PLANS, Driver, Manager, RunnerCase
from mobile_agent.verify import fixtures, runner, runs, smart
from mobile_agent.verify.checks import (
    CheckError,
    check_from_dict,
    dump_check,
    load_check,
    project_root,
)


class RegistryTests(unittest.TestCase):
    def test_every_builtin_recipe_builds_a_check_that_loads(self):
        self.assertEqual(len(set(recipes.names())), len(recipes.names()), "recipe names are unique")
        for recipe in recipes.BUILTIN:
            given = {param.name: "x://y" if param.name == "url" else "Hello" for param in recipe.params
                     if param.required}
            with self.subTest(recipe=recipe.name):
                _, data = recipes.build(recipe.name, {**given, "bundle": "com.example.app"})
                self.assertEqual(data["app"]["bundle"], "com.example.app")
                self.assertTrue(recipe.summary and recipe.summary[0].islower() and not recipe.summary.endswith("."))

    def test_an_unset_param_drops_what_names_it(self):
        _, data = recipes.build("paywall", {"url": "x://paywall", "bundle": "com.example.app"})
        self.assertEqual(data["launch"], {"url": "x://paywall"})
        _, data = recipes.build("paywall", {"url": "x://paywall", "bundle": "com.example.app", "launch_arg": "-Local"})
        self.assertEqual(data["launch"]["args"], ["-Local"])

    def test_a_missing_required_param_an_unknown_param_and_a_missing_app_are_refused(self):
        with self.assertRaisesRegex(recipes.RecipeError, "needs text"):
            recipes.build("smoke", {"bundle": "com.example.app"})
        with self.assertRaisesRegex(recipes.RecipeError, "no param 'colour'"):
            recipes.build("smoke", {"text": "Hi", "bundle": "com.example.app", "colour": "red"})
        with self.assertRaisesRegex(recipes.RecipeError, "needs the app"):
            recipes.build("smoke", {"text": "Hi"})
        with self.assertRaisesRegex(recipes.RecipeError, "no recipe 'nope'"):
            recipes.build("nope", {})


class AddCommandTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name).resolve()
        previous = os.getcwd()
        os.chdir(self.folder)
        self.addCleanup(os.chdir, previous)

    def test_list_prints_each_recipe_on_one_line(self):
        code, out, _ = main(["test", "add", "--list"])
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), len(recipes.BUILTIN))
        for recipe, line in zip(recipes.BUILTIN, lines):
            self.assertTrue(line.startswith(recipe.name) and line.endswith(recipe.summary))

    def test_it_writes_a_normal_check_that_loads_and_never_overwrites_without_force(self):
        argv = ["test", "add", "paywall", "--bundle", "com.example.app", "--set", "url=x://paywall"]
        code, _, err = main(argv)
        self.assertEqual(code, 0, err)
        path = self.folder / ".mobster" / "checks" / "paywall.yaml"
        check = load_check(path)
        self.assertEqual(check.bundle_id, "com.example.app")
        self.assertEqual(check.open_url, "x://paywall")
        path.write_text(path.read_text() + "# mine\n")
        code, _, err = main(argv)
        self.assertEqual(code, 3)
        self.assertIn("pass --force", err)
        self.assertIn("# mine", path.read_text())
        code, _, _ = main(argv + ["--force"])
        self.assertEqual(code, 0)
        self.assertNotIn("# mine", path.read_text())

    def test_usage_errors_exit_3_and_write_nothing(self):
        for argv in (["test", "add"], ["test", "add", "nope"], ["test", "add", "smoke", "--bundle", "com.example.app"],
                     ["test", "add", "smoke", "--bundle", "com.example.app", "--set", "text"],
                     ["test", "add", "--list", "smoke"], ["test", "add", "smoke", "x", "--set", "text=a"],
                     ["test", "--list"], ["test", "--force"]):
            with self.subTest(argv=argv):
                code, _, err = main(argv)
                self.assertEqual(code, 3, err)
        self.assertFalse((self.folder / ".mobster").exists())


BUNDLE = "org.mozilla.ios.Firefox"


PHONE = {"id": "00008130-001A2B3C4D5E6F70", "udid": "00008130-001A2B3C4D5E6F70", "kind": "usb", "name": "Studio iPhone",
         "state": "ready", "wdaUrl": "http://127.0.0.1:8100"}


WDA = {"id": "wda-1", "kind": "wda", "name": "Lab phone", "wdaUrl": "http://192.0.2.4:8100"}


SIM = {"id": "11111111-2222-3333-4444-555555555555",
       "udid": "11111111-2222-3333-4444-555555555555", "kind": "simulator",
       "name": "Mobster · iPhone 17 Pro · iOS 26.4"}


def check_file(folder, stem, **data):
    data.setdefault("app", {"bundle": BUNDLE})
    data.setdefault("expect", [{"text": "Choose your plan"}])
    data.setdefault("name", stem.replace("-", " ").capitalize())
    path = Path(folder) / f"{stem}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
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
        self.assertEqual(project_root(nested), self.root)


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


class FirefoxExampleTests(unittest.TestCase):
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
        for text, words in (("devices: [Studio iPhone]\n", "simulators only"), ("sims: [a]\n", "unknown key"),
                            ("simulators: []\n", "must list"), ("- {device: a, os: b}\n", "write"),
                            ("- 3\n", "write")):
            matrix.write_text(text)
            with self.subTest(text=text), self.assertRaisesRegex(T.TargetError, words):
                T.load_matrix(matrix)

    def test_a_matrix_entry_is_never_looked_up_as_a_device(self):
        (self.root / ".mobster" / "matrix.yaml").write_text("- iPhone 17 Pro\n")
        named = devices({**PHONE, "name": "iPhone 17 Pro"})
        targets = T.resolve(root=self.root, named_device=named)
        self.assertEqual([(t.kind, t.device_type, t.record) for t in targets], [("simulator", "iPhone 17 Pro", None)])
        targets = T.resolve(sims=["iPhone 17 Pro"], named_device=named)
        self.assertEqual(targets[0].record, None)

    def test_resolve_orders_dedupes_and_defaults(self):
        named = devices(PHONE, SIM)
        targets = T.resolve(sims=["iPhone 17 Pro", "iPhone 17 Pro"], devices=["studio iphone", SIM["name"]],
                            root=self.root, named_device=named)
        self.assertEqual([t.key for t in targets], ["sim:iPhone 17 Pro@", "device:00008130-001A2B3C4D5E6F70",
                                                     "device:11111111-2222-3333-4444-555555555555"])
        self.assertEqual([t.lane for t in targets], ["sims", "device:00008130-001A2B3C4D5E6F70",
                                                      "device:11111111-2222-3333-4444-555555555555"])
        self.assertEqual(T.resolve(root=self.root, named_device=named), [T.DEFAULT])
        with self.assertRaisesRegex(T.TargetError, "No device is named"):
            T.resolve(devices=["Kate's iPhone"], named_device=named)


class Plist:
    @staticmethod
    def app(folder, platform, bundle=BUNDLE):
        app = Path(folder) / f"Firefox-{platform}.app"
        app.mkdir()
        with open(app / "Info.plist", "wb") as handle:
            plistlib.dump({"CFBundleIdentifier": bundle, "DTPlatformName": platform,
                           "CFBundleSupportedPlatforms": ["iPhoneSimulator" if platform == "iphonesimulator"
                                                          else "iPhoneOS"]}, handle)
        return str(app)

    @staticmethod
    def ipa(folder, bundle=BUNDLE, name="Firefox"):
        path = Path(folder) / f"{name}.ipa"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"Payload/{name}.app/Info.plist",
                             plistlib.dumps({"CFBundleIdentifier": bundle, "DTPlatformName": "iphoneos"}))
            archive.writestr(f"Payload/{name}.app/{name}", b"\0" * 16)
        return str(path)


class Devicectl:
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
            T.for_target(self.check(device="Studio iPhone"), T.DEFAULT, named_device=named)
        self.assertEqual(caught.exception.klass, "usage")
        self.assertIn('--device "Studio iPhone"', caught.exception.message)
        ran, _, record = T.for_target(self.check(device=SIM["name"]), T.DEFAULT, named_device=named)
        self.assertEqual(record, SIM)
        ran, _, record = T.for_target(self.check(device="iPhone 17 Pro"), T.DEFAULT, named_device=named)
        self.assertEqual((ran.device, record), ("iPhone 17 Pro", None))

    def test_social_and_dating_apps_run_like_any_other_app(self):
        hinge = "co.hinge.mobile.ios"
        check = self.check(bundle_id=hinge)
        for target in (T.DEFAULT, T.simulator("iPhone 17 Pro")):
            with self.subTest(target=target.key):
                self.assertEqual(T.for_target(check, target)[0].bundle_id, hinge)
        phone = T.Target("device:x", "device", record=PHONE)
        built = self.gate([{"bundleIdentifier": hinge, "name": "Hinge", T.DEVELOPER_FIELD: True}])
        planned, _, record = T.for_target(check, phone, gate=built)
        self.assertEqual((planned.bundle_id, planned.reset, record), (hinge, check.reset, PHONE))
        self.assertEqual(self.devicectl.calls, [])
        ran, _, _ = built.admit(planned, PHONE)
        self.assertEqual(ran.reset, "none")
        store = self.gate([{"bundleIdentifier": hinge, "name": "Hinge", T.DEVELOPER_FIELD: False}])
        with self.assertRaises(T.Refused) as caught:
            store.admit(check, PHONE)
        self.assertEqual(caught.exception.klass, "not_developer_build")
        self.assertEqual(caught.exception.message,
                         "Mobster tests your own apps on your iPhone. Hinge is from the App Store.")

    def test_an_app_store_app_on_a_real_iphone_couldnt_run_before_any_tap(self):
        gate = self.gate([{"bundleIdentifier": BUNDLE, "name": "Firefox", T.DEVELOPER_FIELD: False}])
        with self.assertRaises(T.Refused) as caught:
            gate.admit(self.check(), PHONE)
        self.assertEqual(caught.exception.message,
                         "Mobster tests your own apps on your iPhone. Firefox is from the App Store.")
        self.assertEqual(caught.exception.klass, "not_developer_build")
        self.assertEqual(self.devicectl.calls[0][:6], ["xcrun", "devicectl", "device", "info", "apps", "--device"])
        self.assertEqual(self.devicectl.calls[0][6], PHONE["udid"])

    def test_a_field_devicectl_leaves_out_counts_as_not_a_developer_build(self):
        gate = self.gate([{"bundleIdentifier": BUNDLE, "name": "Firefox"}])
        with self.assertRaisesRegex(T.Refused, "from the App Store"):
            gate.admit(self.check(), PHONE)

    def test_a_developer_build_runs_with_its_data_kept(self):
        gate = self.gate([{"bundleIdentifier": BUNDLE, "name": "Firefox", T.DEVELOPER_FIELD: True}])
        ran, notes, record = gate.admit(self.check(reset="reinstall"), PHONE)
        self.assertEqual((ran.bundle_id, ran.app_path, ran.reset, ran.device), (BUNDLE, None, "none", None))
        self.assertIn("never clears an app's data", notes[0])
        gate.admit(self.check(), PHONE)
        self.assertEqual(len(self.devicectl.calls), 1)  # one app list per phone per suite

    def test_apps_that_are_part_of_ios_and_apps_not_on_the_phone(self):
        gate = self.gate([])
        with self.assertRaisesRegex(T.Refused, "Settings is part of iOS"):
            gate.admit(self.check(bundle_id="com.apple.Preferences"), PHONE)
        with self.assertRaisesRegex(T.Refused, "isn't on “Studio iPhone”") as caught:
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
        self.assertIn("Installed Firefox-iphoneos.app", notes[0])
        self.assertEqual(self.devicectl.calls, [])

    def test_a_simulator_build_falls_back_to_the_build_on_the_phone(self):
        gate = self.gate([{"bundleIdentifier": BUNDLE, T.DEVELOPER_FIELD: True}])
        app = Plist.app(self.folder, "iphonesimulator")
        ran, notes, _ = gate.admit(check_from_dict({"app_path": app}), PHONE)
        self.assertEqual(self.installs, [])
        self.assertIn("is a simulator build", notes[0])
        self.assertEqual(T.device_build(app), False)
        self.assertEqual(T.device_build("/x/Firefox.ipa"), True)
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

    def test_an_ipa_of_a_dating_app_installs_like_any_build_and_must_be_the_app_the_check_names(self):
        tinder = "com.cardify.tinder"
        ipa = Plist.ipa(self.folder, bundle=tinder, name="Tinder")
        self.assertEqual(T.bundle_of(ipa), tinder)
        installs = []

        def install(record, path):
            installs.append(path)
            return {"bundle_id": tinder}
        ran, notes, _ = self.gate([], installer=install).admit(check_from_dict({"app_path": ipa}), PHONE)
        self.assertEqual((ran.bundle_id, installs), (tinder, [ipa]))
        self.assertIn("Installed Tinder.ipa", notes[0])
        with self.assertRaises(T.Refused) as caught:
            self.gate([]).admit(self.check(app_path=ipa), PHONE)  # app.bundle names another app
        self.assertEqual(caught.exception.klass, "usage")
        self.assertEqual(self.installs, [])

    def test_malformed_app_plist_refuses_before_device_listing_or_install(self):
        app = Path(self.folder) / "Broken.app"
        app.mkdir()
        (app / "Info.plist").write_bytes(b"<plist><dict><key>CFBundleIdentifier</key>")
        gate = self.gate([])
        with self.assertRaisesRegex(T.Refused, "can't read the bundle ID") as caught:
            gate.preflight(check_from_dict({"app_path": str(app)}), PHONE)
        self.assertEqual(caught.exception.klass, "usage")
        self.assertEqual(self.devicectl.calls, [])
        self.assertEqual(self.installs, [])

    def test_app_state_fixture_refuses_phone_before_listing_or_install(self):
        gate = self.gate([])
        check = replace(self.check(app_path=Plist.ipa(self.folder, name="FixtureApp")), business=object())
        with self.assertRaisesRegex(T.Refused, "only on managed simulators") as caught:
            gate.admit(check, PHONE)
        self.assertEqual(caught.exception.klass, "business")
        self.assertEqual(self.devicectl.calls, [])
        self.assertEqual(self.installs, [])

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
            self.gate([], installer=install).admit(
                self.check(app_path=Plist.ipa(self.folder, name="Opaque")), PHONE)
        ran, _, _ = self.gate([]).admit(self.check(app_path=Plist.ipa(self.folder, name="Firefox2")), PHONE)
        self.assertEqual((ran.bundle_id, ran.app_path), (BUNDLE, None))

    def test_without_a_gate_a_real_device_is_refused(self):
        with self.assertRaises(T.Refused):
            T.for_target(self.check(), T.Target("device:x", "device", record=PHONE))


EXECUTE_BUNDLE = "org.mozilla.ios.Firefox"


def validate_junit(data):
    root = ET.fromstring(data)
    assert root.tag == "testsuites", root.tag
    numeric = ("tests", "failures", "errors", "skipped")
    for name in numeric:
        int(root.get(name))
    float(root.get("time"))
    totals = dict.fromkeys(numeric, 0)
    for suite in root:
        assert suite.tag == "testsuite", suite.tag
        for name in ("name", "timestamp"):
            assert suite.get(name), name
        float(suite.get("time"))
        tags = [child.tag for child in suite]
        assert set(tags) <= {"properties", "testcase", "system-out", "system-err"}, tags
        assert tags.count("properties") <= 1 and (not tags.count("properties") or tags[0] == "properties"), tags
        counts = dict.fromkeys(numeric, 0)
        for case in suite.findall("testcase"):
            counts["tests"] += 1
            for name in ("classname", "name", "time"):
                assert case.get(name) is not None, name
            float(case.get("time"))
            kids = [child.tag for child in case]
            outcome = [tag for tag in kids if tag in ("failure", "error", "skipped")]
            assert len(outcome) <= 1, kids
            order = {"properties": 0, "failure": 1, "error": 1, "skipped": 1, "system-out": 2, "system-err": 3}
            assert all(tag in order for tag in kids), kids
            assert [order[tag] for tag in kids] == sorted(order[tag] for tag in kids), kids
            for prop in case.iter("property"):
                assert prop.get("name") and prop.get("value") is not None
            for tag in outcome:
                if tag == "failure":
                    counts["failures"] += 1
                    assert case.find("failure").get("type") and case.find("failure").get("message") is not None
                elif tag == "error":
                    counts["errors"] += 1
                else:
                    counts["skipped"] += 1
        for name in numeric:
            assert int(suite.get(name)) == counts[name], (name, suite.get(name), counts[name])
            totals[name] += counts[name]
    for name in numeric:
        assert int(root.get(name)) == totals[name], (name, root.get(name), totals[name])
    return root


class EndToEndTests(RunnerCase):
    def setUp(self):
        super().setUp()
        checks = self.root / ".mobster" / "checks"
        checks.mkdir(parents=True)
        (checks / "paywall.yaml").write_text(
            "version: 1\nname: The paywall shows <three> plans\napp: {bundle: org.mozilla.ios.Firefox}\n"
            "launch: {url: 'firefox://paywall'}\ntags: [smoke]\nexpect:\n  - text: Choose your plan\n")
        (checks / "restore.yaml").write_text(
            "version: 1\nname: Restore shows an error\napp: {bundle: org.mozilla.ios.Firefox}\n"
            "expect:\n  - text: Restore failed\n")
        (checks / "onboarding.yaml").write_text(
            "version: 1\nname: Onboarding reaches the paywall\napp: {bundle: org.mozilla.ios.Firefox}\n"
            "steps: [Complete onboarding.]\nexpect:\n  - text: Choose your plan\n")
        self.manager = Manager()
        self.driver = Driver([PLANS])
        self.watch(self.driver, self.manager)
        patch = mock.patch.object(runner, "build_driver", lambda target, m, run_dir, mode_: self.driver)
        patch.start()
        self.addCleanup(patch.stop)

    def run_suite(self, **options):
        planned = api.plan(api.Options(**options), cwd=self.root)
        attempts = E.Attempts(runs_dir=self.root / ".mobster" / "runs", keyless=options.get("keyless", False),
                              simulators=self.manager, assert_timeout=1)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "",
                                          "MOBSTER_SMART_MODEL": ""}):
            return api.execute(planned, attempts=attempts, cwd=self.root)

    def test_a_suite_on_real_verify_runs_writes_junit_html_and_json_with_the_exit_code(self):
        doc, paths = self.run_suite(retries=0, junit=str(self.root / "build" / "junit.xml"),
                                    html=str(self.root / "build" / "report"))
        results = {check["name"]: check["results"][0] for check in doc["checks"]}
        self.assertEqual(results["The paywall shows <three> plans"]["status"], "passed")
        self.assertEqual(results["Restore shows an error"]["status"], "failed")
        onboarding = results["Onboarding reaches the paywall"]
        self.assertEqual((onboarding["status"], onboarding["reason"]["class"]), ("couldnt_run", "usage"))
        self.assertIn("need Smart", onboarding["reason"]["message"])
        self.assertEqual(doc["exitCode"], 1)
        self.assertEqual(doc["schema"], "mobster.test/1")
        self.assertEqual([device["name"] for device in doc["devices"]], ["Each check's simulator",
                                                                           "iPhone 17 Pro · iOS 26.4"])
        folder = paths["folder"]
        self.assertEqual(folder.parent, self.root / ".mobster" / "test-results")
        passed = results["The paywall shows <three> plans"]
        self.assertRegex(passed["report"], r"^runs/\d{8}-\d{6}-[0-9a-f]{4}\.html$")
        self.assertTrue((folder / passed["report"]).is_file())
        self.assertTrue(passed["frame"].endswith("-verdict-ax.jpg"))
        on_disk = json.loads((folder / "results.json").read_text())
        self.assertEqual(on_disk["summary"], doc["summary"])
        self.assertEqual(set(on_disk["checks"][0]["results"][0]) >= {"device", "status", "attempts", "flaky",
                                                                       "report", "durationMs", "healed"}, True)
        data = (folder / "junit.xml").read_bytes()
        self.assertEqual(data, (self.root / "build" / "junit.xml").read_bytes())
        root = validate_junit(data)
        self.assertEqual((root.get("tests"), root.get("failures"), root.get("skipped")), ("3", "2", "0"))
        case = next(c for c in root.iter("testcase") if c.get("name") == "The paywall shows <three> plans")
        report = next(p.get("value") for p in case.iter("property") if p.get("name") == "report")
        self.assertTrue((self.root / report).is_file())
        self.assertEqual(case.get("classname"), "checks.paywall")
        failure = next(c for c in root.iter("testcase") if c.get("name") == "Restore shows an error").find("failure")
        self.assertEqual(failure.get("type"), "assertion")
        page = (folder / "index.html").read_text()
        self.assertIn("The paywall shows &lt;three&gt; plans", page)
        self.assertNotIn("<three>", page)
        self.assertIn("default-src 'none'", page)
        self.assertNotRegex(page, r"src=\"https?:|url\(\s*['\"]?https?:|@import|<link\b")
        self.assertEqual(re.findall(r'href="((?:https?:|/)[^"]*)"', page), [links.MADE_WITH_REPORT])
        self.assertIn('src="data:image/jpeg;base64,', page)
        self.assertTrue((self.root / "build" / "report" / "index.html").is_file())
        self.assertTrue((self.root / "build" / "report" / passed["report"]).is_file())
        self.assertEqual((self.root / ".mobster" / ".gitignore").read_text(), runs.GITIGNORE)

    def test_tags_and_keyless(self):
        doc, _ = self.run_suite(tags=("smoke",), keyless=True)
        self.assertEqual([check["name"] for check in doc["checks"]], ["The paywall shows <three> plans"])
        self.assertEqual(doc["exitCode"], 0)
        with self.assertRaisesRegex(api.UsageError, "No check has the tag nightly"):
            api.plan(api.Options(tags=("nightly",)), cwd=self.root)
        doc, _ = self.run_suite(paths=(".mobster/checks/onboarding.yaml",), keyless=True)
        reason = doc["checks"][0]["results"][0]["reason"]
        self.assertEqual(reason["class"], "usage")
        self.assertIn("--keyless", reason["message"])
        self.assertNotIn("acquire", self.manager.names()[-1:])


class AttemptRuleTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name) / ".mobster" / "runs"

    def check(self, **data):
        return check_from_dict({"bundle_id": EXECUTE_BUNDLE, "expect": [{"text": "Hi"}], **data})

    def test_a_stopped_suite_and_steps_without_a_key_never_prepare(self):
        manager = Manager()
        attempts = E.Attempts(runs_dir=self.runs, simulators=manager)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "MOBSTER_SMART_MODEL": ""}):
            result = attempts(self.check(steps=["Open Settings"]), None, label="x")
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "usage"))
        attempts.stop.set()
        result = attempts(self.check(), None, label="x")
        self.assertEqual(result["reason"]["class"], "stopped")
        self.assertEqual(manager.names(), [])

    def test_a_real_iphone_gets_the_approver_that_refuses_every_commit(self):
        record = {"id": "U-1", "udid": "U-1", "kind": "usb", "name": "Studio iPhone",
                  "wdaUrl": "http://127.0.0.1:8100"}
        manager = E.Attempts(runs_dir=self.runs).manager_for(record)
        self.assertIs(smart.check_approver(manager), smart.deny_commits)
        sim = {"id": "U-1", "kind": "simulator", "udid": "U-1", "name": "Mobster · iPhone 17 Pro"}
        pinned = E.Attempts(runs_dir=self.runs, simulators=Manager()).manager_for(sim)
        self.assertEqual(pinned.udid, "U-1")
        self.assertIs(smart.check_approver(pinned), smart.approve_on_simulator)

    def test_parallel_raises_the_simulator_limit_never_lowers_it(self):
        with mock.patch.dict(os.environ, {"MOBSTER_MAX_SIMS": "2", "MOBSTER_DATA_DIR": str(self.runs.parent)}):
            self.assertEqual(E.suite_simulators(4, None).max_sims, 4)
            self.assertEqual(E.suite_simulators(1, None).max_sims, 2)

    def test_the_recorder_starts_with_the_lease_and_stops_before_it_is_released(self):
        events = []

        class Process:
            def __init__(self, argv, **kwargs):
                events.append(("start", argv, kwargs.get("start_new_session")))
                self.alive = True

            def poll(self):
                return None if self.alive else 0

            def send_signal(self, number):
                events.append(("signal", number))
                self.alive = False

            def wait(self, timeout=None):
                return 0

        class Lease:
            target = type("Target", (), {"udid": "UDID-1"})()

            def release(self):
                events.append(("release",))

        class Sims:
            def acquire(self, device=None, runtime=None, **kwargs):
                return Lease()
            other = "kept"
        video = self.runs.parent / "videos" / "a.mp4"
        recording = E.Recording(Sims(), video, popen=Process)
        lease = recording.acquire("iPhone 17 Pro", None)
        self.assertEqual(recording.other, "kept")
        lease.release()
        lease.release()
        self.assertEqual(events, [("start", E.video_argv("UDID-1", video), True), ("signal", signal.SIGINT),
                                  ("release",), ("release",)])
        self.assertEqual(E.video_argv("U", "/v.mp4"), ["xcrun", "simctl", "io", "U", "recordVideo", "--codec=h264",
                                                         "--force", "/v.mp4"])

        def broken(argv, **kwargs):
            raise FileNotFoundError("xcrun")
        lease = E.Recording(Sims(), video, popen=broken).acquire()
        lease.release()  # no recording, and the run goes on

    def test_abort_all_stops_runs_still_preparing(self):
        aborted = threading.Event()

        class Run:
            state = "preparing"

            def abort(self, klass, message, fix=""):
                aborted.set()
        attempts = E.Attempts(runs_dir=self.runs)
        attempts._live.add(Run())
        attempts.abort_all()
        self.assertTrue(attempts.stop.is_set())
        self.assertTrue(aborted.wait(2))

    def test_interrupting_video_start_or_stop_still_releases_the_simulator(self):
        lease = mock.Mock()
        manager = mock.Mock()
        manager.acquire.return_value = lease
        recording = E.Recording(manager, self.runs.parent / "video.mp4",
                                popen=mock.Mock(side_effect=KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            recording.acquire()
        lease.release.assert_called_once_with()

        lease.reset_mock()
        recorded = E.RecordedLease(lease, mock.Mock(spec=E.subprocess.Popen))
        with mock.patch.object(E, "stop_recording", side_effect=KeyboardInterrupt()), \
                self.assertRaises(KeyboardInterrupt):
            recorded.release()
        lease.release.assert_called_once_with()


class DeviceTargetTests(RunnerCase):
    def test_an_app_store_app_on_the_phone_is_couldnt_run_with_no_tap(self):
        checks = self.root / ".mobster" / "checks"
        checks.mkdir(parents=True)
        (checks / "store.yaml").write_text("version: 1\nname: Store app\napp: {bundle: com.example.storeapp}\n"
                                           "expect: [{text: Hi}]\n")
        phone = {"id": "00008140-1", "udid": "00008140-1", "kind": "usb", "name": "Studio iPhone",
                 "wdaUrl": "http://127.0.0.1:8100"}

        def devicectl(argv):
            out = Path(argv[argv.index("--json-output") + 1])
            out.write_text(json.dumps({"result": {"apps": [{"bundleIdentifier": "com.example.storeapp",
                                                            "name": "Store App", T.DEVELOPER_FIELD: False}]}}))
            return 0, ""
        calls = []

        class Phone:
            real_device = True

            def acquire(self, device=None, runtime=None, *, cancelled=None):
                calls.append("acquire")
                target = SimpleNamespace(name="Studio iPhone", udid=phone["udid"], device_type="iPhone",
                                         runtime="iOS 26.4", wda_url=phone["wdaUrl"])

                class Lease:
                    def __init__(self):
                        self.target = target

                    def release(self):
                        calls.append("release")

                return Lease()

        planned = api.plan(api.Options(devices=("Studio iPhone",)), cwd=self.root,
                           named_device=lambda name: phone if name == "Studio iPhone" else None)
        attempts = E.Attempts(runs_dir=self.root / ".mobster" / "runs", devices=lambda record: Phone())
        doc, _ = api.execute(planned, attempts=attempts, cwd=self.root,
                             gate=T.DeviceGate(runner=devicectl, allowed=lambda: True))
        result = doc["checks"][0]["results"][0]
        self.assertEqual(calls, ["acquire", "release"])
        self.assertEqual((result["status"], result["attempts"]), ("couldnt_run", 1))
        self.assertEqual(result["reason"]["message"],
                         "Mobster tests your own apps on your iPhone. Store App is from the App Store.")
        self.assertEqual(result["device"], "Studio iPhone")


class AuditPlanTests(RunnerCase):
    def setUp(self):
        super().setUp()
        checks = self.root / ".mobster" / "checks"
        checks.mkdir(parents=True)
        (checks / "a.yaml").write_text("version: 1\nname: A\napp: {bundle: org.mozilla.ios.Firefox}\n"
                                       "expect: [{text: Hi}]\n")
        (checks / "b.yaml").write_text("version: 1\nname: B\napp: {bundle: org.mozilla.ios.Firefox}\n"
                                       "expect: [{text: Hi}]\naudit: [a11y]\n")

    def test_audit_a11y_is_added_to_every_check_once(self):
        planned = api.plan(api.Options(audit=("a11y",)), cwd=self.root)
        self.assertEqual([entry.check.audit for entry in planned.entries], [("a11y",), ("a11y",)])
        self.assertEqual(planned.options.public()["audit"], ["a11y"])

    def test_without_the_flag_checks_keep_their_own_audit(self):
        planned = api.plan(api.Options(), cwd=self.root)
        self.assertEqual([entry.check.audit for entry in planned.entries], [(), ("a11y",)])

    def test_an_audit_mobster_does_not_have_is_refused_by_the_command(self):
        from mobile_agent.cli_parser import build_parser
        with self.assertRaises(SystemExit) as stopped, contextlib.redirect_stderr(io.StringIO()) as err:
            build_parser().parse_args(["test", "--audit", "contrast"])
        self.assertEqual(stopped.exception.code, 3)
        self.assertIn("invalid choice: 'contrast'", err.getvalue())
        self.assertRegex(err.getvalue(), r"choose from '?a11y'?\)")


class SpreadPlanTests(RunnerCase):
    PHONES = {"Studio iPhone": {"id": "00008140-1", "udid": "00008140-1", "kind": "usb", "name": "Studio iPhone"},
              "Work iPhone": {"id": "00008140-2", "udid": "00008140-2", "kind": "usb", "name": "Work iPhone"}}

    def setUp(self):
        super().setUp()
        checks = self.root / ".mobster" / "checks"
        checks.mkdir(parents=True)
        (checks / "a.yaml").write_text("version: 1\nname: A\napp: {bundle: org.mozilla.ios.Firefox}\n"
                                       "expect: [{text: Hi}]\n")

    def plan(self, **options):
        return api.plan(api.Options(**options), cwd=self.root, named_device=self.PHONES.get)

    def test_spread_plans_the_named_phones_and_records_the_option(self):
        planned = self.plan(devices=("Studio iPhone", "Work iPhone"), spread=True)
        self.assertEqual([target.label for target in planned.targets], ["Studio iPhone", "Work iPhone"])
        self.assertTrue(planned.options.public()["spread"])

    def test_spread_with_one_device_is_refused_before_anything_runs(self):
        with self.assertRaisesRegex(api.UsageError, r"--spread shares the checks between two or more devices, and "
                                                   r"this run has 1\. Name more with --device or --sim"):
            self.plan(devices=("Studio iPhone",), spread=True)

    def test_an_unknown_device_is_refused_with_what_to_do(self):
        with self.assertRaisesRegex(api.UsageError,
                                    r"No device is named .Desk iPhone.\. `mobster devices` lists them"):
            self.plan(devices=("Studio iPhone", "Desk iPhone"), spread=True)


def run(status="passed", flaky=False, ms=1000):
    return {"suite": "s", "at": 1, "status": status, "flaky": flaky, "ms": ms, "attempts": 1}


def doc(suite, status="passed", flaky=False, ms=1000, name="Feed", file=".mobster/checks/feed.yaml", quarantined=False):
    return {"suite": suite, "startedAt": 1, "checks": [{"name": name, "file": file, "quarantined": quarantined,
            "results": [{"device": "iPhone 17 Pro", "deviceKey": "sim:a", "status": status, "flaky": flaky,
                         "durationMs": ms, "attempts": 2 if flaky else 1}]}]}


class Classify(unittest.TestCase):
    def state(self, *runs):
        return history.stats(list(runs))["state"]

    def test_alternating_results_are_flaky(self):
        runs = [run("passed" if n % 2 else "failed") for n in range(8)] + [run()]
        found = history.stats(runs)
        self.assertEqual(found["state"], "flaky")
        self.assertGreaterEqual(found["flakeScore"], history.FLAKY_AT)

    def test_passing_after_a_retry_counts_even_without_alternation(self):
        runs = [run() for _ in range(8)] + [run(flaky=True), run(flaky=True)]
        self.assertEqual(self.state(*runs), "flaky")

    def test_always_failing_is_deterministic_not_flaky(self):
        found = history.stats([run("failed") for _ in range(10)])
        self.assertEqual((found["state"], found["flakeScore"], found["passRate"]), ("failing", 0.0, 0.0))

    def test_a_check_fixed_once_is_stable_and_a_fresh_regression_is_failing(self):
        self.assertEqual(self.state(*[run("failed")] * 10, *[run()] * 10), "stable")
        self.assertEqual(self.state(*[run()] * 9, run("failed")), "failing")

    def test_always_passing_is_stable_and_too_few_runs_are_new(self):
        self.assertEqual(self.state(*[run()] * 20), "stable")
        self.assertEqual(self.state(run(), run("failed"), run(), run("failed")), "new")

    def test_duration_trend(self):
        self.assertEqual(history.stats([run(ms=1000)] * 4 + [run(ms=2000)] * 4)["trend"], "slower")
        self.assertEqual(history.stats([run(ms=2000)] * 4 + [run(ms=1000)] * 4)["trend"], "faster")
        self.assertEqual(history.stats([run(ms=1000)] * 8)["trend"], "steady")
        self.assertIsNone(history.stats([run()] * 3)["trend"])


class Store(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_keeps_the_last_twenty_and_puts_history_in_the_document(self):
        for n in range(history.KEEP + 5):
            found = history.record(self.root, doc(f"s{n}", "failed" if n < 5 else "passed"))
        entry, ok = history.load(self.root)
        runs = next(iter(entry.values()))["runs"]
        self.assertTrue(ok)
        self.assertEqual((len(runs), runs[0]["suite"]), (history.KEEP, "s5"))
        cell = found["checks"][0]["results"][0]["history"]
        self.assertEqual((cell["runs"], cell["state"]), (history.KEEP, "stable"))
        self.assertEqual(found["history"]["suggestQuarantine"], [])

    def test_bounds_the_number_of_checks_dropping_the_longest_unrun(self):
        for n in range(history.MAX_CHECKS + 3):
            suite = doc(f"s{n}", name=f"c{n}", file=f"c{n}.yaml")
            suite["startedAt"] = n
            history.record(self.root, suite)
        entries, _ = history.load(self.root)
        self.assertEqual(len(entries), history.MAX_CHECKS)
        self.assertNotIn("c0.yaml|sim:a", entries)

    def test_a_result_that_couldnt_run_is_not_kept(self):
        history.record(self.root, doc("s1", "couldnt_run"))
        self.assertEqual(history.load(self.root), ({}, True))

    def test_flaky_check_is_suggested_for_quarantine_unless_it_already_is(self):
        for n in range(8):
            history.record(self.root, doc(f"s{n}", "passed" if n % 2 else "failed"))
        found = history.record(self.root, doc("s9"))
        self.assertEqual([item["file"]
                          for item in found["history"]["suggestQuarantine"]], [".mobster/checks/feed.yaml"])
        found = history.record(self.root, doc("s10", quarantined=True))
        self.assertEqual(found["history"]["suggestQuarantine"], [])

    def test_a_corrupt_file_is_set_aside_and_the_history_starts_again(self):
        path = history.path_of(self.root)
        for data in (b"{not json", json.dumps({"schema": "other", "checks": {}}).encode(), b"[1]", b"", b"\xff"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.assertEqual(history.load(self.root), ({}, False))
            found = history.record(self.root, doc("s1"))
            self.assertEqual(found["checks"][0]["results"][0]["history"]["runs"], 1)
            self.assertEqual(path.with_name(history.FILE + ".corrupt").read_bytes(), data)
            self.assertTrue(history.load(self.root)[1])

    def test_bad_entries_inside_a_readable_file_are_dropped(self):
        path = history.path_of(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        good = run()
        path.write_text(json.dumps({"schema": history.SCHEMA, "checks": {
            "a|k": {"name": "A", "file": "a.yaml", "device": "d", "runs": [good, "x", {"status": "bogus"}, {"ms": -5}]},
            "b|k": "nope", "c|k": {"runs": "nope"}}}))
        entries, ok = history.load(self.root)
        self.assertTrue(ok)
        self.assertEqual(list(entries), ["a|k"])
        self.assertEqual(len(entries["a|k"]["runs"]), 1)

    def test_recording_the_same_suite_again_does_not_count_another_run(self):
        history.record(self.root, doc("s1", "failed"))
        history.record(self.root, doc("s2"))
        history.record(self.root, doc("s1", "failed"))
        rows = history.report(self.root)["checks"]
        self.assertEqual((rows[0]["runs"], rows[0]["lastStatus"]), (2, "passed"))

    def test_suites_from_before_the_history_file_count_and_are_never_written_back(self):
        # UX-16: a project whose five suites ran before history.json existed said "Not run yet" over "1 of 1 passed".
        base = history.path_of(self.root).parent

        def suite(name, status, at, write=True):
            (base / name).mkdir(parents=True)
            found = doc(name, status)
            found["startedAt"] = at
            if write:
                (base / name / "results.json").write_text(json.dumps(found))
            return found

        for n, status in enumerate(["failed", "passed", "passed", "passed", "passed"]):
            suite(f"20261017-10{n}000-ab1{n}", status, 1_000 + n)
        suite("20261017-110000-ffff", "failed", 1_100, write=False)  # no results.json: skipped
        (base / "notes").mkdir()
        (base / "20261017-120000-eeee").mkdir()
        (base / "20261017-120000-eeee" / "results.json").write_text("{not json")
        rows = history.report(self.root)["checks"]
        self.assertEqual([(row["file"], row["runs"], row["lastStatus"]) for row in rows],
                         [(".mobster/checks/feed.yaml", 5, "passed")])
        self.assertEqual(rows[0]["outcomes"], ["failed"] + ["passed"] * 4)
        self.assertFalse(history.path_of(self.root).exists())
        # A suite recorded in the file and also in its folder counts once, in the order the suites ran.
        later = suite("20261018-090000-cccc", "failed", 2_000)
        history.record(self.root, later)
        rows = history.report(self.root)["checks"]
        self.assertEqual((rows[0]["runs"], rows[0]["outcomes"][0], rows[0]["lastStatus"]), (6, "failed", "failed"))
        self.assertEqual(len(history.load(self.root)[0][".mobster/checks/feed.yaml|sim:a"]["runs"]), 1)

    def test_a_malformed_result_is_skipped_and_never_hides_the_rest_of_the_history(self):
        base = history.path_of(self.root).parent
        for n, bad in enumerate(["Expected X; found Y.", ["a"], 3]):
            found = doc(f"20261017-10{n}000-ab1{n}", "passed")
            found["startedAt"] = 1_000 + n
            found["checks"][0]["results"][0]["reason"] = bad
            found["checks"][0]["results"].append("not a result")
            found["checks"].append("not a check")
            (base / found["suite"]).mkdir(parents=True)
            (base / found["suite"] / "results.json").write_text(json.dumps(found))
        rows = history.report(self.root)["checks"]
        self.assertEqual([(row["runs"], row["lastStatus"], row["unmeasured"]) for row in rows], [(3, "passed", 0)])
        # A well-formed unmeasured budget still counts as unmeasured.
        found = doc("s9")
        found["checks"][0]["results"][0]["reason"] = {"class": "budget_unmeasured"}
        history.record(self.root, found)
        self.assertEqual(history.report(self.root)["checks"][0]["unmeasured"], 1)

    def test_another_process_waits_for_the_whole_history_update(self):
        from mobile_agent.env_file import writer
        script = ("import sys; from mobile_agent.tests.test_testing import doc; "
                  "from mobile_agent.testkit import history; print('ready', flush=True); "
                  "history.record(sys.argv[1], doc('child')); print('done', flush=True)")
        child = None
        try:
            with writer(history.path_of(self.root)):
                child = subprocess.Popen([sys.executable, "-c", script, str(self.root)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertTrue(select.select([child.stdout], [], [], 5)[0])
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.wait(timeout=.3)
            out, err = child.communicate(timeout=5)
            self.assertEqual((child.returncode, out.strip(), err), (0, "done", ""))
            self.assertEqual(history.report(self.root)["checks"][0]["runs"], 1)
        finally:
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)

    def test_record_never_raises(self):
        self.assertEqual(history.record(self.root, {"checks": "nope"})["checks"], "nope")


class Surfaces(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "App"
        checks = self.root / ".mobster" / "checks"
        checks.mkdir(parents=True)
        (checks / "feed.yaml").write_text("name: Feed\nbundle_id: org.mozilla.ios.Firefox\nexpect:\n  - text: Hi\n")
        (checks / "solid.yaml").write_text("name: Solid\nbundle_id: org.mozilla.ios.Firefox\nexpect:\n  - text: Hi\n")
        for n in range(8):
            history.record(self.root, doc(f"s{n}", "passed" if n % 2 else "failed", file=".mobster/checks/feed.yaml"))
        history.record(self.root, doc("s9", file=".mobster/checks/feed.yaml"))

    def test_auto_quarantine_only_when_asked_and_never_a_failing_check(self):
        for n in range(6):
            history.record(self.root, doc(f"f{n}", "failed", name="Solid", file=".mobster/checks/solid.yaml"))
        planned = plan(Options(), cwd=self.root)
        self.assertFalse(any(entry.quarantined is not None for entry in planned.entries))
        planned = plan(Options(auto_quarantine=True), cwd=self.root)
        by_file = {entry.file: entry for entry in planned.entries}
        self.assertIn("flaky", by_file[".mobster/checks/feed.yaml"].quarantined)
        self.assertIsNone(by_file[".mobster/checks/solid.yaml"].quarantined)

    def test_a_failure_on_another_device_prevents_auto_quarantine(self):
        other = doc("other", "failed", file=".mobster/checks/feed.yaml")
        other["checks"][0]["results"][0].update(device="Another iPhone", deviceKey="sim:b")
        history.record(self.root, other)
        planned = plan(Options(auto_quarantine=True), cwd=self.root)
        self.assertIsNone(next(entry for entry in planned.entries if entry.name == "Feed").quarantined)

    def test_pr_receives_the_requested_auto_quarantine_option(self):
        from mobile_agent.cli_parser import build_parser
        args = build_parser().parse_args(["test", "pr", "--auto-quarantine"])
        seen = []
        def observe(pr_options, options, **kwargs):
            seen.append(options.auto_quarantine)
            raise RuntimeError("stop before execution")
        with self.assertRaisesRegex(RuntimeError, "stop before execution"):
            cli.pr(args, [], run=observe, cwd=self.root, out=io.StringIO(), err=io.StringIO())
        self.assertEqual(seen, [True])

    def test_cli_history_prints_flaky_first_and_json(self):
        out = io.StringIO()
        args = SimpleNamespace(json=False)
        self.assertEqual(cli.history(args, [str(self.root)], out=out), 0)
        text = out.getvalue()
        self.assertTrue(text.startswith("flaky"))
        self.assertIn("quarantine", text)
        out = io.StringIO()
        cli.history(SimpleNamespace(json=True), [str(self.root)], out=out)
        self.assertEqual(json.loads(out.getvalue())["checks"][0]["state"], "flaky")

    def test_cli_history_with_none_says_so(self):
        out = io.StringIO()
        empty = self.root.parent / "Empty"
        (empty / ".mobster").mkdir(parents=True)
        cli.history(SimpleNamespace(json=False), [str(empty)], out=out)
        self.assertIn("No test history yet", out.getvalue())

    def test_api_serves_only_projects_mobster_ran_in(self):
        request = SimpleNamespace(query={"project": [str(self.root)]})
        with mock.patch("mobile_agent.testkit.recent.known_roots", return_value=[self.root]):
            body = register.check_history(request).value
            self.assertEqual(body["projects"][0]["checks"][0]["state"], "flaky")
            other = register.check_history(SimpleNamespace(query={"project": ["/etc"]}))
        self.assertEqual(other.status, 404)

    def test_the_html_report_lists_a_flaky_check_with_its_next_step(self):
        from mobile_agent.testkit.reports import render_html
        found = history.record(self.root, doc("s10", file=".mobster/checks/feed.yaml"))
        found.update(options={}, summary={"total": 1}, devices=[])
        page = render_html(found, self.root)
        self.assertIn("Flaky over time", page)
        self.assertIn("quarantine file", page)


PAYWALL_DIFF = '''diff --git a/Firefox/Paywall/PaywallView.swift b/Firefox/Paywall/PaywallView.swift
index 1111111..2222222 100644
--- a/Firefox/Paywall/PaywallView.swift
+++ b/Firefox/Paywall/PaywallView.swift
@@ -12,2 +12,3 @@ struct PaywallView: View {
-            Text("Choose a plan")
+            Text("Choose your plan")
+            PlanRow(plan: annual).accessibilityIdentifier("plan_annual")
-            Button("Restore") { restore() }.accessibilityIdentifier("restore_old")
diff --git a/Firefox/Settings/SettingsView.swift b/Firefox/Settings/SettingsView.swift
new file mode 100644
--- /dev/null
+++ b/Firefox/Settings/SettingsView.swift
@@ -0,0 +1,4 @@
+struct SettingsView: View {
+    var body: some View { Toggle("Daily reminder", isOn: $on).accessibilityIdentifier("reminder_toggle") }
+    // Text("Not shown") stays a comment
+}
diff --git a/FirefoxTests/PaywallTests.swift b/FirefoxTests/PaywallTests.swift
--- a/FirefoxTests/PaywallTests.swift
+++ b/FirefoxTests/PaywallTests.swift
@@ -1 +1 @@
-XCTAssert(app.buttons["Old"].exists)
+XCTAssert(app.staticTexts["Choose your plan"].exists)
diff --git a/Firefox/Localizable.strings b/Firefox/Localizable.strings
--- a/Firefox/Localizable.strings
+++ b/Firefox/Localizable.strings
@@ -3 +3 @@
-"streak" = "Day streak";
+"streak" = "Days in a row";
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
'''


def changes(diff=PAYWALL_DIFF, top="/nonexistent"):
    return P.Changes(base="origin/main", merge_base="a" * 40, head="b" * 40, top=Path(top), files=P.parse_diff(diff))


class DiffTests(unittest.TestCase):
    def test_a_diff_becomes_changed_files_with_their_lines(self):
        files = {change.path: change for change in P.parse_diff(PAYWALL_DIFF)}
        self.assertEqual(set(files), {"Firefox/Paywall/PaywallView.swift", "Firefox/Settings/SettingsView.swift",
                                      "FirefoxTests/PaywallTests.swift", "Firefox/Localizable.strings", "README.md"})
        self.assertEqual(files["Firefox/Settings/SettingsView.swift"].status, "A")
        self.assertIn('            Text("Choose your plan")', files["Firefox/Paywall/PaywallView.swift"].added)

    def test_screens_carry_changed_ids_strings_and_names_and_skip_tests(self):
        screens = {screen.name: screen for screen in P.screens_in(changes())}
        self.assertEqual(set(screens), {"PaywallView", "SettingsView", "Localizable.strings"})
        paywall = screens["PaywallView"]
        self.assertEqual(paywall.slug, "paywall")
        self.assertEqual(paywall.added_ids, ["plan_annual"])
        self.assertEqual(paywall.ids, ["plan_annual", "restore_old"])  # removed ids count: a check may name them
        self.assertEqual(paywall.texts, ["Choose your plan", "Choose a plan", "Restore"])
        self.assertEqual(screens["SettingsView"].added_texts, ["Daily reminder"])  # never the commented-out line
        self.assertTrue(screens["Localizable.strings"].strings_only)
        self.assertEqual(screens["Localizable.strings"].texts, ["Days in a row", "Day streak"])

    def test_the_view_name_comes_from_the_file_as_it_is_now(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Firefox/Paywall/PaywallView.swift"
            path.parent.mkdir(parents=True)
            path.write_text("import SwiftUI\nstruct PlanPickerSheet: View {\n var body: some View { EmptyView() }\n}\n"
                            'let link = "firefox://plan-picker"\n')
            screen = next(s for s in P.screens_in(changes(top=folder)) if s.file.endswith("PaywallView.swift"))
        self.assertEqual((screen.name, screen.slug), ("PlanPickerSheet", "plan-picker"))
        self.assertEqual(screen.links, ["firefox://plan-picker"])

    def test_slugs(self):
        for name, slug in (("PaywallView", "paywall"), ("SettingsViewController", "settings"),
                           ("PlanPickerSheet", "plan-picker"), ("ContentView", ""), ("HomeView", "home"),
                           ("URLImporterScreen", "url-importer"), ("OK", "")):
            with self.subTest(name=name):
                self.assertEqual(P.slug_of(name), slug)


class Entry:
    def __init__(self, path, check):
        self.path, self.file, self.check, self.error = Path(path), Path(path).name, check, None
        self.quarantined, self.keys, self.classname = None, (), ""

    @property
    def name(self):
        return self.check.name

    @property
    def tags(self):
        return self.check.tags


def entry(stem, **data):
    data.setdefault("bundle_id", BUNDLE)
    data.setdefault("name", stem)
    data.setdefault("expect", [{"text": "Unrelated"}])
    tags = tuple(data.pop("tags", ()))
    return Entry(f"/project/.mobster/checks/{stem}.yaml", replace(check_from_dict(data), tags=tags))


class PickTests(unittest.TestCase):
    def setUp(self):
        self.screens = P.screens_in(changes())

    def reasons(self, item):
        return {pick.entry.file: pick.reasons for pick in P.pick([item], self.screens)}.get(item.file, [])

    def test_a_check_that_expects_a_changed_id_or_string_is_picked(self):
        self.assertEqual(self.reasons(entry("plans", expect=[{"count": {"id": "/^plan_/"}, "equals": 3}])),
                         ["expects id plan_annual"])
        self.assertEqual(self.reasons(entry("title", expect=[{"text": "choose your plan"}])),
                         ["expects “Choose your plan”"])
        self.assertEqual(self.reasons(entry("old", expect=[{"absent": {"id": "restore_old"}}])),
                         ["expects id restore_old"])

    def test_a_step_that_mentions_a_changed_string_or_a_tag_that_names_the_screen_picks_it(self):
        self.assertEqual(self.reasons(entry("toggles", steps=["Turn on Daily reminder"])),
                         ["a step mentions “Daily reminder”"])
        self.assertEqual(self.reasons(entry("buy", tags=["paywall"])), ["names PaywallView"])
        self.assertEqual(self.reasons(entry("paywall")), ["names PaywallView"])
        self.assertEqual(self.reasons(entry("copy", expect=[{"text": "Days in a row"}])), ["expects “Days in a row”"])

    def test_a_check_about_something_else_is_left_out(self):
        self.assertEqual(self.reasons(entry("today", tags=["smoke"], expect=[{"text": "Drink water"}])), [])
        self.assertEqual(self.reasons(entry("profile", steps=["Open the profile"], tags=["profile"])), [])

    def test_a_check_file_the_pr_changed_is_always_picked(self):
        with tempfile.TemporaryDirectory() as folder:
            path = check_file(Path(folder) / ".mobster" / "checks", "today")
            diff = ("diff --git a/.mobster/checks/today.yaml b/.mobster/checks/today.yaml\n"
                    "--- a/.mobster/checks/today.yaml\n+++ b/.mobster/checks/today.yaml\n@@ -1 +1 @@\n-a\n+b\n")
            item = Entry(path, check_from_dict({"bundle_id": BUNDLE, "name": "today", "expect": [{"text": "x"}]}))
            picked = P.pick([item], [], changes(diff, top=folder))
        self.assertEqual(picked[0].reasons, ["changed in this PR"])


class DraftTests(unittest.TestCase):
    def test_an_uncovered_screen_gets_a_draft_from_its_new_ids_and_strings(self):
        screens = P.screens_in(changes())
        with tempfile.TemporaryDirectory() as folder:
            covered = P.pick([entry("paywall")], screens)
            drafts = P.draft_checks(screens, covered, app={"bundle": BUNDLE, "path": f"{folder}/build/Firefox.app"},
                                    root=folder)
            self.assertEqual([screen.name for screen, _ in drafts], ["SettingsView"])
            path = drafts[0][1]
            self.assertEqual(path, Path(folder) / ".mobster" / "drafts" / "pr" / "settings.yaml")
            text = path.read_text()
            loaded = yaml.safe_load(text)
        self.assertTrue(text.startswith("# Drafted by `mobster test pr` for Firefox/Settings/SettingsView.swift"))
        self.assertEqual(loaded["app"], {"bundle": BUNDLE, "path": "build/Firefox.app"})
        self.assertEqual(loaded["tags"], ["settings", "draft"])
        self.assertEqual(loaded["steps"], ["Open the settings screen"])
        self.assertEqual(loaded["expect"], [{"visible": {"id": "reminder_toggle"}}, {"text": "Daily reminder"}])
        check_from_dict({k: v for k, v in loaded.items() if k != "version"} | {"version": 1})  # a valid check

    def test_a_draft_opens_its_screen_by_deep_link_when_the_file_has_one(self):
        screen = P.Screen(name="PaywallView", file="P.swift", slug="paywall", added_ids=["plan_annual"],
                          links=["firefox://today", "firefox://paywall"])
        data = P.draft_data(screen, {"bundle": BUNDLE})
        self.assertEqual(data["launch"], {"url": "firefox://paywall"})
        self.assertNotIn("steps", data)

    def test_no_app_no_draft_and_at_most_three(self):
        screens = [P.Screen(name=f"Screen{n}View", file=f"{n}.swift", slug=f"screen{n}", added_ids=[f"id{n}"])
                   for n in range(5)]
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(P.draft_checks(screens, [], app=None, root=folder), [])
            self.assertEqual(len(P.draft_checks(screens, [], app={"bundle": BUNDLE}, root=folder)), P.MAX_DRAFTS)


def git(folder, *argv):
    subprocess.run(["git", *argv], cwd=folder, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@example.com", "HOME": folder, "PATH": "/usr/bin:/bin"})


class Repo(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        git(self.root, "init", "-q", "-b", "main")
        view = self.root / "Firefox" / "PaywallView.swift"
        view.parent.mkdir()
        view.write_text('struct PaywallView: View {\n  var body: some View { Text("Choose a plan") }\n}\n')
        self.app = self.root / "build" / "Firefox.app"
        self.app.mkdir(parents=True)
        (self.app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": BUNDLE}))
        check_file(self.root / ".mobster" / "checks", "paywall",
                   app={"bundle": BUNDLE, "path": "build/Firefox.app"}, expect=[{"text": "Choose your plan"}])
        check_file(self.root / ".mobster" / "checks", "today", tags=["smoke"],
                   app={"bundle": BUNDLE, "path": "build/Firefox.app"}, expect=[{"text": "Drink water"}])
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "start")
        git(self.root, "checkout", "-qb", "feature")
        view.write_text('struct PaywallView: View {\n  var body: some View { Text("Choose your plan") }\n}\n')
        settings = self.root / "Firefox" / "SettingsView.swift"
        settings.write_text('struct SettingsView: View {\n  var body: some View { Toggle("Daily reminder", isOn: $x)'
                            '.accessibilityIdentifier("reminder_toggle") }\n}\n')
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "change")


class ReadChangesTests(Repo):
    def test_the_branch_is_diffed_against_where_it_left_main(self):
        found = P.read_changes(self.root, env={})
        self.assertEqual(found.base, "main")
        self.assertEqual(sorted(change.path for change in found.files),
                         ["Firefox/PaywallView.swift", "Firefox/SettingsView.swift"])
        self.assertFalse(found.uncommitted)

    def test_uncommitted_and_new_files_count_an_agent_tests_what_it_just_wrote(self):
        (self.root / "Firefox" / "OnboardingView.swift").write_text('Text("Welcome aboard")\n')
        found = P.read_changes(self.root, env={})
        self.assertIn("Firefox/OnboardingView.swift", [change.path for change in found.files])
        self.assertTrue(found.uncommitted)

    def test_github_base_ref_comes_first_and_an_unknown_base_is_a_usage_error(self):
        self.assertEqual(P.default_base(cwd=self.root, env={"GITHUB_BASE_REF": "main"}), "main")
        with self.assertRaisesRegex(P.PrError, "couldn't find where this branch left nope"):
            P.read_changes(self.root, base="nope", env={})
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(P.PrError, "isn't in a git repository"):
            P.read_changes(folder, env={})


def jpeg(path, color=(240, 240, 240)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 130), color).save(path, "JPEG")


class FakeAttempts:
    def __init__(self, root, verdicts):
        self.root, self.verdicts, self.calls = Path(root), verdicts, []
        self.lock = threading.Lock()

    def __call__(self, check, record, *, label, video_name):
        with self.lock:
            self.calls.append(check)
            n = len(self.calls)
        verdict = self.verdicts.get(check.name, "passed")
        run_dir = self.root / ".mobster" / "runs" / f"run{n}"
        frames = ["frames/01-launch.jpg", "frames/02-step.jpg", "frames/03-verdict-ax.jpg"]
        for frame in frames:
            jpeg(run_dir / frame)
        ok = verdict == "passed"
        assertions = [{"index": 0, "text": "text “Choose your plan”", "ok": ok,
                       "observed": "found in text \"Choose your plan\"" if ok else
                       'not on screen; closest: "Choose a plan"'}]
        result = {"run_id": f"{n:012x}", "verdict": verdict, "run_dir": str(run_dir), "frames": frames,
                  "summary": "1 of 1 expectations held" if ok else "text failed",
                  "reason": None if ok else {"class": "assertion", "message": "text “Choose your plan” failed",
                                              "fix": None},
                  "steps": [{"index": 1, "op": "TAP", "text": "Tap Upgrade", "frame": "frames/02-step.jpg"}],
                  "assertions": assertions, "seconds": 1.0, "report": str(run_dir / "report.html"),
                  "device": {"type": "iPhone 17 Pro", "runtime": "iOS 26.4", "udid": "U"}}
        (run_dir / "result.json").write_text(json.dumps(result))
        (run_dir / "report.html").write_text("<html></html>")
        return result

    def abort_all(self):
        pass


class LeasedPhoneAttempts(FakeAttempts):
    """The PR's fake result still admits an app under a real WDA lease boundary."""

    lease_active = False

    def bind_gate(self, gate):
        self.gate = gate

    def __call__(self, check, record, *, label, video_name):
        from mobile_agent.device_targets import WdaDevice

        class Lease:
            def __init__(lease_self):
                self.lease_active = True

            def close(lease_self):
                self.lease_active = False

        class ReadyWda(WdaDevice):
            def _request(self, method, path, body=None, timeout=30):
                return {"value": {"ready": True}}

        manager = ReadyWda(record, lease=lambda url: Lease())
        held = manager.acquire()
        try:
            admitted, _, _ = self.gate.admit(check, record)
            return super().__call__(admitted, record, label=label, video_name=video_name)
        finally:
            held.release()


def fake_build(calls):
    def runner(argv, cwd, log=None, timeout=None):
        calls.append(argv)
        if "-list" in argv:
            return 0, json.dumps({"project": {"schemes": ["Firefox"]}})
        sdk = "iphoneos" if "generic/platform=iOS" in argv else "iphonesimulator"
        app = Path(cwd) / ".mobster" / "build" / "Build" / "Products" / f"Debug-{sdk}" / "Firefox.app"
        app.mkdir(parents=True, exist_ok=True)
        (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": BUNDLE}))
        return 0, "** BUILD SUCCEEDED **"
    return runner


class TestPrEndToEndTests(Repo):
    def run_pr(self, verdicts=None, pr_options=None, options=None, builds=None, attempts=None, **kwargs):
        (self.root / "Firefox.xcodeproj").mkdir(exist_ok=True)
        self.attempts = attempts or FakeAttempts(self.root, verdicts or {})
        self.builds = [] if builds is None else builds
        return prrun.test_pr(pr_options or prrun.PrOptions(), options or Options(keyless=True), root=self.root,
                             attempts=self.attempts, build_runner=fake_build(self.builds), env={}, **kwargs)

    def cli_pr(self, *argv):
        """`mobster test pr --keyless ARGV` from the command line: (exit code, stderr)."""
        from mobile_agent.cli_parser import build_parser
        args = build_parser().parse_args(["test", "pr", "--keyless", *argv])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.pr(args, [], run=lambda pr_options, options, *, root, progress: self.run_pr(
                pr_options=pr_options, options=options, progress=progress), out=io.StringIO(), err=err,
                cwd=self.root)
        return code, err.getvalue()

    def test_spread_runs_each_picked_check_once_between_the_simulators_named(self):
        sims = ("--sim", "iPhone 17 Pro@iOS 26.4", "--sim", "iPhone Air@iOS 26.4", "--drafts", "off")
        self.assertEqual(self.cli_pr(*sims)[0], 0)
        self.assertEqual(sorted(check.device for check in self.attempts.calls), ["iPhone 17 Pro", "iPhone Air"])
        self.assertEqual(self.cli_pr(*sims, "--spread")[0], 0)
        self.assertEqual([check.name for check in self.attempts.calls], ["Paywall"])

    def test_spread_without_two_devices_or_with_repeat_stops_before_the_build(self):
        for argv, said in ((("--sim", "iPhone 17 Pro@iOS 26.4", "--spread"),
                            "--spread shares the checks between two or more devices, and this run has 1."),
                           (("--sim", "iPhone 17 Pro@iOS 26.4", "--sim", "iPhone Air@iOS 26.4", "--spread", "--repeat",
                             "2"), "--repeat needs one target per check; leave --spread out")):
            with self.subTest(argv=argv):
                code, err = self.cli_pr(*argv)
                self.assertEqual(code, 3)
                self.assertIn(said, err)
                self.assertEqual((self.builds, self.attempts.calls), ([], []))

    def test_a_spread_refusal_starts_no_check_run_on_the_pull_request(self):
        seen = []
        poster = SimpleNamespace(start=lambda changes: seen.append("start"), finish=lambda done: seen.append("finish"),
                                 abandon=lambda: seen.append("abandon"))
        sims = ("iPhone 17 Pro@iOS 26.4", "iPhone Air@iOS 26.4")
        with self.assertRaisesRegex(api.UsageError, "--spread shares the checks between two or more devices"):
            self.run_pr(options=Options(keyless=True, sims=sims[:1], spread=True), github=poster)
        self.assertEqual(seen, ["abandon"])
        seen.clear()
        self.run_pr(pr_options=prrun.PrOptions(drafts="off"), options=Options(keyless=True, sims=sims, spread=True),
                    github=poster)
        self.assertEqual(seen, ["start", "finish"])

    def test_the_covering_check_and_a_draft_run_on_the_fresh_build_and_the_draft_never_fails_the_pr(self):
        result = self.run_pr({"The settings screen shows what this PR changed": "failed"})
        self.assertEqual(result.exit_code, 0)
        pr = result.pr
        self.assertEqual(pr["headline"], "Mobster checks: 1 passed")
        self.assertEqual([(row["check"], row["status"], row["draft"]) for row in pr["rows"]],
                         [("Paywall", "passed", False),
                          ("The settings screen shows what this PR changed", "failed", True)])
        self.assertEqual(pr["plan"]["checks"][0]["reasons"], ["expects “Choose your plan”", "names PaywallView"])
        self.assertEqual(pr["plan"]["drafts"], [{"screen": "SettingsView", "file": ".mobster/drafts/pr/settings.yaml"}])
        built = self.root / ".mobster" / "build" / "Build" / "Products" / "Debug-iphonesimulator" / "Firefox.app"
        self.assertEqual({check.app_path for check in self.attempts.calls}, {str(built)})
        self.assertIn("CODE_SIGNING_ALLOWED=NO", self.builds[-1])
        self.assertNotIn("Today", [check.name for check in self.attempts.calls])  # unrelated: not run
        folder = Path(result.paths["folder"])
        comment = (folder / "pr-comment.md").read_text()
        self.assertTrue(comment.startswith(R.MARKER + "\n### Mobster checks: 1 passed\n"))
        self.assertIn("No check covered 1 changed screen", comment)
        self.assertNotIn("Tested with", comment)  # the line is opt-in
        self.assertEqual(json.loads((folder / "pr.json").read_text())["schema"], R.SCHEMA)
        gifs = [row["gif"] for row in pr["rows"]]
        self.assertTrue(all(gifs))
        self.assertEqual((folder / gifs[1]).read_bytes()[:6], b"GIF89a")
        self.assertTrue((folder / pr["rows"][0]["frame"]).is_file())

    def test_a_failed_check_fails_the_pr_and_leads_the_comment_with_its_gif(self):
        result = self.run_pr({"Paywall": "failed"}, pr_options=prrun.PrOptions(drafts="off"))
        self.assertEqual(result.exit_code, 1)
        comment = Path(result.paths["comment"]).read_text()
        self.assertIn("### Mobster checks: 0 passed, 1 failed", comment)
        self.assertIn('| **Failed** Paywall | iPhone 17 Pro, iOS 26.4 | text “Choose your plan” | not on screen; '
                      'closest: "Choose a plan" | `001-01-paywall-iphone-17-pro-ios-26-4.jpg` |', comment)
        self.assertIn("GIF of the failure: `001-01-paywall-iphone-17-pro-ios-26-4.gif`", comment)
        self.assertNotIn("Tested with", comment)

    def test_tested_with_ends_the_comment_with_a_link_to_mobster_and_pr_json_remembers_it(self):
        result = self.run_pr({"Paywall": "failed"}, pr_options=prrun.PrOptions(drafts="off", tested_with=True))
        comment = Path(result.paths["comment"]).read_text()
        self.assertTrue(comment.rstrip("\n").endswith("<sub>Tested with [Mobster](https://mobster.dev/s/pr)</sub>"))
        self.assertIs(result.pr["testedWith"], True)
        self.assertIn("Tested with", R.comment(result.pr))
        self.assertNotIn("Tested with", R.comment({**result.pr, "testedWith": False}))
        self.assertNotIn("Tested with", R.comment({k: v for k, v in result.pr.items() if k != "testedWith"}))

    def test_xctest_builds_once_for_testing_and_the_tests_join_the_comment_and_the_exit_code(self):
        from mobile_agent.testkit import xctest
        from mobile_agent.tests.test_testing_xctest import FakeSimulators, Runner
        (self.root / "FirefoxTests").mkdir()
        (self.root / "FirefoxTests" / "PlanTests.swift").write_text("import XCTest\n")
        builds, base = [], fake_build([])

        def runner(argv, cwd, log=None, timeout=None):
            builds.append(argv)
            code, output = base(argv, cwd, log, timeout)
            if "build-for-testing" in argv:
                products = Path(cwd) / ".mobster" / "build" / "Build" / "Products"
                (products / "Firefox.xctestrun").write_bytes(plistlib.dumps({
                    "__xctestrun_metadata__": {}, "FirefoxTests": {"BlueprintName": "FirefoxTests",
                                                                   "TestBundlePath": "x"},
                    "FirefoxUITests": {"BlueprintName": "FirefoxUITests", "TestBundlePath": "y"}}))
            return code, output
        (self.root / "Firefox.xcodeproj").mkdir(exist_ok=True)
        self.attempts = FakeAttempts(self.root, {})
        sims, tests = FakeSimulators(), Runner()
        self.attempts.simulators = lambda: sims
        with mock.patch.object(xctest, "run_command", tests):
            result = prrun.test_pr(prrun.PrOptions(drafts="off"), Options(keyless=True, xctest=True), root=self.root,
                                   attempts=self.attempts, build_runner=runner, env={})
        compiles = [argv[-1] for argv in builds if argv[0] == "xcodebuild" and "-list" not in argv]
        self.assertEqual(compiles, ["build-for-testing"])          # one build: the app the checks drive, and the tests
        self.assertIn("-only-testing:FirefoxTests", tests.calls[0])  # the changed file picked its target
        self.assertEqual(result.exit_code, 1)                       # the failed test fails the PR
        self.assertEqual(result.pr["headline"], "Mobster checks: 1 passed, XCTest 1 failed of 4")
        self.assertEqual([target["name"] for target in result.pr["xctest"]["targets"]], ["FirefoxTests"])
        text = cli.pr_text(result)
        self.assertIn("✗ failed  FirefoxTests: PlanTests.testAnnualPlanIsBilledYearly\n    PlanTests.swift:", text)
        comment = Path(result.paths["comment"]).read_text()
        self.assertIn("**XCTest 1 failed of 4**", comment)
        self.assertRegex(comment, r"- `PlanTests\.testAnnualPlanIsBilledYearly` failed: PlanTests\.swift:\d+: ")
        folder = Path(result.paths["folder"])
        self.assertEqual(json.loads((folder / "results.json").read_text())["xctest"]["why"],
                         {"FirefoxTests": "FirefoxTests/PlanTests.swift changed"})
        suites = [suite.get("name") for suite in ET.fromstring((folder / "junit.xml").read_bytes())]
        self.assertEqual(suites[-1], "FirefoxTests")

    def test_no_screen_changed_runs_the_smoke_checks(self):
        git(self.root, "checkout", "-q", "main")
        git(self.root, "checkout", "-qb", "docs")
        (self.root / "README.md").write_text("docs\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "docs")
        result = self.run_pr()
        self.assertEqual(result.pr["plan"]["fallback"], "smoke")
        self.assertEqual([check.name for check in self.attempts.calls], ["Today"])

    def test_a_failed_build_is_a_couldnt_run_verdict_with_the_first_errors(self):
        def broken(argv, cwd, log=None, timeout=None):
            if "-list" in argv:
                return 0, json.dumps({"project": {"schemes": ["Firefox"]}})
            return 65, "PaywallView.swift:3:5: error: cannot find 'annual' in scope\n** BUILD FAILED **"
        (self.root / "Firefox.xcodeproj").mkdir()
        result = prrun.test_pr(prrun.PrOptions(), Options(keyless=True), root=self.root, build_runner=broken,
                               env={}, attempts=FakeAttempts(self.root, {}))
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.pr["headline"], "Mobster checks: the build failed")
        comment = Path(result.paths["comment"]).read_text()
        self.assertIn("cannot find 'annual' in scope", comment)

    def test_no_build_tests_the_builds_the_checks_name(self):
        result = self.run_pr(pr_options=prrun.PrOptions(build=False, drafts="off"))
        self.assertEqual(self.builds, [])
        self.assertEqual([check.app_path for check in self.attempts.calls], [str(self.app)])
        self.assertEqual(result.exit_code, 0)

    def test_on_an_iphone_the_device_build_is_installed_and_testflight_apps_use_the_phone_build(self):
        devicectl = Devicectl([{"bundleIdentifier": BUNDLE, "name": "Firefox", T.DEVELOPER_FIELD: False}])
        named = lambda name: PHONE if name == PHONE["name"] else None  # noqa: E731
        installs = []

        def install(record, path):
            self.assertTrue(self.attempts.lease_active)
            installs.append(path)
            return {"bundle_id": BUNDLE}
        gate = T.DeviceGate(runner=devicectl, installer=install, allowed=lambda: True)
        self.run_pr(pr_options=prrun.PrOptions(drafts="off"), options=Options(devices=(PHONE["name"],),
                                                                              keyless=True),
                    named_device=named, gate=gate, attempts=LeasedPhoneAttempts(self.root, {}))
        self.assertIn("generic/platform=iOS", self.builds[-1])
        self.assertTrue(installs[0].endswith("Debug-iphoneos/Firefox.app"))
        self.assertFalse(self.attempts.lease_active)
        gate = T.DeviceGate(runner=devicectl, installer=install, allowed=lambda: True, testflight=(BUNDLE,))
        builds = []
        result = self.run_pr(pr_options=prrun.PrOptions(drafts="off"),
                             options=Options(devices=(PHONE["name"],), keyless=True, testflight=(BUNDLE,)),
                             named_device=named, gate=gate, builds=builds,
                             attempts=LeasedPhoneAttempts(self.root, {}))
        self.assertEqual(builds, [])
        self.assertEqual(len(installs), 1)
        self.assertEqual(self.attempts.calls[0].app_path, None)
        self.assertFalse(self.attempts.lease_active)
        self.assertEqual(result.exit_code, 0)

    def test_testflight_needs_a_device(self):
        from mobile_agent.testkit.api import UsageError
        with self.assertRaisesRegex(UsageError, "--testflight goes with --device"):
            self.run_pr(options=Options(testflight=(BUNDLE,)))


class TestFlightGateTests(unittest.TestCase):
    def gate(self, testflight=()):
        apps = [{"bundleIdentifier": BUNDLE, "name": "Firefox", T.DEVELOPER_FIELD: False},
                {"bundleIdentifier": "com.spotify.client", "name": "Spotify", T.DEVELOPER_FIELD: False},
                {"bundleIdentifier": "co.hinge.mobile.ios", "name": "Hinge", T.DEVELOPER_FIELD: False}]
        return T.DeviceGate(runner=Devicectl(apps), allowed=lambda: True, testflight=testflight)

    def check(self, bundle=BUNDLE):
        return check_from_dict({"bundle_id": bundle, "expect": [{"text": "x"}]})

    def test_without_the_allow_a_testflight_build_is_refused_like_an_app_store_app(self):
        with self.assertRaises(T.Refused) as caught:
            self.gate().admit(self.check(), PHONE)
        self.assertEqual(caught.exception.klass, "not_developer_build")
        self.assertIn("pass --testflight with its bundle ID", caught.exception.fix)

    def test_with_the_allow_only_the_named_app_runs(self):
        gate = self.gate(testflight=(BUNDLE,))
        ran, notes, _ = gate.admit(self.check(), PHONE)
        self.assertEqual(ran.bundle_id, BUNDLE)
        self.assertIn("that --testflight names as yours", notes[0])
        with self.assertRaisesRegex(T.Refused, "Spotify is from the App Store"):
            gate.admit(self.check("com.spotify.client"), PHONE)

    def test_apple_apps_stay_refused_even_when_named_and_a_social_app_runs_like_any_other(self):
        gate = self.gate(testflight=("com.apple.mobilenotes", "co.hinge.mobile.ios"))
        with self.assertRaisesRegex(T.Refused, "part of iOS"):
            gate.admit(self.check("com.apple.mobilenotes"), PHONE)
        ran, notes, _ = gate.admit(self.check("co.hinge.mobile.ios"), PHONE)
        self.assertEqual(ran.bundle_id, "co.hinge.mobile.ios")
        self.assertIn("that --testflight names as yours", notes[0])


class BuildTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()

    def test_the_project_is_found_at_the_root_or_one_folder_below_and_a_workspace_wins(self):
        self.assertIsNone(B.find_project(self.root))
        (self.root / "ios" / "Firefox.xcodeproj").mkdir(parents=True)
        self.assertEqual(B.find_project(self.root), ("project", self.root / "ios" / "Firefox.xcodeproj"))
        (self.root / "ios" / "Firefox.xcworkspace").mkdir()
        self.assertEqual(B.find_project(self.root)[0], "workspace")
        (self.root / "Other.xcodeproj").mkdir()
        (self.root / "Third.xcodeproj").mkdir()
        with self.assertRaisesRegex(B.BuildError, "more than one Xcode project"):
            B.find_project(self.root)

    def test_a_scheme_is_picked_only_when_it_is_clear(self):
        def lister(names):
            return lambda argv, cwd, log=None, timeout=None: (
                0, "noise\n" + json.dumps({"project": {"schemes": names}}))
        found = ("project", self.root / "Firefox.xcodeproj")
        self.assertEqual(B.pick_scheme(found, runner=lister(["Only"])), "Only")
        self.assertEqual(B.pick_scheme(found, runner=lister(["Firefox", "Widgets"])), "Firefox")
        with self.assertRaisesRegex(B.BuildError, "schemes: A, B"):
            B.pick_scheme(found, runner=lister(["A", "B"]))

    def test_simulator_and_device_builds(self):
        calls = []
        found = ("project", self.root / "Firefox.xcodeproj")
        built = B.build(self.root, scheme="Firefox", project=found, platform="sim", runner=fake_build(calls))
        self.assertEqual(built["bundle"], BUNDLE)
        self.assertIn("generic/platform=iOS Simulator", calls[0])
        self.assertIn(str(self.root / ".mobster" / "build"), calls[0])
        self.assertIn("CODE_SIGNING_ALLOWED=NO", calls[0])
        B.build(self.root, scheme="Firefox", project=found, platform="device", runner=fake_build(calls))
        self.assertIn("-allowProvisioningUpdates", calls[1])
        self.assertNotIn("CODE_SIGNING_ALLOWED=NO", calls[1])


class CommentTests(unittest.TestCase):
    def test_artifact_names_never_overwrite_another_checks_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checks = []
            for index, name in enumerate(("Paywall?", "Paywall!"), 1):
                frame = root / f"source-{index}.jpg"
                frame.write_bytes(str(index).encode())
                checks.append({"name": name, "results": [{"device": "iPhone", "frame": str(frame),
                                                         "status": "failed"}]})
            doc = {"checks": checks}
            frames = R.copy_frames(doc, root)
            gifs = R.make_gifs(doc, root, build=lambda check, result: check["name"].encode())
            self.assertEqual(len(set(frames.values())), 2)
            self.assertEqual(len(set(gifs.values())), 2)
            self.assertEqual([(root / frames[(c, 0)]).read_bytes() for c in range(2)], [b"1", b"2"])
            self.assertEqual([(root / gifs[(c, 0)]).read_bytes() for c in range(2)], [b"Paywall?", b"Paywall!"])

    def pr(self, rows, **extra):
        return {"headline": "Mobster checks: 1 passed, 1 failed", "rows": rows, "head": "abcdef0123",
                "plan": {"screens": [{"name": "PaywallView"}]}, "testedWith": True, **extra}

    def test_cells_are_escaped_and_long_comments_are_cut(self):
        row = {"check": "a | b <script>", "device": "iPhone", "status": "failed", "expected": "x", "found": "y",
               "frame": "pr/frames/a.jpg", "gif": None}
        text = R.comment(self.pr([row]), artifact_url="https://github.com/o/r/actions/runs/1/artifacts/2")
        self.assertIn("a \\| b &lt;script&gt;", text)
        self.assertIn("[a.jpg](https://github.com/o/r/actions/runs/1/artifacts/2)", text)
        self.assertIn("Changed screens: `PaywallView`, at `abcdef0`", text)
        many = [dict(row, check=f"check {n}", found="z" * 300) for n in range(2000)]
        self.assertLessEqual(len(R.comment(self.pr(many))), R.COMMENT_LIMIT)

    def test_drafts_that_were_only_written_are_named(self):
        text = R.comment(self.pr([], plan={"drafts": [{"screen": "SettingsView",
                                                       "file": ".mobster/drafts/pr/settings.yaml"}]}))
        self.assertIn("drafted one for each, without running it", text)
        self.assertIn("- `.mobster/drafts/pr/settings.yaml`", text)

    def test_healed_steps_from_replay_are_listed_for_review(self):
        row = {"check": "paywall", "device": "iPhone", "status": "passed", "expected": "x", "found": "as expected",
               "healed": [{"step": 2, "from": "Upgrade button", "to": "Go Pro button"}], "replayed": True}
        text = R.comment(self.pr([row]))
        self.assertIn("**1 step healed.** Review the diff before you accept it:", text)
        self.assertIn("- paywall, step 2: Upgrade button → Go Pro button", text)
        self.assertIn("iPhone, replayed", text)


class CliTests(Repo):
    def args(self, **kw):
        from mobile_agent.cli_parser import build_parser
        parser = build_parser()
        return parser.parse_args(["test", *kw.pop("argv", [])])

    def test_pr_flags_without_pr_and_testflight_without_a_device_are_usage_errors(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.run(self.args(argv=["--base", "main"]))
        self.assertEqual(code, 3)
        self.assertIn("--base goes with pr", err.getvalue())
        self.assertEqual(cli.suite(self.args(argv=["--testflight", BUNDLE]), [], out=io.StringIO(), err=io.StringIO()),
                         3)

    def test_pr_runs_and_writes_the_comment_where_asked(self):
        seen = {}

        def run(pr_options, options, *, root, progress):
            seen.update(pr=pr_options, options=options, root=root)
            folder = root / "out"
            folder.mkdir()
            (folder / "pr-comment.md").write_text("comment\n")
            pr = {"headline": "Mobster checks: 1 passed", "rows": [], "plan": {"screens": [{"name": "PaywallView"}]}}
            return prrun.PrResult(pr, {"checks": []}, {"folder": folder, "comment": folder / "pr-comment.md"}, 0)
        out = io.StringIO()
        args = self.args(argv=["pr", "--base", "main", "--no-build", "--drafts", "write", "--comment",
                               str(self.root / "c.md"), "--sim", "iPhone 17 Pro"])
        code = cli.pr(args, [], run=run, out=out, err=io.StringIO(), cwd=self.root)
        self.assertEqual(code, 0)
        self.assertEqual((seen["pr"].base, seen["pr"].build, seen["pr"].drafts), ("main", False, "write"))
        self.assertEqual(seen["options"].sims, ("iPhone 17 Pro",))
        self.assertEqual((self.root / "c.md").read_text(), "comment\n")
        self.assertIn("Changed screens: PaywallView", out.getvalue())
        self.assertIn("Mobster checks: 1 passed", out.getvalue())

    @unittest.skipUnless(importlib.util.find_spec("mobile_agent.teams"), "requires the app teams extension")
    def test_share_links_the_comment_to_the_teams_hosted_report(self):
        from mobile_agent.teams import report_share
        rid = "6b1f0a52-3c1e-4d7a-9a40-5be0c2d9e7f1"

        def run(pr_options, options, *, root, progress):
            folder = root / "out"
            folder.mkdir()
            (folder / "pr-comment.md").write_text("comment\n")
            pr = {"headline": "Mobster checks: 1 failed", "testedWith": False, "plan": {},
                  "rows": [{"check": "Paywall", "device": "iPhone", "status": "failed", "expected": "x", "found": "y",
                            "frame": "pr/frames/paywall.jpg"}]}
            return prrun.PrResult(pr, {"checks": []}, {"folder": folder, "comment": folder / "pr-comment.md"}, 1)
        shared = report_share.Shared(report_id=rid, key=bytes(32),
                                     link=f"https://console.mobster.dev/r/{rid}#k={'A' * 43}", expires_at=None)
        out = io.StringIO()
        with mock.patch.object(report_share, "share_folder", return_value=shared):
            code = cli.pr(self.args(argv=["pr", "--share", "--json"]), [], run=run, out=out, err=io.StringIO(),
                          cwd=self.root)
        self.assertEqual(code, 1)
        text = (self.root / "out" / "pr-comment.md").read_text()
        self.assertIn(f"]({shared.page('pr/frames/paywall.jpg')})", text)
        self.assertIn(f"[Report, frames and GIFs]({shared.link})", text)
        self.assertEqual(json.loads(out.getvalue())["hostedReport"], shared.link)
        import shutil
        shutil.rmtree(self.root / "out")
        err = io.StringIO()
        with mock.patch.object(report_share, "share_folder", return_value=shared), \
                mock.patch("mobile_agent.paths.write_text_atomic", side_effect=OSError("disk full")):
            out = io.StringIO()
            code = cli.pr(self.args(argv=["pr", "--share", "--json"]), [], run=run, out=out, err=err, cwd=self.root)
        self.assertEqual(code, 1)
        self.assertIn("couldn't be written into the comment", err.getvalue())
        self.assertNotIn("hostedReport", json.loads(out.getvalue()))


class McpTests(unittest.TestCase):
    def toolset(self, folder, allow_devices=None, allow_testflight=()):
        return Namespace(runs_dir=str(Path(folder) / ".mobster" / "runs"), keyless=True, _key=None,
                         allow_devices=allow_devices, allow_testflight=frozenset(allow_testflight))

    def test_pr_is_a_flag_of_run_tests_and_reaches_a_real_iphone_only_with_the_users_allow(self):
        from mobile_agent.mcp_server.tools import ToolError
        from mobile_agent.testkit.mcp import TestTools
        tools = TestTools()
        self.assertEqual([item["name"] for item in tools.definitions(None)],
                         ["run_tests", "test_history", "import_maestro", "test_schedule", "repro_crash",
                          "review_heals", "compare_test_suites"])
        self.assertTrue({"pr", "base", "device", "testflight"} <= tools.definitions(None)[0]["inputSchema"][
            "properties"].keys())
        with tempfile.TemporaryDirectory() as folder:
            for args in ({"base": "main"}, {"pr": True, "retries": 2}):  # each side's arguments refuse the other
                with self.assertRaisesRegex(ToolError, "go with pr: true|go without pr"):
                    tools.run_tests(args, None, self.toolset(folder))
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch("mobile_agent.device_targets.named_device", return_value=PHONE):
                with self.assertRaisesRegex(ToolError, "--allow-device"):
                    tools._pr_options({"path": folder, "device": "Studio iPhone"}, self.toolset(folder))
                allowed = self.toolset(folder, allow_devices={"studio iphone"})
                _, options, _ = tools._pr_options({"path": folder, "device": "Studio iPhone"}, allowed)
                self.assertEqual(options.devices, ("Studio iPhone",))
                with self.assertRaisesRegex(ToolError, "--allow-testflight"):
                    tools._pr_options({"path": folder, "device": "Studio iPhone", "testflight": BUNDLE}, allowed)
                both = self.toolset(folder, allow_devices={"studio iphone"}, allow_testflight=(BUNDLE,))
                _, options, _ = tools._pr_options({"path": folder, "device": "Studio iPhone", "testflight": BUNDLE},
                                                  both)
                self.assertEqual(options.testflight, (BUNDLE,))
            with self.assertRaisesRegex(ToolError, "path must be absolute"):
                tools._pr_options({"path": "relative"}, self.toolset(folder))

    def test_the_job_answers_with_the_verdict_frames_and_gifs(self):
        from mobile_agent.testkit.jobs import PrJob
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            jpeg(folder / "pr" / "frames" / "paywall.jpg")
            pr = {"headline": "Mobster checks: 0 passed, 1 failed", "counts": {},
                  "plan": {"screens": [{"name": "PaywallView"}], "drafts": []},
                  "rows": [{"check": "paywall", "device": "iPhone 17 Pro", "status": "failed", "expected": "x",
                            "found": "y", "frame": "pr/frames/paywall.jpg", "gif": "pr/gifs/paywall.gif",
                            "draft": False}]}

            def run(pr_options, options, **kwargs):
                return prrun.PrResult(pr, {}, {"folder": folder, "comment": folder / "pr-comment.md"}, 1)
            job = PrJob(prrun.PrOptions(), Options(), folder, run=run)
            job.start()
            self.assertTrue(job.wait(5))
            result = job.result()
        self.assertIn("Mobster checks: 0 passed, 1 failed (exit code 1).", result.text)
        self.assertIn(f"GIF: {folder / 'pr/gifs/paywall.gif'}", result.text)
        self.assertEqual(result.structured["exit_code"], 1)
        self.assertEqual(len(result.images), 1)


class RecentTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.data = self.dir / "data"
        self.project = self.dir / "Firefox"

    def suite(self, suite_id, *, status="failed", run_dir=None):
        folder = self.project / ".mobster" / "test-results" / suite_id
        folder.mkdir(parents=True)
        result = {"device": "iPhone 17 Pro · iOS 26.4", "status": status, "flaky": False,
                  "reason": ({"class": "app_crashed", "message": "It crashed.", "fix": None} if status != "passed"
                             else None),
                  "evidence": {"source": "simulator", "atMs": 1200, "step": 1} if status != "passed" else None,
                  "runs": [{"runId": "20261016-100000-aaaa", "verdict": status, "runDir": str(run_dir or "")}]}
        doc = {"schema": "mobster.test/1", "suite": suite_id, "startedAt": 1, "durationMs": 2000, "exitCode": 1,
               "summary": {"passed": 0, "failed": 1, "total": 1},
               "checks": [{"name": "Feed loads", "file": ".mobster/checks/feed.yaml", "results": [result]}]}
        (folder / "results.json").write_text(json.dumps(doc))
        return doc, folder

    def test_a_suite_says_which_devices_it_ran_on_and_how_many_checks_it_held(self):
        doc, folder = self.suite("20261016-100000-0001", status="passed")
        recent.remember(doc, folder, self.project, data_dir=self.data)
        found = recent.recent(5, data_dir=self.data)[0]
        self.assertEqual((found["devices"], found["checks"]), (["iPhone 17 Pro · iOS 26.4"], 1))
        doc["devices"] = [{"key": "a", "name": "iPhone 16"}, {"key": "b"}, {"name": ""}]
        (folder / "results.json").write_text(json.dumps(doc))
        self.assertEqual(recent.recent(5, data_dir=self.data)[0]["devices"], ["iPhone 16", "b"])

    def test_remembers_newest_first_and_reads_back_what_didnt_pass_with_its_steps(self):
        run_dir = self.project / ".mobster" / "runs" / "20261016-100000-aaaa"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(json.dumps({"steps": [{"index": 1, "text": "Tapped Feed", "at_ms": 900}]}))
        for suite_id, status in (("20261016-100000-0001", "passed"), ("20261016-110000-0002", "failed")):
            doc, folder = self.suite(suite_id, status=status, run_dir=run_dir)
            recent.remember(doc, folder, self.project, data_dir=self.data)
        suites = recent.recent(data_dir=self.data)
        self.assertEqual([suite["suite"] for suite in suites], ["20261016-110000-0002", "20261016-100000-0001"])
        latest = suites[0]
        self.assertEqual(latest["project"], "Firefox")
        self.assertEqual(latest["unpassed"][0]["steps"], [{"index": 1, "text": "Tapped Feed", "atMs": 900}])
        self.assertEqual(latest["unpassed"][0]["evidence"]["step"], 1)
        self.assertEqual(suites[1]["unpassed"], [])

    def test_a_failure_names_each_expectation_and_the_frame_it_failed_on(self):
        """ICP-3: "1 of 2 expectations failed" must say which, what the screen showed instead, and where it looked."""
        run_dir = self.project / ".mobster" / "runs" / "20261016-100000-aaaa"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(json.dumps({"assertions": [
            {"text": 'text "Welcome"', "ok": True, "assertion": {"text": "Welcome"}, "frame": "frames/01-verdict.jpg"},
            {"text": 'text "Day 4"', "ok": False, "assertion": {"text": "Day 4", "name": "The streak goes up"},
             "observed": 'not on screen; closest "Day 3"', "frame": "frames/02-verdict.jpg"},
            {"text": "junk", "ok": "yes"}, {"text": 'text "x"', "ok": False, "frame": "../../secrets.png"}]}))
        doc, folder = self.suite("20261016-110000-0002", run_dir=run_dir)
        recent.remember(doc, folder, self.project, data_dir=self.data)
        failure = recent.recent(data_dir=self.data)[0]["unpassed"][0]
        self.assertEqual(failure["expectations"], [
            {"text": 'text "Welcome"', "name": None, "ok": True, "observed": None},
            {"text": 'text "Day 4"', "name": "The streak goes up", "ok": False,
             "observed": 'not on screen; closest "Day 3"'},
            {"text": 'text "x"', "name": None, "ok": False, "observed": ""}])
        self.assertEqual(failure["frame"], {"run": "20261016-100000-aaaa", "path": "frames/02-verdict.jpg"})

    def test_keeps_the_last_twenty_and_skips_what_isnt_a_suite_folder(self):
        for n in range(recent.KEEP + 3):
            doc, folder = self.suite(f"20261016-1000{n:02d}-{n:04x}")
            recent.remember(doc, folder, self.project, data_dir=self.data)
        index = json.loads((self.data / recent.INDEX).read_text())
        self.assertEqual(len(index), recent.KEEP)
        index.insert(0, {"folder": str(self.dir)})  # not .mobster/test-results/<suite>
        index.insert(0, {"folder": str(self.project / ".mobster" / "test-results" / "20261016-999999-dead")})  # gone
        (self.data / recent.INDEX).write_text(json.dumps(index))
        self.assertEqual(len(recent.recent(limit=3, data_dir=self.data)), 3)
        self.assertEqual(recent.recent(limit=1, data_dir=self.data)[0]["suite"], f"20261016-1000{recent.KEEP + 2:02d}-"
                                                                                f"{recent.KEEP + 2:04x}")

    def test_by_root_gives_each_folders_newest_suite_for_the_projects_list(self):
        other = self.dir / "Other"
        for suite_id, root in (("20261016-100000-0001", self.project), ("20261016-110000-0002", self.project)):
            doc, folder = self.suite(suite_id)
            recent.remember(doc, folder, root, data_dir=self.data)
        self.project, gone = other, self.project
        doc, folder = self.suite("20261016-120000-0003")
        recent.remember(doc, folder, other, data_dir=self.data)
        (folder / "results.json").unlink()   # a suite that can't be read is left out
        runs = recent.by_root(data_dir=self.data)
        self.assertEqual(set(runs), {str(gone.resolve())})
        self.assertEqual(runs[str(gone.resolve())], {"suite": "20261016-110000-0002", "startedAt": 1, "exitCode": 1,
                                                     "summary": {"passed": 0, "failed": 1, "total": 1}})

    def test_a_broken_index_reads_as_none_and_a_write_never_raises(self):
        self.data.mkdir()
        (self.data / recent.INDEX).write_text("{nope")
        self.assertEqual(recent.recent(data_dir=self.data), [])
        recent.remember({"suite": "x"}, self.dir, self.dir, data_dir=self.data / "file.txt" / "under-a-file")

    def test_the_route(self):
        doc, folder = self.suite("20261016-120000-beef")
        recent.remember(doc, folder, self.project, data_dir=self.data)
        with mock.patch.object(recent, "dev_data_dir", lambda: self.data):
            answer = register.recent_suites(SimpleNamespace())
        self.assertEqual((answer.status, answer.value["suites"][0]["suite"]), (200, "20261016-120000-beef"))


def reports_run(attempt, verdict, run_id, report=None, message=None, repetition=1):
    return {"repetition": repetition, "attempt": attempt, "runId": run_id, "verdict": verdict,
            "class": None if verdict == "passed" else "assertion", "message": message, "seconds": 2.5,
            "report": report, "runDir": None, "frame": None, "video": None}


def reports_doc(folder, *, repeat=1):
    folder = Path(folder)
    run_dir = folder / "runs-src" / "20261007-120000-aaaa"
    run_dir.mkdir(parents=True)
    (run_dir / "report.html").write_text("<!doctype html><title>run</title>")
    frame = run_dir / "05-verdict-ax.jpg"
    Image.new("RGB", (400, 870), (30, 30, 40)).save(frame, "JPEG")
    report = str(run_dir / "report.html")
    flaky = {"device": "iPhone 17 Pro · iOS 26.4", "deviceKey": "sim:a", "status": "passed", "attempts": 2,
             "flaky": True, "report": report, "durationMs": 5000, "healed": [], "reason": None, "quarantined": False,
             "frame": str(frame), "video": None, "costUsd": 0.02,
             "flakyReason": {"class": "assertion", "message": "1 of 2 expectations failed\x07"},
             "runs": [reports_run(1, "failed", "20261007-115900-bbbb", None, "1 of 2 expectations failed"),
                      reports_run(2, "passed", "20261007-120000-aaaa", report)]}
    failed = {"device": "iPhone 17 Pro · iOS 26.4", "deviceKey": "sim:a", "status": "failed", "attempts": 1,
              "flaky": False, "report": None, "durationMs": 2500, "healed": [],
              "reason": {"class": "assertion", "message": "text \"Restore <failed>\" failed: not on screen",
                         "fix": None},
              "quarantined": True, "frame": None, "video": None, "costUsd": 0,
              "runs": [reports_run(1, "failed", "20261007-120100-cccc", None, "not on screen")]}
    couldnt = {"device": "Studio iPhone · iOS 26.4", "deviceKey": "device:usb", "status": "couldnt_run", "attempts": 0,
               "flaky": False, "report": None, "durationMs": 0, "healed": [], "quarantined": False, "frame": None,
               "video": None, "costUsd": 0, "runs": [], "notes": ["Installed Firefox.app on Studio iPhone."],
               "reason": {"class": "not_developer_build",
                          "message": "Mobster tests your own apps on your iPhone. Firefox is from the App Store.",
                          "fix": "Install your own build of it from Xcode, then run the check again."}}
    return {"schema": REPORTS_R.SCHEMA, "suite": "20261007-120000-abcd", "startedAt": 1_791_380_000_000,
            "finishedAt": 1_791_380_075_000, "durationMs": 75_000, "exitCode": 3,
            "options": {"repeat": repeat, "retries": 1}, "command": "mobster test --device \"Studio iPhone\"",
            "devices": [{"key": "sim:a", "name": "iPhone 17 Pro · iOS 26.4"},
                        {"key": "device:usb", "name": "Studio iPhone · iOS 26.4"}],
            "checks": [{"name": "Paywall <b>plans</b>", "file": ".mobster/checks/paywall.yaml",
                        "classname": "checks.paywall", "tags": ["smoke"], "quarantined": False,
                        "results": [flaky, couldnt]},
                       {"name": "Restore shows an error", "file": ".mobster/checks/restore.yaml",
                        "classname": "checks.restore", "tags": [], "quarantined": True,
                        "quarantineReason": "Flaky on iOS 26.4 (FB13579)", "results": [failed]}],
            "summary": {"passed": 1, "failed": 0, "needsReview": 0, "couldntRun": 1, "flaky": 1, "quarantined": 1,
                        "total": 3, "costUsd": 0.02},
            "mobster": {"version": "0.3.0"}}


class ReportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name).resolve()
        self.suite = self.folder / "suite"
        self.suite.mkdir()

    def test_canonical_report_writer_captures_completion_inventory(self):
        from mobile_agent.testkit import capsule
        folder = self.folder / "20261024-120000-abcd"
        folder.mkdir()
        data = reports_doc(self.folder)
        data["suite"] = folder.name
        REPORTS_R.write_all(data, folder, cwd=self.folder)
        self.assertEqual(capsule.read_completed_suite(folder), data)
        inventory = json.loads((folder / "evidence-inventory.json").read_text())
        self.assertEqual(inventory["suite"], folder.name)
        self.assertTrue(any(row["path"] == "index.html" and row["state"] == "captured"
                            for row in inventory["files"]))

    def test_inventory_failure_preserves_canonical_reports_and_verdict(self):
        from mobile_agent.testkit import capsule
        folder = self.folder / "20261024-120000-abcd"
        folder.mkdir()
        data = reports_doc(self.folder)
        data["suite"] = folder.name
        for error in (capsule.CapsuleError("too many files"), OSError("disk full")):
            with self.subTest(error=type(error).__name__), mock.patch.object(capsule, "capture", side_effect=error):
                paths = REPORTS_R.write_all(data, folder, cwd=self.folder)
                self.assertTrue(all(paths[key].is_file() for key in ("results", "junit", "html")))
                self.assertEqual(json.loads(paths["results"].read_text()), data)
                self.assertFalse((folder / "evidence-inventory.json").exists())
                with self.assertRaises(capsule.CapsuleError):
                    capsule.read_completed_suite(folder)

    def test_collect_uses_selected_run_for_cell_report(self):
        first = self.folder / "first.html"
        last = self.folder / "last.html"
        first.write_text("first")
        last.write_text("last")
        data = {"checks": [{"results": [{"runId": "first", "report": str(first),
                                           "runs": [{"runId": "first", "report": str(first)},
                                                    {"runId": "last", "report": str(last)}]}]}]}
        REPORTS_R.collect(data, self.suite)
        self.assertEqual(data["checks"][0]["results"][0]["report"], "runs/first.html")

    def test_document_keeps_ordinary_stop_distinct_from_unknown_outcome(self):
        for reason, stopped, exit_code in (("stopped", True, 130), ("unknown_outcome", False, 3),
                                           ("journal", False, 3)):
            with self.subTest(reason=reason):
                doc = REPORTS_R.document({"stopReason": reason, "exitCode": exit_code}, suite_id="s",
                                         options={}, version="test")
                self.assertEqual((doc["stopReason"], doc["stopped"], doc["exitCode"]),
                                 (reason, stopped, exit_code))

    def test_junit_quarantine_flakes_and_strict(self):
        data = reports_doc(self.folder)
        REPORTS_R.collect(data, self.suite)
        root = validate_junit(REPORTS_R.junit(data))
        self.assertEqual([suite.get("name") for suite in root], ["iPhone 17 Pro · iOS 26.4",
            "Studio iPhone · iOS 26.4"])
        cases = {case.get("name"): case for case in root[0].iter("testcase")}
        self.assertIsNone(cases["Paywall <b>plans</b>"].find("failure"))   # a flaky pass passes
        props = {p.get("name"): p.get("value") for p in cases["Paywall <b>plans</b>"].iter("property")}
        self.assertEqual((props["flaky"], props["attempts"], props["report"]),
                         ("true", "2", "runs/20261007-120000-aaaa.html"))
        skipped = cases["Restore shows an error"].find("skipped")
        self.assertEqual(skipped.get("message"), "Quarantined (Failed): Flaky on iOS 26.4 (FB13579)")
        failure = root[1].find("testcase/failure")
        self.assertEqual(failure.get("type"), "couldnt_run")
        self.assertIn("Install your own build", failure.text)
        self.assertIn("Installed Firefox.app", root[1].find("testcase/system-out").text)
        strict = validate_junit(REPORTS_R.junit(data, strict=True, report_base=".mobster/test-results/x"))
        flaky = strict[0].find("testcase")
        self.assertEqual(flaky.find("failure").get("type"), "flaky")
        self.assertNotIn("\x07", flaky.find("failure").text)
        props = {p.get("name"): p.get("value") for p in flaky.iter("property")}
        self.assertEqual(props["report"], ".mobster/test-results/x/runs/20261007-120000-aaaa.html")

    def test_repeats_name_each_run(self):
        data = reports_doc(self.folder, repeat=5)
        system_out = validate_junit(REPORTS_R.junit(data)).find("testsuite/testcase/system-out").text
        self.assertIn("Run 1, attempt 2: Passed", system_out)

    def test_the_html_matrix_flake_table_and_problems(self):
        data = reports_doc(self.folder)
        paths = REPORTS_R.write_all(data, self.suite, html_dir=self.folder / "out", cwd=self.folder)
        page = paths["html"].read_text()
        self.assertRegex(page, r'<span class="verdict none"><svg[^>]*>.*?</svg>Couldn&#x27;t run</span>')
        self.assertIn("<title>Mobster test: 1 of 2 passed</title>", page)
        self.assertIn("Paywall &lt;b&gt;plans&lt;/b&gt;", page)
        self.assertNotIn("<b>plans</b>", page)
        self.assertIn('<span class="col">Studio iPhone, iOS 26.4</span>', page)
        self.assertIn('id="flaky">Flaky<', page)
        self.assertIn('aria-label="failed, passed"', page)
        self.assertIn(">50%<", page)
        self.assertIn("Quarantined", page)
        self.assertIn("Its results don't fail the suite.", page)
        self.assertRegex(page, r'<details class="check is-all is-flaky is-unpassed" id="check-1" open>')
        self.assertIn("Install your own build of it from Xcode, then run the check again.", page)
        self.assertIn('<p class="lede">1 flaky, 1 quarantined</p>', page)
        self.assertNotIn("·", page.split("</style>", 1)[1])
        self.assertRegex(page, r'<ul class="failures"><li class="none">.*?<a href="#check-1">Paywall &lt;b&gt;'
                               r'plans&lt;/b&gt;</a> on Studio iPhone, iOS 26.4<span class="why">'
                               r'Mobster tests your own apps')
        self.assertNotIn('href="#check-2"', page)
        self.assertNotIn("\x07", page)
        self.assertIn('href="runs/20261007-120000-aaaa.html"', page)
        self.assertIn('src="data:image/jpeg;base64,', page)
        self.assertIn("1 passed, 1 flaky, 1 couldn&#x27;t run, 1 quarantined", page)
        self.assertNotRegex(page, r"src=\"(https?:|/)")
        self.assertEqual(re.findall(r'href="((?:https?:|/)[^"]*)"', page), [links.MADE_WITH_REPORT])
        self.assertIn(f'<footer class="foot"><span><a href="{links.MADE_WITH_REPORT}">Mobster</a> ', page)
        self.assertTrue((self.folder / "out" / "runs" / "20261007-120000-aaaa.html").is_file())
        self.assertEqual((self.folder / "out" / "index.html").read_text(), page)
        saved = (self.suite / "results.json").read_text()
        self.assertNotIn(str(self.folder / "runs-src" / "20261007-120000-aaaa" / "report.html"), saved)

    def test_a_spread_report_groups_the_checks_by_device_with_a_suite_each(self):
        data = reports_doc(self.folder)
        data["options"]["spread"] = True
        data["checks"][0]["results"] = [data["checks"][0]["results"][0]]
        phone = dict(data["checks"][1]["results"][0], device="Studio iPhone · iOS 26.4", deviceKey="device:usb",
                     quarantined=False)
        data["checks"][1]["results"] = [phone]
        data["checks"][1]["quarantined"] = False
        page = REPORTS_R.render_html(data, self.suite)
        groups = re.findall(r'<h3 class="dev-group">(.*?)<span class="count">(\d+)</span></h3>', page)
        self.assertEqual(groups, [("iPhone 17 Pro, iOS 26.4", "1"), ("Studio iPhone, iOS 26.4", "1")])
        first, second = page.split('<h3 class="dev-group">')[1:]
        self.assertIn("Paywall", first)
        self.assertNotIn("Restore shows an error", first)
        self.assertIn("Restore shows an error", second)
        self.assertEqual(page.count('id="check-1"'), 1)
        root = validate_junit(REPORTS_R.junit(data))
        self.assertEqual([suite.get("name") for suite in root], ["iPhone 17 Pro · iOS 26.4",
            "Studio iPhone · iOS 26.4"])
        self.assertEqual([len(suite.findall("testcase")) for suite in root], [1, 1])

    def test_a_failing_check_opens_to_its_assertions_beside_the_frame(self):
        from mobile_agent.testkit.suite import attempt_record
        record = attempt_record({"verdict": "failed", "assertions": [
            {"index": 0, "text": 'text "Restore"', "ok": False, "observed": "not on screen", "matches": []},
            {"index": 1, "text": "count id=/^plan_/ == 3", "ok": False,
             "observed": "found 2: plan_weekly, plan_monthly",
             "matches": [{"id": "plan_weekly", "rect": [20.0, 464.0, 362.0, 65.0]}]}]}, 1)
        self.assertEqual(record["assertions"][1], {"index": 1, "text": "count id=/^plan_/ == 3", "ok": False,
                                                   "observed": "found 2: plan_weekly, plan_monthly",
                                                   "outlined": True})
        data = reports_doc(self.folder)
        failed = data["checks"][1]["results"][0]
        failed["assertions"] = record["assertions"]
        failed["frame"] = data["checks"][0]["results"][0]["frame"]
        page = REPORTS_R.render_html(data, self.suite)
        self.assertIn('<code class="assert">count id=/^plan_/ == 3</code>', page)
        self.assertIn("found 2: plan_weekly, plan_monthly", page)
        self.assertIn('aria-label="2, failed"', page)                               # its pill on the frame
        self.assertIn('aria-label="1, failed, not outlined on the frame"', page)   # nothing on screen to outline
        self.assertEqual(page.count('<div class="phone"><img src="data:image/jpeg;base64,'), 2)

    def test_a_missing_report_is_none_and_suite_folders_are_private_and_unique(self):
        data = reports_doc(self.folder)
        data["checks"][0]["results"][0]["runs"][1]["report"] = str(self.folder / "gone.html")
        data["checks"][0]["results"][0]["report"] = str(self.folder / "gone.html")
        REPORTS_R.collect(data, self.suite)
        self.assertIsNone(data["checks"][0]["results"][0]["report"])
        first, folder = REPORTS_R.make_suite_dir(self.folder / "results", "20261007-120000-abcd")
        (folder / "marker").write_text("original")
        with self.assertRaises(FileExistsError):
            REPORTS_R.make_suite_dir(self.folder / "results", "20261007-120000-abcd")
        self.assertEqual((folder / "marker").read_text(), "original")
        with mock.patch.object(REPORTS_R, "new_suite_id", side_effect=[first, "20261007-120001-abcd"]):
            second, other = REPORTS_R.make_suite_dir(self.folder / "results")
        self.assertNotEqual(first, second)
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertRegex(second, r"^\d{8}-\d{6}-[0-9a-f]{4}$")


SUITE_BUNDLE = "org.mozilla.ios.Firefox"


SUITE_PHONE = {"id": "00008130-001A2B3C4D5E6F70", "udid": "00008130-001A2B3C4D5E6F70",
               "kind": "usb", "name": "Studio iPhone", "wdaUrl": "http://127.0.0.1:8100"}


def suite_entry(name, quarantined=None, error=None, **data):
    check = None if error else check_from_dict({"bundle_id": SUITE_BUNDLE, "name": name, "expect": [{"text": "Hi"}],
                                                **data})
    return SuiteEntry(path=Path(f"/p/.mobster/checks/{name}.yaml"), file=f".mobster/checks/{name}.yaml", check=check,
                 error=error, quarantined=quarantined, classname=f"checks.{name}")


def result(verdict, klass=None, message=None, device=None, seconds=1.0, run_id=None):
    return {"run_id": run_id or f"20261007-120000-{abs(hash((verdict, time.monotonic()))) % 65536:04x}",
            "verdict": verdict, "reason": {"class": klass, "message": message or verdict, "fix": None},
            "seconds": seconds, "device": device or {"name": "Mobster · iPhone 17 Pro · iOS 26.4",
                                                     "type": "iPhone 17 Pro", "runtime": "iOS 26.4", "udid": "U"},
            "frames": [], "run_dir": None, "report": None, "cost_usd": 0.01}


class Script:
    def __init__(self, script, default="passed"):
        self.script = {name: list(verdicts) for name, verdicts in script.items()}
        self.default, self.calls, self.lock = default, [], threading.Lock()

    def bind_gate(self, gate):
        pass  # scheduler-only fake: no device I/O

    def __call__(self, check, record, *, label, video_name=None):
        with self.lock:
            self.calls.append((check.name, check.device, (record or {}).get("id"), label))
            queue = self.script.get(check.name)
            verdict = queue.pop(0) if queue else self.default
        if isinstance(verdict, tuple):
            return result(*verdict)
        return result(verdict, "assertion" if verdict == "failed" else None)


class Pool:
    def __init__(self, sims_per_type=4):
        self.free = {}
        self.sims_per_type = sims_per_type
        self.in_use, self.overlaps, self.most = set(), [], 0
        self.lock = threading.Lock()

    def acquire(self, device):
        with self.lock:
            free = self.free.setdefault(device, [f"{device}#{n}" for n in range(self.sims_per_type)])
            sim = free.pop(0)
            if sim in self.in_use:
                self.overlaps.append(sim)
            self.in_use.add(sim)
            self.most = max(self.most, len(self.in_use))
            return sim

    def release(self, device, sim):
        with self.lock:
            self.in_use.discard(sim)
            self.free[device].append(sim)


class SchedulingTests(unittest.TestCase):
    def test_parallel_slots_never_share_a_simulator_and_respect_the_limit(self):
        pool = Pool()
        busy = {"now": 0, "most": 0}
        lock = threading.Lock()

        def attempt(check, record, *, label, video_name=None):
            sim = pool.acquire(check.device)
            with lock:
                busy["now"] += 1
                busy["most"] = max(busy["most"], busy["now"])
            time.sleep(.02)
            with lock:
                busy["now"] -= 1
            pool.release(check.device, sim)
            return result("passed")
        entries = [suite_entry(f"check{n}") for n in range(6)]
        targets = [T.simulator("iPhone 17 Pro"), T.simulator("iPhone SE (3rd generation)")]
        suite = Suite(entries, targets, attempt=attempt, parallel=3, cap=8)
        doc = suite.run()
        self.assertEqual(pool.overlaps, [])
        self.assertEqual(busy["most"], 3)
        self.assertEqual(doc["summary"]["passed"], 12)
        self.assertEqual(doc["exitCode"], 0)

    def test_a_named_device_runs_one_check_at_a_time_beside_the_simulators(self):
        running = {"phone": 0, "most_phone": 0, "sims": 0, "together": False}
        lock = threading.Lock()

        def attempt(check, record, *, label, video_name=None):
            key = "phone" if record else "sims"
            with lock:
                running[key] += 1
                running["most_phone"] = max(running["most_phone"], running["phone"])
                running["together"] |= running["phone"] > 0 and running["sims"] > 0
            time.sleep(.03)
            with lock:
                running[key] -= 1
            return result("passed")
        gate = ADMIT
        targets = [T.simulator("iPhone 17 Pro"), T.Target("device:usb", "device", record=SUITE_PHONE)]
        Suite([suite_entry(f"c{n}") for n in range(4)], targets, attempt=LeasedFake(attempt), parallel=2,
              gate=gate, cap=8).run()
        self.assertEqual(running["most_phone"], 1)
        self.assertTrue(running["together"])

    def test_targets_set_the_device_and_the_label_names_the_attempt(self):
        script = Script({"paywall": ["failed", "passed"]})
        Suite([suite_entry("paywall")], [T.simulator("iPhone 16e", "iOS 26.4")], attempt=script, retries=2).run()
        self.assertEqual([call[1] for call in script.calls], ["iPhone 16e", "iPhone 16e"])
        self.assertEqual(script.calls[1][3], "paywall on iPhone 16e, iOS 26.4 (attempt 2 of 3)")


class VerdictTests(unittest.TestCase):
    def run_suite(self, script, entries=None, **kwargs):
        entries = entries or [suite_entry(name) for name in script]
        suite = Suite(entries, kwargs.pop("targets", [T.DEFAULT]), attempt=Script(script), **kwargs)
        return suite.run()

    def results(self, doc):
        return {check["name"]: check["results"][0] for check in doc["checks"]}

    def test_a_pass_on_retry_is_flaky_and_passes_unless_strict(self):
        doc = self.run_suite({"a": ["failed", "passed"], "b": ["passed"]})
        a = self.results(doc)["a"]
        self.assertEqual((a["status"], a["flaky"], a["attempts"]), ("passed", True, 2))
        self.assertEqual(a["flakyReason"]["class"], "assertion")
        self.assertEqual(doc["summary"]["flaky"], 1)
        self.assertEqual(doc["exitCode"], 0)
        self.assertEqual(self.run_suite({"a": ["failed", "passed"]}, strict=True)["exitCode"], 1)

    def test_retries_stop_at_the_limit_and_only_for_what_a_retry_can_fix(self):
        doc = self.run_suite({"a": ["failed"] * 5}, retries=2)
        self.assertEqual(self.results(doc)["a"]["attempts"], 3)
        self.assertEqual(doc["exitCode"], 1)
        for klass in ("usage", "model", "budget", "install", "stopped"):
            doc = self.run_suite({"a": [("couldnt_run", klass)] * 3}, retries=2)
            with self.subTest(klass=klass):
                self.assertEqual(self.results(doc)["a"]["attempts"], 1)
        for klass in sorted(RETRY_CLASSES):
            doc = self.run_suite({"a": [("couldnt_run", klass), "passed"]}, retries=1)
            with self.subTest(klass=klass):
                self.assertEqual(self.results(doc)["a"]["attempts"], 2)
        doc = self.run_suite({"a": ["needs_review", "passed"]})
        self.assertEqual(self.results(doc)["a"]["attempts"], 1)
        self.assertEqual(self.run_suite({"a": ["failed", "passed"]}, retries=0)["exitCode"], 1)

    def test_repeat_gives_a_pass_rate_and_fails_unless_every_run_passed(self):
        doc = self.run_suite({"a": ["passed", "failed", "passed", "passed", "passed"], "b": ["passed"] * 5},
                             repeat=5, retries=0)
        a, b = self.results(doc)["a"], self.results(doc)["b"]
        self.assertEqual((a["status"], a["flaky"], a["passRate"], a["attempts"]), ("failed", True, .8, 5))
        self.assertEqual((b["status"], b["flaky"], b["passRate"]), ("passed", False, 1.0))
        self.assertEqual([run["repetition"] for run in a["runs"]], [1, 2, 3, 4, 5])
        self.assertEqual(doc["exitCode"], 1)

    def test_unknown_action_stops_repetitions_and_retains_uncertain_evidence(self):
        scripted = Script({"a": [("failed", "assertion"), ("couldnt_run", "unknown_outcome"), "passed"]})
        doc = Suite([suite_entry("a")], [T.DEFAULT], attempt=scripted, repeat=3, retries=2).run()
        cell = doc["checks"][0]["results"][0]
        self.assertEqual(len(scripted.calls), 2)
        self.assertEqual((doc["stopReason"], doc["exitCode"]), ("unknown_outcome", 3))
        self.assertEqual((cell["status"], cell["safetyStop"], cell["reason"]["class"]),
                         ("couldnt_run", "unknown_outcome", "unknown_outcome"))

    def test_selected_failed_repetition_owns_its_report_and_replay_evidence(self):
        calls = []

        def attempt(check, record, *, label, video_name=None):
            index = len(calls) + 1
            calls.append(index)
            verdict = "failed" if index == 1 else "passed"
            return {**result(verdict, "assertion" if verdict == "failed" else None, run_id=f"run-{index}"),
                    "report": f"/tmp/run-{index}.html", "replay": {"mode": "replayed", "status": "ready",
                    "fixed": True, "saved": False, "approvedRevision": "a" * 64}}

        doc = Suite([suite_entry("a")], [T.DEFAULT], attempt=attempt, repeat=2, retries=0).run()
        cell = doc["checks"][0]["results"][0]
        self.assertEqual((cell["status"], cell["runId"], cell["report"]),
                         ("failed", "run-1", "/tmp/run-1.html"))
        self.assertEqual((cell["replay"]["fixed"], cell["replay"]["approvedRevision"]), (True, "a" * 64))

    def test_external_stop_after_only_attempt_has_stopped_exit(self):
        stop = threading.Event()

        def attempt(check, record, *, label, video_name=None):
            stop.set()
            return result("passed")

        doc = Suite([suite_entry("a")], [T.DEFAULT], attempt=attempt, stop=stop).run()
        self.assertEqual((doc["stopReason"], doc["exitCode"]), ("stopped", 130))

    def test_the_exit_code_order_failed_then_couldnt_run_then_needs_review(self):
        self.assertEqual(self.run_suite({"a": ["needs_review"], "b": [("couldnt_run", "usage")],
                                         "c": ["failed", "failed"]})["exitCode"], 1)
        self.assertEqual(self.run_suite({"a": ["needs_review"], "b": [("couldnt_run", "usage")]})["exitCode"], 3)
        self.assertEqual(self.run_suite({"a": ["needs_review"], "b": ["passed"]})["exitCode"], 2)

    def test_quarantined_checks_run_and_are_reported_but_never_fail_the_run(self):
        entries = [suite_entry("a", quarantined="Flaky on iOS 26.4"), suite_entry("b")]
        doc = self.run_suite({"a": ["failed", "failed"], "b": ["passed"]}, entries=entries)
        self.assertEqual(self.results(doc)["a"]["status"], "failed")
        self.assertTrue(self.results(doc)["a"]["quarantined"])
        self.assertEqual(doc["summary"], {"passed": 1, "failed": 0, "needsReview": 0, "couldntRun": 0, "flaky": 0,
                                          "quarantined": 1, "total": 2, "costUsd": 0.03})
        self.assertEqual(doc["exitCode"], 0)
        self.assertEqual(doc["checks"][0]["quarantineReason"], "Flaky on iOS 26.4")

    def test_a_broken_check_and_a_refused_target_couldnt_run_without_an_attempt(self):
        script = Script({})
        phone = {"id": "00008130-001A2B3C4D5E6F70", "udid": "00008130-001A2B3C4D5E6F70",
                 "kind": "usb", "name": "Studio iPhone", "wdaUrl": "http://127.0.0.1:8100"}
        entries = [suite_entry("broken", error="broken.yaml: version 2 is not supported"),
                   suite_entry("phone", device="Studio iPhone")]
        doc = Suite(entries, [T.DEFAULT], attempt=script, named_device=lambda name: phone).run()
        self.assertEqual(script.calls, [])
        broken, refused = (check["results"][0] for check in doc["checks"])
        self.assertEqual((broken["status"], broken["reason"]["class"], broken["attempts"]), ("couldnt_run", "usage", 0))
        self.assertEqual((refused["status"], refused["reason"]["class"]), ("couldnt_run", "usage"))
        self.assertEqual(doc["checks"][0]["error"], "broken.yaml: version 2 is not supported")
        self.assertEqual(doc["exitCode"], 3)

    def test_devices_are_named_after_the_simulator_each_run_reported(self):
        doc = self.run_suite({"a": ["passed"]}, targets=[T.DEFAULT, T.simulator("iPhone 16e")])
        self.assertEqual([device["name"] for device in doc["devices"]],
                         ["iPhone 17 Pro · iOS 26.4"])  # both ran on the fake's iPhone 17 Pro · iOS 26.4

    def test_one_check_without_a_matrix_is_one_target_one_progress_pair_and_one_result(self):
        lines = []
        script = Script({"a": [("couldnt_run", "device")]})
        suite = Suite([suite_entry("a")], [T.DEFAULT], attempt=script, retries=0, progress=lines.append)
        doc = suite.run()
        self.assertEqual(len(suite.targets), 1)
        self.assertEqual(len(doc["checks"][0]["results"]), 1)
        self.assertEqual(len(doc["devices"]), 1)
        self.assertEqual([line.splitlines()[0] for line in lines],
                         ["▸ a on Each check's simulator", "✗ couldn't run  a on iPhone 17 Pro, iOS 26.4  (1.0 s)"])
        self.assertEqual(doc["devices"][0]["name"], "iPhone 17 Pro · iOS 26.4")

    def test_a_matrix_of_two_simulators_gives_two_results(self):
        doc = self.run_suite({"a": ["passed"]}, targets=[T.simulator("iPhone 16e"), T.simulator("iPhone 17")])
        self.assertEqual(len(doc["checks"][0]["results"]), 2)

    def test_a_stop_leaves_the_rest_not_run(self):
        stop = threading.Event()

        def attempt(check, record, *, label, video_name=None):
            stop.set()
            return result("passed")
        doc = Suite([suite_entry("a"), suite_entry("b"), suite_entry("c")], [T.DEFAULT], attempt=attempt,
                    stop=stop).run()
        statuses = [check["results"][0]["status"] for check in doc["checks"]]
        self.assertEqual(statuses, ["passed", "couldnt_run", "couldnt_run"])
        self.assertEqual(doc["checks"][1]["results"][0]["reason"]["class"], "stopped")

    def test_ctrl_c_while_waiting_stops_the_runs_in_flight(self):
        started, aborted = threading.Event(), []

        def attempt(check, record, *, label, video_name=None):
            started.set()
            suite.stop.wait(5)
            return result("couldnt_run", "stopped")
        suite = Suite([suite_entry("a"), suite_entry("b")], [T.DEFAULT], attempt=attempt,
                      on_cancel=lambda: aborted.append(True))

        def interrupt_once(original=threading.Thread.join):
            calls = {"n": 0}

            def join(self, timeout=None):
                if self.name.startswith("mobster-test-") and calls["n"] == 0:
                    started.wait(5)
                    calls["n"] += 1
                    raise KeyboardInterrupt
                return original(self, timeout)
            return join
        with mock.patch.object(threading.Thread, "join", interrupt_once()):
            doc = suite.run()
        self.assertIsInstance(suite.interrupted, KeyboardInterrupt)
        self.assertEqual(aborted, [True])
        self.assertEqual([check["results"][0]["status"] for check in doc["checks"]], ["couldnt_run", "couldnt_run"])


PHONE_B = {"id": "00008130-00000000ABCD000B", "udid": "00008130-00000000ABCD000B", "kind": "usb",
           "name": "Work iPhone", "wdaUrl": "http://127.0.0.1:8101"}


PHONE_C = {"id": "00008130-00000000ABCD000C", "udid": "00008130-00000000ABCD000C", "kind": "usb",
           "name": "Desk iPhone", "wdaUrl": "http://127.0.0.1:8102"}


ADMIT = type("Gate", (), {"preflight": lambda self, check, record: (check, [], record)})()


class LeasedFake:
    """Scheduler fake that models an attempt owning the phone admission boundary."""

    def __init__(self, run):
        self.run = run

    def bind_gate(self, gate):
        pass

    def __call__(self, check, record, *, label, video_name=None):
        return self.run(check, record, label=label, video_name=video_name)


def phones(*records):
    return [T.Target(f"device:{record['id']}", "device", record=record) for record in records]


class SpreadTests(unittest.TestCase):
    def run_spread(self, attempt, count=6, targets=None, alive=None, **kwargs):
        targets = targets or phones(SUITE_PHONE, PHONE_B, PHONE_C)
        suite = Suite([suite_entry(f"c{n}") for n in range(count)], targets, attempt=LeasedFake(attempt),
                      spread=True, gate=ADMIT,
                      alive=alive or (lambda target: True), **kwargs)
        return suite.run()

    def test_each_check_runs_once_and_every_phone_takes_work_one_check_at_a_time(self):
        running, most, seen = {}, {}, {}
        lock = threading.Lock()

        def attempt(check, record, *, label, video_name=None):
            key = record["id"]
            with lock:
                running[key] = running.get(key, 0) + 1
                most[key] = max(most.get(key, 0), running[key])
                seen.setdefault(check.name, []).append(key)
            time.sleep(.03)
            with lock:
                running[key] -= 1
            return result("passed")
        doc = self.run_spread(attempt, count=9)
        self.assertEqual(sorted(seen), [f"c{n}" for n in range(9)])
        self.assertTrue(all(len(phones_) == 1 for phones_ in seen.values()))   # a check ran once
        self.assertEqual(set(most.values()), {1})                              # one at a time per phone
        self.assertEqual(len(most), 3)                                         # every phone took work
        self.assertEqual(doc["summary"]["passed"], 9)
        self.assertEqual(doc["summary"]["total"], 9)
        self.assertEqual([len(check["results"]) for check in doc["checks"]], [1] * 9)
        self.assertEqual(len(doc["devices"]), 3)
        self.assertEqual(doc["exitCode"], 0)

    def test_a_phone_that_drops_marks_its_check_couldnt_run_and_never_runs_it_again(self):
        calls = []

        def attempt(check, record, *, label, video_name=None):
            calls.append((check.name, record["id"]))
            if record["id"] == PHONE_B["id"]:
                time.sleep(.02)
                return result("couldnt_run", "wda", "WebDriverAgent stopped answering.")
            time.sleep(.03)
            return result("passed")
        doc = self.run_spread(attempt, count=8, targets=phones(SUITE_PHONE, PHONE_B),
                              alive=lambda target: target.record["id"] != PHONE_B["id"], retries=2)
        lost = [check for check in doc["checks"] if check["results"][0]["status"] == "couldnt_run"]
        self.assertEqual(len(lost), 1)
        cell = lost[0]["results"][0]
        self.assertEqual((cell["reason"]["class"], cell["attempts"]), ("device_lost", 1))
        self.assertIn("Work iPhone", cell["reason"]["message"])
        self.assertIn("didn't run it again", cell["reason"]["message"])
        self.assertEqual(sum(1 for name, _ in calls if name == lost[0]["name"]), 1)   # never retried, nowhere
        self.assertEqual([record for _, record in calls].count(PHONE_B["id"]), 1)     # takes no more checks
        self.assertEqual(doc["summary"]["passed"], 7)
        self.assertEqual(doc["exitCode"], 3)

    def test_a_phone_another_mobster_holds_hands_its_check_to_another_phone_untouched(self):
        calls = []

        def attempt(check, record, *, label, video_name=None):
            calls.append((check.name, record["id"]))
            if record["id"] == PHONE_B["id"]:
                return {**result("couldnt_run", "busy", "Work iPhone is in use by another Mobster app or command."),
                        "not_dispatched": True}
            time.sleep(.03)
            return result("passed")
        doc = self.run_spread(attempt, count=5, targets=phones(SUITE_PHONE, PHONE_B))
        self.assertEqual(doc["summary"]["passed"], 5)
        self.assertEqual([record for _, record in calls].count(PHONE_B["id"]), 1)
        self.assertTrue(all(check["results"][0]["attempts"] == 1 for check in doc["checks"]))
        self.assertEqual(doc["exitCode"], 0)

    def test_with_every_phone_gone_the_rest_couldnt_run_and_say_why(self):
        def attempt(check, record, *, label, video_name=None):
            return result("couldnt_run", "wda", "WebDriverAgent stopped answering.")
        doc = self.run_spread(attempt, count=5, targets=phones(SUITE_PHONE, PHONE_B), alive=lambda target: False)
        statuses = [check["results"][0]["status"] for check in doc["checks"]]
        self.assertEqual(statuses, ["couldnt_run"] * 5)
        classes = {check["results"][0]["reason"]["class"] for check in doc["checks"]}
        self.assertEqual(classes, {"device_lost"})
        never = [check["results"][0] for check in doc["checks"] if check["results"][0]["attempts"] == 0]
        self.assertEqual(len(never), 3)
        self.assertEqual({cell["device"] for cell in never}, {"Not run"})
        self.assertEqual(doc["exitCode"], 3)

    def test_without_spread_a_lost_phone_fails_its_remaining_checks_and_the_others_carry_on(self):
        def attempt(check, record, *, label, video_name=None):
            if record["id"] == PHONE_B["id"]:
                return result("couldnt_run", "wda", "WebDriverAgent stopped answering.")
            return result("passed")
        suite = Suite([suite_entry(f"c{n}")
                       for n in range(3)], phones(SUITE_PHONE, PHONE_B), attempt=LeasedFake(attempt), gate=ADMIT,
                      alive=lambda target: target.record["id"] != PHONE_B["id"], retries=2)
        doc = suite.run()
        on_b = [check["results"][1] for check in doc["checks"]]
        self.assertEqual([cell["attempts"] for cell in on_b], [1, 0, 0])
        self.assertEqual({cell["reason"]["class"] for cell in on_b}, {"device_lost"})
        self.assertEqual([check["results"][0]["status"] for check in doc["checks"]], ["passed"] * 3)

    def test_a_phone_that_still_answers_keeps_the_retry_it_always_had(self):
        script = Script({"c0": [("couldnt_run", "wda"), "passed"]})
        suite = Suite([suite_entry("c0")], phones(SUITE_PHONE), attempt=script, gate=ADMIT, alive=lambda target: True,
                      retries=1)
        doc = suite.run()
        self.assertEqual(doc["checks"][0]["results"][0]["attempts"], 2)

    def test_a_check_a_phone_refuses_is_couldnt_run_there_and_the_others_run(self):
        class Refuse:
            def preflight(self, check, record):
                if record["id"] == PHONE_B["id"]:
                    raise T.Refused("Instagram is from the App Store.", "not_developer_build")
                return check, [], record
        calls = []

        def attempt(check, record, *, label, video_name=None):
            calls.append(record["id"])
            return result("passed")
        suite = Suite([suite_entry(f"c{n}")
                       for n in range(4)], phones(SUITE_PHONE, PHONE_B), attempt=LeasedFake(attempt), spread=True,
                      gate=Refuse(), alive=lambda target: True)
        doc = suite.run()
        self.assertNotIn(PHONE_B["id"], calls)
        refused = [check["results"][0] for check in doc["checks"] if check["results"][0]["status"] != "passed"]
        self.assertTrue(all(cell["reason"]["class"] == "not_developer_build" for cell in refused))
        self.assertEqual(len(calls) + len(refused), 4)


class VisualManager(Manager):
    def __init__(self, image):
        super().__init__()
        self.image = image

    def screenshot_png(self, target, path):
        self._step("screenshot_png", Path(path).name)
        self.image.save(path, "PNG")
        return Path(path)


class RepeatPinEffectsTests(RunnerCase):
    def test_launch_only_repeat_refuses_changed_fixture_bytes_before_second_acquire(self):
        asset = self.root / ".mobster" / "fixtures" / "files" / "cart.json"
        asset.parent.mkdir(parents=True)
        asset.write_text('{"items": 1}')
        (asset.parent.parent / "cart.yaml").write_text(
            "files:\n  - from: files/cart.json\n    to: Documents/cart.json\n")
        check = check_from_dict({"version": 1, "name": "A fixture check", "app": {"bundle": BUNDLE},
                                 "fixture": ["cart"], "expect": [{"text": "Choose your plan"}]},
                                base_dir=self.root)
        check = fixtures.with_fixtures(check, self.root)
        manager = VisualManager(Image.new("RGB", (4, 4), "blue"))
        driver = Driver([PLANS])
        self.watch(driver, manager)
        attempts = E.Attempts(runs_dir=self.root / ".mobster" / "runs", keyless=True,
                              replay="off", simulators=manager)
        attempts.bind_repeat(check, None)
        with mock.patch.object(runner, "build_driver", lambda target, m, run_dir, mode_: driver), \
                mock.patch.object(fixtures, "seed_simulator", return_value=["file copied"]):
            first = attempts(check, None, label="first")
            acquired = manager.names().count("acquire")
            asset.write_text('{"items": 2}')
            second = attempts(check, None, label="second")
        self.assertEqual(first["verdict"], "passed")
        self.assertEqual((second["verdict"], second["reason"]["class"]), ("couldnt_run", "replay"))
        self.assertEqual(manager.names().count("acquire"), acquired)

    def test_repeat_refuses_minor_runtime_change_before_reset_or_launch(self):
        check = check_from_dict({"version": 1, "name": "A launch-only check",
                                 "app": {"bundle": BUNDLE}, "expect": [{"text": "Choose your plan"}]},
                                base_dir=self.root)

        class ChangingRuntime(VisualManager):
            runtime = "iOS 26.4"

            def acquire(self, *args, **kwargs):
                lease = super().acquire(*args, **kwargs)
                lease.target = SimpleNamespace(**{**vars(lease.target), "runtime": self.runtime})
                return lease

        manager = ChangingRuntime(Image.new("RGB", (4, 4), "blue"))
        driver = Driver([PLANS])
        self.watch(driver, manager)
        attempts = E.Attempts(runs_dir=self.root / ".mobster" / "runs", keyless=True,
                              replay="off", simulators=manager)
        attempts.bind_repeat(check, None)
        with mock.patch.object(runner, "build_driver", lambda target, m, run_dir, mode_: driver):
            first = attempts(check, None, label="first")
            effects = manager.names().count("reset"), manager.names().count("launch")
            manager.runtime = "iOS 26.5"
            second = attempts(check, None, label="second")
        self.assertEqual(first["verdict"], "passed")
        self.assertEqual((second["verdict"], second["reason"]["class"]), ("couldnt_run", "replay"))
        self.assertEqual((manager.names().count("reset"), manager.names().count("launch")), effects)


class InterruptedWorkerTests(unittest.TestCase):
    def test_interrupt_during_worker_start_and_again_during_drain_preserves_unknown(self):
        started, release = threading.Event(), threading.Event()

        def attempt(check, record, *, label, video_name=None):
            started.set()
            self.assertTrue(release.wait(3))
            return result("couldnt_run", "unknown_outcome", "action may have completed")

        suite = Suite([suite_entry("one")], [T.DEFAULT], attempt=attempt, retries=0)
        original_start, original_join = threading.Thread.start, threading.Thread.join
        joins = 0
        workers = []

        def interrupted_start(worker):
            workers.append(worker)
            original_start(worker)
            self.assertTrue(started.wait(3))
            raise KeyboardInterrupt()

        def interrupted_join(worker, timeout=None):
            nonlocal joins
            joins += 1
            if joins == 1:
                release.set()
                raise KeyboardInterrupt()
            return original_join(worker, timeout)

        with mock.patch.object(threading.Thread, "start", interrupted_start), \
                mock.patch.object(threading.Thread, "join", interrupted_join):
            doc = suite.run()
        suite.seal()
        self.assertIsInstance(suite.interrupted, KeyboardInterrupt)
        self.assertEqual((doc["stopReason"], doc["exitCode"]), ("unknown_outcome", 3))
        self.assertEqual(doc["checks"][0]["results"][0]["reason"]["class"], "unknown_outcome")
        self.assertFalse(any(worker.is_alive() for worker in workers))


if __name__ == "__main__":
    unittest.main()

import plistlib
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import harness_api
from mobile_agent.catalog import APPS
from mobile_agent.installed_apps import (
    InstalledApps,
    Inventory,
    ListingError,
    apps_view,
    list_apps,
    merge,
    parse_listing,
)
from mobile_agent.tests.fakes import Clock

UDID = "00008130-001A2B3C4D5E6F70"


def listing(*apps):
    return plistlib.dumps([dict(app) if isinstance(app, dict) else app for app in apps], fmt=plistlib.FMT_XML)


INSTAGRAM = {"CFBundleIdentifier": "com.burbn.instagram", "CFBundleDisplayName": "Instagram",
             "CFBundleName": "Instagram", "CFBundleShortVersionString": "402.0"}
TIKTOK = {"CFBundleIdentifier": "com.zhiliaoapp.musically", "CFBundleDisplayName": "TikTok"}
RUNNER = {"CFBundleIdentifier": "app.mobster.wda.runner.xctrunner", "CFBundleName": "WebDriverAgentRunner-Runner"}
SYSTEM = [{"CFBundleIdentifier": bundle} for bundle in ("com.apple.Preferences", "com.apple.mobilesafari")]


def completed(stdout=b"", code=0, stderr=b""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class ParseTests(unittest.TestCase):
    def test_reads_names_versions_and_falls_back_to_the_bundle_name(self):
        apps = parse_listing(listing(
            INSTAGRAM,
            {"CFBundleIdentifier": "com.example.noname", "CFBundleName": "Plain‮Name\n"},
            {"CFBundleIdentifier": "com.example.bare"},
            {"CFBundleIdentifier": "not a bundle", "CFBundleDisplayName": "Bad"},
            {"CFBundleDisplayName": "No bundle"},
            "not a dict",
            dict(INSTAGRAM, CFBundleDisplayName="Duplicate"),
        ))
        self.assertEqual([a["bundleId"] for a in apps],
                         ["com.example.bare", "com.burbn.instagram", "com.example.noname"])
        self.assertEqual(apps[1], {"bundleId": "com.burbn.instagram", "name": "Instagram", "version": "402.0"})
        self.assertEqual(apps[2]["name"], "Plain Name")
        self.assertEqual(apps[0]["name"], "com.example.bare")

    def test_ignores_text_before_the_plist(self):
        self.assertEqual(len(parse_listing(b"Total: 1 apps\n" + listing(INSTAGRAM))), 1)

    def test_bad_output_is_an_error_not_an_empty_phone(self):
        for output in (b"", b"ERROR: Could not connect to lockdownd", b"<?xml version='1.0'?><plist><array><dict>",
                       plistlib.dumps({"CFBundleIdentifier": "com.a.b"})):
            with self.subTest(output=output[:30]), self.assertRaises(ListingError):
                parse_listing(output)


class ListAppsTests(unittest.TestCase):
    def test_runs_only_the_list_command_with_a_timeout(self):
        runner = Mock(return_value=completed(listing(INSTAGRAM)))
        apps = list_apps("/opt/homebrew/bin/ideviceinstaller", UDID, "user", runner, timeout=7)
        self.assertEqual(apps[0]["bundleId"], "com.burbn.instagram")
        command = runner.call_args.args[0]
        self.assertEqual(command[:6], ["/opt/homebrew/bin/ideviceinstaller", "-u", UDID, "list", "--user", "--xml"])
        self.assertEqual(runner.call_args.kwargs["timeout"], 7)
        self.assertFalse({"install", "uninstall", "upgrade", "archive", "restore"} & set(command))
        self.assertIn("CFBundleIdentifier", command)
        list_apps("tool", UDID, "system", runner)
        self.assertIn("--system", runner.call_args.args[0])
        with self.assertRaises(ValueError):
            list_apps("tool", UDID, "all", runner)

    def test_missing_tool_device_or_pairing_is_a_reason(self):
        runner = Mock(return_value=completed(listing(INSTAGRAM)))
        with self.assertRaisesRegex(ListingError, "not installed"):
            list_apps(None, UDID, runner=runner)
        with self.assertRaisesRegex(ListingError, "no iPhone"):
            list_apps("tool", None, runner=runner)
        with self.assertRaisesRegex(ListingError, "no iPhone"):
            list_apps("tool", "../../etc", runner=runner)
        runner.assert_not_called()
        failures = {
            "not paired": completed(
                code=1, stderr=b"ERROR: Could not connect to lockdownd: Pairing dialog response pending (-19)"),
            "not connected": completed(code=1, stderr=b"No device found with udid 00008130."),
            "exit code 2": completed(code=2),
            "took longer": subprocess.TimeoutExpired("ideviceinstaller", 7),
            "could not run": FileNotFoundError(),
        }
        for reason, outcome in failures.items():
            with self.subTest(reason=reason), self.assertRaisesRegex(ListingError, reason):
                list_apps("tool", UDID, runner=Mock(side_effect=[outcome]))


class InventoryTests(unittest.TestCase):
    def make(self, runner, device=None, clock=None):
        device = device or Mock(return_value={"udid": UDID, "trusted": True})
        return InstalledApps(device, tool="ideviceinstaller", runner=runner, clock=clock or Clock())

    def test_an_unplugged_phone_on_wifi_is_read_through_coredevice(self):
        user = [{"bundleId": "org.mozilla.ios.Firefox", "name": "Firefox", "version": "2.4.1"}]
        system = [{"bundleId": "com.apple.mobilesafari", "name": "Safari", "version": "26.0"}]
        calls = []

        def read(udid, kind):
            calls.append((udid, kind))
            return user if kind == "user" else system
        runner = Mock()
        apps = InstalledApps(Mock(return_value=None), tool="ideviceinstaller", runner=runner, clock=Clock(),
                             over_wifi=lambda: UDID)
        with patch.object(harness_api.hooks(), "coredevice_apps", read):
            inventory = apps.refresh()
        self.assertEqual((inventory.udid, [a["bundleId"] for a in inventory.user], inventory.system),
                         (UDID, ["org.mozilla.ios.Firefox"], {"com.apple.mobilesafari"}))
        self.assertEqual(calls, [(UDID, "user"), (UDID, "system")])
        runner.assert_not_called()

    def test_wifi_that_cannot_list_leaves_the_apps_unknown_and_never_asks_usbmux(self):
        runner = Mock()

        def read(udid, kind):
            raise RuntimeError("device is locked")
        for over_wifi in (lambda: UDID, lambda: None, Mock(side_effect=OSError)):
            apps = InstalledApps(Mock(return_value=None), tool="ideviceinstaller", runner=runner, clock=Clock(),
                                 over_wifi=over_wifi)
            with self.subTest(over_wifi=over_wifi), self.assertLogs("mobster.apps", "WARNING"), \
                    patch.object(harness_api.hooks(), "coredevice_apps", read):
                self.assertIsNone(apps.refresh())
        runner.assert_not_called()

    def test_no_phone_untrusted_phone_or_no_tool_is_unknown_and_logged_once(self):
        runner = Mock()
        for device in (None, {"udid": UDID, "trusted": False}):
            apps = self.make(runner, device=Mock(return_value=device))
            with self.assertLogs("mobster.apps", "WARNING") as logs:
                self.assertIsNone(apps.refresh())
                self.assertIsNone(apps.refresh())
            self.assertEqual(len(logs.output), 1)
        runner.assert_not_called()
        missing = InstalledApps(Mock(return_value={"udid": UDID, "trusted": True}), tool="", runner=runner)
        with self.assertLogs("mobster.apps", "WARNING") as logs:
            self.assertIsNone(missing.refresh())
        self.assertIn("not installed", logs.output[0])

    def test_system_failure_keeps_the_user_apps(self):
        runner = Mock(side_effect=[completed(listing(INSTAGRAM)), completed(b"garbage")])
        with self.assertLogs("mobster.apps", "WARNING"):
            inventory = self.make(runner).refresh()
        self.assertEqual(len(inventory.user), 1)
        self.assertIsNone(inventory.system)

    def test_a_hiccup_keeps_the_last_listing_but_another_phone_never_sees_it(self):
        device = Mock(return_value={"udid": UDID, "trusted": True})
        runner = Mock(side_effect=[completed(listing(INSTAGRAM)), completed(listing(*SYSTEM)),
                                   subprocess.TimeoutExpired("x", 1)])
        apps = self.make(runner, device=device)
        first = apps.refresh()
        with self.assertLogs("mobster.apps", "WARNING"):
            self.assertIs(apps.refresh(), first)
        device.return_value = {"udid": "00008130-000A1B2C3D4E5F60", "trusted": True}
        runner.side_effect = [completed(code=1, stderr=b"lockdownd error")]
        with self.assertLogs("mobster.apps", "WARNING"):
            self.assertIsNone(apps.refresh())
        device.return_value = None
        apps.inventory = first
        with self.assertLogs("mobster.apps", "WARNING"):
            self.assertIsNone(apps.refresh())

    def test_snapshot_is_cached_for_a_minute_and_refreshed_in_the_background(self):
        clock = Clock()
        runner = Mock(side_effect=lambda *a, **k: completed(listing(INSTAGRAM)))
        apps = self.make(runner, clock=clock)
        first = apps.snapshot()
        self.assertEqual(first.user[0]["bundleId"], "com.burbn.instagram")
        calls = runner.call_count
        clock.now += 30
        self.assertIs(apps.snapshot(), first)
        self.assertEqual(runner.call_count, calls)
        clock.now += 31
        release, started = threading.Event(), threading.Event()

        def slow(*args, **kwargs):
            started.set()
            release.wait(5)
            return completed(listing(INSTAGRAM, TIKTOK))

        runner.side_effect = slow
        self.assertIs(apps.snapshot(), first)
        self.assertTrue(started.wait(5))
        release.set()
        apps.worker and apps.worker.join(5)
        self.assertEqual(len(apps.snapshot().user), 2)


def inventory(system=frozenset({"com.apple.Preferences", "com.apple.mobilesafari"})):
    user = parse_listing(listing(INSTAGRAM, TIKTOK, RUNNER))
    return Inventory(UDID, tuple(user), system, 0.0)


class MergeTests(unittest.TestCase):
    def test_catalog_then_every_found_app(self):
        apps = merge(APPS, inventory())
        self.assertEqual([a["id"] for a in apps[:len(APPS)]], [a["id"] for a in APPS])
        by_id = {a["id"]: a for a in apps}
        self.assertEqual(len(by_id), len(apps))
        instagram = by_id["com.burbn.instagram"]
        self.assertEqual(instagram,
                         {"id": "com.burbn.instagram", "name": "Instagram", "bundleId": "com.burbn.instagram",
                          "available": True, "installed": True, "availability": "installed",
                          "automationVerified": False, "foundOnPhone": True, "version": "402.0"})
        tiktok = by_id["tiktok"]
        self.assertTrue(tiktok["installed"] and tiktok["foundOnPhone"])
        self.assertTrue(by_id["settings"]["installed"])
        self.assertEqual(by_id["settings"]["availability"], "installed")
        self.assertNotIn("foundOnPhone", by_id["settings"])
        self.assertIs(by_id["podcasts"]["installed"], False)
        self.assertNotIn("app.mobster.wda.runner.xctrunner", by_id)
        self.assertEqual(sum(a["bundleId"] == "com.zhiliaoapp.musically" for a in apps), 1)

    def test_unknown_system_list_or_no_phone_claims_nothing(self):
        apps = {a["id"]: a for a in merge(APPS, inventory(system=None))}
        self.assertIsNone(apps["settings"]["installed"])
        self.assertTrue(apps["com.burbn.instagram"]["installed"])
        self.assertEqual(merge(APPS, None), APPS)
        self.assertIsNot(merge(APPS, None)[0], APPS[0])


class Parked:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


class RuntimeTests(unittest.TestCase):
    def runtime(self, root, snapshot=None):
        from mobile_agent.server import Runtime
        config = SimpleNamespace(socket=None, ax_socket=None, wda_url="http://127.0.0.1:8100", session=None,
                                 ocr=False, enable_live=True, port=8765, state_db=str(Path(root) / "r.sqlite3"),
                                 data_dir=root)
        runtime = Runtime(config)
        self.addCleanup(runtime.close)
        runtime.installed_apps = Mock(snapshot=Mock(return_value=snapshot))
        return runtime

    def test_icons_are_looked_up_only_for_apps_the_phone_lists(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = self.runtime(root, inventory())
            self.assertEqual(runtime.icon_bundles(), {"com.burbn.instagram", "com.zhiliaoapp.musically",
                                                      "com.apple.Preferences", "com.apple.mobilesafari"})
            self.assertNotIn("com.apple.podcasts", runtime.icon_bundles())
            runtime.installed_apps.snapshot.return_value = inventory(system=None)
            self.assertNotIn("com.apple.Preferences", runtime.icon_bundles())
            runtime.installed_apps = None
            self.assertEqual(runtime.icon_bundles(), set())
            self.assertIsNone(runtime.icons.get("com.apple.MobileSMS"))

    def test_api_apps_says_where_the_apps_come_from(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = self.runtime(root, inventory())
            runtime.installed_apps.kept = Mock(return_value=None)
            payload = runtime.apps_payload()
            self.assertEqual(payload["inventory"]["state"], "live")
            self.assertNotIn("podcasts", {app["id"] for app in payload["apps"]})
            self.assertIn("podcasts", {app["id"] for app in runtime.apps()})
            runtime.installed_apps = None
            self.assertEqual(runtime.apps_payload(), {"apps": APPS, "inventory": {"state": "unknown", "at": None}})

    def test_a_found_app_runs_by_bundle(self):
        import os

        from mobile_agent.drivers import WDA
        from mobile_agent.journal import Lease
        from mobile_agent.server import Runtime
        from mobile_agent.tests.test_engines import CLEAN, Script
        seen = {}

        class FakeAgent:
            def run(self, goal, **kwargs):
                seen["goal"] = goal
                return {"status": "completed", "answer": "Opened.", "steps": 1, "actions": 0, "elapsed": 1.0}

        def build_frontier(driver, client, **kwargs):
            seen.update(kwargs)
            return FakeAgent()

        with tempfile.TemporaryDirectory() as root, patch("mobile_agent.server.threading.Thread", Parked), \
                patch("mobile_agent.journal.Lease.device", side_effect=lambda _: Lease(Path(root) / "d.lock")), \
                patch.dict(os.environ, {**CLEAN, "ANTHROPIC_API_KEY": "sk-ant-test-000000000000"}, clear=True):
            runtime = self.runtime(root, inventory())
            runtime.status = Mock(return_value={"live_enabled": True, "helper_configured": False})
            runtime.wda_session = Mock(return_value="s")
            with self.assertRaisesRegex(ValueError, "Podcasts isn't installed"):
                runtime.create("podcasts", "Play something", "live")
            with self.assertRaisesRegex(ValueError, "catalog"):
                runtime.create("com.example.notonphone", "Open it", "live")
            run = runtime.create("com.burbn.instagram", "Open my profile", "live")
            self.assertEqual(run.app["bundleId"], "com.burbn.instagram")
            driver = Mock(spec=WDA)
            driver.last_image = None
            built = {}
            with patch.object(Runtime, "smart_client", lambda _runtime: Script([[("DONE", None, None)]], [])), \
                    patch("mobile_agent.runtime_driver.build_target_driver",
                          side_effect=lambda **k: built.update(k) or driver), \
                    patch("mobile_agent.runtime_driver.prepare_wda_phone"), \
                    patch("mobile_agent.runtime_driver.attach_frame_clock"), \
                    patch("mobile_agent.engines.build_frontier", build_frontier):
                runtime.work(run)
            if run.lease:
                run.lease.close()
            driver.call.assert_any_call("POST", "/wda/apps/activate", {"bundleId": "com.burbn.instagram"})
            self.assertEqual(built["expected_bundle"], "com.burbn.instagram")
            self.assertEqual(seen["goal"], "In Instagram: Open my profile")
            self.assertIn("com.burbn.instagram", seen["apps"])
            runtime.active_runs.clear()
            second = runtime.create("safari", "Compare", "live", allowed_bundles=["com.burbn.instagram"])
            self.assertEqual(second.allowed_bundles, {"com.apple.mobilesafari", "com.burbn.instagram"})
            if second.lease:
                second.lease.close()

    def test_saved_tasks_accept_a_found_app(self):
        from mobile_agent.journal import Journal
        from mobile_agent.workflows.service import Workflows
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root) / "runs.sqlite3")
            self.addCleanup(journal.close)
            runtime = SimpleNamespace(journal=journal, lock=threading.RLock(), runs={},
                                      config=SimpleNamespace(port=8765),
                                      apps=lambda: merge(APPS, inventory()))
            workflows = Workflows(runtime)
            saved = workflows.create({"name": "Profile", "appId": "com.burbn.instagram", "goal": "Open my profile"})
            self.assertEqual(saved["appId"], "com.burbn.instagram")
            with self.assertRaises(ValueError):
                workflows.create({"name": "Other", "appId": "com.example.notonphone", "goal": "Open"})


class ManagedRuntimeTests(unittest.TestCase):
    def test_the_app_launch_needs_no_extension(self):
        from mobile_agent import cli_parser
        from mobile_agent.server import Runtime
        with tempfile.TemporaryDirectory() as root, patch.object(harness_api.hooks(), "cli", []), \
                patch("mobile_agent.server.DeviceManager"), patch("mobile_agent.server.SetupService"), \
                patch("mobile_agent.server.threading.Thread", Parked):
            config = cli_parser.build_parser().parse_args(
                ["serve", "--port", "8765", "--exit-with-parent", "--env-file", str(Path(root) / "agent.env"),
                 "--enable-live", "--manage-device", "--data-dir", root])
            self.assertFalse(hasattr(config, "socket"))
            config.state_db = str(Path(root) / "r.sqlite3")
            runtime = Runtime(config)
            self.addCleanup(runtime.close)
        self.assertIsInstance(runtime.installed_apps, InstalledApps)


class AppsViewTests(unittest.TestCase):
    def installed(self, root, snapshot):
        from mobile_agent.installed_apps import InventoryCache
        apps = InstalledApps(lambda: None, cache=InventoryCache(Path(root) / "installed-apps"))
        apps.snapshot = Mock(return_value=snapshot)
        return apps

    def test_a_read_phone_shows_only_what_it_has_and_keeps_its_own_apps(self):
        with tempfile.TemporaryDirectory() as root:
            read = inventory()
            view = apps_view(APPS, self.installed(root, Inventory(UDID, read.user, read.system, 0.0, 1_791_500_000.0)))
            self.assertEqual(view["inventory"], {"state": "live", "at": 1_791_500_000_000})
            ids = {app["id"] for app in view["apps"]}
            self.assertNotIn("podcasts", ids)
            self.assertIn("settings", ids)
            self.assertIn("com.burbn.instagram", ids)
            self.assertTrue(all(app.get("installed") is not False for app in view["apps"]))

    def test_an_unplugged_phone_shows_its_kept_apps_as_of_when_they_were_read(self):
        with tempfile.TemporaryDirectory() as root:
            from mobile_agent.installed_apps import InventoryCache
            cache = InventoryCache(Path(root) / "installed-apps")
            cache.save(Inventory(UDID, inventory().user, inventory().system, 5.0, 1_791_400_000.0))
            view = apps_view(APPS, self.installed(root, None))
            self.assertEqual(view["inventory"], {"state": "cached", "at": 1_791_400_000_000})
            self.assertIn("com.burbn.instagram", {app["id"] for app in view["apps"]})
            self.assertNotIn("podcasts", {app["id"] for app in view["apps"]})
            self.assertEqual(json_load(Path(root) / "installed-apps" / "last.json"), {"udid": UDID})
            self.assertEqual(set(json_load(Path(root) / "installed-apps" / f"{UDID}.json")),
                             {"udid", "wall", "user", "system"})

    def test_no_phone_ever_read_is_none_and_no_way_to_read_one_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(apps_view(APPS, self.installed(root, None))["inventory"], {"state": "none", "at": None})
        unknown = apps_view(APPS, None)
        self.assertEqual(unknown["inventory"], {"state": "unknown", "at": None})
        self.assertEqual(unknown["apps"], APPS)

    def test_a_reading_is_kept_on_disk_and_a_bad_file_reads_as_none(self):
        with tempfile.TemporaryDirectory() as root:
            from mobile_agent.installed_apps import InventoryCache
            device = {"udid": UDID, "trusted": True}
            runner = Mock(side_effect=[completed(listing(INSTAGRAM)), completed(listing(*SYSTEM))])
            apps = InstalledApps(lambda: device, tool="/opt/homebrew/bin/ideviceinstaller", runner=runner,
                                 cache=InventoryCache(Path(root) / "kept"), wall=lambda: 1_791_000_000.0)
            apps.refresh()
            kept = apps.kept()
            self.assertEqual((kept.udid, kept.wall, [app["bundleId"] for app in kept.user]),
                             (UDID, 1_791_000_000.0, ["com.burbn.instagram"]))
            self.assertIn("com.apple.Preferences", kept.system)
            (Path(root) / "kept" / f"{UDID}.json").write_text("{not json")
            self.assertIsNone(apps.kept())


def json_load(path):
    import json
    return json.loads(Path(path).read_text())


if __name__ == "__main__":
    unittest.main()

"""Any app on the USB iPhone: the ideviceinstaller listing, the /api/apps merge, and runs for found apps."""

import plistlib
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import installed_apps
from mobile_agent.catalog import APPS, app_label
from mobile_agent.installed_apps import InstalledApps, Inventory, ListingError, list_apps, merge, parse_listing

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
        self.assertEqual([a["bundleId"] for a in apps], ["com.example.bare", "com.burbn.instagram", "com.example.noname"])
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
            "not paired": completed(code=1, stderr=b"ERROR: Could not connect to lockdownd: Pairing dialog response pending (-19)"),
            "not connected": completed(code=1, stderr=b"No device found with udid 00008130."),
            "exit code 2": completed(code=2),
            "took longer": subprocess.TimeoutExpired("ideviceinstaller", 7),
            "could not run": FileNotFoundError(),
        }
        for reason, outcome in failures.items():
            with self.subTest(reason=reason), self.assertRaisesRegex(ListingError, reason):
                list_apps("tool", UDID, runner=Mock(side_effect=[outcome]))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class InventoryTests(unittest.TestCase):
    def make(self, runner, device=None, clock=None):
        device = device or Mock(return_value={"udid": UDID, "trusted": True})
        return InstalledApps(device, tool="ideviceinstaller", runner=runner, clock=clock or Clock())

    def test_reads_user_and_system_apps(self):
        runner = Mock(side_effect=[completed(listing(INSTAGRAM, TIKTOK)), completed(listing(*SYSTEM))])
        inventory = self.make(runner).refresh()
        self.assertEqual([a["bundleId"] for a in inventory.user], ["com.burbn.instagram", "com.zhiliaoapp.musically"])
        self.assertEqual(inventory.system, {"com.apple.Preferences", "com.apple.mobilesafari"})

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
        first = apps.snapshot()  # the first read waits for the phone
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
        self.assertIs(apps.snapshot(), first)  # stale, returned at once while the refresh runs
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
        self.assertEqual(instagram, {"id": "com.burbn.instagram", "name": "Instagram", "bundleId": "com.burbn.instagram",
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
    """A thread that never runs: no worker, no device watcher."""

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

    def test_apps_lists_the_phone_apps_in_wda_mode(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = self.runtime(root, inventory())
            apps = runtime.apps()
            self.assertIn("com.burbn.instagram", {a["id"] for a in apps})
            self.assertEqual(app_label("com.burbn.instagram"), "Instagram (com.burbn.instagram)")
            runtime.installed_apps = None
            self.assertEqual(runtime.apps(), APPS)

    def test_a_found_app_runs_by_bundle_and_only_in_that_app(self):
        from mobile_agent.drivers import WDA
        from mobile_agent.journal import Lease
        seen = {}

        class FakeAgent:
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)

            def run(self, goal, **kwargs):
                seen["goal"] = goal
                return {"event": "result", "status": "completed_unverified", "independently_verified": False}

        with tempfile.TemporaryDirectory() as root, patch("mobile_agent.server.threading.Thread", Parked), \
                patch("mobile_agent.server.Lease.device", side_effect=lambda _: Lease(Path(root) / "d.lock")):
            runtime = self.runtime(root, inventory())
            runtime.status = Mock(return_value={"live_enabled": True, "helper_configured": False})
            runtime.wda_session = Mock(return_value="s")
            with self.assertRaisesRegex(ValueError, "Podcasts isn't installed"):
                runtime.create("podcasts", "Play something", "live")
            with self.assertRaisesRegex(ValueError, "catalog"):
                runtime.create("com.example.notonphone", "Open it", "live")
            run = runtime.create("com.burbn.instagram", "Open my profile", "live", engine="fast")
            self.assertEqual(run.app["bundleId"], "com.burbn.instagram")
            self.assertIsNone(run.allowed_bundles)
            driver = Mock(spec=WDA)
            driver.last_image = None
            built = {}
            with patch("mobile_agent.server.build_models", return_value=(Mock(), None)), \
                    patch("mobile_agent.server.warm_clients"), \
                    patch("mobile_agent.server.build_target_driver", side_effect=lambda **k: built.update(k) or driver), \
                    patch("mobile_agent.server.prepare_wda_phone"), patch("mobile_agent.server.attach_frame_clock"), \
                    patch("mobile_agent.server.Agent", FakeAgent):
                runtime.work(run)
            if run.lease:
                run.lease.close()
            # Launched by bundle ID, observed only in that app, and no other app may be opened.
            driver.call.assert_any_call("POST", "/wda/apps/activate", {"bundleId": "com.burbn.instagram"})
            self.assertEqual(built["expected_bundle"], "com.burbn.instagram")
            self.assertEqual(seen["launch_bundle"], "com.burbn.instagram")
            self.assertIsNone(seen["allowed_bundles"])
            self.assertEqual(seen["goal"], "In Instagram: Open my profile")
            # A multi-app task may add a found app, too.
            runtime.active_runs.clear()
            second = runtime.create("safari", "Compare", "live", allowed_bundles=["com.burbn.instagram"])
            self.assertEqual(second.allowed_bundles, {"com.apple.mobilesafari", "com.burbn.instagram"})
            if second.lease:
                second.lease.close()

    def test_the_agent_refuses_to_launch_another_app(self):
        from mobile_agent.agent import Agent
        agent = Agent(Mock(), Mock(), launch_bundle="com.burbn.instagram")
        self.assertIsNone(agent.allowed_bundles)

    def test_saved_tasks_accept_a_found_app(self):
        from mobile_agent.journal import Journal
        from mobile_agent.workflows import Workflows
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root) / "runs.sqlite3")
            self.addCleanup(journal.close)
            runtime = SimpleNamespace(journal=journal, lock=threading.RLock(), runs={}, config=SimpleNamespace(port=8765),
                                      apps=lambda: merge(APPS, inventory()))
            workflows = Workflows(runtime)
            saved = workflows.create({"name": "Profile", "appId": "com.burbn.instagram", "goal": "Open my profile"})
            self.assertEqual(saved["appId"], "com.burbn.instagram")
            with self.assertRaises(ValueError):
                workflows.create({"name": "Other", "appId": "com.example.notonphone", "goal": "Open"})


class ManagedRuntimeTests(unittest.TestCase):
    def test_the_desktop_runtime_reads_the_chosen_phone(self):
        from mobile_agent.server import Runtime
        with tempfile.TemporaryDirectory() as root, patch("mobile_agent.server.DeviceManager") as manager, \
                patch("mobile_agent.server.SetupService"), patch("mobile_agent.server.threading.Thread", Parked):
            config = SimpleNamespace(socket=None, ax_socket=None, wda_url="http://127.0.0.1:8100", session=None,
                                     ocr=False, enable_live=False, port=8765, state_db=str(Path(root) / "r.sqlite3"),
                                     manage_device=True, data_dir=root, env_file=str(Path(root) / "agent.env"))
            runtime = Runtime(config)
            self.addCleanup(runtime.close)
        self.assertIsInstance(runtime.installed_apps, InstalledApps)
        self.assertIs(runtime.installed_apps.device, manager.return_value.device)
        self.assertTrue(str(runtime.icons.directory).startswith(root))

    def test_the_app_launch_needs_no_extension(self):
        # The Mac app's serve arguments, parsed without the optional extension (which adds --socket): the
        # signed app has no extension, and reading config.socket crashed every launch (27 Sep 2026).
        from mobile_agent import __main__ as cli
        from mobile_agent.extensions import Hooks
        from mobile_agent.server import Runtime
        with tempfile.TemporaryDirectory() as root, patch.object(cli, "load_extensions", return_value=Hooks()), \
                patch("mobile_agent.server.DeviceManager"), patch("mobile_agent.server.SetupService"), \
                patch("mobile_agent.server.threading.Thread", Parked):
            config = cli.build_parser().parse_args(
                ["serve", "--port", "8765", "--exit-with-parent", "--env-file", str(Path(root) / "agent.env"),
                 "--enable-live", "--manage-device", "--data-dir", root])
            self.assertFalse(hasattr(config, "socket"))
            config.state_db = str(Path(root) / "r.sqlite3")
            runtime = Runtime(config)
            self.addCleanup(runtime.close)
        self.assertIsInstance(runtime.installed_apps, InstalledApps)


if __name__ == "__main__":
    unittest.main()

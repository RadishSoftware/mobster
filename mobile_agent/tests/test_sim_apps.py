"""The app under test: app_info (including a device build) and the reset's container path guard."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mobile_agent.sim import SimError, SimulatorManager, apps
from mobile_agent.tests.test_sim_fakes import make_app

UDID = "0A1B2C3D-0000-4000-8000-000000000001"
OTHER = "0A1B2C3D-0000-4000-8000-000000000002"


class AppInfoTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.manager = SimulatorManager(data_dir=self.folder / "data")

    def test_a_simulator_build(self):
        app = make_app(self.folder, CFBundleDisplayName="Daybreak Beta")
        info = self.manager.app_info(str(app))
        self.assertEqual((info.bundle_id, info.name, info.version),
                         ("dev.mobster.daybreak", "Daybreak Beta", "1.0 (1)"))
        self.assertEqual(info.path, str(app.resolve()))

    def test_name_falls_back_to_the_bundle_name_then_the_id(self):
        self.assertEqual(self.manager.app_info(str(make_app(self.folder))).name, "Daybreak")
        app = make_app(self.folder / "b", CFBundleName="")
        self.assertEqual(self.manager.app_info(str(app)).name, "dev.mobster.daybreak")

    def test_a_device_build_is_refused_with_the_simulator_build_line(self):
        app = make_app(self.folder, platforms=("iPhoneOS",))
        with self.assertRaises(SimError) as caught:
            self.manager.app_info(str(app))
        self.assertEqual(caught.exception.kind, "install")
        self.assertTrue(str(caught.exception).startswith("This .app was built for a device. Build it for the iOS "
                                                         "Simulator: xcodebuild"))
        self.assertIn("-destination 'generic/platform=iOS Simulator'", str(caught.exception))
        self.assertTrue(caught.exception.fix)

    def test_without_supported_platforms_the_platform_name_decides(self):
        simulator = make_app(self.folder / "a", platforms=None, DTPlatformName="iphonesimulator")
        self.assertEqual(self.manager.app_info(str(simulator)).bundle_id, "dev.mobster.daybreak")
        for folder, extra in (("b", {"DTPlatformName": "iphoneos"}), ("c", {})):
            with self.subTest(extra=extra), self.assertRaises(SimError):
                self.manager.app_info(str(make_app(self.folder / folder, platforms=None, **extra)))

    def test_not_an_app(self):
        (self.folder / "Empty.app").mkdir()
        (self.folder / "Broken.app").mkdir()
        (self.folder / "Broken.app" / "Info.plist").write_text("not a plist")
        for path in (self.folder / "missing.app", self.folder / "Empty.app", self.folder / "Broken.app"):
            with self.subTest(path=path.name), self.assertRaises(SimError) as caught:
                self.manager.app_info(str(path))
            self.assertEqual(caught.exception.kind, "install")
        with self.assertRaises(SimError):
            self.manager.app_info(str(make_app(self.folder / "d", bundle="")))

    def test_version_text(self):
        self.assertEqual(apps.version_text({"CFBundleShortVersionString": "2.1", "CFBundleVersion": "40"}), "2.1 (40)")
        self.assertEqual(apps.version_text({"CFBundleShortVersionString": "2.1", "CFBundleVersion": "2.1"}), "2.1")
        self.assertEqual(apps.version_text({"CFBundleVersion": "40"}), "40")
        self.assertEqual(apps.version_text({}), "")


class LinkTests(unittest.TestCase):
    def test_url_schemes_as_declared_in_order_without_duplicates(self):
        info = {"CFBundleURLTypes": [{"CFBundleURLSchemes": ["daybreak", " Daybreak-Beta ", ""]},
                                     {"CFBundleURLName": "no schemes"}, "junk", {"CFBundleURLSchemes": ["daybreak"]}]}
        self.assertEqual(apps.url_schemes(info), ["daybreak", "Daybreak-Beta"])
        self.assertEqual(apps.url_schemes({}), [])

    def test_scheme_approvals_read_from_the_simulators_own_preferences(self):
        import plistlib
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(apps, "devices_root", return_value=Path(folder)):
            udid = "0A1B2C3D-0000-4000-8000-000000000001"
            self.assertEqual(apps.scheme_approvals(udid), {})
            preferences = Path(folder) / udid / "data" / "Library" / "Preferences"
            preferences.mkdir(parents=True)
            path = preferences / "com.apple.launchservices.schemeapproval.plist"
            path.write_bytes(b"not a plist")
            self.assertEqual(apps.scheme_approvals(udid), {})
            with open(path, "wb") as stream:
                plistlib.dump({"com.apple.CoreSimulator.CoreSimulatorBridge-->daybreak": "dev.mobster.daybreak"},
                              stream)
            self.assertEqual(apps.scheme_approvals(udid),
                             {"com.apple.CoreSimulator.CoreSimulatorBridge-->daybreak": "dev.mobster.daybreak"})


class ContainerGuardTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.devices = self.root / "Devices"
        patcher = mock.patch.object(apps, "devices_root", return_value=self.devices)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.container = self.devices / UDID / "data" / "Containers" / "Data" / "Application" / "APP"
        self.theirs = self.devices / OTHER / "data" / "Containers" / "Data" / "Application" / "APP"
        for container in (self.container, self.theirs):
            for name in apps.EMPTIED:
                (container / name).mkdir(parents=True)

    def test_a_container_inside_the_simulator_passes(self):
        self.assertEqual(apps.guard_container(str(self.container), UDID), self.container)

    def test_another_simulators_container_is_refused(self):
        with self.assertRaises(SimError) as caught:
            apps.guard_container(str(self.theirs), UDID)
        self.assertEqual(caught.exception.kind, "simulator")
        self.assertIn("outside this simulator's folder", str(caught.exception))

    def test_a_symlink_or_dotdot_that_escapes_is_refused(self):
        link = self.devices / UDID / "data" / "Containers" / "Data" / "Application" / "LINK"
        link.symlink_to(self.theirs)
        escape = str(self.devices / UDID / "data" / ".." / ".." / OTHER / "data")
        for path in (str(link), escape, str(self.devices / UDID), "", "/tmp"):
            with self.subTest(path=path), self.assertRaises(SimError):
                apps.guard_container(path, UDID)

    def test_a_missing_simulator_folder_is_refused(self):
        with self.assertRaises(SimError) as caught:
            apps.guard_container(str(self.container), "0A1B2C3D-0000-4000-8000-00000000FFFF")
        self.assertIn("is missing", str(caught.exception))

    def test_empty_keeps_the_folders_and_never_follows_links(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        (self.container / "Documents" / "note.txt").write_text("x")
        (self.container / "Library" / "Preferences").mkdir()
        (self.container / "Library" / "Preferences" / "dev.mobster.daybreak.plist").write_text("x")
        (self.container / "tmp" / "outside-link").symlink_to(outside)
        (self.container / "SystemData").mkdir()
        (self.container / "SystemData" / "kept").write_text("x")
        removed = apps.empty_container(str(self.container), UDID)
        self.assertEqual(removed, 3)
        for name in apps.EMPTIED:
            self.assertTrue((self.container / name).is_dir())
            self.assertEqual(os.listdir(self.container / name), [])
        self.assertEqual((outside / "keep.txt").read_text(), "keep")
        self.assertTrue((self.container / "SystemData" / "kept").exists())

    def test_a_linked_documents_folder_is_refused(self):
        documents = self.container / "Documents"
        documents.rmdir()
        documents.symlink_to(self.root)
        with self.assertRaises(SimError):
            apps.empty_container(str(self.container), UDID)


if __name__ == "__main__":
    unittest.main()

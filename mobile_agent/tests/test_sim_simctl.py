"""simctl's JSON, the default device type and runtime, and the names Mobster gives its simulators."""

import unittest

from mobile_agent.sim import SimError, simctl
from mobile_agent.tests.test_sim_fakes import DEVICE_TYPES, RUNTIMES, FakeSimctl

LIST = {"devicetypes": DEVICE_TYPES, "runtimes": RUNTIMES, "devices": {
    "com.apple.CoreSimulator.SimRuntime.iOS-26-4": [
        {"udid": "0A1B2C3D-0000-4000-8000-000000000001", "name": "Mobster · iPhone 17 Pro · iOS 26.4",
         "state": "Booted", "isAvailable": True,
         "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro",
         "dataPath": "/Users/example/Library/Developer/CoreSimulator/Devices/"
                     "0A1B2C3D-0000-4000-8000-000000000001/data"},
        {"udid": "0A1B2C3D-0000-4000-8000-000000000002", "name": "Another simulator", "state": "Shutdown",
         "isAvailable": False, "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-Air"},
        {"name": "no udid"}, "not a dict"],
    "com.apple.CoreSimulator.SimRuntime.watchOS-26-4": []}}


class ParsingTests(unittest.TestCase):
    def test_runtimes_keep_ios_only_with_versions_and_device_types(self):
        runtimes = simctl.parse_runtimes(LIST)
        self.assertEqual([runtime.name for runtime in runtimes], ["iOS 26.4", "iOS 18.6", "iOS 27.0"])
        self.assertEqual(runtimes[0].version, (26, 4))
        self.assertTrue(runtimes[0].available)
        self.assertFalse(runtimes[2].available)
        self.assertIn("iPhone 17 Pro", runtimes[0].device_types)

    def test_device_types(self):
        kinds = simctl.parse_device_types(LIST)
        self.assertEqual(kinds[0].name, "iPhone 17 Pro")
        self.assertEqual(kinds[0].family, "iPhone")
        self.assertEqual(kinds[0].min_runtime, 1703936)
        self.assertEqual(kinds[-1].family, "iPad")

    def test_devices_skip_malformed_entries(self):
        devices = simctl.parse_devices(LIST)
        self.assertEqual([device.udid for device in devices],
                         ["0A1B2C3D-0000-4000-8000-000000000001", "0A1B2C3D-0000-4000-8000-000000000002"])
        first, second = devices
        self.assertEqual((first.state, first.runtime), ("Booted", "com.apple.CoreSimulator.SimRuntime.iOS-26-4"))
        self.assertTrue(first.data_path.endswith("/data"))
        self.assertFalse(second.available)

    def test_empty_and_missing_sections(self):
        self.assertEqual(simctl.parse_runtimes({}), [])
        self.assertEqual(simctl.parse_devices(None), [])
        self.assertEqual(simctl.parse_device_types({"devicetypes": None}), [])

    def test_the_fake_lists_what_real_simctl_lists(self):
        fake = FakeSimctl("/nonexistent")
        self.assertEqual(simctl.parse_runtimes(fake.listing())[0].name, "iOS 26.4")

    def test_created_udid_and_groups(self):
        self.assertEqual(simctl.parse_created("0a1b2c3d-0000-4000-8000-000000000009\n"),
                         "0A1B2C3D-0000-4000-8000-000000000009")
        self.assertIsNone(simctl.parse_created("An error was encountered"))
        self.assertEqual(simctl.parse_groups("group.dev.example.shared\t/path/to/Shared Group/X\n\n"),
                         [("group.dev.example.shared", "/path/to/Shared Group/X")])
        self.assertEqual(simctl.parse_groups(""), [])

    def test_last_line_prefers_stderr(self):
        result = simctl.Result(1, "out 1\nout 2\n", "\nerror: the reason\n  \n")
        self.assertEqual(result.last_line(), "error: the reason")
        self.assertEqual(simctl.Result(1, "only out\n", "").last_line(), "only out")
        self.assertFalse(result.ok)

    def test_run_never_raises(self):
        result = simctl.run(["/nonexistent/tool"])
        self.assertIsNone(result.code)
        self.assertIn("could not run", result.err)


class DefaultTests(unittest.TestCase):
    def setUp(self):
        self.runtimes = simctl.parse_runtimes(LIST)
        self.kinds = simctl.parse_device_types(LIST)

    def test_newest_available_runtime(self):
        self.assertEqual(simctl.choose_runtime(self.runtimes).name, "iOS 26.4")  # 27.0 is not available

    def test_runtime_by_name_version_prefix_or_identifier(self):
        for wanted in ("iOS 18.6", "ios 18.6", "18.6", "iOS 18", "com.apple.CoreSimulator.SimRuntime.iOS-18-6"):
            with self.subTest(wanted=wanted):
                self.assertEqual(simctl.choose_runtime(self.runtimes, wanted).name, "iOS 18.6")

    def test_missing_runtime_names_the_installed_ones_and_the_download(self):
        with self.assertRaises(SimError) as caught:
            simctl.choose_runtime(self.runtimes, "iOS 27.0")
        self.assertEqual(caught.exception.kind, "environment")
        self.assertIn("Installed: iOS 26.4, iOS 18.6", str(caught.exception))
        self.assertEqual(caught.exception.fix, "xcodebuild -downloadPlatform iOS")
        with self.assertRaises(SimError) as caught:
            simctl.choose_runtime([])
        self.assertEqual(caught.exception.kind, "environment")

    def test_iphone_17_pro_first(self):
        runtime = simctl.choose_runtime(self.runtimes)
        self.assertEqual(simctl.choose_device_type(self.kinds, runtime).name, "iPhone 17 Pro")

    def test_fallbacks_in_order(self):
        runtime = simctl.choose_runtime(self.runtimes)
        without = [kind for kind in self.kinds if kind.name != "iPhone 17 Pro"]
        self.assertEqual(simctl.choose_device_type(without, runtime).name, "iPhone 16 Pro")
        without = [kind for kind in without if kind.name != "iPhone 16 Pro"]
        self.assertEqual(simctl.choose_device_type(without, runtime).name, "iPhone 15 Pro")
        without = [kind for kind in without if kind.name != "iPhone 15 Pro"]
        self.assertEqual(simctl.choose_device_type(without, runtime).name, "iPhone Air")  # the newest iPhone left

    def test_only_types_the_runtime_supports(self):
        old = simctl.choose_runtime(self.runtimes, "iOS 18.6")
        self.assertEqual(simctl.choose_device_type(self.kinds, old).name, "iPhone 16 Pro")
        with self.assertRaises(SimError) as caught:
            simctl.choose_device_type(self.kinds, old, "iPhone 17 Pro")
        self.assertIn("doesn't run iOS 18.6", str(caught.exception))

    def test_device_by_name_or_identifier(self):
        runtime = simctl.choose_runtime(self.runtimes)
        self.assertEqual(simctl.choose_device_type(self.kinds, runtime, "iphone air").name, "iPhone Air")
        self.assertEqual(simctl.choose_device_type(
            self.kinds, runtime, "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro").name, "iPhone 16 Pro")

    def test_unknown_device_lists_iphones(self):
        runtime = simctl.choose_runtime(self.runtimes)
        with self.assertRaises(SimError) as caught:
            simctl.choose_device_type(self.kinds, runtime, "iPhone 99")
        self.assertEqual(caught.exception.kind, "environment")
        self.assertIn("iPhone 17 Pro", str(caught.exception))
        self.assertNotIn("iPad", str(caught.exception))


class NamingTests(unittest.TestCase):
    def test_first_name_then_numbered(self):
        base = "Mobster · iPhone 17 Pro · iOS 26.4"
        self.assertEqual(simctl.sim_name("iPhone 17 Pro", "iOS 26.4"), base)
        self.assertEqual(simctl.sim_name("iPhone 17 Pro", "iOS 26.4", {base}), base + " · 2")
        self.assertEqual(simctl.sim_name("iPhone 17 Pro", "iOS 26.4", {base, base + " · 2"}), base + " · 3")
        self.assertEqual(simctl.sim_name("iPhone 17 Pro", "iOS 26.4", {base + " · 2"}), base)

    def test_mobster_names(self):
        self.assertTrue(simctl.is_mobster_name("Mobster · iPhone 17 Pro · iOS 26.4 · 2"))
        for name in ("iOSWorld Mobster", "mobster-smoke", "Mobster iPhone", "", None):
            with self.subTest(name=name):
                self.assertFalse(simctl.is_mobster_name(name))


if __name__ == "__main__":
    unittest.main()

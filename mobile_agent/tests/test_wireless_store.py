"""Track wireless: which phones may be reached over Wi-Fi (SPEC §3.7, data model). The primary phone keeps
``wifi`` in its device.json with the UDID it belongs to; a phone added besides it keeps it on its devices.json entry.
Offline: temporary data folders only."""

import json
from pathlib import Path
import tempfile
import time
import unittest

from mobile_agent.devices import DeviceStore
from mobile_agent.wireless import store

PRO = "00008130-001A2B3C4D5E6F70"
XR = "00008020-000A1B2C3D4E5F60"
SE = "00008130-0011223344556677"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="mobster-wifi-store-"))
        (self.data / "device.json").write_text(json.dumps({"udid": PRO, "device_name": "Sam's iPhone", "team": "T"}))
        (self.data / "devices.json").write_text(json.dumps({"version": 1, "lastUsed": None, "phones": [
            {"udid": XR, "slot": 1, "wdaPort": 8101, "mjpegPort": 9101, "name": "Work iPhone"}]}))

    def settings(self):
        return json.loads((self.data / "device.json").read_text())

    def test_off_until_turned_on(self):
        for udid in (PRO, XR, SE):
            with self.subTest(udid=udid):
                self.assertEqual(store.get(self.data, udid), store.OFF)
                self.assertFalse(store.enabled(self.data, udid))
        self.assertEqual(store.enabled_udids(self.data), set())

    def test_the_primary_phone_keeps_it_in_device_json_with_its_udid(self):
        before = round(time.time() * 1000)
        value = store.put(self.data, PRO.lower(), True, "mobster")
        self.assertEqual((value["enabled"], value["via"]), (True, "mobster"))
        self.assertGreaterEqual(value["enabledAt"], before)
        saved = self.settings()
        self.assertEqual((saved["team"], saved["device_name"]), ("T", "Sam's iPhone"))   # nothing else changes
        self.assertEqual(saved["wifi"]["udid"], PRO.lower())
        self.assertTrue(store.enabled(self.data, PRO))
        self.assertEqual(store.enabled_udids(self.data), {PRO})
        store.put(self.data, PRO, False)
        self.assertIsNone(self.settings()["wifi"])    # device.json keeps the key, empty
        self.assertEqual(store.get(self.data, PRO), store.OFF)

    def test_choosing_another_primary_phone_never_carries_it_over(self):
        store.put(self.data, PRO, True, "xcode")
        settings = self.settings()
        settings["udid"] = SE                         # Setup: another iPhone chosen
        (self.data / "device.json").write_text(json.dumps(settings))
        self.assertFalse(store.enabled(self.data, SE))
        self.assertFalse(store.enabled(self.data, PRO))
        self.assertEqual(store.enabled_udids(self.data), set())

    def test_a_phone_added_besides_it_keeps_it_on_its_own_entry(self):
        store.put(self.data, XR, True, "xcode")
        entry = DeviceStore(self.data).phones()[0]
        self.assertEqual((entry["wifi"]["enabled"], entry["wifi"]["via"], entry["slot"]), (True, "xcode", 1))
        self.assertNotIn("wifi", self.settings())
        self.assertEqual(store.enabled_udids(self.data), {XR})
        store.put(self.data, PRO, True, "mobster")
        self.assertEqual(store.enabled_udids(self.data), {PRO, XR})
        store.put(self.data, XR, False)
        self.assertNotIn("wifi", DeviceStore(self.data).phones()[0])
        self.assertEqual(store.enabled_udids(self.data), {PRO})

    def test_a_phone_not_set_up_here(self):
        with self.assertRaises(LookupError):
            store.put(self.data, SE, True, "mobster")
        with self.assertRaises(ValueError):
            store.put(self.data, PRO, True, "someone")

    def test_hand_edited_values_read_safely(self):
        for wifi in ("yes", {"enabled": "true"}, {"enabled": 1}, None, [True]):
            settings = self.settings()
            settings["wifi"] = wifi
            (self.data / "device.json").write_text(json.dumps(settings))
            with self.subTest(wifi=wifi):
                self.assertFalse(store.enabled(self.data, PRO))
        settings = self.settings()
        settings["wifi"] = {"enabled": True, "udid": PRO, "enabledAt": True, "via": "script"}
        (self.data / "device.json").write_text(json.dumps(settings))
        self.assertEqual(store.get(self.data, PRO), {"enabled": True, "enabledAt": None, "via": None})
        settings["wifi"]["udid"] = XR                 # another phone's value is never this one's
        (self.data / "device.json").write_text(json.dumps(settings))
        self.assertFalse(store.enabled(self.data, PRO))

    def test_no_data_folder_yet(self):
        empty = Path(tempfile.mkdtemp(prefix="mobster-wifi-empty-"))
        self.assertEqual(store.get(empty, PRO), store.OFF)
        self.assertEqual(store.enabled_udids(empty), set())
        with self.assertRaises(LookupError):
            store.put(empty, PRO, True, "mobster")
        self.assertFalse((empty / "device.json").exists())


if __name__ == "__main__":
    unittest.main()

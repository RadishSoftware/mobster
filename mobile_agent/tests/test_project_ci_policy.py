"""CI reads device rules afresh and refuses corruption rather than losing an allowlist."""

import json
import tempfile
import unittest
from pathlib import Path

from mobile_agent.devices import DeviceStore


class StrictDeviceRulesTests(unittest.TestCase):
    def test_missing_rules_have_no_saved_restrictions_but_malformed_rules_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeviceStore(directory)
            self.assertEqual(store.read_strict()["prefs"], {})
            path = Path(directory) / "devices.json"
            path.write_text('{"prefs":{"SIM":{"apps":["com.example.app"]}},"prefs":{}}')
            self.assertEqual(store.read()["prefs"], {})
            with self.assertRaises(ValueError):
                store.read_strict()
            path.write_text(json.dumps({**store._empty(), "prefs": {"SIM": {"apps": "all"}}}))
            with self.assertRaises(ValueError):
                store.read_strict()

    def test_symlinked_rules_refuse_instead_of_reading_an_untrusted_allow_all(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeviceStore(directory)
            outside = Path(directory) / "outside.json"
            outside.write_text(json.dumps(store._empty()))
            (Path(directory) / "devices.json").symlink_to(outside)
            with self.assertRaises(ValueError):
                store.read_strict()

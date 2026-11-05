"""A business-state check cannot install or inspect apps on a phone."""

import plistlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from mobile_agent.testkit.targets import DeviceGate, Refused


class BusinessPhoneGateTests(unittest.TestCase):
    def test_valid_device_ipa_refuses_before_listing_or_install(self):
        with tempfile.TemporaryDirectory() as folder:
            ipa = Path(folder) / "FixtureApp.ipa"
            with zipfile.ZipFile(ipa, "w") as archive:
                archive.writestr("Payload/FixtureApp.app/Info.plist",
                                 plistlib.dumps({"CFBundleIdentifier": "dev.mobster.fixture"}))
            check = SimpleNamespace(app_path=str(ipa), bundle_id="dev.mobster.fixture",
                                    reset="auto", business=object())
            effects = []

            def list_apps(argv):
                effects.append("list")
                raise AssertionError("phone apps were listed")

            def install(record, path):
                effects.append("install")
                raise AssertionError("the app was installed")

            gate = DeviceGate(runner=list_apps, installer=install, allowed=lambda: True)
            phone = {"id": "<UDID>", "udid": "<UDID>", "kind": "usb", "name": "Test iPhone"}
            with self.assertRaisesRegex(Refused, "only on managed simulators") as caught:
                gate.admit(check, phone)
            self.assertEqual(caught.exception.klass, "business")
            self.assertEqual(effects, [])


if __name__ == "__main__":
    unittest.main()

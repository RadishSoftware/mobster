"""Shared CLI device resolution never substitutes another phone."""

import unittest
from unittest.mock import patch

from mobile_agent.cli_run import NoDevice, device_target


class DeviceTargetTests(unittest.TestCase):
    def test_named_device_resolves_or_refuses_without_probing(self):
        ready = {"id": "phone-ready", "name": "Ready phone", "wdaUrl": "http://127.0.0.1:8103"}
        unready = {"id": "phone-unready", "name": "Unready phone", "reason": "Unlock it."}
        with patch("mobile_agent.devices.discover", return_value=[ready, unready]) as discover:
            self.assertIs(device_target("Ready phone"), ready)
            for name, message in (("Missing phone", "No device"), ("Unready phone", "isn't set up yet")):
                with self.subTest(name=name), self.assertRaisesRegex(NoDevice, message):
                    device_target(name)
            self.assertEqual(discover.call_count, 3)
            for call in discover.call_args_list:
                self.assertEqual(call.args, ())
                self.assertEqual(call.kwargs, {"probe": False})


if __name__ == "__main__":
    unittest.main()

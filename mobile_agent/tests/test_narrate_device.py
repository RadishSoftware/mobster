import unittest
from types import SimpleNamespace

from mobile_agent import narrate
from mobile_agent.run import Run

APP = {"id": "settings", "name": "Settings", "bundleId": "com.apple.Preferences"}
UNPROVEN = {"status": "completed_unverified", "outcome": "check", "reason": None}


def finished(origin, phone=None, name=None):
    run = Run(app=dict(APP), goal="Read the version", mode="live", origin=origin, phone=phone, device_name=name)
    run.finish(dict(UNPROVEN))
    return run.summary


class WhereToLookTests(unittest.TestCase):
    def test_the_take_a_look_sentence_and_the_message_follow_it(self):
        sim = {**UNPROVEN, "deviceKind": "simulator"}
        self.assertTrue(narrate.take_a_look_line(sim).endswith("Look at the simulator."))
        self.assertTrue(narrate.take_a_look_line({"deviceKind": "usb", "deviceName": "Studio iPhone"}).endswith(
            "Look at Studio iPhone."))
        self.assertEqual(narrate.take_a_look_line(), narrate.TAKE_A_LOOK_LINE)
        self.assertEqual(narrate.completion_message("completed_unverified", "", None, sim),
                         "Mobster couldn't confirm all of this on the screen. Look at the simulator.")


class RunSummaryTests(unittest.TestCase):
    def test_a_run_names_its_device_for_the_app_chat_mcp_and_remote_alike(self):
        for origin in ("app", "cli", "mcp", "remote"):
            with self.subTest(origin=origin):
                sim = finished(origin, SimpleNamespace(kind="simulator"), "iPhone 17 Pro")
                self.assertEqual((sim["deviceKind"], sim["deviceName"]), ("simulator", "iPhone 17 Pro"))
                self.assertIn("Look at the simulator", narrate.Outcome("r", sim["status"], sim).detail)
                phone = finished(origin, None, "Michelle's iPhone")
                self.assertEqual(phone["deviceKind"], "usb")
                self.assertIn("Look at Michelle's iPhone", narrate.Outcome("r", phone["status"], phone).detail)

    def test_a_summary_that_already_names_its_device_is_left_alone(self):
        run = Run(app=dict(APP), goal="x", mode="live")
        run.finish({**UNPROVEN, "deviceKind": "wda"})
        self.assertEqual(run.summary["deviceKind"], "wda")


if __name__ == "__main__":
    unittest.main()

"""Apps pinned with @App on a phone of several are limited by that phone's own app list."""

import unittest

from mobile_agent.tests.test_fanout import WorldTest


class PinnedAppsOnAPhoneTests(WorldTest):
    def test_a_pin_outside_the_phones_apps_is_refused_and_one_inside_starts_there(self):
        status, body = self.request("POST", f"/api/devices/{self.work}",
                                    {"apps": ["com.apple.Preferences", "com.apple.MobileSMS"]})
        self.assertEqual(status, 200, body)
        status, body = self.request("POST", "/api/runs", {"goal": "Read my chats and my notes", "device": self.work,
                                                          "pinnedApps": ["messages", "notes"]})
        self.assertEqual((status, body["code"]), (400, "app_not_allowed"))
        self.assertIn("Notes isn't one of the apps Mobster may use on Work iPhone", body["error"])
        run = self.runtime.create(None, "Read my chats", "live", device=self.work, pinned_apps=["messages"])
        self.assertEqual((run.app["id"], [pin["id"] for pin in run.pinned_apps]), ("messages", ["messages"]))
        self.runtime.stop_run(run.id)
        self.finished(run.id)


if __name__ == "__main__":
    unittest.main()

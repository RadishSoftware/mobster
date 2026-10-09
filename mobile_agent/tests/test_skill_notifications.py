"""READ_NOTIFICATIONS (skills/notifications.py) on a fake phone: at most 30 rows in at most 6 s, never a tap on a
row, codes masked, Notification Center closed and the app brought back. No phone, no model."""

import dataclasses
import unittest

from mobile_agent.agent_hooks import Prepared
from mobile_agent.skills.notifications import (CODE_MASK, MAX_ROWS, MAX_SECONDS, NotificationCenter,
                                               ReadNotifications, parse_row, screen_rows)
from mobile_agent.state import Element
from mobile_agent.tests.test_skill_codes import CANARY, FakeClock, FakePhone, context, screen


def crowded(count):
    """Notification Center with ``count`` rows, a code among them."""
    base = screen("notification_center")
    rows = [Element(f"n{i}", f"Shop, Order {i} shipped, Your parcel is on its way., {i + 1}m ago", "Cell",
                    (.04, min(.9, .2 + i * .02), .92, .02), locator=f"/nc/{i}") for i in range(count - 1)]
    rows.append(Element("code", "Messages, Chase, Your Chase verification code is 482913., now", "Cell",
                        (.04, .95, .92, .02), locator="/nc/code"))
    return dataclasses.replace(base, elements=rows)


class ReadNotificationsTests(unittest.TestCase):
    def phone(self, nc="notification_center", read_cost=.3):
        self.clock = FakeClock()
        return FakePhone("com.chase.sig", {"com.chase.sig": screen("signin")}, clock=self.clock, nc=nc,
                         read_cost=read_cost)

    def perform(self, phone):
        skill = ReadNotifications(clock=self.clock)
        snapshot = phone.observe()
        prepared = skill.prepare(context(phone), {"operation": "READ_NOTIFICATIONS"}, snapshot)
        self.assertEqual(prepared, Prepared(None, None))  # nothing to approve: it is read-only
        return skill.perform(context(phone), prepared, {"operation": "READ_NOTIFICATIONS"}, snapshot)

    def test_it_reads_the_rows_masks_codes_and_brings_the_app_back(self):
        phone = self.phone()
        started = self.clock.now
        result = self.perform(phone)
        self.assertLessEqual(self.clock.now - started, MAX_SECONDS)
        self.assertIn("Chase, Your Chase verification code is " + CODE_MASK, result.feedback)
        self.assertIn("Weather, Rain expected", result.feedback)
        self.assertNotIn(CANARY, result.feedback)
        self.assertNotIn("Monday, October 5", result.feedback)  # Notification Center's own heading
        self.assertEqual((result.secrets, result.changed, result.stop), ((), False, None))
        self.assertEqual(phone.front, "com.chase.sig")
        self.assertFalse(phone.nc_open)
        self.assertEqual(phone.hint, "com.chase.sig")
        activations = [c for c in phone.calls if isinstance(c, tuple) and c[1] == "/wda/apps/activate"]
        self.assertEqual(activations[-1][2], {"bundleId": "com.chase.sig"})

    def test_with_no_app_named_the_read_hint_goes_back_to_auto_not_springboard(self):
        # WebDriverAgent prefers the hinted app when several are active: a SpringBoard hint left behind would read
        # SpringBoard over the app in front from then on.
        phone = self.phone()
        NotificationCenter(phone, self.clock).read(origin="", size=phone.observe())
        self.assertEqual(phone.hint, "auto")
        self.assertFalse(phone.nc_open)

    def test_it_never_taps_a_notification(self):
        phone = self.phone()
        self.perform(phone)
        self.assertEqual(phone.taps, [])  # no tap at all, on a row or anywhere
        drags = [c for c in phone.calls if isinstance(c, tuple) and c[1] == "/actions"]
        self.assertTrue(drags)
        for _method, _path, body in drags:
            moves = [a for a in body["actions"][0]["actions"] if a["type"] == "pointerMove"]
            self.assertEqual(len(moves), 2)  # every pointer action is a drag

    def test_at_most_30_rows_within_6_seconds_even_when_reads_are_slow(self):
        phone = self.phone(nc=crowded(45), read_cost=1.2)
        started = self.clock.now
        rows = NotificationCenter(phone, self.clock).read(origin="com.chase.sig", size=screen("signin"))
        self.assertLessEqual(len(rows), MAX_ROWS)
        self.assertLessEqual(self.clock.now - started, MAX_SECONDS)
        self.assertFalse(phone.nc_open)
        self.assertEqual(phone.front, "com.chase.sig")

    def test_a_read_that_fails_still_closes_and_restores(self):
        phone = self.phone()
        original = phone.observe

        def broken(timeout=10):
            if phone.nc_open:
                raise TimeoutError("WDA didn't answer")
            return original(timeout)
        phone.observe = broken
        result = ReadNotifications(clock=self.clock).perform(context(phone), Prepared(None, None), {},
                                                             screen("signin"))
        self.assertIn("couldn't be read", result.feedback)
        self.assertFalse(phone.nc_open)
        self.assertEqual((phone.front, phone.hint), ("com.chase.sig", "com.chase.sig"))

    def test_rows(self):
        row = parse_row("Messages, Chase, Your code is 482913, 2m ago")
        self.assertEqual((row.app, row.sender, row.age_s), ("Messages", "Chase", 120))
        self.assertEqual(row.public()["text"], f"Messages, Chase, Your code is {CODE_MASK}, 2m ago")
        listed = parse_row("Unread, Chase, Your code is 482913, 10:42 AM", now=FakeClock().wall, app="Messages")
        self.assertEqual((listed.app, listed.sender, listed.age_s), ("Messages", "Chase", 120))
        texts = [r.text for r in screen_rows(screen("notification_center"))]
        self.assertEqual(len(texts), 3)
        self.assertFalse([t for t in texts if t in ("9:41", "Notification Center", "Monday, October 5")])


if __name__ == "__main__":
    unittest.main()

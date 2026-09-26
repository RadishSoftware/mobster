"""An UNCLEAR verdict is overridden only for acts the request's own words ask for. Offline."""

import unittest

from mobile_agent.task_policy import asks_for_changes, requested_by

ARCHIVE = ("Filter my Inbox in Mail to show only the Transactions category, then archive a QuickBite receipt. "
           "Confirm it was archived.")
PAY = "Send $32.50 to Maya Patel on SplitPay for 'dinner split' and confirm the payment was sent."
LOOK = "In Settings > General > About, what is the iOS version? Do not change any setting."


class RequestedActionTests(unittest.TestCase):
    def test_the_named_act_and_plain_navigation_are_requested(self):
        self.assertTrue(requested_by(ARCHIVE, "TAP", "Button", "Archive"))
        self.assertTrue(requested_by(ARCHIVE, "TAP", "Cell", "QuickBite, Your receipt", side_effect_risk=.66))
        self.assertTrue(requested_by(PAY, "TAP", "Button", "Pay $32.50"))
        self.assertTrue(requested_by(PAY, "SUBMIT", "SearchField", "Maya Patel"))

    def test_acts_the_request_does_not_name_are_not(self):
        self.assertFalse(requested_by(ARCHIVE, "TAP", "Button", "Delete"))
        self.assertFalse(requested_by(PAY, "TAP", "Button", "Request", side_effect_risk=.8))
        self.assertFalse(requested_by(PAY, "TAP", "Switch", "Notifications"))

    def test_a_look_only_request_never_overrides(self):
        self.assertFalse(asks_for_changes(LOOK))
        self.assertFalse(requested_by(LOOK, "TAP", "Cell", "About", side_effect_risk=.01))
        self.assertFalse(requested_by("Open the note. Only look; do not share or delete anything.", "TAP", "Button",
                                      "Share"))


if __name__ == "__main__":
    unittest.main()

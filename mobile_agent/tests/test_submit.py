import unittest
from unittest.mock import ANY, Mock

from mobile_agent.drivers import WDA
from mobile_agent.effect_ledger import PERSISTENT_OPERATIONS
from mobile_agent.state import from_wda
from mobile_agent.task_policy import RiskTier, action_risk_tier

FIELD = '<XCUIElementTypeTextField label="Address" value="wikipedia.org" x="10" y="440" width="380" height="40"/>'
KEYBOARD = ('<XCUIElementTypeKeyboard x="0" y="500" width="400" height="300">'
            '<XCUIElementTypeKey label="q" x="0" y="510" width="40" height="50"/></XCUIElementTypeKeyboard>')


def app(*children):
    return '<XCUIElementTypeApplication width="400" height="800">' + "".join(children) + "</XCUIElementTypeApplication>"


class SubmitTests(unittest.TestCase):
    def field(self, xml):
        return next(e for e in from_wda(xml).elements if e.editable)

    def test_submit_is_offered_only_with_a_keyboard(self):
        self.assertNotIn("SUBMIT", self.field(app(FIELD)).actions)
        self.assertIn("SUBMIT", self.field(app(KEYBOARD, FIELD)).actions)

    def test_wda_submit_presses_return(self):
        snapshot = from_wda(app(KEYBOARD, FIELD))
        driver = WDA("http://localhost:8100", "s")
        self.addCleanup(driver.close)
        driver.call = Mock()
        driver.execute("SUBMIT", self.field(app(KEYBOARD, FIELD)), snapshot)
        driver.call.assert_called_once_with("POST", "/wda/keys", {"value": ["\n"]}, ANY)

    def test_submit_without_a_keyboard_is_refused(self):
        snapshot = from_wda(app(FIELD))
        driver = WDA("http://localhost:8100", "s")
        self.addCleanup(driver.close)
        driver.call = Mock()
        with self.assertRaises(ValueError):
            driver.execute("SUBMIT", snapshot.elements[0], snapshot)
        driver.call.assert_not_called()

    def test_submitting_a_message_is_a_side_effect_and_is_ledgered(self):
        self.assertIs(action_risk_tier("SUBMIT", "Message", "Send Alex a message saying hi"), RiskTier.SIDE_EFFECT)
        self.assertIs(action_risk_tier("SUBMIT", "Address", "Search the web for Ada Lovelace"), RiskTier.NAVIGATION)
        self.assertIn("SUBMIT", PERSISTENT_OPERATIONS)

    def test_type_submit_types_then_presses_return_from_the_driver(self):
        snapshot = from_wda(app(KEYBOARD, FIELD))
        driver = WDA("http://localhost:8100", "s")
        self.addCleanup(driver.close)
        driver.call = Mock()
        driver.execute("TYPE_SUBMIT", self.field(app(KEYBOARD, FIELD)), snapshot, text="Ada Lovelace")
        driver.call.assert_called_once_with("POST", "/wda/keys", {"value": ["Ada Lovelace", "\n"]}, ANY)

    def test_type_submit_is_offered_on_every_editable_field(self):
        self.assertIn("TYPE_SUBMIT", self.field(app(FIELD)).actions)

    def test_type_submit_tiers(self):
        self.assertIs(action_risk_tier("TYPE_SUBMIT", "Address", "Search the web for Ada Lovelace"), RiskTier.TYPE)
        self.assertIs(action_risk_tier("TYPE_SUBMIT", "Message", "Send Alex a message"), RiskTier.SIDE_EFFECT)
        self.assertIn("TYPE_SUBMIT", PERSISTENT_OPERATIONS)


if __name__ == "__main__":
    unittest.main()

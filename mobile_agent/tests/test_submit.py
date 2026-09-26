"""SUBMIT: press Return on a focused field, only while a keyboard is up. Offline."""

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


class FamilyGateTests(unittest.TestCase):
    def decide(self, split):
        from mobile_agent.models import Jev
        from mobile_agent.tests.test_task_policy import choice
        field = from_wda(app(FIELD))
        model = Jev("offline-test-only")

        def reply(_method, _path, body, _timeout):
            answers = {}
            for name, question in body["questions"].items():
                if question["type"] == "noul":
                    answers[name] = {"type": "noul", "noul": 0}
                elif question["type"] == "score":
                    answers[name] = {"type": "score", "score": 0}
                elif name == "operation":
                    probabilities = {key: 0.0 for key in question["criteria"]}
                    probabilities.update(split)
                    rest = 1 - sum(probabilities.values())
                    probabilities["WAIT"] += rest
                    top = max(probabilities, key=probabilities.get)
                    answers[name] = {"type": "choice", "choice": top, "confidence": probabilities[top],
                                     "probabilities": probabilities}
                else:
                    answers[name] = choice(question, "continue" if name == "stop_gate" else next(iter(question["criteria"])))
            return {"model": "offline-test-only", "answers": answers}
        model.http.request = Mock(side_effect=reply)
        return model.decide(field, "Search the web for Ada Lovelace", [], can_type=True)

    def test_split_text_entry_intent_is_not_demoted(self):
        decision = self.decide({"TYPE_SUBMIT": .45, "TYPE": .3})
        self.assertEqual((decision.operation, decision.demoted_from), ("TYPE_SUBMIT", None))

    def test_weak_text_entry_intent_is_still_demoted(self):
        decision = self.decide({"TYPE_SUBMIT": .3, "TYPE": .1})
        self.assertEqual(decision.operation, "WAIT")

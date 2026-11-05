import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from mobile_agent import engines
from mobile_agent.contract_screen import header_recipient
from mobile_agent.frontier import FrontierAgent, failure_feedback
from mobile_agent.run import Run
from mobile_agent.state import Element, Snapshot, from_wda
from mobile_agent.tests import test_engines
from mobile_agent.tests.test_frontier import Driver, Script, screen


class StateTests(unittest.TestCase):
    def test_secure_descendants_and_offscreen_text_are_not_model_evidence(self):
        state = from_wda('''<XCUIElementTypeApplication width="400" height="800">
            <XCUIElementTypeSecureTextField><XCUIElementTypeStaticText label="secret" x="0" y="0" width="100" height="40"/></XCUIElementTypeSecureTextField>
            <XCUIElementTypeButton label="Offscreen" x="0" y="900" width="100" height="40"/>
            </XCUIElementTypeApplication>''')  # noqa: E501
        self.assertEqual(state.text, "")
        self.assertEqual(state.elements, [])

    def test_offscreen_hidden_secure_and_virtual_roles(self):
        xml = '''<AppiumAUT><XCUIElementTypeApplication width="400" height="800">
          <XCUIElementTypeButton label="Search" visible="true" enabled="true" x="348" y="20" width="100" height="40"/>
          <XCUIElementTypeButton label="Hidden" visible="false" x="0" y="0" width="50" height="30"/>
          <XCUIElementTypeSecureTextField label="Password" value="do-not-expose" x="0" y="20" width="100" height="30"/>
          <XCUIElementTypeSearchField label="Query" value="coffee" x="10" y="70" width="300" height="40"/>
        </XCUIElementTypeApplication></AppiumAUT>'''
        state = from_wda(xml)
        self.assertEqual([e.label for e in state.elements], ["Search", "Query"])
        self.assertEqual(state.elements[0].rect, (.87, .025, .13, .05))
        self.assertTrue(state.elements[1].editable)
        self.assertNotIn("do-not-expose", json.dumps(state.public()))
        self.assertEqual(state.elements[1].locator,
                         "/AppiumAUT/XCUIElementTypeApplication[1]/XCUIElementTypeSearchField[1]")

    def test_bad_coordinates_and_xml(self):
        for rect in [(0, 0, 2, .1), (float("nan"), 0, .1, .1), (0, 0, 0, 0)]:
            with self.assertRaises(ValueError):
                Element("a", "bad", "Button", rect)
        with self.assertRaises(ValueError):
            from_wda('<!DOCTYPE x [<!ENTITY y "bad">]><x/>')


class UnknownOutcomeTests(unittest.TestCase):
    def run_failing(self, error):
        row = screen(Element("1", "Next", "Button", (.1, .1, .2, .05)))

        class Failing(Driver):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                super().execute(operation, target, snapshot, text, timeout)
                raise error

        driver, events = Failing([row] * 4), []
        FrontierAgent(driver, Script([("TAP", "Next", None), ("DONE", None, None), ("DONE", None, None)]),
                      emit=events.append, screenshots=False, settle_seconds=0).run("Open next.")
        return driver, events

    def test_a_device_error_of_unknown_outcome_is_sent_once(self):
        for error in (ConnectionError(), ConnectionResetError("reset"), TimeoutError("Operation deadline exceeded")):
            with self.subTest(error=type(error).__name__):
                driver, events = self.run_failing(error)
                self.assertEqual(driver.actions, [("TAP", "Next", None)])
                self.assertEqual([e["operation"] for e in events if e["event"] == "frontier_failed"], ["TAP"])

    def test_a_timeout_tells_the_model_to_check_the_screen_before_repeating(self):
        self.assertIn("Look at the screen before acting",
                      failure_feedback("TAP", Element("1", "Next", "Button", (.1, .1, .2, .05)), None,
                                       TimeoutError("Operation deadline exceeded")))


class EmptyRedirectTests(unittest.TestCase):
    def test_an_empty_redirect_sends_nothing_and_a_second_send_asks_again(self):
        sent = test_engines.draft("I'm running late")
        driver = test_engines.Driver([test_engines.COMPOSE] + [sent] * 4)
        script = test_engines.Script([[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)],
                                      [("TAP", "Send", None)], [("DONE", None, None)], [("DONE", None, None)]],
                                     [test_engines.item()])
        asked = []
        with patch.dict(os.environ, dict(test_engines.CLEAN), clear=True):
            engines.build_frontier(driver, script, apps={"com.apple.MobileSMS": "Messages"}, emit=lambda e: None,
                                   config=test_engines.QUICK,
                                   approve=lambda request: asked.append(request) or "redirected:  ").run(
                "Text Sam I'm running late")
        self.assertEqual(driver.actions, [("TYPE", "Message", "I'm running late")])
        self.assertEqual([a["title"] for a in asked], ["Send this message to Sam?"] * 2)


class HeaderRecipientTests(unittest.TestCase):
    @staticmethod
    def snap(elements):
        return Snapshot(elements, "\n".join(e.label or e.value for e in elements), 402, 874, "synthetic_fixture",
                        bundle_id="com.apple.MobileSMS")

    def test_the_recipient_comes_from_the_conversation_header(self):
        header = [Element("n", "Messages", "NavigationBar", (0, .06, 1, .11)),
                  Element("b", "Messages", "Button", (.04, .06, .1, .05)),
                  Element("p", "Contact photo", "Button", (.43, .06, .15, .07)),
                  Element("h", "+1 (555) 564-8583", "Button", (.26, .13, .48, .04))]
        send = Element("s", "Send", "Button", (.85, .59, .1, .03))
        under = [Element("e", "iMessage  Encrypted", "StaticText", (.04, .19, .92, .04)),
                 Element("d", "Today 12:00 AM", "StaticText", (.04, .24, .92, .01))]
        self.assertEqual(header_recipient(self.snap([*header, *under, send]), send), "+1 (555) 564-8583")
        to = Element("t", "To:", "TextField", (.1, .12, .8, .04), True, value="Zara Okonkwo")
        self.assertEqual(header_recipient(self.snap([to, send]), send), "Zara Okonkwo")
        two = [*header, Element("x", "Alex", "Button", (.3, .1, .3, .03))]
        self.assertIsNone(header_recipient(self.snap([*two, send]), send))


class ApprovalChoiceTests(unittest.TestCase):
    def test_a_question_offers_its_choices_and_refuses_one_it_did_not_offer(self):
        run = Run({"id": "a", "name": "Settings", "bundleId": "com.apple.Preferences"}, "Turn on Wi-Fi", "live")
        answers = []
        request = {"kind": "question", "step": 0, "operation": "SYSTEM_DIALOG", "label": "Allow notifications?",
                   "text": None, "question": "Allow notifications?", "choices": [
                       {"id": "c1", "label": "Allow"}, {"id": "c2", "label": "Don't Allow"}], "default_choice": "c2"}
        thread = threading.Thread(target=lambda: answers.append(run.request_approval(request, timeout=5)))
        thread.start()
        for _ in range(200):
            if run.public()["approval"]:
                break
            time.sleep(.01)
        pending = run.public()["approval"]
        self.assertEqual((pending["kind"], pending["defaultChoice"]), ("question", "c2"))
        with self.assertRaises(ValueError):
            run.answer_approval(pending["id"], True, "delete")
        self.assertTrue(run.answer_approval(pending["id"], True, "c2"))
        thread.join(2)
        self.assertEqual(answers, ["choice:c2"])


class StopSuppressesTheAnswerTests(unittest.TestCase):
    def test_a_stop_during_the_answering_call_publishes_no_answer(self):
        row = screen(Element("1", "Model Number, MTQM3LL/A", "StaticText", (.1, .3, .8, .05)))
        for turn in (1, 2):
            with self.subTest(turn=turn):
                stop, script = threading.Event(), Script([("DONE", None, None)] * 4)
                script.answers = {1: "MTQM3LL/A", 2: "MTQM3LL/A"}
                complete = script.complete

                def answering(messages, schema, timeout=60, turn=turn):
                    out = complete(messages, schema, timeout)
                    if "actions" in schema["properties"] and script.usage["calls"] >= turn:
                        stop.set()
                    return out

                script.complete = answering
                result = FrontierAgent(Driver([row] * 4), script, screenshots=False, settle_seconds=0,
                                       cancelled=stop.is_set).run("Report the model number.")
                self.assertEqual((result["status"], result["answer"]), ("stopped", None))
                self.assertEqual(script.usage["calls"], turn)


if __name__ == "__main__":
    unittest.main()

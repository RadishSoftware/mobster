"""Typing that timed out but landed: a field that reads back exactly the text written holds the agent's text, so a Send
after it is bound to it (iOSWorld lockedin-004 th1, 12 Oct: a 430-character reply sat in the composer while Send was
refused 7 and 13 times as "no text you typed"). A field holding anything else still refuses. Offline."""

import unittest

from mobile_agent.frontier import FrontierAgent, reads_back
from mobile_agent.state import FIELD_CUT, FIELD_HEAD, FIELD_TAIL, Element
from mobile_agent.tests.test_contract import Driver, Script, item, screen

REQ = "Reply to Rachel in LockedIn that I'm interested."
ITEMS = [item(app="LockedIn", what="reply to Rachel", act="send_message", quote="Reply to Rachel in LockedIn")]
REPLY = "Hi Rachel, thanks for reaching out. I'm interested in the role."


def chat(value):
    return screen(Element("1", "chat_message_field", "TextField", (.1, .9, .6, .04), editable=True, value=value),
                  Element("2", "Send", "Button", (.8, .9, .1, .04)))


class SlowTyping(Driver):
    """Typing lands, then the request times out (WDA's deadline): the outcome is unknown to the agent."""

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        super().execute(operation, target, snapshot, text, timeout)
        if operation in ("TYPE", "TYPE_SUBMIT"):
            raise TimeoutError("Operation deadline exceeded")


def run(screens, steps):
    driver, script, events = SlowTyping(screens), Script(steps, ITEMS), []
    result = FrontierAgent(driver, script, apps={"com.example.chat": "LockedIn"}, emit=events.append,
                           screenshots=False, settle_seconds=0).run(REQ)
    return driver, script, result


class TimedOutTypingTests(unittest.TestCase):
    def test_a_send_after_typing_that_timed_out_but_landed_goes_through(self):
        sent = screen(Element("3", REPLY, "StaticText", (.1, .5, .6, .04)),
                      Element("1", "chat_message_field", "TextField", (.1, .9, .6, .04), editable=True, value=""))
        driver, script, result = run([chat(""), chat(REPLY), sent, sent],
                                     [[("TYPE", "chat_message_field", REPLY)], [("TAP", "Send", None)],
                                      [("DONE", None, None)]])
        self.assertEqual([a[0] for a in driver.actions], ["TYPE", "TAP"])
        self.assertIn("reads back exactly the text you wrote", script.prompts[1])
        self.assertEqual(result["contract"]["receipted"], 1)

    def test_a_field_holding_part_of_the_text_still_refuses_send(self):
        part = REPLY[:20]
        driver, script, _ = run([chat(""), chat(part), chat(part), chat(part)],
                                [[("TYPE", "chat_message_field", REPLY)], [("TAP", "Send", None)],
                                 [("DONE", None, None)], [("DONE", None, None)]])
        self.assertEqual([a[0] for a in driver.actions], ["TYPE"])
        self.assertIn("no text you typed", script.prompts[2])

    def test_a_long_value_read_as_head_and_tail_matches_only_its_own_text(self):
        text = "a" * FIELD_HEAD + "b" * 100 + "c" * FIELD_TAIL
        shown = text[:FIELD_HEAD] + FIELD_CUT + text[-FIELD_TAIL:]
        self.assertTrue(reads_back(shown, text))
        self.assertFalse(reads_back(shown, "x" + text[1:]))
        self.assertTrue(reads_back(" Hi  Rachel ", "Hi Rachel"))
        self.assertFalse(reads_back("Hi", "Hi Rachel"))


if __name__ == "__main__":
    unittest.main()

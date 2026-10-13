"""A done action's line in the turn log keeps the row it acted on and the text it typed (iOSWorld dev runs, 10-12 Oct:
a restaurant row's "0.9 mi • Financial District" was cut to "0.9 mi • Fin" at 60 characters, and the neighborhood search
went elsewhere; a sent reply's questions were cut at 80 and the answer could not repeat them). Offline."""

import unittest
from unittest import mock

from mobile_agent import frontier
from mobile_agent.frontier import LOG_LABEL_CHARS, LOG_TEXT_CHARS, FrontierAgent
from mobile_agent.state import Element
from mobile_agent.tests.test_frontier import Driver, Script, screen

ROW = "Embarcadero Grill, $$$, •, Californian, •, 4.5, 0.9 mi • Financial District, Open until 10 PM"
REPLY = ("Hi Rachel, thanks for reaching out. I'm interested. Three questions: what does the team own today, "
         "which parts of the stack would I change first, and how is on-call shared?")


def prompts(events):
    return [e["text"] for e in events if e["event"] == "frontier_prompt"]


def run(screens, steps, request):
    events = []
    FrontierAgent(Driver(screens), Script(steps), emit=events.append, screenshots=False, settle_seconds=0).run(request)
    return events


class TurnLogLinesTests(unittest.TestCase):
    def test_a_tapped_row_is_kept_whole(self):
        results = screen(Element("1", ROW, "Button", (0, .3, 1, .1)), bundle="com.example.dine")
        page = screen(Element("1", "Embarcadero Grill", "StaticText", (0, .1, 1, .05)), bundle="com.example.dine")
        events = run([results, page, page, page], [("TAP", ROW, None), ("DONE", None, None), ("DONE", None, None)],
                     "Open Embarcadero Grill.")
        self.assertIn("0.9 mi • Financial District, Open until 10 PM", prompts(events)[1])

    def test_typed_text_is_kept_whole(self):
        field = Element("1", "Message", "TextField", (0, .8, 1, .05), editable=True,
                        actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        # Sent: the composer is empty again, and only the turn log still holds what was written.
        events = run([screen(field), screen(field), screen(field), screen(field)],
                     [("TYPE_SUBMIT", "Message", REPLY), ("DONE", None, None), ("DONE", None, None)], "Reply to Rachel.")
        log = prompts(events)[1].split("Turns used", 1)[0]
        self.assertIn("how is on-call shared?", log)

    def test_a_longer_label_is_still_bounded(self):
        long_row = "Row " + "x" * 140
        rows = screen(Element("1", long_row, "Button", (0, .3, 1, .1)))
        other = screen(Element("1", "Detail", "StaticText", (0, .1, 1, .05)))
        with mock.patch.object(frontier, "LOG_LABEL_CHARS", 100):
            events = run([rows, other, other, other], [("TAP", long_row, None), ("DONE", None, None),
                                                       ("DONE", None, None)], "Open the row.")
        log = prompts(events)[1].split("Turns used", 1)[0]
        self.assertIn("TAP 'Row " + "x" * 96 + "'", log)
        self.assertGreater(LOG_LABEL_CHARS, 160)  # a row's whole label as the screen lists it (screen_rows: 160)
        self.assertGreater(LOG_TEXT_CHARS, len(REPLY))


if __name__ == "__main__":
    unittest.main()

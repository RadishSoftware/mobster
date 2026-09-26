"""Off-screen page rows that share a word with the request are kept before nearer, unrelated ones. Offline."""

import unittest
from types import SimpleNamespace
from unittest import mock

from mobile_agent import extraction
from mobile_agent.extraction import Evidence, focus_terms
from mobile_agent.state import OffscreenNode

GOAL = "According to this Wikipedia article, on what date did Apollo 11 land on the Moon?"


def page(filler=40):
    """An infobox: the splashdown row near the top, filler rows, the Moon-landing row far below."""
    nodes = [OffscreenNode("Landing date", "StaticText", (.05, 1.1, .3, .02), locator="/a"),
             OffscreenNode("July 24, 1969", "StaticText", (.5, 1.1, .4, .02), locator="/b")]
    for i in range(filler):
        nodes.append(OffscreenNode(f"Apollo 11 crew member {i}", "StaticText", (.05, 1.2 + i * .05, .5, .02),
                                   locator=f"/f{i}"))
    y = 1.2 + filler * .05 + .5
    nodes += [OffscreenNode("Lunar landing date", "StaticText", (.05, y, .3, .02), locator="/c"),
              OffscreenNode("July 20, 1969", "StaticText", (.5, y, .4, .02), locator="/d")]
    return SimpleNamespace(elements=[], offscreen=nodes, source="wda", bundle_id="com.apple.mobilesafari")


class OffscreenFocusTests(unittest.TestCase):
    def test_request_words_are_short_stems_without_question_words(self):
        self.assertEqual(focus_terms(GOAL), ("apoll", "land", "moon"))

    def test_a_far_row_the_request_names_is_kept_when_nearer_rows_fill_the_budget(self):
        evidence = Evidence()
        evidence.focus = focus_terms(GOAL)
        with mock.patch.object(extraction, "OFFSCREEN_EVIDENCE_NODES", 10):
            evidence.add(page(), 0)
        texts = [e["text"] for e in evidence.entries]
        self.assertIn("July 20, 1969", texts)
        self.assertIn("July 24, 1969", texts)
        self.assertEqual(len(texts), 10)

    def test_without_a_focus_the_nearest_rows_are_kept(self):
        evidence = Evidence()
        with mock.patch.object(extraction, "OFFSCREEN_EVIDENCE_NODES", 10):
            evidence.add(page(), 0)
        self.assertNotIn("July 20, 1969", [e["text"] for e in evidence.entries])


if __name__ == "__main__":
    unittest.main()

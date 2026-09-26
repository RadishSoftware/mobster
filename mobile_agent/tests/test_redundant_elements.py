"""A row's own text, its SF Symbol and stacked layout artifacts are not offered to Jev twice. Offline."""

import unittest

from mobile_agent.models import compact_screen, redundant_ids
from mobile_agent.state import Element, Snapshot


class RedundantElementTests(unittest.TestCase):
    def test_row_parts_and_stacks_are_redundant_but_the_row_is_not(self):
        row = [Element("0", "Wi-Fi, Home", "Button", (0, .2, 1, .06)),
               Element("1", "Wi-Fi", "StaticText", (.1, .21, .3, .04)),
               Element("2", "Home", "StaticText", (.6, .21, .2, .04)),
               Element("3", "chevron.forward", "Image", (.9, .21, .05, .04)),
               Element("4", "Search", "SearchField", (.1, .9, .8, .05), True)]
        stack = [Element(str(10 + index), f"App {index}", "StaticText", (0, .131, .2, .028)) for index in range(6)]
        skip = redundant_ids(row + stack)
        self.assertEqual(skip, {"1", "2", "3", *(str(10 + index) for index in range(6))})
        public = Snapshot(row + stack, "text", 393, 852, "wda").public()
        self.assertEqual([e["id"] for e in compact_screen(public)["elements"]], ["0", "4"])

    def test_a_horizontal_row_of_buttons_is_not_a_stack(self):
        buttons = [Element(str(index), f"Tab {index}", "StaticText", (index * .15, .9, .1, .05)) for index in range(6)]
        self.assertEqual(redundant_ids(buttons), set())


if __name__ == "__main__":
    unittest.main()

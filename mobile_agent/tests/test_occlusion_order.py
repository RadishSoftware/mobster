"""Chrome hides only what is drawn before it: a sheet over an old navigation bar stays visible. Offline."""

import unittest

from mobile_agent.state import wda_occlusion

W, H = 393, 852
BAR = ("/App/Window[1]/NavigationBar[1]", "NavigationBar", "Lists", "", (0, 59, 393, 113), True)
ROW_UNDER_BAR = ("/App/Window[1]/Table[1]/Cell[1]", "Cell", "Scrolled row", "", (0, 70, 393, 110), True)
SHEET_DONE = ("/App/Window[2]/Sheet[1]/Button[2]", "Button", "Done", "", (337, 79, 373, 115), True)


class OcclusionOrderTests(unittest.TestCase):
    def test_a_row_scrolled_under_the_bar_is_hidden_but_a_later_sheet_button_is_not(self):
        nodes = [ROW_UNDER_BAR, BAR, SHEET_DONE]
        covered = wda_occlusion(nodes, W, H)
        self.assertTrue(covered(ROW_UNDER_BAR[0], *ROW_UNDER_BAR[4]))
        self.assertFalse(covered(SHEET_DONE[0], *SHEET_DONE[4]))


if __name__ == "__main__":
    unittest.main()

"""Fixes from the H2 A/B traces (10 Oct): a chained tap by a tile's name taps the tile, not its name text (in Files
that text opens a rename field), and SCROLL_TO in a list with a search field stops after a few swipes and says to
search. Offline: scripted model and fake drivers."""

import unittest
import unittest.mock

from mobile_agent import frontier
from mobile_agent.frontier import FrontierAgent
from mobile_agent.state import Element
from mobile_agent.tests.test_frontier import Driver, Script, screen

FILES = "com.apple.DocumentsApp"


class ChainedTapOnATile(unittest.TestCase):
    def run_chain(self, tile):
        browse = screen(Element("1", "On My iPhone", "Cell", (0, .2, 1, .06)), bundle=FILES)
        name = Element("n", "MobsterBench", "StaticText", (.05, .33, .2, .03))
        folder = screen(*((tile, name) if tile is not None else (name,)), bundle=FILES)
        driver = Driver([browse, folder, folder, folder, folder])
        script = Script([[("TAP", "On My iPhone", None), ("TAP", "MobsterBench", None)], ("DONE", None, None),
                         ("DONE", None, None)])
        FrontierAgent(driver, script, screenshots=False, settle_seconds=0).run("Open the MobsterBench folder in Files.")
        return driver.actions

    def test_the_tile_is_tapped_not_its_name(self):
        tile = Element("t", "MobsterBench, 4 items", "Cell", (.02, .2, .3, .17))
        self.assertEqual(self.run_chain(tile)[:2], [("TAP", "On My iPhone", None),
                                                    ("TAP", "MobsterBench, 4 items", None)])

    def test_a_name_in_no_tile_is_still_tapped(self):
        self.assertEqual(self.run_chain(None)[1], ("TAP", "MobsterBench", None))

    def test_a_tile_that_does_not_hold_the_text_is_not_taken(self):
        elsewhere = Element("t", "MobsterBench, 4 items", "Cell", (.6, .6, .3, .1))
        self.assertEqual(self.run_chain(elsewhere)[1], ("TAP", "MobsterBench", None))


class TapByLabelTakesTheControl(unittest.TestCase):
    """A tap named by label alone goes to the control when text on screen reads the same (Calculator's display shows
    "4" after the first 4: H1-H3, 10 Oct, the second 4 tapped the display)."""

    def keypad(self, shown):
        return screen(Element("d", shown, "StaticText", (.8, .2, .1, .07)),
                      Element("k4", "4", "Key", (.04, .74, .21, .06)), Element("k7", "7", "Key", (.04, .68, .21, .06)),
                      bundle="com.apple.calculator")

    def test_a_repeated_key_is_pressed_not_the_display(self):
        class Roles(Driver):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                super().execute(operation, target, snapshot, text, timeout)
                self.actions[-1] = (operation, target.label, target.role)
        driver = Roles([self.keypad("0"), self.keypad("4"), self.keypad("44"), self.keypad("447"),
                        self.keypad("447")])
        script = Script([[("TAP", "4", None), ("TAP", "4", None), ("TAP", "7", None)], ("DONE", "447", None),
                         ("DONE", "447", None)])
        FrontierAgent(driver, script, screenshots=False, settle_seconds=0).run("Type 447 in Calculator.")
        self.assertEqual(driver.actions[:3], [("TAP", "4", "Key"), ("TAP", "4", "Key"), ("TAP", "7", "Key")])

    def test_text_alone_is_still_found_and_a_named_role_still_wins(self):
        only_text = screen(Element("d", "4", "StaticText", (.8, .2, .1, .07)))
        self.assertEqual(FrontierAgent._find(only_text, "4").role, "StaticText")
        self.assertEqual(FrontierAgent._find(self.keypad("4"), "4", "StaticText").role, "StaticText")
        self.assertEqual(FrontierAgent._find(self.keypad("4"), "4").role, "Key")


class ScrollToInASearchableList(unittest.TestCase):
    def run_scroll(self, searchable):
        def page(i):
            rows = [Element(f"r{i}{k}", f"Person {i}-{k}", "Cell", (0, .2 + k * .1, 1, .08)) for k in range(5)]
            field = [Element("s", "Search", "SearchField", (0, .1, 1, .05))] if searchable else []
            return screen(*(field + rows), bundle="com.apple.MobileAddressBook")
        pages = [page(i) for i in range(12)]
        driver = Driver(pages)
        script = Script([("SCROLL_TO", None, "Bench Tester"), ("DONE", None, None), ("DONE", None, None)])
        with unittest.mock.patch("time.sleep"):
            FrontierAgent(driver, script, screenshots=False, settle_seconds=0).run("Find Bench Tester in Contacts.")
        return [a for a in driver.actions if a[0] == "SWIPE_UP"], driver

    def test_with_a_search_field_it_stops_after_three_swipes(self):
        swipes, _ = self.run_scroll(True)
        self.assertEqual(len(swipes), frontier.SCROLL_TO_SEARCHABLE_SWIPES)

    def test_without_one_it_swipes_as_far_as_before(self):
        swipes, _ = self.run_scroll(False)
        self.assertEqual(len(swipes), frontier.MACRO_SWIPES)


if __name__ == "__main__":
    unittest.main()

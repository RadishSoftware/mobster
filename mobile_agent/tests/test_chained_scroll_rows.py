"""Rows that chained swipes scroll past reach the next prompt (iOSWorld mem-009, 12 Oct: SWIPE_UP, SWIPE_UP in one turn
scrolled "ATL->PIT" and "JFK->SFO" past between the swipes; no prompt ever held them, and the count of cities lost
JFK). Offline: a scripted model, fake screens."""

import unittest

from mobile_agent.frontier import PASSED_HEAD, FrontierAgent, row_texts
from mobile_agent.state import Element
from mobile_agent.tests.test_frontier import Driver, Script, screen

TRIPS = "Which cities did I fly to most?"


def trips(*indices):
    elements = []
    for slot, index in enumerate(indices):
        top = .1 + .25 * slot
        elements.append(Element(str(index), f"Trip {index}, Departed, Seat {index}A", "Button", (0, top, 1, .2)))
        elements.append(Element(f"{index}-seat", f"Seat {index}A", "StaticText", (.6, top + .05, .2, .04)))
    return screen(*elements, bundle="com.example.trips")


def prompts(events):
    return [e["text"] for e in events if e["event"] == "frontier_prompt"]


def run(screens, steps):
    driver, events = Driver(screens), []
    FrontierAgent(driver, Script(steps), emit=events.append, screenshots=False, settle_seconds=0).run(TRIPS)
    return driver, events


class ChainedScrollRowsTests(unittest.TestCase):
    def test_rows_between_two_chained_swipes_reach_the_next_prompt(self):
        pages = [trips(0, 1, 2), trips(3, 4, 5), trips(6, 7, 8), trips(6, 7, 8), trips(6, 7, 8)]
        driver, events = run(pages, [[("SWIPE_UP", None, None), ("SWIPE_UP", None, None)], ("DONE", None, None),
                                     ("DONE", None, None)])
        self.assertEqual([a[0] for a in driver.actions], ["SWIPE_UP", "SWIPE_UP"])
        section = prompts(events)[1].split(PASSED_HEAD, 1)[1].split("\n\n", 1)[0]
        self.assertEqual(section.splitlines(), ["Trip 3, Departed, Seat 3A", "Trip 4, Departed, Seat 4A",
                                                "Trip 5, Departed, Seat 5A"])
        self.assertNotIn(PASSED_HEAD, prompts(events)[2])  # shown once

    def test_rows_the_first_or_last_screen_shows_are_not_repeated(self):
        pages = [trips(0, 1, 2), trips(2, 3, 4), trips(4, 5, 6), trips(4, 5, 6), trips(4, 5, 6)]
        _, events = run(pages, [[("SWIPE_UP", None, None), ("SWIPE_UP", None, None)], ("DONE", None, None),
                                ("DONE", None, None)])
        section = prompts(events)[1].split(PASSED_HEAD, 1)[1].split("\n\n", 1)[0]
        self.assertEqual(section.splitlines(), ["Trip 3, Departed, Seat 3A"])

    def test_one_swipe_adds_nothing(self):
        pages = [trips(0, 1, 2), trips(3, 4, 5), trips(3, 4, 5), trips(3, 4, 5)]
        _, events = run(pages, [("SWIPE_UP", None, None), ("DONE", None, None), ("DONE", None, None)])
        self.assertNotIn(PASSED_HEAD, prompts(events)[1])

    def test_a_swipe_then_a_tap_adds_nothing(self):
        pages = [trips(0, 1, 2), trips(3, 4, 5), trips(3, 4, 5), trips(3, 4, 5)]
        _, events = run(pages, [[("SWIPE_UP", None, None), ("TAP", "Trip 4, Departed, Seat 4A", None)],
                                ("DONE", None, None), ("DONE", None, None)])
        self.assertNotIn(PASSED_HEAD, "\n".join(prompts(events)))

    def test_a_row_is_its_label_not_its_own_texts(self):
        self.assertEqual(row_texts(trips(1)), ["Trip 1, Departed, Seat 1A"])


class WithKeptListsTests(unittest.TestCase):
    """With MOBSTER_CTX_KEPT on, the turn log keeps a list from the screen a turn was decided on; the rows its chained
    swipes scrolled past are the next prompt's own section. The two never hold the same row."""

    def test_the_kept_list_and_the_rows_scrolled_past_are_disjoint(self):
        from mobile_agent.tests.test_context_marks import on, run, screen as titled

        def page(*indices):
            return titled(Element("t", "My Trips", "NavigationBar", (0, .02, 1, .06)),
                          *[Element(str(i), f"Trip {i}, Departed, Seat {i}A", "Button", (0, .1 + .25 * slot, 1, .2))
                            for slot, i in enumerate(indices)])
        detail = titled(Element("t", "Trip 7", "NavigationBar", (0, .02, 1, .06)),
                        Element("d", "Gate E13", "StaticText", (0, .3, .5, .04)))
        home = titled(Element("t", "Home", "NavigationBar", (0, .02, 1, .06)),
                      Element("m", "My Trips", "Button", (.4, .91, .2, .06)))
        with on("KEPT"):  # a list is kept from the second turn on (the run's first screen is not one it left)
            _, _, prompts = run(TRIPS, [home, page(0, 1, 2), page(3, 4, 5), page(6, 7, 8), detail, detail, detail],
                                [("TAP", "My Trips", None),
                                 [("SWIPE_UP", None, None), ("SWIPE_UP", None, None),
                                  ("TAP", "Trip 7, Departed, Seat 7A", None)], ("DONE", None, None),
                                 ("DONE", None, None)])
        prompt = prompts[2]
        kept = prompt.split("\n  read: ", 1)[1].split("\n", 1)[0]
        passed = prompt.split(PASSED_HEAD, 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertIn("Trip 0", kept)  # the screen the turn was decided on, left by the tap
        self.assertEqual(passed, ["Trip 3, Departed, Seat 3A", "Trip 4, Departed, Seat 4A", "Trip 5, Departed, Seat 5A"])
        self.assertFalse(any(row.split(",")[0] in kept for row in passed))

    def test_a_kept_list_still_reads_the_turn_screen_after_an_act(self):
        # The act tracking's loop once reused the name of the turn's screen, and the kept list read a set of hashes.
        from mobile_agent.tests.test_context_lists import KeptListTests
        from mobile_agent.tests.test_context_marks import on, run, screen as titled
        film = KeptListTests.FILM + [Element("s", "Save", "Button", (.6, .45, .3, .04))]
        saved = KeptListTests.FILM + [Element("s", "Saved", "StaticText", (.6, .45, .3, .04))]
        with on("KEPT"):
            _, result, prompts = run("Is The Drama on my Wishlist in Cinephile?",
                                     [titled(*film), titled(*saved), titled(*KeptListTests.LISTS), titled(*saved),
                                      titled(*saved), titled(*saved)],
                                     [("TAP", "Save", None), ("TAP", "My Lists", None), ("TAP", "Movies", None),
                                      ("DONE", None, None), ("DONE", None, None)])
        self.assertEqual(result["status"], "completed")
        self.assertIn("\n  read: My Lists › Wishlist: Create custom list", prompts[3])


if __name__ == "__main__":
    unittest.main()

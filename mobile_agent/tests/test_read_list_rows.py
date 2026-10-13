"""READ_LIST counts rows, not a card's own texts, and a second READ_LIST goes on from where the first stopped
(iOSWorld dev runs, 12 Oct: SkyTrip's trip cards published 13 texts each, so 60 "rows" were ~5 trips, and the next
READ_LIST read the same screen, filled its cap again and never scrolled). Offline: a scripted model, fake screens."""

import unittest
from unittest import mock

from mobile_agent import frontier
from mobile_agent.frontier import FrontierAgent
from mobile_agent.state import Element
from mobile_agent.tests.test_frontier import Driver, Script, screen

TRIPS = "Which cities did I fly to most?"
FIELDS = ("Departed", "Flight", "Seat", "Cost", "Date", "Gate")


def card(index, top):
    """A trip card as SwiftUI publishes it: the Button's combined label, then each of its texts inside it."""
    label = f"SFO, C{index:02d}, " + ", ".join(f"{f} {index}" for f in FIELDS)
    rect = (0, top, 1, .12)
    children = [Element(f"{index}-{n}", text, "StaticText", (.05 + .1 * n, top + .02, .08, .03))
                for n, text in enumerate([f"C{index:02d}"] + [f"{f} {index}" for f in FIELDS])]
    return [Element(f"{index}", label, "Button", rect), *children]


def page(*indices):
    return screen(*[e for slot, index in enumerate(indices) for e in card(index, .1 + .14 * slot)], bundle="com.example.trips")


def prompts(events):
    return [e["text"] for e in events if e["event"] == "frontier_prompt"]


def run(screens, steps, request=TRIPS):
    driver, events = Driver(screens), []
    FrontierAgent(driver, Script(steps), emit=events.append, screenshots=False, settle_seconds=0).run(request)
    return driver, events


class ReadListRowsTests(unittest.TestCase):
    def test_a_list_of_cards_is_read_to_its_end(self):
        # 4 cards a screen, 16 cards: 8 texts a card filled 60 rows on the second screen before; now 16 rows.
        pages = [page(0, 1, 2, 3), page(3, 4, 5, 6), page(6, 7, 8, 9), page(9, 10, 11, 12), page(12, 13, 14, 15),
                 page(12, 13, 14, 15)]
        driver, events = run(pages, [("READ_LIST", None, None), ("DONE", None, None), ("DONE", None, None)])
        listing = prompts(events)[1].split(frontier.LISTING_HEAD, 1)[1]
        for index in range(16):
            self.assertIn(f"SFO, C{index:02d}, Departed {index}", listing)
        self.assertNotIn("\nSeat 3\n", listing)  # a card's own text is not a row of its own
        self.assertNotIn("READ_LIST stopped", prompts(events)[1])
        self.assertEqual([a[0] for a in driver.actions], ["SWIPE_UP"] * 5)

    def test_a_text_inside_a_card_that_its_label_lacks_is_kept(self):
        rows = page(0)
        rows.elements.append(Element("badge", "Delayed 40 min", "StaticText", (.6, .12, .3, .03)))
        driver, events = run([rows, rows], [("READ_LIST", None, None), ("DONE", None, None), ("DONE", None, None)])
        self.assertIn("Delayed 40 min", prompts(events)[1])

    def test_a_second_read_list_goes_on_from_where_the_first_stopped(self):
        pages = [page(0, 1), page(1, 2), page(2, 3), page(3, 4), page(4, 5), page(4, 5)]
        with mock.patch.object(frontier, "READ_LIST_ROWS", 3):
            driver, events = run(pages, [("READ_LIST", None, None), ("READ_LIST", None, None), ("DONE", None, None),
                                         ("DONE", None, None)])
        first, second = (p.split(frontier.LISTING_HEAD, 1)[1].split("\n\n", 1)[0] for p in prompts(events)[1:3])
        self.assertEqual([line[:8] for line in first.splitlines()], ["SFO, C00", "SFO, C01", "SFO, C02"])
        self.assertEqual([line[:8] for line in second.splitlines()], ["SFO, C03", "SFO, C04", "SFO, C05"])
        self.assertEqual([a[0] for a in driver.actions], ["SWIPE_UP"] * 4)

    def test_a_screen_holding_more_rows_than_the_cap_is_read_on_without_a_swipe(self):
        tall = page(0, 1, 2, 3, 4)
        with mock.patch.object(frontier, "READ_LIST_ROWS", 3):
            driver, events = run([tall, tall, tall], [("READ_LIST", None, None), ("READ_LIST", None, None),
                                                      ("DONE", None, None), ("DONE", None, None)])
        second = prompts(events)[2].split(frontier.LISTING_HEAD, 1)[1].split("\n\n", 1)[0]
        self.assertEqual([line[:8] for line in second.splitlines()], ["SFO, C03", "SFO, C04"])

    def test_a_read_list_after_another_action_reads_afresh(self):
        # Moved by another action in between: the rows on screen are read again, as before this change (Row 1 twice).
        def rows(*indices):
            return screen(*[Element(str(i), f"Row {i}", "Cell", (0, .2 + .1 * slot, 1, .08))
                            for slot, i in enumerate(indices)])
        pages = [rows(0, 1), rows(1, 2), rows(1, 2), rows(1, 2), rows(1, 2)]
        with mock.patch.object(frontier, "READ_LIST_ROWS", 2):
            _, events = run(pages, [("READ_LIST", None, None), ("SWIPE_DOWN", None, None), ("READ_LIST", None, None),
                                    ("DONE", None, None), ("DONE", None, None)])
        third = prompts(events)[3].split(frontier.LISTING_HEAD, 1)[1].split("\n\n", 1)[0]
        self.assertEqual(third.splitlines(), ["Row 1", "Row 2"])

    def test_a_list_that_ended_is_read_from_its_top_again(self):
        # The end of a list is not a stop: a later READ_LIST in the same place reads what is there.
        pages = [page(0, 1), page(1, 2), page(1, 2), page(1, 2), page(1, 2)]
        _, events = run(pages, [("READ_LIST", None, None), ("READ_LIST", None, None), ("DONE", None, None),
                                ("DONE", None, None)])
        second = prompts(events)[2].split(frontier.LISTING_HEAD, 1)[1].split("\n\n", 1)[0]
        self.assertIn("SFO, C01", second)


if __name__ == "__main__":
    unittest.main()

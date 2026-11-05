import unittest
from types import SimpleNamespace

from mobile_agent import offers
from mobile_agent.state import Element


def screen(*rows):
    elements = []
    for index, row in enumerate(rows):
        text, selected = row if isinstance(row, tuple) else (row, False)
        elements.append(Element(f"n{index}", text, "StaticText", (0.1, 0.1 + 0.05 * index, 0.8, 0.04),
                                selected=selected))
    return SimpleNamespace(elements=elements)


class OfferReasonTests(unittest.TestCase):
    def test_a_screen_with_an_offer_gives_its_price_period_and_trial(self):
        said = offers.reason(screen("Firefox Plus", "$39.99/year", "7-day free trial", "Start free trial"),
                             "Start free trial", "Spends money")
        self.assertEqual(said, "Spends money. $39.99 a year, after a 7-day free trial.")

    def test_a_plan_named_on_the_price_line_is_named(self):
        self.assertEqual(offers.reason(screen("Family plan $69.99 per year", "Subscribe"), "Subscribe"),
                         "Family plan, $69.99 a year.")

    def test_a_screen_without_a_price_says_so_and_guesses_nothing(self):
        for rows in (("Start free trial",), ("Welcome back", "Continue"), ()):
            with self.subTest(rows=rows):
                self.assertEqual(offers.reason(screen(*rows), "Start free trial"), "Price not shown.")

    def test_a_price_with_no_period_says_the_period_is_not_shown(self):
        self.assertEqual(offers.reason(screen("Total $5.00"), "Pay"), "$5.00 (period not shown).")

    def test_several_prices_with_none_selected_are_listed_not_chosen_between(self):
        said = offers.reason(screen("Annual $39.99 per year", "Monthly $4.99 a month"), "Subscribe")
        self.assertEqual(said, "Several prices on the screen: $39.99 a year, $4.99 a month. Check which is selected.")
        self.assertNotIn("after", said)

    def test_the_selected_plan_wins_when_exactly_one_is_selected(self):
        said = offers.reason(screen(("Annual $39.99 per year", True), "Monthly $4.99 a month"), "Subscribe")
        self.assertEqual(said, "$39.99 a year.")
        both = offers.reason(screen(("Annual $39.99 per year", True), ("Monthly $4.99 a month", True)), "Subscribe")
        self.assertTrue(both.startswith("Several prices"))

    def test_the_same_offer_shown_twice_is_one_offer(self):
        self.assertEqual(offers.reason(screen("$39.99/year", "Subscribe for $39.99 per year"), "Subscribe"),
                         "$39.99 a year.")


if __name__ == "__main__":
    unittest.main()

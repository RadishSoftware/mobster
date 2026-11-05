"""A value recorded from a row the picture can't show (under a sticky footer or a bar, or cut by the screen's edge) is
scrolled into view before the turn leaves the screen: contract_view.hidden_below, Contract.hidden_row and the
agent's one code swipe (FrontierAgent._reveal_found). Rows are taken from the iOSWorld dev runs of 18 Oct."""

import unittest
from types import SimpleNamespace as NS

from mobile_agent import contract as K
from mobile_agent import contract_view as KS
from mobile_agent.frontier import FrontierAgent
from mobile_agent.state import Element, Snapshot


def el(label, role="Button", x=.04, y=.5, w=.92, h=.06, value=""):
    return NS(label=label, role=role, rect=(x, y, w, h), editable=False, value=value)


def hidden(screen, row):
    """hidden_below for ``row`` on ``screen``, its elements in document order (back to front)."""
    return KS.hidden_below(row, screen.index(row), KS.screen_bars(screen))


TAB_BAR = [el(name, x=x, y=.89, w=.16, h=.06) for name, x in (("Home", .08), ("You", .3), ("Cart", .55), ("Menu", .8))]


class HiddenBelowTests(unittest.TestCase):
    def test_a_row_under_a_sticky_footer_is_hidden(self):
        # multi-069: the ride sheet's footer, drawn after the list, lies over most of the Comfort row and its price.
        comfort = el("Comfort, $22.40, Newer cars with extra legroom", y=.84, h=.08)
        price = el("$22.40", "StaticText", .8, .851, .14, .023)
        footer = el("Request CityRideX · $17.50", y=.856, h=.059)
        screen = [comfort, price, footer]
        self.assertTrue(hidden(screen, comfort))
        self.assertTrue(hidden(screen, price), "inside the footer's frame, drawn before it: under it")
        self.assertFalse(hidden(screen, footer), "the footer is in front, and shown")
        label = el("Request CityRideX", "StaticText", .3, .87, .4, .03)
        self.assertFalse(hidden([footer, label], label), "the footer's own label, drawn after it")

    def test_a_card_under_a_tab_bar_of_buttons_is_hidden(self):
        # multi-078: the sunscreen's card runs under MegaMart's tab bar, which is only its four buttons.
        card = el("Neutrogena Ultra Sheer SPF 55 Sunscreen, $10.99, FREE delivery", x=.03, y=.75, w=.89, h=.22)
        self.assertTrue(hidden([card, *TAB_BAR], card))
        tab = el("Home", "StaticText", .1, .9, .1, .02)
        self.assertFalse(hidden([*TAB_BAR[:1], tab, *TAB_BAR[1:]], tab), "a tab's own label, drawn after its button")

    def test_a_row_cut_by_the_screen_foot_is_hidden(self):
        cut = el("Seaport Raw Bar, Seafood, 4.3", x=.8, y=.76, w=.2, h=.24)  # mem-039
        self.assertTrue(hidden([cut], cut))

    def test_a_row_under_the_top_bar_is_not_counted(self):
        # multi-028: no stroke reliably starts inside the content under a header, so no swipe is tried.
        bar = el("#general", role="NavigationBar", x=0, y=.07, w=1, h=.06)
        message = el("Security review for the release is complete.", "StaticText", .19, .09, .63, .05)
        self.assertFalse(hidden([bar, message], message))
        top = el("Giants at Rockies, Thu, Apr 14", y=0, h=.11)  # mem-024: cut at the top
        self.assertFalse(hidden([top], top))

    def test_rows_nothing_lies_over_stay_shown(self):
        # multi-008: a row's own trailing Add button; stayfinder-001: two lines of a card overlapping by a rounding;
        # a row just above the tab bar.
        row, add = el("Protein shake, 1 bottle, 160", x=0, y=.82, w=1), el("Add", x=.82, y=.82, w=.14)
        self.assertFalse(hidden([row, add], row))
        line = el("$327 per night", "StaticText", .05, .87, .35, .03)
        other = el("- Kitchen", "StaticText", .05, .86, .91, .02)
        self.assertFalse(hidden([other, line], line))
        total = el("Order total, $46.95", y=.8, h=.08)
        self.assertFalse(hidden([total, *TAB_BAR], total))
        pane = el("Whole screen", "Other", 0, 0, 1, 1)
        self.assertFalse(hidden([pane], pane), "a pane: no scroll shows all of it")

    def test_a_wide_control_listed_before_a_row_is_not_counted_over_it(self):
        # Draw order is back to front: a control listed first is behind the row (and a tab bar of buttons that an app
        # lists before its screens, as QuickChat does, is missed: no swipe, as before this change).
        bar, row = el("Compose", y=.86, h=.06), el("Order total, $46.95", y=.84, h=.08)
        self.assertFalse(hidden([bar, row], row))


def report(request, what):
    return K.Contract.from_items([{"kind": "REPORT", "app": "CityRide", "what": what, "payload": "", "act": "none",
                                   "count": "", "condition": "", "quote": request}], request)


class HiddenRowTests(unittest.TestCase):
    REQ = "Compare CityRideX with Comfort for Home to Marina."
    COMFORT = el("Comfort, $22.40, Newer cars with extra legroom", y=.84, h=.08)
    FOOTER = el("Request CityRideX · $17.50", y=.86, h=.06)

    def marked(self, value, step=3, status="found"):
        contract = report(self.REQ, "the price difference")
        contract.update([{"id": 1, "status": status, "value": value}], step)
        return contract

    def test_the_row_that_alone_holds_a_value_marked_this_turn(self):
        contract = self.marked("CityRideX $17.50 vs Comfort $22.40 ($4.90 more)")
        self.assertIs(contract.hidden_row([self.COMFORT, self.FOOTER], 3), self.COMFORT)

    def test_no_row_when_the_value_is_shown_elsewhere_earlier_or_missing(self):
        shown = el("Comfort $22.40", "StaticText", .1, .4, .5, .03)
        self.assertIsNone(self.marked("Comfort $22.40").hidden_row([self.COMFORT, self.FOOTER, shown], 3))
        self.assertIsNone(self.marked("Comfort $22.40", step=2).hidden_row([self.COMFORT, self.FOOTER], 3),
                          "marked on an earlier turn: that turn's pictures were what it saw")
        self.assertIsNone(self.marked(None, status="missing").hidden_row([self.COMFORT, self.FOOTER], 3))
        self.assertIsNone(self.marked("CityRideX $17.50").hidden_row([self.COMFORT, self.FOOTER], 3))


# ------------------------------------------------------------------ the agent's swipe

def screen(*elements):
    return Snapshot(list(elements), "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture",
                    bundle_id="com.example.ride")


class Driver:
    def __init__(self, screens):
        self.screens, self.index, self.actions = list(screens), 0, []

    def drag_next(self):
        self.actions.append("drag_next")
        return True

    def observe(self, timeout=10):
        return self.screens[min(self.index, len(self.screens) - 1)]

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append(operation)
        self.index += 1


class Script:
    """A model answering from a list of chunks of (operation, app); turn 1 marks the report item found."""

    def __init__(self, steps, request, value):
        self.steps, self.request, self.value = list(steps), request, value
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def complete(self, messages, schema, timeout=60):
        if "items" in schema["properties"]:
            return {"items": [{"kind": "REPORT", "app": "CityRide", "what": "the price difference", "payload": "",
                               "act": "none", "count": "", "condition": "", "quote": self.request}]}, {}
        self.usage["calls"] += 1
        chunk = self.steps.pop(0)
        return {"thought": "", "plan": "", "notes_add": [], "answer": self.value if chunk[-1][0] == "DONE" else None,
                "checklist_updates": [{"id": 1, "status": "found", "value": self.value}]
                if self.usage["calls"] == 1 else [],
                "actions": [{"operation": op, "target": None, "target_label": None, "text": None, "app": app}
                            for op, app in chunk]}, {}


class RevealSwipeTests(unittest.TestCase):
    REQ = "Compare CityRideX with Comfort for Home to Marina, then open Clock."
    APPS = {"com.example.ride": "CityRide", "com.example.clock": "Clock"}
    COMFORT = Element("1", "Comfort, $22.40, Newer cars with extra legroom", "Button", (.04, .84, .92, .08))
    FOOTER = Element("2", "Request CityRideX · $17.50", "Button", (.04, .86, .92, .06))
    SHOWN = Element("1", "Comfort, $22.40, Newer cars with extra legroom", "Button", (.04, .44, .92, .08))

    def run_agent(self, screens, steps, value="CityRideX $17.50 vs Comfort $22.40"):
        driver, events = Driver(screens), []
        result = FrontierAgent(driver, Script(steps, self.REQ, value), apps=self.APPS, emit=events.append,
                               screenshots=False, settle_seconds=0).run(self.REQ)
        return driver.actions, result, [e for e in events if e.get("event") == "frontier_cleanup"]

    def test_a_hidden_value_is_scrolled_into_view_before_the_app_switch(self):
        hidden, revealed = screen(self.COMFORT, self.FOOTER), screen(self.SHOWN, self.FOOTER)
        actions, _, cleanup = self.run_agent([hidden, revealed, revealed, revealed],
                                             [[("LAUNCH_APP", "com.example.clock")], [("DONE", None)]])
        self.assertEqual(actions[:3], ["drag_next", "SWIPE_UP", "LAUNCH_APP"], "up: XCTest's half-screen drag")
        self.assertEqual([(e["state"], e["reason"], e["operation"]) for e in cleanup],
                         [("sending", "reveal_value", "SWIPE_UP"), ("sent", "reveal_value", "SWIPE_UP")],
                         "journaled before it is sent")

    def test_a_shown_value_switches_apps_with_no_swipe(self):
        shown = screen(self.SHOWN, self.FOOTER)
        actions, _, cleanup = self.run_agent([shown] * 4, [[("LAUNCH_APP", "com.example.clock")], [("DONE", None)]])
        self.assertEqual(actions, ["LAUNCH_APP"])
        self.assertEqual(cleanup, [])

    def test_once_a_turn_even_when_the_row_stays_hidden(self):
        # A footer no swipe moves: HOME then LAUNCH_APP in one chunk swipe once, never again.
        hidden = screen(self.COMFORT, self.FOOTER)
        actions, _, _ = self.run_agent([hidden] * 5, [[("HOME", None), ("LAUNCH_APP", "com.example.clock")],
                                                      [("DONE", None)]])
        self.assertEqual(actions.count("SWIPE_UP"), 1)
        self.assertEqual(actions[:2], ["drag_next", "SWIPE_UP"])

    def test_an_accepted_done_reveals_first(self):
        hidden, revealed = screen(self.COMFORT, self.FOOTER), screen(self.SHOWN, self.FOOTER)
        actions, result, _ = self.run_agent([hidden, revealed, revealed], [[("DONE", None)], [("DONE", None)]])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(actions, ["drag_next", "SWIPE_UP"])


if __name__ == "__main__":
    unittest.main()

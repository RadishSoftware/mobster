"""An input that changed nothing stops its chunk before the next action (verify runs, 12 Oct: SET_TEXT "12" left a
timer's wheel at 25 min and the same chunk tapped Start). Offline: a scripted model and a fake wheel."""

import unittest

from mobile_agent.frontier import FrontierAgent, input_shows
from mobile_agent.state import Element
from mobile_agent.tests.test_frontier import Driver, Script, screen

START = Element("2", "Start", "Button", (.7, .5, .2, .05))
TIMER = "Set a timer for 12 minutes."


def wheel(value):
    return Element("1", "Minutes", "PickerWheel", (.3, .2, .3, .3), value=value)


class WheelDriver(Driver):
    """A Driver whose wheel moves to the value it is given, or stays put when ``stuck``."""

    def __init__(self, screens, stuck):
        super().__init__(screens)
        self.stuck = stuck

    def set_value(self, target, value, timeout=10):
        self.actions.append(("SET_VALUE", target.label, value))
        if not self.stuck:
            self.index += 1


def run(screens, steps, stuck):
    driver, events = WheelDriver(screens, stuck), []
    result = FrontierAgent(driver, Script(steps), emit=events.append, screenshots=False,
                           settle_seconds=0).run(TIMER)
    return driver, result, events


def stops(events):
    return [e.get("reason") for e in events if e["event"] == "frontier_chunk_stop"]


class InputNotTakenTests(unittest.TestCase):
    def test_a_wheel_that_did_not_move_stops_the_chunk_before_start(self):
        at_25 = screen(wheel("25 min"), START)
        driver, _, events = run([at_25, at_25, at_25], [[("SET_TEXT", "Minutes", "12"), ("TAP", "Start", None)],
                                                       ("DONE", None, None), ("DONE", None, None)], stuck=True)
        self.assertEqual(driver.actions, [("SET_VALUE", "Minutes", "12")])
        self.assertIn("input_not_taken", stops(events))

    def test_a_wheel_that_moved_lets_start_run(self):
        driver, _, events = run([screen(wheel("25 min"), START), screen(wheel("12 min"), START),
                                 screen(wheel("12 min"), START)],
                                [[("SET_TEXT", "Minutes", "12"), ("TAP", "Start", None)], ("DONE", None, None),
                                 ("DONE", None, None)], stuck=False)
        self.assertEqual(driver.actions, [("SET_VALUE", "Minutes", "12"), ("TAP", "Start", None)])
        self.assertNotIn("input_not_taken", stops(events))

    def test_a_wheel_already_at_the_value_lets_start_run(self):
        at_12 = screen(wheel("12 min"), START)
        driver, _, events = run([at_12, at_12, at_12], [[("SET_TEXT", "Minutes", "12"), ("TAP", "Start", None)],
                                                       ("DONE", None, None), ("DONE", None, None)], stuck=True)
        self.assertEqual(driver.actions, [("SET_VALUE", "Minutes", "12"), ("TAP", "Start", None)])
        self.assertNotIn("input_not_taken", stops(events))

    def test_a_second_input_still_runs_after_one_that_did_not_take(self):
        hours = Element("3", "Hours", "PickerWheel", (.0, .2, .3, .3), value="0 hours")
        stuck = screen(wheel("25 min"), hours, START)
        driver, _, events = run([stuck] * 3, [[("SET_TEXT", "Minutes", "12"), ("SET_TEXT", "Hours", "1"),
                                               ("TAP", "Start", None)], ("DONE", None, None), ("DONE", None, None)],
                                stuck=True)
        self.assertEqual(driver.actions, [("SET_VALUE", "Minutes", "12"), ("SET_VALUE", "Hours", "1")])
        self.assertIn("input_not_taken", stops(events))

    def test_what_counts_as_showing_the_text(self):
        field = Element("4", "Message", "TextField", (.1, .8, .8, .05), editable=True, value="Dinner at 7")
        self.assertTrue(input_shows(field, "Dinner at 7"))
        self.assertTrue(input_shows(field, " dinner  at 7 "))
        self.assertFalse(input_shows(field, "Dinner at 8"))
        self.assertFalse(input_shows(field, "Dinner"))  # a field's value must be the whole text
        self.assertTrue(input_shows(wheel("25 min"), "25"))
        self.assertTrue(input_shows(wheel("25 min"), "25 min"))
        self.assertFalse(input_shows(wheel("25 min"), "12"))
        self.assertFalse(input_shows(wheel(""), "12"))
        self.assertFalse(input_shows(None, "12"))


if __name__ == "__main__":
    unittest.main()

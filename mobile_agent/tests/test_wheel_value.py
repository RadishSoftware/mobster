"""A picker wheel is set to a value and read back (WDA.set_value). The verify runs of 12 Oct started 25-minute timers
after SET_TEXT "12": the timer's items are "12 min", WDA ignored "12" silently, and set_value returned as if it had
worked. Offline: a fake WDA with one wheel."""

import unittest

from mobile_agent.drivers import WDA, WheelNotSet, wheel_items, wheel_number, wheel_shows
from mobile_agent.frontier import FrontierAgent
from mobile_agent.state import Element
from mobile_agent.tests.test_frontier import Driver, Script, screen


class FakeWheel(WDA):
    """One wheel on a 400x800 screen. ``items`` are what the wheel shows and what WDA's value route accepts (none when
    ``accepts`` is off); ``steps`` says whether WDA's picker-wheel select moves it."""

    def __init__(self, items, at, steps=True, accepts=True):
        super().__init__("http://localhost:8199", "s1")
        WDA._screen_sizes[self.url] = (400, 800)
        self.items, self.at, self.steps, self.accepts, self.calls = list(items), items.index(at), steps, accepts, []

    def call(self, method, path, body=None, timeout=10):
        self.calls.append((method, path, (body or {}).get("value") or (body or {}).get("order")))
        if path == "/elements":
            return [{"ELEMENT": "w"}]
        if path == "/element/w/rect":
            return {"x": 120, "y": 160, "width": 120, "height": 240}
        if path == "/element/w/value":
            if self.accepts and body["value"] in self.items:
                self.at = self.items.index(body["value"])
            return None
        if path == "/element/w/attribute/value":
            return self.items[self.at]
        if path == "/wda/pickerwheel/w/select":
            if self.steps:
                self.at = (self.at + (1 if body["order"] == "next" else -1)) % len(self.items)
            return None
        raise AssertionError(path)

    def _dispatching(self):
        pass


def wheel():
    return Element("1", "", "PickerWheel", (.3, .2, .3, .3), value="")


TIMER = [f"{n} min" for n in range(60)]
ALARM = [f"{n:02d}" for n in range(60)]


class WheelValueTests(unittest.TestCase):
    def test_a_timer_wheel_takes_its_own_spelling(self):
        driver = FakeWheel(TIMER, "25 min")
        driver.set_value(wheel(), "12")
        self.assertEqual(driver.items[driver.at], "12 min")
        self.assertNotIn("select", " ".join(path for _, path, _ in driver.calls))

    def test_an_alarm_minute_takes_two_digits(self):
        driver = FakeWheel(ALARM, "17")
        driver.set_value(wheel(), "0")
        self.assertEqual(driver.items[driver.at], "00")

    def test_a_wheel_already_at_the_value_is_left_alone(self):
        driver = FakeWheel(TIMER, "12 min")
        driver.set_value(wheel(), "12")
        self.assertEqual([c for c in driver.calls if c[0] == "POST" and c[1] != "/elements"], [])

    def test_a_wheel_no_spelling_reaches_is_stepped_there(self):
        driver = FakeWheel(TIMER, "25 min", accepts=False)
        driver.set_value(wheel(), "12")
        self.assertEqual(driver.items[driver.at], "12 min")
        self.assertEqual(sum(1 for _, path, _ in driver.calls if path.endswith("/select")), 13)

    def test_a_wheel_that_will_not_move_raises(self):
        driver = FakeWheel(TIMER, "25 min", steps=False, accepts=False)
        with self.assertRaises(WheelNotSet) as raised:
            driver.set_value(wheel(), "12")
        self.assertIn("'25 min'", str(raised.exception))
        self.assertEqual(driver.items[driver.at], "25 min")

    def test_a_word_wheel_is_set_or_refused(self):
        driver = FakeWheel(["AM", "PM"], "PM")
        driver.set_value(wheel(), "AM")
        self.assertEqual(driver.items[driver.at], "AM")
        stuck = FakeWheel(["AM", "PM"], "PM", steps=False)
        with self.assertRaises(WheelNotSet):
            stuck.set_value(wheel(), "noon")

    def test_spellings_and_matching(self):
        self.assertEqual(wheel_items("12", "25 min"), ["12", "12 min", "12 mins"])
        self.assertIn("12 min", wheel_items("12", "25 min"))
        self.assertIn("00", wheel_items("0", "17"))
        self.assertIn("1 hour", wheel_items("1", "0 hours"))
        self.assertIn("6", wheel_items("6 o’clock", "7 o’clock"))
        self.assertEqual(wheel_number("05 minutes"), 5)
        self.assertIsNone(wheel_number("AM"))
        self.assertTrue(wheel_shows("12 min", "12"))
        self.assertTrue(wheel_shows("00", "0"))
        self.assertTrue(wheel_shows("AM", "am"))
        self.assertFalse(wheel_shows("25 min", "12"))
        self.assertFalse(wheel_shows("PM", "AM"))
        self.assertFalse(wheel_shows(None, "12"))


class StuckWheelDriver(Driver):
    def set_value(self, target, value, timeout=10):
        self.actions.append(("SET_VALUE", target.label, value))
        raise WheelNotSet(f"the wheel shows '25 min', not {value!r}: none of its items matched")


class WheelInTheLoopTests(unittest.TestCase):
    def test_a_wheel_not_set_stops_the_chain_and_says_what_it_shows(self):
        minutes = Element("1", "Minutes", "PickerWheel", (.3, .2, .3, .3), value="25 min")
        start = Element("2", "Start", "Button", (.7, .5, .2, .05))
        shown = screen(minutes, start)
        driver, events, prompts = StuckWheelDriver([shown] * 3), [], []
        script = Script([[("SET_TEXT", "Minutes", "12"), ("TAP", "Start", None)], ("DONE", None, None),
                         ("DONE", None, None)])
        complete = script.complete

        def remember(messages, schema, timeout=60):
            prompts.append(str(messages))
            return complete(messages, schema, timeout)
        script.complete = remember
        FrontierAgent(driver, script, emit=events.append, screenshots=False, settle_seconds=0).run(
            "Set a timer for 12 minutes.")
        self.assertEqual(driver.actions, [("SET_VALUE", "Minutes", "12")])
        self.assertIn("WheelNotSet", " ".join(e.get("error", "") for e in events if e["event"] == "frontier_failed"))
        self.assertTrue(any("25 min" in p and "none of its items matched" in p for p in prompts[1:]))


if __name__ == "__main__":
    unittest.main()

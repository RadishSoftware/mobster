import unittest

from mobile_agent.drivers import WDA
from mobile_agent.frontier import FrontierAgent
from mobile_agent.state import Element, from_wda
from mobile_agent.tests.test_frontier import Driver, Script, screen
from mobile_agent.wda.text import WheelNotSet


class FakeWheel(WDA):
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


class WheelValueTests(unittest.TestCase):
    def test_a_timer_wheel_takes_its_own_spelling(self):
        driver = FakeWheel(TIMER, "25 min")
        driver.set_value(wheel(), "12")
        self.assertEqual(driver.items[driver.at], "12 min")
        self.assertNotIn("select", " ".join(path for _, path, _ in driver.calls))

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


class StuckWheelDriver(Driver):
    capabilities = Driver.capabilities | {"set_value"}

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


CHAT = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" label="QuickChat" x="0" y="0" width="402" height="874">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="402" height="874">
    <XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="402" height="874">
      <XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="402" height="780">
        <XCUIElementTypeButton type="XCUIElementTypeButton" label="Add status" x="16" y="340" width="200" height="80"/>
      </XCUIElementTypeOther>
      <XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="402" height="780">
        <XCUIElementTypeButton type="XCUIElementTypeButton" label="Sofia Kim" x="16" y="340" width="370" height="120"/>
      </XCUIElementTypeOther>
      <XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="402" height="780">
        <XCUIElementTypeButton type="XCUIElementTypeButton" label="Keypad" x="16" y="340" width="200" height="80"/>
      </XCUIElementTypeOther>
      <XCUIElementTypeTabBar type="XCUIElementTypeTabBar" label="Tab Bar" x="0" y="780" width="402" height="94"/>
    </XCUIElementTypeOther>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>"""

PARENT = "/XCUIElementTypeApplication/XCUIElementTypeWindow[1]/XCUIElementTypeOther[1]"


class TabPageTests(unittest.TestCase):
    def test_same_frame_sibling_pages_are_reported_with_their_positions(self):
        snapshot = from_wda(CHAT)
        self.assertEqual(snapshot.stacked_pages, ((PARENT, ((0, PARENT + "/XCUIElementTypeOther[1]"),
                                                            (1, PARENT + "/XCUIElementTypeOther[2]"),
                                                            (2, PARENT + "/XCUIElementTypeOther[3]"))),))

    def test_two_same_frame_siblings_are_an_overlay_not_tabs(self):
        third = """      <XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="402" height="780">
        <XCUIElementTypeButton type="XCUIElementTypeButton" label="Keypad" x="16" y="340" width="200" height="80"/>
      </XCUIElementTypeOther>
"""
        self.assertIn(third, CHAT)
        self.assertEqual(from_wda(CHAT.replace(third, "")).stacked_pages, ())

    def test_the_driver_hides_the_pages_wda_reports_not_visible(self):
        driver = WDA("http://127.0.0.1:8100", "s")
        calls = []

        def call(method, path, body=None, timeout=10):
            calls.append((method, path, body))
            if path == "/source?format=xml&excluded_attributes=visible,accessible,index,traits":
                return CHAT
            if path == "/elements":
                return [{"ELEMENT": "P2"}, {"ELEMENT": "BAR"}]
            return {"/element/P2/attribute/index": 1, "/element/BAR/attribute/index": 2}.get(path)
        driver.call = call
        labels = [e.label for e in driver.observe().elements]
        self.assertIn("Sofia Kim", labels)
        self.assertNotIn("Add status", labels)
        self.assertIn("XCUIElementTypeWindow[1]/XCUIElementTypeOther[1]/XCUIElementTypeOther[`visible == 1`]",
                      next(body["value"] for _, path, body in calls if path == "/elements"))

    def test_the_answer_is_kept_until_a_tap_outside_the_pages(self):
        driver = WDA("http://127.0.0.1:8100", "s")
        asked = []

        def call(method, path, body=None, timeout=10):
            if path.startswith("/source"):
                return CHAT
            if path == "/elements":
                asked.append(body["value"])
                return [{"ELEMENT": "P2"}]
            return {"/element/P2/attribute/index": 1}.get(path)
        driver.call = call
        driver._check_current = lambda snapshot, deadline: None
        driver._tap = lambda x, y, deadline: None
        snapshot = driver.observe()
        row = next(e for e in snapshot.elements if e.label == "Sofia Kim")
        driver.execute("TAP", row, snapshot)
        driver.observe()
        self.assertEqual(len(asked), 1)
        driver._note_tap(PARENT + "/XCUIElementTypeTabBar[1]/XCUIElementTypeButton[2]")
        driver.observe()
        self.assertEqual(len(asked), 2)

    def test_a_relaunch_asks_which_page_shows_again(self):
        driver = WDA("http://127.0.0.1:8100", "s")
        asked = []

        def call(method, path, body=None, timeout=10):
            if path.startswith("/source"):
                return CHAT
            if path == "/elements":
                asked.append(body["value"])
                return [{"ELEMENT": "P2"}]
            return {"/element/P2/attribute/index": 1}.get(path)
        driver.call = call
        driver.http = type("Http", (), {"request": staticmethod(lambda *a, **k: {})})()
        driver._check_current = lambda snapshot, deadline: None
        driver.observe()
        driver.observe()
        self.assertEqual(len(asked), 1)
        self.assertTrue(driver.relaunch_app("com.example.quickchat"))
        driver.observe()
        self.assertEqual(len(asked), 2)

    def test_an_unknown_answer_hides_nothing(self):
        driver = WDA("http://127.0.0.1:8100", "s")
        driver.call = lambda method, path, body=None, timeout=10: (
            CHAT if path.startswith("/source") else [] if path == "/elements" else None)
        labels = [e.label for e in driver.observe().elements]
        self.assertIn("Sofia Kim", labels)
        self.assertIn("Add status", labels)


SEARCH = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" label="QuickChat" x="0" y="0" width="402" height="874">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="402" height="874">
    <XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="402" height="874">
      <XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="460" width="402" height="80">
        <XCUIElementTypeButton type="XCUIElementTypeButton" label="Updates" x="16" y="480" width="48" height="48"/>
        <XCUIElementTypeButton type="XCUIElementTypeButton" label="Calls" x="104" y="480" width="32" height="48"/>
        <XCUIElementTypeButton type="XCUIElementTypeButton" label="63, QuickChat" x="248" y="468" width="64" height="60"/>
        <XCUIElementTypeButton type="XCUIElementTypeButton" label="Me" x="348" y="480" width="28" height="48"/>
      </XCUIElementTypeOther>
      <XCUIElementTypeScrollView type="XCUIElementTypeScrollView" x="0" y="0" width="402" height="874">
        <XCUIElementTypeButton type="XCUIElementTypeButton" label="Weekend Trip" x="16" y="376" width="368" height="122"/>
        <XCUIElementTypeButton type="XCUIElementTypeButton" label="Sofia Kim" x="16" y="512" width="368" height="120"/>
      </XCUIElementTypeScrollView>
      <XCUIElementTypeButton type="XCUIElementTypeButton" label="Got It!" x="60" y="490" width="120" height="30"/>
    </XCUIElementTypeOther>
    <XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="584" width="402" height="290">
      <XCUIElementTypeKeyboard type="XCUIElementTypeKeyboard" x="0" y="584" width="402" height="232">
        <XCUIElementTypeKey type="XCUIElementTypeKey" label="Q" x="4" y="594" width="36" height="46"/>
      </XCUIElementTypeKeyboard>
    </XCUIElementTypeOther>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>"""  # noqa: E501


class CoveringBandTests(unittest.TestCase):
    def test_a_result_between_the_tab_bar_and_the_keyboard_is_not_a_target(self):
        labels = [e.label for e in from_wda(SEARCH).elements]
        self.assertNotIn("Sofia Kim", labels)
        self.assertIn("Weekend Trip", labels)
        self.assertIn("63, QuickChat", labels)
        self.assertIn("Q", labels)

    def test_a_row_below_a_bottom_tab_bar_is_under_its_background(self):
        source = SEARCH.replace('y="480" width="48" height="48"', 'y="750" width="48" height="48"').replace(
            'y="480" width="32" height="48"', 'y="750" width="32" height="48"').replace(
            'y="468" width="64" height="60"', 'y="738" width="64" height="60"').replace(
            'y="480" width="28" height="48"', 'y="750" width="28" height="48"')
        source = source.replace(
            '<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="584" width="402" height="290">',
            '<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="900" width="402" height="1">')
        source = source.replace('x="0" y="584" width="402" height="232"', 'x="0" y="900" width="402" height="232"')
        source = source.replace('label="Q" x="4" y="594"', 'label="Q" x="4" y="910"')
        source = source.replace('label="Sofia Kim" x="16" y="512" width="368" height="120"',
                                'label="Sofia Kim" x="16" y="804" width="368" height="70"')
        labels = [e.label for e in from_wda(source).elements]
        self.assertNotIn("Sofia Kim", labels)
        self.assertIn("63, QuickChat", labels)

    def test_a_tooltip_drawn_after_the_tab_bar_stays_a_target(self):
        self.assertIn("Got It!", [e.label for e in from_wda(SEARCH).elements])

    def test_a_partly_covered_row_is_tapped_on_its_visible_part(self):
        source = SEARCH.replace('label="Weekend Trip" x="16" y="376" width="368" height="122"',
                                'label="Weekend Trip" x="16" y="420" width="368" height="100"')
        trip = next(e for e in from_wda(source).elements if e.label == "Weekend Trip")
        self.assertLess(trip.tap_point[1] * 874, 474)


if __name__ == "__main__":
    unittest.main()

"""SwiftUI keeps every tab of a TabView in the tree: only the page WDA reports visible is on screen. Offline."""

import unittest

from mobile_agent.drivers import WDA
from mobile_agent.state import from_wda

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
        self.assertEqual(len(asked), 1)  # a tap inside the page shown switches no tab
        driver._note_tap(PARENT + "/XCUIElementTypeTabBar[1]/XCUIElementTypeButton[2]")
        driver.observe()
        self.assertEqual(len(asked), 2)  # a tab-bar tap: asked again

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
</XCUIElementTypeApplication>"""


class CoveringBandTests(unittest.TestCase):
    def test_a_result_between_the_tab_bar_and_the_keyboard_is_not_a_target(self):
        labels = [e.label for e in from_wda(SEARCH).elements]
        self.assertNotIn("Sofia Kim", labels)  # 26 pt showed, and took no taps (live, 25 Sep)
        self.assertIn("Weekend Trip", labels)
        self.assertIn("63, QuickChat", labels)  # a two-line tab is on the bar's line, not under it
        self.assertIn("Q", labels)

    def test_a_row_below_a_bottom_tab_bar_is_under_its_background(self):
        source = SEARCH.replace('y="480" width="48" height="48"', 'y="750" width="48" height="48"').replace(
            'y="480" width="32" height="48"', 'y="750" width="32" height="48"').replace(
            'y="468" width="64" height="60"', 'y="738" width="64" height="60"').replace(
            'y="480" width="28" height="48"', 'y="750" width="28" height="48"')
        source = source.replace('<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="584" width="402" height="290">',
                                '<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="900" width="402" height="1">')
        source = source.replace('x="0" y="584" width="402" height="232"', 'x="0" y="900" width="402" height="232"')
        source = source.replace('label="Q" x="4" y="594"', 'label="Q" x="4" y="910"')  # no keyboard up
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

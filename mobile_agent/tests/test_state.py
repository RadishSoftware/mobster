import re
import unittest

from mobile_agent.drivers import WDA
from mobile_agent.state import from_wda, invisible_marks, pane_key, validate_input_text, wda_occlusion
from mobile_agent.transport import Deadline

CALCULATOR = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Calculator" label="Calculator"
    bundleId="com.apple.calculator" x="0" y="0" width="393" height="852">
  <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="‎123‎+‎456"
      x="20" y="200" width="350" height="40"/>
  <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Edit field" value="‎579"
      x="20" y="250" width="350" height="80"/>
</XCUIElementTypeApplication>"""


class InvisibleMarkTests(unittest.TestCase):
    def test_calculator_display_reads_as_the_digits_it_shows(self):
        snapshot = from_wda(CALCULATOR)
        texts = {e.label for e in snapshot.elements} | {e.value for e in snapshot.elements}
        self.assertIn("579", texts)
        self.assertIn("123+456", texts)
        self.assertFalse(any("‎" in t for t in texts if t))

    def test_visible_text_is_untouched(self):
        self.assertEqual(invisible_marks("Café 12 km"), "Café 12 km")
        self.assertEqual(invisible_marks("﻿a​b⁦c⁩"), "abc")


class LongFieldTests(unittest.TestCase):
    def test_a_long_text_view_keeps_its_end(self):
        body = "Launch plan. " * 80 + "Note: Q2 planning."
        xml = ('<?xml version="1.0" encoding="UTF-8"?><XCUIElementTypeApplication type="XCUIElementTypeApplication" '
               'label="Docs" x="0" y="0" width="393" height="852"><XCUIElementTypeTextView '
               f'type="XCUIElementTypeTextView" label="Body" value="{body}" x="10" y="100" width="370" height="600"/>'
               '</XCUIElementTypeApplication>')
        value = from_wda(xml).elements[0].value
        self.assertTrue(value.endswith("Note: Q2 planning."))
        self.assertTrue(value.startswith("Launch plan."))
        self.assertLessEqual(len(value), 500)


class ParkedKeyboardTests(unittest.TestCase):
    def source(self, keyboard_y):
        return ('<?xml version="1.0" encoding="UTF-8"?><XCUIElementTypeApplication type="XCUIElementTypeApplication" '
                'label="SplitPay" x="0" y="0" width="402" height="874"><XCUIElementTypeTextField '
                'type="XCUIElementTypeTextField" value="Find a person" label="" x="70" y="92" width="224" '
                'height="24"/><XCUIElementTypeKeyboard type="XCUIElementTypeKeyboard" x="0" '
                f'y="{keyboard_y}" width="402" height="233"/></XCUIElementTypeApplication>')

    def test_an_off_screen_keyboard_is_parked_and_fields_can_submit(self):
        snapshot = from_wda(self.source(891))
        self.assertEqual(snapshot.keyboard, "parked")
        self.assertIn("SUBMIT", snapshot.elements[0].actions)
        self.assertEqual(from_wda(self.source(641)).keyboard, "visible")


class PickerAndMultilineTests(unittest.TestCase):
    def test_picker_wheels_are_elements_by_value(self):
        xml = ('<?xml version="1.0" encoding="UTF-8"?><XCUIElementTypeApplication type="XCUIElementTypeApplication" '
               'label="Clock" x="0" y="0" width="402" height="874"><XCUIElementTypePickerWheel '
               'type="XCUIElementTypePickerWheel" value="6 o’clock" x="111" y="102" width="55" height="292"/>'
               '</XCUIElementTypeApplication>')
        wheel = from_wda(xml).elements[0]
        self.assertEqual((wheel.role, wheel.value), ("PickerWheel", "6 o’clock"))

    def test_line_breaks_are_text_only_in_a_text_view(self):
        self.assertEqual(validate_input_text("one\ntwo", multiline=True), "one\ntwo")
        with self.assertRaises(ValueError):
            validate_input_text("one\ntwo")


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


NOTES = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" label="Notes" bundleId="com.apple.mobilenotes"
    x="0" y="0" width="393" height="852">
  <XCUIElementTypeCollectionView type="XCUIElementTypeCollectionView" label="Search results" x="0" y="0"
      width="393" height="852">
    <XCUIElementTypeCell type="XCUIElementTypeCell" label="MobsterBench Note, Locker code: 4817" x="16" y="99"
        width="361" height="81"/>
  </XCUIElementTypeCollectionView>
  <XCUIElementTypeCollectionView type="XCUIElementTypeCollectionView" x="0" y="0" width="393" height="852">
    <XCUIElementTypeCell type="XCUIElementTypeCell" label="Quick Notes" x="16" y="59" width="361" height="52"/>
    <XCUIElementTypeCell type="XCUIElementTypeCell" label="All iCloud" x="16" y="224" width="361" height="53"/>
  </XCUIElementTypeCollectionView>
</XCUIElementTypeApplication>"""


class StackedPaneTests(unittest.TestCase):
    def test_overlapping_screen_sized_lists_are_reported(self):
        snapshot = from_wda(NOTES)
        self.assertEqual(set(snapshot.stacked_panes),
                         {pane_key("CollectionView", "Search results", 0, 0, 393, 852),
                          pane_key("CollectionView", "", 0, 0, 393, 852)})
        single = NOTES.replace('label="Search results" x="0" y="0"', 'label="Search results" x="0" y="600"')
        single = single.replace('width="393" height="852">\n    <XCUIElementTypeCell type="XCUIElementTypeCell" '
                                'label="MobsterBench', 'width="393" height="100">\n    <XCUIElementTypeCell '
                                'type="XCUIElementTypeCell" label="MobsterBench')
        self.assertEqual(from_wda(single).stacked_panes, ())

    def test_the_pane_wda_reports_hidden_is_skipped_so_the_results_show(self):
        hidden = frozenset({pane_key("CollectionView", None, 0, 0, 393, 852)})
        labels = [e.label for e in from_wda(NOTES, hidden).elements]
        self.assertIn("MobsterBench Note, Locker code: 4817", labels)
        self.assertNotIn("Quick Notes", labels)


SHEET = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" label="Bluesky" x="0" y="0" width="402" height="874">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="402" height="874">
    <XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="402" height="874">
      <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Today" x="16" y="100" width="100" height="40"/>
      <XCUIElementTypeButton type="XCUIElementTypeButton" label="Add" x="300" y="500" width="60" height="40"/>
    </XCUIElementTypeOther>
    <XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="402" height="874">
      <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Search Food" x="16" y="135" width="200" height="42"/>
      <XCUIElementTypeButton type="XCUIElementTypeButton" label="Close" x="16" y="82" width="60" height="36"/>
    </XCUIElementTypeOther>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>"""  # noqa: E501
BEHIND = "/XCUIElementTypeApplication/XCUIElementTypeWindow[1]/XCUIElementTypeOther[1]"


class PresentedLayerTests(unittest.TestCase):
    def test_a_screen_sized_layer_behind_a_later_one_is_reported(self):
        self.assertEqual(from_wda(SHEET).stacked_layers, (BEHIND,))
        single = re.sub(
            r"<XCUIElementTypeOther[^>]*>\s*<XCUIElementTypeStaticText[^>]*Search Food.*?</XCUIElementTypeOther>",
            "", SHEET, flags=re.S)
        self.assertEqual(from_wda(single).stacked_layers, ())

    def test_the_layer_wda_reports_hidden_is_skipped(self):
        labels = [e.label for e in from_wda(SHEET, hidden_paths=frozenset({BEHIND})).elements]
        self.assertEqual(labels, ["Search Food", "Close"])


class LayerVisibilityAcrossAnActionTests(unittest.TestCase):
    def driver(self):
        driver = WDA("http://localhost:8100", "test")
        driver.state = {"behind_visible": False}

        def call(method, path, body=None, timeout=10):
            if path.startswith("/source"):
                return SHEET
            if path == "/elements":
                return [{"ELEMENT": "layer-1"}]
            if path.endswith("/attribute/visible"):
                return driver.state["behind_visible"]
            return None
        driver.call = call
        return driver

    def labels(self, driver):
        return [e.label for e in driver.observe(timeout=5).elements]

    def test_an_action_asks_again_which_layer_shows(self):
        driver = self.driver()
        self.assertEqual(self.labels(driver), ["Search Food", "Close"])
        driver._tap(30, 100, Deadline(5))
        driver.state["behind_visible"] = True
        self.assertIn("Today", self.labels(driver))

    def test_without_an_action_the_answer_is_reused(self):
        driver = self.driver()
        self.assertEqual(self.labels(driver), ["Search Food", "Close"])
        driver.state["behind_visible"] = True
        self.assertEqual(self.labels(driver), ["Search Food", "Close"])


if __name__ == "__main__":
    unittest.main()

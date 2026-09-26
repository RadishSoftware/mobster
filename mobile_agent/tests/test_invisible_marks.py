"""Invisible direction and width marks never reach evidence or the step model. Offline."""

import unittest

from mobile_agent.state import from_wda, invisible_marks

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


if __name__ == "__main__":
    unittest.main()


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
    """A hardware keyboard parks iOS's keyboard below the screen; keystrokes still reach the field."""

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

    def test_an_on_screen_keyboard_is_visible(self):
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
        from mobile_agent.state import validate_input_text
        self.assertEqual(validate_input_text("one\ntwo", multiline=True), "one\ntwo")
        with self.assertRaises(ValueError):
            validate_input_text("one\ntwo")

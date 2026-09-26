"""A zero-size wrapper WDA calls not visible does not hide its visible children (iOS 26 glass toolbars). Offline."""

import unittest

from mobile_agent.state import from_wda

MAIL = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" label="Mail" bundleId="com.example.mail"
    x="0" y="0" width="402" height="874">
  <XCUIElementTypeButton type="XCUIElementTypeButton" label="Mailboxes" visible="true" x="19" y="66" width="104"
      height="36"/>
  <XCUIElementTypeToolbar type="XCUIElementTypeToolbar" label="Toolbar" visible="true" x="0" y="788" width="402"
      height="86">
    <XCUIElementTypeOther type="XCUIElementTypeOther" visible="false" x="28" y="798" width="0" height="0">
      <XCUIElementTypeOther type="XCUIElementTypeOther" visible="true" x="32" y="803" width="337" height="38">
        <XCUIElementTypeSearchField type="XCUIElementTypeSearchField" label="Search Mail" value="Search Mail"
            visible="true" x="32" y="803" width="337" height="38"/>
      </XCUIElementTypeOther>
    </XCUIElementTypeOther>
  </XCUIElementTypeToolbar>
  <XCUIElementTypeOther type="XCUIElementTypeOther" visible="false" x="0" y="300" width="402" height="200">
    <XCUIElementTypeButton type="XCUIElementTypeButton" label="Hidden action" visible="true" x="10" y="310"
        width="100" height="40"/>
  </XCUIElementTypeOther>
</XCUIElementTypeApplication>"""


class GlassWrapperTests(unittest.TestCase):
    def test_a_field_inside_a_zero_size_hidden_wrapper_is_on_screen(self):
        snapshot = from_wda(MAIL)
        field = next(e for e in snapshot.elements if e.label == "Search Mail")
        self.assertTrue(field.editable)
        self.assertIn("/XCUIElementTypeOther[1]/XCUIElementTypeOther[1]/XCUIElementTypeSearchField[1]", field.locator)

    def test_a_hidden_container_with_area_still_hides_its_subtree(self):
        self.assertNotIn("Hidden action", [e.label for e in from_wda(MAIL).elements])

    def test_an_inaccessible_button_filling_a_labelled_wrapper_takes_its_label(self):
        source = MAIL.replace("</XCUIElementTypeApplication>", """
  <XCUIElementTypeOther type="XCUIElementTypeOther" label="Filters" accessible="true" x="354" y="75" width="32"
      height="33">
    <XCUIElementTypeButton type="XCUIElementTypeButton" name="line.3.horizontal" label="Drag" accessible="false"
        x="354" y="75" width="32" height="33"/>
  </XCUIElementTypeOther>
</XCUIElementTypeApplication>""")
        labels = [e.label for e in from_wda(source).elements]
        self.assertIn("Filters", labels)
        self.assertNotIn("Drag", labels)


class ContentTapTests(unittest.TestCase):
    ROW = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" label="TeamChat" x="0" y="0" width="402" height="874">
  <XCUIElementTypeButton type="XCUIElementTypeButton" label="#general, 2" x="20" y="451" width="366" height="21">
    <XCUIElementTypeImage type="XCUIElementTypeImage" label="number" x="20" y="454" width="13" height="15"/>
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="#general" x="48" y="451" width="67" height="21"/>
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="2" x="366" y="451" width="20" height="21"/>
  </XCUIElementTypeButton>
  <XCUIElementTypeButton type="XCUIElementTypeButton" label="Lazy Bear" x="16" y="500" width="370" height="120">
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Lazy Bear" x="16" y="540" width="370" height="40"/>
  </XCUIElementTypeButton>
</XCUIElementTypeApplication>"""

    def test_a_wide_row_with_an_empty_centre_is_tapped_on_its_text(self):
        row = next(e for e in from_wda(self.ROW).elements if e.label == "#general, 2")
        x, y = row.tap_point
        self.assertAlmostEqual(x * 402, 48 + 67 / 2, delta=3)
        self.assertAlmostEqual(y * 874, 451 + 21 / 2, delta=3)

    def test_a_row_whose_centre_hits_its_content_is_tapped_at_the_centre(self):
        row = next(e for e in from_wda(self.ROW).elements if e.label == "Lazy Bear")
        self.assertEqual(row.tap_point, row.center)


class PhantomCellTests(unittest.TestCase):
    # A fast source (no visibility): the second cell is not rendered, and its text reports the
    # table's top edge instead of the cell's own off-screen frame.
    TABLE = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" label="Mail" x="0" y="0" width="402" height="874">
  <XCUIElementTypeTable type="XCUIElementTypeTable" x="0" y="250" width="402" height="624">
    <XCUIElementTypeCell type="XCUIElementTypeCell" name="mail_row_a" x="0" y="250" width="402" height="96">
      <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Robinhood" x="72" y="264" width="260" height="16"/>
    </XCUIElementTypeCell>
    <XCUIElementTypeCell type="XCUIElementTypeCell" name="mail_row_b" x="0" y="1210" width="402" height="96">
      <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Yesterday" x="0" y="250" width="56" height="15"/>
    </XCUIElementTypeCell>
  </XCUIElementTypeTable>
</XCUIElementTypeApplication>"""

    def test_text_of_an_unrendered_cell_is_off_screen_with_its_cell(self):
        snapshot = from_wda(self.TABLE)
        self.assertIn("Robinhood", [e.label for e in snapshot.elements])
        self.assertNotIn("Yesterday", [e.label for e in snapshot.elements])
        phantom = next(node for node in snapshot.offscreen if node.label == "Yesterday")
        self.assertEqual(phantom.direction, "below")


if __name__ == "__main__":
    unittest.main()

"""Two screen-sized lists drawn over one another: WDA's visibility decides which is shown. Offline."""

import unittest

from mobile_agent.state import from_wda, pane_key

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

    def test_the_pane_wda_reports_hidden_is_skipped_so_the_results_show(self):
        hidden = frozenset({pane_key("CollectionView", None, 0, 0, 393, 852)})
        labels = [e.label for e in from_wda(NOTES, hidden).elements]
        self.assertIn("MobsterBench Note, Locker code: 4817", labels)
        self.assertNotIn("Quick Notes", labels)

    def test_a_single_list_is_not_stacked(self):
        single = NOTES.replace('label="Search results" x="0" y="0"', 'label="Search results" x="0" y="600"')
        single = single.replace('width="393" height="852">\n    <XCUIElementTypeCell type="XCUIElementTypeCell" '
                                'label="MobsterBench', 'width="393" height="100">\n    <XCUIElementTypeCell '
                                'type="XCUIElementTypeCell" label="MobsterBench')
        self.assertEqual(from_wda(single).stacked_panes, ())


if __name__ == "__main__":
    unittest.main()


SHEET = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" label="CalTrack" x="0" y="0" width="402" height="874">
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
</XCUIElementTypeApplication>"""
BEHIND = "/XCUIElementTypeApplication/XCUIElementTypeWindow[1]/XCUIElementTypeOther[1]"


class PresentedLayerTests(unittest.TestCase):
    def test_a_screen_sized_layer_behind_a_later_one_is_reported(self):
        self.assertEqual(from_wda(SHEET).stacked_layers, (BEHIND,))

    def test_the_layer_wda_reports_hidden_is_skipped(self):
        labels = [e.label for e in from_wda(SHEET, hidden_paths=frozenset({BEHIND})).elements]
        self.assertEqual(labels, ["Search Food", "Close"])

    def test_a_single_layer_reports_nothing(self):
        import re
        single = re.sub(r"<XCUIElementTypeOther[^>]*>\s*<XCUIElementTypeStaticText[^>]*Search Food.*?</XCUIElementTypeOther>",
                        "", SHEET, flags=re.S)
        self.assertEqual(from_wda(single).stacked_layers, ())

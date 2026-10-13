"""Two screen-sized lists drawn over one another: WDA's visibility decides which is shown. Offline."""

import unittest

from mobile_agent.drivers import Deadline
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


class LayerVisibilityAcrossAnActionTests(unittest.TestCase):
    """WDA's answer on which window layer is hidden is kept a few seconds, never across an action. A sheet closing
    leaves its container in the tree for a moment, so the layer behind it stacks as before: the answer from while the
    sheet was up hid the screen the action uncovered (iOSWorld splitpay-001, 11 Oct: only the keyboard was read)."""

    def driver(self):
        from mobile_agent.drivers import WDA
        driver = WDA("http://localhost:8100", "test")
        driver.state = {"behind_visible": False, "probes": 0}

        def call(method, path, body=None, timeout=10):
            if path.startswith("/source"):
                return SHEET
            if path == "/elements":
                return [{"ELEMENT": "layer-1"}]
            if path.endswith("/attribute/visible"):
                driver.state["probes"] += 1
                return driver.state["behind_visible"]
            return None
        driver.call = call
        return driver

    def labels(self, driver):
        return [e.label for e in driver.observe(timeout=5).elements]

    def test_an_action_asks_again_which_layer_shows(self):
        driver = self.driver()
        self.assertEqual(self.labels(driver), ["Search Food", "Close"])  # the sheet is up
        driver._tap(30, 100, Deadline(5))  # its button
        driver.state["behind_visible"] = True  # it closed; its container is still in the tree
        self.assertIn("Today", self.labels(driver))
        self.assertEqual(driver.state["probes"], 2)

    def test_without_an_action_the_answer_is_reused(self):
        driver = self.driver()
        self.assertEqual(self.labels(driver), ["Search Food", "Close"])
        driver.state["behind_visible"] = True  # WDA would say otherwise now, but nothing was done
        self.assertEqual(self.labels(driver), ["Search Food", "Close"])
        self.assertEqual(driver.state["probes"], 1)


if __name__ == "__main__":
    unittest.main()

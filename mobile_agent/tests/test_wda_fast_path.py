"""WDA fast path: occlusion without `visible`, and settle-by-agreement. Offline."""

import unittest
from unittest.mock import Mock, patch

from mobile_agent.drivers import WDA, WDA_SETTLE_CAP_SECONDS
from mobile_agent.state import from_wda


def app(*children):
    return ('<XCUIElementTypeApplication width="400" height="800">'
            + "".join(children) + "</XCUIElementTypeApplication>")


def node(role, label, x, y, w, h, *children):
    inner = "".join(children)
    return (f'<XCUIElementType{role} label="{label}" x="{x}" y="{y}" width="{w}" height="{h}">'
            f"{inner}</XCUIElementType{role}>")


NAV = node("NavigationBar", "Display", 0, 0, 400, 100, node("Button", "Settings", 0, 50, 90, 40))
ROWS = node("Table", "", 0, 0, 400, 800,
            node("Cell", "Hidden under bar", 0, 30, 400, 40),
            node("Cell", "Visible row", 0, 200, 400, 40))


class OcclusionTests(unittest.TestCase):
    def labels(self, xml):
        return [e.label for e in from_wda(xml).elements]

    def test_rows_scrolled_under_chrome_are_neither_targets_nor_text(self):
        snapshot = from_wda(app(ROWS, NAV))
        labels = [e.label for e in snapshot.elements]
        self.assertIn("Visible row", labels)
        self.assertNotIn("Hidden under bar", labels)
        self.assertNotIn("Hidden under bar", snapshot.text)

    def test_chrome_keeps_its_own_controls(self):
        self.assertIn("Settings", self.labels(app(ROWS, NAV)))
        self.assertIn("Display", self.labels(app(ROWS, NAV)))

    def test_keyboard_covers_rows_behind_it(self):
        keyboard = node("Keyboard", "", 0, 500, 400, 300, node("Key", "q", 0, 510, 40, 50))
        rows = node("Table", "", 0, 0, 400, 800, node("Cell", "Behind keys", 0, 600, 400, 40))
        labels = self.labels(app(rows, keyboard))
        self.assertNotIn("Behind keys", labels)
        self.assertIn("q", labels)

    def test_frontmost_modal_excludes_everything_outside_it(self):
        alert = node("Alert", "Delete?", 50, 300, 300, 200, node("Button", "Cancel", 60, 440, 130, 40))
        labels = self.labels(app(ROWS, alert))
        self.assertIn("Cancel", labels)
        self.assertNotIn("Visible row", labels)

    def test_on_screen_children_of_a_scrolled_away_container_are_kept(self):
        # Measured in Safari: the web container's frame scrolls off screen
        # while its text stays visible.
        scrolled = node("Other", "", 0, -1189, 400, 852, node("StaticText", "Still visible", 0, 300, 400, 40))
        self.assertEqual(self.labels(app(scrolled)), ["Still visible"])

    def test_identifier_names_are_not_screen_text(self):
        xml = app('<XCUIElementTypeOther name="SafariWindow?View=Narrow&amp;UUID=1" x="0" y="0" width="400" height="800">'
                  '<XCUIElementTypeButton name="GridCellCloseButtonIdentifier" x="0" y="100" width="40" height="40"/>'
                  '<XCUIElementTypeButton name="Done" x="0" y="200" width="40" height="40"/></XCUIElementTypeOther>')
        snapshot = from_wda(xml)
        self.assertNotIn("SafariWindow", snapshot.text)
        self.assertNotIn("GridCellCloseButtonIdentifier", snapshot.text)
        # The identifier never becomes a label; the unlabelled button stays a (label-less) target.
        self.assertEqual([e.label for e in snapshot.elements], ["", "Done"])

    def test_a_list_drawn_over_another_hides_the_rows_behind_it(self):
        # Settings search: the root list stays in the tree with on-screen rects
        # under the results list; its rows must be neither targets nor text.
        root = node("CollectionView", "", 0, 0, 400, 800,
                    node("Cell", "General", 0, 200, 400, 44), node("Cell", "Privacy", 0, 260, 400, 44))
        bar = node("NavigationBar", "Settings", 0, 0, 400, 150,
                   node("SearchField", "Search", 10, 100, 300, 36), node("Button", "Cancel", 320, 100, 70, 36))
        results = node("Table", "", 0, 0, 400, 800, node("Cell", "About", 0, 200, 400, 44))
        keyboard = node("Keyboard", "", 0, 500, 400, 300, node("Key", "a", 0, 510, 40, 50))
        for layout in ((root, bar, results, keyboard), (root, results, bar, keyboard)):
            snapshot = from_wda(app(*layout))
            labels = [e.label for e in snapshot.elements]
            self.assertIn("About", labels)
            self.assertNotIn("General", labels)
            self.assertNotIn("Privacy", snapshot.text)
            # Chrome is never hidden by a pane, in either document order.
            self.assertTrue({"Search", "Cancel", "a"} <= set(labels), labels)

    def test_a_single_list_or_small_overlapping_lists_hide_nothing(self):
        rows = node("CollectionView", "", 0, 0, 400, 800, node("Cell", "General", 0, 200, 400, 44))
        strip = node("CollectionView", "", 0, 180, 400, 100, node("Cell", "Chip", 0, 190, 100, 40))
        self.assertEqual(self.labels(app(rows)), ["General"])
        self.assertEqual(self.labels(app(rows, strip)), ["General", "Chip"])
        # Nested lists (a carousel inside a list) are one surface.
        nested = node("CollectionView", "", 0, 0, 400, 800, node("Cell", "Row", 0, 100, 400, 44),
                      node("CollectionView", "", 0, 300, 400, 400, node("Cell", "Card", 0, 320, 200, 200)))
        self.assertEqual(self.labels(app(nested)), ["Row", "Card"])

    def test_explicit_invisibility_is_still_honored(self):
        xml = app('<XCUIElementTypeButton label="Gone" visible="false" x="0" y="300" width="50" height="50"/>',
                  node("Button", "Here", 0, 400, 50, 50))
        self.assertEqual(self.labels(xml), ["Here"])


class Clock:
    """Simulated time: each source read takes ``per_read`` seconds."""
    def __init__(self, per_read=.3):
        self.now, self.per_read = 1000.0, per_read

    def __call__(self):
        return self.now


CLOCK = Clock()


class Screens:
    """A scripted device: each observe returns the next screen, then repeats the last."""
    def __init__(self, *xmls):
        self.xmls, self.reads = list(xmls), 0

    def __call__(self, method, path, body=None, timeout=10):
        CLOCK.now += CLOCK.per_read
        self.reads += 1
        return self.xmls.pop(0) if len(self.xmls) > 1 else self.xmls[0]


A = app(node("Button", "A", 0, 200, 100, 40))
B = app(node("Button", "B", 0, 200, 100, 40))
C = app(node("Button", "C", 0, 200, 100, 40))


class SettleTests(unittest.TestCase):
    def setUp(self):
        CLOCK.per_read = .3
        for target in ("mobile_agent.drivers.time.monotonic", "mobile_agent.transport.time.monotonic"):
            patcher = patch(target, CLOCK)
            patcher.start()
            self.addCleanup(patcher.stop)

    def driver(self, *xmls):
        driver = WDA("http://localhost:8100", "test")
        self.addCleanup(driver.close)
        driver.call = Screens(*xmls)
        return driver

    def test_change_is_returned_only_once_two_reads_agree(self):
        driver = self.driver(B, C, C)
        with patch("mobile_agent.drivers.time.sleep"):
            after = driver.wait_for_change(from_wda(A), timeout=5, wait_seconds=.6)
        self.assertEqual(after.elements[0].label, "C")
        self.assertEqual(driver.call.reads, 3)

    def test_unchanged_screen_returns_after_the_quiet_window(self):
        driver = self.driver(A)
        after = driver.wait_for_change(from_wda(A), timeout=5, wait_seconds=.05)
        self.assertEqual(after.elements[0].label, "A")

    def test_never_settling_screen_is_bounded_by_the_cap(self):
        driver = WDA("http://localhost:8100", "test")
        self.addCleanup(driver.close)
        frames = iter(range(10 ** 6))
        driver.call = lambda *a, **k: app(node("Button", str(next(frames)), 0, 200, 100, 40))
        clock = [0.0]
        def monotonic():
            clock[0] += .1
            return clock[0]
        with patch("mobile_agent.drivers.time.monotonic", monotonic), \
                patch("mobile_agent.transport.time.monotonic", monotonic), \
                patch("mobile_agent.drivers.time.sleep"):
            driver.wait_for_change(from_wda(A), timeout=30, wait_seconds=.6)
            driver._stable = None
            driver.observe_ready(timeout=30)
        self.assertLess(clock[0], 4 * WDA_SETTLE_CAP_SECONDS + 1)

    def test_observe_ready_reuses_the_read_that_proved_stability(self):
        driver = self.driver(B, B)
        with patch("mobile_agent.drivers.time.sleep"):
            settled = driver.wait_for_change(from_wda(A), timeout=5)
        reads = driver.call.reads
        self.assertIs(driver.observe_ready(timeout=5), settled)
        self.assertEqual(driver.call.reads, reads)

    def test_any_dispatch_invalidates_the_reusable_read(self):
        driver = self.driver(B, B)
        with patch("mobile_agent.drivers.time.sleep"):
            settled = driver.wait_for_change(from_wda(A), timeout=5)
        driver.execute("TAP", settled.elements[0], settled)
        self.assertIsNone(driver._stable)

    def test_a_briefly_stable_half_loaded_screen_is_not_settled(self):
        # Measured on iOS 26 General: 24 elements for ~450 ms, then 29.
        CLOCK.per_read = .1
        driver = self.driver(B, B, C, C, C, C, C, C, C)
        after = driver.wait_for_change(from_wda(A), timeout=5, wait_seconds=.6)
        self.assertEqual(after.elements[0].label, "C")

    def test_a_failed_settle_read_returns_the_last_good_read(self):
        from mobile_agent.transport import TransportError
        driver = self.driver(B)
        reads = iter([B])
        def call(*args, **kwargs):
            CLOCK.now += CLOCK.per_read
            try:
                return next(reads)
            except StopIteration:
                raise TransportError("read timed out") from None
        driver.call = call
        after = driver.wait_for_change(from_wda(A), timeout=5)
        self.assertEqual(after.elements[0].label, "B")

    def test_a_failed_first_settle_read_is_not_hidden(self):
        from mobile_agent.transport import TransportError
        driver = self.driver(B)
        driver.call = Mock(side_effect=TransportError("down"))
        with self.assertRaises(TransportError):
            driver.wait_for_change(from_wda(A), timeout=5)

    def test_observe_ready_waits_for_agreement(self):
        driver = self.driver(A, B, B)
        self.assertEqual(driver.observe_ready(timeout=5).elements[0].label, "B")
        self.assertEqual(driver.call.reads, 3)


if __name__ == "__main__":
    unittest.main()

"""WDA settle latency: back-to-back reads and at-rest memory. Offline."""

import unittest
from unittest.mock import patch

from mobile_agent.drivers import WDA
from mobile_agent.state import from_wda


def app(label, bundle=None):
    stamp = f' bundleId="{bundle}"' if bundle is not None else ""
    return (f'<XCUIElementTypeApplication width="400" height="800"{stamp}>'
            f'<XCUIElementTypeButton label="{label}" x="0" y="200" width="100" height="40"/>'
            "</XCUIElementTypeApplication>")


class Clock:
    """Simulated time: each source read takes ``per_read`` seconds."""
    def __init__(self, per_read=.3):
        self.now, self.per_read = 1000.0, per_read

    def __call__(self):
        return self.now


CLOCK = Clock()


class Screens:
    def __init__(self, *xmls):
        self.xmls, self.reads = list(xmls), 0

    def __call__(self, method, path, body=None, timeout=10):
        if path.startswith("/source"):
            CLOCK.now += CLOCK.per_read
            self.reads += 1
            return self.xmls.pop(0) if len(self.xmls) > 1 else self.xmls[0]
        return None


class SettledMemoryTests(unittest.TestCase):
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

    def test_revisiting_a_proven_screen_needs_one_read(self):
        driver = self.driver(app("B"), app("B"))
        driver.wait_for_change(from_wda(app("A")), timeout=5)  # proves B at rest (2 reads)
        driver.call = Screens(app("B"))
        driver.wait_for_change(from_wda(app("A")), timeout=5)
        self.assertEqual(driver.call.reads, 1)

    def test_unproven_screen_still_needs_two_agreeing_reads(self):
        driver = self.driver(app("B"), app("C"), app("C"))
        after = driver.wait_for_change(from_wda(app("A")), timeout=5)
        self.assertEqual((after.elements[0].label, driver.call.reads), ("C", 3))

    def test_memory_is_not_trusted_right_after_a_swipe(self):
        driver = self.driver(app("B"), app("B"))
        driver.wait_for_change(from_wda(app("A")), timeout=5)
        driver.execute("SWIPE_UP", None, from_wda(app("A")))
        driver.call = Screens(app("B"), app("B"))
        driver.wait_for_change(from_wda(app("A")), timeout=5)
        self.assertEqual(driver.call.reads, 2)

    def test_observe_ready_accepts_a_proven_screen_in_one_read(self):
        driver = self.driver(app("B"), app("B"))
        driver.wait_for_change(from_wda(app("A")), timeout=5)
        driver._stable = None
        driver.call = Screens(app("B"))
        self.assertEqual(driver.observe_ready(timeout=5).elements[0].label, "B")
        self.assertEqual(driver.call.reads, 1)

    def test_settle_polls_without_sleeping(self):
        driver = self.driver(app("B"), app("C"), app("C"))
        with patch("mobile_agent.drivers.time.sleep") as sleep:
            driver.wait_for_change(from_wda(app("A")), timeout=5)
        sleep.assert_not_called()

    def test_memory_is_bounded(self):
        driver = self.driver(app("x"))
        for index in range(200):
            driver._prove_settled(from_wda(app(str(index))))
        self.assertLessEqual(len(driver._settled), 64)


class BundleIdentityTests(unittest.TestCase):
    def test_application_node_bundle_is_bound_to_the_snapshot(self):
        self.assertEqual(from_wda(app("A", "com.apple.Preferences")).bundle_id, "com.apple.Preferences")

    def test_missing_or_malformed_bundle_stays_unknown(self):
        self.assertEqual(from_wda(app("A")).bundle_id, "")
        self.assertEqual(from_wda(app("A", "not a bundle")).bundle_id, "")

    def test_bundle_is_part_of_screen_identity(self):
        self.assertNotEqual(from_wda(app("A", "a.one")).content_fingerprint,
                            from_wda(app("A", "a.two")).content_fingerprint)


if __name__ == "__main__":
    unittest.main()

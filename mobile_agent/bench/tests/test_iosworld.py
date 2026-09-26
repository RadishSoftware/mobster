"""The iOSWorld adapter writes trajectories in iOSWorld's own format. Offline."""

import time
import unittest
from types import SimpleNamespace

from mobile_agent.bench.iosworld import RecordingDriver, answer_text, describe


class FakeShots:
    def __init__(self):
        self.taken = []

    def take(self, name):
        self.taken.append(name)
        return f"/shots/{name}.png"


class FakeDriver:
    def __init__(self):
        self.calls = []

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.calls.append(("execute", operation))

    def call(self, method, path, body=None, timeout=10):
        self.calls.append(("call", path))


class IOSWorldAdapterTests(unittest.TestCase):
    def test_each_dispatch_is_a_step_with_its_pre_action_screenshot(self):
        driver = RecordingDriver(FakeDriver(), FakeShots())
        button = SimpleNamespace(label="Log Food", role="Button")
        driver.call("POST", "/wda/apps/activate", {"bundleId": "com.example.caltrack"})
        driver.execute("TAP", button, None)
        driver.execute("TYPE_SUBMIT", SimpleNamespace(label="Search", role="SearchField"), None, text="oatmeal")
        driver.call("GET", "/source")
        self.assertEqual([s["actions"][0]["type"] for s in driver.steps], ["launch_app", "tap", "type"])
        self.assertEqual(driver.steps[1]["actions"][0]["element"], {"label": "Log Food", "role": "Button"})
        self.assertEqual(driver.steps[2]["actions"][0]["text"], "oatmeal")
        self.assertEqual([s["screenshot"] for s in driver.steps],
                         ["/shots/step-001.png", "/shots/step-002.png", "/shots/step-003.png"])
        self.assertEqual(driver._driver.calls[-1], ("call", "/source"))

    def test_the_judge_screenshot_does_not_age_the_observation(self):
        from mobile_agent.state import Element, Snapshot
        button = Element("1", "Log Food", "Button", (.1, .1, .2, .05))
        snapshot = Snapshot([button], "Log Food", 400, 800, "wda", captured_at=100.0)
        shots, seen = FakeShots(), []
        shots.take = lambda name: time.sleep(.05) or "/shots/x.png"
        inner = FakeDriver()
        inner.execute = lambda operation, target, snap, text=None, timeout=10: seen.append(snap)
        RecordingDriver(inner, shots).execute("TAP", button, snapshot)
        self.assertGreaterEqual(seen[0].captured_at, 100.05)
        self.assertEqual(seen[0].elements, snapshot.elements)
        self.assertEqual(snapshot.captured_at, 100.0)

    def test_swipes_keys_and_answers(self):
        self.assertEqual(describe("SWIPE_UP"), {"type": "swipe", "direction": "up"})
        self.assertEqual(describe("HOME"), {"type": "home"})
        self.assertEqual(answer_text({"data": "312 kcal"}), "312 kcal")
        self.assertEqual(answer_text({"data": {"calories": "312"}}), '{"calories": "312"}')
        self.assertIsNone(answer_text({"data": None}))


if __name__ == "__main__":
    unittest.main()

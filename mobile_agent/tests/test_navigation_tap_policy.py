"""Which taps may skip separate action verification. Offline."""

import unittest

from mobile_agent.task_policy import action_risk_tier, is_plain_navigation_tap


def skip(role="Cell", label="General", *, goal="Open General, then About", confidence=.99, risk=.02,
         operation="TAP"):
    tier = action_risk_tier(operation, label, goal).value
    return is_plain_navigation_tap(operation, role, label, risk_tier=tier, confidence=confidence,
                                   side_effect_risk=risk)


class NavigationTapTests(unittest.TestCase):
    def test_confident_low_risk_row_tap_skips(self):
        self.assertTrue(skip())
        self.assertTrue(skip("Button", "About"))

    def test_every_condition_is_required(self):
        self.assertFalse(skip(confidence=.8))
        self.assertFalse(skip(risk=.3))
        self.assertFalse(skip(risk=None))
        self.assertFalse(skip(operation="TYPE"))

    def test_stateful_roles_always_verify(self):
        for role in ("Switch", "Slider", "PickerWheel", "SegmentedControl", "TextField", "Stepper"):
            self.assertFalse(skip(role, "Airplane Mode"), role)

    def test_state_changing_labels_always_verify(self):
        for label in ("Erase All Content and Settings", "Turn Off", "Sign Out", "Reset", "Clear History",
                      "Delete App", "Software Update", "Forget This Device", "Emergency SOS", "Done"):
            self.assertFalse(skip("Button", label), label)

    def test_side_effect_goals_always_verify(self):
        self.assertFalse(skip(goal="Delete the note named Groceries"))


if __name__ == "__main__":
    unittest.main()


class LazyPageTests(unittest.TestCase):
    def test_plain_navigation_tap_survives_unrelated_page_changes(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from mobile_agent.agent import Agent
        from mobile_agent.models import Decision
        from mobile_agent.state import Element, Snapshot
        from mobile_agent.task_policy import StopGate
        link = Element("0", "Gustave Eiffel", "Link", (0, .5, .5, .05), locator="/a[1]")
        before = Snapshot([link], "Gustave Eiffel", 400, 800, "wda")
        loaded = Snapshot([Element("0", "Photo", "Image", (0, .1, 1, .2), locator="/img[1]"),
                           Element("1", "Gustave Eiffel", "Link", (0, .5, .5, .05), locator="/a[1]")],
                          "Photo\nGustave Eiffel", 400, 800, "wda")
        after = Snapshot([Element("0", "Born 1832", "StaticText", (0, .3, 1, .05), locator="/p[1]")],
                         "Born 1832", 400, 800, "wda")
        reads = iter([before, loaded, after, after, after, after])
        driver = SimpleNamespace(can_type=False, execute=Mock(), observe=lambda timeout=10: next(reads))
        model = Mock(spec=["decide"])
        model.decide.side_effect = [
            Decision("TAP", "0", .97, .05, .05, "t", 0, {}, StopGate.CONTINUE,
                     risk_tier="navigation", side_effect_risk=.01),
            Decision("DONE", None, .97, .9, .05, "t", 0, {}, StopGate.CONTINUE),
            Decision("DONE", None, .97, .9, .05, "t", 0, {}, StopGate.CONTINUE)]
        result = Agent(driver, model, settle_seconds=0).run("Open the engineer's article", execute=True)
        self.assertEqual(driver.execute.call_count, 1)
        tapped = driver.execute.call_args.args[1]
        self.assertEqual((tapped.id, tapped.label), ("1", "Gustave Eiffel"))
        self.assertEqual(result["status"], "completed_unverified")

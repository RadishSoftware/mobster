"""Compiled-run replay. Offline."""

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from mobile_agent.agent import Agent
from mobile_agent.models import Decision
from mobile_agent.replay import ReplayStore, record, request_key, resolve
from mobile_agent.state import Element, Snapshot
from mobile_agent.task_policy import ActionSupport, StopGate


def screen(*labels):
    return Snapshot([Element(str(i), label, "Cell", (0, .05 + i * .08, 1, .05), locator=f"/cell[{i}]")
                     for i, label in enumerate(labels)], "\n".join(labels), 400, 800, "wda")


ROOT, GENERAL = screen("General", "Privacy"), screen("About", "Keyboard")


def decision(operation, target=None):
    return Decision(operation, target, .99, .99, 0, "offline", 0, {}, StopGate.CONTINUE,
                    risk_tier="navigation", side_effect_risk=.01)


class Phone:
    """Root --tap General--> General."""
    can_type = False

    def __init__(self):
        self.current, self.actions = ROOT, []

    def observe(self, timeout=10):
        return self.current

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, target.label if target else None))
        if operation == "TAP" and target.label == "General":
            self.current = GENERAL

    def wait_for_change(self, snapshot, timeout=2, wait_seconds=.6):
        return self.current


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.store = ReplayStore(tempfile.mkdtemp())

    def model(self):
        model = Mock(spec=["decide", "verify_action"])
        model.decide.side_effect = lambda snapshot, *a, **k: (
            decision("TAP", "0") if snapshot is ROOT else decision("DONE"))
        model.verify_action.return_value = ActionSupport.ALLOWED
        return model

    def run_once(self, model):
        phone = Phone()
        result = Agent(phone, model, replay_store=self.store, settle_seconds=.01).run(
            "Open General", execute=True)
        return result, phone

    def test_a_completed_run_is_replayed_without_deciding_its_actions(self):
        first, _ = self.run_once(self.model())
        self.assertEqual(first["status"], "completed_unverified")
        model = self.model()
        second, phone = self.run_once(model)
        self.assertEqual(second["status"], "completed_unverified")
        self.assertTrue(second["replayed"])
        self.assertEqual(phone.actions, [("TAP", "General")])
        # Only the live DONE decision and its completion check were asked.
        self.assertTrue(all(call.args[0] is GENERAL for call in model.decide.call_args_list))

    def test_a_changed_screen_abandons_the_recording(self):
        self.run_once(self.model())
        key = request_key("Open General", None, None, None)
        steps = self.store.load(key)
        steps[0]["before"] = "0" * 64
        steps[0]["signature"] = [["Cell", "Something else entirely"]]
        self.store.save(key, steps)
        model = self.model()
        result, phone = self.run_once(model)
        self.assertFalse(result["replayed"])
        self.assertIs(model.decide.call_args_list[0].args[0], ROOT)

    def test_resolution_requires_an_identical_screen_and_a_unique_target(self):
        entry = record(ROOT.content_fingerprint, decision("TAP", "0"), ROOT.elements[0])
        self.assertEqual(resolve(entry, ROOT), "0")
        self.assertIs(resolve(entry, GENERAL), False)
        self.assertIsNone(record(ROOT.content_fingerprint, decision("TYPE", "0"), ROOT.elements[0]))

    def test_corrupt_recordings_are_ignored(self):
        key = request_key("x", None, None, None)
        self.store._path(key).parent.mkdir(parents=True, exist_ok=True)
        self.store._path(key).write_text('[{"operation": "TYPE"}]')
        self.assertIsNone(self.store.load(key))


if __name__ == "__main__":
    unittest.main()


class AutoReplayTests(unittest.TestCase):
    def test_auto_requests_replay_with_their_recorded_intent(self):
        from mobile_agent.task_policy import OutputIntent
        store = ReplayStore(tempfile.mkdtemp())
        def model():
            m = Mock(spec=["decide", "verify_action"])
            m.decide.side_effect = lambda snapshot, *a, **k: (
                Decision("TAP", "0", .99, .99, 0, "offline", 0, {}, StopGate.CONTINUE,
                         OutputIntent.ACTION_ONLY if k.get("classify_output") else None,
                         risk_tier="navigation", side_effect_risk=.01)
                if snapshot is ROOT else
                Decision("DONE", None, .99, .99, 0, "offline", 0, {}, StopGate.CONTINUE,
                         OutputIntent.ACTION_ONLY if k.get("classify_output") else None))
            m.verify_action.return_value = ActionSupport.ALLOWED
            return m
        for attempt in range(2):
            phone, m = Phone(), model()
            result = Agent(phone, m, replay_store=store, settle_seconds=.01).run(
                "Open General", execute=True, output_format="auto")
            self.assertEqual(result["status"], "completed_unverified")
        self.assertTrue(result["replayed"])
        self.assertTrue(all(call.args[0] is GENERAL for call in m.decide.call_args_list))


class StructuralReplayTests(unittest.TestCase):
    def test_a_slightly_changed_screen_still_replays(self):
        entry = record(ROOT.content_fingerprint, decision("TAP", "0"), ROOT.elements[0], ROOT)
        grown = screen("General", "Privacy", "Keyboard", "About", "Search", "Fonts", "Language", "Region",
                       "Date", "VPN")
        base = screen("General", "Privacy", "Keyboard", "About", "Search", "Fonts", "Language", "Region", "Date")
        entry = record(base.content_fingerprint, decision("TAP", "0"), base.elements[0], base)
        self.assertEqual(resolve(entry, grown), "0")

    def test_a_different_screen_or_app_does_not_replay(self):
        entry = record(ROOT.content_fingerprint, decision("TAP", "0"), ROOT.elements[0], ROOT)
        self.assertIs(resolve(entry, GENERAL), False)
        moved = screen("General", "Privacy")
        moved.bundle_id = "other.app"
        self.assertIs(resolve(entry, moved), False)

    def test_an_ambiguous_target_does_not_replay(self):
        base = screen("General", "Privacy", "Keyboard", "About", "Search", "Fonts", "Language", "Region", "Date")
        entry = record(base.content_fingerprint, decision("TAP", "0"), base.elements[0], base)
        entry["target"]["locator"] = "/moved"  # its recorded position is gone
        doubled = screen("General", "General", "Privacy", "Keyboard", "About", "Search", "Fonts", "Language",
                         "Region", "Date")
        self.assertIs(resolve(entry, doubled), False)

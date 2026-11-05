"""Replay admission guards for approvals and coordinate actions."""

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from mobile_agent.replay_core.engine import Replayer
from mobile_agent.replay_core.path import PathError, new_path, signature_hash, validate
from mobile_agent.replay_core.recorder import Recorder
from mobile_agent.state import signature
from mobile_agent.tests import replay_world as W


def path(steps):
    return new_path(steps, kind="check", identity={"x": 1}, env={"app_version": "1"})


class ReplaySafetyTests(unittest.TestCase):
    def test_approval_uses_the_current_screen_and_current_app_permission(self):
        step = {"op": "TAP", "app": W.BUNDLE, "target": {"label": "Continue", "role": "Button"},
                "commit": "change"}
        for change, status in (("none", "done"), ("moved", "drift"), ("revoked", "handoff")):
            with self.subTest(change=change):
                state = W.new_state()
                phone = W.LumenPhone(state)
                rows = phone.screen_rows
                moved, allowed = [False], [True]
                def screen_rows():
                    return [replace(element, rect=(.2, .85, .7, .06)) if moved[0] and
                            element.label == "Continue" else element for element in rows()]
                def approve(_):
                    moved[0] = change == "moved"
                    allowed[0] = change != "revoked"
                    return "approved"
                with mock.patch.object(phone, "screen_rows", screen_rows), \
                        mock.patch.object(phone, "execute", wraps=phone.execute) as execute:
                    result = Replayer(phone, approve=approve, allows_app=lambda bundle: allowed[0],
                                      wait_seconds=0).run(path([step]))
                self.assertEqual(result.status, status, result.reason)
                self.assertEqual(execute.call_count, 1 if change == "none" else 0)
                if change == "none":
                    self.assertEqual(result.steps[0]["commit"], "change")
                    self.assertEqual(state["screen"], "signup")
                else:
                    self.assertEqual(state["screen"], "welcome")

    def test_approval_compares_the_pre_callback_revision(self):
        phone = W.LumenPhone(W.new_state())
        snapshot = phone.observe()
        def approve(_):
            snapshot.revision = "replaced while approval waited"
            return "approved"
        with mock.patch.object(phone, "observe", return_value=snapshot), \
                mock.patch.object(phone, "execute", wraps=phone.execute) as execute:
            result = Replayer(phone, approve=approve, wait_seconds=0).run(path([
                {"op": "TAP", "app": W.BUNDLE, "target": {"label": "Continue", "role": "Button"},
                 "commit": "change"}]))
        self.assertEqual(result.status, "drift", result.reason)
        execute.assert_not_called()

    def test_an_app_bound_step_refuses_an_unknown_foreground_app(self):
        phone = W.LumenPhone(W.new_state())
        original = phone.observe
        asked = []
        def observe(timeout=10):
            snapshot = original(timeout=timeout)
            snapshot.bundle_id = ""
            return snapshot
        with mock.patch.object(phone, "observe", side_effect=observe), \
                mock.patch.object(phone, "execute", wraps=phone.execute) as execute:
            result = Replayer(phone, approve=lambda request: asked.append(request) or "approved",
                              wait_seconds=0).run(path([
                {"op": "TAP", "app": W.BUNDLE, "target": {"label": "Continue", "role": "Button"},
                 "commit": "change"}]))
        self.assertEqual(result.status, "drift", result.reason)
        self.assertEqual(asked, [])
        execute.assert_not_called()

    def test_seek_refuses_unknown_foreground_before_swiping(self):
        phone = W.LumenPhone(W.new_state())
        original = phone.observe
        def observe(timeout=10):
            snapshot = original(timeout=timeout)
            snapshot.bundle_id = ""
            return snapshot
        with mock.patch.object(phone, "observe", side_effect=observe), \
                mock.patch.object(phone, "execute", wraps=phone.execute) as execute:
            result = Replayer(phone, wait_seconds=0).run(path([
                {"op": "TAP", "app": W.BUNDLE, "target": {"label": "Missing", "role": "Button"},
                 "seek": True}]))
        self.assertEqual(result.status, "drift", result.reason)
        execute.assert_not_called()

    def test_a_current_gate_commit_becomes_part_of_the_executed_step(self):
        step = {"op": "TAP", "app": W.BUNDLE, "target": {"label": "Continue", "role": "Button"},
                "commit": None}
        decision = SimpleNamespace(asks=True, act="send_message", kind="commit")
        result = Replayer(W.LumenPhone(W.new_state()), approve=lambda request: "approved",
                          decide=lambda *args: decision, wait_seconds=0).run(path([step]))
        self.assertEqual(result.status, "done", result.reason)
        self.assertEqual(result.steps[0]["commit"], "send_message")

    def test_post_approval_read_failure_never_retries_an_earlier_action(self):
        phone = W.LumenPhone(W.new_state())
        original = phone.observe
        reads = [0]
        def observe(timeout=10):
            reads[0] += 1
            if reads[0] == 3:
                raise TimeoutError("lost before approved dispatch")
            return original(timeout=timeout)
        with mock.patch.object(phone, "observe", side_effect=observe), \
                mock.patch.object(phone, "execute", wraps=phone.execute) as execute:
            result = Replayer(phone, approve=lambda request: "approved", wait_seconds=0).run(path([
                {"op": "HOME"}, {"op": "TAP", "app": W.BUNDLE,
                                  "target": {"label": "Continue", "role": "Button"}, "commit": "change"}]))
        self.assertEqual((result.status, result.model_status), ("error", "unknown_outcome"))
        self.assertEqual([call.args[0] for call in execute.call_args_list], ["HOME"])

    def test_coordinate_replay_requires_exact_geometry_and_legacy_proof(self):
        reference = W.LumenPhone(W.new_state()).observe()
        screen = {"hash": signature_hash(signature(reference)), "signature": signature(reference),
                  "fingerprint": reference.content_fingerprint}
        for op in ("TAP_XY", "DISMISS"):
            for change, status in (("same", "done"), ("moved", "drift"), ("rotated", "drift"),
                                   ("legacy", "drift")):
                with self.subTest(op=op, change=change):
                    phone = W.LumenPhone(W.new_state())
                    original = phone.observe
                    def observe(timeout=10):
                        snapshot = original(timeout=timeout)
                        if change == "moved":
                            snapshot.elements[1] = replace(snapshot.elements[1], rect=(.2, .85, .7, .06))
                        elif change == "rotated":
                            snapshot.width, snapshot.height = snapshot.height, snapshot.width
                        return snapshot
                    step = {"op": op, "app": W.BUNDLE, "point": [.5, .88],
                            "screen": {key: value for key, value in screen.items() if change != "legacy" or
                                       key != "fingerprint"}}
                    with mock.patch.object(phone, "observe", side_effect=observe), \
                            mock.patch.object(phone, "tap_point", wraps=phone.tap_point) as tap:
                        result = Replayer(phone, wait_seconds=0).run(path([step]))
                    self.assertEqual(result.status, status, result.reason)
                    self.assertEqual(tap.call_count, 1 if change == "same" else 0)

    def test_point_recording_retains_pre_action_proof_after_mutating_dispatch(self):
        phone = W.LumenPhone(W.new_state())
        snapshot = phone.observe()
        before = snapshot.content_fingerprint
        recorder = Recorder()
        recorder.saw(snapshot)
        def mutate(x, y, screen, **kwargs):
            screen.width += 1
        with mock.patch.object(phone, "tap_point", side_effect=mutate):
            recorder.wrap(phone).tap_point(.5, .88, snapshot)
        step = recorder.step_from(SimpleNamespace(op="TAP_XY", changed=True,
                                                  screen=signature_hash(signature(snapshot)), app=W.BUNDLE,
                                                  text=None, commit=None, target=None))
        self.assertEqual(step["screen"]["fingerprint"], before)
        self.assertNotEqual(snapshot.content_fingerprint, before)
        replay_phone = W.LumenPhone(W.new_state())
        with mock.patch.object(replay_phone, "tap_point", side_effect=mutate) as tap:
            result = Replayer(replay_phone).run(path([step]))
        self.assertEqual(result.status, "done", result.reason)
        tap.assert_called_once()
        self.assertEqual(result.steps[0]["screen"]["fingerprint"], before)

    def test_malformed_coordinate_proof_is_not_a_trusted_path(self):
        step = {"op": "TAP_XY", "point": [.5, .5],
                "screen": {"hash": "", "fingerprint": "not a sha256"}}
        with self.assertRaises(PathError):
            validate(path([step]))

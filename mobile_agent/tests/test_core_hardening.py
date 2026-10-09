"""Offline fault-injection regressions; no phone or model endpoint is contacted."""

from dataclasses import replace
import json
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, Mock, patch

from mobile_agent.agent import Agent
from mobile_agent.demo import DemoDriver, DemoHelper, DemoModel, screen
from mobile_agent.drivers import DriverRejection, WDA, WDA_SOURCE_PATH, _swipe_points
from mobile_agent.extraction import Evidence, validate_extraction, validate_schema
from mobile_agent.models import Decision, Helper, Jev, validate_choice
from mobile_agent.task_policy import ActionSupport, StopGate
from mobile_agent.state import from_ocr, from_wda, validate_input_text
from mobile_agent.transport import Deadline, HTTP, TransportError, decode_json
from mobile_agent.tests.timing import bound


XML = '<XCUIElementTypeApplication width="400" height="800"><XCUIElementTypeButton label="Search" x="1" y="1" width="100" height="40"/></XCUIElementTypeApplication>'


class Clock:
    now = 0
    def __call__(self):
        return self.now


class StateHardeningTests(unittest.TestCase):
    def test_revision_only_changes_are_stale_but_not_observable_progress(self):
        original = replace(screen(), revision="session:1")
        revised = replace(original, revision="session:2")
        self.assertNotEqual(original.fingerprint, revised.fingerprint)
        self.assertEqual(original.content_fingerprint, revised.content_fingerprint)

    def test_content_fingerprint_preserves_evidence_geometry_and_routing(self):
        original = screen()
        for candidate in [replace(original, width=800), replace(original, bundle_id="other.app"),
                          replace(original, text=original.text + "\nPlease sign in"),
                          replace(original, elements=[replace(e, value="changed") for e in original.elements]),
                          replace(original, elements=[replace(e, actions=()) for e in original.elements]),
                          replace(original, elements=[replace(e, locator="/other") for e in original.elements])]:
            with self.subTest(candidate=candidate):
                self.assertNotEqual(original.content_fingerprint, candidate.content_fingerprint)

    def test_fingerprint_includes_dimensions_revision_and_routing(self):
        original = screen()
        candidates = [replace(original, width=800), replace(original, height=400),
            replace(original, revision="another-process:1"),
            replace(original, elements=[replace(e, locator="/different") for e in original.elements]),
            replace(original, text="a" * 12000 + "secretly changed")]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertNotEqual(original.fingerprint, candidate.fingerprint)
        self.assertNotEqual(replace(original, text="a" * 12000 + "1").fingerprint,
                            replace(original, text="a" * 12000 + "2").fingerprint)


    def test_ocr_cannot_silently_truncate_observation(self):
        with self.assertRaises(ValueError):
            from_ocr([{"text": "label", "rect": [0, 0, .1, .1]}] * 241, 400, 800)
        for rows in [None, {}, [None], [{"text": 123}]]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                from_ocr(rows, 400, 800)

    def test_wda_node_budget_includes_unlabeled_structure(self):
        from mobile_agent.state import WDA_NODE_BUDGET
        xml = ('<XCUIElementTypeApplication width="400" height="800">'
               + '<XCUIElementTypeOther/>' * (WDA_NODE_BUDGET + 1) + '</XCUIElementTypeApplication>')
        with self.assertRaises(ValueError):
            from_wda(xml)

    def test_a_view_wider_than_a_double_has_no_size_and_the_screen_still_reads(self):
        # Reminders on iOS 26.4 (27 Sep) lists a spacer 1.797693134862316e+308 wide: past DBL_MAX, so inf.
        for rich in (False, True):
            xml = ('<XCUIElementTypeApplication width="400" height="800"><XCUIElementTypeOther>'
                   '<XCUIElementTypeOther x="0" y="0" width="1.797693134862316e+308" height="1"/>'
                   '<XCUIElementTypeOther x="0" y="0" width="1.797693134862316e+308" height="1"/></XCUIElementTypeOther>'
                   '<XCUIElementTypeButton label="New Reminder" x="10" y="700" width="140" height="44"/>'
                   '<XCUIElementTypeStaticText label="nan" x="nan" y="20" width="40" height="20"/>'
                   '</XCUIElementTypeApplication>')
            with self.subTest(rich=rich):
                state = from_wda(xml, rich=rich)
                self.assertIn("New Reminder", [e.label for e in state.elements])
                self.assertTrue(all(all(abs(v) < 1e6 for v in e.rect) for e in state.elements))

    def test_text_blocks_control_and_unicode_submission_characters(self):
        for text in [None, "", "\t", "hello\n", "hello\r", "x\x7f", "x\x85", "x\u2028", "x\ud800"]:
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                validate_input_text(text)
        self.assertEqual(validate_input_text("café ☕"), "café ☕")


class ModelHardeningTests(unittest.TestCase):
    def test_choice_rejects_container_confusion(self):
        for answer in [None, [], "TAP", {"probabilities": [], "choice": "TAP"}]:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                validate_choice(answer, {"TAP"})

    def test_decision_rejects_unsupported_actions_and_invalid_metrics(self):
        decision = DemoModel().decide(screen(), "Search", [])
        for changes in [{"operation": "DELETE"}, {"confidence": True}, {"target": 7},
                        {"operation": "DONE", "target": "0"}, {"model": None},
                        {"latency_ms": float("nan")}, {"usage": []}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(decision, **changes)


    def test_provider_malformed_noul_cannot_authorize(self):
        model = Jev("test-only")
        def reply(method, path, body, timeout):
            answers = {"operation": {"type": "choice", "choice": "WAIT", "confidence": 1,
                "probabilities": {op: int(op == "WAIT") for op in body["questions"]["operation"]["criteria"]}},
                "goal": [], "blocked": {"type": "noul", "noul": .1},
                "stop_gate": {"type": "choice", "choice": "continue", "confidence": 1,
                    "probabilities": {"continue": 1, "stop": 0, "unclear": 0}}}
            return {"model": "test", "answers": answers}
        model.http.request = reply
        with self.assertRaises(ValueError):
            model.decide(screen(), "Search", [])

    def test_helper_duplicate_milestones_and_control_text_fail_closed(self):
        with patch.dict("os.environ", {"TEXT_MODEL_API_KEY": "test", "TEXT_MODEL": "test"}):
            helper = Helper()
        for mode, content in [("plan", {"subgoals": ["Open", "Open"]}), ("text", {"text": "hello\x7f"})]:
            helper.http.request = Mock(return_value={"choices": [{"message": {"content": json.dumps(content)}}]})
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                helper.ask(mode, screen(), "Test", [])
        self.assertEqual(helper.calls, 2)

    def test_cerebras_uses_completion_budget_and_optional_reasoning_effort(self):
        with patch.dict("os.environ", {"TEXT_MODEL_API_KEY": "test", "TEXT_MODEL": "qwen-3.8-27b",
                "TEXT_MODEL_BASE_URL": "https://api.cerebras.ai/v1", "TEXT_MODEL_REASONING_EFFORT": "none"}):
            helper = Helper()
        body = helper.completion_body([], 300)
        self.assertEqual(body["max_completion_tokens"], 300)
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertNotIn("max_tokens", body)

    def test_other_compatible_helpers_keep_original_token_parameter(self):
        with patch.dict("os.environ", {"TEXT_MODEL_API_KEY": "test", "TEXT_MODEL": "small",
                "TEXT_MODEL_BASE_URL": "https://openrouter.ai/api/v1", "TEXT_MODEL_REASONING_EFFORT": ""}):
            helper = Helper()
        body = helper.completion_body([], 300)
        self.assertEqual(body["max_tokens"], 300)
        self.assertNotIn("reasoning_effort", body)

    def test_unsupported_reasoning_effort_rejected_before_network(self):
        with patch.dict("os.environ", {"TEXT_MODEL_API_KEY": "test", "TEXT_MODEL": "small", "TEXT_MODEL_REASONING_EFFORT": "fast"}):
            with self.assertRaises(ValueError):
                Helper()


class AdapterHardeningTests(unittest.TestCase):


    def test_wda_tap_is_one_coordinate_action_without_resolution(self):
        state = from_wda(XML)
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        driver.execute("TAP", state.elements[0], state)
        driver.call.assert_called_once()
        method, path, body = driver.call.call_args[0][:3]
        self.assertEqual((method, path), ("POST", "/actions"))
        move = body["actions"][0]["actions"][0]
        self.assertEqual((move["x"], move["y"]), (50, 20))

    def test_wda_regrounds_an_aging_snapshot_before_tapping(self):
        state = from_wda(XML)
        state.captured_at -= 5
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock(return_value=XML.replace('label="Search"', 'label="Purchase"'))
        with self.assertRaises(DriverRejection):
            driver.execute("TAP", state.elements[0], state)
        self.assertEqual([c[0][1] for c in driver.call.call_args_list], [WDA_SOURCE_PATH])

    def test_wda_aging_but_unchanged_snapshot_still_taps(self):
        state = from_wda(XML)
        state.captured_at -= 5
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock(side_effect=[XML, None])
        driver.execute("TAP", state.elements[0], state)
        self.assertEqual([c[0][1] for c in driver.call.call_args_list], [WDA_SOURCE_PATH, "/actions"])

    def test_wda_type_into_a_focused_field_is_one_keystroke_call(self):
        xml = XML.replace("</XCUIElementTypeApplication>",
            '<XCUIElementTypeKeyboard x="0" y="500" width="400" height="300"/></XCUIElementTypeApplication>')
        xml = xml.replace("XCUIElementTypeButton", "XCUIElementTypeTextField")
        state = from_wda(xml)
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        driver.execute("TYPE", state.elements[0], state, "hi")
        driver.call.assert_called_once_with("POST", "/wda/keys", {"value": ["hi"]}, ANY)

    def test_wda_type_focuses_first_and_types_only_once_a_keyboard_is_up(self):
        field = XML.replace("XCUIElementTypeButton", "XCUIElementTypeTextField")
        focused = field.replace("</XCUIElementTypeApplication>",
            '<XCUIElementTypeKeyboard x="0" y="500" width="400" height="300"/></XCUIElementTypeApplication>')
        state = from_wda(field)
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock(side_effect=lambda method, path, *a, **k: focused if path.startswith("/source") else None)
        driver.execute("TYPE", state.elements[0], state, "hi")
        # An element lookup may come first (it finds nothing here); then the focusing tap.
        paths = [c[0][1] for c in driver.call.call_args_list if c[0][1] != "/elements"]
        self.assertEqual(paths[0], "/actions")
        self.assertEqual(driver.call.call_args_list[-1][0][1:3], ("/wda/keys", {"value": ["hi"]}))

    def test_wda_type_sends_nothing_when_focus_never_arrives(self):
        field = XML.replace("XCUIElementTypeButton", "XCUIElementTypeTextField")
        state = from_wda(field)
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock(side_effect=lambda method, path, *a, **k: field if path.startswith("/source") else None)
        with patch("mobile_agent.drivers.WDA_KEYBOARD_WAIT_SECONDS", .05), self.assertRaises(TransportError):
            driver.execute("TYPE", state.elements[0], state, "hi")
        self.assertNotIn("/wda/keys", [c[0][1] for c in driver.call.call_args_list])

    def test_wda_stale_grounding_is_a_pre_dispatch_refusal(self):
        state = from_wda(XML)
        state.captured_at -= 5
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock(return_value=XML.replace('label="Search"', 'label="Purchase"'))
        with self.assertRaises(DriverRejection) as refused:
            driver.execute("TAP", state.elements[0], state)
        self.assertIn(refused.exception.code, DriverRejection.PRE_DISPATCH)

    def test_wda_invalid_text_never_even_resolves_a_target(self):
        xml = XML.replace("XCUIElementTypeButton", "XCUIElementTypeTextField")
        state = from_wda(xml)
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        with self.assertRaises(ValueError):
            driver.execute("TYPE", state.elements[0], state, "hi\r")
        driver.call.assert_not_called()

    def test_swipe_geometry_covers_all_four_directions(self):
        width, height = 400, 800
        self.assertEqual(_swipe_points("SWIPE_UP", width, height), (200, 600, 200, 200))
        self.assertEqual(_swipe_points("SWIPE_DOWN", width, height), (200, 200, 200, 600))
        self.assertEqual(_swipe_points("SWIPE_LEFT", width, height), (300, 400, 100, 400))
        self.assertEqual(_swipe_points("SWIPE_RIGHT", width, height), (100, 400, 300, 400))
        with self.assertRaises(ValueError):
            _swipe_points("SWIPE_DIAGONAL", width, height)

    def test_wda_supports_all_four_swipe_directions(self):
        state = from_wda(XML)
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        for operation, (x1, y1, x2, y2) in {
                "SWIPE_UP": (200, 600, 200, 200), "SWIPE_DOWN": (200, 200, 200, 600),
                "SWIPE_LEFT": (300, 400, 100, 400), "SWIPE_RIGHT": (100, 400, 300, 400)}.items():
            with self.subTest(operation=operation):
                driver.execute(operation, None, state)
                method, path, body = driver.call.call_args[0][:3]
                if operation in ("SWIPE_LEFT", "SWIPE_RIGHT"):
                    # A flick: same endpoints, a fast pointer move (paging views turn on speed).
                    self.assertEqual((method, path), ("POST", "/actions"))
                    moves = [a for a in body["actions"][0]["actions"] if a["type"] == "pointerMove"]
                    self.assertEqual((moves[0]["x"], moves[0]["y"], moves[-1]["x"], moves[-1]["y"]), (x1, y1, x2, y2))
                    continue
                self.assertEqual((method, path), ("POST", "/wda/dragfromtoforduration"))
                self.assertEqual((body["fromX"], body["fromY"], body["toX"], body["toY"]),
                                 (x1, y1, x2, y2))


    def test_wda_configure_applies_measured_snapshot_depth(self):
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock(side_effect=lambda method, path, *a, **k:
                           {"width": 393, "height": 852} if path == "/window/size" else None)
        # Never reach a real WDA on localhost:8100 from a unit test.
        from mobile_agent.transport import TransportError
        driver.http.request = Mock(side_effect=TransportError("no WDA in unit tests"))
        driver.configure()
        driver.call.assert_called_with("POST", "/appium/settings", {"settings": {
            "snapshotMaxDepth": 60, "waitForIdleTimeout": 0, "animationCoolOffTimeout": 0,
            "snapshotTimeout": 6, "customSnapshotTimeout": 6, "activeAppDetectionPoint": "196,426"}}, ANY)

    def test_wda_advertises_host_keys_at_construction(self):
        driver = WDA("http://localhost:8100", "test")
        self.assertEqual(driver.host_operations, frozenset({"HOME", "VOLUME_UP", "VOLUME_DOWN"}))

    def test_wda_home_uses_pressButton(self):
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        result = driver.execute("HOME", None, from_wda(XML))
        driver.call.assert_called_once_with("POST", "/wda/pressButton", {"name": "home"}, ANY)
        self.assertEqual(result, {"dispatch_attempted": True})

    def test_wda_volume_keys_use_pressButton_names(self):
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        for operation, name in (("VOLUME_UP", "volumeUp"), ("VOLUME_DOWN", "volumeDown")):
            with self.subTest(operation=operation):
                driver.execute(operation, None, from_wda(XML))
                self.assertEqual(driver.call.call_args[0][:3],
                                 ("POST", "/wda/pressButton", {"name": name}))


class LoopHardeningTests(unittest.TestCase):
    def test_invalid_budget_types_are_rejected(self):
        for values in [{"max_steps": True}, {"max_seconds": "1"}, {"max_seconds": True},
                       {"settle_seconds": float("nan")}, {"max_helper_calls": .5}, {"max_helper_calls": True}]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                Agent(DemoDriver(), DemoModel(), **values)

    def test_invalid_inputs_are_rejected_before_observation(self):
        driver = DemoDriver()
        driver.observe = Mock()
        for goal, options in [(None, {}), ("Search", {"subgoals": "Search"}),
                              ("Search", {"subgoals": []}), ("Search", {"expected_text": ""})]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                Agent(driver, DemoModel()).run(goal, **options)
        driver.observe.assert_not_called()

    def test_original_goal_is_always_checked_after_explicit_milestones(self):
        model = DemoModel()
        model.decide = Mock(return_value=model.decide(screen("results"), "", []))
        result = Agent(DemoDriver(), model).run("Full request", subgoals=["One milestone"], execute=True)
        self.assertEqual(result["subgoals"], 2)
        self.assertEqual(model.decide.call_args.args[1], "Full request")

    def test_done_with_contradictory_blocked_watcher_is_not_complete(self):
        model = Mock()
        model.decide.return_value = replace(DemoModel().decide(screen("results"), "", []), blocked_probability=.99)
        result = Agent(DemoDriver(), model).run("Search", execute=True)
        self.assertEqual(result["status"], "inconsistent_completion")

    def test_completion_recheck_also_rejects_contradictory_watcher(self):
        done = DemoModel().decide(screen("results"), "", [])
        model = Mock()
        model.decide.side_effect = [done, replace(done, blocked_probability=.99)]
        result = Agent(DemoDriver(), model).run("Search", execute=True)
        self.assertEqual(result["status"], "completion_not_confirmed")


    def test_repeat_effect_after_acknowledged_dispatch_is_refused(self):
        # Same labeled control re-tapped after the screen moved on: the narrow
        # (operation, label) effect key refuses the duplicate even though the
        # observation changed, so blocked_pairs never saw it as ineffective.
        driver = DemoDriver()
        model = Mock()
        model.verify_action.return_value = ActionSupport.ALLOWED
        model.decide.side_effect = [
            Decision("TAP", "0", 1, 0, 0, "test", 0, {}, StopGate.CONTINUE),
            Decision("TAP", "1", 1, 0, 0, "test", 0, {}, StopGate.CONTINUE)]
        result = Agent(driver, model).run("Tap Search twice", execute=True)
        self.assertEqual(driver.actions, ["TAP"])
        self.assertEqual(result["status"], "duplicate_effect_blocked")

    def test_pre_dispatch_refusal_releases_the_effect_slot(self):
        driver = DemoDriver()
        driver.execute = Mock(side_effect=[DriverRejection("stale_revision"), None])
        model = Mock()
        model.verify_action.return_value = ActionSupport.ALLOWED
        model.decide.side_effect = [
            Decision("TAP", "0", 1, 0, 0, "test", 0, {}, StopGate.CONTINUE),
            Decision("TAP", "0", 1, 0, 0, "test", 0, {}, StopGate.CONTINUE),
            Decision("DONE", None, 1, 1, 0, "test", 0, {}, StopGate.CONTINUE),
            Decision("DONE", None, 1, 1, 0, "test", 0, {}, StopGate.CONTINUE)]
        result = Agent(driver, model).run("Tap once", execute=True)
        self.assertEqual(driver.execute.call_count, 2)
        self.assertEqual(result["status"], "completed_unverified")


    def test_unclear_stop_gate_allows_reversible_wait_on_empty_screen(self):
        events = []
        driver = DemoDriver()
        driver.observe = Mock(return_value=from_ocr([], 400, 800))
        model = Mock()
        model.decide.side_effect = [
            Decision("WAIT", None, 0.7, 0, 0, "test", 0, {}, StopGate.UNCLEAR),
            Decision("DONE", None, 1, 1, 0, "test", 0, {}, StopGate.CONTINUE),
            Decision("DONE", None, 1, 1, 0, "test", 0, {}, StopGate.CONTINUE)]
        result = Agent(driver, model, emit=events.append).run("Wait, then finish", execute=True)
        self.assertEqual(result["status"], "completed_unverified")
        self.assertIn({"event": "stop_gate_unclear", "step": 0, "operation": "WAIT"}, events)

    def test_unclear_stop_gate_still_blocks_wait_despite_content(self):
        driver = DemoDriver()
        model = Mock()
        model.decide.return_value = Decision("WAIT", None, 0.7, 0, 0, "test", 0, {}, StopGate.UNCLEAR)
        result = Agent(driver, model).run("Wait under uncertainty, unless a sign-in wall appears", execute=True)
        self.assertEqual(result["status"], "needs_clarification")

    def test_unclear_stop_gate_without_any_condition_lets_a_wait_through(self):
        driver = DemoDriver()
        model = Mock()
        model.decide.side_effect = [Decision("WAIT", None, 0.7, 0, 0, "test", 0, {}, StopGate.UNCLEAR),
                                    Decision("DONE", None, 1, 1, 0, "test", 0, {}, StopGate.CONTINUE),
                                    Decision("DONE", None, 1, 1, 0, "test", 0, {}, StopGate.CONTINUE)]
        events = []
        Agent(driver, model, emit=events.append).run("Wait under uncertainty", execute=True)
        self.assertIn("stop_gate_unfounded", [e["event"] for e in events])

    def test_unclear_stop_gate_still_blocks_state_changing_tap(self):
        driver = DemoDriver()
        model = Mock()
        model.verify_action.return_value = ActionSupport.ALLOWED
        model.decide.return_value = Decision("TAP", "0", 0.7, 0, 0, "test", 0, {}, StopGate.UNCLEAR)
        result = Agent(driver, model).run("Tap under uncertainty", execute=True)
        self.assertEqual(driver.actions, [])
        self.assertEqual(result["status"], "needs_clarification")

    def test_host_keys_dispatch_on_device_level_wda_snapshots(self):
        state = from_wda(XML)
        driver = DemoDriver()
        driver.observe = Mock(return_value=state)
        driver.host_operations = frozenset({"HOME", "VOLUME_UP", "VOLUME_DOWN"})
        model = Mock()
        model.decide.side_effect = [
            Decision("HOME", None, 1, 0, 0, "test", 0, {}, StopGate.CONTINUE),
            Decision("DONE", None, 1, 1, 0, "test", 0, {}, StopGate.CONTINUE),
            Decision("DONE", None, 1, 1, 0, "test", 0, {}, StopGate.CONTINUE)]
        result = Agent(driver, model).run("Go home", execute=True)
        self.assertEqual(driver.actions, ["HOME"])
        self.assertEqual(result["status"], "completed_unverified")

    def test_host_keys_rejected_on_ocr_snapshots(self):
        state = from_ocr([{"text": "Search", "rect": [.1, .1, .5, .1]}], 400, 800)
        driver = DemoDriver()
        driver.observe = Mock(return_value=state)
        driver.host_operations = frozenset({"VOLUME_UP"})
        model = Mock()
        model.decide.return_value = Decision("VOLUME_UP", None, 1, 0, 0, "test", 0, {}, StopGate.CONTINUE)
        result = Agent(driver, model).run("Turn it up", execute=True)
        self.assertEqual(result["status"], "invalid_action")
        self.assertEqual(driver.actions, [])

    def test_host_keys_offered_on_wda_snapshots_without_a_bundle(self):
        criteria = {}

        def fake_inference(http, path, body, timeout, **kwargs):
            criteria.update(body["questions"]["operation"]["criteria"])
            options = list(body["questions"]["operation"]["criteria"])
            probs = {option: (0.9 if option == "HOME" else 0.1 / max(1, len(options) - 1))
                     for option in options}
            total = sum(probs.values())
            probs = {option: value / total for option, value in probs.items()}
            return {"model": "jev-latest", "usage": {}, "answers": {
                "operation": {"type": "choice", "choice": "HOME", "probabilities": probs,
                              "confidence": 0.9},
                "goal": {"type": "noul", "noul": 0.95},
                "blocked": {"type": "noul", "noul": 0.01},
                "stop_gate": {"type": "choice", "choice": "continue",
                              "probabilities": {"continue": 0.9, "stop": 0.05, "unclear": 0.05},
                              "confidence": 0.9}}}

        with patch("mobile_agent.models.request_inference", side_effect=fake_inference):
            decision = Jev(key="test").decide(from_wda(XML), "Go home", [],
                                              host_operations={"HOME", "VOLUME_UP", "VOLUME_DOWN"})
        self.assertIn("HOME", criteria)
        self.assertIn("VOLUME_UP", criteria)
        self.assertIn("VOLUME_DOWN", criteria)
        self.assertEqual(decision.operation, "HOME")

    def test_existing_field_value_blocks_duplicate_append(self):
        state = screen("search")
        state.elements[0] = replace(state.elements[0], value="coffee")
        driver = DemoDriver()
        driver.observe = Mock(return_value=state)
        result = Agent(driver, DemoModel(), DemoHelper()).run("Search coffee", execute=True)
        self.assertEqual(result["status"], "duplicate_text_blocked")
        self.assertEqual(driver.actions, [])

    def test_zero_settle_does_not_request_zero_timeout_native_wait(self):
        driver = DemoDriver()
        driver.wait_for_change = Mock(side_effect=AssertionError("must not wait"))
        result = Agent(driver, DemoModel(), max_steps=1, settle_seconds=0).run("Search", execute=True)
        self.assertEqual(result["status"], "max_steps")
        self.assertEqual(result["last_action_outcome"], "observed")
        driver.wait_for_change.assert_not_called()

    def test_observation_overrun_cannot_authorize_inference(self):
        clock, model = Clock(), Mock()
        driver = DemoDriver()
        def observe(timeout):
            clock.now += 2
            return screen()
        driver.observe = observe
        with patch("mobile_agent.agent.time.monotonic", clock):
            result = Agent(driver, model, max_seconds=1).run("Search", execute=True)
        self.assertEqual(result["status"], "timeout")
        model.decide.assert_not_called()

    def test_error_outcome_differentiates_before_during_and_after_action(self):
        driver = DemoDriver()
        driver.observe = Mock(side_effect=ConnectionError())
        before = Agent(driver, DemoModel()).run("Search", execute=True)
        self.assertEqual((before["attempted_actions"], before["last_action_outcome"]), (0, "none"))
        driver = DemoDriver()
        driver.execute = Mock(side_effect=ConnectionError())
        during = Agent(driver, DemoModel()).run("Search", execute=True)
        self.assertEqual((during["attempted_actions"], during["last_action_outcome"]), (1, "unknown"))
        driver = DemoDriver()
        driver.observe = Mock(side_effect=[screen(), screen(), screen(), ConnectionError()])
        after = Agent(driver, DemoModel()).run("Search", execute=True)
        self.assertEqual((after["actions"], after["last_action_outcome"]), (1, "acknowledged"))
        self.assertEqual(driver.actions, ["TAP"])


class TransportHardeningTests(unittest.TestCase):
    def test_deadlines_reject_nonfinite_and_boolean_timeouts(self):
        for timeout in [0, -1, True, float("nan"), float("inf"), "1"]:
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                Deadline(timeout)

    def test_strict_json_rejects_duplicate_fields_and_nonfinite(self):
        for raw in ['{"ok":false,"ok":true}', '{"n":NaN}', '{"n":Infinity}', '{"n":1e999}']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                decode_json(raw)

    def test_http_rejected_or_oversized_response_always_closes_connection(self):
        for status, raw in [(503, b'{}'), (200, b'x' * 16000001), (200, b'[]'),
                            (200, b'{"ok":false,"ok":true}')]:
            client = HTTP("http://localhost")
            connection = Mock(sock=None)
            connection.getresponse.return_value = SimpleNamespace(status=status, read=lambda limit: raw)
            client.connection = connection
            with self.subTest(status=status, size=len(raw)), self.assertRaises(TransportError):
                client.request("GET", "/test")
            connection.close.assert_called()
            self.assertEqual(connection.request.call_count, 1)

    def test_http_truncated_content_length_cannot_become_valid_json(self):
        client = HTTP("http://localhost")
        connection = Mock(sock=None)
        connection.getresponse.return_value = SimpleNamespace(status=200, length=100, read=lambda limit: b'{}')
        client.connection = connection
        with self.assertRaises(TransportError):
            client.request("GET", "/test")
        connection.close.assert_called()

    def test_http_slow_drip_is_bounded_by_absolute_deadline(self):
        # AF_UNIX socketpair is fully local: no network service or model call is made.
        local, remote = socket.socketpair()
        client = HTTP("http://localhost")
        client.connection.sock = local
        def peer():
            try:
                remote.recv(4096)
                remote.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n")
                for _ in range(100):
                    remote.sendall(b" ")
                    time.sleep(.01)
            except OSError:
                pass
            finally:
                remote.close()
        thread = threading.Thread(target=peer, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with self.assertRaises(TransportError):
                client.request("GET", "/test", timeout=.08)
            self.assertLess(time.monotonic() - started, bound(.5))
        finally:
            client.close()
            thread.join(.5)
        self.assertFalse(thread.is_alive())


    def test_concurrent_http_request_is_not_replayed_or_interleaved(self):
        client = HTTP("http://localhost")
        client.connection = Mock(sock=None)
        client._inflight.acquire()
        try:
            with self.assertRaises(TransportError):
                client.request("GET", "/test")
            client.connection.request.assert_not_called()
        finally:
            client._inflight.release()


OUTPUT_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}},
                 "required": ["title"], "additionalProperties": False}


class ExtractionHardeningTests(unittest.TestCase):
    def evidence(self):
        evidence = Evidence()
        evidence.add(screen("results"), 2)
        return evidence.public()

    def extraction(self):
        return {"data": {"title": "Coffee brewing guide"}, "citations": [{
            "path": "/title", "evidence_id": "e0", "quote": "Coffee brewing guide"}]}

    def test_schema_is_canonical_validated_copy(self):
        schema = validate_schema(OUTPUT_SCHEMA)
        self.assertEqual(schema, OUTPUT_SCHEMA)
        self.assertIsNot(schema, OUTPUT_SCHEMA)

    def test_schema_rejects_network_refs_regex_unbounded_arrays_and_open_objects(self):
        for schema in [True, {"$ref": "https://example.invalid/schema"},
                       {"type": "string", "pattern": "(a+)+$"}, {"type": "object"},
                       {"type": "array", "items": {"type": "string"}},
                       {"type": "object", "properties": {}, "required": ["undefined"], "additionalProperties": False},
                       {"type": "string", "anyOf": [{"type": "string"}]}]:
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                validate_schema(schema)

    def test_literal_data_with_valid_citations_passes(self):
        schema = validate_schema(OUTPUT_SCHEMA)
        self.assertEqual(validate_extraction(self.extraction(), schema, self.evidence())["data"],
                         {"title": "Coffee brewing guide"})

    def test_schema_violation_or_invented_fact_fails(self):
        for data in [{"title": 1}, {"title": "Invented guide"}, {"title": "Coffee brewing guide", "extra": "x"}]:
            extraction = self.extraction()
            extraction["data"] = data
            with self.subTest(data=data), self.assertRaises(ValueError):
                validate_extraction(extraction, OUTPUT_SCHEMA, self.evidence())

    def test_missing_duplicate_and_fabricated_citations_fail(self):
        for changes in [{"path": "/wrong"}, {"evidence_id": "invented"}, {"quote": "Not observed"},
                        {"quote": "Coffee"}]:
            extraction = self.extraction()
            extraction["citations"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_extraction(extraction, OUTPUT_SCHEMA, self.evidence())
        for citations in [[], self.extraction()["citations"] * 2]:
            with self.assertRaises(ValueError):
                validate_extraction({"data": {"title": "Coffee brewing guide"}, "citations": citations},
                                    OUTPUT_SCHEMA, self.evidence())

    def test_number_cannot_be_substring_of_another_number_or_shorthand(self):
        schema = validate_schema({"type": "integer"})
        for quote in ["100 likes", "1.2K likes"]:
            evidence = {"entries": [{"id": "e0", "text": quote}]}
            with self.assertRaises(ValueError):
                validate_extraction({"data": 1, "citations": [{"path": "", "evidence_id": "e0", "quote": quote}]}, schema, evidence)

    def test_empty_array_cannot_invent_absence_of_results(self):
        schema = validate_schema({"type": "array", "items": {"type": "string"}, "maxItems": 10})
        with self.assertRaises(ValueError):
            validate_extraction({"data": [], "citations": []}, schema, self.evidence())

    def test_nullable_value_is_explicit_unknown_not_uncited_fact(self):
        schema = validate_schema({"type": ["string", "null"]})
        self.assertIsNone(validate_extraction({"data": None, "citations": []}, schema, self.evidence())["data"])

    def test_json_pointer_escaping_for_real_property_names(self):
        schema = validate_schema({"type": "object", "properties": {"a/b~c": {"type": "string"}},
                                  "additionalProperties": False})
        extraction = self.extraction()
        extraction["data"] = {"a/b~c": "Coffee brewing guide"}
        extraction["citations"][0]["path"] = "/a~1b~0c"
        self.assertEqual(validate_extraction(extraction, schema, self.evidence())["data"], extraction["data"])

    def test_evidence_is_bounded_and_deduplicated(self):
        evidence = Evidence()
        for index in range(410):
            current = screen()
            current.elements = [replace(current.elements[0], id=str(index), label=str(index))]
            evidence.add(current, index)
            evidence.add(current, index)
        self.assertEqual(len(evidence.entries), 400)
        self.assertTrue(evidence.truncated)

    def test_no_helper_does_not_fabricate_schema_data(self):
        driver = DemoDriver()
        driver.stage = "results"
        result = Agent(driver, DemoModel()).run("Read result", execute=True, output_schema=OUTPUT_SCHEMA)
        self.assertEqual(result["status"], "completion_not_confirmed")
        self.assertEqual(result["data_status"], "helper_unavailable")
        self.assertFalse(result["schema_validated"])
        self.assertIsNone(result["data"])
        self.assertTrue(result["evidence"]["entries"])

    def test_structured_result_is_validated_after_helper(self):
        driver = DemoDriver()
        driver.stage = "results"
        helper = DemoHelper()
        helper.extract = Mock(return_value=self.extraction())
        result = Agent(driver, DemoModel(), helper).run("Read result", execute=True, output_schema=OUTPUT_SCHEMA)
        self.assertEqual(result["data_status"], "extracted")
        self.assertTrue(result["schema_validated"])
        self.assertFalse(result["independently_verified"])
        self.assertEqual(result["data"], {"title": "Coffee brewing guide"})

    def test_bad_helper_output_cannot_complete_a_requested_extraction(self):
        driver = DemoDriver()
        driver.stage = "results"
        helper = DemoHelper()
        helper.extract = Mock(return_value={"data": {"title": "Made up"}, "citations": []})
        result = Agent(driver, DemoModel(), helper).run("Read result", execute=True, output_schema=OUTPUT_SCHEMA)
        self.assertEqual(result["status"], "completion_not_confirmed")
        self.assertEqual(result["data_status"], "extraction_failed")
        self.assertIsNone(result["data"])

    def test_schema_errors_prevent_device_observation(self):
        driver = DemoDriver()
        driver.observe = Mock()
        with self.assertRaises(ValueError):
            Agent(driver, DemoModel()).run("Read", execute=True, output_schema={"type": "object"})
        driver.observe.assert_not_called()

    def test_no_schema_still_returns_real_observed_evidence(self):
        result = Agent(DemoDriver(), DemoModel()).run("Search")
        self.assertEqual(result["data_status"], "not_requested")
        self.assertEqual(result["evidence"]["entries"][0]["text"], "Search")


if __name__ == "__main__":
    unittest.main()

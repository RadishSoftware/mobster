import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from mobile_agent import permissions
from mobile_agent.ai_plans.phone_tools import CHAINABLE, NAMES, PhoneTools
from mobile_agent.drivers import WDA
from mobile_agent.frontier import FrontierAgent
from mobile_agent.state import Element, from_wda
from mobile_agent.tests.test_frontier import Driver, screen
from mobile_agent.tests.test_seam_prompts import Recording
from mobile_agent.wda.point_gestures import check_gesture_path, same_point_image, validate_gesture_request

REQUEST = {"start_x": 25, "start_y": 50, "end_x": 75, "end_y": 50,
           "start_label": "slider handle", "end_label": "right end", "duration": .5}
HOLD = {"x": 50, "y": 50, "label": "slider handle"}
FLICK = {key: value for key, value in REQUEST.items() if key != "duration"}
NEW_GESTURES = (("hold", HOLD, ("HOLD", .5, .5, 1.5)),
                ("flick", FLICK, ("FLICK", .25, .5, .75, .5, .12)))
GOAL = "Move the slider"
APPS = {"com.example.mail": "Canvas"}


def blank():
    return screen(Element("title", "Drawing board", "StaticText", (.1, .1, .8, .1)))


class GesturePhone(Driver):
    capabilities = Driver.capabilities | {"capture_preview"}

    def __init__(self):
        super().__init__([blank(), screen(Element("done", "Slider moved", "StaticText", (.1, .1, .8, .1)))])
        self.color = "white"

    def capture_preview(self, timeout=5):
        out = io.BytesIO()
        Image.new("RGB", (400, 800), self.color).save(out, "PNG")
        return out.getvalue()

    def drag_point(self, sx, sy, ex, ey, snapshot, *, duration, hold_seconds, timeout):
        self.last_options = {"duration": duration, "hold_seconds": hold_seconds, "timeout": timeout}
        self.actions.append(("DRAG", sx, sy, ex, ey, duration))
        self.index += 1

    def hold_point(self, x, y, snapshot, *, duration, timeout):
        self.last_options = {"duration": duration, "timeout": timeout}
        self.actions.append(("HOLD", x, y, duration))
        self.index += 1

    def flick_point(self, sx, sy, ex, ey, snapshot, *, duration, timeout):
        self.last_options = {"duration": duration, "timeout": timeout}
        self.actions.append(("FLICK", sx, sy, ex, ey, duration))
        self.index += 1


def two_controls(phone):
    phone.screens[0] = screen(Element("handle", "Slider handle", "Image", (.2, .45, .1, .1)),
                              Element("end", "Right end", "Image", (.7, .45, .1, .1)))


class Base(unittest.TestCase):
    def setUp(self):
        self.video = patch("mobile_agent.frontier.screen.video_frame", return_value=None)
        self.video.start()
        self.addCleanup(self.video.stop)

    def tools(self, phone=None, mode=permissions.ASK_IMPORTANT, gate=None, **options):
        phone = phone or GesturePhone()
        tools = PhoneTools(phone, apps=APPS, goal=GOAL, emit=lambda _: None,
                           permissions=gate or permissions.PermissionGate(mode, goal=GOAL),
                           build=lambda *a, **kw: FrontierAgent(*a, **kw, settle_seconds=0), **options)
        self.assertFalse(tools.call("screen", {}).is_error)
        return phone, tools

    def native(self, phone, actions, *, approve=None, emit=None, cancelled=None, mode=None):
        client = Recording([actions, ("DONE", None, None), ("DONE", None, None)])
        agent = FrontierAgent(phone, client, apps=APPS, skills=(), settle_seconds=0, approve=approve, emit=emit,
                              cancelled=cancelled,
                              permissions=permissions.PermissionGate(mode, goal=GOAL) if mode else None)
        return agent, client


class ValidationTests(unittest.TestCase):
    def test_invalid_coordinates_refuse(self):
        for value in (True, float("nan"), float("inf"), 10 ** 1000, 0, 100):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_gesture_request("drag", {**REQUEST, "end_x": value})

    def test_sensitive_controls_on_the_path_refuse_and_off_the_path_do_not(self):
        drag = validate_gesture_request("drag", REQUEST)
        for label, role in (("Delete", "Button"), ("Brightness", "Slider"), ("Date", "DatePicker"),
                            ("Choice", "SegmentedControl"), ("Password", "SecureTextField")):
            with self.subTest(role=role):
                with self.assertRaises(ValueError):
                    check_gesture_path(screen(Element("control", label, role, (.45, .45, .1, .1))), drag)
                check_gesture_path(screen(Element("control", label, role, (.45, .7, .1, .1))), drag)

    def test_duration_and_shape_boundaries_refuse(self):
        for kind, arguments, _ in NEW_GESTURES:
            for value in (True, "1", float("nan"), float("inf"), -.1, 11):
                with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                    validate_gesture_request(kind, {**arguments, "duration": value})
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validate_gesture_request(kind, {**arguments, "hold_seconds": 1})
        with self.assertRaises(ValueError):
            validate_gesture_request("flick", {**FLICK, "end_x": 25})
        for hold in (True, float("nan"), -.1, 2.1):
            with self.subTest(hold=hold), self.assertRaises(ValueError):
                validate_gesture_request("drag", {**REQUEST, "hold_seconds": hold})

    def test_same_point_image_sees_a_change_anywhere_and_refuses_what_it_cannot_read(self):
        def part(color, patch_at=None):
            image = Image.new("RGB", (200, 400), color)
            if patch_at:
                image.paste("black", patch_at)
            out = io.BytesIO()
            image.save(out, "JPEG")
            import base64
            return {"image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()}}
        self.assertTrue(same_point_image(part("white"), part("white"), 50, 50))
        self.assertFalse(same_point_image(part("white"), part("white", (150, 300, 190, 390)), 50, 50))
        self.assertFalse(same_point_image(part("white"), part("blue"), 50, 50))
        self.assertFalse(same_point_image(part("white"), {"image_url": {"url": "nope"}}, 50, 50))
        self.assertFalse(same_point_image(part("white"), None, 50, 50))


class OutsideAgentTests(Base):
    def test_a_drag_is_sent_once_returns_a_fresh_screen_and_cannot_chain(self):
        phone, tools = self.tools(mode=permissions.DONT_ASK)
        result = tools.call("drag_point", REQUEST)
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(phone.actions, [("DRAG", .25, .5, .75, .5, .5)])
        self.assertIn("Dragged", result.text)
        self.assertTrue(result.images)
        for name in ("drag_point", "hold_point", "flick_point", "tap_point"):
            self.assertIn(name, NAMES)
            self.assertNotIn(name, CHAINABLE)

    def test_a_gesture_needs_the_screenshot_it_was_chosen_from(self):
        phone = GesturePhone()
        tools = PhoneTools(phone, apps=APPS, goal=GOAL, emit=lambda _: None,
                           permissions=permissions.PermissionGate(permissions.DONT_ASK),
                           build=lambda *a, **kw: FrontierAgent(*a, **kw, settle_seconds=0))
        self.assertTrue(tools.call("drag_point", REQUEST).is_error)
        self.assertFalse(tools.call("screen", {"image": False}).is_error)
        self.assertTrue(tools.call("hold_point", HOLD).is_error)
        self.assertEqual(phone.actions, [])

    def test_a_gesture_uses_its_picture_once(self):
        phone, tools = self.tools(mode=permissions.DONT_ASK)
        self.assertFalse(tools.call("drag_point", REQUEST).is_error)
        phone.index = 0
        self.assertTrue(tools.call("drag_point", REQUEST).is_error)
        self.assertEqual(len(phone.actions), 1)

    def test_a_gesture_asks_as_a_whole_and_a_denial_sends_nothing(self):
        phone = GesturePhone()
        two_controls(phone)
        approve = Mock(return_value="denied")
        _, tools = self.tools(phone, approve=approve)
        self.assertTrue(tools.call("drag_point", REQUEST).is_error)
        approve.assert_called_once()
        self.assertEqual(approve.call_args.args[0]["operation"], "DRAG_XY")
        self.assertEqual(phone.actions, [])

    def test_an_approved_gesture_is_sent_and_full_access_asks_nothing(self):
        phone = GesturePhone()
        approve = Mock(return_value="approved")
        _, tools = self.tools(phone, approve=approve)
        self.assertFalse(tools.call("drag_point", REQUEST).is_error)
        approve.assert_called_once()
        self.assertEqual(len(phone.actions), 1)
        phone = GesturePhone()
        approve = Mock(return_value="denied")
        _, tools = self.tools(phone, mode=permissions.DONT_ASK, approve=approve)
        self.assertFalse(tools.call("drag_point", REQUEST).is_error)
        approve.assert_not_called()
        self.assertEqual(len(phone.actions), 1)

    def test_nobody_to_ask_refuses(self):
        phone, tools = self.tools()
        result = tools.call("drag_point", REQUEST)
        self.assertTrue(result.is_error)
        self.assertIn("needs the person's OK", result.text)
        self.assertEqual(phone.actions, [])

    def test_a_block_at_the_second_endpoint_stops_a_full_access_drag(self):
        phone = GesturePhone()
        two_controls(phone)
        seen = []

        class Gate(permissions.PermissionGate):
            def decide(self, action, **kwargs):
                seen.append(action.label)
                if action.label == "Right end":
                    return permissions.Decision("block", "navigate", None, "endpoint blocked", 0.1)
                return super().decide(action, **kwargs)

        _, tools = self.tools(phone, gate=Gate(permissions.DONT_ASK, goal=GOAL))
        result = tools.call("drag_point", REQUEST)
        self.assertTrue(result.is_error)
        self.assertIn("endpoint blocked", result.text)
        self.assertEqual(seen, ["Slider handle", "Right end"])
        self.assertEqual(phone.actions, [])

    def test_changed_denied_cancelled_or_changed_during_approval_never_dispatch(self):
        for kind, arguments, _ in (("drag", REQUEST, None), *NEW_GESTURES):
            for mode in ("changed", "denied", "cancelled", "approval_changed", "sensitive"):
                with self.subTest(kind=kind, mode=mode):
                    phone, stopped = GesturePhone(), [False]
                    if mode == "sensitive":
                        phone.screens[0] = screen(Element("delete", "Delete", "Button", (.45, .45, .1, .1)))

                    def approve(_):
                        if mode == "approval_changed":
                            phone.color = "blue"
                        if mode == "cancelled":
                            stopped[0] = True
                        return "denied" if mode == "denied" else "approved"

                    _, tools = self.tools(phone, approve=approve, cancelled=lambda: stopped[0])
                    if mode == "changed":
                        phone.color = "blue"
                    result = tools.call(kind + "_point", arguments)
                    self.assertTrue(result.is_error, result.text)
                    self.assertEqual(phone.actions, [])

    def test_a_changed_tree_never_dispatches(self):
        phone, tools = self.tools(mode=permissions.DONT_ASK)
        phone.screens[0] = screen(Element("other", "Something else", "StaticText", (.1, .1, .8, .1)))
        self.assertTrue(tools.call("hold_point", HOLD).is_error)
        self.assertEqual(phone.actions, [])

    def test_a_label_naming_a_commit_the_request_never_asked_for_is_refused(self):
        phone, tools = self.tools(mode=permissions.DONT_ASK)
        result = tools.call("hold_point", {**HOLD, "label": "Send message"})
        self.assertTrue(result.is_error)
        self.assertEqual(phone.actions, [])

    def test_an_unknown_outcome_stops_the_run_before_any_retry(self):
        for kind, arguments, _ in (("drag", REQUEST, None), *NEW_GESTURES):
            with self.subTest(kind=kind):
                phone, tools = self.tools(mode=permissions.DONT_ASK)

                def fail(*a, **kw):
                    phone.actions.append("attempted")
                    raise RuntimeError("ConnectionRefusedError")

                setattr(phone, kind + "_point", fail)
                first = tools.call(kind + "_point", arguments)
                self.assertTrue(first.is_error)
                self.assertIn("unknown", first.text)
                self.assertIn(kind, first.text)
                self.assertTrue(tools.call(kind + "_point", arguments).is_error)
                self.assertTrue(tools.call("screen", {}).is_error)
                self.assertEqual(phone.actions, ["attempted"])

    def test_an_acknowledged_drag_consumes_the_picture_even_if_reading_the_result_fails(self):
        phone, tools = self.tools(mode=permissions.DONT_ASK)
        with patch.object(tools.agent, "_settle", side_effect=TimeoutError("read failed")):
            self.assertTrue(tools.call("drag_point", REQUEST).is_error)
        self.assertTrue(tools.call("drag_point", REQUEST).is_error)
        self.assertEqual(len(phone.actions), 1)

    def test_a_failed_journal_write_after_dispatch_stops_without_replaying(self):
        phone, tools = self.tools(mode=permissions.DONT_ASK)

        def failed_ack(event):
            if event["event"] == "action_acknowledged":
                raise OSError("journal unavailable")

        tools.out = failed_ack
        self.assertTrue(tools.call("drag_point", REQUEST).is_error)
        self.assertTrue(tools.call("drag_point", REQUEST).is_error)
        self.assertEqual(len(phone.actions), 1)

    def test_a_failed_journal_write_before_dispatch_sends_nothing(self):
        phone, tools = self.tools(mode=permissions.DONT_ASK)

        def failed_start(event):
            if event["event"] == "action_started":
                raise OSError("journal unavailable")

        tools.out = failed_start
        self.assertTrue(tools.call("drag_point", REQUEST).is_error)
        self.assertEqual(phone.actions, [])

    def test_hold_flick_and_drag_each_act_once_with_start_and_acknowledgment(self):
        for kind, arguments, expected in NEW_GESTURES:
            with self.subTest(kind=kind):
                phone, tools = self.tools(mode=permissions.DONT_ASK)
                events = []
                tools.out = events.append
                result = tools.call(kind + "_point", arguments)
                self.assertFalse(result.is_error, result.text)
                self.assertTrue(result.images)
                self.assertEqual(phone.actions, [expected])
                self.assertEqual([(e["event"], e["operation"]) for e in events
                                  if e["event"] in ("action_started", "action_acknowledged")],
                                 [("action_started", kind.upper() + "_XY"),
                                  ("action_acknowledged", kind.upper() + "_XY")])

    def test_a_hold_has_a_response_budget_and_a_drag_a_pickup_hold(self):
        phone, tools = self.tools(mode=permissions.DONT_ASK)
        self.assertFalse(tools.call("hold_point", {**HOLD, "duration": 10}).is_error)
        self.assertEqual(phone.last_options, {"duration": 10, "timeout": 15})
        for value in (0, 2):
            phone, tools = self.tools(mode=permissions.DONT_ASK)
            self.assertFalse(tools.call("drag_point", {**REQUEST, "hold_seconds": value}).is_error)
            self.assertEqual(phone.last_options["hold_seconds"], value)
        phone, tools = self.tools(mode=permissions.DONT_ASK)
        self.assertFalse(tools.call("drag_point", REQUEST).is_error)
        self.assertEqual(phone.last_options["hold_seconds"], .1)

    def test_a_driver_without_the_gesture_refuses_before_sending(self):
        phone, tools = self.tools(mode=permissions.DONT_ASK)
        phone.hold_point = None
        result = tools.call("hold_point", HOLD)
        self.assertTrue(result.is_error)
        self.assertEqual(phone.actions, [])

    def test_tap_point_taps_once_asks_where_the_gate_asks_and_a_changed_screen_refuses(self):
        phone, tools = self.tools(mode=permissions.DONT_ASK)
        result = tools.call("tap_point", {"x": 50, "y": 50, "label": "round button"})
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(phone.actions, [("TAP_POINT", .5, .5)])
        self.assertTrue(result.images)
        phone, tools = self.tools(mode=permissions.DONT_ASK)
        phone.color = "blue"
        self.assertTrue(tools.call("tap_point", {"x": 50, "y": 50, "label": "round button"}).is_error)
        self.assertEqual(phone.actions, [])
        phone, tools = self.tools(mode=permissions.DONT_ASK)
        for bad in ({"x": 0, "y": 50}, {"x": 50, "y": 100}, {"x": True, "y": 50}, {"x": 50, "y": 50, "label": " "}):
            self.assertTrue(tools.call("tap_point", {"label": "round button", **bad}).is_error)
        self.assertEqual(phone.actions, [])

    def test_a_tap_point_whose_outcome_is_unknown_is_not_retried(self):
        phone, tools = self.tools(mode=permissions.DONT_ASK)

        def fail(*args, **kwargs):
            phone.actions.append("attempted")
            raise RuntimeError("ConnectionRefusedError")

        phone.tap_point = fail
        self.assertTrue(tools.call("tap_point", {"x": 50, "y": 50, "label": "round button"}).is_error)
        self.assertTrue(tools.call("tap_point", {"x": 50, "y": 50, "label": "round button"}).is_error)
        self.assertEqual(phone.actions, ["attempted"])


class NativeAgentTests(Base):
    def test_post_dispatch_observer_failure_has_typed_unknown_result(self):
        phone, events = GesturePhone(), []

        def observe(event):
            events.append(event)
            if event["event"] == "action_acknowledged":
                raise RuntimeError("post-dispatch observer failed")

        agent, _ = self.native(phone, [("DRAG_XY", None, json.dumps(REQUEST)),
                                       ("TAP_XY", None, "tile")], emit=observe)
        result = agent.run(GOAL)
        self.assertEqual(phone.actions, [("DRAG", .25, .5, .75, .5, .5)])
        self.assertEqual((result["status"], result["code"]), ("blocked", "unknown_outcome"))
        self.assertIn("unknown", result["reason"])

    def test_a_decision_and_a_start_are_journaled_before_the_one_drag_and_the_chain_ends(self):
        phone, events = GesturePhone(), []
        original = phone.drag_point

        def drag(*args, **kwargs):
            self.assertEqual(events[-1]["event"], "action_started")
            self.assertIn("frontier_decision", [e["event"] for e in events])
            return original(*args, **kwargs)

        phone.drag_point = drag
        agent, _ = self.native(phone, [("DRAG_XY", None, json.dumps(REQUEST)), ("TAP_XY", None, "tile")],
                               emit=events.append)
        result = agent.run(GOAL)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(phone.actions, [("DRAG", .25, .5, .75, .5, .5)])
        self.assertEqual([(e["event"], e["operation"]) for e in events
                          if e["event"] in ("action_started", "action_acknowledged")],
                         [("action_started", "DRAG_XY"), ("action_acknowledged", "DRAG_XY")])

    def test_each_gesture_acts_once(self):
        for kind, arguments, expected in (("drag", REQUEST, ("DRAG", .25, .5, .75, .5, .5)), *NEW_GESTURES):
            with self.subTest(kind=kind):
                phone, events = GesturePhone(), []
                agent, _ = self.native(phone, (kind.upper() + "_XY", None, json.dumps(arguments)),
                                       emit=events.append)
                result = agent.run(GOAL)
                self.assertEqual(result["status"], "completed", result)
                self.assertEqual(phone.actions, [expected])
                self.assertEqual([(e["event"], e["operation"]) for e in events
                                  if e["event"] in ("action_started", "action_acknowledged")],
                                 [("action_started", kind.upper() + "_XY"),
                                  ("action_acknowledged", kind.upper() + "_XY")])

    def test_changed_denied_cancelled_and_sensitive_points_never_dispatch(self):
        for kind, arguments, _ in (("drag", REQUEST, None), *NEW_GESTURES):
            for mode in ("changed", "denied", "cancelled", "sensitive"):
                with self.subTest(kind=kind, mode=mode):
                    phone, stopped = GesturePhone(), [False]
                    if mode == "sensitive":
                        phone.screens[0] = screen(Element("delete", "Delete", "Button", (.45, .45, .1, .1)))

                    def approve(_):
                        if mode == "cancelled":
                            stopped[0] = True
                        if mode == "changed":
                            phone.color = "blue"
                        return "denied" if mode == "denied" else "approved"

                    agent, _ = self.native(phone, (kind.upper() + "_XY", None, json.dumps(arguments)),
                                           approve=approve, cancelled=lambda: stopped[0], mode=permissions.ASK_ALL)
                    result = agent.run(GOAL)
                    self.assertIn(result["status"], ("completed", "approval_denied", "stopped"), result)
                    self.assertEqual(phone.actions, [])

    def test_a_cancel_before_dispatch_and_a_failed_start_write_send_nothing(self):
        phone, stopped = GesturePhone(), [False]

        def approve(_):
            stopped[0] = True
            return "approved"

        agent, _ = self.native(phone, ("DRAG_XY", None, json.dumps(REQUEST)), approve=approve,
                               cancelled=lambda: stopped[0], mode=permissions.ASK_ALL)
        self.assertEqual(agent.run(GOAL)["status"], "stopped")
        self.assertEqual(phone.actions, [])
        phone = GesturePhone()

        def emit(event):
            if event["event"] == "action_started":
                raise OSError("journal unavailable")

        agent, _ = self.native(phone, ("DRAG_XY", None, json.dumps(REQUEST)), emit=emit)
        try:
            agent.run(GOAL)
        except OSError:
            pass
        self.assertEqual(phone.actions, [])

    def test_a_screen_that_changes_while_the_person_decides_is_not_dragged(self):
        phone = GesturePhone()

        def approve(_):
            phone.color = "blue"
            return "approved"

        agent, _ = self.native(phone, ("DRAG_XY", None, json.dumps(REQUEST)), approve=approve,
                               mode=permissions.ASK_ALL)
        agent.run(GOAL)
        self.assertEqual(phone.actions, [])

    def test_an_unknown_outcome_ends_the_run_without_recovery_or_replay(self):
        for error in (RuntimeError("ConnectionRefusedError"), TypeError("failed after dispatch")):
            for kind, arguments, _ in (("drag", REQUEST, None), *NEW_GESTURES):
                with self.subTest(error=type(error).__name__, kind=kind):
                    phone = GesturePhone()

                    def fail(*args, **kwargs):
                        phone.actions.append("attempted")
                        raise error

                    setattr(phone, kind + "_point", fail)
                    agent, _ = self.native(phone, (kind.upper() + "_XY", None, json.dumps(arguments)))
                    with patch.object(agent, "_recover", return_value=True) as recover:
                        result = agent.run(GOAL)
                    self.assertEqual(result["status"], "blocked", result)
                    self.assertIn(f"{kind} outcome is unknown", result["reason"])
                    self.assertEqual(phone.actions, ["attempted"])
                    recover.assert_not_called()

    def test_bad_json_and_a_missing_picture_are_refused_without_a_gesture(self):
        phone = GesturePhone()
        agent, _ = self.native(phone, ("DRAG_XY", None, "not json"))
        agent.run(GOAL)
        agent, _ = self.native(phone, ("HOLD_XY", None, json.dumps({**HOLD, "x": 500})))
        agent.run(GOAL)
        self.assertEqual(phone.actions, [])
        agent = FrontierAgent(phone, Recording([("HOLD_XY", None, json.dumps(HOLD)), ("DONE", None, None),
                                                ("DONE", None, None)]),
                              apps=APPS, skills=(), settle_seconds=0, screenshots=False)
        agent.run(GOAL)
        self.assertEqual(phone.actions, [])


WDA_XML = ('<XCUIElementTypeApplication width="400" height="800">'
           '<XCUIElementTypeStaticText label="Board" x="10" y="20" width="100" height="40"/>'
           '</XCUIElementTypeApplication>')


class WdaGestureTests(unittest.TestCase):
    def driver(self):
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        return driver, from_wda(WDA_XML)

    def sent(self, driver):
        self.assertEqual(driver.call.call_count, 1)
        method, path, body = driver.call.call_args[0][:3]
        self.assertEqual((method, path), ("POST", "/actions"))
        return body["actions"][0]["actions"]

    def test_a_drag_with_a_pickup_hold_anchors_the_start_before_moving(self):
        driver, state = self.driver()
        driver.drag_point(.25, .5, .75, .5, state, duration=.5, hold_seconds=.2)
        self.assertEqual(self.sent(driver), [
            {"type": "pointerMove", "duration": 0, "x": 100, "y": 400}, {"type": "pointerDown"},
            {"type": "pause", "duration": 200}, {"type": "pointerMove", "duration": 0, "x": 100, "y": 400},
            {"type": "pointerMove", "duration": 500, "x": 300, "y": 400}, {"type": "pointerUp"}])

    def test_a_drag_without_a_hold_and_a_flick_have_no_pause(self):
        driver, state = self.driver()
        driver.drag_point(.25, .5, .75, .5, state, duration=1, hold_seconds=0)
        self.assertNotIn("pause", [step["type"] for step in self.sent(driver)])
        driver, state = self.driver()
        driver.flick_point(.25, .5, .75, .5, state, duration=.12)
        self.assertEqual(self.sent(driver)[2], {"type": "pointerMove", "duration": 120, "x": 300, "y": 400})

    def test_a_hold_presses_in_place_for_the_duration(self):
        driver, state = self.driver()
        driver.hold_point(.5, .5, state, duration=2)
        self.assertEqual(self.sent(driver), [
            {"type": "pointerMove", "duration": 0, "x": 200, "y": 400}, {"type": "pointerDown"},
            {"type": "pause", "duration": 2000}, {"type": "pointerUp"}])

    def test_bad_values_are_refused_before_anything_is_sent(self):
        driver, state = self.driver()
        for call in (lambda: driver.hold_point(.5, .5, state, duration=11),
                     lambda: driver.hold_point(1.5, .5, state, duration=1),
                     lambda: driver.hold_point(True, .5, state, duration=1),
                     lambda: driver.flick_point(.5, .5, .5, .5, state, duration=.12),
                     lambda: driver.flick_point(.1, .5, .9, .5, state, duration=1),
                     lambda: driver.drag_point(.1, .5, .9, .5, state, duration=3, hold_seconds=0),
                     lambda: driver.drag_point(.1, .5, .9, .5, state, duration=1, hold_seconds=3),
                     lambda: driver.drag_point(.1, .5, .9, .5, SimpleNamespace(width=0, height=0, captured_at=0),
                                               duration=1, hold_seconds=0)):
            with self.assertRaises(ValueError):
                call()
        driver.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()

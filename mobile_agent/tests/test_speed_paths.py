import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mobile_agent import drivers, frontier, smart_result
from mobile_agent.drivers import WDA
from mobile_agent.frontier import Action, FrontierAgent, prompt_text
from mobile_agent.state import Element, Snapshot, from_wda
from mobile_agent.tests.timing import bound
from mobile_agent.transport import TransportError
from mobile_agent.wda import observe as wda_observe


def app(*children):
    return '<XCUIElementTypeApplication width="400" height="800">' + "".join(children) + "</XCUIElementTypeApplication>"


class WDASettlingHookTests(unittest.TestCase):
    def test_reports_the_first_agreeing_pair_once_and_survives_callback_errors(self):
        before = from_wda(app('<XCUIElementTypeStaticText label="Settings" x="0" y="100" width="400" height="40"/>'))
        after = from_wda(app('<XCUIElementTypeStaticText label="General" x="0" y="100" width="400" height="40"/>'))
        driver = WDA("http://localhost:8100", "s")
        self.addCleanup(driver.close)
        driver.observe = Mock(return_value=after)
        seen = []

        def hook(read):
            seen.append(read.text)
            raise RuntimeError("speculation failure must not break settling")
        result = driver.wait_for_change(before, timeout=3, wait_seconds=.6, on_settling=hook)
        self.assertEqual(result.text, "General")
        self.assertEqual(seen, ["General"])


class WDAConfigureTests(unittest.TestCase):
    def setUp(self):
        WDA._screen_sizes.clear()
        self.addCleanup(WDA._screen_sizes.clear)

    def test_springboard_size_is_used_and_later_runs_configure_in_one_request(self):
        driver = WDA("http://localhost:8100", "s")
        self.addCleanup(driver.close)
        driver.http.request = Mock(return_value={"value": {"screenSize": {"width": 393, "height": 852}}})
        driver.call = Mock(return_value=None)
        driver.configure()
        self.assertEqual(driver.call.call_args.args[2]["settings"]["activeAppDetectionPoint"], "196,426")
        self.assertNotIn("/window/size", [c.args[1] for c in driver.call.call_args_list])
        later = WDA("http://localhost:8100", "s2")
        self.addCleanup(later.close)
        later.call = Mock(return_value=None)
        later.configure()
        later.call.assert_called_once()
        self.assertEqual(later.call.call_args.args[2]["settings"]["activeAppDetectionPoint"], "196,426")

    def test_a_hanging_window_size_falls_back_to_the_first_observation(self):
        driver = WDA("http://localhost:8100", "s")
        self.addCleanup(driver.close)
        posted = []

        def call(method, path, body=None, timeout=10):
            if path == "/window/size":
                self.assertLessEqual(timeout, 3)
                raise TransportError("stale element reference")
            if path == "/appium/settings":
                posted.append(body["settings"])
                return None
            return app('<XCUIElementTypeStaticText label="General" x="0" y="100" width="400" height="40"/>')
        driver.call = Mock(side_effect=call)
        driver.http.request = Mock(side_effect=TransportError("no WDA in unit tests"))
        driver.configure()
        self.assertEqual(posted[0]["activeAppDetectionPoint"], "160,320")
        driver.observe()
        self.assertEqual(posted[-1]["activeAppDetectionPoint"], "200,400")
        driver.observe()
        self.assertEqual(len(posted), 2)


class WDAForegroundHintTests(unittest.TestCase):
    def setUp(self):
        WDA._overlay_apps.clear()
        self.addCleanup(WDA._overlay_apps.clear)

    def driver(self, failures):
        driver = WDA("http://localhost:8100", "s")
        self.addCleanup(driver.close)
        self.settings, self.reads = [], 0
        page = app('<XCUIElementTypeStaticText label="Eiffel Tower" x="0" y="100" width="400" height="40"/>')

        def request(method, path, body=None, timeout=10):
            if path.endswith("/appium/settings"):
                self.settings.append(body["settings"])
            elif "/source" in path:
                self.reads += 1
                if self.reads <= failures:
                    raise TransportError("HTTP 404; request not retried")
                return {"value": page, "status": 0}
            return {"value": None, "status": 0}
        driver.http.request = Mock(side_effect=request)
        return driver

    def test_failed_read_is_retried_with_the_activated_app_as_hint(self):
        driver = self.driver(failures=1)
        driver.call("POST", "/wda/apps/activate", {"bundleId": "com.apple.mobilesafari"})
        self.assertEqual(driver.observe().text, "Eiffel Tower")
        self.assertEqual(self.settings, [{"defaultActiveApplication": "com.apple.mobilesafari"}] * 2)
        driver.call("POST", "/wda/apps/activate", {"bundleId": "com.apple.Preferences"})
        self.assertEqual(self.settings[-1], {"defaultActiveApplication": "com.apple.Preferences"})

    def test_without_a_known_app_or_after_one_retry_the_failure_stands(self):
        with self.assertRaises(TransportError):
            self.driver(failures=1).observe()
        driver = self.driver(failures=2)
        driver.call("POST", "/wda/apps/activate", {"bundleId": "com.apple.mobilesafari"})
        with self.assertRaises(TransportError):
            driver.observe()
        self.assertEqual(self.reads, 2)


FIXTURES = Path(__file__).parent / "fixtures" / "frontier"


def fixture_screens(name):
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    out = {}
    for key, rows in data["screens"].items():
        elements = [Element(r["id"], r["label"], r["role"], tuple(r["rect"]), editable=r.get("editable", False),
                            value=r.get("value", ""), locator=f"/{r['id']}") for r in rows]
        out[key] = Snapshot(elements, "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture",
                            bundle_id=data["bundle"])
    return out


class Still(drivers.Driver):
    capabilities = frozenset({"tap_point"})

    def __init__(self, screens, clock=time.monotonic):
        self.screens, self.index, self.actions, self.clock = list(screens), 0, [], clock
        self.first_action_at = None

    def observe(self, timeout=10):
        return self.screens[min(self.index, len(self.screens) - 1)]

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        if self.first_action_at is None:
            self.first_action_at = self.clock()
        self.actions.append((operation, getattr(target, "label", target), text))
        self.index += 1

    def tap_point(self, x, y, snapshot, timeout=10, purpose=None):
        self.execute("TAP_POINT", (round(x, 2), round(y, 2)), snapshot)

    def close(self):
        pass


class SlowModel:
    def __init__(self, steps, scale, items=(), data_json=None):
        self.steps, self.scale, self.items, self.data_json = list(steps), scale, list(items), data_json
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}
        self.lock = threading.Lock()
        self.kinds = []

    def complete(self, messages, schema, timeout=60, **kwargs):
        planning = "items" in schema["properties"] and "actions" not in schema["properties"]
        time.sleep((2.6 if planning else 2.4) * self.scale)
        with self.lock:
            self.kinds.append("planning" if planning else "decision" if "actions" in schema["properties"] else "other")
            if planning:
                return {"items": list(self.items)}, {}
            if "actions" not in schema["properties"]:
                return {"data": {"battery": "87%"}}, {}
            self.usage["calls"] += 1
            prompt = prompt_text(messages)
            rows = prompt.split("Screen elements:\n", 1)[1].splitlines()
            op, label = self.steps.pop(0) if self.steps else ("DONE", None)
            target = next((r.split()[0] for r in rows if label and f'"{label}"' in r), None)
            reply = {"thought": "", "plan": None, "notes_add": [], "checklist_updates": [],
                     "actions": [{"op": op, "target": target, "text": None}],
                     "answer": "Battery health is 87%." if op == "DONE" else None}
            if "data_json" in schema["properties"]:
                reply["data_json"] = self.data_json if op == "DONE" else None
            return reply, {}


class EarlyDecisionTests(unittest.TestCase):
    SCALE = .2
    ITEMS = [{"kind": "REPORT", "app": "Settings", "what": "battery health", "payload": "", "act": "none",
              "count": "", "condition": "", "quote": "battery health"}]

    def first_action(self, early):
        row = Snapshot([Element("1", "Battery", "Button", (.1, .3, .8, .05))], "Battery", 400, 800,
                       "synthetic_fixture", bundle_id="com.apple.Preferences")
        phone = Still([row, row, row])
        model = SlowModel([("TAP", "Battery")], self.SCALE, self.ITEMS)
        events = []
        agent = FrontierAgent(phone, model, apps={"com.apple.Preferences": "Settings"}, emit=events.append,
                              screenshots=False, settle_seconds=0, early_decision=early, skills=())
        started = time.monotonic()
        agent.run("What's my battery health?")
        return phone.first_action_at - started, events, agent

    def test_time_to_first_action_drops_by_the_contract_call(self):
        serial, _, _ = self.first_action(False)
        early, events, agent = self.first_action(True)
        self.assertGreaterEqual(serial, 5.0 * self.SCALE)
        self.assertLessEqual(early, bound(2.8 * self.SCALE))
        self.assertGreaterEqual(serial - early, 2.0 * self.SCALE)
        kinds = [e["event"] for e in events]
        self.assertLess(kinds.index("frontier_contract"), kinds.index("frontier_action"))


class SchemaInDoneTests(unittest.TestCase):
    SCHEMA = {"type": "object", "properties": {"battery": {"type": "string"}}, "required": ["battery"],
              "additionalProperties": False}

    def run_with(self, data_json):
        row = Snapshot([Element("1", "Maximum Capacity", "StaticText", (.1, .3, .8, .05), value="87%")], "", 400, 800,
                       "synthetic_fixture", bundle_id="com.apple.Preferences")
        model = SlowModel([("DONE", None)] * 3, 0, data_json=data_json)
        agent = FrontierAgent(Still([row] * 4), model, apps={"com.apple.Preferences": "Settings"}, screenshots=False,
                              settle_seconds=0, skills=(), output_schema=self.SCHEMA)
        outcome = agent.run("What's my battery health?")
        before = list(model.kinds)
        summary = smart_result.summarize(outcome, agent, None, request="What's my battery health?",
                                          output_schema=self.SCHEMA)
        return summary, [k for k in model.kinds[len(before):] if k == "other"], before

    def test_a_fitting_answer_needs_no_structuring_call(self):
        summary, structuring, kinds = self.run_with('{"battery": "87%"}')
        self.assertEqual(structuring, [])
        self.assertEqual((summary["data"], summary["data_status"]), ({"battery": "87%"}, "validated"))
        self.assertEqual(kinds.count("other"), 0)

    def test_an_answer_that_does_not_fit_is_structured_as_before(self):
        summary, structuring, _ = self.run_with('{"battery": 87}')
        self.assertEqual(structuring, ["other"])
        self.assertEqual(summary["data"], {"battery": "87%"})


class ReadListCapTests(unittest.TestCase):
    def test_a_list_of_500_rows_stops_within_eight_seconds_and_sixty_rows(self):
        clock = [1000.0]

        class Long(Still):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                super().execute(operation, target, snapshot, text, timeout)
                clock[0] += 1.2

        pages = [Snapshot([Element(f"{p}-{i}", f"Place {p * 12 + i}", "Cell", (0, .1 + .07 * i, 1, .06))
                           for i in range(12)], "", 400, 800, "synthetic_fixture", bundle_id="com.apple.Maps")
                 for p in range(42)]
        phone = Long(pages, clock=lambda: clock[0])
        agent = FrontierAgent(phone, None, screenshots=False, skills=())
        with patch("time.monotonic", lambda: clock[0]), \
                patch("time.sleep", lambda s: clock.__setitem__(0, clock[0] + s)):
            started = clock[0]
            rows = agent._read_list(pages[0])
            spent = clock[0] - started
        self.assertLessEqual(spent, frontier.READ_LIST_SECONDS)
        self.assertLessEqual(len(rows), frontier.READ_LIST_ROWS)
        self.assertIn("before the end of the list", agent.run_state.macro_feedback)

    def test_it_stops_at_the_row_it_was_asked_to_find(self):
        pages = [Snapshot([Element(f"{p}-{i}", f"Cafe {p * 5 + i}", "Cell", (0, .1 + .1 * i, 1, .08))
                           for i in range(5)],
                          "", 400, 800, "synthetic_fixture", bundle_id="com.apple.Maps") for p in range(10)]
        phone = Still(pages)
        agent = FrontierAgent(phone, None, screenshots=False, skills=())
        with patch("time.sleep"):
            rows = agent._read_list(pages[0], "Cafe 7")
        self.assertEqual(len(phone.actions), 1)
        self.assertIn("Cafe 7", rows)
        self.assertIn("first row containing", agent.run_state.macro_feedback)


class CalculatorTests(unittest.TestCase):
    SCREENS = fixture_screens("calculator_keypad")

    def test_the_keys_are_listed_and_eight_is_tappable(self):
        keypad = self.SCREENS["keypad"]
        aliases, lines = frontier.screen_rows(keypad)
        listed = {element.label for element in aliases.values()}
        self.assertTrue({"8", "Add", "Equals", "All Clear", "Point"} <= listed)
        self.assertNotIn(frontier.KEYBOARD_ROW, lines)
        phone, events = Still([keypad] * 6), []
        script = SlowModel([("TAP", "8"), ("DONE", None)], 0)
        FrontierAgent(phone, script, apps={"com.apple.calculator": "Calculator"}, emit=events.append,
                      screenshots=False, settle_seconds=0, skills=()).run("In Calculator, press 8.")
        self.assertEqual(phone.actions[0], ("TAP", "8", None))
        self.assertFalse([e for e in events if e["event"] == "frontier_refused"
                          and "listed element" in str(e.get("detail"))])

    def test_a_software_keyboard_still_hides_its_keys(self):
        keys = [Element(str(i), k, "Key", (.1 * i, .8, .1, .05)) for i, k in enumerate("1234567890")]
        typing = Snapshot(keys, "", 400, 800, "synthetic_fixture", keyboard="visible")
        aliases, lines = frontier.screen_rows(typing)
        self.assertEqual(aliases, {})
        self.assertIn(frontier.KEYBOARD_ROW, lines)

    def test_tap_xy_presses_only_an_unlisted_app_key_and_refuses_anything_else(self):
        hidden = Element("9", "8", "Key", (.5, .6, .2, .08))
        snapshot = Snapshot([hidden], "8", 400, 800, "synthetic_fixture", bundle_id="com.apple.calculator")
        agent = FrontierAgent(None, None, skills=())
        point, why = agent._tap_xy("press 8", Action("TAP_XY", target="60,64", text="8"), {}, snapshot)
        self.assertIsNone(why)
        point, why = agent._tap_xy("press 8", Action("TAP_XY", target="60,64", text="8"), {"e1": hidden}, snapshot)
        self.assertIn("is a listed element (e1)", why)
        get = Element("9", "Get", "Button", (.7, .9, .2, .04))
        snapshot = Snapshot([get], "Get", 400, 800, "synthetic_fixture", bundle_id="com.apple.AppStore")
        point, why = agent._tap_xy("Install Duolingo", Action("TAP_XY", target="80,92", text="Get"), {}, snapshot)
        self.assertIsNone(point)
        self.assertIn("TAP it with target_label 'Get'", why)


class SettlePollTests(unittest.TestCase):
    def test_settle_reads_start_at_least_250_ms_apart(self):
        clock = [100.0]
        starts = []
        first = Snapshot([Element("1", "A", "Button", (.1, .1, .2, .05))], "", 400, 800, "synthetic_fixture",
                         bundle_id="com.apple.calculator")
        changing = iter([Snapshot([Element("1", label, "Button", (.1, .1, .2, .05))], "", 400, 800,
                                  "synthetic_fixture", bundle_id="com.apple.calculator") for label in "BCDDDDDDDDDD"])
        driver = WDA("http://localhost:8100", "test")

        def observe(timeout=10):
            starts.append(clock[0])
            clock[0] += .08
            return next(changing)
        driver.observe = observe
        with patch("mobile_agent.drivers.time.monotonic", lambda: clock[0]), \
                patch("mobile_agent.drivers.time.sleep", lambda s: clock.__setitem__(0, clock[0] + s)), \
                patch("mobile_agent.transport.time.monotonic", lambda: clock[0]):
            driver.wait_for_change(first, timeout=5, wait_seconds=.6)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertTrue(gaps)
        self.assertGreaterEqual(min(gaps), wda_observe.WDA_SETTLE_POLL_SECONDS - 1e-9)
        self.assertGreaterEqual(wda_observe.WDA_SETTLE_POLL_SECONDS, .25)

    def test_the_last_settle_starts_the_next_call_early_and_the_timing_report_has_it(self):
        screens = [Snapshot([Element("1", f"Step {i}", "Button", (.1, .3, .8, .05))], "", 400, 800,
                            "synthetic_fixture", bundle_id="com.apple.Preferences") for i in range(6)]

        class Settling(Still):
            capabilities = Still.capabilities | {"settled_by", "settles_early", "settles_on_one_read",
                                                 "wait_for_change"}
            settles_early, settles_on_one_read = True, True
            settled_by = "agreeing_reads"

            def __init__(self, screens):
                super().__init__(screens)
                self.settles = []

            def wait_for_change(self, before, wait_seconds=.6, timeout=2, **options):
                time.sleep(.02)
                self.settles.append(options)
                return self.observe()
        phone = Settling(screens)
        model = SlowModel([("TAP", "Step 0"), ("DONE", None), ("DONE", None)], 0)
        result = FrontierAgent(phone, model, apps={"com.apple.Preferences": "Settings"}, screenshots=False,
                               settle_seconds=.6, skills=()).run("Open Step 0.")
        self.assertEqual(phone.settles[-1], {"single_read": True, "early": True})
        self.assertGreater(result["timing"]["settle"], 0)


class ScreenshotStallTests(unittest.TestCase):
    def test_two_screenshot_timeouts_turn_off_the_frame_clock_for_five_steps(self):
        driver = WDA("http://localhost:8100", "test")
        driver.frame_clock = object()

        class Stuck:
            def request(self, *args, **kwargs):
                raise TimeoutError("timed out")
        driver.preview_http = Stuck()
        for _ in range(2):
            with self.assertRaises(TimeoutError):
                driver.capture_preview()
        self.assertEqual(driver.ax_only_steps, wda_observe.WDA_AX_ONLY_STEPS)
        self.assertIsNone(driver._frame_clock())
        for left in (4, 3, 2, 1, 0):
            self.assertEqual(driver.next_step(), left)
        self.assertIsNotNone(driver._frame_clock())

    def test_a_stalled_stream_counts_once_per_stall(self):
        from collections import deque

        from mobile_agent.wda.frame_clock import STALL_SECONDS, Frame, FrameClock
        clock = FrameClock.__new__(FrameClock)
        clock.condition, clock.clock, clock.stalls, clock._stalled_after = threading.Condition(), lambda: 50.0, 0, None
        clock.records, clock.log_path, clock._log_lock = deque(maxlen=8), None, threading.Lock()
        clock.latest = Frame(7, 50.0 - STALL_SECONDS - 1, None, (), False)
        self.assertTrue(clock.check_stall())
        self.assertFalse(clock.check_stall())
        driver = WDA("http://localhost:8100", "test")
        driver.frame_clock = clock
        clock.stalls = 2
        driver.next_step()
        self.assertEqual(driver.ax_only_steps, wda_observe.WDA_AX_ONLY_STEPS)

    def test_the_model_gets_no_screenshot_request_while_ax_only(self):
        class Phone:
            ax_only_steps = 3

            def capture_preview(self, timeout=3):
                raise AssertionError("no screenshot while WDA's screenshots time out")
        self.assertIsNone(frontier.screenshot_part(Phone()))


class SwipeTargetTests(unittest.TestCase):
    SCREENS = fixture_screens("alarm_rows")

    def test_the_stroke_runs_through_the_target_row(self):
        alarms = self.SCREENS["alarms"]
        target = next(e for e in alarms.elements if e.label.startswith("6:15"))
        driver, bodies = WDA("http://localhost:8100", "test"), []
        driver.call = lambda method, path, body=None, timeout=10: bodies.append((path, body))
        driver.execute("SWIPE_LEFT", target, alarms)
        path, body = bodies[-1]
        moves = [a for a in body["actions"][0]["actions"] if a["type"] == "pointerMove"]
        centre = (target.rect[1] + target.rect[3] / 2) * alarms.height
        self.assertTrue(all(abs(m["y"] - centre) <= 1 for m in moves))
        self.assertTrue(all(target.rect[0] * 400 <= m["x"] <= (target.rect[0] + target.rect[2]) * 400 for m in moves))

    def test_an_untargeted_row_swipe_is_refused_and_a_targeted_one_runs(self):
        alarms = self.SCREENS["alarms"]
        phone, events = Still([alarms] * 6), []
        model = SlowModel([("SWIPE_LEFT", None), ("SWIPE_LEFT", "6:15 AM, Mobster test"), ("DONE", None),
                            ("DONE", None)], 0)
        FrontierAgent(phone, model, apps={"com.apple.mobiletimer": "Clock"}, emit=events.append, screenshots=False,
                      settle_seconds=0, skills=()).run("Delete my 6:15 AM Mobster test alarm.")
        self.assertIn("untargeted_swipe", [e.get("label") for e in events if e["event"] == "frontier_refused"])
        self.assertEqual(phone.actions[0], ("SWIPE_LEFT", "6:15 AM, Mobster test", None))


class FreshnessTests(unittest.TestCase):
    def test_a_stream_frame_older_than_the_tree_is_not_used(self):
        class Video:
            def __init__(self, at):
                self.at = at

            def latest(self):
                return (1, "image/jpeg", b"jpeg", 0, self.at, "wda_mjpeg")

        class Clock:
            def __init__(self, at):
                self.video = Video(at)

            def still_for(self):
                return 1.0
        phone = type("Phone", (), {})()
        phone.frame_clock = Clock(10.0)
        self.assertEqual(frontier.video_frame(phone, since=9.5), b"jpeg")
        self.assertIsNone(frontier.video_frame(phone, since=10.5))
        self.assertEqual(frontier.video_frame(phone), b"jpeg")


if __name__ == "__main__":
    unittest.main()

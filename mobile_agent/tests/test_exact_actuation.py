"""Bet 2, exact actuation: rich rows, exact text (paste, read-back, one repair), TAP_XY, WDA recovery,
glides and the early call. Offline: a fake WDA stands in for the simulator."""

import os
import unittest
from unittest.mock import patch

from mobile_agent import drivers
from mobile_agent.bench import sim_wda
from mobile_agent.drivers import WDA
from mobile_agent.frontier import (FrontierAgent, receipt_for, screen_rows, text_status)
from mobile_agent.state import Element, Snapshot, from_wda, truncated_field_value
from mobile_agent.tests.test_frontier import Driver, Script, screen
from mobile_agent.transport import TransportError

FIELD = "/XCUIElementTypeApplication/XCUIElementTypeWindow[1]/XCUIElementTypeTextField[1]"


def app(*children, bundle="com.example.app"):
    return (f'<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="App" label="App" x="0" y="0" '
            f'width="400" height="800" bundleId="{bundle}"><XCUIElementTypeWindow x="0" y="0" width="400" '
            f'height="800">{"".join(children)}</XCUIElementTypeWindow></XCUIElementTypeApplication>')


def node(role, x, y, w, h, children="", **attributes):
    extra = " ".join(f'{key}="{value}"' for key, value in attributes.items())
    return (f'<XCUIElementType{role} type="XCUIElementType{role}" x="{x}" y="{y}" width="{w}" height="{h}" {extra}>'
            f'{children}</XCUIElementType{role}>')


class RichRowTests(unittest.TestCase):
    CART = node("Other", 16, 700, 370, 54, name="view_cart_button")

    def test_an_identified_other_leaf_of_touch_size_is_a_target_only_in_rich_rows(self):
        xml = app(self.CART, node("Other", 10, 100, 20, 20, name="tiny_dot"),
                  node("Other", 10, 200, 300, 80, node("StaticText", 20, 210, 100, 20, label="Inside"), name="wrapper"),
                  node("Button", 10, 400, 100, 44, label="Back"))
        plain, rich = from_wda(xml), from_wda(xml, rich=True)
        self.assertNotIn("view_cart_button", [e.label for e in plain.elements])
        self.assertIn("view_cart_button", [e.label for e in rich.elements])
        # Too small, or not a leaf (its text is listed on its own): not a target.
        self.assertFalse({"tiny_dot", "wrapper"} & {e.label for e in rich.elements})
        self.assertEqual([e.label for e in plain.elements], [e.label for e in rich.elements if e.role != "Other"])

    def test_disabled_controls_are_listed_as_disabled_and_selected_ones_marked(self):
        xml = app(node("Button", 10, 100, 100, 44, label="Place Order", enabled="false"),
                  node("Button", 10, 200, 100, 44, label="Today", traits="Button, Selected"),
                  node("TextField", 10, 300, 300, 40, name="subject", value="Subject", placeholderValue="Subject"))
        plain, rich = from_wda(xml), from_wda(xml, rich=True)
        self.assertNotIn("Place Order", [e.label for e in plain.elements])
        by_label = {e.label: e for e in rich.elements}
        self.assertFalse(by_label["Place Order"].enabled)
        self.assertTrue(by_label["Today"].selected)
        self.assertEqual((by_label["subject"].placeholder, by_label["subject"].text), ("Subject", ""))
        self.assertFalse(any(e.selected or e.placeholder for e in plain.elements))
        lines = screen_rows(rich)[1]
        self.assertTrue(any('"Place Order"' in line and "disabled" in line for line in lines))
        self.assertTrue(any('"Today"' in line and " selected @" in line for line in lines))
        self.assertTrue(any('empty (placeholder "Subject")' in line for line in lines))

    def test_public_rows_do_not_change_for_jev(self):
        xml = app(node("Button", 10, 200, 100, 44, label="Today", traits="Button, Selected"))
        self.assertEqual(from_wda(xml).public(), from_wda(xml, rich=True).public())

    def test_a_cut_field_value_is_recognised(self):
        value = "a" * 800
        xml = app(node("TextView", 0, 100, 400, 400, name="body", value=value))
        shown = from_wda(xml).elements[0].value
        self.assertTrue(truncated_field_value(shown))
        self.assertFalse(truncated_field_value("short"))


class FakeWDA(WDA):
    """One text field on a 400x800 screen, and the WDA routes write_text uses."""

    def __init__(self, role="TextView", value="", placeholder="", multiline=None, paste_menu=True, drops=0,
                 simulator=True):
        super().__init__("http://localhost:8100", "s1")
        self.role, self.value, self.placeholder = role, value, placeholder
        self.multiline = role == "TextView" if multiline is None else multiline
        self.paste_menu, self.menu_up, self.pasteboard = paste_menu, False, None
        self.drops, self.keys, self.calls = drops, [], []
        self.submitted = False
        WDA._simulators[self.url] = simulator

    def element(self):
        shown = self.value or self.placeholder
        return Element("0", "body" if self.role == "TextView" else "", self.role, (.05, .2, .9, .3), True, FIELD,
                       shown[:200] + " … " + shown[-295:] if len(shown) > 500 else shown,
                       ("TAP", "TYPE", "TYPE_SUBMIT"), placeholder=self.placeholder)

    def observe(self, timeout=10):
        return Snapshot([self.element()], self.value, 400, 800, "wda", bundle_id="com.example.app", keyboard="parked")

    def _enter(self, text):
        if self.drops:
            text = text[::2] if len(text) > 1 else text
            self.drops -= 1
        if "\n" in text and not self.multiline:
            head, _, _ = text.partition("\n")
            self.value += head
            self.submitted = True
            return
        self.value += text

    def call(self, method, path, body=None, timeout=10):
        self.calls.append((method, path))
        if path == "/wda/setPasteboard":
            import base64
            self.pasteboard = base64.b64decode(body["content"]).decode()
            return None
        if path == "/actions":
            actions = body["actions"][0]["actions"]
            if any(a.get("type") == "pause" and a.get("duration", 0) >= 500 for a in actions):
                self.menu_up = self.paste_menu and not self.value
            elif self.menu_up:
                self.menu_up = False
                self.value += self.pasteboard if self.multiline else " ".join(self.pasteboard.split("\n"))
            return None
        if path == "/elements":
            if "MenuItem" in body["value"]:
                return [{"ELEMENT": "menu"}] if self.menu_up else []
            return [{"ELEMENT": "field"}]
        if path == "/element/active":
            return {"ELEMENT": "field"}
        if path == "/element/menu/rect":
            return {"x": 100, "y": 150, "width": 60, "height": 40}
        if path == "/element/field/rect":
            return {"x": 20, "y": 160, "width": 360, "height": 240}
        if path == "/element/field/attribute/value":
            return self.value or self.placeholder
        if path == "/element/field/attribute/placeholderValue":
            return self.placeholder or None
        if path == "/element/field/clear":
            self.value = ""
            return None
        if path == "/element/field/value":
            self._enter(body["value"])
            return None
        if path == "/wda/keys":
            for key in body["value"]:
                if key == "\n" and not self.multiline:
                    self.submitted = True
                else:
                    self._enter(key)
            return None
        if path == "/appium/settings":
            return None
        raise AssertionError(path)


class ExactTextTests(unittest.TestCase):
    def write(self, driver, text, **options):
        with patch.object(drivers, "WDA_PARKED_FOCUS_SECONDS", 0), patch("time.sleep"):
            return driver.write_text(driver.element(), driver.observe(), text, **options)

    def test_a_long_body_is_pasted_and_read_back_whole(self):
        driver = FakeWDA()
        body = "x" * 900 + "\nend"
        receipt = self.write(driver, body)
        self.assertEqual((receipt["method"], receipt["matches"], receipt["chars"]), ("paste", True, len(body)))
        self.assertEqual(driver.value, body)
        self.assertNotIn(("POST", "/element/field/value"), driver.calls)

    def test_line_breaks_for_a_field_not_known_to_take_them_are_pasted_never_typed(self):
        one_line = FakeWDA(role="TextField", multiline=False)
        receipt = self.write(one_line, "Weekly sync\nnotes")
        self.assertEqual((receipt["method"], receipt["flattened"], receipt["matches"]), ("paste", True, True))
        self.assertEqual(one_line.value, "Weekly sync notes")
        self.assertFalse(one_line.submitted)
        vertical = FakeWDA(role="TextField", multiline=True)  # SwiftUI's TextField(axis: .vertical)
        receipt = self.write(vertical, "Threat models\nSafety")
        self.assertEqual((receipt["flattened"], vertical.value), (False, "Threat models\nSafety"))

    def test_without_a_paste_menu_line_breaks_become_spaces_rather_than_a_return(self):
        driver = FakeWDA(role="TextField", multiline=False, paste_menu=False)
        with patch.object(drivers, "WDA_PASTE_MENU_SECONDS", 0):
            receipt = self.write(driver, "one\ntwo")
        self.assertEqual((receipt["method"], driver.value, driver.submitted), ("type", "one two", False))
        self.assertTrue(receipt["matches"])

    def test_a_read_back_that_differs_is_repaired_once(self):
        driver = FakeWDA(role="TextField", drops=1)
        receipt = self.write(driver, "Saturday Run")
        self.assertTrue(receipt["repaired"])
        self.assertTrue(receipt["matches"])
        self.assertEqual(driver.value, "Saturday Run")

    def test_a_second_miss_is_reported_not_retried_again(self):
        driver = FakeWDA(role="TextField", drops=5, paste_menu=False)
        with patch.object(drivers, "WDA_PASTE_MENU_SECONDS", 0):
            receipt = self.write(driver, "Saturday Run")
        self.assertEqual((receipt["repaired"], receipt["matches"]), (True, False))
        self.assertIn("DIFFERS", text_status(receipt))

    def test_empty_text_clears_the_field(self):
        driver = FakeWDA(role="TextField", value="Wrap up", placeholder="Label")
        receipt = self.write(driver, "")
        self.assertEqual((receipt["method"], receipt["matches"], driver.value), ("clear", True, ""))

    def test_an_append_keeps_the_earlier_text_and_adds_at_the_end(self):
        driver = FakeWDA(value="Groceries: eggs")
        receipt = self.write(driver, ", milk", append=True)
        self.assertEqual((receipt["expected"], receipt["matches"]), ("Groceries: eggs, milk", True))
        long_driver = FakeWDA(value="start ")
        receipt = self.write(long_driver, "y" * 300, append=True)
        self.assertEqual((receipt["method"], long_driver.value), ("paste", "start " + "y" * 300))

    def test_submit_presses_return_after_the_text_is_proven(self):
        driver = FakeWDA(role="TextField", multiline=False)
        self.write(driver, "pizza", submit=True)
        self.assertEqual((driver.value, driver.submitted), ("pizza", True))


class TuneAndSettleTests(unittest.TestCase):
    def tearDown(self):
        WDA._simulators.clear()

    def test_tune_sets_children_and_the_typing_rate_only_where_measured(self):
        driver = FakeWDA(simulator=True)
        with patch.object(drivers, "WDA_SIMULATOR_TYPING_FREQUENCY", 180), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOBSTER_TYPING_FREQUENCY", None)
            settings = driver.tune()
        self.assertEqual(settings, {"snapshotMaxChildren": drivers.WDA_MAX_CHILDREN, "maxTypingFrequency": 180})
        self.assertEqual(driver.paste_min_chars, 211)  # typing at 180 pays until ~2.5 s of it
        self.assertTrue(driver.rich_rows and driver.glide_everywhere)
        phone = FakeWDA(simulator=False)
        with patch.object(drivers, "WDA_SIMULATOR_TYPING_FREQUENCY", 180):
            os.environ.pop("MOBSTER_TYPING_FREQUENCY", None)
            self.assertNotIn("maxTypingFrequency", phone.tune())

    def test_the_plain_driver_keeps_its_source_path_and_measured_glide_apps(self):
        driver = WDA("http://localhost:8100", "s1")
        self.assertFalse(driver.rich_rows or driver.glide_everywhere)
        snapshot = Snapshot([], "", 400, 800, "wda", bundle_id="com.example.app")
        with patch.dict(os.environ, {"MOBSTER_GLIDE_BUNDLES": ""}):
            self.assertFalse(driver._glide_ok("SWIPE_UP", snapshot))
            self.assertFalse(driver.drag_next())
            driver.glide_everywhere = True
            self.assertTrue(driver._glide_ok("SWIPE_UP", snapshot))
            self.assertTrue(driver.drag_next())
            self.assertFalse(driver._glide_ok("SWIPE_UP", snapshot))  # once
            self.assertTrue(driver._glide_ok("SWIPE_UP", snapshot))

    def test_an_early_settle_returns_the_first_agreeing_changed_reads(self):
        before = screen(Element("1", "Inbox", "Button", (.1, .1, .2, .05)))
        after = screen(Element("1", "Message", "Button", (.1, .1, .2, .05)))
        driver = WDA("http://localhost:8100", "s1")
        reads = iter([after, after, after, after, after, after])
        driver.observe = lambda timeout=10: next(reads)
        result = driver.wait_for_change(before, timeout=2, wait_seconds=.6, early=True)
        self.assertIs(result, after)
        self.assertEqual(driver.settled_by, "agreeing_reads")


class RecoveryTests(unittest.TestCase):
    def test_recover_restarts_a_silent_runner_and_reattaches(self):
        driver = FakeWDA()
        driver.tuned_settings = {"snapshotMaxChildren": 300}
        alive = {"up": False}
        restarts = []
        driver._status_ok = lambda timeout=3: alive["up"]
        driver.restarter = lambda timeout: (restarts.append(timeout), alive.update(up=True))
        driver.new_session = lambda: "s2"
        driver.configure = lambda timeout=10: None
        driver.recover(timeout=10)
        self.assertEqual(len(restarts), 1)
        self.assertEqual(driver.prefix, "/session/s2")
        self.assertIn(("POST", "/appium/settings"), driver.calls)

    def test_without_a_restarter_it_waits_for_the_supervisor(self):
        driver = FakeWDA()
        answers = iter([False, False, True])
        driver._status_ok = lambda timeout=3: next(answers)
        driver.new_session, driver.configure = (lambda: "s3"), (lambda timeout=10: None)
        with patch("time.sleep"):
            driver.recover(timeout=30)
        self.assertEqual(driver.prefix, "/session/s3")


class SimulatorRunnerTests(unittest.TestCase):
    PS = ("101 /usr/bin/xcodebuild test-without-building -xctestrun a.xctestrun -destination id=AAA\n"
          "102 /usr/bin/xcodebuild test-without-building -xctestrun a.xctestrun -destination id=BBB\n"
          "103 /bin/zsh -c something id=AAA\n")

    def test_only_this_simulators_runner_is_found(self):
        self.assertEqual(sim_wda.runner_pids("AAA", self.PS), [101])

    def test_a_shared_process_group_is_never_signalled(self):
        with patch("os.getpgid", return_value=50), patch("os.killpg") as killpg, patch("os.kill") as kill:
            sim_wda._signal(101, 15)
        killpg.assert_not_called()
        kill.assert_called_once_with(101, 15)


class FrontierDriver(Driver):
    """The loop's test driver with write_text and a tap_point that says why."""

    def __init__(self, screens):
        super().__init__(screens)
        self.writes = []

    def write_text(self, target, snapshot, text, append=False, submit=False, timeout=30):
        self.writes.append((target.label, text, append, submit))
        self.index += 1
        return {"method": "type", "value": text, "expected": text, "matches": True, "repaired": False,
                "flattened": False, "chars": len(text)}

    def tap_point(self, x, y, snapshot, timeout=10, purpose="dismiss"):
        self.actions.append(("TAP_POINT", round(x, 3), round(y, 3), purpose))
        self.index += 1


def field(label="Label", value="Wrap up", role="TextField"):
    return Element("1", label, role, (.1, .3, .8, .05), value=value, editable=True, actions=("TAP", "TYPE", "TYPE_SUBMIT"),
                   locator="/f1")


class FrontierExactTests(unittest.TestCase):
    def run_agent(self, driver, steps, request="Set the alarm label to Saturday Run."):
        events = []
        result = FrontierAgent(driver, Script(steps), emit=events.append, screenshots=False,
                               settle_seconds=0).run(request)
        return result, events

    def test_set_text_empty_clears_through_write_text(self):
        driver = FrontierDriver([screen(field())] * 4)
        self.run_agent(driver, [("SET_TEXT", "Label", ""), ("DONE", None, None), ("DONE", None, None)],
                       "Clear the alarm label.")
        self.assertEqual(driver.writes, [("Label", "", False, False)])

    def test_set_text_empty_without_write_text_uses_clear_text(self):
        driver = Driver([screen(field())] * 4)
        with patch.dict(os.environ, {"MOBSTER_EXACT_TEXT": "off"}):
            self.run_agent(driver, [("SET_TEXT", "Label", ""), ("DONE", None, None), ("DONE", None, None)],
                           "Clear the alarm label.")
        self.assertEqual(driver.actions[0], ("CLEAR", "Label", None))

    def test_line_breaks_reach_write_text_for_any_field(self):
        driver = FrontierDriver([screen(field(label="", value="Slide body"))] * 4)
        steps = [("SET_TEXT", "Slide body", "Threat models\nSafety"), ("DONE", None, None), ("DONE", None, None)]
        self.run_agent(driver, steps, "Add a bullet about safety to the agenda.")
        self.assertEqual(driver.writes[0][1], "Threat models\nSafety")

    def test_typing_appends_and_a_search_replaces(self):
        search = Element("2", "Search", "SearchField", (.1, .05, .8, .05), editable=True,
                         actions=("TAP", "TYPE", "TYPE_SUBMIT"), locator="/s")
        driver = FrontierDriver([screen(field(role="TextView", label="Body"), search)] * 5)
        self.run_agent(driver, [("TYPE", "Body", "more"), ("TYPE_SUBMIT", "Search", "pizza"), ("DONE", None, None),
                                ("DONE", None, None)], "Add more to the note and search for pizza.")
        self.assertEqual(driver.writes, [("Body", "more", True, False), ("Search", "pizza", False, True)])

    def test_a_written_field_shows_its_status_instead_of_a_window(self):
        long = "note " * 300
        element = Element("1", "Body", "TextView", (.1, .2, .8, .5), editable=True, locator="/body",
                          value=long[:200] + " … " + long[-295:], actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        receipts = {"/body": {"value": long, "expected": long, "matches": True}}
        self.assertIs(receipt_for(receipts, element), receipts["/body"])
        line = screen_rows(screen(element), receipts)[1][0]
        self.assertIn("1,500 chars, matches what you wrote", line)
        self.assertLess(len(line), 250)
        other = Element("1", "Body", "TextView", (.1, .2, .8, .5), editable=True, locator="/body", value="changed",
                        actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        self.assertIsNone(receipt_for(receipts, other))  # the field no longer holds what was written

    def test_a_disabled_control_is_refused_with_a_reason(self):
        order = Element("1", "Place Order", "Button", (.1, .8, .8, .06), enabled=False)
        driver = FrontierDriver([screen(order)] * 3)
        _, events = self.run_agent(driver, [("TAP", "Place Order", None), ("DONE", None, None), ("DONE", None, None)],
                                   "Place my order.")
        self.assertEqual(driver.actions, [])
        self.assertIn("disabled", [e.get("label") for e in events if e["event"] == "frontier_refused"])

    def test_notes_are_not_added_twice(self):
        script = Script([("DONE", None, None), ("DONE", None, None)])
        complete = script.complete

        def with_notes(messages, schema, timeout=60):
            out, usage = complete(messages, schema, timeout)
            if "actions" in out:
                out["notes_add"] = ["Gate E13", "gate  e13", "Seat 4A"]
            return out, usage
        script.complete = with_notes
        result = FrontierAgent(Driver([screen(field())] * 3), script, screenshots=False, settle_seconds=0).run(
            "Tell me my gate.")
        self.assertEqual(result["notes"], ["Gate E13", "Seat 4A"])


class TapXYTests(unittest.TestCase):
    def run_xy(self, elements, target, name, request="Open my cart."):
        driver = FrontierDriver([screen(*elements)] * 4)
        script = Script([("DONE", None, None), ("DONE", None, None)])
        complete = script.complete
        first = {"done": False}

        def reply(messages, schema, timeout=60):
            if "actions" in schema["properties"] and not first["done"]:
                first["done"] = True
                script.usage["calls"] += 1
                return {"thought": "", "plan": None, "notes_add": [], "checklist_updates": [], "answer": None,
                        "actions": [{"op": "TAP_XY", "target": target, "text": name}]}, {}
            return complete(messages, schema, timeout)
        script.complete = reply
        events = []
        FrontierAgent(driver, script, emit=events.append, screenshots=False, settle_seconds=0).run(request)
        return driver, events

    def test_a_point_for_an_unlisted_control_is_tapped(self):
        driver, _ = self.run_xy([Element("1", "Menu", "Button", (.1, .1, .2, .05))], "52,91", "cart bar")
        self.assertEqual(driver.actions, [("TAP_POINT", .52, .91, "tap_xy")])

    def test_a_listed_control_is_tapped_by_its_id_instead(self):
        driver, events = self.run_xy([Element("1", "View Cart", "Button", (.1, .8, .8, .06))], "50,83", "View Cart")
        self.assertEqual(driver.actions, [])
        self.assertIn("tap_xy", [e.get("label") for e in events if e["event"] == "frontier_refused"])

    def test_never_near_a_commit_control(self):
        send = Element("1", "Send", "Button", (.8, .9, .15, .05))
        driver, _ = self.run_xy([send], "85,87", "arrow icon", request="Draft a reply to Sam.")
        self.assertEqual(driver.actions, [])
        driver, _ = self.run_xy([send], "40,50", "arrow icon", request="Draft a reply to Sam.")
        self.assertEqual(driver.actions, [("TAP_POINT", .4, .5, "tap_xy")])

    def test_it_can_be_turned_off(self):
        with patch.dict(os.environ, {"MOBSTER_TAP_XY": "off"}):
            driver, _ = self.run_xy([Element("1", "Menu", "Button", (.1, .1, .2, .05))], "52,91", "cart bar")
        self.assertEqual(driver.actions, [])


class FrontierRecoveryTests(unittest.TestCase):
    def test_a_refused_connection_is_recovered_and_the_turn_resumes(self):
        row = screen(Element("1", "Account", "Button", (.1, .9, .2, .05)))

        class Dying(Driver):
            recovered = 0

            def observe(self, timeout=10):
                if self.index == 1 and not self.recovered:
                    raise TransportError("HTTP ConnectionRefusedError; nothing sent; not retried")
                return super().observe(timeout)

            def recover(self):
                self.recovered += 1

        driver = Dying([row] * 4)
        events = []
        result = FrontierAgent(driver, Script([("TAP", "Account", None), ("DONE", None, None), ("DONE", None, None)]),
                               emit=events.append, screenshots=False, settle_seconds=0).run("Open my account.")
        self.assertEqual(driver.recovered, 1)
        self.assertEqual(result["status"], "completed")
        self.assertIn("frontier_wda_recovery", [e["event"] for e in events])

    def test_other_errors_are_not_recoveries(self):
        driver = Driver([screen(Element("1", "A", "Button", (.1, .1, .2, .05)))])
        driver.recover = lambda: self.fail("not a lost connection")
        agent = FrontierAgent(driver, Script([]), screenshots=False)
        self.assertFalse(agent._recover(TransportError("HTTP 500; request not retried")))


class ReadListDragTests(unittest.TestCase):
    def test_read_list_drags_by_default_after_f2(self):
        pages = [screen(Element("1", f"Row {i}", "StaticText", (.1, .3, .8, .05))) for i in (1, 2, 3)]

        class Recording(Driver):
            forced = 0

            def drag_next(self):
                self.forced += 1
                return True

        driver = Recording(pages + [pages[-1]] * 2)
        agent = FrontierAgent(driver, Script([]), screenshots=False)
        env = {k: v for k, v in os.environ.items() if k != "MOBSTER_GLIDE_READ_LIST"}
        with patch("mobile_agent.frontier.MACRO_PAUSE_SECONDS", 0), patch.dict(os.environ, env, clear=True):
            lines = agent._read_list(pages[0])
        self.assertEqual(lines, ["Row 1", "Row 2", "Row 3"])
        self.assertEqual(driver.forced, len(driver.actions))  # every swipe was a drag


class RecordingTests(unittest.TestCase):
    def test_a_write_is_one_trajectory_step_and_a_tap_xy_says_so(self):
        from mobile_agent.bench.iosworld import RecordingDriver

        class Shots:
            def take(self, name):
                return name
        inner = FrontierDriver([screen(field())])
        recording = RecordingDriver(inner, Shots())
        recording.write_text(field(), screen(field()), "Saturday Run")
        recording.write_text(field(), screen(field()), "")
        recording.tap_point(.5, .9, screen(field()), purpose="tap_xy")
        kinds = [step["actions"][0] for step in recording.steps]
        self.assertEqual(kinds[0]["type"], "type")
        self.assertTrue(kinds[0]["replace"])
        self.assertEqual(kinds[1]["type"], "clear_text")
        self.assertEqual(kinds[2]["purpose"], "tap_xy")


class GlideListTests(unittest.TestCase):
    def test_a_glide_that_shows_nothing_new_is_checked_once_with_a_drag(self):
        pages = [screen(Element("1", f"Row {i}", "StaticText", (.1, .3, .8, .05))) for i in (1, 2)]

        class Snapping(Driver):
            drags = 0

            def drag_next(self):
                self.drags += 1
                self.dragging = True
                return True

            def execute(self, operation, target, snapshot, text=None, timeout=10):
                self.actions.append((operation, getattr(self, "dragging", False)))
                if getattr(self, "dragging", False) or self.index:
                    self.index += 1  # the drag moves the list; the first glide snapped back
                self.dragging = False

        driver = Snapping(pages + [pages[-1]] * 3)
        agent = FrontierAgent(driver, Script([]), screenshots=False)
        with patch("mobile_agent.frontier.MACRO_PAUSE_SECONDS", 0), \
                patch.dict(os.environ, {"MOBSTER_GLIDE_READ_LIST": "on"}):
            lines = agent._read_list(pages[0])
        self.assertEqual(lines, ["Row 1", "Row 2"])
        self.assertEqual(driver.drags, 1)


if __name__ == "__main__":
    unittest.main()

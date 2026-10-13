"""The frontier step loop over Mobster's driver: guards, repeats, editing, dismissal. Offline."""

import json
import threading
import time
import unittest
import unittest.mock
from types import SimpleNamespace

from mobile_agent.frontier import (KEYBOARD_ROW, CallCancelled, FrontierAgent, OpenAIChat, cost_usd, normalize_reply,
                                   failure_feedback, guard, prompt_messages, prompt_text, screen_rows,
                                   typing_timeout)
from mobile_agent.state import Element, Snapshot


def screen(*elements, bundle="com.example.mail"):
    return Snapshot(list(elements), "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture",
                    bundle_id=bundle)


class Driver:
    can_type = True

    def __init__(self, screens):
        self.screens, self.index, self.actions = list(screens), 0, []

    def observe(self, timeout=10):
        return self.screens[min(self.index, len(self.screens) - 1)]

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, getattr(target, "label", target), text))
        self.index += 1

    def tap_point(self, x, y, snapshot, timeout=10):
        self.actions.append(("TAP_POINT", round(x, 3), round(y, 3)))
        self.index += 1

    def clear_text(self, target, timeout=10):
        self.actions.append(("CLEAR", target.label, None))


class Script:
    """A model that answers each step from a list of (operation, target label, text)."""

    def __init__(self, steps):
        self.steps, self.usage = list(steps), {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
                                               "calls": 0}

    def complete(self, messages, schema, timeout=60):
        if "items" in schema["properties"]:  # the task contract's call: none
            return {"items": []}, {}
        self.usage["calls"] += 1
        step = self.steps.pop(0)
        chunk = step if isinstance(step, list) else [step]
        prompt = prompt_text(messages)
        rows = prompt.split("Screen elements:\n", 1)[1].splitlines() if "Screen elements:\n" in prompt else []
        actions = []
        for index, (op, label, text) in enumerate(chunk):
            target = next((row.split()[0] for row in rows if label and f'"{label}"' in row), None)
            actions.append({"operation": op, "target": target if index == 0 else None,
                            "target_label": label if index else None, "text": text, "app": None})
        return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": list(getattr(self, "updates", {}).get(
                    self.usage["calls"], [])), "actions": actions,
                "answer": getattr(self, "answers", {}).get(self.usage["calls"], "done") if chunk[-1][0] == "DONE"
                else None}, {}


ARCHIVE = "Archive the QuickBite receipt in Mail."


class FrontierLoopTests(unittest.TestCase):
    def run_script(self, request, screens, steps):
        driver = Driver(screens)
        events = []
        result = FrontierAgent(driver, Script(steps), emit=events.append, screenshots=False,
                               settle_seconds=0).run(request)
        return driver, result, events

    def test_an_unrequested_act_is_refused_and_a_requested_one_runs(self):
        row = screen(Element("1", "Delete", "Button", (.1, .1, .2, .05)), Element("2", "Archive", "Button", (.4, .1, .2, .05)))
        driver, result, events = self.run_script(ARCHIVE, [row, row, row], [("TAP", "Delete", None),
                                                                           ("TAP", "Archive", None),
                                                                           ("DONE", None, None), ("DONE", None, None)])
        self.assertEqual(driver.actions, [("TAP", "Archive", None)])
        self.assertEqual(result["status"], "completed")
        self.assertIn("frontier_refused", [e["event"] for e in events])

    def test_a_sideways_swipe_strokes_through_its_target_row_and_names_it(self):
        # B7 (5 Oct): the driver used to swipe across the middle of the screen whatever the model aimed at, and
        # the owner's 5:55 AM alarm opened instead of the one targeted. The stroke now goes through the target
        # row wherever it is, so the step names it (this test pinned the old, untargeted stroke).
        from mobile_agent.engines import step_text
        label = "Buy oat milk, Incomplete"
        for rect in ((.05, .10, .9, .05), (.05, .47, .9, .06)):
            with self.subTest(rect=rect):
                row = screen(Element("1", label, "Cell", rect))
                swiped = screen(Element("1", label, "Cell", rect),
                                Element("2", "Delete", "Button", (.8, rect[1], .15, rect[3])))
                driver, _, events = self.run_script("Show the delete button on Buy oat milk in Reminders.",
                                                    [row, swiped, swiped, swiped],
                                                    [("SWIPE_LEFT", label, None),
                                                     ("DONE", None, None), ("DONE", None, None)])
                self.assertEqual(driver.actions[0], ("SWIPE_LEFT", label, None))
                action = next(e for e in events if e["event"] == "frontier_action")
                self.assertEqual(action["target_label"], label)
                self.assertEqual(step_text("SWIPE_LEFT", action["target_label"]), "Swiped left on Buy oat milk")

    def test_done_chained_after_an_edit_waits_for_one_look_at_the_result(self):
        row = screen(Element("1", "Archive", "Button", (.4, .1, .2, .05)))
        archived = screen(Element("1", "Archived", "StaticText", (.4, .1, .2, .05)))
        script = Script([[("TAP", "Archive", None), ("DONE", None, None)], ("DONE", None, None)])
        driver = Driver([row, archived, archived])
        events = []
        result = FrontierAgent(driver, script, emit=events.append, screenshots=False, settle_seconds=0).run(ARCHIVE)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(script.usage["calls"], 2)
        self.assertIn("verify_before_done", [e.get("reason") for e in events])

    def test_dismissing_an_unchanged_screen_tries_another_point_each_time(self):
        menu = screen(Element("1", "Pin Note", "Button", (.36, .08, .62, .05)))
        driver, _, _ = self.run_script("Update my reading list note.", [menu] * 5,
                                       [("DISMISS", None, None), ("DISMISS", None, None), ("DISMISS", None, None),
                                        ("DONE", None, None)])
        points = [a[1:] for a in driver.actions if a[0] == "TAP_POINT"]
        self.assertEqual(len(points), 3)
        self.assertEqual(len(set(points)), 3)

    def test_done_after_an_idle_dismiss_is_kept(self):
        note = screen(Element("1", "Currently reading: Pachinko", "StaticText", (.1, .3, .8, .05)))
        script = Script([("DONE", None, None), [("DISMISS", None, None), ("DONE", None, None)]])
        result = FrontierAgent(Driver([note] * 4), script, screenshots=False, settle_seconds=0).run(
            "Update my reading list note.")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(script.usage["calls"], 2)

    def test_the_first_done_is_checked_against_the_request_once(self):
        note = screen(Element("1", "Flight DL1358, Gate E13", "StaticText", (.1, .3, .8, .05)))
        events = []
        script = Script([("DONE", None, None), ("DONE", None, None)])
        result = FrontierAgent(Driver([note] * 3), script, emit=events.append, screenshots=False,
                               settle_seconds=0).run("Tell me my flight's gate and terminal.")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(script.usage["calls"], 2)
        prompts = [e["text"] for e in events if e["event"] == "frontier_prompt"]
        self.assertIn("item by item", prompts[1])

    def test_a_tap_that_focuses_a_field_does_not_stop_the_typing_after_it(self):
        body = Element("1", "Body", "TextView", (.1, .2, .8, .5), editable=True, actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        typed = Element("1", "Body", "TextView", (.1, .2, .8, .5), value="Q2 note", editable=True,
                        actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        driver, _, _ = self.run_script("Add a note about Q2 planning.", [screen(body), screen(body), screen(typed),
                                                                         screen(typed)],
                                       [[("TAP", "Body", None), ("TYPE", "Body", "Q2 note")], ("DONE", None, None),
                                        ("DONE", None, None)])
        self.assertEqual([a[0] for a in driver.actions], ["TAP", "TYPE"])

    def test_the_last_turn_only_reports_and_its_answer_is_kept(self):
        rows = [screen(Element("1", f"Row {i}", "Button", (.1, .1 * i, .8, .05))) for i in range(1, 6)]
        script = Script([("TAP", "Row 1", None), ("TAP", "Row 2", None), ("DONE", None, None)])
        schemas = []
        complete = script.complete
        script.complete = lambda messages, schema, timeout=60: (schemas.append(schema), complete(messages, schema))[1]
        result = FrontierAgent(Driver(rows), script, max_steps=3, screenshots=False, settle_seconds=0).run(
            "Tell me what Row 2 says.")
        self.assertEqual(result["status"], "max_steps")
        self.assertEqual(result["answer"], "done")
        self.assertEqual(schemas[-1]["properties"]["actions"]["items"]["properties"]["op"]["enum"],
                         ["DONE", "BLOCKED"])

    def test_the_same_label_on_a_changed_list_is_not_a_repeat(self):
        def requests(*names):
            return screen(*[e for i, name in enumerate(names) for e in (
                Element(f"n{i}", f"{name} requested $30", "StaticText", (.1, .2 + .1 * i, .5, .05)),
                Element(f"p{i}", "Pay", "Button", (.7, .2 + .1 * i, .2, .05)))])
        lists = [requests("Maya", "Kai", "Leo"), requests("Kai", "Leo"), requests("Leo"), requests()]
        driver, _, events = self.run_script("Pay my pending requests under $50.", lists + lists[-1:],
                                            [("TAP", "Pay", None)] * 3 + [("DONE", None, None)] * 2)
        self.assertEqual([a[:2] for a in driver.actions], [("TAP", "Pay")] * 3)
        self.assertNotIn("repeat", [e.get("label") for e in events if e["event"] == "frontier_refused"])

    def test_an_exact_repeat_is_refused(self):
        field = screen(Element("1", "Body", "TextView", (.1, .2, .8, .5), editable=True,
                               actions=("TAP", "TYPE", "TYPE_SUBMIT")))
        driver, _, _ = self.run_script("Add a note about Q2 planning to the doc.", [field] * 4,
                                       [("TYPE", "Body", "Q2 note"), ("TYPE", "Body", "Q2 note"), ("DONE", None, None)])
        self.assertEqual([a[0] for a in driver.actions], ["TYPE"])

    def test_set_text_clears_first_and_dismiss_taps_where_nothing_is(self):
        label = Element("1", "Label", "TextField", (.1, .3, .8, .05), value="Wrap up", editable=True,
                        actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        top = Element("2", "Menu", "Button", (0, 0, 1, .1))
        driver, _, _ = self.run_script("Set the alarm label to Saturday Run.", [screen(label, top)] * 4,
                                       [("SET_TEXT", "Label", "Saturday Run"), ("DISMISS", None, None),
                                        ("DONE", None, None)])
        self.assertEqual(driver.actions[0], ("CLEAR", "Label", None))
        self.assertEqual(driver.actions[1], ("TYPE", "Label", "Saturday Run"))
        self.assertEqual(driver.actions[2], ("TAP_POINT", .04, .5))  # the top strip is covered

    def test_set_text_replaces_in_one_driver_call_when_the_driver_can(self):
        label = Element("1", "Label", "TextField", (.1, .3, .8, .05), value="Wrap up", editable=True,
                        actions=("TAP", "TYPE", "TYPE_SUBMIT"))

        class Replacing(Driver):
            def replace_text(self, target, text, timeout=10):
                self.actions.append(("REPLACE", target.label, text))
                self.index += 1
                return target.role == "TextField"

        driver = Replacing([screen(label)] * 4)
        FrontierAgent(driver, Script([("SET_TEXT", "Label", "Saturday Run"), ("DONE", None, None),
                                      ("DONE", None, None)]), screenshots=False, settle_seconds=0).run(
            "Set the alarm label to Saturday Run.")
        self.assertEqual(driver.actions[0], ("REPLACE", "Label", "Saturday Run"))
        self.assertNotIn("CLEAR", [a[0] for a in driver.actions])


    def test_a_chunk_runs_without_asking_again_and_stops_at_a_missing_target(self):
        form = screen(Element("1", "Title", "TextField", (.1, .2, .8, .05), editable=True,
                              actions=("TAP", "TYPE", "TYPE_SUBMIT")),
                      Element("2", "Save", "Button", (.7, .05, .2, .05)))
        script = Script([[("TYPE", "Title", "Q2 plan"), ("TAP", "Save", None), ("TAP", "Share", None)],
                         ("DONE", None, None), ("DONE", None, None)])
        driver = Driver([form, form, form, form])
        result = FrontierAgent(driver, script, screenshots=False, settle_seconds=0).run("Add a doc titled Q2 plan.")
        self.assertEqual([a[:2] for a in driver.actions], [("TYPE", "Title"), ("TAP", "Save")])
        # The chain ran on one call ("Share" was not on screen); then DONE, checked once against the request.
        self.assertEqual(script.usage["calls"], 3)
        self.assertEqual(result["status"], "completed")

    def test_the_spend_cap_ends_the_run(self):
        row = screen(Element("1", "Next", "Button", (.1, .1, .2, .05)))
        script = Script([("DONE", None, None)] + [("TAP", "Next", None)] * 5)
        script.model = "gpt-5.5"
        script.usage["prompt_tokens"] = 2_000_000  # $10 already
        driver = Driver([row] * 6)
        result = FrontierAgent(driver, script, screenshots=False, settle_seconds=0,
                               max_cost_usd=1.0).run("Tap next and add it.")
        self.assertEqual(result["status"], "budget")
        self.assertEqual(script.usage["calls"], 1)  # one report-only call, no actions
        self.assertEqual(result["answer"], "done")
        self.assertEqual(driver.actions, [])

    def test_a_loop_over_the_same_control_is_refused(self):
        row = screen(Element("1", "Recent", "Button", (.1, .1, .2, .05)), Element("2", "Drag", "Button", (.5, .1, .2, .05)))
        steps = [[("TAP", "Recent", None), ("TAP", "Drag", None)]] * 3 + [("TAP", "Recent", None), ("DONE", None, None)]
        driver = Driver([row] * 20)
        FrontierAgent(driver, Script(steps), screenshots=False, settle_seconds=0).run("Create a doc and add it.")
        self.assertEqual(driver.actions.count(("TAP", "Recent", None)), 2)

    def test_line_breaks_are_kept_only_for_text_views(self):
        field = Element("1", "Title", "TextField", (.1, .2, .8, .05), editable=True, actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        body = Element("2", "Body", "TextView", (.1, .3, .8, .5), editable=True, actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        form = screen(field, body)
        driver = Driver([form] * 6)
        FrontierAgent(driver, Script([("TYPE", "Title", "Q2\nplan"), ("TYPE", "Body", "line one\nline two"),
                                      ("DONE", None, None)]), screenshots=False, settle_seconds=0).run("Add a note.")
        self.assertEqual(driver.actions[:2], [("TYPE", "Title", "Q2 plan"), ("TYPE", "Body", "line one\nline two")])


class FindTests(unittest.TestCase):
    def test_a_chain_finds_an_unlabelled_field_by_placeholder_and_a_label_by_part(self):
        field = Element("1", "", "SearchField", (.1, .1, .8, .05), value="Search restaurants", editable=True,
                        actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        save = Element("2", "Save Note", "Button", (.7, .9, .2, .05))
        snap = screen(field, save)
        self.assertIs(FrontierAgent._find(snap, "Search restaurants"), field)
        self.assertIs(FrontierAgent._find(snap, "Save"), save)
        self.assertIsNone(FrontierAgent._find(snap, "Delete"))


class KeypadChainTests(unittest.TestCase):
    def test_a_keypad_chain_presses_the_keys_it_names_even_as_the_tree_shifts(self):
        keys = [Element(str(i), k, "Button", (.1 * i, .8, .08, .05), locator=f"/App/Button[{i}]")
                for i, k in enumerate(["1", "2", "3", "5", "0", "."], 1)]
        before = screen(*keys)
        # After a press the amount label is inserted first, so every button's path shifts by one.
        shifted = [Element(str(i), e.label, "Button", e.rect, locator=f"/App/Button[{i + 1}]")
                   for i, e in enumerate(keys, 1)]
        amounts = ["$3", "$32", "$32.", "$32.5", "$32.50"]
        afters = [screen(Element("0", amount, "StaticText", (.1, .2, .8, .05)), *shifted) for amount in amounts]
        driver = Driver([before] + afters)  # this fake shows the next screen after each press
        chain = [("TAP", "3", None), ("TAP", "2", None), ("TAP", ".", None), ("TAP", "5", None), ("TAP", "0", None)]
        FrontierAgent(driver, Script([chain, ("DONE", None, None)]), screenshots=False,
                      settle_seconds=0).run("Send $32.50 to Maya on SplitPay.")
        self.assertEqual([a[1] for a in driver.actions], ["3", "2", ".", "5", "0"])


class SwitchAndWaitTests(unittest.TestCase):
    def test_a_row_wide_switch_is_tapped_at_its_knob(self):
        from mobile_agent.drivers import switch_point
        row = Element("1", "Outdoor Seating", "Switch", (.05, .5, .9, .05))
        x, _ = switch_point(row, 400)
        self.assertAlmostEqual(x, .95 - 30 / 400)
        button = Element("2", "Done", "Button", (.05, .5, .9, .05))
        self.assertEqual(switch_point(button, 400), button.center)

    def test_idle_waits_are_refused_after_two(self):
        still = screen(Element("1", "Search", "SearchField", (.1, .1, .8, .05), editable=True,
                               actions=("TAP", "TYPE", "TYPE_SUBMIT")))
        events = []
        script = Script([("WAIT", None, None)] * 4 + [("DONE", None, None)])
        FrontierAgent(Driver([still] * 3), script, emit=events.append, screenshots=False,
                      settle_seconds=0).run("Find the doc and star it.")
        self.assertEqual([e["label"] for e in events if e["event"] == "frontier_refused"], ["idle_wait", "idle_wait"])


class DeviceLossTests(unittest.TestCase):
    def test_a_device_that_stops_answering_still_gets_a_report(self):
        row = screen(Element("1", "Next", "Button", (.1, .1, .2, .05)))

        class Dying(Driver):
            def observe(self, timeout=10):
                if self.index:
                    raise TimeoutError("Operation deadline exceeded")
                return row
        script = Script([("TAP", "Next", None), ("DONE", None, None)])
        with unittest.mock.patch("mobile_agent.frontier.time.sleep"):
            result = FrontierAgent(Dying([row]), script, screenshots=False, settle_seconds=0).run("Open next.")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["answer"], "done")  # the report-only call answered from the notes


class SpeedPathTests(unittest.TestCase):
    """Reads the loop no longer takes (24 Sep: a source read of a long list costs 0.7-1.7 s on the
    simulator) and model calls a chain no longer stops for."""

    ENTRY = Element("1", "Search products", "Button", (.1, .1, .8, .05))
    FIELD = Element("2", "search_field", "SearchField", (.1, .1, .7, .05), editable=True,
                    actions=("TAP", "TYPE", "TYPE_SUBMIT"))

    def test_a_typed_action_after_a_tap_goes_to_the_one_field_the_tap_opened(self):
        search = screen(self.FIELD, Element("3", "Cancel", "Button", (.8, .1, .1, .05)))
        driver = Driver([screen(self.ENTRY), search, search])
        script = Script([[("TAP", "Search products", None), ("TYPE_SUBMIT", "Search products", "salmon")],
                         ("DONE", None, None), ("DONE", None, None)])
        FrontierAgent(driver, script, screenshots=False, settle_seconds=0).run("Search for salmon.")
        self.assertEqual(driver.actions, [("TAP", "Search products", None), ("TYPE_SUBMIT", "search_field", "salmon")])
        self.assertEqual(script.usage["calls"], 3)  # one chain, then DONE checked once against the request

    def test_a_submit_after_typing_goes_to_the_one_field_it_typed_into(self):
        # Typed into, the unlabelled field moved in the tree: its path no longer finds it.
        before = Element("2", "", "SearchField", (.1, .1, .7, .05), editable=True, locator="/a/SearchField",
                         actions=("TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT"))
        typed = Element("2", "", "SearchField", (.1, .1, .7, .05), value="running", editable=True,
                        locator="/a/b/SearchField", actions=("TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT"))
        driver = Driver([screen(before), screen(typed), screen(typed)])
        script = Script([[("SET_TEXT", "", "running"), ("SUBMIT", "", None)], ("DONE", None, None)])
        script.complete = self.by_id(script, "e1")
        FrontierAgent(driver, script, screenshots=False, settle_seconds=0).run("Search mail for running.")
        self.assertEqual([a[0] for a in driver.actions], ["CLEAR", "TYPE", "SUBMIT"])
        # A parked keyboard: the field takes no Return, so the chain stops for the model instead.
        parked = Element("2", "", "SearchField", (.1, .1, .7, .05), value="running", editable=True,
                         locator="/a/b/SearchField", actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        driver = Driver([screen(before), screen(parked), screen(parked)])
        script = Script([[("SET_TEXT", "", "running"), ("SUBMIT", "", None)], ("DONE", None, None)])
        script.complete = self.by_id(script, "e1")
        FrontierAgent(driver, script, screenshots=False, settle_seconds=0).run("Search mail for running.")
        self.assertEqual([a[0] for a in driver.actions], ["CLEAR", "TYPE"])

    @staticmethod
    def by_id(script, alias):
        """The script's answers, with the first action naming ``alias`` and later ones its id too."""
        complete = script.complete

        def answer(messages, schema, timeout=60):
            out, usage = complete(messages, schema, timeout)
            for act in out["actions"]:
                if act["operation"] not in ("DONE", "BLOCKED"):
                    act["target"], act["target_label"] = alias, None
            return out, usage
        return answer

    def test_with_two_fields_the_chain_stops_for_the_model(self):
        other = Element("3", "Zip code", "TextField", (.1, .3, .8, .05), editable=True,
                        actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        driver = Driver([screen(self.ENTRY), screen(self.FIELD, other)])
        events = []
        FrontierAgent(driver, Script([[("TAP", "Search products", None), ("TYPE", "Search products", "salmon")],
                                      ("DONE", None, None)]), emit=events.append, screenshots=False,
                      settle_seconds=0).run("Search for salmon.")
        self.assertEqual(driver.actions, [("TAP", "Search products", None)])
        self.assertIn("target_missing", [e.get("reason") for e in events if e["event"] == "frontier_chunk_stop"])

    def reads_before_acting(self, still_for):
        row = screen(Element("1", "Next", "Button", (.1, .1, .2, .05)))
        row.read_started = 1.0

        class Counting(Driver):
            frame_clock = SimpleNamespace(still_for=lambda: still_for)
            reads = 0

            def observe(self, timeout=10):
                self.reads += 1
                return super().observe(timeout)
        driver = Counting([row, row])
        result = FrontierAgent(driver, Script([("TAP", "Next", None), ("DONE", None, None)]), screenshots=False,
                               settle_seconds=0).run("Open next.")
        self.assertEqual(driver.actions, [("TAP", "Next", None)])
        return driver.reads, result["timing"]["reused"]

    def test_a_screen_still_since_its_read_is_not_read_again_before_acting(self):
        self.assertEqual(self.reads_before_acting(1e9), (2, 1))  # the first read and the settle
        self.assertEqual(self.reads_before_acting(None), (3, 0))  # no healthy stream: read again
        self.assertEqual(self.reads_before_acting(0.0), (3, 0))   # moved since the read began

    def test_an_unproven_screen_is_read_once_before_acting_when_it_agrees(self):
        # A blinking caret keeps the stream from proving a screen still; a read that agrees with the
        # settled one is enough (observe_ready paid two to four reads of up to 1.7 s each).
        row = screen(Element("1", "Next", "Button", (.1, .1, .2, .05)))
        other = screen(Element("1", "Next", "Button", (.1, .3, .2, .05)))
        for later, ready_calls in ((row, 2), (other, 3)):
            class Settling(Driver):
                readies = 0

                def observe(self, timeout=10):
                    return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]

                def observe_ready(self, timeout=10):
                    self.readies += 1
                    return self.observe(timeout)
            driver = Settling([row, later, later])
            FrontierAgent(driver, Script([("TAP", "Next", None), ("DONE", None, None), ("DONE", None, None)]),
                          screenshots=False, settle_seconds=0).run("Open next.")
            self.assertEqual(driver.actions, [("TAP", "Next", None)])
            # The first observation and the settle; and the refresh only when its read moved.
            self.assertEqual(driver.readies, ready_calls)

    def one_read_run(self, verify_screen=None, steps=None):
        a, b, c = (Element(str(i), k, "Button", (.1 * i, .1, .08, .05)) for i, k in enumerate("ABC", 1))
        screens = [screen(a, b, c), screen(b, c), screen(c), screen(Element("9", "Done", "Button", (0, 0, .1, .1)))]

        class OneRead(Driver):
            settles_on_one_read, settled_by = True, None
            # With a verify screen the stream is still since its read: no read before acting.
            frame_clock = SimpleNamespace(still_for=lambda: 1e9 if verify_screen is not None else None)

            def __init__(self, screens):
                super().__init__(screens)
                self.single, self.threads, self.acted_on = [], [], []

            def wait_for_change(self, before, timeout=2, wait_seconds=.6, single_read=False):
                self.single.append(single_read)
                self.settled_by = "one_read" if single_read else None
                return self.screens[min(self.index, len(self.screens) - 1)]

            def observe(self, timeout=10):
                self.threads.append(threading.current_thread().name)
                if verify_screen is not None and threading.current_thread().name == "frontier-verify":
                    return verify_screen
                return super().observe(timeout)

            def execute(self, operation, target, snapshot, text=None, timeout=10):
                self.acted_on.append(snapshot)
                super().execute(operation, target, snapshot, text=text, timeout=timeout)
        driver, events = OneRead(screens), []
        script = Script(steps or [[("TAP", "A", None), ("TAP", "B", None)], ("TAP", "C", None), ("DONE", None, None)])
        result = FrontierAgent(driver, script, emit=events.append, screenshots=False, settle_seconds=0).run("Tap A, B, C.")
        return driver, events, result

    def test_a_chunk_ends_on_one_read_proven_by_a_read_while_the_model_thinks(self):
        driver, events, result = self.one_read_run()
        self.assertEqual([a[1] for a in driver.actions], ["A", "B", "C"])
        self.assertEqual(driver.single, [False, True, True])  # mid-chunk settles stay proven
        self.assertEqual(driver.threads.count("frontier-verify"), 2)
        self.assertEqual((result["timing"]["verified"], result["timing"]["verify_changed"]), (2, 0))
        self.assertNotIn("frontier_verify", [e["event"] for e in events])

    def test_home_right_before_an_app_switch_is_not_settled(self):
        mail = screen(Element("1", "Inbox", "Button", (.1, .1, .2, .05)))
        shop = screen(Element("2", "Cart", "Button", (.1, .1, .2, .05)), bundle="com.example.shop")

        class Switching(Driver):
            settled = 0

            def wait_for_change(self, before, timeout=2, wait_seconds=.6, single_read=False):
                self.settled += 1
                return self.screens[min(self.index, len(self.screens) - 1)]
        driver = Switching([mail, mail, shop, shop])
        script = Script([[("HOME", None, None), ("LAUNCH_APP", None, None)], ("DONE", None, None),
                         ("DONE", None, None)])
        original = script.complete

        def complete(messages, schema, timeout=60):
            out, usage = original(messages, schema, timeout)
            for action in out["actions"]:
                if action["operation"] == "LAUNCH_APP":
                    action["app"] = "com.example.shop"
            return out, usage
        script.complete = complete
        FrontierAgent(driver, script, apps={"com.example.shop": "Shop"}, screenshots=False,
                      settle_seconds=0).run("Open my cart in Shop.")
        self.assertEqual([a[0] for a in driver.actions], ["HOME", "LAUNCH_APP"])
        self.assertEqual(driver.settled, 1)  # the app switch only

    def test_typing_then_return_reads_once_between(self):
        field = Element("1", "Search Mail", "SearchField", (.1, .1, .8, .05), value="CityRide", editable=True,
                        actions=("TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT"))

        class Settling(Driver):
            settled = 0

            def wait_for_change(self, before, timeout=2, wait_seconds=.6, single_read=False):
                self.settled += 1
                return self.screens[min(self.index, len(self.screens) - 1)]
        typed = Element("1", "Search Mail", "SearchField", (.1, .1, .8, .05), value="DL1358", editable=True,
                        actions=("TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT"))
        driver = Settling([screen(field)] + [screen(typed)] * 3)
        FrontierAgent(driver, Script([[("SET_TEXT", "Search Mail", "DL1358"), ("SUBMIT", "Search Mail", None)],
                                      ("DONE", None, None), ("DONE", None, None)]),
                      screenshots=False, settle_seconds=0).run("Find my DL1358 receipt in Mail.")
        self.assertEqual([a[0] for a in driver.actions], ["CLEAR", "TYPE", "SUBMIT"])
        self.assertEqual(driver.settled, 1)  # Return's effect only

    def test_app_switches_settle_on_agreeing_reads(self):
        driver, _, _ = self.one_read_run(steps=[("HOME", None, None), ("TAP", "C", None), ("DONE", None, None)])
        self.assertEqual([a[0] for a in driver.actions], ["HOME", "TAP"])
        self.assertEqual(driver.single, [False, True])

    def test_a_verify_read_that_differs_is_the_screen_acted_on(self):
        moved = screen(Element("7", "C", "Button", (.5, .5, .08, .05)))
        moved.read_started = 1.0
        driver, events, result = self.one_read_run(verify_screen=moved)
        self.assertEqual(result["timing"]["verify_changed"], 2)
        self.assertIn("frontier_verify", [e["event"] for e in events])
        self.assertEqual(driver.acted_on[-1].elements, moved.elements)  # C was re-found on the newer read


class HelperTests(unittest.TestCase):
    def test_long_field_values_show_their_end(self):
        body = "x" * 460 + " Note: Q2 planning"
        _, lines = screen_rows(screen(Element("1", "Body", "TextView", (0, 0, 1, 1), value=body, editable=True,
                                              actions=("TAP", "TYPE", "TYPE_SUBMIT"))))
        self.assertIn("Note: Q2 planning", lines[0])

    def test_a_long_description_mentioning_an_act_is_not_that_act(self):
        film = Element("1", "Moana, 74%, Jul 8, 2026, Teenage Moana answers the Ocean's call and voyages", "Cell",
                       (0, .3, 1, .1))
        self.assertIsNone(guard("Add Moana to my watchlist.", "TAP", film, None))
        self.assertIsNotNone(guard("Add Moana to my watchlist.", "TAP", Element("2", "Call", "Button", (0, 0, .1, .1)), None))

    def test_guard_and_cost(self):
        self.assertIsNone(guard(ARCHIVE, "TAP", Element("1", "Inbox", "Button", (0, 0, .1, .1)), None))
        self.assertIsNotNone(guard(ARCHIVE, "TAP", Element("1", "Send", "Button", (0, 0, .1, .1)), None))
        self.assertEqual(cost_usd("gpt-5.5", {"prompt_tokens": 1_000_000, "completion_tokens": 0}), 5.0)
        self.assertIsNone(cost_usd("unknown", {"prompt_tokens": 1}))



class FailureFeedbackTests(unittest.TestCase):
    TITLE = Element("1", "Title", "TextField", (.1, .1, .8, .05), editable=True, actions=("TAP", "TYPE", "TYPE_SUBMIT"))

    def test_newlines_for_a_one_line_field_are_explained(self):
        error = ValueError("TYPE requires bounded nonempty text without control characters")
        self.assertIn("one line", failure_feedback("SET_TEXT", self.TITLE, "Map\n\nTop colleagues", error))

    def test_a_typing_timeout_warns_against_retyping(self):
        error = RuntimeError("HTTP TimeoutError; request outcome unknown; not retried")
        self.assertIn("never retype", failure_feedback("TYPE", self.TITLE, "note", error))
        self.assertNotIn("retype", failure_feedback("TAP", self.TITLE, None, error))

    def test_other_errors_carry_their_message(self):
        self.assertIn("gone", failure_feedback("TAP", self.TITLE, None, ValueError("gone")))

    def test_long_text_gets_time_to_type(self):
        self.assertEqual(typing_timeout(None), 15)
        self.assertGreater(typing_timeout("x" * 1300), 120)


class MacroTests(unittest.TestCase):
    def test_read_list_scrolls_to_the_end_and_shows_every_row_once(self):
        def rows(*labels):
            return screen(*[Element(str(i), label, "Cell", (0, .2 + .1 * i, 1, .08)) for i, label in enumerate(labels)])
        pages = [rows("Receipt 1", "Receipt 2"), rows("Receipt 2", "Receipt 3"), rows("Receipt 3", "Receipt 4"),
                 rows("Receipt 3", "Receipt 4")]
        driver = Driver(pages)
        events = []
        FrontierAgent(driver, Script([("READ_LIST", None, None), ("DONE", None, None), ("DONE", None, None)]),
                      emit=events.append, screenshots=False, settle_seconds=0).run("Total all my receipts.")
        self.assertEqual([a[0] for a in driver.actions], ["SWIPE_UP"] * 3)
        listing = [e["text"] for e in events if e["event"] == "frontier_prompt"][1]
        self.assertIn("Receipt 1\nReceipt 2\nReceipt 3\nReceipt 4", listing)
        self.assertNotIn("List read by READ_LIST", [e["text"] for e in events if e["event"] == "frontier_prompt"][2])

    def test_scroll_to_stops_at_the_item_and_a_chained_tap_opens_it(self):
        top = screen(Element("1", "Alpha", "Cell", (0, .2, 1, .08)))
        lower = screen(Element("1", "Omega Trattoria", "Cell", (0, .5, 1, .08)))
        opened = screen(Element("1", "Book a table", "Button", (0, .5, 1, .08)))
        driver = Driver([top, lower, opened, opened])
        FrontierAgent(driver, Script([[("SCROLL_TO", None, "omega"), ("TAP", "Omega Trattoria", None)],
                                      ("DONE", None, None), ("DONE", None, None)]),
                      screenshots=False, settle_seconds=0).run("Open Omega Trattoria.")
        self.assertEqual(driver.actions, [("SWIPE_UP", None, None), ("TAP", "Omega Trattoria", None)])


class CostTests(unittest.TestCase):
    def test_the_cached_prefix_and_the_schema_stay_the_same_all_task_long(self):
        seen = []

        class Recording(Script):
            def complete(self, messages, schema, timeout=60):
                if "actions" in schema["properties"]:  # not the task contract's call
                    seen.append((messages, json.dumps(schema)))
                return super().complete(messages, schema, timeout)

        rows = [screen(Element("1", "Next", "Button", (.1, .1, .2, .05))),
                screen(Element("1", "Other", "Button", (.1, .5, .2, .05)), Element("2", "Add", "Button", (0, 0, .1, .1)))]
        FrontierAgent(Driver(rows), Recording([("TAP", "Next", None), ("DONE", None, None), ("DONE", None, None)]),
                      screenshots=False, settle_seconds=0).run("Tap next and add it.")
        prefixes = {(m[0]["content"], json.dumps(m[1]["content"][0])) for m, _ in seen}
        self.assertEqual(len(prefixes), 1)
        self.assertEqual(len({schema for _, schema in seen}), 1)
        first = seen[0][0][1]["content"]
        self.assertTrue(first[0]["cache"])
        self.assertNotIn("Turns used", first[0]["text"])
        self.assertIn("Turns used: 0 of 50", first[1]["text"])

    def test_the_request_marks_the_breakpoint_and_forces_one_call(self):
        messages = prompt_messages("Do it.", "- Mail: com.apple.mobilemail", "0 of 50", "Mail", "", [], [], None,
                                   ['e1 Button "Next" @1,1,1,1'],
                                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA", "detail": "low"}})
        body = OpenAIChat("gpt-5.6-terra", key="k").body(messages, {"type": "object"})
        self.assertEqual(body["input"][0]["role"], "developer")
        stable, turn, image = body["input"][1]["content"]
        self.assertEqual(stable, {"type": "input_text", "text": "Request: Do it.\n\nApps you may use:\n- Mail: com.apple.mobilemail",
                                  "prompt_cache_breakpoint": {"mode": "explicit"}})
        self.assertNotIn("prompt_cache_breakpoint", turn)
        self.assertEqual(image, {"type": "input_image", "image_url": "data:image/jpeg;base64,AA", "detail": "low"})
        self.assertEqual(body["prompt_cache_options"], {"mode": "explicit"})
        self.assertEqual(body["tool_choice"], {"type": "function", "name": "step"})
        older = OpenAIChat("gpt-5.5", key="k").body(messages, {"type": "object"})
        self.assertNotIn("prompt_cache_options", older)
        self.assertNotIn("prompt_cache_breakpoint", older["input"][1]["content"][0])

    def test_the_answer_is_the_function_calls_arguments(self):
        reply = {"output": [{"type": "reasoning"}, {"type": "function_call", "name": "step", "arguments": '{"a": 1}'}]}
        self.assertEqual(OpenAIChat.answer(reply), {"a": 1})
        with self.assertRaises(RuntimeError):
            OpenAIChat.answer({"output": [{"type": "message"}], "status": "incomplete"})

    def test_cache_reads_and_writes_are_priced_at_their_rates(self):
        usage = {"prompt_tokens": 3000, "cached_tokens": 1000, "cache_write_tokens": 500, "completion_tokens": 100}
        self.assertAlmostEqual(cost_usd("gpt-5.6-terra", usage), (1500 * 2 + 1000 * .2 + 500 * 2.5 + 100 * 12) / 1e6)
        self.assertAlmostEqual(cost_usd("gpt-5.6-luna", {"prompt_tokens": 1000}), 1000 * .2 / 1e6)

    def test_keyboard_keys_are_left_out(self):
        _, lines = screen_rows(screen(Element("1", "Cart", "Button", (.4, .9, .2, .06)),
                                      Element("2", "q", "Key", (0, .7, .1, .05)), Element("3", "w", "Key", (.1, .7, .1, .05))))
        self.assertEqual(lines, ['e1 Button "Cart" @40,90,20,6', KEYBOARD_ROW])
        _, lines = screen_rows(screen(Element("1", "Cart", "Button", (.4, .9, .2, .06))))
        self.assertEqual(lines, ['e1 Button "Cart" @40,90,20,6'])


class CompactReplyTests(unittest.TestCase):
    def test_a_compact_reply_reads_as_a_full_one(self):
        out = normalize_reply({"thought": "t", "plan": None, "actions": [
            {"op": "TAP", "target": "e7", "text": None},
            {"op": "TYPE_SUBMIT", "target": "Search Mail", "text": "invitation"},
            {"op": "LAUNCH_APP", "target": "com.example.notes", "text": None}]})
        self.assertEqual(out["actions"], [
            {"operation": "TAP", "target": "e7", "target_label": None, "text": None, "app": None},
            {"operation": "TYPE_SUBMIT", "target": None, "target_label": "Search Mail", "text": "invitation", "app": None},
            {"operation": "LAUNCH_APP", "target": None, "target_label": None, "text": None, "app": "com.example.notes"}])

    def test_a_full_reply_is_left_as_it_is(self):
        full = {"actions": [{"operation": "TAP", "target": "e1", "target_label": None, "text": None, "app": None}]}
        self.assertIs(normalize_reply(full), full)


# OpenAI's own reply to an account with no credit, pretty-printed as it comes: its code sits at character ~265.
QUOTA_BODY = json.dumps({"error": {
    "message": "You exceeded your current quota, please check your plan and billing details. For more information on "
               "this error, read the docs: https://platform.openai.com/docs/guides/error-codes/api-errors." + " " * 200
               + "(request id req_0123456789abcdef)",
    "type": "insufficient_quota", "param": None, "code": "insufficient_quota"}}, indent=4).encode()


class Reply:
    def __init__(self, status, body):
        self.status, self.body = status, body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self.body


class Connections:
    """Stands in for http.client.HTTPSConnection: each request gets the next scripted Reply."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), 0
        outer = self

        class Connection:
            def __init__(self, host, timeout=None):
                self.timeout = timeout

            def request(self, *args):
                outer.requests += 1

            def getresponse(self):
                return outer.replies.pop(0) if len(outer.replies) > 1 else outer.replies[0]

            def close(self):
                pass

        self.Connection = Connection


OK_REPLY = {"output": [{"type": "function_call", "arguments": json.dumps({"actions": []})}],
            "usage": {"input_tokens": 10, "output_tokens": 2}}


class ModelErrorTests(unittest.TestCase):
    """QA matrix A9, A11 and A12 (code-read risks 10 and 11): an empty account is read from a real-length error body,
    a server error is sent again, and Stop ends a wait to retry at once."""

    def post(self, connections, chat=None, sleep=None):
        chat = chat or OpenAIChat("gpt-5.6-sol", key="sk-test")
        with unittest.mock.patch("mobile_agent.frontier.http.client.HTTPSConnection", connections.Connection), \
                unittest.mock.patch("mobile_agent.frontier.time.sleep", sleep or (lambda seconds: None)):
            return chat.complete([{"role": "user", "content": "hi"}], {"type": "object", "properties": {}})

    def test_an_empty_account_is_known_from_a_long_body_and_is_not_retried(self):
        from mobile_agent import engines
        connections = Connections(Reply(429, QUOTA_BODY))
        with self.assertRaises(RuntimeError) as failed:
            self.post(connections)
        text = str(failed.exception)
        self.assertTrue(text.startswith("OpenAI HTTP 429: insufficient_quota: You exceeded your current quota"), text)
        self.assertEqual(connections.requests, 1)
        # The loop cuts the error to 300 characters for the result; the code survives the cut.
        self.assertTrue(engines.out_of_credit(text[:300]))
        self.assertEqual(engines.plain_error(text[:300]), engines.NO_CREDIT)
        self.assertEqual(engines.model_error(text[:300]), "no_credit")

    def test_the_old_cut_lost_the_code(self):
        from mobile_agent import engines
        self.assertGreater(QUOTA_BODY.find(b"insufficient_quota"), 300)  # why the cut needed the code first
        self.assertFalse(engines.out_of_credit("OpenAI HTTP 429: " + QUOTA_BODY[:300].decode()))

    def test_a_server_error_is_sent_again_and_the_task_goes_on(self):
        waits = []
        connections = Connections(Reply(503, {"error": {"message": "overloaded", "type": "server_error"}}),
                                  Reply(200, OK_REPLY))
        out, usage = self.post(connections, sleep=waits.append)
        self.assertEqual((out, connections.requests), ({"actions": []}, 2))
        self.assertEqual(waits, [1.0])

    def test_server_errors_that_keep_coming_end_the_call_after_two_resends(self):
        connections = Connections(Reply(502, b"<html>Bad gateway</html>"))
        with self.assertRaisesRegex(RuntimeError, r"^OpenAI HTTP 502: <html>Bad gateway</html>$"):
            self.post(connections)
        self.assertEqual(connections.requests, 3)

    def test_a_client_error_is_never_sent_again(self):
        connections = Connections(Reply(400, {"error": {"message": "Invalid schema", "type": "invalid_request_error"}}))
        with self.assertRaisesRegex(RuntimeError, "OpenAI HTTP 400: invalid_request_error: Invalid schema"):
            self.post(connections)
        self.assertEqual(connections.requests, 1)

    def test_stop_ends_a_rate_limit_wait_at_once(self):
        stop = threading.Event()
        threading.Timer(.3, stop.set).start()
        chat = OpenAIChat("gpt-5.6-sol", key="sk-test")
        chat.cancelled = stop.is_set
        connections = Connections(Reply(429, {"error": {"message": "Rate limit reached", "type": "requests",
                                                        "code": "rate_limit_exceeded"}}))
        started = time.monotonic()
        with unittest.mock.patch("mobile_agent.frontier.http.client.HTTPSConnection", connections.Connection):
            with self.assertRaises(CallCancelled):
                chat.complete([{"role": "user", "content": "hi"}], {"type": "object", "properties": {}})
        self.assertLess(time.monotonic() - started, 1.0)  # the backoff alone is 1 + 2 + 4 + 8 + 16 s
        self.assertLessEqual(connections.requests, 2)

    def test_without_stop_a_rate_limit_is_still_waited_out(self):
        waits = []
        connections = Connections(Reply(429, {"error": {"code": "rate_limit_exceeded"}}), Reply(200, OK_REPLY))
        chat = OpenAIChat("gpt-5.6-sol", key="sk-test")
        chat.cancelled = lambda: False
        out, _ = self.post(connections, chat=chat, sleep=waits.append)
        self.assertEqual(out, {"actions": []})
        self.assertAlmostEqual(sum(waits), 1.0)

    def test_the_loop_hands_its_stop_to_the_client_and_takes_it_back(self):
        stop = threading.Event()
        seen = []

        class Stopping(Script):
            cancelled = None

            def complete(self, messages, schema, timeout=60):
                if "items" in schema["properties"]:
                    return {"items": []}, {}
                seen.append(callable(self.cancelled))
                stop.set()  # the person pressed Stop while this call waited to retry
                raise CallCancelled("Stopped while a model call waited to retry")

        script = Stopping([])
        result = FrontierAgent(Driver([screen(Element("1", "Next", "Button", (.1, .1, .2, .05)))]), script,
                               screenshots=False, settle_seconds=0, cancelled=stop.is_set).run("Tap next.")
        self.assertEqual((result["status"], seen, script.cancelled), ("stopped", [True], None))

    def test_a_model_error_without_stop_still_ends_as_an_error(self):
        class Failing(Script):
            def complete(self, messages, schema, timeout=60):
                if "items" in schema["properties"]:
                    return {"items": []}, {}
                raise RuntimeError("OpenAI HTTP 500: server_error: oops")

        result = FrontierAgent(Driver([screen(Element("1", "Next", "Button", (.1, .1, .2, .05)))]), Failing([]),
                               screenshots=False, settle_seconds=0, cancelled=lambda: False).run("Tap next.")
        self.assertEqual(result["status"], "error")


class ActionCeilingTests(unittest.TestCase):
    """QA matrix code-read risk 14: a task that ends at the action ceiling says so, not that it used all its turns."""

    def test_the_ceiling_is_named_in_the_result_and_its_sentence(self):
        from mobile_agent import engines
        labels = "ABCDEFGH"
        screens = [screen(*(Element(str(i), label, "Button", (.1, .05 * i, .2, .04)) for i, label in enumerate(labels)),
                          Element("99", f"Step {n}", "StaticText", (.1, .9, .5, .04))) for n in range(12)]
        steps = [[("TAP", "A", None), ("TAP", "B", None)], [("TAP", "C", None), ("TAP", "D", None)],
                 [("TAP", "E", None), ("TAP", "F", None)]]
        with unittest.mock.patch("mobile_agent.frontier.MAX_ACTIONS", 4):
            result = FrontierAgent(Driver(screens), Script(steps), screenshots=False, settle_seconds=0).run("Tap them.")
        self.assertEqual((result["status"], result["actions"], result["reason"]), ("max_steps", 4, "4 actions used"))
        self.assertEqual(engines.reason_of("max_steps", "couldnt_finish", result["reason"], None),
                         "The task took 4 actions, the most one task can take, before finishing.")
        self.assertEqual(engines.reason_of("max_steps", "couldnt_finish", "50 turns used", None),
                         "The task used all 50 steps before finishing.")


class GeminiReasoningTests(unittest.TestCase):
    def body_for(self, reasoning):
        from mobile_agent.frontier import GeminiChat
        client = GeminiChat("gemini-3.8-flash", reasoning=reasoning, project="p")
        reply = {"candidates": [{"content": {"parts": [{"text": "{}"}]}}], "usageMetadata": {}}
        with unittest.mock.patch.object(GeminiChat, "_post", return_value=reply) as post, \
                unittest.mock.patch("mobile_agent.gemini.GCloudToken.get", return_value="t"):
            client.complete([{"role": "user", "content": "hi"}], {"type": "object"})
        return json.loads(post.call_args.args[1])["generationConfig"]

    def test_none_asks_for_the_least_thinking_not_the_default(self):
        self.assertEqual(self.body_for("none")["thinkingConfig"], {"thinkingLevel": "LOW"})

    def test_named_levels_pass_through(self):
        self.assertEqual(self.body_for("medium")["thinkingConfig"], {"thinkingLevel": "MEDIUM"})


if __name__ == "__main__":
    unittest.main()

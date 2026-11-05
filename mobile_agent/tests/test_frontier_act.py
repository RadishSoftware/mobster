import unittest
from unittest import mock

from mobile_agent import frontier
from mobile_agent.frontier import OPERATIONS, Action, FrontierAgent, normalize_reply, parse_reply, reads_back
from mobile_agent.frontier.safety import input_shows
from mobile_agent.state import FIELD_CUT, FIELD_HEAD, FIELD_TAIL, Element
from mobile_agent.tests import test_contract as C
from mobile_agent.tests.test_frontier import Driver, Script, screen


def run(driver, steps, request="Do it.", model=None, **kwargs):
    events = []
    result = FrontierAgent(driver, model or Script(steps), emit=events.append, screenshots=False, settle_seconds=0,
                           **kwargs).run(request)
    return result, events


def prompts(events):
    return [e["text"] for e in events if e["event"] == "frontier_prompt"]


def stops(events):
    return [e.get("reason") for e in events if e["event"] == "frontier_chunk_stop"]


def card(index, top):
    fields = [f"{f} {index}" for f in ("Departed", "Flight", "Seat", "Cost", "Date", "Gate")]
    children = [Element(f"{index}-{n}", text, "StaticText", (.05 + .1 * n, top + .02, .08, .03))
                for n, text in enumerate([f"C{index:02d}"] + fields)]
    return [Element(f"{index}", f"SFO, C{index:02d}, " + ", ".join(fields), "Button", (0, top, 1, .12)), *children]


def trips(*indices):
    return screen(*[e for slot, index in enumerate(indices) for e in card(index, .1 + .14 * slot)],
                  bundle="com.example.trips")


def listing(prompt):
    block = prompt.split(frontier.LISTING_HEAD, 1)[1].split("\n\n", 1)[0].splitlines()
    return [line for line in block if line not in (frontier.FENCE_OPEN, frontier.FENCE_CLOSE)]


READ, DONE = ("READ_LIST", None, None), ("DONE", None, None)


class ReadListTests(unittest.TestCase):
    def test_a_list_of_cards_is_read_to_its_end_one_row_a_card(self):
        driver = Driver([trips(0, 1, 2, 3), trips(3, 4, 5, 6), trips(6, 7, 8, 9), trips(9, 10, 11, 12),
                         trips(12, 13, 14, 15), trips(12, 13, 14, 15)])
        _, events = run(driver, [READ, DONE, DONE])
        rows = prompts(events)[1].split(frontier.LISTING_HEAD, 1)[1]
        for index in range(16):
            self.assertIn(f"SFO, C{index:02d}, Departed {index}", rows)
        self.assertNotIn("\nSeat 3\n", rows)
        self.assertNotIn("READ_LIST stopped", prompts(events)[1])
        self.assertEqual([a[0] for a in driver.actions], ["SWIPE_UP"] * 5)
        badge = trips(0)
        badge.elements.append(Element("badge", "Delayed 40 min", "StaticText", (.6, .12, .3, .03)))
        _, events = run(Driver([badge, badge]), [READ, DONE, DONE])
        self.assertIn("Delayed 40 min", prompts(events)[1])

    def test_a_second_read_list_goes_on_from_where_the_first_stopped(self):
        driver = Driver([trips(0, 1), trips(1, 2), trips(2, 3), trips(3, 4), trips(4, 5), trips(4, 5)])
        with mock.patch.object(frontier.act, "READ_LIST_ROWS", 3):
            _, events = run(driver, [READ, READ, DONE, DONE])
            first, second = (listing(p) for p in prompts(events)[1:3])
            self.assertEqual([line[:8] for line in first], ["SFO, C00", "SFO, C01", "SFO, C02"])
            self.assertEqual([line[:8] for line in second], ["SFO, C03", "SFO, C04", "SFO, C05"])
            self.assertEqual([a[0] for a in driver.actions], ["SWIPE_UP"] * 4)
            _, events = run(Driver([trips(0, 1, 2, 3, 4)] * 3), [READ, READ, DONE, DONE])
        self.assertEqual([line[:8] for line in listing(prompts(events)[2])], ["SFO, C03", "SFO, C04"])

    def test_a_read_list_after_another_action_or_at_a_list_s_end_reads_afresh(self):
        def rows(*indices):
            return screen(*[Element(str(i), f"Row {i}", "Cell", (0, .2 + .1 * slot, 1, .08))
                            for slot, i in enumerate(indices)])
        with mock.patch.object(frontier.act, "READ_LIST_ROWS", 2):
            _, events = run(Driver([rows(0, 1), rows(1, 2), rows(1, 2), rows(1, 2), rows(1, 2)]),
                            [READ, ("SWIPE_DOWN", None, None), READ, DONE, DONE])
        self.assertEqual(listing(prompts(events)[3]), ["Row 1", "Row 2"])
        _, events = run(Driver([trips(0, 1), trips(1, 2), trips(1, 2), trips(1, 2), trips(1, 2)]),
                        [READ, READ, DONE, DONE])
        self.assertIn("SFO, C01", prompts(events)[2].split(frontier.LISTING_HEAD, 1)[1].split("\n\n", 1)[0])


def keypad(shown):
    return screen(Element("d", shown, "StaticText", (.8, .2, .1, .07)), Element("k4", "4", "Key", (.04, .74, .21, .06)),
                  Element("k7", "7", "Key", (.04, .68, .21, .06)), bundle="com.apple.calculator")


class TargetTests(unittest.TestCase):
    def test_a_chained_tap_by_a_tile_s_name_taps_the_tile_that_holds_it(self):
        name = Element("n", "MobsterBench", "StaticText", (.05, .33, .2, .03))
        label = "MobsterBench, 4 items"
        for tile, tapped in ((Element("t", label, "Cell", (.02, .2, .3, .17)), label), (None, "MobsterBench"),
                             (Element("t", label, "Cell", (.6, .6, .3, .1)), "MobsterBench")):
            folder = screen(*((tile, name) if tile else (name,)), bundle="com.apple.DocumentsApp")
            driver = Driver([screen(Element("1", "On My iPhone", "Cell", (0, .2, 1, .06)),
                                    bundle="com.apple.DocumentsApp")] + [folder] * 4)
            run(driver, [[("TAP", "On My iPhone", None), ("TAP", "MobsterBench", None)], DONE, DONE])
            self.assertEqual(driver.actions[1], ("TAP", tapped, None))

    def test_a_repeated_key_is_pressed_not_the_display_that_reads_the_same(self):
        class Roles(Driver):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                super().execute(operation, target, snapshot, text, timeout)
                self.actions[-1] = (operation, target.label, target.role)
        driver = Roles([keypad("0"), keypad("4"), keypad("44"), keypad("447"), keypad("447")])
        run(driver, [[("TAP", "4", None), ("TAP", "4", None), ("TAP", "7", None)], ("DONE", "447", None),
                     ("DONE", "447", None)], "Type 447 in Calculator.")
        self.assertEqual(driver.actions[:3], [("TAP", "4", "Key"), ("TAP", "4", "Key"), ("TAP", "7", "Key")])

    def test_text_alone_is_still_found_and_a_named_role_still_wins(self):
        self.assertEqual(FrontierAgent._find(screen(Element("d", "4", "StaticText", (.8, .2, .1, .07))), "4").role,
                         "StaticText")
        self.assertEqual(FrontierAgent._find(keypad("4"), "4", "StaticText").role, "StaticText")
        self.assertEqual(FrontierAgent._find(keypad("4"), "4").role, "Key")

    def test_scroll_to_in_a_list_with_a_search_field_stops_early_and_says_to_search(self):
        for searchable, swipes in ((True, frontier.SCROLL_TO_SEARCHABLE_SWIPES), (False, frontier.MACRO_SWIPES)):
            field = [Element("s", "Search", "SearchField", (0, .1, 1, .05))] if searchable else []
            pages = [screen(*field, *[Element(f"r{i}{k}", f"Person {i}-{k}", "Cell", (0, .2 + k * .1, 1, .08))
                                      for k in range(5)], bundle="com.apple.MobileAddressBook") for i in range(12)]
            driver = Driver(pages)
            with mock.patch("time.sleep"):
                run(driver, [("SCROLL_TO", None, "Bench Tester"), DONE, DONE], "Find Bench Tester in Contacts.")
            self.assertEqual(len([a for a in driver.actions if a[0] == "SWIPE_UP"]), swipes)


class Writing(Driver):
    capabilities = Driver.capabilities | {"write_text"}

    def __init__(self, screens, matches=True):
        super().__init__(screens)
        self.writes, self.matches = [], matches

    def write_text(self, element, snapshot, text, append=False, submit=False, timeout=30):
        self.writes.append((element.label, text, append, submit))
        self.index += 1
        return {"method": "type", "value": text if self.matches else text + text, "expected": text,
                "matches": self.matches, "repaired": False, "flattened": False, "chars": len(text),
                "submitted": bool(submit and self.matches)}


class WriteTests(unittest.TestCase):
    def write(self, field, op, text, matches=True, bundle="com.example.app"):
        after = screen(Element("2", "Results", "StaticText", (0, .3, 1, .05)), bundle=bundle)
        driver = Writing([screen(field, bundle=bundle), after, after, after], matches=matches)
        _, events = run(driver, [[(op, field.label, text)], DONE, DONE], "Search the web for " + text)
        return driver, prompts(events)

    def test_a_submit_into_an_old_query_replaces_it_and_a_note_body_is_added_to(self):
        driver, _ = self.write(Element("1", "Address", "TextField", (0, .9, 1, .05), True, value="Grace Hopper"),
                               "TYPE_SUBMIT", "en.m.wikipedia.org/wiki/IOS_26", bundle="com.apple.mobilesafari")
        self.assertEqual(driver.writes[0][2], False)
        driver, _ = self.write(Element("1", "Body", "TextView", (0, .2, 1, .5), True, value="Shopping list"),
                               "TYPE", "eggs")
        self.assertEqual(driver.writes[0][2], True)

    def test_an_unmatched_submit_tells_the_model_return_was_not_pressed(self):
        _, texts = self.write(Element("1", "Search", "TextField", (0, .1, 1, .05), True), "TYPE_SUBMIT",
                              "Statue of Liberty", matches=False)
        self.assertIn("Return was not pressed", texts[1])


class ReadsDriver(Driver):
    def __init__(self, reads):
        super().__init__(reads)
        self.reads = list(reads)

    def observe(self, timeout=10):
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, getattr(target, "label", target), text))


def contacts(*rows, typed="Bench"):
    return screen(Element("s", "Search", "SearchField", (0, .1, 1, .05), True, value=typed), *rows,
                  bundle="com.apple.MobileAddressBook")


class SubmitSettleTests(unittest.TestCase):
    def chain(self, reads):
        driver = ReadsDriver(reads)
        with mock.patch("time.sleep"), mock.patch.object(frontier.act, "SUBMIT_SETTLE_SECONDS", 0.0):
            _, events = run(driver, [[("TYPE_SUBMIT", "Search", "Bench"), ("TAP", "Bench Tester", None)], DONE, DONE],
                            "In Contacts, open Bench Tester and report the company.")
        return driver, events

    def test_a_tap_on_settled_results_goes_ahead(self):
        settled = contacts(Element("r", "Bench Tester", "Cell", (0, .3, 1, .06)))
        driver, _ = self.chain([contacts(typed="")] + [settled] * 5)
        self.assertIn(("TAP", "Bench Tester", None), driver.actions)

    def test_results_still_moving_are_never_tapped(self):
        driver, events = self.chain([contacts(typed="")] + [
            contacts(Element("r", "Bench Tester", "Cell", (0, .3 + .05 * i, 1, .06)),
                     Element(f"x{i}", f"Suggestion {i}", "Cell", (0, .2, 1, .06))) for i in range(12)])
        self.assertNotIn(("TAP", "Bench Tester", None), driver.actions)
        self.assertIn("results_moving", stops(events))


SEARCH = Element("1", "Search MegaMart", "Button", (.0, .1, 1., .05))
FIELD = Element("2", "search_products_field", "SearchField", (.05, .1, .9, .05), editable=True,
                actions=("TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT"))
TYPE_HEADSET = [("TYPE_SUBMIT", "Search MegaMart", "PS5 headset"), DONE, DONE]


class FieldPromptTests(unittest.TestCase):
    def test_typing_into_a_search_button_taps_it_then_types_in_the_field_it_opened(self):
        results = screen(FIELD, Element("3", "Sony PULSE 3D Wireless Headset, $89.99", "Cell", (.05, .3, .9, .08)))
        driver = Driver([screen(SEARCH), screen(FIELD), results, results, results])
        result, events = run(driver, TYPE_HEADSET)
        self.assertEqual(driver.actions[:2], [("TAP", "Search MegaMart", None),
                                              ("TYPE_SUBMIT", "search_products_field", "PS5 headset")])
        self.assertEqual(result["status"], "completed")
        self.assertEqual([(e["operation"], e["as"]) for e in events if e["event"] == "frontier_repaired"],
                         [("TYPE_SUBMIT", "TAP")])
        driver = Driver([screen(Element("1", "Search", "Button", (.0, .9, .2, .05)))] + [screen(FIELD)] * 3)
        run(driver, [("TYPE_SUBMIT", "Search", "pizza"), DONE, DONE])
        self.assertEqual(driver.actions[:2], [("TAP", "Search", None),
                                              ("TYPE_SUBMIT", "search_products_field", "pizza")])

    def test_two_fields_after_the_tap_stop_the_chain_and_nothing_is_typed(self):
        other = Element("4", "Zip code", "TextField", (.05, .5, .9, .05), editable=True,
                        actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        driver = Driver([screen(SEARCH)] + [screen(FIELD, other)] * 3)
        _, events = run(driver, TYPE_HEADSET)
        self.assertEqual(driver.actions, [("TAP", "Search MegaMart", None)])
        self.assertIn("target_missing", stops(events))

    def test_a_tap_that_could_commit_or_a_button_that_is_no_field_prompt_is_never_put_in(self):
        typed = Element("5", "Note", "TextField", (.05, .5, .9, .05), editable=True, value="Call the bank",
                        actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        save = Element("6", "Save", "Button", (.4, .9, .2, .05))
        for elements, steps, tap in (((SEARCH, typed), TYPE_HEADSET, ("TAP", "Search MegaMart", None)),
                                     ((save,), [("TYPE", "Save", "x"), DONE, DONE], ("TAP", "Save", None))):
            driver = Driver([screen(*elements)] * 4)
            _, events = run(driver, steps)
            self.assertNotIn(tap, driver.actions)
            self.assertFalse([e for e in events if e["event"] == "frontier_repaired"])


class Replies:
    def __init__(self, replies):
        self.replies = list(replies)
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def complete(self, messages, schema, timeout=60):
        if "items" in schema["properties"]:
            return {"items": []}, {}
        self.usage["calls"] += 1
        return self.replies.pop(0), {}


class RepliesTests(unittest.TestCase):
    def test_a_reply_s_op_case_is_fixed_and_nothing_else_is_guessed(self):
        reply = {"thought": "open search", "plan": None, "notes_add": [], "checklist_updates": [],
                 "actions": [{"op": "TAP", "target": "e3", "text": None},
                             {"op": "TYPE_SUBMIT", "target": "Search", "text": "milk"}], "answer": None}
        out, actions, problem = parse_reply(reply, OPERATIONS)
        self.assertEqual((out, problem), (normalize_reply(reply), None))
        self.assertEqual(actions, [Action("TAP", target="e3"),
                                   Action("TYPE_SUBMIT", target_label="Search", text="milk")])
        out, actions, problem = parse_reply({"thought": "", "actions": [{"op": " tap ", "target": "e1", "text": None}]},
                                            OPERATIONS)
        self.assertEqual((problem, actions[0].op, out["notes_add"]), (None, "TAP", []))
        out, actions, problem = parse_reply({"actions": [{"op": "TAP", "target": "e1", "text": None},
                                                         {"op": "CLICK", "target": "Send", "text": None},
                                                         {"op": "TAP", "target": "Send", "text": None}]}, OPERATIONS)
        self.assertEqual([a.op for a in actions], ["TAP"])
        self.assertIn("'CLICK' is not one of the listed actions", problem)
        for raw in (["TAP e1"], "TAP e1", None, {"actions": "TAP e1"}, {"actions": [{"op": "TAP", "target": 7}]}):
            out, actions, problem = parse_reply(raw, OPERATIONS)
            self.assertEqual((out["actions"], actions), ([], []), raw)
            self.assertTrue(problem)

    def test_an_unlisted_action_runs_nothing_and_the_model_is_told(self):
        driver = Driver([screen(Element("1", "Archive", "Button", (.4, .1, .2, .05)))] * 4)
        model = Replies([{"thought": "", "actions": [{"op": "CLICK", "target": "e1", "text": None}]},
                         {"thought": "", "actions": [{"op": "done", "target": None, "text": None}],
                          "answer": "Archived."},
                         {"actions": [{"op": "DONE", "target": None, "text": None}], "answer": "Archived."}])
        result, events = run(driver, None, "Say hello.", model=model)
        self.assertEqual(driver.actions, [])
        self.assertIn("unknown_action", [e.get("label") for e in events if e["event"] == "frontier_refused"])
        self.assertEqual(result["status"], "completed")

    def test_two_replies_in_a_row_with_nothing_runnable_end_the_run_without_acting(self):
        driver = Driver([screen(Element("1", "Send", "Button", (.4, .9, .2, .05)))] * 4)
        model = Replies([{"actions": [{"op": "press", "target": "e1"}]}, "Send it", {"actions": []}])
        result, _ = run(driver, None, "Say hello.", model=model)
        self.assertEqual((driver.actions, result["status"], model.usage["calls"]), ([], "error", 2))


START = Element("2", "Start", "Button", (.7, .5, .2, .05))


def wheel(value):
    return Element("1", "Minutes", "PickerWheel", (.3, .2, .3, .3), value=value)


class WheelDriver(Driver):
    capabilities = Driver.capabilities | {"set_value"}

    def __init__(self, screens, stuck):
        super().__init__(screens)
        self.stuck = stuck

    def set_value(self, target, value, timeout=10):
        self.actions.append(("SET_VALUE", target.label, value))
        if not self.stuck:
            self.index += 1


class InputNotTakenTests(unittest.TestCase):
    def timer(self, screens, chunk, stuck):
        driver = WheelDriver(screens, stuck)
        _, events = run(driver, [chunk, DONE, DONE], "Set a timer for 12 minutes.")
        return driver.actions, stops(events)

    def test_a_wheel_that_did_not_move_stops_the_chunk_before_start(self):
        actions, reasons = self.timer([screen(wheel("25 min"), START)] * 3,
                                      [("SET_TEXT", "Minutes", "12"), ("TAP", "Start", None)], stuck=True)
        self.assertEqual(actions, [("SET_VALUE", "Minutes", "12")])
        self.assertIn("input_not_taken", reasons)

    def test_a_wheel_that_moved_or_already_shows_the_value_lets_start_run(self):
        chunk = [("SET_TEXT", "Minutes", "12"), ("TAP", "Start", None)]
        for screens, stuck in (([screen(wheel("25 min"), START)] + [screen(wheel("12 min"), START)] * 2, False),
                               ([screen(wheel("12 min"), START)] * 3, True)):
            actions, reasons = self.timer(screens, chunk, stuck)
            self.assertEqual(actions, [("SET_VALUE", "Minutes", "12"), ("TAP", "Start", None)])
            self.assertNotIn("input_not_taken", reasons)

    def test_a_field_shows_only_its_whole_text_and_an_empty_one_shows_nothing(self):
        field = Element("4", "Message", "TextField", (.1, .8, .8, .05), editable=True, value="Dinner at 7")
        self.assertTrue(input_shows(field, " dinner  at 7 "))
        self.assertFalse(input_shows(field, "Dinner at 8"))
        self.assertFalse(input_shows(field, "Dinner"))
        self.assertFalse(input_shows(wheel(""), "12"))
        self.assertFalse(input_shows(None, "12"))

    def test_a_second_input_still_runs_after_one_that_did_not_take(self):
        hours = Element("3", "Hours", "PickerWheel", (.0, .2, .3, .3), value="0 hours")
        actions, reasons = self.timer([screen(wheel("25 min"), hours, START)] * 3,
                                      [("SET_TEXT", "Minutes", "12"), ("SET_TEXT", "Hours", "1"),
                                       ("TAP", "Start", None)], stuck=True)
        self.assertEqual(actions, [("SET_VALUE", "Minutes", "12"), ("SET_VALUE", "Hours", "1")])
        self.assertIn("input_not_taken", reasons)


REPLY = "Hi Rachel, thanks for reaching out. I'm interested in the role."


def chat(value):
    return C.screen(Element("1", "chat_message_field", "TextField", (.1, .9, .6, .04), editable=True, value=value),
                    Element("2", "Send", "Button", (.8, .9, .1, .04)))


class SlowTyping(C.Driver):
    def execute(self, operation, target, snapshot, text=None, timeout=10):
        super().execute(operation, target, snapshot, text, timeout)
        if operation in ("TYPE", "TYPE_SUBMIT"):
            raise TimeoutError("Operation deadline exceeded")


class TimedOutTypingTests(unittest.TestCase):
    def typing(self, screens, steps):
        items = [C.item(app="LockedIn", what="reply to Rachel", act="send_message",
                        quote="Reply to Rachel in LockedIn")]
        driver, script = SlowTyping(screens), C.Script(steps, items)
        result, _ = run(driver, None, "Reply to Rachel in LockedIn that I'm interested.", model=script,
                        apps={"com.example.chat": "LockedIn"})
        return [a[0] for a in driver.actions], script.prompts, result

    def test_a_send_after_typing_that_timed_out_but_landed_goes_through(self):
        sent = C.screen(Element("3", REPLY, "StaticText", (.1, .5, .6, .04)),
                        Element("1", "chat_message_field", "TextField", (.1, .9, .6, .04), editable=True, value=""))
        actions, texts, result = self.typing([chat(""), chat(REPLY), sent, sent],
                                             [[("TYPE", "chat_message_field", REPLY)], [("TAP", "Send", None)], [DONE]])
        self.assertEqual(actions, ["TYPE", "TAP"])
        self.assertIn("reads back exactly the text you wrote", texts[1])
        self.assertEqual(result["contract"]["receipted"], 1)

    def test_a_field_holding_part_of_the_text_still_refuses_send(self):
        part = chat(REPLY[:20])
        actions, texts, _ = self.typing([chat(""), part, part, part],
                                        [[("TYPE", "chat_message_field", REPLY)], [("TAP", "Send", None)], [DONE],
                                         [DONE]])
        self.assertEqual(actions, ["TYPE"])
        self.assertIn("no text you typed", texts[2])

    def test_a_long_value_read_as_head_and_tail_matches_only_its_own_text(self):
        text = "a" * FIELD_HEAD + "b" * 100 + "c" * FIELD_TAIL
        shown = text[:FIELD_HEAD] + FIELD_CUT + text[-FIELD_TAIL:]
        self.assertTrue(reads_back(shown, text))
        self.assertFalse(reads_back(shown, "x" + text[1:]))
        self.assertTrue(reads_back(" Hi  Rachel ", "Hi Rachel"))
        self.assertFalse(reads_back("Hi", "Hi Rachel"))


if __name__ == "__main__":
    unittest.main()

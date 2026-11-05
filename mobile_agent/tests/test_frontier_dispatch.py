import unittest
from unittest.mock import patch

from mobile_agent import contract as K
from mobile_agent import drivers
from mobile_agent.frontier import FrontierAgent, failure_feedback, prompt_text, screen_goal
from mobile_agent.permissions import ASK_IMPORTANT, DONT_ASK, PermissionGate, policy
from mobile_agent.state import Element, Snapshot
from mobile_agent.tests import test_frontier as F
from mobile_agent.tests.fakes import Clock
from mobile_agent.wda.guards import DriverRejection

ARCHIVE = "Archive the QuickBite receipt in Mail."
DONE = ("DONE", None, None)


def run(request, elements, steps, *, bundle="com.example.app", **kwargs):
    asked = []
    if kwargs.pop("ask", False):
        kwargs["approve"] = lambda request_: asked.append(request_) or "denied"
    driver = F.Driver([F.screen(*elements, bundle=bundle)] * 10)
    script = F.Script(list(steps) + [DONE] * 3)
    script.model = kwargs.pop("model", None)
    script.usage["prompt_tokens"] += kwargs.pop("tokens", 0)
    result = FrontierAgent(driver, script, emit=lambda e: None, screenshots=False, settle_seconds=0,
                           **kwargs).run(request)
    return result, driver, asked


def asking(mode=ASK_IMPORTANT):
    return {"ask": True, "permissions": PermissionGate(mode, apps=policy.ALLOW_ALL)}


def failing_once(driver, error):
    real, sent = driver.execute, []

    def execute(operation, target, snapshot, text=None, timeout=10):
        sent.append((operation, target.label))
        if len(sent) == 1:
            raise error
        return real(operation, target, snapshot, text, timeout)
    driver.execute = execute
    return sent


def deciding_after(seconds):
    clock, archive = Clock(), F.screen(Element("1", "Archive", "Button", (.4, .1, .2, .05)))
    script = F.Script([("TAP", "Archive", None), DONE, DONE])
    complete = script.complete

    def slow(messages, schema, timeout=60):
        if "items" not in schema["properties"]:
            clock.now += seconds
        return complete(messages, schema, timeout)
    script.complete = slow
    driver = F.Driver([archive] * 6)
    with patch("time.monotonic", clock):
        result = FrontierAgent(driver, script, screenshots=False, settle_seconds=0, max_seconds=300).run(ARCHIVE)
    return result, driver.actions


ARCHIVE_ROW = Element("1", "Archive", "Button", (.4, .1, .2, .05))
NEXT = Element("1", "Next", "Button", (.1, .1, .2, .05))
SEARCH_FIELD = dict(role="SearchField", rect=(.1, .06, .7, .05), editable=True)


class UnknownOutcomeTests(unittest.TestCase):
    def test_an_outcome_unknown_action_is_never_sent_again_and_one_refused_before_dispatch_is(self):
        for error, sent_twice in ((DriverRejection("native_exception_outcome_unknown"), False),
                                  (DriverRejection("stale_revision"), True)):
            driver = F.Driver([F.screen(ARCHIVE_ROW)] * 8)
            sent = failing_once(driver, error)
            FrontierAgent(driver, F.Script([("TAP", "Archive", None), ("TAP", "Archive", None), DONE, DONE]),
                          screenshots=False, settle_seconds=0).run(ARCHIVE)
            self.assertEqual(len(sent), 2 if sent_twice else 1, error.code)

    def test_after_an_unknown_outcome_another_action_still_runs(self):
        driver, events = F.Driver([F.screen(ARCHIVE_ROW, Element("2", "Inbox", "Button", (.1, .1, .2, .05)))] * 8), []
        sent = failing_once(driver, TimeoutError("timed out"))
        FrontierAgent(driver, F.Script([("TAP", "Archive", None), ("TAP", "Archive", None), ("TAP", "Inbox", None),
                                        DONE, DONE]), emit=events.append, screenshots=False,
                      settle_seconds=0).run(ARCHIVE)
        self.assertEqual(sent, [("TAP", "Archive"), ("TAP", "Inbox")])
        self.assertIn("outcome_unknown", [e.get("label") for e in events if e["event"] == "frontier_refused"])

    def test_the_model_is_told_whether_a_failed_action_may_have_happened(self):
        for code in ("native_exception_outcome_unknown", "physical_tap_outcome_unknown"):
            self.assertIn("may have happened", failure_feedback("TAP", None, None, DriverRejection(code)))
        said = failure_feedback("TAP", None, None, DriverRejection("stale_revision"))
        self.assertIn("could not be performed", said)
        self.assertNotIn("may have happened", said)
        self.assertIn("timed out", failure_feedback("TAP", None, None, TimeoutError("timed out")))

    def test_a_declined_action_is_not_sent_again_and_the_rest_of_its_chunk_waits(self):
        class Declining(F.Driver):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                self.actions.append((operation, getattr(target, "label", target), text))
                raise DriverRejection("activation_declined", {"activation_route": "accessibility",
                                                              "dispatch_attempted": True})
        row = F.screen(Element("1", "Open Search", "Button", (.1, .1, .3, .05)),
                       Element("2", "Recent", "Button", (.5, .1, .3, .05)))
        driver, events = Declining([row] * 6), []
        FrontierAgent(driver, F.Script([[("TAP", "Open Search", None), ("TAP", "Recent", None)], DONE, DONE]),
                      emit=events.append, screenshots=False, settle_seconds=0).run("Open Search.")
        self.assertEqual(driver.actions, [("TAP", "Open Search", None)])
        failed = [e for e in events if e["event"] == "frontier_failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("no automatic retry", failed[0]["error"])
        self.assertIn("outcome is unknown: it may have happened",
                      [e["text"] for e in events if e["event"] == "frontier_prompt"][1])


class DeadlineTests(unittest.TestCase):
    def test_a_decision_back_after_the_time_limit_acts_on_nothing_and_ends_timed_out(self):
        result, actions = deciding_after(10_000)
        self.assertEqual(actions, [])
        self.assertEqual((result["status"], result["reason"]), ("timeout", "300 s used"))

    def test_a_decision_inside_the_time_limit_acts(self):
        self.assertEqual(deciding_after(20)[1], [("TAP", "Archive", None)])


class AskTests(unittest.TestCase):
    def test_a_block_and_a_like_are_put_to_the_user_in_ask_important(self):
        block = Element("0", "Block this Caller", "Cell", (0, .6, .995, .05))
        like = Element("0", "Like", "Button", (.8, .06, .1, .05))
        post = Element("1", "nasa: Saturn tonight", "StaticText", (.1, .3, .8, .05))
        for request, elements, bundle in (("Block this caller", [block], "com.apple.mobilephone"),
                                          ("Like nasa's latest post", [like, post], "com.burbn.instagram")):
            label = elements[0].label
            _, driver, asked = run(request, elements, [("TAP", label, None)], bundle=bundle, **asking())
            self.assertEqual(([r["label"] for r in asked], driver.actions), ([label], []))
        self.assertEqual((asked[0]["title"], asked[0]["act"]), ("Add this reaction?", "react"))

    def test_a_harmless_tap_and_a_song_row_that_starts_with_like_run_unasked(self):
        for request, element, bundle in (
                ("Open my recent calls", Element("0", "Recents", "Button", (0, .9, .2, .05)), "com.apple.mobilephone"),
                ("Play Like a Prayer", Element("0", "Like a Prayer, Madonna", "Cell", (0, .3, .995, .05)),
                 "com.apple.Music")):
            _, driver, asked = run(request, [element], [("TAP", element.label, None)], bundle=bundle, **asking())
            self.assertEqual((asked, driver.actions), ([], [("TAP", element.label, None)]))

    def test_full_access_likes_and_blocks_unasked(self):
        for label, bundle in (("Like", "com.burbn.instagram"), ("Block this Caller", "com.apple.mobilephone")):
            _, driver, asked = run(f"{label} it", [Element("0", label, "Button", (.8, .06, .1, .05))],
                                   [("TAP", label, None)], bundle=bundle, **asking(DONT_ASK))
            self.assertEqual((asked, driver.actions), ([], [("TAP", label, None)]))


class SpendCapTests(unittest.TestCase):
    def test_a_cap_on_an_unpriced_model_fails_closed_and_says_why(self):
        result, driver, _ = run("Tap next and add it.", [NEXT], [("TAP", "Next", None), ("TAP", "Next", None)],
                                model="some-unpriced-model", tokens=2_000_000, max_cost_usd=1.0)
        self.assertEqual((driver.actions, result["status"]), ([], "budget"))
        self.assertIn("no price for some-unpriced-model", result["reason"])
        self.assertIsNone(result["cost_usd"])

    def test_a_model_under_its_cap_still_acts(self):
        for model, tokens in (("some-unpriced-model", 1_000), ("claude-haiku-5-5", 2_000_000)):
            _, driver, _ = run("Tap next.", [NEXT], [("TAP", "Next", None)], model=model, tokens=tokens,
                               max_cost_usd=1.0)
            self.assertEqual(driver.actions, [("TAP", "Next", None)], model)


class TypedTextTests(unittest.TestCase):
    def search(self, value, op, actions=("TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT")):
        field = Element("0", "Search", value=value, actions=actions, **SEARCH_FIELD)
        return run("Search coffee", [field], [(op, "Search", "coffee")])[1].actions

    def test_text_a_field_already_holds_is_not_typed_again_and_new_text_is(self):
        self.assertEqual(self.search("coffee", "TYPE"), [])
        self.assertEqual(self.search("coffee", "TYPE_SUBMIT"), [("SUBMIT", "Search", None)])
        self.assertEqual(self.search("tea", "TYPE"), [("TYPE", "Search", "coffee")])

    def test_a_submit_that_replaces_the_field_goes_ahead(self):
        class Writing(F.Driver):
            capabilities = F.Driver.capabilities | {"write_text"}

            def write_text(self, element, snapshot, text, append=True, submit=False, timeout=10):
                self.actions.append(("WRITE", element.label, text, append, submit))
                self.index += 1
                return {"value": text, "matches": True, "submitted": submit}
        field = Element("0", "Search", value="coffee", actions=("TAP", "TYPE", "TYPE_SUBMIT", "SUBMIT"),
                        **SEARCH_FIELD)
        driver = Writing([F.screen(field)] * 10)
        FrontierAgent(driver, F.Script([("TYPE_SUBMIT", "Search", "coffee")] + [DONE] * 3), screenshots=False,
                      settle_seconds=0).run("Search coffee")
        self.assertEqual(driver.actions, [("WRITE", "Search", "coffee", False, True)])


APP = "com.example.food"


def food(title, *labels):
    elements = [Element("0", title, "NavigationBar", (0, .05, 1, .06))]
    elements += [Element(str(i + 1), label, "Button", (.1, .2 + .08 * i, .8, .05)) for i, label in enumerate(labels)]
    return Snapshot(elements, "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture", bundle_id=APP)


OATMEAL, BANANA, TODAY = food("Oatmeal", "Add Food", "Done"), food("Banana", "Add Food", "Done"), food(
    "Today", "Oatmeal", "Banana")
CARD_ROW = "Freedom Unlimited, VISA, $4,588.98, Current balance, You've set up automatic payments."
WALLET, CARD = food("Wallet", CARD_ROW), food("Card", "Back")


class Phone(drivers.Driver):
    PAGES = {"Add Food": TODAY, "Done": TODAY, "Oatmeal": OATMEAL, "Banana": BANANA, CARD_ROW: CARD, "Back": WALLET}

    def __init__(self, first):
        self.screen, self.taps = first, []

    def observe(self, timeout=10):
        return self.screen

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        label = getattr(target, "label", "")
        self.taps.append((operation, label, snapshot.elements[0].label))
        if label in self.PAGES:
            self.screen = self.PAGES[label]
        elif label.endswith("Pay"):
            self.screen = food("Requests", *[e.label for e in self.screen.elements[1:] if e.label != label])

    def close(self):
        pass


class Model:
    def __init__(self, *chunks):
        self.chunks, self.prompts = list(chunks), []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def complete(self, messages, schema, timeout=60, **kwargs):
        if "items" in schema["properties"]:
            return {"items": []}, {}
        self.usage["calls"] += 1
        self.prompts.append(prompt_text(messages))
        rows = self.prompts[-1].split("Screen elements:\n", 1)[1].splitlines()
        op, label = self.chunks.pop(0) if self.chunks else ("DONE", None)
        target = next((r.split()[0] for r in rows if label and f'"{label}"' in r), None)
        return {"thought": "", "plan": None, "notes_add": [], "checklist_updates": [],
                "actions": [{"op": op, "target": target, "text": None}], "answer": "ok" if op == "DONE" else None}, {}


def tap(label):
    return ("TAP", label)


class RepeatActTests(unittest.TestCase):
    def run_taps(self, first, *chunks, request="Log one serving of oatmeal."):
        phone, model, events = Phone(first), Model(*chunks, ("DONE", None)), []
        result = FrontierAgent(phone, model, apps={APP: "Food"}, emit=events.append, screenshots=False,
                               settle_seconds=0, skills=()).run(request)
        refused = [e for e in events if e.get("event") == "frontier_refused" and e.get("label") == "repeat_act"]
        return result, phone, model, len(refused)

    def adds(self, phone):
        return [t for t in phone.taps if t[1] == "Add Food"]

    def test_the_same_act_on_the_same_page_is_refused_with_a_look_first_note(self):
        result, phone, model, refused = self.run_taps(OATMEAL, tap("Add Food"), tap("Oatmeal"), tap("Add Food"))
        self.assertEqual((refused, self.adds(phone)), (1, [("TAP", "Add Food", "Oatmeal")]))
        self.assertIn("Refused: you already tapped 'Add Food'", model.prompts[3])
        self.assertEqual(result["status"], "completed")

    def test_a_repeat_is_refused_until_the_run_has_looked_at_a_screen_it_had_not_seen(self):
        _, phone, model, refused = self.run_taps(OATMEAL, tap("Add Food"), tap("Oatmeal"), tap("Add Food"),
                                                 tap("Add Food"))
        self.assertEqual((refused, len(self.adds(phone))), (2, 1))
        self.assertIn("Refused again", model.prompts[4])
        _, phone, _, refused = self.run_taps(OATMEAL, tap("Add Food"), tap("Oatmeal"), tap("Add Food"), tap("Done"),
                                             tap("Oatmeal"), tap("Add Food"))
        self.assertEqual((refused, len(self.adds(phone))), (2, 1))
        _, phone, _, refused = self.run_taps(OATMEAL, tap("Add Food"), tap("Oatmeal"), tap("Add Food"), tap("Done"),
                                             tap("Banana"), tap("Done"), tap("Oatmeal"), tap("Add Food"))
        self.assertEqual((refused, len(self.adds(phone))), (1, 2))

    def test_the_same_act_after_looking_elsewhere_or_on_another_page_is_not_refused(self):
        _, phone, _, refused = self.run_taps(OATMEAL, tap("Add Food"), tap("Banana"), tap("Done"), tap("Oatmeal"),
                                             tap("Add Food"))
        self.assertEqual((refused, len(self.adds(phone))), (0, 2))
        _, phone, _, refused = self.run_taps(OATMEAL, tap("Add Food"), tap("Banana"), tap("Add Food"))
        self.assertEqual((refused, [t[2] for t in self.adds(phone)]), (0, ["Oatmeal", "Banana"]))

    def test_rows_without_an_act_a_list_of_acts_and_closing_twice_are_not_refused(self):
        _, phone, _, refused = self.run_taps(WALLET, tap(CARD_ROW), tap("Back"), tap(CARD_ROW),
                                             request="What is my card balance?")
        self.assertEqual((refused, len([t for t in phone.taps if t[1] == CARD_ROW])), (0, 2))
        _, phone, _, refused = self.run_taps(food("Requests", "Maya, $20, Pay", "Kai, $15, Pay"),
                                             tap("Maya, $20, Pay"), tap("Kai, $15, Pay"),
                                             request="Pay every pending request.")
        self.assertEqual((refused, len(phone.taps)), (0, 2))
        _, phone, _, refused = self.run_taps(OATMEAL, tap("Done"), tap("Oatmeal"), tap("Done"))
        self.assertEqual((refused, len([t for t in phone.taps if t[1] == "Done"])), (0, 2))


SETTINGS = "com.apple.Preferences"
OPEN_CAMERA = [{"kind": "READ", "app": "Settings", "what": "Camera"},
               {"kind": "FORBID", "app": "Settings", "what": "any setting", "act": "other"}]


class ScreenFinishTests(unittest.TestCase):
    def finish(self, items, title="Camera"):
        class Navigator:
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}
            turns = 0

            def complete(self, messages, schema, timeout=60, reasoning=None):
                if "items" in schema["properties"]:
                    return {"items": items}, {}
                self.turns += 1
                act = {"op": "TAP", "target": "e2", "text": None} if self.turns == 1 else {
                    "op": "DONE", "target": None, "text": None}
                return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [], "actions": [act],
                        "answer": None if self.turns == 1 else "Opened Camera."}, {}
        root = F.screen(Element("1", "Settings", "NavigationBar", (0, .07, 1, .06)),
                        Element("2", "Camera", "Button", (0, .4, 1, .06)), bundle=SETTINGS)
        page = F.screen(Element("3", title, "NavigationBar", (0, .07, 1, .06)),
                        Element("4", "Formats", "Button", (0, .3, 1, .06)), bundle=SETTINGS)
        model, events = Navigator(), []
        result = FrontierAgent(F.Driver([root] + [page] * 4), model, apps={SETTINGS: "Settings"}, emit=events.append,
                               screenshots=False, settle_seconds=0).run(
            "In Settings, open Camera. Do not change any setting.")
        return result, model.turns, [e for e in events if e.get("event") == "frontier_screen_finish"]

    def test_a_screen_titled_with_the_goal_ends_the_run_with_no_done_turn(self):
        result, turns, finishes = self.finish(OPEN_CAMERA)
        self.assertEqual((result["status"], turns, len(finishes)), ("completed", 1, 1))

    def test_another_title_or_something_to_report_keeps_the_done_turn(self):
        report = {"kind": "REPORT", "app": "Settings", "what": "the camera format"}
        for items, title in ((OPEN_CAMERA, "Camera Modes"), (OPEN_CAMERA + [report], "Camera")):
            _, turns, finishes = self.finish(items, title)
            self.assertEqual(finishes, [], title)
            self.assertGreaterEqual(turns, 2)

    def test_the_contract_s_goal_is_only_a_navigation_contract_s_last_read(self):
        nav = K.Contract.from_items([{"kind": "READ", "app": "Settings", "what": "General"},
                                     {"kind": "READ", "app": "Settings", "what": "iPhone Storage"}], "x")
        self.assertEqual(screen_goal(nav), ["iphone storage"])
        self.assertIsNone(screen_goal(K.Contract.from_items([{"kind": "READ", "what": "contact Bench Tester"},
                                                             {"kind": "REPORT", "what": "city"}], "x")))


if __name__ == "__main__":
    unittest.main()


class SettledRectTests(unittest.TestCase):
    def test_the_box_is_the_elements_on_the_screen_the_step_ends_on_and_otherwise_none(self):
        from types import SimpleNamespace

        from mobile_agent.frontier.stages.after import settled_rect
        from mobile_agent.state import Element
        field = Element("e1", "Message", "TextField", (.1, .8, .8, .05), locator="id=msg")
        moved = Element("e9", "Message", "TextField", (.1, .6, .8, .05), locator="id=msg")
        other = Element("e2", "Send", "Button", (.8, .9, .1, .05), locator="id=send")
        after = SimpleNamespace(elements=[other, moved])
        self.assertEqual(settled_rect("TYPE", field, after), [.1, .6, .8, .05])
        self.assertEqual(settled_rect("TAP", field, after), [.1, .6, .8, .05])
        self.assertIsNone(settled_rect("TAP", other, SimpleNamespace(elements=[moved])))  # gone: the screen changed
        self.assertIsNone(settled_rect("TAP_XY", field, after))  # a coordinate tap names no element
        self.assertIsNone(settled_rect("TAP", None, after))
        self.assertIsNone(settled_rect("SWIPE_UP", field, after))
        nameless = Element("e3", "Row", "Cell", (.1, .1, .8, .05))
        self.assertIsNone(settled_rect("TAP", nameless, SimpleNamespace(elements=[nameless])))  # no locator to match

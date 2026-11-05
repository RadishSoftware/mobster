import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import harness_api
from mobile_agent.contract import Contract
from mobile_agent.frontier import FrontierAgent
from mobile_agent.learned import app_path as PA
from mobile_agent.learned import parts as PT
from mobile_agent.learned import programs as P
from mobile_agent.replay_core.path import screen_shape, signature_hash
from mobile_agent.replay_core.store import env_of
from mobile_agent.state import Element, signature
from mobile_agent.tests.test_frontier import Driver, screen

CAL, NOTES, BANK = "com.example.bluesky", "com.example.notes", "com.example.bank"
APPS = {CAL: "Bluesky", NOTES: "Notes", BANK: "Bank"}
ON = {"MOBSTER_PROGRAMS": "on", "MOBSTER_FRONTIER_CONTRACT": "on", "MOBSTER_SCREEN_FINISH": "on"}

CAL_HOME = screen(Element("1", "Today", "NavigationBar", (0, .07, 1, .06)),
                  Element("2", "Add Food", "Button", (.1, .8, .8, .06)),
                  Element("3", "Calories left", "StaticText", (.1, .2, .8, .06)), bundle=CAL)
CAL_SEARCH = screen(Element("4", "Search foods", "SearchField", (0, .1, 1, .06)),
                    Element("5", "Cancel", "Button", (.8, .1, .2, .06)),
                    Element("6", "Recent", "StaticText", (0, .2, 1, .06)), bundle=CAL)


def results(food, calories):
    return screen(*CAL_SEARCH.elements, Element("7", f"{food}, 1 serving, {calories} cal", "Cell", (0, .3, 1, .06)),
                  bundle=CAL)


NOTES_LIST = screen(Element("1", "Notes", "NavigationBar", (0, .07, 1, .06)),
                    Element("2", "New Note", "Button", (.8, .9, .2, .06)),
                    Element("3", "Folders", "Button", (0, .07, .2, .06)), bundle=NOTES)
EDITOR = screen(Element("4", "Title", "TextField", (0, .15, 1, .06)), Element("5", "Note", "TextView", (0, .2, 1, .6)),
                Element("6", "Done", "Button", (.8, .07, .2, .06)), bundle=NOTES)
TYPING = screen(*EDITOR.elements, *(Element(f"k{key}", key, "Key", (.1 * n, .8, .1, .05))
                                    for n, key in enumerate("qwertyuiop")), bundle=NOTES)


def saved(title, body):
    return screen(*NOTES_LIST.elements, Element("9", f"{title}, {body}", "Cell", (0, .3, 1, .06)), bundle=NOTES)


def step(op, label, role, snapshot, text=None, commit=None):
    pairs = signature(snapshot)
    return {"op": op, "app": snapshot.bundle_id,
            "target": {"label": label, "role": role, "id": f"/0/{label}", "frame": [0, 0, .1, .1]},
            "screen": {"hash": signature_hash(pairs), "signature": pairs, "shape": screen_shape(pairs)},
            "text": text, "param": None, "commit": commit, "point": None, "live": False,
            "search": role == "SearchField"}


CAL_GOAL = "In Bluesky's food database, how many calories are in one serving of Salmon?"
CAL_STEPS = [step("TAP", "Add Food", "Button", CAL_HOME), step("TYPE_SUBMIT", "Search foods", "SearchField",
                                                                    CAL_SEARCH, text="Salmon", commit="other")]
CAL_ITEMS = [{"kind": "READ", "app": "Bluesky", "what": "food database entry for Salmon", "payload": "",
              "act": "none", "quote": "In Bluesky's food database"},
             {"kind": "REPORT", "app": "Bluesky", "what": "calories in one serving of Salmon", "payload": "",
              "act": "none", "quote": "how many calories are in one serving of Salmon"}]
NOTE_GOAL = "Create a note titled 'Errands 5' that says 'post office'."
NOTE_STEPS = [step("TAP", "New Note", "Button", NOTES_LIST),
              step("TYPE", "Title", "TextField", EDITOR, text="Errands 5"),
              step("TYPE", "Note", "TextView", TYPING, text="post office"),
              step("TAP", "Done", "Button", TYPING)]
NOTE_ITEMS = [{"kind": "WRITE", "app": "Notes", "what": "note title", "payload": "Errands 5", "act": "none",
               "quote": "Create a note titled 'Errands 5'"},
              {"kind": "WRITE", "app": "Notes", "what": "note body", "payload": "post office", "act": "none",
               "quote": "that says 'post office'"}]


def learn(store, app, goal, steps, items, final, trusted=True):
    with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "on"}):
        P.learn(store, app=app, goal=goal, device="d", steps=steps, items=items, done=True, run_id="r1",
                final_pairs=signature(final))
    if trusted:
        store.note(store.cache.load(path_identity(store, goal), env_of(None, None, "d")), missed=False)


def path_identity(store, goal):
    return next(path["identity"] for path in store.all() if P.code_fills(path["identity"]["template"], goal))


def library(trusted=True):
    store = P.Programs(Path(tempfile.mkdtemp()))
    learn(store, CAL, CAL_GOAL, CAL_STEPS, CAL_ITEMS, results("Salmon", 233), trusted)
    learn(store, NOTES, NOTE_GOAL, NOTE_STEPS, NOTE_ITEMS, saved("Errands 5", "post office"), trusted)
    return store


def part(store, app):
    return max(((path, info) for path, info in PT.Parts(store, device="d").candidates() if info["app"] == app),
               key=lambda pair: pair[1]["end"] - pair[1]["start"])


class Learning(unittest.TestCase):
    def test_a_programs_parts_are_its_spans_and_no_screen_text_is_kept(self):
        store = library(trusted=False)
        found = list(PT.Parts(store, device="d").candidates())
        sizes = sorted((info["app"], info["end"] - info["start"] + 1) for _, info in found)
        self.assertEqual(sizes, [(CAL, 2), (NOTES, 4)])  # two fields of one form are never two parts
        notes = part(store, NOTES)[1]
        self.assertEqual((notes["blanks"], sorted(notes["exit"]["shows"])), (["p1", "p2"], ["p1", "p2"]))
        text = "".join(path.read_text() for path in store.directory.glob("*.json"))
        self.assertNotIn("Calories left", text)  # the screens' pairs are hashed, as programs keep them
        self.assertNotIn("signature", text)

    def test_a_keypad_amount_a_switch_or_home_is_never_inside_a_part(self):
        steps = [NOTE_STEPS[0], step("TAP", "1", "Button", EDITOR), step("TAP", "Pinned", "Switch", EDITOR),
                 step("TAP", "Done", "Button", EDITOR)]
        template, kept, values = P.program_from(NOTE_GOAL, steps)
        for start, end in PT.parts_of({"steps": kept}):
            roles = [(s["target"] or {}).get("label") for s in kept[start:end + 1]]
            self.assertNotIn("1", roles)
            self.assertNotIn("Pinned", roles)

    def test_a_commit_is_kept_only_for_a_commit_the_run_was_asked_for(self):
        compose = screen(Element("4", "To", "TextField", (0, .1, 1, .06)),
                         Element("5", "Message", "TextView", (0, .3, 1, .3)),
                         Element("6", "Send", "Button", (.8, .9, .2, .06)), bundle=NOTES)
        goal = "Send Maya Patel the message 'see you at noon'."
        steps = [step("TYPE", "To", "TextField", compose, text="Maya Patel"),
                 step("TYPE", "Message", "TextView", compose, text="see you at noon"),
                 step("TAP", "Send", "Button", compose, commit="send_message")]
        template, kept, values = P.program_from(goal, steps)
        asked = [{"kind": "COMMIT", "app": "Notes", "what": "message to Maya Patel", "payload": "see you at noon",
                  "act": "send_message", "quote": "Send Maya Patel the message 'see you at noon'"}]
        path = {"steps": kept, "contract": P.contract_template(asked, values, template)}
        self.assertEqual(PT.part_info(path, 0, 2)["commit"], "send_message")
        unasked = [{**asked[0], "kind": "WRITE", "act": "none"}]
        path = {"steps": kept, "contract": P.contract_template(unasked, values, template)}
        self.assertIsNone(PT.part_info(path, 0, 2)["commit"])  # it ends before the Send

    def test_parts_run_only_when_programs_are_turned_on_explicitly(self):
        for value in ("", "learn", "off", "yes"):
            self.assertFalse(PT.parts_on({"MOBSTER_PROGRAMS": value}), value)
        self.assertTrue(PT.parts_on({"MOBSTER_PROGRAMS": "on"}))

    def test_a_parts_trust_survives_its_program_learned_again(self):
        store = library(trusted=False)
        path, info = part(store, CAL)
        store.note_part(path, info["key"], "hit", trusted=False)
        learn(store, CAL, CAL_GOAL.replace("Salmon", "Banana"), [CAL_STEPS[0], step(
            "TYPE_SUBMIT", "Search foods", "SearchField", CAL_SEARCH, text="Banana", commit="other")],
              [{name: value.replace("Salmon", "Banana") for name, value in item.items()} for item in CAL_ITEMS],
              results("Banana", 105), trusted=False)
        path, info = part(store, CAL)
        self.assertEqual(path["parts"][info["key"]]["status"], P.TRUSTED)


class Binding(unittest.TestCase):
    def setUp(self):
        self.store = library()
        self.session = PT.Parts(self.store, device="d")
        self.cal, self.notes = part(self.store, CAL), part(self.store, NOTES)

    def items(self, *items):
        return Contract.from_items(items, "").items

    def bind(self, which, request, item=None, peers=()):
        path, info = which
        return self.session.bind(path, info, request, item, peers)

    def test_a_value_is_bound_from_another_wording_by_the_request_and_the_open_items(self):
        request = ("Look up how many calories one serving of Avocado has in Bluesky's food database, then create a "
                   "note titled 'Calories 9' in Notes with that number.")
        report, title = self.items(
            {"kind": "REPORT", "app": "Bluesky", "what": "calories in one serving of Avocado",
             "quote": "how many calories one serving of Avocado has"},
            {"kind": "WRITE", "app": "Notes", "what": "note title", "payload": "Calories 9",
             "quote": "create a note titled 'Calories 9'"})
        self.assertEqual(self.bind(self.cal, request, report), {"p1": "Avocado"})
        self.assertEqual(self.bind(self.notes, request, title), {"p1": "Calories 9"})  # no body: never guessed

    def test_one_part_binds_each_of_two_items_to_its_own_value(self):
        request = (
            "In Bluesky, how many calories are in one serving of Tuna, and how many are in one serving of Edamame?")
        tuna, edamame = self.items(
            {"kind": "REPORT", "app": "Bluesky", "what": "calories in one serving of Tuna", "quote": "Tuna"},
            {"kind": "REPORT", "app": "Bluesky", "what": "calories in one serving of Edamame", "quote": "Edamame"})
        self.assertEqual(self.bind(self.cal, request, tuna), {"p1": "Tuna"})
        self.assertEqual(self.bind(self.cal, request, edamame), {"p1": "Edamame"})
        self.assertEqual(self.bind(self.cal, request), {})

    def test_an_item_worded_otherwise_takes_its_value_from_the_item_that_names_it_as_taught(self):
        request = "Create a note, then tell me how many calories are in one serving of Quinoa in Bluesky."
        read, report = self.items(
            {"kind": "READ", "app": "Bluesky", "what": "Quinoa serving calories", "quote": "Quinoa in Bluesky"},
            {"kind": "REPORT", "app": "Bluesky", "what": "calories in one serving of Quinoa", "quote": "calories"})
        self.assertEqual(self.bind(self.cal, request, read), {"p1": "Quinoa in Bluesky"})
        self.assertEqual(self.bind(self.cal, request, read, (read, report)), {"p1": "Quinoa"})

    def test_a_value_that_isnt_the_requests_words_or_is_a_secret_is_never_bound(self):
        item, = self.items({"kind": "REPORT", "app": "Bluesky", "what": "calories in one serving of Kale chips",
                            "quote": "calories"})
        self.assertEqual(self.bind(self.cal, "How many calories are in kale?", item), {})
        with mock.patch.object(PT, "holds_secret", side_effect=lambda value: value == "hunter2"):
            values = PT.Parts(self.store, device="d").bind(*self.notes, "Create a note titled 'Wifi' that says "
                                                                         "'hunter2'.")
        self.assertEqual(values, {"p1": "Wifi"})

    def test_cues_that_disagree_bind_nothing_and_a_value_never_runs_across_quotes(self):
        request = "Create a note titled 'A b' that says 'x y', and a note titled 'C d' that says 'z w'."
        self.assertNotIn("p1", self.bind(self.notes, request))
        anchors = PT.cue("my note titled '{p1}'?", "p1")
        self.assertEqual(PT.agreed(PT.proposals("a note titled 'Groceries 12' that says 'rice and beans', then",
                                              anchors)), "Groceries 12")


class Model:

    def __init__(self, items=(), replies=(), answer="done"):
        self.items, self.replies, self.answer, self.turns, self.prompts = list(items), list(replies), answer, 0, []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def complete(self, messages, schema, timeout=60, reasoning=None):
        if "items" in schema["properties"]:
            return {"items": self.items}, {}
        self.turns += 1
        self.prompts.append(json.dumps(messages))
        if self.replies:
            return self.replies.pop(0), {}
        return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [], "answer": self.answer,
                "actions": [{"operation": "DONE", "target": None, "text": None}]}, {}


def item(kind, app, what, payload="", quote="", act="none"):
    return {"kind": kind, "app": app, "what": what, "payload": payload, "act": act, "count": "", "condition": "",
            "quote": quote}


COMPOSED = ("Create a note titled 'Garden 6' that says 'water the basil', then tell me how many calories are in "
            "one serving of Avocado in Bluesky.")
COMPOSED_ITEMS = [item("WRITE", "Notes", "note title", "Garden 6", "Create a note titled 'Garden 6'"),
                  item("WRITE", "Notes", "note body", "water the basil", "that says 'water the basil'"),
                  item("REPORT", "Bluesky", "calories in one serving of Avocado", "",
                       "how many calories are in one serving of Avocado")]
NOTE_ONLY = "Create a note titled 'Garden 6' that says 'water the basil'."


def run(screens, store, request=COMPOSED, model=None, apps=None, on=True, approve=None):
    driver, events = Driver(screens), []
    model = model or Model(COMPOSED_ITEMS)
    session = PT.Parts(store, device="d")
    with mock.patch.dict(os.environ, {**ON, "MOBSTER_PROGRAMS": "on" if on else ""}):
        result = FrontierAgent(driver, model, apps=apps or {NOTES: "Notes", CAL: "Bluesky"}, emit=events.append,
                               screenshots=False, settle_seconds=0, approve=approve,
                               program_parts=session).run(request)
    return result, driver, model, events, session


def part_events(events, phase=None):
    return [e for e in events if e.get("event") == "frontier_part" and (phase is None or e.get("phase") == phase)]


def status(store, app):
    path = next(path for path in store.all() if path["identity"]["app"] == app)
    info = max((PT.part_info(path, *span) for span in PT.parts_of(path)), key=lambda info: info["end"] - info["start"])
    return ((path.get("parts") or {}).get(info["key"]) or {}).get("status")


class Use(unittest.TestCase):
    def test_parts_of_two_programs_compose_with_no_model_call_until_the_answer(self):
        store = library()
        found = {"thought": "", "plan": "", "notes_add": [], "answer": "240 calories",
                 "checklist_updates": [{"id": 3, "status": "found", "value": "240 cal"}],
                 "actions": [{"operation": "DONE", "target": None, "text": None}]}
        screens = [NOTES_LIST, EDITOR, TYPING, TYPING, saved("Garden 6", "water the basil"), CAL_HOME, CAL_SEARCH,
                   results("Avocado", 240)]
        result, driver, model, events, _ = run(screens, store, model=Model(COMPOSED_ITEMS, [found]))
        self.assertEqual(driver.actions, [("TAP", "New Note", None), ("TYPE", "Title", "Garden 6"),
                                          ("TYPE", "Note", "water the basil"), ("TAP", "Done", None),
                                          ("LAUNCH_APP", CAL, None), ("TAP", "Add Food", None),
                                          ("TYPE_SUBMIT", "Search foods", "Avocado")])
        self.assertEqual(model.turns, 1)  # only the turn that reads the calories and answers
        self.assertEqual([e["outcome"] for e in part_events(events, "end")], ["hit", "hit"])
        self.assertEqual(result["parts"]["hits"], 2)

    def test_off_or_no_part_for_this_request_changes_nothing(self):
        store = library()
        _, driver, _, events, _ = run([NOTES_LIST, NOTES_LIST], store, on=False)
        self.assertEqual((driver.actions, part_events(events)), ([], []))
        items = [item("READ", "Notes", "my folders", quote="Show my folders")]
        _, driver, _, events, _ = run([NOTES_LIST, NOTES_LIST], store, request="Show my folders in Notes.",
                                      model=Model(items))
        self.assertEqual((driver.actions, part_events(events, "start")), ([], []))

    def test_a_screen_the_part_wasnt_made_for_gets_nothing_done(self):
        other = screen(Element("9", "Settings", "NavigationBar", (0, .07, 1, .06)),
                       Element("8", "Account", "Button", (0, .3, 1, .06)), bundle=NOTES)
        _, driver, model, events, _ = run([other] * 3, library())
        self.assertNotIn(("TAP", "New Note", None), driver.actions)
        self.assertFalse(part_events(events, "start"))

    def test_a_step_that_stops_fitting_hands_over_with_the_rest_as_a_plan_and_a_provisional_part_is_forgotten(self):
        store = library(trusted=False)
        broken = screen(Element("7", "Choose a template", "NavigationBar", (0, .07, 1, .06)),
                        Element("8", "Blank", "Button", (0, .3, 1, .06)), bundle=NOTES)
        _, driver, model, events, _ = run([NOTES_LIST, broken, broken, broken], store)
        self.assertEqual(driver.actions[0], ("TAP", "New Note", None))
        self.assertNotIn(("TYPE", "Title", "Garden 6"), driver.actions)  # never typed on the wrong screen
        self.assertIn("a part of a saved program", model.prompts[0])
        self.assertIn("Its remaining steps were", model.prompts[0])
        self.assertEqual(part_events(events, "end")[0]["outcome"], "miss")
        self.assertEqual(status(store, NOTES), "forgotten")

    def test_an_unbound_value_hands_back_at_its_step_and_is_no_miss(self):
        store = library()
        request = "Create a note titled 'Calories 9' in Notes with the calories of one serving of Avocado."
        items = [item("WRITE", "Notes", "note title", "Calories 9", "Create a note titled 'Calories 9'")]
        _, driver, model, events, _ = run([NOTES_LIST, EDITOR, TYPING, TYPING], store, request=request,
                                          model=Model(items))
        self.assertEqual(driver.actions[:2], [("TAP", "New Note", None), ("TYPE", "Title", "Calories 9")])
        self.assertIn("no text from this request to type", model.prompts[0])
        self.assertEqual(part_events(events, "end")[0]["outcome"], "partial")
        self.assertNotEqual(status(store, NOTES), "forgotten")

    def test_a_provisional_part_lets_the_model_look_and_a_done_run_trusts_it(self):
        store = library(trusted=False)
        screens = [NOTES_LIST, EDITOR, TYPING, TYPING, saved("Garden 6", "water the basil")]
        _, driver, model, events, session = run(screens, store, apps={NOTES: "Notes"},
                                                model=Model(COMPOSED_ITEMS[:2]), request=NOTE_ONLY + " Then stop.")
        self.assertEqual(len(driver.actions), 4)
        self.assertEqual(model.turns, 1)  # supervised: the model's DONE ends it, not code
        session.settle(done=True)
        self.assertEqual(status(store, NOTES), P.TRUSTED)

    def test_a_trusted_part_that_proves_the_contract_ends_the_run_with_no_call(self):
        screens = [NOTES_LIST, EDITOR, TYPING, TYPING, saved("Garden 6", "water the basil")]
        result, _, model, events, _ = run(screens, library(), apps={NOTES: "Notes"}, model=Model(COMPOSED_ITEMS[:2]),
                                          request=NOTE_ONLY + " Then stop.")
        self.assertEqual((model.turns, result["status"]), (0, "completed"))
        self.assertTrue(part_events(events, "finish"))

    def test_an_end_that_doesnt_hold_is_a_miss_and_the_model_is_told(self):
        wrong = saved("Something else", "entirely")
        _, _, model, events, _ = run([NOTES_LIST, EDITOR, TYPING, TYPING, wrong, wrong], library(),
                                     apps={NOTES: "Notes"}, model=Model(COMPOSED_ITEMS[:2]),
                                     request=NOTE_ONLY + " Then stop.")
        self.assertEqual(part_events(events, "end")[0]["outcome"], "miss")
        self.assertIn("end check failed", model.prompts[0])

    def test_a_part_that_wrote_never_serves_a_read_nor_a_value_its_request_says_otherwise(self):
        store = library()
        read = [item("READ", "Notes", "note titled 'Shopping List'", quote="my note titled 'Shopping List'"),
                item("REPORT", "Notes", "first line of the note titled 'Shopping List'", quote="the first line")]
        _, driver, _, _, _ = run([NOTES_LIST, NOTES_LIST], store, model=Model(read), apps={NOTES: "Notes"},
                                 request="Tell me the first line of my note titled 'Shopping List' in Notes.")
        self.assertNotIn(("TAP", "New Note", None), driver.actions)  # no note is made for a read
        rename = [item("WRITE", "Notes", "note title", "Chores", "Rename my note titled 'Errands' to 'Chores'")]
        _, driver, _, events, _ = run([NOTES_LIST, NOTES_LIST], store, model=Model(rename), apps={NOTES: "Notes"},
                                      request="Rename my note titled 'Errands' to 'Chores'.")
        self.assertNotIn(("TAP", "New Note", None), driver.actions)
        self.assertFalse(part_events(events, "start"))

    def test_a_look_a_visit_counted_as_done_still_runs_until_its_value_was_on_screen(self):
        store = library()
        request = "Look up one serving of Avocado in Bluesky's food database."
        items = [item("READ", "Bluesky", "food database entry for Avocado", quote="one serving of Avocado")]
        _, driver, _, _, _ = run([CAL_HOME, CAL_SEARCH, results("Avocado", 240)], store, request=request,
                                 model=Model(items), apps={CAL: "Bluesky"})
        self.assertEqual(driver.actions[:2], [("TAP", "Add Food", None), ("TYPE_SUBMIT", "Search foods", "Avocado")])
        _, _, _, events, _ = run([results("Avocado", 240)] * 3, store, request=request, model=Model(items),
                                 apps={CAL: "Bluesky"})
        self.assertFalse(part_events(events, "start"))  # Avocado is on screen already: that look is done

    def test_a_part_that_writes_never_starts_where_this_run_already_wrote(self):
        typed = {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [], "answer": None,
                 "actions": [{"operation": "TYPE", "target": "e1", "text": "Garden 6"},
                             {"operation": "TYPE", "target": None, "target_label": "Note", "text": "water the basil"},
                             {"operation": "TAP", "target": None, "target_label": "Done", "text": None}]}
        _, driver, _, events, _ = run([EDITOR, EDITOR, EDITOR, NOTES_LIST, NOTES_LIST, NOTES_LIST], library(),
                                      apps={NOTES: "Notes"}, model=Model(COMPOSED_ITEMS[:2], [typed]),
                                      request=NOTE_ONLY + " Then stop.")
        self.assertEqual(driver.actions[0], ("TYPE", "Title", "Garden 6"))
        self.assertNotIn(("TAP", "New Note", None), driver.actions)  # the model wrote it: no second note by code
        self.assertFalse(part_events(events, "start"))

    def test_another_app_is_opened_only_once_the_app_in_front_has_nothing_open_and_only_if_allowed(self):
        store = library()
        request = "Check my folders in Notes, then tell me how many calories are in one serving of Avocado in Bluesky."
        items = [COMPOSED_ITEMS[2],
                 item("WRITE", "Notes", "note title", "Folders 2", "Create a note titled 'Folders 2'")]
        _, driver, _, _, _ = run([NOTES_LIST, NOTES_LIST], store, request=request, model=Model(items))
        self.assertNotIn(("LAUNCH_APP", CAL, None), driver.actions)  # Notes still has an open item
        _, driver, _, _, _ = run([NOTES_LIST, NOTES_LIST], store, request=request, model=Model(items[:1]),
                                 apps={NOTES: "Notes"})
        self.assertNotIn(("LAUNCH_APP", CAL, None), driver.actions)  # Bluesky isn't an app this task may use
        _, driver, _, _, _ = run([NOTES_LIST, NOTES_LIST], store, request=request, model=Model(items[:1]))
        self.assertNotIn(("LAUNCH_APP", CAL, None), driver.actions)  # nothing done yet where the task starts
        from mobile_agent.frontier import screen_rows
        folders = next(alias for alias, e in screen_rows(NOTES_LIST, {})[0].items() if e.label == "Folders")
        looked = {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [], "answer": None,
                  "actions": [{"operation": "TAP", "target": folders, "text": None}]}
        _, driver, _, _, _ = run([NOTES_LIST, NOTES_LIST, CAL_HOME, CAL_SEARCH, results("Avocado", 240)], store,
                                 request=request, model=Model(items[:1], [looked]))
        self.assertEqual(driver.actions[:2], [("TAP", "Folders", None), ("LAUNCH_APP", CAL, None)])


class Commits(unittest.TestCase):
    PAY = screen(Element("1", "Pay", "NavigationBar", (0, .07, 1, .06)),
                 Element("2", "Memo", "TextField", (0, .3, 1, .06)),
                 Element("3", "Pay", "Button", (.1, .9, .8, .06)), bundle=BANK)
    PAID = screen(Element("1", "Payment sent", "StaticText", (0, .3, 1, .06)),
                  Element("4", "Done", "Button", (.1, .9, .8, .06)), bundle=BANK)
    GOAL = "In Bank, pay my rent with the memo 'October rent'."
    ITEMS = [item("COMMIT", "Bank", "rent payment", "October rent", "pay my rent with the memo 'October rent'", "pay")]

    def store(self):
        store = P.Programs(Path(tempfile.mkdtemp()))
        steps = [step("TYPE", "Memo", "TextField", self.PAY, text="October rent"),
                 step("TAP", "Pay", "Button", self.PAY, commit="pay")]
        learn(store, BANK, self.GOAL, steps, [{**self.ITEMS[0], "count": "1"}], self.PAID)
        return store

    def go(self, request, items, asked):
        driver, events = Driver([self.PAY, self.PAY, self.PAID, self.PAID]), []
        with mock.patch.dict(os.environ, ON):
            FrontierAgent(driver, Model(items), apps={BANK: "Bank"}, emit=events.append, screenshots=False,
                          settle_seconds=0, approve=lambda request: asked.append(request) or "approved",
                          program_parts=PT.Parts(self.store(), device="d")).run(request)
        return driver, events

    def test_a_parts_commit_asks_the_person_as_a_models_does(self):
        asked = []
        request = "In Bank, pay my rent with the memo 'November rent', then show me my balance."
        items = [{**self.ITEMS[0], "payload": "November rent", "quote": "pay my rent with the memo 'November rent'"}]
        driver, _ = self.go(request, items, asked)
        self.assertEqual(driver.actions[:2], [("TYPE", "Memo", "November rent"), ("TAP", "Pay", None)])
        self.assertEqual(len(asked), 1)  # the person was asked before the payment

    def test_a_commit_part_never_runs_for_a_request_that_doesnt_ask_for_that_commit(self):
        items = [item("WRITE", "Bank", "memo", "November rent", "draft my rent memo 'November rent'"),
                 item("FORBID", "Bank", "rent", quote="don't pay", act="pay")]
        driver, events = self.go("In Bank, draft my rent memo 'November rent' but don't pay.", items, [])
        self.assertNotIn(("TAP", "Pay", None), driver.actions)
        self.assertFalse(part_events(events, "start"))


class AppPath(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        patcher = mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def ctx(self, goal, run_id="r1"):
        return SimpleNamespace(origin="app", engine="smart", data_dir=self.dir, goal=goal, app_bundle=NOTES,
                               device_id="d", run_id=run_id)

    def test_the_hook_hands_the_loop_its_parts_and_the_listener_learns_with_the_values_shown(self):
        run = SimpleNamespace(id="r1", origin="app", engine="smart", mode="live", goal=NOTE_GOAL,
                              app={"bundleId": NOTES}, device_id="d", data_dir=self.dir)
        listener = PA.ProgramListener()
        listener.on_created(run, None)
        done = PA.pre_run(self.ctx(NOTE_GOAL), None, None)
        self.assertIsInstance(done.parts, PT.Parts)
        agent = SimpleNamespace(apps={NOTES: "Notes"})
        for snapshot in (NOTES_LIST, EDITOR, TYPING, saved("Errands 5", "post office")):
            done.parts._saw(agent, snapshot)
        for n, original in enumerate(NOTE_STEPS):
            listener.on_step(run, harness_api.StepRecord(
                run_id="r1", step=n, index=0, op=original["op"], app=original["app"], target=original["target"],
                text=original["text"], screen=original["screen"]["hash"], changed=True, approval=None,
                commit=original["commit"], shape=tuple(original["screen"]["shape"])))
        listener.on_finished(run, {"status": "completed", "outcome": "done"}, {"contract": NOTE_ITEMS})
        store = P.Programs(P.programs_dir(self.dir))
        _, info = part(store, NOTES)
        self.assertEqual(sorted(info["exit"]["shows"]), ["p1", "p2"])
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": ""}):
            self.assertIsNone(getattr(PA.pre_run(self.ctx(NOTE_GOAL, "r2"), None, None), "parts", None))

    def test_the_builder_hands_the_parts_to_the_agent(self):
        from mobile_agent import engines
        parts = PT.Parts(P.Programs(self.dir), device="d")
        agent = engines.build_frontier(Driver([NOTES_LIST]), Model(), apps={NOTES: "Notes"}, emit=lambda e: None,
                                       program_parts=parts)
        self.assertIs(agent.program_parts, parts)


if __name__ == "__main__":
    unittest.main()

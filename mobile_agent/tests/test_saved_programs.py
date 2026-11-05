import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent.frontier import FrontierAgent
from mobile_agent.learned import app_path as PA
from mobile_agent.learned import programs as P
from mobile_agent.replay_core.path import screen_shape
from mobile_agent.state import Element, signature
from mobile_agent.tests.test_frontier import Driver, screen

NOTES = "com.example.notes"
GOAL = "Create a note titled 'Errands 5' that says 'post office'."
SIBLING = "Create a note titled 'Garden 6' that says 'water the basil'."


def shape(snapshot):
    return screen_shape(signature(snapshot))


def step(op, label=None, role="Button", snapshot=None, text=None, app=NOTES, commit=None, seen=False):
    return {"op": op, "app": app, "target": {"label": label or "", "role": role, "id": None, "frame": None}
            if label is not None else None,
            "screen": {"hash": "h", **({"signature": [["Button", "secret screen text"]]} if seen else {}),
                       **({"shape": shape(snapshot)} if snapshot is not None else {})},
            "text": text, "param": None, "commit": commit, "point": None, "live": False}


LIST = screen(Element("1", "Notes", "NavigationBar", (0, .07, 1, .06)),
              Element("2", "New Note", "Button", (.8, .9, .2, .06)), bundle=NOTES)
EDITOR = screen(Element("3", "Note", "TextView", (0, .2, 1, .6)), Element("4", "Done", "Button", (.8, .07, .2, .06)),
                bundle=NOTES)


def learned_steps():
    return [step("TAP", "New Note", snapshot=LIST, seen=True),
            step("TYPE", "Note", "TextView", EDITOR, text="Errands 5", seen=True),
            step("TYPE", "Note", "TextView", EDITOR, text="post office", seen=True)]


class Learning(unittest.TestCase):
    def test_typed_values_from_the_request_become_blanks_and_no_screen_text_is_kept(self):
        template, steps, values = P.program_from(GOAL, learned_steps())
        self.assertEqual(template, "Create a note titled '{p1}' that says '{p2}'.")
        self.assertEqual(values, {"p1": "Errands 5", "p2": "post office"})
        self.assertEqual([s.get("param") for s in steps], [None, "p1", "p2"])
        self.assertTrue(all(s["text"] is None for s in steps[1:]))
        self.assertTrue(all("signature" not in s["screen"] for s in steps))  # hashed pairs only

    def test_text_the_model_wrote_ends_the_program_there(self):
        written = learned_steps()[:2] + [step("TYPE", "Note", "TextView", EDITOR, text="bring the parcel slip")]
        _, steps, _ = P.program_from(GOAL, written)
        self.assertEqual(len(steps), 2)
        for unsafe in (step("TAP_XY", None), {**step("TAP", "x"), "live": True}, step("READ_LIST", None)):
            self.assertEqual(len(P.program_from(GOAL, learned_steps()[:1] + [unsafe] + learned_steps()[1:])[1]), 1)

    def test_a_row_named_by_the_request_then_its_details_is_found_by_its_name(self):
        rows = screen(Element("1", "Banana Bread, Recipes, ·, Mom's recipe", "Cell", (0, .3, 1, .06)),
                      Element("2", "Workout Plan, Personal, ·, Weekly Schedule:", "Cell", (0, .4, 1, .06)),
                      bundle=NOTES)
        goal = "In Notes, what's the first line of my note titled 'Banana Bread'?"
        template, steps, values = P.program_from(goal, [step("TAP", "Banana Bread, Recipes, ·, Mom's recipe", "Cell",
                                                             rows, seen=True)])
        self.assertEqual(template, "In Notes, what's the first line of my note titled '{p1}'?")
        self.assertEqual(steps[0]["label_prefix"], "p1")
        self.assertNotIn("Mom's recipe", json.dumps(steps))
        sibling = P.filled_steps({"steps": steps}, {"p1": "Workout Plan"})[0]
        ok, element, _ = P.fits(sibling, rows)
        self.assertTrue(ok)
        self.assertEqual(element.label, "Workout Plan, Personal, ·, Weekly Schedule:")
        twice = screen(Element("1", "Workout Plan, Personal", "Cell", (0, .3, 1, .06)),
                       Element("2", "Workout Plan, Archive", "Cell", (0, .4, 1, .06)), bundle=NOTES)
        self.assertFalse(P.fits(sibling, twice)[0])
        self.assertFalse(P.fits(P.filled_steps({"steps": steps}, {"p1": "Books"})[0], rows)[0])

    def test_a_commits_control_is_never_a_blank_and_names_inside_words_stay(self):
        goal = "In SplitPay, pay Maya Patel $12 for lunch."
        steps = [step("TAP", "Maya Patel", snapshot=LIST, seen=True), step("TAP", "Pay", snapshot=EDITOR, seen=True,
                                                                         commit="pay")]
        template, out, values = P.program_from(goal, steps)
        self.assertEqual(template, "In SplitPay, pay {p1} $12 for lunch.")  # "pay" stays the request's own word
        self.assertNotIn("label_param", out[1])
        items = [{"kind": "COMMIT", "app": "SplitPay", "what": "Maya Patel $12 for lunch", "payload": "",
                  "act": "pay", "count": "1", "condition": "", "quote": "pay Maya Patel $12 for lunch"}]
        kept = P.contract_template(items, {"p1": "Maya Patel", "p2": "pay"}, "In SplitPay, {p2} {p1} $12 for lunch.")
        self.assertEqual((kept[0]["app"], kept[0]["act"]), ("SplitPay", "pay"))  # enums and names kept as they are
        self.assertEqual(kept[0]["quote"], "{p2} {p1} $12 for lunch")

    def test_a_contract_keeps_only_the_requests_words(self):
        values = {"p1": "Errands 5", "p2": "post office"}
        template = "Create a note titled '{p1}' that says '{p2}'."
        items = [{"kind": "WRITE", "app": "Notes", "what": "note titled Errands 5", "payload": "post office",
                  "act": "none", "count": "", "condition": "", "quote": "Create a note titled 'Errands 5'"}]
        kept = P.contract_template(items, values, template)
        self.assertEqual(kept[0]["what"], "note titled {p1}")
        self.assertEqual(kept[0]["payload"], "{p2}")
        screen_words = [{**items[0], "payload": "post office at 4417 Elm"}]
        self.assertIsNone(P.contract_template(screen_words, values, template))


SETTINGS = "com.apple.Preferences"
ROOT = screen(Element("1", "Settings", "NavigationBar", (0, .07, 1, .06)),
              Element("2", "General", "Button", (0, .4, 1, .05)), bundle=SETTINGS)
GENERAL = screen(Element("1", "General", "NavigationBar", (0, .07, 1, .06)),
                 Element("2", "About", "Cell", (0, .2, 1, .05)),
                 Element("3", "Software Update", "Cell", (0, .26, 1, .05)),
                 bundle=SETTINGS)
GENERAL_LOW = screen(Element("1", "General", "NavigationBar", (0, .07, 1, .06)),
                     Element("4", "Date & Time", "Cell", (0, .3, 1, .05)),
                     Element("5", "Fonts", "Cell", (0, .4, 1, .05)),
                     Element("6", "Keyboard", "Cell", (0, .5, 1, .05)), bundle=SETTINGS)
OPEN_PAGE = "Open Settings › General › {}."


def scrolled_steps(page="Date & Time", sought=None):
    return [step("TAP", "General", snapshot=ROOT, app=SETTINGS),
            {**step("SCROLL_TO", None, snapshot=GENERAL, app=SETTINGS), "live": True, "scroll": sought or page},
            step("TAP", page, "Cell", snapshot=GENERAL_LOW, app=SETTINGS)]


class ScrolledNavigation(unittest.TestCase):

    def test_only_a_programs_blank_named_scroll_is_a_kept_step_and_the_generic_replayer_refuses_it(self):
        from mobile_agent.replay_core.engine import Replayer
        from mobile_agent.replay_core.path import valid_step
        kept = {"op": "SCROLL_TO", "app": SETTINGS, "target": None, "param": "p2", "live": False,
                "screen": {"hash": "h"}}
        self.assertTrue(valid_step(kept))
        self.assertFalse(valid_step({**kept, "param": None}))  # not a blank: live, the model's each run
        self.assertFalse(valid_step({**kept, "target": {"label": "Fonts", "role": "Cell"}}))
        with self.assertRaises(ValueError):
            Replayer(Driver([GENERAL]), params={"p2": "Fonts"}).act(kept, None, GENERAL)

    def test_a_scroll_to_a_row_the_request_doesnt_name_still_ends_the_program(self):
        for sought in ("Legal & Regulatory", "Date"):
            template, steps, _ = P.program_from(OPEN_PAGE.format("Date & Time"), scrolled_steps(sought=sought)[:2]
                                                + scrolled_steps()[2:])
            if sought == "Date":
                self.assertEqual(P.code_fills(template, OPEN_PAGE.format("Fonts")), [])
                continue
            self.assertEqual([s["op"] for s in steps], ["TAP"])
            self.assertEqual(template, "Open Settings › {p1} › Date & Time.")
        unsafe = {**scrolled_steps()[1], "scroll": "\u2022\u2022\u2022\u2022"}
        self.assertEqual(len(P.program_from(OPEN_PAGE.format("Date & Time"), scrolled_steps()[:1] + [unsafe])[1]), 1)

    def test_the_replay_scrolls_to_this_requests_page_and_taps_it_only_on_a_screen_like_the_recorded_one(self):
        _, steps, _ = P.program_from(OPEN_PAGE.format("Date & Time"), scrolled_steps())
        program = matched(P.filled_steps({"steps": steps}, {"p1": "General", "p2": "Fonts"}),
                          {"p1": "General", "p2": "Fonts"}, READ_NOTES)
        def scroll_to(agent, snapshot, text):  # the macro's swipes, as one action that brings the page up
            agent.driver.actions.append(("SCROLL_TO", None, text))
            agent.driver.index += 1
            return True
        with mock.patch.object(FrontierAgent, "_scroll_to", scroll_to):
            _, driver, _, _ = run([ROOT, GENERAL, GENERAL_LOW, GENERAL_LOW, GENERAL_LOW], program,
                                  request=OPEN_PAGE.format("Fonts"))
        self.assertEqual(driver.actions[:3], [("TAP", "General", None), ("SCROLL_TO", None, "Fonts"),
                                              ("TAP", "Fonts", None)])
        program = matched(P.filled_steps({"steps": steps}, {"p1": "General", "p2": "Fonts"}),
                          {"p1": "General", "p2": "Fonts"}, READ_NOTES)
        _, driver, model, _ = run([LIST, LIST, LIST], program, request=OPEN_PAGE.format("Fonts"))
        self.assertEqual(driver.actions, [])
        self.assertIn("saved program", model.prompts[0])


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store = P.Programs(self.dir)

    def save(self, items=()):
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "on"}):
            return P.learn(self.store, app=NOTES, goal=GOAL, device="d", steps=learned_steps(), items=items,
                           done=True, run_id="r1")

    def test_a_done_run_is_kept_without_answers_notes_or_screen_text(self):
        self.save()
        files = list(self.dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text()
        self.assertNotIn("secret screen text", text)
        self.assertNotIn("Errands 5", text)  # the value is a blank; only the template's own words stay
        self.assertEqual(oct(files[0].stat().st_mode & 0o777), "0o600")

    def test_off_or_not_done_learns_nothing(self):
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": ""}):
            self.assertIsNone(P.learn(self.store, app=NOTES, goal=GOAL, device="d", steps=learned_steps(), items=(),
                                      done=True))
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "on"}):
            self.assertIsNone(P.learn(self.store, app=NOTES, goal=GOAL, device="d", steps=learned_steps(), items=(),
                                      done=False))
        self.assertEqual(self.store.all(), [])

    def test_a_new_program_is_provisional_and_a_failed_first_replay_forgets_it_at_once(self):
        self.save()
        path = self.store.all()[0]
        self.assertEqual(path["status"], P.PROVISIONAL)
        self.assertIsNone(self.store.note(path, missed=True))
        self.assertEqual(self.store.all(), [])

    def test_a_proven_replay_makes_it_trusted_and_then_two_misses_in_a_row_forget(self):
        self.save()
        path = self.store.note(self.store.all()[0], missed=False)
        self.assertEqual(path["status"], P.TRUSTED)
        path = self.store.note(path, missed=True)
        path = self.store.note(path, missed=False)
        self.assertEqual(path["misses"], 0)
        path = self.store.note(path, missed=True)
        self.assertIsNotNone(path)
        self.assertIsNone(self.store.note(path, missed=True))
        self.assertEqual(self.store.all(), [])

    def test_it_keeps_what_its_first_run_took_and_learned_again_keeps_that(self):
        from mobile_agent.replay_core.path import first_run
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "on"}):
            P.learn(self.store, app=NOTES, goal=GOAL, device="d", steps=learned_steps(), items=(), done=True,
                    run_id="r1", first=first_run("r1", 48.214, 0.0612))
            P.learn(self.store, app=NOTES, goal=GOAL, device="d", steps=learned_steps(), items=(), done=True,
                    run_id="r2", first=first_run("r2", 21.0, 0.03))
        path = self.store.all()[0]
        self.assertEqual({k: path["first"][k] for k in ("runId", "seconds", "costUsd")},
                         {"runId": "r1", "seconds": 48.21, "costUsd": 0.0612})
        self.assertEqual(P.public(path)["first"]["runId"], "r1")
        self.assertIsNone(first_run("r3", 5, None)["costUsd"])
        self.assertIsNone(first_run("r3", None, 0.01))

    def test_learned_again_after_a_proven_replay_it_stays_trusted(self):
        self.save()
        identity = self.store.all()[0]["identity"]
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "on"}):
            P.learn(self.store, app=NOTES, goal="Create a note titled 'X 1' that says 'y z'.", device="d",
                    steps=learned_steps()[:1], items=(), done=True, trusted=identity)
        self.assertEqual({p["status"] for p in self.store.all()}, {P.PROVISIONAL})
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "on"}):
            P.learn(self.store, app=NOTES, goal=GOAL, device="d", steps=learned_steps(), items=(), done=True,
                    trusted=identity)
        self.assertEqual(next(p for p in self.store.all() if p["identity"] == identity)["status"], P.TRUSTED)
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "on"}):
            P.learn(self.store, app=NOTES, goal=GOAL, device="d", steps=learned_steps(), items=(), done=True)
        self.assertEqual(next(p for p in self.store.all() if p["identity"] == identity)["status"], P.TRUSTED)


class Matching(unittest.TestCase):
    def setUp(self):
        self.store = P.Programs(Path(tempfile.mkdtemp()))
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "on"}):
            P.learn(self.store, app=NOTES, goal=GOAL, device="d", steps=learned_steps(), items=(), done=True)
        self.path = self.store.all()[0]

    def test_values_from_the_request_that_fill_the_template_into_it_are_accepted(self):
        matched = P.accept(self.path, {"p1": "Garden 6", "p2": "water the basil"}, SIBLING)
        self.assertIsNotNone(matched)
        self.assertEqual(matched.values["p2"], "water the basil")

    def test_a_value_not_in_the_request_or_a_template_that_isnt_the_request_is_refused(self):
        for values, goal in (({"p1": "Garden 6", "p2": "water the roses"}, SIBLING),
                             ({"p1": "Garden 6", "p2": "water the basil"}, SIBLING + " Then share it."),
                             ({"p1": "Garden 6"}, SIBLING),
                             ({"p1": "Garden 6", "p2": "water the basil", "p3": "x"}, SIBLING)):
            self.assertIsNone(P.accept(self.path, values, goal), values)

    def test_a_secret_never_fills_a_blank(self):
        goal = "Create a note titled 'Garden 6' that says 'hunter2'."
        self.assertIsNotNone(P.accept(self.path, {"p1": "Garden 6", "p2": "hunter2"}, goal))
        self.assertIsNone(P.accept(self.path, {"p1": "Garden 6", "p2": "hunter2"}, goal,
                                   secret=lambda value: value == "hunter2"))

    def test_a_longer_request_never_fills_the_last_blank_with_its_other_clauses(self):
        client = mock.Mock()
        longer = ("Create a note titled 'Errand 23' that says 'return library books', then tell me the first line of "
                  "my note titled 'Apartment Notes'.")
        self.assertEqual(P.code_fills(self.path["identity"]["template"], longer), [])
        self.assertIsNone(P.match(client, self.store, goal=longer, app=NOTES, device="d")[0])
        client.complete.assert_not_called()  # no program: the run is the model's
        self.assertIsNone(P.accept(self.path, {"p1": "Errand 23", "p2": "return library books', then tell me the "
                                               "first line of my note titled 'Apartment Notes"}, longer))
        for template, value in (("Open {p1}", "General, then turn off Bluetooth"), ("Open {p1}", "Wi-Fi; Bluetooth"),
                                ("Open {p1}", "Wi-Fi. Open Bluetooth"), ("Say '{p1}'", 'the "Best" plan')):
            self.assertTrue(P.crosses(template, "p1", value), value)

    def test_only_an_ambiguous_fill_asks_and_code_checks_the_answer(self):
        store = P.Programs(Path(tempfile.mkdtemp()))
        goal = "Log one serving of Apple for lunch today."
        steps = [step("TYPE", "Search", "SearchField", LIST, text="Apple", seen=True),
                 step("TAP", "lunch", snapshot=EDITOR, seen=True)]
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "on"}):
            P.learn(store, app=NOTES, goal=goal, device="d", steps=steps, items=(), done=True)
        ambiguous = "Log one serving of fish for one for lunch today."  # "fish" + "one for lunch", or "fish for one"
        client = mock.Mock()
        client.complete.return_value = ({"program": 0, "values": [{"blank": "p1", "value": "fish for one"},
                                                                 {"blank": "p2", "value": "lunch"}]}, {"x": 1})
        matched, _ = P.match(client, store, goal=ambiguous, app=NOTES, device="d")
        client.complete.assert_called_once()
        self.assertEqual(matched.values["p1"], "fish for one")
        client.complete.return_value = ({"program": 0, "values": [{"blank": "p1", "value": "fish"},
                                                                 {"blank": "p2", "value": "dinner"}]}, {})
        self.assertIsNone(P.match(client, store, goal=ambiguous, app=NOTES, device="d")[0])  # not the request
        client.complete.side_effect = RuntimeError("down")
        self.assertIsNone(P.match(client, store, goal=ambiguous, app=NOTES, device="d")[0])


class Model:

    def __init__(self, items=(), answer="done"):
        self.items, self.answer, self.turns, self.prompts = list(items), answer, 0, []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def complete(self, messages, schema, timeout=60, reasoning=None):
        if "items" in schema["properties"]:
            return {"items": self.items}, {}
        self.turns += 1
        self.prompts.append(json.dumps(messages))
        return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [], "answer": self.answer,
                "actions": [{"op": "DONE", "target": None, "text": None}]}, {}


def matched(steps, values=None, items=None, trusted=True):
    path = {"identity": {"template": "x"}, "steps": steps, "status": P.TRUSTED if trusted else P.PROVISIONAL}
    out = P.Matched(path, values or {})
    out.items = items
    return out


def run(screens, program, request=SIBLING, model=None):
    driver, events = Driver(screens), []
    model = model or Model()
    result = FrontierAgent(driver, model, apps={NOTES: "Notes"}, emit=events.append, screenshots=False,
                           settle_seconds=0, saved_program=program).run(request)
    return result, driver, model, events


READ_NOTES = [{"kind": "READ", "app": "Notes", "what": "notes", "payload": "", "act": "none", "count": "",
               "condition": "", "quote": ""}]


class Replay(unittest.TestCase):
    def test_steps_that_fit_act_with_no_model_call_and_type_this_requests_values(self):
        steps = [step("TAP", "New Note", snapshot=LIST), {**step("TYPE", "Note", "TextView", EDITOR), "param": "p1"}]
        program = matched(steps, {"p1": "Garden 6"}, READ_NOTES)
        result, driver, model, events = run([LIST, EDITOR, EDITOR, EDITOR], program)
        self.assertEqual(driver.actions[:2], [("TAP", "New Note", None), ("TYPE", "Note", "Garden 6")])
        self.assertEqual(model.turns, 0)  # the contract is proven and reports nothing: done in code
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["program"]["status"], "done")
        sources = [e.get("source") for e in events if e.get("event") == "frontier_decision"]
        self.assertEqual(sources, ["program", "program"])

    def test_a_provisional_programs_replay_ends_through_the_models_done(self):
        steps = [step("TAP", "New Note", snapshot=LIST), {**step("TYPE", "Note", "TextView", EDITOR), "param": "p1"}]
        program = matched(steps, {"p1": "Garden 6"}, READ_NOTES, trusted=False)
        result, driver, model, events = run([LIST, EDITOR, EDITOR, EDITOR], program)
        self.assertEqual(driver.actions[:2], [("TAP", "New Note", None), ("TYPE", "Note", "Garden 6")])
        self.assertEqual(model.turns, 1)  # supervised: the model looks, the DONE gate decides
        self.assertIn("learned from one earlier run", model.prompts[0])
        self.assertFalse(any(e.get("event") == "frontier_program_finish" for e in events))
        self.assertEqual(result["program"]["status"], "done")

    def test_a_program_given_as_a_function_is_matched_with_the_runs_client(self):
        steps = [step("TAP", "New Note", snapshot=LIST)]
        seen = []

        def find(client, request):
            seen.append((client, request))
            return matched(steps, {}, READ_NOTES)
        model = Model()
        result, driver, _, events = run([LIST, EDITOR, EDITOR], find, model=model)
        self.assertEqual(seen, [(model, SIBLING)])
        self.assertEqual(driver.actions[0], ("TAP", "New Note", None))
        result, driver, _, _ = run([LIST, LIST], lambda client, request: None, model=Model(READ_NOTES))
        self.assertNotIn("program", result)

    def test_a_contract_with_something_to_report_takes_one_model_turn_at_the_end(self):
        steps = [step("TAP", "New Note", snapshot=LIST)]
        items = READ_NOTES + [{"kind": "REPORT", "app": "Notes", "what": "the note's title", "payload": "",
                               "act": "none", "count": "", "condition": "", "quote": ""}]
        result, driver, model, _ = run([LIST, EDITOR, EDITOR], matched(steps, {}, items))
        self.assertEqual(driver.actions[0], ("TAP", "New Note", None))
        self.assertGreaterEqual(model.turns, 1)  # the model reads and answers: code can't
        self.assertIn("repeated the 1 steps of a saved program", model.prompts[0])

    def test_a_screen_that_doesnt_fit_hands_over_with_the_rest_as_a_plan(self):
        other = screen(Element("9", "Folders", "NavigationBar", (0, .07, 1, .06)),
                       Element("8", "Shared", "Button", (0, .3, 1, .06)), bundle=NOTES)
        steps = [step("TAP", "New Note", snapshot=LIST), {**step("TYPE", "Note", "TextView", EDITOR), "param": "p1"}]
        result, driver, model, _ = run([other, other, other], matched(steps, {"p1": "Garden 6"}, READ_NOTES))
        self.assertEqual(driver.actions, [])  # nothing done on a screen the program wasn't made for
        self.assertGreaterEqual(model.turns, 1)
        self.assertIn("saved program", model.prompts[0])
        self.assertIn("Its remaining steps were", model.prompts[0])
        self.assertEqual(result["program"]["status"], "stopped")

    def test_a_commit_goes_through_the_contracts_check(self):
        compose = screen(Element("5", "Message", "TextView", (0, .3, 1, .3)),
                         Element("6", "Send", "Button", (.8, .9, .2, .06)),
                         bundle=NOTES)
        sent = screen(Element("7", "Sent", "StaticText", (0, .3, 1, .06)), bundle=NOTES)
        steps = [step("TAP", "Send", snapshot=compose, commit="send")]
        request = "In Notes, send it."
        declared = READ_NOTES + [{"kind": "COMMIT", "app": "Notes", "what": "the note", "payload": "", "act": "other",
                                  "count": "1", "condition": "", "quote": "send it"}]
        _, driver, _, _ = run([compose, sent, sent, sent], matched(steps, {}, declared), request=request)
        self.assertIn(("TAP", "Send", None), driver.actions)
        result, driver, _, events = run([compose, compose, compose, compose], matched(steps, {}, READ_NOTES),
                                        request=request)
        self.assertNotIn(("TAP", "Send", None), driver.actions)  # not declared: refused before dispatch
        self.assertTrue(any(e.get("event") == "frontier_refused" for e in events))
        self.assertEqual(result["program"]["status"], "stopped")


if __name__ == "__main__":
    unittest.main()


from types import SimpleNamespace  # noqa: E402

from mobile_agent import harness_api  # noqa: E402


def record(run_id, n, op, label, role, snapshot, text=None):
    return harness_api.StepRecord(run_id=run_id, step=n, index=0, op=op, app=NOTES,
                                  target={"label": label, "role": role, "id": None, "frame": [0, 0, .1, .1]},
                                  text=text, screen="h", changed=True, approval=None, commit=None,
                                  shape=tuple(shape(snapshot)))


CONTRACT = [{"kind": "WRITE", "app": "Notes", "what": "note titled Errands 5", "payload": "post office", "act": "none",
             "count": "", "condition": "", "quote": "Create a note titled 'Errands 5'"}]


class AppPathHelpers(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        patcher = mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_through(self, run_id, goal, outcome="done", status="completed", matched=None, origin="app"):
        run = SimpleNamespace(id=run_id, origin=origin, engine="smart", mode="live", goal=goal,
                              app={"bundleId": NOTES}, device_id="d", data_dir=self.dir)
        listener = PA.ProgramListener()
        listener.on_created(run, None)
        if matched is not None:
            PA._RUNS.get(run_id)["matched"] = matched
        for n, (op, label, role, snapshot, text) in enumerate((
                ("TAP", "New Note", "Button", LIST, None), ("TYPE", "Note", "TextView", EDITOR, "Errands 5"),
                ("TYPE", "Note", "TextView", EDITOR, "post office"))):
            listener.on_step(run, record(run_id, n, op, label, role, snapshot, text))
        listener.on_finished(run, {"status": status, "outcome": outcome}, {"notes": [], "contract": CONTRACT})
        return P.Programs(P.programs_dir(self.dir))

    def ctx(self, goal, run_id="r9", origin="app"):
        return SimpleNamespace(origin=origin, engine="smart", data_dir=self.dir, goal=goal, app_bundle=NOTES,
                               device_id="d", run_id=run_id)


class AppPath(AppPathHelpers):
    def test_a_done_run_is_learned_with_its_contract_and_nothing_else_is(self):
        store = self.run_through("r1", GOAL)
        path = store.all()[0]
        self.assertEqual(path["identity"]["template"], "Create a note titled '{p1}' that says '{p2}'.")
        self.assertEqual(path["contract"][0]["payload"], "{p2}")
        self.assertEqual(path["status"], P.PROVISIONAL)
        self.dir = Path(tempfile.mkdtemp())
        self.assertEqual(self.run_through("r2", GOAL, outcome="check").all(), [])  # not proven: not learned
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "off"}):
            self.dir = Path(tempfile.mkdtemp())
            self.assertEqual(self.run_through("r3", GOAL).all(), [])

    def test_unset_it_is_on_for_the_mac_app_serve_mcp_and_remote_and_off_elsewhere(self):
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": ""}):
            self.assertEqual(len(self.run_through("r1", GOAL).all()), 1)
            self.assertIsInstance(PA.pre_run(self.ctx(SIBLING), None, None).program, P.Matched)
            for origin in ("app", "api", "mcp", "remote"):
                self.assertEqual(P.mode(origin=origin), "on", origin)
            for origin in ("tui", "cli", "workflow", "schedule", "hook", None):
                self.assertEqual(P.mode(origin=origin), "off", origin)
            self.assertIsNone(PA.pre_run(self.ctx(SIBLING, origin="tui"), None, None))

    def test_off_turns_it_off_for_the_mac_app_and_on_turns_it_on_anywhere(self):
        self.run_through("r1", GOAL)
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "off"}):
            self.assertEqual(P.mode(origin="app"), "off")
            self.assertIsNone(PA.pre_run(self.ctx(SIBLING), None, None))
            self.dir = Path(tempfile.mkdtemp())
            self.assertEqual(self.run_through("r2", GOAL).all(), [])
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "on"}):
            self.assertEqual(P.mode(origin="tui"), "on")
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "yes"}):
            self.assertEqual(P.mode(origin="app"), "off")

    def test_the_hook_hands_over_a_program_code_matched_and_nothing_otherwise(self):
        self.run_through("r1", GOAL)
        done = PA.pre_run(self.ctx(SIBLING), None, None)
        self.assertIsInstance(done.program, P.Matched)  # matched by code: no call
        self.assertEqual(done.program.values["p2"], "water the basil")
        self.assertIsNone(getattr(PA.pre_run(self.ctx("Delete my note 'Garden 6'."), None, None), "program", None))
        with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "learn"}):
            self.assertIsNone(PA.pre_run(self.ctx(SIBLING), None, None))

    def test_the_hook_hands_nothing_for_a_request_that_says_more_than_the_program(self):
        self.run_through("r1", GOAL)
        longer = ("Create a note titled 'Garden 6' that says 'water the basil', then tell me the first line of my "
                  "note titled 'Apartment Notes'.")
        self.assertIsNone(getattr(PA.pre_run(self.ctx(longer), None, None), "program", None))  # the model runs it
        self.assertIsInstance(PA.pre_run(self.ctx(SIBLING), None, None).program, P.Matched)

    def test_a_proven_replay_trusts_the_program_and_a_failed_one_forgets_it(self):
        store = self.run_through("r1", GOAL)
        good = P.Matched(store.all()[0], {"p1": "Garden 6", "p2": "water the basil"})
        good.stopped = "done"  # it took every step
        store = self.run_through("r2", SIBLING, matched=good)
        statuses = {p["identity"]["template"]: p["status"] for p in store.all()}
        self.assertEqual(statuses["Create a note titled '{p1}' that says '{p2}'."], P.TRUSTED)
        self.assertTrue(all(status == P.PROVISIONAL for template, status in statuses.items()
                            if template != "Create a note titled '{p1}' that says '{p2}'."))
        self.dir = Path(tempfile.mkdtemp())
        store = self.run_through("r3", GOAL)
        bad = P.Matched(store.all()[0], {"p1": "Garden 6", "p2": "water the basil"})
        bad.stopped = "its step didn't fit"
        run = SimpleNamespace(id="r4", origin="app", engine="smart", mode="live", goal=SIBLING,
                              app={"bundleId": NOTES}, device_id="d", data_dir=self.dir)
        listener = PA.ProgramListener()
        listener.on_created(run, None)
        PA._RUNS.get("r4")["matched"] = bad
        listener.on_finished(run, {"status": "completed", "outcome": "check"}, {})
        self.assertEqual(store.all(), [])  # provisional and failed: forgotten at once

    def test_the_builder_hands_the_program_to_the_agent(self):
        from mobile_agent import engines
        program = matched([step("TAP", "New Note", snapshot=LIST)])
        agent = engines.build_frontier(Driver([LIST]), Model(), apps={NOTES: "Notes"}, emit=lambda e: None,
                                       saved_program=program)
        self.assertIs(agent.saved_program, program)


from mobile_agent.permissions import ASK_IMPORTANT, DONT_ASK, PermissionGate, gate_for_run, policy  # noqa: E402

MAIL = "com.apple.mobilemail"
COMPOSE = screen(Element("5", "Message", "TextView", (0, .3, 1, .3)), Element("6", "Send", "Button", (.8, .9, .2, .06)),
                 bundle=MAIL)
SENT = screen(Element("7", "Sent", "StaticText", (0, .3, 1, .06)), bundle=MAIL)
OUTSIDE = ("mcp", "remote")  # a coding agent's phone_task through `mobster mcp`, and Mobster Remote


class OutsideAgentPath(AppPathHelpers):

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("MOBSTER_PROGRAMS", None)  # unset: the default for the origin decides

    def trusted(self, origin):
        store = self.run_through("r1", GOAL, origin=origin)
        store.note(store.all()[0], missed=False)
        self.assertEqual([path["status"] for path in store.all()], [P.TRUSTED])

    def test_with_programs_unset_a_run_from_mcp_or_remote_learns_and_repeats_in_code(self):
        for origin in OUTSIDE:
            with self.subTest(origin=origin):
                self.dir = Path(tempfile.mkdtemp())
                self.assertEqual(P.mode(origin=origin), "on")
                self.trusted(origin)  # learned from a done run, then proven
                handed = PA.pre_run(self.ctx(SIBLING, origin=origin), None, None)
                self.assertIsInstance(handed.program, P.Matched)  # matched by code: no call
                self.assertEqual(handed.program.values, {"p1": "Garden 6", "p2": "water the basil"})
                result, driver, _, events = run([LIST, EDITOR, EDITOR, EDITOR, EDITOR], handed.program)
                self.assertEqual(driver.actions[:3], [("TAP", "New Note", None), ("TYPE", "Note", "Garden 6"),
                                                      ("TYPE", "Note", "water the basil")])
                sources = [e.get("source") for e in events if e.get("event") == "frontier_decision"]
                self.assertEqual(sources[:3], ["program"] * 3)  # the three steps came from the program, not the model
                self.assertEqual(result["program"]["replayed"], 3)

    def test_a_replayed_step_that_sends_still_asks_on_an_mcp_or_remote_run(self):
        steps = [step("TAP", "Send", snapshot=COMPOSE, app=MAIL, commit="send")]
        request = "In Mail, send it."
        declared = [{"kind": "COMMIT", "app": "Mail", "what": "the message", "payload": "", "act": "other",
                     "count": "1", "condition": "", "quote": "send it"}]
        for origin in OUTSIDE:
            with self.subTest(origin=origin):
                gate = gate_for_run(override=DONT_ASK, origin=origin)
                self.assertEqual(gate.mode, ASK_IMPORTANT)
                asked = []
                driver = Driver([COMPOSE, SENT, SENT, SENT])
                result = FrontierAgent(driver, Model(), apps={MAIL: "Mail"}, emit=lambda e: None, screenshots=False,
                                       settle_seconds=0, permissions=PermissionGate(gate.mode, apps=policy.ALLOW_ALL),
                                       approve=lambda request_: asked.append(request_) or "denied",
                                       saved_program=matched(steps, {}, declared)).run(request)
                self.assertEqual([r["label"] for r in asked], ["Send"])  # put to the person
                self.assertNotIn(("TAP", "Send", None), driver.actions)  # and denied: nothing was sent
                self.assertEqual((result["status"], result["actions"]), ("approval_denied", 0))

    def test_a_replayed_step_that_only_navigates_runs_unasked_on_an_mcp_or_remote_run(self):
        for origin in OUTSIDE:
            with self.subTest(origin=origin):
                gate = gate_for_run(override=DONT_ASK, origin=origin)
                asked = []
                program = matched([step("TAP", "New Note", snapshot=LIST)], {}, READ_NOTES)
                driver = Driver([LIST, EDITOR, EDITOR, EDITOR])
                FrontierAgent(driver, Model(), apps={NOTES: "Notes"}, emit=lambda e: None, screenshots=False,
                              settle_seconds=0, permissions=PermissionGate(gate.mode, apps=policy.ALLOW_ALL),
                              approve=lambda request_: asked.append(request_) or "denied",
                              saved_program=program).run(SIBLING)
                self.assertEqual(asked, [])
                self.assertEqual(driver.actions[:1], [("TAP", "New Note", None)])

    def test_programs_off_turns_it_off_for_mcp_and_remote(self):
        for origin in OUTSIDE:
            with self.subTest(origin=origin):
                self.dir = Path(tempfile.mkdtemp())
                self.trusted(origin)
                with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "off"}):
                    self.assertEqual(P.mode(origin=origin), "off")
                    self.assertIsNone(PA.pre_run(self.ctx(SIBLING, origin=origin), None, None))
                    self.assertIsNone(PA.pre_run(self.ctx(GOAL, origin=origin), None, None))
                    other = Path(tempfile.mkdtemp())
                    self.dir, kept = other, self.dir
                    self.assertEqual(self.run_through("r3", GOAL, origin=origin).all(), [])
                    self.dir = kept
                with mock.patch.dict(os.environ, {"MOBSTER_PROGRAMS": "learn"}):
                    self.assertIsNone(PA.pre_run(self.ctx(SIBLING, origin=origin), None, None))

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import contract as K
from mobile_agent import engines, smart_events, smart_result
from mobile_agent.frontier import FrontierAgent
from mobile_agent.state import Element
from mobile_agent.tests.test_engines import COMPOSE, QUICK, Driver, Script, item, screen, sent

CLEAN = {k: v for k, v in os.environ.items() if not k.startswith(("MOBSTER_", "OPENAI", "TEXT_MODEL", "TYPESAFE"))}


class OutcomeTests(unittest.TestCase):
    def contract(self, *items):
        return K.Contract.from_items(list(items), "Text Sam and tell me the weather")

    def test_statuses_map_to_five_outcomes(self):
        proven = self.contract()
        self.assertEqual(smart_result.outcome_of("completed", proven, "x"), "done")
        self.assertEqual(smart_result.outcome_of("completed", None, "x"), "check")
        self.assertEqual(smart_result.outcome_of("max_steps", proven, "partial"), "check")
        self.assertEqual(smart_result.outcome_of("max_steps", proven, None), "couldnt_finish")
        self.assertEqual(smart_result.outcome_of("blocked", proven, "no"), "couldnt_finish")
        self.assertEqual(smart_result.outcome_of("approval_denied", proven, None), "declined")
        self.assertEqual(smart_result.outcome_of("stopped", proven, None), "stopped")
        self.assertEqual(smart_result.outcome_of("approval_timeout", proven, None), "stopped")

    def test_a_commit_proven_without_a_receipt_is_done(self):
        contract = self.contract(item("COMMIT", "Reminders", "reminder", "save", "Text Sam"))
        commit = contract.items[0]
        self.assertEqual(smart_events.item_state(contract, commit), "open")
        commit.claim = "done"
        contract.visited.add("Reminders")
        self.assertTrue(contract.proof(commit)[0])
        self.assertEqual(smart_events.item_state(contract, commit), "done")

    def test_a_message_is_never_proven_by_the_words_already_in_the_chat(self):
        contract = self.contract(item(what="message to Sam"))
        message = contract.items[0]
        contract.observe(sent("I'm running late"), "Messages", 1)  # last week's message, same words
        message.claim = "done"  # the model took it for this one: nothing was sent
        self.assertEqual(contract.proof(message), (False, "not done yet"))
        self.assertEqual(smart_events.item_state(contract, message), "open")

    def test_the_terminal_draws_a_markdown_answer(self):
        from rich.console import Console

        from mobile_agent.narrate import Outcome
        from mobile_agent.tui.render import outcome_renderable
        console = Console(width=80, color_system=None, record=True)
        data = "School starts Sept 2.\n\n- **Trips:** the museum\n\n| Date | Event |\n|---|---|\n| Sep 2 | First day |"
        console.print(outcome_renderable(Outcome("k", "completed", {"status": "completed", "outcome": "done",
                                                                    "answer": "School starts Sept 2.", "data": data})))
        text = console.export_text()
        self.assertIn("• Trips: the museum", text)
        self.assertIn("First day", text)
        self.assertNotIn("**", text)

    def test_a_smart_list_answer_is_a_headline_over_the_list(self):
        numbered = (
            "Here are your reminders for today:\n1. Buy oat milk.\n2. Call the dentist at 10.\n3. Water the plants.")
        result = smart_result.summarize({"status": "completed", "answer": numbered, "actions": 2, "steps": 3,
                                         "elapsed": 1.0}, None, None, request="What are my reminders today?")
        self.assertEqual(result["answer"], "Here are your reminders for today:")
        self.assertEqual(result["data"], numbered)

    def test_answers_read_like_the_steps_without_a_first_person_subject(self):
        drop = smart_result.without_first_person
        self.assertEqual(drop("I added and saved the reminder “Buy oat milk”."),
                         "Added and saved the reminder “Buy oat milk”.")
        self.assertEqual(drop("I couldn't find a reminder called Buy oat milk."),
                         "Couldn't find a reminder called Buy oat milk.")
        self.assertEqual(drop("I couldn’t find a reminder called Buy oat milk."),
                         "Couldn’t find a reminder called Buy oat milk.")
        self.assertEqual(drop("I found 3 unread emails."), "Found 3 unread emails.")
        self.assertEqual(drop("Added the reminder."), "Added the reminder.")
        self.assertEqual(drop("iOS 26.4 is installed."), "iOS 26.4 is installed.")
        self.assertEqual(drop("I-95 is clear."), "I-95 is clear.")
        self.assertEqual(drop("I'm not able to see the battery."), "I'm not able to see the battery.")
        self.assertEqual(drop(""), "")
        self.assertIsNone(drop(None))

    def test_only_the_one_line_answer_drops_it(self):
        # The run's answer and data are what the phone showed: the bench and exports read them verbatim.
        raw = "I added the reminder “Buy oat milk”. It is due today."
        outcome = {"status": "completed", "answer": raw, "steps": 4, "elapsed": 1.0}
        added = K.Contract.from_items([item("WRITE", "Reminders", "reminder", "none", "Add a reminder",
                                            "Buy oat milk")],
                                      "Add a reminder")
        summary = smart_result.summarize(outcome, SimpleNamespace(_contract=added), None, request="Add a reminder")
        self.assertEqual(summary["answer"], "Added the reminder “Buy oat milk”.")
        self.assertEqual(summary["data"], raw)
        note = "I left the keys under the mat. Text me when you land."
        summary = smart_result.summarize({"status": "completed", "answer": note, "steps": 2, "elapsed": 1.0},
                                    SimpleNamespace(_contract=None), None, request="Read my last note")
        self.assertEqual(summary["data"], note)

    def test_words_read_on_the_phone_keep_their_i(self):
        class Events:
            def __init__(self, *quotes):
                self.quotes = quotes

            def final_proof(self):
                return [{"quote": q, "app": "Notes", "screen": "Note", "frameId": None} for q in self.quotes]

        read = K.Contract.from_items([item("REPORT", "Notes", "the last note", "none", "Read my last note")],
                                     "Read my last note")
        note = "I left the keys under the mat. Text me when you land."
        done = {"status": "completed", "steps": 2, "elapsed": 1.0}
        summary = smart_result.summarize({**done, "answer": note}, SimpleNamespace(_contract=read),
                                    Events("I left the keys under the mat."), request="Read my last note")
        self.assertEqual(summary["answer"], "I left the keys under the mat.")
        self.assertEqual(summary["data"], note)
        for said in ("I made it home, thanks for dinner!", "I sent Dana the invoice?"):
            summary = smart_result.summarize({**done, "answer": said}, SimpleNamespace(_contract=read), Events(),
                                        request="Read my last message")
            self.assertEqual(summary["answer"], said)
        # A task that acts still drops it, unless the sentence is a quote the proof holds (a note it wrote).
        wrote = K.Contract.from_items([item("WRITE", "Notes", "note", "none", "Write a note", "I left the keys")],
                                      "Write a note")
        summary = smart_result.summarize({**done, "answer": "I wrote the note “I left the keys”."},
                                    SimpleNamespace(_contract=wrote), Events("I left the keys"), request="Write a note")
        self.assertEqual(summary["answer"], "Wrote the note “I left the keys”.")
        summary = smart_result.summarize({**done, "answer": "I left the keys under the mat."},
                                    SimpleNamespace(_contract=wrote), Events("I left the keys under the mat"),
                                    request="Write a note")
        self.assertEqual(summary["answer"], "I left the keys under the mat.")

    def test_a_schema_is_filled_from_the_answer_as_written(self):
        seen = []

        class Client:
            def complete(self, messages, schema, timeout=60):
                seen.append(messages[1]["content"][0]["text"])
                return {"data": {"where": "under the mat"}}, {}

        schema = {"type": "object", "required": ["where"], "properties": {"where": {"type": "string"}}}
        outcome = {"status": "completed", "answer": "I left the keys under the mat.", "steps": 2, "elapsed": 1.0}
        summary = smart_result.summarize(outcome, SimpleNamespace(_contract=None, client=Client()), None,
                                    request="Where are the keys?", output_schema=schema)
        self.assertIn("Answer: I left the keys under the mat.", seen[0])
        self.assertEqual(summary["full_answer"], "I left the keys under the mat.")

    def test_an_answer_that_wont_structure_falls_back_to_text_with_a_note(self):
        class Client:
            model = "gpt-5.6-sol"

            def __init__(self, reply):
                self.reply = reply

            def complete(self, messages, schema, timeout=60):
                if isinstance(self.reply, Exception):
                    raise self.reply
                return self.reply, {}

        answer = "You have no reminders due this week."
        outcome = {"status": "completed", "answer": answer, "steps": 3, "elapsed": 9.0}
        for reply in ({"columns": ["title", "title"], "rows": [], "single": False},
                      {"columns": ["title", "due"], "rows": [["only one value"]], "single": False},
                      {"columns": ["title"], "rows": [["a"], ["b"]], "single": True},
                      {"columns": [], "rows": [], "single": False},
                      RuntimeError('OpenAI HTTP 500: {"error": "server"}')):
            summary = smart_result.summarize(outcome, SimpleNamespace(_contract=None, client=Client(reply)), None,
                                        request="Export my reminders as CSV", output_format="csv")
            self.assertEqual((summary["data"], summary["resolved_output_format"], summary["data_status"]),
                             (answer, "text", "observed"), reply)
            self.assertEqual(summary["output_note"],
                             "Mobster couldn’t turn this answer into CSV, so it’s shown as text.")
            self.assertTrue(summary["structure_error"])
        # An empty list is a real answer: a table with its columns and no rows.
        summary = smart_result.summarize(outcome, SimpleNamespace(
            _contract=None, client=Client({"columns": ["title"], "rows": [], "single": False})), None,
            request="Export my reminders as CSV", output_format="csv")
        self.assertEqual((summary["data"], summary["resolved_output_format"]), ([], "csv"))

    def test_a_structuring_call_that_finds_no_credit_says_so(self):
        quota = RuntimeError('OpenAI HTTP 429: {"error": {"message": "You exceeded your current quota, please check '
                             'your plan and billing details. For more information on this error, read the docs: '
                             'https://platform.openai.com/docs/guides/error-codes/api-errors.", "type": '
                             '"insufficient_quota", "param": null, "code": "insufficient_quota"}}')
        self.assertIsNone(engines.model_failure(f"RuntimeError: {str(quota)[:160]}")[0])  # the old cut

        class Client:
            model = "gpt-5.6-sol"

            def complete(self, messages, schema, timeout=60):
                raise quota

        answer = "You have 2 reminders: Buy oat milk, Call the dentist."
        outcome = {"status": "completed", "answer": answer, "steps": 3, "elapsed": 9.0}
        summary = smart_result.summarize(outcome, SimpleNamespace(_contract=None, client=Client()), None,
                                    request="Export my reminders as CSV", output_format="csv")
        self.assertEqual(summary["structure_error"], "RuntimeError: OpenAI HTTP 429: insufficient_quota")
        self.assertEqual(engines.model_failure(summary["structure_error"]), (False, engines.NO_CREDIT))
        self.assertEqual(engines.plain_error(summary["structure_error"]), engines.NO_CREDIT)
        self.assertEqual((summary["data"], summary["resolved_output_format"]), (answer, "text"))
        self.assertEqual(summary["output_note"], "Your OpenAI account ran out of credit before Mobster could turn "
                                                 "this answer into CSV, so it’s shown as text.")
        # A schema's call says the same; a 402 with no code keeps its status.
        schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
        summary = smart_result.summarize(outcome, SimpleNamespace(_contract=None, client=Client()), None,
                                    request="How many reminders?", output_schema=schema)
        self.assertEqual(engines.model_failure(summary["structure_error"]), (False, engines.NO_CREDIT))
        self.assertIn("ran out of credit", summary["output_note"])
        self.assertEqual(smart_result.call_problem(RuntimeError("OpenAI HTTP 402: " + "x" * 400)),
                         "RuntimeError: OpenAI HTTP 402")
        # Any other failure is cut as before.
        self.assertEqual(smart_result.call_problem(RuntimeError("OpenAI HTTP 500: " + "x" * 400)),
                         ("RuntimeError: OpenAI HTTP 500: " + "x" * 400)[:160])

    def test_structured_output_is_validated_against_the_schema(self):
        schema = {"type": "object", "additionalProperties": False, "required": ["version"],
                  "properties": {"version": {"type": "string"}}}

        class Client:
            model = "gpt-5.6-sol"
            per_call_reasoning = per_call_strict = True

            def __init__(self, data):
                self.data, self.options = data, []

            def complete(self, messages, schema_, **options):
                self.options.append(options)
                return {"data": self.data}, {}

        good = Client({"version": "26.4"})
        self.assertEqual(smart_result.structure(good, "r", "iOS 26.4", [], schema), ({"version": "26.4"}, None))
        # The options travel with the call; the shared client is left as it was.
        self.assertEqual(good.options, [{"timeout": 60, "reasoning": "none", "strict": False}])
        self.assertEqual(vars(good).keys(), {"data", "options"})
        self.assertIsNone(smart_result.structure(Client({"version": 26}), "r", "x", [], schema)[0])

class CheckTheAnswerTests(unittest.TestCase):

    def run_smart(self, screens, steps, items, request, answer, updates=None):
        driver, script, out = Driver(screens), Script(steps, items, answer, updates), []
        events = smart_events.SmartEvents(out.append, driver=driver, frame=lambda: None,
                                     apps={"com.apple.mobilecal": "Calendar", "com.google.ios.youtube": "YouTube",
                                           "com.apple.Preferences": "Settings"})
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            agent = engines.build_frontier(driver, script, apps=events.apps, emit=events, config=QUICK, skills=())
            events.agent = agent
            result = agent.run(request)
            events.finish()
            return smart_result.summarize(result, agent, events, request=request), out

    def test_an_empty_day_answer_ends_done(self):
        day = json.loads((Path(__file__).parent / "fixtures" / "frontier" / "calendar_empty_day.json").read_text())
        rows = day["screens"]["day"]
        snapshot = screen(*[Element(r["id"], r["label"], r["role"], tuple(r["rect"])) for r in rows],
                          bundle="com.apple.mobilecal")
        summary, out = self.run_smart(
            [snapshot] * 3, [[("DONE", None, None)], [("DONE", None, None)]],
            [item("REPORT", "Calendar", "events tomorrow", "none", "What's on my calendar tomorrow")],
            "What's on my calendar tomorrow?", "No events tomorrow.",
            updates={0: [{"id": 1, "status": "found", "value": "No events tomorrow"}]})
        self.assertEqual((summary["status"], summary["outcome"]), ("completed", "done"))
        self.assertNotIn("chevron.forward", json.dumps(summary["proof"]))

    def test_a_search_typed_as_the_requested_write_is_not_already_done(self):
        c = K.Contract.from_items([item("WRITE", "YouTube", "search query", "none", "Enter 'pasta carbonara recipe'",
                                        "pasta carbonara recipe")], "Enter 'pasta carbonara recipe' in YouTube")
        search = Element("1", "Search YouTube", "SearchField", (.1, .1, .8, .05), editable=True)
        c.record_typed("YouTube", search, "pasta carbonara recipe", 1)
        self.assertEqual(smart_result.untouched_writes(c), [])
        other = K.Contract.from_items([item("WRITE", "Notes", "note body", "none", "Write a note", "buy milk")],
                                      "Write a note: buy milk")
        other.record_typed("Notes", search, "groceries", 1)
        self.assertEqual(len(smart_result.untouched_writes(other)), 1)  # an unrelated search is still not the write

    def test_a_value_is_never_quoted_from_an_sf_symbol_or_inside_a_word(self):
        c = K.Contract.from_items([item("REPORT", "Settings", "Bluetooth state", "none", "is Bluetooth on")],
                                  "In Settings, is Bluetooth on or off?")
        c.observe(screen(Element("1", "chevron.forward", "Image", (.9, .3, .05, .03)),
                         Element("2", "Bluetooth", "Button", (.05, .3, .8, .05), value="On")), "Settings", 0)
        c.update([{"id": 1, "status": "found", "value": "on"}], 0)
        row, _ = smart_events.SmartEvents._report_row(c, c.items[0])
        self.assertEqual(row, "Bluetooth On")
        c2 = K.Contract.from_items([item("REPORT", "Settings", "Bluetooth state", "none", "is Bluetooth on")], "x")
        c2.observe(screen(Element("1", "chevron.forward", "Image", (.9, .3, .05, .03))), "Settings", 0)
        c2.update([{"id": 1, "status": "found", "value": "on"}], 0)
        self.assertEqual(smart_events.SmartEvents._report_row(c2, c2.items[0]), (None, None))

    def test_blocked_always_carries_the_models_own_sentence(self):
        sentence = "The Wi-Fi settings need your passcode, which Mobster can't enter."

        class Blocking(Script):
            def complete(self, messages, schema, timeout=60, **kwargs):
                out, usage = super().complete(messages, schema, timeout)
                if "actions" in out:
                    out = {**out, "answer": None, "actions": [{**out["actions"][0], "text": sentence}]}
                return out, usage
        script = Blocking([[("BLOCKED", None, None)]], [])
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            result = FrontierAgent(Driver([COMPOSE] * 2), script, screenshots=False, settle_seconds=0,
                                   skills=()).run("Join the office Wi-Fi.")
        self.assertEqual((result["status"], result["answer"], result["reason"]), ("blocked", sentence, sentence))
        summary = smart_result.summarize(result, SimpleNamespace(client=script, _contract=None), None,
                                    request="Join the office Wi-Fi.")
        self.assertEqual(summary["reason"], sentence)

    def test_a_guard_stop_keeps_its_code_in_the_summary(self):
        outcome = {"status": "blocked", "answer": None, "reason": "Your iPhone was unplugged. Plug it back in and try "
                   "again.", "code": "unplugged", "steps": 1, "actions": 0, "elapsed": 1.0}
        summary = smart_result.summarize(outcome, SimpleNamespace(client=None, _contract=None), None, request="x")
        self.assertEqual((summary["code"], summary["reason"], summary["outcome"]),
                         ("unplugged", "Your iPhone was unplugged. Plug it back in and try again.", "couldnt_finish"))

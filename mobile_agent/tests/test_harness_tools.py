"""Track harness, tools (SPEC §3.1 F2, F7): ASK_USER (offered only where a person watches, at most twice, the answer
as feedback, no answer stops safely, never an approval) and the tool policy (order, availability, interactive-only,
a budget of 8, op validation, prompt lines only for tools offered). Offline, on fakes."""

import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import engines, harness_api
from mobile_agent.agent_hooks import Prepared, SkillResult
from mobile_agent.frontier import FrontierAgent, default_skills, system_prompt
from mobile_agent.harness import settings
from mobile_agent.harness.tools import MAX_TOOLS, offered
from mobile_agent.harness.tools.ask_user import NO_ANSWER, PROMPT, AskUser, factory, feedback_for, split
from mobile_agent.server import make_handler
from mobile_agent.state import Element
from mobile_agent.tests.seam_support import isolate, request
from mobile_agent.tests.seam_tasks import COMPOSE, SmartBase, Script, item
from mobile_agent.tests.test_frontier import Driver, Script as FrontierScript, screen
from mobile_agent.tests.test_server_engine import Base
from mobile_agent.tests.timing import bound


def context(origin="app", extras=None, runtime=None, clarify=lambda request: "answer:x"):
    return harness_api.RunContext(run_id="a" * 12, goal="Text Sam", engine="smart", origin=origin, app_bundle=None,
                                  device_id=None, device_kind="usb", extras=harness_api.frozen_mapping(extras or {}),
                                  data_dir=None, emit=lambda event: None, clarify=clarify,
                                  cancelled=lambda: False, runtime=runtime)


class Tool:
    """A registered tool for the policy tests."""

    def __init__(self, op, *, available=True, interactive_only=False, prompt=None):
        self.op, self.needs_target, self.interactive_only = op, False, interactive_only
        self.prompt = prompt if prompt is not None else f"- {op}: does a thing."
        self._available = available

    def available(self, ctx):
        if isinstance(self._available, Exception):
            raise self._available
        return self._available

    def prepare(self, ctx, act, snapshot):
        return Prepared(None)

    def perform(self, ctx, prepared, act, snapshot):
        return SkillResult("done")


class PolicyTests(unittest.TestCase):
    def test_defaults_pass_and_registered_tools_follow_the_rules_in_order(self):
        defaults = tuple(default_skills())
        tools = (Tool("READ_ATTACHMENT"), Tool("TAP"), Tool("READ_ATTACHMENT"), Tool("bad op"),
                 Tool("PUT_FILE", available=False), Tool("SAVE_SCREENSHOT", available=RuntimeError("x")),
                 Tool("ASK_ME", interactive_only=True), Tool("TWO_LINES", prompt="- TWO_LINES: a\nb"),
                 Tool("WRONG", prompt="- OTHER: not mine"), Tool("NO_DASH", prompt="NO_DASH: fine too"))
        kept = offered(defaults + tools, run_context=context("mcp"), driver=Driver([]))
        self.assertEqual([s.op for s in kept], [s.op for s in defaults] + ["READ_ATTACHMENT", "NO_DASH"])
        kept = offered(defaults + tools, run_context=context("app"), driver=Driver([]))
        self.assertIn("ASK_ME", [s.op for s in kept])

    def test_at_most_eight_registered_tools(self):
        tools = tuple(Tool("T_" + chr(65 + i) * 2) for i in range(MAX_TOOLS + 3))
        kept = offered(tuple(default_skills()) + tools, run_context=context(), driver=Driver([]))
        self.assertEqual([s.op for s in kept][len(default_skills()):], [t.op for t in tools[:MAX_TOOLS]])

    def test_a_tools_line_is_in_the_prompt_only_when_it_is_offered(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-000000000000"}):
            client = engines.build_client(key="sk-test-000000000000")
            watched = engines.build_frontier(Driver([]), client, apps={}, emit=lambda e: None,
                                             skills=tuple(default_skills()) + (AskUser(),), run_context=context("app"))
            unattended = engines.build_frontier(Driver([]), client, apps={}, emit=lambda e: None,
                                                skills=tuple(default_skills()) + (AskUser(),),
                                                run_context=context("workflow"))
        self.assertIn("ASK_USER", [s.op for s in watched.skills])
        self.assertIn(PROMPT, system_prompt(watched.skills))
        self.assertNotIn("ASK_USER", [s.op for s in unattended.skills])
        self.assertEqual(system_prompt(unattended.skills), system_prompt(default_skills()))


class AskUserUnitTests(unittest.TestCase):
    def test_choices_come_after_bars(self):
        self.assertEqual(split("Which Sam? | Sam Lee | Sam Ortiz"),
                         ("Which Sam?", [{"id": "c1", "label": "Sam Lee"}, {"id": "c2", "label": "Sam Ortiz"}]))
        self.assertEqual(split("Which Sam?"), ("Which Sam?", []))
        self.assertEqual(split("Which Sam? | Sam Lee"), ("Which Sam?", []))  # one choice is no choice
        self.assertEqual(len(split("Q? | " + " | ".join(f"o{i}" for i in range(9)))[1]), 6)

    def test_answers_become_feedback(self):
        choices = [{"id": "c1", "label": "Sam Lee"}]
        self.assertEqual(feedback_for("answer:the one from work", choices)[0],
                         "The user answered your question: 'the one from work'. Go on with that.")
        self.assertEqual(feedback_for("choice:c1", choices)[0], "The user answered your question: 'Sam Lee'. Go on with that.")
        self.assertEqual(feedback_for("timeout", choices), (NO_ANSWER, "approval_timeout"))
        self.assertEqual(feedback_for("stopped", choices)[1], "stopped")
        self.assertIn("chose not to answer", feedback_for("denied", choices)[0])

    def test_at_most_two_questions_and_never_without_an_asker(self):
        asked = []
        tool = AskUser()
        ctx = SimpleNamespace(clarify=lambda request: asked.append(request) or "answer:Lee")
        for _ in range(3):
            result = tool.perform(ctx, None, {"text": "Which Sam? | Sam Lee | Sam Ortiz"}, None)
        self.assertEqual(len(asked), 2)
        self.assertIn("already asked the user twice", result.feedback)
        self.assertEqual((asked[0]["kind"], asked[0]["operation"], asked[0]["label"], asked[0]["allow_text"]),
                         ("clarify", "ASK_USER", "Which Sam?", True))
        self.assertIn("can't ask", AskUser().perform(SimpleNamespace(clarify=None), None, {"text": "Q?"}, None).feedback)
        self.assertIn("needs the question", AskUser().perform(ctx, None, {"text": "  "}, None).feedback)

    def test_a_secret_never_goes_into_the_question(self):
        asked = []
        AskUser().perform(SimpleNamespace(clarify=lambda r: asked.append(r) or "denied"), None,
                          {"text": "Is the code 482913 the right one?"}, None)
        self.assertNotIn("482913", repr(asked))


class OfferTests(Base):
    def setUp(self):
        super().setUp()
        isolate(self)
        self.app = self.runtime()

    def test_offered_only_where_a_person_watches_and_the_setting_allows(self):
        shows = {"askUser": True}   # the surface can show a question
        for origin in ("app", "tui", "cli"):
            self.assertIsInstance(factory(context(origin, shows, runtime=self.app)), AskUser)
        for origin in ("mcp", "workflow", "schedule", "api"):
            self.assertIsNone(factory(context(origin, shows, runtime=self.app)))
        self.assertIsNone(factory(context("app", {"askUser": False}, runtime=self.app)))
        self.assertIsNone(factory(context("app", runtime=self.app)))   # a surface that can't show one never gets one
        settings.save(self.app, {"askUser": False})
        self.assertIsNone(factory(context("app", shows, runtime=self.app)))
        settings.save(self.app, {"askUser": True})
        with patch.dict(os.environ, {"MOBSTER_ASK_USER": "off"}):
            self.assertIsNone(factory(context("app", shows, runtime=self.app)))
        self.assertIsInstance(factory(context("app", shows, runtime=self.app)), AskUser)

    def test_the_setting_route(self):
        handler = make_handler(self.app)
        status, data, _ = request(handler, "GET", "/api/harness")
        self.assertEqual((status, data["askUser"], data["askUserLimit"], data["pauseLimitSeconds"]),
                         (200, True, 2, 600))
        self.assertEqual((data["ledger"], data["earlyLaunch"], data["longRun"]), (False, False, None))
        status, data, _ = request(handler, "POST", "/api/harness", {"askUser": False})
        self.assertEqual((status, data["askUser"]), (200, False))
        self.assertFalse(settings.ask_user(self.app))
        for bad in ({}, {"askUser": "no"}, {"askUser": True, "other": 1}):
            self.assertEqual(request(handler, "POST", "/api/harness", bad)[0], 400)


class FrontierQuestionTests(unittest.TestCase):
    def test_an_answer_never_approves_the_send_after_it(self):
        body = Element("1", "Message", "TextField", (.1, .8, .6, .05), editable=True,
                       actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        send = Element("2", "Send", "Button", (.8, .8, .1, .05))
        rows = [screen(body, send, bundle="com.apple.MobileSMS")] * 10
        approvals = []
        driver = Driver(rows)
        script = FrontierScript([("ASK_USER", None, "Which Sam? | Sam Lee | Sam Ortiz"),
                                 [("TYPE", "Message", "On my way")], ("TAP", "Send", None), ("DONE", None, None),
                                 ("DONE", None, None)])
        with patch.dict(os.environ, {"MOBSTER_FRONTIER_CONTRACT": "off"}):
            result = FrontierAgent(driver, script, screenshots=False, settle_seconds=0, skills=(AskUser(),),
                                   apps={"com.apple.MobileSMS": "Messages"},
                                   clarify=lambda request: "answer:yes send it",
                                   approve=lambda request: approvals.append(request) or "denied").run(
                "Send Sam a message that I'm on my way")
        self.assertEqual(len(approvals), 1)                      # the send still asked
        self.assertEqual(approvals[0]["label"], "Send")
        self.assertNotIn("Send", [a[1] for a in driver.actions])  # and was declined, so never tapped
        self.assertEqual(result["status"], "approval_denied")

    def test_waiting_for_an_answer_never_spends_the_tasks_time_and_no_answer_stops_safely(self):
        thing = screen(Element("1", "Thing", "StaticText", (.1, .3, .8, .05)))

        def slow(request):
            time.sleep(.4)
            return "timeout"
        events = []
        result = FrontierAgent(Driver([thing] * 4), FrontierScript([("ASK_USER", None, "Which one?")]),
                               emit=events.append, screenshots=False, settle_seconds=0, skills=(AskUser(),),
                               clarify=slow).run("Open the thing")
        self.assertEqual((result["status"], result["reason"]), ("approval_timeout", NO_ANSWER))
        self.assertLess(result["elapsed"], bound(.3))   # the .4 s wait isn't the task's time
        self.assertIn(("skill_finished", False), [(e["event"], e.get("ok")) for e in events
                                                  if e["event"] == "skill_finished"])


class SmartRunQuestionTests(SmartBase):
    def test_a_question_in_a_task_from_the_app_goes_through_the_run_and_its_answer_stays_out_of_the_journal(self):
        runtime = self.runtime()
        run = runtime.create("messages", "Text Sam I'm on my way", "live", origin="app", extras={"askUser": True})

        def answer():
            for _ in range(500):
                pending = run.public()["approval"]
                if pending:
                    run.answer_approval(pending["id"], True, choice="c2")
                    return
                time.sleep(.01)
        thread = threading.Thread(target=answer)
        thread.start()
        script = Script([[("ASK_USER", None, "Which Sam? | Sam Lee | Sam Ortiz")], [("DONE", None, None)],
                         [("DONE", None, None)]], [item(kind="READ", act="none", what="Sam", quote="Sam")],
                        answer="Asked.")
        self.work(runtime, run, script, screens=[COMPOSE] * 8)
        thread.join(5)
        asked = next(e for e in run.events if e["event"] == "approval_requested")
        self.assertEqual((asked["kind"], asked["operation"], asked["label"], asked["choices"]),
                         ("clarify", "ASK_USER", "Which Sam?", ["c1", "c2"]))
        self.assertIn("The user answered your question: 'Sam Ortiz'", script.prompts[1])
        resolved = next(e for e in run.events if e["event"] == "approval_resolved")
        self.assertEqual(resolved["decision"], "choice:c2")
        steps = [e["text"] for e in run.events if e["event"] == "step"]
        self.assertIn("Asked you a question", steps)

    def test_no_question_tool_where_nobody_can_answer_one(self):
        for origin, extras in (("api", {"askUser": True}), ("app", None)):
            with self.subTest(origin=origin, extras=extras):
                runtime = self.runtime()
                run = runtime.create("messages", "Read Sam's message", "live", origin=origin, extras=extras)
                script = self.script()
                systems = []
                complete = script.complete
                script.complete = lambda messages, schema, timeout=60, complete=complete, systems=systems: (
                    systems.append(messages[0]["content"]), complete(messages, schema, timeout))[1]
                self.work(runtime, run, script)
                self.assertTrue(systems)
                self.assertTrue(all("ASK_USER" not in system for system in systems))


if __name__ == "__main__":
    unittest.main()

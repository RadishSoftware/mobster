import json
import os
import threading
import time
import unittest
from unittest.mock import Mock

from mobile_agent import orchestrate
from mobile_agent.run import Run
from mobile_agent.runtime_work import smart_request
from mobile_agent.server import Runtime, make_handler
from mobile_agent.tests.seam_support import request
from mobile_agent.tests.test_server_engine import MESSAGES, Base
from mobile_agent.tests.test_server_hardening import APP

WHATSAPP = {"id": "whatsapp", "name": "WhatsApp", "bundleId": "net.whatsapp.WhatsApp", "installed": True}
INSTAGRAM = {"id": "instagram", "name": "Instagram", "bundleId": "com.burbn.instagram", "installed": True}
GONE = {"id": "signal", "name": "Signal", "bundleId": "org.whispersystems.signal", "installed": False}
PLAN = {"summary": "Read your messages in 3 apps and draft replies",
        "apps": [{"name": "Messages", "bundleId": "com.apple.MobileSMS", "does": "Read unread chats"},
                 {"name": "WhatsApp", "does": "Read unread chats"}, "Instagram"],
        "policy": ["Nothing sends until you OK it"], "readOnly": False}
DRAFTS = [{"id": "sam", "app": "Messages", "act": "send_message", "to": "Sam Lee", "title": "Reply to Sam",
           "text": "Yes, 7 works. See you there", "context": "Are we still on for 7?"},
          {"id": "ana", "app": "WhatsApp", "act": "send_message", "to": "Ana", "title": "Reply to Ana",
           "text": "Sending the photos tonight"},
          {"id": "jo", "app": "Instagram", "act": "send_message", "to": "jo.makes", "title": "Reply to Jo",
           "text": "Love it, where was this?"}]


def run_obj():
    return Run({"id": "messages", "name": "Messages", "bundleId": "com.apple.MobileSMS"}, "Go through my messages",
               "live")


def wait_for_approval(run):
    for _ in range(300):
        if run.public()["approval"]:
            return run.public()["approval"]
        time.sleep(.01)
    raise AssertionError("no approval appeared")


def in_background(call):
    answers = []
    thread = threading.Thread(target=lambda: answers.append(call()))
    thread.start()
    return thread, answers


def events(run, name):
    return [event for event in run.events if event["event"] == name]


class InterpretationTests(unittest.TestCase):
    def test_the_line_is_emitted_clipped_and_without_blocking(self):
        run = run_obj()
        run.interpret("Watching Reels for about 5 minutes, saving anything on AI, design or people you follow  ",
                      apps=["Instagram"])
        (event,) = events(run, "interpretation")
        self.assertEqual(event["text"], "Watching Reels for about 5 minutes, saving anything on AI, design or people "
                                        "you follow")
        self.assertEqual(event["apps"], [{"name": "Instagram", "bundleId": None}])
        self.assertTrue(event["readOnly"])
        run.interpret("x" * 400)
        self.assertEqual(len(events(run, "interpretation")[-1]["text"]), 160)

    def test_a_bad_shape_fails_in_the_engine_not_in_the_app(self):
        run = run_obj()
        for bad in ("", "   ", None, 7):
            with self.assertRaises(ValueError):
                run.interpret(bad)
        with self.assertRaises(ValueError):
            run.interpret("Reading", read_only="yes")
        self.assertEqual(events(run, "interpretation"), [])


class PlanTests(unittest.TestCase):
    def test_an_invalid_answer_cannot_grant_a_plan_cost_raise(self):
        run = run_obj()
        long_plan = {**PLAN, "long": {"budget": {"label": "As planned"}}}
        thread, answers = in_background(lambda: run.propose_plan(long_plan, timeout=5))
        pending = wait_for_approval(run)
        with self.assertRaises(ValueError):
            run.answer_approval(pending["id"], True, choice="missing", allow_cost=True)
        self.assertEqual(run.plan_terms, {})
        self.assertTrue(run.answer_approval(pending["id"], True))
        thread.join(3)
        self.assertEqual(answers, ["approved"])
        self.assertEqual(run.plan_terms, {})

    def test_an_edited_plan_starts_only_after_approval_and_exhausted_edits_stop(self):
        for answers, starts in ((["redirected:Skip Instagram", "approved"], True),
                                 (["redirected:Skip Instagram"] * 3, False)):
            with self.subTest(starts=starts):
                runtime = object.__new__(Runtime)
                runtime.plan_for = Mock(return_value=PLAN)
                run = run_obj()
                run.origin = "app"
                run.propose_plan = Mock(side_effect=answers)
                result = runtime.confirm_plan(run)
                self.assertEqual(result is None, starts)
                if starts:
                    self.assertEqual([message.text for message in run.steering.drain()], ["Skip Instagram"])
                    self.assertEqual(run.plan, PLAN)
                else:
                    self.assertEqual(result["status"], "approval_denied")
                    self.assertIsNone(run.plan)

    def test_start_skip_and_edit_answer_the_plan_card(self):
        for answer, expected in (((True,), "approved"), ((False,), "denied"),
                                 ((False, None, "Skip Instagram"), "redirected:Skip Instagram")):
            run = run_obj()
            thread, answers = in_background(lambda: run.propose_plan(PLAN, timeout=5))
            pending = wait_for_approval(run)
            self.assertEqual((pending["kind"], pending["operation"]), ("plan", "PLAN"))
            self.assertEqual([app["name"] for app in pending["plan"]["apps"]], ["Messages", "WhatsApp", "Instagram"])
            self.assertEqual(pending["plan"]["policy"], ["Nothing sends until you OK it"])
            self.assertTrue(run.answer_approval(pending["id"], *answer))
            thread.join(2)
            self.assertEqual(answers, [expected])
        (requested,) = events(run, "approval_requested")
        self.assertEqual(requested["plan"]["summary"], PLAN["summary"])
        self.assertEqual(events(run, "approval_resolved")[0]["decision"], "redirected")

    def test_no_answer_or_stop_never_starts_it(self):
        self.assertEqual(run_obj().propose_plan(PLAN, timeout=.05), "timeout")
        run = run_obj()
        thread, answers = in_background(lambda: run.propose_plan(PLAN, timeout=5))
        wait_for_approval(run)
        run.stop.set()
        with run.condition:
            run.condition.notify_all()
        thread.join(3)
        self.assertEqual(answers, ["stopped"])

    def test_a_malformed_plan_is_refused_before_anyone_is_asked(self):
        run = run_obj()
        for bad in ({}, {**PLAN, "apps": []}, {**PLAN, "apps": ["x"] * 9}, {**PLAN, "summary": ""},
                    {**PLAN, "policy": ["a", "b", "c", "d"]}, {**PLAN, "readOnly": "no"}, "plan"):
            with self.assertRaises(ValueError):
                run.propose_plan(bad, timeout=.05)
        self.assertEqual(run.events, [])


class ProposalTests(unittest.TestCase):
    def ask(self, run, items=DRAFTS, timeout=5):
        thread, answers = in_background(lambda: run.propose(items, timeout=timeout))
        return thread, answers, wait_for_approval(run)

    def test_expired_proposals_refuse_decisions_before_the_waiter_wakes(self):
        run = run_obj()
        thread, answers, pending = self.ask(run)
        with run.condition:
            run.approval["expiresAt"] = (time.time() - 1) * 1000
            self.assertFalse(run.decide_proposals(pending["id"], True, ["sam"]))
        thread.join(3)
        self.assertEqual(answers[0]["decision"], "timeout")
        self.assertTrue(all(item["decision"] == "skip" for item in answers[0]["items"].values()))

    def test_each_item_is_decided_on_its_own_and_only_sends_come_back_to_act_on(self):
        run = run_obj()
        thread, answers, pending = self.ask(run)
        self.assertEqual((pending["kind"], pending["title"], pending["act"], pending["app"]),
                         ("proposals", "Review 3 drafts", "send_message", "Several apps"))
        self.assertEqual(pending["items"][0]["text"], "Yes, 7 works. See you there")
        self.assertTrue(run.decide_proposals(pending["id"], True, ["sam"]))
        self.assertTrue(run.decide_proposals(pending["id"], True, ["ana"], text="Sending them tomorrow instead"))
        self.assertFalse(run.decide_proposals(pending["id"], False, ["sam"]))
        self.assertTrue(thread.is_alive())
        self.assertTrue(run.decide_proposals(pending["id"], False, ["jo"]))
        thread.join(2)
        (result,) = answers
        self.assertEqual(result["decision"], "decided")
        self.assertEqual(result["approvalId"], pending["id"])
        self.assertEqual(result["items"], {
            "sam": {"decision": "send", "text": "Yes, 7 works. See you there", "edited": False},
            "ana": {"decision": "send", "text": "Sending them tomorrow instead", "edited": True},
            "jo": {"decision": "skip", "text": "Love it, where was this?", "edited": False}})
        decided = [(e["item_id"], e["decision"], e["edited"]) for e in events(run, "proposal_decided")]
        self.assertEqual(decided, [("sam", "send", False), ("ana", "send", True), ("jo", "skip", False)])
        resolved = events(run, "approval_resolved")[-1]
        self.assertEqual((resolved["decision"], resolved["sent"], resolved["skipped"]), ("decided", 2, 1))
        self.assertLess(events(run, "proposal_decided")[-1]["seq"], resolved["seq"])

    def test_drafts_and_what_they_answer_never_enter_the_event_log(self):
        run = run_obj()
        thread, _, pending = self.ask(run)
        run.decide_proposals(pending["id"], True, ["sam"], text="A private edit")
        run.answer_approval(pending["id"], False)
        thread.join(2)
        logged = json.dumps(run.events)
        for secret in ("Yes, 7 works", "Are we still on", "A private edit", "Sending the photos", "where was this"):
            self.assertNotIn(secret, logged)
        self.assertEqual(events(run, "approval_requested")[0]["items"][0],
                         {"id": "sam", "app": "Messages", "bundleId": None, "act": "send_message", "to": "Sam Lee",
                          "title": "Reply to Sam"})

    def test_a_plain_yes_sends_the_rest_as_drafted_and_a_plain_no_skips_them(self):
        for approve, decision in ((True, "send"), (False, "skip")):
            run = run_obj()
            thread, answers, pending = self.ask(run)
            run.decide_proposals(pending["id"], not approve, ["sam"])
            self.assertTrue(run.answer_approval(pending["id"], approve))
            thread.join(2)
            items = answers[0]["items"]
            self.assertEqual(items["sam"]["decision"], "skip" if approve else "send")
            self.assertEqual({items["ana"]["decision"], items["jo"]["decision"]}, {decision})

    def test_nobody_answering_sends_only_what_was_chosen_and_stop_sends_nothing(self):
        run = run_obj()
        thread, answers, pending = self.ask(run, timeout=.4)
        run.decide_proposals(pending["id"], True, ["sam"])
        thread.join(3)
        self.assertEqual(answers[0]["decision"], "timeout")
        self.assertEqual({key: item["decision"] for key, item in answers[0]["items"].items()},
                         {"sam": "send", "ana": "skip", "jo": "skip"})
        run = run_obj()
        thread, answers, pending = self.ask(run)
        run.decide_proposals(pending["id"], True, ["sam", "ana"])
        run.stop.set()
        with run.condition:
            run.condition.notify_all()
        thread.join(3)
        self.assertEqual(answers[0]["decision"], "stopped")
        self.assertEqual({item["decision"] for item in answers[0]["items"].values()}, {"skip"})
        self.assertFalse(run.decide_proposals(pending["id"], True, ["jo"]))

    def test_wrong_answers_are_refused(self):
        run = run_obj()
        thread, _, pending = self.ask(run)
        with self.assertRaises(ValueError):
            run.decide_proposals(pending["id"], True, ["nobody"])
        with self.assertRaises(ValueError):
            run.decide_proposals(pending["id"], True, ["sam", "sam"])
        with self.assertRaises(ValueError):
            run.decide_proposals(pending["id"], False, ["sam"], text="an edit only sends")
        with self.assertRaises(ValueError):
            run.decide_proposals(pending["id"], True, ["sam", "ana"], text="one item only")
        with self.assertRaises(ValueError):
            run.answer_approval(pending["id"], False, instruction="say something else")
        self.assertFalse(run.decide_proposals("000000000000", True, ["sam"]))
        run.answer_approval(pending["id"], False)
        thread.join(2)
        thread, _ = in_background(lambda: run.request_approval({"operation": "TAP", "label": "Send"}, timeout=5))
        other = wait_for_approval(run)
        with self.assertRaises(ValueError):
            run.decide_proposals(other["id"], True, ["sam"])
        run.answer_approval(other["id"], False)
        thread.join(2)

    def test_a_result_is_said_per_item(self):
        run = run_obj()
        run.proposal_result("abc", "sam", "sent", "Sent to Sam Lee")
        self.assertEqual({k: v for k, v in events(run, "proposal_result")[0].items() if k not in ("seq", "timestamp")},
                         {"event": "proposal_result", "approval_id": "abc", "item_id": "sam", "status": "sent",
                          "line": "Sent to Sam Lee"})
        with self.assertRaises(ValueError):
            run.proposal_result("abc", "sam", "maybe")

    def test_a_malformed_batch_is_refused(self):
        for bad in ([], [{**DRAFTS[0], "id": "has space"}], [DRAFTS[0], DRAFTS[0]], [{**DRAFTS[0], "title": ""}],
                    [{**DRAFTS[0], "text": "x" * 4001}], [DRAFTS[0]] * 21):
            with self.assertRaises(ValueError):
                orchestrate.proposals(bad)


class TerminalTests(unittest.TestCase):
    def test_the_terminal_answers_a_plan_with_start_or_skip_and_drafts_all_at_once(self):
        from mobile_agent.narrate import verbs
        self.assertEqual(verbs({"kind": "plan"}), ("Start", "Skip"))
        self.assertEqual(verbs({"kind": "proposals", "act": "send_message"}), ("Send all", "Skip all"))
        self.assertEqual(verbs({"kind": "proposals", "act": None}), ("Approve all", "Skip all"))


class ProposalRouteTests(Base):
    def request(self, runtime, path, body, app_session=None, session=None):
        status, data, _ = request(make_handler(runtime, app_session=app_session), "POST", path, body,
                                  headers={"X-Mobster-App-Session": session} if session else None)
        return status, data

    def test_sending_needs_the_mac_app_and_skipping_does_not(self):
        runtime = self.runtime()
        run = run_obj()
        runtime.runs[run.id] = run
        thread, answers = in_background(lambda: run.propose(DRAFTS, timeout=5))
        pending = wait_for_approval(run)
        path = f"/api/runs/{run.id}/approval"
        secret = "s" * 32
        status, body = self.request(runtime, path, {"id": pending["id"], "approve": True, "items": ["sam"]},
                                    app_session=secret)
        self.assertEqual((status, body["code"]), (403, "app_only"))
        status, _ = self.request(runtime, path, {"id": pending["id"], "approve": False, "items": ["jo"]},
                                 app_session=secret)
        self.assertEqual(status, 200)
        status, _ = self.request(runtime, path, {"id": pending["id"], "approve": True, "items": ["sam"],
                                                 "text": "Edited in the app"}, app_session=secret, session=secret)
        self.assertEqual(status, 200)
        status, body = self.request(runtime, path, {"id": pending["id"], "approve": True, "items": ["jo"]},
                                    app_session=secret, session=secret)
        self.assertEqual(status, 409)
        status, _ = self.request(runtime, path, {"id": pending["id"], "approve": True, "items": "ana"},
                                 app_session=secret, session=secret)
        self.assertEqual(status, 400)
        status, _ = self.request(runtime, path, {"id": pending["id"], "approve": True}, app_session=secret,
                                 session=secret)
        self.assertEqual(status, 200)
        thread.join(2)
        self.assertEqual({key: item["decision"] for key, item in answers[0]["items"].items()},
                         {"sam": "send", "ana": "send", "jo": "skip"})
        self.assertEqual(answers[0]["items"]["sam"]["text"], "Edited in the app")


class SubrunTests(unittest.TestCase):
    def test_each_app_is_a_row_and_its_steps_carry_its_id(self):
        run = run_obj()
        planned = run.subruns_planned([{"id": "m", "app": "Messages", "title": "Read Messages"},
                                       {"id": "w", "app": "WhatsApp", "title": "Read WhatsApp"}])
        self.assertEqual(planned, ["m", "w"])
        run.subrun_start("Messages", "com.apple.MobileSMS", "Read Messages", subrun_id="m")
        run.emit({"event": "step", "text": "Opened Messages"})
        self.assertTrue(run.subrun_progress("m", "Read 12 chats", {"read": 12}))
        self.assertFalse(run.subrun_progress("m", "Read 13 chats", {"read": 13}))
        with self.assertRaises(ValueError):
            run.subrun_start("WhatsApp", subrun_id="w")
        run.subrun_finish("m", "done", "3 need a reply", {"read": 12, "drafted": 3})
        run.emit({"event": "step", "text": "Opened WhatsApp"})
        names = [(e["event"], e.get("subrun")) for e in run.events]
        self.assertEqual(names, [("subruns_planned", None), ("subrun_started", "m"), ("step", "m"),
                                 ("subrun_progress", "m"), ("subrun_finished", "m"), ("step", None)])
        self.assertEqual(events(run, "subrun_finished")[0]["counts"], {"read": 12, "drafted": 3})

    def test_bad_sub_runs_are_refused(self):
        run = run_obj()
        with self.assertRaises(ValueError):
            run.subrun_finish("nope")
        with self.assertRaises(ValueError):
            run.subrun_progress("nope", "line")
        sub = run.subrun_start("Messages")
        with self.assertRaises(ValueError):
            run.subrun_finish(sub, "great")
        with self.assertRaises(ValueError):
            run.subrun_progress(sub, "line", {"Bad Key": 1})
        with self.assertRaises(ValueError):
            run.subrun_start("Messages", run_id="not-a-run")


class PinnedAppTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        os.environ["TYPESAFE_API_KEY"] = "jev-key-000000000000"

    def test_a_pinned_app_is_where_the_task_starts_and_several_are_named_to_smart(self):
        runtime = self.runtime(apps=(APP, MESSAGES, WHATSAPP, INSTAGRAM))
        run = runtime.create(None, "Go through my messages", "live", engine="smart",
                             pinned_apps=["messages", "whatsapp", "instagram"])
        self.assertEqual(run.app["id"], "messages")
        self.assertEqual([pin["name"] for pin in run.pinned_apps], ["Messages", "WhatsApp", "Instagram"])
        self.assertEqual(run.metadata()["pinnedApps"][1], {"id": "whatsapp", "name": "WhatsApp",
                                                           "bundleId": "net.whatsapp.WhatsApp"})
        self.assertEqual(smart_request(run), "In Messages, WhatsApp and Instagram: Go through my messages")
        self.assertEqual(list(runtime.smart_apps(run))[:3],
                         ["com.apple.MobileSMS", "net.whatsapp.WhatsApp", "com.burbn.instagram"])
        self.finish(runtime, run)
        one = runtime.create(None, "Text Sam hi", "live", engine="smart", pinned_apps=["whatsapp"])
        self.assertEqual(smart_request(one), "In WhatsApp: Text Sam hi")

    def test_pins_must_be_apps_on_the_phone(self):
        runtime = self.runtime(apps=(APP, MESSAGES, GONE))
        for bad in (["nope"], [], ["messages", "messages"], "messages", ["a", "b", "c", "d", "e", "f"]):
            with self.assertRaises(ValueError):
                runtime.create(None, "Read my chats", "live", engine="smart", pinned_apps=bad)
        with self.assertRaisesRegex(ValueError, "Signal isn't installed"):
            runtime.create(None, "Read my chats", "live", engine="smart", pinned_apps=["signal"])
        self.assertEqual(runtime.runs, {})

    def test_the_api_takes_pinned_apps(self):
        runtime = self.runtime(apps=(APP, MESSAGES, WHATSAPP))
        status, body, _ = super().request(runtime, "POST", "/api/runs", {"goal": "Read my chats", "engine": "smart",
                                                                         "pinnedApps": ["whatsapp", "messages"]})
        self.assertEqual(status, 201)
        self.assertEqual((body["run"]["appId"], [pin["id"] for pin in body["run"]["pinnedApps"]]),
                         ("whatsapp", ["whatsapp", "messages"]))


if __name__ == "__main__":
    unittest.main()

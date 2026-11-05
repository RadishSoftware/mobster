import os
import threading
import time
from unittest.mock import patch

from mobile_agent import server
from mobile_agent.permissions import policy
from mobile_agent.permissions.exceptions import key_of
from mobile_agent.tests.seam_support import request
from mobile_agent.tests.seam_tasks import COMPOSE, Script, SmartBase, item, sent

MESSAGES = {"id": "messages", "name": "Messages", "bundleId": "com.apple.MobileSMS", "installed": True}
NOTES = {"id": "notes", "name": "Notes", "bundleId": "com.apple.mobilenotes", "installed": True}
BOTH = "Read Sam's message in Messages and save it in Notes"
READ = [item(kind="READ", act="none", what="Sam", quote="Sam")]


class Launching(Script):

    def complete(self, messages, schema, timeout=60):
        out, usage = super().complete(messages, schema, timeout)
        for action in out.get("actions") or ():
            if action["operation"] == "LAUNCH_APP":
                action["app"], action["text"] = action["text"], None
        return out, usage


def opens_notes():
    return Launching([[("LAUNCH_APP", None, NOTES["bundleId"])], [("DONE", None, None)], [("DONE", None, None)]],
                     READ)


def app_cards(seen):
    return [card for card in seen if card.get("act") == "use app"]


def launched(phone, bundle):
    return any(bundle in str(call) for call in phone.calls) or any(
        op == "LAUNCH_APP" and bundle in str(target) for op, target, _ in phone.actions)


def started(run):
    return "app_launch_started" in [event.get("event") for event in run.events]


class AppApprovalRunTests(SmartBase):
    def setUp(self):
        super().setUp()
        access = patch.dict(os.environ, {"MOBSTER_APP_ACCESS": "ask"})
        access.start()
        self.addCleanup(access.stop)

    def answering(self, run, answer, remember=False):
        seen = []

        def loop():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                pending = run.approval
                if pending and pending["id"] not in {card["id"] for card in seen}:
                    seen.append(dict(pending))
                    if pending.get("act") == "use app":
                        yes = answer(pending) if callable(answer) else answer
                        run.answer_approval(pending["id"], yes, remember=bool(remember and yes))
                    else:
                        run.answer_approval(pending["id"], True)
                time.sleep(.01)
        threading.Thread(target=loop, daemon=True).start()
        return seen

    def kept(self, runtime, bundle):
        return runtime.permission_exceptions().contains(bundle, policy.APP_LABEL, policy.APP_OP)

    def test_every_planned_app_is_shown_before_any_is_allowed_or_remembered(self):
        planned = [{"id": f"app{i}", "bundleId": f"com.example.app{i}",
                    "name": f"App {i} with a long name for reviewing a task across several apps", "installed": True}
                   for i in range(8)]
        apps = [MESSAGES, *planned]
        names, bundles = [a["name"] for a in apps], [a["bundleId"] for a in apps]
        for approve in (True, False):
            with self.subTest(approve=approve):
                runtime = self.runtime(apps=apps)
                run = runtime.create("messages", "Read Sam's message", "live", origin="app")
                run.plan = {"apps": planned}
                seen = self.answering(run, approve, remember=approve)
                phone = self.work(runtime, run, self.script())
                cards = app_cards(seen)
                self.assertEqual(len(cards), 1)
                self.assertEqual(cards[0]["apps"], names)
                self.assertEqual(cards[0]["appIds"], bundles)
                kept = {entry["app"] for entry in runtime.permission_exceptions().public()}
                self.assertEqual(kept, set(bundles) if approve else set())
                if approve:
                    self.assertTrue(started(run))
                    self.assertEqual(len(app_cards(seen)), 1)
                else:
                    self.assertEqual(run.summary["stop_code"], "blocked_app")
                    self.assertFalse(started(run))
                    self.assertEqual(phone.actions, [])

    def test_one_card_covers_every_app_the_task_names_and_both_open_without_a_second(self):
        runtime = self.runtime(apps=(MESSAGES, NOTES))
        run = runtime.create("messages", BOTH, "live", origin="app")
        seen = self.answering(run, True)
        phone = self.work(runtime, run, opens_notes())
        cards = app_cards(seen)
        self.assertEqual([(c["title"], c["apps"], c["appIds"]) for c in cards],
                         [("Allow Mobster to use Messages and Notes?", ["Messages", "Notes"],
                           [MESSAGES["bundleId"], NOTES["bundleId"]])])
        self.assertEqual((cards[0]["rememberable"], cards[0]["rememberLabel"]), (True, "Always allow"))
        self.assertTrue(started(run))
        self.assertTrue(launched(phone, NOTES["bundleId"]), (phone.calls, phone.actions))
        self.assertNotEqual(run.summary.get("stop_code"), "blocked_app")
        self.assertFalse(self.kept(runtime, MESSAGES["bundleId"]) or self.kept(runtime, NOTES["bundleId"]))
        again = runtime.create("messages", BOTH, "live", origin="app")
        seen = self.answering(again, True, remember=True)
        self.work(runtime, again, self.script())
        self.assertEqual([c["apps"] for c in app_cards(seen)], [["Messages", "Notes"]])
        self.assertTrue(self.kept(runtime, MESSAGES["bundleId"]) and self.kept(runtime, NOTES["bundleId"]))
        third = runtime.create("messages", BOTH, "live", origin="app")
        seen = self.answering(third, True)
        self.work(runtime, third, opens_notes())
        self.assertEqual(app_cards(seen), [])
        runtime.permission_exceptions().remove(key_of(NOTES["bundleId"], policy.APP_LABEL, policy.APP_OP))
        fourth = runtime.create("messages", BOTH, "live", origin="app")
        seen = self.answering(fourth, True)
        self.work(runtime, fourth, self.script())
        self.assertEqual([c["apps"] for c in app_cards(seen)], [["Notes"]])

    def test_an_app_the_task_opens_that_wasnt_on_the_card_asks_and_not_now_keeps_it_shut(self):
        runtime = self.runtime(apps=(MESSAGES, NOTES))
        run = runtime.create("messages", "Read Sam's message", "live", origin="app")
        seen = self.answering(run, lambda card: card["apps"] == ["Messages"])
        phone = self.work(runtime, run, opens_notes())
        self.assertEqual([c["title"] for c in app_cards(seen)],
                         ["Allow Mobster to use Messages?", "Allow Mobster to use Notes?"])
        self.assertFalse(launched(phone, NOTES["bundleId"]), (phone.calls, phone.actions))
        self.assertEqual(run.summary["stop_code"], "blocked_app")

    def test_not_now_on_a_card_for_several_apps_opens_none_of_them(self):
        runtime = self.runtime(apps=(MESSAGES, NOTES))
        run = runtime.create("messages", BOTH, "live", origin="app")
        seen = self.answering(run, False)
        phone = self.work(runtime, run, opens_notes())
        self.assertEqual([c["apps"] for c in app_cards(seen)], [["Messages", "Notes"]])
        self.assertEqual(run.summary["stop_code"], "blocked_app")
        self.assertIn("needs your OK", run.summary["reason"])
        self.assertFalse(started(run))
        self.assertFalse(launched(phone, NOTES["bundleId"]) or launched(phone, MESSAGES["bundleId"]))
        self.assertEqual(phone.actions, [])
        self.assertFalse(self.kept(runtime, MESSAGES["bundleId"]) or self.kept(runtime, NOTES["bundleId"]))

    def test_an_app_only_the_plan_names_joins_the_card_and_still_needs_its_ok(self):
        runtime = self.runtime(apps=(MESSAGES, NOTES))
        plan = {"apps": [{"name": "Notes", "bundleId": NOTES["bundleId"]}]}
        run = runtime.create("messages", "Read Sam's message", "live", origin="app")
        run.plan = plan
        seen = self.answering(run, True)
        phone = self.work(runtime, run, opens_notes())
        self.assertEqual([c["apps"] for c in app_cards(seen)], [["Messages", "Notes"]])
        self.assertTrue(launched(phone, NOTES["bundleId"]), (phone.calls, phone.actions))
        declined = runtime.create("messages", "Read Sam's message", "live", origin="app")
        declined.plan = plan
        seen = self.answering(declined, False)
        phone = self.work(runtime, declined, opens_notes())
        self.assertEqual([c["apps"] for c in app_cards(seen)], [["Messages", "Notes"]])
        self.assertEqual(declined.summary["stop_code"], "blocked_app")
        self.assertEqual(phone.actions, [])
        with patch.dict(os.environ, {"MOBSTER_BLOCKED_APPS": NOTES["bundleId"]}):
            blocked = runtime.create("messages", "Read Sam's message", "live", origin="app")
            blocked.plan = plan
            seen = self.answering(blocked, True)
            phone = self.work(runtime, blocked, opens_notes())
        self.assertEqual(app_cards(seen), [])
        self.assertEqual(blocked.summary["stop_code"], "blocked_app")
        self.assertIn("Notes is on your list of apps Mobster never opens", blocked.summary["reason"])
        self.assertEqual(phone.actions, [])
        self.assertFalse(self.kept(runtime, NOTES["bundleId"]))

    def test_nobody_to_answer_keeps_the_app_shut(self):
        runtime = self.runtime()
        quiet = runtime.create("messages", "Read Sam's message", "live", origin="app")
        with patch("mobile_agent.run.APPROVAL_TIMEOUT_SECONDS", .2):
            phone = self.work(runtime, quiet, self.script())
        self.assertEqual(quiet.summary["stop_code"], "blocked_app")
        self.assertFalse(started(quiet))
        self.assertEqual(phone.actions, [])
        self.assertFalse(self.kept(runtime, MESSAGES["bundleId"]))

    def test_a_send_in_an_app_the_card_allowed_still_asks(self):
        runtime = self.runtime()
        run = runtime.create("messages", "Text Sam I'm running late", "live", origin="app")
        seen = self.answering(run, True, remember=True)
        script = Script([[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)], [("DONE", None, None)]],
                        [item()], answer="Sent Sam “I'm running late”.")
        phone = self.work(runtime, run, script, screens=[COMPOSE, COMPOSE, sent("I'm running late"),
                                                         sent("I'm running late")])
        self.assertEqual([(c["act"], c["kind"]) for c in seen], [("use app", "commit"), ("send_message", "commit")])
        self.assertEqual(phone.actions, [("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)])
        self.assertEqual([(entry["app"], entry["op"]) for entry in runtime.permission_exceptions().public()],
                         [(MESSAGES["bundleId"], policy.APP_OP)])

    def test_without_the_mac_apps_session_the_card_cant_be_answered_and_nothing_opens(self):
        runtime = self.runtime(apps=(MESSAGES, NOTES))
        handler = server.make_handler(runtime, "t0ken", app_session="s3ssion")
        token = {"Authorization": "Bearer t0ken"}
        run = runtime.create("messages", BOTH, "live", origin="app")
        statuses = []

        def answer_from_outside_the_app():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not statuses:
                pending = run.approval
                if pending and pending.get("act") == "use app":
                    status, data, _ = request(handler, "POST", f"/api/runs/{run.id}/approval",
                                              {"id": pending["id"], "approve": True, "remember": True}, headers=token)
                    statuses.append((status, data.get("code")))
                elif pending and pending.get("kind") == "plan":
                    run.answer_approval(pending["id"], True)
                time.sleep(.01)
        threading.Thread(target=answer_from_outside_the_app, daemon=True).start()
        with patch("mobile_agent.run.APPROVAL_TIMEOUT_SECONDS", 1):
            phone = self.work(runtime, run, self.script())
        self.assertEqual(statuses, [(403, "app_only")])
        self.assertEqual(run.summary["stop_code"], "blocked_app")
        self.assertEqual(phone.actions, [])
        self.assertFalse(self.kept(runtime, MESSAGES["bundleId"]) or self.kept(runtime, NOTES["bundleId"]))
        run = runtime.create("messages", BOTH, "live", origin="app")
        answered = []

        def answer_in_the_app():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                pending = run.approval
                if pending and pending["id"] not in answered:
                    answered.append(pending["id"])
                    body = {"id": pending["id"], "approve": True,
                            **({"remember": True} if pending.get("act") == "use app" else {})}
                    status, _, _ = request(handler, "POST", f"/api/runs/{run.id}/approval", body,
                                           headers={**token, "X-Mobster-App-Session": "s3ssion"})
                    answered.append(status)
                time.sleep(.01)
        threading.Thread(target=answer_in_the_app, daemon=True).start()
        self.work(runtime, run, self.script())
        self.assertNotIn(403, answered)
        self.assertTrue(started(run))
        self.assertTrue(self.kept(runtime, MESSAGES["bundleId"]) and self.kept(runtime, NOTES["bundleId"]))

    def test_an_outside_agents_task_gets_the_same_card_and_its_off_apps_stay_refused(self):
        runtime = self.runtime(apps=(MESSAGES, NOTES))
        run = runtime.create("messages", BOTH, "live", origin="mcp")
        seen = self.answering(run, True)
        self.work(runtime, run, self.script())
        self.assertEqual([c["apps"] for c in app_cards(seen)], [["Messages", "Notes"]])
        self.assertTrue(started(run))
        self.assertFalse(self.kept(runtime, NOTES["bundleId"]))
        with patch.dict(os.environ, {"MOBSTER_OUTSIDE_BLOCKED_APPS": NOTES["bundleId"]}):
            off = runtime.create("messages", BOTH, "live", origin="mcp")
            seen = self.answering(off, True)
            phone = self.work(runtime, off, opens_notes())
        self.assertEqual(app_cards(seen), [])
        self.assertEqual(phone.actions, [])
        self.assertFalse(launched(phone, NOTES["bundleId"]), (phone.calls, phone.actions))
        self.assertEqual(off.summary["stop_code"], "blocked_app")
        self.assertIn("off for agents outside Mobster", off.summary["reason"])

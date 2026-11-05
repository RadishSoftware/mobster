import ast
import os
import pathlib
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from mobile_agent import permissions, server
from mobile_agent.permissions import (
    ASK_ALL,
    ASK_IMPORTANT,
    DONT_ASK,
    Action,
    AppPolicy,
    PermissionGate,
    PermissionGuard,
    decision_for,
    outside,
    policy,
    rules,
)
from mobile_agent.state import Element

OPEN = policy.ALLOW_ALL
MODES = (ASK_ALL, ASK_IMPORTANT, DONT_ASK)
BANK = "com.chase"


def tap(label, role="Button", **fields):
    return Action("TAP", label, role, **fields)


def typed(label, text, role="TextField", **fields):
    return Action("TYPE", label, role, text=text, **fields)


class ConventionTests(unittest.TestCase):

    def decide(self, mode, action, act=None, **gate):
        return PermissionGate(mode, apps=OPEN, **gate).decide(action, existing_act=act).decision

    def test_reading_and_moving_around_an_allowed_app_never_ask_in_any_mode(self):
        moves = [tap("Spending insights", "Cell", app=BANK, app_name="Chase", screen="Accounts"),
                 tap("Plafonds", "Cell", app="com.boursorama.mobile", screen="Ma carte"),
                 tap("入出金明細", "Cell", app="jp.co.smbc.direct", screen="口座"),
                 tap("Location Services", "Cell", app="com.apple.Preferences", screen="Privacy & Security"),
                 tap("Privacy & Security", "Cell", app="com.apple.Preferences", screen="Settings"),
                 tap("Transactions", "Tab", app=BANK), Action("BACK", app=BANK), Action("SWIPE_UP", app=BANK),
                 Action("TYPE_SUBMIT", "Search", "SearchField", text="coffee", app=BANK),
                 typed("Message", "Running late", app="com.apple.MobileSMS")]
        for mode in MODES:
            for action in moves:
                self.assertEqual(self.decide(mode, action), "allow", (mode, action.op, action.label))

    def test_anything_with_an_effect_outside_the_phone_asks_in_the_default_mode(self):
        for act in ("send_message", "react", "share", "call", "follow", "order", "book", "delete", "pay", "other"):
            action = tap("Do it", app="com.example.app")
            self.assertEqual(self.decide(ASK_IMPORTANT, action, act), "ask", act)
            self.assertEqual(self.decide(ASK_ALL, action, act), "ask", act)
            self.assertEqual(self.decide(DONT_ASK, action, act), "allow", act)

    def test_a_change_that_stays_on_the_phone_asks_only_before_every_change(self):
        for action in (tap("Wi-Fi", "Switch", app="com.apple.Preferences", value="1", goal="Turn off Wi-Fi"),
                       tap("Save", app="com.apple.mobilenotes"), tap("Add to list", app="com.apple.reminders"),
                       tap("Mark as Read", app="com.apple.mobilemail"),
                       Action("SUBMIT", "Title", "TextField", app="com.apple.reminders")):
            self.assertEqual(self.decide(ASK_ALL, action), "ask", action.label)
            self.assertEqual(self.decide(ASK_IMPORTANT, action), "allow", action.label)
            self.assertEqual(self.decide(DONT_ASK, action), "allow", action.label)
        self.assertEqual(self.decide(ASK_IMPORTANT, tap("Wi-Fi", "Switch", app="com.apple.Preferences", value="1",
                                                        goal="What's my Wi-Fi network?")), "ask")
        self.assertEqual(self.decide(ASK_IMPORTANT, tap("Find My iPhone", "Switch", app="com.apple.Preferences",
                                                        value="1", goal="Turn off Find My iPhone")), "ask")
        for label in ("New Message", "Edit", "Settings", "Filters"):
            self.assertEqual(self.decide(ASK_ALL, tap(label, app="com.apple.MobileSMS")), "allow", label)

    def test_a_one_time_code_asks_in_every_mode(self):
        codes = [typed("Verification code", "482913", app="com.example.bank"),
                 typed("Enter code", "48 29 13", app="com.example.bank"),
                 typed("", "482913", app="com.example.shop", context="Enter the 6-digit code we sent to •••• 42"),
                 typed("Code", "4829", app="com.example.shop"),
                 typed("認証コード", "482913", app="jp.example.app"),
                 Action("TYPE_SUBMIT", "One-time code", "TextField", text="482913", app="com.example.bank")]
        for mode in MODES:
            for action in codes:
                decision = PermissionGate(mode, apps=OPEN).decide(action)
                self.assertEqual((decision.decision, decision.act, decision.title),
                                 ("ask", "code", "Enter this code?"), (mode, action.label))
                self.assertFalse(decision.rememberable)
        for action in (typed("Quantity", "2", app="com.example.shop"), typed("Search", "482913", "SearchField"),
                       typed("Phone", "4155550123", app="com.apple.MobileAddressBook")):
            self.assertEqual(self.decide(DONT_ASK, action), "allow", action.label)
            self.assertEqual(self.decide(ASK_ALL, action), "allow", action.label)

    def test_what_each_mode_asks(self):
        table = {
            ASK_ALL: {"navigate": "allow", "input": "allow", "change": "ask", "commit": "ask", "floor": "ask"},
            ASK_IMPORTANT: {"navigate": "allow", "input": "allow", "change": "allow", "commit": "ask", "floor": "ask"},
            DONT_ASK: {"navigate": "allow", "input": "allow", "change": "allow", "commit": "allow", "floor": "ask"},
        }
        for mode, expected in table.items():
            for kind_, answer in expected.items():
                self.assertEqual(decision_for(kind_, mode), answer, (mode, kind_))

    def test_a_point_tap_on_a_named_delete_button_reads_as_a_commit(self):
        from mobile_agent.permissions import consequence
        found = consequence.read(Action("TAP_XY", "Delete", "Button"))
        self.assertEqual(found.act, "delete")
        self.assertEqual(self.decide(ASK_IMPORTANT, Action("TAP_XY", "Delete", "Button")), "ask")

    def beside(self, label, value="On my way", placeholder="iMessage", rect=(.82, .88, .1, .05),
               app="com.apple.MobileSMS"):
        composer = Element("c", "iMessage", "TextView", (.12, .88, .66, .05), editable=True, value=value,
                           placeholder=placeholder)
        button = Element("b", label, "Button", rect)
        return Action("TAP", label, "Button", app=app, rect=rect, around=(composer, button))

    def test_an_unnamed_send_beside_a_written_message_asks(self):
        from mobile_agent.permissions import consequence
        for label in ("Up", "Direct", "arrow.up"):
            found = consequence.read(self.beside(label), self.beside(label).around)
            self.assertEqual((found.act, found.signal), ("send", "beside_composer"), label)
            self.assertEqual(self.decide(ASK_IMPORTANT, self.beside(label)), "ask", label)

    def test_the_composers_own_tools_an_empty_box_and_other_lines_still_go_ahead(self):
        from mobile_agent.permissions import consequence
        for action in (self.beside("Camera"), self.beside("Voice Message"), self.beside("Emoji"),
                       self.beside("Up", value=""), self.beside("Up", value="iMessage"),
                       self.beside("Up", rect=(.82, .2, .1, .05)), self.beside("Up", rect=(.01, .88, .1, .05))):
            self.assertIsNone(consequence.read(action, action.around), action)

    def test_the_labels_say_the_modes_in_these_terms(self):
        self.assertEqual(policy.MODE_LABELS, {ASK_ALL: "Ask before every change", ASK_IMPORTANT: "Ask before acting",
                                              DONT_ASK: "Full access"})
        for count in (1, 2):
            self.assertLessEqual(len(policy.app_card_reason(count)), 120)
            self.assertLessEqual(len(policy.task_card_reason(count)), 120)

class AskSwitchTests(unittest.TestCase):
    def test_the_older_switch_maps_to_a_mode(self):
        self.assertEqual(policy.mode_for_ask(False, ASK_ALL), DONT_ASK)
        self.assertEqual(policy.mode_for_ask(True, ASK_ALL), ASK_ALL)
        self.assertEqual(policy.mode_for_ask(True, DONT_ASK), ASK_IMPORTANT)
        self.assertEqual(policy.mode_for_ask(True, None), ASK_IMPORTANT)


class AllowRuleTests(unittest.TestCase):

    def setUp(self):
        import tempfile

        from mobile_agent.permissions.exceptions import Exceptions
        self.store = Exceptions(os.path.join(tempfile.mkdtemp(), "permission-exceptions.json"))

    def gate(self, mode=ASK_IMPORTANT, origin=None):
        return PermissionGate(mode, apps=OPEN, exceptions=self.store, origin=origin)

    def test_a_rule_lets_that_kind_of_action_through_in_that_app_only(self):
        send = tap("Send", app="com.apple.MobileSMS", app_name="Messages")
        first = self.gate().decide(send, existing_act="send_message")
        self.assertEqual((first.decision, first.rememberable, first.rule), ("ask", True, "send"))
        spec = PermissionGate.remember(first, "com.apple.MobileSMS", "Messages")
        self.assertEqual((spec["label"], spec["op"]), ("send", rules.RULE_OP))
        self.assertEqual(spec["words"], "Don’t ask again for sending messages in Messages")
        self.store.add(spec["app"], spec["label"], spec["op"], spec["app_name"])
        for label, act in (("Send", "send_message"), ("Reply", "reply"), ("arrow.up.circle.fill", "send")):
            self.assertEqual(self.gate().decide(tap(label, app="com.apple.MobileSMS"), existing_act=act).decision,
                             "allow", label)
        self.assertEqual(self.gate().decide(tap("Send", app="net.whatsapp.WhatsApp"), "send_message").decision, "ask")
        self.assertEqual(self.gate().decide(tap("Tapback", app="com.apple.MobileSMS"), "react").decision, "ask")
        remote = self.gate(origin="remote").decide(send, existing_act="send_message")
        self.assertEqual((remote.decision, remote.rememberable), ("ask", False))
        self.assertTrue(self.store.remove(self.store.public()[0]["key"]))
        self.assertEqual(self.gate().decide(send, existing_act="send_message").decision, "ask")

    def test_never_for_paying_buying_booking_deleting_or_an_act_it_cannot_name(self):
        for act in ("pay", "order", "transfer", "request_money", "book", "delete", "install", "cancel", "other",
                    "submit", "confirm"):
            decision = self.gate().decide(tap("Go", app="com.example.shop"), existing_act=act)
            self.assertEqual(decision.decision, "ask", act)
            self.assertFalse(decision.rememberable, act)
            self.assertIsNone(PermissionGate.remember(decision, "com.example.shop", "Shop"), act)
            self.store.add("com.example.shop", act, rules.RULE_OP)
            self.assertEqual(self.gate().decide(tap("Go", app="com.example.shop"), existing_act=act).decision, "ask")

    def test_a_change_rule_holds_in_ask_before_every_change(self):
        save = tap("Save", app="com.apple.mobilenotes")
        first = self.gate(ASK_ALL).decide(save)
        self.assertEqual((first.decision, first.rule), ("ask", "save"))
        self.store.add("com.apple.mobilenotes", "save", rules.RULE_OP, "Notes")
        self.assertEqual(self.gate(ASK_ALL).decide(save).decision, "allow")

    def test_safety_lists_a_rule_by_its_kind(self):
        from mobile_agent.permissions.register import _rule
        item = self.store.add("com.apple.MobileSMS", "react", rules.RULE_OP, "Messages")
        self.assertEqual(_rule(item)["words"], "Reactions")
        link = self.store.add("com.culturedcode.ThingsiPhone", "Links", "OPEN_URL", "Things")
        self.assertNotIn("words", _rule(link))


class FrontierHookTests(unittest.TestCase):
    REQ = "Message Leo in QuickChat that dinner is at 7."

    def run_agent(self, mode, steps, screens, items=None):
        from mobile_agent.frontier import FrontierAgent
        from mobile_agent.tests.test_contract import AgentLoopTests, Driver, Script
        asked = []

        def approve(request):
            asked.append(request)
            return "approved"
        driver = Driver(screens)
        script = Script(steps, AgentLoopTests.ITEMS if items is None else items)
        gate = PermissionGate(mode, apps=OPEN)
        hooks = {"approve": None, "floor_approve": approve} if mode == DONT_ASK else {"approve": approve}
        result = FrontierAgent(driver, script, apps={"com.example.chat": "QuickChat"}, emit=lambda e: None,
                               screenshots=False, settle_seconds=0, permissions=gate, **hooks).run(self.REQ)
        return driver, asked, result, script

    def chat(self, typed_text=""):
        from mobile_agent.tests.test_contract import screen
        return screen(Element("1", "Send", "Button", (.8, .9, .1, .04)),
                      Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True,
                              value=typed_text or "Message", placeholder="Message"),
                      Element("4", "Leo", "NavigationBar", (0, 0, 1, .06)))

    def test_ask_before_acting_asks_before_sending_and_offers_an_allow_rule(self):
        sent = self.chat("Dinner is at 7")
        from mobile_agent.tests.test_contract import screen
        done = screen(Element("3", "Dinner is at 7", "StaticText", (.1, .5, .6, .04)),
                      Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value="Message"))
        driver, asked, result, _ = self.run_agent(ASK_IMPORTANT, [[("TYPE", "Message", "Dinner is at 7"),
                                                                  ("TAP", "Send", None)], [("DONE", None, None)]],
                                                  [self.chat(), sent, done, done])
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0]["label"], "Send")
        self.assertEqual(asked[0]["remember"]["words"], "Don’t ask again for sending messages in QuickChat")
        self.assertIn(("TAP", "Send", None), driver.actions)

    def test_full_access_sends_without_asking(self):
        sent = self.chat("Dinner is at 7")
        driver, asked, _, _ = self.run_agent(DONT_ASK, [[("TYPE", "Message", "Dinner is at 7"), ("TAP", "Send", None)],
                                                       [("DONE", None, None)]], [self.chat(), sent, sent, sent])
        self.assertEqual(asked, [])
        self.assertIn(("TAP", "Send", None), driver.actions)

    def test_full_access_still_asks_before_typing_a_one_time_code(self):
        from mobile_agent.tests.test_contract import screen
        login = screen(Element("5", "Verification code", "TextField", (.1, .5, .8, .05), editable=True),
                       Element("6", "Enter the 6-digit code we sent you", "StaticText", (.1, .4, .8, .05)))
        driver, asked, _, _ = self.run_agent(DONT_ASK, [[("TYPE", "Verification code", "482913")],
                                                       [("DONE", None, None)]], [login] * 3, items=[])
        self.assertEqual([(r["title"], r["act"]) for r in asked], [("Enter this code?", "code")])
        self.assertIsNone(asked[0]["remember"])


class AllowlistTests(unittest.TestCase):
    def test_outside_agents_use_the_same_apps_unless_the_person_turns_one_off_for_them(self):
        apps = AppPolicy(outside_blocked=frozenset({"co.hinge.mobile.ios"}))
        self.assertTrue(apps.allows("com.burbn.instagram", outside=True))
        self.assertFalse(apps.allows("co.hinge.mobile.ios", outside=True))
        self.assertTrue(apps.allows("co.hinge.mobile.ios"))
        self.assertIn("off for agents outside Mobster", apps.blocked_message("Hinge", outside=True))
        self.assertEqual(apps.status("co.hinge.mobile.ios")["outside"], False)
        current = policy.Settings(apps=apps)
        saved = policy.changes_for({"outsideOn": "co.hinge.mobile.ios", "outsideOff": "com.burbn.instagram"}, current)
        self.assertEqual(saved, {"MOBSTER_OUTSIDE_BLOCKED_APPS": "com.burbn.instagram"})
        self.assertEqual(policy.from_env({"MOBSTER_OUTSIDE_BLOCKED_APPS": "com.burbn.instagram"}).apps.outside_blocked,
                         frozenset({"com.burbn.instagram"}))

    def test_every_app_asks_the_first_time_alike(self):
        import tempfile

        from mobile_agent.permissions.exceptions import Exceptions
        grants = Exceptions(os.path.join(tempfile.mkdtemp(), "permission-exceptions.json"))
        apps = AppPolicy()
        for bundle, name in (("com.chase", "Chase"), ("com.apple.Passbook", "Wallet"), ("com.x.y", "1Password"),
                             ("com.burbn.instagram", "Instagram")):
            self.assertTrue(apps.allows(bundle), name)
            self.assertFalse(apps.approved(bundle, grants), name)
            self.assertEqual(apps.status(bundle, grants=grants)["why"], "asks")
        grants.add("com.chase", policy.APP_LABEL, policy.APP_OP, "Chase")
        self.assertTrue(apps.approved("com.chase", grants))
        self.assertTrue(apps.approved("com.apple.springboard"))
        self.assertTrue(AppPolicy(access="all").approved("com.burbn.instagram"))
        self.assertTrue(AppPolicy(allowed=frozenset({"com.apple.MobileSMS"})).approved("com.apple.MobileSMS"))
        never = AppPolicy(access="all", blocked=frozenset({"com.burbn.instagram"}))
        self.assertFalse(never.allows("com.burbn.instagram"))
        self.assertFalse(never.approved("com.burbn.instagram"))
        with patch.object(policy, "DEFAULT_ACCESS", "ask"):
            self.assertEqual(policy.from_env({}).apps.access, "ask")
        self.assertEqual(policy.from_env({"MOBSTER_APP_ACCESS": "only"}).apps.access, "ask")

    def test_allow_covers_a_conversations_follow_ups_but_not_a_new_conversation_or_an_app_switched_off(self):
        asked = []

        def ask(request):
            asked.append(request["title"])
            return "approved"
        names = {"com.apple.Preferences": "Settings", "com.burbn.instagram": "Instagram"}
        chat = set()
        first = PermissionGuard(None, AppPolicy(), names, ask=ask, conversation=chat)
        self.assertTrue(first.allows_app("com.apple.Preferences"))
        follow_up = PermissionGuard(None, AppPolicy(), names, ask=ask, conversation=chat)
        self.assertTrue(follow_up.allows_app("com.apple.Preferences"))
        self.assertEqual(asked, ["Allow Mobster to use Settings?"])
        self.assertEqual(first._conversation, True)
        self.assertIn("for this conversation", policy.task_card_reason(1, True))
        self.assertTrue(PermissionGuard(None, AppPolicy(), names, ask=ask, conversation=set())
                        .allows_app("com.apple.Preferences"))
        self.assertTrue(PermissionGuard(None, AppPolicy(), names, ask=ask).allows_app("com.apple.Preferences"))
        self.assertEqual(len(asked), 3)
        off = AppPolicy(access="all", blocked=frozenset({"com.apple.Preferences"}))
        self.assertFalse(PermissionGuard(None, off, names, ask=ask,
                                         conversation=chat).allows_app("com.apple.Preferences"))
        self.assertEqual(len(asked), 3)

    def test_allow_covers_the_task_and_always_allow_is_remembered_until_switched_off(self):
        import tempfile

        from mobile_agent.permissions import remember_in
        from mobile_agent.permissions.exceptions import Exceptions, key_of
        grants = Exceptions(os.path.join(tempfile.mkdtemp(), "permission-exceptions.json"))
        asked, answers = [], ["approved"]

        def ask(request):
            asked.append(request)
            answer = answers.pop(0) if answers else "denied"
            if answer == "always":
                remember_in(grants, request["remember"])
                return "approved"
            return answer
        names = {"com.culturedcode.ThingsiPhone": "Things", "com.apple.mobilenotes": "Notes"}
        guard = PermissionGuard(None, AppPolicy(), names, ask=ask, grants=grants)
        self.assertTrue(guard.allows_app("com.culturedcode.ThingsiPhone"))
        self.assertEqual((asked[0]["title"], asked[0]["act"]), ("Allow Mobster to use Things?", "use app"))
        self.assertEqual(asked[0]["remember"]["words"], "Always allow")
        self.assertEqual(asked[0]["bundles"], ["com.culturedcode.ThingsiPhone"])
        self.assertTrue(guard.allows_app("com.culturedcode.ThingsiPhone"))
        self.assertEqual(len(asked), 1)
        self.assertFalse(grants.contains("com.culturedcode.ThingsiPhone", policy.APP_LABEL, policy.APP_OP))
        answers.append("always")
        self.assertTrue(PermissionGuard(None, AppPolicy(), names, ask=ask, grants=grants)
                        .allows_app("com.culturedcode.ThingsiPhone"))
        self.assertEqual(len(asked), 2)
        self.assertTrue(grants.contains("com.culturedcode.ThingsiPhone", policy.APP_LABEL, policy.APP_OP))
        self.assertTrue(PermissionGuard(None, AppPolicy(), names, ask=ask, grants=grants)
                        .allows_app("com.culturedcode.ThingsiPhone"))
        self.assertEqual(len(asked), 2)
        grants.remove(key_of("com.culturedcode.ThingsiPhone", policy.APP_LABEL, policy.APP_OP))
        again = PermissionGuard(None, AppPolicy(), names, ask=ask, grants=grants)
        self.assertFalse(again.allows_app("com.culturedcode.ThingsiPhone"))
        self.assertEqual(len(asked), 3)
        self.assertFalse(again.allows_app("com.culturedcode.ThingsiPhone"))
        self.assertEqual(len(asked), 3)
        self.assertIn("needs your OK", again.blocked_message("com.culturedcode.ThingsiPhone"))
        fresh = Exceptions(os.path.join(tempfile.mkdtemp(), "permission-exceptions.json"))
        self.assertFalse(PermissionGuard(None, AppPolicy(), names, ask=None, grants=fresh)
                         .allows_app("com.culturedcode.ThingsiPhone"))

    def test_one_card_covers_a_tasks_apps_and_a_declined_card_opens_none(self):
        import tempfile

        from mobile_agent.permissions import remember_in
        from mobile_agent.permissions.exceptions import Exceptions
        names = {"com.culturedcode.ThingsiPhone": "Things", "com.apple.mobilenotes": "Notes",
                 "com.apple.mobilecal": "Calendar"}
        both = ["com.culturedcode.ThingsiPhone", "com.apple.mobilenotes"]
        grants = Exceptions(os.path.join(tempfile.mkdtemp(), "permission-exceptions.json"))
        asked = []
        guard = PermissionGuard(None, AppPolicy(), names, grants=grants,
                                ask=lambda request: asked.append(request) or "approved")
        self.assertTrue(guard.request(both))
        self.assertEqual([(r["title"], r["apps"], r["bundles"]) for r in asked],
                         [("Allow Mobster to use Things and Notes?", ["Things", "Notes"], both)])
        self.assertTrue(guard.allows_app("com.culturedcode.ThingsiPhone"))
        self.assertTrue(guard.allows_app("com.apple.mobilenotes"))
        self.assertEqual(len(asked), 1)
        self.assertFalse(any(grants.contains(b, policy.APP_LABEL, policy.APP_OP) for b in both))
        self.assertTrue(guard.allows_app("com.apple.mobilecal"))
        self.assertEqual([r["title"] for r in asked][1:], ["Allow Mobster to use Calendar?"])
        remember_in(grants, asked[0]["remember"])
        self.assertTrue(all(grants.contains(b, policy.APP_LABEL, policy.APP_OP) for b in both))
        fresh = Exceptions(os.path.join(tempfile.mkdtemp(), "permission-exceptions.json"))
        declined, said = PermissionGuard(None, AppPolicy(), names, grants=fresh,
                                         ask=lambda request: said.append(request) or "denied"), []
        self.assertFalse(declined.request(both))
        self.assertFalse(declined.allows_app("com.culturedcode.ThingsiPhone"))
        self.assertFalse(declined.allows_app("com.apple.mobilenotes"))
        self.assertEqual(len(said), 1)
        self.assertFalse(any(fresh.contains(b, policy.APP_LABEL, policy.APP_OP) for b in both))
        broken = Exceptions(os.path.join(tempfile.mkdtemp(), "permission-exceptions.json"))
        broken.contains = lambda *a, **k: (_ for _ in ()).throw(OSError("unreadable"))
        kept = PermissionGuard(None, AppPolicy(), names, grants=broken, ask=lambda request: "approved")
        self.assertTrue(kept.request(both))
        self.assertTrue(kept.allows_app("com.apple.mobilenotes"))

        class Inner:
            def allows_app(self, bundle):
                return bundle != "com.denied.by.inner"
        count = len(asked)
        never = PermissionGuard(Inner(), AppPolicy(access="all", blocked=frozenset({"com.burbn.instagram"})),
                                ask=lambda request: asked.append(request) or "approved", grants=grants)
        self.assertFalse(never.allows_app("com.burbn.instagram"))
        self.assertFalse(never.allows_app("com.denied.by.inner"))
        self.assertTrue(never.allows_app("com.apple.MobileSMS"))
        self.assertEqual(len(asked), count)
        self.assertIn("never opens", never.blocked_message("com.burbn.instagram"))


class SettingsTests(unittest.TestCase):
    def test_a_full_block_list_never_silently_drops_a_new_refusal(self):
        bundles = [f"com.example.app{index}" for index in range(policy.MAX_LISTED)]
        current = policy.Settings(apps=AppPolicy(blocked=frozenset(bundles), outside_blocked=frozenset(bundles)))
        for body in ({"block": "com.example.extra"}, {"outsideOff": "com.example.extra"}):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "at most 300"):
                policy.changes_for(body, current)
        saved = policy.changes_for({"unblock": bundles[0], "block": "com.example.extra"}, current)
        self.assertIn("com.example.extra", saved[policy.ENV_BLOCKED].split(","))
        self.assertNotIn(bundles[0], saved[policy.ENV_BLOCKED].split(","))

    def test_the_older_switch_reads_as_a_mode(self):
        self.assertEqual(policy.from_env({}).mode, ASK_IMPORTANT)
        self.assertEqual(policy.from_env({"MOBSTER_ASK_BEFORE_ACTING": "0"}).mode, DONT_ASK)
        self.assertEqual(policy.from_env({"MOBSTER_PERMISSION_MODE": "ask_all",
                                          "MOBSTER_ASK_BEFORE_ACTING": "0"}).mode, ASK_ALL)

    def test_changes_validate_and_keep_the_older_switch_in_step(self):
        current = policy.from_env({})
        self.assertEqual(policy.changes_for({"permissionMode": "dont_ask"}, current),
                         {"MOBSTER_PERMISSION_MODE": "dont_ask", "MOBSTER_ASK_BEFORE_ACTING": "0"})
        saved = policy.changes_for({"block": "com.burbn.instagram"}, current)
        self.assertEqual(saved["MOBSTER_BLOCKED_APPS"], "com.burbn.instagram")
        self.assertEqual(policy.changes_for({"appAccess": "ask"}, current), {"MOBSTER_APP_ACCESS": "ask"})
        for bad in ({"permissionMode": "yolo"}, {"appAccess": "only"}, {"appAccess": "all"},
                    {"blockedApps": ["not a bundle"]}, {"allow": "com.chase"}, {}):
            with self.assertRaises(ValueError):
                policy.changes_for(bad, current)

    def test_an_outside_agents_task_never_runs_below_ask_important(self):
        self.assertEqual(policy.effective_mode(DONT_ASK, None, "mcp"), ASK_IMPORTANT)
        self.assertEqual(policy.effective_mode(DONT_ASK, None, "app"), DONT_ASK)
        self.assertEqual(policy.effective_mode(ASK_IMPORTANT, ASK_ALL, "app"), ASK_ALL)
        self.assertTrue(policy.loosens(ASK_IMPORTANT, DONT_ASK))
        self.assertFalse(policy.loosens(ASK_IMPORTANT, ASK_ALL))

    def test_the_mcp_process_reads_the_apps_settings_file(self):
        import tempfile
        from pathlib import Path
        folder = Path(tempfile.mkdtemp())
        path = folder / "agent.env"
        path.write_text("MOBSTER_PERMISSION_MODE=ask_all\nMOBSTER_BLOCKED_APPS=com.burbn.instagram\n")
        with patch.dict(os.environ, {}, clear=True):
            settings = policy.from_file(path)
        self.assertEqual(settings.mode, ASK_ALL)
        self.assertFalse(settings.apps.allows("com.burbn.instagram"))


class OutsideTests(unittest.TestCase):
    def test_an_outside_approval_waits_for_the_person_and_dedupes_retries(self):
        registry = outside.Registry()
        body = {"key": "k1", "request": {"label": "Send", "title": "Send this message?"}}
        made = registry.create(body)
        again = registry.create(body)
        self.assertEqual(made["id"], again["id"])
        threading.Timer(.05, lambda: registry.answer(made["id"], True)).start()
        self.assertEqual(registry.get(made["id"], wait=2)["state"], "approved")
        self.assertFalse(registry.answer(made["id"], False))

    def test_reusing_a_pending_key_for_another_action_is_refused(self):
        registry = outside.Registry()
        body = {"key": "k1", "device": "d", "request": {"label": "Send", "text": "Meet at 7"}}
        made = registry.create(body)
        with self.assertRaises(ValueError):
            registry.create({**body, "request": {**body["request"], "text": "Meet at 8"}})
        self.assertEqual(registry.pending()[1], [made])
        self.assertTrue(registry.answer(made["id"], True))

    def test_changed_actions_do_not_share_a_pending_approval(self):
        send = tap("Send", app="com.apple.MobileSMS", screen="Sam", typed="Meet at 7")
        self.assertNotEqual(outside.key_for("d", send), outside.key_for(
            "d", tap("Send", app=send.app, screen="Sam", typed="Meet at 8")))
        self.assertNotEqual(outside.key_for("d", send), outside.key_for(
            "d", tap("Send", app=send.app, screen="Kate", typed=send.typed)))
        self.assertNotEqual(outside.key_for("d", tap("Send", text="Button\u241fhi")),
                            outside.key_for("d", tap("Send\u241fButton", text="hi")))

    def test_send_approval_shows_the_composer_text(self):
        send = tap("Send", app="com.apple.MobileSMS", screen="Sam", typed="Meet at 7")
        with patch.object(outside, "ask_app", return_value={"state": "approved"}) as ask:
            self.assertTrue(outside.check_action(send, device="d", settings=policy.Settings(ASK_IMPORTANT, OPEN)))
        self.assertEqual(ask.call_args.args[0]["text"], "Meet at 7")

    def test_an_unanswered_approval_expires(self):
        now = [1000.0]
        registry = outside.Registry(clock=lambda: now[0])
        made = registry.create({"request": {"label": "Send"}})
        now[0] += outside.EXPIRES_S + 1
        self.assertEqual(registry.get(made["id"])["state"], "timeout")
        self.assertEqual(registry.pending()[1], [])

    def test_only_max_pending_approvals_wait_at_once(self):
        registry = outside.Registry(clock=lambda: 1000.0)
        for number in range(outside.MAX_PENDING):
            registry.create({"request": {"label": f"Send {number}"}})
        with self.assertRaisesRegex(ValueError, "Too many approvals are waiting"):
            registry.create({"request": {"label": "One more"}})
        first = registry.pending()[1][0]["id"]
        self.assertTrue(registry.answer(first, False))
        registry.create({"request": {"label": "One more"}})

    def test_an_answered_approval_is_kept_for_keep_s_and_then_dropped(self):
        now = [1000.0]
        registry = outside.Registry(clock=lambda: now[0])
        made = registry.create({"request": {"label": "Send"}})
        self.assertTrue(registry.answer(made["id"], True))
        now[0] += outside.KEEP_S - 1
        self.assertEqual(registry.get(made["id"])["state"], "approved")
        now[0] += 2
        self.assertIsNone(registry.get(made["id"]))

    def test_check_action_allows_asks_and_refuses(self):
        settings = policy.Settings(ASK_IMPORTANT, OPEN)
        self.assertFalse(outside.check_action(tap("Search", app="com.apple.MobileSMS"), device="d",
                                              settings=settings))
        with self.assertRaises(outside.Refused) as blocked:
            outside.check_action(tap("Send", app="com.chase", app_name="Chase"), device="d",
                                 settings=policy.Settings(ASK_IMPORTANT, AppPolicy(
                                     outside_blocked=frozenset({"com.chase"}))))
        self.assertIn("Chase", str(blocked.exception))

        class App:
            def __init__(self, state):
                self.state = state

            def call(self, method, path, body=None, timeout=None):
                return {"approval": {"id": "a" * 12, "state": self.state, "expiresAt": time.time() * 1000 + 6e5,
                                     "instruction": "say 7pm"}}

        send = tap("Send", app="com.apple.MobileSMS", screen="Sam", typed="hi")
        self.assertTrue(outside.check_action(send, device="d", settings=settings,
                                             connect=lambda: App("approved")))
        for state, words in (("denied", "declined"), ("redirected", "say 7pm"), ("pending", "same call again"),
                             ("timeout", "10 minutes")):
            with self.assertRaises(outside.Refused) as refused:
                outside.check_action(send, device="d", settings=settings, connect=lambda: App(state),
                                     wait_s=0)
            self.assertIn(words, str(refused.exception), state)
            self.assertEqual(refused.exception.state, state)

    def test_the_card_and_the_replies_name_the_app_the_way_the_person_knows_it(self):
        import tempfile
        from pathlib import Path

        from mobile_agent.installed_apps import Inventory, InventoryCache
        udid = "00000000-00000000ABCD0002"
        chrome, bank = "com.google.chrome.ios", "com.example.bank"
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "installed-apps"
            InventoryCache(folder).save(Inventory(udid, ({"bundleId": bank, "name": "Example Bank", "version": "1"},
                                                         {"bundleId": chrome, "name": chrome, "version": "1"}),
                                                  frozenset(), 0.0, 1_791_000_000.0))
            self.assertEqual(outside.app_name(bank, udid, folder), "Example Bank")
            self.assertEqual(outside.app_name(chrome, udid, folder), "Chrome")
            self.assertEqual(outside.app_name(chrome, None, folder), "Chrome")
            self.assertEqual(outside.app_name("com.example.unknown", udid, folder), "com.example.unknown")
            self.assertEqual(outside.app_name("com.apple.mobilesafari", udid, Path(root) / "none"), "Safari")

    def test_a_card_for_a_browser_asks_to_use_chrome_not_its_bundle_id(self):
        import json
        import tempfile
        from pathlib import Path

        from mobile_agent.permissions.exceptions import Exceptions
        sent = []

        class App:
            def call(self, method, path, body=None, timeout=None):
                sent.append(body)
                return {"approval": {"id": "a" * 12, "state": "pending", "expiresAt": time.time() * 1000 + 6e5}}

        with tempfile.TemporaryDirectory() as root, self.assertRaises(outside.Refused) as waiting:
            outside.check_app("com.google.chrome.ios", device="d", settings=policy.Settings(ASK_IMPORTANT, AppPolicy()),
                              connect=lambda: App(), wait_s=0, grants=Exceptions(Path(root) / "exceptions.json"))
        self.assertEqual(waiting.exception.state, "pending")
        asked = json.dumps(sent)
        self.assertIn("Allow Mobster to use Chrome?", asked)
        self.assertNotIn("com.google.chrome.ios", asked)
        self.assertIn("Chrome", str(waiting.exception))


class QueryFieldTests(unittest.TestCase):

    SAFARI, SETTINGS, INSTAGRAM = "com.apple.mobilesafari", "com.apple.Preferences", "com.burbn.instagram"
    MESSAGES, NOTES, CONTACTS = "com.apple.MobileSMS", "com.apple.mobilenotes", "com.apple.MobileAddressBook"

    class Asked(Exception):
        """The card was raised: the approval connection was opened."""

    def field(self, label, role="TextField", rect=(.05, .92, .7, .05), eid=None, **fields):
        return Element(eid or f"f-{label}", label, role, rect, editable=True, **fields)

    def verdict(self, op, bundle, target, others=()):
        from mobile_agent.permissions import action_from
        from mobile_agent.state import Snapshot
        snapshot = Snapshot([target, *others], "", 1, 1, "wda", bundle_id=bundle)

        def connect():
            raise self.Asked()
        try:
            outside.check_action(action_from(op, target, snapshot), device="d", connect=connect, wait_s=0,
                                 settings=policy.Settings(ASK_IMPORTANT, OPEN))
        except self.Asked:
            return "asks"
        except outside.Refused as refused:
            return f"refused: {refused}"
        return "acts"

    def clear(self, bundle, field, rect=(.78, .07, .1, .05), label="Clear", others=()):
        return self.verdict("TAP", bundle, Element("c", label, "Button", rect), [field, *others])

    def test_return_in_a_search_field_or_an_address_bar_acts_without_a_card(self):
        for name, bundle, field in (
                ("Safari's address bar", self.SAFARI, self.field("Address", placeholder="Search or enter website")),
                ("Safari's address bar with no placeholder", self.SAFARI, self.field("Address")),
                ("Settings' search", self.SETTINGS, self.field("Search", "SearchField", (.05, .1, .9, .05))),
                ("Instagram's search", self.INSTAGRAM, self.field("Search with Meta AI", rect=(.05, .05, .7, .05)))):
            with self.subTest(name):
                self.assertEqual(self.verdict("TYPE_SUBMIT", bundle, field), "acts")

    def test_return_in_a_field_that_sends_or_posts_or_that_is_not_certainly_a_query_still_asks(self):
        send = Element("s", "Send", "Button", (.85, .9, .1, .05))
        for name, bundle, field, others in (
                ("a message composer", self.MESSAGES, self.field("Message", "TextView", (.1, .9, .7, .05)), ()),
                ("a comment field", self.INSTAGRAM, self.field("Add a comment…", rect=(.1, .9, .7, .05)), ()),
                ("a search field beside a Send button", self.MESSAGES,
                 self.field("Search messages", rect=(.1, .9, .7, .05)), (send,)),
                ("a field it can't tell", self.SETTINGS, self.field("Name", rect=(.1, .5, .7, .05)), ()),
                ("an address field outside a browser", self.CONTACTS,
                 self.field("Address", rect=(.1, .5, .7, .05)), ()),
                ("an address field inside a web page", self.SAFARI,
                 self.field("Address", rect=(.1, .5, .7, .05), locator="//XCUIElementTypeWebView/TextField"), ()),
                ("a comment field inside a web page", self.SAFARI,
                 self.field("Add a comment", rect=(.1, .5, .7, .05), locator="//XCUIElementTypeWebView/TextField"),
                 ())):
            with self.subTest(name):
                self.assertEqual(self.verdict("TYPE_SUBMIT", bundle, field, others), "asks")

    def test_the_card_still_names_a_send_when_the_return_is_not_a_search(self):
        field = self.field("Message", "TextView", (.1, .9, .7, .05))
        self.assertEqual(self.verdict("TYPE_SUBMIT", self.MESSAGES, field), "asks")
        self.assertEqual(self.verdict("TYPE_SUBMIT", self.MESSAGES, self.field("iMessage", "TextView")), "asks")

    def test_clearing_a_search_field_acts_without_a_card(self):
        instagram = self.field("Search with Meta AI", rect=(.05, .065, .72, .06))
        settings = self.field("Search", "SearchField", (.05, .065, .72, .06))
        self.assertEqual(self.clear(self.INSTAGRAM, instagram, others=(Element("x", "Cancel", "Button",
                                                                               (.88, .07, .1, .05)),)), "acts")
        self.assertEqual(self.clear(self.SETTINGS, settings), "acts")

    def test_clearing_a_draft_a_note_or_a_history_still_asks(self):
        message = self.field("Message", "TextView", (.05, .9, .72, .06))
        note = self.field("Note", "TextView", (.05, .5, .72, .3))
        search = self.field("Search", "SearchField", (.05, .065, .72, .06))
        self.assertEqual(self.clear(self.MESSAGES, message, (.8, .9, .1, .05)), "asks")
        self.assertEqual(self.clear(self.NOTES, note, (.8, .5, .1, .05)), "asks")
        self.assertEqual(self.verdict("TAP", self.INSTAGRAM, Element("c", "Clear", "Button", (.78, .07, .1, .05))),
                         "asks")
        for label in ("Clear all", "Clear History", "Clear recent searches"):
            with self.subTest(label):
                self.assertEqual(self.clear(self.INSTAGRAM, search, label=label), "asks")
        self.assertEqual(self.clear(self.INSTAGRAM, search, rect=(.78, .5, .1, .05)), "asks")
        self.assertEqual(self.clear(self.INSTAGRAM, search, others=(self.field("Message", "TextView",
                                                                                (.05, .07, .72, .06)),)), "asks")

    def test_a_coding_agents_phone_tools_clear_a_search_bar_and_still_ask_for_a_draft(self):
        from types import SimpleNamespace

        from mobile_agent import permissions
        from mobile_agent.ai_plans.phone_tools import PhoneTools, ToolProblem
        from mobile_agent.state import Snapshot

        def approval(bundle, field, clear):
            cards = []
            tools = PhoneTools.__new__(PhoneTools)
            tools.goal, tools.typed, tools.approved_send, tools.steps, tools.changes = "", None, None, 0, 0
            tools.permissions = permissions.PermissionGate(permissions.ASK_IMPORTANT)
            tools.approve = lambda request: cards.append(request) or "denied"
            tools.agent = SimpleNamespace(_app_name=lambda snapshot: "App", _masked=lambda snapshot: snapshot,
                                          _mask=lambda text: text)
            snapshot = Snapshot([field, clear], "", 1, 1, "wda", bundle_id=bundle)
            try:
                tools._approval("TAP", clear, snapshot, None, False)
            except ToolProblem:
                pass
            return cards

        search = self.field("Search", "SearchField", (.05, .065, .72, .06))
        message = self.field("Message", "TextView", (.05, .9, .72, .06))
        self.assertEqual(approval(self.SETTINGS, search, Element("c", "Clear", "Button", (.78, .07, .1, .05))), [])
        cards = approval(self.MESSAGES, message, Element("c", "Clear", "Button", (.8, .9, .1, .05)))
        self.assertEqual([card["title"] for card in cards], ["Clear these?"])

from mobile_agent.tests.test_server_engine import MESSAGES
from mobile_agent.tests.test_server_engine import Base as _ServerBase  # noqa: E402

CHASE = {"id": "chase", "name": "Chase", "bundleId": "com.chase", "installed": True}
SESSION = "s" * 40


class ServerTests(_ServerBase):

    def setUp(self):
        super().setUp()
        from mobile_agent.tests.seam_support import isolate
        isolate(self)
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        self.app = self.runtime(managed=True, apps=(MESSAGES, CHASE))
        from mobile_agent import tracks
        tracks.load()
        from mobile_agent.server import make_handler
        self.handler = make_handler(self.app, app_session=SESSION)

    def call(self, method, path, body=None, app=False):
        from mobile_agent.tests.seam_support import request
        return request(self.handler, method, path, body, headers={"X-Mobster-App-Session": SESSION} if app else {})

    def test_settings_show_the_mode_and_the_older_switch_follows(self):
        status, data, _ = self.call("GET", "/api/permissions")
        self.assertEqual((status, data["permissionMode"]), (200, ASK_IMPORTANT))
        self.assertEqual(self.call("POST", "/api/permissions", {"permissionMode": DONT_ASK})[0], 403)
        status, data, _ = self.call("POST", "/api/permissions", {"permissionMode": DONT_ASK}, app=True)
        self.assertEqual((status, data["permissionMode"]), (200, DONT_ASK))
        self.assertFalse(self.app.settings()["askBeforeActing"])
        with open(self.app.setup.env_file) as saved:
            self.assertIn("MOBSTER_PERMISSION_MODE=dont_ask", saved.read())
        self.app.update_settings({"askBeforeActing": True})
        self.assertEqual(self.app.settings()["permissionMode"], ASK_IMPORTANT)

    def test_apps_are_approved_one_by_one_and_switched_off_anytime(self):
        with patch.dict(os.environ, {"MOBSTER_APP_ACCESS": "ask"}):
            status, data, _ = self.call("GET", "/api/permissions")
            chase = next(a for a in data["apps"] if a["bundleId"] == "com.chase")
            self.assertEqual((chase["allowed"], chase["why"]), (False, "asks"))
            self.assertEqual(self.call("POST", "/api/permissions", {"allow": "com.chase"})[0], 403)
            status, data, _ = self.call("POST", "/api/permissions", {"allow": "com.chase"}, app=True)
            self.assertEqual([a["bundleId"] for a in data["approvedApps"]], ["com.chase"])
            self.assertTrue(next(a for a in data["apps"] if a["bundleId"] == "com.chase")["allowed"])
            self.assertEqual(data["exceptions"], [])
            status, data, _ = self.call("POST", "/api/permissions", {"revoke": "com.chase"}, app=True)
            self.assertEqual(data["approvedApps"], [])
            self.assertEqual(next(a for a in data["apps"] if a["bundleId"] == "com.chase")["why"], "asks")

    def test_a_task_in_an_app_not_approved_yet_starts_and_asks_before_opening_it(self):
        from mobile_agent.api_errors import APIError
        with patch.dict(os.environ, {"MOBSTER_APP_ACCESS": "ask"}):
            run = self.app.create("chase", "Check my balance", "live")
            guard = self.app.make_guard(run, None)
            self.assertIs(guard.grants, self.app.permission_exceptions())
            asked = []
            guard.ask = lambda request: asked.append(request) or "denied"
            self.assertFalse(guard.request(["com.chase"]))
            self.assertEqual(asked[0]["title"], "Allow Mobster to use Chase?")
            self.finish(self.app, run)
        with patch.dict(os.environ, {"MOBSTER_BLOCKED_APPS": "com.chase"}):
            with self.assertRaises(APIError) as caught:
                self.app.create("chase", "Check my balance", "live")
            self.assertEqual((caught.exception.status, caught.exception.code), (403, "app_not_allowed"))
            self.assertIn("never opens", str(caught.exception))

    def test_a_task_may_ask_more_anywhere_but_less_only_from_the_app(self):
        body = {"appId": "messages", "goal": "Text Sam hi", "permissionMode": DONT_ASK}
        status, data, _ = self.call("POST", "/api/runs", body)
        self.assertEqual((status, data.get("code")), (403, "app_only"))
        status, data, _ = self.call("POST", "/api/runs", {**body, "permissionMode": ASK_ALL})
        self.assertEqual(status, 201, data)
        run = self.app.runs[data["run"]["id"]]
        self.assertEqual(self.app.permission_gate(run).mode, ASK_ALL)
        self.finish(self.app, run)

    def test_a_phones_own_ask_before_acting_sets_the_runs_mode(self):
        run = self.app.create("messages", "Text Sam hi", "live")
        try:
            self.assertEqual(self.app.permission_gate(run).mode, ASK_IMPORTANT)
            run.ask_override = False
            self.assertEqual(self.app.permission_gate(run).mode, DONT_ASK)
            run.ask_override = True
            self.assertEqual(self.app.permission_gate(run).mode, ASK_IMPORTANT)
            with patch.dict(os.environ, {"MOBSTER_PERMISSION_MODE": ASK_ALL}):
                self.assertEqual(self.app.permission_gate(run).mode, ASK_ALL)
            with patch.dict(os.environ, {"MOBSTER_PERMISSION_MODE": DONT_ASK}):
                self.assertEqual(self.app.permission_gate(run).mode, ASK_IMPORTANT)
            run.ask_override, run.extras = False, {**(run.extras or {}), "permissionMode": ASK_ALL}
            self.assertEqual(self.app.permission_gate(run).mode, ASK_ALL)
        finally:
            self.finish(self.app, run)

    def test_an_approval_carries_the_gates_reason(self):
        run = self.app.create("messages", "Text Sam", "live")
        box = []
        thread = threading.Thread(target=lambda: box.append(run.request_approval(
            {"operation": "TYPE", "label": "Code", "title": "Enter this code?", "reason": "Types a one-time code"},
            timeout=5)))
        thread.start()
        for _ in range(500):
            if run.public()["approval"]:
                break
            time.sleep(.005)
        self.assertEqual(run.public()["approval"]["reason"], "Types a one-time code")
        run.stop.set()
        thread.join(5)
        self.finish(self.app, run)

    def test_dont_ask_again_adds_an_allow_rule_from_the_app_that_can_be_taken_back(self):
        run = self.app.create("messages", "Text Sam", "live")
        gate = self.app.permission_gate(run)
        decision = gate.decide(Action("TAP", "Send", "Button", app="com.apple.MobileSMS"), existing_act="send")
        box = []
        thread = threading.Thread(target=lambda: box.append(run.request_approval(
            {"operation": "TAP", "label": "Send", "title": "Send?",
             "remember": gate.remember(decision, "com.apple.MobileSMS", "Messages")}, timeout=5)))
        thread.start()
        for _ in range(500):
            if run.public()["approval"]:
                break
            time.sleep(.005)
        approval = run.public()["approval"]
        self.assertTrue(approval["rememberable"])
        self.assertEqual(approval["rememberLabel"], "Don’t ask again for sending messages in Messages")
        status, _, _ = self.call("POST", f"/api/runs/{run.id}/approval", {"id": approval["id"], "approve": True,
                                                                         "remember": True}, app=True)
        self.assertEqual(status, 200)
        thread.join(5)
        status, data, _ = self.call("GET", "/api/permissions")
        self.assertEqual([(e["label"], e["words"]) for e in data["exceptions"]], [("send", "Sending messages")])
        self.assertEqual(self.app.permission_gate(run).decide(
            Action("TAP", "Send", "Button", app="com.apple.MobileSMS"), existing_act="send").decision, "allow")
        key = data["exceptions"][0]["key"]
        self.assertEqual(self.call("POST", "/api/permissions", {"forget": key})[0], 403)
        status, data, _ = self.call("POST", "/api/permissions", {"forget": key}, app=True)
        self.assertEqual(data["exceptions"], [])
        self.finish(self.app, run)

    def test_an_outside_agent_raises_an_approval_only_the_app_answers(self):
        status, data, _ = self.call("POST", "/api/approvals", {"key": "k", "request": {
            "label": "Send", "title": "Send this message?", "reason": "Sends a message to Sam", "app_name": "Messages"},
            "source": {"client": "Claude Code"}})
        self.assertEqual(status, 201, data)
        approval = data["approval"]["id"]
        status, data, _ = self.call("GET", "/api/approvals")
        self.assertEqual([a["id"] for a in data["approvals"]], [approval])
        self.assertEqual(self.call("POST", f"/api/approvals/{approval}", {"approve": True})[0], 403)
        self.assertEqual(self.call("POST", f"/api/approvals/{approval}", {"approve": True}, app=True)[0], 200)
        status, data, _ = self.call("GET", f"/api/approvals/{approval}")
        self.assertEqual(data["approval"]["state"], "approved")


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILDERS = frozenset({"build_frontier", "FrontierAgent"})
NO_MODE = ("tests/", "bench/", "evals/", "testkit/", "verify/", "replay_core/")


def builder_calls():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(NO_MODE) or ".venv/" in rel:
            continue
        found = []

        def visit(node, function):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function = node.name
            if isinstance(node, ast.Call):
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if called in BUILDERS:
                    found.append((rel, node.lineno, function, node))
            for child in ast.iter_child_nodes(node):
                visit(child, function)
        visit(ast.parse(path.read_text(encoding="utf-8"), filename=rel), "<module>")
        yield from found


def passes_a_mode(call):
    keyword = next((k for k in call.keywords if k.arg == "permissions"), None)
    return keyword is not None and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)


def demo(call):
    return any(isinstance(arg, ast.Call) and getattr(arg.func, "id", None) == "DemoDriver" for arg in call.args)


class EveryProductBuilderPassesAModeTests(unittest.TestCase):
    def test_every_agent_a_product_path_builds_carries_the_runs_permission_gate(self):
        found, missing = [], []
        for rel, line, function, call in builder_calls():
            if rel == "engines.py" and function == "build_frontier":
                continue
            if demo(call):
                continue
            found.append(f"{rel}:{line}")
            if not passes_a_mode(call):
                missing.append(f"{rel}:{line} ({function})")
        self.assertEqual(missing, [], "these build an agent with no permission mode")
        self.assertIn("runtime_work.py", {f.split(":")[0] for f in found}, found)


class RuntimeGateTests(unittest.TestCase):

    def gate(self, origin, env=None, extras=None, ask_override=None):
        run = NS(extras=extras or {}, ask_override=ask_override, origin=origin, goal="Like my latest post",
                 emit=lambda event: None)
        runtime = NS(remembering=lambda run_: None, permission_exceptions=lambda: None)
        clean = {k: v for k, v in os.environ.items() if not k.startswith("MOBSTER_")}
        with patch.dict(os.environ, {**clean, **(env or {})}, clear=True):
            return server.Runtime.permission_gate(runtime, run)

    def test_with_no_saved_mode_every_origin_asks_before_acting(self):
        for origin in ("app", "api", "tui", "schedule", "remote", "mcp", "cli", None):
            gate = self.gate(origin)
            self.assertIsInstance(gate, permissions.PermissionGate, origin)
            self.assertEqual(gate.mode, permissions.ASK_IMPORTANT, origin)

    def test_full_access_is_the_persons_choice_and_never_an_outside_agents(self):
        full = {"MOBSTER_PERMISSION_MODE": permissions.DONT_ASK}
        self.assertEqual(self.gate("app", full).mode, permissions.DONT_ASK)
        for origin in ("remote", "mcp"):
            self.assertEqual(self.gate(origin, full).mode, permissions.ASK_IMPORTANT, origin)
        self.assertEqual(self.gate("app", full, ask_override=True).mode, permissions.ASK_IMPORTANT)
        self.assertEqual(self.gate("app", extras={"permissionMode": "whatever"}).mode, permissions.ASK_IMPORTANT)

    def test_a_gate_never_has_no_mode(self):
        self.assertEqual(permissions.PermissionGate(None).mode, permissions.ASK_IMPORTANT)
        self.assertEqual(permissions.PermissionGate("").mode, permissions.ASK_IMPORTANT)


if __name__ == "__main__":
    unittest.main()

import io
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import drivers, permissions
from mobile_agent.frontier import FrontierAgent
from mobile_agent.permissions import (
    ASK_ALL,
    ASK_IMPORTANT,
    DONT_ASK,
    Action,
    AppPolicy,
    PermissionGate,
    PermissionGuard,
    outside,
    policy,
    rules,
)
from mobile_agent.permissions.exceptions import Exceptions, key_of
from mobile_agent.state import Element, Snapshot
from mobile_agent.tests import test_frontier as F

OPEN = policy.ALLOW_ALL
MESSAGES = "com.apple.MobileSMS"
CHASE = "com.chase"


def tap(label, role="Button", **fields):
    return Action("TAP", label, role, **fields)


def typed(label, text, role="TextField", **fields):
    return Action("TYPE", label, role, text=text, **fields)


def store():
    return Exceptions(os.path.join(tempfile.mkdtemp(), "permission-exceptions.json"))


class MoneyRuleTests(unittest.TestCase):
    def setUp(self):
        self.store = store()

    def gate(self, mode=ASK_IMPORTANT, origin=None):
        return PermissionGate(mode, apps=OPEN, exceptions=self.store, origin=origin)

    def test_a_send_in_a_bank_never_gets_a_rule_and_a_rule_there_is_never_used(self):
        fifty = tap("Send", app=CHASE, app_name="Chase", screen="Zelle", context="Send $50.00 to Sam")
        first = self.gate().decide(fifty, existing_act="send")
        self.assertEqual((first.decision, first.rememberable), ("ask", False))
        self.assertIsNone(PermissionGate.remember(first, CHASE, "Chase"))
        self.store.add(CHASE, "send", rules.RULE_OP, "Chase")
        big = tap("Send", app=CHASE, app_name="Chase", screen="Zelle", context="Send $2,500.00 to Sam")
        self.assertEqual(self.gate().decide(big, existing_act="send").decision, "ask")
        paypal = tap("Send", app="com.yourcompany.PPClient", app_name="PayPal")
        self.assertFalse(self.gate().decide(paypal, existing_act="send").rememberable)
        message = tap("Send", app=MESSAGES, app_name="Messages", screen="Sam")
        self.assertTrue(self.gate().decide(message, existing_act="send").rememberable)

    def test_a_price_on_a_follow_join_or_accept_means_no_rule(self):
        for label, act, context in (("Subscribe", "subscribe", "Subscribe $4.99/month"),
                                    ("Join", "join", "Join for $9.99"), ("Accept", "accept", "Accept $20 from Sam"),
                                    ("Subscribe", "subscribe", "Free trial, then 7.99 USD")):
            decision = self.gate().decide(tap(label, app="com.example.app", context=context), existing_act=act)
            self.assertEqual((decision.decision, decision.rememberable), ("ask", False), context)
        for label, act in (("Subscribe", "subscribe"), ("Join", "join"), ("Accept", "accept")):
            self.assertTrue(self.gate().decide(tap(label, app="com.google.ios.youtube"), existing_act=act).rememberable,
                            label)

    def test_a_word_that_undoes_something_never_gets_a_rule_in_any_kind(self):
        for label in ("Remove from list", "Clear history", "Delete", "Reset", "Move to Trash", "Erase"):
            decision = self.gate(ASK_ALL).decide(tap(label, app="com.apple.reminders"))
            self.assertEqual(decision.decision, "ask", label)
            self.assertFalse(decision.rememberable, label)
        self.assertTrue(self.gate(ASK_ALL).decide(tap("Save", app="com.apple.mobilenotes")).rememberable)

    def test_the_home_screen_and_ios_sheets_never_get_a_rule(self):
        for bundle in ("com.apple.springboard", "com.apple.PassbookUIService", "com.apple.ios.StoreKitUIService",
                       "com.apple.SomethingViewService"):
            decision = self.gate().decide(tap("Share", app=bundle), existing_act="share")
            self.assertEqual((decision.decision, decision.rememberable), ("ask", False), bundle)
        self.assertTrue(self.gate().decide(tap("Share", app="com.apple.mobileslideshow"), existing_act="share")
                        .rememberable)


class ForcedAskTests(unittest.TestCase):
    def setUp(self):
        self.store = store()
        self.store.add(MESSAGES, "send", rules.RULE_OP, "Messages")
        self.send = tap("Send", app=MESSAGES, app_name="Messages")

    def test_a_forced_ask_asks_whatever_the_rule_or_the_mode(self):
        for mode in (ASK_IMPORTANT, ASK_ALL, DONT_ASK):
            forced = PermissionGate(mode, apps=OPEN, exceptions=self.store).decide(self.send, "send", forced=True)
            self.assertEqual((forced.decision, forced.rememberable, forced.rule), ("ask", False, None), mode)
        self.assertEqual(PermissionGate(ASK_IMPORTANT, apps=OPEN, exceptions=self.store)
                         .decide(self.send, "send").decision, "allow")


def fixture_screen(stage):
    elements, text = {
        "home": ([Element("0", "Search", "Button", (.8, .06, .1, .05)),
                  Element("1", "Profile", "Button", (.8, .92, .15, .05))], "For You\nSearch\nProfile"),
        "search": ([Element("0", "Search", "SearchField", (.1, .06, .7, .05), True),
                    Element("1", "Search", "Button", (.82, .06, .15, .05))], "Search TikTok"),
        "results": ([Element("0", "Coffee brewing guide", "Button", (.05, .2, .9, .3)),
                     Element("1", "@coffee.studio", "Button", (.1, .55, .5, .05))],
                    "Top results\nCoffee brewing guide\n@coffee.studio")}[stage]
    return elements, text


class StagePhone(drivers.Driver):
    can_type = True

    def __init__(self, bundle=None):
        self.bundle, self.stage, self.actions = bundle, "home", []

    def observe(self, timeout=10):
        elements, text = fixture_screen(self.stage)
        where = {"bundle_id": self.bundle} if self.bundle else {}
        return Snapshot(elements, text, 402, 874, "synthetic_fixture", **where)

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append(operation)
        if self.stage == "home" and operation == "TAP":
            self.stage = "search"
        elif self.stage == "search" and operation == "TYPE":
            self.stage = "results"

    def close(self):
        pass


class FrontierGateTests(unittest.TestCase):
    def run_frontier(self, request, elements, bundle, steps, *, mode=ASK_IMPORTANT, answer="approved",
                     exceptions=None, guard=None):
        asked = []

        def approve(request_):
            asked.append(request_)
            return answer
        gate = PermissionGate(mode, apps=OPEN, exceptions=exceptions)
        hooks = {"approve": None, "floor_approve": approve} if mode == DONT_ASK else {"approve": approve}
        driver = F.Driver([F.screen(*elements, bundle=bundle)] * 8)
        script = F.Script(list(steps) + [("DONE", None, None)] * 3)
        result = FrontierAgent(driver, script, emit=lambda e: None, screenshots=False, settle_seconds=0,
                               permissions=gate, guard=guard, **hooks).run(request)
        return result, driver, asked, script

    def test_full_access_types_freely_but_asks_before_a_one_time_code(self):
        code = Element("0", "Verification code", "TextField", (.1, .06, .7, .05), True)
        result, driver, asked, _ = self.run_frontier("Sign in to my bank", [code], "com.example.bank",
                                                     [("TYPE", "Verification code", "482913")], mode=DONT_ASK,
                                                     answer="denied")
        self.assertEqual([(r["act"], r["title"]) for r in asked], [("code", "Enter this code?")])
        self.assertEqual((result["status"], driver.actions), ("approval_denied", []))
        search = Element("0", "Search", "SearchField", (.1, .06, .7, .05), True)
        _, driver, asked, _ = self.run_frontier("Search coffee", [search], "com.example.bank",
                                                [("TYPE", "Search", "coffee")], mode=DONT_ASK)
        self.assertEqual(asked, [])
        self.assertEqual(driver.actions, [("TYPE", "Search", "coffee")])

    def test_ask_before_every_change_asks_before_save(self):
        save = Element("0", "Save", "Button", (.8, .06, .1, .05))
        result, driver, asked, _ = self.run_frontier("Save the note", [save], "com.apple.mobilenotes",
                                                     [("TAP", "Save", None)], mode=ASK_ALL, answer="denied")
        self.assertEqual([r["title"] for r in asked], ["Save this?"])
        self.assertEqual((result["status"], driver.actions), ("approval_denied", []))
        _, driver, asked, _ = self.run_frontier("Save the note", [save], "com.apple.mobilenotes",
                                                [("TAP", "Save", None)])
        self.assertEqual(asked, [])
        self.assertEqual(driver.actions, [("TAP", "Save", None)])

    def test_a_rule_lets_a_declared_send_go_and_without_it_the_send_asks(self):
        from mobile_agent.tests.test_contract import AgentLoopTests, Driver, Script, screen

        def chat(value="Message", sent=None):
            return screen(*([Element("3", sent, "StaticText", (.1, .5, .6, .04))] if sent else []),
                          Element("1", "Send", "Button", (.8, .9, .1, .04)),
                          Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value=value,
                                  placeholder="Message"), Element("4", "Leo", "NavigationBar", (0, 0, 1, .06)))
        for rule in (True, False):
            exceptions, asked = store(), []
            if rule:
                exceptions.add("com.example.chat", "send", rules.RULE_OP, "QuickChat")
            driver = Driver([chat(), chat("Dinner is at 7"), chat(sent="Dinner is at 7"), chat(sent="Dinner is at 7")])
            script = Script([[("TYPE", "Message", "Dinner is at 7"), ("TAP", "Send", None)], [("DONE", None, None)],
                             [("DONE", None, None)]], AgentLoopTests.ITEMS)
            FrontierAgent(driver, script, apps={"com.example.chat": "QuickChat"}, emit=lambda e: None,
                          screenshots=False, settle_seconds=0,
                          permissions=PermissionGate(ASK_IMPORTANT, apps=OPEN, exceptions=exceptions),
                          approve=lambda request: asked.append(request) or "denied").run(AgentLoopTests.REQ)
            if rule:
                self.assertEqual(asked, [])
                self.assertIn(("TAP", "Send", None), driver.actions)
            else:
                self.assertEqual([r["label"] for r in asked], ["Send"])
                self.assertNotIn(("TAP", "Send", None), driver.actions)

    def test_an_app_nobody_allowed_is_left_before_anything_is_read_or_done(self):
        grants = store()
        guard = PermissionGuard(None, AppPolicy(access="ask"), {"com.burbn.instagram": "Instagram"}, ask=None,
                                grants=grants)
        feed = [Element("0", "Search", "Button", (.8, .06, .1, .05)),
                Element("1", "nasa: Saturn tonight", "StaticText", (.1, .3, .8, .05))]
        result, driver, _, script = self.run_frontier("Read my Instagram feed", feed, "com.burbn.instagram",
                                                      [("TAP", "Search", None)], guard=guard)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(driver.actions, [("HOME", None, None)])
        self.assertEqual(script.usage["calls"], 0)
        grants.add("com.burbn.instagram", policy.APP_LABEL, policy.APP_OP, "Instagram")
        result, driver, _, script = self.run_frontier("Read my Instagram feed", feed, "com.burbn.instagram",
                                                      [("TAP", "Search", None)], guard=guard)
        self.assertNotEqual(result["status"], "blocked")
        self.assertIn(("TAP", "Search", None), driver.actions)


class GuardTests(unittest.TestCase):
    def test_switching_an_app_off_mid_run_asks_again(self):
        grants, asked = store(), []

        def always(request):
            asked.append(request)
            permissions.remember_in(grants, request["remember"])
            return "approved"
        guard = PermissionGuard(None, AppPolicy(), {"com.culturedcode.ThingsiPhone": "Things"}, ask=always,
                                grants=grants)
        self.assertTrue(guard.allows_app("com.culturedcode.ThingsiPhone"))
        self.assertTrue(guard.allows_app("com.culturedcode.ThingsiPhone"))
        self.assertEqual(len(asked), 1)
        grants.remove(key_of("com.culturedcode.ThingsiPhone", policy.APP_LABEL, policy.APP_OP))
        guard.ask = lambda request: asked.append(request) or "denied"
        self.assertFalse(guard.allows_app("com.culturedcode.ThingsiPhone"))
        self.assertEqual(len(asked), 2)

    def test_the_never_open_list_is_read_live(self):
        guard = PermissionGuard(None, lambda: policy.from_env().apps, ask=None, grants=store())
        with patch.dict(os.environ, {"MOBSTER_APP_ACCESS": "all"}):
            self.assertTrue(guard.allows_app("com.burbn.instagram"))
            with patch.dict(os.environ, {"MOBSTER_BLOCKED_APPS": "com.burbn.instagram"}):
                self.assertFalse(guard.allows_app("com.burbn.instagram"))

    def test_say_what_to_do_instead_is_kept_for_the_run(self):
        guard = PermissionGuard(None, AppPolicy(), {"com.burbn.instagram": "Instagram"},
                                ask=lambda request: "redirected:Use Safari instead", grants=store())
        self.assertFalse(guard.allows_app("com.burbn.instagram"))
        self.assertEqual(guard.take_instruction(), "Use Safari instead")
        self.assertIsNone(guard.take_instruction())
        guard = PermissionGuard(None, AppPolicy(), {"com.burbn.instagram": "Instagram"},
                                ask=lambda request: "denied", grants=store())
        self.assertFalse(guard.allows_app("com.burbn.instagram"))
        self.assertIsNone(guard.take_instruction())
        self.assertIn("switch Instagram on in Apps", guard.blocked_message("com.burbn.instagram"))


class CodeDetectionTests(unittest.TestCase):
    def test_what_is_a_code(self):
        for action in (typed("", "4", screen="Two-Factor Authentication"),
                       typed("", "482913", context="We sent a code to •••• 42"),
                       typed("", "7", context="Enter the code we texted you"),
                       typed("Digit 1", "7", screen="Verify it's you"),
                       typed("", "482913", hint="oneTimeCode"),
                       typed("Field", "482913"),
                       typed("", "5"),
                       typed("iMessage", "482913", role="TextView")):
            self.assertTrue(rules.code_entry(action), action)
        for action in (typed("Quantity", "2"), typed("Phone", "4155550123"), typed("Search", "482913", "SearchField"),
                       typed("Year", "2026"), typed("Message", "See you at 7"), typed("Title", "Groceries"),
                       Action("TAP", "Code", "Button")):
            self.assertFalse(rules.code_entry(action), action)

    def test_a_code_from_the_action_reads_the_fields_placeholder(self):
        for placeholder in ("One-time code", "Search"):
            for text, origins in (("A7BC9D", ()), ("482913", None)):
                with self.subTest(placeholder=placeholder, text=text):
                    element = Element("1", "", "TextField", (.1, .3, .8, .05), True, placeholder=placeholder)
                    snapshot = Snapshot([element], "", 400, 800, "wda", bundle_id="com.example.app")
                    action = permissions.action_from("TYPE", element, snapshot, text=text, origins=origins)
                    self.assertEqual(rules.code_entry(action), placeholder == "One-time code")


class SettingsSurfaceTests(unittest.TestCase):
    def test_safety_says_when_the_env_file_allows_apps(self):
        settings = policy.from_env({"MOBSTER_APPS_ALLOWED": "com.apple.MobileSMS"})
        self.assertEqual(settings.public()["envAllowed"], ["com.apple.MobileSMS"])
        self.assertEqual(policy.from_env({}).public()["envAllowed"], [])


class OpenUrlSkillTests(unittest.TestCase):
    def ctx(self, guard, origin="app", remembered=None):
        runtime = SimpleNamespace(permission_exceptions=lambda: remembered) if remembered is not None else None
        return SimpleNamespace(driver=SimpleNamespace(call=lambda *a, **k: None), guard=guard,
                               run=SimpleNamespace(origin=origin, runtime=runtime), emit=lambda e: None)

    def test_a_link_into_an_app_nobody_allowed_is_refused_before_it_opens(self):
        from mobile_agent.skills.open_url import OpenUrl
        grants = store()
        guard = PermissionGuard(None, AppPolicy(), {"com.burbn.instagram": "Instagram"}, ask=None, grants=grants)
        skill = OpenUrl()
        prepared = skill.prepare(self.ctx(guard), {"text": "instagram://user?username=nasa"}, None)
        self.assertIsNone(prepared.title)
        result = skill.perform(self.ctx(guard), prepared, {}, None)
        self.assertIn("Refused", result.feedback)
        self.assertIn("Instagram", result.feedback)
        grants.add("com.burbn.instagram", policy.APP_LABEL, policy.APP_OP, "Instagram")
        prepared = skill.prepare(self.ctx(guard), {"text": "instagram://user?username=nasa"}, None)
        self.assertEqual(prepared.state, {"url": "instagram://user?username=nasa"})

    def test_a_web_page_is_checked_against_the_browser_it_opens_in(self):
        from mobile_agent.skills.open_url import OpenUrl
        grants = store()
        guard = PermissionGuard(None, AppPolicy(), {"com.google.chrome.ios": "Chrome"}, ask=None, grants=grants)
        act = {"text": "https://example.com/menu", "app": "com.google.chrome.ios"}
        prepared = OpenUrl().prepare(self.ctx(guard), act, None)
        self.assertIn("Chrome", prepared.state["refused"])
        grants.add("com.google.chrome.ios", policy.APP_LABEL, policy.APP_OP, "Chrome")
        self.assertEqual(OpenUrl().prepare(self.ctx(guard), act, None).state, {"url": "https://example.com/menu"})

    def test_an_outside_agents_run_never_uses_or_offers_a_link_rule(self):
        from mobile_agent.deeplinks import LINK_LABEL, LINK_OP
        from mobile_agent.skills.open_url import OpenUrl
        remembered = store()
        remembered.add("url:things", LINK_LABEL, LINK_OP, "Things")
        skill = OpenUrl()
        mine = skill.prepare(self.ctx(None, "app", remembered), {"text": "things:///add?title=Milk"}, None)
        self.assertIsNone(mine.title)
        theirs = skill.prepare(self.ctx(None, "remote", remembered), {"text": "things:///add?title=Milk"}, None)
        self.assertIsNotNone(theirs.title)
        self.assertIsNone(theirs.remember)


class ReplayTests(unittest.TestCase):
    def test_a_replay_hands_off_before_an_app_it_may_not_use(self):
        from mobile_agent.replay_core.engine import Replayer
        driver = StagePhone()
        steps = {"steps": [{"op": "LAUNCH_APP", "app": "com.burbn.instagram"}]}
        result = Replayer(driver, allows_app=lambda bundle: False, app_names={"com.burbn.instagram": "Instagram"},
                          sleep=lambda s: None).run(steps)
        self.assertEqual(result.status, "handoff")
        self.assertIn("Instagram isn't allowed", result.reason)
        self.assertEqual(driver.actions, [])
        driver = StagePhone()
        result = Replayer(driver, allows_app=lambda bundle: True, sleep=lambda s: None).run(steps)
        self.assertEqual(driver.actions, ["LAUNCH_APP"])


class ReplaySeekTests(unittest.TestCase):
    def test_a_seek_never_swipes_in_an_app_nobody_allowed(self):
        from mobile_agent.replay_core.engine import Replayer
        steps = {"steps": [{"op": "TAP", "target": {"label": "Camera", "role": "Cell"}, "seek": True,
                            "app": "com.apple.Preferences"}]}
        for allowed, swiped in ((False, False), (True, True)):
            driver = StagePhone(bundle="com.apple.Preferences")
            with patch("mobile_agent.replay_core.engine.WAIT_SECONDS", .05):
                result = Replayer(driver, allows_app=lambda bundle, allowed=allowed: allowed,
                                  sleep=lambda s: None).run(steps)
            self.assertEqual("SWIPE_UP" in driver.actions, swiped, allowed)
            if not allowed:
                self.assertEqual(result.status, "handoff")


class CliTests(unittest.TestCase):
    CARD = {"id": "a1", "kind": "app", "title": "Allow Mobster to use Instagram?"}
    CODE = {"id": "a2", "kind": "commit", "title": "Enter this code?", "act": "enter_code"}

    class Session:
        def __init__(self, request, **_):
            self.request, self.answers, self.stopped = request, [], False
            self.runtime = SimpleNamespace()

        def start(self, app_id, goal):
            self.run = SimpleNamespace(id="r1", status="approval_denied", summary={})
            return self.run

        def follow(self, run, on_event):
            on_event({"event": "approval_requested", "approval_id": self.request["id"], "title": self.request["title"]})
            on_event({"event": "result", "status": "approval_denied" if not self.answers[-1][1] else "completed"})

        def pending_approval(self, run):
            return None if self.answers else dict(self.request)

        def answer(self, approval_id, approve, run=None, remember=False):
            self.answers.append((approval_id, approve, "always") if remember else (approval_id, approve))

        def stop(self, run):
            self.stopped = True

        def close(self):
            pass

    def run_cli(self, request, *, json_flag, typed=None):
        from mobile_agent import smart_run
        sessions = []

        def make(**kwargs):
            sessions.append(self.Session(request, **kwargs))
            return sessions[-1]
        args = SimpleNamespace(goal="Post my last photo", execute=True, json=json_flag, spend_cap_usd=None,
                               long=False, budget=None, yes=False, wda_url="http://127.0.0.1:8100", device=None,
                               state_db=None)
        out = io.StringIO()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test-0000"}), \
                patch("mobile_agent.tui.session.Session", side_effect=make), \
                patch.object(smart_run, "long_plan", return_value=None), \
                patch.object(smart_run, "remember_only", return_value=None), \
                patch("builtins.input", side_effect=list(typed or ())), \
                patch("sys.stdout", out):
            out.isatty = lambda: not json_flag
            code = smart_run.run_smart(args)
        return code, sessions[0], out.getvalue()

    def test_no_terminal_means_no_unapproved_app_and_no_code(self):
        for request in (self.CARD, self.CODE):
            with self.subTest(request=request["title"]):
                code, session, out = self.run_cli(request, json_flag=True)
                self.assertEqual(session.answers, [(request["id"], False)])
                self.assertNotEqual(code, 0)
                self.assertIn("nobody can answer here", out)

    def test_a_terminal_answers_the_card(self):
        code, session, out = self.run_cli(self.CARD, json_flag=False, typed=["y"])
        self.assertEqual(session.answers, [("a1", True)])
        self.assertIn("Allow Mobster to use Instagram?", out)
        code, session, _ = self.run_cli(self.CARD, json_flag=False, typed=["n"])
        self.assertEqual(session.answers, [("a1", False)])
        code, session, _ = self.run_cli(self.CARD, json_flag=False, typed=["a", "y"])
        self.assertEqual(session.answers, [("a1", True)])

    def test_terminal_app_cards_name_every_app_even_when_the_wire_title_is_short(self):
        from mobile_agent.tui.widgets import ApprovalCard
        names = [f"App {i} with a long name for reviewing a task across several apps" for i in range(9)]
        card = {"id": "a3", "kind": "commit", "act": "use app", "title": "Allow Mobster to use App 0?",
                "apps": names, "rememberable": True, "rememberLabel": "Always allow",
                "reason": "Allow is for this task. Always allow keeps them for later tasks."}
        code, session, out = self.run_cli(card, json_flag=False, typed=["a"])
        self.assertEqual(session.answers, [("a3", True, "always")])
        for name in names:
            self.assertIn(name, out)
        self.assertIn(card["reason"], out)
        widget = ApprovalCard()
        widget.request = card
        drawn = []
        with patch.object(widget.body, "update", lambda text: drawn.append(text.plain)):
            widget.redraw()
        for name in names:
            self.assertIn(name, drawn[-1])

    def test_a_tasks_app_card_takes_always_allow(self):
        card = {"id": "a3", "kind": "commit", "act": "use app", "title": "Allow Mobster to use Messages and Notes?",
                "apps": ["Messages", "Notes"], "rememberable": True, "rememberLabel": "Always allow"}
        code, session, out = self.run_cli(card, json_flag=False, typed=["a"])
        self.assertEqual(session.answers, [("a3", True, "always")])
        import re
        self.assertIn("[y] Allow   [a] Always allow   [n] Not now", re.sub(r"\x1b\[[0-9;]*m", "", out))
        code, session, _ = self.run_cli(card, json_flag=False, typed=["y"])
        self.assertEqual(session.answers, [("a3", True)])
        code, session, _ = self.run_cli(card, json_flag=True)
        self.assertEqual(session.answers, [("a3", False)])


class PutFileTests(unittest.TestCase):
    def test_a_file_goes_only_into_an_app_the_person_allowed(self):
        from mobile_agent.attachments.agent import PutFile
        allowed = SimpleNamespace(allows_app=lambda bundle: True)
        refused = SimpleNamespace(allows_app=lambda bundle: False)
        apps = AppPolicy()
        self.assertTrue(PutFile._approved(SimpleNamespace(guard=allowed), "com.apple.Pages", apps))
        self.assertFalse(PutFile._approved(SimpleNamespace(guard=refused), "com.apple.Pages", apps))
        with patch("mobile_agent.permissions.exceptions.default_path", lambda: store().path):
            self.assertFalse(PutFile._approved(None, "com.apple.Pages", apps))


class CodingAgentTests(unittest.TestCase):
    def setUp(self):
        from mobile_agent import engines
        from mobile_agent.ai_plans.phone_tools import PhoneTools
        from mobile_agent.tests import sota_world as world
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        kwargs = engines.frontier_kwargs
        patcher = patch.object(engines, "frontier_kwargs", lambda *a, **kw: {**kwargs(*a, **kw), "settle_seconds": 0})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = world.new_phone_state()
        self.asked, self.answer, self.grants = [], "denied", store()
        names = dict(world.APP_NAMES)
        guard = PermissionGuard(None, AppPolicy(), names, grants=self.grants,
                                ask=lambda request: self.asked.append(request) or self.answer)
        self.phone = world.ScriptedPhone(self.state)
        self.tools = PhoneTools(self.phone, apps=names, goal="Text Alex that I'm running late", emit=lambda e: None,
                                approve=lambda request: "approved", ask=lambda request: "approved", guard=guard,
                                permissions=permissions.PermissionGate(permissions.ASK_IMPORTANT,
                                                                       goal="Text Alex that I'm running late"))

    def test_open_app_asks_first_and_opens_nothing_on_not_now(self):
        result = self.tools.call("open_app", {"app": "Messages"})
        self.assertTrue(result.is_error)
        self.assertEqual(self.asked[0]["title"], "Allow Mobster to use Messages?")
        self.assertNotIn("LAUNCH_APP", [entry[0] for entry in self.state["log"]])
        self.grants.add("com.apple.MobileSMS", policy.APP_LABEL, policy.APP_OP, "Messages")
        result = self.tools.call("open_app", {"app": "Messages"})
        self.assertFalse(result.is_error, result.text)
        self.assertIn("Opened Messages", result.text)
        self.assertEqual(len(self.asked), 1)


class McpTests(unittest.TestCase):
    def harness(self):
        from mobile_agent.tests.test_mcp_new_tools import NewToolsHarness
        test = NewToolsHarness()
        test.setUp()
        self.addCleanup(test.doCleanups)
        test.make()
        grants = store()
        for patcher in (patch.dict(os.environ, {"MOBSTER_APP_ACCESS": "ask"}),
                        patch("mobile_agent.permissions.exceptions.default_path", lambda: grants.path)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return test, grants

    def test_nothing_of_an_app_nobody_allowed_is_read_and_the_phone_goes_home(self):
        test, grants = self.harness()
        with patch.object(outside, "ask_app", lambda request, **kw: {"state": "denied"}):
            shown = test.call("screen", device="Work iPhone", image=False)
        self.assertTrue(shown.is_error)
        self.assertIn("declined", shown.text)
        self.assertNotIn("Verification code", shown.text)
        from mobile_agent.tests.test_mcp_new_tools import FakeDeviceRun
        phone = FakeDeviceRun.opened[-1].driver
        self.assertIn(("POST", "/wda/pressButton", {"name": "home"}), phone.calls)
        asked = []
        with patch.object(outside, "ask_app", lambda request, **kw: asked.append(request) or {"state": "approved"}):
            shown = test.call("screen", device="Work iPhone", image=False)
        self.assertFalse(shown.is_error, shown.text)
        self.assertEqual(asked[0]["title"], "Allow Mobster to use com.chase.sig?")
        self.assertTrue(grants.contains("com.chase.sig", policy.APP_LABEL, policy.APP_OP))

    def browser_opens_on_a_link(self, test, grants, home_works=True):
        from mobile_agent.tests.test_mcp_new_tools import FakeDeviceRun, Screens
        grants.add("com.chase.sig", policy.APP_LABEL, policy.APP_OP, "Chase")
        grants.add("com.apple.mobilesafari", policy.APP_LABEL, policy.APP_OP, "Safari")
        self.assertFalse(test.call("screen", device="Work iPhone", image=False).is_error)
        run = FakeDeviceRun.opened[-1]
        phone = run.driver
        chrome = Screens("signin")
        chrome.data = {**chrome.data, "bundle": "com.google.chrome.ios"}
        phone.apps["com.google.chrome.ios"] = chrome
        run.open_url = lambda url: setattr(phone, "front", "com.google.chrome.ios")
        original = phone.call

        def call(method, path, body=None, timeout=10):
            if path == "/wda/pressButton" and home_works:
                phone.front = "com.chase.sig"
            return original(method, path, body, timeout)
        phone.call = call
        return phone

    def test_a_link_that_lands_in_an_app_nobody_allowed_names_it_and_says_what_it_waits_for(self):
        test, grants = self.harness()
        phone = self.browser_opens_on_a_link(test, grants)
        asked = []
        pending = {"state": "pending", "expiresAt": time.time() * 1000 + 8.5 * 60000}
        with patch.object(outside, "ask_app", lambda request, **kw: asked.append(request) or pending):
            result = test.call("open_url", device="Work iPhone", url="https://example.com/menu")
        self.assertEqual(asked[0]["title"], "Allow Mobster to use Chrome?")
        self.assertTrue(result.is_error)
        self.assertNotIn("The action failed", result.text)
        self.assertTrue(result.text.startswith("Opened “https://example.com/menu”."), result.text)
        self.assertIn("Chrome is in front, and Mobster is waiting for the user to allow it", result.text)
        self.assertIn("allow Chrome in the Mobster app", result.text)
        self.assertIn("ask you to use Safari instead", result.text)
        self.assertIn("pressed Home and read nothing in it", result.text)
        self.assertIn(("POST", "/wda/pressButton", {"name": "home"}), phone.calls)
        self.assertNotIn("com.google.chrome.ios", result.text.replace("launch_app com.google.chrome.ios", ""))

    def test_screen_and_launch_app_still_refuse_while_the_card_waits_and_say_what_to_do(self):
        test, grants = self.harness()
        phone = self.browser_opens_on_a_link(test, grants, home_works=False)
        pending = {"state": "pending", "expiresAt": time.time() * 1000 + 8.5 * 60000}
        with patch.object(outside, "ask_app", lambda request, **kw: pending):
            test.call("open_url", device="Work iPhone", url="https://example.com/menu")
            shown = test.call("screen", device="Work iPhone", image=False)
            phone.front = "com.chase.sig"
            launched = test.call("launch_app", device="Work iPhone", bundle_id="com.google.chrome.ios")
        self.assertTrue(shown.is_error)
        self.assertIn("Chrome is in front, and Mobster is waiting for the user to allow it", shown.text)
        self.assertIn("the phone still reports Chrome in front: call home", shown.text)
        self.assertIn("ask you to use Safari instead", shown.text)
        self.assertNotIn("Verification code", shown.text)
        self.assertTrue(launched.is_error)
        self.assertIn("Allow Mobster to use Chrome", launched.text)
        self.assertIn("make the same call again to keep waiting", launched.text)
        self.assertIn("nothing was done", launched.text)
        self.assertNotIn(("launch", "com.google.chrome.ios"), [c for c in phone.calls if isinstance(c, tuple)])

    def test_a_declined_app_is_named_and_not_reopened(self):
        test, grants = self.harness()
        self.browser_opens_on_a_link(test, grants)
        with patch.object(outside, "ask_app", lambda request, **kw: {"state": "denied"}):
            result = test.call("open_url", device="Work iPhone", url="https://example.com/menu")
        self.assertTrue(result.is_error)
        self.assertIn("Chrome is in front, and the user declined to let Mobster use it", result.text)
        self.assertIn("Don't open Chrome again unless the user says to", result.text)
        self.assertNotIn("The action failed", result.text)

    def test_wait_for_checks_app_permission_before_reading_assertions(self):
        test, _grants = self.harness()
        with patch.object(outside, "ask_app", return_value={"state": "denied"}), \
                patch("mobile_agent.tests.test_mcp_tools.FakeRun.evaluate") as evaluate:
            result = test.call("wait_for", device="Work iPhone", expect=[{"text": "private"}])
        self.assertTrue(result.is_error)
        evaluate.assert_not_called()

    def test_returning_home_after_an_app_refusal_requires_a_durable_intent(self):
        test, _grants = self.harness()
        with patch.object(outside, "ask_app", return_value={"state": "denied"}), \
                patch("mobile_agent.journal.os.fsync", side_effect=OSError("disk full")):
            result = test.call("screen", device="Work iPhone", image=False)
        self.assertTrue(result.is_error)
        self.assertIn("journal could not be saved", result.text)
        from mobile_agent.tests.test_mcp_new_tools import FakeDeviceRun
        self.assertNotIn(("POST", "/wda/pressButton", {"name": "home"}), FakeDeviceRun.opened[-1].driver.calls)

    def test_returning_home_never_retries_an_unknown_dispatch_outcome(self):
        import json
        test, _grants = self.harness()
        session = test.tools._device_session("Work iPhone")
        with patch.object(outside, "ask_app", return_value={"state": "denied"}), \
                patch.object(session.run.driver, "call", side_effect=TimeoutError("outcome unknown")) as dispatch:
            result = test.call("screen", device="Work iPhone", image=False)
        self.assertTrue(result.is_error)
        dispatch.assert_called_once_with("POST", "/wda/pressButton", {"name": "home"}, timeout=10)
        rows = [json.loads(line) for line in (session.run.run_dir / "actions.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["outcome"], "unknown")

    def test_typing_a_code_asks_without_return_too(self):
        test, grants = self.harness()
        grants.add("com.chase.sig", policy.APP_LABEL, policy.APP_OP, "Chase")
        asked = []
        with patch.object(outside, "ask_app", lambda request, **kw: asked.append(request) or {"state": "denied"}):
            result = test.call("type_text", device="Work iPhone", target={"label": "Verification code"},
                               text="482913")
        self.assertTrue(result.is_error)
        self.assertEqual([(r["act"], r["title"]) for r in asked], [("code", "Enter this code?")])
        from mobile_agent.tests.test_mcp_new_tools import FakeDeviceRun
        self.assertNotIn("482913", FakeDeviceRun.opened[-1].driver.typed)
        phone, written = FakeDeviceRun.opened[-1].driver, []
        phone.write_text = lambda element, snapshot, text, **kw: written.append(text) or {"value": text,
                                                                                         "matches": True}
        with patch.object(outside, "ask_app", lambda request, **kw: {"state": "approved"}):
            result = test.call("type_text", device="Work iPhone", target={"label": "Verification code"},
                               text="482913")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(written, ["482913"])

    def test_an_explicit_code_identifier_is_kept_when_the_fast_element_omits_it(self):
        from mobile_agent.tests.test_mcp_tools import Node, Tree
        test, grants = self.harness()
        grants.add("com.chase.sig", policy.APP_LABEL, policy.APP_OP, "Chase")
        self.assertFalse(test.call("screen", device="Work iPhone", image=False).is_error)
        session = test.tools.device_sessions[next(iter(test.tools.device_sessions))]
        element = Element("1", "Search", "SearchField", (.1, .3, .8, .05), True, locator="/app/code")
        node = Node("/app/code", "SearchField", identifier="oneTimeCode", label="Search", rect=(40, 240, 320, 40))
        tree = Tree([node], bundle_id="com.chase.sig")
        snapshot = Snapshot([element], "", 400, 800, "wda", bundle_id="com.chase.sig")
        written = Mock()
        session.run.driver.write_text = written
        with patch.object(test.tools, "_current", return_value=(tree, snapshot)), \
                patch.object(outside, "ask_app", return_value={"state": "denied"}) as ask:
            refused = test.call("type_text", device="Work iPhone", target={"id": "oneTimeCode"}, text="A7BC9D")
        self.assertTrue(refused.is_error)
        self.assertEqual(ask.call_args.args[0]["act"], "code")
        written.assert_not_called()

    def test_the_passcode_keypad_is_refused_even_without_a_phone_guard(self):
        from mobile_agent.tests.test_mcp_tools import Node, Tree
        test, grants = self.harness()
        grants.add("com.chase.sig", policy.APP_LABEL, policy.APP_OP, "Chase")
        self.assertFalse(test.call("screen", device="Work iPhone", image=False).is_error)
        session = next(iter(test.tools.device_sessions.values()))
        elements = [Element(str(n), str(n), "Button", (.1, .3, .1, .05), locator=f"/app/key/{n}")
                    for n in range(10)]
        tree = Tree([Node("/app/key/1", "Button", label="1", rect=(40, 240, 40, 40))],
                    bundle_id="com.apple.springboard")
        snapshot = Snapshot(elements, "", 400, 800, "wda", bundle_id="com.apple.springboard")
        with patch.object(test.tools, "_current", return_value=(tree, snapshot)), \
                patch.object(session.run.driver, "execute") as dispatch:
            refused = test.call("tap", device="Work iPhone", target={"label": "1"})
        self.assertTrue(refused.is_error)
        self.assertIn("passcode keypad", refused.text)
        dispatch.assert_not_called()


class SmartTests(unittest.TestCase):
    def test_a_declined_app_with_what_to_do_instead_carries_on(self):
        from mobile_agent.frontier import FrontierAgent
        from mobile_agent.tests.test_contract import Driver, Script, screen

        class LaunchScript(Script):
            def complete(self, messages, schema, timeout=60):
                out, usage = super().complete(messages, schema, timeout)
                for action in out.get("actions") or ():
                    if action["operation"] == "LAUNCH_APP":
                        action["app"] = "com.example.chat"
                return out, usage
        home = screen(Element("1", "QuickChat", "Icon", (.1, .1, .1, .1)), bundle="com.apple.springboard")
        for answer, carries_on in (("redirected:Use Mail instead", True), ("denied", False)):
            guard = PermissionGuard(None, AppPolicy(), {"com.example.chat": "QuickChat"}, grants=store(),
                                    ask=lambda request, answer=answer: answer)
            script = LaunchScript([[("LAUNCH_APP", None, None)], [("DONE", None, None)], [("DONE", None, None)]], [])
            driver = Driver([home] * 6)
            agent = FrontierAgent(driver, script, apps={"com.example.chat": "QuickChat"}, emit=lambda e: None,
                                  screenshots=False, settle_seconds=0, guard=guard)
            result = agent.run("Message Leo in QuickChat")
            self.assertNotIn(("LAUNCH_APP", "com.example.chat", None), driver.actions)
            if carries_on:
                self.assertTrue(any("Use Mail instead" in prompt for prompt in script.prompts[1:]))
            else:
                self.assertEqual(result.get("code") or result.get("status"), "blocked_app", result)

    def test_after_a_link_the_app_it_landed_in_is_checked_before_anything_is_read(self):
        from mobile_agent.agent_hooks import Prepared, SkillResult
        from mobile_agent.frontier import FrontierAgent
        from mobile_agent.tests.test_contract import Driver, Script, screen
        home = screen(Element("1", "Search", "SearchField", (.1, .1, .8, .05), editable=True),
                      bundle="com.apple.springboard")
        landed = screen(Element("1", "Feed", "StaticText", (.1, .1, .8, .05)), bundle="com.burbn.instagram")

        class Link:
            op, needs_target, prompt = "OPEN_URL", False, "- OPEN_URL text: open a link."

            def available(self, ctx):
                return True

            def prepare(self, ctx, act, snapshot):
                return Prepared(None, {})

            def perform(self, ctx, prepared, act, snapshot):
                ctx.driver.index = 1
                return SkillResult(feedback="Opened the link.", changed=True)
        for grants_it, stops in ((False, True), (True, False)):
            grants = store()
            if grants_it:
                grants.add("com.burbn.instagram", policy.APP_LABEL, policy.APP_OP, "Instagram")
            guard = PermissionGuard(None, AppPolicy(), {"com.burbn.instagram": "Instagram"}, ask=None, grants=grants)
            driver = Driver([home, landed, landed, landed, landed])
            script = Script([[("OPEN_URL", None, "instagram://feed")], [("DONE", None, None)],
                             [("DONE", None, None)]], [])
            agent = FrontierAgent(driver, script, apps={"com.burbn.instagram": "Instagram"}, emit=lambda e: None,
                                  screenshots=False, settle_seconds=0, guard=guard, skills=(Link(),))
            result = agent.run("Open my Instagram feed")
            if stops:
                self.assertIn(("HOME", None, None), driver.actions)
                self.assertFalse(any("Feed" in prompt for prompt in script.prompts))
            else:
                self.assertTrue(any("Feed" in prompt for prompt in script.prompts), result)

    def test_a_long_runs_ask_on_a_declared_send_never_meets_a_rule(self):
        from mobile_agent.frontier import FrontierAgent
        from mobile_agent.longform import session as S
        from mobile_agent.tests.test_contract import AgentLoopTests, Driver, Script, screen
        from mobile_agent.tests.test_longform import long_session
        def chat(value):
            return screen(Element("1", "Send", "Button", (.8, .9, .1, .04)),
                          Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value=value,
                                  placeholder="Message"), Element("4", "Leo", "NavigationBar", (0, 0, 1, .06)))
        exceptions = store()
        exceptions.add("com.example.chat", "send", rules.RULE_OP, "QuickChat")
        for session, expected in ((True, 1), (False, 0)):
            asked = []
            driver = Driver([chat("Message")] + [chat("Dinner is at 7")] * 6)
            script = Script([[("TYPE", "Message", "Dinner is at 7"), ("TAP", "Send", None)], [("DONE", None, None)],
                             [("DONE", None, None)]], AgentLoopTests.ITEMS)
            agent = FrontierAgent(driver, script, apps={"com.example.chat": "QuickChat"}, emit=lambda e: None,
                                  screenshots=False, settle_seconds=0,
                                  permissions=PermissionGate(DONT_ASK, apps=OPEN, exceptions=exceptions),
                                  floor_approve=lambda request: asked.append(request) or "approved",
                                  longform=long_session() if session else None,
                                  forced_approve=(lambda request: asked.append(request) or "denied") if session
                                  else None)
            try:
                agent.run(AgentLoopTests.REQ)
            finally:
                S.unbind("r1")
            self.assertEqual(len(asked), expected, session)

    def test_a_declined_app_without_an_instruction_stops(self):
        guard = PermissionGuard(None, AppPolicy(), {"com.example.chat": "QuickChat"}, grants=store(),
                                ask=lambda request: "denied")
        self.assertFalse(guard.allows_app("com.example.chat"))
        self.assertIsNone(guard.take_instruction())


class ServerStartTests(unittest.TestCase):
    def test_the_instruction_reaches_the_task_and_not_now_stops_it(self):
        from mobile_agent import engines
        from mobile_agent.runtime_driver import PhoneNotReady
        from mobile_agent.server import Runtime
        from mobile_agent.steering import SteeringQueue

        def run(answer):
            guard = PermissionGuard(None, AppPolicy(), {"com.burbn.instagram": "Instagram"}, grants=store(),
                                    ask=lambda request: answer)
            events = []
            return SimpleNamespace(goal="Post my photo", app={"bundleId": "com.burbn.instagram", "name": "Instagram"},
                                   pinned_apps=(), allowed_bundles=None, guard=guard, engine=engines.SMART,
                                   dry_run=False, stop=SimpleNamespace(is_set=lambda: False),
                                   steering=SteeringQueue(), emit=events.append, start_on_home=False), events
        runtime = SimpleNamespace(apps_for=lambda phone: [])
        redirected, events = run("redirected:Post it from Photos instead")
        self.assertTrue(Runtime.approve_start_apps(runtime, redirected))
        self.assertTrue(redirected.start_on_home)
        self.assertEqual([e["text"] for e in events if e["event"] == "user_message"],
                         ["Post it from Photos instead"])
        declined, _ = run("denied")
        with self.assertRaises(PhoneNotReady):
            Runtime.approve_start_apps(runtime, declined)


if __name__ == "__main__":
    unittest.main()

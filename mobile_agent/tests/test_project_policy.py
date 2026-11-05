"""A project's policy (permissions/project_policy.py): it adds questions and removes none."""

import datetime
import os
import random
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from zoneinfo import ZoneInfo

from mobile_agent import runtime_limits
from mobile_agent.permissions import (
    ASK_ALL,
    ASK_IMPORTANT,
    DONT_ASK,
    Action,
    AppPolicy,
    PermissionGate,
    PermissionGuard,
)
from mobile_agent.permissions import policy as P
from mobile_agent.permissions import project_policy as PP
from mobile_agent.permissions.exceptions import Exceptions
from mobile_agent.state import Element

OPEN = P.ALLOW_ALL
MODES = (ASK_ALL, ASK_IMPORTANT, DONT_ASK)
NEW_YORK = ZoneInfo("America/New_York")


def at(day, hour, minute=0, zone=NEW_YORK):
    """Epoch seconds of 2026-10-``day`` ``hour``:``minute`` (the 19th is a Monday)."""
    return datetime.datetime(2026, 10, day, hour, minute, tzinfo=zone).timestamp()


def policy_of(text, counts=None):
    return PP.parse(text, counts=counts)


def send(**fields):
    return Action("TAP", "Send", "Button", app=fields.pop("app", "com.example.chat"), **fields)


def save(**fields):
    return Action("TAP", "Save", "Button", app=fields.pop("app", "com.apple.mobilenotes"), **fields)


def asks(mode, action, policy=None, act=None, **gate):
    return PermissionGate(mode, apps=OPEN, policy=policy, **gate).decide(action, existing_act=act)


class ParseTests(unittest.TestCase):

    def test_a_full_file_loads_every_field(self):
        policy = policy_of("""
version: 1
default: allow
rules:
  - name: Work hours
    profile: "@getmobster"
    app: com.burbn.instagram
    kind: [post, comment]
    action: allow
    window: mon-fri 09:00-18:00
    timezone: America/New_York
    max_per_day: 5
  - kind: buy
""")
        first, second = policy.rules
        self.assertEqual((first.name, first.profile, first.app, first.kinds, first.action, first.max_per_day),
                         ("Work hours", "@getmobster", "com.burbn.instagram", frozenset({"post", "comment"}), "allow",
                             5))
        self.assertEqual((first.days, first.start, first.end), (frozenset(range(5)), 540, 1080))
        # A rule that says nothing about its action asks: the safe way round.
        self.assertEqual((second.action, second.app, second.profile, second.kinds), ("ask", None, None,
                                                                                      frozenset({"buy"})))

    def test_a_mistake_is_named_and_never_ignored(self):
        bad = {
            "x: 1": "the file takes",
            "version: 2": "version is 1",
            "default: maybe": "default is allow or ask",
            "rules: nope": "rules is a list",
            "rules:\n  - actoin: ask": "rule 1: a rule takes",
            "rules:\n  - action: deny": "rule 1: action is allow or ask",
            "rules:\n  - kind: sneeze": "rule 1: kind is one of",
            "rules:\n  - kind: []": "rule 1: kind is one of",
            "rules:\n  - app: instagram": "rule 1: app is a bundle id",
            "rules:\n  - action: ask\n    window: 09:00-10:00": "always asks",
            "rules:\n  - action: ask\n    max_per_day: 3": "always asks",
            "rules:\n  - action: allow\n    window: whenever": "rule 1: window is days and hours",
            "rules:\n  - action: allow\n    window: mon-fri 09:00-09:00": "start and an end that differ",
            "rules:\n  - action: allow\n    window: fun-sat 09:00-10:00": "window's days",
            "rules:\n  - action: allow\n    window: 09:00-25:00": "24-hour",
            "rules:\n  - action: allow\n    max_per_day: 0": "whole number from 1",
            "rules:\n  - action: allow\n    max_per_day: true": "whole number from 1",
            "rules:\n  - action: allow\n    max_per_day: 2.5": "whole number from 1",
            "rules:\n  - action: allow\n    timezone: Mars/Base": "time zone",
            "rules:\n  - name: ''": "name is text",
            "- just\n- a list": "the file takes",
        }
        for text, words in bad.items():
            with self.assertRaises(PP.PolicyError, msg=text) as raised:
                policy_of(text)
            self.assertIn(words, str(raised.exception), text)
        with self.assertRaises(PP.PolicyError):
            policy_of("rules: [unclosed")
        with self.assertRaises(PP.PolicyError):
            policy_of("rules:\n" + "  - kind: post\n" * (PP.MAX_RULES + 1))

    def test_only_a_rule_that_can_ask_makes_the_policy_narrow(self):
        with self.assertRaises(PP.PolicyError):
            policy_of("")
        self.assertFalse(policy_of("version: 1").narrows)
        self.assertFalse(policy_of("rules:\n  - action: allow\n    kind: post").narrows)
        for narrowing in ("default: ask", "rules:\n  - kind: post",
                          "rules:\n  - action: allow\n    window: 09:00-10:00",
                          "rules:\n  - action: allow\n    max_per_day: 1"):
            self.assertTrue(policy_of(narrowing).narrows, narrowing)

    def test_the_file_is_read_from_the_projects_mobster_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder).resolve()
            self.assertIsNone(PP.load(folder))
            (folder / ".mobster").mkdir()
            self.assertIsNone(PP.load(folder))
            (folder / ".mobster" / "policy.yaml").write_text("rules:\n  - kind: post\n")
            self.assertEqual(len(PP.load(folder).rules), 1)
            # Walks up from a folder inside the project, as `mobster test` does.
            inner = folder / "App" / "Sources"
            inner.mkdir(parents=True)
            self.assertEqual(PP.project_folder(inner), folder)
            self.assertIsNone(PP.project_folder(tempfile.gettempdir() + "/nothing-here-xyz"))

    def test_a_file_that_cannot_be_used_asks_before_every_act(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder).resolve()
            (folder / ".mobster").mkdir()
            file = folder / ".mobster" / "policy.yaml"
            for text, words in (("rules: [", "isn't valid YAML"), ("rules:\n  - kind: sneeze\n", "kind is one of"),
                                ("x" * (PP.MAX_BYTES + 1), "at most 64 KB")):
                file.write_text(text)
                policy = PP.load(folder)
                self.assertIsNotNone(policy.error, text[:20])
                self.assertIn(words, policy.error)
                self.assertTrue(policy.narrows)
                for kinds in ({"post"}, {"setting"}, {"other"}):
                    self.assertIn("can't be read", policy.asks(frozenset(kinds), "com.example.app"))
            file.unlink()
            file.write_text("rules: []\n")
            other = folder / "real.yaml"
            other.write_text("rules:\n  - action: allow\n")
            file.unlink()
            file.symlink_to(other)
            self.assertIn("not a link", PP.load(folder).error)
            file.unlink()
            file.write_bytes(b"\xff\xfe\x00bad")
            self.assertTrue(PP.load(folder).error)


class RuleTests(unittest.TestCase):

    def test_an_act_is_in_every_kind_that_names_it(self):
        self.assertEqual(PP.kinds_of("commit", "send_message"), {"message"})
        self.assertEqual(PP.kinds_of("commit", "Place Order"), {"buy"})
        self.assertEqual(PP.kinds_of("commit", "subscribe"), {"follow", "buy"})
        self.assertEqual(PP.kinds_of("commit", "unsend"), {"message", "delete"})
        self.assertEqual(PP.kinds_of("commit", "a thing nobody named"), {"other"})
        self.assertEqual(PP.kinds_of("commit", None), {"other"})
        self.assertEqual(PP.kinds_of("change", "save"), {"change"})
        self.assertEqual(PP.kinds_of("change", "whatever"), {"change"})

    def test_a_rule_reaches_its_app_its_profile_and_its_kind_only(self):
        policy = policy_of("""
rules:
  - app: com.burbn.instagram
    profile: "@getmobster"
    kind: post
""")
        getmobster = {"id": "a1b2", "handle": "getmobster"}
        post = frozenset({"post"})
        self.assertTrue(policy.asks(post, "com.burbn.instagram", getmobster))
        self.assertTrue(policy.asks(post, "com.burbn.instagram", {"id": "x", "handle": "@GETMOBSTER"}))
        self.assertTrue(policy.asks(post, "com.burbn.instagram", {"id": "getmobster"}))
        self.assertIsNone(policy.asks(post, "com.burbn.instagram", {"id": "z", "handle": "other"}))
        self.assertIsNone(policy.asks(post, "com.burbn.instagram", None), "a task with no profile is no profile's")
        self.assertIsNone(policy.asks(post, "com.zhiliaoapp.musically", getmobster))
        self.assertIsNone(policy.asks(frozenset({"message"}), "com.burbn.instagram", getmobster))

    def test_any_covers_every_kind_and_default_ask_covers_what_no_rule_lists(self):
        policy = policy_of("default: ask\nrules:\n  - action: allow\n    app: com.apple.mobilenotes\n    kind: change")
        self.assertIsNone(policy.asks(frozenset({"change"}), "com.apple.mobilenotes"))
        self.assertIn("doesn't list", policy.asks(frozenset({"message"}), "com.apple.MobileSMS"))
        self.assertIn("doesn't list", policy.asks(frozenset({"change"}), "com.apple.reminders"))
        everything = policy_of("rules:\n  - kind: any\n    app: '*'")
        self.assertTrue(everything.asks(frozenset({"other"}), "com.example.app"))
        self.assertTrue(everything.asks(frozenset({"setting"}), "com.example.app"))

    def test_every_matching_rule_must_agree_so_the_stricter_wins(self):
        policy = policy_of("""
rules:
  - action: allow
    kind: post
  - action: ask
    kind: post
    app: com.burbn.instagram
    name: Instagram posts
""")
        self.assertIn("“Instagram posts” asks", policy.asks(frozenset({"post"}), "com.burbn.instagram"))
        self.assertIsNone(policy.asks(frozenset({"post"}), "com.example.app"))

    def test_a_window_allows_only_inside_it_in_its_own_time_zone(self):
        policy = policy_of("""
rules:
  - action: allow
    window: mon-fri 09:00-18:00
    timezone: America/New_York
""")
        post = frozenset({"post"})
        self.assertIsNone(policy.asks(post, "a.b", now=at(19, 9, 0)))
        self.assertIsNone(policy.asks(post, "a.b", now=at(19, 17, 59)))
        self.assertIn("only mon-fri 09:00-18:00", policy.asks(post, "a.b", now=at(19, 18, 0)))
        self.assertTrue(policy.asks(post, "a.b", now=at(19, 8, 59)))
        self.assertTrue(policy.asks(post, "a.b", now=at(24, 12)), "a Saturday")
        # The same instant is evening in New York and morning in Tokyo: the rule's zone decides.
        self.assertTrue(policy.asks(post, "a.b", now=at(19, 20, 0, ZoneInfo("Asia/Tokyo")) + 86400))

    def test_a_window_past_midnight_belongs_to_the_day_it_started(self):
        policy = policy_of("rules:\n  - action: allow\n    window: fri,sat 22:00-02:00\n    timezone: America/New_York")
        post = frozenset({"post"})
        self.assertIsNone(policy.asks(post, "a.b", now=at(23, 23, 0)), "Friday 23:00")
        self.assertIsNone(policy.asks(post, "a.b", now=at(24, 1, 0)), "Saturday 01:00, Friday's")
        self.assertIsNone(policy.asks(post, "a.b", now=at(25, 1, 0)), "Sunday 01:00, Saturday's")
        self.assertTrue(policy.asks(post, "a.b", now=at(26, 1, 0)), "Monday 01:00 follows no listed day")
        self.assertTrue(policy.asks(post, "a.b", now=at(22, 23, 0)), "Thursday 23:00")
        self.assertTrue(policy.asks(post, "a.b", now=at(23, 12, 0)), "Friday noon")


class CapTests(unittest.TestCase):
    CAP = "rules:\n  - action: allow\n    kind: post\n    max_per_day: 2\n    timezone: America/New_York\n"

    def test_acts_that_went_unasked_are_counted_and_the_next_one_asks(self):
        policy = policy_of(self.CAP)
        post = frozenset({"post"})
        for _ in range(2):
            self.assertIsNone(policy.asks(post, "a.b", now=at(19, 10)))
            self.assertIsNone(policy.take(post, "a.b", now=at(19, 10)))
        self.assertIn("today's limit of 2", policy.asks(post, "a.b", now=at(19, 23, 59)))
        self.assertTrue(policy.take(post, "a.b", now=at(19, 23, 59)))
        # The day is the rule's: a minute later in New York it is a new one.
        self.assertIsNone(policy.asks(post, "a.b", now=at(20, 0, 1)))
        # Acts of another kind are not counted against it.
        self.assertIsNone(policy.take(frozenset({"message"}), "a.b", now=at(19, 10)))

    def test_the_count_survives_a_restart_and_is_shared_by_processes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state" / "policy-counts.json"
            first, second = (policy_of(self.CAP, PP.Counts(path)) for _ in range(2))
            post = frozenset({"post"})
            self.assertIsNone(first.take(post, "a.b", now=at(19, 10)))
            self.assertIsNone(second.take(post, "a.b", now=at(19, 10)))
            self.assertTrue(first.asks(post, "a.b", now=at(19, 11)))
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_two_workers_never_take_more_than_the_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "policy-counts.json"
            post, taken, lock = frozenset({"post"}), [], threading.Lock()
            now = at(19, 10)
            text = self.CAP.replace("max_per_day: 2", "max_per_day: 5")

            def work():
                policy = policy_of(text, PP.Counts(path))
                for _ in range(4):
                    if policy.take(post, "a.b", now=now) is None:
                        with lock:
                            taken.append(1)
            threads = [threading.Thread(target=work) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(taken), 5)

    def test_counts_that_cannot_be_read_ask(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "policy-counts.json"
            path.write_text("not json")
            policy = policy_of(self.CAP, PP.Counts(path))
            post = frozenset({"post"})
            self.assertIn("limit", policy.asks(post, "a.b", now=at(19, 10)))
            self.assertTrue(policy.take(post, "a.b", now=at(19, 10)))
            self.assertEqual(path.read_text(), "not json", "an unreadable file is left for the person to look at")

    def test_raising_the_cap_keeps_todays_count(self):
        counts = PP.Counts()
        post = frozenset({"post"})
        first = policy_of(self.CAP, counts)
        first.take(post, "a.b", now=at(19, 10))
        first.take(post, "a.b", now=at(19, 10))
        second = policy_of(self.CAP.replace("max_per_day: 2", "max_per_day: 3"), counts)
        self.assertIsNone(second.asks(post, "a.b", now=at(19, 11)))
        second.take(post, "a.b", now=at(19, 11))
        self.assertTrue(second.asks(post, "a.b", now=at(19, 11)))


class GateTests(unittest.TestCase):
    """The policy makes the engine ask where it acted before, and never where it asked or refused before."""

    def test_the_policy_asks_where_the_mode_let_the_act_go_ahead(self):
        sending = policy_of("rules:\n  - kind: message\n    name: No unasked messages")
        before = asks(DONT_ASK, send(), act="send")
        after = asks(DONT_ASK, send(), sending, act="send")
        self.assertEqual((before.decision, after.decision, after.by_policy), ("allow", "ask", True))
        self.assertIn("No unasked messages", after.reason)
        self.assertEqual(after.title, "Send this message?")
        self.assertFalse(after.rememberable, "no Don't ask again for what the policy asks")
        # A change that stays on the phone: ask_important never asked.
        notes = policy_of("rules:\n  - kind: change\n    app: com.apple.mobilenotes")
        self.assertEqual(asks(ASK_IMPORTANT, save()).decision, "allow")
        decision = asks(ASK_IMPORTANT, save(), notes)
        self.assertEqual((decision.decision, decision.kind, decision.by_policy), ("ask", "change", True))
        self.assertEqual(asks(ASK_IMPORTANT, save(app="com.apple.reminders"), notes).decision, "allow")

    def test_a_remembered_allow_rule_does_not_outrank_the_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Exceptions(Path(folder) / "exceptions.json")
            action = Action("TAP", "Send", "Button", app="com.example.chat", app_name="Chat")
            self.assertEqual(asks(ASK_IMPORTANT, action, act="send", exceptions=store).decision, "ask")
            store.add("com.example.chat", "send", "ACT", "Chat")
            self.assertEqual(asks(ASK_IMPORTANT, action, act="send", exceptions=store).decision, "allow")
            policy = policy_of("rules:\n  - kind: message")
            decision = asks(ASK_IMPORTANT, action, policy, act="send", exceptions=store)
            self.assertEqual((decision.decision, decision.by_policy), ("ask", True))

    def test_an_ask_the_mode_made_stays_and_stops_offering_dont_ask_again(self):
        action = Action("TAP", "Send", "Button", app="com.example.chat", app_name="Chat")
        plain = asks(ASK_IMPORTANT, action, act="send")
        self.assertTrue(plain.rememberable)
        both = asks(ASK_IMPORTANT, action, policy_of("rules:\n  - kind: message"), act="send")
        self.assertEqual((both.decision, both.by_policy, both.rememberable, both.rule), ("ask", False, False, None))
        quiet = asks(ASK_IMPORTANT, action, policy_of("rules:\n  - kind: buy"), act="send")
        self.assertEqual((quiet.decision, quiet.rememberable), ("ask", True))

    def test_allow_rules_remove_no_question_in_any_mode(self):
        everything = policy_of("rules:\n  - action: allow\n    kind: any\n    app: '*'\n    max_per_day: 1000")
        code = Action("TYPE", "Verification code", "TextField", text="482913", app="com.example.bank")
        for mode in MODES:
            self.assertEqual(asks(mode, code, everything).decision, "ask", mode)
            self.assertEqual(asks(mode, code, everything).kind, "floor", mode)
        for mode in (ASK_ALL, ASK_IMPORTANT):
            self.assertEqual(asks(mode, send(), everything, act="send").decision, "ask", mode)
        self.assertEqual(asks(ASK_ALL, save(), everything).decision, "ask")
        # Opening an app is the app list's, which the policy never touches.
        guard = PermissionGuard(None, AppPolicy(blocked=frozenset({"com.example.bank"})))
        self.assertFalse(guard.request(["com.example.bank"]))

    def test_the_policy_is_never_looser_than_the_mode_for_any_rules(self):
        rng = random.Random(7)
        acts = (None, "send", "post", "react", "follow", "buy", "delete", "call", "book", "other")
        actions = [send(), save(), Action("TAP", "Wi-Fi", "Switch", app="com.apple.Preferences", value="1"),
                   Action("TAP", "Like", "Button", app="com.burbn.instagram"),
                   Action("TAP", "Home", "Tab", app="com.example.chat"),
                   Action("TYPE", "Verification code", "TextField", text="482913", app="com.example.bank")]
        for _ in range(60):
            lines = ["default: " + rng.choice(("allow", "ask")), "rules:"]
            for _ in range(rng.randint(0, 4)):
                lines.append(f"  - kind: {rng.choice(PP.KINDS)}")
                lines.append(f"    action: {rng.choice(('allow', 'ask'))}")
                if lines[-1].endswith("allow") and rng.random() < .5:
                    lines.append(f"    max_per_day: {rng.randint(1, 3)}")
            policy = policy_of("\n".join(lines))
            for mode in MODES:
                for action in actions:
                    for act in acts:
                        base = asks(mode, action, act=act)
                        narrowed = asks(mode, action, policy, act=act)
                        if base.asks:
                            self.assertTrue(narrowed.asks, (lines, mode, action.label, act))
                            self.assertEqual((narrowed.kind, narrowed.act), (base.kind, base.act))
                        elif narrowed.asks:
                            self.assertTrue(narrowed.by_policy and narrowed.kind in ("commit", "change"))
                        if base.kind in ("navigate", "input", "floor"):
                            self.assertEqual(narrowed.decision, base.decision)

    def test_a_policy_ask_is_said_in_the_gates_event(self):
        events = []
        gate = PermissionGate(DONT_ASK, apps=OPEN, emit=events.append, policy=policy_of("rules:\n  - kind: message"))
        gate.decide(send(), existing_act="send")
        self.assertEqual([(e["event"], e["decision"], e["policy"]) for e in events],
                         [("permission_decision", "ask", True)])
        self.assertNotIn("label", events[0])

    def test_a_cap_counts_what_went_ahead_unasked_and_not_what_asked(self):
        policy = policy_of("rules:\n  - action: allow\n    kind: message\n    max_per_day: 2")
        decisions = [asks(DONT_ASK, send(), policy, act="send") for _ in range(4)]
        self.assertEqual([d.decision for d in decisions], ["allow", "allow", "ask", "ask"])
        self.assertIn("limit", decisions[2].reason)
        # In a mode that asks, nothing goes ahead unasked, so nothing is counted.
        fresh = policy_of("rules:\n  - action: allow\n    kind: message\n    max_per_day: 1")
        for _ in range(3):
            self.assertEqual(asks(ASK_IMPORTANT, send(), fresh, act="send").by_policy, False)
        self.assertEqual(asks(DONT_ASK, send(), fresh, act="send").decision, "allow")


class EngineTests(unittest.TestCase):
    """The frontier agent, with a policy: it asks where it sent before, and still refuses with nobody to ask."""
    REQ = "Message Leo in QuickChat that dinner is at 7."

    def run_agent(self, mode, policy, answer="approved", asker=True):
        from mobile_agent.frontier import FrontierAgent
        from mobile_agent.tests.test_contract import AgentLoopTests, Driver, Script, screen
        asked = []

        def approve(request):
            asked.append(request)
            return answer
        chat = lambda text: screen(Element("1", "Send", "Button", (.8, .9, .1, .04)),  # noqa: E731
                                   Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True,
                                           value=text or "Message", placeholder="Message"),
                                   Element("4", "Leo", "NavigationBar", (0, 0, 1, .06)))
        sent, done = chat("Dinner is at 7"), screen(
            Element("3", "Dinner is at 7", "StaticText", (.1, .5, .6, .04)),
            Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value="Message"))
        driver = Driver([chat(""), sent, sent, done, done])
        script = Script([[("TYPE", "Message", "Dinner is at 7"), ("TAP", "Send", None)], [("DONE", None, None)]],
                        AgentLoopTests.ITEMS)
        gate = PermissionGate(mode, apps=OPEN, policy=policy)
        hooks = {"approve": None, "floor_approve": approve if asker else None} if mode == DONT_ASK else {
            "approve": approve if asker else None}
        FrontierAgent(driver, script, apps={"com.example.chat": "QuickChat"}, emit=lambda e: None, screenshots=False,
                      settle_seconds=0, permissions=gate, **hooks).run(self.REQ)
        return driver, asked

    def test_full_access_sent_without_asking_and_the_policy_makes_it_ask(self):
        driver, asked = self.run_agent(DONT_ASK, None)
        self.assertEqual(asked, [])
        self.assertIn(("TAP", "Send", None), driver.actions)
        driver, asked = self.run_agent(DONT_ASK, policy_of("rules:\n  - kind: message\n    name: Ask first"))
        self.assertEqual([r["label"] for r in asked], ["Send"])
        self.assertIn("Ask first", asked[0]["reason"])
        self.assertIn(("TAP", "Send", None), driver.actions)
        self.assertIsNone(asked[0]["remember"], "the policy's ask offers no Don't ask again")

    def test_the_policys_ask_declined_sends_nothing(self):
        driver, asked = self.run_agent(DONT_ASK, policy_of("rules:\n  - kind: message"), answer="denied")
        self.assertEqual(len(asked), 1)
        self.assertNotIn(("TAP", "Send", None), driver.actions)

    def test_with_nobody_to_answer_a_policy_ask_is_refused_even_in_full_access(self):
        driver, asked = self.run_agent(DONT_ASK, None, asker=False)
        self.assertIn(("TAP", "Send", None), driver.actions)
        driver, asked = self.run_agent(DONT_ASK, policy_of("rules:\n  - kind: message"), asker=False)
        self.assertNotIn(("TAP", "Send", None), driver.actions)
        self.assertEqual(asked, [])

    def test_a_policy_that_allows_still_asks_where_the_mode_asks_and_still_refuses(self):
        allow = policy_of("rules:\n  - action: allow\n    kind: any\n    app: '*'")
        driver, asked = self.run_agent(ASK_IMPORTANT, allow)
        self.assertEqual(len(asked), 1)
        driver, asked = self.run_agent(ASK_IMPORTANT, allow, asker=False)
        self.assertNotIn(("TAP", "Send", None), driver.actions)
        driver, asked = self.run_agent(ASK_IMPORTANT, allow, answer="denied")
        self.assertNotIn(("TAP", "Send", None), driver.actions)


class RunPolicyTests(unittest.TestCase):

    def make(self, folder, text="rules:\n  - kind: post\n"):
        (Path(folder) / ".mobster").mkdir(exist_ok=True)
        (Path(folder) / ".mobster" / "policy.yaml").write_text(text)

    def test_it_is_off_until_the_flag_is_on(self):
        with tempfile.TemporaryDirectory() as folder:
            self.make(folder)
            run = NS(extras={"project": {"id": "p", "path": folder}}, origin="app")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(runtime_limits.run_policy(run), (None, None))
            with patch.dict(os.environ, {"MOBSTER_POLICY": "off"}, clear=True):
                self.assertEqual(runtime_limits.run_policy(run), (None, None))
            with patch.dict(os.environ, {"MOBSTER_POLICY": "on"}, clear=True):
                policy, profile = runtime_limits.run_policy(run)
            self.assertEqual(len(policy.rules), 1)

    def test_a_task_names_its_project_and_a_local_task_uses_its_folder(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"MOBSTER_POLICY": "on"}, clear=True):
            self.make(folder)
            account = {"id": "a1", "handle": "getmobster"}
            named = NS(extras={"project": {"id": "p", "path": folder}, "account": account}, origin="mcp")
            policy, profile = runtime_limits.run_policy(named, Path(folder) / "state" / "terminal.sqlite3")
            self.assertEqual((len(policy.rules), profile), (1, account))
            self.assertEqual(policy.counts.path, Path(folder) / "state" / "policy-counts.json")
            # Without a project, an outside task has none: only a task from this terminal has a working folder.
            for origin in ("app", "mcp", "remote", "api", "schedule"):
                self.assertEqual(runtime_limits.run_policy(NS(extras={}, origin=origin)), (None, None), origin)
            with patch.object(PP, "project_folder", return_value=Path(folder)):
                for origin in ("cli", "tui"):
                    self.assertEqual(len(runtime_limits.run_policy(NS(extras={}, origin=origin))[0].rules), 1)
            with patch.object(PP, "project_folder", return_value=None):
                self.assertEqual(runtime_limits.run_policy(NS(extras={}, origin="cli")), (None, None))

    def test_the_run_gate_carries_the_policy_and_the_profile(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"MOBSTER_POLICY": "on"}, clear=True):
            self.make(folder, "rules:\n  - kind: message\n    profile: getmobster\n")
            account = {"id": "a1", "handle": "getmobster"}
            run = NS(extras={"project": {"id": "p", "path": folder}, "account": account}, ask_override=None,
                     origin="app", goal="Message Leo", emit=lambda event: None)
            runtime = NS(remembering=lambda run_: None, permission_exceptions=lambda: None, config=NS(state_db=None))
            from mobile_agent import server
            gate = server.Runtime.permission_gate(runtime, run)
            self.assertTrue(gate.narrowed)
            self.assertEqual(gate.profile, account)
            self.assertEqual(gate.decide(send(), existing_act="send").by_policy, False, "ask_important asks already")
            gate.mode = DONT_ASK
            self.assertTrue(gate.decide(send(), existing_act="send").by_policy)
            gate.profile = {"id": "b", "handle": "someone-else"}
            self.assertEqual(gate.decide(send(), existing_act="send").decision, "allow")

    def test_mcp_taps_use_the_policy_of_the_folder_the_server_runs_in(self):
        from mobile_agent.permissions import outside
        with tempfile.TemporaryDirectory() as folder:
            self.make(folder)
            with patch.object(PP, "project_folder", return_value=Path(folder)):
                with patch.dict(os.environ, {}, clear=True):
                    self.assertIsNone(outside._project_policy())
                with patch.dict(os.environ, {"MOBSTER_POLICY": "on"}, clear=True):
                    self.assertEqual(len(outside._project_policy().rules), 1)


if __name__ == "__main__":
    unittest.main()

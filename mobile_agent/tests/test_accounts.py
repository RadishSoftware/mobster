
import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import fleet_health as fh
from mobile_agent.accounts import context as account_context
from mobile_agent.accounts import screen as account_screen
from mobile_agent.accounts.store import AccountNotFound, AccountStore, AmbiguousAccount, mentioned, resolve, store_for
from mobile_agent.api_errors import APIError
from mobile_agent.frontier import FrontierAgent
from mobile_agent.state import Element
from mobile_agent.tests.test_engines import COMPOSE, Driver, Script, draft, item, screen, sent

INSTAGRAM = {"bundleId": "com.burbn.instagram", "name": "Instagram"}
TIKTOK = {"bundleId": "com.zhiliaoapp.musically", "name": "TikTok"}
MESSAGES = {"bundleId": "com.apple.MobileSMS", "name": "Messages"}


def profile(handle, *, own=True, bundle="com.apple.MobileSMS", at=False):
    top = (Element("10", handle, "NavigationBar", (0, .05, 1, .06)) if not at else
           Element("10", f"@{handle}", "StaticText", (.1, .3, .6, .04)))
    control = Element("11", "Edit profile" if own else "Follow", "Button", (.1, .4, .4, .05))
    return screen(top, control, Element("12", "Chats", "Button", (.1, .95, .2, .04)), bundle=bundle)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = AccountStore(tempfile.mkdtemp(prefix="mobster-accounts-"))

    def test_add_change_and_remove(self):
        added = self.store.add({"handle": "@getmobster", "app": INSTAGRAM, "notes": "Warm, short, no emoji",
                                "links": [{"label": "Shop", "url": "https://mobster.dev/download"}],
                                "variables": {"caption": "New drop today"}}, device=("usb-1", "Studio iPhone"))
        self.assertEqual(added["handle"], "getmobster")
        self.assertEqual((added["device"], added["deviceName"]), ("usb-1", "Studio iPhone"))
        changed = self.store.update(added["id"], {"signature": "— Team Radish Software", "device": None})
        self.assertEqual(changed["signature"], "— Team Radish Software")
        self.assertIsNone(changed["device"])
        self.assertEqual(changed["notes"], "Warm, short, no emoji")
        self.store.remove(added["id"])
        self.assertEqual(self.store.all(), [])
        with self.assertRaises(AccountNotFound):
            self.store.remove(added["id"])

    def test_one_handle_once_per_app_and_on_several_apps(self):
        self.store.add({"handle": "getmobster", "app": INSTAGRAM})
        self.store.add({"handle": "getmobster", "app": TIKTOK})
        with self.assertRaisesRegex(ValueError, "already in your profiles"):
            self.store.add({"handle": "GETMOBSTER", "app": INSTAGRAM})

    def test_what_the_phone_should_be_set_to_is_optional_checked_and_kept(self):
        added = self.store.add({"handle": "getmobster", "app": INSTAGRAM, "region": "gb", "language": "EN",
                                "timezone": "Europe/London"})
        self.assertEqual((added["region"], added["language"], added["timezone"]), ("GB", "en", "Europe/London"))
        again = self.store.all()[0]
        self.assertEqual((again["region"], again["language"], again["timezone"]), ("GB", "en", "Europe/London"))
        cleared = self.store.update(added["id"], {"region": None, "timezone": ""})
        self.assertIsNone(cleared["region"])
        self.assertEqual([k for k in ("region", "language", "timezone") if self.store.all()[0].get(k)], ["language"])
        for bad in ({"region": "USA"}, {"language": "e"}, {"timezone": "Mars/Base"}, {"timezone": 5}):
            with self.assertRaises(ValueError):
                self.store.update(added["id"], bad)
        for bad, said in (({"timezone": "Mars/Olympus_Mons"}, "'Mars/Olympus_Mons' isn't a time zone Mobster knows. "
                            "Use a name such as America/Chicago, or leave it empty."),
                          ({"region": "Narnia"}, "'Narnia' isn't a region Mobster can use. Use a two-letter country "
                           "code such as US, or leave it empty.")):
            with self.assertRaises(ValueError) as caught:
                self.store.update(added["id"], bad)
            self.assertEqual(str(caught.exception), said)
        plain = self.store.add({"handle": "other", "app": INSTAGRAM})
        self.assertNotIn("region", plain)

    def test_a_value_too_long_is_named_as_the_form_names_it(self):
        for body, said in (({"signature": "x" * 301},
                            "A signature can be at most 300 characters. Shorten it and try again."),
                           ({"notes": "x" * 2001}, "Notes can be at most 2,000 characters. Shorten it and try again.")):
            with self.subTest(body=body), self.assertRaises(ValueError) as caught:
                self.store.add({"handle": "getmobster", "app": INSTAGRAM, **body})
            self.assertEqual(str(caught.exception), said)
        self.assertEqual(self.store.all(), [])

    def test_values_are_checked_and_secrets_refused(self):
        for body, words in (({"handle": "ac me", "app": INSTAGRAM}, "handle"),
                            ({"handle": "getmobster", "app": {"bundleId": "instagram", "name": "Instagram"}}, "bundle"),
                            ({"handle": "getmobster", "app": INSTAGRAM, "links": [{"label": "Shop",
                                "url": "mobster.dev"}]},
                             "https://"),
                            ({"handle": "getmobster", "app": INSTAGRAM, "variables": {"handle": "x"}}, "already"),
                            ({"handle": "getmobster", "app": INSTAGRAM, "notes": "password: hunter2!"}, "passwords"),
                            ({"handle": "getmobster", "app": INSTAGRAM,
                                "variables": {"key": "sk-ant-abcdefghijklmnop1234"}},
                             "API keys")):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, words):
                self.store.add(body)
        ok = self.store.add({"handle": "getmobster", "app": INSTAGRAM,
            "variables": {"caption": "Use code RADISH20 today"}})
        self.assertEqual(ok["variables"]["caption"], "Use code RADISH20 today")

    def test_a_damaged_file_reads_as_empty(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{not json")
        self.assertEqual(self.store.all(), [])
        self.store.add({"handle": "getmobster", "app": INSTAGRAM})
        self.assertEqual(len(json.loads(self.store.path.read_text())["accounts"]), 1)
        self.assertEqual(oct(self.store.path.stat().st_mode & 0o777), "0o600")

    def test_a_damaged_links_field_does_not_hide_the_account_list(self):
        account = self.store.add({"handle": "getmobster", "app": INSTAGRAM})
        for links in (42, True, {"label": "Shop"}, "not a list"):
            with self.subTest(links=links):
                self.store.path.write_text(json.dumps({"accounts": [{**account, "links": links}]}))
                self.assertEqual(self.store.all(), [{**account, "links": []}])

    def test_how_a_request_names_an_account(self):
        insta = self.store.add({"handle": "getmobster", "app": INSTAGRAM})
        tiktok = self.store.add({"handle": "getmobster", "app": TIKTOK})
        sam = self.store.add({"handle": "sam.personal", "app": INSTAGRAM})
        accounts = self.store.all()
        self.assertEqual(resolve(accounts, insta["id"])["id"], insta["id"])
        self.assertEqual(resolve(accounts, "@sam.personal")["id"], sam["id"])
        self.assertEqual(resolve(accounts, "@getmobster on TikTok")["id"], tiktok["id"])
        self.assertEqual(resolve(accounts, "getmobster", bundle=INSTAGRAM["bundleId"])["id"], insta["id"])
        with self.assertRaisesRegex(AmbiguousAccount, "Instagram and TikTok"):
            resolve(accounts, "@getmobster")
        with self.assertRaises(AccountNotFound):
            resolve(accounts, "@nobody")

    def test_a_message_names_an_account_with_its_handle(self):
        self.store.add({"handle": "getmobster", "app": INSTAGRAM})
        self.store.add({"handle": "sam.personal", "app": INSTAGRAM})
        accounts = self.store.all()
        self.assertEqual(mentioned(accounts, "Reply to my comments as @getmobster.")["handle"], "getmobster")
        self.assertIsNone(mentioned(accounts, "Reply as @getmobster and @sam.personal"))
        self.assertIsNone(mentioned(accounts, "Write to team@getmobster"))
        self.assertIsNone(mentioned(accounts, "Reply to @someone"))


class ScreenTests(unittest.TestCase):
    GETMOBSTER = {"id": "a" * 12, "handle": "getmobster", "app": MESSAGES, "deviceName": "Studio iPhone"}

    def check(self, known=()):
        return account_screen.AccountCheck(self.GETMOBSTER, known, "Studio iPhone")

    def test_a_sign_in_page_or_a_security_check_in_the_profiles_app_is_kept_for_the_phones_page(self):
        events = []
        check = account_screen.AccountCheck(self.GETMOBSTER, (), "Studio iPhone", emit=events.append)
        login = screen(Element("1", "Phone number, username or email address", "TextField", (.1, .3, .8, .05)),
                       Element("2", "Log in", "Button", (.1, .4, .8, .05)),
                       Element("3", "Forgot password?", "Button", (.1, .5, .8, .05)))
        self.assertEqual(check.look(login), ("unknown", None))
        notice = screen(Element("1", "We suspect automated behavior on your account", "StaticText", (.1, .3, .8, .05)),
                        Element("2", "Continue", "Button", (.1, .4, .8, .05)))
        check.look(notice)
        check.look(profile("getmobster"))
        signals = [(e["signal"], e["account"]) for e in events if e["event"] == "account_signal"]
        self.assertEqual(signals, [("signed_out", "a" * 12), ("challenge", "a" * 12)])
        events.clear()
        check.look(screen(Element("1", "Log in", "Button", (0, 0, 1, 1)), bundle="com.example.other"))
        self.assertEqual(events, [])

    def test_a_sighting_is_handed_over_at_once_and_never_breaks_the_look(self):
        seen = []
        check = account_screen.AccountCheck(self.GETMOBSTER, (), "Studio iPhone", emit=lambda event: None)
        check.on_signal = seen.append
        notice = screen(Element("1", "Confirm it\u2019s you", "StaticText", (.1, .3, .8, .05)),
                        Element("2", "Next", "Button", (.1, .4, .8, .05)))
        check.look(notice)
        self.assertEqual(seen, ["challenge"])
        check.on_signal = lambda signal: 1 / 0
        self.assertEqual(check.look(notice), ("unknown", None))

    def test_a_screen_that_shows_the_profile_is_never_a_sighting(self):
        events, seen = [], []
        check = account_screen.AccountCheck(self.GETMOBSTER, (), "Studio iPhone", emit=events.append)
        check.on_signal = seen.append
        page = screen(Element("10", "getmobster", "NavigationBar", (0, .05, 1, .06)),
                      Element("11", "Edit profile", "Button", (.1, .4, .4, .05)),
                      Element("13", "Confirm it\u2019s you", "StaticText", (.1, .6, .8, .05)),
                      bundle="com.apple.MobileSMS")
        self.assertEqual(check.look(page), ("confirmed", "@getmobster"))
        self.assertEqual(([e for e in events if e["event"] == "account_signal"], seen), ([], []))

    def test_text_inside_a_cell_or_a_field_is_not_a_sighting(self):
        events = []
        check = account_screen.AccountCheck(self.GETMOBSTER, (), "Studio iPhone", emit=events.append)
        thread = screen(Element("1", "We suspect automated behavior on your account", "Cell", (.1, .3, .8, .05)),
                        Element("2", "Send", "Button", (.1, .4, .8, .05)),
                        Element("3", "", "TextField", (.1, .5, .8, .05), value="Confirm it\u2019s you"))
        check.look(thread)
        self.assertEqual([e for e in events if e["event"] == "account_signal"], [])

    def test_an_own_profile_confirms_or_shows_another_account(self):
        self.assertEqual(account_screen.read(profile("getmobster"), "getmobster"), ("confirmed", "@getmobster"))
        self.assertEqual(account_screen.read(profile("getmobster", at=True), "getmobster"), ("confirmed",
            "@getmobster"))
        self.assertEqual(account_screen.read(profile("sam.personal"), "getmobster"), ("mismatch", "@sam.personal"))
        self.assertEqual(account_screen.read(profile("sam.personal", at=True), "getmobster"), ("mismatch",
            "@sam.personal"))

    def test_someone_elses_profile_and_other_screens_say_nothing(self):
        self.assertEqual(account_screen.read(profile("getmobster", own=False), "getmobster"), ("unknown", None))
        self.assertEqual(account_screen.read(COMPOSE, "getmobster"), ("unknown", None))

    def test_a_handle_is_a_whole_token(self):
        self.assertTrue(account_screen.shows("getmobster ✓", "getmobster"))
        self.assertTrue(account_screen.shows("@getmobster.", "getmobster"))
        self.assertFalse(account_screen.shows("mobster.dev", "getmobster"))
        self.assertFalse(account_screen.shows("notacme", "getmobster"))
        self.assertFalse(account_screen.shows("radish-other", "getmobster"))
        self.assertFalse(account_screen.shows("other-getmobster", "getmobster"))
        self.assertTrue(account_screen.shows("@radish-other.", "radish-other"))

    def test_a_hyphenated_handle_does_not_confirm_its_prefix(self):
        self.assertEqual(account_screen.read(profile("radish-other"), "getmobster"), ("mismatch", "@radish-other"))
        self.assertEqual(account_screen.read(profile("radish-other", at=True), "getmobster"),
                         ("mismatch", "@radish-other"))
        self.assertEqual(account_screen.read(profile("radish-other", at=True), "radish-other"),
                         ("confirmed", "@radish-other"))

    def test_a_commit_in_another_app_is_not_as_the_account(self):
        self.assertIsNone(self.check().commit("post", screen(bundle="com.apple.mobileslideshow")))

    def test_unconfirmed_commits_are_refused_then_the_run_stops(self):
        check = self.check()
        for _ in range(account_screen.MAX_REFUSALS - 1):
            self.assertIn("confirm the profile first", check.commit("post", COMPOSE)["refuse"])
        stopped = check.commit("post", COMPOSE)
        self.assertEqual(stopped["code"], "account_unconfirmed")
        self.assertEqual(stopped["stop"],
            "Mobster couldn't confirm on screen that @getmobster is signed in to Messages, so "
                                          "it stopped before posting. Open Messages on Studio iPhone, "
                                          "make sure @getmobster "
                                          "is the profile signed in, and run the task again.")

    def test_a_confirmed_account_commits_as_itself(self):
        check = self.check()
        check.look(profile("getmobster"))
        self.assertEqual(check.commit("send_message", COMPOSE), {"acting_as": "@getmobster on Messages"})

    def test_another_account_stops_before_the_commit(self):
        check = self.check(known=("sam.personal",))
        check.look(profile("sam.personal"))
        self.assertEqual(check.commit("post", COMPOSE),
                         {"stop": "This would post as @sam.personal, not @getmobster. Mobster stopped before posting.",
                          "code": "account_mismatch"})

    def test_a_later_check_that_shows_another_account_wins(self):
        check = self.check()
        check.look(profile("getmobster"))
        check.look(profile("sam.personal"))
        self.assertEqual(check.commit("reply", COMPOSE)["code"], "account_mismatch")

    def test_a_commit_screen_that_shows_another_account_overrides_a_previous_confirmation(self):
        check = self.check()
        check.look(profile("getmobster"))
        self.assertEqual(check.commit("post", profile("sam.personal"))["code"], "account_mismatch")
        self.assertEqual(check.commit("post", COMPOSE)["code"], "account_mismatch")


class FrontierTests(unittest.TestCase):

    REQ = "Text Sam hi"
    ITEMS = [item(quote="Text Sam")]

    def run_agent(self, screens, steps, check, approve=None):
        driver, script, events = Driver(screens), Script(steps, self.ITEMS), []
        skills = (account_screen.CheckAccount(check),)
        result = FrontierAgent(driver, script, apps={"com.apple.MobileSMS": "Messages"}, emit=events.append,
                               screenshots=False, settle_seconds=0, commit_check=check.commit, skills=skills,
                               approve=approve).run(self.REQ)
        return driver, script, result

    def test_an_unconfirmed_send_is_refused_and_never_sent(self):
        check = account_screen.AccountCheck(ScreenTests.GETMOBSTER)
        driver, script, result = self.run_agent(
            [COMPOSE, draft("hi")], [[("TYPE", "Message", "hi"), ("TAP", "Send", None)]] + [[("DONE", None, None)]] * 8,
            check)
        self.assertEqual(driver.actions, [("TYPE", "Message", "hi")])
        self.assertIn("confirm the profile first", script.prompts[1])
        self.assertIn("Not confirmed on screen: message to Sam", result["answer"])

    def test_confirmed_on_the_profile_the_send_goes_out_and_asks_as_the_account(self):
        check, asked = account_screen.AccountCheck(ScreenTests.GETMOBSTER), []

        def approve(request):
            asked.append(request)
            return "approved"

        driver, _, result = self.run_agent(
            [profile("getmobster"), COMPOSE, draft("hi"), sent("hi"), sent("hi")],
            [[("CHECK_PROFILE", None, None)], [("TAP", "Chats", None)],
             [("TYPE", "Message", "hi"), ("TAP", "Send", None)], [("DONE", None, None)], [("DONE", None, None)]],
            check, approve=approve)
        self.assertIn(("TAP", "Send", None), driver.actions)
        self.assertEqual(asked[-1]["acting_as"], "@getmobster on Messages")
        self.assertEqual(asked[-1]["recipient"], "Sam")
        self.assertEqual(result["status"], "completed")

    def test_another_account_signed_in_stops_the_run_at_the_check(self):
        check = account_screen.AccountCheck(ScreenTests.GETMOBSTER)
        driver, _, result = self.run_agent([profile("sam.personal")], [[("CHECK_PROFILE", None, None)]], check)
        self.assertEqual(driver.actions, [])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["code"], "account_mismatch")
        self.assertIn("@sam.personal is signed in to Messages, not @getmobster", result["reason"])

    def test_a_commit_on_another_accounts_screen_stops_before_sending(self):
        check = account_screen.AccountCheck(ScreenTests.GETMOBSTER)
        check.look(profile("sam.personal"))
        driver, _, result = self.run_agent(
            [COMPOSE, draft("hi")], [[("TYPE", "Message", "hi"), ("TAP", "Send", None)]], check)
        self.assertEqual(driver.actions, [("TYPE", "Message", "hi")])
        self.assertEqual((result["status"], result["code"]), ("blocked", "account_mismatch"))
        self.assertEqual(result["reason"],
                         "This would send as @sam.personal, not @getmobster. Mobster stopped before sending.")


class ContextTests(unittest.TestCase):
    ACCOUNT = {"id": "b" * 12, "handle": "getmobster", "app": INSTAGRAM, "notes": "Warm and short.",
        "signature": "— Radish Software",
               "links": [{"label": "Shop", "url": "https://mobster.dev/download"},
                         {"label": "Sale page", "url": "https://getmobster.example/sale"}],
               "variables": {"caption": "New drop"}}

    def test_placeholders_take_the_accounts_values(self):
        goal = ("Post {caption} with {link}, {Sale page} and {SHOP}, signed {signature} as {handle}; "
                "keep {other} and {{x}}")
        self.assertEqual(account_context.fill(goal, self.ACCOUNT),
                         "Post New drop with https://mobster.dev/download, https://getmobster.example/sale and "
                         "https://mobster.dev/download, signed — Radish Software as @getmobster; "
                         "keep {other} and {{x}}")

    def test_the_agent_reads_the_account_and_its_notes(self):
        text = account_context.text(self.ACCOUNT)
        self.assertIn("You act as @getmobster on Instagram", text)
        self.assertIn("CHECK_PROFILE", text)
        self.assertIn("How this profile sounds (the person's notes): Warm and short.", text)
        self.assertIn("Link, Shop: https://mobster.dev/download", text)


class RuntimeTests(unittest.TestCase):

    def setUp(self):
        from mobile_agent.tests.test_conversations_support import ThreadsBase

        class Case(ThreadsBase):
            def runTest(self):
                pass

        self.base = Case()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.runtime = self.base.runtime()
        self.store = store_for(self.runtime)
        self.getmobster = self.store.add({"handle": "getmobster", "app": MESSAGES, "signature": "— Radish Software",
                                    "variables": {"caption": "New drop"}})

    def call(self, method, path, body=None, headers=None):
        return self.base.call(self.runtime, method, path, body, headers=headers)

    def test_a_task_as_an_account_starts_in_its_app_with_its_values_on_smart(self):
        run = self.runtime.create(None, "Text Sam {caption} {signature}", "live", engine="smart",
                                  extras={"account": "@getmobster"})
        self.assertEqual(run.app["bundleId"], "com.apple.MobileSMS")
        self.assertEqual(run.goal, "Text Sam New drop — Radish Software")
        self.assertEqual(run.extras["account"]["handle"], "getmobster")
        self.assertNotIn("variables", run.extras["account"])
        self.assertEqual(run.public()["extras"]["account"]["id"], self.getmobster["id"])

    def test_admitted_profile_context_is_immutable_private_and_survives_edits(self):
        sentinel = "Private profile voice sentinel"
        self.store.update(self.getmobster["id"], {"notes": sentinel,
                                             "links": [{"label": "Shop", "url": "https://example.com/shop"}]})
        run = self.runtime.create(None, "Text Sam {caption}", "live", engine="smart",
                                  extras={"account": {"id": self.getmobster["id"], "notes": "Spoofed"}})
        captured = run.account_context
        self.assertEqual((run.goal, captured["notes"]), ("Text Sam New drop", sentinel))
        for public in (run.public(), run.metadata(), self.runtime.journal.load(), repr(run)):
            self.assertNotIn(sentinel, str(public))
        with self.assertRaises(TypeError):
            captured["variables"]["caption"] = "Changed"
        with self.assertRaises(TypeError):
            captured["app"]["name"] = "Changed"
        with self.assertRaises(TypeError):
            captured["links"][0]["url"] = "https://example.com/changed"
        self.store.update(self.getmobster["id"], {"notes": "Edited voice", "variables": {"caption": "Edited"}})
        self.assertIn(sentinel, account_context.provider(self.runtime.run_context(run)).blocks(None)[0].text)
        self.store.remove(self.getmobster["id"])
        self.assertIn(sentinel, account_context.provider(self.runtime.run_context(run)).blocks(None)[0].text)

    def test_same_key_replays_only_the_exact_profile_context(self):
        run, replayed = self.runtime.create(None, "Text Sam hi", "live", "profile-key",
                                            engine="smart", extras={"account": "@getmobster"})
        self.assertFalse(replayed)
        same, replayed = self.runtime.create(None, "Text Sam hi", "live", "profile-key",
                                             engine="smart", extras={"account": self.getmobster["id"]})
        self.assertTrue(replayed)
        self.assertIs(same, run)
        self.store.update(self.getmobster["id"], {"notes": "A different voice"})
        with self.assertRaises(APIError) as caught:
            self.runtime.create(None, "Text Sam hi", "live", "profile-key", engine="smart",
                                extras={"account": self.getmobster["id"]})
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_a_queued_profile_keeps_its_admitted_context_when_it_starts(self):
        with patch.object(self.runtime, "work", return_value=None):
            holder = self.runtime.create("messages", "Text Sam first", "live", engine="smart")
            waiting = self.runtime.create(None, "Text Sam {caption}", "live", engine="smart", queue=True,
                                          extras={"account": "@getmobster"})
            self.assertIsNotNone(waiting.queue_info)
            self.store.update(self.getmobster["id"], {"notes": "Edited later", "variables": {"caption": "Edited"}})
            self.base.finish(self.runtime, holder)
            self.runtime.run_ended(holder)
        self.assertIn(waiting.id, self.runtime.active_runs)
        self.assertEqual((waiting.goal, waiting.account_context["variables"]["caption"]),
                         ("Text Sam New drop", "New drop"))
        self.assertNotIn("Edited later",
                         account_context.provider(self.runtime.run_context(waiting)).blocks(None)[0].text)

    def test_account_bound_graph_inputs_keep_boolean_and_list_values(self):
        from mobile_agent.graph import dispatch as graph_dispatch
        from mobile_agent.tests.test_project_workflows import GRAPH
        graph = json.loads(json.dumps(GRAPH))
        graph["inputs"].extend([{"name": "enabled", "type": "boolean", "required": True},
                                {"name": "items", "type": "list", "required": True}])
        saved = self.runtime.workflows.create({"name": "Read as Radish Software", "graph": graph,
                                               "account": self.getmobster["id"]})
        workflow = self.runtime.workflows._get(saved["id"])
        given = {"enabled": False, "items": ["first", "second"]}
        values = self.runtime.workflows._account_values(workflow, given, typed=True)
        self.assertEqual(graph_dispatch.run_values(workflow["graph"], values)["enabled"], False)
        self.assertEqual(graph_dispatch.run_values(workflow["graph"], values)["items"], given["items"])
        self.assertIsNone(self.runtime.workflows._check_values(workflow, given, None, draft=True))

    def test_historical_profile_context_never_reads_the_edited_store(self):
        ctx = SimpleNamespace(extras={"account": {"id": self.getmobster["id"]}}, account_context=None)
        with patch("mobile_agent.accounts.store.store_for", side_effect=AssertionError("live read")):
            self.assertIsNone(account_context.provider(ctx))

    def test_pickup_keeps_the_source_profile_after_edit_or_removal(self):
        from mobile_agent.harness import checkpoints
        self.store.update(self.getmobster["id"], {"notes": "Original voice"})
        source = self.runtime.create(None, "Text Sam {caption}", "live", engine="smart",
                                     extras={"account": "@getmobster"})
        checkpoints.save(self.runtime.journal, source.id, {"step": 1, "plan": "Text Sam", "history": []})
        self.base.finish(self.runtime, source, "stopped")
        with self.assertRaisesRegex(ValueError, "profile and workflow stay fixed"):
            self.runtime.create("messages", source.goal, "live", engine="smart",
                                extras={"resumeFrom": source.id})
        other = self.store.add({"handle": "another", "app": MESSAGES})
        with self.assertRaisesRegex(ValueError, "profile and workflow stay fixed"):
            self.runtime.create("messages", source.goal, "live", engine="smart",
                                extras={"resumeFrom": source.id, "account": other["id"]})
        self.store.remove(other["id"])
        self.store.update(self.getmobster["id"], {"notes": "Edited voice", "variables": {"caption": "Edited"}})
        self.store.remove(self.getmobster["id"])
        resumed, replayed = checkpoints.pick_up(self.runtime, source.id, idempotency_key="profile-pickup",
                                                 origin="app")
        self.assertFalse(replayed)
        self.assertIs(resumed.account_context, source.account_context)
        self.assertEqual((resumed.goal, resumed.extras["account"]), (source.goal, source.extras["account"]))
        self.assertEqual(resumed.account_context["notes"], "Original voice")

    def test_pickup_refuses_a_historical_profile_without_a_snapshot(self):
        from mobile_agent.harness import checkpoints
        source = self.runtime.create(None, "Text Sam hi", "live", engine="smart",
                                     extras={"account": "@getmobster"})
        checkpoints.save(self.runtime.journal, source.id, {"step": 1, "plan": "Text Sam", "history": []})
        self.base.finish(self.runtime, source, "stopped")
        source.account_context = None
        with self.assertRaises(APIError) as caught:
            checkpoints.pick_up(self.runtime, source.id, idempotency_key="historical-pickup", origin="app")
        self.assertEqual(caught.exception.code, "profile_context_missing")

    def test_pickup_refuses_if_its_source_expires_during_admission(self):
        from mobile_agent.harness import checkpoints
        source = self.runtime.create(None, "Text Sam hi", "live", engine="smart",
                                     extras={"account": "@getmobster"})
        checkpoints.save(self.runtime.journal, source.id, {"step": 1, "plan": "Text Sam", "history": []})
        self.base.finish(self.runtime, source, "stopped")
        from mobile_agent.accounts import routing
        route = routing.route

        def source_expires(*args):
            routed = route(*args)
            self.runtime.runs.pop(source.id)
            return routed

        with patch.object(routing, "route", side_effect=source_expires):
            with self.assertRaises(APIError) as caught:
                checkpoints.pick_up(self.runtime, source.id, idempotency_key="expired-pickup", origin="app")
        self.assertEqual(caught.exception.code, "run_expired")

    def test_quick_modes_name_runs_as_the_account_and_an_unknown_account_is_refused(self):
        run = self.runtime.create("messages", "Text Sam hi", "live", engine="fast", extras={"account": "@getmobster"})
        self.assertEqual((run.engine, run.extras["account"]["handle"]), ("smart", "getmobster"))
        with self.assertRaisesRegex(ValueError, "No profile has that name"):
            self.runtime.create("messages", "Text Sam hi", "live", engine="smart", extras={"account": "@nobody"})

    def test_a_task_naming_another_phone_than_the_accounts_is_refused(self):
        self.store.update(self.getmobster["id"], {"device": "usb-studio"}, device=("usb-studio", "Studio iPhone"))
        with patch.object(self.runtime.fleet, "find", return_value=SimpleNamespace(id="usb-desk")):
            with self.assertRaisesRegex(ValueError, "@getmobster on Messages is on Studio iPhone"):
                self.runtime.create("messages", "Text Sam hi", "live", engine="smart", device="Desk iPhone",
                                    extras={"account": "@getmobster"})

    def fake_phone_locale(self, locale):
        health = self.runtime.fleet.health
        health.read = lambda url: locale
        self.runtime.fleet._lease_free = lambda url, udid=None: True
        return health

    def test_a_task_as_a_profile_on_a_phone_set_to_another_region_is_refused_before_anything_is_claimed(self):
        self.store.update(self.getmobster["id"], {"region": "GB", "timezone": "Europe/London"})
        self.fake_phone_locale({"region": "US", "language": "en", "timezone": "America/Chicago"})
        with self.assertRaises(APIError) as caught:
            self.runtime.create("messages", "Text Sam hi", "live", engine="smart", extras={"account": "@getmobster"})
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "profile_region_mismatch"))
        self.assertIn("No action was taken.", str(caught.exception))
        self.assertEqual(self.runtime.active_runs, {})
        self.fake_phone_locale({"region": "GB", "language": "en", "timezone": "Europe/London"})
        self.runtime.fleet.health.locales.clear()
        run = self.runtime.create("messages", "Text Sam hi", "live", engine="smart", extras={"account": "@getmobster"})
        self.assertEqual(run.extras["account"]["handle"], "getmobster")

    def release(self, run):
        run.finish({"event": "result", "status": "completed"})
        if run.lease:
            run.lease.close()
            run.lease = None
        with self.runtime.lock:
            self.runtime.active_runs.pop(run.id, None)

    def test_a_language_the_runner_cannot_tell_and_a_zone_abbreviation_never_block_a_task(self):
        self.store.update(self.getmobster["id"], {"region": "US", "language": "fr", "timezone": "Europe/London"})
        self.fake_phone_locale(fh.parse_locale({"currentLocale": "en_US", "timeZone": "CST"}))
        run = self.runtime.create("messages", "Text Sam hi", "live", engine="smart", extras={"account": "@getmobster"})
        self.assertEqual(run.extras["account"]["handle"], "getmobster")
        self.release(run)
        self.fake_phone_locale(fh.parse_locale({"currentLocale": "en_GB", "timeZone": "CST"}))
        self.runtime.fleet.health.locales.clear()
        with self.assertRaises(APIError) as caught:
            self.runtime.create("messages", "Text Sam hi", "live", engine="smart", extras={"account": "@getmobster"})
        self.assertEqual(caught.exception.code, "profile_region_mismatch")

    def test_a_retry_of_a_task_that_started_returns_it_but_a_new_request_is_still_refused(self):
        self.store.update(self.getmobster["id"], {"region": "GB"})
        self.fake_phone_locale({"region": "GB"})
        run, replayed = self.runtime.create("messages", "Text Sam hi", "live", engine="smart", idempotency_key="k1",
                                            extras={"account": "@getmobster"})
        self.assertFalse(replayed)
        self.release(run)
        self.fake_phone_locale({"region": "US"})
        self.runtime.fleet.health.locales.clear()
        again, replayed = self.runtime.create("messages", "Text Sam hi", "live", engine="smart", idempotency_key="k1",
                                              extras={"account": "@getmobster"})
        self.assertEqual((again.id, replayed), (run.id, True))
        with self.assertRaises(APIError) as caught:
            self.runtime.create("messages", "Text Sam hi", "live", engine="smart", idempotency_key="k2",
                                extras={"account": "@getmobster"})
        self.assertEqual(caught.exception.code, "profile_region_mismatch")

    def test_a_retry_after_a_security_check_appeared_returns_the_task_and_a_new_one_is_refused(self):
        health = self.fake_phone_locale({"region": "US"})
        run, _ = self.runtime.create("messages", "Text Sam hi", "live", engine="smart", idempotency_key="k1",
                                     extras={"account": "@getmobster"})
        health.signals.mark(self.getmobster["id"], "challenge", None)
        again, replayed = self.runtime.create("messages", "Text Sam hi", "live", engine="smart", idempotency_key="k1",
                                              extras={"account": "@getmobster"})
        self.assertEqual((again.id, replayed), (run.id, True))
        with self.assertRaises(APIError) as caught:
            self.runtime.create("messages", "Text Sam hi", "live", engine="smart", idempotency_key="k2",
                                extras={"account": "@getmobster"})
        self.assertEqual(caught.exception.code, "profile_challenge")
        with self.assertRaises(APIError) as caught:
            self.runtime.create("messages", "Text Sam bye", "live", engine="smart", idempotency_key="k1",
                                extras={"account": "@getmobster"})
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_a_phone_whose_values_cannot_be_read_and_a_task_with_no_profile_still_run(self):
        self.store.update(self.getmobster["id"], {"region": "GB"})
        self.fake_phone_locale(None)
        run = self.runtime.create("messages", "Text Sam hi", "live", engine="smart", extras={"account": "@getmobster"})
        self.assertIsNotNone(run)
        self.release(run)
        self.fake_phone_locale({"region": "US"})
        self.runtime.fleet.health.locales.clear()
        with self.assertRaises(APIError) as caught:
            self.runtime.create("messages", "Text Sam hi", "live", engine="smart", extras={"account": "@getmobster"})
        self.assertEqual(caught.exception.code, "profile_region_mismatch")
        plain = self.runtime.create("messages", "Text Sam hi", "live", engine="smart")
        self.assertIsNotNone(plain)

    def test_a_security_check_waiting_for_the_profile_refuses_until_it_is_marked_handled(self):
        health = self.fake_phone_locale({"region": "US"})
        health.signals.mark(self.getmobster["id"], "challenge", None)
        with self.assertRaises(APIError) as caught:
            self.runtime.create("messages", "Text Sam hi", "live", engine="smart", extras={"account": "@getmobster"})
        self.assertEqual(caught.exception.code, "profile_challenge")
        status, data, _ = self.call("POST", "/api/fleet/health/acknowledge", {"profile": self.getmobster["id"]})
        self.assertEqual((status, data), (200, {"cleared": True}))
        self.assertIsNotNone(self.runtime.create("messages", "Text Sam hi", "live", engine="smart",
                                                 extras={"account": "@getmobster"}))

    def test_marking_as_handled_says_so_when_another_mobster_holds_the_health_file(self):
        health = self.fake_phone_locale({"region": "US"})
        health.signals.mark(self.getmobster["id"], "challenge", None)
        with patch.object(fh.Signals, "clear", side_effect=OSError("Another Mobster process is holding it")):
            status, data, _ = self.call("POST", "/api/fleet/health/acknowledge", {"profile": self.getmobster["id"]})
        self.assertEqual((status, data["code"]), (503, "health_busy"))
        self.assertIn("Choose Mark as handled again", data["error"])
        self.assertEqual(health.signals.get(self.getmobster["id"])["signal"], "challenge")

    def test_a_finished_run_that_stopped_on_the_profile_check_marks_the_profile(self):
        run = self.runtime.create("messages", "Text Sam hi", "live", engine="smart", extras={"account": "@getmobster"})
        run.finish({"event": "result", "status": "blocked", "stop_code": "account_unconfirmed"})
        self.runtime.finished(run)
        self.assertEqual(self.runtime.fleet.health.signals.get(self.getmobster["id"])["signal"], "signed_out")

    def test_the_api_lists_adds_changes_and_removes(self):
        status, data, _ = self.call("POST", "/api/accounts", {"handle": "@sam.personal", "app": MESSAGES})
        self.assertEqual(status, 201, data)
        self.assertEqual(data["account"]["display"], "@sam.personal on Messages")
        added = data["account"]["id"]
        status, data, _ = self.call("POST", f"/api/accounts/{added}", {"notes": "Casual."})
        self.assertEqual((status, data["account"]["notes"]), (200, "Casual."))
        status, data, _ = self.call("GET", "/api/accounts")
        self.assertEqual([a["handle"] for a in data["accounts"]], ["getmobster", "sam.personal"])
        status, data, _ = self.call("POST", "/api/accounts", {"handle": "getmobster", "app": MESSAGES},
                                    headers={"X-Mobster-Origin": "remote"})
        self.assertEqual((status, data["code"]), (403, "not_here"))
        status, _, _ = self.call("DELETE", f"/api/accounts/{added}")
        self.assertEqual(status, 200)
        status, data, _ = self.call("DELETE", f"/api/accounts/{added}")
        self.assertEqual((status, data["code"]), (404, "account_not_found"))

    def test_two_accounts_on_one_phone_are_refused_up_front(self):
        self.store.add({"handle": "mobster.dev", "app": MESSAGES})
        status, data, _ = self.call("POST", "/api/accounts/runs",
                                    {"accounts": ["@getmobster", "@mobster.dev"], "goal": "Text Sam hi",
                                        "engine": "smart"})
        self.assertEqual((status, data["code"]), (409, "same_phone"))
        self.assertEqual(self.runtime.runs, {})

    def test_a_task_for_several_accounts_starts_one_run_each(self):
        status, data, _ = self.call("POST", "/api/accounts/runs",
                                    {"accounts": ["@getmobster"], "goal": "Text Sam {caption}", "engine": "smart"})
        self.assertEqual(status, 201, data)
        self.assertEqual(data["runs"][0]["run"]["goal"], "Text Sam New drop")

    def test_a_conversation_message_names_the_account_and_the_next_one_keeps_it(self):
        status, data, _ = self.call("POST", "/api/threads", {"message": {"text": "Text Sam hi",
            "account": "@getmobster",
                                                                          "engine": "smart"}})
        self.assertEqual(status, 201, data)
        run = self.runtime.runs[data["run"]["id"]]
        self.assertEqual(run.extras["account"]["handle"], "getmobster")
        self.base.finish(self.runtime, run)
        self.base.settle(self.runtime)
        status, data, _ = self.call("POST", f"/api/threads/{data['thread']['id']}/messages",
                                    {"text": "Now text Leo hi", "engine": "smart"})
        self.assertEqual(status, 201, data)
        self.assertEqual(self.runtime.runs[data["run"]["id"]].extras["account"]["handle"], "getmobster")

    def test_a_whole_run_stops_before_sending_as_another_account(self):
        run = self.runtime.create("messages", "Text Sam hi", "live", engine="smart", extras={"account": "@getmobster"})
        script = Script([[("CHECK_PROFILE", None, None)]], [item(quote="Text Sam")])
        phone = self.base.work(self.runtime, run, script, screens=[profile("sam.personal")] * 3)
        self.assertEqual(phone.actions, [])
        self.assertEqual(run.summary["status"], "blocked")
        self.assertIn("not @getmobster", run.summary["reason"])
        self.assertIn("The profile:", script.prompts[0])

    def test_the_approval_says_as_which_account(self):
        run = self.runtime.create("messages", "Text Sam hi", "live", engine="smart", extras={"account": "@getmobster"})
        script = Script([[("CHECK_PROFILE", None, None)], [("TAP", "Chats", None)],
                         [("TYPE", "Message", "hi"), ("TAP", "Send", None)], [("DONE", None, None)],
                         [("DONE", None, None)]], [item(quote="Text Sam")])
        seen = []

        def approve_when_asked():
            for _ in range(1000):
                pending = run.public()["approval"]
                if pending:
                    seen.append(pending.get("actingAs"))
                    run.answer_approval(pending["id"], True)
                    return
                time.sleep(.01)

        helper = threading.Thread(target=approve_when_asked)
        helper.start()
        phone = self.base.work(self.runtime, run, script,
                               screens=[profile("getmobster"), COMPOSE, draft("hi"), sent("hi"), sent("hi")])
        helper.join(10)
        self.assertEqual(seen, ["@getmobster on Messages"])
        self.assertIn(("TAP", "Send", None), phone.actions)

    def test_a_saved_workflow_runs_as_its_account(self):
        workflow = self.runtime.workflows.create({"name": "Drop", "appId": "messages", "goal": "Text Sam {caption}",
                                                  "engine": "smart", "account": "@getmobster"})
        self.assertEqual(workflow["account"], self.getmobster["id"])
        run, _ = self.runtime.workflows.run(workflow["id"], "key-1")
        self.assertEqual((run.extras["account"]["handle"], run.goal), ("getmobster", "Text Sam New drop"))
        quick = self.runtime.workflows.update(workflow["id"], {"revision": workflow["revision"], "engine": "fast"})
        self.assertEqual((quick["engine"], quick["account"]), ("smart", self.getmobster["id"]))
        cleared = self.runtime.workflows.update(workflow["id"], {"revision": quick["revision"], "account": None})
        self.assertNotIn("account", cleared)

    def test_the_account_fills_its_blanks_for_a_schedule_and_a_run_value_wins(self):
        workflow = self.runtime.workflows.create({"name": "Drop", "appId": "messages", "goal": "Text Sam {caption}",
                                                  "engine": "smart", "account": "@getmobster", "cron": "0 9 * * *",
                                                  "enabled": True})
        self.assertTrue(workflow["enabled"])
        with self.assertRaisesRegex(ValueError, "Give caption a value"):
            self.runtime.workflows.create({"name": "Bare", "appId": "messages", "goal": "Text Sam {caption}",
                                           "engine": "smart", "cron": "0 9 * * *", "enabled": True})
        run, _ = self.runtime.workflows.run(workflow["id"], "key-2", values={"caption": "Restock"})
        self.assertEqual(run.goal, "Text Sam Restock")


class SavedBeforeTheRenameTests(unittest.TestCase):

    SAVED = {"version": 1, "accounts": [{
        "id": "0a1b2c3d4e5f", "handle": "getmobster", "app": MESSAGES, "device": None, "deviceName": None,
        "notes": "Warm and short.", "signature": "— Radish Software", "links": [{"label": "Shop", "url": "https://getmobster.example"}],
        "variables": {"caption": "New drop"}, "createdAt": 1760300000000, "updatedAt": 1760300000000}]}

    def setUp(self):
        from mobile_agent.tests.test_conversations_support import ThreadsBase

        class Case(ThreadsBase):
            def runTest(self):
                pass

        self.base = Case()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.runtime = self.base.runtime()
        self.store = store_for(self.runtime)
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text(json.dumps(self.SAVED, indent=1) + "\n")

    def test_a_saved_list_loads_and_is_listed_as_it_was(self):
        self.assertEqual(self.store.all(), self.SAVED["accounts"])
        status, data, _ = self.base.call(self.runtime, "GET", "/api/accounts")
        self.assertEqual(status, 200)
        self.assertEqual([a["display"] for a in data["accounts"]], ["@getmobster on Messages"])

    def test_a_task_names_a_saved_profile_by_its_id_or_handle(self):
        for name in ("0a1b2c3d4e5f", "@getmobster", "@getmobster on Messages"):
            self.assertEqual(resolve(self.store.all(), name)["id"], "0a1b2c3d4e5f")
        run = self.runtime.create(None, "Text Sam {caption} {shop}", "live", engine="smart",
                                  extras={"account": "0a1b2c3d4e5f"})
        self.assertEqual((run.extras["account"]["handle"], run.goal),
                         ("getmobster", "Text Sam New drop https://getmobster.example"))

    def test_a_workflow_keeps_its_profile_under_the_key_it_was_saved_with(self):
        workflow = self.runtime.workflows.create({"name": "Drop", "appId": "messages", "goal": "Text Sam {caption}",
                                                  "engine": "smart", "account": "@getmobster"})
        stored = json.loads(self.runtime.workflows.journal.connection.execute(
            "SELECT data FROM workflows WHERE id=?", (workflow["id"],)).fetchone()[0])
        self.assertEqual(stored["account"], "0a1b2c3d4e5f")
        run, _ = self.runtime.workflows.run(workflow["id"], "key-1")
        self.assertEqual((run.extras["account"]["handle"], run.goal), ("getmobster", "Text Sam New drop"))

    def test_an_edit_writes_the_file_back_in_the_same_shape(self):
        self.store.update("0a1b2c3d4e5f", {"notes": "Casual."})
        saved = json.loads(self.store.path.read_text())
        self.assertEqual(set(saved), {"version", "accounts"})
        self.assertEqual(saved["accounts"][0]["notes"], "Casual.")

    def test_a_journaled_check_account_step_reads_as_checking_the_profile(self):
        from mobile_agent import narrate
        self.assertEqual(narrate.operation_words("CHECK_ACCOUNT"), ("Check the profile", "Checked the profile"))
        self.assertEqual(narrate.operation_words("CHECK_PROFILE"), ("Check the profile", "Checked the profile"))


class McpTests(unittest.TestCase):

    def setUp(self):
        from mobile_agent.tests.test_conversations import Served

        class Case(Served):
            def runTest(self):
                pass

        self.base = Case()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.runtime = self.base.serve()
        store_for(self.runtime).add({"handle": "getmobster", "app": MESSAGES})

    def tools(self, **kwargs):
        from mobile_agent.accounts.mcp import AccountsProvider
        from mobile_agent.mcp_server import registry
        from mobile_agent.mcp_server.tools import ToolSet
        from mobile_agent.tests.test_conversations import TOKEN
        from mobile_agent.threads.client import Client
        from mobile_agent.threads.mcp import ThreadsProvider
        connect = lambda: Client.connect(self.base.url, origin="mcp", tokens=[TOKEN])  # noqa: E731
        registry._providers[:] = [ThreadsProvider(connect=connect), AccountsProvider(connect=connect)]
        tools = ToolSet(runs_dir=tempfile.mkdtemp(prefix="mobster-accounts-mcp-"), keyless=True,
                        devices=lambda: [], **kwargs)
        self.addCleanup(tools.shutdown)
        return tools

    def test_list_profiles_names_each_profile_and_its_phone(self):
        result = self.tools().call("list_profiles", {}, None)
        self.assertFalse(result.is_error, result.text)
        self.assertIn("@getmobster on Messages, on the default phone", result.text)
        self.assertEqual([row["profile"] for row in result.structured["profiles"]], ["@getmobster on Messages"])
        self.assertNotIn("notes", json.dumps(result.structured))

    def test_list_accounts_still_answers_in_its_old_shape(self):
        tools = self.tools()
        listed = {tool["name"]: tool for tool in tools.list_tools()}
        self.assertTrue(listed["list_accounts"]["description"].startswith("Deprecated: use list_profiles"))
        self.assertTrue(listed["list_accounts"]["annotations"]["readOnlyHint"])
        result = tools.call("list_accounts", {}, None)
        self.assertFalse(result.is_error, result.text)
        self.assertEqual([(row["account"], row["handle"], row["phone"]) for row in result.structured["accounts"]],
                         [("@getmobster on Messages", "@getmobster", "the default phone")])

    def run_as(self, **arguments):
        self.base.scripted(Script([[("DONE", None, None)], [("DONE", None, None)]],
                                  [item(kind="READ", act="none", what="Sam", quote="Sam")], answer="Done."))
        result = self.tools().call("phone_task", {"message": "Read my last message from Sam", "wait_seconds": 20,
                                                  **arguments}, None)
        self.assertFalse(result.is_error, result.text)
        return self.runtime.runs[result.structured["run_id"]]

    def test_phone_task_runs_as_the_profile(self):
        run = self.run_as(profile="@getmobster")
        self.assertEqual((run.origin, run.extras["account"]["handle"]), ("mcp", "getmobster"))

    def test_phone_task_still_takes_account_its_old_name(self):
        run = self.run_as(account="@getmobster")
        self.assertEqual((run.origin, run.extras["account"]["handle"]), ("mcp", "getmobster"))

    def test_phone_task_refuses_two_different_profiles(self):
        result = self.tools().call("phone_task", {"message": "Read my last message from Sam", "profile": "@getmobster",
                                                  "account": "@sam.personal"}, None)
        self.assertTrue(result.is_error)
        self.assertIn("Name the profile once", result.text)
        self.assertEqual(self.runtime.runs, {})

    def test_with_allow_device_the_profiles_phone_must_be_named(self):
        result = self.tools(allow_devices=["Studio iPhone"]).call(
            "phone_task", {"message": "Read my last message from Sam", "profile": "@getmobster"}, None)
        self.assertTrue(result.is_error)
        self.assertIn("name the profile's phone with device too", result.text)
        self.assertEqual(self.runtime.runs, {})


if __name__ == "__main__":
    unittest.main()

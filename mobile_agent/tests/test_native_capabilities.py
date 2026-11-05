import base64
import json
import os
import tempfile
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import deeplinks
from mobile_agent.agent_hooks import Prepared
from mobile_agent.phone_io import PhoneIOError, clipboard, controls, photos
from mobile_agent.tests.fakes import Clock
from mobile_agent.wda import system_dialogs as sd

LOCATION = ("Allow “Maps” to use your location?", ["Allow Once", "Allow While Using App", "Don’t Allow"])
NOTIFY = ("“Duolingo” Would Like to Send You Notifications", ["Don’t Allow", "Allow"])
PHOTOS = ("“Instagram” Would Like Full Access to Your Photo Library", ["Limit Access…", "Allow Full Access",
                                                                         "Don’t Allow"])
TRACK = ("Allow “TikTok” to track your activity across other companies’ apps and websites?",
         ["Ask App Not to Track", "Allow"])
PASTE = ("“Notes” would like to paste from “WebDriverAgentRunner-Runner”", ["Don’t Allow Paste", "Allow Paste"])
OPEN_IN = ("Open in “Instagram”?", ["Cancel", "Open"])
PASSWORD = ("Save Password?", ["Save Password", "Not Now"])
APPLE = ("Sign In to Apple Account", ["Cancel", "Sign In"])
APP_OWN = ("Delete this post?", ["Cancel", "Delete"])


def dialog(pair):
    return sd.classify(*pair)


class ClassifyTests(unittest.TestCase):
    def test_ios_prompts_are_named_with_their_app(self):
        cases = [(LOCATION, ("location", "Maps")), (NOTIFY, ("notifications", "Duolingo")),
                 (PHOTOS, ("photos", "Instagram")), (TRACK, ("tracking", "TikTok")), (PASTE, ("paste", "Notes")),
                 (OPEN_IN, ("open_in_app", "Instagram")), (PASSWORD, ("save_password", "")),
                 (APPLE, ("apple_account", "")), (APP_OWN, ("unknown", ""))]
        for pair, (kind, app) in cases:
            with self.subTest(text=pair[0]):
                found = dialog(pair)
                self.assertEqual((found.kind, found.app), (kind, app))

    def test_an_unknown_allow_dont_allow_prompt_is_a_permission(self):
        self.assertEqual(sd.classify("“Bank” wants to do a new thing", ["Don't Allow", "Allow"]).kind, "permission")


class DecideTests(unittest.TestCase):
    def test_a_privacy_grant_asks_in_every_mode_and_is_never_pressed(self):
        for mode in sd.MODES:
            for pair in (LOCATION, PHOTOS):
                with self.subTest(mode=mode, text=pair[0]):
                    decision = sd.decide(dialog(pair), mode, can_ask=True)
                    self.assertEqual(decision.action, "ask")
                    self.assertNotEqual(decision.action, "press")
                    stopped = sd.decide(dialog(pair), mode, can_ask=False)
                    self.assertEqual(stopped.action, "stop")
                    self.assertIn("Answer it on your iPhone", stopped.reason)

    def test_the_approval_offers_ioss_own_buttons_and_suggests_the_narrowest(self):
        decision = sd.decide(dialog(LOCATION), sd.ASK_IMPORTANT)
        self.assertEqual(decision.title, "Let “Maps” use your location?")
        self.assertEqual([c["label"] for c in sd.choices(decision)], LOCATION[1])
        self.assertEqual(decision.button, "Allow Once")
        self.assertEqual(sd.answered(decision, "choice:b1"), "Allow While Using App")
        self.assertEqual(sd.answered(decision, "approved"), "Allow Once")
        self.assertEqual(sd.answered(decision, "denied"), "Don’t Allow")
        self.assertIsNone(sd.answered(decision, "timeout"))

    def test_notifications_ask_unless_dont_ask_or_nobody_can_answer(self):
        self.assertEqual(sd.decide(dialog(NOTIFY), sd.ASK_IMPORTANT).action, "ask")
        for decision in (sd.decide(dialog(NOTIFY), sd.DONT_ASK), sd.decide(dialog(NOTIFY), sd.ASK_ALL, can_ask=False)):
            self.assertEqual((decision.action, decision.button), ("press", "Don’t Allow"))
            self.assertIn("doesn't need notifications", decision.reason)

    def test_tracking_is_refused_unless_the_user_asked_to_be_asked_every_time(self):
        decision = sd.decide(dialog(TRACK), sd.ASK_IMPORTANT)
        self.assertEqual((decision.action, decision.button), ("press", "Ask App Not to Track"))
        self.assertEqual(sd.decide(dialog(TRACK), sd.ASK_ALL).action, "ask")

    def test_allow_paste_only_for_text_mobster_just_put_there(self):
        clock, driver = Clock(), SimpleNamespace()
        recent = sd.Recent()
        not_ours = sd.decide(dialog(PASTE), sd.ASK_IMPORTANT, recent_=recent, now=clock())
        self.assertEqual((not_ours.action, not_ours.button), ("press", "Don’t Allow Paste"))
        sd.note_paste(driver, clock=clock)
        clock.now += 3
        ours = sd.decide(dialog(PASTE), sd.ASK_IMPORTANT, recent_=sd.recent(driver), now=clock())
        self.assertEqual((ours.action, ours.button), ("press", "Allow Paste"))
        clock.now += sd.PASTE_WINDOW_S + 1
        stale = sd.decide(dialog(PASTE), sd.ASK_IMPORTANT, recent_=sd.recent(driver), now=clock())
        self.assertEqual(stale.button, "Don’t Allow Paste")

    def test_open_in_app_is_pressed_only_after_mobster_opened_the_link(self):
        clock, driver = Clock(), SimpleNamespace()
        sd.note_open(driver, "instagram://user?username=nasa", clock=clock)
        mine = sd.decide(dialog(OPEN_IN), sd.ASK_IMPORTANT, recent_=sd.recent(driver), now=clock())
        self.assertEqual((mine.action, mine.button), ("press", "Open"))
        clock.now += 60
        later = sd.decide(dialog(OPEN_IN), sd.ASK_IMPORTANT, recent_=sd.recent(driver), now=clock(), can_ask=False)
        self.assertEqual((later.action, later.button), ("press", "Cancel"))

    def test_clipboard_and_link_ownership_require_an_observed_past_action(self):
        for record, now in ((sd.Recent(), 1), (sd.Recent(pasted_at=10, opened_at=10), 9)):
            with self.subTest(record=record, now=now):
                paste = sd.decide(dialog(PASTE), recent_=record, now=now, can_ask=False)
                link = sd.decide(dialog(OPEN_IN), recent_=record, now=now, can_ask=False)
                self.assertEqual(paste.button, "Don’t Allow Paste")
                self.assertEqual(link.button, "Cancel")
        record = sd.Recent(pasted_at=0, opened_at=0)
        self.assertEqual(sd.decide(dialog(PASTE), recent_=record, now=1).button, "Allow Paste")
        self.assertEqual(sd.decide(dialog(OPEN_IN), recent_=record, now=1).button, "Open")

    def test_recent_actions_belong_to_the_driver_and_expire_with_it(self):
        driver = AlertPhone()
        record = sd.recent(driver)
        self.assertIs(sd.recent(driver), record)
        self.assertIsNot(sd.recent(AlertPhone()), record)
        retained = weakref.ref(record)
        del driver, record
        self.assertIsNone(retained())

    def test_passwords_and_apple_account_are_never_answered_for_the_user(self):
        save = sd.decide(dialog(PASSWORD), sd.DONT_ASK)
        self.assertEqual((save.action, save.button), ("press", "Not Now"))
        for mode in sd.MODES:
            self.assertEqual(sd.decide(dialog(APPLE), mode, can_ask=True).action, "stop")
        self.assertEqual(sd.decide(sd.classify("Trust This Computer?", ["Trust", "Don’t Trust"]), sd.DONT_ASK).action,
                         "stop")

    def test_an_apps_own_alert_is_left_to_the_agent(self):
        self.assertEqual(sd.decide(dialog(APP_OWN), sd.DONT_ASK, in_app=True).action, "leave")
        self.assertEqual(sd.decide(dialog(APP_OWN), sd.DONT_ASK, in_app=False).action, "ask")

    def test_the_mode_comes_from_the_settings_and_outside_agents_never_go_below_ask_important(self):
        self.assertEqual(sd.permission_mode(env={}), sd.ASK_IMPORTANT)
        self.assertEqual(sd.permission_mode(env={"MOBSTER_ASK_BEFORE_ACTING": "0"}), sd.DONT_ASK)
        self.assertEqual(sd.permission_mode(env={"MOBSTER_PERMISSION_MODE": "ask_all"}), sd.ASK_ALL)
        self.assertEqual(sd.permission_mode("mcp", env={"MOBSTER_PERMISSION_MODE": "dont_ask"}), sd.ASK_IMPORTANT)
        self.assertEqual(sd.permission_mode("remote", env={"MOBSTER_ASK_BEFORE_ACTING": "0"}), sd.ASK_IMPORTANT)


class AlertPhone:
    capabilities = frozenset({"call", "active_app"})

    def __init__(self, pair=None, active="com.duolingo"):
        self.dialog, self.calls, self.active, self.after = pair, [], active, None

    def call(self, method, path, body=None, timeout=10):
        self.calls.append((method, path, body))
        if path == "/alert/text":
            return self.dialog[0] if self.dialog else None
        if path == "/wda/alert/buttons":
            return list(self.dialog[1]) if self.dialog else []
        if path == "/alert/accept":
            if self.dialog is None or body["name"] not in self.dialog[1]:
                raise RuntimeError("no such button")
            self.dialog = None
            return None
        return None

    def active_app(self, timeout=5):
        return "com.apple.springboard" if self.dialog else self.active

    def pressed(self):
        return [body["name"] for method, path, body in self.calls if path == "/alert/accept"]


class FrontierDialogTests(unittest.TestCase):
    def agent(self, phone, approve=None, mode=sd.ASK_IMPORTANT):
        from mobile_agent.frontier import FrontierAgent
        from mobile_agent.state import Element, Snapshot
        screen = Snapshot([Element("1", "Lesson 1", "Button", (.1, .4, .8, .06))], "Lesson 1", 400, 800, "wda",
                          bundle_id="com.duolingo")
        phone.observe = lambda timeout=10: screen
        phone.observe_ready = phone.observe
        events = []
        agent = FrontierAgent(phone, SimpleNamespace(usage={}), apps={"com.duolingo": "Duolingo"}, emit=events.append,
                              dialogs=True, permission_mode=mode, dialog_approve=approve)
        agent.run_state.started = 0.0
        return agent, screen, events

    def test_dont_ask_presses_dont_allow_for_notifications_and_tells_the_model(self):
        phone = AlertPhone(NOTIFY)
        agent, screen, events = self.agent(phone, mode=sd.DONT_ASK)
        history = []
        ended, said, after, waited = agent._system_dialog(screen, 3, history)
        self.assertIsNone(ended)
        self.assertEqual(phone.pressed(), ["Don’t Allow"])
        self.assertIn("Mobster chose “Don’t Allow”", said)
        answered = [e for e in events if e["event"] == "system_dialog"][-1]
        self.assertEqual((answered["kind"], answered["button"], answered["by"]), ("notifications", "Don’t Allow",
                                                                                  "mobster"))
        self.assertEqual([e["event"] for e in events][:1], ["system_dialog_shown"])

    def test_a_location_prompt_asks_the_person_with_ioss_buttons(self):
        phone = AlertPhone(LOCATION)
        asked = []
        agent, screen, events = self.agent(phone, approve=lambda request: asked.append(request) or "choice:b1")
        ended, said, _, _ = agent._system_dialog(screen, 1, [])
        self.assertIsNone(ended)
        self.assertEqual(asked[0]["kind"], "question")
        self.assertEqual([c["label"] for c in asked[0]["choices"]], LOCATION[1])
        self.assertEqual(asked[0]["default_choice"], "b0")
        self.assertEqual(phone.pressed(), ["Allow While Using App"])
        self.assertIn("You chose “Allow While Using App”", said)

    def test_with_nobody_to_ask_a_grant_stops_the_run_and_leaves_the_prompt_up(self):
        from mobile_agent.frontier import GuardStop
        phone = AlertPhone(PHOTOS)
        agent, screen, _ = self.agent(phone)
        with self.assertRaises(GuardStop) as stop:
            agent._system_dialog(screen, 1, [])
        self.assertEqual(stop.exception.verdict.code, "system_dialog")
        self.assertEqual(phone.pressed(), [])
        self.assertIsNotNone(phone.dialog)

    def test_a_prompt_answered_on_the_phone_while_the_person_chose_is_reported_not_asked_again(self):
        phone = AlertPhone(NOTIFY)

        def answered_elsewhere(request):
            phone.dialog = None
            return "choice:b0"
        agent, screen, events = self.agent(phone, approve=answered_elsewhere)
        history = []
        ended, said, _, _ = agent._system_dialog(screen, 3, history)
        self.assertIsNone(ended)
        self.assertIn("it was answered on the iPhone before Mobster could choose", said)
        self.assertIn("don't ask about it again", said)
        self.assertNotIn("couldn't answer", said)
        self.assertTrue([e for e in events if e["event"] == "system_dialog"][-1]["gone"])
        self.assertEqual(len(history), 1)

    def test_a_runner_that_stops_answering_is_not_taken_for_an_answered_prompt(self):
        phone = AlertPhone(NOTIFY)
        call = phone.call

        def down(method, path, body=None, timeout=10):
            if path.startswith("/alert") or path.startswith("/wda/alert"):
                raise ConnectionRefusedError("runner down")
            return call(method, path, body, timeout)

        def unreadable(timeout=10):
            raise ConnectionRefusedError("runner down")
        agent, screen, events = self.agent(phone, approve=lambda request: (setattr(phone, "call", down),
                                                                           setattr(phone, "observe_ready", unreadable),
                                                                           "choice:b0")[-1])
        _, said, after, _ = agent._system_dialog(screen, 3, [])
        self.assertIn("couldn't answer it", said)
        self.assertIs(after, screen)
        self.assertNotIn("gone", [e for e in events if e["event"] == "system_dialog"][-1])

    def test_a_press_that_fails_with_the_prompt_still_up_says_so_as_before(self):
        phone = AlertPhone(NOTIFY)
        call = phone.call

        def stuck(method, path, body=None, timeout=10):
            if path == "/alert/accept":
                raise RuntimeError("WebDriverAgent didn't answer")
            return call(method, path, body, timeout)
        phone.call = stuck
        agent, screen, events = self.agent(phone, approve=lambda request: "choice:b0")
        _, said, _, _ = agent._system_dialog(screen, 3, [])
        self.assertIn("couldn't answer it", said)
        self.assertNotIn("gone", [e for e in events if e["event"] == "system_dialog"][-1])

    def test_a_declined_approval_presses_dont_allow_and_a_timeout_presses_nothing(self):
        phone = AlertPhone(LOCATION)
        agent, screen, _ = self.agent(phone, approve=lambda request: "denied")
        agent._system_dialog(screen, 1, [])
        self.assertEqual(phone.pressed(), ["Don’t Allow"])
        phone = AlertPhone(LOCATION)
        agent, screen, _ = self.agent(phone, approve=lambda request: "timeout")
        ended, _, _, _ = agent._system_dialog(screen, 1, [])
        self.assertEqual(ended["status"], "approval_timeout")
        self.assertEqual(phone.pressed(), [])

    def test_no_dialog_costs_one_front_app_read_and_nothing_more(self):
        phone = AlertPhone(None)
        agent, screen, events = self.agent(phone)
        ended, said, after, _ = agent._system_dialog(screen, 1, [])
        self.assertEqual((ended, said, after), (None, "", screen))
        self.assertEqual([c[1] for c in phone.calls], [])
        agent._system_dialog(screen, 2, [])
        self.assertEqual(events, [])

    def test_a_benchmark_leaves_dialogs_to_the_model(self):
        from mobile_agent.frontier import FrontierAgent
        with mock.patch.dict(os.environ, {"MOBSTER_SYSTEM_DIALOGS": ""}):
            agent = FrontierAgent(AlertPhone(), SimpleNamespace(usage={}))
        self.assertFalse(agent.dialogs)


class DeepLinkTests(unittest.TestCase):
    def test_risk(self):
        self.assertEqual(deeplinks.risk_of("maps://?q=coffee"), "opens")
        self.assertEqual(deeplinks.risk_of("mailto:sam@example.com?subject=Hi"), "prefills")
        for url in ("tel:+15550100", "facetime:sam@example.com", "shortcuts://run-shortcut?name=Morning"):
            self.assertEqual(deeplinks.risk_of(url), "acts", url)
        for url in ("javascript:alert(1)", "file:///etc/passwd", "itms-services://?action=download", "no scheme"):
            with self.assertRaises(deeplinks.LinkError):
                deeplinks.risk_of(url)

    def test_the_map_fills_templates_encoded_and_finds_the_app(self):
        url = deeplinks.BY_ID["maps.search"].fill(query="flat white & cake")
        self.assertEqual(url, "maps://?q=flat%20white%20%26%20cake")
        with self.assertRaises(KeyError):
            deeplinks.BY_ID["instagram.profile"].fill()
        self.assertEqual(deeplinks.app_for("instagram://user?username=nasa"), "com.burbn.instagram")
        self.assertEqual(deeplinks.app_for("https://www.tiktok.com/@nasa"), "com.zhiliaoapp.musically")
        self.assertEqual(deeplinks.app_for("https://example.com"), "")
        self.assertEqual([link.id for link in deeplinks.search("maps", "search")], ["maps.search"])
        ids = [link.id for link in deeplinks.LINKS]
        self.assertEqual(len(ids), len(set(ids)))
        for link in deeplinks.LINKS:
            self.assertIn(link.risk, ("opens", "prefills", "acts"))
            self.assertEqual(deeplinks.risk_of(link.template.replace("{", "").replace("}", "")), link.risk, link.id)

    def test_open_link_opens_in_the_app_and_answers_open_in_for_its_own_link(self):
        phone = AlertPhone(None)
        original = phone.call

        def call(method, path, body=None, timeout=10):
            if path == "/url":
                phone.dialog = OPEN_IN
            return original(method, path, body, timeout)
        phone.call = call
        opened = deeplinks.open_link(phone, "instagram://user?username=nasa", bundle="com.burbn.instagram",
                                     sleep=lambda s: None)
        self.assertEqual(opened["prompt"], "Open")
        self.assertIn(("POST", "/url", {"url": "instagram://user?username=nasa", "bundleId": "com.burbn.instagram"}),
                      phone.calls)

    INBOX_WEB = ("https://www.instagram.com/direct/inbox/", "https://www.instagram.com/direct/inbox",
                 "https://instagram.com/direct/inbox/?utm_source=x", "http://www.instagram.com/direct/inbox/")
    REELS_WEB = ("https://www.instagram.com/reels/", "https://www.instagram.com/reels", "https://instagram.com/reels/#top")

    def test_a_web_address_that_opens_the_feed_maps_to_the_app_link_for_the_screen_it_names(self):
        for url in self.INBOX_WEB:
            self.assertEqual(deeplinks.app_form(url), "instagram://direct-inbox", url)
            self.assertEqual(deeplinks.app_for(url), "com.burbn.instagram", url)
            self.assertEqual(deeplinks.decide(url).app, "com.burbn.instagram", url)
        for url in self.REELS_WEB:
            self.assertEqual(deeplinks.app_form(url), "instagram://reels_home", url)
        for url in ("instagram://direct-inbox", "instagram://reels_home", "https://www.instagram.com/p/abc123/",
                    "https://www.instagram.com/reel/abc123/", "https://www.instagram.com/reels/abc123/",
                    "https://www.instagram.com/nasa/", "https://www.instagram.com/", "https://www.instagram.com/direct/t/1",
                    "https://example.com/direct/inbox/", "https://www.instagram.com.evil.test/reels/",
                    "https://evil.test/?u=https://www.instagram.com/reels/", "tel:+15550100", "", None):
            self.assertEqual(deeplinks.app_form(url), url, url)


class WebLinkInTheTasksBrowser(unittest.TestCase):
    def run_link(self, act, bundle="com.apple.mobilesafari", web=True):
        from mobile_agent.skills.open_url import OpenUrl
        skill, phone = OpenUrl(sleep=lambda s: None), AlertPhone(None)
        ctx = SimpleNamespace(driver=phone, emit=lambda e: None)
        prepared = skill.prepare(ctx, {"text": "https://en.m.wikipedia.org/wiki/Cupertino"}, None)
        page = [SimpleNamespace(role="WebView", label="", value="", editable=False)] if web else \
            [SimpleNamespace(role="Cell", label="Address", value="", editable=False)]
        skill.perform(ctx, prepared, act, SimpleNamespace(bundle_id=bundle, elements=page))
        return [body for method, path, body in phone.calls if path == "/url"]

    def test_the_named_browser_opens_it(self):
        self.assertEqual(self.run_link({"app": "com.apple.mobilesafari"}),
                         [{"url": "https://en.m.wikipedia.org/wiki/Cupertino", "bundleId": "com.apple.mobilesafari"}])

    def test_an_app_in_front_showing_no_web_page_leaves_it_to_the_default_browser(self):
        self.assertEqual(self.run_link({}, bundle="com.apple.MobileAddressBook", web=False),
                         [{"url": "https://en.m.wikipedia.org/wiki/Cupertino"}])


class AFailedOpenIsNotTriedElsewhere(unittest.TestCase):
    def test_one_open_then_feedback(self):
        from mobile_agent.skills.open_url import OpenUrl

        class Failing(AlertPhone):
            def call(self, method, path, body=None, timeout=10):
                if path == "/url":
                    self.calls.append((method, path, body))
                    raise TimeoutError("no answer")
                return super().call(method, path, body, timeout)
        skill, phone = OpenUrl(sleep=lambda s: None), Failing(None)
        ctx = SimpleNamespace(driver=phone, emit=lambda e: None)
        prepared = skill.prepare(ctx, {"text": "https://example.org"}, None)
        web = [SimpleNamespace(role="WebView", label="", value="", editable=False)]
        result = skill.perform(ctx, prepared, {}, SimpleNamespace(bundle_id="com.example.reader", elements=web))
        self.assertEqual([b for m, p, b in phone.calls if p == "/url"],
                         [{"url": "https://example.org", "bundleId": "com.example.reader"}])
        self.assertIn("didn't open", result.feedback)


class NamedBrowserInTheCompactReply(unittest.TestCase):
    def test_the_target_names_the_app_and_an_element_id_does_not(self):
        from mobile_agent.frontier import normalize_reply
        out = normalize_reply({"actions": [{"op": "OPEN_URL", "target": "com.apple.mobilesafari",
                                            "text": "https://en.m.wikipedia.org/wiki/Cupertino"},
                                           {"op": "OPEN_URL", "target": "e7", "text": "https://example.org"},
                                           {"op": "OPEN_URL", "target": None, "text": "https://example.org"}]})
        self.assertEqual([a["app"] for a in out["actions"]], ["com.apple.mobilesafari", None, None])

    def test_from_the_home_screen_the_named_listed_browser_opens_it_and_an_unlisted_one_does_not(self):
        from mobile_agent.frontier import FrontierAgent
        from mobile_agent.state import Element, Snapshot
        from mobile_agent.tests.test_frontier import Driver

        class Phone(Driver):
            capabilities = Driver.capabilities | {"call"}

            def __init__(self, screens):
                super().__init__(screens)
                self.urls = []

            def call(self, method, path, body=None, timeout=10):
                if path == "/url":
                    self.urls.append(body)
                return {"value": None}

        class Reply:
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

            def __init__(self, app):
                self.app, self.turns = app, 0

            def complete(self, messages, schema, timeout=60):
                if "items" in schema["properties"]:
                    return {"items": []}, {}
                self.turns += 1
                act = ({"op": "OPEN_URL", "target": self.app, "text": "https://en.m.wikipedia.org/wiki/Cupertino"}
                       if self.turns == 1 else {"op": "DONE", "target": None, "text": None})
                return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [], "actions": [act],
                        "answer": None if self.turns == 1 else "done"}, {}
        home = Snapshot([Element("1", "Safari", "Icon", (.1, .1, .1, .1))], "Safari", 400, 800, "synthetic_fixture",
                        bundle_id="com.apple.springboard")
        for named, apps, expected in (("com.apple.mobilesafari", {"com.apple.mobilesafari": "Safari"},
                                       "com.apple.mobilesafari"),
                                      ("com.example.other", {"com.apple.mobilesafari": "Safari"}, None)):
            phone = Phone([home, home, home, home])
            FrontierAgent(phone, Reply(named), apps=apps, screenshots=False, settle_seconds=0).run(
                "In Safari, open the Wikipedia article on Cupertino.")
            self.assertEqual([u.get("bundleId") for u in phone.urls], [expected], named)


class PhotosTests(unittest.TestCase):
    def test_newest_compares_modification_times_across_every_dcim_folder(self):
        roll = photos.CameraRoll(None)
        listing = {
            "/DCIM": [{"name": "100APPLE", "folder": True}, {"name": "101APPLE", "folder": True}],
            "/DCIM/100APPLE": [{"name": "IMG_0001.JPG", "folder": False, "bytes": 10, "modified": 200}],
            "/DCIM/101APPLE": [{"name": "IMG_0100.JPG", "folder": False, "bytes": 10, "modified": 100}],
        }
        roll._ls = lambda folder: listing[folder]
        self.assertEqual([item["name"] for item in roll.newest(count=1)], ["IMG_0001.JPG"])
        self.assertEqual([item["name"] for item in roll.newest(count=2)], ["IMG_0001.JPG", "IMG_0100.JPG"])

    def test_camera_roll_refuses_paths_and_control_characters_in_device_filenames(self):
        from mobile_agent.phone_io.files import PhoneFiles
        rows = [f"-rw-r--r--  1 mobile mobile 10 09 Oct 2026 10:00:00 {name}"
                for name in ("../../escape.jpg", "/absolute.jpg", "bad\x00.jpg", "IMG_0001.JPG")]
        files = PhoneFiles({"kind": "usb", "udid": "00008020-000A1B2C3D4E5F60"}, afc="afcclient",
                           runner=lambda argv, timeout: (0, "\n".join(rows)))
        entries = photos.CameraRoll(files)._ls("/DCIM/100APPLE")
        self.assertEqual([entry["name"] for entry in entries], ["IMG_0001.JPG"])

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.jpeg = self.folder / "beach.jpg"
        self.jpeg.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)

    def runner(self, status="authorized", saved=True, missing=False):
        calls = []

        def request(method, path, body=None, timeout=15):
            calls.append((method, path, body))
            if missing:
                raise PhoneIOError("WebDriverAgent doesn't know that command.", "unknown_route")
            if path == "/mobster/photos/status":
                return {"status": status, "bundleId": "app.mobster.wda.runner.xctrunner"}
            if path == "/mobster/photos":
                return {"saved": saved, "status": "limited" if saved else status, "localIdentifier": "ID/L0/001"}
            return None
        return request, calls

    def test_send_posts_the_file_and_reports_what_photos_said(self):
        request, calls = self.runner()
        result = photos.send(request, self.jpeg, name="Studio iPhone")
        self.assertEqual((result["kind"], result["localIdentifier"], result["bytes"]), ("photo", "ID/L0/001", 104))
        body = calls[-1][2]
        self.assertEqual((body["kind"], body["extension"], body["filename"]), ("photo", "jpg", "beach.jpg"))
        self.assertEqual(base64.b64decode(body["data"]), self.jpeg.read_bytes())

    def test_send_reservation_refusal_propagates_before_activation_or_media_dispatch(self):
        request, calls = self.runner(status="not_determined")
        moves = []
        refused = RuntimeError("transfer already reserved")

        def reserve():
            self.assertEqual([path for _, path, _ in calls], ["/mobster/photos/status"])
            raise refused

        with self.assertRaises(RuntimeError) as caught:
            photos.send(request, self.jpeg, activate=moves.append, before_send=reserve)
        self.assertIs(caught.exception, refused)
        self.assertEqual(moves, [])
        self.assertEqual([path for _, path, _ in calls], ["/mobster/photos/status"])

    def test_send_preflight_failure_never_reserves_a_transfer(self):
        request, calls = self.runner(missing=True)
        with self.assertRaises(PhoneIOError):
            photos.send(request, self.jpeg, before_send=lambda: self.fail("preflight must finish first"))
        self.assertEqual([path for _, path, _ in calls], ["/mobster/photos/status"])

    def test_the_first_time_the_runner_comes_forward_for_ios_question_and_the_app_goes_back(self):
        request, calls = self.runner(status="not_determined")
        moves = []
        photos.send(request, self.jpeg, activate=moves.append, front=lambda: "com.instagram")
        self.assertEqual(moves, ["app.mobster.wda.runner.xctrunner", "com.instagram"])

    def test_an_old_runner_and_a_refusal_say_what_to_do(self):
        request, _ = self.runner(missing=True)
        with self.assertRaises(PhoneIOError) as old:
            photos.send(request, self.jpeg, name="Studio iPhone")
        self.assertEqual(old.exception.code, "needs_rebuild")
        self.assertIn("Rebuild it once in Setup", str(old.exception))
        request, _ = self.runner(status="denied", saved=False)
        with self.assertRaises(PhoneIOError) as denied:
            photos.send(request, self.jpeg, name="Studio iPhone")
        self.assertIn("Settings › Privacy & Security › Photos", str(denied.exception))

    def test_only_photos_and_videos_by_their_bytes(self):
        fake = self.folder / "photo.jpg"
        fake.write_bytes(b"#!/bin/sh\necho hi\n")
        with self.assertRaises(PhoneIOError):
            photos.sniff(fake)
        movie = self.folder / "clip.bin"
        movie.write_bytes(b"\x00\x00\x00\x18ftypqt  " + b"0" * 20)
        self.assertEqual(photos.sniff(movie), ("video", "mov"))
        heic = self.folder / "IMG.heic"
        heic.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"0" * 20)
        self.assertEqual(photos.sniff(heic), ("photo", "heic"))

    def test_the_camera_roll_gives_the_newest_first_by_count_or_since(self):
        listing = {
            "/DCIM": ["drwxr-xr-x  1 mobile mobile 0 09 Oct 2026 10:00:00 100APPLE",
                      "drwxr-xr-x  1 mobile mobile 0 09 Oct 2026 11:00:00 101APPLE"],
            "/DCIM/100APPLE": ["-rw-r--r--  1 mobile mobile 10 08 Oct 2026 09:00:00 IMG_0001.HEIC",
                               "-rw-r--r--  1 mobile mobile 11 08 Oct 2026 09:00:00 IMG_0001.AAE"],
            "/DCIM/101APPLE": ["-rw-r--r--  1 mobile mobile 12 09 Oct 2026 10:30:00 IMG_0100.PNG",
                               "-rw-r--r--  1 mobile mobile 13 09 Oct 2026 10:45:00 RPReplay_Final1.MP4"]}
        sizes = {"IMG_0100.PNG": 12, "RPReplay_Final1.MP4": 13, "IMG_0001.HEIC": 10}

        def runner(argv, timeout):
            command = argv[argv.index("--") + 1:]
            if command[0] == "ls":
                return 0, "\n".join(listing.get(command[-1], []))
            Path(command[2]).write_bytes(b"x" * sizes[Path(command[1]).name])
            return 0, ""
        from mobile_agent.phone_io.files import PhoneFiles
        files = PhoneFiles({"kind": "usb", "udid": "00008020-000A1B2C3D4E5F60", "name": "Studio iPhone"},
                           runner=runner, afc="/bin/afcclient")
        roll = photos.CameraRoll(files)
        newest = roll.newest(count=2)
        self.assertEqual([item["name"] for item in newest], ["RPReplay_Final1.MP4", "IMG_0100.PNG"])
        self.assertEqual(newest[0]["kind"], "video")
        import time as clock
        since = clock.mktime(clock.strptime("09 Oct 2026 10:40:00", "%d %b %Y %H:%M:%S"))
        self.assertEqual([i["name"] for i in roll.newest(count=5, since=since)], ["RPReplay_Final1.MP4"])
        pulled = roll.pull(self.folder / "out", count=3)
        self.assertEqual(sorted(p.name for p in (self.folder / "out").iterdir()),
                         ["IMG_0001.HEIC", "IMG_0100.PNG", "RPReplay_Final1.MP4"])
        self.assertEqual(len(pulled), 3)
        self.assertNotIn("--documents", " ".join(map(str, runner.__code__.co_varnames)))

    def test_since_takes_ages_and_times(self):
        self.assertEqual(photos.parse_since("10m", now=10_000), 9_400)
        self.assertEqual(photos.parse_since("2h", now=10_000), 10_000 - 7200)
        self.assertIsNone(photos.parse_since(None))
        with self.assertRaises(ValueError):
            photos.parse_since("yesterday-ish")

    def test_a_picture_goes_on_the_clipboard_as_an_image(self):
        calls = []
        result = clipboard.set_image(lambda m, p, b, t: calls.append((p, b)), self.jpeg)
        self.assertEqual(result["name"], "beach.jpg")
        self.assertEqual(calls[0][1]["contentType"], "image")
        with self.assertRaises(PhoneIOError):
            text = self.folder / "notes.txt"
            text.write_text("hello")
            clipboard.set_image(lambda *a: None, text)


class RunnerPatchTests(unittest.TestCase):
    def test_the_photos_and_password_routes_patch_the_pinned_source_once(self):
        from mobile_agent.wda import patches as wda_patches
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "WebDriverAgentLib" / "Commands" / "FBCustomCommands.m"
            source.parent.mkdir(parents=True)
            fixture = json.loads((Path(__file__).parent / "fixtures/wda-private-input.json").read_text())
            source.write_text(fixture["files"]["WebDriverAgentLib/Commands/FBCustomCommands.m"])
            photos_patches = [p for p in wda_patches.WDA_PATCHES if p[0].endswith("FBCustomCommands.m")]
            with mock.patch.object(wda_patches, "WDA_PATCHES", tuple(photos_patches)):
                wda_patches.patch_wda(folder)
                once = source.read_text()
                wda_patches.patch_wda(folder)
            self.assertEqual(source.read_text(), once)
            self.assertIn("#import <Photos/Photos.h>", once)
            self.assertEqual(once.count('@"/mobster/photos"'), 2)
            self.assertIn("PHAccessLevelReadWrite", once)
            self.assertIn("handleMobsterPhotosAdd", once)
            self.assertEqual(once.count("+ (id<FBResponsePayload>)handleMobsterSecret:"), 1)


class ControlTests(unittest.TestCase):
    def phone(self, style="light", orientation="PORTRAIT"):
        calls = []

        def call(method, path, body=None, timeout=10):
            calls.append((method, path, body))
            if path == "/wda/device/info":
                return {"userInterfaceStyle": style}
            if path == "/orientation" and method == "GET":
                return orientation
            return None
        return call, calls

    def test_buttons(self):
        call, calls = self.phone()
        controls.press(call, "volume_up")
        self.assertEqual(calls[-1], ("POST", "/wda/pressButton", {"name": "volumeup"}))
        with self.assertRaises(PhoneIOError):
            controls.press(call, "volume_up", simulator=True)
        with self.assertRaises(PhoneIOError):
            controls.press(call, "action")

    def test_display_settings_return_the_earlier_values(self):
        call, calls = self.phone()
        self.assertEqual(controls.set_appearance(call, "dark"), {"appearance": "dark", "before": "light"})
        self.assertEqual(calls[-1], ("POST", "/wda/device/appearance", {"name": "dark"}))
        self.assertEqual(controls.set_orientation(call, "landscape_right"),
                         {"orientation": "landscape_right", "before": "portrait"})
        self.assertEqual(calls[-1][2], {"orientation": "UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT"})


class NotificationSinceTests(unittest.TestCase):
    def test_rows_have_title_body_and_time_and_since_keeps_the_newer(self):
        from mobile_agent.skills.notifications import parse_row, since_filter
        now = 100_000.0
        new = parse_row("Messages, Sam, Are you free at 6?, 2m ago", now)
        old = parse_row("Mail, Bank, Your statement is ready, 3h ago", now)
        public = new.public(now=now)
        self.assertEqual((public["app"], public["title"], public["body"]), ("Messages", "Sam", "Are you free at 6?"))
        self.assertTrue(public["time"])
        self.assertEqual(since_filter([new, old], now - 600, now), [new])
        self.assertEqual(since_filter([new, old], None, now), [new, old])


class MCPNativeToolTests(unittest.TestCase):
    def harness(self, **options):
        from mobile_agent.tests.test_mcp_new_tools import NewToolsHarness
        test = NewToolsHarness()
        test.setUp()
        self.addCleanup(test.doCleanups)
        test.make(**options)
        return test

    def test_listing_media_tools_needs_their_flags(self):
        names = {t["name"] for t in self.harness().tools.list_tools()}
        self.assertTrue({"press_button", "set_display"} <= names)
        self.assertFalse({"send_media", "pull_media"} & names)
        tools = self.harness(allow_files=True, allow_photos=True).tools.list_tools()
        named = {t["name"]: t for t in tools}
        self.assertTrue({"send_media", "pull_media"} <= set(named))
        for name in ("press_button", "set_display", "send_media", "pull_media", "open_url",
                     "set_clipboard", "read_notifications"):
            self.assertLessEqual(len(named[name]["description"]), 220, name)

    def test_listing_deep_links_for_instagram_offers_the_app_links_and_no_web_address_that_opens_the_feed(self):
        text = self.harness().call("open_url", app="Instagram").text
        self.assertIn("instagram://direct-inbox", text)
        self.assertIn("instagram://reels_home", text)
        self.assertNotIn("instagram.com/direct/inbox", text)
        self.assertNotIn("instagram.com/reels", text)
        self.assertNotRegex(text, r"https?://")

    def test_open_url_with_a_web_address_that_opens_the_feed_opens_the_app_link_and_says_so(self):
        test = self.harness()
        test.call("screen", device="Work iPhone", image=False)
        from mobile_agent.tests.test_mcp_devices import FakeDeviceRun
        run = FakeDeviceRun.opened[-1]
        run.open_url = lambda url: deeplinks.open_link(run.driver, url, bundle=deeplinks.app_for(url) or None,
                                                       sleep=lambda s: None)
        rewritten = test.call("open_url", device="Work iPhone", url="https://www.instagram.com/direct/inbox/")
        self.assertFalse(rewritten.is_error, rewritten.text)
        self.assertIn("as “instagram://direct-inbox”", rewritten.text)
        self.assertIn("opens the home feed", rewritten.text)
        sent = [c for c in run.driver.calls if isinstance(c, tuple) and len(c) >= 3 and c[1] == "/url"]
        self.assertEqual([c[2]["url"] for c in sent], ["instagram://direct-inbox"])
        plain = test.call("open_url", device="Work iPhone", url="instagram://reels_home")
        self.assertFalse(plain.is_error, plain.text)
        self.assertNotIn("home feed", plain.text)

    def test_open_url_refuses_acting_links_on_a_real_iphone(self):
        test = self.harness()
        result = test.call("open_url", device="Work iPhone", url="tel:+15550100")
        self.assertTrue(result.is_error)
        self.assertIn("Mobster app isn't open", result.text)
        self.assertTrue(test.call("open_url", device="Work iPhone", url="javascript:alert(1)").is_error)

    def test_alert_wont_grant_a_privacy_permission_on_a_real_iphone_but_denies(self):
        from mobile_agent.tests.test_mcp_devices import FakeDeviceRun
        test = self.harness()
        test.call("screen", device="Work iPhone", image=False)
        phone = FakeDeviceRun.opened[-1].driver
        prompt = {"dialog": LOCATION}
        original = phone.call

        def call(method, path, body=None, timeout=10):
            if path == "/alert/text":
                return prompt["dialog"][0] if prompt["dialog"] else None
            if path == "/wda/alert/buttons":
                return list(prompt["dialog"][1]) if prompt["dialog"] else []
            if path.startswith("/alert/"):
                prompt["pressed"] = body
                prompt["dialog"] = None
                return None
            return original(method, path, body, timeout)
        phone.call = call
        refused = test.call("alert", device="Work iPhone", action="accept", button="Allow While Using App")
        self.assertTrue(refused.is_error)
        self.assertIn("the owner's", refused.text)
        self.assertNotIn("pressed", prompt)
        screen = test.call("screen", device="Work iPhone", image=False)
        self.assertEqual(screen.structured["screen"]["alert"]["kind"], "location")
        done = test.call("alert", device="Work iPhone", action="accept", button="Don't Allow")
        self.assertFalse(done.is_error, done.text)
        self.assertEqual(prompt["pressed"], {"name": "Don’t Allow"})

    def test_press_button_and_set_display(self):
        from mobile_agent.tests.test_mcp_devices import FakeDeviceRun
        test = self.harness()
        result = test.call("press_button", device="Work iPhone", button="volume_down")
        self.assertFalse(result.is_error, result.text)
        phone = FakeDeviceRun.opened[-1].driver
        self.assertIn(("POST", "/wda/pressButton", {"name": "volumedown"}),
                      [c[:3] for c in phone.calls if isinstance(c, tuple) and len(c) >= 3])
        locked = test.call("press_button", device="Work iPhone", button="lock")
        self.assertIn("Mobster can't unlock it again", locked.text)
        shown = test.call("set_display", device="Work iPhone", appearance="dark")
        self.assertFalse(shown.is_error, shown.text)
        self.assertIn("Set appearance dark", shown.text)
        self.assertTrue(test.call("set_display", device="Work iPhone").is_error)


class PutFilePhotosTests(unittest.TestCase):
    def test_a_picture_can_go_to_photos_after_the_users_ok_and_a_pdf_cant(self):
        from mobile_agent.attachments.agent import PutFile
        image = SimpleNamespace(name="beach.jpg", kind="image", data={"bytes": 104})
        pdf = SimpleNamespace(name="menu.pdf", kind="pdf", data={"bytes": 900, "label": "a PDF"})
        ctx = SimpleNamespace(interactive=True, runtime=None, device_id=None, emit=lambda e: None)
        skill = PutFile(store=None, ctx=ctx)
        with mock.patch("mobile_agent.attachments.agent.run_files", return_value=[image, pdf]):
            prepared = skill.prepare(ctx, {"text": "beach.jpg > photos"}, None)
            self.assertEqual(prepared.title, "Add “beach.jpg” to Photos on your iPhone?")
            refused = skill.prepare(ctx, {"text": "menu.pdf > photos"}, None)
            self.assertIsNone(refused.title)
        self.assertIsInstance(prepared, Prepared)


class AddressFieldIsReplaced(unittest.TestCase):
    def written(self, label, op="TYPE", placeholder=""):
        from mobile_agent.frontier import FrontierAgent
        from mobile_agent.frontier.turn import RunState
        from mobile_agent.state import Element
        calls = []

        class Driver:
            def write_text(self, element, snapshot, text, append=False, submit=False, timeout=30):
                calls.append(append)
                return {"value": text, "matches": True, "expected": text}
        agent = FrontierAgent.__new__(FrontierAgent)
        agent.driver, agent.run_state = Driver(), RunState(started=0.0, macro_feedback="")
        agent._foreign_text = lambda element: False
        field = Element("a", label, "TextField", (.3, .9, .4, .04), True, placeholder=placeholder,
                        actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        agent._write(op, field, "wikipedia cupertino", None)
        return calls[0]

    def test_an_address_field_is_replaced(self):
        self.assertFalse(self.written("Address"))
        self.assertFalse(self.written("", placeholder="Search or enter website name"))

    def test_an_email_or_delivery_address_still_appends(self):
        self.assertTrue(self.written("Email address"))
        self.assertTrue(self.written("Delivery address"))


if __name__ == "__main__":
    unittest.main()

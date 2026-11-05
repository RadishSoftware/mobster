import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import deeplinks, permissions
from mobile_agent.deeplinks import LINK_LABEL, LINK_OP, decide
from mobile_agent.permissions import exceptions

THINGS = "things:///add?title=Milk"
PHONE = "com.apple.mobilephone"


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store = exceptions.Exceptions(self.dir / "permission-exceptions.json")


class DecideTests(Store):
    def test_a_web_page_maps_the_app_store_and_the_maps_apps_never_ask(self):
        for url in ("https://example.com/hours", "http://example.com", "maps://?q=coffee",
                    "itms-apps://apps.apple.com/app/id284882215", "App-prefs:WIFI", "instagram://user?username=nasa",
                    "shortcuts://open-shortcut?name=Morning"):
            self.assertEqual(decide(url).action, "open", url)

    def test_links_that_act_ask_every_time_even_after_dont_ask_again(self):
        for url in ("tel:+15555550123", "facetime:sam@example.com", "sms:+15555550123&body=hi",
                    "mailto:sam@example.com", "shortcuts://run-shortcut?name=Morning"):
            decision = decide(url, remembered=lambda app, label, op: True)
            self.assertEqual((decision.action, decision.always, decision.remember()), ("ask", True, None), url)
        self.assertEqual(decide("tel:+15555550123").title, "Call +15555550123?")
        self.assertEqual(decide("https://user:secret@example.com/").action, "ask")

    def test_another_apps_link_asks_once_then_dont_ask_again_opens_it(self):
        first = decide(THINGS, self.store.contains)
        self.assertEqual((first.action, first.always, first.title), ("ask", False, "Open this link in Things?"))
        spec = first.remember()
        self.assertEqual(spec, {"app": "url:things", "label": LINK_LABEL, "op": LINK_OP, "app_name": "Things",
                                "words": "Don’t ask again for Things"})
        self.store.add(spec["app"], spec["label"], spec["op"], spec["app_name"])
        self.assertEqual(decide("things:///show?id=today", self.store.contains).action, "open")
        self.assertEqual(decide("venmo://paycharge", self.store.contains).action, "ask")


class StoreTests(Store):
    def test_the_list_is_the_permission_exceptions_file_one_entry_per_app_and_another_process_sees_it(self):
        entry = self.store.add("url:things", LINK_LABEL, LINK_OP, "Things")
        self.assertEqual(set(entry), {"key", "app", "appName", "label", "op", "addedAt"})
        self.assertEqual(oct(os.stat(self.store.path).st_mode & 0o777), "0o600")
        other = exceptions.Exceptions(self.store.path)
        self.assertTrue(other.contains("url:things", "links", "open_url"))
        self.store.add("url:venmo", LINK_LABEL, LINK_OP, "Venmo")
        time.sleep(0.01)
        self.assertTrue(other.contains("url:venmo", LINK_LABEL, LINK_OP))
        self.assertTrue(self.store.remove(entry["key"]))
        self.assertFalse(other.contains("url:things", LINK_LABEL, LINK_OP))


class ApprovalRememberTests(Store):
    def run_(self):
        from mobile_agent.run import Run
        run = Run({"id": "messages", "name": "Messages", "bundleId": "com.apple.MobileSMS"}, "Add milk", "live")
        added = []
        run.on_remember = added.append
        return run, added

    def ask(self, run, request):
        answers = []
        thread = threading.Thread(target=lambda: answers.append(run.request_approval(request, timeout=10)))
        thread.start()
        for _ in range(200):
            if run.approval:
                break
            time.sleep(0.01)
        return thread, answers

    def test_approved_with_dont_ask_again_is_remembered_and_a_plain_approval_is_not(self):
        spec = decide(THINGS).remember()
        request = {"kind": "commit", "operation": "OPEN_URL", "label": "Open things://", "title": "Open this link in "
                   "Things?", "app_name": "Things", "remember": spec}
        run, added = self.run_()
        thread, answers = self.ask(run, request)
        self.assertEqual((run.approval["rememberable"], run.approval["rememberLabel"]),
                         (True, "Don’t ask again for Things"))
        self.assertTrue(run.answer_approval(run.approval["id"], True, remember=True))
        thread.join(5)
        self.assertEqual((answers, added), (["approved"], [spec]))
        run, added = self.run_()
        thread, answers = self.ask(run, request)
        run.answer_approval(run.approval["id"], True)
        thread.join(5)
        self.assertEqual(added, [])

    def test_a_decline_or_a_card_without_the_offer_remembers_nothing(self):
        run, added = self.run_()
        thread, _ = self.ask(run, {"kind": "commit", "operation": "OPEN_URL", "label": "Call", "title": "Call?",
                                   "remember": decide(THINGS).remember()})
        run.answer_approval(run.approval["id"], False, remember=True)
        thread.join(5)
        run2, added2 = self.run_()
        thread, _ = self.ask(run2, {"kind": "commit", "operation": "OPEN_URL", "label": "Call", "title": "Call?"})
        self.assertFalse(run2.approval["rememberable"])
        run2.answer_approval(run2.approval["id"], True, remember=True)
        thread.join(5)
        self.assertEqual((added, added2), ([], []))


class EngineSkillTests(Store):
    def ctx(self):
        runtime = SimpleNamespace(permission_exceptions=lambda: self.store)
        return SimpleNamespace(driver=None, emit=lambda e: None, run=SimpleNamespace(runtime=runtime))

    def test_the_first_link_into_an_app_asks_with_dont_ask_again_then_opens(self):
        from mobile_agent.skills.open_url import OpenUrl
        skill = OpenUrl(sleep=lambda s: None)
        prepared = skill.prepare(self.ctx(), {"text": THINGS}, None)
        self.assertEqual((prepared.title, prepared.remember["app"]), ("Open this link in Things?", "url:things"))
        self.store.add("url:things", LINK_LABEL, LINK_OP, "Things")
        prepared = skill.prepare(self.ctx(), {"text": THINGS}, None)
        self.assertEqual((prepared.title, prepared.remember), (None, None))

    def test_a_call_still_asks_after_dont_ask_again_for_phone_and_a_web_page_never_asks(self):
        from mobile_agent.skills.open_url import OpenUrl
        skill = OpenUrl(sleep=lambda s: None)
        self.store.add(PHONE, LINK_LABEL, LINK_OP, "Phone")
        prepared = skill.prepare(self.ctx(), {"text": "tel:+15555550123"}, None)
        self.assertEqual((prepared.title, prepared.remember), ("Call +15555550123?", None))
        self.assertIsNone(skill.prepare(self.ctx(), {"text": "https://example.com"}, None).title)


class CodingAgentTests(Store):
    def setUp(self):
        super().setUp()
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
        self.asked, self.opened, self.answer = [], [], "approved"

        def approve(request):
            self.asked.append(request)
            if self.answer == "approved" and request.get("remember"):
                spec = request["remember"]
                self.store.add(spec["app"], spec["label"], spec["op"], spec["app_name"])
            return self.answer
        phone = world.ScriptedPhone(world.new_phone_state())
        phone.open_url = self.opened.append
        self.tools = PhoneTools(phone, apps=dict(world.APP_NAMES), goal="Add milk to Things", emit=lambda e: None,
                                approve=approve, ask=approve, remembered=self.store.contains,
                                permissions=permissions.PermissionGate(permissions.ASK_IMPORTANT,
                                                                       goal="Add milk to Things"))

    def call(self, url):
        result = self.tools.call("open_url", {"url": url})
        return result.text, result.is_error

    def test_the_first_link_to_an_app_asks_and_after_dont_ask_again_it_opens(self):
        self.call(THINGS)
        self.assertEqual([(r["title"], r["remember"]["words"]) for r in self.asked],
                         [("Open this link in Things?", "Don’t ask again for Things")])
        text, failed = self.call("things:///show?id=today")
        self.assertFalse(failed, text)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.opened, [THINGS, "things:///show?id=today"])

    def test_a_call_asks_after_dont_ask_again_for_phone_links_and_a_web_page_never_asks(self):
        self.store.add(PHONE, LINK_LABEL, LINK_OP, "Phone")
        self.answer = "denied"
        text, failed = self.call("tel:+15555550123")
        self.assertTrue(failed)
        self.assertEqual([(r["title"], r.get("remember")) for r in self.asked], [("Call +15555550123?", None)])
        text, failed = self.call("https://example.com/hours")
        self.assertFalse(failed, text)
        self.assertEqual((len(self.asked), self.opened), (1, ["https://example.com/hours"]))

    def test_with_nobody_to_answer_a_link_that_asks_isnt_opened(self):
        self.tools.approve = self.tools.ask = None
        text, failed = self.call(THINGS)
        self.assertTrue(failed)
        self.assertIn("didn't open it", text)
        self.assertEqual(self.opened, [])


class WdaOpenTests(Store):
    INSTAGRAM = "com.burbn.instagram"

    def setUp(self):
        super().setUp()
        from mobile_agent.ai_plans.phone_tools import PhoneTools
        from mobile_agent.tests import sota_world as world
        self.calls, self.sheet = [], None
        phone = world.ScriptedPhone(world.new_phone_state())
        phone.open_url = None
        phone.call = self.call_wda
        patcher = patch.object(deeplinks.time, "sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tools = PhoneTools(phone, apps=dict(world.APP_NAMES), goal="Open the inbox", emit=lambda e: None,
                                approve=lambda request: "approved", ask=lambda request: "approved",
                                remembered=self.store.contains,
                                permissions=permissions.PermissionGate(permissions.ASK_IMPORTANT,
                                                                       goal="Open the inbox"))

    def call_wda(self, method, path, body=None, timeout=None):
        self.calls.append((method, path, body))
        if path == "/alert/text":
            return self.sheet
        if path == "/wda/alert/buttons":
            return ["Cancel", "Open"]
        return None

    def test_a_web_address_opens_its_app_form_with_the_bundle_and_presses_open_on_the_sheet(self):
        self.sheet = "Open in “Instagram”?"
        result = self.tools.call("open_url", {"url": "https://www.instagram.com/direct/inbox/"})
        self.assertFalse(result.is_error, result.text)
        self.assertIn(("POST", "/url", {"url": "instagram://direct-inbox", "bundleId": self.INSTAGRAM}), self.calls)
        self.assertIn(("POST", "/alert/accept", {"name": "Open"}), self.calls)

    def test_a_link_that_asks_is_still_refused_with_nobody_to_answer(self):
        self.tools.approve = self.tools.ask = None
        result = self.tools.call("open_url", {"url": "tel:+15555550123"})
        self.assertTrue(result.is_error)
        self.assertNotIn("/url", [path for method, path, body in self.calls])


class CommandLineTests(Store):
    def open(self, url, yes=False):
        from contextlib import contextmanager

        from mobile_agent.phone_io import cli
        opened = []

        @contextmanager
        def wda(record):
            yield None
        with patch.object(exceptions, "default_path", lambda: self.store.path), \
                patch.object(cli, "choose_device", lambda name: {"id": "d", "name": "iPhone"}), \
                patch("mobile_agent.phone_io.wda", wda, create=True), \
                patch.object(deeplinks, "open_link", lambda driver, url, bundle=None: opened.append(url) or {}), \
                patch.object(cli, "_print", lambda *a, **k: None):
            cli._open(SimpleNamespace(url=url, yes=yes, device=None), False)
        return opened

    def test_a_web_address_that_opens_the_feed_opens_its_app_link_and_the_command_says_so(self):
        from contextlib import contextmanager

        from mobile_agent.phone_io import cli
        printed, calls = [], []

        @contextmanager
        def wda(record):
            yield lambda method, path, body=None, timeout=10: calls.append((path, body))
        with patch.object(exceptions, "default_path", lambda: self.store.path), \
                patch.object(cli, "choose_device", lambda name: {"id": "d", "name": "iPhone"}), \
                patch("mobile_agent.phone_io.wda", wda, create=True), \
                patch.object(cli, "_print", lambda payload, as_json, text: printed.append((payload, text))), \
                patch.object(deeplinks.time, "sleep", lambda s: None):
            cli._open(SimpleNamespace(url="https://www.instagram.com/reels/", yes=False, device=None), False)
            cli._open(SimpleNamespace(url="instagram://reels_home", yes=False, device=None), False)
        self.assertIn(("/url", {"url": "instagram://reels_home", "bundleId": "com.burbn.instagram"}), calls)
        self.assertNotIn("https://www.instagram.com/reels/", [body["url"] for path, body in calls if path == "/url"])
        self.assertEqual(printed[0][0]["requested"], "https://www.instagram.com/reels/")
        self.assertEqual(printed[0][1], "Opened instagram://reels_home on iPhone: the app link for "
                                        "https://www.instagram.com/reels/, which opens the home feed.")
        self.assertEqual(printed[1][1], "Opened instagram://reels_home on iPhone.")

    def test_a_link_that_asks_needs_yes_until_dont_ask_again_and_a_call_always_does(self):
        from mobile_agent.phone_io import PhoneIOError
        with self.assertRaises(PhoneIOError):
            self.open(THINGS)
        self.assertEqual(self.open(THINGS, yes=True), [THINGS])
        self.store.add("url:things", LINK_LABEL, LINK_OP, "Things")
        self.assertEqual(self.open(THINGS), [THINGS])
        self.store.add(PHONE, LINK_LABEL, LINK_OP, "Phone")
        with self.assertRaises(PhoneIOError):
            self.open("tel:+15550100")
        self.assertEqual(self.open("https://example.com"), ["https://example.com"])


class MCPTests(Store):
    def harness(self):
        from mobile_agent.tests.test_mcp_new_tools import NewToolsHarness
        test = NewToolsHarness()
        test.setUp()
        self.addCleanup(test.doCleanups)
        test.make()
        patcher = patch.object(exceptions, "default_path", lambda: self.store.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return test

    def test_another_apps_link_asks_in_the_mac_app_every_time_and_a_rule_is_never_used(self):
        from mobile_agent.permissions import outside
        test = self.harness()
        self.store.add("url:things", LINK_LABEL, LINK_OP, "Things")
        refused = test.call("open_url", device="Work iPhone", url=THINGS)
        self.assertTrue(refused.is_error)
        self.assertIn("Mobster app isn't open", refused.text)
        self.assertIn("Nothing was opened", refused.text)
        asked = []
        with patch.object(outside, "ask_app", lambda request, **kw: asked.append(request) or {"state": "approved"}):
            opened = test.call("open_url", device="Work iPhone", url=THINGS)
        self.assertFalse(opened.is_error, opened.text)
        self.assertEqual(asked[0]["operation"], "OPEN_URL")
        with patch.object(outside, "ask_app", lambda request, **kw: {"state": "denied"}):
            declined = test.call("open_url", device="Work iPhone", url=THINGS)
        self.assertTrue(declined.is_error)
        self.assertIn("declined", declined.text)

    def test_a_call_asks_every_time_and_a_web_page_opens(self):
        test = self.harness()
        self.store.add(PHONE, LINK_LABEL, LINK_OP, "Phone")
        refused = test.call("open_url", device="Work iPhone", url="tel:+15550100")
        self.assertTrue(refused.is_error)
        self.assertIn("Mobster app isn't open", refused.text)
        opened = test.call("open_url", device="Work iPhone", url="https://example.com")
        self.assertFalse(opened.is_error, opened.text)


if __name__ == "__main__":
    unittest.main()

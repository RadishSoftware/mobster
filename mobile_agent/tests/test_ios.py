import json
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from mobile_agent import drivers, mirroring
from mobile_agent.ios import kb, system
from mobile_agent.learned.routes import RouteMemory
from mobile_agent.state import Element, Snapshot
from mobile_agent.wda import system_dialogs

FIXTURES = Path(__file__).parent / "fixtures" / "ios"
CLOCK = "com.iosworld.benchmark.clock"


def tree(name, bundle=None):
    data = json.loads((FIXTURES / "tree" / f"{name}.json").read_text())
    elements = []
    for index, e in enumerate(data["elements"]):
        actions = ("TAP", "TYPE") if e.get("editable") else ("TAP",)
        elements.append(Element(id=f"e{index}", label=e["label"], role=e["role"], rect=tuple(e["rect"]),
                                editable=bool(e.get("editable")), value=e.get("value") or "", actions=actions,
                                placeholder=e.get("placeholder") or "", selected=bool(e.get("selected"))))
    return Snapshot(elements=elements, text="\n".join(e.label for e in elements), width=402.0, height=874.0,
                    source="wda", bundle_id=bundle or data["bundle"], keyboard=data.get("keyboard") or "",
                    image_hash=name, revision=name)


def ocr(name):
    return json.loads((FIXTURES / "ocr" / f"{name}.json").read_text())


class TreePhone(drivers.Driver):
    capabilities = frozenset({"active_app", "opens_by_bundle"})
    opens_by_bundle = True

    def __init__(self, screen="home_p1", hidden=()):
        self.screen, self.hidden, self.ops = screen, set(hidden), []
        self.front = "com.apple.springboard"

    def observe(self, timeout=10):
        if self.screen.startswith("app:"):
            return Snapshot(elements=[Element(id="e0", label="An app", role="StaticText", rect=(.1, .1, .5, .05))],
                            text="An app", width=402.0, height=874.0, source="wda", bundle_id=self.front,
                            image_hash=self.screen)
        return tree(self.screen, self.front if self.front != "com.apple.springboard" else None)

    def active_app(self, timeout=5):
        return self.front

    def _go(self, screen, front="com.apple.springboard"):
        self.screen, self.front = screen, front

    def execute(self, op, target, snapshot, text=None, timeout=10):
        label = getattr(target, "label", target)
        self.ops.append((op, label, text))
        screen = self.screen
        if op == "HOME":
            self._go("home_p1")
        elif op == "LAUNCH_APP":
            self._go("settings_root" if target == "com.apple.Preferences" else "app:" + target, target)
        elif op == "SWIPE_LEFT":
            following = {"home_p1": "home_p2", "home_p2": "home_p3", "home_p3": "app_library"}
            self._go(following.get(screen, screen))
        elif op == "SWIPE_UP":
            pass
        elif op == "SWIPE_DOWN" and screen.startswith("home"):
            self._go("spotlight")
        elif op == "TAP":
            self._tap(screen, label)
        elif op == "TYPE":
            self._type(screen, text)
        else:
            raise ValueError(op)

    def _tap(self, screen, label):
        if screen.startswith("home") and label == "Search":
            self._go("spotlight")
        elif screen == "home_p2" and label == "Contacts":
            self._go("app:com.apple.MobileAddressBook", "com.apple.MobileAddressBook")
        elif screen == "app_library" and label == "dewey-search-field":
            self._go("app_library_search")
        elif screen == "app_library_search_clock" and label == "Clock":
            self._go("app:" + CLOCK, CLOCK)
        elif screen == "spotlight_settings" and label == "Settings":
            self._go("settings_root", "com.apple.Preferences")
        elif label == "Clear text":
            pass
        else:
            raise AssertionError(f"no recorded screen after tapping {label!r} on {screen}")

    def _type(self, screen, text):
        if screen in ("spotlight", "spotlight_nohit", "spotlight_settings"):
            name = text.strip()
            self._go("spotlight_settings" if name == "Settings" and name not in self.hidden else "spotlight_nohit")
        elif screen == "app_library_search" and text == "Clock":
            self._go("app_library_search_clock")
        else:
            raise AssertionError(f"no recorded screen after typing on {screen}")

    def close(self):
        pass


class Memory:
    def __init__(self):
        self.routes, self.missed = {}, []

    def get(self, phone, what):
        return self.routes.get((phone, what))

    def put(self, phone, what, route):
        self.routes[(phone, what)] = route

    def miss(self, phone, what, route):
        self.missed.append((what, route))
        self.routes.pop((phone, what), None)


def no_bundle(phone):
    phone.opens_by_bundle = False
    return phone


def quick():
    return mock.patch.object(system.time, "sleep", lambda s: None)


class KnowledgeBaseTests(unittest.TestCase):
    def test_versions_answer_with_their_own_entries(self):
        self.assertEqual(kb.query("show lowercase keys", 26)[0].id, "settings.a11y.keyboards.ios26")
        self.assertEqual(kb.query("show lowercase keys", 18)[0].id, "settings.a11y.keyboards.ios18")
        self.assertEqual(kb.known(None), kb.NEWEST)
        self.assertEqual(kb.known("27.1"), kb.NEWEST)
        self.assertEqual(kb.known("17.5"), kb.OLDEST)
        self.assertIn("unknown", kb.answer("app library", None))


class OpenAppTests(unittest.TestCase):
    def test_by_bundle_id_when_the_connection_can(self):
        phone = TreePhone()
        with quick():
            outcome = system.open_app(phone, "com.apple.Preferences", "Settings")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.route, "bundle")
        self.assertEqual([op for op, *_ in phone.ops], ["LAUNCH_APP"])

    def test_spotlight_opens_an_app_only_through_its_top_hit(self):
        phone = no_bundle(TreePhone())
        with quick():
            outcome = system.open_app(phone, "com.apple.Preferences", "Settings")
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertEqual(outcome.route, "spotlight")
        self.assertIn(("TAP", "Settings", None), phone.ops)

    def test_an_app_hidden_from_spotlight_opens_from_the_app_library(self):
        phone, memory = no_bundle(TreePhone(hidden={"Clock"})), Memory()
        with quick():
            outcome = system.open_app(phone, CLOCK, "Clock", routes=memory, phone="phone-1")
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertEqual(outcome.route, "app_library")
        self.assertIn("spotlight: Spotlight's top hit isn't Clock", outcome.steps)
        self.assertNotIn("Zzqxw", " ".join(str(op) for op in phone.ops))
        self.assertFalse(any(op == "TAP" and label in ("Search in Apps", "Search Maps") for op, label, _ in phone.ops))
        self.assertEqual(memory.get("phone-1", CLOCK), "app_library")

    def test_the_remembered_route_goes_first(self):
        phone, memory = no_bundle(TreePhone(hidden={"Clock"})), Memory()
        memory.put("phone-1", CLOCK, "app_library")
        with quick():
            outcome = system.open_app(phone, CLOCK, "Clock", routes=memory, phone="phone-1")
        self.assertEqual((outcome.ok, outcome.route), (True, "app_library"))
        self.assertNotIn(("TAP", "Search", None), phone.ops)

    def test_the_home_screen_pages_are_the_last_route(self):
        phone = no_bundle(TreePhone())
        with quick():
            outcome = system.ROUTE_FUNCS["home_pages"](phone, "com.apple.MobileAddressBook", "Contacts")
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertEqual([op for op, *_ in phone.ops if op == "SWIPE_LEFT"], ["SWIPE_LEFT"])

    def test_a_wrong_app_under_the_same_name_is_not_opened(self):
        phone = no_bundle(TreePhone(hidden={"Clock"}))
        with quick():
            outcome = system.ROUTE_FUNCS["app_library"](phone, "com.apple.mobiletimer", "Clock")
        self.assertFalse(outcome.ok)

    def test_no_name_and_no_bundle_route_means_no_guess(self):
        phone = no_bundle(TreePhone())
        outcome = system.open_app(phone, "com.example.app", None)
        self.assertFalse(outcome.ok)
        self.assertEqual(phone.ops, [])


class MirroringHelper:
    def __init__(self, hidden=()):
        self.screen, self.hidden, self.requests, self.text = "home_p1", set(hidden), [], ""

    def request(self, op, timeout=15, **fields):
        self.requests.append((op, fields))
        if op == "capture":
            if "path" in fields:
                Path(fields["path"]).write_bytes(b"\xff\xd8\xff\xd9")
            answer = dict(ocr(self.screen)) if self.screen != "app" else {
                "ok": True, "window": {"w": 318, "h": 701}, "hash": "app", "rows": [
                    {"text": "Alarms", "confidence": 1, "rect": [.4, .1, .2, .03]}]}
            if not fields.get("ocr"):
                answer.pop("rows", None)
            return answer
        if op == "key" and fields["combo"] == "cmd+1":
            self.screen = "home_p1"
        elif op == "key" and fields["combo"] == "cmd+3":
            self.screen, self.text = "spotlight", ""
        elif op == "type":
            self.text += fields["text"]
            if self.screen.startswith("spotlight"):
                self.screen = "spotlight_settings" if self.text == "Settings" and "Settings" not in self.hidden \
                    else "spotlight_nohit"
            elif self.screen == "app_library_search" and self.text == "Clock":
                self.screen = "app_library_search_clock"
        elif op == "drag":
            following = {"home_p1": "home_p3", "home_p3": "app_library"}
            self.screen = following.get(self.screen, self.screen)
        elif op == "tap":
            snapshot = mirroring.screen_from(ocr(self.screen))
            hit = next((e for e in snapshot.elements if abs(e.center[0] - fields["x"]) < .03
                        and abs(e.center[1] - fields["y"]) < .03), None)
            label = hit.label if hit else ""
            if self.screen == "app_library" and label == "Q App Library":
                self.screen, self.text = "app_library_search", ""
            elif self.screen == "app_library_search_clock" and label == "Clock":
                self.screen = "app"
            elif self.screen == "spotlight_settings" and label == "Settings":
                self.screen = "app"
        return {"ok": True}

    def gestures(self):
        return [(op, f) for op, f in self.requests if op not in ("capture", "state")]

    def close(self):
        pass


class MirroringOpenAppTests(unittest.TestCase):
    def setUp(self):
        self.frames = TemporaryDirectory()

    def tearDown(self):
        self.frames.cleanup()

    def driver(self, helper, **names):
        return mirroring.MirroringDriver(helper, frames_dir=self.frames.name, sleep=lambda s: None,
                                         app_names={CLOCK: "Clock", **names})

    def test_the_recorded_spotlight_miss_opens_from_the_app_library_and_never_presses_return(self):
        helper = MirroringHelper(hidden={"Clock"})
        d = self.driver(helper)
        d.routes = Memory()
        with quick():
            d.execute("LAUNCH_APP", CLOCK, None)
        sent = helper.gestures()
        self.assertNotIn(("key", {"combo": "return"}), sent)
        self.assertIn(("key", {"combo": "cmd+3"}), sent)
        self.assertEqual(d.current_app, CLOCK)
        self.assertEqual(d.routes.get(mirroring.PHONE_KEY, CLOCK), "app_library")

    def test_an_app_no_route_finds_fails_with_where_it_stopped(self):
        helper = MirroringHelper(hidden={"Settings"})
        d = self.driver(helper, **{"com.example.gone": "Gone"})
        with quick(), self.assertRaises(mirroring.TransportError) as caught:
            d.execute("LAUNCH_APP", "com.example.gone", None)
        self.assertIn("App Library", str(caught.exception))


def screen_with(rows):
    elements = [Element(id=f"t{i}", label=text, role="StaticText", rect=rect) for i, (text, rect) in enumerate(rows)]
    return Snapshot(elements=elements, text="\n".join(t for t, _ in rows), width=393.0, height=866.0,
                    source="mirroring", bundle_id="com.example.app")


class AlertOnScreenTests(unittest.TestCase):
    NOTIFICATIONS = [("“Duolingo” Would Like to Send You Notifications", (.15, .38, .7, .05)),
                     ("Notifications may include alerts, sounds and icon badges.", (.15, .44, .7, .04)),
                     ("Don't Allow", (.15, .52, .3, .04)), ("Allow", (.55, .52, .3, .04))]

    def test_an_apps_own_ok_and_cancel_stay_the_apps(self):
        rows = [("Delete this post?", (.2, .4, .6, .04)), ("Cancel", (.15, .5, .3, .04)), ("OK", (.55, .5, .3, .04))]
        self.assertIsNone(system_dialogs.from_screen(screen_with(rows)))

    def test_a_pixels_only_connection_taps_the_button_by_name(self):
        snapshot = screen_with(self.NOTIFICATIONS)
        dialog = system_dialogs.from_screen(snapshot)
        driver = mock.Mock(spec=["execute", "observe"])
        system_dialogs.press(driver, system_dialogs.Decision("press", dialog, "Don't Allow"), snapshot=snapshot)
        tapped = driver.execute.call_args.args
        self.assertEqual((tapped[0], tapped[1].label), ("TAP", "Don't Allow"))

    def test_an_ios_update_nag_gets_later_and_never_install(self):
        nag = system_dialogs.classify("iOS 26.1 is now available. Install Now or Later?",
                                      ["Install Now", "Later", "Install Tonight"])
        decision = system_dialogs.decide(nag, system_dialogs.ASK_IMPORTANT, can_ask=False)
        self.assertEqual((nag.kind, decision.action, decision.button), ("software_update", "press", "Later"))

    def test_an_update_nag_with_no_later_still_asks(self):
        nag = system_dialogs.classify("A software update is available.", ["Install Now", "Details"])
        self.assertEqual(system_dialogs.decide(nag, system_dialogs.ASK_IMPORTANT, can_ask=True).action, "ask")
        self.assertEqual(system_dialogs.decide(nag, system_dialogs.DONT_ASK, can_ask=False).action, "stop")

    def test_a_screen_time_limit_stops_and_undo_typing_keeps_the_text(self):
        limit = system_dialogs.classify("Time Limit. You've reached your limit on Instagram.", ["OK", "Ignore Limit"])
        self.assertEqual(system_dialogs.decide(limit, system_dialogs.DONT_ASK, can_ask=False).action, "stop")
        shake = system_dialogs.classify("Undo Typing", ["Cancel", "Undo"])
        decision = system_dialogs.decide(shake, system_dialogs.DONT_ASK, can_ask=False)
        self.assertEqual((decision.action, decision.button), ("press", "Cancel"))

    def test_a_website_sign_in_asks(self):
        consent = system_dialogs.classify("“Strava” Wants to Use “google.com” to Sign In", ["Cancel", "Continue"])
        self.assertEqual(consent.kind, "web_sign_in")
        self.assertEqual(system_dialogs.decide(consent, system_dialogs.DONT_ASK, can_ask=True).action, "ask")
        self.assertEqual(system_dialogs.decide(consent, system_dialogs.DONT_ASK, can_ask=False).action, "stop")


class RouteMemoryTests(unittest.TestCase):
    def test_routes_are_kept_per_phone_and_app_and_forgotten_on_a_miss(self):
        with TemporaryDirectory() as folder:
            memory = RouteMemory(Path(folder) / "routes")
            self.assertIsNone(memory.get("phone-1", CLOCK))
            memory.put("phone-1", CLOCK, "app_library")
            self.assertEqual(RouteMemory(Path(folder) / "routes").get("phone-1", CLOCK), "app_library")
            self.assertIsNone(memory.get("other", CLOCK))
            files = list((Path(folder) / "routes").iterdir())
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
            self.assertNotIn("Clock", files[0].read_text())
            memory.miss("phone-1", CLOCK, "spotlight")
            self.assertEqual(memory.get("phone-1", CLOCK), "app_library")
            memory.miss("phone-1", CLOCK, "app_library")
            self.assertIsNone(memory.get("phone-1", CLOCK))
            memory.put("phone-1", CLOCK, "teleport")
            self.assertIsNone(memory.get("phone-1", CLOCK))


class GuideTests(unittest.TestCase):
    def test_the_slice_reaches_the_engine_through_its_hook_on_ioss_own_screens(self):
        from mobile_agent import harness_api
        driver = mock.Mock(spec=[])
        driver.ios_version = "26.4"
        section = harness_api.hooks().ios_slice(tree("settings_root"), "Make the text cursor stop blinking", driver)
        self.assertIn("How iOS 26 works here", section)
        self.assertIn("Prefer Non-Blinking Cursor", section)

    def test_an_apps_own_screen_gets_no_slice(self):
        from mobile_agent import harness_api
        driver = mock.Mock(spec=[])
        driver.ios_version = "26.4"
        snapshot = tree("settings_root", bundle="com.example.notes")
        self.assertEqual(harness_api.hooks().ios_slice(snapshot, "Make the text cursor stop blinking", driver), "")

    def test_the_agent_is_offered_no_ios_docs_action(self):
        from mobile_agent import harness_api, tracks
        tracks.load()
        modules = [getattr(entry.value, "__module__", "") for entry in harness_api._tools]
        self.assertFalse([m for m in modules if m.startswith("mobile_agent.ios")])

    def test_the_mcp_tool_is_read_only_and_local(self):
        from mobile_agent.ios.mcp import IosDocsProvider
        provider = IosDocsProvider()
        self.assertTrue(provider.annotations()["ios_docs"]["readOnlyHint"])
        result = provider.call("ios_docs", {"query": "app library search", "ios_version": "26.0.1"}, None, None)
        self.assertEqual(result.structured["iosVersion"], 26)
        self.assertTrue(result.structured["entries"])
        self.assertIn("App Library", result.text)


if __name__ == "__main__":
    unittest.main()

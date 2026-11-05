"""The queue's phone checks (phone_ready.py): the flag, what each helper read means, the two helper routes that are
added only with the flag on, Low Power Mode in a phone's status and on a scheduled task. Fakes only: no phone."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import device_status, phone_ready, power_watch, setup_service
from mobile_agent.wda import patches as wda_patches
from mobile_agent.wda import timeouts as wda_timeouts

ON = {phone_ready.ENV: "1"}
FIXTURE = Path(__file__).parent / "fixtures" / "wda-private-input.json"
SOURCE = "WebDriverAgentLib/Commands/FBCustomCommands.m"


def helper(answers):
    """phone_ready.read as a helper that answers ``answers`` (path -> value), and nothing else."""
    return mock.patch.object(phone_ready, "read", side_effect=lambda url, path: answers.get(path))


helper_of = helper


class FlagTests(unittest.TestCase):
    def test_it_is_off_unless_set_to_a_yes(self):
        self.assertFalse(phone_ready.enabled({}))
        for value in ("", "0", "off", "false", "maybe"):
            self.assertFalse(phone_ready.enabled({phone_ready.ENV: value}), value)
        for value in ("1", "true", "YES", " on "):
            self.assertTrue(phone_ready.enabled({phone_ready.ENV: value}), value)


class ReadTests(unittest.TestCase):
    def test_a_read_is_a_bounded_sessionless_get_and_a_failure_is_none(self):
        calls = []

        def kept(url, path, timeout):
            calls.append((url, path, timeout))
            return {"value": {"lowPowerMode": True}}
        with mock.patch("mobile_agent.transport.read_kept", kept):
            self.assertEqual(phone_ready.read("http://h", "/mobster/power"), {"lowPowerMode": True})
        self.assertEqual(calls, [("http://h", "/mobster/power", wda_timeouts.WDA_CALL_TIMEOUTS["probe"])])
        with mock.patch("mobile_agent.transport.read_kept", side_effect=OSError("down")):
            self.assertIsNone(phone_ready.read("http://h", "/wda/locked"))
        self.assertIsNone(phone_ready.read(None, "/wda/locked"))

    def test_only_an_unlocked_phone_does_not_hold(self):
        for locked, held in ((False, None), (True, phone_ready.HOLD_LOCKED), (None, phone_ready.HOLD_UNREADABLE),
                             ("no", phone_ready.HOLD_UNREADABLE)):
            with self.subTest(locked=locked), helper({"/wda/locked": locked}):
                self.assertEqual(phone_ready.hold("http://h"), held)

    def test_photos_access_that_lets_it_add_passes_and_the_rest_refuses_in_the_transfers_words(self):
        from mobile_agent.phone_io.photos import STATUS_WORDS
        for status in ("authorized", "limited"):
            with self.subTest(status=status), helper({"/mobster/photos/status": {"status": status}}):
                self.assertIsNone(phone_ready.photos_refusal("http://h", "alex"))
        for status in ("denied", "restricted", "not_determined"):
            with self.subTest(status=status), helper({"/mobster/photos/status": {"status": status}}):
                refused = phone_ready.photos_refusal("http://h", "alex")
                words = phone_ready.NOT_ASKED if status == "not_determined" else STATUS_WORDS[status]
                self.assertEqual((refused.code, str(refused)), ("photos_" + status, words.format(name="alex")))
        self.assertIn("press Allow beside", phone_ready.NOT_ASKED)   # Setup's row, not a question already on the iPhone
        for answer in (None, {}, {"status": "authorised"}, "authorized"):
            with self.subTest(answer=answer), helper({"/mobster/photos/status": answer}):
                self.assertEqual(phone_ready.photos_refusal("http://h", "alex").code, "photos_unreadable")

    def test_low_power_mode_is_a_warning_only_when_the_helper_says_so(self):
        for answer, warned in (({"lowPowerMode": True}, True), ({"lowPowerMode": False}, False), (None, False),
                               ({"lowPowerMode": "yes"}, False)):
            with self.subTest(answer=answer), helper({"/mobster/power": answer}):
                words = phone_ready.low_power_warning("http://h", "alex", "the first post")
                self.assertEqual(words is not None, warned)
        with helper({"/mobster/power": {"lowPowerMode": True}}):
            self.assertEqual(phone_ready.low_power_warning("http://h", "alex", "the first post"),
                             "Low Power Mode is on for alex, so it locks itself after 30 seconds. Turn it off in "
                             "Settings › Battery before the first post.")

    def test_the_helper_routes_read_in_the_probe_bound(self):
        for path in ("/mobster/power", "/mobster/photos/status"):
            self.assertEqual(wda_timeouts.classify("GET", path), "probe")
        self.assertTrue(wda_timeouts.is_action("POST", "/mobster/photos/authorize"))


class HelperPatchTests(unittest.TestCase):
    def test_the_routes_join_the_helper_only_with_the_flag_on(self):
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop(phone_ready.ENV, None)
            off = wda_patches.patch_digest()
            self.assertEqual(wda_patches.active_patches(), wda_patches.WDA_PATCHES)
        with mock.patch.dict(os.environ, ON):
            on = wda_patches.patch_digest()
            patches = wda_patches.active_patches()
            self.assertEqual(len(patches), len(wda_patches.WDA_PATCHES) + len(wda_patches.PREFLIGHT_PATCHES))
            self.assertEqual(patches[-len(wda_patches.PREFLIGHT_PATCHES):], wda_patches.PREFLIGHT_PATCHES)
            named = [p for p in patches if "stringByAppendingPathExtension:extension" in p[2]]
            # the chunked save's file gets its extension
            self.assertEqual(named, [wda_patches.TRANSFER_COMMANDS_NAMED_PATCH])
        self.assertFalse(any("stringByAppendingPathExtension:extension" in p[2] for p in wda_patches.WDA_PATCHES))
        self.assertNotEqual(off, on)   # each phone rebuilds its helper once when the flag changes

    def patched(self):
        """The real FBCustomCommands.m (the pinned release) with every patch that edits it, applied twice."""
        fixture = json.loads(FIXTURE.read_text())
        own = tuple(p for p in wda_patches.WDA_PATCHES if p[0] == SOURCE)
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, ON), \
                mock.patch.object(wda_patches, "WDA_PATCHES", own):
            path = Path(folder) / SOURCE
            path.parent.mkdir(parents=True)
            path.write_text(fixture["files"][SOURCE])
            wda_patches.patch_wda(folder)
            once = path.read_text()
            wda_patches.patch_wda(folder)
            self.assertEqual(path.read_text(), once)
        return once

    def test_the_routes_and_their_commands_apply_once_beside_the_photos_and_secret_routes(self):
        text = self.patched()
        for route in ('@"/mobster/photos/authorize"', '@"/mobster/power"'):
            routes = text.count(f"FBRoute POST:{route}") + text.count(f"FBRoute GET:{route}")
            self.assertEqual(routes, 2, route)  # session, none
        for method in ("handleMobsterPhotosAuthorize", "handleMobsterPower"):
            self.assertEqual(text.count(f"+ (id<FBResponsePayload>){method}:(FBRouteRequest *)request"), 1, method)
        self.assertEqual(text.count("[path stringByAppendingPathExtension:extension]"), 1)   # in the chunked save only
        for kept in ("handleMobsterPhotosAdd:", "handleMobsterPhotosSave:", "handleMobsterSecret:"):
            self.assertEqual(text.count(f"+ (id<FBResponsePayload>){kept}(FBRouteRequest *)request"), 1, kept)

    def test_authorize_asks_and_saves_nothing_and_power_only_reads(self):
        text = self.patched()
        authorize = text[text.index("handleMobsterPhotosAuthorize:(FBRouteRequest *)request\n{"):]
        authorize = authorize[:authorize.index("handleMobsterPower")]
        self.assertIn("PHAuthorizationStatusNotDetermined == status", authorize)   # asks only while unanswered
        self.assertIn("requestAuthorizationForAccessLevel:PHAccessLevelReadWrite", authorize)
        for forbidden in ("performChanges", "PHAssetCreationRequest", "deleteAssets", "resetAuthorizationStatus"):
            self.assertNotIn(forbidden, authorize)
        power = text[text.index("handleMobsterPower:(FBRouteRequest *)request\n{"):]
        power = power[:power.index("\n}\n")]
        self.assertIn("isLowPowerModeEnabled", power)
        for forbidden in ("setBatteryMonitoringEnabled", "performChanges", "request.arguments"):
            self.assertNotIn(forbidden, power)


class StatusTests(unittest.TestCase):
    def test_a_phones_status_has_low_power_mode_only_with_the_flag_and_a_helper_that_says(self):
        wda = lambda url, battery=True: ({"level": 80, "state": "unplugged"}, "nominal")  # noqa: E731
        with mock.patch.object(device_status.native_helpers, "devices_allowed", return_value=True), \
                helper({"/mobster/power": {"lowPowerMode": True}}):
            without = device_status.read_phone("wda", wda_url="http://h", wda=wda, clock=lambda: 1)
            with mock.patch.dict(os.environ, ON):
                with_flag = device_status.read_phone("wda", wda_url="http://h", wda=wda, clock=lambda: 1)
        self.assertNotIn("lowPowerMode", without)
        self.assertIs(with_flag["lowPowerMode"], True)
        rows = dict(device_status.rows(with_flag, {}))
        self.assertEqual(rows["Low Power Mode"], "on, so it locks after 30 seconds")
        self.assertNotIn("Low Power Mode", dict(device_status.rows({**with_flag, "lowPowerMode": False}, {})))


class ScheduledTaskTests(unittest.TestCase):
    def run_started(self, origin, answer, flag=True):
        events, done = [], threading.Event()
        run = SimpleNamespace(id="r1", origin=origin, emit=lambda event: (events.append(event), done.set()))
        phone = SimpleNamespace(wda_url="http://h", name="alex", id="phone-one")
        runtime = SimpleNamespace(phone_for=lambda r: phone, active_runs={}, lock=threading.Lock(), runs={})
        watch = power_watch.PowerWatch(runtime, read_mac=lambda: {"power": "ac"}, interval=3600)
        with mock.patch.dict(os.environ, ON if flag else {phone_ready.ENV: "0"}), helper({"/mobster/power": answer}):
            watch.run_started(run)
            done.wait(1.0)
            watch.run_finished(run)
            watch.close()
        return events

    def test_a_scheduled_task_on_a_phone_in_low_power_mode_gets_one_warning(self):
        events = self.run_started("schedule", {"lowPowerMode": True})
        self.assertEqual([e["event"] for e in events], ["phone_low_power"])
        self.assertIn("Low Power Mode is on for alex", events[0]["message"])

    def test_nothing_is_said_for_a_normal_phone_another_origin_an_unreadable_phone_or_the_flag_off(self):
        self.assertEqual(self.run_started("schedule", {"lowPowerMode": False}), [])
        self.assertEqual(self.run_started("schedule", None), [])
        self.assertEqual(self.run_started("app", {"lowPowerMode": True}), [])
        self.assertEqual(self.run_started("schedule", {"lowPowerMode": True}, flag=False), [])


class SetupStepTests(unittest.TestCase):
    """Setup's "Let Mobster add to Photos": a step only with the queue checks on, answered on the iPhone."""

    def states(self, photos, *, runner="running", flag=True):
        from mobile_agent.tests.test_device_setup import ChecklistTests
        helper = ChecklistTests("states")
        device = {"udid": "u", "name": "iPhone", "trusted": True}
        service, env = helper.service(device=device, built=True, runner=runner, live=True, frame=True)
        service.manager.wda_url = "http://127.0.0.1:8100"
        from mobile_agent import mac_tools, setup_service
        with mock.patch.dict(os.environ, {**env, **({phone_ready.ENV: "1"} if flag else {})}, clear=True), \
                mock.patch.object(mac_tools, "executable", return_value="/opt/homebrew/bin/brew"), \
                mock.patch.object(setup_service, "plan_ready", return_value=False), \
                mock.patch.object(setup_service, "agents_ready", return_value=[]), \
                helper_of({"/mobster/photos/status": photos}):
            return service.state()

    def step(self, state):
        return next((s for s in state["steps"] if s["id"] == "photos"), None)

    def test_without_the_flag_setup_is_what_it_was(self):
        state = self.states({"status": "not_determined"}, flag=False)
        self.assertIsNone(self.step(state))
        self.assertEqual([s["id"] for s in state["steps"]],
                         ["tools", "api_key", "phone", "wda_build", "wda_running", "live", "video"])

    def test_each_answer_the_helper_gives_has_its_own_state_and_words(self):
        for status, expected, action in (("authorized", "done", None), ("limited", "done", None),
                                         ("not_determined", "todo", "allow_photos"), ("denied", "todo", None),
                                         ("restricted", "todo", None)):
            with self.subTest(status=status):
                step = self.step(self.states({"status": status}))
                self.assertEqual((step["state"], step["action"]), (expected, action))
                self.assertEqual(step["detail"], setup_service.PHOTOS_DETAIL[status])
        self.assertIn("Settings › Privacy & Security › Photos", setup_service.PHOTOS_DETAIL["denied"])
        self.assertEqual(self.step(self.states({"status": "not_determined"}))["title"], "Let Mobster add to Photos")

    def test_a_helper_that_doesnt_answer_reads_as_working_and_one_not_running_waits(self):
        self.assertEqual(self.step(self.states(None))["state"], "working")
        self.assertEqual(self.step(self.states({"status": "bogus"}))["state"], "working")
        waiting = self.step(self.states({"status": "authorized"}, runner="stopped"))
        self.assertEqual((waiting["state"], waiting["blocked_by"]), ("blocked", "wda_running"))

    def test_the_step_never_holds_the_rest_of_setup_back(self):
        state = self.states({"status": "not_determined"})
        self.assertTrue(state["complete"])
        self.assertEqual(self.step(state)["state"], "todo")


class AllowPhotosTests(unittest.TestCase):
    """Allow asks iOS to show its Photos question and nothing else: the person answers it on the iPhone."""

    def service(self):
        from mobile_agent.tests.test_device_setup import ChecklistTests
        return ChecklistTests("states").service()[0]

    def phone(self, statuses, calls, busy=None):
        import contextlib

        from mobile_agent.phone_io import PhoneIOError

        def request(method, path, body=None, timeout=15):
            calls.append((method, path))
            if path == "/mobster/photos/status":
                status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
                return {"status": status, "bundleId": "app.mobster.wda.runner.x"}
            if path == "/mobster/photos/authorize":
                return {"status": "not_determined", "asked": True}
            return {}

        @contextlib.contextmanager
        def runner():
            if busy:
                raise PhoneIOError(busy, "device_busy")
            yield request
        return SimpleNamespace(name="Studio iPhone", runner=runner)

    def allow(self, phone):
        with mock.patch.dict(os.environ, ON), mock.patch("mobile_agent.phone_io.transfer.phone_from_runtime",
                                                          return_value=phone):
            return self.service().allow_photos()

    def test_it_asks_only_while_ios_has_not_been_asked_and_saves_nothing(self):
        calls = []
        self.allow(self.phone(["not_determined"], calls))
        self.assertEqual(calls, [("GET", "/mobster/photos/status"), ("POST", "/wda/apps/activate"),
                                 ("POST", "/mobster/photos/authorize")])
        for answered in ("authorized", "limited", "denied", "restricted"):
            calls.clear()
            self.allow(self.phone([answered], calls))
            self.assertEqual(calls, [("GET", "/mobster/photos/status")], answered)   # never asked again, never changed
        every = {path for _, path in calls}
        self.assertFalse(every & {"/mobster/photos", "/mobster/photos/save", "/mobster/photos/chunk"})

    def test_it_refuses_with_the_checks_off_a_busy_phone_or_a_helper_without_the_route(self):
        off = mock.patch.dict(os.environ, {phone_ready.ENV: ""})
        with off, self.assertRaisesRegex(LookupError, "off in this Mobster"):
            self.service().allow_photos()
        with self.assertRaisesRegex(LookupError, "busy with a task"):
            self.allow(self.phone(["not_determined"], [], busy="Studio iPhone is busy with a task."))
        no_helper = SimpleNamespace(name="Studio iPhone", runner=None)
        with self.assertRaisesRegex(LookupError, "isn't set up"):
            self.allow(no_helper)


class FleetActionTests(unittest.TestCase):
    def test_a_set_up_phone_allows_photos_through_its_own_setup_and_one_without_a_helper_is_refused(self):
        from mobile_agent.fleet import Fleet
        setup = mock.Mock()
        phone = SimpleNamespace(kind="usb", manager=object(), setup=setup, id="phone-two", health_at=5.0)
        fleet = SimpleNamespace(runtime=SimpleNamespace(setup=mock.Mock()))
        self.assertIs(Fleet.setup_action(fleet, phone, "photos", {}), phone)
        setup.allow_photos.assert_called_once_with("phone-two")
        fleet.runtime.setup.allow_photos.assert_not_called()
        with self.assertRaises(KeyError):
            Fleet.setup_action(fleet, phone, "photos", {"x": 1})
        with self.assertRaises(LookupError):
            entry = SimpleNamespace(kind="usb", manager=None, setup=None, id="p", health_at=0)
            Fleet.setup_action(fleet, entry, "photos", {})


class AuthorizeTests(unittest.TestCase):
    def test_authorize_returns_where_the_answer_stands(self):
        from mobile_agent.phone_io import photos
        calls = []

        def request(method, path, body=None, timeout=15):
            calls.append((method, path, body, timeout))
            if path.endswith("/status"):
                return {"status": "not_determined", "bundleId": "app.mobster.wda.runner.x"}
            return {"status": "limited", "asked": True}
        self.assertEqual(photos.authorize(request, activate=lambda bundle: calls.append(("activate", bundle))),
                         {"status": "limited", "asked": True})
        self.assertEqual(calls[1], ("activate", "app.mobster.wda.runner.x"))
        self.assertEqual(calls[2][2], {"authorizationTimeout": photos.AUTHORIZE_WAIT_S})
        answer = photos.authorize(lambda *a, **k: {"status": "authorized"})
        self.assertEqual(answer, {"status": "authorized", "asked": False})

    def test_a_helper_built_without_the_route_says_to_rebuild(self):
        from mobile_agent.phone_io import PhoneIOError, photos

        def request(method, path, body=None, timeout=15):
            if path.endswith("/status"):
                return {"status": "not_determined", "bundleId": "x"}
            raise PhoneIOError("WebDriverAgent doesn't know that command.", "unknown_route")
        with self.assertRaises(PhoneIOError) as caught:
            photos.authorize(request)
        self.assertEqual(caught.exception.code, "needs_rebuild")


class RouteTests(unittest.TestCase):
    """POST /api/setup/photos is the person's click in the Mac app: another process with the token can't send it."""

    def setUp(self):
        import http.client

        from mobile_agent.server import BoundedServer, make_handler
        runtime = mock.Mock()
        runtime.setup.state.return_value = {"complete": False, "steps": []}
        self.runtime, self.http = runtime, http
        self.server = BoundedServer(("127.0.0.1", 0), make_handler(runtime, "t" * 43, app_session="s" * 43))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, session=None, body="{}"):
        connection = self.http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        headers = {"Host": "127.0.0.1:8765", "Authorization": "Bearer " + "t" * 43, "Content-Type": "application/json",
                   **({"X-Mobster-App-Session": session} if session else {})}
        connection.request("POST", "/api/setup/photos", body=body, headers=headers)
        response = connection.getresponse()
        response.read()
        connection.close()
        return response.status

    def test_only_the_mac_app_can_press_allow_and_a_refusal_is_a_409_with_its_sentence(self):
        self.assertEqual(self.post(), 403)
        self.runtime.setup.allow_photos.assert_not_called()
        self.assertEqual(self.post("s" * 43), 200)
        self.runtime.setup.allow_photos.assert_called_once_with()
        self.runtime.setup.allow_photos.side_effect = LookupError("busy")
        self.assertEqual(self.post("s" * 43), 409)
        self.assertEqual(self.post("s" * 43, body='{"x": 1}'), 404)   # it takes no fields


if __name__ == "__main__":
    unittest.main()

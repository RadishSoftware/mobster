import argparse
import io
import json
import os
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from mobile_agent import __main__ as cli
from mobile_agent import harness_api, lockscreen, mac_tools
from mobile_agent.agent_hooks import READY, STOP_CODES, GuardVerdict
from mobile_agent.api_errors import APIError
from mobile_agent.runtime_driver import PhoneNotReady, prepare_wda_phone
from mobile_agent.tests.test_server_engine import READY as READY_TARGET
from mobile_agent.tests.test_server_engine import Base

SB = lockscreen.SPRINGBOARD
UDID = "00008130-0011223344556677"


def node(label, kind="StaticText", children=()):
    return {"type": f"XCUIElementType{kind}", "label": label, "children": list(children)}


def tree(*children):
    return {"type": "XCUIElementTypeApplication", "label": "SpringBoard", "children": list(children)}


DIGITS = [node(str(d), "Button") for d in (1, 2, 3, 4, 5, 6, 7, 8, 9, 0)]
# Synthetic fixtures in iOS 18-26 wording; the owner's `mobster devices lock-probe` confirms them (PLAN §6.1).
FIXTURES = {
    "lock_screen": tree(node("Monday, 5 October"), node("09:41"), node("Swipe up to open")),
    "keypad_6": tree(node("Enter Passcode"), *DIGITS, node("Emergency", "Button"), node("Cancel", "Button")),
    "keypad_4": tree(node("Enter iPhone Passcode"), *DIGITS, node("Delete", "Button")),
    "face_id": tree(node("Battery 80 percent"), node("Face ID", "Other")),
    "app_locked": tree(node("“Chase” is Locked"), node("Require Face ID")),
    "app_store": tree(node("Get"), node("Double Click to Install"), node("Cancel", "Button")),
    "home": tree(node("Messages", "Icon"), node("Safari", "Icon"), node("Settings", "Icon")),
    # A notification banner that happens to say "locked": not a locked app.
    "banner": tree(node("Chase, Your account is locked, now", "Other"), node("Your account is locked")),
    # A bank's own PIN pad lives in the bank's tree, not SpringBoard's: SpringBoard shows only its status bar.
    "bank_pin_pad": tree(node("Battery 80 percent"), node("Wi-Fi")),
}


def labels(name):
    return lockscreen.tree_labels(FIXTURES[name])


class FakeWda:

    def __init__(self, locked=False, front="com.apple.Preferences", source=None, locked_error=None,
                 source_error=None, hinted=None, delay=0.0):
        self.locked, self.front, self.source = locked, front, source
        self.locked_error, self.source_error, self.delay = locked_error, source_error, delay
        self.requests, self._hinted = [], hinted
        self.http = mock.Mock()
        self.http.request.side_effect = self._sessionless

    def _sessionless(self, method, path, body=None, timeout=None):
        self.requests.append((method, path, body))
        time.sleep(self.delay)
        if path == "/wda/locked":
            if self.locked_error:
                raise self.locked_error
            return {"value": self.locked}
        if path == "/wda/activeAppInfo":
            return {"value": {"bundleId": self.front}}
        raise AssertionError(path)

    def call(self, method, path, body=None, timeout=None):
        self.requests.append((method, path, body))
        if path == "/source?format=json":
            if self.source_error:
                raise self.source_error
            return self.source
        if path == "/appium/settings":
            return None
        raise AssertionError(f"the guard sent {method} {path}")

    def writes(self):
        return [r for r in self.requests if r[0] != "GET" and r[1] != "/appium/settings"]


class Classifier(unittest.TestCase):
    def test_each_fixture_reads_as_its_state(self):
        cases = {
            ("lock_screen", True, SB): "lock_screen", ("keypad_6", True, SB): "passcode_keypad",
            ("keypad_4", True, SB): "passcode_keypad", ("face_id", False, "com.chase"): "face_id_sheet",
            ("app_locked", False, SB): "app_locked_sheet", ("app_store", False, "com.apple.AppStore"):
                "apple_confirmation",
            ("home", False, SB): "unlocked", ("banner", False, "com.apple.MobileSMS"): "unlocked",
            ("bank_pin_pad", False, "com.chase"): "unlocked",
        }
        for (name, locked, front), expected in cases.items():
            with self.subTest(name):
                self.assertEqual(lockscreen.classify(locked, front, labels(name))[0], expected)

    def test_only_the_two_lock_screen_keypads_are_a_passcode_keypad(self):
        found = {name for name in FIXTURES for locked in (True, False) for front in (SB, "com.chase")
                 if lockscreen.classify(locked, front, labels(name))[0] == "passcode_keypad"}
        self.assertEqual(found, {"keypad_6", "keypad_4"})
        # Nine keys, or ten keys without the title, are not the lock screen's keypad.
        nine = tree(node("Enter Passcode"), *DIGITS[:9])
        untitled = tree(*DIGITS)
        for fixture in (nine, untitled):
            self.assertEqual(lockscreen.classify(True, SB, lockscreen.tree_labels(fixture))[0], "lock_screen")

    def test_a_keypad_over_an_unlocked_phone_is_never_the_lock_screen(self):
        state, _ = lockscreen.classify(False, SB, labels("keypad_6"))
        self.assertEqual(state, "app_locked_sheet")

    def test_an_unreadable_phone_is_unknown(self):
        self.assertEqual(lockscreen.classify(None)[0], "unknown")

    def test_every_stop_uses_a_frozen_code_and_names_no_digits(self):
        for state in lockscreen.STATES:
            for cause in ("preflight", "resume"):
                verdict = lockscreen.verdict_for(state, cause=cause, app="Chase")
                if verdict.state == "stop":
                    self.assertIn(verdict.code, STOP_CODES)
                    self.assertFalse(any(ch.isdigit() for ch in verdict.message), verdict.message)

class Guard(unittest.TestCase):
    def guard(self, **kwargs):
        return lockscreen.guard_for(device_id=kwargs.pop("device_id", None), wda_url="http://127.0.0.1:8100",
                                    mode=kwargs.pop("mode", "interactive"), usb=kwargs.pop("usb", False))

    def test_a_locked_phone_stops_at_preflight_in_one_read(self):
        phone = FakeWda(locked=True)
        started = time.monotonic()
        verdict = self.guard().check(phone, cause="preflight")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(verdict, GuardVerdict("stop", "phone_locked", lockscreen.PHONE_LOCKED))
        self.assertEqual(phone.requests, [("GET", "/wda/locked", None)])

    def test_an_unlocked_phone_is_ready_and_preflight_reads_no_tree(self):
        phone = FakeWda(locked=False)
        self.assertEqual(self.guard().check(phone, cause="preflight"), READY)
        self.assertEqual(phone.requests, [("GET", "/wda/locked", None)])

    def test_a_suspected_sheet_reads_springboard_and_puts_the_run_app_back(self):
        phone = FakeWda(front="com.apple.AppStore", source=FIXTURES["app_store"], hinted="com.apple.AppStore")
        verdict = self.guard().check(phone, cause="sheet_suspected")
        self.assertEqual((verdict.state, verdict.code), ("stop", "apple_confirmation"))
        settings = [r[2]["settings"]["defaultActiveApplication"] for r in phone.requests if r[1] == "/appium/settings"]
        self.assertEqual(settings, [SB, "com.apple.AppStore"])
        self.assertEqual(phone.writes(), [])

    def test_the_hint_is_restored_even_when_the_tree_read_fails(self):
        phone = FakeWda(source_error=TimeoutError(), hinted=None)
        self.assertEqual(self.guard().check(phone, cause="launch_failed"), READY)
        settings = [r[2]["settings"]["defaultActiveApplication"] for r in phone.requests if r[1] == "/appium/settings"]
        self.assertEqual(settings, [SB, "auto"])

    def test_face_id_names_the_app_in_front(self):
        phone = FakeWda(front="com.apple.MobileSMS", source=FIXTURES["face_id"])
        verdict = self.guard().check(phone, cause="lock_suspected")
        self.assertEqual(verdict.code, "face_id")
        self.assertTrue(verdict.message.startswith("Messages is asking for Face ID."), verdict.message)

    def test_the_guard_never_taps_types_or_presses_a_button(self):
        for name in FIXTURES:
            for locked in (True, False, None):
                for cause in ("preflight", "launch_failed", "lock_suspected", "resume", "sheet_suspected"):
                    phone = FakeWda(locked=locked, front=SB, source=FIXTURES[name])
                    guard = self.guard()
                    guard.check(phone, cause=cause)
                    guard.finish(phone)
                    self.assertEqual(phone.writes(), [], (name, locked, cause))

    def test_an_unreadable_phone_fails_closed(self):
        phone = FakeWda(locked_error=TimeoutError())
        verdict = self.guard().check(phone, cause="preflight")
        self.assertEqual((verdict.state, verdict.message), ("stop", lockscreen.NOT_RESPONDING))
        self.assertEqual(self.guard().check(phone, cause="resume").state, "stop")

    def test_an_unplugged_phone_says_so(self):
        phone = FakeWda(locked_error=ConnectionRefusedError())
        guard = lockscreen.DetectGuard(device_id=UDID, usb=True, attached_reader=lambda udid: False)
        self.assertEqual(guard.check(phone, cause="preflight"),
                         GuardVerdict("stop", "unplugged", lockscreen.UNPLUGGED))
        self.assertFalse(guard.attached())
        # A simulator or a bare WDA address is never "unplugged".
        self.assertIsNone(lockscreen.DetectGuard(device_id=UDID, usb=False,
                                                 attached_reader=lambda udid: False).attached())

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            self.guard(mode="anything")


class Attached(unittest.TestCase):
    def setUp(self):
        mac_tools._attached_cache.update(at=None, udids=None)
        self.addCleanup(mac_tools._attached_cache.update, at=None, udids=None)

    def test_a_pulled_cable_is_known_within_the_ttl_and_one_listing_serves_every_caller(self):
        listings = []

        def run(command, timeout=10):
            listings.append(command)
            return f"{UDID}\n" if len(listings) == 1 else ""
        with mock.patch.object(mac_tools, "tool", return_value="/bin/idevice_id"), \
                mock.patch.object(mac_tools, "run", side_effect=run):
            self.assertTrue(mac_tools.usb_attached(UDID, now=100.0))
            self.assertTrue(mac_tools.usb_attached(UDID, now=101.0))
            self.assertEqual(len(listings), 1)
            self.assertFalse(mac_tools.usb_attached(UDID, now=102.5))
        self.assertEqual(listings[0], ["/bin/idevice_id", "-l"])

class Preflight(unittest.TestCase):
    def test_the_server_preflight_raises_the_guard_sentence_and_presses_nothing(self):
        phone = FakeWda(locked=True)
        with self.assertRaises(PhoneNotReady) as raised:
            prepare_wda_phone(phone)
        self.assertEqual((str(raised.exception), raised.exception.code), (lockscreen.PHONE_LOCKED, "phone_locked"))
        self.assertEqual(phone.writes(), [])

    def test_a_guard_that_raises_stops_the_run(self):
        broken = mock.Mock()
        broken.check.side_effect = RuntimeError("boom")
        with self.assertRaises(PhoneNotReady):
            prepare_wda_phone(FakeWda(), broken)


class CliRun(unittest.TestCase):

    def test_the_json_result_names_the_stop_and_nothing_acts(self):
        from types import SimpleNamespace

        from mobile_agent import smart_run
        result = {"event": "result", "status": "error", "stop_code": "phone_locked", "reason": lockscreen.PHONE_LOCKED}

        class Session:
            def __init__(self, **_):
                self.runtime = SimpleNamespace()

            def start(self, app_id, goal):
                return SimpleNamespace(id="r1", status="error", summary=dict(result))

            def follow(self, run, on_event):
                on_event({"event": "run_started"})
                on_event(dict(result))

            def stop(self, run):
                pass

            def close(self):
                pass
        args = argparse.Namespace(goal="Open Settings", execute=True, json=True, spend_cap_usd=None, long=False,
                                  budget=None, yes=False, wda_url="http://127.0.0.1:8100", device=None, state_db=None)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test-0000"}), \
                mock.patch("mobile_agent.tui.session.Session", Session), \
                mock.patch.object(smart_run, "long_plan", return_value=None), \
                mock.patch.object(smart_run, "remember_only", return_value=None), redirect_stdout(out):
            code = smart_run.run_smart(args)
        line = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertEqual((line["status"], line["stop_code"], line["reason"]),
                         ("error", "phone_locked", lockscreen.PHONE_LOCKED))


class LockProbe(unittest.TestCase):
    def test_lock_probe_reports_what_the_guard_sees_and_taps_nothing(self):
        phone = FakeWda(locked=True, front=SB, source=FIXTURES["keypad_6"])
        phone.close = lambda: None
        out = io.StringIO()
        with mock.patch("mobile_agent.drivers.resolve_wda_session", return_value="S"), \
                mock.patch("mobile_agent.drivers.build_driver", return_value=phone), \
                mock.patch("mobile_agent.journal.Lease.device"), redirect_stdout(out):
            code = cli.main(["devices", "lock-probe", "--json", "--wda-url", "http://127.0.0.1:8100"])
        result = json.loads(out.getvalue())
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual((result["locked"], result["state"]), (True, "passcode_keypad"))
        self.assertIn({"type": "XCUIElementTypeStaticText", "label": "Enter Passcode"}, result["labels"])
        self.assertEqual(phone.writes(), [])

    def test_a_silent_runner_is_explained_by_coredevice_not_by_a_guess(self):
        asked = []

        def read(udid):
            return asked.append(udid) or answer[0]
        silent = FakeWda(locked_error=TimeoutError("timed out"))
        for answer, expected in (([True], ("lock_screen", True, True)), ([False], ("unknown", None, False)),
                                 ([None], ("unknown", None, None)), (["yes"], ("unknown", None, None))):
            with self.subTest(answer=answer), mock.patch.object(harness_api.hooks(), "coredevice_locked", read):
                result = lockscreen.probe(silent, udid="<UDID>")
                self.assertEqual((result["state"], result["locked"], result["coreDevice"]),
                                 (expected[0], expected[1], expected[2]))
        self.assertEqual(asked, ["<UDID>"] * 4)
        asked.clear()
        answer = [True]
        with mock.patch.object(harness_api.hooks(), "coredevice_locked", read):
            result = lockscreen.probe(FakeWda(locked=False, front="com.apple.Preferences"), udid="<UDID>")
            self.assertNotIn("coreDevice", result)
            self.assertEqual(lockscreen.probe(silent)["state"], "unknown")   # no UDID: not asked
        self.assertEqual(asked, [])
        self.assertIn("CoreDevice lock state: locked", lockscreen.describe(
            {"state": "lock_screen", "coreDevice": True, "labels": []}))

class ServerEntryPoints(Base):

    def setUp(self):
        super().setUp()
        session = mock.patch("mobile_agent.runtime_driver.resolve_wda_session", return_value="s")
        session.start()
        self.addCleanup(session.stop)

    def test_starting_a_task_on_a_locked_phone_says_it_is_locked(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        runtime = self.runtime()
        runtime.target_status = mock.Mock(return_value={**READY_TARGET, "ready": False,
                                                        "health": "Your iPhone is locked."})
        runtime.manager = mock.Mock(health={"locked": True})
        # The engine-health state is what names the lock (engine_health.cause_of), not the manager's probe alone.
        runtime.engine = mock.Mock(return_value={"issues": [{"source": "phone:primary", "cause": "locked"}]})
        with self.assertRaises(APIError) as refused:
            runtime.create(None, "What iOS version is this iPhone on?", "live")
        error = refused.exception
        self.assertEqual((str(error), error.status, error.code, error.details),
                         (lockscreen.PHONE_LOCKED, 503, "device_unavailable", {"stop": "phone_locked"}))
        # Not locked, just not ready: the old sentence, unchanged.
        runtime.manager = mock.Mock(health={"locked": False})
        runtime.engine = mock.Mock(return_value={"issues": []})
        with self.assertRaises(APIError) as refused:
            runtime.create(None, "What iOS version is this iPhone on?", "live")
        self.assertIn("not ready for live tasks", str(refused.exception))

    def test_a_run_on_a_phone_that_locked_since_stops_at_preflight_without_a_model_call(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        runtime = self.runtime()
        run = runtime.create("messages", "Text Sam hi", "live")
        phone = FakeWda(locked=True)
        phone.close = lambda: None
        client = mock.Mock()
        started = time.monotonic()
        with mock.patch("mobile_agent.runtime_driver.build_target_driver", return_value=phone), \
                mock.patch("mobile_agent.engines.build_client", return_value=client):
            self.worker.stop()
            try:
                runtime.work(run)
            finally:
                self.worker.start()
        if run.lease:
            run.lease.close()
            run.lease = None
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((run.summary["reason"], run.summary["stop_code"], run.summary["outcome"]),
                         (lockscreen.PHONE_LOCKED, "phone_locked", "couldnt_finish"))
        self.assertFalse(run.summary["actions_may_have_run"])
        self.assertFalse(run.summary["costUsd"])
        self.assertEqual([c for c in client.mock_calls if c[0] in ("complete", "create", "respond")], [])
        self.assertEqual(phone.writes(), [])

    def test_a_scheduled_run_gets_a_schedule_guard(self):
        runtime = self.runtime()
        seen = {}
        with mock.patch("mobile_agent.lockscreen.guard_for", side_effect=lambda **kw: seen.update(kw) or "g"):
            for key, mode in (("workflow:w1:scheduled:2026-10-05T09:00", "schedule"),
                              ("workflow:w1:manual:abc", "interactive"), (None, "interactive")):
                run = mock.Mock(idempotency_key=key)
                guard = runtime.make_guard(run, None)
                # Every run's guard also holds the user's app allowlist (permissions.PermissionGuard).
                self.assertEqual(guard.inner, "g")
                self.assertEqual(seen["mode"], mode)
        self.assertFalse(seen["usb"])


class AfterErrorTests(unittest.TestCase):

    def guard(self, attached, wifi_lost=False):
        guard = lockscreen.DetectGuard(device_id="00008130-001A2B3C4D5E6F70", usb=True,
                                       attached_reader=lambda _udid: attached)
        guard.attached = lambda: attached
        guard.wifi_lost = lambda: wifi_lost
        return guard

    def test_the_phone_names_the_cause_or_nothing(self):
        cases = [
            (self.guard(False), FakeWda(), ("unplugged", lockscreen.UNPLUGGED)),
            (self.guard(None, wifi_lost=True), FakeWda(), ("unplugged", lockscreen.WIFI_LOST)),
            (self.guard(True), FakeWda(locked=True), ("phone_locked", lockscreen.LOCKED_DURING)),
            (self.guard(True), FakeWda(locked=False), None),
            # Unreadable is no reason by itself: the step's own error stands.
            (self.guard(True), FakeWda(locked_error=TimeoutError("timed out")), None),
            (None, FakeWda(locked=True), None),
        ]
        for guard, phone, expected in cases:
            with self.subTest(expected=expected):
                verdict = lockscreen.after_error(guard, phone)
                self.assertEqual(verdict and (verdict.code, verdict.message), expected)
                self.assertTrue(all(method == "GET" for method, _, _ in phone.requests))  # nothing pressed


if __name__ == "__main__":
    unittest.main()

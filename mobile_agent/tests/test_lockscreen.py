"""The phone guard (lockscreen.py): a locked, unplugged or sheet-covered iPhone stops a task in code, before any
model call, from the Mac app, a schedule and `mobster run`, and the guard never taps or types anything.
Offline: synthetic SpringBoard trees and a fake WebDriverAgent; no phone, no model."""

import argparse
from contextlib import redirect_stdout
import io
import json
import os
import time
import unittest
from unittest import mock

from mobile_agent import __main__ as cli
from mobile_agent import device_manager, lockscreen
from mobile_agent.agent_hooks import READY, STOP_CODES, GuardVerdict
from mobile_agent.api_errors import PHONE_STOP_CODES
from mobile_agent.server import APIError, PhoneNotReady, prepare_wda_phone
from mobile_agent.tests.test_server_engine import READY as READY_TARGET, Base

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
    """WDA's answers for the guard; records every request so a test can prove nothing was tapped or typed."""

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
        """Every request that is not a read or the active-application setting: must stay empty."""
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

    def test_the_locked_app_names_itself(self):
        self.assertEqual(lockscreen.classify(False, SB, labels("app_locked")), ("app_locked_sheet", "Chase"))

    def test_an_unreadable_phone_is_unknown(self):
        self.assertEqual(lockscreen.classify(None)[0], "unknown")

    def test_every_stop_uses_a_frozen_code_and_names_no_digits(self):
        for state in lockscreen.STATES:
            for cause in ("preflight", "resume"):
                verdict = lockscreen.verdict_for(state, cause=cause, app="Chase")
                if verdict.state == "stop":
                    self.assertIn(verdict.code, STOP_CODES)
                    self.assertIn(verdict.code, PHONE_STOP_CODES)
                    self.assertFalse(any(ch.isdigit() for ch in verdict.message), verdict.message)
        self.assertEqual(lockscreen.verdict_for("lock_screen", cause="preflight").message, lockscreen.PHONE_LOCKED)
        self.assertEqual(lockscreen.verdict_for("face_id_sheet", cause="sheet_suspected", app="Chase").message,
                         "Chase is asking for Face ID. Mobster can't use Face ID: open Chase on your iPhone once, "
                         "then try again.")
        self.assertEqual(lockscreen.verdict_for("apple_confirmation", cause="sheet_suspected").message,
                         "The App Store wants you to confirm on your iPhone. Mobster never does that for you.")

    def test_the_locked_sentence_never_offers_an_unlock_this_build_does_not_have(self):
        self.assertFalse(lockscreen.CAN_UNLOCK)
        self.assertNotIn("Settings", lockscreen.PHONE_LOCKED)
        # The agent loop's own copies of the sentences (the keypad stop, the model's refusal) say the same.
        from mobile_agent import frontier
        self.assertNotIn("Settings", frontier.PHONE_LOCKED)
        self.assertNotIn("let Mobster unlock", frontier.PHONE_LOCKED)
        self.assertTrue(lockscreen.PHONE_LOCKED.startswith(frontier.PHONE_LOCKED))
        self.assertIn("Mobster can't unlock the phone", frontier.KEYPAD_REFUSAL)
        self.assertTrue(all(value is False for key, value in lockscreen.unlock_settings().items()
                            if key in ("enabled", "scripts", "supported")))


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

    def test_blocked_apps_come_from_the_env_file(self):
        with mock.patch.dict(os.environ, {"MOBSTER_BLOCKED_APPS": " com.chase , com.venmo,,"}):
            guard = self.guard()
        self.assertFalse(guard.allows_app("com.chase"))
        self.assertFalse(guard.allows_app("com.venmo"))
        self.assertTrue(guard.allows_app("com.apple.MobileSMS"))

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            self.guard(mode="anything")


class Attached(unittest.TestCase):
    def setUp(self):
        device_manager._attached_cache.update(at=None, udids=None)
        self.addCleanup(device_manager._attached_cache.update, at=None, udids=None)

    def test_a_pulled_cable_is_known_within_the_ttl_and_one_listing_serves_every_caller(self):
        listings = []

        def run(command, timeout=10):
            listings.append(command)
            return f"{UDID}\n" if len(listings) == 1 else ""
        with mock.patch.object(device_manager, "tool", return_value="/bin/idevice_id"), \
                mock.patch.object(device_manager, "run", side_effect=run):
            self.assertTrue(device_manager.usb_attached(UDID, now=100.0))
            self.assertTrue(device_manager.usb_attached(UDID, now=101.0))
            self.assertEqual(len(listings), 1)
            self.assertFalse(device_manager.usb_attached(UDID, now=102.5))
        self.assertEqual(listings[0], ["/bin/idevice_id", "-l"])

    def test_without_the_tools_it_cannot_tell(self):
        with mock.patch.object(device_manager, "tool", return_value=None):
            self.assertIsNone(device_manager.usb_attached(UDID, now=1.0))
        self.assertIsNone(device_manager.usb_attached("not-a-udid"))


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
    """`mobster run` had no lock check: a locked phone went to the model and failed on unreadable screens."""

    def args(self, **extra):
        return argparse.Namespace(goal="Open Settings", execute=True, helper=False, json=True, max_steps=3,
                                  max_seconds=5, spend_cap_usd=None, expected_text=None, allow_app=None,
                                  wda_url="http://127.0.0.1:8100", **extra)

    def test_a_locked_phone_stops_before_the_agent_runs(self):
        phone = FakeWda(locked=True)
        phone.frame_clock = phone.frame_clock_mode = None
        phone.close = lambda: None
        agent = mock.Mock()
        with mock.patch.dict(os.environ, {"HOME": os.environ.get("TMPDIR", "/tmp"), "MOBSTER_FRAME_CLOCK": "off"}), \
                mock.patch("mobile_agent.compose.build_models", return_value=(mock.Mock(), None)), \
                mock.patch("mobile_agent.compose.build_target_driver", return_value=phone), \
                mock.patch("mobile_agent.frame_clock.attach_frame_clock"), \
                mock.patch("mobile_agent.agent.Agent", agent), \
                mock.patch("mobile_agent.journal.Lease.device"), redirect_stdout(io.StringIO()):
            with self.assertRaises(cli.PhoneStopped) as raised:
                cli.run_task(self.args(), lease_key="k", target={"wda_url": "http://127.0.0.1:8100", "session": "S"})
        self.assertEqual((str(raised.exception), raised.exception.code), (lockscreen.PHONE_LOCKED, "phone_locked"))
        agent.assert_not_called()
        self.assertEqual(phone.writes(), [])

    def test_the_json_error_names_the_stop(self):
        out = io.StringIO()
        with mock.patch.object(cli, "run_task", side_effect=cli.PhoneStopped(lockscreen.PHONE_LOCKED, "phone_locked")), \
                mock.patch("mobile_agent.drivers.resolve_wda_session", return_value="S"), redirect_stdout(out):
            code = cli.main(["run", "Open Settings", "--execute", "--json", "--wda-url", "http://127.0.0.1:8100",
                             "--engine", "fast"])
        line = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertEqual((line["error_type"], line["stop_code"], line["error"]),
                         ("PhoneStopped", "phone_locked", lockscreen.PHONE_LOCKED))


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

    def test_device_options_belong_to_lock_probe(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            cli.main(["devices", "--wda-url", "http://127.0.0.1:8100"])


class ServerEntryPoints(Base):
    """The Mac app and schedules: a locked phone is named as locked, never "not ready", and a run stops at
    preflight with no model call and $0."""

    def setUp(self):
        super().setUp()
        session = mock.patch("mobile_agent.server.resolve_wda_session", return_value="s")
        session.start()
        self.addCleanup(session.stop)

    def test_starting_a_task_on_a_locked_phone_says_it_is_locked(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        runtime = self.runtime()
        runtime.target_status = mock.Mock(return_value={**READY_TARGET, "ready": False,
                                                        "health": "Your iPhone is locked."})
        runtime.manager = mock.Mock(health={"locked": True})
        with self.assertRaises(APIError) as refused:
            runtime.create(None, "What iOS version is this iPhone on?", "live")
        error = refused.exception
        self.assertEqual((str(error), error.status, error.code, error.details),
                         (lockscreen.PHONE_LOCKED, 503, "device_unavailable", {"stop": "phone_locked"}))
        # Not locked, just not ready: the old sentence, unchanged.
        runtime.manager = mock.Mock(health={"locked": False})
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
        with mock.patch("mobile_agent.server.build_target_driver", return_value=phone), \
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
                self.assertEqual(runtime.make_guard(run, None), "g")
                self.assertEqual(seen["mode"], mode)
        self.assertFalse(seen["usb"])

    def test_the_smart_loop_gets_the_guard_when_it_takes_one(self):
        from mobile_agent.server import frontier_guard
        self.assertEqual(frontier_guard(None), {})

        def with_guard(driver, client, *, guard=None):
            pass

        def without_guard(driver, client):
            pass
        with mock.patch("mobile_agent.engines.build_frontier", with_guard):
            self.assertEqual(frontier_guard("g"), {"guard": "g"})
        with mock.patch("mobile_agent.engines.build_frontier", without_guard):
            self.assertEqual(frontier_guard("g"), {})


if __name__ == "__main__":
    unittest.main()

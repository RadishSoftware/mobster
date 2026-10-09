"""Before a task: a locked or wedged phone is refused plainly, overlays are cleared. Before setup: the
preflight screen reads Xcode, the cable, trust and Developer Mode at once. Offline."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from mobile_agent.server import PhoneNotReady, prepare_wda_phone
from mobile_agent.setup_service import SetupService


def driver(locked=False, locked_error=None, home_error=None):
    d = Mock()
    d.http.request.side_effect = locked_error or (lambda *a: {"value": locked})
    d.call.side_effect = home_error
    return d


class PreflightTests(unittest.TestCase):
    def test_an_unlocked_phone_goes_home_first(self):
        d = driver()
        prepare_wda_phone(d)
        d.call.assert_called_once_with("POST", "/wda/pressButton", {"name": "home"}, 4)

    def test_a_locked_phone_is_refused_without_any_action(self):
        d = driver(locked=True)
        with self.assertRaisesRegex(PhoneNotReady, "locked"):
            prepare_wda_phone(d)
        d.call.assert_not_called()

    def test_a_wedged_phone_is_named_instead_of_a_transport_error(self):
        d = driver(locked_error=TimeoutError())
        with self.assertRaisesRegex(PhoneNotReady, "isn't responding.*No action"):
            prepare_wda_phone(d)
        d.call.assert_not_called()
        with self.assertRaisesRegex(PhoneNotReady, "Home Screen"):
            prepare_wda_phone(driver(home_error=TimeoutError()))


def xcode(state="ok"):
    return {"state": state, "version": "Xcode 26.4" if state == "ok" else None, "app": None, "selected": None,
            "fixes": [], "developer_dir": None}


def setup_service(*, state="ok", devices=(), device=None, usb=None, developer_mode=None, delay=0.0, usb_tools=True):
    """A setup service over a fake manager; each slow read sleeps `delay` and records its thread."""
    threads = set()

    def slow(value):
        def read(*args):
            threads.add(threading.current_thread().name)
            time.sleep(delay)
            return value
        return read
    paths = {name: "/bin/x" if usb_tools else None for name in ("iproxy", "idevice_id", "ideviceinfo")}
    manager = SimpleNamespace(
        tools=slow({**paths, "xcodebuild": "/bin/x", "git": "/bin/x", "xcode": xcode(state)}),
        devices=slow(list(devices)), usb_iphones=slow(usb), device=lambda listed=None: device,
        settings=lambda: {}, built=lambda: False, teams=lambda: [], build={"state": "idle", "error": None, "log_tail": []},
        runner_state=lambda: {"state": "stopped", "error": None}, expired=lambda: False, signature_expiry=lambda: None)
    runtime = SimpleNamespace(config=SimpleNamespace(enable_live=False), video=SimpleNamespace(status=lambda: {}))
    service = SetupService(runtime, manager, Path(tempfile.mkdtemp()) / "agent.env")
    service.developer_mode = lambda phone: developer_mode
    return service, threads


def words(state):
    return {check["id"]: (check["state"], check["word"]) for check in state["preflight"]}


class SetupPreflightTests(unittest.TestCase):
    def test_a_fresh_mac_with_nothing_plugged_in(self):
        service, _ = setup_service(state="missing", usb_tools=False, usb=0)
        self.assertEqual(words(service.state()), {
            "xcode": ("todo", "Not installed"), "cable": ("todo", "Not connected"),
            "trust": ("waiting", "Waiting"), "developer_mode": ("waiting", "Waiting")})

    def test_the_cable_is_seen_before_the_iphone_tools_are_installed(self):
        service, _ = setup_service(usb_tools=False, usb=1)
        checks = words(service.state())
        self.assertEqual(checks["cable"], ("ok", "Connected"))
        self.assertEqual(checks["trust"], ("unknown", "Can't tell yet"))
        service, _ = setup_service(usb_tools=False, usb=None)
        self.assertEqual(words(service.state())["cable"], ("unknown", "Can't tell yet"))

    def test_trust_and_developer_mode_follow_the_phone(self):
        untrusted = {"udid": "u", "name": "iPhone", "trusted": False}
        service, _ = setup_service(devices=[untrusted], device=untrusted)
        self.assertEqual(words(service.state())["trust"], ("todo", "Tap Trust"))
        phone = {"udid": "u", "name": "Test iPhone", "trusted": True}
        for mode, expected in ((True, ("ok", "On")), (False, ("todo", "Off")), (None, ("unknown", "After the first build"))):
            service, _ = setup_service(devices=[phone], device=phone, developer_mode=mode)
            state = service.state()
            self.assertEqual(words(state)["developer_mode"], expected)
            self.assertEqual(words(state)["trust"], ("ok", "Trusted"))
            self.assertIn("Test iPhone", state["preflight"][1]["detail"])

    def test_each_xcode_problem_reads_as_one_word_and_one_line(self):
        for problem in ("not_selected", "license", "first_launch", "no_ios", "broken"):
            service, _ = setup_service(state=problem)
            check = service.state()["preflight"][0]
            self.assertEqual(check["state"], "todo")
            self.assertTrue(check["word"] and "\n" not in check["detail"] and check["detail"].endswith("."))

    def test_xcode_and_the_phones_are_read_at_the_same_time(self):
        phone = {"udid": "u", "name": "Test iPhone", "trusted": True}
        service, threads = setup_service(state="ok", devices=[phone], device=phone, usb=1, delay=0.3)
        started = time.monotonic()
        service.state()
        # Two 0.3 s reads one after another would take 0.6 s.
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(len(threads), 2)

    def test_system_information_is_read_only_when_libimobiledevice_sees_no_phone(self):
        calls = []
        phone = {"udid": "u", "name": "Test iPhone", "trusted": True}
        service, _ = setup_service(devices=[phone], device=phone)
        service.manager.usb_iphones = lambda: calls.append(1) or 1
        service.state()
        self.assertEqual(calls, [])                      # libimobiledevice already sees the phone
        service, _ = setup_service(devices=[])
        service.manager.usb_iphones = lambda: calls.append(1) or 1
        self.assertEqual(words(service.state())["cable"], ("ok", "Connected"))
        service, _ = setup_service(usb_tools=False)
        service.manager.usb_iphones = lambda: calls.append(1) or 0
        service.state()
        self.assertEqual(len(calls), 2)                  # nothing listed, and no libimobiledevice


if __name__ == "__main__":
    unittest.main()

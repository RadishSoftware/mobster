import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from mobile_agent.setup_service import SetupService


def xcode(state="ok"):
    return {"state": state, "version": "Xcode 26.4" if state == "ok" else None, "app": None, "selected": None,
            "fixes": [], "developer_dir": None}


def setup_service(*, state="ok", devices=(), device=None, usb=None, developer_mode=None, delay=0.0, usb_tools=True):
    def slow(value):
        def read(*args):
            time.sleep(delay)
            return value
        return read
    paths = {name: "/bin/x" if usb_tools else None for name in ("iproxy", "idevice_id", "ideviceinfo")}
    manager = SimpleNamespace(
        tools=slow({**paths, "xcodebuild": "/bin/x", "git": "/bin/x", "xcode": xcode(state)}),
        devices=slow(list(devices)), usb_iphones=slow(usb), device=lambda listed=None: device,
        settings=lambda: {}, built=lambda: False, teams=lambda: [],
        build={"state": "idle", "error": None, "log_tail": []},
        runner_state=lambda: {"state": "stopped", "error": None}, expired=lambda: False, signature_expiry=lambda: None)
    runtime = SimpleNamespace(config=SimpleNamespace(enable_live=False), video=SimpleNamespace(status=lambda: {}))
    service = SetupService(runtime, manager, Path(tempfile.mkdtemp()) / "agent.env")
    service.developer_mode = lambda phone: developer_mode
    return service


def words(state):
    return {check["id"]: (check["state"], check["word"]) for check in state["preflight"]}


class SetupPreflightTests(unittest.TestCase):
    def test_a_fresh_mac_with_nothing_plugged_in(self):
        service = setup_service(state="missing", usb_tools=False, usb=0)
        self.assertEqual(words(service.state()), {
            "xcode": ("todo", "Not installed"), "cable": ("todo", "Not connected"),
            "trust": ("waiting", "Waiting"), "developer_mode": ("waiting", "Waiting")})

    def test_the_cable_is_seen_before_the_iphone_tools_are_installed(self):
        checks = words(setup_service(usb_tools=False, usb=1).state())
        self.assertEqual(checks["cable"], ("ok", "Connected"))
        self.assertEqual(checks["trust"], ("unknown", "Can't tell yet"))
        self.assertEqual(words(setup_service(usb_tools=False, usb=None).state())["cable"],
                         ("unknown", "Can't tell yet"))

    def test_trust_and_developer_mode_follow_the_phone(self):
        untrusted = {"udid": "u", "name": "iPhone", "trusted": False}
        self.assertEqual(words(setup_service(devices=[untrusted], device=untrusted).state())["trust"],
                         ("todo", "Tap Trust"))
        phone = {"udid": "u", "name": "Test iPhone", "trusted": True}
        for mode, expected in ((True, ("ok", "On")), (False, ("todo", "Off")),
                               (None, ("unknown", "After the first build"))):
            state = setup_service(devices=[phone], device=phone, developer_mode=mode).state()
            self.assertEqual(words(state)["developer_mode"], expected)
            self.assertEqual(words(state)["trust"], ("ok", "Trusted"))
            self.assertIn("Test iPhone", state["preflight"][1]["detail"])

    def test_each_xcode_problem_reads_as_one_word_and_one_line(self):
        for problem in ("license", "first_launch", "no_ios", "broken"):
            check = setup_service(state=problem).state()["preflight"][0]
            self.assertEqual(check["state"], "todo")
            self.assertTrue(check["word"] and "\n" not in check["detail"] and check["detail"].endswith("."))

    def test_xcode_and_the_phones_are_read_at_the_same_time(self):
        phone = {"udid": "u", "name": "Test iPhone", "trusted": True}
        service = setup_service(state="ok", devices=[phone], device=phone, usb=1, delay=0.3)
        started = time.monotonic()
        service.state()
        self.assertLess(time.monotonic() - started, 0.5)

    def test_system_information_is_read_only_when_libimobiledevice_sees_no_phone(self):
        calls = []
        phone = {"udid": "u", "name": "Test iPhone", "trusted": True}
        service = setup_service(devices=[phone], device=phone)
        service.manager.usb_iphones = lambda: calls.append(1) or 1
        service.state()
        self.assertEqual(calls, [])
        service = setup_service(devices=[])
        service.manager.usb_iphones = lambda: calls.append(1) or 1
        self.assertEqual(words(service.state())["cable"], ("ok", "Connected"))
        service = setup_service(usb_tools=False)
        service.manager.usb_iphones = lambda: calls.append(1) or 0
        service.state()
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

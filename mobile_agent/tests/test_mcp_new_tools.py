"""The MCP tools for phone I/O, codes and unlock: use_code (the code never reaches the agent: not in a result, a log
line, a step or a screenshot), read_notifications, set_clipboard (and no tool that reads the clipboard),
install_app, unlock_status (booleans only) and unlock (only with the phone's scripts setting), and the phone guard
at a device session's start (a locked phone answers with the phone_locked sentence at once). Fakes only."""

import base64
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from mobile_agent.agent_hooks import GuardVerdict
from mobile_agent.mcp_server import direct
from mobile_agent.mcp_server.tools import inline_jpeg
from mobile_agent.phone_io import PhoneIOError
from mobile_agent.skills import codes, notifications
from mobile_agent.state import Element, Snapshot
from mobile_agent.tests.test_mcp_devices import PHONE, SIM, FakeDeviceRun, records
from mobile_agent.tests.test_mcp_tools import Node, Tree, ToolsHarness
from mobile_agent.tests.test_skill_codes import CANARY, FIXTURES, FakeClock, FakePhone

LOCKED = "Your iPhone is locked. Unlock it and try again. No action was taken."


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class Screens:
    """One fixture as both reads MCP makes: the evidence tree (points) and the fast snapshot (fractions), sharing
    paths, with the values typed so far."""

    def __init__(self, name, values=None):
        self.name, self.data, self.values = name, fixture(name), values if values is not None else {}

    def path(self, index):
        return f"/app/{self.name}/{index}"

    def tree(self):
        width, height = self.data["width"], self.data["height"]
        nodes = []
        for index, item in enumerate(self.data["elements"]):
            x, y, w, h = item["rect"]
            nodes.append(Node(self.path(index), item["role"], label=item["label"],
                              value=self.values.get(self.path(index), item.get("value", "")),
                              placeholder=item.get("placeholder", ""), rect=(x * width, y * height, w * width,
                                                                              h * height)))
        return Tree(nodes, bundle_id=self.data["bundle"], size=(width, height))

    def snapshot(self, keyboard=""):
        elements = []
        for index, item in enumerate(self.data["elements"]):
            editable = bool(item.get("editable"))
            elements.append(Element(str(index), item["label"], item["role"], tuple(item["rect"]), editable=editable,
                                    locator=self.path(index), value=self.values.get(self.path(index), ""),
                                    actions=("TAP", "TYPE", "TYPE_SUBMIT") if editable else ("TAP",),
                                    placeholder=item.get("placeholder", "")))
        return Snapshot(elements, "\n".join(e.label for e in elements if e.label), self.data["width"],
                        self.data["height"], "wda", bundle_id=self.data["bundle"], keyboard=keyboard)


class MCPPhone(FakePhone):
    """FakePhone with what ToolSet also asks of a driver; typed text shows in the focused field."""

    def __init__(self, clock, nc="notification_center"):
        self.apps = {"com.chase.sig": Screens("signin"), "com.apple.MobileSMS": Screens("messages_list")}
        super().__init__("com.chase.sig", {}, clock=clock, nc=nc)
        self.nc_screens = Screens(nc)
        self.focused_path = None

    @property
    def screens(self):
        return {bundle: s.snapshot("visible" if self.focused else "") for bundle, s in self.apps.items()}

    @screens.setter
    def screens(self, value):
        pass

    def tree(self):
        if self.nc_open and self.hint == codes.SPRINGBOARD:
            return self.nc_screens.tree()
        return self.apps[self.front].tree()

    def observe(self, timeout=10):
        snapshot = super().observe(timeout)
        return snapshot

    def tap_point(self, x, y, snapshot, timeout=10):
        super().tap_point(x, y, snapshot, timeout)
        for element in getattr(snapshot, "elements", ()):
            ex, ey, ew, eh = element.rect
            if ex <= x <= ex + ew and ey <= y <= ey + eh and element.editable:
                self.focused_path = element.locator

    def call(self, method, path, body=None, timeout=10):
        result = super().call(method, path, body, timeout)
        if path == "/wda/keys" and self.focused_path:
            self.apps[self.front].values[self.focused_path] = "".join(body["value"])
        return result

    def wait_for_change(self, snapshot, timeout=2, wait_seconds=.6, **options):
        return self.observe()

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.calls.append(("execute", operation, getattr(target, "locator", None)))
        return {"dispatch_attempted": True}

    def observe_ready(self, timeout=10):
        return self.observe()

    def capture_preview(self, timeout=3):
        raise OSError("no preview")


def white_jpeg(path, size=(402, 874)):
    from PIL import Image
    Image.new("RGB", size, (255, 255, 255)).save(path, "JPEG")
    return Path(path)


class PhoneRun(FakeDeviceRun):
    """A device session on MCPPhone: its tree follows the phone, its frames and screenshots are real JPEGs."""

    def __init__(self, record, *, runs_dir, **options):
        super().__init__(record, runs_dir=runs_dir, **options)
        self.clock = FakeClock()
        self.driver = MCPPhone(self.clock)
        self.manager = SimpleNamespace(screenshot=lambda target, path, **kw: white_jpeg(path))
        self.frame_paths = []

    def read_tree(self):
        self.reads += 1
        return self.driver.tree()

    def frame(self, label):
        path = self.run_dir / "frames" / f"{len(self.frames) + 2:02d}-{label}.jpg"
        white_jpeg(path)
        self.frames.append(label)
        self.frame_paths.append(path)
        return path


class NewToolsHarness(ToolsHarness):
    def make(self, **options):
        FakeDeviceRun.opened = []
        options.setdefault("devices", records)
        options.setdefault("device_run", PhoneRun)
        tools = super().make(**options)
        clock = FakeClock()
        for module in (codes, notifications):
            patcher = mock.patch.object(module, "CLOCK", clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        return tools

    def everything_said(self, *results):
        said = [result.text + json.dumps(result.structured, ensure_ascii=False) for result in results]
        return "\n".join(said + [str(line) for line in self.logged])


class UseCodeToolTests(NewToolsHarness):
    def test_the_code_is_typed_and_never_reaches_the_agent(self):
        self.make()
        self.tools.imager = inline_jpeg
        first = self.call("screen", device="Work iPhone", image=False)
        result = self.call("use_code", device="Work iPhone", target={"label": "Verification code"})
        self.assertFalse(result.is_error, result.text)
        run = FakeDeviceRun.opened[-1]
        self.assertEqual(run.driver.typed, [CANARY])
        self.assertEqual({k: result.structured[k] for k in ("entered", "source", "sender", "age_s")},
                         {"entered": True, "source": "notification", "sender": "Chase", "age_s": 120})
        self.assertIn("Entered the code from Chase", result.text)
        # The field now holds the code on the phone; every later look at the screen shows it masked.
        self.assertEqual(run.driver.apps["com.chase.sig"].values["/app/signin/2"], CANARY)
        later = self.call("screen", device="Work iPhone")
        self.assertIn("••••", later.text)
        step = run.steps[-1]
        self.assertEqual((step["op"], step["typed"]), ("USE_CODE", "••••"))
        self.assertNotIn(CANARY, self.everything_said(first, result, later) + json.dumps(run.steps))
        # The screenshot and the step's frame have the field blacked out.
        from PIL import Image
        shot = Image.open(io.BytesIO(base64.b64decode(later.images[0])))
        x, y = shot.width * (.06 + .44), shot.height * (.26 + .03)
        self.assertLess(sum(shot.getpixel((round(x), round(y)))), 60)
        self.assertGreater(sum(shot.getpixel((5, 5))), 700)
        frame = Image.open(run.frame_paths[-1])
        self.assertLess(sum(frame.getpixel((round(frame.width * .5), round(frame.height * .29)))), 60)

    def test_refusals_are_tool_errors_that_type_nothing(self):
        self.make()
        result = self.call("use_code", device="Work iPhone", target={"label": "Verify"})
        self.assertTrue(result.is_error)
        self.assertIn("isn't a text field", result.text)
        run = FakeDeviceRun.opened[-1]
        run.driver.nc_screens = Screens("nc_two_codes")
        run.driver.nc = run.driver.nc_screens.snapshot()
        result = self.call("use_code", device="Work iPhone", target={"label": "Verification code"})
        self.assertTrue(result.is_error)
        self.assertIn("Two different codes", result.text)
        self.assertEqual(run.driver.typed, [])
        result = self.call("use_code", device="Work iPhone", target={"label": "Verification code"}, sender="Google")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(run.driver.typed, ["731906"])
        self.assertNotIn("731906", self.everything_said(result))


class OnePhoneRun(PhoneRun):
    """PhoneRun where every session on the device drives the same phone: what was typed stays in its field when a
    session is stopped (or idles out) and the next call opens another."""

    phone = None

    def __init__(self, record, *, runs_dir, **options):
        super().__init__(record, runs_dir=runs_dir, **options)
        if OnePhoneRun.phone is None:
            OnePhoneRun.phone = self.driver
        self.driver = OnePhoneRun.phone


class CodeMemoryTests(NewToolsHarness):
    def setUp(self):
        super().setUp()
        OnePhoneRun.phone = None

    def screen_text(self, result):
        return result.text + json.dumps(result.structured, ensure_ascii=False)

    def test_a_code_stays_masked_in_a_session_opened_after_stop(self):
        self.make(device_run=OnePhoneRun)
        self.tools.imager = inline_jpeg
        self.assertFalse(self.call("use_code", device="Work iPhone", target={"label": "Verification code"}).is_error)
        self.assertFalse(self.call("stop", device="Work iPhone").is_error)
        later = self.call("screen", device="Work iPhone")
        self.assertEqual(len(FakeDeviceRun.opened), 2)  # a new session, on the same phone
        self.assertEqual(OnePhoneRun.phone.apps["com.chase.sig"].values["/app/signin/2"], CANARY)
        self.assertIn("••••", later.text)
        self.assertNotIn(CANARY, self.screen_text(later))
        # The new session's screenshot has the field blacked out too.
        from PIL import Image
        shot = Image.open(io.BytesIO(base64.b64decode(later.images[0])))
        self.assertLess(sum(shot.getpixel((round(shot.width * .5), round(shot.height * .29)))), 60)

    def test_a_typing_error_names_only_its_kind_and_the_code_is_masked_after(self):
        self.make(device_run=OnePhoneRun)
        self.call("screen", device="Work iPhone", image=False)
        phone = OnePhoneRun.phone
        original = phone.call

        def call(method, path, body=None, timeout=10):
            result = original(method, path, body, timeout)
            if path == "/wda/keys":  # the keys landed, then the answer timed out
                raise TimeoutError(f"no answer to POST {path} {json.dumps(body)}")
            return result
        phone.call = call
        result = self.call("use_code", device="Work iPhone", target={"label": "Verification code"})
        self.assertTrue(result.is_error)
        self.assertEqual(result.text, "Mobster couldn't finish entering the code (TimeoutError). Call screen to see "
                                      "the field before trying again.")
        self.assertEqual(phone.apps["com.chase.sig"].values["/app/signin/2"], CANARY)
        later = self.call("screen", device="Work iPhone", image=False)
        self.assertIn("••••", later.text)
        self.assertNotIn(CANARY, self.everything_said(result, later))

    def test_the_next_steps_frame_is_blacked_out_although_the_code_left_the_outline(self):
        # The after frame is taken while the outline is read: a frame from before the screen moved on can still
        # show the code the outline no longer has.
        self.make()
        self.assertFalse(self.call("use_code", device="Work iPhone", target={"label": "Verification code"}).is_error)
        run = FakeDeviceRun.opened[-1]
        run.driver.apps["com.chase.sig"].values.clear()  # the app took the code and cleared the field
        result = self.call("tap", device="Work iPhone", target={"label": "Resend code"}, image=False)
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(self.tools.device_sessions[PHONE].secret_rects, [])
        from PIL import Image
        frame = Image.open(run.frame_paths[-1])
        self.assertLess(sum(frame.getpixel((round(frame.width * .5), round(frame.height * .29)))), 60)
        self.assertGreater(sum(frame.getpixel((5, 5))), 700)


class PhoneIOToolTests(NewToolsHarness):
    def test_read_notifications_masks_codes_and_returns_to_the_app(self):
        self.make()
        result = self.call("read_notifications", device="Work iPhone")
        self.assertFalse(result.is_error, result.text)
        self.assertIn("•••••• (use use_code)", result.text)
        self.assertEqual(len(result.structured["notifications"]), 3)
        self.assertNotIn(CANARY, self.everything_said(result))
        phone = FakeDeviceRun.opened[-1].driver
        self.assertEqual((phone.front, phone.nc_open, phone.hint), ("com.chase.sig", False, "com.chase.sig"))
        self.assertEqual(phone.taps, [])

    def test_set_clipboard_and_no_tool_reads_it(self):
        self.make()
        names = [tool["name"] for tool in self.tools.list_tools()]
        self.assertIn("set_clipboard", names)
        self.assertFalse([name for name in names if "clipboard" in name and name != "set_clipboard"])
        result = self.call("set_clipboard", device="Work iPhone", text="https://example.com/invite/42")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(result.structured["characters"], 29)
        phone = FakeDeviceRun.opened[-1].driver
        sent = [c for c in phone.calls if isinstance(c, tuple) and c[1] == "/wda/setPasteboard"]
        self.assertEqual(sent[0][2], {"content": base64.b64encode(b"https://example.com/invite/42").decode(),
                                      "contentType": "plaintext"})
        self.assertFalse([line for line in self.logged if "example.com/invite" in str(line)])
        result = self.call("set_clipboard", device="Work iPhone", text="é" * 40000)
        self.assertTrue(result.is_error)
        self.assertIn("64 KB", result.text)

    def test_install_app_takes_an_absolute_local_path(self):
        calls = []

        def installer(record, path, progress=None):
            calls.append((record["id"], path))
            if path.endswith("Unsigned.ipa"):
                raise PhoneIOError("The build isn't signed for this iPhone.", "not_signed", "Sign it for this iPhone.")
            return {"ok": True, "device": record["id"], "name": record["name"], "path": path,
                    "bundle_id": "com.example.daybreak", "seconds": 12.5}
        self.make(installer=installer)
        result = self.call("install_app", path="/tmp/Daybreak.ipa", device="Work iPhone")
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(result.text, "Installed com.example.daybreak on Work iPhone in 12.5 s. Open it with launch_app.")
        self.assertEqual(calls, [(PHONE, "/tmp/Daybreak.ipa")])
        result = self.call("install_app", path="build/Daybreak.ipa", device="Work iPhone")
        self.assertTrue(result.is_error)
        self.assertIn("absolute path", result.text)
        result = self.call("install_app", path="/tmp/Unsigned.ipa", device="Work iPhone")
        self.assertEqual(result.text, "The build isn't signed for this iPhone. Sign it for this iPhone.")


class FakeGuard:
    def __init__(self, *verdicts):
        self.verdicts, self.checks, self.finished = list(verdicts), [], 0

    def check(self, driver, *, cause):
        self.checks.append(cause)
        return self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]

    def attached(self):
        return True

    def allows_app(self, bundle_id):
        return True

    def finish(self, driver):
        self.finished += 1


class Lease:
    def __init__(self):
        self.target, self.released = SimpleNamespace(udid=PHONE), 0

    def release(self):
        self.released += 1


class TreeRun(direct.DeviceRun):
    """direct.DeviceRun, reading the fake phone's tree instead of WebDriverAgent's XML."""

    def read_tree(self):
        return self.driver.tree()


def device_runs(guard, leases):
    """direct.DeviceRun on a fake lease, driver and guard."""
    def factory(record, *, runs_dir):
        def acquire():
            leases.append(Lease())
            return leases[-1]
        return TreeRun(record, runs_dir=runs_dir, manager=SimpleNamespace(acquire=acquire),
                       driver_factory=lambda target: MCPPhone(FakeClock()), guard_factory=lambda record, emit: guard)
    return factory


class GuardTests(NewToolsHarness):
    def test_a_locked_phone_answers_with_the_locked_sentence_at_once(self):
        guard, leases = FakeGuard(GuardVerdict("stop", "phone_locked", LOCKED)), []
        self.make(device_run=device_runs(guard, leases))
        started = time.monotonic()
        result = self.call("screen", device="Work iPhone")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(result.is_error)
        self.assertEqual(result.text, LOCKED)
        self.assertEqual(guard.checks, ["preflight"])
        self.assertEqual([lease.released for lease in leases], [1])  # the device is free again
        self.assertEqual(guard.finished, 1)

    def test_a_ready_phone_opens_and_the_guard_finishes_at_stop(self):
        guard, leases = FakeGuard(GuardVerdict("ready")), []
        self.make(device_run=device_runs(guard, leases))
        self.assertFalse(self.call("screen", device="Work iPhone", image=False).is_error)
        self.assertEqual(guard.finished, 0)
        self.assertFalse(self.call("stop", device="Work iPhone").is_error)
        self.assertEqual(guard.finished, 1)
        self.assertEqual(leases[0].released, 1)

    def test_a_guard_that_fails_stops_the_session(self):
        class Broken(FakeGuard):
            def check(self, driver, *, cause):
                raise RuntimeError("SpringBoard didn't answer")
        guard, leases = Broken(GuardVerdict("ready")), []
        self.make(device_run=device_runs(guard, leases))
        result = self.call("screen", device="Work iPhone")
        self.assertTrue(result.is_error)
        self.assertEqual(result.text, "Mobster couldn't check whether your iPhone is unlocked, so it stopped.")
        self.assertEqual(leases[0].released, 1)

    def test_a_guard_that_answers_no_verdict_stops_the_session(self):
        # Not a verdict (None, a dict, a misspelt state): never taken as "ready".
        for answer in (None, {"state": "ready"}, "ready", GuardVerdict("readyy")):
            with self.subTest(answer=answer):
                guard, leases = FakeGuard(answer), []
                self.make(device_run=device_runs(guard, leases))
                result = self.call("screen", device="Work iPhone")
                self.assertTrue(result.is_error)
                self.assertEqual(result.text, "Mobster couldn't check whether your iPhone is unlocked, so it stopped.")
                self.assertEqual(leases[0].released, 1)

    def test_no_guard_for_a_simulator_or_a_build_without_lockscreen(self):
        self.assertIsNone(direct.default_guard({"id": "S", "kind": "simulator"}))
        with mock.patch.dict("sys.modules", {"mobile_agent.lockscreen": None}):
            self.assertIsNone(direct.default_guard({"id": "P", "kind": "usb"}))


class UnlockTests(NewToolsHarness):
    def settings(self, **values):
        base = {"enabled": True, "askBeforeUnlocking": True, "scripts": False, "relockAfter": True, "needsCheck": False}
        return mock.patch.object(direct, "unlock_settings", return_value={**base, **values})

    def test_unlock_without_the_scripts_setting_is_refused_with_the_settings_path(self):
        guard, leases = FakeGuard(GuardVerdict("unlocked")), []
        self.make(device_run=device_runs(guard, leases))
        with self.settings(scripts=False), mock.patch.object(direct, "lockscreen_available", return_value=True):
            result = self.call("unlock", device="Work iPhone")
        self.assertTrue(result.is_error)
        self.assertIn("Settings › iPhones › Work iPhone › Unlock with passcode › Scripts and MCP may unlock", result.text)
        self.assertEqual((guard.checks, leases), ([], []))
        with self.settings(enabled=False, scripts=True), mock.patch.object(direct, "lockscreen_available",
                                                                            return_value=True):
            self.assertTrue(self.call("unlock", device="Work iPhone").is_error)
        with self.settings(scripts=True, needsCheck=True), mock.patch.object(direct, "lockscreen_available",
                                                                              return_value=True):
            result = self.call("unlock", device="Work iPhone")
        self.assertIn("won't try it again", result.text)
        self.assertEqual(guard.checks, [])

    def test_unlock_with_the_setting_runs_the_guard_once(self):
        guard, leases = FakeGuard(GuardVerdict("unlocked"), GuardVerdict("ready")), []
        self.make(device_run=device_runs(guard, leases))
        with self.settings(scripts=True), mock.patch.object(direct, "lockscreen_available", return_value=True):
            result = self.call("unlock", device="Work iPhone")
            self.assertFalse(result.is_error, result.text)
            self.assertEqual(result.text, "Mobster unlocked “Work iPhone”.")
            self.assertTrue(result.structured["unlocked"])
            again = self.call("unlock", device="Work iPhone")
        self.assertEqual(again.text, "“Work iPhone” is unlocked.")
        self.assertEqual(guard.checks, ["preflight", "resume"])

    def test_unlock_status_is_booleans_only(self):
        self.make(wda_get=lambda url, timeout: True)
        with self.settings(scripts=True), mock.patch.object(direct, "lockscreen_available", return_value=True), \
                mock.patch.object(direct, "passcode_saved", return_value=True):
            result = self.call("unlock_status", device="Work iPhone")
        self.assertFalse(result.is_error, result.text)
        values = {k: v for k, v in result.structured.items() if k != "device"}
        self.assertEqual(values, {"supported": True, "enabled": True, "passcode_saved": True,
                                  "scripts_may_unlock": True, "ask_before_unlocking": True, "relock_after": True,
                                  "needs_check": False, "locked": True})
        self.assertIn("it is locked now", result.text)

    def test_the_detection_only_guard_cannot_unlock(self):
        # lockscreen.py with CAN_UNLOCK = False (#69): the guard stops a locked phone, but nothing here unlocks one,
        # so no Settings path is named for a setting that doesn't exist.
        from mobile_agent import lockscreen
        with mock.patch.object(lockscreen, "CAN_UNLOCK", False):
            self.assertFalse(direct.lockscreen_available())
            guard, leases = FakeGuard(GuardVerdict("unlocked")), []
            self.make(device_run=device_runs(guard, leases), wda_get=lambda url, timeout: True)
            result = self.call("unlock", device="Work iPhone")
            self.assertTrue(result.is_error)
            self.assertEqual(result.text, "Mobster can't unlock an iPhone and never enters a passcode. Ask the user "
                                          "to unlock “Work iPhone” themselves.")
            self.assertEqual((guard.checks, leases), ([], []))
            status = self.call("unlock_status", device="Work iPhone")
            self.assertEqual(status.structured, {"device": PHONE, "supported": False, "locked": True})
            self.assertEqual(status.text, "“Work iPhone” is locked. Mobster can't unlock an iPhone and never enters a "
                                          "passcode. Ask the user to unlock it.")
        with mock.patch.object(lockscreen, "CAN_UNLOCK", True):
            self.assertTrue(direct.lockscreen_available())
        with mock.patch.dict("sys.modules", {"mobile_agent.lockscreen": None}):
            self.assertFalse(direct.lockscreen_available())

    def test_the_lock_tools_report_the_lock_and_never_promise_an_unlock(self):
        # CAN_UNLOCK is False in this build: unlock_status reports whether the phone is locked, and the tools'
        # descriptions (the docs page is generated from them) say Mobster can't unlock it and the user has to.
        from mobile_agent import lockscreen
        self.assertIs(lockscreen.CAN_UNLOCK, False)
        for locked, said in ((False, "“Work iPhone” is unlocked. Mobster can't unlock an iPhone and never enters a "
                                     "passcode."),
                             (None, "“Work iPhone” may be locked: its runner didn't answer. Mobster can't unlock an "
                                    "iPhone and never enters a passcode. If it is, ask the user to unlock it.")):
            with self.subTest(locked=locked):
                self.make(wda_get=lambda url, timeout, locked=locked: locked)
                status = self.call("unlock_status", device="Work iPhone")
                self.assertEqual((status.text, status.structured["locked"]), (said, locked))
                self.assertEqual(set(status.structured), {"device", "supported", "locked"})
        self.make()
        simulator = self.call("unlock_status", device=SIM)
        self.assertEqual((simulator.structured["supported"], simulator.text),
                         (False, "“Mobster · iPhone 17 Pro · iOS 26.4” is a simulator: it has no passcode."))
        tools = {tool["name"]: tool["description"] for tool in self.tools.list_tools()}
        for name in ("unlock_status", "unlock"):
            with self.subTest(tool=name):
                text = tools[name]
                self.assertIn("can't unlock an iPhone and never enters a passcode", text)
                for promise in ("saved passcode", "its owner let", "may unlock", "Unlock the iPhone"):
                    self.assertNotIn(promise, text)
        self.assertIn("ask the user to unlock it", tools["unlock_status"])
        self.assertIn("the user has to unlock it themselves", tools["unlock"])

    def test_settings_default_to_off_and_never_carry_a_secret(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("mobile_agent.paths.user_data_dir",
                                                                 return_value=Path(folder)):
            settings = direct.unlock_settings({"id": PHONE, "udid": PHONE})
        self.assertEqual(settings, {"enabled": False, "askBeforeUnlocking": True, "scripts": False,
                                    "relockAfter": True, "needsCheck": False})
        self.assertTrue(all(isinstance(value, bool) for value in settings.values()))


if __name__ == "__main__":
    unittest.main()

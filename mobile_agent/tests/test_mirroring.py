import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from mobile_agent import compose, mirroring, mirroring_runtime
from mobile_agent.agent_hooks import READY

FIXTURES = Path(__file__).parent / "fixtures" / "mirroring"


def capture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def tiny_jpeg():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (8, 16), (200, 10, 10)).save(out, "JPEG")
    return out.getvalue()


class FakeHelper:

    def __init__(self, *screens, state=None, refuse=None):
        self.screens = list(screens) or ["settings"]
        self.refuse = refuse  # a MirroringError every gesture raises, as the helper's refusal does
        self.requests = []
        self.state = state or {"ok": True, "state": "ready", "window": capture("settings")["window"]}
        self.closed = False

    def request(self, op, timeout=15, **fields):
        self.requests.append((op, fields))
        if op == "state":
            if isinstance(self.state, Exception):
                raise self.state
            return self.state
        if op == "capture":
            name = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
            if "path" in fields:
                Path(fields["path"]).write_bytes(tiny_jpeg())
            answer = capture(name)
            if not fields.get("ocr"):
                answer.pop("rows", None)
            return answer
        if self.refuse is not None and op != "restore":
            raise self.refuse
        return {"ok": True}

    def gestures(self):
        return [(op, fields) for op, fields in self.requests if op not in ("capture", "state")]

    def close(self):
        self.closed = True


FRAMES = None


def setUpModule():
    global FRAMES
    FRAMES = TemporaryDirectory()


def tearDownModule():
    FRAMES.cleanup()


def driver(*screens, **kwargs):
    return mirroring.MirroringDriver(FakeHelper(*screens), frames_dir=FRAMES.name, sleep=lambda s: None, **kwargs)


class ScreenFromCaptureTests(unittest.TestCase):
    def test_lines_become_tappable_elements_in_reading_order(self):
        snapshot = mirroring.screen_from(capture("settings"), bundle="com.apple.Preferences")
        labels = [e.label for e in snapshot.elements]
        self.assertEqual(labels, ["9:41", "Settings", "Search", "Wi-Fi", "Bluetooth", "General", "Battery"])
        self.assertEqual(snapshot.source, "mirroring")
        self.assertEqual(snapshot.bundle_id, "com.apple.Preferences")
        wifi = next(e for e in snapshot.elements if e.label == "Wi-Fi")
        self.assertEqual((wifi.role, wifi.actions, wifi.editable), ("StaticText", ("TAP",), False))
        self.assertAlmostEqual(snapshot.width, 393.0)
        self.assertAlmostEqual(snapshot.height, 393.0 * 701 / 318)

    def test_a_placeholder_is_an_editable_field(self):
        search = next(e for e in mirroring.screen_from(capture("settings")).elements if e.label == "Search")
        self.assertEqual(search.role, "SearchField")
        self.assertTrue(search.editable)
        self.assertEqual(search.text, "")  # it shows its placeholder: empty
        self.assertNotIn("SUBMIT", search.actions)  # no keyboard yet

    def test_keyboard_keys_are_left_out_and_the_keyboard_is_noted(self):
        snapshot = mirroring.screen_from(capture("keyboard"))
        self.assertEqual(snapshot.keyboard, "visible")
        self.assertEqual([e.label for e in snapshot.elements], ["Search", "Cancel", "Recent Searches"])
        self.assertIn("SUBMIT", snapshot.elements[0].actions)

class DriverReadingTests(unittest.TestCase):
    def test_observe_reads_text_and_keeps_the_frame_for_the_model(self):
        d = driver("settings")
        snapshot = d.observe()
        self.assertIn("Battery", snapshot.text)
        self.assertEqual(d.helper.requests[0][0], "capture")
        self.assertTrue(d.helper.requests[0][1]["ocr"])
        preview = d.capture_preview()
        self.assertTrue(preview.startswith("data:image/jpeg;base64,"))
        self.assertEqual(d.source_reads, 1)

    def test_a_blocked_window_is_an_error_that_says_what_to_do(self):
        d = driver("blocked")
        with self.assertRaises(mirroring.MirroringError) as caught:
            d.observe()
        self.assertEqual(caught.exception.code, "login")
        self.assertIn("never types passwords", mirroring.fix_for(caught.exception))

    def test_wait_for_change_returns_the_new_screen_once_two_frames_agree(self):
        d = driver("settings")
        before = d.observe()
        d.helper.screens = ["keyboard", "keyboard", "keyboard"]
        after = d.wait_for_change(before, timeout=2, wait_seconds=.6)
        self.assertEqual(after.keyboard, "visible")


class DriverActingTests(unittest.TestCase):
    def test_tap_lands_on_the_elements_centre_in_window_fractions(self):
        d = driver("settings")
        snapshot = d.observe()
        battery = next(e for e in snapshot.elements if e.label == "Battery")
        d.execute("TAP", battery, snapshot)
        [(op, point)] = d.helper.gestures()
        self.assertEqual(op, "tap")
        self.assertAlmostEqual(point["x"], .3)
        self.assertAlmostEqual(point["y"], .5325)

    def test_tap_refuses_an_element_from_another_read(self):
        d = driver("settings")
        snapshot = d.observe()
        other = mirroring.screen_from(capture("home")).elements[0]
        with self.assertRaises(ValueError):
            d.execute("TAP", other, snapshot)
        self.assertEqual(d.helper.gestures(), [])

    def test_an_old_read_is_checked_before_a_tap(self):
        d = driver("settings")
        snapshot = d.observe()
        snapshot.captured_at -= 5
        battery = next(e for e in snapshot.elements if e.label == "Battery")
        d.helper.screens = ["home"]  # the screen moved on: Battery isn't there any more
        with self.assertRaises(ValueError):
            d.execute("TAP", battery, snapshot)
        self.assertEqual(d.helper.gestures(), [])
        d.helper.screens = ["settings"]  # the same frame: it goes ahead
        d.execute("TAP", battery, snapshot)
        self.assertEqual([op for op, _ in d.helper.gestures()], ["tap"])

    def test_home_is_cmd_1_and_an_app_tapped_on_the_home_screen_is_the_front_app(self):
        d = driver("home")
        d.execute("HOME", None, None)
        self.assertEqual(d.helper.gestures(), [("key", {"combo": "cmd+1"})])
        snapshot = d.observe()
        self.assertEqual(snapshot.bundle_id, "com.apple.springboard")
        notes = next(e for e in snapshot.elements if e.label == "Notes")
        d.execute("TAP", notes, snapshot)
        self.assertEqual(d.active_app(), "com.apple.mobilenotes")

    def test_launch_app_opens_by_name_and_never_presses_return_on_a_guess(self):
        # Spotlight's Return opened a web search for an app hidden from search: the shared
        # ios.system.open_app taps only a Top Hit that is the app (tests/test_ios.py has the recorded screens). On a
        # screen that never changes, every route fails and says where.
        d = driver("settings", app_names={"com.burbn.instagram": "Instagram"})
        with mock.patch("mobile_agent.ios.system.time.sleep", lambda s: None), \
                self.assertRaises(mirroring.TransportError):
            d.execute("LAUNCH_APP", "com.burbn.instagram", None)
        self.assertIn(("key", {"combo": "cmd+3"}), d.helper.gestures())
        self.assertNotIn(("key", {"combo": "return"}), d.helper.gestures())

    def test_an_app_hidden_from_spotlight_goes_home_and_says_so(self):
        # After Return, Spotlight is still up with its keyboard: no app result to open.
        d = driver("keyboard", app_names={"com.burbn.instagram": "Instagram"})
        with self.assertRaises(mirroring.TransportError) as caught:
            d.execute("LAUNCH_APP", "com.burbn.instagram", None)
        self.assertIn("App Library", str(caught.exception))
        self.assertEqual(d.helper.gestures()[-1], ("key", {"combo": "cmd+1"}))
        self.assertEqual(d.current_app, "com.apple.springboard")

    def test_launch_app_refuses_an_app_with_no_name(self):
        d = driver("settings")
        with self.assertRaises(ValueError):
            d.execute("LAUNCH_APP", "com.example.unnamed", None)
        self.assertEqual(d.helper.gestures(), [])

    def test_type_focuses_the_field_and_types_only_once_the_keyboard_is_up(self):
        d = driver("settings", "settings", "keyboard")
        snapshot = d.observe()
        search = next(e for e in snapshot.elements if e.label == "Search")
        d.execute("TYPE_SUBMIT", search, snapshot, text="battery")
        ops = d.helper.gestures()
        self.assertEqual(ops[0][0], "tap")
        self.assertEqual(ops[1:], [("type", {"text": "battery", "mode": "keys"}), ("key", {"combo": "return"})])

    def test_type_stops_when_the_field_doesnt_take_focus(self):
        d = driver("settings")
        clock = iter(range(0, 1000, 1))
        d._clock = lambda: next(clock)
        snapshot = d.observe()
        search = next(e for e in snapshot.elements if e.label == "Search")
        snapshot.captured_at = d._clock()
        with self.assertRaises(mirroring.TransportError):
            d.execute("TYPE", search, snapshot, text="battery")
        self.assertNotIn("type", [op for op, _ in d.helper.gestures()])

    def test_text_with_no_us_key_is_pasted(self):
        d = driver("keyboard")
        snapshot = d.observe()
        d.execute("TYPE", snapshot.elements[0], snapshot, text="café")
        self.assertEqual(d.helper.gestures(), [("type", {"text": "café", "mode": "paste"})])

    def test_type_refuses_a_label_that_isnt_a_field(self):
        d = driver("settings")
        snapshot = d.observe()
        wifi = next(e for e in snapshot.elements if e.label == "Wi-Fi")
        with self.assertRaises(ValueError):
            d.execute("TYPE", wifi, snapshot, text="x")
        self.assertEqual(d.helper.gestures(), [])

    def test_swipes(self):
        d = driver("settings")
        snapshot = d.observe()
        d.execute("SWIPE_UP", None, snapshot)
        d.execute("SWIPE_DOWN", None, snapshot)
        battery = next(e for e in snapshot.elements if e.label == "Battery")
        d.execute("SWIPE_LEFT", battery, snapshot)
        (_, up), (_, down), (op, drag) = d.helper.gestures()
        self.assertGreater(up["dy"], 0)
        self.assertLess(down["dy"], 0)
        self.assertEqual(op, "drag")
        self.assertGreater(drag["x1"], drag["x2"])
        self.assertAlmostEqual(drag["y1"], battery.center[1])

    def test_tap_point_and_long_press(self):
        d = driver("settings")
        snapshot = d.observe()
        d.tap_point(.5, .9, snapshot, purpose="tap_xy")
        d.long_press(snapshot.elements[1], snapshot)
        self.assertEqual([op for op, _ in d.helper.gestures()], ["tap", "press"])
        with self.assertRaises(ValueError):
            d.tap_point(1.5, .5, snapshot)

class HelperProtocolTests(unittest.TestCase):
    ECHO = ("import json,os,sys\n"
            "for line in sys.stdin:\n"
            "    r=json.loads(line)\n"
            "    if r['op']=='slow': continue\n"
            "    out={'ok':r['op']!='bad','id':r['id'],'op':r['op'],'seen':r}\n"
            "    if os.environ.get('HELPER_LOG'): open(os.environ['HELPER_LOG'],'a').write(json.dumps(r)+'\\n')\n"
            "    if r['op']=='bad': out.update(code='covered',error='Another window covers the phone')\n"
            "    print(json.dumps(out),flush=True)\n")

    def helper(self):
        def spawn(args, **kwargs):
            return subprocess.Popen([sys.executable, "-c", self.ECHO], **kwargs)
        return mirroring.Helper(path="unused", spawn=spawn)

    def test_answers_errors_and_timeouts(self):
        helper = self.helper()
        try:
            self.assertEqual(helper.request("state")["op"], "state")
            with self.assertRaises(mirroring.MirroringError) as caught:
                helper.request("bad")
            self.assertEqual(caught.exception.code, "covered")
            with self.assertRaises(mirroring.MirroringError):
                helper.request("slow", timeout=.3)
            self.assertEqual(helper.request("state")["op"], "state")  # the next request gets its own answer
        finally:
            helper.close()


class BackgroundInputTests(unittest.TestCase):

    def logged(self, body):
        with TemporaryDirectory() as folder:
            log = Path(folder) / "requests.jsonl"
            with mock.patch.dict(os.environ, {"HELPER_LOG": str(log)}):
                helper = HelperProtocolTests().helper()
            d = mirroring.MirroringDriver(helper, frames_dir=FRAMES.name, sleep=lambda s: None)
            try:
                body(d)
            finally:
                d.close()
            return [json.loads(line) for line in log.read_text().splitlines()]

    def test_the_driver_acts_without_bringing_the_window_forward(self):
        fresh = type("Read", (), {"captured_at": time.monotonic()})()

        def act(d):
            d.tap_point(.5, .5, fresh)
            d.execute("HOME", None, None)
            d.long_press(mirroring.screen_from(capture("settings")).elements[0],
                         mirroring.screen_from(capture("settings")))

        with mock.patch.dict(os.environ, {"MOBSTER_MIRRORING_INPUT": ""}):
            sent = self.logged(act)
        gestures = [r for r in sent if r["op"] != "restore"]
        self.assertEqual([r["op"] for r in gestures], ["tap", "key", "press"])
        self.assertEqual({r["input"] for r in gestures}, {"background"})
        # Nothing here asks the helper to raise or activate a window; "restore" gives back a front that was never taken.
        self.assertEqual({r["op"] for r in sent}, {"tap", "key", "press", "restore"})

    def test_a_gesture_goes_in_the_foreground_only_when_the_person_says_so(self):
        helper = HelperProtocolTests().helper()
        try:
            for value, expected in (("", "background"), ("background", "background"), (" Foreground ", "foreground")):
                with mock.patch.dict(os.environ, {"MOBSTER_MIRRORING_INPUT": value}):
                    self.assertEqual(helper.request("tap", x=.5, y=.5)["seen"]["input"], expected)
            self.assertNotIn("input", helper.request("state")["seen"])  # reads don't take a mode
        finally:
            helper.close()

    def test_a_misspelt_input_setting_sends_nothing(self):
        sent = []

        class Process:
            stdin = type("In", (), {"write": lambda self, line: sent.append(line), "flush": lambda self: None,
                                    "close": lambda self: None})()
            stdout = io.StringIO("")

            def wait(self, timeout=None):
                return 0

        helper = mirroring.Helper(path="unused", spawn=lambda *a, **k: Process())
        with mock.patch.dict(os.environ, {"MOBSTER_MIRRORING_INPUT": "forground"}):
            with self.assertRaises(mirroring.MirroringError) as caught:
                helper.request("tap", x=.5, y=.5)
        self.assertEqual(caught.exception.code, "usage")
        self.assertIn("background or foreground", str(caught.exception))
        self.assertEqual(sent, [])
        helper.close()

    def test_background_delivery_that_fails_is_one_attempt_and_never_takes_the_front(self):
        refusal = mirroring.MirroringError("iPhone Mirroring isn't answering, so nothing was sent.", "background")
        d = driver("settings")
        d.helper.refuse = refusal
        snapshot = d.observe()
        battery = next(e for e in snapshot.elements if e.label == "Battery")
        with self.assertRaises(mirroring.MirroringError) as caught:
            d.execute("TAP", battery, snapshot)
        self.assertEqual(caught.exception.code, "background")
        self.assertIn("MOBSTER_MIRRORING_INPUT=foreground", mirroring.fix_for(caught.exception))
        self.assertEqual([op for op, _ in d.helper.gestures()], ["tap"])  # not retried, no second way tried
        with self.assertRaises(mirroring.MirroringError):
            d.execute("HOME", None, None)
        self.assertEqual([op for op, _ in d.helper.gestures()], ["tap", "key"])


class SettingTests(unittest.TestCase):
    def test_off_unless_the_experimental_setting_is_on(self):
        with mock.patch.dict(os.environ, {mirroring.ENV_FLAG: ""}):
            self.assertFalse(mirroring.enabled())
            with self.assertRaises(ValueError):
                compose.build_target_driver(connection="mirroring")
            self.assertFalse(mirroring_runtime.wanted())

    def test_on_builds_the_mirroring_driver(self):
        with mock.patch.dict(os.environ, {mirroring.ENV_FLAG: "1"}), \
                mock.patch.object(mirroring, "Helper", lambda: FakeHelper("settings")):
            built = compose.build_target_driver(connection="mirroring", app_names={"a.b": "AB"})
            self.addCleanup(lambda: __import__("shutil").rmtree(built._dir, ignore_errors=True))
            self.assertIsInstance(built, mirroring.MirroringDriver)
            self.assertEqual(built.app_names, {"a.b": "AB"})
            built.close()
            self.assertTrue(mirroring_runtime.wanted())
            self.assertFalse(mirroring_runtime.wanted(type("Config", (), {"manage_device": True})()))

def guard():
    return mirroring_runtime.HelperGuard("mirroring", "iPhone Mirroring", mirroring_runtime.message_for,
                                         mirroring.fix_for)


class GuardTests(unittest.TestCase):

    def test_goes_ahead_while_the_phone_shows(self):
        d = driver("settings")
        self.assertIs(guard().check(d, cause="preflight"), READY)

    def test_stops_on_an_interstitial(self):
        d = driver("settings")
        d.helper.state = {"ok": True, "state": "blocked", "said": ["iPhone in Use"], "login": False}
        verdict = guard().check(d, cause="preflight")
        self.assertEqual(verdict.state, "stop")
        self.assertIn("iPhone in Use", verdict.message)
        self.assertIn("Lock your iPhone", verdict.message)

    def test_stops_when_it_cant_read_the_window(self):
        d = driver("settings")
        d.helper.state = mirroring.MirroringError("no", "permission")
        verdict = guard().check(d, cause="preflight")
        self.assertEqual(verdict.state, "stop")
        self.assertIn("Screen Recording", verdict.message)
        self.assertEqual(guard().check(object(), cause="resume").state, "stop")

    def test_never_reports_a_cable(self):
        self.assertIsNone(guard().attached())
        self.assertFalse(guard().wifi_lost())


class AgentLoopTests(unittest.TestCase):

    def test_the_agent_taps_a_line_and_finishes(self):
        from mobile_agent import engines
        from mobile_agent.tests import test_engines
        d = driver("settings")
        script = test_engines.Script([[("TAP", "Battery", None)], [("DONE", None, None)], [("DONE", None, None)]], [],
                                     answer="87%")
        with mock.patch.dict(os.environ, dict(test_engines.CLEAN), clear=True):
            result = engines.build_frontier(d, script, apps={"com.apple.Preferences": "Settings"}, emit=lambda e: None,
                                            config=test_engines.QUICK).run("What's my battery at?")
        self.assertEqual((result["status"], result["answer"]), ("completed", "87%"), result)
        self.assertEqual([op for op, _ in d.helper.gestures()], ["tap"])
        self.assertIn('"Battery"', script.prompts[0])


class RuntimeTests(unittest.TestCase):

    def runtime(self, helper):
        from mobile_agent.tui.session import serve_config
        config = serve_config(wda_url=mirroring_runtime.CONFIG_URL, enable_live=True, state_db=None, env_file=None,
                              spend_cap_usd=None, data_dir=FRAMES.name)
        with mock.patch.object(mirroring, "Helper", lambda: helper):
            runtime = mirroring_runtime.MirroringRuntime(config)
            self.addCleanup(runtime.close)
            return runtime, runtime.target_status()

    def test_ready_while_the_window_shows_the_phone(self):
        _, status = self.runtime(FakeHelper("settings"))
        self.assertEqual((status["ready"], status["driver"], status["health"]), (True, "mirroring", None))

    def test_the_live_view_streams_the_window_and_taps_go_through_the_helper(self):
        runtime, _ = self.runtime(FakeHelper("settings"))
        self.assertEqual(runtime.video.status()["kind"], "process")
        helper = FakeHelper("settings")
        runtime.control.driver = mirroring.MirroringDriver(helper, frames_dir=FRAMES.name, sleep=lambda s: None)
        runtime.control_device({"action": "tap", "x": .5, "y": .5})
        runtime.control_device({"action": "button", "name": "home"})
        self.assertEqual([op for op, _ in helper.gestures()], ["tap", "key"])

    def test_not_ready_on_an_interstitial_with_what_to_do(self):
        helper = FakeHelper("settings", state={"ok": True, "state": "blocked", "said": ["iPhone in Use"]})
        runtime, status = self.runtime(helper)
        self.assertFalse(status["ready"])
        self.assertIn("Lock your iPhone", status["health"])
        self.assertIsInstance(runtime.phone_guard(None, None), mirroring_runtime.HelperGuard)
        self.assertIn("iPhone in Use", runtime.not_ready_message({}))


class LiveControlTests(unittest.TestCase):

    def control(self):
        helper = FakeHelper("settings")
        control = mirroring.MirroringControl(
            lambda: mirroring.MirroringDriver(helper, frames_dir=FRAMES.name, sleep=lambda s: None))
        return control, helper

    def test_each_gesture(self):
        control, helper = self.control()
        control.perform({"action": "tap", "x": .2, "y": .3})
        control.perform({"action": "tap", "x": .2, "y": .3, "holdMs": 800})
        control.perform({"action": "swipe", "x1": .5, "y1": .8, "x2": .5, "y2": .2, "durationMs": 300})
        control.perform({"action": "type", "text": "hello", "submit": True})
        control.perform({"action": "button", "name": "home"})
        self.assertEqual([op for op, _ in helper.gestures()], ["tap", "press", "drag", "type", "key", "key"])

    def test_no_volume_keys_and_bad_gestures_send_nothing(self):
        control, helper = self.control()
        with self.assertRaises(ValueError):
            control.perform({"action": "button", "name": "volume_up"})
        with self.assertRaises(ValueError):
            control.perform({"action": "tap", "x": 2, "y": .3})
        self.assertEqual([op for op, _ in helper.gestures() if op != "restore"], [])

class TransferTests(unittest.TestCase):

    def run_with(self, answer, calls):
        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(answer) + "\n", stderr="")
        return run

    def test_to_phone_drops_at_an_element_or_the_middle(self):
        calls = []
        with TemporaryDirectory() as folder, mock.patch.object(mirroring, "helper_path", lambda: "/bin/helper"):
            photo = Path(folder) / "post.jpg"
            photo.write_bytes(b"x")
            answer = mirroring.transfer_to_phone([photo], run=self.run_with({"ok": True, "sent": ["post.jpg"]}, calls))
            self.assertEqual(answer["sent"], ["post.jpg"])
            self.assertEqual(calls[0][:4], ["/bin/helper", "drop", "0.5000", "0.5000"])
            self.assertTrue(calls[0][4].endswith("post.jpg"))
            target = mirroring.screen_from(capture("settings")).elements[3]
            mirroring.transfer_to_phone([photo], at=target, run=self.run_with({"ok": True, "sent": []}, calls))
            self.assertEqual(calls[1][2:4], [f"{target.tap_point[0]:.4f}", f"{target.tap_point[1]:.4f}"])

    def test_to_phone_refuses_before_sending_anything(self):
        calls = []
        with self.assertRaises(ValueError):
            mirroring.transfer_to_phone(["/no/such/file.jpg"], run=self.run_with({"ok": True}, calls))
        with self.assertRaises(ValueError):
            mirroring.transfer_to_phone([], run=self.run_with({"ok": True}, calls))
        self.assertEqual(calls, [])

    def test_a_refused_drop_is_an_error_with_its_code(self):
        calls = []
        with TemporaryDirectory() as folder, mock.patch.object(mirroring, "helper_path", lambda: "/bin/helper"):
            photo = Path(folder) / "post.jpg"
            photo.write_bytes(b"x")
            with self.assertRaises(mirroring.MirroringError) as caught:
                mirroring.transfer_to_phone([photo], run=self.run_with(
                    {"ok": False, "code": "covered", "error": "Another window covers the phone"}, calls))
        self.assertEqual(caught.exception.code, "covered")

    def test_from_phone_returns_what_arrived(self):
        calls = []
        with TemporaryDirectory() as folder, mock.patch.object(mirroring, "helper_path", lambda: "/bin/helper"):
            got = mirroring.transfer_from_phone((.3, .4), Path(folder) / "in", run=self.run_with(
                {"ok": True, "received": [str(Path(folder) / "in" / "IMG_1.HEIC")]}, calls))
            self.assertEqual([Path(p).name for p in got], ["IMG_1.HEIC"])
            self.assertEqual(calls[0][1:4], ["drag-out", "0.3000", "0.4000"])
            self.assertTrue((Path(folder) / "in").is_dir())


if __name__ == "__main__":
    unittest.main()

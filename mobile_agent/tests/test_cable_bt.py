import base64
import json
import os
import stat
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from mobile_agent import cable_bt, cable_bt_runtime, compose, mirroring, mirroring_runtime
from mobile_agent.agent_hooks import READY
from mobile_agent.api_errors import APIError
from mobile_agent.tests.test_mirroring import capture, tiny_jpeg
from mobile_agent.transport import TransportError
from mobile_agent.tui.session import serve_config


class World:
    def __init__(self, gain=(.0012, .0006), visible=True, drift=(0.0, 0.0)):
        self.gain, self.visible, self.drift = gain, visible, drift
        self.at = (.5, .5)
        self.marked = None

    def move(self, dx, dy):
        x = self.at[0] + dx * self.gain[0] + self.drift[0]
        y = self.at[1] + dy * self.gain[1] + self.drift[1]
        self.at = (min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0))


class FakeHid:
    def __init__(self, worlds, ready=True):
        self.worlds = worlds
        self.ready = ready
        self.requests = []

    def request(self, op, timeout=15, **fields):
        self.requests.append((op, fields))
        if op == "status":
            return {"ok": True, "state": "on", "hosts": [{"host": h, "ready": self.ready} for h in self.worlds]}
        world = self.worlds.get(fields.get("host"))
        if world is None:
            raise cable_bt.CableBtError("That iPhone isn't using this Mac as a keyboard now", "no_host")
        if op == "move":
            world.move(fields["dx"], fields["dy"])
        return {"ok": True}

    def sent(self, *ops):
        return [(op, f) for op, f in self.requests if op in ops]


class FakeScreen:
    def __init__(self, world, *screens):
        self.world = world
        self.screens = list(screens) or ["settings"]
        self.requests = []
        self.closed = False

    def request(self, op, timeout=15, **fields):
        self.requests.append((op, fields))
        if op == "capture":
            name = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
            if "path" in fields:
                Path(fields["path"]).write_bytes(tiny_jpeg())
            answer = capture(name) if isinstance(name, str) else dict(name)
            answer.setdefault("age", 20)
            if not fields.get("ocr"):
                answer.pop("rows", None)
            return answer
        if op == "frame":
            return {"jpeg": base64.b64encode(tiny_jpeg()).decode(), "key": "frame-1"}
        if op == "mark":
            self.world.marked = self.world.at
            return {"ok": True}
        if op == "blobs":
            blobs = []
            if self.world.visible and self.world.marked != self.world.at:
                for x, y in (self.world.marked, self.world.at):
                    blobs.append({"x": x, "y": y, "w": .04, "h": .02, "area": 30})
            return {"ok": True, "blobs": blobs}
        return {"ok": True}

    def close(self):
        self.closed = True


FRAMES = None


def setUpModule():
    global FRAMES
    FRAMES = TemporaryDirectory()


def tearDownModule():
    FRAMES.cleanup()


def phone(*screens, world=None, cal=None, hid=None, path=None, usb_lock=None):
    world = world or World()
    hid = hid or FakeHid({"HOST-A": world})
    screen = FakeScreen(world, *screens)
    cal = cal or cable_bt.Calibration(host="HOST-A")
    made = cable_bt.Phone(screen, hid, cal, udid="UDID-A", sleep=lambda s: None,
                          calibration_file=path or Path(FRAMES.name) / "cal.json", locked=lambda udid: usb_lock)
    return made, world, hid, screen


def driver(*screens, **kwargs):
    made, world, hid, screen = phone(*screens, **kwargs)
    return cable_bt.CableBtDriver(made, frames_dir=FRAMES.name, sleep=lambda s: None), world, hid, screen


class PointerTests(unittest.TestCase):
    def test_a_tap_clicks_only_with_the_pointer_seen_on_the_target(self):
        made, world, hid, _ = phone()
        made.request("tap", x=.3, y=.7)
        self.assertEqual(len(hid.sent("click")), 1)
        self.assertAlmostEqual(world.at[0], .3, delta=.02)
        self.assertAlmostEqual(world.at[1], .7, delta=.01)

    def test_a_wrong_gain_is_corrected_before_the_click(self):
        world = World(gain=(.0012, .0006))
        made, world, hid, _ = phone(world=world, cal=cable_bt.Calibration(host="HOST-A", gain=(.0014, .0007)))
        made.request("tap", x=.6, y=.6)
        self.assertEqual(len(hid.sent("click")), 1)
        self.assertGreaterEqual(made.pointer.corrections, 1)
        tx, ty = made.pointer.tolerance()
        self.assertLessEqual(abs(world.at[0] - .6), tx)
        self.assertLessEqual(abs(world.at[1] - .6), ty)

    def test_an_unseen_pointer_refuses_and_clicks_nothing(self):
        made, _, hid, _ = phone(world=World(visible=False), cal=cable_bt.Calibration(host="HOST-A", gain=(.001, .0005)))
        with self.assertRaises(cable_bt.CableBtError) as caught:
            made.request("tap", x=.5, y=.5)
        self.assertEqual(caught.exception.code, "pointer")
        self.assertFalse(hid.sent("click", "buttons"))

    def test_a_pointer_that_never_lands_refuses_after_two_corrections(self):
        world = World(drift=(.1, 0.0))
        made, _, hid, _ = phone(world=world, cal=cable_bt.Calibration(host="HOST-A", gain=world.gain))
        with self.assertRaises(cable_bt.CableBtError) as caught:
            made.request("tap", x=.3, y=.5)
        self.assertEqual(caught.exception.code, "pointer")
        self.assertGreaterEqual(made.pointer.corrections, 1)
        self.assertLessEqual(made.pointer.corrections, cable_bt.MAX_CORRECTIONS)
        self.assertFalse(hid.sent("click"))

    def test_a_pointer_pinned_at_the_edge_while_measuring_refuses(self):
        made, _, hid, _ = phone(world=World(gain=(.01, .01)))
        with self.assertRaises(cable_bt.CableBtError) as caught:
            made.request("tap", x=.5, y=.5)
        self.assertIn("Tracking Speed", str(caught.exception))
        self.assertFalse(hid.sent("click"))

    def test_a_long_press_holds_the_click(self):
        made, _, hid, _ = phone(cal=cable_bt.Calibration(host="HOST-A", gain=(.0012, .0006)))
        made.request("press", x=.5, y=.5, ms=1500)
        self.assertEqual(hid.sent("click")[0][1]["ms"], 1500)

    def test_a_drag_holds_the_button_through_the_move_and_lets_go(self):
        made, world, hid, _ = phone(cal=cable_bt.Calibration(host="HOST-A", gain=(.0012, .0006)))
        made.request("drag", x1=.5, y1=.7, x2=.5, y2=.3)
        buttons = [f["mask"] for _, f in hid.sent("buttons")]
        self.assertEqual(buttons, [1, 0])
        held = [f for op, f in hid.requests if op == "move" and f.get("mask") == 1]
        self.assertTrue(held and all(f["dy"] < 0 for f in held))
        self.assertAlmostEqual(world.at[1], .3, delta=.02)


class PhoneTests(unittest.TestCase):
    def test_capture_blacks_out_the_demonstration_status_bar(self):
        made, _, _, screen = phone()
        made.request("capture", ocr=True)
        self.assertEqual(screen.requests[-1][1]["mask"], cable_bt.STATUS_BAR)

    def test_text_without_us_keys_is_refused_before_a_key(self):
        made, _, hid, _ = phone()
        for text, mode in (("café", "keys"), ("hello", "paste"), ("x" * 81, "keys")):
            with self.assertRaises(cable_bt.CableBtError):
                made.request("type", text=text, mode=mode)
        self.assertFalse(hid.sent("type"))

    def test_state(self):
        made, _, _, _ = phone("settings")
        self.assertEqual(made.state(), {"state": "ready"})
        locked = {"rows": [{"text": "Swipe up to open", "confidence": 1, "rect": [.3, .9, .4, .02]}],
                  "window": {"w": 590, "h": 1278}, "hash": "x", "dark": .2}
        self.assertEqual(phone(locked)[0].state()["state"], "locked")
        clock = dict(locked, rows=[{"text": "9:41", "confidence": 1, "rect": [.2, .13, .6, .09]},
                                   {"text": "Tue Jan 9", "confidence": 1, "rect": [.35, .09, .3, .02]}])
        self.assertEqual(phone(clock)[0].state()["state"], "locked")
        small = dict(locked, rows=[{"text": "9:41", "confidence": 1, "rect": [.8, .5, .1, .02]}])
        self.assertEqual(phone(small)[0].state()["state"], "ready")
        self.assertEqual(phone("settings", usb_lock=True)[0].state()["state"], "locked")
        self.assertEqual(phone("settings", usb_lock=False)[0].state()["state"], "ready")
        dark = dict(locked, rows=[], dark=.99)
        self.assertEqual(phone(dark)[0].state()["state"], "asleep")
        stale = dict(capture("settings"), age=5000)
        self.assertEqual(phone(stale)[0].state()["state"], "no-video")
        unpaired = phone("settings", hid=FakeHid({"HOST-A": World()}, ready=False))[0]
        self.assertEqual(unpaired.state()["state"], "no-keyboard")

    def test_usb_lock_read(self):
        def run(answer):
            return lambda argv, timeout: answer
        tool = lambda name: "/usr/local/bin/" + name
        self.assertTrue(cable_bt.usb_locked("UDID-A", tool=tool, run=run("true\n")))
        self.assertFalse(cable_bt.usb_locked("UDID-A", tool=tool, run=run("false")))
        self.assertIsNone(cable_bt.usb_locked("UDID-A", tool=tool, run=run(None)))
        self.assertIsNone(cable_bt.usb_locked("UDID-A", tool=lambda name: None, run=run("true")))
        self.assertIsNone(cable_bt.usb_locked("", tool=tool, run=run("true")))

    def test_calibration_is_kept_owner_only(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "cable_bt.json"
            cable_bt.Calibration(host="HOST-A", gain=(.001, .0005)).save("UDID-A", path)
            loaded = cable_bt.Calibration.load("UDID-A", path)
            self.assertEqual((loaded.host, loaded.gain, loaded.home), ("HOST-A", (.001, .0005), "cmd+h"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.write_text(json.dumps({"phones": {"UDID-A": {"host": "H", "gain": [5, -1]}}}))
            self.assertEqual(cable_bt.Calibration.load("UDID-A", path).gain, ())
            self.assertEqual(cable_bt.Calibration.load("OTHER", path).host, "")

    def test_assign_host_picks_the_pointer_that_moves_in_this_video(self):
        mine, theirs = World(), World()
        hid = FakeHid({"HOST-THEIRS": theirs, "HOST-MINE": mine})
        self.assertEqual(cable_bt.assign_host(FakeScreen(mine), hid, sleep=lambda s: None), "HOST-MINE")
        self.assertIsNone(cable_bt.assign_host(FakeScreen(World()), hid, sleep=lambda s: None))
        self.assertFalse(hid.sent("click"))


class OpenPhoneTests(unittest.TestCase):
    def test_an_unpaired_phone_shows_the_mac_for_pairing_and_is_refused(self):
        world = World()
        hid = FakeHid({"HOST-OTHER": World()})
        screen = FakeScreen(world)
        with TemporaryDirectory() as folder, \
                mock.patch.object(cable_bt, "hid_link", return_value=hid), \
                mock.patch.object(cable_bt, "open_screen", return_value=screen):
            with self.assertRaises(cable_bt.CableBtError) as caught:
                cable_bt.open_phone(udid="UDID-A", calibration_file=Path(folder) / "c.json")
        self.assertEqual(caught.exception.code, "no_host")
        self.assertEqual(hid.sent("advertise")[0][1]["seconds"], cable_bt.ADVERTISE_SECONDS)
        self.assertTrue(screen.closed)
        self.assertFalse(hid.sent("click"))

    def test_a_paired_phone_is_found_by_its_pointer_and_kept(self):
        world = World()
        hid = FakeHid({"HOST-OTHER": World(), "HOST-A": world})
        with TemporaryDirectory() as folder, \
                mock.patch.object(cable_bt, "hid_link", return_value=hid), \
                mock.patch.object(cable_bt, "open_screen", return_value=FakeScreen(world)), \
                mock.patch.object(cable_bt.time, "sleep"):
            path = Path(folder) / "c.json"
            made = cable_bt.open_phone(udid="UDID-A", calibration_file=path)
            self.assertEqual(made.cal.host, "HOST-A")
            self.assertEqual(cable_bt.Calibration.load("UDID-A", path).host, "HOST-A")


class DriverTests(unittest.TestCase):
    def test_tap_on_an_element(self):
        made, world, hid, _ = driver("settings")
        snapshot = made.observe()
        wifi = next(e for e in snapshot.elements if e.label == "Wi-Fi")
        made.execute("TAP", wifi, snapshot)
        self.assertEqual(len(hid.sent("click")), 1)
        self.assertAlmostEqual(world.at[0], wifi.tap_point[0], delta=.03)

    def test_home_and_spotlight(self):
        made, _, hid, _ = driver("settings")
        made.execute("HOME", None, None)
        with mock.patch("mobile_agent.ios.system.time.sleep", lambda s: None), self.assertRaises(TransportError):
            made.execute("LAUNCH_APP", "com.apple.Preferences", None)
        combos = [f["combo"] for _, f in hid.sent("key")]
        self.assertEqual(combos[0], "cmd+h")
        self.assertIn("cmd+space", combos)
        self.assertNotIn("return", combos)
        self.assertEqual(hid.sent("type")[0][1]["text"], "Settings")

    def test_typed_text_is_read_back_before_return(self):
        typed = dict(capture("settings"))
        typed["rows"] = typed["rows"] + [{"text": "hello there", "confidence": 1, "rect": [.1, .15, .5, .03]}]
        made, _, hid, _ = driver("settings", typed)
        snapshot = made.observe()
        search = next(e for e in snapshot.elements if e.label == "Search")
        made.execute("TYPE_SUBMIT", search, snapshot, text="hello there")
        self.assertEqual(hid.sent("type")[0][1]["text"], "hello there")
        self.assertEqual(hid.sent("key")[-1][1]["combo"], "return")

    def test_text_that_doesnt_show_presses_no_return(self):
        made, _, hid, _ = driver("settings")
        snapshot = made.observe()
        search = next(e for e in snapshot.elements if e.label == "Search")
        with self.assertRaises(TransportError):
            made.execute("TYPE_SUBMIT", search, snapshot, text="never shown")
        self.assertFalse(hid.sent("key"))

    def test_non_us_text_is_refused_before_any_tap(self):
        made, _, hid, _ = driver("settings")
        snapshot = made.observe()
        search = next(e for e in snapshot.elements if e.label == "Search")
        with self.assertRaises(ValueError):
            made.execute("TYPE", search, snapshot, text="naïve")
        self.assertFalse(hid.sent("click", "type"))

    def test_swipe_up_drags_the_finger_up_and_down_drags_it_down(self):
        for operation, sign in (("SWIPE_UP", -1), ("SWIPE_DOWN", 1)):
            made, _, hid, _ = driver("settings")
            made.observe()
            made.execute(operation, None, None)
            held = [f for op, f in hid.requests if op == "move" and f.get("mask") == 1]
            self.assertTrue(held and all(f["dy"] * sign > 0 for f in held), operation)
            self.assertEqual([f["mask"] for _, f in hid.sent("buttons")], [1, 0])

    def test_no_volume_keys(self):
        made, _, _, _ = driver("settings")
        with self.assertRaises(ValueError):
            made.execute("VOLUME_UP", None, None)


class LinkTests(unittest.TestCase):
    SCRIPT = (
        "import json,sys,threading,time\n"
        "print(json.dumps({'event':'state','state':'on'}),flush=True)\n"
        "def answer(r):\n"
        "    if r['op']=='slow': time.sleep(.3)\n"
        "    if r['op']=='bad': "
        "print(json.dumps({'id':r['id'],'ok':False,'code':'no_host','error':'nope'}),flush=True); return\n"
        "    print(json.dumps({'id':r['id'],'ok':True,'op':r['op']}),flush=True)\n"
        "for line in sys.stdin:\n"
        "    r=json.loads(line)\n"
        "    if r['op']=='quit': break\n"
        "    threading.Thread(target=answer,args=(r,)).start()\n"
    )

    def link(self):
        return cable_bt.Link([sys.executable, "-c", self.SCRIPT], "test")

    def test_overlapping_requests_each_get_their_own_answer(self):
        link = self.link()
        try:
            out = {}
            slow = threading.Thread(target=lambda: out.setdefault("slow", link.request("slow", timeout=5)))
            slow.start()
            out["fast"] = link.request("fast", timeout=5)
            slow.join()
            self.assertEqual((out["slow"]["op"], out["fast"]["op"]), ("slow", "fast"))
        finally:
            link.close()

    def test_refusals_timeouts_and_a_stopped_helper(self):
        link = self.link()
        try:
            with self.assertRaises(cable_bt.CableBtError) as caught:
                link.request("bad", timeout=5)
            self.assertEqual(caught.exception.code, "no_host")
            with self.assertRaises(cable_bt.CableBtError) as caught:
                link.request("quit", timeout=2)
            self.assertIn(caught.exception.code, ("stopped", "write"))
            with self.assertRaises(cable_bt.CableBtError):
                link.request("fast", timeout=2)
        finally:
            link.close()


class SharedHelperTests(unittest.TestCase):
    SERVER = (
        "import json,os,socket,sys,threading\n"
        "path=sys.argv[1]\n"
        "s=socket.socket(socket.AF_UNIX); s.bind(path); s.listen(4)\n"
        "print(json.dumps({'event':'ready','ok':True}),flush=True); sys.stdout.close()\n"
        "def serve(c):\n"
        "    f=c.makefile('rw')\n"
        "    for line in f:\n"
        "        r=json.loads(line); f.write(json.dumps({'id':r['id'],'ok':True,'state':'on','hosts':[]})+'\\n'); "
        "f.flush()\n"
        "while True:\n"
        "    c,_=s.accept(); threading.Thread(target=serve,args=(c,),daemon=True).start()\n"
    )

    def test_the_first_process_starts_it_and_the_next_connect(self):
        with TemporaryDirectory(dir="/tmp") as folder:
            path = os.path.join(folder, "k.sock")
            started, servers = [], []

            def spawn(command, **kwargs):
                started.append(command)
                servers.append(subprocess.Popen([sys.executable, "-c", self.SERVER, command[-1]], **kwargs))
                return servers[-1]

            with mock.patch.object(cable_bt, "helper_path", return_value="bt-hid"), \
                    mock.patch.object(cable_bt, "_HID", None):
                first = cable_bt.hid_link(spawn=spawn, path=path)
                self.assertEqual(started[0][1:], ["--socket", path])
                second = cable_bt._connect(path)
                self.assertEqual(second.request("status")["state"], "on")
                self.assertEqual(len(started), 1)
                for link in (first, second):
                    link.close()
            for server in servers:
                server.kill()
                server.wait()


class SettingTests(unittest.TestCase):
    def test_off_unless_set(self):
        with mock.patch.dict(os.environ, {cable_bt.ENV_FLAG: ""}):
            self.assertFalse(cable_bt.enabled())
            with self.assertRaises(ValueError):
                compose.build_target_driver(connection="cable_bt")
        with mock.patch.dict(os.environ, {cable_bt.ENV_FLAG: "1"}):
            self.assertTrue(cable_bt.enabled())
            made = compose.build_target_driver(connection="cable_bt", helper=phone()[0])
            self.assertIsInstance(made, cable_bt.CableBtDriver)

    def test_wanted_only_without_manage_device(self):
        with mock.patch.dict(os.environ, {cable_bt.ENV_FLAG: "1"}):
            self.assertTrue(cable_bt_runtime.wanted(None))
            self.assertFalse(cable_bt_runtime.wanted(mock.Mock(manage_device=True)))


class GuardAndRuntimeTests(unittest.TestCase):
    def runtime(self, *screens):
        made, world, hid, screen = phone(*screens)
        config = serve_config(wda_url=cable_bt_runtime.CONFIG_URL, enable_live=True, state_db=None, env_file=None,
                              spend_cap_usd=None, data_dir=FRAMES.name)
        with mock.patch.object(mirroring, "Helper", side_effect=AssertionError("the mirroring helper was started")):
            runtime = cable_bt_runtime.CableBtRuntime(config, open_phone=lambda udid=None: made)
        self.addCleanup(runtime.close)
        return runtime, hid

    def test_the_live_view_is_the_cable_video_and_taps_go_through_the_pointer(self):
        runtime, hid = self.runtime("settings")
        self.assertEqual(runtime.video.status()["kind"], "cable")
        self.assertEqual(runtime.video.pull(300, 50, None), (tiny_jpeg(), "frame-1"))
        with mock.patch.object(mirroring, "Helper", side_effect=AssertionError("the mirroring helper was started")):
            runtime.control_device({"action": "tap", "x": .5, "y": .5})
        self.assertEqual(len(hid.sent("click")), 1)

    def test_a_task_holding_the_phone_refuses_the_live_view(self):
        runtime, hid = self.runtime("settings")
        runtime.active_runs["run"] = None
        with self.assertRaises(APIError) as caught:
            runtime.control_device({"action": "tap", "x": .5, "y": .5})
        self.assertEqual(caught.exception.code, "device_busy")
        self.assertEqual(hid.sent("click"), [])

    def test_guard_goes_on_only_when_ready(self):
        guard = mirroring_runtime.HelperGuard("cable_bt", "the iPhone", cable_bt_runtime.message_for, cable_bt.fix_for)
        made, _, _, _ = driver("settings")
        self.assertIs(guard.check(made, cause="preflight"), READY)
        locked = {"rows": [{"text": "Enter Passcode", "confidence": 1, "rect": [.3, .3, .4, .03]}],
                  "window": {"w": 590, "h": 1278}, "hash": "y", "dark": .1}
        made, _, _, _ = driver(locked)
        verdict = guard.check(made, cause="preflight")
        self.assertEqual(verdict.state, "stop")
        self.assertIn("never unlocks", verdict.message)

    def test_messages_say_what_happened_then_what_to_do(self):
        message = cable_bt_runtime.message_for({"state": "no-keyboard", "code": "no_host",
                                                "said": ["The iPhone isn't using this Mac as a keyboard"]})
        self.assertTrue(message.startswith("The iPhone isn't using this Mac as a keyboard. Pair the iPhone"))
        self.assertIsNone(cable_bt_runtime.message_for({"state": "ready"}))

    def test_a_borrowed_phone_outlives_the_run(self):
        made, _, _, screen = phone("settings")
        lent = cable_bt_runtime.Borrowed(made)
        run_driver = cable_bt.CableBtDriver(lent, frames_dir=FRAMES.name, sleep=lambda s: None)
        run_driver.close()
        self.assertFalse(screen.closed)
        made.close()
        self.assertTrue(screen.closed)


if __name__ == "__main__":
    unittest.main()

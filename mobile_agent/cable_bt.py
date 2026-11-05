"""Cable + Bluetooth: the iPhone's screen video over USB, and this Mac as its Bluetooth keyboard and mouse."""

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass

from . import perception
from .drivers import grounded_target, register_driver
from .mirroring import HELPER_TIMEOUT, MirroringDriver, helper_path, typable
from .state import validate_input_text
from .transport import TransportError

SOURCE = "cable_bt"
ENV_FLAG = "MOBSTER_EXPERIMENTAL_CABLE_BT"

# While the phone is captured, iOS draws a demonstration status bar; this top share of the picture is blacked out and
# its text left unread (59 of 852 pt on an iPhone 15 Pro: the status bar and the Dynamic Island).
STATUS_BAR = .069
# The helper's video width in pixels (scaled in hardware): about Mirroring's capture and WDA's screenshots.
VIDEO_WIDTH = 590

# The keys MirroringDriver sends for Home and Spotlight (iPhone Mirroring's own); Phone turns them into the iPhone's.
MIRRORING_HOME, MIRRORING_SPOTLIGHT = "cmd+1", "cmd+3"

# The pointer. Homing: big steps into the top-left corner. Moving: a constant pace, so one gain holds.
HOME_COUNTS = 6000
HOME_STEP = 1500
HOME_INTERVAL_MS = 8
MOVE_STEP = 12
MOVE_INTERVAL_MS = 12
CALIBRATE_COUNTS = (240, 480)
# A click lands only within this distance of the target (points of a 393-point-wide screen).
TOLERANCE_PT = 9.0
MAX_CORRECTIONS = 2
# How long the video is watched for the pointer after a move, and how often.
FIND_SECONDS = .6
FIND_POLL_SECONDS = .04
# A pointer-sized change in the 148 x 320 difference grid (AssistiveTouch's pointer is about 7 cells across).
POINTER_AREA = (6, 220)
SETTLE_SECONDS = .06
# With a hardware keyboard the software keyboard doesn't appear: a tapped field gets this long to take focus.
FOCUS_SECONDS = .45
CLICK_MS = 50
LONG_PRESS_MS = 1500
# While no paired phone answers, the Mac shows itself for pairing this long.
ADVERTISE_SECONDS = 600
DRAG_STEP = 10
DRAG_INTERVAL_MS = 10

PHONE_WIDTH_PT = perception.PHONE_WIDTH_PT

# Lock and sleep, read from the picture: the phone must stay unlocked, and Mobster never unlocks it.
# The lock screen's clock is about 8% of the screen tall on iOS 26; app times are under 3%.
LOCK_CLOCK_HEIGHT = .05
LOCK_LINES = ("swipe up to open", "swipe up to unlock", "enter passcode", "touch id or enter passcode",
              "face id", "press home to open", "press home to unlock")


def usb_locked(udid, *, tool=None, run=None):
    if not udid:
        return None
    from . import mac_tools
    tool, run = tool or mac_tools.tool, run or mac_tools.run
    info = tool("ideviceinfo")
    value = run([info, "-u", udid, "-k", "PasswordProtected"], timeout=3) if info else None
    answer = (value or "").strip().casefold()
    return {"true": True, "false": False}.get(answer)


def _lock_clock(row):
    """The lock screen's big clock: a time alone on its line, drawn tall in the top third (an app's times are small)."""
    import re
    rect = row.get("rect") or (0, 1, 0, 0)
    return (bool(re.fullmatch(r"\d{1,2}[:.]\d{2}", " ".join(str(row.get("text") or "").split())))
            and float(rect[3]) >= LOCK_CLOCK_HEIGHT and float(rect[1]) < .35)


def enabled():
    """Whether the experimental Cable + Bluetooth connection is on (``MOBSTER_EXPERIMENTAL_CABLE_BT=1``)."""
    return os.environ.get(ENV_FLAG, "").strip().casefold() in ("1", "true", "yes", "on")


class CableBtError(TransportError):
    """A helper refused or failed."""

    def __init__(self, message, code="capture"):
        super().__init__(message)
        self.code = code


FIXES = {
    "no_device": "Plug the iPhone into this Mac with a cable, unlock it and tap Trust if it asks, then try again.",
    "ambiguous_device": "Mobster can't tell the plugged-in iPhones apart. Unplug the others, then try again.",
    "permission": "Allow Camera for Mobster in System Settings > Privacy & Security, then try again.",
    "no_bluetooth": "Turn Bluetooth on on this Mac, then try again.",
    "bt_permission": "Allow Bluetooth for Mobster in System Settings > Privacy & Security, then try again.",
    "no_host": "Pair the iPhone with this Mac: on the iPhone, open Settings > Bluetooth and tap Mobster Keyboard. "
               "Then try again.",
    "pointer": "Turn on AssistiveTouch on the iPhone (Settings > Accessibility > Touch), turn off Automatically Hide "
               "Pointer (Accessibility > Pointer Control), then try again.",
    "locked": "Unlock your iPhone, then try again. Mobster never unlocks it.",
}


def fix_for(error):
    """The sentence that says what to do about ``error`` (a CableBtError), or ""."""
    return FIXES.get(getattr(error, "code", ""), "")


class Link:
    """One helper process, spoken to in JSON lines."""

    def __init__(self, command, label, process=None):
        self.label = label
        self._process = process or subprocess.Popen([str(part) for part in command], stdin=subprocess.PIPE,
                                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                                    bufsize=1)
        self._lock = threading.Lock()
        self._waiting = {}
        self._next = 0
        self.closed = False
        self._reader = threading.Thread(target=self._read, name=f"{label}-helper", daemon=True)
        self._reader.start()

    def _read(self):
        for line in self._process.stdout:
            try:
                answer = json.loads(line)
            except ValueError:
                continue
            ident = answer.get("id") if isinstance(answer, dict) else None
            with self._lock:
                box = self._waiting.pop(ident, None) if ident is not None else None
            if box is not None:
                box.put(answer)
        with self._lock:
            self.closed = True
            waiting, self._waiting = self._waiting, {}
        for box in waiting.values():
            box.put(None)

    def request(self, op, timeout=HELPER_TIMEOUT, **fields):
        """The helper's answer to ``op``; CableBtError when it refuses, fails, stops or doesn't answer in time."""
        box = queue.Queue(maxsize=1)
        with self._lock:
            if self.closed:
                raise CableBtError(f"Mobster's {self.label} helper stopped", "stopped")
            self._next += 1
            ident = self._next
            self._waiting[ident] = box
            try:
                self._process.stdin.write(json.dumps({"op": op, "id": ident, **fields}) + "\n")
                self._process.stdin.flush()
            except (OSError, ValueError):
                self._waiting.pop(ident, None)
                raise CableBtError(f"Mobster's {self.label} helper stopped", "stopped") from None
        try:
            answer = box.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._waiting.pop(ident, None)
            raise CableBtError(f"Mobster's {self.label} helper didn't answer {op} in {timeout:g} s", "write") from None
        if answer is None:
            raise CableBtError(f"Mobster's {self.label} helper stopped", "stopped")
        if answer.get("ok") is not True:
            raise CableBtError(str(answer.get("error") or f"The {self.label} helper refused"),
                               str(answer.get("code") or "capture"))
        return answer

    def close(self):
        process, self._process = self._process, None
        if process is None:
            return
        for step in (lambda: process.stdin.close(), lambda: process.wait(timeout=2)):
            try:
                step()
            except Exception:  # noqa: BLE001 -- closing is best effort; kill below
                process.kill()
                break
        try:
            process.stdout.close()
        except Exception:  # noqa: BLE001
            pass


_HID = None
_HID_LOCK = threading.Lock()


class _SocketProcess:
    """A connection to the shared Bluetooth keyboard helper, shaped like the Popen that Link talks to."""

    def __init__(self, connection):
        self._connection = connection
        self.stdout = connection.makefile("r", encoding="utf-8", newline="\n")
        self.stdin = connection.makefile("w", encoding="utf-8", newline="\n")

    def wait(self, timeout=None):
        self.kill()
        return 0

    def kill(self):
        import socket
        try:
            self._connection.shutdown(socket.SHUT_RDWR)  # the reader sees the end before its file is closed
        except OSError:
            pass
        self._connection.close()


def socket_path():
    """Where the shared Bluetooth keyboard helper listens: this user's private temporary folder."""
    import tempfile
    return os.path.join(tempfile.gettempdir(), f"mobster-{os.getuid()}-bt-hid.sock")


def _connect(path):
    import socket
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(path)
    except OSError:
        connection.close()
        return None
    return Link(None, "Bluetooth keyboard", process=_SocketProcess(connection))


def _start_shared(path, spawn=subprocess.Popen):
    """Start the shared helper (it outlives this process, and ends 2 minutes after its last client leaves)."""
    process = spawn([str(helper_path("bt_hid")), "--socket", path], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    line = process.stdout.readline()
    process.stdout.close()
    try:
        ready = json.loads(line or "{}")
    except ValueError:
        ready = {}
    if not ready.get("ok"):
        raise CableBtError("Mobster's Bluetooth keyboard helper didn't start: "
                           + str(ready.get("error") or "no answer"),
                           "no_bluetooth")


# One helper for every Mobster process on this Mac: one HID service is what the iPhones see.
def hid_link(spawn=subprocess.Popen, path=None):
    global _HID
    with _HID_LOCK:
        if _HID is not None and not _HID.closed:
            return _HID
        path = path or socket_path()
        link = _connect(path)
        if link is None:
            _start_shared(path, spawn=spawn)
            link = _connect(path)
            if link is None:
                raise CableBtError("Mobster couldn't reach its Bluetooth keyboard helper", "no_bluetooth")
        deadline = time.monotonic() + 5
        while True:
            state = link.request("status", timeout=3).get("state")
            if state == "on":
                break
            if state == "unauthorized" or time.monotonic() >= deadline:
                link.close()
                code = "bt_permission" if state == "unauthorized" else "no_bluetooth"
                raise CableBtError("macOS doesn't let Mobster use Bluetooth" if code == "bt_permission"
                                   else "Bluetooth isn't on on this Mac", code)
            time.sleep(.1)
        _HID = link
        return link


def open_screen(udid=None, name=None, phones=None):
    # a capture device may not carry the phone's UDID: its name and the phone count go along, never a guess between two
    if udid and name is None and phones is None:
        from .listen import capture_hints
        hints = capture_hints(udid)
        hinted = dict(zip(hints[::2], hints[1::2]))
        name = hinted.get("--name")
        phones = int(hinted["--phones"]) if hinted.get("--phones", "").isdigit() else None
    link = Link([helper_path("phone_screen")], "iPhone screen")
    try:
        link.request("open", timeout=25, udid=udid, name=name, phones=phones, width=VIDEO_WIDTH)
    except Exception:
        link.close()
        raise
    return link


def calibration_path():
    from .paths import user_data_dir
    return user_data_dir() / "cable_bt.json"


@dataclass
class Calibration:
    """One phone's pairing and pointer: its Bluetooth host on this Mac and the pointer's gain per count."""
    host: str = ""
    gain: tuple = ()
    home: str = "cmd+h"
    spotlight: str = "cmd+space"

    @classmethod
    def load(cls, udid, path=None):
        try:
            record = json.loads((path or calibration_path()).read_text()).get("phones", {}).get(udid or "", {})
        except (OSError, ValueError, AttributeError):
            record = {}
        gain = record.get("gain")
        ok = isinstance(gain, (list, tuple)) and len(gain) == 2 and all(isinstance(v, (int, float)) and 0 < v < 1
                                                                        for v in gain)
        return cls(host=str(record.get("host") or ""), gain=tuple(gain) if ok else (),
                   home=str(record.get("home") or "cmd+h"), spotlight=str(record.get("spotlight") or "cmd+space"))

    def save(self, udid, path=None):
        path = path or calibration_path()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        phones = data.setdefault("phones", {})
        phones[udid or ""] = {"host": self.host, "gain": list(self.gain), "home": self.home,
                              "spotlight": self.spotlight}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = path.with_suffix(".partial")
        partial.write_text(json.dumps(data, indent=2))
        os.chmod(partial, 0o600)
        os.replace(partial, path)


class Pointer:
    """AssistiveTouch's pointer on one phone: put where a tap should land, and seen there in the video first."""

    def __init__(self, screen, hid, calibration, *, aspect=2556 / 1179, save=None, sleep=time.sleep,
                 clock=time.monotonic):
        self.screen, self.hid, self.cal = screen, hid, calibration
        self.aspect = aspect  # height / width
        self._save = save or (lambda cal: None)
        self._sleep, self._clock = sleep, clock
        self.corrections = 0
        self.taps = 0

    def tolerance(self):
        return TOLERANCE_PT / PHONE_WIDTH_PT, TOLERANCE_PT / (PHONE_WIDTH_PT * self.aspect)

    def _move(self, dx, dy, *, step=MOVE_STEP, interval=MOVE_INTERVAL_MS, mask=0):
        dx, dy = int(round(dx)), int(round(dy))
        if not dx and not dy:
            return
        reports = max(abs(dx), abs(dy)) / max(step, 1)
        self.hid.request("move", host=self.cal.host, dx=dx, dy=dy, step=step, interval_ms=interval, mask=mask,
                         timeout=5 + reports * (interval / 1000 + .02))

    def home(self):
        """Into the top-left corner: big steps, far more than the screen."""
        self._move(-HOME_COUNTS, -HOME_COUNTS, step=HOME_STEP, interval=HOME_INTERVAL_MS)
        self._sleep(SETTLE_SECONDS)

    def find(self, near, avoid=(0.0, 0.0)):
        deadline = self._clock() + FIND_SECONDS
        tx, ty = self.tolerance()
        while True:
            blobs = self.screen.request("blobs", timeout=3).get("blobs") or []
            candidates = [b for b in blobs if POINTER_AREA[0] <= int(b.get("area") or 0) <= POINTER_AREA[1]
                          and not (abs(b["x"] - avoid[0]) < tx * 1.5 and abs(b["y"] - avoid[1]) < ty * 1.5)]
            if candidates:
                return min(candidates, key=lambda b: (b["x"] - near[0]) ** 2 + ((b["y"] - near[1]) * self.aspect) ** 2)
            if self._clock() >= deadline:
                return None
            self._sleep(FIND_POLL_SECONDS)

    def calibrate(self):
        """Measure the pointer's gain: from the corner, a known move, and where the video shows it."""
        self.home()
        self.screen.request("mark")
        cx, cy = CALIBRATE_COUNTS
        self._move(cx, cy)
        seen = self.find(near=(.5, .5))
        if seen is None:
            raise CableBtError("Mobster couldn't see AssistiveTouch's pointer move on the iPhone; nothing was tapped",
                               "pointer")
        if not (.03 < seen["x"] < .97 and .03 < seen["y"] < .97):
            raise CableBtError("The pointer ran to the screen's edge while Mobster measured it; nothing was tapped. "
                               "Set Tracking Speed lower in AssistiveTouch, then try again.", "pointer")
        self.cal.gain = (seen["x"] / cx, seen["y"] / cy)
        self._save(self.cal)
        return self.cal.gain

    def aim(self, fx, fy):
        if not (0 <= fx <= 1 and 0 <= fy <= 1):
            raise ValueError("The point is outside the screen")
        if not self.cal.gain:
            self.calibrate()
        gx, gy = self.cal.gain
        tx, ty = self.tolerance()
        self.home()
        self.screen.request("mark")
        self._move(fx / gx, fy / gy)
        avoid, total = (0.0, 0.0), (fx / gx, fy / gy)
        for attempt in range(MAX_CORRECTIONS + 1):
            seen = self.find(near=(fx, fy), avoid=avoid)
            if seen is None:
                raise CableBtError("Mobster couldn't see the pointer after moving it; nothing was tapped", "pointer")
            ex, ey = fx - seen["x"], fy - seen["y"]
            if attempt == 0 and .05 < seen["x"] < .95 and .05 < seen["y"] < .95:
                # Learn from a clean move out of the corner (no correction mixed in), slowly.
                gx = .7 * gx + .3 * (seen["x"] / total[0]) if total[0] > 0 else gx
                gy = .7 * gy + .3 * (seen["y"] / total[1]) if total[1] > 0 else gy
                self.cal.gain = (gx, gy)
            if abs(ex) <= tx and abs(ey) <= ty:
                return seen
            if attempt == MAX_CORRECTIONS:
                break
            self.corrections += 1
            avoid = (seen["x"], seen["y"])
            self.screen.request("mark")
            self._move(ex / gx, ey / gy)
        raise CableBtError("The pointer didn't land on the target; nothing was tapped", "pointer")

    def tap(self, fx, fy, hold_ms=None):
        self.aim(fx, fy)
        self.hid.request("click", host=self.cal.host, ms=hold_ms or CLICK_MS, timeout=5 + (hold_ms or 0) / 1000)
        self.taps += 1
        if self.taps % 20 == 1:
            self._save(self.cal)

    def drag(self, x1, y1, x2, y2):
        """A touch drag: the pointer seen at the start, the button held through a constant-pace move, then let go."""
        self.aim(x1, y1)
        gx, gy = self.cal.gain
        self.hid.request("buttons", host=self.cal.host, mask=1)
        try:
            self._move((x2 - x1) / gx, (y2 - y1) / gy, step=DRAG_STEP, interval=DRAG_INTERVAL_MS, mask=1)
        finally:
            self.hid.request("buttons", host=self.cal.host, mask=0)


class Phone:
    """One phone on this connection, speaking the protocol MirroringDriver's helper speaks."""

    def __init__(self, screen, hid, calibration, *, udid="", sleep=time.sleep, clock=time.monotonic,
                 calibration_file=None, locked=None):
        self.screen, self.hid, self.udid = screen, hid, udid
        self.cal = calibration
        self._file = calibration_file
        self.pointer = Pointer(screen, hid, calibration, save=self.save, sleep=sleep, clock=clock)
        self.locked = locked or usb_locked
        self.window = None

    def save(self, cal):
        try:
            cal.save(self.udid, self._file)
        except OSError:
            pass  # a calibration not kept is measured again next time

    def _capture(self, fields):
        fields = dict(fields)
        fields.setdefault("mask", STATUS_BAR)
        answer = self.screen.request("capture", timeout=HELPER_TIMEOUT, **fields)
        window = answer.get("window") or {}
        if window.get("w") and window.get("h"):
            self.window = window
            self.pointer.aspect = float(window["h"]) / float(window["w"])
        return answer

    def state(self):
        try:
            seen = self._capture({"ocr": True, "fast": True})
        except CableBtError as error:
            return {"state": "no-video", "code": error.code, "said": [str(error)]}
        if (seen.get("age") or 0) > 3000:
            return {"state": "no-video", "code": "capture", "said": ["The iPhone's screen video stopped"]}
        if float(seen.get("dark") or 0) > .97:
            return {"state": "asleep", "code": "locked", "said": ["The iPhone's screen is off"]}
        if self.locked(self.udid):
            return {"state": "locked", "code": "locked", "said": ["The iPhone is locked"]}
        rows = seen.get("rows") or ()
        lines = [" ".join(str(row.get("text") or "").split()).casefold() for row in rows]
        if any(line.startswith(lock) for line in lines for lock in LOCK_LINES) or any(map(_lock_clock, rows)):
            return {"state": "locked", "code": "locked", "said": ["The iPhone is locked"]}
        try:
            hosts = self.hid.request("status", timeout=3).get("hosts") or []
        except CableBtError as error:
            return {"state": "no-keyboard", "code": error.code, "said": [str(error)]}
        if not any(h.get("host") == self.cal.host and h.get("ready") for h in hosts):
            return {"state": "no-keyboard", "code": "no_host",
                    "said": ["The iPhone isn't using this Mac as a keyboard"]}
        return {"state": "ready"}

    def _key(self, combo):
        self.hid.request("key", host=self.cal.host, combo=combo)

    def request(self, op, timeout=HELPER_TIMEOUT, **fields):
        if op == "capture":
            return self._capture(fields)
        if op == "state":
            return {"ok": True, **self.state()}
        if op == "tap":
            self.pointer.tap(fields["x"], fields["y"])
        elif op == "press":
            self.pointer.tap(fields["x"], fields["y"], hold_ms=fields.get("ms") or LONG_PRESS_MS)
        elif op == "drag":
            self.pointer.drag(fields["x1"], fields["y1"], fields["x2"], fields["y2"])
        elif op == "scroll":
            # MirroringDriver's scroll: dy window points, positive reveals what's below (the finger moves up).
            height = float((self.window or {}).get("h") or 1)
            span = max(min(abs(float(fields.get("dy") or 0)) / height, .6), .1)
            x, y = float(fields.get("x", .5)), float(fields.get("y", .5))
            top, bottom = y - span / 2, y + span / 2
            if float(fields.get("dy") or 0) > 0:
                self.pointer.drag(x, bottom, x, top)
            else:
                self.pointer.drag(x, top, x, bottom)
        elif op == "key":
            combo = fields.get("combo") or ""
            # MirroringDriver sends iPhone Mirroring's own keys for Home (cmd+1) and Spotlight (cmd+3): the iPhone's are
            # this phone's calibrated ones (cmd+H and cmd+space).
            self._key({MIRRORING_HOME: self.cal.home, MIRRORING_SPOTLIGHT: self.cal.spotlight}.get(combo, combo))
        elif op == "type":
            if fields.get("mode") != "keys" or not typable(fields.get("text") or ""):
                raise CableBtError("Cable + Bluetooth types US keyboard characters only (at most 80); nothing was "
                                   "typed", "refused")
            text = fields["text"]
            self.hid.request("type", host=self.cal.host, text=text, timeout=5 + len(text) * .1)
        elif op == "restore":
            self.hid.request("release", host=self.cal.host, timeout=3)
        else:
            raise CableBtError(f"Cable + Bluetooth can't {op}", "usage")
        return {"ok": True}

    def close(self):
        self.screen.close()


def assign_host(screen, hid, sleep=time.sleep):
    hosts = [h["host"] for h in hid.request("status", timeout=3).get("hosts") or () if h.get("ready")]
    # moving a pointer touches nothing (only a click does), so nudging every free host is safe
    for host in hosts:
        hid.request("move", host=host, dx=-HOME_COUNTS, dy=-HOME_COUNTS, step=HOME_STEP, interval_ms=HOME_INTERVAL_MS)
        sleep(SETTLE_SECONDS)
        screen.request("mark")
        hid.request("move", host=host, dx=200, dy=400, step=MOVE_STEP, interval_ms=MOVE_INTERVAL_MS, timeout=10)
        sleep(.25)
        if any(POINTER_AREA[0] <= int(b.get("area") or 0) <= POINTER_AREA[1]
               for b in screen.request("blobs").get("blobs") or ()):
            return host
    return None


def open_phone(udid=None, name=None, phones=None, *, calibration_file=None):
    hid = hid_link()
    screen = open_screen(udid=udid, name=name, phones=phones)
    try:
        cal = Calibration.load(udid, calibration_file)
        ready = [h["host"] for h in hid.request("status", timeout=3).get("hosts") or () if h.get("ready")]
        if cal.host not in ready:
            cal.host = assign_host(screen, hid) or ""
            if not cal.host:
                # Show the Mac as "Mobster Keyboard" in the iPhone's Bluetooth settings for a while, for pairing.
                hid.request("advertise", seconds=ADVERTISE_SECONDS)
                raise CableBtError("This iPhone isn't using this Mac as a Bluetooth keyboard and mouse", "no_host")
            cal.gain = ()
        phone = Phone(screen, hid, cal, udid=udid or "", calibration_file=calibration_file)
        phone.save(cal)
        return phone
    except Exception:
        screen.close()
        raise


def _shows(snapshot, text):
    """Whether ``text`` (typed) is on the screen: its words in order, in one element's text, any case."""
    wanted = " ".join(text.split()).casefold()
    if not wanted:
        return True
    return any(wanted in " ".join(e.label.split()).casefold() for e in snapshot.elements)


class CableBtDriver(MirroringDriver):
    """The device contract over Cable + Bluetooth: MirroringDriver's, over a ``Phone`` (tests pass a fake)."""

    source = SOURCE

    def __init__(self, helper=None, *, udid=None, name=None, phones=None, app_names=None, frames_dir=None,
                 sleep=time.sleep, clock=time.monotonic, labeller=None, icon_memory=None):
        if helper is None:
            helper = open_phone(udid=udid, name=name, phones=phones)
        super().__init__(helper, app_names=app_names, frames_dir=frames_dir, sleep=sleep, clock=clock,
                         labeller=labeller, icon_memory=icon_memory)

    def observe(self, timeout=10):
        result, started = self._capture(True, timeout)
        return perception.snapshot_from(result, source=SOURCE, bundle=self.current_app, read_started=started,
                                        icons=self._icons(result))

    def _await_keyboard(self):
        # A Bluetooth keyboard keeps the software keyboard away: wait for focus, then read the text back after typing.
        self._sleep(FOCUS_SECONDS)
        return True

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        if operation not in ("TYPE", "TYPE_SUBMIT"):
            return super().execute(operation, target, snapshot, text=text, timeout=timeout)
        grounded_target(operation, target, snapshot)
        validate_input_text(text, multiline=False)
        if not typable(text):
            raise ValueError("Cable + Bluetooth types US keyboard characters only (at most 80); nothing was typed")
        self._check_current(snapshot, target=target)
        x, y = target.tap_point
        self._tap(x, y)
        self._await_keyboard()
        self._type(text)
        if not _shows(self.observe(timeout=5), text):
            raise TransportError("The typed text didn't show on the screen, so Return wasn't pressed")
        if operation == "TYPE_SUBMIT":
            self.helper.request("key", combo="return")
        return None


register_driver(SOURCE, CableBtDriver)

"""iPhone Mirroring, an experimental connection (MOBSTER_EXPERIMENTAL_MIRRORING=1); a refused gesture isn't retried."""

import base64
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import perception
from .drivers import Driver, grounded_target, register_driver
from .state import validate_bundle_id, validate_input_text
from .transport import TransportError
from .wda.gestures import _swipe_points
from .wda.text import NoKeyboardFocus

SOURCE = "mirroring"
ENV_FLAG = "MOBSTER_EXPERIMENTAL_MIRRORING"
SPRINGBOARD = "com.apple.springboard"

# A screen read older than this is checked against a fresh capture before a tap lands on it.
FRESH_SECONDS = 1.0
# The model's picture reuses the last read's frame when it is this recent (it goes with that read); WATCH samples
# faster than FRESH_SECONDS, so anything older is captured again.
PREVIEW_REUSE_SECONDS = .3
# Settling: frames this far apart, until two agree, capped.
SETTLE_POLL_SECONDS = .12
SETTLE_CAP_SECONDS = 2.0
# A scroll moves about this much of the window's height (SWIPE_UP reveals what is below).
SCROLL_FRACTION = .45
# iPhone Mirroring's own keys for iOS's moves (ios.system asks for them by name).
SHORTCUTS = {"home": "cmd+1", "app_switcher": "cmd+2", "spotlight": "cmd+3"}
# The phone's key in the route memory: a Mac mirrors one iPhone.
PHONE_KEY = "mirroring"
# Text longer than this, or with characters that have no US key, goes in through the Mac's pasteboard.
KEYSTROKE_MAX_CHARS = 80
KEYBOARD_WAIT_SECONDS = 2.0
HELPER_TIMEOUT = 15
INPUT_ENV = "MOBSTER_MIRRORING_INPUT"
GESTURES = ("tap", "press", "drag", "scroll", "key", "type")


def enabled():
    """Whether the experimental Mirroring connection is switched on (``MOBSTER_EXPERIMENTAL_MIRRORING=1``)."""
    return os.environ.get(ENV_FLAG, "").strip().casefold() in ("1", "true", "yes", "on")


class MirroringError(TransportError):
    """The helper refused or failed."""

    def __init__(self, message, code="capture", said=()):
        super().__init__(message)
        self.code = code
        self.said = tuple(said)


# What to do about each refusal, said after what happened.
FIXES = {
    "not_running": "Open iPhone Mirroring on this Mac and connect your iPhone, then try again.",
    "no_window": "Open iPhone Mirroring's window and connect your iPhone, then try again.",
    "blocked": "iPhone Mirroring isn't showing your iPhone. Lock your iPhone, keep it nearby, and click Connect or "
               "Try Again in iPhone Mirroring, then try again.",
    "login": "iPhone Mirroring is asking for this Mac's password. Enter it yourself, or set iPhone Mirroring to "
             "authenticate automatically in its settings (cmd+,). Mobster never types passwords.",
    "covered": "Another window covers iPhone Mirroring, and input is set to foreground. Bring it to the front, or "
               "remove MOBSTER_MIRRORING_INPUT, then try again.",
    "background": "iPhone Mirroring can't take input in the background right now. Wait for it to answer, or set "
                  "MOBSTER_MIRRORING_INPUT=foreground to let Mobster bring the window forward for each gesture, "
                  "then try again.",
    "permission": "Give the app you run Mobster from Screen Recording and Accessibility in System Settings > "
                  "Privacy & Security, then try again.",
}


def input_mode():
    """``background`` (default) or ``foreground``, from MOBSTER_MIRRORING_INPUT; a typo is an error, never a mode."""
    value = os.environ.get(INPUT_ENV, "").strip().casefold()
    if value in ("", "background"):
        return "background"
    if value == "foreground":
        return "foreground"
    raise MirroringError(f"{INPUT_ENV} is \"{value}\", so nothing was sent. Set it to background or foreground, or "
                         "remove it, then try again.", "usage")


def fix_for(error):
    """The sentence that says what to do about ``error`` (a MirroringError), or ""."""
    return FIXES.get(getattr(error, "code", ""), "")


def helper_path(name="mirroring"):
    """A helper's executable: the Mac app's own, else built from source (needs Xcode's command line tools)."""
    from . import native_helpers
    native_helpers.require_devices()
    return native_helpers.find(name) or native_helpers.build(name)


class Helper:
    """One helper process, spoken to in JSON lines; a request waits for its own answer, at most ``timeout``."""

    def __init__(self, path=None, spawn=subprocess.Popen):
        self._process = spawn([str(path or helper_path())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._lines = queue.Queue()
        self._lock = threading.Lock()
        self._next = 0
        self._reader = threading.Thread(target=self._read, name="mirroring-helper", daemon=True)
        self._reader.start()

    def _read(self):
        for line in self._process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def request(self, op, timeout=HELPER_TIMEOUT, **fields):
        """The helper's answer to ``op``; MirroringError when it refuses, fails, or doesn't answer in ``timeout``."""
        if op in GESTURES:
            fields = {**fields, "input": input_mode()}
        with self._lock:
            self._next += 1
            ident = self._next
            try:
                self._process.stdin.write(json.dumps({"op": op, "id": ident, **fields}) + "\n")
                self._process.stdin.flush()
            except (OSError, ValueError):
                raise MirroringError("Mobster's Mirroring helper stopped", "capture") from None
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise MirroringError(f"Mobster's Mirroring helper didn't answer {op} in {timeout:g} s", "capture")
                try:
                    line = self._lines.get(timeout=left)
                except queue.Empty:
                    continue
                if line is None:
                    raise MirroringError("Mobster's Mirroring helper stopped", "capture")
                try:
                    answer = json.loads(line)
                except ValueError:
                    continue
                if answer.get("id") not in (ident, None):
                    continue  # a late answer to a request that timed out
                if answer.get("ok") is not True:
                    raise MirroringError(str(answer.get("error") or "iPhone Mirroring refused"),
                                         str(answer.get("code") or "capture"), answer.get("said") or ())
                return answer

    def close(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.stdin.close()
        except Exception:
            pass
        try:
            process.wait(timeout=2)
        except Exception:
            process.kill()
        try:
            process.stdout.close()
        except Exception:
            pass


def screen_from(capture, *, bundle="", read_started=0.0, icons=()):
    """A ``Snapshot`` of one helper capture (perception.snapshot_from, with this driver's source)."""
    return perception.snapshot_from(capture, source=SOURCE, bundle=bundle, read_started=read_started, icons=icons)


def typable(text):
    """Whether ``text`` can go in as US key codes (letters, digits, ASCII punctuation, spaces, line breaks)."""
    return len(text) <= KEYSTROKE_MAX_CHARS and all(c == "\n" or " " <= c <= "~" for c in text)


class MirroringDriver(Driver):
    """The device contract over iPhone Mirroring."""

    capabilities = frozenset({
        "observe_ready", "wait_for_change", "capture_preview", "active_app", "source_reads", "source_seconds",
        "tap_point", "dismiss_at", "long_press", "helper", "opens_by_bundle", "reads_front_app", "system_shortcut",
        "type_focused", "clear_focused"})
    can_type = True
    reports_settling = False
    # ios.system: apps open by name (no bundle id), and the front app can't be read (``current_app`` is a guess).
    opens_by_bundle = False
    reads_front_app = False
    # ios.system's route memory (learned.routes.RouteMemory), set by the runtime; None remembers nothing.
    routes = None

    def __init__(self, helper=None, *, app_names=None, frames_dir=None, sleep=time.sleep, clock=time.monotonic,
                 labeller=None, icon_memory=None):
        self.helper = helper if helper is not None else Helper()
        # Icons: a chat client that names the glyphs Vision can't read (perception.label_icons), and what it learned.
        # Without one, icons stay unlisted and a tap on one is a point tap, which asks.
        self.labeller = labeller
        self.icon_memory = icon_memory if icon_memory is not None else perception.IconMemory()
        self.app_names = dict(app_names or {})
        self._sleep, self._clock = sleep, clock
        self._own_dir = not frames_dir
        self._dir = Path(frames_dir) if frames_dir else Path(tempfile.mkdtemp(prefix="mobster-mirroring-"))
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._frame = self._dir / "frame.jpg"
        self._frame_at = None
        self._window = None
        self.current_app = ""
        # Read counts and time, as WDA's driver keeps them (the run's timing: source_reads, source_seconds).
        self.source_reads = 0
        self.source_seconds = 0.0

    def _capture(self, ocr, timeout):
        started = self._clock()
        result = self.helper.request("capture", timeout=min(timeout, HELPER_TIMEOUT), path=str(self._frame), ocr=ocr)
        if result.get("blocked"):
            code = "login" if result.get("login") else "blocked"
            said = result.get("said") or ()
            raise MirroringError(f"iPhone Mirroring isn't showing your iPhone ({(said or ['blocked'])[0]})", code,
                                 said)
        self._window = result.get("window") or self._window
        self._frame_at = self._clock()
        if ocr:
            self.source_reads += 1
            self.source_seconds += self._clock() - started
        return result, started

    def observe(self, timeout=10):
        result, started = self._capture(True, timeout)
        return screen_from(result, bundle=self.current_app, read_started=started, icons=self._icons(result))

    def _icons(self, result):
        """The named icons on the frame just captured: [(rect, label, kind)] (perception.label_icons)."""
        if self.labeller is None and not self.icon_memory.data:
            return []
        try:
            from PIL import Image
            with Image.open(self._frame) as image:
                image.load()
                rows = [r for r in result.get("rows") or ()
                        if float(r.get("confidence") or 0) >= perception.MIN_CONFIDENCE]
                boxes = perception.icon_candidates(image, rows)
                named = perception.label_icons(self.labeller, image, boxes, bundle=self.current_app,
                                               memory=self.icon_memory)
                named = perception.with_selection(image, named, boxes)
        except (OSError, ValueError):
            return []
        return named

    def _settled_hash(self, timeout, unlike=None, wait_seconds=None):
        cap = self._clock() + min(SETTLE_CAP_SECONDS, timeout)
        give_up = self._clock() + wait_seconds if wait_seconds is not None else None
        previous = None
        while self._clock() < cap:
            result, _ = self._capture(False, max(cap - self._clock(), .5))
            digest = result.get("hash")
            changed = unlike is None or digest != unlike or (give_up is not None and self._clock() >= give_up)
            if previous is not None and digest == previous and changed:
                return True
            previous = digest
            self._sleep(SETTLE_POLL_SECONDS)
        return False

    def observe_ready(self, timeout=10):
        """A read of a screen at rest: frames until two agree (capped for video), then one read with text."""
        self._settled_hash(timeout)
        return self.observe(timeout=timeout)

    def wait_for_change(self, snapshot, timeout=2, wait_seconds=.6, on_settling=None, single_read=False,
                        early=False):
        """The screen after an action."""
        self._settled_hash(timeout, unlike=getattr(snapshot, "image_hash", None), wait_seconds=wait_seconds)
        return self.observe(timeout=max(timeout, 5))

    def capture_preview(self, timeout=3):
        """The model's picture: the frame the last read captured when it is fresh, else a new capture."""
        if (self._frame_at is None or self._clock() - self._frame_at > PREVIEW_REUSE_SECONDS
                or not self._frame.is_file()):
            self._capture(False, timeout)
        data = self._frame.read_bytes()
        return "data:image/jpeg;base64," + base64.b64encode(data).decode()

    def active_app(self, timeout=5):
        if not self.current_app:
            raise TransportError("iPhone Mirroring can't tell which app is in front")
        return self.current_app

    def _check_current(self, snapshot, target=None):
        """Before a tap on an older read, the screen must still show it; unsure means no tap."""
        if self._clock() - getattr(snapshot, "captured_at", 0) <= FRESH_SECONDS:
            return
        result, _ = self._capture(target is not None, 5)
        if result.get("hash") == snapshot.image_hash:
            return
        if target is not None:
            if perception.still_there(target, screen_from(result)):
                return
        raise ValueError("The screen changed since it was read; nothing was tapped")

    def _tap(self, x, y, hold_ms=None):
        fields = {"x": float(x), "y": float(y)}
        if hold_ms is None:
            self.helper.request("tap", **fields)
        else:
            self.helper.request("press", ms=hold_ms, **fields)

    def _opened_from_home(self, target):
        """A tap on an app's name on the Home Screen opens that app: the driver's idea of the front app follows."""
        if self.current_app != SPRINGBOARD:
            return
        wanted = " ".join(target.label.split()).casefold()
        for bundle, name in self._names().items():
            if name.casefold() == wanted:
                self.current_app = bundle
                return

    def _names(self):
        from .catalog import _EXTRA_NAMES, APPS
        names = {app["bundleId"]: app["name"] for app in APPS}
        names.update(_EXTRA_NAMES)
        names.update(self.app_names)
        return names

    def _type(self, text):
        if typable(text):
            self.helper.request("type", text=text, mode="keys", timeout=HELPER_TIMEOUT + len(text) * .1)
        else:
            self.helper.request("type", text=text, mode="paste")

    def _await_keyboard(self):
        deadline = self._clock() + KEYBOARD_WAIT_SECONDS
        while self._clock() < deadline:
            self._sleep(.25)
            if self.observe(timeout=5).keyboard == "visible":
                return True
        return False

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        if operation == "HOME":
            self.helper.request("key", combo="cmd+1")
            self.current_app = SPRINGBOARD
            return {"dispatch_attempted": True}
        if operation in ("VOLUME_UP", "VOLUME_DOWN"):
            raise ValueError("iPhone Mirroring has no volume keys")
        if operation == "LAUNCH_APP":
            return self._launch(target)
        if operation == "TAP":
            grounded_target(operation, target, snapshot)
            self._check_current(snapshot, target=target)
            x, y = target.tap_point
            self._tap(x, y)
            self._opened_from_home(target)
            return None
        if operation in ("TYPE", "TYPE_SUBMIT"):
            grounded_target(operation, target, snapshot)
            validate_input_text(text, multiline=False)
            if getattr(snapshot, "keyboard", "") != "visible":
                self._check_current(snapshot, target=target)
                x, y = target.tap_point
                self._tap(x, y)
                if not self._await_keyboard():
                    raise NoKeyboardFocus("The field did not take keyboard focus; nothing was typed")
            self._type(text)
            if operation == "TYPE_SUBMIT":
                self.helper.request("key", combo="return")
            return None
        if operation == "SUBMIT":
            grounded_target(operation, target, snapshot)
            self.helper.request("key", combo="return")
            return None
        if operation in ("SWIPE_UP", "SWIPE_DOWN"):
            height = float((self._window or {}).get("h") or 700)
            dy = height * SCROLL_FRACTION * (1 if operation == "SWIPE_UP" else -1)
            self.helper.request("scroll", x=.5, y=.5, dy=dy, dx=0)
            return None
        if operation in ("SWIPE_LEFT", "SWIPE_RIGHT"):
            aimed = None
            if target is not None:
                grounded_target(operation, target, snapshot)
                aimed = target.rect
            x1, y1, x2, y2 = _swipe_points(operation, 1.0, 1.0, aimed)
            self.helper.request("drag", x1=x1, y1=y1, x2=x2, y2=y2, ms=250)
            return None
        raise ValueError(f"iPhone Mirroring can't do {operation}")

    def _launch(self, bundle):
        """Open ``bundle`` by its name, the way a person would (ios.system.open_app)."""
        from .ios import system
        bundle = validate_bundle_id(bundle)
        name = self._names().get(bundle)
        if not name:
            raise ValueError(f"iPhone Mirroring opens apps by name, and {bundle} has none here")
        outcome = system.open_app(self, bundle, name, routes=self.routes, phone=PHONE_KEY)
        if not outcome.ok:
            raise TransportError(outcome.detail or f"Mobster couldn't open {name}")
        self.current_app = bundle
        return {"route": outcome.route}

    # ios.system calls these by name.

    def system_shortcut(self, name):
        """Send iPhone Mirroring's key for an iOS move ("home", "spotlight", "app_switcher"); False when it has none."""
        combo = SHORTCUTS.get(name)
        if combo is None:
            return False
        self.helper.request("key", combo=combo)
        self.current_app = SPRINGBOARD
        return True

    def type_focused(self, text):
        """Type into the field that has the keyboard."""
        validate_input_text(text, multiline=False)
        self._type(text)

    def clear_focused(self):
        """Empty the field that has the keyboard: select all, then delete."""
        self.helper.request("key", combo="cmd+a")
        self.helper.request("key", combo="delete")

    def tap_point(self, x, y, snapshot, timeout=10, purpose=None):
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError("The point is outside the screen")
        self._check_current(snapshot)
        self._tap(x, y)

    dismiss_at = tap_point

    def transfer_to_phone(self, paths, at=None):
        """Files from the Mac onto the phone by drag and drop (module function transfer_to_phone)."""
        return transfer_to_phone(paths, at=at)

    def transfer_from_phone(self, at, dest_dir):
        """An item off the phone into ``dest_dir`` by drag and drop (module function transfer_from_phone)."""
        return transfer_from_phone(at, dest_dir)

    def long_press(self, target, snapshot, timeout=10):
        grounded_target("TAP", target, snapshot)
        self._check_current(snapshot, target=target)
        x, y = target.tap_point
        self._tap(x, y, hold_ms=1500)

    def close(self):
        helper, self.helper = self.helper, None
        if helper is None:
            return
        try:
            helper.request("restore", timeout=3)
        except Exception:
            pass
        helper.close()
        try:
            self._frame.unlink(missing_ok=True)
            if self._own_dir:
                self._dir.rmdir()
        except OSError:
            pass


register_driver(SOURCE, MirroringDriver)


def stream_command(fps, detail):
    """The helper's live-stream argv (``mirroring stream FPS MAX_WIDTH QUALITY``) for a live-view quality."""
    from .wda.frame_source import DETAIL_QUALITY, DETAIL_WIDTH
    return [str(helper_path()), "stream", str(int(fps)), str(DETAIL_WIDTH[detail]), str(DETAIL_QUALITY[detail])]


def live_video(**kwargs):
    """The live view's frame source for iPhone Mirroring."""
    from .wda.frame_source import ProcessFrameSource
    return ProcessFrameSource(stream_command, source="mirroring_sck", **kwargs)


class MirroringControl:
    """The person's gestures from the live view, through the helper: nothing is sent while the phone is covered."""
    def __init__(self, driver_factory=None):
        self.driver_factory = driver_factory or MirroringDriver
        self.driver = None
        self.lock = threading.Lock()

    def _driver(self):
        if self.driver is None:
            self.driver = self.driver_factory()
        return self.driver

    def perform(self, body, teach=None):
        from .manual_control import parse_command
        command = parse_command(body)
        with self.lock:
            driver = self._driver()
            before = teach.read(driver) if teach is not None else None
            try:
                self._dispatch(driver, command)
            except Exception:
                self.close()
                raise
            if teach is not None:
                teach.record(command, before)
        return command["action"]

    def observe(self, timeout=8):
        with self.lock:
            return self._driver().observe(timeout=timeout)

    @staticmethod
    def _dispatch(driver, command):
        helper = driver.helper
        kind = command["action"]
        if kind == "button":
            if command["name"] != "home":
                raise ValueError("iPhone Mirroring has no volume keys")
            helper.request("key", combo="cmd+1")
            driver.current_app = SPRINGBOARD
        elif kind == "type":
            driver._type(command["text"])
            if command["submit"]:
                helper.request("key", combo="return")
        elif kind == "tap":
            point = {"x": command["x"], "y": command["y"]}
            if command["holdMs"] >= 400:
                helper.request("press", ms=command["holdMs"], **point)
            else:
                helper.request("tap", **point)
        else:
            helper.request("drag", x1=command["x1"], y1=command["y1"], x2=command["x2"], y2=command["y2"],
                           ms=command["durationMs"])

    def close(self):
        driver, self.driver = self.driver, None
        if driver is not None:
            driver.close()


TRANSFER_SECONDS = 75


def _point(at):
    """Screen fractions for ``at``: an element (its tap point), (x, y), or None for the middle of the screen."""
    if at is None:
        return .5, .5
    if hasattr(at, "tap_point"):
        return at.tap_point
    x, y = at
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise ValueError("The point is outside the screen")
    return float(x), float(y)


def _transfer(args, run=subprocess.run):
    try:
        done = run([str(helper_path())] + args, capture_output=True, text=True, timeout=TRANSFER_SECONDS)
    except subprocess.TimeoutExpired:
        raise MirroringError("The transfer didn't finish in time; check the phone for what arrived",
                             "refused") from None
    lines = [line for line in (done.stdout or "").splitlines() if line.strip()]
    try:
        answer = json.loads(lines[-1]) if lines else {}
    except ValueError:
        answer = {}
    if answer.get("ok") is not True:
        raise MirroringError(str(answer.get("error") or "The transfer failed"), str(answer.get("code") or "refused"))
    return answer


def transfer_to_phone(paths, *, at=None, run=subprocess.run):
    """Drop Mac files on the phone at ``at``; blocked or covered sends nothing; the drop isn't read back."""
    files = [Path(p).expanduser() for p in paths]
    if not files:
        raise ValueError("No files to send")
    for file in files:
        if not file.is_file():
            raise ValueError(f"No file at {file.name}")
    x, y = _point(at)
    return _transfer(["drop", f"{x:.4f}", f"{y:.4f}"] + [str(f.resolve()) for f in files], run=run)


def transfer_from_phone(at, dest_dir, *, run=subprocess.run):
    dest = Path(dest_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    x, y = _point(at)
    answer = _transfer(["drag-out", f"{x:.4f}", f"{y:.4f}", str(dest.resolve())], run=run)
    return [str(p) for p in answer.get("received") or ()]


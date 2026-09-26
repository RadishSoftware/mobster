"""Direct control of the USB iPhone from the dashboard's live view.

The user taps, drags and types on the picture of their phone. Coordinates
arrive normalized to the screen (0..1) because the picture is scaled; they are
mapped to WDA points here with the screen size WDA reports. Every gesture is a
W3C pointer action, so a drag follows the user's own speed instead of the
agent's fixed swipe.

The server refuses control while a task holds the phone and while live actions
are off; this module only validates and dispatches.
"""

import math
import threading
import time

from .drivers import WDA, WDA_TAP_HOLD_MS
from .state import validate_input_text

BUTTONS = {"home": "home", "volume_up": "volumeUp", "volume_down": "volumeDown"}
MAX_HOLD_MS = 3000
SWIPE_MS = (40, 3000)
SIZE_TTL_SECONDS = 30


def _unit(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be a number from 0 to 1")
    return float(value)


def parse_command(body):
    """A validated gesture, or ValueError. Unknown fields are refused, never ignored."""
    if not isinstance(body, dict):
        raise ValueError("A control command must be an object")
    kind = body.get("action")
    allowed = {"tap": {"x", "y", "holdMs"}, "swipe": {"x1", "y1", "x2", "y2", "durationMs"},
               "type": {"text", "submit"}, "button": {"name"}}
    if kind not in allowed:
        raise ValueError("action must be tap, swipe, type or button")
    extra = set(body) - allowed[kind] - {"action"}
    if extra:
        raise ValueError(f"Unsupported field: {sorted(extra)[0]}")
    if kind == "tap":
        hold = body.get("holdMs", WDA_TAP_HOLD_MS)
        if type(hold) is not int or not 0 <= hold <= MAX_HOLD_MS:
            raise ValueError(f"holdMs must be 0 to {MAX_HOLD_MS}")
        return {"action": "tap", "x": _unit(body.get("x"), "x"), "y": _unit(body.get("y"), "y"), "holdMs": hold}
    if kind == "swipe":
        duration = body.get("durationMs", 250)
        if type(duration) is not int or not SWIPE_MS[0] <= duration <= SWIPE_MS[1]:
            raise ValueError(f"durationMs must be {SWIPE_MS[0]} to {SWIPE_MS[1]}")
        return {"action": "swipe", **{k: _unit(body.get(k), k) for k in ("x1", "y1", "x2", "y2")},
                "durationMs": duration}
    if kind == "type":
        submit = body.get("submit", False)
        if type(submit) is not bool:
            raise ValueError("submit must be true or false")
        return {"action": "type", "text": validate_input_text(body.get("text")), "submit": submit}
    if body.get("name") not in BUTTONS:
        raise ValueError("name must be home, volume_up or volume_down")
    return {"action": "button", "name": body["name"]}


class ManualControl:
    def __init__(self, wda_url, session_provider):
        self.wda_url, self.session_provider = wda_url, session_provider
        self.lock = threading.Lock()
        self.driver = None
        self.session = None
        self.size = None  # (width, height, read_at)

    def _driver(self):
        session = self.session_provider()
        if not session:
            raise ConnectionError("Your iPhone is not connected")
        if self.driver is None or session != self.session:
            self.close()
            self.driver, self.session, self.size = WDA(self.wda_url, session), session, None
        return self.driver

    def _points(self, driver):
        """The screen in points. /wda/screen measures through SpringBoard; /window/size
        goes through the "active" app, which can be a transient system overlay (measured
        on iOS 26: an 8-10 s stall, then a stale-element error), so it is only a fallback."""
        if self.size is None or time.monotonic() - self.size[2] > SIZE_TTL_SECONDS:
            size = None
            try:
                screen = WDA.response_value(driver.http.request("GET", "/wda/screen", None, 3))
                size = screen.get("screenSize") if isinstance(screen, dict) else None
            except Exception:
                size = None
            if not self._valid(size):
                size = driver.call("GET", "/window/size", timeout=3)
            if not self._valid(size):
                raise ConnectionError("Your iPhone did not report its screen size")
            self.size = (float(size["width"]), float(size["height"]), time.monotonic())
        return self.size[:2]

    @staticmethod
    def _valid(size):
        return isinstance(size, dict) and all(
            type(size.get(k)) in (int, float) and math.isfinite(size[k]) and size[k] > 0 for k in ("width", "height"))

    def perform(self, body):
        command = parse_command(body)
        with self.lock:
            driver = self._driver()
            try:
                self._dispatch(driver, command)
            except Exception:
                # A dropped session is re-resolved on the next gesture.
                self.close()
                raise
        return command["action"]

    def _dispatch(self, driver, command):
        kind = command["action"]
        if kind == "button":
            driver.call("POST", "/wda/pressButton", {"name": BUTTONS[command["name"]]}, timeout=5)
            return
        if kind == "type":
            keys = [command["text"], "\n"] if command["submit"] else [command["text"]]
            driver.call("POST", "/wda/keys", {"value": keys}, timeout=10)
            return
        width, height = self._points(driver)
        if kind == "tap":
            x, y = round(command["x"] * width), round(command["y"] * height)
            steps = [{"type": "pointerMove", "duration": 0, "x": x, "y": y}, {"type": "pointerDown"},
                     {"type": "pause", "duration": command["holdMs"]}, {"type": "pointerUp"}]
        else:
            steps = [{"type": "pointerMove", "duration": 0, "x": round(command["x1"] * width),
                      "y": round(command["y1"] * height)}, {"type": "pointerDown"},
                     {"type": "pointerMove", "duration": command["durationMs"], "x": round(command["x2"] * width),
                      "y": round(command["y2"] * height)}, {"type": "pointerUp"}]
        driver.call("POST", "/actions", {"actions": [{"type": "pointer", "id": "finger",
                                                      "parameters": {"pointerType": "touch"}, "actions": steps}]},
                    timeout=10 + (command.get("holdMs", 0) + command.get("durationMs", 0)) / 1000)

    def close(self):
        driver, self.driver, self.session = self.driver, None, None
        if driver is not None:
            driver.close()

"""The phone as the screenshot baselines see it: WDA screenshots plus raw actions.

Baselines get the SAME primitive gestures Mobster's WDA driver uses (a W3C
tap with a 40 ms hold, XCTest's ``dragfromtoforduration`` for drags, ``/wda/keys``
for typing, ``pressButton`` for Home, ``apps/activate`` for opening an app), so
a speed difference is the agent's, not the gesture's.

Device access rules (the phone is the user's): every call has a short timeout
because WDA serves one request at a time and a stuck read can block it for
minutes; ``health()`` runs before every attempt, and a locked or unresponsive
phone stops the run. Nothing here POSTs to the Mobster app (port 8765) or
restarts the WDA runner.
"""

import base64
from dataclasses import dataclass
import io
import struct
import time

from ..drivers import WDA, WDA_SWIPE_SECONDS, WDA_TAP_HOLD_MS, _swipe_points, resolve_wda_session
from ..transport import HTTP, TransportError

SCREENSHOT_TIMEOUT = 6
ACTION_TIMEOUT = 10
HEALTH_TIMEOUT = 3


@dataclass
class Health:
    ok: bool
    locked: bool | None = None
    session: str | None = None
    reason: str = ""
    ms: float = 0.0


def health(url, timeout=HEALTH_TIMEOUT):
    """GET-only: WDA status, session and lock state. Never unlocks, never creates a session."""
    started = time.monotonic()
    client = HTTP(url)
    try:
        try:
            status = client.request("GET", "/status", timeout=timeout)
        except Exception as error:
            return Health(False, reason=f"WDA unreachable: {type(error).__name__}")
        session = status.get("sessionId") if isinstance(status, dict) else None
        value = status.get("value") if isinstance(status, dict) else None
        if isinstance(value, dict) and value.get("ready") is False:
            return Health(False, session=session, reason="WDA reports not ready")
        try:
            locked = client.request("GET", "/wda/locked", timeout=timeout).get("value")
        except Exception as error:
            return Health(False, session=session, reason=f"lock state unavailable: {type(error).__name__}")
        if locked is True:
            return Health(False, locked=True, session=session, reason="phone is locked")
        if not session:
            return Health(False, locked=bool(locked), reason="WDA has no active session")
        return Health(True, locked=False, session=session, ms=(time.monotonic() - started) * 1000)
    finally:
        client.close()


def png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])


def downscale_png(data, long_side=1280):
    """Bound upload size (and nothing else) for model input; coordinates stay normalized."""
    from PIL import Image
    image = Image.open(io.BytesIO(data))
    width, height = image.size
    scale = min(1.0, long_side / max(width, height))
    if scale < 1.0:
        image = image.convert("RGB").resize((round(width * scale), round(height * scale)), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=False)
    return out.getvalue(), image.size


class Phone:
    """Raw device I/O on the current WDA session. Points, never pixels, for gestures."""

    def __init__(self, url, session=None):
        self.url = url.rstrip("/")
        self.session = session or resolve_wda_session(self.url, create=False)
        if not self.session:
            raise TransportError("WDA has no active session")
        self.driver = WDA(self.url, self.session)
        self.driver.configure()
        self._size = None

    # -------------------------------------------------------------- reads
    def observe(self, timeout=8):
        """One AX read (same fast source Mobster reads)."""
        return self.driver.observe(timeout=timeout)

    def screenshot(self, timeout=SCREENSHOT_TIMEOUT):
        value = self.driver.call("GET", "/screenshot", timeout=timeout)
        if not isinstance(value, str) or not value:
            raise TransportError("WDA screenshot unavailable")
        data = base64.b64decode(value)
        return data, png_size(data)

    def size(self):
        """Screen size in points (fixed per phone)."""
        if self._size is None:
            known = WDA._screen_sizes.get(self.url)
            if known:
                self._size = tuple(known)
            else:
                snapshot = self.observe()
                self._size = (snapshot.width, snapshot.height)
        return self._size

    def foreground(self, timeout=4):
        try:
            return self.driver.active_app(timeout=timeout)
        except Exception:
            return ""

    # -------------------------------------------------------------- gestures
    def tap(self, x, y, hold_ms=WDA_TAP_HOLD_MS):
        self.driver._stable = None
        self.driver.call("POST", "/actions", {"actions": [{
            "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
            "actions": [{"type": "pointerMove", "duration": 0, "x": round(x), "y": round(y)},
                        {"type": "pointerDown"}, {"type": "pause", "duration": int(hold_ms)},
                        {"type": "pointerUp"}]}]}, timeout=ACTION_TIMEOUT + hold_ms / 1000)

    def long_press(self, x, y, seconds=2):
        self.tap(x, y, hold_ms=max(0.5, min(float(seconds), 5)) * 1000)

    def drag(self, x1, y1, x2, y2, duration=WDA_SWIPE_SECONDS):
        self.driver.call("POST", "/wda/dragfromtoforduration", {
            "fromX": x1, "fromY": y1, "toX": x2, "toY": y2, "duration": duration}, timeout=ACTION_TIMEOUT)

    def swipe(self, operation, region=None):
        """The same scroll gesture Mobster's driver issues, optionally inside an element's frame."""
        width, height = self.size()
        x1, y1, x2, y2 = _swipe_points(operation, width, height)
        if region:
            rx, ry, rw, rh = region
            cx, cy = (rx + rw / 2) * width, (ry + rh / 2) * height
            half_w, half_h = rw * width * .25, rh * height * .25
            x1, x2 = (cx, cx) if x1 == x2 else (cx + half_w, cx - half_w) if x1 > x2 else (cx - half_w, cx + half_w)
            y1, y2 = (cy, cy) if y1 == y2 else (cy + half_h, cy - half_h) if y1 > y2 else (cy - half_h, cy + half_h)
        self.drag(x1, y1, x2, y2)

    def back_gesture(self):
        """iOS has no system Back key: the interactive edge swipe that navigation stacks honour."""
        width, height = self.size()
        self.driver.call("POST", "/actions", {"actions": [{
            "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
            "actions": [{"type": "pointerMove", "duration": 0, "x": 2, "y": round(height * .5)},
                        {"type": "pointerDown"},
                        {"type": "pointerMove", "duration": 250, "x": round(width * .7), "y": round(height * .5)},
                        {"type": "pointerUp"}]}]}, timeout=ACTION_TIMEOUT)

    def type_text(self, text, enter=False):
        keys = [text, "\n"] if enter else [text]
        self.driver.call("POST", "/wda/keys", {"value": keys}, timeout=ACTION_TIMEOUT)

    def key(self, value):
        self.driver.call("POST", "/wda/keys", {"value": [value]}, timeout=ACTION_TIMEOUT)

    def home(self):
        self.driver.call("POST", "/wda/pressButton", {"name": "home"}, timeout=ACTION_TIMEOUT)

    def press_button(self, name):
        self.driver.call("POST", "/wda/pressButton", {"name": name}, timeout=ACTION_TIMEOUT)

    def activate(self, bundle_id):
        self.driver.call("POST", "/wda/apps/activate", {"bundleId": bundle_id}, timeout=15)

    def wait_keyboard(self, seconds=2.0):
        """True once a keyboard is up (same signal Mobster's driver waits for)."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                snapshot = self.observe(timeout=4)
            except Exception:
                return False
            if any("SUBMIT" in element.actions for element in snapshot.elements):
                return True
        return False

    def close(self):
        self.driver.close()

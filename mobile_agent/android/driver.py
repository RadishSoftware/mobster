"""The Android driver: reads the screen with uiautomator, acts with `adb shell input`, over one adb connection.

It hands the agent and verify the same Snapshot and AXTree an iPhone does. Nothing here talks to the Appium
UIAutomator2 server yet: a dump costs about a second where the server's tree costs a few hundred ms, and the server's
APKs need a pinned download (see docs/android.mdx).
"""

import time

from ..drivers import Driver, register_driver
from ..state import HOST_KEY_OPERATIONS, TEXT_OPERATIONS, from_wda_root
from ..transport import Deadline, TransportError
from ..wda.gestures import _swipe_points
from ..wda.guards import DriverRejection, grounded_target
from . import adb as adb_module
from .tree import to_wda

FRESH_SECONDS = 1.0
SWIPE_MILLIS = 350
KEYCODES = {"HOME": 3, "BACK": 4, "VOLUME_UP": 24, "VOLUME_DOWN": 25, "SUBMIT": 66}
SOURCE = "android"


class AndroidDriver(Driver):
    capabilities = frozenset({"active_app", "capture_preview", "effects", "evidence_source", "launch", "long_press",
                              "open_url", "tap_point"})
    # Every method that changes the device: a verify run journals each call (verify/actions.py, guard_driver).
    effects = ("execute", "launch", "open_url", "tap_point", "long_press")
    can_type = True

    def __init__(self, serial, adb=None):
        self.adb = adb or adb_module.Adb(serial)
        self.serial = serial
        self._screen = None
        self.last_image = None

    def _guard(self, call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except adb_module.AdbError as error:
            raise TransportError(str(error)) from None

    def screen(self):
        """(width px, height px, dpi), read once: a rotation or a density change needs a new driver."""
        if self._screen is None:
            self._screen = self._guard(self.adb.screen_px)
        return self._screen

    def evidence_source(self, timeout=15):
        """The tree as WDA-shaped XML, for verify's evidence read."""
        width, height, density = self.screen()
        xml = self._guard(self.adb.dump_ui, timeout)
        try:
            return to_wda(xml, width, height, density)
        except ValueError as error:
            raise TransportError(f"The Android accessibility tree could not be read: {error}") from None

    def observe(self, timeout=10):
        from ..state import _parse_wda
        snapshot = from_wda_root(_parse_wda(self.evidence_source(timeout)))
        snapshot.source = SOURCE
        return snapshot

    def active_app(self, timeout=5):
        return self._guard(self.adb.front_package)

    def capture_preview(self, timeout=3):
        return self._guard(self.adb.screenshot_data_uri, timeout)

    def launch(self, package, timeout=20):
        self._guard(self.adb.launch, package)
        return self.observe(timeout)

    def open_url(self, url):
        self._guard(self.adb.open_url, url)

    def tap_point(self, x, y):
        """Tap a screen point given as fractions of the screen."""
        width, height, _ = self.screen()
        self._guard(self.adb.tap, x * width, y * height)

    def long_press(self, x, y):
        width, height, _ = self.screen()
        self._guard(self.adb.long_press, x * width, y * height)

    def _check_current(self, snapshot, deadline):
        if time.monotonic() - snapshot.captured_at <= FRESH_SECONDS:
            return
        if self.observe(timeout=deadline.remaining()).fingerprint != snapshot.fingerprint:
            raise DriverRejection("stale_revision")

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        deadline = Deadline(timeout)
        width, height, _ = self.screen()
        if operation in HOST_KEY_OPERATIONS or operation == "BACK":
            self._guard(self.adb.key, KEYCODES[operation])
            return {"dispatch_attempted": True}
        if operation == "LAUNCH_APP":
            self._guard(self.adb.launch, target)
            return {}
        if operation == "SUBMIT":
            grounded_target(operation, target, snapshot)
            if "SUBMIT" not in target.actions:
                raise ValueError("SUBMIT needs a keyboard and an editable field")
            self._check_current(snapshot, deadline)
            self._guard(self.adb.key, KEYCODES["SUBMIT"])
        elif operation == "TAP":
            grounded_target(operation, target, snapshot)
            self._check_current(snapshot, deadline)
            x, y = target.tap_point
            self._guard(self.adb.tap, x * width, y * height)
        elif operation in TEXT_OPERATIONS:
            grounded_target(operation, target, snapshot)
            self._check_current(snapshot, deadline)
            if not isinstance(text, str) or not text:
                raise ValueError("TYPE requires text")
            x, y = target.tap_point
            self._guard(self.adb.tap, x * width, y * height)
            self._guard(self.adb.text, text)
            if operation == "TYPE_SUBMIT":
                self._guard(self.adb.key, KEYCODES["SUBMIT"])
        elif operation in {"SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT"}:
            aimed = None
            if target is not None and operation in {"SWIPE_LEFT", "SWIPE_RIGHT"}:
                grounded_target(operation, target, snapshot)
                aimed = target.rect
            x1, y1, x2, y2 = _swipe_points(operation, width, height, aimed)
            self._guard(self.adb.swipe, x1, y1, x2, y2, SWIPE_MILLIS)
        else:
            raise ValueError("Unsupported Android operation")
        return {}

    def close(self):
        """Nothing is held open: adb runs one command per call."""


register_driver("android", AndroidDriver)

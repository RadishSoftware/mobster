"""The runtime for the experimental Cable + Bluetooth connection: ``MirroringRuntime`` with cable_bt.py's driver."""

import os
import threading

from . import mirroring_runtime
from .wda.frame_source import FrameRelay

CONFIG_URL = "cable-bt://iphone"
UDID_ENV = "MOBSTER_CABLE_BT_UDID"


def wanted(config=None):
    from . import cable_bt
    return cable_bt.enabled() and not getattr(config, "manage_device", False)


def message_for(state):
    """What the phone's state means for a task, then what to do; None when it can be driven."""
    from .cable_bt import FIXES
    kind = state.get("state")
    if kind == "ready":
        return None
    said = (state.get("said") or ["Mobster can't drive the iPhone over Cable + Bluetooth"])[0].rstrip(".")
    fix = state.get("fix") or FIXES.get(state.get("code") or "", "")
    return f"{said}. {fix}".strip()


class CableVideo(FrameRelay):
    """The live view and tile from the capture helper the driver shares, so watching costs no second session."""

    kind = "cable"

    def __init__(self, phone, **kwargs):
        self._phone = phone  # a callable: the runtime's open phone
        super().__init__(source="cable_video", **kwargs)

    def pull(self, width, quality, after):
        import base64
        answer = self._phone().screen.request("frame", timeout=5, width=width, quality=quality, after=after)
        if answer.get("same"):
            return None
        return base64.b64decode(answer["jpeg"]), answer["key"]


class Borrowed:
    """The runtime's phone lent to one run's driver: closing the driver leaves the phone's video running."""

    def __init__(self, phone):
        self.phone = phone

    def request(self, op, timeout=15, **fields):
        return self.phone.request(op, timeout=timeout, **fields)

    def close(self):
        pass


class CableBtRuntime(mirroring_runtime.MirroringRuntime):
    """``server.Runtime`` over Cable + Bluetooth."""

    connection = "cable_bt"
    subject = "the iPhone"

    def __init__(self, config, open_phone=None):
        self._phone = None
        self._phone_lock = threading.Lock()
        self._open_phone = open_phone
        super().__init__(config)

    # kept open across tasks: opening the capture device takes about 1.6 s, and a measured pointer stays measured
    def phone(self):
        """The phone, opened on first use and kept open."""
        with self._phone_lock:
            if self._phone is None:
                opener = self._open_phone
                if opener is None:
                    from .cable_bt import open_phone as opener
                self._phone = opener(udid=os.environ.get(UDID_ENV, "").strip() or None)
            return self._phone

    def mirroring_state(self):
        """The phone's state (cable_bt.Phone.state), read at most every STATUS_SECONDS."""
        import time
        with self._status_lock:
            if time.monotonic() - self._status_at < mirroring_runtime.STATUS_SECONDS:
                return self._status
            try:
                self._status = self.phone().state()
            except Exception as error:  # noqa: BLE001 -- reported as not ready, with what to do
                from .cable_bt import fix_for
                self._status = {"state": "unreadable", "said": [str(error)], "fix": fix_for(error)}
            self._status_at = time.monotonic()
            return self._status

    def message_for(self, state):
        return message_for(state)

    def fix_for(self, error):
        from .cable_bt import fix_for
        return fix_for(error)

    def make_video(self, config):
        """The live view and the grid's tile: the cable video, shared with the driver (frame_source.FrameRelay)."""
        try:
            quality = self.video_quality()
        except Exception:  # noqa: BLE001 -- the default until Settings says otherwise
            from .wda.video import DEFAULT_QUALITY as quality
        return CableVideo(self.phone, quality=quality)

    def make_control(self):
        """The person's taps on the live view: the phone's own pointer, lent for one gesture's driver."""
        from .cable_bt import CableBtDriver
        from .mirroring import MirroringControl
        return MirroringControl(driver_factory=lambda: CableBtDriver(Borrowed(self.phone())))

    def build_driver(self, names):
        from .compose import build_target_driver
        return build_target_driver(connection="cable_bt", app_names=names, helper=Borrowed(self.phone()),
                                   labeller=self.icon_labeller(), icon_memory=self.icon_memory())

    def close(self, *args, **kwargs):
        try:
            return super().close(*args, **kwargs)
        finally:
            phone, self._phone = self._phone, None
            if phone is not None:
                phone.close()

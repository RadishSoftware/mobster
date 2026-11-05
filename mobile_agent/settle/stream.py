"""A stream of the screen, used only to say "nothing moved", never to read it.

The accessibility read stays the observation. An observer answers one question: did the pixels stay still over a
window? Predictive settle (``predict.py``) asks it before it accepts a read it took after a learned wait.

* ``WdaMjpegObserver``: the FrameClock's view of WDA's MJPEG stream (``wda.video.WdaVideo``). WDA serves that
  stream for iOS Simulators and for a phone over its own port; the FrameClock already decodes it at ~25 fps.
* ``PhoneScreenStream``: the phone's screen over the cable (pymobiledevice3's screen-capture service or CoreDevice).
  A stub. Nothing in this repository has run it on a phone.
"""

from typing import Protocol


class StreamObserver(Protocol):
    def still_between(self, start: float, end: float) -> bool | None:
        """True when no tile moved from ``start`` to ``end`` (time.monotonic seconds), False when one did, None
        when the stream cannot say (stale, too few frames, a busy screen)."""


class WdaMjpegObserver:
    """The FrameClock watch of one action, as a StreamObserver."""

    def __init__(self, clock, watch):
        self.clock, self.watch = clock, watch

    def still_between(self, start, end):
        if not getattr(self.watch, "usable", False):
            return None
        try:
            return bool(self.clock.quiet_between(self.watch, start, end))
        except Exception:
            return None


class PhoneScreenStream:
    """Unverified, 26 Oct 2026: written from pymobiledevice3's API surface, never run against a phone (no test
    phone exists). It reports itself unavailable, so a caller falls back to the WDA stream or to AX polling."""

    UNVERIFIED = "unverified 26 Oct 2026: no phone has run this path"

    def still_between(self, start, end):
        return None

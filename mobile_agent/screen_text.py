"""The image head of the two-headed check: what the screen's pixels say inside a control's frame.

Before a tap that would go ahead without asking, Mobster's agent compares the control's accessibility label with the
text Apple Vision reads in the same frame of the same screen (frontier ``_two_heads``). When the pixels name an act
the label doesn't (the tree says "Done" or "Button", the screen shows "Delete"), the person is asked first.

The text is read from the live stream's frame of the screen the tree was read from (frontier.video_frame, free),
while the model decides (``prefetch``), so the check adds no wait to a step in the usual case. No frame, no helper,
or no answer within ``wait`` seconds: the reader says None (unknown) and the tree's reading stands.

``reader()``: a ScreenText when the helper is installed (the Mac app ships it as Contents/Resources/screen-text;
``mobster`` from source builds it with ``native_helpers.build("screen_text")``), else None.
"""

import json
import subprocess
import threading

PAD = .01           # a frame's margin: Vision's boxes are a little smaller than the control
KEEP = 4
RUN_SECONDS = 3.0


class ScreenText:
    def __init__(self, helper, run=None):
        self.helper = str(helper)
        self._run = run or self._helper
        self._lock = threading.Lock()
        self._screens = {}
        self._order = []

    @staticmethod
    def _key(snapshot):
        return (getattr(snapshot, "content_fingerprint", None), getattr(snapshot, "read_started", None),
                id(snapshot))

    def _helper(self, data):
        done = subprocess.run([self.helper], input=data, capture_output=True, timeout=RUN_SECONDS, check=True)
        return json.loads(done.stdout or b"[]")

    def read(self, data):
        try:
            return [row for row in self._run(data) if isinstance(row, dict)]
        except Exception:  # noqa: BLE001 -- unknown: the caller keeps what it has
            return None

    def prefetch(self, driver, snapshot):
        """Start reading ``snapshot``'s frame in the background (once per screen)."""
        key = self._key(snapshot)
        with self._lock:
            if key in self._screens:
                return
            entry = {"done": threading.Event(), "rows": None}
            self._screens[key] = entry
            self._order.append(key)
            while len(self._order) > KEEP:
                self._screens.pop(self._order.pop(0), None)

        def work():
            try:
                from .frontier.screen import video_frame
                frame = video_frame(driver, since=getattr(snapshot, "read_started", None))
                if frame is not None:
                    entry["rows"] = [r for r in self._run(frame) if isinstance(r, dict)]
            except Exception:  # noqa: BLE001 -- unknown: the tree's reading stands
                entry["rows"] = None
            finally:
                entry["done"].set()
        threading.Thread(target=work, daemon=True, name="mobster-screen-text").start()

    def __call__(self, driver, element, snapshot, wait=.15):
        """The text the pixels show inside ``element``'s frame ("" when they show none), or None when unknown."""
        self.prefetch(driver, snapshot)
        with self._lock:
            entry = self._screens.get(self._key(snapshot))
        if entry is None or not entry["done"].wait(wait) or entry["rows"] is None:
            return None
        x, y, w, h = element.rect
        inside = []
        for row in entry["rows"]:
            rx, ry, rw, rh = (row.get("rect") or (0, 0, 0, 0))[:4]
            cx, cy = rx + rw / 2, ry + rh / 2
            if x - PAD <= cx <= x + w + PAD and y - PAD <= cy <= y + h + PAD:
                inside.append(str(row.get("text") or ""))
        return " ".join(" ".join(inside).split())


def reader():
    """A ScreenText on the installed helper, or None."""
    try:
        from . import native_helpers
        helper = native_helpers.find("screen-text")
    except Exception:  # noqa: BLE001
        helper = None
    return ScreenText(helper) if helper is not None else None


def read_image(data):
    """The text Vision reads in one image (bytes), as ScreenText.read gives it; None without the helper."""
    found = reader()
    return found.read(data) if found is not None else None

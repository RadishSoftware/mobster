"""FrameClock: the phone's MJPEG stream as a clock for "the action took effect"
and "the screen is still".

WDA streams the screen as MJPEG at ~30 fps (``wda_video.WdaVideo``). Reading
that stream costs almost nothing, while every AX read costs 130-290 ms and
proving a screen settled from AX alone needs a 0.5 s quiet period. This module
decodes each frame at reduced size in greyscale (JPEG DCT draft mode, ~0.25 ms
a frame), compares 16-px tiles, and answers the questions the WDA driver
asks around an action:

* ``mark()`` before dispatch, then ``wait_effect(token)``: the first frame that
  differs from the pre-action frame outside regions that were already busy.
* ``quiet_between(token, start, end)`` / ``still_for()``: no changed tile
  (outside constantly busy tiles: video, spinners) in a window, or since a time
  (the settle rules in WDA.wait_for_change / observe_ready); ``wait_stable`` is
  the blocking form.
* ``displacement(a, b)``: vertical/horizontal content shift between two
  frames (scroll odometry), by row/column-profile matching.

It never decides anything on its own: the driver still takes an AX read and
compares structure before it accepts a screen, and every question returns
``None`` when the stream is stale, so the caller falls back to AX polling.

Modes (``MOBSTER_FRAME_CLOCK``): ``off``; ``shadow`` computes and logs the
decisions next to today's AX-only settle without changing behavior; ``on``
uses them. Unset means ``DEFAULT_MODE``.
"""

import io
import json
import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field

try:  # Optional at import time: without Pillow the clock reports itself unavailable.
    from PIL import Image, ImageChops, ImageStat
except ImportError:  # pragma: no cover - exercised only in stripped installs
    Image = ImageChops = ImageStat = None

log = logging.getLogger("mobster.frameclock")

ENV = "MOBSTER_FRAME_CLOCK"
MODES = ("off", "shadow", "on")
# "on" since the live FrameClock validation of 23 Sep 2026:
# 0/32 premature settles vs 0/32 for AX alone, AX read latency unchanged with
# the stream up. MOBSTER_FRAME_CLOCK=shadow or off reverts.
DEFAULT_MODE = "on"

# Decoded frame height. JPEG draft mode scales by 1/2, 1/4 or 1/8 inside the
# decoder; the factor is chosen so frames land at >= this many rows whatever
# the MJPEG scaling (medium: 590x1278 -> 1/4 -> 148x319).
TARGET_ROWS = 300
TILE = 16                 # tile side in decoded pixels (~43 pt on an iPhone 15 Pro)
PIXEL_THRESHOLD = 16      # grey levels; identical screens re-encode to identical bytes
TILE_MIN = 2              # tile mean of the 0/255 change map: >= 2 changed pixels of 256
# The status bar (clock, battery, Live Activities) is not the app's and changes
# on its own; it is cropped before any comparison. So is the right edge, where
# the vertical scroll indicator fades out ~1.1 s after every scroll (measured:
# 6 tiles changing long after the content stopped).
STATUS_BAR_FRACTION = .05
SCROLL_INDICATOR_FRACTION = .03
EFFECT_TILES = 2          # changed tiles (outside busy regions) that count as an effect
STABLE_SECONDS = .18      # no change this long after the effect = at rest
STABLE_MIN_FRAMES = 3     # ...seen over at least this many frames
BASELINE_SECONDS = 1.5    # activity before mark() that defines busy regions
QUIET_BEFORE_MARK = .2    # the screen must be still this long before dispatch
ACTIVE_RATIO = .2         # changed in more than this share of baseline frames = masked
PERIODIC_CHANGES = 2      # changed at least this often in the baseline = ignored for effects
RECENT_SECONDS = .6       # ...and still changing this recently (a caret blinks every ~0.5 s)
MAX_MASKED_FRACTION = .25 # more of the screen busy than this: the clock declines
STALE_SECONDS = .3        # newest frame older than this: unhealthy
MIN_FPS = 10              # frames in the last second (measured: ~28 fps, p99 gap 127 ms)
GAP_SECONDS = 1.0         # a longer pause between frames is logged (the settle is blind meanwhile)
# MJPEG arrival lags the screen by at most ~55 ms (measured against /screenshot
# captures during scroll momentum, iPhone 15 Pro over USB).
CAPTURE_LAG = .06
WATCH_SECONDS = 12        # a token's watch expires after this long
MAX_WATCHES = 8
HISTORY = 96              # (time, changed tiles) per frame, ~3 s; no images
PROFILE_STRIPS = 32       # columns kept for scroll odometry
MIN_SHIFT_CONFIDENCE = .3 # below this an axis reports no shift


def frame_clock_mode(value=None):
    """``off`` | ``shadow`` | ``on`` from the argument or ``MOBSTER_FRAME_CLOCK``."""
    raw = os.environ.get(ENV) if value is None else value
    raw = (raw or "").strip().casefold()
    if raw in {"0", "off", "false", "no", "disabled"}:
        return "off"
    if raw in {"shadow", "log", "observe"}:
        return "shadow"
    if raw in {"1", "on", "true", "yes", "enabled"}:
        return "on"
    return DEFAULT_MODE


def available():
    return Image is not None


# -- image primitives ---------------------------------------------------------

_CHANGE_LUT = [0 if value <= PIXEL_THRESHOLD else 255 for value in range(256)]


def decode(data):
    """JPEG bytes -> small greyscale image with the status bar cropped off."""
    image = Image.open(io.BytesIO(data))
    if image.format != "JPEG":
        raise ValueError("FrameClock decodes JPEG frames only")
    width, height = image.size
    factor = 1
    while factor < 8 and height // (factor * 2) >= TARGET_ROWS:
        factor *= 2
    image.draft("L", (max(1, width // factor), max(1, height // factor)))
    image = image.convert("L")
    top = int(image.size[1] * STATUS_BAR_FRACTION)
    right = image.size[0] - max(1, round(image.size[0] * SCROLL_INDICATOR_FRACTION))
    return image.crop((0, top, right, image.size[1]))


def tile_grid(size):
    return math.ceil(size[0] / TILE), math.ceil(size[1] / TILE)


def changed_tiles(a, b):
    """Indexes (row-major) of tiles where ``b`` differs from ``a``; () when equal.

    Different sizes (rotation, a quality change) count as every tile changed.
    """
    if a is b:
        return ()
    if a.size != b.size:
        columns, rows = tile_grid(b.size)
        return tuple(range(columns * rows))
    change = ImageChops.difference(a, b).point(_CHANGE_LUT)
    if change.getbbox() is None:
        return ()
    grid = change.reduce(TILE).tobytes()
    return tuple(index for index, value in enumerate(grid) if value >= TILE_MIN)


def _profile(image, strips, axis):
    """Mean intensity per row (axis 0) or column (axis 1), in ``strips`` bands."""
    width, height = image.size
    return image.resize((strips, height) if axis == 0 else (width, strips), Image.BOX)


def _shift_cost(a, b, shift, length, axis):
    """Mean absolute difference when ``b`` shows ``a`` moved by ``shift`` (toward 0)."""
    def cut(image, start, end):
        width, height = image.size
        return image.crop((0, start, width, end)) if axis == 0 else image.crop((start, 0, end, height))
    if shift >= 0:
        x, y = cut(a, shift, length), cut(b, 0, length - shift)
    else:
        x, y = cut(a, 0, length + shift), cut(b, -shift, length)
    return sum(ImageStat.Stat(ImageChops.difference(x, y)).mean)


def _axis_shift(a, b, axis, max_fraction=.9):
    """Content shift of ``b`` against ``a`` along one axis, in decoded pixels.

    Positive means content moved toward the origin (up, or left): what a
    SWIPE_UP / SWIPE_LEFT does. Static bands at both ends (navigation and tab
    bars) are trimmed first, so fixed chrome does not pin the answer at 0.
    Returns ``(shift, confidence)``; confidence is 0 when there is nothing to
    match (a blank screen) and approaches 1 for a sharp, unique match.
    """
    strips = PROFILE_STRIPS
    pa, pb = _profile(a, strips, axis), _profile(b, strips, axis)
    length = pa.size[1] if axis == 0 else pa.size[0]
    flat = (1, length) if axis == 0 else (length, 1)
    rows = ImageChops.difference(pa, pb).resize(flat, Image.BOX).tobytes()
    start, end = 0, length
    while start < end and rows[start] <= 1:
        start += 1
    while end > start and rows[end - 1] <= 1:
        end -= 1
    if end - start < 8:
        return 0, 1.0 if end - start == 0 else 0.0
    if axis == 0:
        pa, pb = pa.crop((0, start, strips, end)), pb.crop((0, start, strips, end))
    else:
        pa, pb = pa.crop((start, 0, end, strips)), pb.crop((start, 0, end, strips))
    span = end - start
    limit = int(span * max_fraction)
    overlap = max(8, int(span * .3))
    # Exhaustive at this resolution: a coarse pass aliases on periodic lists
    # (Settings rows repeat every ~16 decoded pixels). ~10 ms, once per swipe.
    costs = {shift: _shift_cost(pa, pb, shift, span, axis)
             for shift in range(-limit, limit + 1) if span - abs(shift) >= overlap}
    if not costs:
        return 0, 0.0
    best = min(costs, key=costs.get)
    ordered = sorted(costs.values())
    typical = ordered[len(ordered) // 2] or 1e-9
    return best, max(0.0, min(1.0, 1 - costs[best] / typical))


def displacement(a, b, axes="xy"):
    """Content shift from frame ``a`` to frame ``b`` as screen fractions.

    ``a``/``b`` are decoded images or ``Frame`` objects; ``axes`` limits the
    search ("y" for a vertical scroll, ~8 ms; "xy" ~16 ms). Returns
    ``{"dy", "dx", "confidence"}``: ``dy`` > 0 when content moved up (the list
    scrolled toward its end), ``dx`` > 0 when it moved left. None when the
    frames are missing or of different geometry.
    """
    a = getattr(a, "image", a)
    b = getattr(b, "image", b)
    if a is None or b is None or a.size != b.size:
        return None
    if not changed_tiles(a, b):
        return {"dy": 0.0, "dx": 0.0, "confidence": 1.0}
    dy, vertical = _axis_shift(a, b, 0) if "y" in axes else (0, 0.0)
    dx, horizontal = _axis_shift(a, b, 1) if "x" in axes else (0, 0.0)
    # An axis without a clear match reports no shift: a vertical scroll changes
    # every column profile, and the best horizontal "match" is then noise.
    if vertical < MIN_SHIFT_CONFIDENCE:
        dy = 0
    if horizontal < MIN_SHIFT_CONFIDENCE:
        dx = 0
    width, height = a.size
    # The status bar was cropped off; express shifts against the full screen.
    full_height = height / (1 - STATUS_BAR_FRACTION)
    return {"dy": round(dy / full_height, 4), "dx": round(dx / width, 4),
            "confidence": round(max(vertical, horizontal), 3)}


# -- clock --------------------------------------------------------------------

@dataclass
class Frame:
    seq: int
    t: float            # arrival on this Mac (monotonic)
    image: object
    changed: tuple = () # tiles changed against the previous frame
    same_bytes: bool = False  # byte-identical to the previous frame


@dataclass
class Watch:
    """Everything the worker learns about one action, frame by frame."""
    t: float
    ref: Frame
    stable_mask: frozenset
    effect_mask: frozenset
    usable: bool
    reason: str = ""
    effect: Frame | None = None
    effect_tiles: int = 0
    anchor: Frame | None = None
    latest: Frame | None = None
    frames_since_anchor: int = 0
    stable_at: float | None = None       # first time the at-rest condition held
    moves: deque = field(default_factory=lambda: deque(maxlen=400))  # (t, moved) after the effect
    # (t, bytes changed) for every frame after mark(). A static screen re-encodes
    # to identical JPEG bytes (measured: 29/29 frames), so this sees sub-tile
    # motion such as iOS 26's glass morph. Too strict to settle on (measured:
    # bytes keep changing ~1-1.2 s after a Settings push, long after AX is
    # stable), it is kept for diagnostics.
    exact: deque = field(default_factory=lambda: deque(maxlen=400))
    closed: bool = False
    release_t: float | None = None       # dispatch returned (+ capture lag)
    release_frame: Frame | None = None   # first frame at or after release_t


class FrameClock:
    """Consumes JPEG frames from a ``WdaVideo`` relay, or its own MJPEG connection.

    Thread-safe. Frames are processed on a private worker thread; the relay is
    never blocked (it only notifies a condition). Memory is bounded: the latest
    frame, one anchor, and per-watch reference frames, plus a short history of
    changed-tile sets without images.
    """

    def __init__(self, video=None, *, wda_url=None, session=None, mjpeg_url=None,
                 clock=time.monotonic, log_path=None, start=True):
        if not available():
            raise RuntimeError("FrameClock needs Pillow")
        self.clock = clock
        self.owns_video = video is None
        if video is None:
            if not wda_url:
                raise ValueError("FrameClock needs a WdaVideo relay or a WDA URL")
            from .wda_video import WdaVideo
            if isinstance(session, str):
                session = (lambda value: lambda: value)(session)
            video = WdaVideo(wda_url, mjpeg_url=mjpeg_url, session=session, screenshot_fallback=False)
        self.video = video
        self.log_path = log_path
        self.condition = threading.Condition()
        self.closed = False
        self.viewer = None
        self.worker = None
        self.latest = None           # Frame
        self._data = None            # bytes of the latest frame (identical-bytes shortcut)
        self.anchor = None           # global still-tracker anchor (no mask)
        self.still_since = None      # arrival of the last frame with a changed tile
        self.exact_since = None      # arrival of the last frame whose bytes changed
        self.history = deque(maxlen=HISTORY)   # (t, changed tiles)
        self.arrivals = deque(maxlen=128)
        self.decode_ms = deque(maxlen=256)
        self.watches = []
        self.records = deque(maxlen=256)
        self.frames = 0
        self.skipped = 0
        self.errors = 0
        self.last_seq = 0
        self.resets = 0
        self._log_lock = threading.Lock()
        if start:
            self.start()

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        with self.condition:
            if self.worker is not None or self.closed:
                return
            self.viewer = self.video.subscribe()
            self.worker = threading.Thread(target=self._run, name="mobster-frame-clock", daemon=True)
            self.worker.start()

    def close(self, timeout=2):
        with self.condition:
            if self.closed:
                return
            self.closed = True
            viewer, self.viewer = self.viewer, None
            self.watches.clear()
            self.condition.notify_all()
        if viewer is not None:
            try:
                self.video.unsubscribe(viewer)  # wakes the worker's next_frame
            except Exception:
                pass
        if self.owns_video:
            try:
                self.video.close()
            except Exception:
                pass
        worker = self.worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- worker ------------------------------------------------------------

    def _run(self):
        sequence = 0
        while True:
            with self.condition:
                if self.closed:
                    return
                viewer = self.viewer
            try:
                frame = self.video.next_frame(viewer, sequence, timeout=.5)
            except Exception:
                frame = None
            if frame is None:
                if getattr(self.video, "closed", False):
                    return
                continue
            sequence = frame[0]
            source = frame[5] if len(frame) > 5 else "wda_mjpeg"
            arrival = frame[4] if len(frame) > 4 else self.clock()
            if frame[1] != "image/jpeg" or source != "wda_mjpeg":
                continue  # Screenshot fallback frames are too slow to be a clock.
            last = self.latest
            if last is not None and arrival - last.t >= GAP_SECONDS:
                self.record({"kind": "gap", "ms": round((arrival - last.t) * 1000)})
            self.ingest(frame[2], arrival, sequence)

    def ingest(self, data, arrival=None, seq=None):
        """Process one JPEG frame. Public so tests (and offline replays) can feed frames."""
        arrival = self.clock() if arrival is None else arrival
        started = time.perf_counter()
        with self.condition:
            previous, previous_data = self.latest, self._data
        if seq is not None and self.last_seq and seq > self.last_seq + 1:
            self.skipped += seq - self.last_seq - 1
        same = previous is not None and data == previous_data
        if same:
            image, changed = previous.image, ()
        else:
            try:
                image = decode(data)
            except Exception:
                self.errors += 1
                return None
            changed = changed_tiles(previous.image, image) if previous is not None else ()
        current = Frame(seq if seq is not None else self.frames + 1, arrival, image, changed, same)
        with self.condition:
            if self.closed:
                return None
            if previous is not None and previous.image.size != image.size:
                self._reset_locked()
            self.latest, self._data = current, data
            self.last_seq = current.seq
            self.frames += 1
            self.arrivals.append(arrival)
            self.history.append((arrival, changed))
            # Global stillness: the last frame with a changed tile (no mask) ...
            if self.anchor is None:
                self.anchor, self.still_since = current, arrival
            elif changed or (not same and changed_tiles(self.anchor.image, image)):
                self.anchor, self.still_since = current, arrival
            # ... and, exactly, the last frame whose bytes changed.
            if self.exact_since is None or not same:
                self.exact_since = arrival
            now = self.clock()
            self.watches = [w for w in self.watches if not w.closed and now - w.t <= WATCH_SECONDS]
            for watch in self.watches:
                self._update_watch(watch, current)
            self.decode_ms.append((time.perf_counter() - started) * 1000)
            self.condition.notify_all()
        return current

    def _reset_locked(self):
        """Geometry changed mid-stream: every reference is void."""
        self.resets += 1
        self.anchor = self.still_since = None
        self.history.clear()
        for watch in self.watches:
            watch.usable, watch.reason, watch.closed = False, "geometry_changed", True

    def _update_watch(self, watch, frame):
        if not watch.usable or frame.t <= watch.t:
            return
        watch.latest = frame
        watch.exact.append((frame.t, not frame.same_bytes))
        if watch.release_t is not None and watch.release_frame is None and frame.t >= watch.release_t:
            watch.release_frame = frame
        if watch.effect is None:
            tiles = [t for t in changed_tiles(watch.ref.image, frame.image) if t not in watch.effect_mask]
            if len(tiles) >= EFFECT_TILES:
                watch.effect, watch.effect_tiles = frame, len(tiles)
                watch.anchor, watch.frames_since_anchor = frame, 0
                watch.moves.append((frame.t, True))
            return
        moved = any(t not in watch.stable_mask for t in frame.changed) or any(
            t not in watch.stable_mask for t in changed_tiles(watch.anchor.image, frame.image))
        watch.moves.append((frame.t, moved))
        if moved:
            watch.anchor, watch.frames_since_anchor = frame, 0
        else:
            watch.frames_since_anchor += 1
            if (watch.stable_at is None and frame.t - watch.anchor.t >= STABLE_SECONDS
                    and watch.frames_since_anchor >= STABLE_MIN_FRAMES):
                watch.stable_at = frame.t

    # -- health ------------------------------------------------------------

    def healthy(self):
        """Fresh frames at a usable rate. Everything else answers None when not."""
        with self.condition:
            return self._healthy_locked()

    def _healthy_locked(self):
        if self.closed or self.latest is None:
            return False
        now = self.clock()
        if now - self.latest.t > STALE_SECONDS:
            return False
        return self._recent_frames_locked(now) >= MIN_FPS

    def _recent_frames_locked(self, now):
        """Frames that arrived in the last second."""
        return sum(1 for t in self.arrivals if now - t <= 1.0)

    def stats(self):
        with self.condition:
            timings = sorted(self.decode_ms)
            now = self.clock()
            return {"healthy": self._healthy_locked(), "frames": self.frames, "skipped": self.skipped,
                    "errors": self.errors, "resets": self.resets,
                    "fps": self._recent_frames_locked(now),
                    "frame_age_ms": None if self.latest is None else round((now - self.latest.t) * 1000, 1),
                    "process_ms_p50": round(timings[len(timings) // 2], 3) if timings else None,
                    "process_ms_p99": round(timings[int(len(timings) * .99)], 3) if timings else None,
                    "watches": len(self.watches)}

    # -- questions ---------------------------------------------------------

    def _baseline_locked(self, end):
        """Busy tiles in the baseline window ending at ``end``.

        Only activity that is still going on counts: a tile must also have
        changed within the last RECENT_SECONDS. A scroll that ended just before
        the action leaves no mask (masking it would hide the list region from
        the action's own transition).
        """
        counts, recent, frames = {}, set(), 0
        for t, changed in self.history:
            if end - BASELINE_SECONDS <= t <= end:
                frames += 1
                for tile in changed:
                    counts[tile] = counts.get(tile, 0) + 1
                if t >= end - RECENT_SECONDS:
                    recent.update(changed)
        active = frozenset(t for t, n in counts.items()
                           if frames >= 10 and n > frames * ACTIVE_RATIO and t in recent)
        periodic = frozenset(t for t, n in counts.items() if n >= PERIODIC_CHANGES and t in recent) | active
        return active, periodic

    def mark(self):
        """A token for "now", taken right before dispatch.

        The token watches every later frame. It is ``usable`` only when the
        stream is healthy, the screen was still (outside constantly busy
        tiles) for ``QUIET_BEFORE_MARK``, and busy regions cover less than
        ``MAX_MASKED_FRACTION`` of the screen.
        """
        with self.condition:
            now = self.clock()
            latest = self.latest
            if not self._healthy_locked():
                return Watch(now, latest, frozenset(), frozenset(), False, "unhealthy", closed=True)
            active, periodic = self._baseline_locked(latest.t)
            columns, rows = tile_grid(latest.image.size)
            watch = Watch(now, latest, active, periodic, True)
            if len(periodic) > MAX_MASKED_FRACTION * columns * rows:
                watch.usable, watch.reason, watch.closed = False, "busy_screen", True
            elif any(t >= latest.t - QUIET_BEFORE_MARK and any(tile not in active for tile in changed)
                     for t, changed in self.history):
                watch.usable, watch.reason, watch.closed = False, "not_still_before_action", True
            else:
                if len(self.watches) >= MAX_WATCHES:
                    self.watches.pop(0).closed = True
                self.watches.append(watch)
            return watch

    def set_release(self, watch, t):
        """The action's request returned at ``t``: remember the first frame
        captured after it (odometry of what moved after release)."""
        with self.condition:
            if watch is not None and watch.release_t is None:
                watch.release_t = t + CAPTURE_LAG

    def release(self, watch):
        if watch is None:
            return
        with self.condition:
            watch.closed = True
            if watch in self.watches:
                self.watches.remove(watch)

    def _wait(self, predicate, timeout):
        """Wait until ``predicate()`` is truthy; None on timeout or a stale stream."""
        deadline = self.clock() + max(0.0, timeout)
        with self.condition:
            while True:
                value = predicate()
                if value:
                    return value
                if self.closed or not self._healthy_locked():
                    return None
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return None
                self.condition.wait(min(remaining, .05))

    def wait_effect(self, watch, timeout):
        """The first frame after ``mark()`` with >= EFFECT_TILES changed tiles
        outside busy regions, or None (no change within ``timeout``, stale
        stream, or an unusable token)."""
        if watch is None or not watch.usable:
            return None
        return self._wait(lambda: watch.effect, timeout)

    def wait_stable(self, watch, timeout, duration=STABLE_SECONDS):
        """After the effect: the arrival time from which nothing (outside
        constantly busy tiles) changed for ``duration``; None on timeout."""
        if watch is None or not watch.usable:
            return None

        def settled():
            if watch.effect is None or watch.latest is None or watch.anchor is None:
                return None
            if (watch.latest.t - watch.anchor.t >= duration
                    and watch.frames_since_anchor >= STABLE_MIN_FRAMES):
                return watch.anchor
            return None
        return self._wait(settled, timeout)

    def quiet_between(self, watch, start, end, min_frames=STABLE_MIN_FRAMES, exact=False):
        """After the effect, frames arrived in (start, end] (at least
        ``min_frames``) and none of them changed: no changed tile outside busy
        regions, or byte-identical frames when ``exact``."""
        with self.condition:
            if watch is None or not watch.usable or watch.effect is None or watch.effect.t > start:
                return False
            source = watch.exact if exact else watch.moves
            window = [moved for t, moved in source if start < t <= end]
            return len(window) >= min_frames and not any(window)

    def moved_since(self, watch, since):
        """True when any frame arriving after ``since`` moved (outside busy tiles)."""
        with self.condition:
            return any(moved and t > since for t, moved in watch.moves)

    def last_motion(self, watch):
        with self.condition:
            return max((t for t, moved in watch.moves if moved), default=None)

    def still_for(self, exact=True):
        """Seconds since the stream's bytes last changed (``exact``: nothing on
        screen changed at all, sub-tile motion included), or since a tile last
        changed anywhere (status bar aside, no mask) when ``exact`` is False;
        None when unhealthy. Tile stillness misses iOS 26's glass morph, which
        changes AX (measured), so callers that skip an AX read use the default."""
        with self.condition:
            since = self.exact_since if exact else self.still_since
            if not self._healthy_locked() or since is None:
                return None
            return self.clock() - since

    def latest_frame(self):
        with self.condition:
            return self.latest

    # -- logging -----------------------------------------------------------

    def record(self, entry):
        """Keep a settle comparison in memory and append it to the JSONL log."""
        entry = {"at": round(time.time(), 3), **entry}
        with self.condition:
            self.records.append(entry)
        log.info("frameclock %s", entry)
        if not self.log_path:
            return
        try:
            line = json.dumps(entry, sort_keys=True, default=str) + "\n"
            with self._log_lock:
                if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > 5_000_000:
                    os.replace(self.log_path, self.log_path + ".1")
                with open(self.log_path, "a", encoding="utf-8") as handle:
                    handle.write(line)
        except OSError:
            pass


def attach_frame_clock(driver, video=None, *, wda_url=None, session=None, log_path=None, log_dir=None,
                       mode=None, mjpeg_url=None):
    """Give a WDA driver a FrameClock for its lifetime, per ``MOBSTER_FRAME_CLOCK``.

    ``video`` is a running ``WdaVideo`` relay to subscribe to (the server's);
    without one the clock opens its own MJPEG connection (CLI, evals). The
    driver's ``close()`` releases it. Settle comparisons go to
    ``MOBSTER_FRAME_CLOCK_LOG``, else ``log_path``, else
    ``<log_dir>/frameclock.jsonl``. Returns the clock, or None when off or
    unavailable; failures never prevent the run.
    """
    mode = frame_clock_mode(mode)
    if mode == "off" or not available() or not hasattr(driver, "frame_clock"):
        return None
    log_path = (os.environ.get("MOBSTER_FRAME_CLOCK_LOG") or log_path
                or (os.path.join(str(log_dir), "frameclock.jsonl") if log_dir else None))
    try:
        if video is not None:
            if not all(hasattr(video, name) for name in ("subscribe", "unsubscribe", "next_frame")):
                return None
            clock = FrameClock(video, log_path=log_path)
        else:
            if not wda_url:
                return None
            clock = FrameClock(wda_url=wda_url, session=session, mjpeg_url=mjpeg_url, log_path=log_path)
    except Exception as error:
        log.info("frameclock unavailable: %s", type(error).__name__)
        return None
    driver.frame_clock, driver.frame_clock_mode = clock, mode
    return clock

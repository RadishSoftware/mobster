"""Live video from an iPhone through WebDriverAgent's MJPEG stream, relayed to every viewer from one connection."""

import base64
import http.client
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from . import timeouts as wda_timeouts

# User-selectable quality. WDA's own default is 10 fps at full resolution, which is
# what a viewer gets whenever these settings fail to apply; 60 is WDA's ceiling and
# the phone may deliver less (measured on an iPhone 15 Pro: 28.5 fps at medium/30).
# One iproxy pair per phone: its WDA port and its MJPEG stream's port (8100 -> 9100, 8101 -> 9101).
WDA_BASE_PORT, MJPEG_BASE_PORT = 8100, 9100


def default_mjpeg_url(wda_url):
    """The MJPEG URL paired with a WDA port (8100 -> 9100), or None when the port has no pair."""
    parts = urlsplit(wda_url or "")
    try:
        port = parts.port  # None without one: http's 80 or https's 443, which have no pair either
    except ValueError:
        return None
    video = MJPEG_BASE_PORT + (port - WDA_BASE_PORT) if port is not None and port >= WDA_BASE_PORT else None
    if not parts.hostname or video is None or video > 65535:
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname  # an IPv6 address keeps its brackets
    return urlunsplit((parts.scheme, f"{host}:{video}", "", "", ""))


VIDEO_FPS = (15, 30, 60)
VIDEO_DETAIL = {"low": {"mjpegScalingFactor": 35, "mjpegServerScreenshotQuality": 40},
                "medium": {"mjpegScalingFactor": 50, "mjpegServerScreenshotQuality": 50},
                "high": {"mjpegScalingFactor": 75, "mjpegServerScreenshotQuality": 65}}
DEFAULT_QUALITY = (30, "medium")


def mjpeg_settings(fps, detail):
    if fps not in VIDEO_FPS or detail not in VIDEO_DETAIL:
        raise ValueError("Frame rate is 15, 30 or 60; detail is low, medium or high")
    return {"mjpegServerFramerate": fps, **VIDEO_DETAIL[detail]}


SCREENSHOT_FPS = 4
IDLE_GRACE_SECONDS = 3.0
MJPEG_RETRY_SECONDS = 5.0
# A stream silent this long is dropped and reopened. WDA sends no frame while it serves a long
# request (a simulator's 1,263-node source read, an XCTest drag of ~8 s); a stream that had
# delivered frames reconnects at once, since waiting MJPEG_RETRY_SECONDS left the FrameClock
# blind ~10 s after every such stall.
MJPEG_READ_SECONDS = 5.0
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_HEADER_LINE = 1024
MAX_VIEWERS = 8
# How long a still picture (peek) waits for a fresh frame when the relay was stopped.
PEEK_WAIT_SECONDS = 2.0
BOUNDARY = "mobsterframe"
# A paced viewer is sent an unchanged screen again this often. The dashboard calls a picture with no frame for 2 s
# stale ("Last frame at ..."), so a still phone must never look like a stalled stream.
HEARTBEAT_SECONDS = 1.0


# WDA writes every frame it makes into its socket and never drops one, so on a link slower than the stream the frames
# queue on the phone and the picture falls further behind every second (13 Oct 2026, scripts/measure_live_link.py
# through a 4 Mbit/s link: 2.6 frames a second, each 25.7 s old after 30 s, 172 MB queued; with this, 6.2 frames a
# second, 0.15 s old). The relay times each frame's body as it arrives. On a link with room, a frame comes
# in a burst and the connection then waits for the next one (a USB iPhone: under 15% of the time). On a saturated
# link the next frame's bytes are already waiting, so receiving fills the whole time between frames. That share is
# the link's load: above LINK_BUSY the relay steps down a level and reconnects, which drops what queued on the phone;
# below LINK_IDLE for a while it steps back up. A phone that is itself slow (busy, hot) sends small bursts with long
# waits between them, so it never reads as a slow link.
LINK_STEPS = ((30, 50, 50), (20, 45, 45), (15, 40, 40), (10, 35, 40), (6, 30, 35), (3, 25, 30))
LINK_BUSY = 0.85
LINK_IDLE = 0.45
# Each reading covers at least this long and LINK_FRAMES frames.
LINK_WINDOW_SECONDS = 1.0
LINK_FRAMES = 3
# A step up waits this long at a low load, twice as long after one that had to come back down (to LINK_RECOVER_MAX).
LINK_RECOVER_SECONDS = 8.0
LINK_RECOVER_MAX = 64.0
# A step up that is still standing after this long resets the wait.
LINK_STABLE_SECONDS = 30.0


def link_ladder(settings):
    """The levels a link may use under ``settings`` (the user's choice, level 0)."""
    top = (settings["mjpegServerFramerate"], settings["mjpegScalingFactor"], settings["mjpegServerScreenshotQuality"])
    ladder = [top]
    for step in LINK_STEPS:
        level = tuple(min(a, b) for a, b in zip(step, top))
        if level != ladder[-1] and all(a <= b for a, b in zip(level, ladder[-1])):
            ladder.append(level)
    return ladder


class LinkGovernor:
    """The level a link can carry (0: the user's choice), from the share of time the relay spends receiving."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.level = 0
        self.levels = 1
        self.load = None
        self.recover = LINK_RECOVER_SECONDS
        self._reset_window()
        self.idle_since = None
        self.raised_at = None

    def _reset_window(self):
        self.busy = self.span = 0.0
        self.count = 0

    def reset(self, levels):
        """A new ladder (the user changed the picture): start again at its top."""
        self.level, self.levels, self.load = 0, levels, None
        self.recover, self.idle_since, self.raised_at = LINK_RECOVER_SECONDS, None, None
        self._reset_window()

    def restart(self):
        """A new connection: the first frames after it measure the connection, not the link."""
        self._reset_window()

    def frame(self, body, interval):
        if interval is None or interval <= 0:
            return None
        self.busy += min(body, interval)
        self.span += interval
        self.count += 1
        if self.span < LINK_WINDOW_SECONDS or self.count < LINK_FRAMES:
            return None
        load, now = self.busy / self.span, self.clock()
        self.load = round(load, 2)
        self._reset_window()
        if self.raised_at is not None and now - self.raised_at >= LINK_STABLE_SECONDS:
            self.raised_at, self.recover = None, LINK_RECOVER_SECONDS
        if load > LINK_BUSY:
            self.idle_since = None
            if self.level + 1 >= self.levels:
                return None
            if self.raised_at is not None:  # the last step up didn't hold: wait longer before the next
                self.recover, self.raised_at = min(self.recover * 2, LINK_RECOVER_MAX), None
            self.level += 1
            return "down"
        if load < LINK_IDLE and self.level > 0:
            if self.idle_since is None:
                self.idle_since = now
            elif now - self.idle_since >= self.recover:
                self.level -= 1
                self.idle_since, self.raised_at = None, now
                return "up"
            return None
        self.idle_since = None
        return None


class VideoUnavailable(RuntimeError):
    """No viewer slot, or the relay is closed."""


def read_mjpeg_frames(stream, on_body=None, clock=time.monotonic):
    """Yield JPEG frames from a multipart MJPEG byte stream (WDA's format)."""
    while True:
        headers = {}
        line = stream.readline(MAX_HEADER_LINE)
        if not line:
            return
        if not line.strip() or line.startswith(b"--"):
            continue
        while line and line.strip():
            if b":" in line:
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().casefold()] = value.strip()
            line = stream.readline(MAX_HEADER_LINE)
        if not line:
            return
        length = headers.get("content-length")
        if length is not None and length.isdigit():
            size = int(length)
            if size <= 0 or size > MAX_FRAME_BYTES:
                raise ValueError("MJPEG frame size out of range")
            started = clock()
            data = stream.read(size)
            if len(data) != size:
                return
            if on_body is not None:
                on_body(clock() - started)
            yield data
        else:
            buffer = bytearray()
            while b"\xff\xd9" not in buffer:
                chunk = stream.read(4096)
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > MAX_FRAME_BYTES:
                    raise ValueError("MJPEG frame too large")
            end = buffer.index(b"\xff\xd9") + 2
            yield bytes(buffer[:end])


class WdaVideo:
    def __init__(self, wda_url, mjpeg_url=None, session=None, on_activity=None, clock=time.monotonic,
                 quality=DEFAULT_QUALITY, screenshot_fallback=True):
        self.wda_url = wda_url.rstrip("/")
        self.settings = mjpeg_settings(*quality)  # the user's choice; a slow link uses a level below it
        self.configured = None  # (session, settings) last applied successfully
        self.quality_changed = threading.Event()  # set when the relay worker should send the settings again
        self.link = LinkGovernor(clock)
        self.link.reset(len(link_ladder(self.settings)))
        if not mjpeg_url:
            # The stream paired with this WDA's port (8100 -> 9100, a simulator's 8203 -> 9203): a caller
            # that names none must never watch, and settle on, another device's screen. A port with no pair
            # (8080) has no stream: screenshots only, or none for a private consumer.
            mjpeg_url = default_mjpeg_url(self.wda_url)
        self.mjpeg_url = mjpeg_url.rstrip("/") if mjpeg_url else None
        self.session = session  # callable returning the current WDA session id, or None
        self.on_activity = on_activity or (lambda active: None)
        self.clock = clock
        # Off for private consumers (the FrameClock): polling /screenshot would
        # queue behind, and delay, the agent's own WDA requests.
        self.screenshot_fallback = screenshot_fallback
        self.condition = threading.Condition()
        self.viewers = set()
        # (sequence, content_type, bytes, captured_at_ms wall clock, arrival monotonic, source)
        self.frame = None
        self.sequence = 0
        self.source = None         # "wda_mjpeg" | "wda_screenshot"
        self.error = None
        self.worker = None
        self.closed = False
        self.idle_since = None
        self.frame_times = []
        self._streamed = 0  # frames the current MJPEG connection delivered

    def status(self):
        with self.condition:
            live = self.frame is not None and self.clock() - self.frame[4] < 3
            return {"status": "live" if live else "starting" if self.viewers else "idle",
                    "kind": "mjpeg", "sourceKind": self.source, "error": self.error,
                    "viewers": len(self.viewers), "fps": round(self.fps(), 1),
                    "targetFps": self.settings["mjpegServerFramerate"],
                    "reducedFps": self.reduced_fps(), "linkLoad": self.link.load,
                    "capturedAt": self.frame[3] if self.frame else None,
                    "stream": "/api/device/stream"}

    def fps(self):
        now = self.clock()
        recent = [t for t in self.frame_times if now - t <= 2]
        return len(recent) / 2

    def reduced_fps(self):
        """The frame rate a slow link brought the stream down to, or None at the user's own choice."""
        with self.condition:
            if not self.link.level:
                return None
            return link_ladder(self.settings)[self.link.level][0]

    def applied_settings(self):
        """The settings WDA should stream with now: the user's choice, or the level a slow link holds."""
        with self.condition:
            if not self.link.level:
                return dict(self.settings)
            fps, scaling, quality = link_ladder(self.settings)[self.link.level]
            return {**self.settings, "mjpegServerFramerate": fps, "mjpegScalingFactor": scaling,
                    "mjpegServerScreenshotQuality": quality}

    def subscribe(self):
        with self.condition:
            if self.closed:
                raise VideoUnavailable("Live video is shutting down")
            if len(self.viewers) >= MAX_VIEWERS:
                raise VideoUnavailable("Too many live viewers")
            viewer = object()
            self.viewers.add(viewer)
            self.idle_since = None
            if self.worker is None or not self.worker.is_alive():
                self._start_worker()
            return viewer

    def _start_worker(self):
        # The caller holds the condition, also during the activity notification.
        self.worker = threading.Thread(target=self._run, name="mobster-wda-video", daemon=True)
        self.worker.start()
        self.on_activity(True)

    def unsubscribe(self, viewer):
        with self.condition:
            self.viewers.discard(viewer)
            if not self.viewers:
                self.idle_since = self.clock()
            self.condition.notify_all()

    def next_frame(self, viewer, after, timeout=5.0):
        """The newest frame newer than sequence ``after``, or None on timeout/close.
        The FrameClock reads every frame here, identical ones included: its "the screen is still" answers count them."""
        deadline = self.clock() + timeout
        with self.condition:
            while not self.closed and viewer in self.viewers:
                if self.frame is not None and self.frame[0] > after:
                    return self.frame
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return None
                self.condition.wait(remaining)
            return None

    def latest(self):
        with self.condition:
            return self.frame

    def peek(self, wait=PEEK_WAIT_SECONDS):
        """The newest frame for a still picture (a tile); starts a stopped relay and waits up to ``wait`` for one."""
        try:
            viewer = self.subscribe()
        except VideoUnavailable:
            return self.latest()
        try:
            deadline = self.clock() + wait
            with self.condition:
                while not self.closed and (self.frame is None or self.clock() - self.frame[4] > IDLE_GRACE_SECONDS):
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        break
                    self.condition.wait(remaining)
                return self.frame
        finally:
            self.unsubscribe(viewer)

    def paced_frames(self, viewer, rate=lambda: None, heartbeat=HEARTBEAT_SECONDS, timeout=10.0):
        """The frames one viewer is sent, until the relay closes or the viewer unsubscribes."""
        sequence, sent, sent_at, looked_at = 0, None, None, None
        while True:
            fps = rate()
            if isinstance(fps, (int, float)) and fps > 0 and looked_at is not None:
                wait = looked_at + 1 / fps - self.clock()
                if wait > 0 and self._sleep(viewer, wait):
                    return
            frame = self.next_frame(viewer, sequence, timeout=timeout)
            if frame is None:
                with self.condition:
                    if self.closed or viewer not in self.viewers:
                        return
                continue  # no new frame yet: the stream is starting or stalled
            sequence, now = frame[0], self.clock()
            looked_at = now
            if (sent is not None and frame[2] == sent[2] and frame[1] == sent[1]
                    and now - sent_at < heartbeat):
                continue
            sent, sent_at = frame, now
            yield frame

    def _sleep(self, viewer, seconds):
        """Sleep ``seconds`` (the condition wakes on every frame); True when the relay closed or the viewer left."""
        time.sleep(seconds)
        with self.condition:
            return self.closed or viewer not in self.viewers

    def release_idle(self):
        """Let go of the phone: end every stream and stop the worker; a viewer that subscribes again starts it."""
        with self.condition:
            self.viewers.clear()
            self.idle_since = self.clock() - IDLE_GRACE_SECONDS
            self.frame = None
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def _publish(self, content_type, data, source):
        now = self.clock()
        with self.condition:
            self.sequence += 1
            self.frame = (self.sequence, content_type, data, int(time.time() * 1000), now, source)
            self.source, self.error = source, None
            self.frame_times = [t for t in self.frame_times if now - t <= 2] + [now]
            self.condition.notify_all()

    def _should_run(self):
        with self.condition:
            if self.closed:
                return False
            if self.viewers:
                return True
            return self.idle_since is not None and self.clock() - self.idle_since < IDLE_GRACE_SECONDS

    def set_quality(self, fps, detail):
        """Change frame rate and detail; WDA applies them to a running stream."""
        settings = mjpeg_settings(fps, detail)
        with self.condition:
            if self.settings == settings:
                return
            self.settings = settings
            self.link.reset(len(link_ladder(settings)))
            self.quality_changed.set()

    def _configure(self):
        """Apply the quality settings; True once WDA accepted them for its session, else retried before each reconnect.
        Only the relay worker sends them, so an older request can't land after a newer one."""
        self.quality_changed.clear()
        try:
            session = self.session() if self.session else None
        except Exception:
            session = None
        if not session:
            return False
        settings = self.applied_settings()
        key = (session, tuple(sorted(settings.items())))
        if self.configured == key:
            return True
        try:
            self._request(self.wda_url, "POST", f"/session/{session}/appium/settings",
                          {"settings": settings}, timeout=5)
        except Exception:
            return False
        self.configured = key
        return True

    def _run(self):
        try:
            while self._should_run():
                self._configure()  # a no-op once applied for this session and quality
                self._streamed = 0
                try:
                    if self.mjpeg_url:
                        self._stream_mjpeg()
                except Exception as error:
                    with self.condition:
                        self.error = type(error).__name__
                if self._streamed:
                    continue  # it was streaming: a stall, not an absent server
                retry_at = self.clock() + MJPEG_RETRY_SECONDS
                while self._should_run() and self.clock() < retry_at:
                    if self.quality_changed.is_set():
                        self._configure()
                    if not self.screenshot_fallback:
                        time.sleep(.25)
                    elif not self._screenshot_once():
                        time.sleep(.5)
                    else:
                        time.sleep(1 / SCREENSHOT_FPS)
        finally:
            with self.condition:
                self.worker = None
                self.frame = None
                self.frame_times = []
                self.on_activity(False)
                # A viewer may subscribe after the last _should_run() check and before this cleanup, while the
                # old worker is still alive. Hand it to a new worker before the lock is released.
                if self.viewers and not self.closed:
                    self._start_worker()

    def _stream_mjpeg(self):
        parts = urlsplit(self.mjpeg_url)
        client = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        connection = client(parts.hostname, parts.port, timeout=MJPEG_READ_SECONDS)
        try:
            connection.request("GET", parts.path or "/")
            response = connection.getresponse()
            if response.status != 200:
                raise ConnectionError(f"MJPEG server answered {response.status}")
            self.link.restart()
            body, ended = [0.0], [None]
            for data in read_mjpeg_frames(response, on_body=lambda seconds: body.__setitem__(0, seconds),
                                          clock=self.clock):
                self._streamed += 1
                self._publish("image/jpeg", data, "wda_mjpeg")
                now = self.clock()
                with self.condition:
                    step = self.link.frame(body[0], now - ended[0] if ended[0] is not None else None)
                ended[0] = now
                if step == "down":
                    return  # _run applies the lower level and reconnects, dropping what queued on the phone
                if step == "up":
                    self.quality_changed.set()
                if not self._should_run():
                    return
                if self.quality_changed.is_set():
                    self._configure()
        finally:
            connection.close()

    def _screenshot_once(self):
        try:
            value = self._request(self.wda_url, "GET", "/screenshot", timeout=5).get("value")
            data = base64.b64decode(value, validate=True)
        except Exception as error:
            with self.condition:
                self.error = type(error).__name__
            return False
        content_type = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
        self._publish(content_type, data, "wda_screenshot")
        return True

    @staticmethod
    def _request(base, method, path, body=None, timeout=5):
        """One bounded WDA request: the deadline covers headers and body, so a dripping phone can't stretch it."""
        from ..transport import HTTP
        client = HTTP(base)
        try:
            return client.request(method, path, body, timeout=wda_timeouts.bounded(timeout, method, path))
        finally:
            client.close()

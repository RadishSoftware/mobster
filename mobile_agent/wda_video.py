"""Live video from a USB iPhone through WebDriverAgent.

WDA serves an MJPEG stream from the phone (port 9100 on the device, forwarded
by iproxy like the 8100 API). This relays it through the local API, which is
the only origin the desktop webview may load from, and fans it out to viewers:

* one upstream connection, whatever the number of viewers;
* each viewer receives the newest frame, so a slow viewer skips frames
  instead of building a backlog;
* nothing runs while nobody watches (after a short grace period).

When the MJPEG port is not reachable the relay falls back to polling WDA's
screenshot endpoint, and keeps retrying MJPEG in the background.
"""

import base64
import http.client
import json
import threading
import time
from urllib.parse import urlsplit

# User-selectable quality. WDA's own default is 10 fps at full resolution, which is
# what a viewer gets whenever these settings fail to apply; 60 is WDA's ceiling and
# the phone may deliver less (measured on an iPhone 15 Pro: 28.5 fps at medium/30).
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
BOUNDARY = "mobsterframe"


class VideoUnavailable(RuntimeError):
    """No viewer slot, or the relay is closed."""


def read_mjpeg_frames(stream):
    """Yield JPEG frames from a multipart MJPEG byte stream (WDA's format).

    Each part is: a boundary line, headers including Content-Length, a blank
    line, then exactly Content-Length bytes. Parts without a length fall back
    to scanning for the JPEG end marker.
    """
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
            data = stream.read(size)
            if len(data) != size:
                return
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
        self.settings = mjpeg_settings(*quality)
        self.configured = None  # (session, settings) last applied successfully
        parts = urlsplit(self.wda_url)
        self.mjpeg_url = (mjpeg_url or f"{parts.scheme}://{parts.hostname}:9100").rstrip("/")
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

    # -- viewer side -------------------------------------------------------

    def status(self):
        with self.condition:
            live = self.frame is not None and self.clock() - self.frame[4] < 3
            return {"status": "live" if live else "starting" if self.viewers else "idle",
                    "kind": "mjpeg", "sourceKind": self.source, "error": self.error,
                    "viewers": len(self.viewers), "fps": round(self.fps(), 1),
                    "targetFps": self.settings["mjpegServerFramerate"],
                    "capturedAt": self.frame[3] if self.frame else None,
                    "stream": "/api/device/stream"}

    def fps(self):
        now = self.clock()
        recent = [t for t in self.frame_times if now - t <= 2]
        return len(recent) / 2

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
                self.worker = threading.Thread(target=self._run, name="mobster-wda-video", daemon=True)
                self.worker.start()
                self.on_activity(True)
            return viewer

    def unsubscribe(self, viewer):
        with self.condition:
            self.viewers.discard(viewer)
            if not self.viewers:
                self.idle_since = self.clock()
            self.condition.notify_all()

    def next_frame(self, viewer, after, timeout=5.0):
        """The newest frame newer than sequence ``after``, or None on timeout/close."""
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

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    # -- upstream side -----------------------------------------------------

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
            self.settings = settings
        if self.worker is not None:
            threading.Thread(target=self._configure, name="mobster-video-quality", daemon=True).start()

    def _configure(self):
        """Apply the quality settings; True once WDA accepted them for its current session.

        A failure used to be silent, leaving viewers on WDA's 10 fps default for the
        whole stream; the relay now retries before every reconnect until they stick.
        """
        try:
            session = self.session() if self.session else None
        except Exception:
            session = None
        if not session:
            return False
        with self.condition:
            settings = dict(self.settings)
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
                    self._stream_mjpeg()
                except Exception as error:
                    with self.condition:
                        self.error = type(error).__name__
                if self._streamed:
                    continue  # it was streaming: a stall, not an absent server
                retry_at = self.clock() + MJPEG_RETRY_SECONDS
                while self._should_run() and self.clock() < retry_at:
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

    def _stream_mjpeg(self):
        parts = urlsplit(self.mjpeg_url)
        connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=MJPEG_READ_SECONDS)
        try:
            connection.request("GET", parts.path or "/")
            response = connection.getresponse()
            if response.status != 200:
                raise ConnectionError(f"MJPEG server answered {response.status}")
            for data in read_mjpeg_frames(response):
                self._streamed += 1
                self._publish("image/jpeg", data, "wda_mjpeg")
                if not self._should_run():
                    return
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
        parts = urlsplit(base)
        connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            connection.request(method, path, payload, headers)
            response = connection.getresponse()
            data = response.read(MAX_FRAME_BYTES * 2)
            if response.status >= 300:
                raise ConnectionError(f"WDA answered {response.status}")
            return json.loads(data)
        finally:
            connection.close()

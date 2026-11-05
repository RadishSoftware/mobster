"""A phone's live picture for the Mac app, whatever the connection."""

import struct
import subprocess
import threading
import time

from .video import (
    DEFAULT_QUALITY,
    HEARTBEAT_SECONDS,
    IDLE_GRACE_SECONDS,
    MAX_FRAME_BYTES,
    MAX_VIEWERS,
    PEEK_WAIT_SECONDS,
    VIDEO_DETAIL,
    VideoUnavailable,
    WdaVideo,
    mjpeg_settings,
)

# A tile in the grid of phones: small and slow; the focused phone gets the chosen width and rate.
TILE_WIDTH = 196
TILE_FPS = 4
TILE_QUALITY = .5
# The full picture's width per detail level, as a share of FULL_WIDTH (the connection's own frame width).
FULL_WIDTH = 590
DETAIL_SCALE = {"low": .5, "medium": .75, "high": 1.0}
DETAIL_QUALITY = {detail: VIDEO_DETAIL[detail]["mjpegServerScreenshotQuality"] / 100 for detail in VIDEO_DETAIL}
# After a failed pull, wait this long before the next.
RETRY_SECONDS = 1.0
# A helper process that exits is started again after this long while someone watches.
RESTART_SECONDS = 1.0
# The frame width a helper process is asked for at each detail level (its JPEG quality is DETAIL_QUALITY).
DETAIL_WIDTH = {"low": 360, "medium": 480, "high": 720}


def streams(video):
    """Whether ``video`` serves the live stream and tiles (WdaVideo, or a FrameRelay)."""
    return isinstance(video, (WdaVideo, FrameRelay))


class FrameRelay:
    """WdaVideo's frame source interface over ``pull(width, quality, after)``: (jpeg, key), or None when unchanged."""

    kind = "relay"

    def __init__(self, pull=None, *, source="relay", quality=DEFAULT_QUALITY, clock=time.monotonic,
                 sleep=time.sleep):
        if pull is not None:
            self.pull = pull
        self.source_name = source
        self.clock, self._sleep_for = clock, sleep
        self.condition = threading.Condition()
        self.viewers = set()
        self.focused = set()
        self.frame = None
        self.sequence = 0
        self.key = None
        self.error = None
        self.worker = None
        self.closed = False
        self.idle_since = None
        self.frame_times = []
        self.pulls = 0
        self.set_quality(*quality)

    def pull(self, width, quality, after):
        raise NotImplementedError

    def set_quality(self, fps, detail):
        mjpeg_settings(fps, detail)  # validates both, as WDA's do
        with self.condition:
            self.target_fps = fps
            self.width = int(FULL_WIDTH * DETAIL_SCALE[detail])
            self.quality = DETAIL_QUALITY[detail]

    def reduced_fps(self):
        return None

    def mode(self):
        """(width, quality, fps) to pull at now: the chosen ones while a stream is focused, a tile's otherwise."""
        with self.condition:
            if self.focused:
                return self.width, self.quality, self.target_fps
            return TILE_WIDTH, TILE_QUALITY, TILE_FPS

    def status(self):
        with self.condition:
            live = self.frame is not None and self.clock() - self.frame[4] < 3
            return {"status": "live" if live else "starting" if self.viewers else "idle", "kind": self.kind,
                    "sourceKind": self.source_name, "error": self.error, "viewers": len(self.viewers),
                    "fps": round(self.fps(), 1), "targetFps": self.target_fps, "reducedFps": None, "linkLoad": None,
                    "capturedAt": self.frame[3] if self.frame else None, "stream": "/api/device/stream"}

    def fps(self):
        now = self.clock()
        return len([t for t in self.frame_times if now - t <= 2]) / 2

    def subscribe(self, focus=True):
        """A viewer. ``focus`` (a live stream) asks for the full picture; a peek is a tile."""
        with self.condition:
            if self.closed:
                raise VideoUnavailable("Live video is shutting down")
            if len(self.viewers) >= MAX_VIEWERS:
                raise VideoUnavailable("Too many live viewers")
            viewer = object()
            self.viewers.add(viewer)
            if focus:
                self.focused.add(viewer)
            self.idle_since = None
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._run, name=f"mobster-{self.kind}-video", daemon=True)
                self.worker.start()
            self.condition.notify_all()
            return viewer

    def unsubscribe(self, viewer):
        with self.condition:
            self.viewers.discard(viewer)
            self.focused.discard(viewer)
            if not self.viewers:
                self.idle_since = self.clock()
            self.condition.notify_all()

    next_frame = WdaVideo.next_frame

    def latest(self):
        with self.condition:
            return self.frame

    def peek(self, wait=PEEK_WAIT_SECONDS):
        """A current still picture (a tile): starts a stopped source at tile size and waits up to ``wait``."""
        try:
            viewer = self.subscribe(focus=False)
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
        """WdaVideo.paced_frames: new frames at most ``rate()`` a second, the same one again every ``heartbeat``."""
        sequence, sent_at, looked_at = 0, None, None
        while True:
            fps = rate()
            if isinstance(fps, (int, float)) and fps > 0 and looked_at is not None:
                wait = looked_at + 1 / fps - self.clock()
                if wait > 0:
                    self._sleep_for(wait)
                    with self.condition:
                        if self.closed or viewer not in self.viewers:
                            return
            frame = self.next_frame(viewer, sequence, timeout=min(timeout, heartbeat))
            looked_at = self.clock()
            if frame is None:
                with self.condition:
                    if self.closed or viewer not in self.viewers:
                        return
                    frame = self.frame
                if frame is None or (sent_at is not None and looked_at - sent_at < heartbeat):
                    continue
            sequence, sent_at = frame[0], looked_at
            yield frame

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def _should_run(self):
        with self.condition:
            if self.closed:
                return False
            if self.viewers:
                return True
            return self.idle_since is not None and self.clock() - self.idle_since < IDLE_GRACE_SECONDS

    def publish(self, data, key, content_type="image/jpeg"):
        now = self.clock()
        with self.condition:
            self.sequence += 1
            self.key = key
            self.frame = (self.sequence, content_type, data, int(time.time() * 1000), now, self.source_name)
            self.error = None
            self.frame_times = [t for t in self.frame_times if now - t <= 2] + [now]
            self.condition.notify_all()

    def _run(self):
        last_mode = None
        while self._should_run():
            width, quality, fps = self.mode()
            started = self.clock()
            # A change of size needs a new picture even when the screen is still.
            after = self.key if (width, quality) == last_mode else None
            try:
                pulled = self.pull(width, quality, after)
                self.pulls += 1
            except Exception as error:  # noqa: BLE001 -- reported in status; the viewer sees no new frame
                with self.condition:
                    self.error = str(error) or type(error).__name__
                self._sleep_for(RETRY_SECONDS)
                continue
            last_mode = (width, quality)
            if pulled is not None:
                self.publish(*pulled)
            else:
                with self.condition:  # the same picture: it is still current
                    if self.frame is not None:
                        self.frame = self.frame[:4] + (self.clock(),) + self.frame[5:]
            wait = 1 / fps - (self.clock() - started)
            if wait > 0:
                self._sleep_for(wait)


def read_exact(stream, size):
    """``size`` bytes from ``stream``, or None at its end."""
    chunks, left = [], size
    while left:
        chunk = stream.read(left)
        if not chunk:
            return None
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def read_frames(stream):
    """The JPEGs a helper writes: 4-byte big-endian length, then the bytes. Ends at the stream's end or a bad length."""
    while True:
        header = read_exact(stream, 4)
        if header is None:
            return
        (length,) = struct.unpack(">I", header)
        if not 0 < length <= MAX_FRAME_BYTES:
            return
        data = read_exact(stream, length)
        if data is None:
            return
        yield data


class ProcessFrameSource(WdaVideo):
    """A frame source fed by a helper process. ``command_for(fps, detail)``: the helper's argv for a quality."""

    kind = "process"

    def __init__(self, command_for, *, source="process", on_activity=None, clock=time.monotonic,
                 quality=DEFAULT_QUALITY, spawn=subprocess.Popen):
        super().__init__("http://127.0.0.1:0", mjpeg_url=None, session=None, on_activity=on_activity, clock=clock,
                         quality=quality, screenshot_fallback=False)
        self.command_for = command_for
        self.source_name = source
        self.quality = tuple(quality)
        self.spawn = spawn
        self.process = None

    def status(self):
        value = super().status()
        value.update(kind=self.kind, stream="/api/device/stream")
        return value

    def set_quality(self, fps, detail):
        settings = mjpeg_settings(fps, detail)  # validates
        with self.condition:
            self.settings, self.quality = settings, (fps, detail)
        self._stop_process()  # the loop starts it again at the new quality

    def _configure(self):
        return True

    def _stop_process(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:  # noqa: BLE001 -- it is going away either way
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass

    def _run(self):
        try:
            while self._should_run():
                try:
                    fps, detail = self.quality
                    self.process = self.spawn(list(self.command_for(fps, detail)), stdin=subprocess.PIPE,
                                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    process = self.process
                    for data in read_frames(process.stdout):
                        self._publish("image/jpeg", data, self.source_name)
                        if not self._should_run():
                            break
                    code = process.poll()
                    with self.condition:
                        if code not in (None, 0, -15):
                            self.error = f"exited {code}"
                except Exception as error:  # noqa: BLE001 -- reported in status, retried while watched
                    with self.condition:
                        self.error = type(error).__name__
                finally:
                    self._stop_process()
                deadline = self.clock() + RESTART_SECONDS
                while self._should_run() and self.clock() < deadline:
                    time.sleep(.1)
        finally:
            with self.condition:
                self.worker = None
                self.frame = None
                self.frame_times = []
            self.on_activity(False)

    def close(self):
        super().close()
        threading.Thread(target=self._stop_process, name="mobster-frames-stop", daemon=True).start()

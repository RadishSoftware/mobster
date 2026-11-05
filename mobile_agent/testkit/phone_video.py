"""`mobster test --video` on a USB iPhone, from WebDriverAgent's MJPEG stream: nothing on the phone changes."""

import http.client
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..wda.video import default_mjpeg_url, read_mjpeg_frames

HELPER = "mjpeg-mp4"
WIDTH = 590              # px: half an iPhone 15 Pro's 1179, about what a simulator's half-width frames are
CONNECT_TIMEOUT = 5.0
RETRY_SECONDS = 1.0
FINISH_WAIT = 30.0


def helper_path():
    """The helper's executable: the Mac app's, else built from source once (needs Xcode's command line tools)."""
    from .. import native_helpers
    return native_helpers.find(HELPER) or native_helpers.build("mjpeg_mp4")


class PhoneRecording:
    """A device manager that records the phone's screen while each lease is held."""

    def __init__(self, manager, path, *, log=None, clock=time.monotonic, helper=None, popen=subprocess.Popen,
                 connect=None):
        self._manager, self.path = manager, Path(path)
        self._log = log or (lambda line: None)
        self._clock, self._helper, self._popen = clock, helper, popen
        self._connect = connect or _connect
        self.started = None  # time.monotonic() of the video's first frame

    def __getattr__(self, name):
        return getattr(self._manager, name)

    def acquire(self, *args, **kwargs):
        from .execute import RecordedLease
        lease = self._manager.acquire(*args, **kwargs)
        url = getattr(lease.target, "mjpeg_url", "") or default_mjpeg_url(getattr(lease.target, "wda_url", ""))
        recorder = None
        if url:
            recorder = Recorder(url, self.path, log=self._log, clock=self._clock, helper=self._helper,
                                popen=self._popen, connect=self._connect, on_first=self._first)
            recorder.start()
        else:
            self._log("no screen recording: the phone has no video stream")
        return RecordedLease(lease, recorder)

    def _first(self, at):
        self.started = at


class Recorder:
    """One recording: the stream reader thread and the helper it feeds. ``stop()`` finishes the file."""

    def __init__(self, url, path, *, log, clock, helper, popen, connect, on_first):
        self.url, self.path = url, Path(path)
        self._log, self._clock, self._helper, self._popen = log, clock, helper, popen
        self._connect, self._on_first = connect, on_first
        self._stop = threading.Event()
        self._thread = None
        self._process = None
        self._connection = None
        self._lock = threading.Lock()
        self._first = None
        self._last = None        # (ms, jpeg) of the last frame sent
        self.frames = 0

    def start(self):
        self._thread = threading.Thread(target=self._run, name="mobster-phone-video", daemon=True)
        self._thread.start()

    def _open_helper(self):
        helper = self._helper or helper_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return self._popen([str(helper), str(self.path), "--width", str(WIDTH)], stdin=subprocess.PIPE,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)

    def _run(self):
        try:
            process = self._open_helper()
        except Exception as error:
            self._log(f"no screen recording ({type(error).__name__}: {error})")
            return
        with self._lock:
            self._process = process
        while not self._stop.is_set():
            try:
                connection, frames = self._connect(self.url)
                with self._lock:
                    self._connection = connection
                for jpeg in frames:
                    if self._stop.is_set():
                        break
                    self._send(jpeg)
            except Exception:
                pass  # a dropped stream (WebDriverAgent restarting) is opened again until the lease ends
            finally:
                with self._lock:
                    connection, self._connection = self._connection, None
                _close(connection)
            self._stop.wait(RETRY_SECONDS)

    def _send(self, jpeg, now=None):
        now = self._clock() if now is None else now
        if self._first is None:
            self._first = now
            self._on_first(now)
        ms = round((now - self._first) * 1000)
        if self._last is not None and (jpeg == self._last[1] or ms <= self._last[0]):
            return
        self._write(ms, jpeg)

    def _write(self, ms, jpeg, process=None):
        process = process or self._process
        if process is None or process.stdin is None:
            return
        try:
            process.stdin.write(struct.pack(">QI", ms, len(jpeg)) + jpeg)
            self._last = (ms, jpeg)
            self.frames += 1
        except (BrokenPipeError, OSError, ValueError):
            self._stop.set()

    def stop(self):
        """End the stream, show the last frame until now, and let the helper finish the MP4."""
        self._stop.set()
        with self._lock:
            connection = self._connection
        _close(connection)
        if self._thread is not None:
            self._thread.join(CONNECT_TIMEOUT + 2)
        with self._lock:
            process, self._process = self._process, None
        if process is None:
            return
        if self._last is not None and self._first is not None:
            ms = round((self._clock() - self._first) * 1000)
            if ms > self._last[0]:
                self._write(ms, self._last[1], process)
        error = b""
        try:
            process.stdin.close()
            error = process.stderr.read() if process.stderr else b""
            process.wait(timeout=FINISH_WAIT)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        if process.returncode != 0 or not self.frames:
            self._log("no screen recording" + (f": {error.decode(errors='replace').strip()[-200:]}" if error else ""))
            try:
                self.path.unlink()
            except OSError:
                pass


def _connect(url):
    """(socket, frames): WebDriverAgent's MJPEG stream at ``url`` and its JPEG frames."""
    # Shutting the socket down (_close) ends a read that waits on a still screen at once.
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=CONNECT_TIMEOUT)
    connection.request("GET", parts.path or "/")
    sock = connection.sock
    response = connection.getresponse()
    if response.status != 200:
        connection.close()
        raise ConnectionError(f"MJPEG server answered {response.status}")
    return sock, read_mjpeg_frames(response)


def _close(sock):
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        sock.close()
    except Exception:
        pass


__all__ = ["PhoneRecording", "Recorder", "helper_path"]

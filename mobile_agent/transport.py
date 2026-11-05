"""Bounded HTTP with one deadline per operation and no automatic retry: an interrupted request's outcome is unknown."""

import http.client
import json
import math
import select
import socket
import threading
import time
from urllib.parse import urlsplit

from . import __version__
from .errors import MobsterError


def user_agent():
    """The shared client identity used when calling Mobster services."""
    import platform
    return f"Mobster/{__version__} (macOS {platform.mac_ver()[0] or 'unknown'})"


class TransportError(MobsterError):
    """``status`` and ``wda_error``: a failed answer's HTTP status and WDA's error name, when there was an answer.
    Not its message, which can hold app text."""

    def __init__(self, *args, status=None, wda_error=None):
        super().__init__(*args)
        self.status, self.wda_error = status, wda_error


# The request never left, so its outcome is known: nothing happened.
class NotSent(TransportError):
    pass


# WDA checks the session before it runs anything, so nothing ran.
class StaleSession(NotSent):
    pass


# Every error name WebDriverAgent v16.12.10 answers with, and the one HTTP status it pairs with each
# (WebDriverAgentLib/Routing/FBCommandStatus.m). Anything else isn't WDA's name and is never kept: it could be app text.
WDA_ERRORS = {
    "unknown error": 500, "unable to capture screen": 500, "no such element": 404, "invalid element state": 400,
    "invalid argument": 400, "stale element reference": 404, "invalid selector": 400, "no such alert": 404,
    "unexpected alert open": 500, "unknown method": 405, "session not created": 500, "invalid coordinates": 400,
    "unsupported operation": 500, "unknown command": 404, "timeout": 408, "element not visible": 400,
    "invalid session id": 404,
}


def error_name(value):
    """WDA's error name from an answer's ``value``, or None: only one of WDA_ERRORS' names, exactly."""
    name = value.get("error") if isinstance(value, dict) else None
    return name if isinstance(name, str) and name in WDA_ERRORS else None


def answer_error(raw):
    """Strictly decoded: a body with two ``error`` fields, say, names nothing."""
    try:
        return error_name(decode_json(raw).get("value"))
    except (ValueError, AttributeError, UnicodeDecodeError, RecursionError):
        return None


class Deadline:
    def __init__(self, timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be a finite positive number")
        self.end = time.monotonic() + timeout

    def remaining(self):
        left = self.end - time.monotonic()
        if left <= 0:
            raise TimeoutError("Operation deadline exceeded")
        return left


def _unique_object(pairs):
    # dict(pairs) then a length check: 4-6% faster than a Python loop on a 66 KB WDA tree (~400 us), 2026-09-24.
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON field")
    return result


def _invalid_constant(value):
    raise ValueError("Nonfinite JSON value")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON value")
    return result


def decode_json(raw):
    return json.loads(raw, parse_constant=_invalid_constant, parse_float=_finite_float,
                      object_pairs_hook=_unique_object)


class HTTP:
    def __init__(self, base_url, key="", timeout=20):
        url = urlsplit(base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError("Expected HTTP(S) base URL without embedded credentials")
        if key and url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote credentials require HTTPS")
        cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        self.connection = cls(url.hostname, url.port, timeout=timeout)
        self.prefix, self.key = url.path.rstrip("/"), key
        self._inflight = threading.Lock()

    def request(self, method, path, body=None, timeout=20):
        deadline = Deadline(timeout)
        if not isinstance(path, str) or not path.startswith("/") or any(c in path for c in "\r\n"):
            raise ValueError("Expected an absolute HTTP request path")
        # Encoding/validation precedes dispatch; invalid input cannot poison a live connection.
        payload = None if body is None else json.dumps(body, allow_nan=False).encode()
        if not self._inflight.acquire(blocking=False):
            raise TransportError("HTTP client already has a request in flight; not retried")
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        guard, finished = threading.Lock(), threading.Event()
        active_socket = {"value": None}
        answered_in_full = False

        def expire():
            # Socket timeouts restart on each chunk: cut a slow drip at the absolute deadline.
            with guard:
                if finished.is_set():
                    return
                sock = active_socket["value"] or self.connection.sock
                if sock:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                self.connection.close()

        timer = threading.Timer(max(0, deadline.end - time.monotonic()), expire)
        timer.daemon = True
        timer.start()
        try:
            if self.connection.sock is not None and peer_closed(self.connection.sock):
                # A kept connection the server has closed since, as WDA's failClient does after a header-level 400
                # with no Connection header. Nothing of this request is sent yet, so a new connection resends nothing.
                self.connection.close()
            self.connection.timeout = deadline.remaining()
            if self.connection.sock:
                self.connection.sock.settimeout(deadline.remaining())
            self.connection.request(method, self.prefix + path, payload, headers)
            active_socket["value"] = self.connection.sock
            deadline.remaining()
            response = self.connection.getresponse()
            raw = response.read(16_000_001)
            deadline.remaining()
            if len(raw) > 16_000_000:
                raise TransportError("HTTP response exceeds limit; outcome unknown; not retried")
            if isinstance(getattr(response, "length", None), int) and response.length != 0:
                raise TransportError("Incomplete HTTP response; outcome unknown; not retried")
            if response.status >= 300:
                # Read to its end, so the connection is ready for the next request. WebDriverAgent keeps a descriptor
                # per connection until it restarts: routine 404s would each cost one.
                answered_in_full = response.isclosed()
                name = answer_error(raw)
                if response.status == 404 and name == "invalid session id":
                    raise StaleSession("HTTP 404 StaleSession: invalid session id; nothing sent; not retried")
                raise TransportError(f"HTTP {response.status}; request not retried",
                                     status=response.status, wda_error=name)
            result = decode_json(raw)
            deadline.remaining()
            if not isinstance(result, dict):
                raise TransportError("HTTP response must be an object; outcome unknown; not retried")
            return result
        except TransportError:
            if not answered_in_full:
                self.connection.close()
            raise
        except (OSError, http.client.HTTPException, ValueError, RecursionError) as exc:
            self.connection.close()
            if isinstance(exc, ConnectionRefusedError):
                raise NotSent(f"HTTP {type(exc).__name__}; nothing sent; not retried") from None
            raise TransportError(f"HTTP {type(exc).__name__}; request outcome unknown; not retried") from None
        finally:
            with guard:
                finished.set()
                timer.cancel()
            self._inflight.release()

    def close(self):
        self.connection.close()


def peer_closed(sock):
    """Whether an idle kept socket has anything to read, which on an idle HTTP connection means the server closed it
    (or sent something unasked). Either way it can't carry the next request. A socket that can't be asked is closed."""
    try:
        if getattr(sock, "pending", lambda: 0)():
            return True
        # poll, not select: select refuses a descriptor numbered 1024 or more, which would read every socket as closed
        # and bring back the per-request connections #2058 removed.
        probe = select.poll()
        probe.register(sock, select.POLLIN)
        return bool(probe.poll(0))
    except (OSError, ValueError):
        return True


def request_once(base_url, method, path, body=None, timeout=20):
    client = HTTP(base_url)
    try:
        return client.request(method, path, body, timeout=timeout)
    finally:
        client.close()


# A new connection per repeated read ran WebDriverAgent out of file descriptors in about 14 minutes (17 Oct).
KEPT_MAX = 16
CLOSED_WHILE_KEPT = ("RemoteDisconnected", "BrokenPipeError", "ConnectionResetError", "ConnectionAbortedError",
                     "BadStatusLine")
_kept = {}  # base url -> (HTTP, lock), oldest first
_kept_lock = threading.Lock()


def read_kept(base_url, path, timeout=2.0):
    # Reads only: a kept connection the helper closed while idle is asked once more, which is safe for a GET.
    base = base_url.rstrip("/")
    deadline = Deadline(timeout)
    with _kept_lock:
        entry = _kept.get(base)
        while entry is None and len(_kept) >= KEPT_MAX:
            for address, (old_client, old_lock) in _kept.items():
                if old_lock.acquire(blocking=False):
                    del _kept[address]
                    old_client.close()
                    old_lock.release()
                    break
            else:
                break
        if entry is None and len(_kept) < KEPT_MAX:
            entry = (HTTP(base), threading.Lock())
            _kept[base] = entry
        acquired = entry is not None and entry[1].acquire(blocking=False)
        if acquired:
            _kept.pop(base)
            _kept[base] = entry
    if not acquired:
        return request_once(base, "GET", path, timeout=deadline.remaining())
    client, lock = entry
    try:
        reused = client.connection.sock is not None
        try:
            return client.request("GET", path, timeout=deadline.remaining())
        except TransportError as error:
            if not reused or not any(name in str(error) for name in CLOSED_WHILE_KEPT):
                raise
            return client.request("GET", path, timeout=deadline.remaining())
    finally:
        lock.release()


def wda_screenshot(wda_url, timeout=2):
    try:
        value = request_once(wda_url, "GET", "/screenshot", timeout=timeout).get("value")
        return "data:image/png;base64," + value if isinstance(value, str) and value else None
    except Exception:
        return None

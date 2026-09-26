"""Bounded HTTP transport with a shared per-operation deadline. No replay.

``HTTP`` is one persistent ``http.client`` connection with a single in-flight request:
absolute ``Deadline`` across status/headers/body, response size cap, and no automatic
retry — an interrupted request's outcome is unknown.

``Deadline`` is a monotonic budget shared by every phase of one operation.
"""

import http.client
import json
import math
import socket
import threading
import time
from urllib.parse import urlsplit

from .errors import MobsterError


class TransportError(MobsterError):
    pass


class Deadline:
    """A single monotonic budget shared by every phase of one operation."""
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
    # dict(pairs) in C, then a length check, instead of a per-pair Python loop:
    # same result and error, 4-6% faster on a 66 KB WDA tree (~400 us), 2026-09-24.
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
    """Strict JSON: duplicate keys, NaN/Infinity and overflowing floats are errors."""
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

        def expire():
            # Socket timeouts alone restart on each received chunk. Interrupt a slow-drip
            # status/header/body at the absolute deadline instead of extending the run.
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
                raise TransportError("HTTP response exceeds limit")
            if isinstance(getattr(response, "length", None), int) and response.length != 0:
                raise TransportError("Incomplete HTTP response; outcome unknown; not retried")
            if response.status >= 300:
                raise TransportError(f"HTTP {response.status}; request not retried")
            result = decode_json(raw)
            deadline.remaining()
            if not isinstance(result, dict):
                raise TransportError("HTTP response must be an object")
            return result
        except TransportError:
            self.connection.close()
            raise
        except (OSError, http.client.HTTPException, ValueError, RecursionError) as exc:
            self.connection.close()
            # A refused connection never carried the request, so its outcome is known: nothing happened.
            outcome = "nothing sent" if isinstance(exc, ConnectionRefusedError) else "request outcome unknown"
            raise TransportError(f"HTTP {type(exc).__name__}; {outcome}; not retried") from None
        finally:
            with guard:
                finished.set()
                timer.cancel()
            self._inflight.release()

    def close(self):
        self.connection.close()

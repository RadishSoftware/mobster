"""Warm model connections shared across runs in one process.

Each run used to build new model clients, so its first call paid a fresh TLS
handshake: the first Jev decision of a run was 419 ms p50 against 249 ms warm
(latency breakdown, 23 Sep 2026). The pool keeps idle ``HTTP``
clients keyed by base URL and a hash of the credential, and hands them to the
next run.

Rules that keep this as safe as a fresh connection:

* A pooled client is owned by exactly one ``PooledHTTP`` at a time. Two
  borrowers never share a connection, so speculation side channels stay
  independent.
* Only a client that finished its last request cleanly and has no request in
  flight goes back to the pool. Closing a borrower while a request is running
  aborts that request, exactly as ``HTTP.close`` always did.
* Before reuse, a client whose socket is readable (the peer closed it or sent
  unsolicited bytes) or that sat idle past ``max_idle`` is discarded. The
  transport never retries, so reusing a dead socket would fail a real call.
* Credentials are never stored in the pool key, only a SHA-256 digest.
"""

import hashlib
import select
import threading
import time

from .transport import HTTP


# Seconds an idle connection may wait in the pool, kept well under what each
# front end allows so a reused socket is not closed as a request goes out.
# Measured 2026-09-23 after one request: aiplatform.googleapis.com closed an
# idle keep-alive connection at 240 s; api.typesafe.ai kept it past 900 s.
MAX_IDLE_SECONDS = 120
IDLE_LIMITS = {"https://aiplatform.googleapis.com": 180, "https://api.typesafe.ai/v1": 600}
MAX_IDLE_PER_KEY = 4


def _digest(key):
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


def _dropped(client):
    """True when the socket is closed or has unread bytes (never safe to reuse)."""
    sock = getattr(client.connection, "sock", None)
    if sock is None:
        return True
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return True
    return bool(readable)


class ConnectionPool:
    def __init__(self, max_idle=None, per_key=MAX_IDLE_PER_KEY, factory=HTTP, clock=time.monotonic):
        # None: per-host limits from IDLE_LIMITS, else MAX_IDLE_SECONDS.
        self.max_idle, self.per_key = max_idle, per_key
        self._factory, self._clock = factory, clock
        self._idle = {}
        self._lock = threading.Lock()
        self.stats = {"created": 0, "reused": 0, "discarded": 0, "released": 0}

    def acquire(self, base_url, key=""):
        pool_key = (base_url, _digest(key))
        stale = []
        client = None
        reused = False
        with self._lock:
            idle = self._idle.get(pool_key, [])
            while idle:
                candidate, since = idle.pop()
                limit = self.max_idle if self.max_idle is not None else IDLE_LIMITS.get(base_url, MAX_IDLE_SECONDS)
                if self._clock() - since <= limit and not _dropped(candidate):
                    client = candidate
                    reused = True
                    self.stats["reused"] += 1
                    break
                stale.append(candidate)
                self.stats["discarded"] += 1
            if client is None:
                self.stats["created"] += 1
        for old in stale:
            old.close()
        if client is None:
            client = self._factory(base_url, key)
        client.key = key
        # Read by PooledHTTP for the trace's ``reused`` flag on a fresh borrow.
        client.pool_reused = reused
        return client

    def release(self, base_url, key, client):
        """Return a quiet, connected client; close anything else."""
        inflight = getattr(client, "_inflight", None)
        busy = inflight is not None and inflight.locked()
        connected = getattr(client.connection, "sock", None) is not None
        if busy or not connected:
            client.close()
            return False
        client.key = key
        pool_key = (base_url, _digest(key))
        extra = None
        with self._lock:
            idle = self._idle.setdefault(pool_key, [])
            idle.append((client, self._clock()))
            self.stats["released"] += 1
            if len(idle) > self.per_key:
                extra, _ = idle.pop(0)
        if extra is not None:
            extra.close()
        return True

    def prewarm(self, base_url, key="", timeout=5.0):
        """Open one connection (TCP + TLS) off the caller's thread and pool it."""
        def run():
            try:
                client = self._factory(base_url, key)
                with self._lock:
                    self.stats["created"] += 1
                client.connection.timeout = timeout
                client.connection.connect()
                self.release(base_url, key, client)
            except Exception:
                pass  # Warming is an optimization; the first real call connects itself.
        thread = threading.Thread(target=run, name="mobster-prewarm", daemon=True)
        thread.start()
        return thread

    def idle_count(self, base_url=None, key=None):
        with self._lock:
            if base_url is None:
                return sum(len(v) for v in self._idle.values())
            return len(self._idle.get((base_url, _digest(key)), []))

    def clear(self):
        with self._lock:
            clients = [client for idle in self._idle.values() for client, _ in idle]
            self._idle.clear()
        for client in clients:
            client.close()


POOL = ConnectionPool()


class PooledHTTP:
    """``HTTP``'s interface over a client borrowed from a pool.

    ``close()`` returns the client (or aborts it if a request is still running);
    a later ``request()`` borrows again, as ``http.client`` reconnects after close.
    """

    def __init__(self, base_url, key="", pool=None):
        self.base_url, self._key = base_url, key
        self._pool = POOL if pool is None else pool
        self._client = None
        self._active = 0
        self._fresh_borrow = False
        self._lock = threading.Lock()
        # Whether the latest request went out on a connection that was already
        # open (pooled, prewarmed, or this borrower's own), for the latency trace.
        self.last_reused = None

    def _borrow(self):
        # Caller holds self._lock.
        if self._client is None:
            self._client = self._pool.acquire(self.base_url, self._key)
            self._fresh_borrow = True
        return self._client

    @property
    def connection(self):
        with self._lock:
            return self._borrow().connection

    @property
    def key(self):
        return self._key

    @key.setter
    def key(self, value):
        # Vertex sets a short-lived bearer token around each request and clears
        # it afterwards; the pool key stays the construction-time credential.
        with self._lock:
            self._borrow().key = value

    def request(self, method, path, body=None, timeout=20):
        with self._lock:
            if self._active > 0:
                # This borrower's connection carries a request already (an answer probe and
                # the finishing extraction on one channel, 24 Sep: "already has a request in
                # flight"). A second connection from the pool serves this one, then returns.
                extra = self._pool.acquire(self.base_url, self._key)
                if self._client is not None:
                    extra.key = self._client.key  # Carry a per-request bearer token (Vertex).
                # last_reused is left alone: the in-flight request reads it when it
                # returns, and this concurrent one must not overwrite its value.
            else:
                extra = None
                client = self._borrow()
                if self._fresh_borrow:
                    self._fresh_borrow = False
                    self.last_reused = bool(getattr(client, "pool_reused", False))
                else:
                    self.last_reused = getattr(client.connection, "sock", None) is not None
                self._active += 1
        if extra is not None:
            try:
                return extra.request(method, path, body, timeout)
            finally:
                self._pool.release(self.base_url, self._key, extra)
        try:
            return client.request(method, path, body, timeout)
        finally:
            with self._lock:
                self._active -= 1

    def close(self):
        with self._lock:
            client, self._client = self._client, None
            busy = self._active > 0
        if client is None:
            return
        if busy:
            client.close()  # Abort the in-flight request, exactly like HTTP.close.
        else:
            self._pool.release(self.base_url, self._key, client)


def pooled_http(base_url, key=""):
    return PooledHTTP(base_url, key)

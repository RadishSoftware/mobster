"""Warm model connections shared across runs in one process, each borrowed by one caller at a time."""

import hashlib
import threading
import time

from .transport import HTTP, peer_closed

# Under each front end's own limit (2026-09-23: aiplatform.googleapis.com closed one at 240 s, api.typesafe.ai at 900+).
MAX_IDLE_SECONDS = 120
IDLE_LIMITS = {"https://aiplatform.googleapis.com": 180, "https://api.typesafe.ai/v1": 600}
MAX_IDLE_PER_KEY = 4


def _digest(key):
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


class ConnectionPool:
    def __init__(self, max_idle=None, per_key=MAX_IDLE_PER_KEY, factory=HTTP, clock=time.monotonic):
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
                sock = getattr(candidate.connection, "sock", None)
                if self._clock() - since <= limit and sock is not None and not peer_closed(sock):
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

    def clear(self):
        with self._lock:
            clients = [client for idle in self._idle.values() for client, _ in idle]
            self._idle.clear()
        for client in clients:
            client.close()


POOL = ConnectionPool()


class PooledHTTP:
    def __init__(self, base_url, key="", pool=None):
        self.base_url, self._key = base_url, key
        self._pool = POOL if pool is None else pool
        self._client = None
        self._active = 0
        self._fresh_borrow = False
        self._lock = threading.Lock()
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
        # Vertex sets a short-lived bearer token per request; the pool key stays the construction-time credential.
        with self._lock:
            self._borrow().key = value

    def request(self, method, path, body=None, timeout=20):
        with self._lock:
            if self._active > 0:
                # The borrower's connection carries a request already (24 Sep): a second one serves this, then returns.
                extra = self._pool.acquire(self.base_url, self._key)
                if self._client is not None:
                    extra.key = self._client.key  # Carry a per-request bearer token (Vertex).
                # last_reused stays the in-flight request's.
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

"""The run's local proxy: it shapes one simulator's traffic with a network profile and answers ``stubs:``.

It listens on 127.0.0.1 only, serves one request per connection, and holds no state outside its own sockets: stopping
it, or the process dying, ends every effect. Plain http requests and https requests to a stubbed host are read and
answered or forwarded; every other https connection is a tunnel the proxy relays without opening."""

import asyncio
import random
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import netstubs

HEAD_LIMIT = 1 << 20
BODY_LIMIT = 8 << 20
HEAD_TIMEOUT = 30
STALL_SECONDS = 1.0     # what a lost packet costs: about one retransmission timeout
CHUNK = 16384
ROW_LIMIT = 500
CAPTURE_LIMIT = 200       # requests kept whole for wire assertions (verify/wire.py)
CAPTURE_BYTES = 65536     # of each request body and each response, as sent
SKIPPED_HEADERS = ("proxy-connection", "connection", "keep-alive", "proxy-authorization", "te", "upgrade")


@dataclass(frozen=True)
class Profile:
    """A link: round-trip time, bandwidth each way, the share of chunks that stall, the share of connections that
    reset."""
    rtt_ms: int = 0
    down_kbps: int = 0    # 0 is unlimited
    up_kbps: int = 0
    stall_pct: float = 0
    reset_pct: float = 0
    offline: bool = False


PROFILES = {
    "offline": Profile(offline=True),
    "slow-3g": Profile(rtt_ms=400, down_kbps=400, up_kbps=400, stall_pct=2),
    "lte": Profile(rtt_ms=50, down_kbps=12000, up_kbps=5000),
    "flaky": Profile(rtt_ms=150, down_kbps=1500, up_kbps=750, stall_pct=5, reset_pct=10),
}


class ProxyError(Exception):
    pass


def _reset(writer):
    """Close with a TCP reset, which is what a dropped link looks like to the app."""
    sock = writer.get_extra_info("socket")
    try:
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:
        pass
    writer.transport.abort()


def _parse_head(raw):
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) != 3:
        raise ValueError("bad request line")
    headers = []
    for line in lines[1:]:
        if line:
            name, _, value = line.partition(":")
            headers.append((name.strip(), value.strip()))
    return parts[0], parts[1], headers


def _header(headers, name):
    return next((value for key, value in headers if key.lower() == name), None)


class _Link:
    """One connection's clock: which way data last went, so a change of direction costs half a round trip."""

    def __init__(self):
        self.last = None
        self.started = time.monotonic()


class Proxy:
    def __init__(self, profile=None, stubs=(), authority=None, *, resolve=None, upstream_ssl=None, seed=0,
                 watch=None):
        self.profile = profile or Profile()
        self.stubs = tuple(stubs)
        # Host patterns whose https the proxy opens so wire assertions see paths and bodies; None keeps no captures.
        self.watch = tuple(watch) if watch is not None else None
        self._captures = [] if watch is not None else None
        self.authority = authority
        self._resolve = dict(resolve or {})       # host -> (address, port), for tests and local origins
        self._upstream_ssl = upstream_ssl
        self._rng = random.Random(seed)
        self._rows, self.total = [], 0
        self._started = time.monotonic()
        self.port = None
        self._loop = self._thread = self._stop = self._server = None
        self._error = None
        self._ready = threading.Event()
        self._tasks = set()

    # lifecycle

    def start(self):
        self._thread = threading.Thread(target=self._main, name="mobster-net-proxy", daemon=True)
        self._thread.start()
        self._ready.wait(15)
        if self._error is not None or self.port is None:
            why = type(self._error).__name__ if self._error else "timeout"
            raise ProxyError(f"The network proxy didn't start ({why}).")
        return self.port

    def stop(self):
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(10)

    def rows(self):
        return [dict(row) for row in self._rows]

    def captures(self):
        """The requests seen whole (wire assertions only): method, host, path, headers, body and response prefix."""
        return [dict(item) for item in self._captures or ()]

    def _main(self):
        loop = self._loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._serve())
        except BaseException as error:  # reported by start()
            self._error = error
            self._ready.set()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _serve(self):
        self._stop = asyncio.Event()
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0, limit=HEAD_LIMIT)
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()
        await self._stop.wait()
        self._server.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # shaping

    async def _shape(self, link, way, size):
        profile = self.profile
        if link.last != way and profile.rtt_ms:
            await asyncio.sleep(profile.rtt_ms / 2000)
        link.last = way
        if profile.stall_pct and self._rng.random() * 100 < profile.stall_pct:
            await asyncio.sleep(STALL_SECONDS)
        kbps = profile.down_kbps if way == "down" else profile.up_kbps
        if kbps:
            await asyncio.sleep(size * 8 / (kbps * 1000))

    async def _send(self, writer, data, link, way):
        for start in range(0, len(data), CHUNK):
            piece = data[start:start + CHUNK]
            await self._shape(link, way, len(piece))
            writer.write(piece)
            await writer.drain()

    # rows

    def _row(self, **row):
        self.total += 1
        row = {"atMs": round((time.monotonic() - self._started) * 1000), "via": "", "status": None, "bytes": 0,
               "ms": None, **row}
        if len(self._rows) < ROW_LIMIT:
            self._rows.append(row)
        return row

    @staticmethod
    def _finish(row, link, **fields):
        row.update(fields, ms=round((time.monotonic() - link.started) * 1000))

    # connections

    async def _client(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        link = _Link()
        try:
            await self._handle(reader, writer, link)
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError,
                ssl.SSLError, OSError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            self._tasks.discard(task)
            try:
                writer.close()
            except Exception:
                pass

    async def _read_head(self, reader):
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)

    async def _handle(self, reader, writer, link):
        raw = await self._read_head(reader)
        method, target, headers = _parse_head(raw)
        host = urlsplit(target).hostname if "://" in target else None
        if not host:
            host = target.rsplit(":", 1)[0] if method == "CONNECT" else (_header(headers, "host") or "").split(":")[0]
        row = self._row(method=method, host=host or "", path="" if method == "CONNECT" else urlsplit(target).path)
        if self.profile.offline or (self.profile.reset_pct and self._rng.random() * 100 < self.profile.reset_pct):
            self._finish(row, link, via="refused")
            _reset(writer)
            return
        if self.profile.rtt_ms:
            await asyncio.sleep(self.profile.rtt_ms / 1000)   # the connection's own handshake
        if method == "CONNECT":
            name, _, port = target.rpartition(":")
            opened = [stub["host"] for stub in self.stubs] + list(self.watch or ())
            if any(netstubs.host_matches(pattern, name) for pattern in opened) and self.authority is not None:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                await writer.start_tls(self.authority.context(name))
                raw = await self._read_head(reader)
                method, target, headers = _parse_head(raw)
                row.update(method=method, path=urlsplit(target).path)
                await self._request(reader, writer, link, row, method, target, headers, "https", name, int(port or 443))
            else:
                await self._tunnel(reader, writer, link, row, name, int(port or 443))
        else:
            split = urlsplit(target)
            await self._request(reader, writer, link, row, method, target, headers, split.scheme or "http",
                                host, split.port or 80)

    async def _connect(self, host, port, tls):
        address, number = self._resolve.get(host, (host, port))
        if not tls:
            return await asyncio.open_connection(address, number, limit=HEAD_LIMIT)
        context = self._upstream_ssl or ssl.create_default_context()
        return await asyncio.open_connection(address, number, ssl=context, server_hostname=host, limit=HEAD_LIMIT)

    async def _tunnel(self, reader, writer, link, row, host, port):
        row["via"] = "tunnel"   # while it's open too: a no_request judged mid-run must see this connection
        try:
            upstream_reader, upstream_writer = await self._connect(host, port, False)
        except OSError:
            self._finish(row, link, via="tunnel", status=502)
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        sizes = {"up": 0, "down": 0}

        async def pipe(source, sink, way):
            try:
                while data := await source.read(CHUNK):
                    sizes[way] += len(data)
                    await self._send(sink, data, link, way)
            except (ConnectionError, OSError):
                pass
            finally:
                sink.close()
        try:
            await asyncio.gather(pipe(reader, upstream_writer, "up"), pipe(upstream_reader, writer, "down"))
        finally:
            self._finish(row, link, via="tunnel", status=200, bytes=sizes["down"])
            upstream_writer.close()

    async def _body(self, reader, headers):
        """The request's body as sent (chunked framing kept), or b"" when it has none."""
        if "chunked" in (_header(headers, "transfer-encoding") or "").lower():
            body = b""
            while True:
                size_line = await reader.readuntil(b"\r\n")
                body += size_line
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
                body += await reader.readexactly(size + 2) if size else await reader.readuntil(b"\r\n")
                if not size:
                    return body
                if len(body) > BODY_LIMIT:
                    raise ValueError("body too large")
        length = int(_header(headers, "content-length") or 0)
        if length > BODY_LIMIT:
            raise ValueError("body too large")
        return await reader.readexactly(length) if length else b""

    async def _request(self, reader, writer, link, row, method, target, headers, scheme, host, port):
        body = await self._body(reader, headers)
        split = urlsplit(target)
        path = split.path or "/"
        origin = path + (f"?{split.query}" if split.query else "")
        await self._shape(link, "up", len(body))
        seen = None
        if self._captures is not None and len(self._captures) < CAPTURE_LIMIT:
            seen = {"atMs": row["atMs"], "method": method, "host": host, "path": origin, "headers": list(headers),
                    "body": body[:CAPTURE_BYTES], "status": None, "response": b""}
            self._captures.append(seen)
        stub = next((s for s in self.stubs if netstubs.matches(s, host, method, path)), None)
        if stub is not None:
            await self._stub(writer, link, row, stub, seen)
            return
        kept = "".join(f"{name}: {value}\r\n" for name, value in headers if name.lower() not in SKIPPED_HEADERS)
        head = f"{method} {origin} HTTP/1.1\r\n" + kept + "Connection: close\r\n\r\n"
        try:
            upstream_reader, upstream_writer = await self._connect(host, port, scheme == "https")
        except (OSError, ssl.SSLError):
            self._finish(row, link, via="pass", status=502)
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        try:
            upstream_writer.write(head.encode("latin-1") + body)
            await upstream_writer.drain()
            first, size = True, 0
            while data := await upstream_reader.read(CHUNK):
                if seen is not None and len(seen["response"]) < CAPTURE_BYTES:
                    seen["response"] += data[:CAPTURE_BYTES - len(seen["response"])]
                if first:
                    first = False
                    try:
                        row["status"] = int(data.split(b" ", 2)[1])
                        if seen is not None:
                            seen["status"] = row["status"]
                    except (IndexError, ValueError):
                        pass
                size += len(data)
                await self._send(writer, data, link, "down")
            self._finish(row, link, via="pass", bytes=size)
        finally:
            upstream_writer.close()

    async def _stub(self, writer, link, row, stub, seen=None):
        status, kind, body = netstubs.answer(stub)
        if stub["delay_ms"]:
            await asyncio.sleep(stub["delay_ms"] / 1000)
        reason = {200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
                  403: "Forbidden", 404: "Not Found", 429: "Too Many Requests", 500: "Internal Server Error",
                  503: "Service Unavailable"}.get(status, "Stub")
        head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {kind}\r\nContent-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n").encode()
        if seen is not None:
            seen.update(status=status, response=(head + body)[:CAPTURE_BYTES])
        await self._send(writer, head + body, link, "down")
        self._finish(row, link, via="stub", status=status, bytes=len(body))

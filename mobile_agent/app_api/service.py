"""The App API over HTTP: who may call (this Mac, the bearer token), what they may ask (a GET of an endpoint, or a
session: sessions.py), and the answer (JSON, CSV, an event stream, a video)."""

import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from . import endpoints as endpoints_
from . import openapi, reading

LOCAL_CLIENTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
MAX_QUERY_FIELDS = 8
MAX_BODY = 16_000
SESSION_PATH = re.compile(r"/v1/sessions/([a-f0-9]{12})(?:/(end|events|screen|live|recording|logs|view|stream))?")
VIEW_FPS = 15
BOUNDARY = "mobsterframe"


@dataclass
class Answer:
    status: int
    body: bytes
    content_type: str = "application/json"
    headers: dict = field(default_factory=dict)
    stream: object = None             # an iterator of bytes sent after the headers (SSE, MJPEG); then the body is b""

    @property
    def json(self):
        return json.loads(self.body)


def _json(value, status=200, **headers):
    return Answer(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), headers=headers)


def _error(status, code, message, **headers):
    if status == 409:
        headers.setdefault("Retry-After", "5")
    return _json({"error": message, "code": code}, status, **headers)


class AppAPI:
    """One App API: its endpoints, the tokens it takes (``tokens``, tokens.TokenStore), and the phone side (``toolset``,
    mcp_server/tools.py)."""

    def __init__(self, packs, toolset, tokens, *, port, version="", health_dir=None, profiles=None, installed=None,
                 client_factory=None, sessions=None, clock=time.monotonic, sleep=time.sleep,
                 deadline=reading.READ_SECONDS):
        self.endpoints = {e.path: e for e in endpoints_.endpoints(packs)}
        self.toolset, self.tokens = toolset, tokens
        self.port, self.version = port, version
        self.health_dir = health_dir
        self.profiles = profiles
        self.client_factory = client_factory      # read_screen's model (screen.default_client when None)
        self.installed = installed or (lambda phone: None)
        self.clock, self.sleep, self.deadline = clock, sleep, deadline
        self.sessions = sessions                  # sessions.Sessions, or None: no session endpoints

    def _local(self, headers, client):
        """An Answer refusing a request that isn't from this Mac to this server, or None."""
        if client not in LOCAL_CLIENTS:
            return _error(403, "local_only", "The App API answers only this Mac. Call it from this Mac.")
        if headers.get("host", "") not in {f"{name}:{self.port}" for name in LOCAL_HOSTS}:
            # A web page that renamed a host to 127.0.0.1 (DNS rebinding) still sends its own Host.
            return _error(403, "local_only", "The App API answers only requests to 127.0.0.1 or localhost on its "
                                             "port. Call it at that address.")
        return None

    def _client(self, headers):
        """The token's client name, or None when the request carries no token this server knows."""
        supplied = headers.get("authorization", "")
        given = supplied[7:].strip() if supplied[:7].casefold() == "bearer " else ""
        record = self.tokens.verify(given) if given else None
        return (record or {}).get("name") or ("" if record is not None else None)

    def handle(self, method, target, headers, client, body=None):
        """One request: ``headers`` is a mapping or (name, value) pairs, any case; ``client`` the caller's address;
        ``body`` the request's bytes (a POST's JSON). Fails closed: anything unsure is refused."""
        pairs = list(headers.items() if hasattr(headers, "items") else headers or ())
        names = [str(k).casefold() for k, _v in pairs]
        if any(names.count(name) > 1 for name in ("host", "authorization", "origin")):
            return _error(400, "bad_headers", "Send Host and Authorization once each.")
        headers = {str(k).casefold(): str(v) for k, v in pairs}
        refused = self._local(headers, client)
        if refused is not None:
            return refused
        parts = urlsplit(target)
        path = parts.path.rstrip("/") or "/"
        try:
            query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=bool(parts.query),
                             max_num_fields=MAX_QUERY_FIELDS)
        except ValueError:
            return _error(400, "bad_input", "The query string couldn't be read. Send each parameter once.")
        found = SESSION_PATH.fullmatch(path)
        if found and found.group(2) in ("view", "stream") and method == "GET" and self.sessions is not None:
            # The live view: a page and a stream an <iframe> or <img> loads, so it takes the session's viewer key
            # (made for this session, good until it ends) instead of a bearer token. It only shows the screen.
            return self._viewer(found.group(1), found.group(2), query)
        if headers.get("origin") is not None:
            return _error(403, "browser", "The App API doesn't answer web pages. Call it from a script or a tool.")
        owner = self._client(headers)
        if owner is None:
            return _error(401, "unauthorized", "The bearer token is missing, wrong or revoked. Make one with mobster "
                                               "api tokens create, and send it as Authorization: Bearer.",
                          **{"WWW-Authenticate": "Bearer"})
        posting = method == "POST" and (path == "/v1/sessions" or (found and found.group(2) == "end"))
        if method != "GET" and not posting:
            return _error(405, "reads_only", "Every App API endpoint is a GET, except making and ending a session "
                                             "(POST). Nothing taps, types or posts through it.", Allow="GET")
        if body and not posting:
            return _error(405, "reads_only", "The App API only reads: send no body.")
        if path == "/v1/sessions" or found:
            return self._session_route(method, path, found, query, owner, body)
        if path == "/v1/openapi.json":
            return _json(openapi.document(list(self.endpoints.values()), port=self.port, version=self.version))
        if path == "/v1/apps":
            return self._apps(query)
        endpoint = self.endpoints.get(path)
        if endpoint is None:
            return self._not_found(path)
        within = None
        if "session" in query:
            if self.sessions is None or len(query["session"]) != 1:
                return _error(400, "bad_input", "session is a session's id, sent once.")
            try:
                within = self.sessions.get(query.pop("session")[0], owner)
            except reading.Failure as failure:
                return _error(failure.status, failure.code, str(failure))
            if not within.open:
                return _error(409, "session_ended", "That session has ended. Make a new one.")
            if "phone" in query or "profile" in query:
                return _error(400, "bad_input", "Pass session, or phone or profile, not both.")
            query["phone"] = [within.phone["id"]]
        csv = False
        if "format" in query:
            fmt = query.pop("format")
            if fmt not in (["json"], ["csv"]):
                return _error(400, "bad_input", "format is json or csv, sent once.")
            csv = fmt == ["csv"]
        elif "text/csv" in headers.get("accept", ""):
            csv = True
        try:
            inputs = endpoints_.from_query(endpoint, query)
            check(endpoint.input_schema, inputs)
        except ValueError as error:
            return _error(400, "bad_input", f"{error}. GET /v1/openapi.json says what {endpoint.path} takes.")
        try:
            from ..mcp_server.protocol import Call
            value = reading.answer(endpoint, inputs, self.toolset, call=Call(endpoint.name), profiles=self.profiles,
                                   client_factory=self.client_factory, guard=self._guard(within), clock=self.clock,
                                   sleep=self.sleep, deadline=self.deadline)
        except reading.Failure as failure:
            return _error(failure.status, failure.code, str(failure))
        if within is not None:
            self.sessions.touch(within)
            self.sessions.event(within, "read", endpoint=endpoint.path, rows=len(value["rows"]),
                                took_ms=value["took_ms"])
        return self._csv(endpoint, value) if csv else _json(value)

    def _guard(self, within):
        """A read's phone check: a phone an App API session holds is read only inside that session."""
        if self.sessions is None:
            return None

        def guard(run):
            record = getattr(run, "record", None) or {}
            ident = getattr(run, "device_id", None) or record.get("id")
            holder = self.sessions.held(ident) if ident else None
            if holder is not None and holder is not within:
                raise reading.Failure(409, "in_session", "A session holds this phone. Read inside it with "
                                                         "session=<its id>, or end it first.")
        return guard

    def _session_route(self, method, path, found, query, owner, body):
        from . import sessions as sessions_
        if self.sessions is None:
            return _error(404, "not_found", "This App API has no sessions.")
        base = f"http://127.0.0.1:{self.port}"
        try:
            if path == "/v1/sessions" and method == "POST":
                try:
                    inputs = json.loads(body or b"{}")
                    check(sessions_.CREATE_SCHEMA, inputs, "body")
                except ValueError as error:
                    return _error(400, "bad_input", f"{error}. GET /v1/openapi.json says what a session takes.")
                session = self.sessions.create(inputs, owner)
                return _json(session.public(base), 201)
            if path == "/v1/sessions":
                if set(query) - {"status"}:
                    return _error(400, "bad_input", "/v1/sessions takes status.")
                status = (query.get("status") or [None])[0]
                return _json({"sessions": [s.public(base) for s in self.sessions.for_client(owner)
                                           if status is None or s.status == status]})
            session = self.sessions.get(found.group(1), owner)
            what = found.group(2)
            if what is None:
                return _json(session.public(base))
            if what == "end":
                return _json(self.sessions.end(session).public(base))
            if what == "events":
                after = (query.get("after") or ["0"])[0]
                if not after.isdigit():
                    return _error(400, "bad_input", "after is an event's seq.")
                return Answer(200, b"", "text/event-stream", {"Cache-Control": "no-store"},
                              stream=self.sessions.stream(session, int(after)))
            if what == "logs":
                return _json(self.sessions.logs(session))
            if what == "recording":
                return Answer(200, self.sessions.recording(session), "video/mp4",
                              {"Content-Disposition": f'attachment; filename="mobster-session-{session.id}.mp4"'})
            if not session.open:
                return _error(409, "session_ended", "That session has ended. Make a new one.")
            self.sessions.touch(session)
            if what == "live":
                return _json({"url": f"{base}/v1/sessions/{session.id}/view?key={session.key}",
                              "stream_url": f"{base}/v1/sessions/{session.id}/stream?key={session.key}",
                              "expires_at": session.public(base)["expires_at"], "interactive": False})
            if what == "screen":
                return self._screen(session, query)
        except reading.Failure as failure:
            return _error(failure.status, failure.code, str(failure))
        return _error(404, "not_found", "No such endpoint. GET /v1/openapi.json lists them.")

    def _screen(self, session, query):
        """The session's screen, read by the MCP ``screen`` tool on its phone (the same masking and app permission)."""
        image = (query.get("image") or ["false"])[0]
        if set(query) - {"image"} or image not in ("true", "false"):
            return _error(400, "bad_input", "screen takes image=true or image=false.")
        result = self.toolset.call("screen", {"device": session.phone["id"], "image": image == "true"}, None)
        if getattr(result, "is_error", False):
            return _error(409, "couldnt_read", result.text)
        screen = dict((result.structured or {}).get("screen") or {})
        if image == "true" and result.images:
            screen["image"] = "data:image/jpeg;base64," + result.images[0]
        self.sessions.event(session, "screen", app=screen.get("app"), elements=len(screen.get("elements") or ()))
        return _json({"session": session.id, "screen": screen})

    def _viewer(self, ident, what, query):
        key = (query.get("key") or [""])[0]
        session = self.sessions.by_key(ident, key)
        if session is None:
            return _error(404, "unknown_session", "This live view has ended, or its link is wrong.")
        if what == "view":
            return Answer(200, VIEW_PAGE.replace("{stream}", f"/v1/sessions/{session.id}/stream?key={key}").encode(),
                          "text/html; charset=utf-8",
                          {"Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
                           "Referrer-Policy": "no-referrer"})
        video = self.sessions.video_for(session)
        if video is None:
            return _error(404, "no_video", "This phone has no video stream.")
        return Answer(200, b"", f"multipart/x-mixed-replace; boundary={BOUNDARY}", {"Cache-Control": "no-store"},
                      stream=_mjpeg(video, session))

    def _not_found(self, path):
        found = re.fullmatch(r"/v1/([^/]{1,40})/([^/]{1,40})", path)
        apps = {e.app: e.pack for e in self.endpoints.values() if e.pack}
        if not found:
            return _error(404, "not_found", "No such endpoint. GET /v1/openapi.json lists them.")
        if found.group(1) not in apps:
            return _error(404, "unknown_app", f"Mobster has no endpoints for “{found.group(1)}”. GET /v1/apps lists "
                                              "the apps it reads.")
        pack = apps[found.group(1)]
        return _error(
            404, "unknown_reader",
            f"{pack.name} has no endpoint “{found.group(2)}”. It has: "
            f"{', '.join(e.thing for e in self.endpoints.values() if e.app == pack.key)}. "
            "GET /v1/screen reads any screen.")

    def _apps(self, query):
        from ..packs import health
        phone = (query.get("phone") or [None])[0]
        if set(query) - {"phone"} or len(query.get("phone") or [None]) != 1 or (phone and len(phone) > 128):
            return _error(400, "bad_input", "/v1/apps takes phone, sent once.")
        entries = health.recent(self.health_dir) if self.health_dir else ()
        try:
            installed = self.installed(phone)
        except Exception:
            installed = None
        return _json(openapi.apps(list(self.endpoints.values()), entries, installed, phone))

    @staticmethod
    def _csv(endpoint, value):
        from .. import result_blocks
        block = {"columns": value["columns"],
                 "rows": [{"cells": {c["key"]: row.get(c["key"]) for c in value["columns"]}} for row in value["rows"]]}
        return Answer(200, result_blocks.table_csv(block).encode(), "text/csv; charset=utf-8",
                      {"Content-Disposition": 'attachment; '
                       f'filename="{endpoint.app or "screen"}-{endpoint.thing}.csv"'})


VIEW_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Mobster live view</title>
<style>html,body{margin:0;height:100%;background:#0b0b0c}body{display:flex;align-items:center;justify-content:center}
img{max-width:100%;max-height:100%;object-fit:contain}</style></head>
<body><img src="{stream}" alt="The iPhone's screen, live"></body></html>
"""


def _mjpeg(video, session):
    """The session's screen as an MJPEG multipart stream, until the session ends or the viewer leaves."""
    viewer = video.subscribe()
    try:
        for frame in video.paced_frames(viewer, lambda: VIEW_FPS):
            if not session.open:
                break
            data = frame[2]
            yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(data)}\r\n\r\n").encode() \
                + data + b"\r\n"
    finally:
        video.unsubscribe(viewer)


def check(schema, inputs, where="query"):
    """``inputs`` against ``schema``, with the MCP server's validator: ValueError when it breaks it."""
    from ..mcp_server.protocol import ProtocolError
    from ..mcp_server.schemas import validate
    try:
        validate(schema, inputs, where)
    except ProtocolError as error:
        raise ValueError(str(getattr(error, "message", error)).rstrip(".").replace(f"{where}.", "")) from None


def server(api, port, host="127.0.0.1"):
    """An HTTP server for ``api`` on ``host`` (127.0.0.1 only) and ``port`` (0 picks one); call serve_forever."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    if host not in ("127.0.0.1", "::1"):
        raise ValueError("the App API listens on 127.0.0.1 only")

    class Handler(BaseHTTPRequestHandler):
        server_version = "MobsterAppAPI"
        sys_version = ""

        def _answer(self):
            length = self.headers.get("Content-Length")
            if self.headers.get("Transfer-Encoding"):
                answer = _error(400, "bad_input", "Send the body with a Content-Length.")
            elif length not in (None, "0") and (not length.isdigit() or int(length) > MAX_BODY):
                answer = _error(413, "too_large", f"A request body is at most {MAX_BODY} bytes.")
            else:
                body = self.rfile.read(int(length)) if length not in (None, "0") else None
                answer = api.handle(self.command, self.path, list(self.headers.items()), self.client_address[0],
                                    body)
            self.send_response(answer.status)
            self.send_header("Content-Type", answer.content_type)
            if answer.stream is None:
                self.send_header("Content-Length", str(len(answer.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in answer.headers.items():
                if name != "Cache-Control":
                    self.send_header(name, value)
            self.end_headers()
            if self.command == "HEAD":
                return
            if answer.stream is None:
                self.wfile.write(answer.body)
                return
            try:
                for chunk in answer.stream:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                close = getattr(answer.stream, "close", None)
                if close:
                    close()
            self.close_connection = True

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _answer

        def log_message(self, format, *args):
            pass  # a query can hold a person's name: nothing is logged

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    api.port = httpd.server_address[1]
    return httpd


"""The local API's request handler base: credentials, replies, registered routes and request bodies."""

import base64
import hashlib
import hmac
import json
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, urlsplit

from . import api_routes, eventbus, harness_api, permissions, tracks
from .api_errors import APIError
from .journal import JournalError
from .transport import decode_json

# Read before the 413: 64 MB covers any file dragged in by mistake, and loopback reads it in well under a second.
DRAIN_LIMIT = 64 << 20


def size_words(limit):
    if limit >= 1 << 20:
        return f"{limit / (1 << 20):.3g} MB"
    return f"{max(1, round(limit / 1024))} KB"


APP_ONLY = "Do this in the Mobster app."
# A timed-out request may have acted, so it never says only "try again".
TOOK_TOO_LONG = ("This request took too long, so Mobster stopped waiting. Check that nothing "
                 "started before you try again.")
FILE_TROUBLE = "Mobster couldn't read or write a file on this Mac. Check that the disk has free space, then try again."
# Without the Mac app's session only a decline and a redirect stay open to the API.
APPROVE_IN_APP = "Approve this in the Mobster app on this Mac. From here you can only decline it."
# Only the Mac app answers these runs' approvals: the place they came from must not approve its own drafts.
APPROVE_IN_APP_ORIGINS = frozenset({"remote", "console"})
ALREADY_ANSWERED = {
    "approved": "This was already approved, so Mobster is going ahead.",
    "choice": "This was already answered, so Mobster is going ahead.",
    "answered": "This question was already answered.",
    "denied": "This was already declined, so Mobster won't do it.",
    "redirected": "This was already declined with something else to do instead.",
    "timeout": "Nobody answered in time, so Mobster didn't do it.",
    "stopped": "This task was stopped, so Mobster won't do it.",
}


# send_stream and send_frame without a video serve the primary's; None is "this device has none", never the primary's.
PRIMARY_VIDEO = object()


# A workflow made a tool, by its slug; a 12-character id is a workflow's own routes.
WORKFLOW_TOOL = re.compile(r"/api/workflows/([a-z0-9_]{1,48})")
DEVICE_PATH = re.compile(r"/api/devices/([^/]{1,128})(?:/(stream|frame|video/status|info|status|setup|control|teach|"
                         r"disconnect|connect|take-back|resume-queue)|/setup/(build|start|stop|refresh|photos))?")
# Changing whether a phone asks first or which apps it may use needs the Mac app's session.
DEVICE_FIELDS = frozenset({"name", "default", "askBeforeActing", "apps"})


def api_path(path):
    return "/api/" + path[len("/v1/"):] if path.startswith("/v1/") else path


def etag(data):
    return f'"{hashlib.blake2b(data, digest_size=12).hexdigest()}"'


def etag_matches(header, tag):
    if not header:
        return False
    for item in header.split(","):
        item = item.strip()
        if item == "*" or item.removeprefix("W/") == tag:
            return True
    return False


# Registered routes that need the Mac app's session when the sidecar has one (S6.6).
TAKE_BACK_IN_APP = "Taking a phone back is the Mac app's: open Mobster and press Take it back."
APP_ONLY_ROUTES = frozenset({("POST", "/api/phone/files/put"), ("POST", "/api/wireless/reset"),
                             ("POST", "/api/permissions")})
APP_ONLY_PATTERNS = (("POST", re.compile(r"/api/approvals/[a-f0-9]{12}")),
                     ("POST", re.compile(r"/api/queue/[a-f0-9]{12}/(?:start|approve|import)")),
                     ("POST", re.compile(r"/api/queue/[a-f0-9]{12}/items/[a-f0-9]{12}/(?:mark|client)")),
                     ("POST", re.compile(r"/api/transfers")),
                     ("GET", re.compile(r"/api/teams")), ("POST", re.compile(r"/api/teams/[a-z]{1,16}")))

# Before a plan, the page's token opens Setup's routes only: a page patched past the plan screen still can't run a task.
SETUP_SCOPE_LABEL = b"mobster setup scope v1"
SETUP_SCOPE_ROUTES = frozenset({
    ("GET", "/api/setup"), ("GET", "/api/keys"), ("GET", "/api/ai-plans"),
    ("POST", "/api/keys"), ("POST", "/api/keys/test"),
})
SETUP_SCOPE_PATTERNS = (
    (
        "POST",
        re.compile(
            r"/api/setup/(?:build|start|stop|phone|live|photos|refresh|tools|xcode|key"
            r"|signing/(?:card|approve|resolve|devmode))"),
    ),
    ("POST", re.compile(r"/api/ai-plans/providers/[a-z_]{2,30}/[a-z-]{2,20}")),
)
PLAN_REQUIRED = "Choose a plan or start your free trial in Mobster to run tasks. Setup works without one."


def setup_scope_token(token):
    # The shell derives the same (agent_access.rs): no third secret is passed, and it doesn't reveal the token.
    digest = hmac.new(token.encode(), SETUP_SCOPE_LABEL, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def setup_scope_allows(method, path):
    # Anything not listed is refused.
    return (method, path) in SETUP_SCOPE_ROUTES or any(
        method == m and pattern.fullmatch(path) for m, pattern in SETUP_SCOPE_PATTERNS)


_ANSWERED = object()  # a route body whose error reply already went out


DESKTOP_ORIGINS = frozenset({"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"})


class ApiBase(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    runtime = token = app_session = setup_token = None

    def credential_scope(self):
        if self.token is None:
            return "full"
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header[:7].lower() == "bearer " else ""
        if not supplied and self.command == "GET":
            supplied = (parse_qs(urlsplit(self.path).query).get("token") or [""])[0]
        if hmac.compare_digest(supplied.encode(), self.token.encode()):
            return "full"
        if self.setup_token and hmac.compare_digest(supplied.encode(), self.setup_token.encode()):
            return "setup"
        return None

    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def send_json(self, value, status=200):
        data = json.dumps(value, allow_nan=False).encode()
        # Still no-store with an ETag: the client keeps the last answer itself, never WebKit's cache.
        tag = etag(data) if status == 200 and getattr(self, "command", None) == "GET" else None
        if tag and etag_matches(self.headers.get("If-None-Match"), tag):
            self.send_response(304)
            self.send_header("ETag", tag)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self.send_bytes(data, "application/json", status, (("ETag", tag),) if tag else ())

    def send_api_error(self, exc):
        self.send_json({"error": str(exc), "code": exc.code, **exc.details}, exc.status)

    def send_bytes(self, data, mime, status=200, extra_headers=()):
        # No cache keeps it: deleting a task or the app's data folder wouldn't reach WebKit's disk cache.
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (OSError, TimeoutError):
            self.close_connection = True

    def allowed_origins(self):
        return {f"http://localhost:{self.runtime.config.port}", f"http://127.0.0.1:{self.runtime.config.port}",
                *DESKTOP_ORIGINS}

    def authorized(self):
        # ?token= counts only on a GET: EventSource and <img> can't send headers.
        return self.credential_scope() == "full"

    def has_app_session(self):
        if not self.app_session:
            return False
        supplied = self.headers.get("X-Mobster-App-Session", "")
        return hmac.compare_digest(supplied.encode(), self.app_session.encode())

    def may_read(self, run):
        # A run that stays on this Mac is the Mac app's alone: an API token is told it doesn't exist.
        return not harness_api.stays_on_mac(run.extras) or self.has_app_session()

    def needs_app(self, message=APP_ONLY):
        if not self.app_session or self.has_app_session():
            return False
        self.send_json({"error": message, "code": "app_only"}, 403)
        return True

    def loosening(self, body):
        # Only the person in the app may lower a task's permission mode.
        if not isinstance(body, dict):
            return False
        requested = body.get("permissionMode")
        if requested is None and isinstance(body.get("message"), dict):
            requested = body["message"].get("permissionMode")
        if requested is None or not permissions.loosens(permissions.from_env().mode, requested):
            return False
        return self.needs_app()

    def origin_label(self):
        # A label for the UI only; a claimed origin counts only with its track's proof.
        self.claimed = None  # a connection's earlier request never lends this one its claim
        values = self.headers.get_all("X-Mobster-Origin", []) or []
        if not values:
            return "api"
        if len(values) != 1 or values[0] not in api_routes.HEADER_ORIGINS:
            raise ValueError("X-Mobster-Origin must be app, tui, cli, mcp, remote or console")
        if values[0] in harness_api.CLAIMED_ORIGINS:
            claim = harness_api.origin_claim(values[0])
            fields = claim[0](self.headers) if claim is not None else None
            if not fields:
                raise APIError("That origin needs its own session on this Mac.", 403, "origin_refused")
            self.claimed = dict(fields)
        return values[0]

    def origin_claim(self):
        return dict(getattr(self, "claimed", None) or {})

    def extension(self, method, path):
        found = api_routes.match(method, path)
        if found is None:
            return False
        route, params = found
        self.close_connection = True
        try:
            if api_routes.track_off(route):
                state = tracks.STATUS.get(api_routes.TRACK.get(route.owner), "")
                message = api_routes.NEWER_DATA if state == "error: newer data" else (
                    "This part of Mobster couldn't open its data. Restart Mobster, then try again.")
                return self.send_json({"error": message, "code": "unavailable"}, 503) or True
            if ((method, path) in APP_ONLY_ROUTES or any(
                    method == m and pattern.fullmatch(path) for m, pattern in APP_ONLY_PATTERNS)) and self.needs_app():
                return True
            origin = self.origin_label()
            if route.idempotency and len(self.headers.get_all("Idempotency-Key", [])) > 1:
                raise ValueError("Supply only one Idempotency-Key")
            body = self.route_body(route) if method == "POST" else None
            if body is _ANSWERED or method == "POST" and self.loosening(body):
                return True
            query = parse_qs(urlsplit(self.path).query)
            request = api_routes.Request(runtime=self.runtime, method=method, path=path, params=params, query=query,
                                         headers=self.headers, body=body,
                                         content_type=self.headers.get_content_type()
                                         if self.headers.get("Content-Type") else None,
                                         origin=origin, app_session=self.has_app_session())
            response = route.handler(request)
            if not isinstance(response, api_routes.Response):
                raise TypeError("A route handler returns a Response")
        except (ValueError, TypeError) as exc:
            return self.send_json({"error": str(exc)}, 400) or True
        except APIError as exc:
            return self.send_api_error(exc) or True
        except JournalError:
            return self.send_json({"error": "Mobster couldn't save this to its history on this Mac. Check that the "
                                   "disk has free space, then try again.",
                                   "code": "persistence_unavailable"}, 503) or True
        except TimeoutError:
            return self.send_json({"error": TOOK_TOO_LONG, "code": "request_timeout"}, 408) or True
        except OSError:
            return self.send_json({"error": FILE_TROUBLE,
                                   "code": "io_error"}, 500) or True
        self.send_response_object(response)
        return True

    def route_body(self, route):
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
            raise ValueError("Supply one Content-Length and no Transfer-Encoding")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid body length") from None
        if route.body == "none":
            if size:
                raise ValueError("This request takes no body")
            return None
        if route.body == "json" and self.headers.get_content_type() != "application/json":
            self.send_json({"error": "Use application/json"}, 415)
            return _ANSWERED
        if size > route.max_bytes:
            # Read the refused body first: closing on unread bytes resets the connection before the client reads this.
            self.drain(size)
            limit = size_words(route.max_bytes)
            self.send_json({"error": f"Attach files of {limit} or less." if route.body == "raw" else
                            f"This request is larger than {limit}.", "code": "too_large",
                            "limit": route.max_bytes}, 413)
            return _ANSWERED
        if size <= 0:
            raise ValueError("Invalid body length")
        if route.body == "raw":
            self.connection.settimeout(30)
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError("Incomplete request body")
        if route.body == "raw":
            return raw
        body = decode_json(raw)
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return body

    def drain(self, size):
        if size > DRAIN_LIMIT:
            self.close_connection = True
            return
        try:
            self.connection.settimeout(30)
            left = size
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                left -= len(chunk)
        except (OSError, TimeoutError):
            left = 1
        if left > 0:
            self.close_connection = True

    def send_response_object(self, response):
        if response.is_stream():
            return self.send_sse(response.events)
        if response.data is not None:
            filename = getattr(response, "filename", None)
            disposition = ()
            if filename:
                plain = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "-" if ch in "–—" else "_"
                                for ch in filename)
                disposition = (("Content-Disposition",
                                f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(filename)}"),)
            self.send_bytes(response.data, response.content_type or "application/octet-stream",
                            response.status, disposition)
            return None
        return self.send_json(response.value, response.status)

    def send_sse(self, events):
        if not self.runtime.streams.acquire(blocking=False):
            return self.send_json({"error": "Too many event streams", "code": "stream_limit"}, 429)
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for event in events:
                if event is None:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    ident = event.get("seq", event.get("id"))
                    self.wfile.write((f"id: {ident}\n" if ident is not None else "").encode()
                                     + f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
                if self.runtime.closing:
                    break
        except (OSError, TimeoutError):
            pass
        finally:
            close = getattr(events, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            self.runtime.streams.release()

    def send_events(self):
        query = parse_qs(urlsplit(self.path).query)
        try:
            topics = eventbus.parse_topics((query.get("topics") or [""])[0])
        except ValueError as exc:
            return self.send_json({"error": str(exc), "code": "invalid_topics"}, 400)
        last = self.headers.get("Last-Event-ID") or (query.get("lastEventId") or [None])[0]
        bus = getattr(self.runtime, "bus", None) or eventbus.bus
        keepalive = getattr(self.runtime, "event_keepalive_s", 5.0)
        return self.send_sse(bus.stream(topics, last, keepalive_s=keepalive, closed=lambda: self.runtime.closing))

    def mcp_run(self):
        # The coding agent reaches its phone tools with that run's own token, never the API token.
        from .ai_plans import mcp_http
        path = urlsplit(self.path).path
        if not path.startswith(mcp_http.PATH_PREFIX):
            return False
        self.close_connection = True
        if not self.local_request():
            self.send_json({"error": "Local origin required"}, 403)
            return True

        def read_body(limit):
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise ValueError("one Content-Length and no Transfer-Encoding")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= limit:
                raise ValueError("body length")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("incomplete body")
            return raw
        status, headers, body = mcp_http.respond(self.command, path, self.headers, read_body)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body or b"")))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (OSError, TimeoutError):
                pass
        return True

    def refused(self):
        if not self.local_request():
            self.send_json({"error": "Local origin required"}, 403)
            return True
        scope = self.credential_scope()
        if scope == "full":
            return False
        if scope == "setup":
            if setup_scope_allows(self.command, api_path(urlsplit(self.path).path)):
                return False
            self.send_json({"error": PLAN_REQUIRED, "code": "plan_required"}, 403)
            return True
        self.send_json({"error": "Missing or wrong API token", "code": "unauthorized"}, 401)
        return True

    def local_request(self):
        if len(self.headers.get_all("Host", [])) != 1:
            return False
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return bool(re.fullmatch(r"(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?", host)) and (
            not origin or origin in self.allowed_origins())

    def end_headers(self):
        # Cross-origin reads only for allowlisted local origins: the desktop webview is cross-origin to this API.
        origin = self.headers.get("Origin") if hasattr(self, "headers") and self.headers else None
        if origin and origin in self.allowed_origins():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Expose-Headers", "ETag")
            self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self):
        self.close_connection = True
        if not self.local_request() or not self.headers.get("Origin"):
            return self.send_json({"error": "Local origin required"}, 403)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key, "
                                                         "X-Mobster-Origin, X-Mobster-App-Session, Last-Event-ID, "
                                                         "If-None-Match")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

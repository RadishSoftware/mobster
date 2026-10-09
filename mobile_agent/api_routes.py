"""HTTP routes the SOTA tracks register (seam S3). Frozen after the seams merge.

server.py's handler tries ``match`` right after the token check, before its own routes. A route's pattern is a
regex, full-matched on the /api path (/v1 becomes /api), whose named groups are its params; it must start with one
of its owner's PREFIXES. Write ``{id}`` for a 12-hex-character id and ``{device}`` for a device id: both expand to
named groups (``id``, ``device``). So a track can never shadow a core route.

A handler gets a ``Request`` and returns a ``Response``. Errors map as the core maps them: ValueError/TypeError
-> 400, api_errors.APIError -> its status, journal.JournalError -> 503, TimeoutError -> 408, OSError -> 500.
Every SSE response counts against ``runtime.streams`` (8). ``Request.app_session`` is true only when the Mac app's
per-launch secret came with the request (S6.6); ``Request.origin`` (X-Mobster-Origin) is a label for the UI only
and grants nothing.
"""

from dataclasses import dataclass, field
import json
import re
from typing import Any, Callable, Iterable, Mapping, Optional

ID = r"(?P<id>[a-f0-9]{12})"
DEVICE = r"(?P<device>[^/]{1,128})"
HEADER_ORIGINS = ("app", "tui", "cli", "mcp")

PREFIXES = {   # a Route's pattern must start with one of its owner's prefixes
    "harness": ("/api/runs/{id}/messages", "/api/runs/{id}/pause", "/api/runs/{id}/pickup", "/api/prewarm",
                "/api/harness"),
    "threads": ("/api/threads",),
    "memory": ("/api/memory", "/api/routines"),
    "files": ("/api/attachments", "/api/phone/files"),
    "wireless": ("/api/wireless", "/api/devices/{device}/wifi"),
    "testkit": ("/api/tests",),
}
# The track (tracks.TRACKS) behind each route owner: its routes answer 503 while it is off.
TRACK = {"harness": "harness", "threads": "threads", "memory": "memory", "files": "attachments",
         "wireless": "wireless", "testkit": "testkit"}
BODIES = ("json", "raw", "none")
JSON_MAX = 64_000
NEWER_DATA = "This was saved by a newer Mobster. Update Mobster to use it."


def expand(pattern):
    return pattern.replace("{id}", ID).replace("{device}", DEVICE)


@dataclass(frozen=True)
class Route:
    owner: str                    # a key of PREFIXES
    method: str                   # "GET" | "POST" | "DELETE"
    pattern: str                  # regex, fullmatch on the /api path; {id} and {device} expand to named groups
    handler: Callable[["Request"], "Response"]
    body: str = "json"            # "json" | "raw" | "none"
    max_bytes: int = JSON_MAX     # json <= 64 KB; raw: the route's own cap (attachments: 26_214_400)
    stream: bool = False          # SSE: Response.sse(...)
    idempotency: bool = False     # accepts exactly one Idempotency-Key header


@dataclass
class Request:
    runtime: Any
    method: str
    path: str
    params: dict
    query: dict                   # urllib.parse.parse_qs result
    headers: Mapping[str, str]
    body: Any                     # dict (json) | bytes (raw) | None
    content_type: Optional[str]
    origin: str                   # X-Mobster-Origin (app|tui|cli|mcp), default "api"; a UI label only
    app_session: bool             # X-Mobster-App-Session matched in constant time (S6.6)


class Response:
    def __init__(self, status=200, *, value=None, data=None, content_type=None, events=None, keepalive_s=5.0):
        self.status, self.value, self.data, self.content_type = status, value, data, content_type
        self.events, self.keepalive_s = events, keepalive_s

    @staticmethod
    def json(value, status: int = 200) -> "Response":
        return Response(status, value=value)

    @staticmethod
    def bytes(data: bytes, content_type: str, status: int = 200) -> "Response":
        """Sent with Cache-Control: no-store and X-Content-Type-Options: nosniff."""
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("Response.bytes takes bytes")
        return Response(status, data=bytes(data), content_type=content_type)

    @staticmethod
    def sse(events: Iterable[Optional[dict]], *, keepalive_s: float = 5.0) -> "Response":
        """A text/event-stream. Each dict is one event (``id:`` from its "seq", else its "id"); a None is a
        keepalive comment. The iterable yields something at least every ``keepalive_s`` seconds."""
        return Response(200, events=events, keepalive_s=keepalive_s)

    @staticmethod
    def error(message: str, status: int, code: str, **details) -> "Response":
        return Response(status, value={"error": message, "code": code, **details})

    def is_stream(self):
        return self.events is not None


_routes: list = []


def _prefix_regex(prefix):
    return expand(prefix)


def register(route: Route) -> None:
    """Add ``route``. ValueError on an unknown owner, a pattern outside the owner's prefixes, an unknown body kind,
    or a duplicate (method, pattern)."""
    if route.owner not in PREFIXES:
        raise ValueError(f"Unknown route owner {route.owner!r}")
    if route.method not in ("GET", "POST", "DELETE"):
        raise ValueError("A route's method is GET, POST or DELETE")
    if route.body not in BODIES:
        raise ValueError("A route's body is json, raw or none")
    if route.body == "json" and not 0 < route.max_bytes <= JSON_MAX:
        raise ValueError("A JSON body is at most 64 KB")
    if route.body == "raw" and not 0 < route.max_bytes <= 64 * 1024 * 1024:
        raise ValueError("A raw body states its cap, at most 64 MB")
    if not callable(route.handler):
        raise ValueError("A route needs a handler")
    pattern = expand(route.pattern)
    if not any(_inside(pattern, _prefix_regex(prefix)) for prefix in PREFIXES[route.owner]):
        raise ValueError(f"{route.pattern} is outside {route.owner}'s prefixes")
    try:
        re.compile(pattern)
    except re.error as error:
        raise ValueError(f"Invalid route pattern: {error}") from None
    if any(r.method == route.method and expand(r.pattern) == pattern for r in _routes):
        raise ValueError(f"{route.method} {route.pattern} is already registered")
    _routes.append(route)


def _inside(pattern, prefix):
    """The pattern starts with the prefix and the next character ends a path segment."""
    if not pattern.startswith(prefix):
        return False
    rest = pattern[len(prefix):]
    return rest == "" or rest[0] in "/(?$"


def _owned(path, owner):
    """The path itself (not only the pattern) is under one of the owner's prefixes."""
    return any(re.fullmatch(_prefix_regex(prefix) + r"(?:/.*)?", path) for prefix in PREFIXES[owner])


def match(method: str, path: str) -> Optional[tuple]:
    """(route, params) for ``method`` and ``path``, or None. A path that matches a route for another method gives
    None too (the core then answers 404)."""
    for route in _routes:
        if route.method != method:
            continue
        found = re.fullmatch(expand(route.pattern), path)
        if found and _owned(path, route.owner):
            return route, {k: v for k, v in found.groupdict().items() if v is not None}
    return None


def track_off(route) -> bool:
    """Whether the route's track is off (a newer schema, a failed migration): it answers 503."""
    from . import harness_api
    return harness_api.disabled(TRACK.get(route.owner))


def encode(value):
    return json.dumps(value, allow_nan=False).encode()


def reset_for_tests() -> None:
    _routes.clear()

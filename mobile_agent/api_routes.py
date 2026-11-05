"""HTTP routes the tracks register, each under its owner's PREFIXES so it can never shadow a core route."""

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

ID = r"(?P<id>[a-f0-9]{12})"
DEVICE = r"(?P<device>[^/]{1,128})"
HEADER_ORIGINS = ("app", "tui", "cli", "mcp", "remote", "console")

PREFIXES = {
    "harness": ("/api/runs/{id}/messages", "/api/runs/{id}/pause", "/api/runs/{id}/pickup", "/api/prewarm",
                "/api/harness"),
    "integrations": ("/api/integrations",),
    "threads": ("/api/threads",),
    "memory": ("/api/memory",),
    "files": ("/api/attachments", "/api/phone/files"),
    "wireless": ("/api/wireless", "/api/devices/{device}/wifi"),
    "testkit": ("/api/tests",),
    "remote": ("/api/remote",),
    "accounts": ("/api/accounts",),
    "learned": ("/api/replay",),
    "longform": ("/api/longform", "/api/runs/{id}/longform"),
    "outlets": ("/api/outlets", "/api/runs/{id}/send"),
    "connectors": ("/api/connectors", "/api/runs/{id}/issue"),
    "plugins": ("/api/plugins",),
    "permissions": ("/api/permissions", "/api/approvals"),
    "packs": ("/api/packs",),
    "waiting": ("/api/waiting",),
    "netxray": ("/api/xray/network",),
    # In no track of its own, so it never answers 503.
    "access": ("/api/access",),
    "triggers": ("/api/triggers",),
    "queue": ("/api/queue", "/api/transfers"),
    "feedback": ("/api/feedback",),
    "teams": ("/api/teams",),
    "xray": ("/api/xray/app",),
    "projectenv": ("/api/project/env",),
    "projects": ("/api/projects",),
}
# A route answers 503 while its owner's track is off.
TRACK = {"harness": "harness", "integrations": "harness",
         "threads": "threads", "memory": "memory", "files": "attachments",
         "wireless": "wireless", "testkit": "testkit", "remote": "remote", "accounts": "accounts",
         "learned": "learned", "longform": "longform", "outlets": "outlets",
         "connectors": "connectors", "plugins": "plugins",
         "permissions": "permissions", "packs": "packs", "waiting": "waiting", "netxray": "netxray",
         "triggers": "triggers", "queue": "content_queue", "feedback": "feedback", "xray": "xray", "teams": "teams",
         "projects": "projects"}
BODIES = ("json", "raw", "none")
JSON_MAX = 64_000
NEWER_DATA = "This was saved by a newer Mobster. Update Mobster to use it."


def expand(pattern):
    return pattern.replace("{id}", ID).replace("{device}", DEVICE)


@dataclass(frozen=True)
class Route:
    owner: str
    method: str
    pattern: str
    handler: Callable[["Request"], "Response"]
    body: str = "json"
    max_bytes: int = JSON_MAX
    stream: bool = False
    idempotency: bool = False


@dataclass
class Request:
    runtime: Any
    method: str
    path: str
    params: dict
    query: dict
    headers: Mapping[str, str]
    body: Any
    content_type: Optional[str]
    origin: str  # a UI label only: it grants nothing
    app_session: bool


class Response:
    def __init__(self, status=200, *, value=None, data=None, content_type=None, events=None, keepalive_s=5.0,
                 filename=None):
        self.status, self.value, self.data, self.content_type = status, value, data, content_type
        self.events, self.keepalive_s = events, keepalive_s
        self.filename = filename

    @staticmethod
    def json(value, status: int = 200) -> "Response":
        return Response(status, value=value)

    @staticmethod
    def bytes(data: bytes, content_type: str, status: int = 200, filename: Optional[str] = None) -> "Response":
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("Response.bytes takes bytes")
        return Response(status, data=bytes(data), content_type=content_type, filename=filename)

    @staticmethod
    def sse(events: Iterable[Optional[dict]], *, keepalive_s: float = 5.0) -> "Response":
        return Response(200, events=events, keepalive_s=keepalive_s)

    @staticmethod
    def error(message: str, status: int, code: str, **details) -> "Response":
        return Response(status, value={"error": message, "code": code, **details})

    def is_stream(self):
        return self.events is not None


_routes: list = []




def register(route: Route) -> None:
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
    if not any(_inside(pattern, expand(prefix)) for prefix in PREFIXES[route.owner]):
        raise ValueError(f"{route.pattern} is outside {route.owner}'s prefixes")
    try:
        re.compile(pattern)
    except re.error as error:
        raise ValueError(f"Invalid route pattern: {error}") from None
    if any(r.method == route.method and expand(r.pattern) == pattern for r in _routes):
        raise ValueError(f"{route.method} {route.pattern} is already registered")
    _routes.append(route)


def _inside(pattern, prefix):
    if not pattern.startswith(prefix):
        return False
    rest = pattern[len(prefix):]
    return rest == "" or rest[0] in "/(?$"


def _owned(path, owner):
    return any(re.fullmatch(expand(prefix) + r"(?:/.*)?", path) for prefix in PREFIXES[owner])


def match(method: str, path: str) -> Optional[tuple]:
    for route in _routes:
        if route.method != method:
            continue
        found = re.fullmatch(expand(route.pattern), path)
        if found and _owned(path, route.owner):
            return route, {k: v for k, v in found.groupdict().items() if v is not None}
    return None


def track_off(route) -> bool:
    from . import harness_api
    return harness_api.disabled(TRACK.get(route.owner))




def reset_for_tests() -> None:
    _routes.clear()

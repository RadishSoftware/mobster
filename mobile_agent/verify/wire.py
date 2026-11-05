"""Wire assertions in ``expect:``: what the app sent over the network and what it stored on disk.

``request`` and ``no_request`` are judged on the requests the run's proxy saw (netproxy.py); ``storage`` on the app's
container after the steps (wirestore.py). They run on a simulator, dark behind MOBSTER_EXPERIMENTAL_NETWORK=1. The
evidence in a result is the matched request line or the storage diff, with secrets masked and bodies cut short."""

import fnmatch
import gzip
import json
import os
import re
import zlib
from dataclasses import dataclass
from typing import Any, Optional

from .. import secret_filter
from . import netstubs, wirestore
from .assertions import AssertionResult, CheckError, Matcher, parse_matcher

FLAG = "MOBSTER_EXPERIMENTAL_NETWORK"
KINDS = ("request", "no_request", "storage")
REQUEST_KEYS = ("method", "host", "path", "status", "body", "response")
STORAGE_KEYS = ("file", "sqlite", "query", "changed", "contains", "equals", "at_least", "at_most")
STATUS = re.compile(r"[1-5]xx")
SHORTHAND = re.compile(r"(?:(?P<method>[A-Za-z]+)\s+)?(?P<host>[^/\s]+)?(?P<path>/\S*)?$")
SECRET_HEADERS = re.compile(r"authorization|cookie|token|secret|api[-_]?key|session|password|signature", re.I)
SHOWN_BODY = 200
SHOWN_SEEN = 4
OFF = (f"expect: request, no_request and storage are experimental. Set {FLAG}=1 to use them, or remove them from "
       "the check.")


def enabled(environ=None):
    return (os.environ if environ is None else environ).get(FLAG) == "1"


def is_wire(item):
    return isinstance(item, dict) and any(kind in item for kind in KINDS)


@dataclass(frozen=True)
class WireAssertion:
    kind: str
    method: Optional[str] = None
    host: Optional[str] = None
    path: Optional[Matcher] = None
    status: Any = None                  # a number, or "2xx"
    body: Optional[Matcher] = None      # of the request's body
    response: Optional[Matcher] = None  # of the response's body
    file: Optional[str] = None
    sqlite: Optional[str] = None
    query: Optional[str] = None
    changed: bool = True
    contains: Optional[Matcher] = None
    equals: Optional[int] = None
    at_least: Optional[int] = None
    at_most: Optional[int] = None
    name: Optional[str] = None

    def needs_open(self):
        """Whether the proxy must open the host's https to judge this: a path, status or body is inside it."""
        return bool(self.host) and self.kind != "storage" and bool(self.path or self.status or self.body
                                                                    or self.response)

    def render(self):
        if self.kind == "storage":
            target = f"file {self.file}" if self.file else f"sqlite {self.sqlite}"
            if self.file:
                said = "changed" if self.changed else "unchanged"
                return f"storage {target} {said}" + (f' containing "{self.contains.render()}"' if self.contains else "")
            bound = (f"== {self.equals}" if self.equals is not None else
                     f">= {self.at_least}" if self.at_least is not None else f"<= {self.at_most}")
            return f"storage {target} rows {bound}"
        line = " ".join(part for part in (self.method, (self.host or "") + (self.path.render() if self.path else ""))
                        if part) or "any request"
        extra = "".join((f" -> {self.status}" if self.status else "",
                         f" body {self.body.render()}" if self.body else "",
                         f" response {self.response.render()}" if self.response else ""))
        return f"{self.kind} {line}{extra}"

    def to_dict(self):
        out = {}
        if self.kind == "storage":
            for key in STORAGE_KEYS:
                value = getattr(self, key)
                if key == "changed":
                    if self.file and not value:
                        out[key] = False
                elif value is not None:
                    out[key] = value.raw if isinstance(value, Matcher) else value
        else:
            for key in REQUEST_KEYS:
                value = getattr(self, key)
                if value is not None:
                    out[key] = value.raw if isinstance(value, Matcher) else value
        result = {self.kind: out}
        if self.name:
            result["name"] = self.name
        return result


def _check_keys(body, allowed, where):
    for key in body:
        if key not in allowed:
            raise CheckError(f"{where}.{key} isn't a setting. Use {', '.join(allowed)}.")


def _shorthand(text, where):
    found = SHORTHAND.match(text.strip())
    if not text.strip() or found is None or not (found["host"] or found["path"]):
        raise CheckError(f"{where} needs a method, host and path such as POST api.example.com/v1/cart, or a "
                         "mapping.")
    out = {}
    method, host = found["method"], found["host"]
    if method:
        out["method"] = method
    if host:
        out["host"] = host
    if found["path"]:
        out["path"] = found["path"]
    return out


def _parse_one(obj, where):
    kinds = [kind for kind in KINDS if kind in obj]
    if len(kinds) != 1:
        raise CheckError(f"{where} needs exactly one of {', '.join(KINDS)}; it has {', '.join(kinds)}")
    kind = kinds[0]
    _check_keys(obj, KINDS + ("name",), where)
    body = obj[kind]
    if isinstance(body, str) and kind != "storage":
        body = _shorthand(body, f"{where}.{kind}")
    if not isinstance(body, dict):
        raise CheckError(f"{where}.{kind} must be a mapping such as "
                         + ("{file: Library/Preferences/app.plist}." if kind == "storage"
                            else "{method: POST, host: api.example.com, path: /v1/cart}."))
    name = obj.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 120):
        raise CheckError(f"{where}.name must be text of 1 to 120 characters")
    fields = {"kind": kind, "name": name.strip() if name else None}
    here = f"{where}.{kind}"
    if kind == "storage":
        fields.update(_parse_storage(body, here))
    else:
        fields.update(_parse_request(body, here))
        if kind == "no_request" and (fields.get("status") or fields.get("response")):
            raise CheckError(f"{here} takes no status or response: a request that never left has none.")
    return WireAssertion(**fields)


def _parse_request(body, where):
    _check_keys(body, REQUEST_KEYS, where)
    out = {}
    if "method" in body:
        method = body["method"]
        if not isinstance(method, str) or method.upper() not in netstubs.METHODS:
            raise CheckError(f"{where}.method must be one of {', '.join(netstubs.METHODS)}.")
        out["method"] = method.upper()
    if "host" in body:
        out["host"] = netstubs._host(body["host"], f"{where}.host")
    if "path" in body:
        path = body["path"]
        if isinstance(path, str) and not path.startswith(("/", "*")):
            raise CheckError(f"{where}.path must start with /, such as /v1/cart or /v1/*, or be a /regex/.")
        out["path"] = parse_matcher(path, f"{where}.path")
    if "status" in body:
        status = body["status"]
        if isinstance(status, str) and STATUS.fullmatch(status):
            out["status"] = status
        elif isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            out["status"] = status
        else:
            raise CheckError(f"{where}.status must be a number from 100 to 599 or a class such as 2xx.")
    for key in ("body", "response"):
        if key in body:
            out[key] = parse_matcher(body[key], f"{where}.{key}")
    if not out:
        raise CheckError(f"{where} needs at least a method, host or path.")
    return out


def _parse_storage(body, where):
    _check_keys(body, STORAGE_KEYS, where)
    if ("file" in body) == ("sqlite" in body):
        raise CheckError(f"{where} needs one of file (a path or glob under the app's container) or sqlite.")
    out = {}
    for key in ("file", "sqlite"):
        if key in body:
            value = body[key]
            if not isinstance(value, str) or not value.strip() or value.startswith("/") or ".." in value.split("/"):
                raise CheckError(f"{where}.{key} must be a path inside the app's container, such as "
                                 "Library/Preferences/app.plist, without a leading / or ..")
            out[key] = value.strip()
    if "file" in body:
        if set(body) - {"file", "changed", "contains"}:
            raise CheckError(f"{where}: a file takes changed and contains only.")
        if not isinstance(body.get("changed", True), bool):
            raise CheckError(f"{where}.changed must be true or false.")
        out["changed"] = body.get("changed", True)
        if "contains" in body:
            out["contains"] = parse_matcher(body["contains"], f"{where}.contains")
    else:
        query = body.get("query")
        if not isinstance(query, str) or not wirestore.read_only_query(query):
            raise CheckError(f"{where}.query must be one SELECT or WITH statement, such as "
                             "SELECT 1 FROM cart WHERE sku = 'a1'.")
        bounds = {key: body[key] for key in ("equals", "at_least", "at_most") if key in body}
        if set(body) - {"sqlite", "query", *bounds} or len(bounds) != 1:
            raise CheckError(f"{where}: a sqlite query needs exactly one of equals, at_least and at_most (rows).")
        (key, number), = bounds.items()
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise CheckError(f"{where}.{key} must be a whole number of at least 0.")
        out.update(query=query.strip(), **{key: number})
    return out


def parse(expect):
    """The wire assertions of a check file's ``expect`` list: () when it has none. Raises CheckError."""
    found = [(index, item) for index, item in enumerate(expect or ()) if is_wire(item)]
    if found and not enabled():
        raise CheckError(OFF)
    return tuple(_parse_one(item, f"expect[{index}]") for index, item in found)


def watch_hosts(assertions):
    """The hosts whose https the proxy must open, or None when no assertion reads the proxy."""
    wire = [item for item in assertions if item.kind != "storage"]
    return tuple(dict.fromkeys(item.host for item in wire if item.needs_open())) if wire else None


# evidence

def _mask_header(name, value):
    return "••••••" if SECRET_HEADERS.search(name) else secret_filter.redact(value)


def _text(raw, limit=SHOWN_BODY):
    text = secret_filter.redact(raw.decode("utf-8", "replace").strip())
    return text if len(text) <= limit else text[:limit] + f"… ({len(raw)} bytes)"


def _response_body(raw):
    """A captured response's body as bytes: de-chunked and inflated where it says so, else what was kept."""
    head, _, body = raw.partition(b"\r\n\r\n")
    lowered = head.lower()
    if b"transfer-encoding: chunked" in lowered:
        out, rest = b"", body
        while rest:
            size_line, _, rest = rest.partition(b"\r\n")
            try:
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                break
            if not size:
                break
            out, rest = out + rest[:size], rest[size + 2:]
        body = out
    try:
        if b"content-encoding: gzip" in lowered:
            body = gzip.decompress(body)
        elif b"content-encoding: deflate" in lowered:
            body = zlib.decompress(body)
    except (OSError, EOFError, zlib.error):
        pass
    return body


def request_line(item):
    """``POST api.example.com/v1/cart -> 200 at 4.2 s``: one request, safe to show."""
    where = secret_filter.redact(f"{item['host']}{item.get('path', '')}")
    status = f" -> {item['status']}" if item.get("status") else ""
    return f"{item['method']} {where}{status} at {item['atMs'] / 1000:.1f} s"


def evidence(item):
    """The matched request for wire.json: its line, headers with secrets masked, and bodies cut short."""
    return {"line": request_line(item),
            "headers": [[name, _mask_header(name, value)] for name, value in item.get("headers", [])],
            "body": _text(item["body"]) if item.get("body") else "",
            "response": _text(_response_body(item["response"])) if item.get("response") else ""}


# judging

def _path_matches(matcher, path):
    if matcher.is_regex:
        return bool(matcher.pattern.search(path))
    return fnmatch.fnmatchcase(path.split("?")[0], matcher.text) or fnmatch.fnmatchcase(path, matcher.text)


def _status_matches(want, got):
    if got is None:
        return False
    return got // 100 == int(want[0]) if isinstance(want, str) else got == want


def request_matches(assertion, item):
    if assertion.method and item["method"] != assertion.method:
        return False
    if assertion.host and not netstubs.host_matches(assertion.host, item["host"]):
        return False
    if assertion.path and not _path_matches(assertion.path, item.get("path", "")):
        return False
    if assertion.status and not _status_matches(assertion.status, item.get("status")):
        return False
    if assertion.body and not assertion.body.contains(item["body"].decode("utf-8", "replace")):
        return False
    return not assertion.response or assertion.response.contains(
        _response_body(item["response"]).decode("utf-8", "replace"))


def _opaque(rows):
    """Connections the proxy relayed or refused without opening: they name a host and nothing more."""
    return [{"method": row["method"], "host": row["host"], "path": "", "atMs": row["atMs"], "headers": [],
             "body": b"", "status": None, "response": b"", "opaque": True}
            for row in rows if row.get("via") in ("tunnel", "refused")]


def _judge_request(assertion, captures, rows):
    text = assertion.render()
    hidden = _opaque(rows)
    if assertion.kind == "request":
        hits = [item for item in captures if request_matches(assertion, item)]
        if hits:
            first = hits[0]
            return AssertionResult(True, text, request_line(first) + (f" and {len(hits) - 1} more"
                                                                      if len(hits) > 1 else ""), (evidence(first),))
        seen = ", ".join(request_line(item) for item in captures[:SHOWN_SEEN])
        tunnels = sorted({item["host"] for item in hidden})
        more = (f"; https to {', '.join(tunnels[:SHOWN_SEEN])} wasn't opened, so name the host to see its paths"
                if tunnels and not assertion.host else "")
        if not captures and not hidden:
            return AssertionResult(False, text, "no request from the app reached the proxy" + more)
        return AssertionResult(False, text, f"no request matched; saw {seen or 'none opened'}{more}")
    hits = [item for item in (*captures, *hidden) if request_matches(assertion, item)]
    if hits:
        return AssertionResult(False, text, "left the app: " + request_line(hits[0]), (evidence(hits[0]),))
    if not captures and not hidden:
        return AssertionResult(False, text, "no request from the app reached the proxy, so Mobster can't tell "
                                            "whether this one left")
    undecided = [item for item in hidden if assertion.path or assertion.body]
    if undecided:
        hosts = ", ".join(sorted({item["host"] for item in undecided})[:SHOWN_SEEN])
        return AssertionResult(False, text, f"https to {hosts} wasn't opened, so its paths are unknown; name the "
                                            "host in this assertion")
    return AssertionResult(True, text, f"none of {len(captures) + len(hidden)} requests matched")


class Judge:
    """One run's wire assertions: ``baseline`` before the flow, ``judge`` after it. Never raises."""

    def __init__(self, assertions, *, run, udid, bundle, container=wirestore.container):
        self.assertions = tuple(assertions)
        self._find = lambda: container(run, udid, bundle)
        self._before = {}
        self.evidence = []

    def baseline(self):
        try:
            root = self._find()
        except wirestore.StoreError:
            return
        for index, item in enumerate(self.assertions):
            if item.kind == "storage":
                self._before[index] = wirestore.take(root, item)

    def judge(self, net):
        captures, rows = net.seen() if net is not None else ([], [])
        out = []
        root, problem = None, None
        if any(item.kind == "storage" for item in self.assertions):
            try:
                root = self._find()
            except wirestore.StoreError as error:
                problem = str(error)
        for index, item in enumerate(self.assertions):
            if item.kind != "storage":
                out.append(_judge_request(item, captures, rows) if net is not None else AssertionResult(
                    False, item.render(), "the run had no proxy, so no request was seen"))
            elif root is None:
                out.append(AssertionResult(False, item.render(), problem))
            else:
                out.append(wirestore.judge(item, root, self._before.get(index)))
        self.evidence = [{"assertion": item.render(), "ok": result.ok, "observed": result.observed,
                          "matches": [dict(m) for m in result.matches]}
                         for item, result in zip(self.assertions, out)]
        return out

    def write(self, directory):
        if self.evidence:
            with open(os.path.join(directory, "wire.json"), "w", encoding="utf-8") as handle:
                json.dump(self.evidence, handle, indent=1, default=str)

"""A check's ``stubs:``: fixed answers to named requests, served by the run's proxy (netproxy.py)."""

import difflib
import json
import mimetypes
from pathlib import Path

from .assertions import CheckError
from .conditions import _file

KEYS = ("match", "respond")
MATCH_KEYS = ("host", "path_prefix", "method")
RESPOND_KEYS = ("status", "json", "file", "delay_ms")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
STUB_LIMIT = 50
DELAY_LIMIT = 30000   # ms
BODY_LIMIT = 8 * 1024 * 1024


def _keys(value, allowed, where):
    for key in value:
        if key not in allowed:
            close = difflib.get_close_matches(str(key), allowed, n=1)
            raise CheckError(f"{where}.{key} isn't a setting; " + (f"did you mean {close[0]}? " if close else "")
                             + f"Use {', '.join(allowed)}.")


def _host(value, where):
    text = value.strip().lower() if isinstance(value, str) else ""
    bare = text[2:] if text.startswith("*.") else text
    if not bare or any(c in bare for c in "/:*? ") or bare.startswith(".") or bare.endswith("."):
        raise CheckError(f"{where} must be a host name such as api.example.com or *.example.com, without a scheme, "
                         "port or path.")
    return text


def parse(value, base_dir=None):
    """The normalized ``stubs`` list of a check file: () when absent. Raises CheckError."""
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise CheckError("stubs must list requests to answer, such as [{match: {host: api.example.com}, "
                         "respond: {status: 200, json: {items: []}}}].")
    if len(value) > STUB_LIMIT:
        raise CheckError(f"stubs has more than {STUB_LIMIT} entries. Keep the ones the check needs.")
    out = []
    for index, item in enumerate(value):
        where = f"stubs[{index}]"
        if not isinstance(item, dict) or set(item) != set(KEYS):
            raise CheckError(f"{where} needs match and respond, such as {{match: {{host: api.example.com}}, "
                             "respond: {status: 200, json: {}}}.")
        match, respond = item["match"], item["respond"]
        if not isinstance(match, dict) or not isinstance(respond, dict):
            raise CheckError(f"{where}.match and {where}.respond must each be a mapping.")
        _keys(match, MATCH_KEYS, f"{where}.match")
        _keys(respond, RESPOND_KEYS, f"{where}.respond")
        if "host" not in match:
            raise CheckError(f"{where}.match needs a host.")
        prefix = match.get("path_prefix", "/")
        if not isinstance(prefix, str) or not prefix.startswith("/"):
            raise CheckError(f"{where}.match.path_prefix must start with /, such as /v1/items.")
        method = match.get("method")
        if method is not None and (not isinstance(method, str) or method.upper() not in METHODS):
            raise CheckError(f"{where}.match.method must be one of {', '.join(METHODS)}.")
        status = respond.get("status", 200)
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise CheckError(f"{where}.respond.status must be a whole number from 100 to 599.")
        delay = respond.get("delay_ms", 0)
        if isinstance(delay, bool) or not isinstance(delay, int) or not 0 <= delay <= DELAY_LIMIT:
            raise CheckError(f"{where}.respond.delay_ms must be a whole number of milliseconds from 0 to "
                             f"{DELAY_LIMIT}.")
        if "json" in respond and "file" in respond:
            raise CheckError(f"{where}.respond has both json and file. Keep one.")
        stub = {"host": _host(match["host"], f"{where}.match.host"), "path_prefix": prefix,
                "method": method.upper() if method else None, "status": status, "delay_ms": delay,
                "json": None, "file": None}
        if "json" in respond:
            try:
                json.dumps(respond["json"])
            except (TypeError, ValueError):
                raise CheckError(f"{where}.respond.json must be plain JSON: text, numbers, lists and "
                                 "objects.") from None
            stub["json"] = respond["json"]
        elif "file" in respond:
            stub["file"] = _file(respond["file"], f"{where}.respond.file", base_dir)
            if Path(stub["file"]).stat().st_size > BODY_LIMIT:
                raise CheckError(f"{where}.respond.file is larger than {BODY_LIMIT // 1024 // 1024} MiB. Trim it.")
        out.append(stub)
    return tuple(out)


def to_dict(stubs, project_root=None):
    """The file shape of ``stubs``, with paths relative to ``project_root`` where they sit inside it."""
    def relative(path):
        try:
            return str(Path(path).relative_to(Path(project_root).resolve()))
        except (ValueError, TypeError):
            return path
    out = []
    for stub in stubs:
        match = {"host": stub["host"], "path_prefix": stub["path_prefix"]}
        if stub["method"]:
            match["method"] = stub["method"]
        respond = {"status": stub["status"]}
        if stub["json"] is not None:
            respond["json"] = json.loads(json.dumps(stub["json"]))
        if stub["file"]:
            respond["file"] = relative(stub["file"])
        if stub["delay_ms"]:
            respond["delay_ms"] = stub["delay_ms"]
        out.append({"match": match, "respond": respond})
    return out


def host_matches(pattern, host):
    host = host.lower()
    return host.endswith(pattern[1:]) if pattern.startswith("*.") else host == pattern


def matches(stub, host, method, path):
    return (host_matches(stub["host"], host) and (not stub["method"] or stub["method"] == method.upper())
            and path.startswith(stub["path_prefix"]))


def answer(stub):
    """(status, content type, body bytes) of a stub's response."""
    if stub["file"]:
        kind = mimetypes.guess_type(stub["file"])[0] or "application/octet-stream"
        return stub["status"], kind, Path(stub["file"]).read_bytes()
    if stub["json"] is not None:
        return stub["status"], "application/json", json.dumps(stub["json"]).encode()
    return stub["status"], "text/plain", b""

"""Mobster's MCP server for one run, over MCP's Streamable HTTP transport (spec 2025-03-26 on)."""

import hmac
import json
import secrets
import threading
import uuid

from ..mcp_server.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    negotiate,
)

PATH_PREFIX = "/mcp/runs/"
MAX_BODY = 1024 * 1024
SERVER_NAME = "mobster"


class Endpoint:
    """One run's MCP server: its token, tools and instructions."""

    def __init__(self, run_id, tools, instructions="", version="", log=None):
        self.run_id = run_id
        self.tools = tools
        self.instructions = instructions
        self.version = version
        self.token = secrets.token_urlsafe(32)
        self.log = log or (lambda message: None)

    def authorized(self, header):
        supplied = header[7:] if isinstance(header, str) and header[:7].lower() == "bearer " else ""
        return bool(supplied) and hmac.compare_digest(supplied.encode(), self.token.encode())

    def handle(self, message):
        """The response to one JSON-RPC message, or None for a notification or a response."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "The message is not a JSON-RPC 2.0 request.")
        method = message.get("method")
        if not isinstance(method, str):
            return None  # a response to a request this server never sends
        if "id" not in message:
            return None  # notifications/initialized, notifications/cancelled: nothing to do
        request_id = message["id"]
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object.")
        try:
            if method == "initialize":
                return _result(request_id, self._initialize(params))
            if method == "ping":
                return _result(request_id, {})
            if method == "tools/list":
                return _result(request_id, {"tools": self.tools.definitions()})
            if method == "tools/call":
                return _result(request_id, self._call(params))
            if method in ("resources/list", "prompts/list"):
                return _result(request_id, {method.split("/")[0]: []})
            return _error(request_id, METHOD_NOT_FOUND, f"Mobster doesn't serve {method[:80]}.")
        except Exception as error:  # noqa: BLE001
            self.log(f"MCP {method} failed: {type(error).__name__}: {error}")
            return _error(request_id, INTERNAL_ERROR, "Mobster hit an internal error.")

    def _initialize(self, params):
        return {"protocolVersion": negotiate(params.get("protocolVersion")),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": self.version or "0"},
                "instructions": self.instructions}

    def _call(self, params):
        from ..mcp_server.protocol import ProtocolError
        from ..mcp_server.schemas import validate
        name, arguments = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return {"content": [{"type": "text", "text": "tools/call needs a tool name and an arguments object."}],
                    "isError": True}
        schema = next((d["inputSchema"] for d in self.tools.definitions() if d["name"] == name), None)
        if schema is not None:
            try:
                validate(schema, {k: v for k, v in arguments.items() if v is not None})
            except ProtocolError as error:
                return {"content": [{"type": "text", "text": str(error)}], "isError": True}
        # Text and images only: some clients show structuredContent instead of the text when both come.
        return self.tools.call(name, {k: v for k, v in arguments.items() if v is not None}).payload(False)


def _result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


_endpoints = {}
_lock = threading.Lock()


def register(run_id, tools, instructions="", version="", log=None):
    """Serve ``tools`` for ``run_id`` until ``unregister``. Returns the Endpoint (its ``token`` goes to the CLI)."""
    endpoint = Endpoint(run_id, tools, instructions, version, log)
    with _lock:
        _endpoints[run_id] = endpoint
    return endpoint


def unregister(run_id):
    with _lock:
        _endpoints.pop(run_id, None)


def endpoint_for(run_id):
    with _lock:
        return _endpoints.get(run_id)


def run_id_of(path):
    """The run id in /mcp/runs/<id>, else None."""
    if not path.startswith(PATH_PREFIX):
        return None
    rest = path[len(PATH_PREFIX):].rstrip("/")
    return rest if rest and "/" not in rest and len(rest) <= 64 else None


def respond(method, path, headers, read_body):
    """(status, extra headers, body bytes or None) for an HTTP request to an MCP path."""
    run_id = run_id_of(path)
    endpoint = endpoint_for(run_id) if run_id else None
    if endpoint is None:
        return 404, {}, _json({"error": "No task is using this MCP address now."})
    if not endpoint.authorized(headers.get("Authorization", "")):
        return 401, {"WWW-Authenticate": "Bearer"}, _json({"error": "Missing or wrong token for this task."})
    if method == "DELETE":
        return 200, {}, None
    if method != "POST":
        return 405, {"Allow": "POST, DELETE"}, None
    try:
        raw = read_body(MAX_BODY)
        message = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 400, {}, _json(_error(None, PARSE_ERROR, "The message is not valid JSON."))
    extra = {}
    if isinstance(message, list):
        answers = [a for a in (endpoint.handle(m) for m in message) if a is not None]
        if not answers:
            return 202, {}, None
        return 200, extra, _json(answers)
    if isinstance(message, dict) and message.get("method") == "initialize":
        extra["Mcp-Session-Id"] = uuid.uuid4().hex
    answer = endpoint.handle(message)
    if answer is None:
        return 202, extra, None
    return 200, extra, _json(answer)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8", "replace")

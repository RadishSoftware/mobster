"""A stdlib-only MCP client: stdio (a command Mobster starts) and streamable HTTP (a URL), as mcp_server/protocol.py is
the server side."""

import http.client
import json
import os
import queue
import socket
import ssl
import subprocess
import threading
import time
from urllib.parse import urlsplit

from .. import __version__

PROTOCOL_VERSION = "2025-06-18"
LIST_TIMEOUT = 20.0
CALL_TIMEOUT = 60.0
PING_TIMEOUT = 5.0
CONNECT_TIMEOUT = 30.0          # starting a command (npx may download its package the first time) and initialize
MAX_TOOLS = 500                 # tools/list stops after this many
MAX_PAGES = 10
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
STDERR_KEEP = 2000              # the last characters a stdio server wrote to stderr, for "it didn't start" messages


class ConnectorError(Exception):
    """A failed MCP request. ``message`` is safe to show and journal: never a token, an env value or a URL's query."""

    sent = None   # False: nothing happened; True: the server answered; None: unknown

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class NotSent(ConnectorError):
    """The request never reached the server (the command didn't start, the connection was refused): nothing happened."""

    sent = False


class AuthNeeded(NotSent):
    """The server answered 401 or 403 before it did anything: nothing happened; sign in again."""


class SlowToAnswer(NotSent):
    """A server that was started and has not answered the handshake yet: a first `npx` can take a minute to download."""


class ServerError(ConnectorError):
    sent = True

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class OutcomeUnknown(ConnectorError):
    """The request went out and no answer came in time: it may have happened, and the caller must not send it again."""

    sent = None


def _jsonrpc(method, params, ident=None):
    message = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if ident is not None:
        message["id"] = ident
    return message


def _error_of(message, name):
    error = message.get("error")
    if not isinstance(error, dict):
        return None
    text = " ".join(str(error.get("message") or "an error").split())[:300]
    return ServerError(f"{name} answered with an error: {text}", error.get("code"))


class Session:
    """The MCP conversation over one transport."""

    name = "The server"

    def __init__(self):
        self._ids = 0
        self._lock = threading.Lock()
        self.server_info = {}
        self.instructions = ""
        self.ready = False
        # The server said its tools changed (notifications/tools/list_changed): the service lists them again before
        # the next call.
        self.tools_changed = False

    def _next_id(self):
        with self._lock:
            self._ids += 1
            return self._ids

    def initialize(self, timeout=CONNECT_TIMEOUT):
        deadline = time.monotonic() + timeout
        answer = self._ask(_jsonrpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "Mobster", "version": __version__}}, self._next_id()), deadline, phase="connect")
        result = answer.get("result")
        if not isinstance(result, dict):
            raise NotSent(f"{self.name} didn't answer like an MCP server.")
        self.server_info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
        self.instructions = str(result.get("instructions") or "")[:2000]
        self.protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self._send(_jsonrpc("notifications/initialized", None), deadline)
        self.ready = True
        return result

    def list_tools(self, timeout=LIST_TIMEOUT):
        """Every tool the server lists, as {"name", "description", "inputSchema", "annotations"} dicts."""
        deadline = time.monotonic() + timeout
        tools, cursor = [], None
        for _ in range(MAX_PAGES):
            answer = self._ask(_jsonrpc("tools/list", {"cursor": cursor} if cursor else {}, self._next_id()),
                               deadline, phase="list")
            result = answer.get("result") or {}
            for tool in result.get("tools") or ():
                if isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool["name"]:
                    tools.append(tool)
            cursor = result.get("nextCursor")
            if not cursor or len(tools) >= MAX_TOOLS:
                break
        return tools[:MAX_TOOLS]

    def ping(self, timeout=PING_TIMEOUT):
        """MCP ``ping``: the cheapest proof the session still answers."""
        if not self.ready:
            raise NotSent(f"{self.name} isn't connected.")
        deadline = time.monotonic() + timeout
        try:
            self._ask(_jsonrpc("ping", None, self._next_id()), deadline, phase="list")
        except ServerError:
            pass  # a server without ping still answered
        return True

    def _notified(self, message):
        if message.get("method") == "notifications/tools/list_changed":
            self.tools_changed = True

    def call_tool(self, name, arguments, timeout=CALL_TIMEOUT):
        """The tool's result ({"content", "structuredContent"?, "isError"?})."""
        if not self.ready:
            raise NotSent(f"{self.name} isn't connected.")
        deadline = time.monotonic() + timeout
        ident = self._next_id()
        try:
            answer = self._ask(_jsonrpc("tools/call", {"name": name, "arguments": arguments}, ident), deadline,
                               phase="call")
        except OutcomeUnknown:
            self._cancel(ident)
            raise
        result = answer.get("result")
        if not isinstance(result, dict):
            raise ServerError(f"{self.name} answered without a result.")
        return result

    def _cancel(self, ident):
        try:
            self._send(_jsonrpc("notifications/cancelled", {"requestId": ident, "reason": "timed out"}),
                       time.monotonic() + 2)
        except ConnectorError:
            pass

    def _send(self, message, deadline):
        raise NotImplementedError

    def _ask(self, message, deadline, phase):
        raise NotImplementedError

    def close(self):
        self.ready = False


class StdioSession(Session):
    """A command Mobster starts and talks to over its stdin and stdout, newline-delimited JSON-RPC."""

    def __init__(self, argv, env=None, cwd=None, name="The server"):
        super().__init__()
        self.argv, self.env, self.cwd, self.name = list(argv), env, cwd, name
        self.process = None
        self._answers = {}
        self._waiters = {}
        self._stderr = ""
        self._closed = threading.Event()
        self._write_lock = threading.Lock()

    def start(self):
        try:
            self.process = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
                cwd=self.cwd, start_new_session=True)
        except FileNotFoundError:
            raise NotSent(f"Mobster couldn't find {os.path.basename(self.argv[0])}. Install it, or check the "
                          "command.") from None
        except (OSError, ValueError) as error:
            raise NotSent(f"Mobster couldn't start {os.path.basename(self.argv[0])} "
                          f"({type(error).__name__}).") from None
        threading.Thread(target=self._read, name="mobster-connector-out", daemon=True).start()
        threading.Thread(target=self._drain, name="mobster-connector-err", daemon=True).start()
        return self

    def _read(self):
        stream = self.process.stdout
        try:
            for line in stream:
                if len(line) > MAX_MESSAGE_BYTES:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # a server's stray output line is skipped, as protocol.py skips a client's
                if not isinstance(message, dict):
                    continue
                ident = message.get("id")
                if "method" in message:
                    if ident is None:
                        self._notified(message)
                    else:
                        # A request from the server (sampling, roots, elicitation): Mobster offers none of them.
                        self._write(_jsonrpc_error(ident, -32601, "Mobster doesn't offer this"))
                    continue
                with self._lock:
                    waiter = self._waiters.pop(ident, None)
                if waiter is not None:
                    waiter.put(message)
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()
            with self._lock:
                waiters, self._waiters = list(self._waiters.values()), {}
            for waiter in waiters:
                waiter.put(None)

    def _drain(self):
        try:
            for line in self.process.stderr:
                self._stderr = (self._stderr + line.decode(errors="replace"))[-STDERR_KEEP:]
        except (OSError, ValueError):
            pass

    def stderr_tail(self):
        from ..secret_filter import redact
        return redact(" ".join(self._stderr.split())[-300:])

    def _write(self, message):
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        with self._write_lock:
            self.process.stdin.write(data)
            self.process.stdin.flush()

    def _send(self, message, deadline):
        if self.process is None or self._closed.is_set():
            raise NotSent(f"{self.name} isn't running.")
        try:
            self._write(message)
        except (OSError, ValueError):
            raise NotSent(f"{self.name} stopped.") from None

    def _ask(self, message, deadline, phase):
        if self.process is None or self._closed.is_set():
            raise NotSent(self._stopped_message())
        waiter = queue.Queue(maxsize=1)
        with self._lock:
            self._waiters[message["id"]] = waiter
        try:
            self._write(message)
        except (OSError, ValueError):
            with self._lock:
                self._waiters.pop(message["id"], None)
            raise NotSent(self._stopped_message()) from None
        try:
            answer = waiter.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            with self._lock:
                self._waiters.pop(message["id"], None)
            if phase == "call":
                raise OutcomeUnknown(f"{self.name} didn't answer in time.") from None
            raise SlowToAnswer(f"{self.name} didn't answer in time.") from None
        if answer is None:
            if phase == "call":
                raise OutcomeUnknown(f"{self.name} stopped before it answered.")
            raise NotSent(self._stopped_message())
        error = _error_of(answer, self.name)
        if error is not None:
            if phase == "connect":
                raise NotSent(error.message)
            raise error
        return answer

    def _stopped_message(self):
        tail = self.stderr_tail()
        return f"{self.name} stopped." + (f" It said: {tail}" if tail else "")

    def close(self):
        super().close()
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, 15)
                process.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, 9)
                    process.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass


def _jsonrpc_error(ident, code, message):
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def validate_url(url):
    """``url`` when it is an http(s) URL without credentials; plain http only on this Mac."""
    if not isinstance(url, str):
        raise ValueError("A server's URL is text")
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("A server's URL starts with https://")
    if parts.username or parts.password:
        raise ValueError("Put the sign-in in the token field, not in the URL")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("A server on the internet needs an https:// URL")
    if len(url) > 2000 or any(c in url for c in "\r\n\t "):
        raise ValueError("That URL isn't valid")
    return url


def unreachable(name, url, error):
    """Why a connection to ``url`` didn't open, in words that say what to fix (P05-3: only "couldn't reach")."""
    parts = urlsplit(url)
    where = f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname
    if isinstance(error, socket.gaierror):
        return f"Mobster couldn't find {parts.hostname}. Check the address."
    if isinstance(error, ConnectionRefusedError):
        return f"Nothing answered at {where}. Check the address, and that the server is running."
    if isinstance(error, ssl.SSLCertVerificationError):
        return f"{parts.hostname}'s certificate isn't one this Mac trusts, so Mobster didn't connect."
    if isinstance(error, TimeoutError):
        return f"{where} didn't answer. Check the address, and that the server is running."
    return f"Mobster couldn't reach {name}."


def hint(url):
    """The scheme, host and path of ``url`` (no query): what the API and the UI show."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path or ''}"[:200]


class HttpSession(Session):
    """Streamable HTTP: each message is a POST to the URL; an answer is JSON or an SSE stream."""

    def __init__(self, url, token=None, name="The server", headers=None):
        super().__init__()
        self.url, self.token, self.name = validate_url(url), token, name
        self.extra_headers = dict(headers or {})
        self.session_id = None
        self.protocol = None
        self.resource_metadata = None   # from a 401's WWW-Authenticate: where its OAuth metadata is

    def start(self):
        return self

    def _headers(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "User-Agent": f"Mobster/{__version__}", **self.extra_headers}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        return headers

    def _connection(self, deadline):
        parts = urlsplit(self.url)
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        left = deadline - time.monotonic()
        if left <= 0:
            raise NotSent(f"{self.name} didn't answer in time.")
        return cls(parts.hostname, parts.port, timeout=left), (parts.path or "/") + (
            f"?{parts.query}" if parts.query else "")

    def _post(self, message, deadline, phase, expect_answer):
        connection, path = self._connection(deadline)
        body = json.dumps(message, separators=(",", ":")).encode()
        finished = threading.Event()

        def expire():
            if finished.is_set():
                return
            sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

        timer = threading.Timer(max(0.0, deadline - time.monotonic()), expire)
        timer.daemon = True
        timer.start()
        sent = False
        try:
            try:
                connection.connect()
            except (OSError, http.client.HTTPException) as error:
                raise NotSent(unreachable(self.name, self.url, error)) from None
            try:
                connection.request("POST", path, body, self._headers())
                sent = True
                response = connection.getresponse()
            except (OSError, http.client.HTTPException):
                if sent and phase == "call":
                    raise OutcomeUnknown(f"{self.name} didn't answer in time.") from None
                raise NotSent(f"{self.name} didn't answer.") from None
            status = response.status
            if status in (401, 403):
                challenge = response.getheader("WWW-Authenticate") or ""
                self.resource_metadata = _resource_metadata(challenge)
                response.read(64_000)
                raise AuthNeeded(f"{self.name} needs you to sign in again." if self.token else
                                 f"{self.name} needs you to sign in.")
            if status == 404 and self.session_id and phase != "connect":
                response.read(64_000)
                self.ready = False
                raise NotSent(f"{self.name} ended the session. Try again.")
            session = response.getheader("Mcp-Session-Id")
            if session and phase == "connect":
                self.session_id = session.strip()[:200]
            if not expect_answer:
                response.read(64_000)
                if status >= 400:
                    raise NotSent(f"{self.name} answered HTTP {status}.")
                return None
            if status >= 400:
                response.read(64_000)
                # 4xx: the server refused it before doing anything; 5xx after it was sent may be either.
                if status >= 500 and phase == "call":
                    raise OutcomeUnknown(f"{self.name} answered HTTP {status}.")
                raise NotSent(f"{self.name} answered HTTP {status}.")
            kind = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
            try:
                if kind == "text/event-stream":
                    answer = _from_events(response, message.get("id"), self._notified)
                else:
                    raw = response.read(MAX_MESSAGE_BYTES + 1)
                    if len(raw) > MAX_MESSAGE_BYTES:
                        raise ValueError("too large")
                    answer = json.loads(raw)
            except (OSError, http.client.HTTPException, ValueError):
                if phase == "call":
                    raise OutcomeUnknown(f"{self.name}'s answer was cut off.") from None
                raise NotSent(f"{self.name}'s answer was cut off.") from None
            if isinstance(answer, list):
                answer = next((m for m in answer if isinstance(m, dict) and m.get("id") == message.get("id")), None)
            if not isinstance(answer, dict):
                if phase == "call":
                    raise OutcomeUnknown(f"{self.name} didn't answer like an MCP server.")
                raise NotSent(f"{self.name} didn't answer like an MCP server.")
            return answer
        finally:
            finished.set()
            timer.cancel()
            connection.close()

    def _send(self, message, deadline):
        self._post(message, deadline, "notify", expect_answer=False)

    def _ask(self, message, deadline, phase):
        answer = self._post(message, deadline, phase, expect_answer=True)
        error = _error_of(answer, self.name)
        if error is not None:
            if phase == "connect":
                raise NotSent(error.message)
            raise error
        return answer

    def close(self):
        if self.session_id and self.ready:
            try:
                connection, path = self._connection(time.monotonic() + 3)
                connection.request("DELETE", path, headers=self._headers())
                connection.getresponse().read(1000)
                connection.close()
            except (OSError, http.client.HTTPException, ConnectorError):
                pass
        super().close()


def _resource_metadata(challenge):
    """The ``resource_metadata`` URL of a WWW-Authenticate: Bearer challenge, or None."""
    for part in challenge.split(","):
        key, _, value = part.strip().partition("=")
        if key.strip().lower().endswith("resource_metadata"):
            value = value.strip().strip('"')
            if value.startswith("https://") or value.startswith("http://127.0.0.1") or \
                    value.startswith("http://localhost"):
                return value[:2000]
    return None


def _from_events(response, ident, notified=None):
    """The JSON-RPC answer with ``ident`` from an SSE stream."""
    data, size = [], 0
    while True:
        line = response.readline(MAX_MESSAGE_BYTES)
        if not line:
            raise ValueError("stream ended")
        size += len(line)
        if size > MAX_MESSAGE_BYTES:
            raise ValueError("too large")
        text = line.decode(errors="replace").rstrip("\r\n")
        if text.startswith("data:"):
            data.append(text[5:].lstrip(" "))
            continue
        if text == "" and data:
            payload, data = "\n".join(data), []
            try:
                message = json.loads(payload)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == ident and "method" not in message:
                return message
            if isinstance(message, dict) and "method" in message and message.get("id") is None and notified:
                notified(message)


# A server whose tools Mobster answers itself with the vendor's web API, because the vendor's MCP server refuses
# Mobster's app: Slack's takes only Marketplace and internal apps (slack_api.py).
SLACK = "https://mcp.slack.com/mcp"
# Microsoft 365 has no MCP server a Mobster app can sign in to: its tools are Microsoft Graph's (graph_api.py).
GRAPH = "https://graph.microsoft.com/v1.0"


def connect(spec, *, env=None, token=None, timeout=CONNECT_TIMEOUT, name="The server"):
    """A started, initialized session for ``spec`` ({"transport": "stdio", "command", "args"} or {"transport": "http",
    "url"})."""
    if spec.get("transport") != "stdio" and spec.get("url") == SLACK:
        from . import slack_api  # here, not at the top: slack_api imports this module
        return slack_api.connect(token=token, timeout=timeout, name=name)
    if spec.get("transport") != "stdio" and spec.get("url") == GRAPH:
        from . import graph_api  # here, not at the top: graph_api imports this module
        return graph_api.connect(token=token, timeout=timeout, name=name)
    if spec.get("transport") == "stdio":
        session = StdioSession([spec["command"], *spec.get("args", ())], env=env, name=name)
    else:
        session = HttpSession(spec["url"], token=token, name=name)
    session.start()
    try:
        session.initialize(timeout=timeout)
    except BaseException:
        session.close()
        raise
    return session


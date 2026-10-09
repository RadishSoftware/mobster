"""MCP over stdio: newline-delimited JSON-RPC 2.0, with the standard library only.

The Python ``mcp`` SDK is not used: it pulls in pydantic, starlette, anyio, httpx and opentelemetry,
which would bloat the frozen binary for a few hundred lines of protocol.

- ``claim_stdout()`` keeps the protocol stream to itself: it duplicates fd 1 for the protocol and points
  fd 1 at stderr, so a stray ``print`` or a subprocess's output never corrupts the stream.
- Every request runs on its own worker thread, so a ``wait`` that blocks never holds up ``ping`` or
  ``stop``. Writes are serialized by one lock.
- A request that carries ``params._meta.progressToken`` gets ``notifications/progress`` every 5 s while
  it runs, which keeps a client's idle timer satisfied.
- ``notifications/cancelled`` marks the request cancelled: its waiting stops and no response is sent.
- A line that isn't JSON (or nests past what the parser takes) gets a -32700 parse error, and a message
  that can't be handled is logged and skipped: one bad line never ends the session.
- On stdin EOF the tools shut down (every run stopped, every simulator lease released) and ``serve``
  returns 0.
"""

import json
import os
import sys
import threading
import time
import traceback

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28")
DEFAULT_PROTOCOL_VERSION = "2025-11-25"
# structuredContent exists from this version on; older clients get the text and image blocks only.
STRUCTURED_SINCE = "2025-06-18"
PROGRESS_SECONDS = 5.0
# A line longer than this is refused as a parse error: no tool takes anywhere near this much input.
MAX_LINE_BYTES = 16 * 1024 * 1024

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ProtocolError(Exception):
    """A JSON-RPC error: an unknown method or tool, or arguments that break the schema."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ToolResult:
    """What a tool returns. ``text`` becomes the one text block; ``images`` are base64 JPEGs;
    ``structured`` is sent as structuredContent when the protocol version has it."""

    def __init__(self, text, structured=None, images=(), is_error=False):
        self.text, self.structured, self.images, self.is_error = text, structured, tuple(images), bool(is_error)

    def payload(self, structured_ok):
        content = [{"type": "text", "text": self.text}]
        content += [{"type": "image", "data": image, "mimeType": "image/jpeg"} for image in self.images]
        result = {"content": content, "isError": self.is_error}
        if structured_ok and self.structured is not None:
            result["structuredContent"] = self.structured
        return result


class Call:
    """One in-flight request, as a tool sees it. ``stop`` ends its waiting (the client cancelled it, or the
    server is closing); ``cancelled`` also means no response is sent. ``note(message)`` sets the text of
    its next progress notification. ``started`` is when the request arrived (time.monotonic()): a call's
    time limits count from it."""

    def __init__(self, request_id, progress_token=None):
        self.id = request_id
        self.started = time.monotonic()
        self.progress_token = progress_token
        self.cancelled = threading.Event()
        self.stop = threading.Event()
        self.done = threading.Event()
        self.message = ""
        # The run a tool call started (verify_start, verify), for the error when it runs past its budget.
        self.run_id = None

    def cancel(self):
        self.cancelled.set()
        self.stop.set()

    def note(self, message):
        if isinstance(message, str) and message:
            self.message = message[:300]


def claim_stdout():
    """The protocol's own binary stream. From here on fd 1 is stderr: only the protocol writer uses the
    returned stream, and anything else written to stdout (a print, a child process) lands on stderr."""
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    proto = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    return proto


def negotiate(requested):
    """The protocol version to answer ``initialize`` with: the client's when it is one Mobster speaks."""
    return requested if requested in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION


def _request_id_ok(value):
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


class Server:
    """Serves ``tools`` (an object with ``list_tools()``, ``call(name, arguments, call)``,
    ``instructions()`` and ``shutdown(reason)``) over one pair of binary streams."""

    def __init__(self, tools, *, version, log=None, progress_seconds=PROGRESS_SECONDS, name="mobster"):
        self.tools = tools
        self.server_info = {"name": name, "version": version}
        self.log = log or (lambda message: None)
        self.progress_seconds = progress_seconds
        self.protocol_version = DEFAULT_PROTOCOL_VERSION
        self.client = {}
        self._write_lock = threading.Lock()
        self._calls_lock = threading.Lock()
        self._calls = {}
        self._writer = None
        self._closed = threading.Event()

    # -- Streams -------------------------------------------------------------------------------

    def serve(self, reader, writer):
        """Read requests from ``reader`` until EOF, then shut the tools down. Returns 0."""
        self._writer = writer
        try:
            while True:
                line = reader.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    self._skip_rest(reader, line)
                    self._error(None, PARSE_ERROR, "The message is larger than 16 MB.")
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    self._receive(line)
                except Exception:
                    # One bad message never ends the session: the calls in flight and the open run go on.
                    self.log("A message couldn't be handled: " + traceback.format_exc(limit=5))
        finally:
            self.close("the MCP client disconnected")
        return 0

    @staticmethod
    def _skip_rest(reader, line):
        while line and not line.endswith(b"\n"):
            line = reader.readline(MAX_LINE_BYTES)

    def close(self, reason, grace=5.0):
        """Stop waiting in every request and give them ``grace`` seconds to answer, then stop every run and
        release every lease."""
        if self._closed.is_set():
            return
        self._closed.set()
        with self._calls_lock:
            calls = list(self._calls.values())
        for call in calls:
            call.stop.set()
        end = time.monotonic() + grace
        for call in calls:
            call.done.wait(max(0.0, end - time.monotonic()))
        try:
            self.tools.shutdown(reason)
        except Exception:
            self.log("Shutdown failed: " + traceback.format_exc(limit=5))

    @staticmethod
    def _encode(message):
        # "replace": a lone surrogate (a client's method or tool name, echoed in an error) becomes "?" instead of
        # an exception that would leave the request unanswered.
        return json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8",
                                                                                                    "replace")

    def send(self, message):
        """Write one message. One that can't be encoded (a value JSON has no form for) is never lost silently: a
        response becomes an internal error for its id, and anything else is logged and dropped."""
        try:
            data = self._encode(message)
        except (TypeError, ValueError, RecursionError) as error:
            self.log(f"A message couldn't be encoded ({type(error).__name__}: {str(error)[:200]}).")
            if not isinstance(message, dict) or not ("result" in message or "error" in message):
                return
            request_id = message.get("id")
            data = self._encode({"jsonrpc": "2.0", "id": request_id if _request_id_ok(request_id) else None,
                                 "error": {"code": INTERNAL_ERROR,
                                           "message": "Mobster couldn't encode its answer. See its log on stderr."}})
        with self._write_lock:
            if self._writer is None:
                return
            try:
                self._writer.write(data + b"\n")
                flush = getattr(self._writer, "flush", None)
                if flush is not None:
                    flush()
            except (BrokenPipeError, OSError, ValueError):
                self._writer = None  # the client is gone; EOF on stdin follows

    # -- Dispatch ------------------------------------------------------------------------------

    def _receive(self, line):
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            # RecursionError: arrays or objects nested deeper than the parser goes.
            self._error(None, PARSE_ERROR, "The message is not valid JSON.")
            return
        if isinstance(message, list):
            # A batch is answered element by element: each response carries its own id.
            if not message:
                self._error(None, INVALID_REQUEST, "The batch is empty.")
            for item in message:
                self._dispatch(item)
        else:
            self._dispatch(message)

    def _dispatch(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            request_id = message.get("id") if isinstance(message, dict) else None
            self._error(request_id if _request_id_ok(request_id) else None, INVALID_REQUEST,
                        "The message is not a JSON-RPC 2.0 request.")
            return
        method = message.get("method")
        if not isinstance(method, str):
            return  # a response to a request this server never sends, or noise
        params = message.get("params")
        if params is None:
            params = {}
        if "id" not in message:
            self._notification(method, params)
            return
        request_id = message["id"]
        if not _request_id_ok(request_id):
            self._error(None, INVALID_REQUEST, "The request id must be a string or an integer.")
            return
        if not isinstance(params, dict):
            self._error(request_id, INVALID_PARAMS, "params must be an object.")
            return
        if method == "initialize":
            # Answered on the reader thread, so the version is set before the next message is read.
            self._respond(request_id, lambda: self._initialize(params))
            return
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        token = meta.get("progressToken")
        call = Call(request_id, token if _request_id_ok(token) else None)
        with self._calls_lock:
            self._calls[request_id] = call
        threading.Thread(target=self._run_request, args=(call, method, params), daemon=True,
                         name=f"mobster-mcp-{request_id}").start()

    def _notification(self, method, params):
        if method == "notifications/cancelled" and isinstance(params, dict):
            request_id = params.get("requestId")
            if not _request_id_ok(request_id):
                return  # a list or an object can't name a request (and isn't a key the calls table takes)
            with self._calls_lock:
                call = self._calls.get(request_id)
            if call is not None:
                call.cancel()
                self.log(f"Request {call.id} was cancelled by the client.")
        # notifications/initialized and anything else: nothing to do.

    def _run_request(self, call, method, params):
        ticker = None
        if call.progress_token is not None:
            ticker = threading.Thread(target=self._progress, args=(call,), daemon=True,
                                      name=f"mobster-mcp-progress-{call.id}")
            ticker.start()
        try:
            self._respond(call.id, lambda: self._handle(method, params, call), call)
        except Exception:
            # Never a request left unanswered, nor a traceback from a dying thread (at exit, one raced the
            # interpreter for stderr and aborted the process).
            self.log("Internal error: " + traceback.format_exc(limit=8))
            if not call.cancelled.is_set():
                try:
                    self._error(call.id, INTERNAL_ERROR, "Mobster hit an internal error. See its log on stderr.")
                except Exception:
                    pass
        finally:
            call.done.set()
            with self._calls_lock:
                self._calls.pop(call.id, None)

    def _progress(self, call):
        count = 0
        while not call.done.wait(self.progress_seconds):
            if call.stop.is_set():
                return
            count += 1
            self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                       "params": {"progressToken": call.progress_token, "progress": count,
                                  "message": call.message or "Working"}})

    def _respond(self, request_id, work, call=None):
        try:
            result = work()
        except ProtocolError as error:
            if call is None or not call.cancelled.is_set():
                self._error(request_id, error.code, str(error))
            return
        except Exception:
            self.log("Internal error: " + traceback.format_exc(limit=8))
            if call is None or not call.cancelled.is_set():
                self._error(request_id, INTERNAL_ERROR, "Mobster hit an internal error. See its log on stderr.")
            return
        if call is not None and call.cancelled.is_set():
            return  # the client cancelled it: no response
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id, code, message):
        self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    # -- Methods -------------------------------------------------------------------------------

    def _initialize(self, params):
        requested = params.get("protocolVersion")
        self.protocol_version = negotiate(requested)
        info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        self.client = {"name": str(info.get("name") or "")[:80], "version": str(info.get("version") or "")[:40],
                       "protocol_version": str(requested or "")[:40]}
        self.log(f"Client {self.client['name'] or 'unknown'} {self.client['version']} asked for protocol "
                 f"{requested!s:.40}; answering {self.protocol_version}.")
        return {"protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": dict(self.server_info),
                "instructions": self.tools.instructions()}

    def _handle(self, method, params, call):
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools.list_tools()}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(name, str) or not name:
                raise ProtocolError(INVALID_PARAMS, "tools/call needs the tool's name.")
            if not isinstance(arguments, dict):
                raise ProtocolError(INVALID_PARAMS, "arguments must be an object.")
            started = time.monotonic()
            result = self.tools.call(name, arguments, call)
            self.log(f"{name} returned in {time.monotonic() - started:.2f} s"
                     + (" (error)" if result.is_error else ""))
            return result.payload(self.structured_ok)
        raise ProtocolError(METHOD_NOT_FOUND, f"Mobster doesn't serve {method[:80]}.")

    @property
    def structured_ok(self):
        return self.protocol_version >= STRUCTURED_SINCE

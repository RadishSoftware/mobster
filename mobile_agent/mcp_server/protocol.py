"""MCP over stdio: newline-delimited JSON-RPC 2.0 on the standard library (the ``mcp`` SDK bloats the binary)."""

import json
import os
import sys
import threading
import time
import traceback

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28")
DEFAULT_PROTOCOL_VERSION = "2025-11-25"
STRUCTURED_SINCE = "2025-06-18"
RESULT_TYPE_SINCE = "2026-07-28"
PROGRESS_SECONDS = 5.0
MAX_LINE_BYTES = 16 * 1024 * 1024

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
# A tool that changes the phone is destructive to every client: a tap can delete.
ACTS = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}
LOCAL = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ToolResult:
    def __init__(self, text, structured=None, images=(), is_error=False, no_effect=False):
        self.text, self.structured, self.images, self.is_error = text, structured, tuple(images), bool(is_error)
        self.no_effect = no_effect or ""  # why nothing showed the action took effect; never sent to a client

    def payload(self, structured_ok):
        content = [{"type": "text", "text": self.text}]
        content += [{"type": "image", "data": image, "mimeType": "image/jpeg"} for image in self.images]
        result = {"content": content, "isError": self.is_error}
        if structured_ok and self.structured is not None:
            result["structuredContent"] = self.structured
        return result


class Call:
    def __init__(self, request_id, progress_token=None):
        self.id = request_id
        self.started = time.monotonic()
        self.progress_token = progress_token
        self.cancelled = threading.Event()
        self.stop = threading.Event()
        self.done = threading.Event()
        self.message = ""
        self.run_id = None

    def cancel(self):
        self.cancelled.set()
        self.stop.set()

    def note(self, message):
        if isinstance(message, str) and message:
            self.message = message[:300]


def claim_stdout():
    # From here on fd 1 is stderr, so a stray print or a child process never corrupts the protocol stream.
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    proto = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    return proto


def negotiate(requested):
    return requested if requested in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION


def _error_message(request_id, code, text):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": text}}


def _request_id_ok(value):
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


class Server:
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

    def serve(self, reader, writer):
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
                    self.log("A message couldn't be handled: " + traceback.format_exc(limit=5))
        finally:
            self.close("the MCP client disconnected")
        return 0

    @staticmethod
    def _skip_rest(reader, line):
        while line and not line.endswith(b"\n"):
            line = reader.readline(MAX_LINE_BYTES)

    def close(self, reason, grace=5.0):
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

    def _receive(self, line):
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            # RecursionError: arrays or objects nested deeper than the parser goes.
            self._error(None, PARSE_ERROR, "The message is not valid JSON.")
            return
        if isinstance(message, list):
            if not message:
                self._error(None, INVALID_REQUEST, "The batch is empty.")
            for item in message:
                self._dispatch(item)
        else:
            self._dispatch(message)

    def _dispatch(self, message):
        # Admitted on the reader thread so a cancel that follows is never lost; initialize is answered here so the
        # version is set before the next message is read.
        job = self._admit(message, self.send)
        if job is None:
            return
        call, method, params = job
        if method == "initialize":
            self._answer(call, method, params, self.send, self.protocol_version)
            return
        threading.Thread(target=self._serve_request, args=(call, method, params, self.protocol_version),
                         daemon=True, name=f"mobster-mcp-{call.id}").start()

    def _serve_request(self, call, method, params, version):
        try:
            self._answer(call, method, params, self.send, version)
        except Exception:
            # Never a traceback from a dying thread (at exit, one raced the interpreter for stderr and aborted
            # the process).
            self.log("Internal error: " + traceback.format_exc(limit=8))

    def _claim_call(self, call):
        with self._calls_lock:
            if call.id in self._calls:
                return False
            self._calls[call.id] = call
            return True

    def _finish_call(self, call):
        call.done.set()
        with self._calls_lock:
            if self._calls.get(call.id) is call:
                self._calls.pop(call.id, None)

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

    def _error(self, request_id, code, message):
        self.send(_error_message(request_id, code, message))

    def handle_message(self, message, send, *, protocol_version, cancelled=None):
        job = self._admit(message, send, cancelled)
        if job is None:
            return None
        version = protocol_version if protocol_version in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        return self._answer(*job, send, version)

    def _admit(self, message, send, cancelled=None):
        def refuse(request_id, code, text):
            send(_error_message(request_id, code, text))

        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            request_id = message.get("id") if isinstance(message, dict) else None
            refuse(request_id if _request_id_ok(request_id) else None, INVALID_REQUEST,
                   "The message is not a JSON-RPC 2.0 request.")
            return None
        method = message.get("method")
        if not isinstance(method, str):
            return None  # a response to a request this server never sends, or noise
        params = message.get("params")
        if params is None:
            params = {}
        if "id" not in message:
            self._notification(method, params)
            return None
        request_id = message["id"]
        if not _request_id_ok(request_id):
            refuse(None, INVALID_REQUEST, "The request id must be a string or an integer.")
            return None
        if not isinstance(params, dict):
            refuse(request_id, INVALID_PARAMS, "params must be an object.")
            return None
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        token = meta.get("progressToken")
        call = Call(request_id, token if _request_id_ok(token) else None)
        if cancelled is not None:
            call.stop = call.cancelled = cancelled
        if not self._claim_call(call):
            refuse(request_id, INVALID_REQUEST, "A request with this id is already running.")
            return None
        return call, method, params

    def _answer(self, call, method, params, send, version):
        def reply(*, result=None, error=None):
            out = _error_message(call.id, *error) if error else {"jsonrpc": "2.0", "id": call.id, "result": result}
            send(out)
            return out

        if call.progress_token is not None:
            threading.Thread(target=self._progress, args=(call, send), daemon=True,
                             name=f"mobster-mcp-progress-{call.id}").start()
        try:
            if call.cancelled.is_set():
                return None
            try:
                if method == "initialize":
                    result = self._initialize(params)
                else:
                    result = self._handle(method, params, call, version)
            except ProtocolError as error:
                return None if call.cancelled.is_set() else reply(error=(error.code, str(error)))
            except Exception:
                self.log("Internal error: " + traceback.format_exc(limit=8))
                if call.cancelled.is_set():
                    return None
                return reply(error=(INTERNAL_ERROR, "Mobster hit an internal error. See its log on stderr."))
            if call.cancelled.is_set():
                return None
            if isinstance(result, dict) and version >= RESULT_TYPE_SINCE:
                result = {**result, "resultType": "complete"}
            return reply(result=result)
        finally:
            self._finish_call(call)

    def _progress(self, call, send):
        count = 0
        while not call.done.wait(self.progress_seconds):
            if call.stop.is_set():
                return
            count += 1
            try:
                send({"jsonrpc": "2.0", "method": "notifications/progress",
                      "params": {"progressToken": call.progress_token, "progress": count,
                                 "message": call.message or "Working"}})
            except Exception:  # noqa: BLE001 -- a transport that can't take progress never ends the call
                self.log("A progress notification couldn't be sent: " + traceback.format_exc(limit=3))

    def _initialize(self, params):
        requested = params.get("protocolVersion")
        self.protocol_version = negotiate(requested)
        info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        self.client = {"name": str(info.get("name") or "")[:80], "version": str(info.get("version") or "")[:40],
                       "protocol_version": str(requested or "")[:40]}
        try:
            self.tools.client = self.client  # the approval card says who asks ("it says it is Claude Code")
        except AttributeError:
            pass
        self.log(f"Client {self.client['name'] or 'unknown'} {self.client['version']} asked for protocol "
                 f"{requested!s:.40}; answering {self.protocol_version}.")
        return {"protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": dict(self.server_info),
                "instructions": self.tools.instructions()}

    def _handle(self, method, params, call, version):
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
            return result.payload(version >= STRUCTURED_SINCE)
        raise ProtocolError(METHOD_NOT_FOUND, f"Mobster doesn't serve {method[:80]}.")

"""A small client for the running Mobster's HTTP API: `mobster chat` and MCP's ``phone_task`` use it.

They never run tasks themselves: they hand them to the Mobster Mac app or `mobster serve` (whichever is running),
so approvals show where a person answers them. The client finds the service at $MOBSTER_URL or
http://127.0.0.1:8765, and its per-launch token in $MOBSTER_API_TOKEN, the file $MOBSTER_API_TOKEN_FILE names, or
the token file the service writes beside its journal (the Mac app's data folder, or a source checkout's
mobile_agent/.state). It only ever talks to this Mac (127.0.0.1 or localhost), so the token never leaves it,
and it never prints the token.
"""

import http.client
import json
import os
from pathlib import Path
import socket
import time
from urllib.parse import quote, urlsplit

DEFAULT_URL = "http://127.0.0.1:8765"
LOCAL_HOSTS = ("127.0.0.1", "localhost")
NOT_RUNNING = "Open Mobster or run `mobster serve` to chat."
NO_THREADS = "This Mobster doesn't have conversations yet. Update Mobster, then try again."
REFUSED = ("Mobster is running, but its token isn't readable here. Run this as the same user, or set "
           "MOBSTER_API_TOKEN_FILE to the token file `mobster serve` printed.")
NOT_LOCAL = "Mobster's address must be on this Mac (127.0.0.1 or localhost)."


class NoService(Exception):
    """Nothing answers at the address, or it isn't Mobster."""


class ServiceError(Exception):
    """An HTTP error from Mobster: ``status``, ``code`` and the sentence it sent."""

    def __init__(self, status, data):
        self.status = status
        self.data = data if isinstance(data, dict) else {}
        self.code = self.data.get("code")
        super().__init__(self.data.get("error") or f"Mobster answered {status}")


def default_url():
    return (os.environ.get("MOBSTER_URL") or "").strip() or DEFAULT_URL


def token_candidates():
    """Tokens to try, in order (values, never printed)."""
    from ..paths import source_checkout, user_data_dir
    seen = []
    value = os.environ.get("MOBSTER_API_TOKEN", "").strip()
    if value:
        seen.append(value)
    files = []
    if os.environ.get("MOBSTER_API_TOKEN_FILE"):
        files.append(Path(os.environ["MOBSTER_API_TOKEN_FILE"]).expanduser())
    files.append(user_data_dir() / "state" / "api-token")
    if source_checkout():
        files.append(Path(__file__).resolve().parent.parent / ".state" / "api-token")
    for path in files:
        try:
            text = path.read_text().strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text and text not in seen:
            seen.append(text)
    return seen


class Client:
    def __init__(self, url=None, token=None, origin="cli", timeout=10.0):
        parts = urlsplit(url or default_url())
        if parts.scheme != "http" or (parts.hostname or "") not in LOCAL_HOSTS:
            raise NoService(NOT_LOCAL)
        self.host, self.port = parts.hostname, parts.port or 80
        self.url = f"http://{self.host}:{self.port}"
        self.token, self.origin, self.timeout = token, origin, timeout
        self.status = {}
        self._app_session = None

    @classmethod
    def connect(cls, url=None, origin="cli", tokens=None):
        """A client whose token the service accepts. NoService when nothing (or something else) answers;
        ServiceError 401 when no token works."""
        refused = False
        for token in (token_candidates() if tokens is None else tokens) or [None]:
            client = cls(url, token, origin)
            try:
                status = client.call("GET", "/api/status")
            except ServiceError as error:
                if error.status == 401:
                    refused = True
                    continue
                raise NoService(NOT_RUNNING) from None
            if not isinstance(status, dict) or "api_version" not in status:
                raise NoService(NOT_RUNNING)
            client.status = status
            return client
        if refused:
            raise ServiceError(401, {"error": REFUSED, "code": "unauthorized"})
        raise NoService(NOT_RUNNING)

    @property
    def has_threads(self):
        return (self.status.get("extensions") or {}).get("threads") == "ok"

    def _connection(self, timeout):
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def headers(self, extra=None):
        headers = {"Accept": "application/json", "X-Mobster-Origin": self.origin}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers.update(extra or {})
        return headers

    def call(self, method, path, body=None, *, key=None, timeout=None, raw=None, content_type=None):
        """The JSON answer of one request. ServiceError for a 4xx/5xx; NoService when nothing answers."""
        headers = self.headers({"Idempotency-Key": key} if key else None)
        data = None
        if raw is not None:
            data = raw
            headers["Content-Type"] = content_type or "application/octet-stream"
        elif body is not None or method == "POST":
            data = json.dumps(body if body is not None else {}).encode()
            headers["Content-Type"] = "application/json"
        if data is not None:
            headers["Content-Length"] = str(len(data))
        connection = self._connection(timeout or self.timeout)
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            payload = response.read()
        except (ConnectionError, socket.timeout, TimeoutError, OSError, http.client.HTTPException):
            raise NoService(NOT_RUNNING) from None
        finally:
            connection.close()
        try:
            value = json.loads(payload) if payload else {}
        except ValueError:
            value = {}
        if response.status >= 400:
            raise ServiceError(response.status, value)
        return value

    def events(self, path, last_id=None, timeout=30.0):
        """Server-sent events of ``path``: yields each event's data (a dict), and None for a keepalive. Ends when
        the service closes the stream. NoService when it can't connect; socket.timeout when it goes quiet."""
        headers = self.headers({"Accept": "text/event-stream"})
        if last_id is not None:
            headers["Last-Event-ID"] = str(last_id)
        connection = self._connection(timeout)
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
        except (ConnectionError, socket.timeout, TimeoutError, OSError, http.client.HTTPException):
            connection.close()
            raise NoService(NOT_RUNNING) from None
        if response.status >= 400:
            payload = response.read()
            connection.close()
            try:
                raise ServiceError(response.status, json.loads(payload))
            except ValueError:
                raise ServiceError(response.status, {}) from None
        try:
            data = []
            while True:
                line = response.readline()
                if not line:
                    return
                line = line.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith(":"):
                    yield None
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    try:
                        yield json.loads("\n".join(data))
                    except ValueError:
                        pass
                    data = []
        finally:
            connection.close()

    # -- what chat and MCP use ----------------------------------------------------------------------------------

    def thread(self, thread_id):
        return self.call("GET", f"/api/threads/{quote(thread_id, safe='')}")

    def run(self, run_id):
        return self.call("GET", f"/api/runs/{quote(run_id, safe='')}")["run"]

    def send(self, thread_id, body, key=None):
        """POST a message; a new thread when ``thread_id`` is None. Returns (thread id, response)."""
        if thread_id is None:
            answer = self.call("POST", "/api/threads", {"message": body}, key=key)
            return answer["thread"]["id"], answer
        return thread_id, self.call("POST", f"/api/threads/{quote(thread_id, safe='')}/messages", body, key=key)

    def app_session(self, run_id):
        """Whether approvals here need the Mac app (it holds the app session, S6.6). Asked once, with an approval
        id that can't match, so it never answers anything: the Mac app refuses it 403 app_only, `serve` 409."""
        if self._app_session is None:
            try:
                self.call("POST", f"/api/runs/{quote(run_id, safe='')}/approval",
                          {"id": "000000000000", "approve": True})
                self._app_session = False
            except ServiceError as error:
                self._app_session = error.code == "app_only"
        return self._app_session

    def wait_run(self, run_id, seconds, poll=0.5, until=None):
        """The run once it finished, waits for someone, or ``seconds`` passed (polling)."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            run = self.run(run_id)
            if run.get("finishedAt") is not None or run.get("approval") or (until and until(run)):
                return run
            if time.monotonic() >= deadline:
                return run
            time.sleep(min(poll, max(0.05, deadline - time.monotonic())))

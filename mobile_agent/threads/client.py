"""A small client for the running Mobster's HTTP API: `mobster chat` and MCP's ``phone_task`` use it."""

import http.client
import json
import os
import socket
import time
from pathlib import Path
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

    frozen = False


class ServiceError(Exception):
    """An HTTP error from Mobster: ``status``, ``code`` and the sentence it sent."""

    def __init__(self, status, data):
        self.status = status
        self.data = data if isinstance(data, dict) else {}
        self.code = self.data.get("code")
        super().__init__(self.data.get("error") or f"Mobster answered {status}")


def default_url():
    """$MOBSTER_URL, else the address a running service wrote beside its token file, else 8765."""
    return (os.environ.get("MOBSTER_URL") or "").strip() or discovered_url() or DEFAULT_URL


def written_urls():
    """The addresses in the `api-url` files (the Mac app's data folder, then a source checkout's .state) that are on
    this Mac, whether or not anything answers there. A service that crashed leaves its file behind; a clean exit
    removes it."""
    from ..paths import source_checkout, user_data_dir
    files = [user_data_dir() / "state" / "api-url"]
    if source_checkout():
        files.append(Path(__file__).resolve().parent.parent / ".state" / "api-url")
    urls = []
    for path in files:
        try:
            url = path.read_text().strip()
        except (OSError, UnicodeDecodeError):
            continue
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            continue
        if parts.scheme == "http" and (parts.hostname or "") in LOCAL_HOSTS and port:
            urls.append(f"http://{parts.hostname}:{port}")
    return urls


def service_urls():
    """Where a Mobster service on this Mac is meant to answer: $MOBSTER_URL when it is set, else the addresses services
    wrote (``written_urls``), answering or not."""
    return [url] if (url := (os.environ.get("MOBSTER_URL") or "").strip()) else written_urls()


def discovered_url():
    """The first written address that is on this Mac and accepts a connection. A file left by a crashed service names a
    port nobody holds, so it is skipped."""
    for url in written_urls():
        parts = urlsplit(url)
        try:
            socket.create_connection((parts.hostname, parts.port), timeout=0.2).close()
        except OSError:
            continue
        return url
    return None


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
    def __init__(self, url=None, token=None, origin="cli", timeout=10.0, extra_headers=None):
        parts = urlsplit(url or default_url())
        if parts.scheme != "http" or (parts.hostname or "") not in LOCAL_HOSTS:
            raise NoService(NOT_LOCAL)
        self.host, self.port = parts.hostname, parts.port or 80
        self.url = f"http://{self.host}:{self.port}"
        self.token, self.origin, self.timeout = token, origin, timeout
        # Sent with every request (Mobster Remote's X-Mobster-Remote-Grant).
        self.extra_headers = dict(extra_headers or {})
        self.status = {}
        self._app_session = False

    @classmethod
    def connect(cls, url=None, origin="cli", tokens=None, timeout=10.0):
        """A client whose token the service accepts."""
        refused = False
        for token in (token_candidates() if tokens is None else tokens) or [None]:
            client = cls(url, token, origin, timeout=timeout)
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
            client._app_session = bool(status.get("app_session"))
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
        headers.update(self.extra_headers)
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
        except (ConnectionError, socket.timeout, TimeoutError, OSError, http.client.HTTPException) as error:
            silent = NoService(NOT_RUNNING)
            silent.frozen = isinstance(error, (socket.timeout, TimeoutError))
            raise silent from None
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
        """Server-sent events of ``path``: yields each event's data (a dict), and None for a keepalive."""
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

    def workflows(self):
        """The saved workflows (GET /api/workflows), each with its blanks."""
        return self.call("GET", "/api/workflows").get("workflows") or []

    def workflow_tools(self):
        """Each workflow made a tool (GET /api/workflows/tools): its MCP name, REST path and input schema."""
        return self.call("GET", "/api/workflows/tools").get("tools") or []

    def find_workflow(self, wanted):
        """A workflow by its id or its name (case and spacing ignored). LookupError says what's wrong."""
        wanted = " ".join(str(wanted or "").split())
        listed = self.workflows()
        found = [w for w in listed if w.get("id") == wanted.lower()] or [
            w for w in listed if " ".join(str(w.get("name") or "").split()).casefold() == wanted.casefold()]
        if not found:
            names = ", ".join(f"“{w.get('name')}”" for w in listed[:8])
            raise LookupError(f"No workflow is called {wanted}." + (f" Yours: {names}." if names else
                                                                     " Save one in Mobster first."))
        if len(found) > 1:
            raise LookupError(f"Several workflows are called {wanted}. Give its id: "
                              + ", ".join(w["id"] for w in found))
        return found[0]

    def run_workflow(self, workflow_id, *, values=None, thread_id=None, new_conversation=False, key=None):
        """POST /api/workflows/{id}/run. Returns (thread id or None, response with run and replayed)."""
        import uuid
        body = {}
        if values:
            body["values"] = values
        if thread_id:
            body["threadId"] = thread_id
        if new_conversation:
            body["newConversation"] = True
        answer = self.call("POST", f"/api/workflows/{quote(workflow_id, safe='')}/run", body,
                           key=key or uuid.uuid4().hex)
        return answer.get("threadId"), answer

    def workflow_runs(self, workflow_id):
        """A workflow's newest runs (GET /api/workflows/{id}/runs), each graph run with its nodes."""
        return self.call("GET", f"/api/workflows/{quote(workflow_id, safe='')}/runs").get("batches") or []

    def wait_batch(self, workflow_id, batch_id, seconds, poll=0.5, on_node=None):
        """A workflow's graph run once it ended or ``seconds`` passed (polling)."""
        deadline, seen = time.monotonic() + max(0.0, seconds), {}
        while True:
            batch = next((b for b in self.workflow_runs(workflow_id) if b.get("id") == batch_id), None)
            if batch is None:
                return None
            for instance, node in sorted((batch.get("nodes") or {}).items(), key=lambda item: item[1].get("seq", 0)):
                if on_node is not None and seen.get(instance) != node.get("state"):
                    seen[instance] = node.get("state")
                    on_node(node)
            if batch.get("status") != "running" or time.monotonic() >= deadline:
                return batch
            time.sleep(min(poll, max(0.05, deadline - time.monotonic())))

    def app_session(self):
        """Whether approvals here need the Mac app (it holds the app session, S6.6)."""
        return self._app_session

    def group(self, group_id):
        return self.call("GET", f"/api/groups/{quote(group_id, safe='')}")["group"]

    def wait_group(self, group_id, seconds, poll=0.5):
        """A task on several phones once every phone is done (its merged summary is in), one waits for someone, or
        ``seconds`` passed (polling)."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            group = self.group(group_id)
            if group.get("state") in ("finished", "waiting") or time.monotonic() >= deadline:
                return group
            time.sleep(min(poll, max(0.05, deadline - time.monotonic())))

    def wait_run(self, run_id, seconds, poll=0.5, until=None, through=False, through_approval=False):
        """The run once it finished, waits for someone, or ``seconds`` passed (polling)."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            run = self.run(run_id)
            approval = run.get("approval")
            waits = (bool(approval) and not through and not (through_approval and approval.get("kind") != "clarify")
                     or bool(run.get("phoneWait")) and not through)
            if run.get("finishedAt") is not None or waits or (until and until(run)):
                return run
            if time.monotonic() >= deadline:
                return run
            pause = 1.0 if run.get("approval") else poll
            time.sleep(min(pause, max(0.05, deadline - time.monotonic())))

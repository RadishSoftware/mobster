import http.client
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import api_http, api_routes, harness_api, server, tracks
from mobile_agent import serve as serve_module
from mobile_agent.api_errors import APIError
from mobile_agent.api_routes import Response, Route
from mobile_agent.journal import JournalError
from mobile_agent.server import BoundedServer, make_handler
from mobile_agent.tests.seam_support import isolate, request
from mobile_agent.tests.test_server_engine import Base

TOKEN = "t" * 43
SESSION = "s" * 40
APP = {"X-Mobster-App-Session": SESSION}


def ok(request):
    return Response.json({"params": request.params, "origin": request.origin, "body": request.body,
                          "query": request.query})


def start(testcase, handler):
    httpd = BoundedServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    testcase.addCleanup(httpd.server_close)
    testcase.addCleanup(httpd.shutdown)
    return httpd.server_address[1]


def fetch(port, method, path, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    sent = {"Host": "127.0.0.1:8765", **({"Content-Type": "application/json"} if body is not None else {}),
            **(headers or {})}
    connection.request(method, path, body=body, headers=sent)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response, payload


def mock_runtime():
    runtime = Mock()
    runtime.status.return_value = {"live_enabled": False}
    runtime.config = SimpleNamespace(port=8765)
    return runtime


class FakeServer:
    def __init__(self, address, handler):
        self.handler = handler
        self.server_address = (address[0], 8767)

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        pass


class RegistryTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_prefixes_are_enforced(self):
        api_routes.register(Route("threads", "GET", "/api/threads", ok))
        api_routes.register(Route("threads", "GET", "/api/threads/{id}", ok))
        api_routes.register(Route("harness", "POST", "/api/runs/{id}/messages", ok))
        api_routes.register(Route("wireless", "POST", "/api/devices/{device}/wifi", ok))
        api_routes.register(Route("files", "POST", "/api/attachments", ok, body="raw", max_bytes=26_214_400))
        for route in (Route("memory", "GET", "/api/threads", ok),
                      Route("harness", "GET", "/api/runs/{id}", ok),
                      Route("harness", "POST", "/api/runs/{id}/stop", ok),
                      Route("threads", "GET", "/api/threadsx", ok),
                      Route("threads", "GET", "/api/status", ok),
                      Route("nobody", "GET", "/api/threads", ok),
                      Route("threads", "PUT", "/api/threads/x", ok),
                      Route("threads", "GET", "/api/threads/{id}", ok),
                      Route("threads", "POST", "/api/threads/new", ok, body="json", max_bytes=100_000)):
            with self.subTest(route=(route.owner, route.method, route.pattern)), self.assertRaises(ValueError):
                api_routes.register(route)
        found = api_routes.match("GET", "/api/threads/3f9c2a1b7d0e")
        self.assertEqual(found[1], {"id": "3f9c2a1b7d0e"})
        self.assertIsNone(api_routes.match("GET", "/api/threads/NOT-AN-ID"))
        self.assertIsNone(api_routes.match("POST", "/api/threads"))
        self.assertEqual(api_routes.match("POST", "/api/devices/Sam's%20iPhone/wifi")[1], {"device": "Sam's%20iPhone"})


class RouteTests(Base):
    def setUp(self):
        super().setUp()
        isolate(self)
        tracks._loaded = True
        self.app = self.runtime()
        api_routes._routes.clear()
        self.handler = make_handler(self.app)

    def call(self, method, path, body=None, **kwargs):
        return request(self.handler, method, path, body, **kwargs)

    def test_registered_routes_come_first_and_only_under_their_prefixes(self):
        api_routes.register(Route("threads", "GET", "/api/threads", ok))
        status, data, _ = self.call("GET", "/v1/threads?limit=5", headers={"X-Mobster-Origin": "tui"})
        self.assertEqual(status, 200)
        self.assertEqual((data["origin"], data["query"]), ("tui", {"limit": ["5"]}))
        status, data, _ = self.call("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertIn("extensions", data)
        status, _, _ = self.call("GET", "/api/threads", headers={"X-Mobster-Origin": "workflow"})
        self.assertEqual(status, 400)

    def test_json_and_raw_bodies_and_their_caps(self):
        api_routes.register(Route("memory", "POST", "/api/memory", ok, max_bytes=200))
        api_routes.register(Route("files", "POST", "/api/attachments", ok, body="raw", max_bytes=10))
        api_routes.register(Route("memory", "POST", "/api/memory/clear", ok, body="none"))
        status, data, _ = self.call("POST", "/api/memory", {"text": "Kate Bell is my sister"})
        self.assertEqual((status, data["body"]), (200, {"text": "Kate Bell is my sister"}))
        status, _, _ = self.call("POST", "/api/memory", raw=b"text", content_type="text/plain")
        self.assertEqual(status, 415)
        received = []
        api_routes._routes[1] = Route("files", "POST", "/api/attachments",
                                      lambda r: received.append(r.body) or Response.json({"ok": True}),
                                      body="raw", max_bytes=10)
        status, _, _ = self.call("POST", "/api/attachments", raw=b"%PDF-1.7", content_type="application/pdf")
        self.assertEqual((status, received), (200, [b"%PDF-1.7"]))
        self.assertEqual(self.call("POST", "/api/memory/clear")[0], 200)
        self.assertEqual(self.call("POST", "/api/memory/clear", {"a": 1})[0], 400)

    def test_an_oversize_body_names_its_limit_after_the_body_is_read(self):
        api_routes.register(Route("files", "POST", "/api/attachments", ok, body="raw", max_bytes=26_214_400))
        api_routes.register(Route("memory", "POST", "/api/memory", ok, max_bytes=64_000))
        status, data, _ = self.call("POST", "/api/attachments", raw=b"x" * 26_214_401, content_type="text/plain")
        self.assertEqual((status, data["code"], data["limit"]), (413, "too_large", 26_214_400))
        self.assertEqual(data["error"], "Attach files of 25 MB or less.")
        status, data, _ = self.call("POST", "/api/memory", {"text": "x" * 70_000})
        self.assertEqual((status, data["error"]), (413, "This request is larger than 62 KB."))

    def test_over_a_real_socket_the_client_reads_the_413_instead_of_a_reset(self):
        api_routes.register(Route("files", "POST", "/api/attachments", ok, body="raw", max_bytes=1 << 20))
        port = start(self, self.handler)
        response, payload = fetch(port, "POST", "/api/attachments?name=big.bin", body=b"x" * (3 << 20),
                                  headers={"Content-Type": "application/octet-stream"})
        self.assertEqual((response.status, json.loads(payload)["error"]), (413, "Attach files of 1 MB or less."))

    def test_errors_map_as_the_core_maps_them(self):
        def raises(error):
            def handler(request):
                raise error
            return handler
        cases = {"value": (ValueError("Say what to remember"), 400), "api": (APIError("Busy", 409, "busy"), 409),
                 "journal": (JournalError("disk"), 503), "timeout": (TimeoutError(), 408), "io": (OSError(), 500)}
        for name, (error, _status) in cases.items():
            api_routes.register(Route("memory", "GET", f"/api/memory/{name}", raises(error)))
        for name, (_error, expected) in cases.items():
            with self.subTest(case=name):
                status, data, _ = self.call("GET", f"/api/memory/{name}")
                self.assertEqual(status, expected)
                self.assertIn("error", data)
        self.assertEqual(self.call("GET", "/api/memory/api")[1]["code"], "busy")

    def test_a_track_that_is_off_answers_503(self):
        api_routes.register(Route("files", "GET", "/api/attachments", ok))
        tracks.disable("attachments", "error: newer data")
        status, data, _ = self.call("GET", "/api/attachments")
        self.assertEqual(status, 503)
        self.assertEqual(data["error"], "This was saved by a newer Mobster. Update Mobster to use it.")

    def test_sse_routes_stream_and_count_against_the_limit(self):
        api_routes.register(Route("threads", "GET", "/api/threads/stream",
                                  lambda r: Response.sse(iter([{"seq": 1, "event": "a"}, None,
                                                               {"id": "abc-2", "event": "b"}])), stream=True))
        status, data, headers = self.call("GET", "/api/threads/stream")
        self.assertEqual((status, headers["Content-Type"]), (200, "text/event-stream"))
        text = data.decode()
        self.assertIn('id: 1\ndata: {"seq": 1, "event": "a"}\n\n', text)
        self.assertIn(": keepalive\n\n", text)
        self.assertIn("id: abc-2\n", text)
        for _ in range(8):
            self.assertTrue(self.app.streams.acquire(blocking=False))
        try:
            self.assertEqual(self.call("GET", "/api/threads/stream")[0], 429)
            self.assertEqual(self.call("GET", "/api/events?topics=runs")[0], 429)
        finally:
            for _ in range(8):
                self.app.streams.release()

    def test_events_stream_runs_and_resumes(self):
        bus = self.app.bus
        self.app.event_keepalive_s = .05
        before = bus.position()
        bus.publish("runs", {"event": "run_created", "runId": "a" * 12})
        threading.Timer(.2, lambda: setattr(self.app, "closing", True)).start()
        status, data, headers = self.call("GET", "/api/events?topics=runs", headers={"Last-Event-ID": before})
        self.app.closing = False
        self.assertEqual(status, 200)
        lines = [line for line in data.decode().split("\n\n") if line.startswith("id:")]
        self.assertTrue(lines[0].startswith(f"id: {bus.boot}-"))
        self.assertEqual(json.loads(lines[0].split("data: ", 1)[1])["event"], "run_created")
        self.assertEqual(self.call("GET", "/api/events?topics=Bad")[0], 400)

    def test_a_run_takes_registered_fields_and_its_origin_label(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        harness_api.register_run_field("threadId", lambda value, runtime: str(value))
        status, data, _ = self.call("POST", "/api/runs", {"appId": "messages", "goal": "Text Sam",
                                                           "threadId": "3f9c2a1b7d0e"},
                                    headers={"X-Mobster-Origin": "app"})
        self.assertEqual(status, 201)
        self.assertEqual((data["run"]["extras"], data["run"]["origin"]), ({"threadId": "3f9c2a1b7d0e"}, "app"))
        run = self.app.runs[data["run"]["id"]]
        self.finish(self.app, run)
        status, data, _ = self.call("POST", "/api/runs", {"appId": "messages", "goal": "Hi", "nope": 1})
        self.assertEqual((status, data["error"]), (400, "Unsupported task field"))


class AppSessionTests(Base):
    def setUp(self):
        super().setUp()
        isolate(self)
        tracks._loaded = True
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        self.app = self.runtime(managed=True)
        self.with_session = make_handler(self.app, app_session=SESSION)
        self.without = make_handler(self.app)
        self.put = []
        api_routes.register(Route("files", "POST", "/api/phone/files/put",
                                  lambda r: self.put.append(r.app_session) or Response.json({"ok": True})))
        api_routes.register(Route("wireless", "POST", "/api/wireless/reset", lambda r: Response.json({"ok": True})))
        api_routes.register(Route("files", "GET", "/api/phone/files", lambda r: Response.json({"files": []})))

    def pending(self, kind="commit", **extra):
        run = self.app.create("messages", "Text Sam", "live")
        answer = []
        question = {"kind": kind, "operation": "ASK_USER" if kind == "clarify" else "TAP",
                    "label": "Which Sam?" if kind == "clarify" else "Send", "text": None, "title": "Send?", **extra}
        thread = threading.Thread(target=lambda: answer.append(run.request_approval(question, timeout=5)))
        thread.start()
        for _ in range(500):
            if run.public()["approval"]:
                break
            time.sleep(.005)
        self.addCleanup(lambda: (run.stop.set(), thread.join(5), self.finish(self.app, run)))
        return run, run.public()["approval"]["id"], thread, answer

    def test_approving_needs_the_app(self):
        run, approval, thread, answer = self.pending()
        path = f"/api/runs/{run.id}/approval"
        for headers in ({}, {"X-Mobster-Origin": "app"}, {"X-Mobster-App-Session": "wrong" * 8}):
            with self.subTest(headers=headers):
                status, data, _ = request(self.with_session, "POST", path, {"id": approval, "approve": True},
                                          headers=headers)
                self.assertEqual((status, data["code"]), (403, "app_only"))
                self.assertEqual(data["error"], "Approve this in the Mobster app on this Mac. "
                                                "From here you can only decline it.")
        status, _, _ = request(self.with_session, "POST", path, {"id": approval, "approve": True}, headers=APP)
        self.assertEqual(status, 200)
        thread.join(5)
        self.assertEqual(answer, ["approved"])

    def test_declines_redirects_and_stops_stay_token_only(self):
        run, approval, thread, answer = self.pending()
        status, _, _ = request(self.with_session, "POST", f"/api/runs/{run.id}/approval",
                               {"id": approval, "approve": False, "instruction": "Say 7pm instead"})
        self.assertEqual(status, 200)
        thread.join(5)
        self.assertEqual(answer, ["redirected:Say 7pm instead"])
        status, _, _ = request(self.with_session, "POST", f"/api/runs/{run.id}/stop", {})
        self.assertEqual(status, 200)

    def test_a_choice_on_a_loop_question_needs_the_app(self):
        run, approval, thread, answer = self.pending(kind="question", choices=[{"id": "all", "label": "All"}])
        path = f"/api/runs/{run.id}/approval"
        status, _, _ = request(self.with_session, "POST", path, {"id": approval, "approve": True, "choice": "all"})
        self.assertEqual(status, 403)
        status, _, _ = request(self.with_session, "POST", path, {"id": approval, "approve": True, "choice": "all"},
                               headers=APP)
        self.assertEqual(status, 200)
        thread.join(5)
        self.assertEqual(answer, ["choice:all"])

    def test_answers_to_clarifying_questions_stay_token_only(self):
        run, approval, thread, answer = self.pending(kind="clarify", question="Which Sam?")
        status, _, _ = request(self.with_session, "POST", f"/api/runs/{run.id}/approval",
                               {"id": approval, "approve": True, "answer": "Sam Lee"})
        self.assertEqual(status, 200)
        thread.join(5)
        self.assertEqual(answer, ["answer:Sam Lee"])

    def test_the_safety_settings_need_the_app_and_the_rest_do_not(self):
        for body in ({"askBeforeActing": False}, {"askBeforeActing": True, "videoFps": 30}):
            with self.subTest(body=body):
                status, data, _ = request(self.with_session, "POST", "/api/settings", body)
                self.assertEqual((status, data["code"]), (403, "app_only"))
        self.assertEqual(request(self.with_session, "POST", "/api/settings", {"askBeforeActing": False},
                                 headers=APP)[0], 200)
        self.assertEqual(request(self.with_session, "POST", "/api/settings", {"hearPhone": False})[0], 200)
        self.assertEqual(request(self.with_session, "GET", "/api/settings")[0], 200)

    def test_file_put_and_wireless_reset_need_the_app(self):
        self.assertEqual(request(self.with_session, "POST", "/api/phone/files/put", {"name": "menu.pdf"})[0], 403)
        self.assertEqual(self.put, [])
        self.assertEqual(request(self.with_session, "POST", "/api/phone/files/put", {"name": "menu.pdf"},
                                 headers=APP)[0], 200)
        self.assertEqual(self.put, [True])
        self.assertEqual(request(self.with_session, "POST", "/api/wireless/reset", {"device": "x"})[0], 403)
        self.assertEqual(request(self.with_session, "POST", "/api/wireless/reset", {"device": "x"},
                                 headers=APP)[0], 200)
        self.assertEqual(request(self.with_session, "GET", "/api/phone/files")[0], 200)

    def test_without_a_session_the_api_is_unchanged(self):
        run, approval, thread, answer = self.pending()
        status, _, _ = request(self.without, "POST", f"/api/runs/{run.id}/approval",
                               {"id": approval, "approve": True}, headers={"X-Mobster-App-Session": "anything"})
        self.assertEqual(status, 200)
        thread.join(5)
        self.assertEqual(answer, ["approved"])
        self.assertEqual(request(self.without, "POST", "/api/settings", {"askBeforeActing": False})[0], 200)
        self.assertEqual(request(self.without, "POST", "/api/phone/files/put", {"name": "menu.pdf"})[0], 200)
        self.assertEqual(self.put, [False])


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.runtime = mock_runtime()
        self.runtime.update_settings.return_value = {"askBeforeActing": False}
        self.port = start(self, make_handler(self.runtime, TOKEN))

    def request(self, method="GET", path="/api/status", token=None, host="127.0.0.1:8765", origin=None, body=None):
        headers = {"Host": host}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if origin:
            headers["Origin"] = origin
        return fetch(self.port, method, path, headers, body)[0]

    def test_requests_without_the_token_are_refused_and_with_it_admitted(self):
        self.assertEqual(self.request().status, 401)
        self.assertEqual(self.request(token="x" * 43).status, 401)
        self.assertEqual(self.request("POST", "/api/settings", body='{"askBeforeActing": false}').status, 401)
        self.runtime.update_settings.assert_not_called()
        self.assertEqual(self.request(token=TOKEN).status, 200)
        self.assertEqual(self.request("POST", "/api/settings", token=TOKEN, body='{"askBeforeActing": false}').status,
                         200)

    def test_streams_take_the_token_in_the_query_but_posts_do_not(self):
        self.assertEqual(self.request(path=f"/api/status?token={TOKEN}").status, 200)
        self.assertEqual(self.request(path="/api/device/stream?token=wrong").status, 401)
        self.assertEqual(self.request("POST", f"/api/settings?token={TOKEN}", body="{}").status, 401)

    def test_dns_rebinding_and_foreign_origins_are_refused_even_with_the_token(self):
        self.assertEqual(self.request(token=TOKEN, host="attacker.example:8765").status, 403)
        for origin, status in (("http://127.0.0.1:8765", 200), ("tauri://localhost", 200),
                               ("http://127.0.0.1:5173", 403), ("http://localhost:3000", 403),
                               ("https://evil.example", 403)):
            with self.subTest(origin=origin):
                response = self.request(token=TOKEN, origin=origin)
                self.assertEqual(response.status, status)
                self.assertEqual(response.getheader("Access-Control-Allow-Origin"), origin if status == 200 else None)
        self.assertIsNone(self.request(token=TOKEN).getheader("Access-Control-Allow-Origin"))
        self.assertEqual(self.request("OPTIONS", origin="https://evil.example").status, 403)

    def test_the_preflight_allows_the_apis_headers(self):
        response = self.request("OPTIONS", "/api/runs", origin="tauri://localhost")
        self.assertEqual(response.status, 204)
        allowed = response.getheader("Access-Control-Allow-Headers")
        for name in ("Authorization", "X-Mobster-Origin", "X-Mobster-App-Session", "Last-Event-ID", "Idempotency-Key"):
            self.assertIn(name, allowed)


class LaunchTokenTests(unittest.TestCase):
    def test_the_shell_secrets_are_taken_and_removed_from_the_environment(self):
        with patch.dict(os.environ, {"MOBSTER_API_TOKEN": TOKEN, "MOBSTER_APP_SESSION": SESSION}):
            self.assertEqual(serve_module.api_token(), TOKEN)
            self.assertEqual(serve_module.app_session_secret(), SESSION)
            self.assertNotIn("MOBSTER_API_TOKEN", os.environ)
            self.assertNotIn("MOBSTER_APP_SESSION", os.environ)
        self.assertIsNone(serve_module.app_session_secret())
        for name, read in (("MOBSTER_API_TOKEN", serve_module.api_token),
                           ("MOBSTER_APP_SESSION", serve_module.app_session_secret)):
            with patch.dict(os.environ, {name: "short"}), self.assertRaises(ValueError):
                read()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOBSTER_API_TOKEN", None)
            first, second = serve_module.api_token(), serve_module.api_token()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 40)

    def test_the_mac_app_passes_both_secrets_on_stdin_not_in_the_environment(self):
        session = "s" * 43
        read, write = os.pipe()
        with os.fdopen(read) as stream, os.fdopen(write, "w") as shell:
            shell.write(f"{TOKEN} {session}\n")
            shell.flush()
            with patch.dict(os.environ, {serve_module.SECRETS_ON_STDIN: "1"}):
                supplied = serve_module.launch_secrets(stream)
                self.assertNotIn(serve_module.SECRETS_ON_STDIN, os.environ)
        self.assertEqual(supplied, (TOKEN, session))
        with patch.dict(os.environ, {"MOBSTER_API_TOKEN": "x" * 40, "MOBSTER_APP_SESSION": "y" * 40}):
            self.assertEqual(serve_module.api_token(supplied[0]), TOKEN)
            self.assertEqual(serve_module.app_session_secret(supplied[1]), session)
            self.assertNotIn("MOBSTER_API_TOKEN", os.environ)
            self.assertNotIn("MOBSTER_APP_SESSION", os.environ)

    def test_without_the_marker_stdin_is_never_read(self):
        os.environ.pop(serve_module.SECRETS_ON_STDIN, None)
        self.assertEqual(serve_module.launch_secrets(stream=object()), (None, None))

    def test_a_shell_that_writes_nothing_or_garbage_fails_the_start(self):
        read, write = os.pipe()
        with os.fdopen(read) as stream, os.fdopen(write, "w") as shell:
            with patch.dict(os.environ, {serve_module.SECRETS_ON_STDIN: "1"}), self.assertRaises(ValueError):
                serve_module.launch_secrets(stream, wait=0.05)
            shell.write("only-one\n")
            shell.flush()
            with patch.dict(os.environ, {serve_module.SECRETS_ON_STDIN: "1"}), self.assertRaises(ValueError):
                serve_module.launch_secrets(stream, wait=1)

    def test_serve_requires_the_token_and_leaves_it_in_a_private_file_while_running(self):
        state = Path(tempfile.mkdtemp()) / "state" / "mobster.sqlite3"
        config = SimpleNamespace(port=0, wda_url=None, state_db=state)
        seen = {}

        class Serving(FakeServer):
            def serve_forever(self):
                seen["handler"] = self.handler
                path = state.parent / "api-token"
                seen["file"] = (path.read_text().strip(), stat.S_IMODE(path.stat().st_mode),
                                stat.S_IMODE(path.parent.stat().st_mode))
                url = state.parent / "api-url"
                seen["url"] = (url.read_text().strip(), stat.S_IMODE(url.stat().st_mode))
                raise KeyboardInterrupt

        with patch.dict(os.environ, {"MOBSTER_API_TOKEN": TOKEN}), patch.object(serve_module, "Runtime"), \
                patch.object(serve_module, "WorkflowScheduler"), patch.object(serve_module, "BoundedServer", Serving), \
                patch("builtins.print") as printed:
            serve_module.serve(config)
        self.assertEqual(seen["file"], (TOKEN, 0o600, 0o700))
        self.assertEqual(seen["url"], ("http://127.0.0.1:8767", 0o600))
        self.assertFalse((state.parent / "api-token").exists())
        self.assertFalse((state.parent / "api-url").exists())
        self.assertNotIn(TOKEN, str(printed.call_args))
        handler = seen["handler"]
        request = object.__new__(handler)
        request.headers, request.command, request.path = {"Authorization": "Bearer nope"}, "GET", "/api/status"
        self.assertFalse(request.authorized())
        request.headers = {"Authorization": f"Bearer {TOKEN}"}
        self.assertTrue(request.authorized())


class SetupScopeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = mock_runtime()
        self.runtime.setup.state.return_value = {"complete": False, "steps": []}
        self.setup_token = api_http.setup_scope_token(TOKEN)
        self.port = start(self, make_handler(self.runtime, TOKEN, app_session="s" * 43, setup_token=self.setup_token))

    def request(self, method, path, token, body=None):
        response, payload = fetch(self.port, method, path, {"Authorization": f"Bearer {token}"}, body)
        return response.status, payload

    def test_the_setup_token_is_derived_from_the_launch_token_and_does_not_reveal_it(self):
        self.assertEqual(self.setup_token, api_http.setup_scope_token(TOKEN))
        self.assertNotEqual(self.setup_token, api_http.setup_scope_token("u" * 43))
        self.assertNotIn(TOKEN, self.setup_token)
        self.assertRegex(self.setup_token, r"^[A-Za-z0-9_-]{43}$")

    def test_it_opens_setup_and_the_full_token_still_opens_every_route(self):
        self.assertEqual(self.request("GET", "/api/setup", self.setup_token)[0], 200)
        self.assertEqual(self.request("POST", "/api/setup/refresh", self.setup_token, body="{}")[0], 200)
        self.runtime.setup.refresh.assert_called_once()
        self.assertEqual(self.request("POST", "/api/setup/live", self.setup_token, body='{"enabled": true}')[0], 200)
        self.runtime.setup.set_live.assert_called_once_with(True)
        self.assertEqual(self.request("GET", "/api/status", TOKEN)[0], 200)

    def test_it_opens_the_signing_routes_for_the_setup_page_and_nothing_next_to_them(self):
        setup = self.runtime.setup
        self.assertEqual(self.request("POST", "/api/setup/signing/card", self.setup_token, body="{}")[0], 200)
        setup.signing_card.assert_called_once_with()
        digest = "a" * 64
        approve = json.dumps({"digest": digest})
        self.assertEqual(self.request("POST", "/api/setup/signing/approve", self.setup_token, body=approve)[0], 200)
        setup.signing_approve.assert_called_once_with(digest)
        resolve = json.dumps({"digest": digest, "made": False})
        self.assertEqual(self.request("POST", "/api/setup/signing/resolve", self.setup_token, body=resolve)[0], 200)
        setup.signing_resolve.assert_called_once_with(digest, False)
        self.assertEqual(self.request("POST", "/api/setup/signing/devmode", self.setup_token, body="{}")[0], 200)
        setup.signing_reveal_developer_mode.assert_called_once_with()
        # Other bodies are not routes; unlisted paths and verbs are outside the scope.
        self.assertEqual(self.request("POST", "/api/setup/signing/approve", self.setup_token, body="{}")[0], 404)
        self.assertEqual(self.request("POST", "/api/setup/signing/card", self.setup_token, body='{"x": 1}')[0], 404)
        for path in ("/api/setup/signing/export", "/api/setup/signing/import", "/api/setup/signing/../runs"):
            self.assertEqual(self.request("POST", path, self.setup_token, body="{}")[0], 403, path)
        self.assertEqual(self.request("GET", "/api/setup/signing/card", self.setup_token)[0], 403)

    def test_a_signing_refusal_is_a_409_that_says_why_and_what_to_do(self):
        from mobile_agent.devkit import DevError

        self.runtime.setup.signing_card.side_effect = DevError("Mobster has no App Store Connect key.", "Add one.")
        status, payload = self.request("POST", "/api/setup/signing/card", self.setup_token, body="{}")
        self.assertEqual(status, 409)
        self.assertEqual(
            json.loads(payload), {"error": "Mobster has no App Store Connect key. Add one.", "code": "signing_refused"}
        )

    def test_it_can_not_run_a_task_or_reach_anything_outside_setup(self):
        for method, path, body in (("POST", "/api/runs", '{"goal": "Open Settings", "appId": "settings"}'),
                                   ("POST", "/v1/runs", '{"goal": "Open Settings", "appId": "settings"}'),
                                   ("GET", "/api/runs", None), ("GET", "/api/status", None),
                                   ("GET", "/api/events", None), ("GET", "/api/device/stream", None),
                                   ("POST", "/api/settings", '{"askBeforeActing": false}'),
                                   ("POST", "/api/workflows", "{}"), ("DELETE", "/api/runs/abc", None),
                                   ("GET", "/api/setup/../runs", None), ("POST", "/api/setup/build/../../runs", "{}")):
            status, payload = self.request(method, path, self.setup_token, body)
            self.assertEqual(status, 403, (method, path))
            self.assertIn(b"plan_required", payload, (method, path))
        self.runtime.start.assert_not_called()
        self.runtime.update_settings.assert_not_called()
        self.assertEqual(self.request("GET", f"/api/status?token={self.setup_token}", "wrong")[0], 401)

    def test_without_a_setup_token_configured_it_is_just_a_wrong_token(self):
        request = object.__new__(make_handler(self.runtime, TOKEN))
        request.headers = {"Authorization": f"Bearer {self.setup_token}"}
        request.command, request.path = "GET", "/api/setup"
        self.assertFalse(request.authorized())

    def test_serve_turns_the_setup_scope_on_only_for_the_mac_app(self):
        given = {}
        for name, session in (("app", "s" * 43), ("serve", None)):
            state = Path(tempfile.mkdtemp()) / "state" / "mobster.sqlite3"
            config = SimpleNamespace(port=0, wda_url=None, state_db=state)
            with patch.dict(os.environ, {"MOBSTER_API_TOKEN": TOKEN}), patch.object(serve_module, "Runtime"), \
                    patch.object(serve_module, "WorkflowScheduler"), patch.object(serve_module, "BoundedServer",
                                                                                  FakeServer), \
                    patch.object(serve_module, "make_handler",
                                 wraps=server.make_handler) as made, patch("builtins.print"):
                os.environ.pop(serve_module.SECRETS_ON_STDIN, None)
                os.environ.pop("MOBSTER_APP_SESSION", None)
                if session:
                    os.environ["MOBSTER_APP_SESSION"] = session
                serve_module.serve(config)
            given[name] = made.call_args.kwargs.get("setup_token")
        self.assertEqual(given, {"app": self.setup_token, "serve": None})


if __name__ == "__main__":
    unittest.main()

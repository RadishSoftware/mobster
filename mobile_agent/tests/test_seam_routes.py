"""Seam S3/S4: routes the tracks register, tried before the core's but only under their reserved prefixes; raw bodies
with their own caps; SSE; CORS; the core's error mapping; GET /api/events and the streams cap. Offline."""

import json
import os
import threading
import unittest

from mobile_agent import api_routes, harness_api, tracks
from mobile_agent.api_errors import APIError
from mobile_agent.api_routes import Response, Route
from mobile_agent.journal import JournalError
from mobile_agent.server import make_handler
from mobile_agent.tests.seam_support import isolate, request
from mobile_agent.tests.test_server_engine import Base


def ok(request):
    return Response.json({"params": request.params, "origin": request.origin, "body": request.body,
                          "query": request.query})


class RegistryTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_prefixes_are_enforced(self):
        api_routes.register(Route("threads", "GET", "/api/threads", ok))
        api_routes.register(Route("threads", "GET", "/api/threads/{id}", ok))
        api_routes.register(Route("harness", "POST", "/api/runs/{id}/messages", ok))
        api_routes.register(Route("wireless", "POST", "/api/devices/{device}/wifi", ok))
        api_routes.register(Route("files", "POST", "/api/attachments", ok, body="raw", max_bytes=26_214_400))
        for route in (Route("memory", "GET", "/api/threads", ok),          # another track's prefix
                      Route("harness", "GET", "/api/runs/{id}", ok),       # the core's run route
                      Route("harness", "POST", "/api/runs/{id}/stop", ok),
                      Route("threads", "GET", "/api/threadsx", ok),        # not a path segment of the prefix
                      Route("threads", "GET", "/api/status", ok),
                      Route("nobody", "GET", "/api/threads", ok),
                      Route("threads", "PUT", "/api/threads/x", ok),
                      Route("threads", "GET", "/api/threads/{id}", ok),    # a duplicate
                      Route("threads", "POST", "/api/threads/new", ok, body="json", max_bytes=100_000)):
            with self.subTest(route=(route.owner, route.method, route.pattern)), self.assertRaises(ValueError):
                api_routes.register(route)
        found = api_routes.match("GET", "/api/threads/3f9c2a1b7d0e")
        self.assertEqual(found[1], {"id": "3f9c2a1b7d0e"})
        self.assertIsNone(api_routes.match("GET", "/api/threads/NOT-AN-ID"))
        self.assertIsNone(api_routes.match("POST", "/api/threads"))
        self.assertEqual(api_routes.match("POST", "/api/devices/Sam's%20iPhone/wifi")[1], {"device": "Sam's%20iPhone"})


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        isolate(self)
        tracks._loaded = True  # the core alone: these tests register their own stand-ins under the tracks' prefixes
        self.app = self.runtime()
        api_routes._routes.clear()  # the tracks' own routes (registered by the runtime) make way for the fakes here
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
        self.assertEqual(status, 400)  # the internal origins never come from a header

    def test_json_and_raw_bodies_and_their_caps(self):
        api_routes.register(Route("memory", "POST", "/api/memory", ok, max_bytes=200))
        api_routes.register(Route("files", "POST", "/api/attachments", ok, body="raw", max_bytes=10))
        api_routes.register(Route("memory", "POST", "/api/memory/clear", ok, body="none"))
        status, data, _ = self.call("POST", "/api/memory", {"text": "Kate Bell is my sister"})
        self.assertEqual((status, data["body"]), (200, {"text": "Kate Bell is my sister"}))
        status, _, _ = self.call("POST", "/api/memory", {"text": "x" * 300})
        self.assertEqual(status, 413)
        status, _, _ = self.call("POST", "/api/memory", raw=b"text", content_type="text/plain")
        self.assertEqual(status, 415)
        received = []
        api_routes._routes[1] = Route("files", "POST", "/api/attachments",
                                      lambda r: received.append(r.body) or Response.json({"ok": True}),
                                      body="raw", max_bytes=10)
        status, _, _ = self.call("POST", "/api/attachments", raw=b"%PDF-1.7", content_type="application/pdf")
        self.assertEqual((status, received), (200, [b"%PDF-1.7"]))
        status, data, _ = self.call("POST", "/api/attachments", raw=b"x" * 11, content_type="application/pdf")
        self.assertEqual((status, data["code"]), (413, "too_large"))
        self.assertEqual(self.call("POST", "/api/memory/clear")[0], 200)
        self.assertEqual(self.call("POST", "/api/memory/clear", {"a": 1})[0], 400)

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

    def test_cors_allows_the_new_headers(self):
        status, _, headers = self.call("OPTIONS", "/api/runs", headers={"Origin": "tauri://localhost"})
        self.assertEqual(status, 204)
        allowed = headers["Access-Control-Allow-Headers"]
        for name in ("X-Mobster-Origin", "X-Mobster-App-Session", "Last-Event-ID", "Idempotency-Key"):
            self.assertIn(name, allowed)

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


if __name__ == "__main__":
    unittest.main()

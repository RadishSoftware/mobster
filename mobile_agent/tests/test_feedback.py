import base64
import json
import os
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import api_routes
from mobile_agent.api_errors import APIError
from mobile_agent.feedback import bundle, outbox, routes

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
TOKEN = "eyJhbGciOiJFUzI1NiJ9.test.signature"
PLANTED = {
    "anthropic": "sk-ant-api03-" + "Q" * 40,
    "openai": "sk-proj-" + "R" * 40,
    "jwt": ".".join(("eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiJwbGFudGVkIn0", "c2lnbmF0dXJlcGxhbnRlZA")),
    "supabase": "sb_secret_" + "Z" * 30,
    "card": "4242 4242 4242 4242",
    "password": "-".join(("hunter2", "planted")),
    "otp": "551203",
}
WEBHOOK = "https://" + "discord.com" + "/api/" + "webhooks/100000000000000003/" + "w" * 68


class Service(BaseHTTPRequestHandler):
    seen = []
    answers = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Service.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "raw": body.decode()})
        status, value = Service.answers.pop(0) if Service.answers else (201, None)
        if status == "drop":
            self.connection.close()
            return
        report = json.loads(body)
        value = value or {"feedback": {"reference": "FB-AAAA-" + report["clientId"][:4].upper().replace("A", "B"),
                                       "kind": report["kind"], "createdAt": "2026-10-14T00:00:00Z", "forwarded": True}}
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class FakeRun:
    def __init__(self, run_id, events, created=1.0, device="dev1"):
        self.id, self.events, self.created_at, self.device = run_id, events, created, device

    def public(self, include_events=True):
        return {"id": self.id, "status": "failed", "goal": "Text Sam the plan", "appName": "Messages",
                "engine": "smart",
                "createdAt": self.created_at * 1000, "finishedAt": None, "device": self.device,
                "events": list(self.events) if include_events else [], "eventCount": len(self.events)}


class Fleet:
    def public(self):
        return [{"id": "dev1", "kind": "usb", "model": "iPhone16,1", "modelName": "iPhone 15 Pro", "ios": "26.0",
                 "default": True, "state": "ready", "udid": "00008110-00000000ABCD0005"}]


def runtime(directory=None, runs=()):
    return SimpleNamespace(config=SimpleNamespace(data_dir=str(directory) if directory else None),
                           runs={run.id: run for run in runs}, lock=threading.Lock(), fleet=Fleet(), local_api={})


def request(rt, method, path, body=None, params=None):
    raw = json.dumps(body).encode() if body is not None and method == "POST" and path == "/api/feedback" else body
    return api_routes.Request(runtime=rt, method=method, path=path, params=params or {}, query={}, headers={},
                              body=raw, content_type="application/json", origin="app", app_session=True)


def report(**over):
    value = {"draftId": str(uuid.uuid4()), "kind": "bug", "title": "Send stays grey",
             "description": "It never lights up.",
             "app": {"version": "0.4.7", "macos": "macOS 26.1", "connection": "USB", "phone": "iPhone 15 Pro",
                     "ios": "iOS 26.0"}, "accessToken": TOKEN}
    value.update(over)
    return value


class ServiceCase(unittest.TestCase):
    def setUp(self):
        Service.seen, Service.answers = [], []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        env = mock.patch.dict(os.environ, {"MOBSTER_ACCOUNT_API": f"http://127.0.0.1:{self.server.server_port}"})
        env.start()
        self.addCleanup(env.stop)
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        events = [{"type": "step", "message": f"Typed {PLANTED['anthropic']} into the field"},
                  {"type": "observe", "screenshot": "data:image/jpeg;base64," + "A" * 5000, "text": "Inbox"},
                  {"type": "note", "message": f"Authorization: Bearer {PLANTED['jwt']}",
                   "detail": {"key": PLANTED["supabase"]}},
                  {"type": "screen", "text": f"Your code is {PLANTED['otp']}. Card {PLANTED['card']}"}]
        self.rt = runtime(self.dir, [FakeRun("0123456789ab", events)])
        (self.dir / "logs").mkdir()
        (self.dir / "logs" / "agent.log").write_text(
            f"started\nwebhook {WEBHOOK} answered 204\nOPENAI_API_KEY={PLANTED['openai']}\n"
            f"sam@example.com signed in from {Path.home()}/Library\npassword: {PLANTED['password']}\n"
            "device 00008110-00000000ABCD0005 ready\n")


class BundleTests(ServiceCase):
    def test_the_bundle_masks_every_planted_secret_and_drops_pictures(self):
        built = bundle.build(self.rt, app_version="0.4.7")
        text = built["text"]
        for name, value in PLANTED.items():
            self.assertNotIn(value, text, name)
        self.assertNotIn(WEBHOOK, text)
        self.assertNotIn("sam@example.com", text)
        self.assertNotIn(str(Path.home()), text)
        self.assertNotIn("00008110-00000000ABCD0005", text)
        self.assertNotIn("base64", text)
        value = json.loads(text)
        self.assertEqual(value["phone"],
                         {"model": "iPhone 15 Pro", "ios": "26.0", "connection": "USB", "state": "ready"})
        self.assertEqual(value["run"]["id"], "0123456789ab")
        self.assertEqual(len(value["run"]["events"]), 4)
        self.assertEqual(value["app"]["version"], "0.4.7")
        self.assertIn("started", value["log"])
        self.assertEqual(built["summary"], {"events": 4, "logLines": 6, "runId": "0123456789ab",
                                            "phone": value["phone"], "macos": value["app"]["macos"]})

    def test_check_keeps_what_was_shown_and_is_idempotent(self):
        text = bundle.build(self.rt)["text"]
        self.assertEqual(bundle.check(text), text)
        with self.assertRaises(ValueError):
            bundle.check("[1, 2]")
        edited = json.loads(text)
        edited["note"] = f"key {PLANTED['anthropic']}"
        self.assertNotIn(PLANTED["anthropic"], bundle.check(json.dumps(edited)))

    def test_without_a_data_folder_no_log_is_read_and_a_task_that_is_gone_is_404(self):
        self.assertEqual(json.loads(bundle.build(runtime(None))["text"])["log"], [])
        with self.assertRaises(APIError) as caught:
            bundle.build(self.rt, run_id="fffffffffff0")
        self.assertEqual(caught.exception.status, 404)


class SendTests(ServiceCase):
    def send(self, body):
        return routes.send(request(self.rt, "POST", "/api/feedback", body))

    def test_a_planted_secret_never_reaches_the_payload(self):
        diagnostics = bundle.build(self.rt, app_version="0.4.7")["text"]
        answer = self.send(report(
            title=f"Key {PLANTED['anthropic']} shows",
            description=f"My password: {PLANTED['password']}, card {PLANTED['card']}, token {PLANTED['jwt']}, "
                        f"and the hook {WEBHOOK}. Write to me at sam@example.com",
            app={"version": f"0.4.7 {PLANTED['openai']}"},
            diagnostics=diagnostics, screenshot={"type": "image/png", "data": base64.b64encode(PNG).decode()}))
        self.assertEqual(answer.status, 201)
        self.assertGreaterEqual(answer.value["masked"], 5)
        self.assertEqual(len(Service.seen), 1)
        raw = Service.seen[0]["raw"]
        for name, value in PLANTED.items():
            self.assertNotIn(value, raw, name)
        self.assertNotIn(WEBHOOK, raw)
        sent = json.loads(raw)
        self.assertIn("Write to me at sam@example.com", sent["description"])
        self.assertNotIn("accessToken", sent)
        self.assertNotIn(TOKEN, raw)
        self.assertEqual(Service.seen[0]["auth"], f"Bearer {TOKEN}")
        self.assertEqual(sent["diagnostics"], diagnostics)

    def test_sent_at_once_answers_the_reference(self):
        answer = self.send(report())
        self.assertEqual((answer.status, answer.value["queued"]), (201, False))
        self.assertTrue(answer.value["feedback"]["reference"].startswith("FB-"))
        self.assertEqual(outbox.outbox_for(self.rt).items(), [])

    def test_offline_it_waits_in_the_outbox_then_goes(self):
        Service.answers = [("drop", None)]
        body = report()
        answer = self.send(dict(body))
        self.assertEqual((answer.status, answer.value["queued"]), (202, True))
        waiting = routes.waiting(request(self.rt, "GET", "/api/feedback/outbox")).value["items"]
        self.assertEqual([item["draftId"] for item in waiting], [body["draftId"]])
        self.assertNotIn("description", waiting[0])
        stored = next((self.dir / "feedback" / "outbox").glob("*.json"))
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(TOKEN, stored.read_text())
        flushed = routes.flush(request(self.rt, "POST", "/api/feedback/outbox/flush", {"accessToken": TOKEN})).value
        self.assertEqual([item["draftId"] for item in flushed["sent"]], [body["draftId"]])
        self.assertEqual(flushed["waiting"], 0)
        self.assertEqual({json.loads(s["raw"])["clientId"] for s in Service.seen}, {body["draftId"]})

    def test_a_service_not_set_up_yet_keeps_it_waiting(self):
        Service.answers = [(503, {"error": "not_configured", "message": "x"}), (404, {"error": "NOT_FOUND"})]
        self.assertTrue(self.send(report()).value["queued"])
        result = routes.flush(request(self.rt, "POST", "/api/feedback/outbox/flush", {"accessToken": TOKEN})).value
        self.assertEqual((result["sent"], result["waiting"]), ([], 1))
        self.assertIn("error", result)

    def test_a_refusal_sending_again_wont_fix_is_said_and_kept_nowhere(self):
        Service.answers = [(400, {"error": "invalid_title", "message": "Add a title of 1 to 120 characters."})]
        with self.assertRaises(APIError) as caught:
            self.send(report())
        self.assertEqual(caught.exception.status, 400)
        Service.answers = [(401, {"error": "unauthorized", "message": "Sign in again."})]
        with self.assertRaises(APIError) as caught:
            self.send(report())
        self.assertEqual(caught.exception.code, "session")
        self.assertEqual(outbox.outbox_for(self.rt).items(), [])

    def test_bad_reports_are_refused_before_anything_is_sent(self):
        for over in ({"kind": "praise"}, {"title": " "}, {"draftId": "draft-1"}, {"email": "nope"}, {"runId": "../x"},
                     {"screenshot": {"type": "image/png", "data": base64.b64encode(b"GIF89a....").decode()}},
                     {"diagnostics": "not json"}, {"shell": "zsh"}, {"app": {"version": 4}}):
            with self.assertRaises((ValueError, APIError), msg=str(over)):
                self.send(report(**over))
        with self.assertRaises(APIError) as caught:
            self.send(report(accessToken=None))
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(Service.seen, [])

    def test_only_the_mac_app_sends(self):
        rt = runtime(self.dir)
        rt.local_api = {"app_session": "s"}
        req = request(rt, "POST", "/api/feedback", report())
        req.app_session = False
        with self.assertRaises(APIError) as caught:
            routes.send(req)
        self.assertEqual(caught.exception.status, 403)

    def test_the_outbox_holds_at_most_twenty(self):
        box = outbox.Outbox(None)
        for _ in range(outbox.MAX_ITEMS):
            box.put({"clientId": str(uuid.uuid4()), "kind": "bug", "title": "t"}, "unreachable")
        with self.assertRaises(APIError):
            box.put({"clientId": str(uuid.uuid4()), "kind": "bug", "title": "t"}, "unreachable")
        first = box.items()[0]["draftId"]
        self.assertTrue(box.remove(first))
        self.assertFalse(box.remove(first))


class RouteTests(unittest.TestCase):
    def test_the_routes_are_registered_under_feedback(self):
        saved = list(api_routes._routes)
        self.addCleanup(lambda: (api_routes._routes.clear(), api_routes._routes.extend(saved)))
        api_routes._routes[:] = [r for r in api_routes._routes if r.owner != "feedback"]
        routes.register_routes()
        draft = str(uuid.uuid4())
        for method, path in (("POST", "/api/feedback"), ("POST", "/api/feedback/diagnostics"),
                             ("GET", "/api/feedback/outbox"),
                             ("POST", "/api/feedback/outbox/flush"), ("DELETE", f"/api/feedback/outbox/{draft}")):
            found = api_routes.match(method, path)
            self.assertIsNotNone(found, path)
        self.assertEqual(api_routes.match("POST", "/api/feedback")[0].max_bytes, routes.MAX_BODY)


if __name__ == "__main__":
    unittest.main()

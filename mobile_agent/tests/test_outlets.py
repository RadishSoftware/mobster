import base64
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import journal, secret_filter, server
from mobile_agent.agent_hooks import MASK
from mobile_agent.api_errors import APIError
from mobile_agent.connectors import (
    client as mcp,
)
from mobile_agent.connectors import (
    oauth,
    shared,
)
from mobile_agent.connectors import (
    service as connector_service,
)
from mobile_agent.connectors import (
    store as connector_store,
)
from mobile_agent.connectors import (
    vault as connector_vault,
)
from mobile_agent.outlets import (
    folder as outlet_folder,
)
from mobile_agent.outlets import (
    kinds as outlet_kinds,
)
from mobile_agent.outlets import (
    mail as outlet_mail,
)
from mobile_agent.outlets import (
    net,
    webhook,
)
from mobile_agent.outlets import (
    register as outlet_register,
)
from mobile_agent.outlets import (
    service as outlet_service,
)
from mobile_agent.outlets import (
    store as outlet_store,
)
from mobile_agent.outlets import (
    vault as outlet_vault,
)
from mobile_agent.tests import sota_world as world
from mobile_agent.tests.fake_keychain import FakeBridge
from mobile_agent.tests.seam_support import isolate
from mobile_agent.tests.sota_world import decide as ORIGINAL_DECIDE
from mobile_agent.tests.test_sota_e2e import Service

SECRET = "whsec-test-0123456789abcdef"
ASK = "What's the address of the dinner place Alex sent me in Messages?"
CODE_ASK = "What's my bank's sign-in code?"
CODE_ANSWER = "Your code is 551203 and the card on file is 4242 4242 4242 4242."


class Receiver:

    def __init__(self, testcase, plan=()):
        self.plan = list(plan)
        self.requests = []
        self.lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(size)
                with receiver.lock:
                    receiver.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                    step = receiver.plan.pop(0) if receiver.plan else 200
                if step == "drop":
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
                if step == "slow":
                    time.sleep(0.6)
                    step = 200
                self.send_response(step)
                if 300 <= step < 400:
                    self.send_header("Location", "https://elsewhere.example/hook")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        testcase.addCleanup(self.server.server_close)
        testcase.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hooks/catch/123/abcdef?key=topsecret"

    def bodies(self):
        with self.lock:
            return [json.loads(r["body"]) for r in self.requests]


def verify(secret, headers, body):
    parts = dict(item.split("=", 1) for item in headers["Mobster-Signature"].split(","))
    expected = hmac.new(secret.encode(), parts["t"].encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"]) and parts["t"] == headers["Mobster-Timestamp"]


def code_rule(t):
    if "sign-in code" in t.r:
        return world.done(CODE_ANSWER)
    return ORIGINAL_DECIDE(t)


class OutletsBase(Service):
    ATTEMPTS = 3

    def setUp(self):
        self.keys = Path(tempfile.mkdtemp(prefix="mobster-outlets-keys-")) / "items.json"
        self.addCleanup(shutil.rmtree, self.keys.parent, True)
        patches = [mock.patch.object(outlet_vault, "backend", lambda: outlet_vault.FileVault(self.keys)),
                   mock.patch.object(outlet_service, "BACKOFF", (0.05, 0.05, 0.05, 0.05)),
                   mock.patch.object(outlet_service, "ATTEMPTS", self.ATTEMPTS),
                   mock.patch.object(world, "decide", code_rule)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        original = outlet_service.OutletService.__init__

        def init(service, runtime, **kwargs):
            kwargs.setdefault("backoff", outlet_service.BACKOFF)
            kwargs.setdefault("attempts", outlet_service.ATTEMPTS)
            original(service, runtime, **kwargs)
        patcher = mock.patch.object(outlet_service.OutletService, "__init__", init)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def add(self, url, name="Zapier", secret=SECRET, screenshot=False):
        body = {"name": name, "url": url, **({"secret": secret} if secret else {}), "screenshot": screenshot}
        status, data = self.call("POST", "/api/outlets/destinations", body)
        self.assertEqual(status, 201, data)
        return data["destination"]

    def task(self, goal=ASK, **fields):
        status, data = self.call("POST", "/api/runs", {"goal": goal, "appId": "any", "mode": "live", **fields})
        self.assertIn(status, (200, 201), data)
        return data["run"]["id"]

    def deliveries(self, run_id):
        status, data = self.call("GET", f"/api/outlets/runs/{run_id}")
        self.assertEqual(status, 200, data)
        return data["deliveries"]

    def settled(self, run_id, count=1):
        def done():
            rows = self.deliveries(run_id)
            return rows if len(rows) >= count and all(r["status"] != "sending" for r in rows) else None
        return self.until(done, f"task {run_id}'s result to be sent")


class DeliveryTests(OutletsBase):
    def test_a_finished_task_posts_its_result_signed(self):
        receiver = Receiver(self)
        destination = self.add(receiver.url)
        self.assertEqual(destination["hint"], f"http://127.0.0.1:{receiver.server.server_address[1]}/hooks/…")
        self.assertTrue(destination["hasSecret"])
        run_id = self.task(sendTo="zapier")
        run = self.finished(run_id)
        self.assertEqual(run["extras"]["sendTo"], {"destination": destination["id"]})
        [delivery] = self.settled(run_id)
        self.assertEqual((delivery["status"], delivery["attempts"], delivery["httpStatus"]), ("sent", 1, 200))
        self.assertEqual(delivery["destinationName"], "Zapier")
        [request] = receiver.requests
        self.assertEqual(request["path"], "/hooks/catch/123/abcdef?key=topsecret")
        headers = request["headers"]
        self.assertTrue(verify(SECRET, headers, request["body"]))
        self.assertFalse(verify("another-secret-123", headers, request["body"]))
        self.assertEqual((headers["Mobster-Event"], headers["Mobster-Delivery"]), ("run.finished", delivery["id"]))
        self.assertEqual(headers["Content-Type"], "application/json")
        body = json.loads(request["body"])
        self.assertEqual(set(body), {"type", "runId", "status", "answer", "data", "reason", "startedAt", "finishedAt",
                                     "workflow", "text"})
        self.assertEqual((body["type"], body["runId"], body["status"]), ("run.finished", run_id, run["status"]))
        self.assertIn("214 Pine St", body["answer"])
        self.assertIsNone(body["workflow"])
        self.assertTrue(body["finishedAt"].endswith("Z"))
        self.assertTrue(body["text"].startswith("Mobster: "))
        for path in ("/api/outlets", f"/api/runs/{run_id}", f"/api/outlets/runs/{run_id}"):
            text = json.dumps(self.call("GET", path)[1])
            self.assertNotIn("topsecret", text)
            self.assertNotIn(SECRET, text)
        self.runtime.journal.connection.execute("PRAGMA wal_checkpoint(FULL)")
        for file in self.state_db.parent.glob("mobster.sqlite3*"):
            data = file.read_bytes()
            self.assertNotIn(b"topsecret", data, file.name)
            self.assertNotIn(SECRET.encode(), data, file.name)
        self.assertEqual(os.stat(self.keys).st_mode & 0o777, 0o600)

    def test_a_finished_result_is_sent_on_a_click_and_refused_when_nothing_matches(self):
        receiver = Receiver(self)
        self.add(receiver.url, name="Zapier")
        run_id = self.task()
        self.finished(run_id)
        status, data = self.call("POST", f"/api/runs/{run_id}/send", {"destination": "nowhere"})
        self.assertEqual((status, data["code"]), (404, "destination_not_found"))
        self.assertEqual(receiver.requests, [])
        status, data = self.call("POST", f"/api/runs/{run_id}/send", {"destination": "zapier"})
        self.assertEqual((status, data["delivery"]["destinationName"]), (201, "Zapier"))
        [delivery] = self.settled(run_id)
        self.assertEqual((delivery["status"], len(receiver.requests)), ("sent", 1))
        status, data = self.call("POST", f"/api/runs/{run_id}/issue", {})
        self.assertEqual((status, data["code"]), (409, "no_tracker"))

    def test_without_a_secret_nothing_is_signed(self):
        receiver = Receiver(self)
        self.add(receiver.url, name="n8n", secret=None)
        run_id = self.task(sendTo="n8n")
        self.settled(run_id)
        headers = receiver.requests[0]["headers"]
        self.assertNotIn("Mobster-Signature", headers)
        self.assertIn("Mobster-Timestamp", headers)

    def test_a_5xx_or_a_dropped_connection_is_tried_again(self):
        receiver = Receiver(self, plan=[503, "drop"])
        self.add(receiver.url)
        run_id = self.task(sendTo="Zapier")
        [delivery] = self.settled(run_id)
        self.assertEqual((delivery["status"], delivery["attempts"]), ("sent", 3))
        ids = {r["headers"]["Mobster-Delivery"] for r in receiver.requests}
        self.assertEqual(ids, {delivery["id"]}, "a retry is the same delivery")
        self.assertEqual(len({r["body"] for r in receiver.requests}), 1)

    def test_it_gives_up_after_its_tries_and_the_task_still_finished(self):
        receiver = Receiver(self, plan=[500] * 10)
        self.add(receiver.url)
        run_id = self.task(sendTo="Zapier")
        run = self.finished(run_id)
        [delivery] = self.settled(run_id)
        self.assertEqual((delivery["status"], delivery["attempts"], delivery["httpStatus"]), ("failed", 3, 500))
        self.assertEqual(delivery["error"], "The address answered 500. Tried 3 times.")
        self.assertEqual(len(receiver.requests), 3)
        after = self.run_of(run_id)
        self.assertEqual((after["status"], after["summary"]), (run["status"], run["summary"]))
        self.assertEqual(after["summary"]["outcome"], "done")
        receiver.plan = []
        status, data = self.call("POST", f"/api/outlets/deliveries/{delivery['id']}/resend", None)
        self.assertEqual(status, 201, data)
        rows = self.settled(run_id, count=2)
        self.assertEqual([r["status"] for r in rows], ["failed", "sent"])
        self.assertNotEqual(rows[1]["id"], delivery["id"])
        self.assertEqual(receiver.bodies()[-1]["answer"], receiver.bodies()[0]["answer"])

    def test_a_refusal_or_a_redirect_is_not_tried_again(self):
        for answer, words in ((404, "The address answered 404. Check it in Settings › Connectors."),
                              (301, "The address answered 301, a redirect. Use the address it points to.")):
            with self.subTest(answer=answer):
                receiver = Receiver(self, plan=[answer])
                destination = self.add(receiver.url, name=f"Hook {answer}")
                run_id = self.task(sendTo=destination["id"])
                [delivery] = self.settled(run_id)
                self.assertEqual((delivery["status"], delivery["attempts"], delivery["error"]), ("failed", 1, words))
                self.assertEqual(len(receiver.requests), 1)

    def test_what_the_engine_masks_stays_masked(self):
        receiver = Receiver(self)
        self.add(receiver.url)
        run_id = self.task(CODE_ASK, sendTo="Zapier")
        self.settled(run_id)
        body = receiver.bodies()[0]
        self.assertNotIn("551203", json.dumps(body))
        self.assertNotIn("4242 4242 4242 4242", json.dumps(body))
        self.assertIn(MASK, body["answer"])
        self.assertIn(MASK, body["text"])

    def test_a_screenshot_goes_only_when_the_destination_opts_in(self):
        receiver = Receiver(self)
        self.add(receiver.url, name="Plain")
        self.add(receiver.url, name="With screen", screenshot=True)
        plain = self.task(sendTo="Plain")
        self.settled(plain)
        self.assertNotIn("screenshot", receiver.bodies()[0])
        shown = self.task(sendTo="With screen")
        self.settled(shown)
        screenshot = receiver.bodies()[1]["screenshot"]
        self.assertEqual(screenshot["contentType"], "image/jpeg")
        self.assertTrue(base64.b64decode(screenshot["base64"]).startswith(b"\xff\xd8"))

    def test_a_conversation_message_names_a_destination(self):
        receiver = Receiver(self)
        self.add(receiver.url)
        thread, run_id = self.start(ASK, sendTo="Zapier")
        self.finished(run_id)
        self.settled(run_id)
        self.assertIn("214 Pine St", receiver.bodies()[0]["answer"])
        _, second = self.start("What time does Luca close?", thread_id=thread)
        self.finished(second)
        time.sleep(0.2)
        self.assertEqual(self.deliveries(second), [])
        self.assertEqual(len(receiver.requests), 1)

    def test_a_finished_task_lands_in_the_folder(self):
        home = Path(tempfile.mkdtemp(prefix="mobster-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / "Results").mkdir()
        with mock.patch.object(outlet_folder, "home", lambda: home):
            status, data = self.call("POST", "/api/outlets/destinations",
                                     {"name": "Results", "kind": "folder", "folder": "~/Results",
                                      "file": "answers.csv"})
            self.assertEqual(status, 201, data)
            run_id = self.task(sendTo="Results")
            [delivery] = self.settled(run_id)
            self.assertEqual(delivery["status"], "sent")
            lines = (home / "Results" / "answers.csv").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("214 Pine St", lines[1])
            self.assertIn(run_id, lines[1])

    def test_an_unknown_destination_is_refused_and_nothing_starts(self):
        receiver = Receiver(self)
        self.add(receiver.url)
        status, data = self.call("POST", "/api/runs", {"goal": ASK, "appId": "any", "mode": "live", "sendTo": "Make"})
        self.assertEqual(status, 400, data)
        self.assertEqual(data["error"], "No destination is called Make. You have: Zapier.")
        self.assertEqual(self.runtime.runs, {})


class WorkflowTests(OutletsBase):
    def save(self, **fields):
        status, data = self.call("POST", "/api/workflows", {"name": "Dinner digest", "appId": None, "engine": "smart",
                                                            "goal": "What's the address of the dinner place {person: "
                                                                    "Alex} sent me in Messages?", **fields})
        self.assertEqual(status, 201, data)
        return data["workflow"]

    def test_a_workflow_sends_with_its_name_and_blanks(self):
        receiver = Receiver(self)
        destination = self.add(receiver.url)
        workflow = self.save(sendTo="Zapier")
        self.assertEqual(workflow["sendTo"], destination["id"])
        status, data = self.call("POST", f"/api/workflows/{workflow['id']}/run", {"values": {"person": "Alex"}})
        self.assertIn(status, (200, 201), data)
        run_id = data["run"]["id"]
        self.finished(run_id)
        self.settled(run_id)
        body = receiver.bodies()[0]
        self.assertEqual(body["workflow"],
                         {"id": workflow["id"], "name": "Dinner digest", "blanks": {"person": "Alex"}})
        self.assertTrue(body["text"].startswith("Mobster › Dinner digest: "))

    def test_a_schedule_fills_its_blanks_with_their_defaults(self):
        receiver = Receiver(self)
        self.add(receiver.url)
        workflow = self.save(sendTo="Zapier")
        record = self.runtime.workflows._get(workflow["id"])
        extras = self.runtime.workflows._send_extras(record)
        self.assertEqual(extras["workflow"]["blanks"], {"person": "Alex"})

    def test_removing_a_destination_stops_its_workflows_and_the_run_still_runs(self):
        receiver = Receiver(self)
        destination = self.add(receiver.url)
        workflow = self.save(sendTo="Zapier")
        status, data = self.call("GET", "/api/outlets")
        self.assertEqual(data["destinations"][0]["workflows"], 1)
        status, data = self.call("DELETE", f"/api/outlets/destinations/{destination['id']}")
        self.assertEqual(status, 200, data)
        self.assertNotIn("sendTo", self.runtime.workflows.get(workflow["id"]))
        self.assertIsNone(outlet_vault.FileVault(self.keys).get(destination["id"]))
        status, data = self.call("POST", f"/api/workflows/{workflow['id']}/run", {})
        self.assertIn(status, (200, 201), data)
        self.assertEqual(self.finished(data["run"]["id"])["summary"]["outcome"], "done")
        self.assertEqual(receiver.requests, [])

    def test_a_workflow_can_drop_its_destination(self):
        receiver = Receiver(self)
        self.add(receiver.url)
        workflow = self.save(sendTo="Zapier")
        status, data = self.call("POST", f"/api/workflows/{workflow['id']}",
                                 {"sendTo": None, "revision": workflow["revision"]})
        self.assertEqual(status, 200, data)
        self.assertNotIn("sendTo", data["workflow"])
        status, data = self.call("POST", "/api/workflows", {"name": "X", "appId": None, "engine": "smart",
                                                            "goal": "Hi", "sendTo": "Nowhere"})
        self.assertEqual(status, 400, data)


class DestinationTests(OutletsBase):
    def test_saving_testing_and_changing_a_destination(self):
        receiver = Receiver(self)
        destination = self.add(receiver.url)
        status, data = self.call("POST", f"/api/outlets/destinations/{destination['id']}/test", None)
        self.assertEqual(status, 200, data)
        self.assertEqual((data["ok"], data["status"]), (True, 200))
        self.assertEqual(data["destination"]["lastTest"]["ok"], True)
        request = receiver.requests[0]
        self.assertEqual(request["headers"]["Mobster-Event"], "test")
        self.assertTrue(verify(SECRET, request["headers"], request["body"]))
        self.assertEqual(json.loads(request["body"])["type"], "test")
        status, data = self.call("POST", f"/api/outlets/destinations/{destination['id']}",
                                 {"secret": "a-new-secret-42"})
        self.assertEqual(status, 200, data)
        self.call("POST", f"/api/outlets/destinations/{destination['id']}/test", None)
        self.assertTrue(verify("a-new-secret-42", receiver.requests[1]["headers"], receiver.requests[1]["body"]))
        status, data = self.call("POST", f"/api/outlets/destinations/{destination['id']}", {"secret": None,
                                                                                            "name": "Zap"})
        self.assertEqual((data["destination"]["hasSecret"], data["destination"]["name"]), (False, "Zap"))
        self.call("POST", f"/api/outlets/destinations/{destination['id']}/test", None)
        self.assertNotIn("Mobster-Signature", receiver.requests[2]["headers"])
        receiver.plan = [410]
        status, data = self.call("POST", f"/api/outlets/destinations/{destination['id']}/test", None)
        self.assertEqual((data["ok"], data["message"]), (False, "The address answered 410. Check it in Settings › "
                                                                "Connectors."))
        self.assertFalse(data["destination"]["lastTest"]["ok"])

    def test_names_are_unique_and_addresses_checked(self):
        receiver = Receiver(self)
        self.add(receiver.url)
        for body, words in (({"name": "zapier", "url": receiver.url}, "You already have a destination called zapier"),
                            ({"name": "X", "url": "http://example.com/hook"},
                             "Use https:// for an address on the internet"),
                            ({"name": "X", "url": "https://user:pw@example.com/hook"}, "Put the secret in Secret"),
                            ({"name": "X", "url": "ftp://example.com"}, "Use an address that starts with https://"),
                            ({"name": "X", "url": "https://example.com", "secret": "short"}, "A secret is 8 to 200"),
                            ({"name": "", "url": "https://example.com"}, "Name the destination"),
                            ({"name": "X", "url": "https://example.com", "extra": 1}, "A destination has a name")):
            with self.subTest(body=body):
                status, data = self.call("POST", "/api/outlets/destinations", body)
                self.assertEqual(status, 400, data)
                self.assertIn(words, data["error"])
                self.assertNotIn("pw@", data["error"])

    def test_only_the_mac_app_saves_destinations(self):
        self.runtime.local_api = {"app_session": True}
        httpd = server.BoundedServer(("127.0.0.1", 0),
                                     server.make_handler(self.runtime, "e" * 43, app_session="s" * 43))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.url = f"http://127.0.0.1:{httpd.server_address[1]}"
        body = {"name": "Zapier", "url": "https://hooks.zapier.com/hooks/catch/1/abc"}
        status, data = self.call("POST", "/api/outlets/destinations", body)
        self.assertEqual((status, data["code"]), (403, "app_only"))
        status, data = self.call("GET", "/api/outlets")
        self.assertEqual((status, data["destinations"]), (200, []))


class PickupTests(unittest.TestCase):

    def test_a_delivery_left_sending_is_sent_at_the_next_launch(self):
        isolate(self)
        journal.register_schema(outlet_store.SCHEMA, outlet_store.VERSION, outlet_store.migrate)
        folder = Path(tempfile.mkdtemp(prefix="mobster-outlets-"))
        self.addCleanup(shutil.rmtree, folder, True)
        book = journal.Journal(folder / "j.sqlite3")
        self.addCleanup(book.close)
        run = SimpleNamespace(id="a1b2c3d4e5f6", status="completed", created_at=1.0, finished_at=2.0, extras={},
                              summary={"status": "completed", "answer": "Done.", "outcome": "done"}, events=[])
        runtime = SimpleNamespace(journal=book, lock=threading.RLock(), runs={run.id: run})
        sent = []

        def post(url, data, headers):
            sent.append(json.loads(data))
            return webhook.Outcome(True, False, 200)
        vault = outlet_vault.FileVault(folder / "keys.json")
        first = outlet_service.OutletService(runtime, vault=vault,
                                             post=lambda *a: webhook.Outcome(False, True, 503, "x"),
                                             backoff=(60,), attempts=5)
        row = first.store.add_destination({"id": "0123456789ab", "name": "Make", "hint": "https://hook.make.com",
                                           "has_secret": False, "screenshot": False, "created_at": 1, "updated_at": 1})
        vault.put(row["id"], "https://hook.us1.make.com/abc", None)
        delivery = first.enqueue(run.id, row["id"])
        deadline = time.monotonic() + 5
        while first.store.delivery(delivery["id"])["attempts"] < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        first.close()
        self.assertEqual(first.store.delivery(delivery["id"])["status"], "sending")
        second = outlet_service.OutletService(runtime, vault=vault, post=post, backoff=(60,), attempts=5)
        second.start()
        deadline = time.monotonic() + 5
        while second.store.delivery(delivery["id"])["status"] == "sending" and time.monotonic() < deadline:
            time.sleep(0.02)
        second.close()
        row = second.store.delivery(delivery["id"])
        self.assertEqual((row["status"], row["attempts"]), ("sent", 2))
        self.assertEqual(sent[0]["runId"], run.id)


class PieceTests(unittest.TestCase):
    def test_addresses(self):
        for url in ("https://hooks.zapier.com/hooks/catch/1/abc/", "https://hook.us1.make.com/x",
                    "https://hooks.slack.com/services/T0/B0/xyz", "https://n8n.example.org:5678/webhook/abc",
                    "http://localhost:5678/webhook/abc", "http://127.0.0.1:8080/x", "http://192.168.1.20:5678/x",
                    "http://homeserver:5678/x", "http://n8n.local/x", "http://[::1]:5678/x"):
            with self.subTest(url=url):
                self.assertEqual(webhook.validate_url(f"  {url} "), url)
        for url in ("", "hooks.zapier.com/x", "http://example.com/x", "http://8.8.8.8/x", "ftp://example.com",
                    "https://user:pass@example.com", "https://exa mple.com", "https://example.com:99999/x",
                    "https://" + "a" * 2000 + ".com", "javascript:alert(1)"):
            with self.subTest(url=url):
                with self.assertRaises(webhook.AddressError):
                    webhook.validate_url(url)

    def test_the_hint_shows_the_host_and_hides_the_key(self):
        self.assertEqual(webhook.hint("https://hooks.zapier.com/hooks/catch/123/abcdef/"),
                         "https://hooks.zapier.com/hooks/…")
        self.assertEqual(webhook.hint("https://hooks.slack.com/services/T0/B0/xyz"),
                         "https://hooks.slack.com/services/…")
        self.assertEqual(webhook.hint("https://example.com"), "https://example.com")
        self.assertEqual(webhook.hint("https://example.com/?token=abc"), "https://example.com/…")
        self.assertEqual(webhook.hint("https://example.com/abc123secret?x=1"), "https://example.com/abc123secret/…")
        self.assertEqual(webhook.hint("http://[::1]:5678/w"), "http://[::1]:5678/w")

    def test_the_body_is_the_result_and_nothing_else(self):
        data = {"balance": "$1,204.50", "code": "Your code is 551203", "items": [{"card": "4242 4242 4242 4242"}]}
        run = SimpleNamespace(id="a1b2c3d4e5f6", status="completed", created_at=1760000000.0, finished_at=1760000042.5,
                              image="data:image/png;base64,AAAA", events=[{"frameId": "f0123456789"}],
                              goal="secret goal",
                              summary={"status": "completed", "answer": "Balance is $1,204.50.",
                                       "full_answer": "Balance is $1,204.50. password: hunter2!", "data": data,
                                       "data_status": "validated", "proof": [{"quote": "x"}], "costUsd": 0.02})
        body = webhook.payload(run, {"id": "0123456789ab", "name": "Bank check", "blanks": {"bank": "Chase"}})
        self.assertEqual(body["answer"], f"Balance is $1,204.50. password: {MASK}")
        self.assertEqual(body["data"], {"balance": "$1,204.50", "code": f"Your code is {MASK}",
                                        "items": [{"card": MASK}]})
        self.assertEqual((body["startedAt"], body["finishedAt"]), ("2025-10-09T08:53:20Z", "2025-10-09T08:54:02Z"))
        self.assertEqual(body["workflow"], {"id": "0123456789ab", "name": "Bank check", "blanks": {"bank": "Chase"}})
        self.assertIsNone(body["reason"])
        self.assertNotIn("screenshot", body)
        text = json.dumps(body)
        for absent in ("secret goal", "costUsd", "proof", "f0123456789", "AAAA"):
            self.assertNotIn(absent, text)
        run.summary = {"status": "blocked", "answer": None, "reason": "The app asked for a sign-in.", "data": None}
        run.status = "blocked"
        body = webhook.payload(run)
        self.assertEqual((body["data"], body["reason"]), (None, "The app asked for a sign-in."))
        self.assertEqual(body["text"], "Mobster: Couldn't finish. The app asked for a sign-in.")
        run.summary = {"status": "blocked", "answer": "Which Sam?", "reason": "It asked which Sam.",
                       "data": "Which Sam?",
                       "data_status": "observed"}
        body = webhook.payload(run)
        self.assertEqual((body["answer"], body["data"], body["text"]), ("Which Sam?", None, "Mobster: Which Sam?"))
        self.assertEqual(secret_filter.redact(body["answer"]), body["answer"])

    def test_slack_reads_text_with_its_own_escaping(self):
        body = {"text": "Mobster: Tom & Jerry <3"}
        slack = json.loads(webhook.encode_body("https://hooks.slack.com/services/T/B/x", body))
        self.assertEqual(slack["text"], "Mobster: Tom &amp; Jerry &lt;3")
        other = json.loads(webhook.encode_body("https://hooks.zapier.com/hooks/catch/1/2", body))
        self.assertEqual(other["text"], body["text"])

    def test_the_signature_is_the_documented_one(self):
        data = b'{"type":"test"}'
        headers = webhook.headers("test", "0123456789ab", data, "s3cret-value", timestamp=1760000000)
        self.assertEqual(headers["Mobster-Signature"],
                         "t=1760000000,v1="
                         + hmac.new(b"s3cret-value", b"1760000000." + data, hashlib.sha256).hexdigest())
        self.assertTrue(verify("s3cret-value", headers, data))

    def test_which_answers_are_tried_again(self):
        for status, retry in ((200, None), (204, None), (301, False), (400, False), (401, False), (404, False),
                              (408, True), (429, True), (500, True), (502, True), (503, True)):
            with self.subTest(status=status):
                outcome = webhook.judge(status)
                self.assertEqual(outcome.ok, retry is None)
                if retry is not None:
                    self.assertEqual(outcome.retry, retry)

    def test_the_keychain_never_sees_the_secret_in_argv(self):
        bridge = FakeBridge()
        vault = outlet_vault.KeychainVault(bridge.runner, bridge=bridge)
        vault.put("0123456789ab", "https://hooks.zapier.com/hooks/catch/1/abc", "my-signing-secret")
        self.assertEqual(vault.get("0123456789ab"), {"url": "https://hooks.zapier.com/hooks/catch/1/abc",
                                                     "secret": "my-signing-secret"})
        self.assertEqual(bridge.cli, [], "written and read through the Security framework, never a command line")
        self.assertTrue(bridge.items[(outlet_vault.SERVICE, "destination:0123456789ab")]["marked"])
        with self.assertRaises(ValueError):
            vault.put("../../etc", "https://x.com", None)

    def test_the_run_field_takes_a_name_an_id_or_a_workflow(self):
        service = SimpleNamespace(find=lambda wanted: {"id": "0123456789ab"} if wanted.lower() in (
            "zapier", "0123456789ab") else (_ for _ in ()).throw(ValueError("No destination is called " + wanted)))
        with mock.patch.object(outlet_service, "service_for", lambda runtime: service):
            self.assertEqual(outlet_register.validate_send_to("Zapier", None), {"destination": "0123456789ab"})
            flow = {"id": "abcdefabcdef", "name": " Daily  digest ", "blanks": {"topic": "basketball"}}
            self.assertEqual(outlet_register.validate_send_to({"destination": "zapier", "workflow": flow}, None),
                             {"destination": "0123456789ab", "workflow": {"id": "abcdefabcdef", "name": "Daily digest",
                                                                          "blanks": {"topic": "basketball"}}})
            for value in (None, 3, {"destination": "zapier", "url": "https://x"}, {"workflow": flow},
                          {"destination": "zapier", "workflow": {"name": "x", "blanks": {"a": 1}}}, "Make"):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        outlet_register.validate_send_to(value, None)
        with mock.patch.object(outlet_service, "service_for", lambda runtime: None):
            with self.assertRaises(ValueError):
                outlet_register.validate_send_to("Zapier", None)


class FakeHTTP:

    def __init__(self, routes):
        self.routes = {key: list(value) for key, value in routes.items()}
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, method, url, data=None, headers=None, *args, **kwargs):
        with self.lock:
            self.calls.append({"method": method, "url": url, "data": data, "headers": dict(headers or {})})
            for (want, prefix), answers in self.routes.items():
                if want == method and url.startswith(prefix):
                    answer = answers.pop(0) if len(answers) > 1 else answers[0]
                    break
            else:
                raise AssertionError(f"unexpected {method} {url}")
        if isinstance(answer, Exception):
            raise answer
        return answer(data) if callable(answer) else answer

    def bodies(self, method="POST"):
        return [json.loads(c["data"]) for c in self.calls if c["method"] == method and c["data"]]


def reply(status, body=None, location=None):
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or b"")
    return net.Reply(status, location, data)


class Harness(unittest.TestCase):

    def setUp(self):
        isolate(self)
        journal.register_schema(outlet_store.SCHEMA, outlet_store.VERSION, outlet_store.migrate)
        self.folder = Path(tempfile.mkdtemp(prefix="mobster-kinds-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.home = self.folder / "home"
        (self.home / "Documents").mkdir(parents=True)
        patcher = mock.patch.object(outlet_folder, "home", lambda: self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.book = journal.Journal(self.folder / "j.sqlite3")
        self.addCleanup(self.book.close)
        self.run = SimpleNamespace(
            id="a1b2c3d4e5f6", status="completed", created_at=1760000000.0, finished_at=1760000042.0, events=[],
            extras={"sendTo": {"destination": "x", "workflow": {"id": "0123456789ab", "name": "Lead check",
                                                                  "blanks": {"person": "Sam Lee"}}}},
            summary={"status": "completed", "answer": "=HYPERLINK(\"https://evil.example\") Sam replied: yes, 3 seats.",
                     "data": {"count": 3, "note": "+1 more"}, "data_status": "validated", "outcome": "done"})
        self.runtime = SimpleNamespace(journal=self.book, lock=threading.RLock(), runs={self.run.id: self.run})
        self.vault = outlet_vault.FileVault(self.folder / "keys.json")
        self.http = FakeHTTP({})
        self.service = self.make()

    def make(self):
        service = outlet_service.OutletService(self.runtime, vault=self.vault, call=self.http, backoff=(0.02,),
                                               attempts=3)
        self.addCleanup(service.close)
        return service

    def deliver(self, destination, service=None):
        service = service or self.service
        delivery = service.enqueue(self.run.id, destination["id"])
        deadline = time.monotonic() + 5
        while service.store.delivery(delivery["id"])["status"] == "sending" and time.monotonic() < deadline:
            time.sleep(0.01)
        return service.store.delivery(delivery["id"])


class FolderTests(Harness):
    def test_each_result_is_appended_as_a_csv_row_and_nothing_is_overwritten(self):
        target = self.home / "Documents" / "Leads.csv"
        destination = self.service.add({"name": "Leads file", "kind": "folder", "folder": "~/Documents",
                                        "file": "Leads.csv"})
        self.assertEqual((destination["kind"], destination["hint"]), ("folder", "~/Documents/Leads.csv"))
        self.assertEqual(destination["config"], {"folder": "~/Documents", "file": "Leads.csv", "format": "csv"})
        self.assertIsNone(self.vault.get(destination["id"]), "a folder keeps nothing in the Keychain")
        self.assertEqual(self.deliver(destination)["status"], "sent")
        self.assertEqual(self.deliver(destination)["status"], "sent")
        lines = target.read_text().splitlines()
        self.assertEqual(lines[0], "Finished,Status,Answer,Reason,Workflow,Data,Run,person,count,note")
        self.assertEqual(len(lines), 3, "one header, then one row per result")
        self.assertIn("Sam Lee", lines[1])
        self.assertIn('"\'=HYPERLINK(""https://evil.example"") Sam replied: yes, 3 seats."', lines[1])
        self.assertTrue(lines[1].endswith(",Sam Lee,3,'+1 more"))
        target.write_text("Answer,Person,Mine\nkept,by,me")
        self.assertEqual(self.deliver(destination)["status"], "sent")
        lines = target.read_text().splitlines()
        self.assertEqual(lines[:2], ["Answer,Person,Mine", "kept,by,me"])
        self.assertTrue(lines[2].endswith(",Sam Lee,"))

    def test_json_lines_hold_the_webhook_body(self):
        destination = self.service.add({"name": "Log", "kind": "folder", "folder": str(self.home / "Documents"),
                                        "file": "log.jsonl"})
        self.assertEqual(destination["config"]["format"], "jsonl")
        self.deliver(destination)
        [line] = (self.home / "Documents" / "log.jsonl").read_text().splitlines()
        body = json.loads(line)
        self.assertEqual((body["type"], body["runId"], body["workflow"]["name"]), ("run.finished", self.run.id,
                                                                                   "Lead check"))
        self.assertEqual(self.service.test(destination["id"])["ok"], True)

    def test_a_folder_outside_home_a_link_or_a_locked_folder_is_refused(self):
        outside = Path(tempfile.mkdtemp(prefix="mobster-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (self.home / "Documents" / "away").symlink_to(outside)
        for folder, words in ((str(outside), "Choose a folder in your home folder"),
                              ("~/Documents/away", "Choose a folder in your home folder"),
                              ("~/Nowhere", "That folder doesn't exist"),
                              ("Documents", "Choose a folder in your home")):
            with self.subTest(folder=folder):
                with self.assertRaises(ValueError) as caught:
                    self.service.add({"name": "F", "kind": "folder", "folder": folder})
                self.assertIn(words, str(caught.exception))
        for name in ("../x.csv", ".hidden.csv", "x.txt"):
            with self.subTest(file=name):
                with self.assertRaises(ValueError):
                    self.service.add({"name": "F", "kind": "folder", "folder": "~/Documents", "file": name})
        (self.home / "Documents" / "linked.csv").symlink_to(outside / "elsewhere.csv")
        with self.assertRaises(ValueError):
            self.service.add({"name": "F", "kind": "folder", "folder": "~/Documents", "file": "linked.csv"})
        self.assertEqual(self.service.list(), [])
        locked = self.home / "Locked"
        locked.mkdir()
        destination = self.service.add({"name": "Locked", "kind": "folder", "folder": "~/Locked"})
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        row = self.deliver(destination)
        self.assertEqual((row["status"], row["attempts"]), ("failed", 1))
        self.assertIn("macOS didn't let Mobster write in ~/Locked", row["error"])
        self.assertFalse(any(locked.iterdir()))


class EmailTests(Harness):
    def fake(self, answer):
        calls = []

        def run(argv, data):
            calls.append((argv, data))
            return answer
        return calls, mock.patch.object(outlet_mail, "run", run)

    def test_a_result_is_an_email_to_the_one_address_saved(self):
        calls, patch = self.fake((0, "sent\n", ""))
        destination = self.service.add({"name": "Me", "kind": "email", "to": "me@example.com"})
        self.assertEqual(destination["hint"], "me@example.com")
        with patch:
            row = self.deliver(destination)
        self.assertEqual(row["status"], "sent")
        [(argv, data)] = calls
        self.assertEqual(argv[:4], [outlet_mail.OSASCRIPT, "-l", "JavaScript", "-e"])
        message = json.loads(data)
        self.assertEqual(message["to"], "me@example.com")
        self.assertEqual(message["subject"][:22], "Mobster: Lead check: =")
        self.assertIn("Sam replied", message["content"])
        self.assertIn("person: Sam Lee", message["content"])
        for word in ("me@example.com", "Sam replied"):
            self.assertNotIn(word, " ".join(argv), "nothing personal in argv")

    def test_mail_without_an_account_or_permission_fails_once_and_says_why(self):
        destination = self.service.add({"name": "Me", "kind": "email", "to": "me@example.com"})
        for answer, words in (((0, "no_account", ""), "Mail has no account to send from"),
                              ((1, "", "execution error: Not authorized to send Apple events to Mail. (-1743)"),
                               "Privacy & Security › Automation"),
                              ((1, "", "execution error: Error: something else (-10000)"), "Check Mail's Outbox")):
            with self.subTest(words=words):
                calls, patch = self.fake(answer)
                with patch:
                    row = self.deliver(destination)
                self.assertEqual((row["status"], row["attempts"], len(calls)), ("failed", 1, 1))
                self.assertIn(words, row["error"])
        for to in ("", "not an address", "a@b", "me@example.com, you@example.com", "x" * 250 + "@example.com"):
            with self.subTest(to=to):
                with self.assertRaises(ValueError):
                    self.service.add({"name": "Other", "kind": "email", "to": to})

    def test_a_task_can_never_choose_the_address(self):
        destination = self.service.add({"name": "Me", "kind": "email", "to": "me@example.com"})
        with mock.patch.object(outlet_service, "service_for", lambda runtime: self.service):
            self.assertEqual(outlet_register.validate_send_to("Me", None), {"destination": destination["id"]})
            for value in ({"destination": "Me", "to": "attacker@example.com"},
                          {"destination": "Me", "workflow": {"name": "x", "to": "attacker@example.com"}}):
                with self.assertRaises(ValueError):
                    outlet_register.validate_send_to(value, None)
        with self.assertRaises(ValueError):
            self.service.add({"name": "Hook", "kind": "webhook", "url": "https://example.com/x",
                              "to": "attacker@example.com"})


class InFlightTests(Harness):
    def test_a_try_under_way_when_mobster_quit_is_never_sent_again(self):
        destination = self.service.add({"name": "Me", "kind": "email", "to": "me@example.com"})
        hook = self.service.add({"name": "Hook", "url": "https://hooks.zapier.com/hooks/catch/1/abc"})
        now = time.time()
        for delivery_id, target, inflight in (("aaaaaaaaaaaa", destination, now), ("bbbbbbbbbbbb", hook, None)):
            self.service.store.add_delivery({"id": delivery_id, "run_id": self.run.id, "destination_id": target["id"],
                                             "destination_name": target["name"], "status": "sending", "attempts": 0,
                                             "next_at": now, "created_at": now, "updated_at": now,
                                             "inflight_at": inflight})
        posts = []
        calls, patch = self.fake_mail()
        later = outlet_service.OutletService(self.runtime, vault=self.vault, backoff=(0.02,), attempts=3,
                                             post=lambda *a: (posts.append(a), outlet_service.webhook.Outcome(
                                                 True, False, 200))[1])
        self.addCleanup(later.close)
        with patch:
            later.start()
            deadline = time.monotonic() + 5
            while later.store.delivery("bbbbbbbbbbbb")["status"] == "sending" and time.monotonic() < deadline:
                time.sleep(0.01)
        email = later.store.delivery("aaaaaaaaaaaa")
        self.assertEqual(email["status"], "failed")
        self.assertEqual(email["error"], "Mobster quit while sending this, so it may have arrived. Check Me, then "
                                         "resend if it's missing.")
        self.assertEqual(calls, [], "never sent again")
        self.assertEqual(later.store.delivery("bbbbbbbbbbbb")["status"], "sent", "a webhook's receiver skips repeats")
        self.assertEqual(len(posts), 1)

    def fake_mail(self):
        calls = []
        return calls, mock.patch.object(outlet_mail, "run", lambda argv, data: (calls.append(argv), (0, "sent", ""))[1])

    def test_kinds_keep_their_kind_and_their_fields(self):
        destination = self.service.add({"name": "Me", "kind": "email", "to": "me@example.com"})
        for body, words in (({"kind": "webhook"}, "keeps its kind"),
                            ({"url": "https://example.com"}, "Change the name"),
                            ({"to": "new@example.com"}, None)):
            with self.subTest(body=body):
                if words:
                    with self.assertRaises(ValueError) as caught:
                        self.service.update(destination["id"], body)
                    self.assertIn(words, str(caught.exception))
                else:
                    self.assertEqual(self.service.update(destination["id"], body)["hint"], "new@example.com")
        with self.assertRaises(ValueError):
            self.service.add({"name": "X", "kind": "fax", "url": "https://example.com"})

    def test_an_empty_name_gives_an_example_for_its_own_kind(self):
        for kind, example in (("webhook", "My server"), ("slack", "Team channel"), ("folder", "Results file"),
                              ("email", "My email"),
                              ("sheets", "Sales sheet"), ("discord", "Team channel"), ("notion", "Notion")):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError) as caught:
                    self.service.add({"name": " ", "kind": kind})
                self.assertIn(f"like {example}.", str(caught.exception))
        destination = self.service.add({"name": "Results", "kind": "folder", "folder": "~/Documents",
                                        "file": "results.csv"})
        with self.assertRaises(ValueError) as caught:
            self.service.update(destination["id"], {"name": ""})
        self.assertIn("like Results file.", str(caught.exception))


class MigrationTests(unittest.TestCase):
    V1 = ("""CREATE TABLE outlet_destinations (id TEXT PRIMARY KEY, name TEXT NOT NULL, hint TEXT NOT NULL,
             has_secret INTEGER NOT NULL DEFAULT 0, screenshot INTEGER NOT NULL DEFAULT 0, last_test TEXT,
             created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
          """CREATE TABLE outlet_deliveries (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, destination_id TEXT NOT NULL,
             destination_name TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
             http_status INTEGER, error TEXT, next_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")

    def test_every_v1_destination_becomes_a_webhook(self):
        connection = sqlite3.connect(":memory:")
        for sql in self.V1:
            connection.execute(sql)
        connection.execute("INSERT INTO outlet_destinations VALUES ('0123456789ab', 'Zapier', "
                           "'https://hooks.zapier.com/hooks/…', 1, 0, NULL, 1, 1)")
        connection.execute("INSERT INTO outlet_deliveries VALUES ('d0d0d0d0d0d0', 'a1b2c3d4e5f6', '0123456789ab', "
                           "'Zapier', 'sending', 1, 503, 'x', 5, 1, 1)")
        outlet_store.migrate(connection, 1)
        outlet_store.migrate(connection, 1)
        row = connection.execute(f"SELECT {', '.join(outlet_store.DESTINATION_COLUMNS)} FROM outlet_destinations")
        destination = outlet_store._destination(row.fetchone())
        self.assertEqual((destination["kind"], destination["config"], destination["has_secret"]), ("webhook", {}, True))
        row = connection.execute(f"SELECT {', '.join(outlet_store.DELIVERY_COLUMNS)} FROM outlet_deliveries")
        self.assertIsNone(outlet_store._delivery(row.fetchone())["inflight_at"])
        self.assertEqual(outlet_store.VERSION, 3)
        self.assertEqual(
            outlet_store._delivery(connection.execute(
                f"SELECT {', '.join(outlet_store.DELIVERY_COLUMNS)} FROM outlet_deliveries").fetchone())["event"],
            "run.finished")

    def test_a_v1_keychain_item_still_reads(self):
        word = outlet_vault.encode("https://hooks.zapier.com/hooks/catch/1/abc", "s3cret-value")
        self.assertEqual(outlet_vault.decode(word), {"url": "https://hooks.zapier.com/hooks/catch/1/abc",
                                                     "secret": "s3cret-value"})
        notion = "ntn_" + "a1B2" * 10
        self.assertEqual(outlet_vault.decode(outlet_vault.encode(None, None, {"token": notion})),
                         {"url": None, "secret": None, "token": notion})
        tokens = {"access_token": "planted-access", "refresh_token": "planted-refresh", "expires_at": 1.0}
        self.assertEqual(outlet_vault.decode(outlet_vault.encode(None, None, {"oauth": tokens, "token": None})),
                         {"url": None, "secret": None, "oauth": tokens})


API = "https://api.mobster.dev"
DISCORD_URL = "https://discord.com" + "/api/" + "webhooks/" + "1428000000000000001/planted-webhook-key"
SERVER, CHANNEL = "1427000000000000001", "1427000000000000002"
DATABASE = "01234567-89ab-cdef-0123-456789abcdef"
SHEET = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
SHEET_URL = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}"


def tokens(access="planted-access", refresh="planted-refresh", expires_at=None, provider="notion", **kept):
    out = {"access_token": access, "refresh_token": refresh, "expires_at": expires_at, "client_id": "client",
           "client_secret": None, "token_endpoint": f"{API}/api/connectors/{provider}/token", "resource": None}
    if kept:
        out["kept"] = kept
    return out


class Apps:

    def __init__(self, missing=(), tested=()):
        self.missing, self.tested_keys = set(missing), set(tested)

    def tested(self, key):
        return key in self.tested_keys

    def get(self, key):
        if key in self.missing:
            return shared.Unavailable("unconfigured")
        return oauth.MobsterApp("client", f"https://{key}.example/authorize", f"{API}/api/connectors/{key}/token", "",
                                (), f"{API}/api/connectors/callback")


class FakeSignIn:
    last = None

    def __init__(self, url, on_done, *, name, app, keep=(), **_):
        self.on_done, self.name, self.app, self.keep, self.cancelled = on_done, name, app, keep, False
        FakeSignIn.last = self

    def begin(self):
        return self.app.authorize_url + "?state=planted"

    def come_back(self, found, message=None):
        self.on_done(oauth.Ended.SIGNED_IN if found else oauth.Ended.REFUSED, found, message)

    def cancel(self):
        self.cancelled = True


class Connected(Harness):
    def make(self):
        self.renewals, self.renewed = [], None
        service = outlet_service.OutletService(self.runtime, vault=self.vault, call=self.http, backoff=(0.02,),
                                               attempts=3, apps=Apps(), sign_in=FakeSignIn, refresh=self.renew,
                                               connectors=lambda: getattr(self, "connectors", None))
        self.addCleanup(service.close)
        return service

    def renew(self, saved, clock=None):
        self.renewals.append(saved["refresh_token"])
        answer = self.renewed.pop(0) if isinstance(self.renewed, list) else self.renewed
        if isinstance(answer, Exception):
            raise answer
        return answer

    def connect(self, kind, found):
        begun = self.service.connect({"kind": kind})
        self.assertTrue(begun["authorizeUrl"].startswith("https://"))
        self.assertEqual(begun["connect"]["state"], "waiting")
        FakeSignIn.last.come_back(found)
        return self.service.connects.get(begun["connect"]["id"])

    def assert_no_secret_in_the_journal(self, *secrets):
        listed = json.dumps(self.service.list())
        self.book.connection.execute("PRAGMA wal_checkpoint(FULL)")
        stored = b"".join(file.read_bytes() for file in self.folder.glob("j.sqlite3*"))
        for secret in secrets:
            self.assertNotIn(secret, listed)
            self.assertNotIn(secret.encode(), stored)


class DiscordTests(Connected):
    def test_one_sign_in_picks_the_channel_and_results_post_with_mentions_off(self):
        found = self.connect("discord", tokens(webhook={"url": DISCORD_URL, "guild_id": SERVER, "channel_id": CHANNEL,
                                                         "name": "Mobster"}))
        self.assertEqual(FakeSignIn.last.keep, ("webhook",))
        self.assertEqual((found["state"], found["targets"]), ("done", None))
        destination = self.service.add({"name": "Team", "kind": "discord", "connect": found["id"]})
        self.assertEqual((destination["hint"], destination["config"]),
                         ("A Discord channel", {"server": SERVER, "channel": CHANNEL}))
        self.assertEqual(self.vault.get(destination["id"]), {"url": DISCORD_URL, "secret": None},
                         "the webhook's address, never the access token")
        with self.assertRaises(APIError):
            self.service.connects.get(found["id"])
        self.http.routes = {("POST", DISCORD_URL): [reply(204)]}
        self.assertEqual(self.deliver(destination)["status"], "sent")
        [body] = self.http.bodies("POST")
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertTrue(body["content"].startswith("Mobster › Lead check: "))
        self.assert_no_secret_in_the_journal("planted-webhook-key", "planted-access")

    def test_it_refuses_without_a_channel_and_a_deleted_webhook_says_connect_again(self):
        for hook in (None, {"url": "https://example.com/api/webhooks/1/x", "guild_id": SERVER, "channel_id": CHANNEL},
                        {"url": DISCORD_URL, "guild_id": "nope", "channel_id": CHANNEL}):
            with self.subTest(webhook=hook):
                found = self.connect("discord", tokens(**({"webhook": hook} if hook else {})))
                self.assertEqual(found["state"], "failed")
                self.assertIn("Connect Discord again", found["message"])
                with self.assertRaises(ValueError):
                    self.service.add({"name": "Team", "kind": "discord", "connect": found["id"]})
        with self.assertRaises(ValueError) as caught:
            self.service.add({"name": "Team", "kind": "discord"})
        self.assertIn("Connect Discord first", str(caught.exception))
        self.assertEqual(self.service.list(), [])
        found = self.connect("discord", tokens(webhook={"url": DISCORD_URL, "guild_id": SERVER, "channel_id": CHANNEL}))
        destination = self.service.add({"name": "Team", "kind": "discord", "connect": found["id"]})
        self.http.routes = {("POST", DISCORD_URL): [reply(404)]}
        row = self.deliver(destination)
        self.assertEqual((row["status"], row["attempts"]), ("failed", 1))
        self.assertIn("the webhook was deleted. Connect Discord again", row["error"])

    def test_a_service_whose_app_isnt_ready_says_so_and_starts_nothing(self):
        self.service.connects.apps = Apps(missing={"discord"})
        FakeSignIn.last = None
        with self.assertRaises(APIError) as caught:
            self.service.connect({"kind": "discord"})
        self.assertEqual((caught.exception.status, caught.exception.code), (503, "connect_unavailable"))
        self.assertIn("Mobster's own Discord app isn't set up yet", str(caught.exception))
        self.assertIsNone(FakeSignIn.last)
        for kind in ("webhook", "folder", "email"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.service.connect({"kind": kind})


def database(title="Leads"):
    properties = {"Name": {"type": "title"}, "Answer": {"type": "rich_text"}, "Status": {"type": "select"},
                  "Count": {"type": "number"}, "Person": {"type": "rich_text"}}
    return reply(200, {"object": "database", "title": [{"plain_text": title}], "properties": properties})


SEARCH = reply(200, {"results": [
    {"object": "database", "id": DATABASE, "title": [{"plain_text": "Leads"}]},
    {"object": "database", "id": "fedcba98-7654-3210-fedc-ba9876543210", "title": []},
    {"object": "page", "id": "11111111-2222-3333-4444-555555555555"}]})


class NotionTests(Connected):
    def add(self):
        self.http.routes = {("POST", "https://api.notion.com/v1/search"): [SEARCH]}
        found = self.connect("notion", tokens(workspace_name="Sam's"))
        self.assertEqual((found["state"], found["account"]), ("done", "Sam's"))
        self.assertEqual(found["targets"], [{"id": DATABASE, "name": "Leads"},
                                            {"id": "fedcba98-7654-3210-fedc-ba9876543210", "name": "Untitled"}])
        destination = self.service.add({"name": "CRM", "kind": "notion", "connect": found["id"], "target": DATABASE})
        self.http.calls.clear()
        return destination

    def test_one_sign_in_lists_the_shared_databases_and_a_result_becomes_a_page(self):
        destination = self.add()
        self.assertEqual(destination["hint"], "Notion › Leads")
        self.assertEqual(destination["config"], {"database": DATABASE, "title": "Leads", "workspace": "Sam's"})
        self.assertEqual(self.vault.get(destination["id"])["oauth"]["access_token"], "planted-access")
        self.http.routes = {("GET", f"https://api.notion.com/v1/databases/{DATABASE}"): [database()],
                            ("POST", "https://api.notion.com/v1/pages"): [reply(200, {"object": "page"})]}
        self.assertEqual(self.deliver(destination)["status"], "sent")
        [page] = self.http.bodies("POST")
        self.assertEqual(page["parent"], {"database_id": DATABASE})
        self.assertEqual(page["properties"]["Count"], {"number": 3})
        self.assertEqual(page["properties"]["Person"]["rich_text"][0]["text"]["content"], "Sam Lee")
        self.assertTrue(all(c["headers"]["Authorization"] == "Bearer planted-access" for c in self.http.calls))
        self.assertTrue(all(c["url"].startswith("https://api.notion.com/v1/") for c in self.http.calls))
        self.assert_no_secret_in_the_journal("planted-access", "planted-refresh")

    def test_a_401_renews_once_and_keeps_the_rotated_tokens(self):
        destination = self.add()
        self.renewed = tokens("planted-access-2", "planted-refresh-2")
        self.http.routes = {("GET", f"https://api.notion.com/v1/databases/{DATABASE}"): [
                                reply(401, {"code": "unauthorized"}), database()],
                            ("POST", "https://api.notion.com/v1/pages"): [reply(200, {"object": "page"})]}
        self.assertEqual(self.deliver(destination)["status"], "sent")
        self.assertEqual(self.renewals, ["planted-refresh"])
        self.assertEqual(self.http.calls[-1]["headers"]["Authorization"], "Bearer planted-access-2")
        self.assertEqual(self.vault.get(destination["id"])["oauth"]["refresh_token"], "planted-refresh-2",
                         "Notion takes each refresh token once: the new one is kept")

    def test_it_refuses_a_database_not_shared_a_sign_in_taken_back_or_a_gone_database(self):
        self.http.routes = {("POST", "https://api.notion.com/v1/search"): [SEARCH]}
        found = self.connect("notion", tokens())
        for target, words in ((None, "Pick a database from the list"),
                              ("0123456789abcdef0123456789abcdef", "Pick a database from the list")):
            with self.subTest(target=target), self.assertRaises(ValueError) as caught:
                self.service.add({"name": "CRM", "kind": "notion", "connect": found["id"], "target": target})
            self.assertIn(words, str(caught.exception))
        self.assertEqual(self.service.list(), [])
        destination = self.add()
        self.renewed = oauth.OAuthError("The sign-in server answered HTTP 400: invalid_grant.")
        self.http.routes = {("GET", f"https://api.notion.com/v1/databases/{DATABASE}"): [
            reply(401, {"code": "unauthorized"})]}
        row = self.deliver(destination)
        self.assertEqual((row["status"], row["attempts"]), ("failed", 1))
        self.assertIn("Notion signed Mobster out. Connect Notion again", row["error"])
        self.assertEqual([c for c in self.http.calls if c["method"] == "POST" and "/pages" in c["url"]], [])
        self.http.routes = {("GET", f"https://api.notion.com/v1/databases/{DATABASE}"): [
            reply(404, {"code": "object_not_found"})]}
        self.assertIn("no longer shares that database", self.deliver(destination)["error"])
        self.renewed = [oauth.OAuthError("Mobster couldn't reach the sign-in server.", retry=True),
                        tokens("planted-access-3", "planted-refresh-3")]
        self.http.routes = {("GET", f"https://api.notion.com/v1/databases/{DATABASE}"): [
            reply(401, {"code": "unauthorized"}), reply(401, {"code": "unauthorized"}), database()],
            ("POST", "https://api.notion.com/v1/pages"): [reply(200, {"object": "page"})]}
        row = self.deliver(destination)
        self.assertEqual((row["status"], row["attempts"]), ("sent", 2))
        self.assertEqual(self.vault.get(destination["id"])["oauth"]["access_token"], "planted-access-3")

    def test_a_page_that_may_exist_is_never_made_again(self):
        destination = self.add()
        self.http.routes = {("GET", f"https://api.notion.com/v1/databases/{DATABASE}"): [database()],
                            ("POST", "https://api.notion.com/v1/pages"): [
                                net.NetError("api.notion.com didn't answer within 10 seconds.", unsent=False)]}
        row = self.deliver(destination)
        self.assertEqual((row["status"], row["attempts"]), ("failed", 1))
        self.assertIn("It may have arrived: check the database", row["error"])
        self.assertEqual(len([c for c in self.http.calls if c["method"] == "POST"]), 1)

    def test_a_destination_saved_with_an_integration_token_still_sends(self):
        destination = self.add()
        self.vault.put(destination["id"], None, None, {"token": "ntn_" + "a1B2" * 10})
        self.http.routes = {("GET", f"https://api.notion.com/v1/databases/{DATABASE}"): [database()],
                            ("POST", "https://api.notion.com/v1/pages"): [reply(200, {"object": "page"})]}
        self.assertEqual(self.deliver(destination)["status"], "sent")
        self.assertEqual(self.http.calls[-1]["headers"]["Authorization"], "Bearer ntn_" + "a1B2" * 10)


class SheetsTests(Connected):
    def add(self, existing=False):
        files = {"files": [{"id": SHEET, "name": "Mobster results"}] if existing else []}
        self.http.routes = {("GET", "https://www.googleapis.com/drive/v3/files?"): [reply(200, files)],
                            ("POST", "https://sheets.googleapis.com/v4/spreadsheets"): [
                                reply(200, {"spreadsheetId": SHEET})]}
        found = self.connect("sheets", tokens(provider="google-sheets"))
        self.assertEqual((found["state"], found["account"], found["targets"]), ("done", "Mobster results", None))
        return self.service.add({"name": "Leads", "kind": "sheets", "connect": found["id"]})

    def test_one_sign_in_makes_mobster_results_and_each_result_is_a_row_by_name(self):
        destination = self.add()
        [made] = self.http.bodies("POST")
        self.assertEqual(made["properties"]["title"], "Mobster results")
        drive = self.http.calls[0]["url"]
        self.assertIn("name+%3D+%27Mobster+results%27", drive)
        self.assertEqual((destination["hint"], destination["config"]["spreadsheet"]),
                         ("Google Sheets › Mobster results", SHEET))
        self.http.calls.clear()
        self.http.routes = {("GET", f"{SHEET_URL}/values/1:1"): [reply(200, {"range": "Results!1:1"})],
                            ("PUT", f"{SHEET_URL}/values/A1?valueInputOption=RAW"): [reply(200, {})],
                            ("POST", f"{SHEET_URL}/values/A1:append"): [reply(200, {})]}
        self.assertEqual(self.deliver(destination)["status"], "sent")
        header = self.http.bodies("PUT")[0]["values"][0]
        self.assertEqual(header[:7], ["Finished", "Status", "Answer", "Reason", "Workflow", "Data", "Run"])
        [row] = self.http.bodies("POST")[0]["values"]
        by_name = dict(zip(header, row))
        self.assertTrue(by_name["Answer"].startswith("=HYPERLINK"), "kept as written: RAW input never runs it")
        self.assertIn("valueInputOption=RAW", self.http.calls[-1]["url"])
        self.assertEqual((by_name["count"], by_name["person"]), (3, "Sam Lee"))
        self.http.calls.clear()
        self.http.routes = {("GET", f"{SHEET_URL}/values/1:1"): [reply(200, {"values": [["Run", "Answer", "Mine"]]})],
                            ("PUT", f"{SHEET_URL}/values/A1"): [reply(200, {})],
                            ("POST", f"{SHEET_URL}/values/A1:append"): [reply(200, {})]}
        self.assertEqual(self.deliver(destination)["status"], "sent")
        header = self.http.bodies("PUT")[0]["values"][0]
        self.assertEqual(header[:3], ["Run", "Answer", "Mine"])
        [row] = self.http.bodies("POST")[0]["values"]
        self.assertEqual((row[0], row[2]), (self.run.id, ""))
        self.assert_no_secret_in_the_journal("planted-access", "planted-refresh")

    def test_connecting_again_uses_the_sheet_it_made_before(self):
        destination = self.add(existing=True)
        self.assertEqual(destination["config"]["spreadsheet"], SHEET)
        self.assertEqual([c["method"] for c in self.http.calls], ["GET"], "found, not made again")

    def test_an_expiring_token_is_renewed_and_a_token_taken_back_adds_nothing(self):
        destination = self.add()
        self.vault.put(destination["id"], None, None, {"oauth": tokens(expires_at=time.time() - 1)})
        self.renewed = tokens("planted-access-2", "planted-refresh")
        self.http.routes = {("GET", f"{SHEET_URL}/values/1:1"): [reply(200, {"values": [["Finished", "Status", "Answer",
                                                                                         "Reason", "Workflow", "Data",
                                                                                         "Run", "person", "count",
                                                                                         "note"]]})],
                            ("POST", f"{SHEET_URL}/values/A1:append"): [reply(200, {})]}
        self.http.calls.clear()
        self.assertEqual(self.deliver(destination)["status"], "sent")
        self.assertEqual([c["method"] for c in self.http.calls], ["GET", "POST"], "a full header is not written again")
        self.assertTrue(all(c["headers"]["Authorization"] == "Bearer planted-access-2" for c in self.http.calls))
        self.vault.put(destination["id"], None, None, {"oauth": tokens(expires_at=time.time() - 1)})
        self.renewed = oauth.OAuthError("The sign-in server answered HTTP 400: invalid_grant.")
        self.http.calls.clear()
        row = self.deliver(destination)
        self.assertEqual((row["status"], row["attempts"]), ("failed", 1))
        self.assertIn("Google Sheets signed Mobster out. Connect Google Sheets again", row["error"])
        self.assertEqual(self.http.calls, [])

    def test_a_gone_sheet_or_a_row_that_may_exist_is_never_added_twice(self):
        destination = self.add()
        self.http.routes = {("GET", f"{SHEET_URL}/values/1:1"): [reply(404, {"error": {"code": 404}})]}
        row = self.deliver(destination)
        self.assertEqual((row["status"], row["attempts"]), ("failed", 1))
        self.assertIn("Mobster can't open Mobster results any more", row["error"])
        self.http.routes = {("GET", f"{SHEET_URL}/values/1:1"): [reply(200, {"values": [["Run"]]})],
                            ("PUT", f"{SHEET_URL}/values/A1"): [reply(200, {})],
                            ("POST", f"{SHEET_URL}/values/A1:append"): [reply(503)]}
        self.http.calls.clear()
        row = self.deliver(destination)
        self.assertEqual((row["status"], row["attempts"]), ("failed", 1))
        self.assertIn("Check Mobster results, then resend if the row is missing", row["error"])
        self.assertEqual(len([c for c in self.http.calls if c["method"] == "POST"]), 1)
        self.http.routes[("POST", f"{SHEET_URL}/values/A1:append")] = [reply(429), reply(200, {})]
        self.assertEqual(self.deliver(destination)["status"], "sent")

    def test_a_sheet_saved_with_the_apps_script_says_connect_again(self):
        destination = self.add()
        self.service.store.update_destination(destination["id"], config={"scope": "named"})
        self.http.calls.clear()
        row = self.deliver(destination)
        self.assertEqual(row["status"], "failed")
        self.assertIn("no script. Connect Google Sheets again", row["error"])
        self.assertEqual(self.http.calls, [])


class Connectors:

    def __init__(self, signed_in=True, rows=1, ready=True):
        self.rows = [{"id": "c0ffee000001", "name": "Slack", "preset": "slack", "auth": "oauth" if signed_in
                      else "none",
                      "signIn": None}][:rows]
        self.apps = Apps(missing=() if ready else {"slack"})
        self.tokens = ["xoxp-planted-1"]
        self.refused = None
        self.asked, self.added, self.signing = [], [], []

    def list(self):
        return self.rows

    def add(self, body):
        self.added.append(body)
        self.rows.append({"id": "c0ffee000002", "name": "Slack", "preset": "slack", "auth": "none", "signIn": None})
        return self.rows[-1]

    def begin_sign_in(self, connector_id):
        self.signing.append(connector_id)
        next(row for row in self.rows if row["id"] == connector_id)["signIn"] = {"state": "waiting", "message": None}
        return {"authorizeUrl": "https://slack.com/oauth/v2_user/authorize?state=planted"}

    def signed_in(self, connector_id, ok=True):
        row = next(row for row in self.rows if row["id"] == connector_id)
        row["signIn"] = {"state": "done" if ok else "failed", "message": None if ok else "You didn't allow Mobster to "
                         "connect."}
        row["auth"] = "oauth" if ok else row["auth"]

    def bearer(self, connector_id, refresh=False):
        self.asked.append((connector_id, refresh))
        if self.refused is not None:
            raise self.refused
        if refresh:
            self.tokens.append(f"xoxp-planted-{len(self.tokens) + 1}")
        return self.tokens[-1]


CHANNELS = reply(200, {"ok": True, "channels": [{"id": "C0123TEAM", "name": "team", "is_private": False},
                                                 {"id": "G0123SALES", "name": "sales", "is_private": True},
                                                 {"id": "D0123", "name": "a-dm"}],
                       "response_metadata": {"next_cursor": ""}})


class SlackTests(Connected):
    def add(self):
        self.connectors = Connectors()
        self.http.routes = {("GET", "https://slack.com/api/auth.test"): [reply(200, {"ok": True,
            "team": "Radish Software"})],
                            ("GET", "https://slack.com/api/users.conversations?"): [CHANNELS]}
        begun = self.service.connect({"kind": "slack"})
        self.assertNotIn("authorizeUrl", begun, "Slack's sign-in is the connector's: nothing opens")
        found = begun["connect"]
        self.assertEqual((found["state"], found["account"]), ("done", "Radish Software"))
        self.assertEqual(found["targets"],
                         [{"id": "G0123SALES", "name": "#sales"}, {"id": "C0123TEAM", "name": "#team"}])
        return self.service.add({"name": "Team", "kind": "slack", "connect": found["id"], "target": "C0123TEAM"})

    def test_it_posts_as_you_with_the_slack_connections_sign_in_and_mentions_escaped(self):
        destination = self.add()
        self.assertEqual((destination["hint"], destination["config"]["channelName"]), ("Slack › #team", "#team"))
        self.assertIsNone(self.vault.get(destination["id"]), "no token of its own")
        self.run.summary["answer"] = "<!channel> Sam & <@U123> replied: yes"
        self.http.routes = {("POST", "https://slack.com/api/chat.postMessage"): [reply(200, {"ok": True, "ts": "1.2"})]}
        self.assertEqual(self.deliver(destination)["status"], "sent")
        [body] = self.http.bodies("POST")
        self.assertEqual((body["channel"], body["link_names"], body["unfurl_links"]), ("C0123TEAM", False, False))
        text = body["blocks"][0]["text"]["text"]
        self.assertNotIn("<!channel>", json.dumps(body))
        self.assertIn("&lt;!channel&gt;", text)
        self.assertTrue(text.startswith("Mobster › Lead check: "))
        self.assertEqual(body["blocks"][1]["elements"][0]["text"], "Done in Mobster, from Lead check")
        self.assertEqual(self.http.calls[-1]["headers"]["Authorization"], "Bearer xoxp-planted-1")
        self.assert_no_secret_in_the_journal("xoxp-planted-1")
        self.assertTrue(self.service.test(destination["id"])["ok"])
        self.assertEqual(self.http.bodies()[-1]["blocks"][1]["elements"][0]["text"], "Test in Mobster")

    def test_without_a_slack_sign_in_connect_signs_in_the_slack_connector_first(self):
        self.http.routes = {("GET", "https://slack.com/api/auth.test"): [reply(200, {"ok": True,
            "team": "Radish Software"})],
                            ("GET", "https://slack.com/api/users.conversations?"): [CHANNELS]}
        for rows, added in ((0, [{"preset": "slack"}]), (1, [])):
            with self.subTest(slack_connector=bool(rows)):
                self.connectors = Connectors(signed_in=False, rows=rows)
                begun = self.service.connect({"kind": "slack"})
                self.assertTrue(begun["authorizeUrl"].startswith("https://slack.com/oauth/"))
                self.assertEqual(self.connectors.added, added, "one Slack sign-in: the connector's")
                [connector] = self.connectors.signing
                connect = begun["connect"]["id"]
                self.assertEqual(self.service.connects.get(connect)["state"], "waiting")
                self.connectors.signed_in(connector)
                found = self.service.connects.get(connect)
                self.assertEqual((found["state"], found["account"], len(found["targets"])), ("done",
                    "Radish Software", 2))
                destination = self.service.add({"name": f"Team {rows}", "kind": "slack", "connect": connect,
                                                "target": "C0123TEAM"})
                self.assertEqual(destination["config"]["channel"], "C0123TEAM")
        self.connectors = Connectors(signed_in=False)
        connect = self.service.connect({"kind": "slack"})["connect"]["id"]
        self.connectors.signed_in("c0ffee000001", ok=False)
        found = self.service.connects.get(connect)
        self.assertEqual((found["state"], found["message"]), ("failed", "You didn't allow Mobster to connect."))

    def test_the_app_lists_slack_only_where_it_can_connect(self):
        everything = ["webhook", "slack", "discord", "sheets", "notion", "folder", "email"]
        without_slack = [kind for kind in everything if kind != "slack"]
        tested = Connectors(signed_in=False, rows=0)
        tested.apps = Apps(tested={"slack"})
        for label, connectors, expected in (("a Slack connector signed in", Connectors(), everything),
                                         ("Slack tested", tested, everything),
                                         ("untested, signed out", Connectors(signed_in=False), without_slack),
                                         ("untested, none", Connectors(signed_in=False, rows=0), without_slack),
                                         ("connectors off", None, without_slack)):
            with self.subTest(label):
                self.connectors = connectors
                self.assertEqual(self.service.kinds(), expected)
        self.connectors = None

    def test_it_refuses_to_connect_without_an_app_to_sign_in_with(self):
        self.connectors = Connectors(signed_in=False, rows=0, ready=False)
        with self.assertRaises(APIError) as caught:
            self.service.connect({"kind": "slack"})
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "slack_own_app"))
        self.assertIn("Connect Slack with your own app under Use in tasks", str(caught.exception))
        self.assertEqual((self.connectors.added, self.connectors.signing), ([], []), "no connector added")
        self.connectors = None
        with self.assertRaises(APIError) as caught:
            self.service.connect({"kind": "slack"})
        self.assertEqual(caught.exception.code, "connectors_off")
        self.connectors = Connectors()
        self.connectors.refused = mcp.AuthNeeded("Slack needs you to sign in again.")
        with self.assertRaises(APIError) as caught:
            self.service.connect({"kind": "slack"})
        self.assertIn("Slack needs you to sign in again under Use in tasks", str(caught.exception))
        self.assertEqual(self.service.list(), [])

    def test_an_expired_token_is_renewed_once_and_a_revoked_one_posts_nothing(self):
        destination = self.add()
        self.http.routes = {("POST", "https://slack.com/api/chat.postMessage"): [
            reply(200, {"ok": False, "error": "token_expired"}), reply(200, {"ok": True})]}
        self.assertEqual(self.deliver(destination)["status"], "sent")
        self.assertEqual(self.connectors.asked[-2:], [("c0ffee000001", False), ("c0ffee000001", True)])
        self.assertEqual(self.http.calls[-1]["headers"]["Authorization"], "Bearer xoxp-planted-2")
        self.http.calls.clear()
        self.connectors.refused = mcp.AuthNeeded("Slack needs you to sign in again.")
        row = self.deliver(destination)
        self.assertEqual((row["status"], row["attempts"]), ("failed", 1))
        self.assertIn("Slack signed Mobster out", row["error"])
        self.assertEqual(self.http.calls, [])
        self.connectors.refused = APIError("That connector was removed.", 404, "not_found")
        self.assertIn("Slack was disconnected from Mobster", self.deliver(destination)["error"])

    def test_a_gone_channel_says_pick_another_and_a_post_that_may_exist_is_not_made_twice(self):
        destination = self.add()
        for error, words in (("channel_not_found", "the channel is gone"), ("is_archived", "the channel was archived"),
                             ("not_in_channel", "you're no longer in the channel")):
            with self.subTest(error=error):
                self.http.routes = {("POST", "https://slack.com/api/chat.postMessage"): [
                    reply(200, {"ok": False, "error": error})]}
                row = self.deliver(destination)
                self.assertEqual((row["status"], row["attempts"]), ("failed", 1))
                self.assertIn(f"Slack refused: {words}. Pick another channel", row["error"])
        self.http.routes = {("POST", "https://slack.com/api/chat.postMessage"): [reply(502)]}
        self.http.calls.clear()
        row = self.deliver(destination)
        self.assertEqual((row["status"], len(self.http.calls)), ("failed", 1), "a 5xx may have arrived: never twice")
        self.http.routes = {("POST", "https://slack.com/api/chat.postMessage"): [reply(429), reply(200, {"ok": True})]}
        self.assertEqual(self.deliver(destination)["status"], "sent", "Slack's 429 posted nothing: tried again")

    def test_a_slack_destination_saved_with_a_webhook_says_connect_again(self):
        destination = self.add()
        self.service.store.update_destination(destination["id"], config={"scope": "named"})
        self.http.calls.clear()
        row = self.deliver(destination)
        self.assertEqual(row["status"], "failed")
        self.assertIn("Mobster now posts as you, in a channel you pick. Connect Slack again", row["error"])
        self.assertEqual(self.http.calls, [])

    def test_changing_the_channel_keeps_what_it_takes(self):
        destination = self.add()
        self.service.update(destination["id"], {"events": ["run.started", "run.finished"], "scope": "all"})
        self.http.routes[("GET", "https://slack.com/api/users.conversations?")] = [CHANNELS]
        found = self.service.connect({"kind": "slack"})["connect"]
        changed = self.service.update(destination["id"], {"connect": found["id"], "target": "G0123SALES"})
        self.assertEqual((changed["hint"], changed["events"], changed["scope"]),
                         ("Slack › #sales", ["run.finished", "run.started"], "all"))


class ConnectLifecycleTests(Connected):
    def test_a_connect_is_forgotten_once_used_cancelled_or_old(self):
        self.http.routes = {("POST", "https://api.notion.com/v1/search"): [SEARCH]}
        begun = self.service.connect({"kind": "notion"})
        flow = FakeSignIn.last
        self.assertEqual(self.service.connects.cancel(begun["connect"]["id"]), begun["connect"]["id"])
        self.assertTrue(flow.cancelled)
        with self.assertRaises(APIError):
            self.service.connects.get(begun["connect"]["id"])
        begun = self.service.connect({"kind": "notion"})
        with self.assertRaises(ValueError) as caught:
            self.service.add({"name": "CRM", "kind": "notion", "connect": begun["connect"]["id"], "target": DATABASE})
        self.assertIn("Finish connecting Notion in your browser", str(caught.exception))
        FakeSignIn.last.come_back(None, "You didn't allow Mobster to connect.")
        self.assertEqual(self.service.connects.get(begun["connect"]["id"])["message"],
                         "You didn't allow Mobster to connect.")
        found = self.connect("notion", tokens())
        with self.assertRaises(ValueError):
            self.service.add({"name": "Sheet", "kind": "sheets", "connect": found["id"]})
        self.service.connects.clock = lambda: time.monotonic() + 3600
        with self.assertRaises(ValueError):
            self.service.add({"name": "CRM", "kind": "notion", "connect": found["id"], "target": DATABASE})


class BearerTests(unittest.TestCase):

    def test_two_renewals_at_once_reach_the_license_api_once(self):
        isolate(self)
        journal.register_schema(connector_store.SCHEMA, connector_store.VERSION, connector_store.migrate)
        folder = Path(tempfile.mkdtemp(prefix="mobster-bearer-"))
        self.addCleanup(shutil.rmtree, folder, True)
        book = journal.Journal(folder / "j.sqlite3")
        self.addCleanup(book.close)
        vault = connector_vault.FileVault(folder / "keys.json")
        gate, renewals = threading.Event(), []

        def renew(saved, clock):
            renewals.append(saved["refresh_token"])
            gate.wait(2)
            return {**saved, "access_token": "xoxp-renewed", "refresh_token": "xoxe-2", "expires_at": time.time() + 600}

        self.enterContext(mock.patch.object(shared, "ask", return_value=({}, frozenset({"slack"}))))
        service = connector_service.ConnectorService(SimpleNamespace(journal=book), vault=vault, refresh=renew)
        self.addCleanup(service.close)
        row = service.add({"preset": "slack"})
        vault.put(row["id"], {"oauth": {"access_token": "xoxp-old", "refresh_token": "xoxe-1", "expires_at": 1.0,
                                        "client_id": "c", "token_endpoint": f"{API}/api/connectors/slack/token"}})
        got = []
        threads = [threading.Thread(target=lambda: got.append(service.bearer(row["id"]))) for _ in range(2)]
        for thread in threads:
            thread.start()
        time.sleep(0.1)
        gate.set()
        for thread in threads:
            thread.join(3)
        self.assertEqual(got, ["xoxp-renewed", "xoxp-renewed"])
        self.assertEqual(renewals, ["xoxe-1"], "Slack takes each refresh token once")
        self.assertEqual(vault.get(row["id"])["oauth"]["refresh_token"], "xoxe-2")
        vault.put(row["id"], {"token": None})
        with self.assertRaises(mcp.AuthNeeded):
            service.bearer(row["id"])


ZAPIER_URL = "https://hooks.zapier.com/hooks/catch/123/abc/"
MAKE_URL = "https://hook.eu2.make.com/abcdef123456"


class EventsEndToEnd(OutletsBase):
    def add_events(self, url, name, events, scope="all", secret=SECRET):
        status, data = self.call("POST", "/api/outlets/destinations", {"name": name, "url": url, "secret": secret,
                                                                       "events": events, "scope": scope})
        self.assertEqual(status, 201, data)
        return data["destination"]

    def test_every_task_starts_and_finishes_signed_with_its_event(self):
        receiver = Receiver(self)
        destination = self.add_events(receiver.url, "Ops", ["run.started", "run.finished"])
        self.assertEqual((destination["events"], destination["scope"]), (["run.finished", "run.started"], "all"))
        run_id = self.task()
        self.finished(run_id)
        rows = self.until(lambda: (lambda r: r if len(r) == 2 and all(x["status"] == "sent" for x in r) else None)(
            self.call("GET", f"/api/outlets/destinations/{destination['id']}/deliveries")[1]["deliveries"]),
            "both events to be sent")
        self.assertEqual([r["event"] for r in rows], ["run.finished", "run.started"])
        by_event = {r["headers"]["Mobster-Event"]: r for r in receiver.requests}
        self.assertEqual(set(by_event), {"run.started", "run.finished"})
        for event, request in by_event.items():
            self.assertTrue(verify(SECRET, request["headers"], request["body"]), event)
        started = json.loads(by_event["run.started"]["body"])
        self.assertEqual(set(started), {"type", "runId", "startedAt", "workflow", "text"})
        self.assertEqual((started["type"], started["runId"]), ("run.started", run_id))
        self.assertNotIn("dinner", json.dumps(started).casefold(), "a start never carries the task's words")
        self.assertIn("214 Pine St", json.loads(by_event["run.finished"]["body"])["answer"])
        self.assertEqual([d["event"] for d in self.deliveries(run_id)], ["run.finished"])

    def test_needs_you_says_what_the_card_asks_and_never_the_message(self):
        receiver = Receiver(self)
        self.add_events(receiver.url, "Pager", ["needs_you"])
        thread, run_id = self.start("Text Alex that I'm running late")
        approval = self.asked(run_id, "commit")
        request = self.until(lambda: next((r for r in receiver.requests
                                           if r["headers"]["Mobster-Event"] == "needs_you"), None), "needs_you")
        body = json.loads(request["body"])
        self.assertTrue(verify(SECRET, request["headers"], request["body"]))
        self.assertEqual(set(body), {"type", "runId", "kind", "question", "askedAt", "workflow", "text"})
        self.assertEqual((body["type"], body["runId"], body["kind"]), ("needs_you", run_id, "approval"))
        self.assertEqual(body["question"], approval["title"])
        self.assertIn("Alex", body["question"])
        self.assertNotIn(approval["text"] or "running late, sorry", request["body"].decode())
        self.call("POST", f"/api/runs/{run_id}/approval", {"id": approval["id"], "approve": False}, origin=None)
        self.finished(run_id)
        self.assertEqual(self.phone["sent"], {})
        self.assertEqual([r["headers"]["Mobster-Event"] for r in receiver.requests], ["needs_you"])

    def test_events_are_refused_for_kinds_that_take_results_only(self):
        status, data = self.call("POST", "/api/outlets/destinations", {
            "name": "Results", "kind": "folder", "folder": "~/Documents", "events": ["needs_you"]})
        self.assertEqual(status, 400, data)
        self.assertIn("results only", data["error"])
        status, data = self.call("POST", "/api/outlets/destinations", {"name": "X", "url": "https://example.com/h",
                                                                       "events": ["run.paused"]})
        self.assertEqual(status, 400, data)
        status, data = self.call("POST", "/api/outlets/destinations", {"name": "X", "url": "https://example.com/h",
                                                                       "scope": "some"})
        self.assertEqual(status, 400, data)

    def test_the_log_and_its_limit(self):
        receiver = Receiver(self, plan=[404])
        destination = self.add_events(receiver.url, "Log", ["run.finished"])
        self.assertEqual(self.call("GET", f"/api/outlets/destinations/{destination['id']}/deliveries")[1],
                         {"deliveries": []})
        run_id = self.task()
        self.finished(run_id)
        [row] = self.until(lambda: (lambda r: r if r and r[0]["status"] == "failed" else None)(
            self.call("GET", f"/api/outlets/destinations/{destination['id']}/deliveries")[1]["deliveries"]),
                           "a failure")
        self.assertEqual((row["event"], row["httpStatus"], row["attempts"]), ("run.finished", 404, 1))
        self.assertEqual(self.call("GET", f"/api/outlets/destinations/{destination['id']}/deliveries?limit=x")[0], 400)
        self.assertEqual(self.call("GET", "/api/outlets/destinations/000000000000/deliveries")[0], 404)
        self.assertNotIn("topsecret", json.dumps(row))


class PresetTests(Harness):
    def test_zapier_and_make_addresses_are_checked_and_n8n_takes_any(self):
        zapier = self.service.add({"name": "Zapier", "url": ZAPIER_URL, "preset": "zapier"})
        self.assertEqual((zapier["kind"], zapier["config"]), ("webhook", {"preset": "zapier"}))
        make = self.service.add({"name": "Make", "url": MAKE_URL, "preset": "make"})
        self.assertEqual(make["config"]["preset"], "make")
        n8n = self.service.add({"name": "n8n", "url": "https://flows.example.org/webhook/abc", "preset": "n8n"})
        self.assertEqual(n8n["config"]["preset"], "n8n")
        for preset, url, words in (("zapier", "https://example.com/hooks/catch/1", "hooks.zapier.com"),
                                   ("make", "https://hooks.zapier.com/hooks/catch/1", "make.com"),
                                   ("make", "http://hook.eu2.make.com/x", "https://")):
            with self.subTest(preset=preset, url=url):
                with self.assertRaises(ValueError) as caught:
                    self.service.add({"name": f"Bad {preset}", "url": url, "preset": preset})
                self.assertIn(words, str(caught.exception))
        with self.assertRaises(ValueError):
            self.service.add({"name": "Other", "url": ZAPIER_URL, "preset": "ifttt"})
        with self.assertRaises(ValueError):
            self.service.update(zapier["id"], {"url": "https://example.com/hook"})
        with self.assertRaises(ValueError):
            self.service.update(zapier["id"], {"preset": "make"})
        changed = self.service.update(zapier["id"], {"url": "https://hooks.zapier.com/hooks/catch/9/z/",
                                                     "events": ["run.finished", "needs_you"], "scope": "all"})
        self.assertEqual((changed["events"], changed["scope"], changed["config"]["preset"]),
                         (["run.finished", "needs_you"], "all", "zapier"))


class ListenerTests(Harness):
    def make(self):
        self.posted = []
        service = outlet_service.OutletService(self.runtime, vault=self.vault, call=self.http, backoff=(0.02,),
                                               attempts=3, post=self.post)
        self.addCleanup(service.close)
        return service

    def post(self, url, data, headers):
        self.posted.append((url, headers.get("Mobster-Event")))
        return webhook.Outcome(True, False, 200)

    def setUp(self):
        super().setUp()
        self.runtime.runs = {self.run.id: self.run}
        outlet_service._services.add(self.runtime, self.service)
        self.addCleanup(outlet_service._services.pop, self.runtime)

    def add(self, name, events, scope="all", **more):
        return self.service.add({"name": name, "url": f"https://example.com/{name}", "events": events, "scope": scope,
                                 **more})

    def test_who_gets_which_event(self):
        every = self.add("every", ["run.started", "needs_you", "run.finished"])
        results = self.add("results", ["run.finished"])
        named = self.add("named", ["needs_you"], scope="named")
        quiet = self.add("quiet", ["run.started"], scope="named")
        self.run.extras["sendTo"]["destination"] = named["id"]
        listener = outlet_service.Listener()
        listener.on_created(self.run, self.runtime)
        listener.on_event(self.run, {"event": "approval_requested", "kind": "clarify",
                                     "label": "Which Sam: Sam Lee or Sam Ortiz?", "title": None})
        listener.on_event(self.run, {"event": "approval_requested", "kind": "commit", "label": "Send",
                                     "preapproved": "review_queue"})
        listener.on_event(self.run, {"event": "step", "kind": "commit"})
        listener.on_finished(self.run, {}, {})
        got = {}
        for row in self.service.store.deliveries_for(self.run.id):
            got.setdefault(row["destination_id"], []).append(row["event"])
        self.assertEqual(got[every["id"]], ["run.started", "needs_you", "run.finished"])
        self.assertEqual(got[results["id"]], ["run.finished"])
        self.assertEqual(got[named["id"]], ["needs_you", "run.finished"],
                         "the destination a task names gets its result")
        self.assertNotIn(quiet["id"], got)
        needs = next(r for r in self.service.store.deliveries_for(self.run.id) if r["event"] == "needs_you")
        self.assertEqual(needs["payload"]["kind"], "question")
        self.assertEqual(needs["payload"]["question"], "Which Sam: Sam Lee or Sam Ortiz?")
        self.assertEqual(needs["payload"]["workflow"]["name"], "Lead check")

    def test_a_stored_event_resends_after_its_run_is_gone(self):
        destination = self.add("every", ["run.started"])
        outlet_service.Listener().on_created(self.run, self.runtime)
        [row] = self.service.store.deliveries_for(self.run.id)
        deadline = time.monotonic() + 5
        while self.service.store.delivery(row["id"])["status"] == "sending" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.runtime.runs.clear()
        again = self.service.resend(row["id"])
        self.assertEqual((again["event"], again["destinationId"]), ("run.started", destination["id"]))
        self.assertEqual([h["event"] for h in self.service.history(destination["id"])], ["run.started", "run.started"])

    def test_retries_pause_with_jitter(self):
        pauses = []
        service = outlet_service.OutletService(self.runtime, vault=self.vault, call=self.http, backoff=(10.0,),
                                               attempts=3, rand=lambda: 1.0,
                                               post=lambda *_: webhook.Outcome(False, True, 503,
                                                                               "The address answered 503."))
        self.addCleanup(service.close)
        service._schedule = lambda delivery_id, when: pauses.append(when - service.clock())
        destination = service.add({"name": "flaky", "url": "https://example.com/flaky"})
        row = service.store.add_delivery({"id": "d1d1d1d1d1d1", "run_id": self.run.id,
                                          "destination_id": destination["id"], "destination_name": "flaky",
                                          "status": "sending", "attempts": 0, "created_at": 1, "updated_at": 1})
        service._attempt(row["id"])
        self.assertAlmostEqual(pauses[0], 12.0, delta=0.5)
        self.assertEqual(service.store.delivery(row["id"])["attempts"], 1)


class PayloadTests(unittest.TestCase):
    def test_needs_you_kinds_and_masking(self):
        run = SimpleNamespace(id="a1b2c3d4e5f6", created_at=1760000000.0)
        body = webhook.needs_payload(run, {"kind": "plan", "label": "Start this plan?"}, None, 1760000001.0)
        self.assertEqual((body["kind"], body["question"], body["askedAt"]), ("plan", "Start this plan?",
                                                                             "2025-10-09T08:53:21Z"))
        self.assertTrue(body["text"].startswith("Mobster needs you: "))
        masked = webhook.needs_payload(run, {"kind": "action", "title": "Type 551203 into the code field?"})
        self.assertNotIn("551203", json.dumps(masked))
        self.assertEqual(webhook.needs_payload(run, {"kind": "action"})["question"],
                         "Mobster needs your answer in the app.")

    def test_subscription_defaults(self):
        self.assertEqual(outlet_kinds.subscription({}, "folder"), {"events": ["run.finished"], "scope": "named"})
        self.assertEqual(outlet_kinds.subscription({"events": ["needs_you", "run.started"]}, "slack")["events"],
                         ["run.started", "needs_you"])

    def test_v2_rows_migrate_to_v3(self):
        connection = sqlite3.connect(":memory:")
        outlet_store.migrate(connection, 0)
        connection.execute("INSERT INTO outlet_deliveries (id, run_id, destination_id, destination_name, status, "
                           "attempts, created_at, updated_at) VALUES ('d', 'r', 'x', 'X', 'sent', 1, 1, 1)")
        row = connection.execute(f"SELECT {', '.join(outlet_store.DELIVERY_COLUMNS)} FROM outlet_deliveries")
        self.assertEqual(outlet_store._delivery(row.fetchone())["event"], "run.finished")
        indexes = {r[1] for r in connection.execute("PRAGMA index_list(outlet_deliveries)")}
        self.assertIn("outlet_deliveries_destination", indexes)


if __name__ == "__main__":
    unittest.main()


class QueueDraftEndToEnd(Service):
    """extras.queueDraft through the real Runtime and the scripted phone: the result lands in the queue as a draft."""

    def setUp(self):
        super().setUp()
        status, data = self.call("POST", "/api/accounts", {"handle": "getmobster", "app": {
            "bundleId": "com.burbn.instagram", "name": "Instagram"}})
        self.assertEqual(status, 201, data)
        self.profile = data["account"]["id"]
        status, data = self.call("POST", "/api/queue", {"name": "Spring"})
        self.assertEqual(status, 201, data)
        self.queue = data["queue"]["id"]

    def draft_run(self, goal=ASK, **fields):
        status, data = self.call("POST", "/api/runs", {
            "goal": goal, "appId": "any", "mode": "live",
            "queueDraft": {"queue": self.queue, "profile": self.profile}, **fields})
        self.assertIn(status, (200, 201), data)
        return data["run"]["id"]

    def items(self):
        return self.call("GET", f"/api/queue/{self.queue}")[1]["queue"]["items"]

    def test_a_finished_task_leaves_its_result_as_one_draft(self):
        run_id = self.draft_run()
        run = self.finished(run_id)
        self.assertEqual(run["status"], "completed")
        [draft] = self.until(self.items, "the draft")
        self.assertEqual((draft["state"], draft["media"], draft["profile"], draft["note"]),
                         ("draft", [], self.profile, "Add photos"))
        self.assertIn("214 Pine St", draft["caption"])

    def test_a_task_that_names_no_queue_or_a_missing_one_adds_nothing(self):
        status, data = self.call("POST", "/api/runs", {"goal": ASK, "appId": "any", "mode": "live",
                                                       "queueDraft": {"queue": "b" * 12, "profile": self.profile}})
        self.assertEqual(status, 400, data)
        self.finished(self.task_without_field())
        self.assertEqual(self.items(), [])

    def task_without_field(self):
        status, data = self.call("POST", "/api/runs", {"goal": ASK, "appId": "any", "mode": "live"})
        self.assertIn(status, (200, 201), data)
        return data["run"]["id"]

import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import harness_api
from mobile_agent.outlets import hooks
from mobile_agent.permissions import policy
from mobile_agent.tests.test_outlets import OutletsBase
from mobile_agent.workflows import records as batch

BASE = "https://remote.mobster.dev/hooks/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


class FakeRemote:
    def __init__(self, base=BASE):
        self.base, self.told = base, []

    def address(self, prefix):
        return self.base

    def hooks_changed(self, ids):
        self.told.append(list(ids))


class HookBase(OutletsBase):
    def setUp(self):
        super().setUp()
        self.remote = FakeRemote()
        self.service = hooks.service_for(self.runtime)
        self.service._remote = self.remote

    def save(self, goal="Text {person: Alex} that I'm running late"):
        status, data = self.call("POST", "/api/workflows", {"name": "Running late", "appId": None, "engine": "smart",
                                                            "goal": goal})
        self.assertEqual(status, 201, data)
        return data["workflow"]

    def make(self, workflow_id):
        status, data = self.call("POST", "/api/outlets/hooks", {"workflowId": workflow_id})
        self.assertEqual(status, 201, data)
        return data["hook"]

    def send(self, hook, body, *, secret=None, at=None, signature=None):
        text = body if isinstance(body, str) else json.dumps(body)
        signed = signature if signature is not None else hooks.sign(secret or hook["secret"], at or time.time(), text)
        return self.service.handle(hook["id"], signed, text)


class MakeAndRevoke(HookBase):
    def test_a_hook_is_remotes_url_with_a_secret_shown_once(self):
        workflow = self.save()
        hook = self.make(workflow["id"])
        self.assertRegex(hook["id"], r"^[0-9a-f]{32}$")
        self.assertEqual(hook["url"], f"{BASE}/{hook['id']}")
        self.assertRegex(hook["secret"], r"^whsec_[0-9a-f]{64}$")
        self.assertEqual(self.remote.told, [[hook["id"]]])
        status, data = self.call("GET", f"/api/outlets/hooks?workflowId={workflow['id']}")
        self.assertEqual(status, 200, data)
        [listed] = data["hooks"]
        self.assertNotIn("secret", listed)
        self.assertEqual((listed["id"], listed["workflowId"], listed["uses"]), (hook["id"], workflow["id"], 0))
        self.assertEqual(self.service.vault.get(hook["id"]), hook["secret"])

    def test_revoking_forgets_the_secret_and_tells_the_relay(self):
        hook = self.make(self.save()["id"])
        status, data = self.call("DELETE", f"/api/outlets/hooks/{hook['id']}")
        self.assertEqual((status, data), (200, {"id": hook["id"]}))
        self.assertIsNone(self.service.vault.get(hook["id"]))
        self.assertEqual(self.remote.told[-1], [])
        self.assertEqual(self.send(hook, {})["status"], 404)
        self.assertEqual(self.call("DELETE", f"/api/outlets/hooks/{hook['id']}")[0], 404)

    def test_no_url_without_remote(self):
        self.remote.base = None
        status, data = self.call("POST", "/api/outlets/hooks", {"workflowId": self.save()["id"]})
        self.assertEqual((status, data["code"]), (409, "remote_off"))
        self.assertIn("Mobster Remote", data["error"])
        self.assertEqual(self.service.store.all(), [])

    def test_an_unknown_workflow_makes_nothing(self):
        status, data = self.call("POST", "/api/outlets/hooks", {"workflowId": "0" * 12})
        self.assertEqual(status, 404, data)
        status, data = self.call("POST", "/api/outlets/hooks", {"workflowId": "../etc"})
        self.assertEqual(status, 400, data)

    def test_a_removed_workflows_hooks_go_with_it(self):
        workflow = self.save()
        hook = self.make(workflow["id"])
        self.assertEqual(self.call("DELETE", f"/api/workflows/{workflow['id']}")[0], 200)
        self.assertEqual(self.call("GET", "/api/outlets/hooks")[1]["hooks"], [])
        self.assertIsNone(self.service.vault.get(hook["id"]))

    def test_the_app_alone_makes_and_revokes(self):
        from mobile_agent import server
        workflow = self.save()
        hook = self.make(workflow["id"])
        self.runtime.local_api = {"app_session": True}
        httpd = server.BoundedServer(("127.0.0.1", 0),
                                     server.make_handler(self.runtime, "e" * 43, app_session="s" * 43))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.url = f"http://127.0.0.1:{httpd.server_address[1]}"
        status, data = self.call("POST", "/api/outlets/hooks", {"workflowId": workflow["id"]})
        self.assertEqual((status, data["code"]), (403, "app_only"))
        status, data = self.call("DELETE", f"/api/outlets/hooks/{hook['id']}")
        self.assertEqual((status, data["code"]), (403, "app_only"))
        self.assertEqual(len(self.call("GET", "/api/outlets/hooks")[1]["hooks"]), 1)


class Calls(HookBase):
    @staticmethod
    def event(identifier="order-1", occurred_at=None, **values):
        return {"_mobsterEvent": {"version": 1, "id": identifier,
                                   "occurredAt": occurred_at if occurred_at is not None else round(time.time() * 1000)},
                "values": {"person": "Sam", **values}}

    def test_a_signed_call_starts_the_workflow_with_its_blanks(self):
        workflow = self.save()
        hook = self.make(workflow["id"])
        answer = self.send(hook, {"Person": "Sam", "color": "blue"})
        self.assertEqual(answer["status"], 202, answer)
        self.assertEqual((answer["filled"], answer["ignored"]), (["person"], ["color"]))
        self.assertEqual(answer["message"], "Started Running late.")
        run = self.until(lambda: next(iter(self.runtime.runs.values()), None), "the workflow's run")
        self.assertEqual(run.origin, "hook")
        self.assertIn("Sam", run.goal)
        [row] = self.call("GET", "/api/outlets/hooks")[1]["hooks"]
        self.assertEqual((row["uses"], row["lastStatus"]), (1, 202))

    def test_blanks_nested_and_values_cleaned(self):
        hook = self.make(self.save()["id"])
        answer = self.send(hook, {"blanks": {"person": "  Sam\n Lee  "}, "person": "ignored"})
        self.assertEqual(answer["status"], 202, answer)
        run = self.until(lambda: next(iter(self.runtime.runs.values()), None), "the workflow's run")
        self.assertIn("Sam Lee", run.goal)

    def test_an_empty_body_takes_the_defaults(self):
        hook = self.make(self.save()["id"])
        self.assertEqual(self.send(hook, "")["status"], 202)
        run = self.until(lambda: next(iter(self.runtime.runs.values()), None), "the workflow's run")
        self.assertIn("Alex", run.goal)

    def test_legacy_blank_names_that_resemble_event_fields_still_work(self):
        hook = self.make(self.save("Report {eventId}, {occurredAt} and {values}")["id"])
        answer = self.send(hook, {"eventId": "order-1", "occurredAt": "today", "values": "three"})
        self.assertEqual(answer["status"], 202, answer)
        self.assertEqual(answer["filled"], ["eventId", "occurredAt", "values"])

    def test_what_starts_nothing(self):
        hook = self.make(self.save()["id"])
        body = json.dumps({"person": "Sam"})
        now = time.time()
        cases = {
            "unsigned": (401, self.service.handle(hook["id"], "", body)),
            "malformed": (401, self.service.handle(hook["id"], "v1=abc", body), "unsigned"),
            "stale": (401, self.send(hook, body, at=now - 301)),
            "future": (401, self.send(hook, body, at=now + 301), "stale"),
            "bad_signature": (401, self.send(hook, body, secret="whsec_" + "0" * 64)),
            "too_large": (413, self.send(hook, "x" * (hooks.MAX_BODY + 1))),
            "not_json": (422, self.send(hook, "[1, 2]")),
            "not_found": (404, self.service.handle("f" * 32, hooks.sign(hook["secret"], now, body), body)),
        }
        for error, (status, answer, *code) in cases.items():
            self.assertEqual((answer["status"], answer["error"]), (status, code[0] if code else error), error)
        self.assertEqual(self.runtime.runs, {})

    def test_a_replay_starts_nothing_again(self):
        hook = self.make(self.save()["id"])
        body = json.dumps({"person": "Sam"})
        signature = hooks.sign(hook["secret"], time.time(), body)
        self.assertEqual(self.service.handle(hook["id"], signature, body)["status"], 202)
        again = self.service.handle(hook["id"], signature, body)
        self.assertEqual((again["status"], again["error"]), (409, "replayed"))
        self.until(lambda: len(self.runtime.runs) == 1 or None, "one run")
        time.sleep(0.1)
        self.assertEqual(len(self.runtime.runs), 1)

    def test_stable_event_id_refuses_a_new_signature_and_changed_content(self):
        hook = self.make(self.save()["id"])
        event = self.event()
        self.assertEqual(self.send(hook, event)["status"], 202)
        again = self.send(hook, event, at=time.time() + 1)
        self.assertEqual((again["status"], again["error"]), (409, "replayed"))
        changed = self.send(hook, self.event(person="Lee", occurred_at=event["_mobsterEvent"]["occurredAt"]),
                            at=time.time() + 2)
        self.assertEqual((changed["status"], changed["error"]), (409, "event_changed"))
        self.assertEqual(len(self.runtime.runs), 1)

    def test_event_values_keep_graph_input_types(self):
        graph = {"version": 2, "inputs": [{"name": "count", "type": "number", "required": True},
                                             {"name": "active", "type": "boolean", "default": False}],
                 "nodes": [{"id": "start", "type": "trigger.webhook", "position": {"x": 0, "y": 0}},
                           {"id": "read", "type": "phone.task", "position": {"x": 0, "y": 150},
                            "config": {"text": "Read About"}}],
                 "edges": [{"id": "e1", "source": "start", "sourcePort": "out", "target": "read",
                            "targetPort": "in"}]}
        workflow = self.runtime.workflows.create({"name": "Typed", "graph": graph})
        hook = self.make(workflow["id"])
        with patch.object(self.runtime.workflows, "run") as run:
            answer = self.send(hook, {"_mobsterEvent": {"version": 1, "id": "typed-1",
                                                         "occurredAt": round(time.time() * 1000)},
                                      "values": {"count": 0, "active": False}})
        self.assertEqual(answer["status"], 202, answer)
        self.assertEqual(run.call_args.args[2], {"count": 0, "active": False})

    def test_malformed_events_leave_no_event_claim(self):
        hook = self.make(self.save()["id"])
        malformed = self.event()
        del malformed["_mobsterEvent"]["occurredAt"]
        cases = [(malformed, 422, "invalid_event"),
                 (self.event(occurred_at=-1), 422, "invalid_event"),
                 (self.event(unknown="field"), 422, "invalid_event")]
        for payload, status, code in cases:
            with self.subTest(code=code):
                answer = self.send(hook, payload)
                self.assertEqual((answer["status"], answer["error"]), (status, code))
        with self.runtime.journal.lock:
            count = self.runtime.journal.connection.execute("SELECT count(*) FROM outlet_hook_events").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(self.runtime.runs, {})

    def test_expired_event_claim_cannot_be_resurrected_with_a_fresh_timestamp(self):
        hook = self.make(self.save()["id"])
        expired = self.event(occurred_at=round((time.time() - 301) * 1000))
        answer = self.send(hook, expired)
        self.assertEqual((answer["status"], answer["error"]), (410, "event_expired"))
        refreshed = self.event(occurred_at=round(time.time() * 1000))
        replay = self.send(hook, refreshed, at=time.time() + 1)
        self.assertEqual((replay["status"], replay["error"]), (409, "event_changed"))
        with self.runtime.journal.lock:
            count = self.runtime.journal.connection.execute("SELECT count(*) FROM outlet_hook_events").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.runtime.runs, {})

    def test_an_event_that_expires_waiting_for_workflow_admission_is_consumed(self):
        hook = self.make(self.save()["id"])
        event = self.event()
        deadline = event["_mobsterEvent"]["occurredAt"] + 300_001
        with patch.object(self.runtime.workflows, "now", return_value=deadline):
            answer = self.send(hook, event)
        self.assertEqual((answer["status"], answer["error"]), (410, "event_expired"))
        self.assertEqual(self.send(hook, event, at=time.time() + 1)["error"], "replayed")
        self.assertEqual(self.runtime.runs, {})

    def test_a_nested_child_batch_counts_as_an_active_workflow(self):
        workflow = self.save()
        hook = self.make(workflow["id"])
        child = batch.new_record(workflow, f"workflow:{workflow['id']}:call:parent", None,
                                 round(time.time() * 1000))
        self.runtime.workflows.batches.add(child)
        answer = self.send(hook, self.event())
        self.assertEqual((answer["status"], answer["error"]), (409, "workflow_overlap"))
        self.assertEqual(self.runtime.runs, {})

    def test_revocation_during_event_validation_refuses_before_claim_or_start(self):
        hook = self.make(self.save()["id"])
        parse = hooks.event_envelope

        def revoke_then_parse(*args):
            self.service.revoke(hook["id"])
            return parse(*args)

        with patch.object(hooks, "event_envelope", side_effect=revoke_then_parse):
            answer = self.send(hook, self.event())
        self.assertEqual((answer["status"], answer["error"]), (404, "not_found"))
        with self.runtime.journal.lock:
            count = self.runtime.journal.connection.execute("SELECT count(*) FROM outlet_hook_events").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(self.runtime.runs, {})

    def test_busy_event_is_consumed_and_never_retried_after_the_busy_claim_goes(self):
        workflow = self.save()
        hook = self.make(workflow["id"])
        with self.runtime.journal.transaction() as connection:
            connection.execute("INSERT INTO workflow_dispatches VALUES (?,?,?,?,?,NULL)",
                               (f"workflow:{workflow['id']}:scheduled:1", workflow["id"], None,
                                round(time.time() * 1000), "claimed"))
        event = self.event()
        answer = self.send(hook, event)
        self.assertEqual((answer["status"], answer["error"]), (409, "workflow_overlap"))
        with self.runtime.journal.transaction() as connection:
            connection.execute("DELETE FROM workflow_dispatches WHERE workflow_id=?", (workflow["id"],))
        replay = self.send(hook, event, at=time.time() + 1)
        self.assertEqual((replay["status"], replay["error"]), (409, "replayed"))
        self.assertEqual(self.runtime.runs, {})

    def test_unknown_event_dispatch_and_capacity_refuse_without_a_second_start(self):
        hook = self.make(self.save()["id"])
        event = self.event()
        with patch.object(self.runtime.workflows, "run", side_effect=RuntimeError("unknown")) as run:
            self.assertEqual(self.send(hook, event)["status"], 500)
            self.assertEqual(self.send(hook, event, at=time.time() + 1)["error"], "replayed")
            self.assertEqual(run.call_count, 1)
        with patch.object(hooks, "MAX_EVENTS", 1):
            answer = self.send(hook, self.event("order-2"))
        self.assertEqual((answer["status"], answer["error"]), (503, "event_limit"))
        self.assertEqual(self.runtime.runs, {})

    def test_rate_limited_per_hook(self):
        hook = self.make(self.save("Open Notes")["id"])
        statuses = [self.send(hook, {"n": n})["status"] for n in range(hooks.RATE + 1)]
        self.assertEqual(statuses[-1], 429)
        self.assertNotIn(429, statuses[:hooks.RATE])

    def test_a_removed_workflow_answers_gone(self):
        workflow = self.save()
        hook = self.make(workflow["id"])
        with self.runtime.journal.transaction() as connection:
            connection.execute("DELETE FROM workflows WHERE id=?", (workflow["id"],))
        answer = self.send(hook, {})
        self.assertEqual((answer["status"], answer["error"]), (410, "workflow_gone"))

    def test_a_secret_the_keychain_lost_starts_nothing(self):
        hook = self.make(self.save()["id"])
        self.service.vault.delete(hook["id"])
        self.assertEqual(self.send(hook, {})["status"], 503)
        self.assertEqual(self.runtime.runs, {})


class Safety(unittest.TestCase):
    def test_hook_runs_always_ask_and_never_read_notifications_or_connectors(self):
        self.assertIn("hook", harness_api.ORIGINS)
        self.assertIn("hook", harness_api.FORCED_ASK_ORIGINS)
        self.assertIn("hook", harness_api.UNATTENDED_ORIGINS)
        self.assertIn("hook", policy.OUTSIDE_ORIGINS)
        self.assertEqual(harness_api.WITHHELD_SKILLS["hook"], {"READ_NOTIFICATIONS", "CALL_TOOL"})
        self.assertEqual(harness_api.steer_sources("hook"), ("app",))

    def test_asks_before_acting_follows_the_origin(self):
        from mobile_agent.server import Runtime
        runtime = SimpleNamespace(ask_before_acting=lambda: False)
        ask = Runtime.asks_before_acting
        self.assertTrue(ask(runtime, SimpleNamespace(origin="hook", ask_override=False)))
        self.assertFalse(ask(runtime, SimpleNamespace(origin="app", ask_override=None)))






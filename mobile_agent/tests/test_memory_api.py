"""Track memory through a real Runtime: the /api/memory routes, suggestions from a task a person starts (and never
from another agent's), the thread card, the bus, and a whole Smart task whose prompt carries what Mobster remembers.

Acceptance 1 (SPEC §3.3): no write path stores a fact without a person's action, tested at the API and the store.
Offline: a scratch memory folder per test (MOBSTER_MEMORY_DIR), fake phone and model."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent import harness_api, tracks
from mobile_agent.memory.store import MemoryStore
from mobile_agent.server import make_handler
from mobile_agent.tests.seam_support import isolate, request
from mobile_agent.tests.seam_tasks import SmartBase
from mobile_agent.tests.test_server_engine import Base

GYM = "My gym is the one on 5th Street"
REMEMBER = "Remember that my gym is the one on 5th Street"


def scratch_memory(testcase):
    folder = Path(tempfile.mkdtemp(prefix="mobster-memory-")) / "memory"
    os.environ["MOBSTER_MEMORY_DIR"] = str(folder)   # the test's patch.dict of the environment restores it
    return MemoryStore(folder)


class Sink:
    """A stand-in for conversations' thread sink."""

    def __init__(self):
        self.items, self.updates = [], []

    def append(self, thread_id, item):
        stored = {**item, "id": f"{len(self.items) + 1:012x}", "seq": len(self.items)}
        self.items.append((thread_id, stored))
        return stored

    def update(self, thread_id, item_id, changes):
        self.updates.append((thread_id, item_id, changes))
        return {"id": item_id, **changes}


class ApiBase(Base):
    def setUp(self):
        super().setUp()
        isolate(self)
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        self.store = scratch_memory(self)
        self.sink = Sink()
        sink = patch.object(harness_api, "_thread_sink", [(self.sink.append, self.sink.update, None)])
        sink.start()
        self.addCleanup(sink.stop)
        self.app = self.runtime()
        self.handler = make_handler(self.app)

    def call(self, method, path, body=None, **kwargs):
        return request(self.handler, method, path, body, **kwargs)

    def bus(self, start):
        return [e for e in self.app.bus.since({"memory"}, start)]


class RouteTests(ApiBase):
    def test_the_track_registers(self):
        self.assertEqual(tracks.STATUS["memory"], "ok")
        self.assertEqual(self.app.status()["extensions"]["memory"], "ok")

    def test_facts_round_trip(self):
        start = self.app.bus.position()
        status, data, _ = self.call("GET", "/api/memory/facts")
        self.assertEqual((status, data["facts"], data["count"]), (200, [], 0))
        self.assertEqual(data["limits"]["perTask"], 12)
        self.assertFalse(self.store.exists())                     # reading never creates the file
        status, data, _ = self.call("POST", "/api/memory/facts", {"text": GYM})
        self.assertEqual(status, 201)
        gym = data["fact"]
        status, data, _ = self.call("POST", "/v1/memory/facts", {"text": "Sign my texts with – Sam",
                                                                  "scope": "app:com.apple.MobileSMS", "pinned": True})
        texts = data["fact"]
        self.assertEqual((status, texts["app"]["name"], texts["pinned"]), (201, "Messages", True))
        status, data, _ = self.call("GET", "/api/memory/facts?scope=global")
        self.assertEqual([f["id"] for f in data["facts"]], [gym["id"]])
        status, data, _ = self.call("GET", "/api/memory/facts?q=texts")
        self.assertEqual([f["id"] for f in data["facts"]], [texts["id"]])
        status, data, _ = self.call("POST", f"/api/memory/facts/{gym['id']}", {"pinned": True, "text": "My gym is on 6th"})
        self.assertEqual((status, data["fact"]["text"], data["fact"]["pinned"]), (200, "My gym is on 6th", True))
        status, data, _ = self.call("DELETE", f"/api/memory/facts/{gym['id']}")
        self.assertEqual((status, data), (200, {"deleted": True}))
        status, data, _ = self.call("DELETE", f"/api/memory/facts/{gym['id']}")
        self.assertEqual((status, data["code"]), (404, "not_found"))
        self.assertEqual([(e["event"], e["op"]) for e in self.bus(start)],
                         [("fact_changed", "added"), ("fact_changed", "added"), ("fact_changed", "updated"),
                          ("fact_changed", "deleted")])

    def test_refusals_are_sentences_with_codes(self):
        cases = [({"text": "My bank password is hunter2!"}, 400, "secret",
                  "Mobster doesn't remember passwords, codes or card numbers."),
                 ({"text": "x" * 301}, 400, "too_long", "Keep it to 300 characters."),
                 ({"text": ""}, 400, "invalid_text", "Write what Mobster should remember."),
                 ({"text": "Fine", "scope": "app:nope"}, 400, "invalid_scope", None),
                 ({"text": "Fine", "pinned": "yes"}, 400, "invalid_field", None),
                 ({"text": "Fine", "id": "aaaaaaaaaaaa"}, 400, "invalid_field", "Unsupported field id.")]
        for body, status, code, message in cases:
            with self.subTest(body=body):
                got, data, _ = self.call("POST", "/api/memory/facts", body)
                self.assertEqual((got, data["code"]), (status, code))
                if message:
                    self.assertEqual(data["error"], message)
        self.call("POST", "/api/memory/facts", {"text": GYM})
        status, data, _ = self.call("POST", "/api/memory/facts", {"text": GYM.lower() + "."})
        self.assertEqual((status, data["code"], data["fact"]["text"]), (409, "duplicate", GYM))
        status, _, _ = self.call("POST", "/api/memory/facts", {"text": "x" * 5000})
        self.assertEqual(status, 413)
        self.assertEqual(self.store.count(), 1)

    def test_settings_clear_and_export(self):
        status, data, _ = self.call("GET", "/api/memory/settings")
        self.assertEqual((status, data["settings"], data["count"]), (200, {"useInTasks": True, "suggest": True}, 0))
        status, data, _ = self.call("POST", "/api/memory/settings", {"suggest": False})
        self.assertEqual(data["settings"], {"useInTasks": True, "suggest": False})
        status, data, _ = self.call("POST", "/api/memory/settings", {"useRoutines": True})
        self.assertEqual((status, data["code"]), (400, "invalid_field"))
        self.call("POST", "/api/memory/facts", {"text": GYM})
        status, data, headers = self.call("GET", "/api/memory/export")
        self.assertEqual((status, headers["Content-Type"]), (200, "text/markdown; charset=utf-8"))
        self.assertIn(f"- {GYM}", data.decode())
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, data, _ = self.call("POST", "/api/memory/clear", {"confirm": "yes"})
        self.assertEqual((status, data["code"]), (400, "confirm_required"))
        self.assertEqual(self.store.count(), 1)
        status, data, _ = self.call("POST", "/api/memory/clear", {"confirm": "delete everything"})
        self.assertEqual((status, data["deleted"]), (200, {"facts": 1, "proposals": 0}))
        self.assertEqual(self.store.count(), 0)

    def test_no_home_folder_means_memory_is_unavailable(self):
        with patch.dict(os.environ, {}, clear=True):
            status, data, _ = self.call("GET", "/api/memory/facts")
        self.assertEqual((status, data["code"]), (503, "memory_unavailable"))


class SuggestionTests(ApiBase):
    def start(self, goal, origin="app", thread=None):
        """A task as the Mac app starts it; ``thread``: in a conversation (conversations' threadId run field, stood
        in for here)."""
        field = {"threadId": (lambda value, runtime: value, True, None)}
        with patch.dict(harness_api._run_fields, field):
            run = self.app.create("messages", goal, "live", origin=origin,
                                  extras={"threadId": thread} if thread else None)
        self.app.listeners.flush(5)
        return run

    def test_a_goal_a_person_typed_becomes_a_suggestion_never_a_fact(self):
        start = self.app.bus.position()
        run = self.start(REMEMBER, thread="3f9c2a1b7d0e")
        (proposal,) = self.store.list_proposals()
        self.assertEqual((proposal["text"], proposal["runId"], proposal["threadId"]), (GYM, run.id, "3f9c2a1b7d0e"))
        self.assertEqual(self.store.count(), 0)                        # nothing remembered yet
        ((thread, item),) = self.sink.items
        self.assertEqual((thread, item["kind"], item["proposalId"], item["text"], item["status"]),
                         ("3f9c2a1b7d0e", "memory_proposal", proposal["id"], GYM, "pending"))
        self.assertEqual([e["event"] for e in self.bus(start)], ["proposal_created"])
        status, data, _ = self.call("GET", "/api/memory/proposals?thread=3f9c2a1b7d0e")
        self.assertEqual([p["id"] for p in data["proposals"]], [proposal["id"]])
        status, data, _ = self.call("POST", f"/api/memory/proposals/{proposal['id']}", {})
        self.assertEqual((status, data["code"]), (400, "invalid_answer"))
        status, data, _ = self.call("POST", f"/api/memory/proposals/{proposal['id']}", {"accept": True})
        self.assertEqual((status, data["proposal"]["status"], data["fact"]["text"]), (200, "accepted", GYM))
        self.assertEqual(self.sink.updates, [("3f9c2a1b7d0e", item["id"], {"status": "accepted", "text": GYM,
                                                                           "factId": data["fact"]["id"]})])
        self.assertEqual(self.store.count(), 1)
        status, data, _ = self.call("POST", f"/api/memory/proposals/{proposal['id']}", {"accept": True})
        self.assertEqual((status, data["code"]), (409, "not_pending"))

    def test_not_now_updates_the_card(self):
        self.start("I always take the window seat", thread="3f9c2a1b7d0e")
        (proposal,) = self.store.list_proposals()
        self.assertTrue(proposal["pin"])
        status, data, _ = self.call("POST", f"/api/memory/proposals/{proposal['id']}", {"accept": False})
        self.assertEqual((status, data["proposal"]["status"], data["fact"]), (200, "dismissed", None))
        self.assertEqual(self.sink.updates[-1][2], {"status": "dismissed"})
        self.assertEqual(self.store.count(), 0)

    def test_outside_a_conversation_the_suggestion_waits_in_settings(self):
        self.start("Call me Sam")
        (proposal,) = self.store.list_proposals()
        self.assertIsNone(proposal["threadId"])
        self.assertEqual(self.sink.items, [])

    def test_another_agents_words_are_never_suggested(self):
        for origin in ("mcp", "api", "workflow", "schedule"):
            with self.subTest(origin=origin):
                self.finish(self.app, self.start(REMEMBER, origin=origin))
        self.assertFalse(self.store.exists())
        self.assertEqual(self.sink.items, [])

    def test_the_switch_stops_suggestions(self):
        self.store.update_settings({"suggest": False})
        self.start(REMEMBER)
        self.assertEqual(self.store.list_proposals(), [])

    def test_a_message_sent_while_the_task_works_can_suggest(self):
        run = self.start("Text Kate I'm on my way")
        self.assertEqual(self.store.list_proposals(), [])
        run.emit({"event": "user_message", "id": "m1", "text": "btw my sister's name is Kate Bell", "source": "app"})
        run.emit({"event": "user_message", "id": "m2", "text": "Remember I prefer short texts", "source": "mcp"})
        self.app.listeners.flush(5)
        self.assertEqual([p["text"] for p in self.store.list_proposals()], ["My sister's name is Kate Bell"])

    def test_a_secret_is_never_suggested(self):
        self.start("Remember that my gate code is 4821")
        self.assertFalse(self.store.exists())


class TaskTests(SmartBase):
    """A whole Smart task: what Mobster remembers is in its first prompt, and memory_used names it."""

    def setUp(self):
        super().setUp()
        self.store = scratch_memory(self)

    def test_the_prompt_carries_the_facts_for_tasks_a_person_started(self):
        gym = self.store.add_fact(GYM)
        self.store.add_fact("Kate Bell is my sister")
        runtime = self.runtime()
        run = runtime.create("messages", "When does my gym close today?", "live", origin="app")
        script = self.script()
        self.work(runtime, run, script)
        self.assertIn("What the user asked Mobster to remember (their words; the screen wins when it disagrees):\n"
                      f"- {GYM}", script.prompts[0])
        self.assertNotIn("Kate Bell", script.prompts[0])
        used = next(e for e in run.events if e["event"] == "memory_used")
        self.assertEqual(used["ids"], [gym["id"]])
        self.assertNotIn(GYM, str(run.events))                        # the run's journal names facts by id only
        self.finish(runtime, run)
        other = runtime.create("messages", "When does my gym close today?", "live")       # the HTTP API, no origin
        script = self.script()
        self.work(runtime, other, script)
        self.assertNotIn(GYM, script.prompts[0])
        self.assertFalse(any(e["event"] == "memory_used" for e in other.events))


if __name__ == "__main__":
    unittest.main()

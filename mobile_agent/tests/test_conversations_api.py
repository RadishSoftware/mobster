"""Conversations over HTTP: the threads routes, the message routing matrix (idle, Smart running, Quick running, a
question waiting, an approval waiting), idempotent posts, limits, delete, and the bus relay. Offline: the task worker
is admission-only (a test runs a task itself when it needs one)."""

import os
import unittest
from unittest.mock import patch

from mobile_agent import server
from mobile_agent.tests.test_conversations_support import ThreadsBase, bus_events, clarify, commit
from mobile_agent.threads import service as threads_service
from mobile_agent.threads import store as thread_store


class ThreadRouteTests(ThreadsBase):
    def test_the_track_registers_and_the_status_says_so(self):
        runtime = self.runtime()
        self.assertEqual(runtime.status()["extensions"]["threads"], "ok")
        self.assertIsNotNone(self.service(runtime))
        status, data, _ = self.call(runtime, "GET", "/api/threads")
        self.assertEqual((status, data), (200, {"threads": [], "next": None}))

    def test_a_first_message_makes_a_thread_and_starts_a_task_in_it(self):
        runtime = self.runtime()
        since = runtime.bus.position()
        data = self.thread(runtime, "Find my last message from Kate Bell and draft a reply")
        thread, item, run = data["thread"], data["item"], data["run"]
        self.assertRegex(thread["id"], r"^[a-f0-9]{12}$")
        self.assertEqual(thread["title"], "Find my last message from Kate Bell and draft a reply")
        self.assertEqual((thread["status"], thread["lastRunId"]), ("running", run["id"]))
        self.assertEqual((item["kind"], item["routed"], item["runId"], item["via"]), ("user", "new_run", run["id"], "cli"))
        self.assertEqual(run["extras"], {"threadId": thread["id"]})  # via is not public
        live = runtime.runs[run["id"]]
        self.assertEqual((live.extras["via"], live.origin), ("cli", "api"))
        self.settle(runtime)
        items = self.items(runtime, thread["id"])
        self.assertEqual([(i["kind"], i["seq"]) for i in items], [("user", 0), ("run", 1)])  # once, though two paths add it
        self.assertEqual(items[1]["goal"], "Find my last message from Kate Bell and draft a reply")
        self.assertNotIn("_context", items[1])
        events = bus_events(runtime, "threads", since)
        self.assertEqual(events[0]["event"], "thread_created")
        topic = bus_events(runtime, f"thread:{thread['id']}", since)
        self.assertEqual([e["event"] for e in topic][:2], ["thread_item", "thread_item"])

    def test_the_app_sends_its_origin_and_may_say_voice(self):
        runtime = self.runtime()
        status, data, _ = self.call(runtime, "POST", "/api/threads", {"message": {"text": "What's on my calendar?",
                                                                                   "via": "voice"}},
                                    headers={"X-Mobster-Origin": "app"})
        self.assertEqual(status, 201, data)
        self.assertEqual(data["item"]["via"], "voice")
        self.assertEqual(runtime.runs[data["run"]["id"]].origin, "app")
        status, data, _ = self.call(runtime, "POST", "/api/threads", {"message": {"text": "x", "via": "voice"}},
                                    headers={"X-Mobster-Origin": "cli"})
        self.assertEqual(status, 409)  # the phone is busy with the first task; nothing else matters here

    def test_a_task_a_person_starts_in_a_thread_may_ask_them_a_question(self):
        # Every surface that shows a conversation shows its questions inline, so a task started there from the Mac
        # app, `mobster chat` or the terminal UI says so (askUser, harness's ASK_USER gate). Without it no thread
        # task could ever ask "Which Sam?" (QA, 7 Oct). MCP and the plain API: nobody is watching.
        from mobile_agent import harness_api
        runtime = self.runtime()
        self.assertIn("askUser", harness_api.run_fields())
        for origin, asks in (("app", True), ("cli", True), ("tui", True), ("mcp", None), (None, None)):
            with self.subTest(origin=origin):
                headers = {"X-Mobster-Origin": origin} if origin else None
                status, data, _ = self.call(runtime, "POST", "/api/threads", {"message": {"text": "Text Sam I'm here"}},
                                            headers=headers)
                self.assertEqual(status, 201, data)
                run = runtime.runs[data["run"]["id"]]
                self.assertEqual(run.extras.get("askUser"), asks)
                self.finish(runtime, run)

    def test_a_follow_up_after_the_task_finished_starts_a_new_task_in_the_same_thread(self):
        runtime = self.runtime()
        data = self.thread(runtime)
        first = runtime.runs[data["run"]["id"]]
        self.finish(runtime, first)
        self.settle(runtime)
        status, answer, _ = self.call(runtime, "POST", f"/api/threads/{data['thread']['id']}/messages",
                                      {"text": "Now reply to that"})
        self.assertEqual(status, 201, answer)
        self.assertNotEqual(answer["run"]["id"], first.id)
        self.assertEqual(runtime.runs[answer["run"]["id"]].extras["threadId"], data["thread"]["id"])
        self.settle(runtime)
        kinds = [(i["kind"], i.get("routed")) for i in self.items(runtime, data["thread"]["id"])]
        self.assertEqual(kinds, [("user", "new_run"), ("run", None), ("user", "new_run"), ("run", None)])

    def test_the_threads_device_wins_when_the_message_names_none(self):
        runtime = self.runtime()
        data = self.thread(runtime)
        self.finish(runtime, runtime.runs[data["run"]["id"]])
        thread_id = data["thread"]["id"]
        self.service(runtime).store.update_thread(thread_id, {"device": "abc", "deviceName": "Sam's iPhone"})
        seen = []
        original = runtime.create

        def create(*args, **kwargs):
            seen.append(kwargs.get("device"))
            return original(*args, **{k: v for k, v in kwargs.items() if k != "device"})
        with patch.object(runtime, "create", create):
            self.call(runtime, "POST", f"/api/threads/{thread_id}/messages", {"text": "Again"})
        self.assertEqual(seen, ["abc"])


class RoutingTests(ThreadsBase):
    def started(self, runtime, engine=None):
        body = {"message": {"text": "Find my last message from Kate Bell", **({"engine": engine} if engine else {})}}
        status, data, _ = self.call(runtime, "POST", "/api/threads", body)
        self.assertEqual(status, 201, data)
        return data["thread"]["id"], runtime.runs[data["run"]["id"]]

    def post(self, runtime, thread_id, body, **headers):
        return self.call(runtime, "POST", f"/api/threads/{thread_id}/messages", body, headers=headers or None)

    def test_a_message_while_mobsters_agent_works_steers_it(self):
        runtime = self.runtime()
        thread_id, run = self.started(runtime)
        self.assertEqual(run.engine, "smart")
        status, data, _ = self.post(runtime, thread_id, {"text": "only look at today's messages"})
        self.assertEqual((status, data["routed"]), (202, "steer"), data)
        self.assertEqual(data["item"]["routed"], "steer")
        self.assertRegex(data["item"]["messageId"], r"^[a-f0-9]{12}$")
        self.assertEqual(run.steering.pending(), 1)
        message = next(e for e in run.events if e["event"] == "user_message")
        self.assertEqual((message["text"], message["source"]), ("only look at today's messages", "cli"))
        self.settle(runtime)
        steers = [i for i in self.items(runtime, thread_id) if i.get("routed") == "steer"]
        self.assertEqual(len(steers), 1)  # the route's item and the listener's user_message are one
        # The rule in it is the thread's rule from now on, copied verbatim.
        thread = self.service(runtime).store.get(thread_id)
        self.assertEqual(thread["rules"], ["only look at today's messages."])

    def test_messages_nobody_read_become_unread(self):
        runtime = self.runtime()
        thread_id, run = self.started(runtime)
        self.post(runtime, thread_id, {"text": "and Sam's too"})
        self.finish(runtime, run)
        self.settle(runtime)
        item = next(i for i in self.items(runtime, thread_id) if i["kind"] == "user" and i["text"] == "and Sam's too")
        self.assertEqual((item["routed"], item["note"]), ("unread", threads_service.UNREAD_NOTE))

    def test_a_message_during_quick_mode_is_refused(self):
        runtime = self.runtime()
        os.environ["TYPESAFE_API_KEY"] = "jev-test"
        status, data, _ = self.call(runtime, "POST", "/api/threads",
                                    {"message": {"text": "Open Messages", "engine": "fast", "appId": "messages"}})
        self.assertEqual(status, 201, data)
        status, data, _ = self.post(runtime, data["thread"]["id"], {"text": "faster please"})
        self.assertEqual((status, data["code"]), (409, "steer_unsupported"))
        self.assertEqual(data["error"], threads_service.STEER_UNSUPPORTED)

    def test_a_waiting_question_takes_the_message_as_its_answer(self):
        runtime = self.runtime()
        thread_id, run = self.started(runtime)
        helper, box = self.ask(run, clarify())
        self.settle(runtime)
        status, data, _ = self.post(runtime, thread_id, {"text": "the one from work"})
        self.assertEqual((status, data["routed"]), (202, "answer"), data)
        helper.join(5)
        self.assertEqual(box["answer"], "answer:the one from work")
        self.assertEqual(run.steering.pending(), 0)
        self.settle(runtime)
        items = self.items(runtime, thread_id)
        question = next(i for i in items if i["kind"] == "clarify")
        self.assertEqual(question["question"], "Which Sam: Sam Lee or Sam Park?")
        self.assertEqual(question["choices"], [{"id": "lee", "label": "Sam Lee"}, {"id": "park", "label": "Sam Park"}])
        self.assertEqual((question["answer"], question["resolution"]), ("the one from work", "answered"))
        resolved = next(e for e in run.events if e["event"] == "approval_resolved")
        self.assertNotIn("the one from work", str(resolved))  # the journal has "answered", never the text

    def test_a_choice_answered_in_the_app_names_its_label_in_the_thread(self):
        runtime = self.runtime()
        thread_id, run = self.started(runtime)
        helper, box = self.ask(run, clarify())
        self.settle(runtime)  # the question is in the thread before anyone sees it to answer
        status, data, _ = self.call(runtime, "POST", f"/api/runs/{run.id}/approval",
                                    {"id": run.approval["id"], "approve": True, "choice": "park"})
        self.assertEqual(status, 200, data)
        helper.join(5)
        self.settle(runtime)
        question = next(i for i in self.items(runtime, thread_id) if i["kind"] == "clarify")
        self.assertEqual((question["answer"], question["resolution"]), ("Sam Park", "answered"))

    def test_a_waiting_approval_refuses_every_message_and_queues_nothing(self):
        runtime = self.runtime()
        thread_id, run = self.started(runtime)
        helper, box = self.ask(run, commit())
        for text in ("yes send it", "y", "Send", "approve"):
            with self.subTest(text=text):
                status, data, _ = self.post(runtime, thread_id, {"text": text})
                self.assertEqual((status, data["code"]), (409, "approval_pending"))
                self.assertEqual(data["error"], server.APPROVAL_PENDING)
                self.assertEqual(data["approvalId"], run.approval["id"])
        self.assertEqual(run.steering.pending(), 0)
        self.assertIsNotNone(run.approval)  # nothing typed answered it
        self.assertNotIn("user_message", [e["event"] for e in run.events])
        self.assertEqual(threads_service.APPROVAL_PENDING, server.APPROVAL_PENDING)
        run.answer_approval(run.approval["id"], False)
        helper.join(5)
        self.assertEqual(box["answer"], "denied")
        self.settle(runtime)
        self.assertFalse([i for i in self.items(runtime, thread_id) if i.get("text") in ("yes send it", "y")])

    def test_steering_follows_the_runs_origin_rules(self):
        runtime = self.runtime()
        status, data, _ = self.call(runtime, "POST", "/api/threads", {"message": {"text": "Check my calendar"}},
                                    headers={"X-Mobster-Origin": "mcp"})
        self.assertEqual(status, 201, data)
        thread_id = data["thread"]["id"]
        status, data, _ = self.post(runtime, thread_id, {"text": "tomorrow, not today"}, **{"X-Mobster-Origin": "cli"})
        self.assertEqual((status, data["code"]), (403, "steer_not_allowed"))
        status, data, _ = self.post(runtime, thread_id, {"text": "tomorrow, not today"}, **{"X-Mobster-Origin": "app"})
        self.assertEqual((status, data["routed"]), (202, "steer"))
        status, data, _ = self.post(runtime, thread_id, {"text": "and Friday"}, **{"X-Mobster-Origin": "mcp"})
        self.assertEqual((status, data["routed"]), (202, "steer"))

    def answers_by_origin(self, started, refused, allowed):
        runtime = self.runtime()
        status, data, _ = self.call(runtime, "POST", "/api/threads", {"message": {"text": "Text Sam the address"}},
                                    headers={"X-Mobster-Origin": started})
        self.assertEqual(status, 201, data)
        thread_id, run = data["thread"]["id"], runtime.runs[data["run"]["id"]]
        with self.assertRaises(server.APIError) as refusal:
            run.steer("Sam Park", source=refused[0] if refused[0] != "api" else "cli")
        self.assertEqual(str(refusal.exception), threads_service.STEER_NOT_ALLOWED)  # Run.steer's own sentence
        for origin in refused:
            with self.subTest(started=started, origin=origin):
                helper, box = self.ask(run, clarify())
                headers = {"X-Mobster-Origin": origin} if origin != "api" else {}  # a script names no origin
                status, answer, _ = self.post(runtime, thread_id, {"text": "Sam Park"}, **headers)
                self.assertEqual((status, answer.get("code")), (403, "steer_not_allowed"), answer)
                self.assertEqual(answer["error"], threads_service.STEER_NOT_ALLOWED)
                self.assertIsNotNone(run.approval)  # still waiting for someone who may answer it
                run.answer_approval(run.approval["id"], False)
                helper.join(5)
        for origin in allowed:
            with self.subTest(started=started, origin=origin):
                helper, box = self.ask(run, clarify())
                status, answer, _ = self.post(runtime, thread_id, {"text": "Sam Park"}, **{"X-Mobster-Origin": origin})
                self.assertEqual((status, answer.get("routed")), (202, "answer"), answer)
                helper.join(5)
                self.assertEqual(box["answer"], "answer:Sam Park")
        self.settle(runtime)
        answers = [i for i in self.items(runtime, thread_id) if i.get("routed") == "answer"]
        self.assertEqual(len(answers), len(allowed))  # a refused answer leaves nothing in the thread

    def test_a_coding_agent_cant_answer_a_question_meant_for_the_person(self):
        # A question in a task started in the Mac app is the person's: phone_task can't answer it through the
        # thread, just as it can't steer that task.
        self.answers_by_origin("app", refused=("mcp",), allowed=("cli", "app"))

    def test_a_task_started_over_mcp_takes_answers_only_from_mcp_and_the_app(self):
        self.answers_by_origin("mcp", refused=("cli", "api"), allowed=("app", "mcp"))

    def test_a_task_that_finished_meanwhile_starts_a_new_one(self):
        runtime = self.runtime()
        thread_id, run = self.started(runtime)
        real = run.steer

        def finished_meanwhile(text, source="app"):
            self.finish(runtime, run)
            return real(text, source)
        with patch.object(run, "steer", finished_meanwhile):
            status, data, _ = self.post(runtime, thread_id, {"text": "Now reply to that"})
        self.assertEqual(status, 201, data)
        self.assertNotEqual(data["run"]["id"], run.id)

    def test_files_go_only_with_a_new_task(self):
        runtime = self.runtime()
        thread_id, _run = self.started(runtime)
        status, data, _ = self.post(runtime, thread_id, {"text": "use this", "attachmentIds": ["a" * 12]})
        self.assertEqual((status, data["code"]), (409, "steer_attachments"))
        self.finish(runtime, _run)
        # With the files track loaded, attachmentIds is a run field, and a file it doesn't have is refused by it.
        status, data, _ = self.post(runtime, thread_id, {"text": "use this", "attachmentIds": ["a" * 12]})
        self.assertEqual((status, data["error"]), (400, "A file attached to this task is gone. Attach it again."))
        # Without the files track, a message with files says files can't go with it.
        with patch("mobile_agent.harness_api.run_fields", return_value={}):
            status, data, _ = self.post(runtime, thread_id, {"text": "use this", "attachmentIds": ["a" * 12]})
        self.assertEqual((status, data["code"]), (409, "attachments_unavailable"))

    def test_new_task_mode_never_steers(self):
        runtime = self.runtime()
        thread_id, run = self.started(runtime)
        status, data, _ = self.post(runtime, thread_id, {"text": "Something else", "mode": "new_task"})
        self.assertEqual((status, data["code"]), (409, "run_active"))  # one task per phone; nothing steered
        self.assertEqual(run.steering.pending(), 0)


class IdempotencyAndLimitTests(ThreadsBase):
    def test_a_retried_message_is_answered_once(self):
        runtime = self.runtime()
        headers = {"Idempotency-Key": "chat-1"}
        status, first, _ = self.call(runtime, "POST", "/api/threads", {"message": {"text": "Check my calendar"}},
                                     headers=headers)
        self.assertEqual(status, 201)
        status, again, _ = self.call(runtime, "POST", "/api/threads", {"message": {"text": "Check my calendar"}},
                                     headers=headers)
        self.assertEqual((status, again["replayed"]), (200, True))
        self.assertEqual((again["thread"]["id"], again["run"]["id"]), (first["thread"]["id"], first["run"]["id"]))
        self.assertEqual(len(runtime.runs), 1)
        status, data, _ = self.call(runtime, "POST", "/api/threads", {"message": {"text": "Something else"}},
                                    headers=headers)
        self.assertEqual((status, data["code"]), (409, "idempotency_conflict"))
        thread_id = first["thread"]["id"]
        self.finish(runtime, runtime.runs[first["run"]["id"]])
        key = {"Idempotency-Key": "chat-2"}
        status, one, _ = self.call(runtime, "POST", f"/api/threads/{thread_id}/messages", {"text": "Now Friday"},
                                   headers=key)
        self.assertEqual(status, 201)
        status, two, _ = self.call(runtime, "POST", f"/api/threads/{thread_id}/messages", {"text": "Now Friday"},
                                   headers=key)
        self.assertEqual((status, two["run"]["id"], two["replayed"]), (200, one["run"]["id"], True))
        self.assertEqual(len(runtime.runs), 2)

    def test_a_first_message_that_cant_start_leaves_no_thread(self):
        runtime = self.runtime()
        runtime.target_status.return_value = {**runtime.target_status.return_value, "ready": False}
        status, data, _ = self.call(runtime, "POST", "/api/threads", {"message": {"text": "Check my calendar"}})
        self.assertEqual(status, 503, data)
        self.assertEqual(self.call(runtime, "GET", "/api/threads")[1]["threads"], [])

    def test_limits_and_bad_requests(self):
        runtime = self.runtime()
        data = self.thread(runtime)
        thread_id = data["thread"]["id"]
        self.finish(runtime, runtime.runs[data["run"]["id"]])
        cases = [({"text": ""}, 400), ({"text": "x" * 4001}, 400), ({"text": "x", "mode": "later"}, 400),
                 ({"text": "x", "via": "smoke-signal"}, 400), ({"nope": 1, "text": "x"}, 400)]
        for body, expected in cases:
            with self.subTest(body=str(body)[:40]):
                self.assertEqual(self.call(runtime, "POST", f"/api/threads/{thread_id}/messages", body)[0], expected)
        self.assertEqual(self.call(runtime, "GET", "/api/threads/000000000000")[0], 404)
        self.assertEqual(self.call(runtime, "POST", "/api/threads/000000000000/messages", {"text": "x"})[1]["code"],
                         "thread_not_found")
        self.assertEqual(self.call(runtime, "GET", "/api/threads?limit=500")[0], 400)

    def test_a_full_thread_says_so(self):
        runtime = self.runtime()
        data = self.thread(runtime)
        self.finish(runtime, runtime.runs[data["run"]["id"]])
        with patch.object(thread_store, "MAX_ITEMS", 3 + thread_store.RESERVE):
            status, answer, _ = self.call(runtime, "POST", f"/api/threads/{data['thread']['id']}/messages",
                                          {"text": "One more"})
        self.assertEqual((status, answer["code"], answer["error"]), (409, "thread_full", thread_store.FULL))

    def test_an_archived_thread_takes_no_messages_until_restored(self):
        runtime = self.runtime()
        data = self.thread(runtime)
        thread_id = data["thread"]["id"]
        self.finish(runtime, runtime.runs[data["run"]["id"]])
        status, answer, _ = self.call(runtime, "POST", f"/api/threads/{thread_id}", {"archived": True,
                                                                                       "title": "Kate"})
        self.assertEqual((status, answer["thread"]["archived"], answer["thread"]["title"]), (200, True, "Kate"))
        self.assertEqual(self.call(runtime, "POST", f"/api/threads/{thread_id}/messages", {"text": "x"})[1]["code"],
                         "thread_archived")
        self.assertEqual(self.call(runtime, "GET", "/api/threads")[1]["threads"], [])
        self.assertEqual(len(self.call(runtime, "GET", "/api/threads?archived=true")[1]["threads"]), 1)
        with self.assertRaisesRegex(ValueError, "archived"):
            runtime.create(None, "x", "live", extras={"threadId": thread_id})
        self.call(runtime, "POST", f"/api/threads/{thread_id}", {"archived": False})
        self.assertEqual(self.call(runtime, "POST", f"/api/threads/{thread_id}/messages", {"text": "x"})[0], 201)
        for bad in ({"title": ""}, {"title": "x" * 81}, {"pinned": "yes"}, {}, {"status": "idle"}):
            self.assertEqual(self.call(runtime, "POST", f"/api/threads/{thread_id}", bad)[0], 400)


class ListAndDeleteTests(ThreadsBase):
    def test_listing_newest_first_with_a_cursor_search_and_device(self):
        runtime = self.runtime()
        store = self.service(runtime).store
        made = []
        for n, title in enumerate(("Kate's birthday", "Gym hours", "Dinner with Sam")):
            thread = store.create_thread(title=title, device="d1" if n == 1 else None,
                                         device_name="Sam's iPhone" if n == 1 else None)
            store.update_thread(thread["id"], {"updatedAt": 1_000_000 + n * 1000}, touch=False)
            made.append(thread["id"])
        store.append(made[0], [{"kind": "user", "text": "Text Kate happy birthday", "routed": "new_run"}])
        store.update_thread(made[0], {"updatedAt": 1_000_000}, touch=False)
        listed = self.call(runtime, "GET", "/api/threads")[1]
        self.assertEqual([t["title"] for t in listed["threads"]], ["Dinner with Sam", "Gym hours", "Kate's birthday"])
        page = self.call(runtime, "GET", "/api/threads?limit=2")[1]
        self.assertEqual(len(page["threads"]), 2)
        rest = self.call(runtime, "GET", f"/api/threads?limit=2&before={page['next']}")[1]
        self.assertEqual(([t["title"] for t in rest["threads"]], rest["next"]), (["Kate's birthday"], None))
        found = self.call(runtime, "GET", "/api/threads?q=birthday")[1]["threads"]
        self.assertEqual([t["title"] for t in found], ["Kate's birthday"])
        found = self.call(runtime, "GET", "/api/threads?q=HAPPY")[1]["threads"]  # a message's words, any case
        self.assertEqual([t["title"] for t in found], ["Kate's birthday"])
        self.assertEqual(self.call(runtime, "GET", "/api/threads?q=100%25")[1]["threads"], [])
        on = self.call(runtime, "GET", "/api/threads?device=Sam%27s%20iPhone")[1]["threads"]
        self.assertEqual([t["title"] for t in on], ["Gym hours"])

    def test_delete_removes_the_threads_runs_and_frames(self):
        runtime = self.runtime()
        data = self.thread(runtime)
        thread_id, run = data["thread"]["id"], runtime.runs[data["run"]["id"]]
        status, answer, _ = self.call(runtime, "DELETE", f"/api/threads/{thread_id}")
        self.assertEqual((status, answer["code"]), (409, "run_active"))
        run.emit({"event": "step", "_frame": b"\xff\xd8 not really a jpeg"})
        self.finish(runtime, run)
        self.settle(runtime)
        removed = []
        threads_service.add_delete_listener(lambda rt, tid, runs: removed.append((tid, runs)))
        self.addCleanup(threads_service._delete_listeners.clear)
        with patch.object(runtime.frames, "delete", wraps=runtime.frames.delete) as frames:
            status, answer, _ = self.call(runtime, "DELETE", f"/api/threads/{thread_id}")
        self.assertEqual((status, answer), (200, {"deleted": True, "runsDeleted": 1}))
        frames.assert_called_with(run.id)
        self.assertNotIn(run.id, runtime.runs)
        self.assertEqual(self.call(runtime, "GET", f"/api/threads/{thread_id}")[0], 404)
        self.assertEqual(removed, [(thread_id, [run.id])])
        self.assertEqual(runtime.journal.connection.execute(
            "SELECT count(*) FROM thread_items WHERE thread_id=?", (thread_id,)).fetchone()[0], 0)

    def test_delete_can_keep_the_runs(self):
        runtime = self.runtime()
        data = self.thread(runtime)
        run = runtime.runs[data["run"]["id"]]
        self.finish(runtime, run)
        status, answer, _ = self.call(runtime, "DELETE", f"/api/threads/{data['thread']['id']}?runs=keep")
        self.assertEqual(answer, {"deleted": True, "runsDeleted": 0})
        self.assertIn(run.id, runtime.runs)
        self.assertEqual(self.call(runtime, "DELETE", f"/api/threads/{data['thread']['id']}?runs=all")[0], 400)

    def test_get_returns_items_after_a_seq_and_the_runs_they_name(self):
        runtime = self.runtime()
        data = self.thread(runtime)
        self.settle(runtime)
        status, answer, _ = self.call(runtime, "GET", f"/api/threads/{data['thread']['id']}?after=0")
        self.assertEqual([i["seq"] for i in answer["items"]], [1])
        self.assertEqual(list(answer["runs"]), [data["run"]["id"]])
        self.assertEqual(answer["runs"][data["run"]["id"]]["events"], [])


class BusTests(ThreadsBase):
    def test_the_threads_run_events_and_items_go_out_on_its_topic(self):
        runtime = self.runtime()
        since = runtime.bus.position()
        data = self.thread(runtime)
        thread_id, run = data["thread"]["id"], runtime.runs[data["run"]["id"]]
        run.emit({"event": "observation", "step": 0, "elements": [], "app_name": "Messages"})
        self.finish(runtime, run)
        runtime.finished(run)
        self.settle(runtime)
        topic = bus_events(runtime, f"thread:{thread_id}", since)
        relayed = [e for e in topic if e["event"] == "run_event"]
        self.assertEqual([e["data"]["event"] for e in relayed], ["observation", "run_finished"])
        self.assertEqual(relayed[0]["runId"], run.id)
        self.assertIn("thread_item_updated", [e["event"] for e in topic])
        runs = bus_events(runtime, "runs", since)
        self.assertEqual([e.get("threadId") for e in runs], [thread_id, thread_id])
        threads = [e for e in bus_events(runtime, "threads", since) if e["event"] == "thread_updated"]
        self.assertEqual(threads[-1]["thread"]["status"], "idle")


if __name__ == "__main__":
    unittest.main()

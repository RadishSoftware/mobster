import json
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import engines, harness_api
from mobile_agent.agent_hooks import READY, Prepared, SkillResult
from mobile_agent.api_errors import APIError
from mobile_agent.frontier import FrontierAgent, default_skills, prompt_text, system_prompt
from mobile_agent.harness import checkpoints, prewarm, settings
from mobile_agent.harness.checkpoints import PICKUP_FEEDBACK
from mobile_agent.harness.tools import offered
from mobile_agent.harness.tools.ask_user import NO_ANSWER, PROMPT, AskUser, factory, feedback_for
from mobile_agent.journal import Journal
from mobile_agent.run import Run
from mobile_agent.server import Runtime, make_handler
from mobile_agent.state import Element, Snapshot
from mobile_agent.tests.seam_support import isolate, request
from mobile_agent.tests.seam_tasks import COMPOSE, Script, SmartBase, item
from mobile_agent.tests.test_frontier import Driver, screen
from mobile_agent.tests.test_frontier import Script as FrontierScript
from mobile_agent.tests.test_server_engine import Base
from mobile_agent.tests.test_server_hardening import APP
from mobile_agent.tests.timing import bound


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = Journal(None)
        self.addCleanup(self.db.close)
        conn = self.db.connection
        conn.execute("BEGIN IMMEDIATE")
        checkpoints.migrate(conn, 0)
        conn.execute("COMMIT")
        self.run_ = Run(dict(APP), "Text Sam", "live", journal=self.db, engine="smart")
        self.db.create(self.run_.metadata())

    def test_saved_redacted_and_kept_to_64_kb(self):
        state = {"step": 3, "plan": "Send it", "notes": ["My PIN is 4821", "Sam is Sam Lee"],
                 "history": [f"{i}.0: TAP 'Row {i}' -> screen changed " + "x" * 300 for i in range(400)],
                 "app": "com.apple.MobileSMS", "cost_usd": .02, "elapsed_s": 9.5, "contract": None}
        self.assertTrue(checkpoints.save(self.db, self.run_.id, state))
        saved = checkpoints.load(self.db, self.run_.id)
        self.assertNotIn("4821", json.dumps(saved))
        self.assertEqual(saved["step"], 3)
        row = self.db.connection.execute("SELECT bytes FROM run_checkpoints").fetchone()
        self.assertLessEqual(row[0], checkpoints.MAX_BYTES)
        self.assertEqual(saved["history"][-1], state["history"][-1][:400])  # the newest lines are the ones kept

    def test_one_row_per_run_and_it_goes_with_its_run(self):
        checkpoints.save(self.db, self.run_.id, {"step": 1})
        checkpoints.save(self.db, self.run_.id, {"step": 2})
        self.assertEqual(checkpoints.load(self.db, self.run_.id)["step"], 2)
        self.run_.finish({"event": "result", "status": "stopped"})
        self.db.delete(self.run_.id)
        self.assertIsNone(checkpoints.load(self.db, self.run_.id))

    def test_what_can_be_picked_up(self):
        def finished(status, summary=None, engine="smart"):
            run = Run(dict(APP), "g", "live", engine=engine)
            run.finish({"event": "result", "status": status, **(summary or {})})
            return run
        for status in ("interrupted", "error", "stopped", "approval_timeout"):
            self.assertTrue(checkpoints.resumable(finished(status)), status)
        self.assertTrue(checkpoints.resumable(finished("blocked", {"code": "unplugged"})))
        self.assertTrue(checkpoints.resumable(finished("blocked", {"stop_code": "phone_locked"})))
        for status in ("completed", "approval_denied", "spend_cap", "max_steps"):
            self.assertFalse(checkpoints.resumable(finished(status)), status)
        self.assertFalse(checkpoints.resumable(finished("blocked", {"code": "face_id"})))
        self.assertFalse(checkpoints.resumable(finished("stopped", engine="fast")))
        self.assertFalse(checkpoints.resumable(Run(dict(APP), "g", "live", engine="smart")))  # still running

    def test_the_handoff_carries_the_state_the_messages_and_what_was_approved(self):
        original = Run(dict(APP), "Text Sam", "live", engine="smart")
        original.events = [{"event": "user_message", "text": "make it short"},
                           {"event": "frontier_approval", "step": 2, "act": "send_message", "answer": "approved"},
                           {"event": "frontier_action", "step": 2, "target_label": "Send"},
                           {"event": "frontier_approval", "step": 4, "act": "send_message", "answer": "denied"},
                           {"event": "user_message", "text": "and say 7pm"}]
        initial = checkpoints.handoff({"step": 5, "plan": "p", "notes": ["n"], "history": ["4.0: TAP 'Send'"],
                                       "cost_usd": .03}, original)
        self.assertEqual((initial.steps_used, initial.cost_usd, initial.plan, initial.history),
                         (5, .03, "p", ("4.0: TAP 'Send'",)))
        self.assertEqual(initial.notes[-1], checkpoints.MESSAGE_NOTE + "make it short · and say 7pm")
        self.assertTrue(initial.feedback.startswith(PICKUP_FEEDBACK))
        self.assertIn("step 2: sending a message (Send)", initial.feedback)
        self.assertNotIn("step 4", initial.feedback)
        # The approved send may already have happened: the new run holds it. The declined one never ran.
        self.assertEqual(initial.held_acts, ("send_message",))

    def test_held_acts_are_the_approved_ones_and_the_ones_whose_answer_was_lost(self):
        original = Run(dict(APP), "Post the photo and text Sam", "live", engine="smart")
        original.events = [{"event": "frontier_failed", "step": 1, "outcome": "failed", "held_act": "pay"},
                           {"event": "frontier_approval", "step": 2, "act": "send_message", "answer": "denied"},
                           {"event": "frontier_failed", "step": 3, "outcome": "unknown", "held_act": "post"},
                           {"event": "frontier_failed", "step": 3, "outcome": "unknown"},
                           {"event": "frontier_approval", "step": 4, "act": "send_message", "answer": "approved"},
                           {"event": "frontier_approval", "step": 5, "act": "send_message", "answer": "approved"}]
        # Oldest first and once each; a failure known not to have run (pay) and a declined send hold nothing.
        self.assertEqual(checkpoints.held_acts(original), ("post", "send_message"))
        self.assertEqual(checkpoints.held_acts(Run(dict(APP), "g", "live", engine="smart")), ())
        initial = checkpoints.handoff({"step": 6}, original)
        self.assertEqual(initial.held_acts, ("post", "send_message"))
        self.assertIn("The answer to an earlier attempt at posting was lost: it may have gone through.",
                      initial.feedback)


class UnpluggingPhone(SmartBase.Phone):

    def __init__(self, screens, log):
        super().__init__(screens)
        self.log, self.unplugged = log, False

    def observe(self, timeout=10):
        self.log.append("observe")
        if self.unplugged:
            raise ConnectionResetError("ConnectionResetError: [Errno 54] Connection reset by peer")
        return super().observe(timeout)

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.log.append(("act", operation, getattr(target, "label", target)))
        super().execute(operation, target, snapshot, text, timeout)
        if getattr(target, "label", None) == "Send":
            self.unplugged = True


class Guard:
    def __init__(self, phone_box):
        self.phone_box = phone_box

    def check(self, driver, *, cause):
        return READY

    def attached(self):
        phone = self.phone_box.get("phone")
        return not (phone is not None and phone.unplugged)

    def allows_app(self, bundle_id):
        return True

    def finish(self, driver):
        pass


class Logging(Script):
    def __init__(self, *args, log, **kwargs):
        super().__init__(*args, **kwargs)
        self.log = log

    def complete(self, messages, schema, timeout=60):
        if "actions" in schema["properties"]:
            self.log.append("decide")
        return super().complete(messages, schema, timeout)


def answer_approvals(run, answer, seen):
    def loop():
        for _ in range(1000):
            pending = run.public()["approval"]
            if pending:
                seen.append(pending)
                run.answer_approval(pending["id"], answer)
                return
            if run.finished_at is not None:
                return
            time.sleep(.01)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


class PickupTests(SmartBase):
    def setUp(self):
        super().setUp()
        self.box = {}
        guard = patch.object(Runtime, "make_guard", lambda runtime, run, phone: Guard(self.box))
        guard.start()
        self.addCleanup(guard.stop)

    def work_on(self, runtime, run, script, phone):
        self.box["phone"] = phone
        self.Phone = lambda screens: phone   # SmartBase.work builds its phone through self.Phone
        try:
            self.work(runtime, run, script)
        finally:
            del self.Phone
        checkpoints.writer.flush(5)

    def test_canonical_pickup_keeps_a_workflow_only_binding(self):
        runtime = self.runtime()
        source = runtime.create("messages", "Read Sam's message", "live", origin="app")
        source.workflow_id = "a" * 12  # a trusted saved dispatch assigned this private field
        checkpoints.save(runtime.journal, source.id, {"step": 1, "plan": "Read Sam", "history": []})
        self.finish(runtime, source, "stopped")
        with self.assertRaisesRegex(ValueError, "profile and workflow stay fixed"):
            runtime.create("messages", source.goal, "live", extras={"resumeFrom": source.id})
        resumed, replayed = checkpoints.pick_up(runtime, source.id, idempotency_key="workflow-pickup",
                                                 origin="app")
        self.assertFalse(replayed)
        self.assertEqual(resumed.workflow_id, source.workflow_id)
        self.assertEqual(runtime.run_context(resumed).workflow_id, source.workflow_id)

    def test_restart_cannot_pick_up_a_workflow_without_its_private_binding(self):
        runtime = self.runtime()
        source, _ = runtime.create("messages", "Read Sam's message", "live",
                                   idempotency_key="workflow:aaaaaaaaaaaa:manual:request", origin="app")
        checkpoints.save(runtime.journal, source.id, {"step": 1, "plan": "Read Sam", "history": []})
        self.finish(runtime, source, "stopped")
        # Rehydration keeps the durable request key but cannot invent its private workflow identity.
        source.workflow_id = None
        with self.assertRaises(APIError) as caught:
            checkpoints.pick_up(runtime, source.id, idempotency_key="restart-pickup", origin="app")
        self.assertEqual(caught.exception.code, "workflow_context_missing")

    def test_second_pickup_after_restart_cannot_lose_workflow_scope(self):
        runtime = self.runtime()
        source, _ = runtime.create("messages", "Read Sam's message", "live",
                                   idempotency_key="workflow:aaaaaaaaaaaa:manual:request", origin="app")
        source.workflow_id = "a" * 12  # represents the trusted binding before restart
        checkpoints.save(runtime.journal, source.id, {"step": 1, "plan": "Read Sam", "history": []})
        self.finish(runtime, source, "stopped")
        first, _ = checkpoints.pick_up(runtime, source.id, idempotency_key="ordinary-pickup-key", origin="app")
        self.assertEqual(first.workflow_id, source.workflow_id)
        checkpoints.save(runtime.journal, first.id, {"step": 2, "plan": "Read Sam", "history": []})
        self.finish(runtime, first, "stopped")
        source.workflow_id = first.workflow_id = None  # journal rehydration has no private workflow binding
        with self.assertRaises(APIError) as caught:
            checkpoints.pick_up(runtime, first.id, idempotency_key="second-pickup", origin="app")
        self.assertEqual(caught.exception.code, "workflow_context_missing")
        with self.assertRaisesRegex(ValueError, "workflow scope cannot be verified"):
            runtime.create("messages", first.goal, "live", extras={"resumeFrom": first.id})
        runtime.runs.pop(source.id)
        with self.assertRaises(APIError) as caught:
            checkpoints.pick_up(runtime, first.id, idempotency_key="second-pickup", origin="app")
        self.assertEqual(caught.exception.code, "workflow_context_missing")

    def test_pickup_refuses_access_limits_it_cannot_preserve(self):
        runtime = self.runtime()
        cases = [(f"origin:{origin}", lambda run, origin=origin: setattr(run, "origin", origin))
                 for origin in ("mcp", "remote", "hook", "trigger", "console")]
        cases += [(name, lambda run, name=name: run.extras.__setitem__(name, "restricted"))
                  for name in ("permissionMode", "projectEnv", "remoteGrant", "consoleMember", "consoleSession")]
        cases += [("allowed bundles", lambda run: setattr(run, "allowed_bundles", frozenset({"com.apple.MobileSMS"}))),
                  ("pinned apps", lambda run: setattr(run, "pinned_apps", [{"id": "messages"}])),
                  ("task cap", lambda run: setattr(run, "cap_usd", 1.0))]
        for label, restrict in cases:
            with self.subTest(label=label):
                source = runtime.create("messages", "Read Sam's message", "live", origin="app")
                checkpoints.save(runtime.journal, source.id, {"step": 1, "plan": "Read Sam", "history": []})
                self.finish(runtime, source, "stopped")
                restrict(source)
                with self.assertRaises(APIError) as caught:
                    checkpoints.pick_up(runtime, source.id, idempotency_key=f"restricted-{source.id}", origin="app")
                self.assertEqual(caught.exception.code, "pickup_scope")

    def test_queued_pickup_stops_when_source_checkpoint_disappears(self):
        runtime = self.runtime()
        source = runtime.create("messages", "Read Sam's message", "live", origin="app")
        checkpoints.save(runtime.journal, source.id, {"step": 1, "plan": "Read Sam", "history": []})
        self.finish(runtime, source, "stopped")
        holder = runtime.create("messages", "Open Sam's chat", "live", origin="app")
        queued, _ = runtime.create(source.app["id"], source.goal, "live", "queued-pickup", origin="app",
                                   extras={"resumeFrom": source.id}, _resume_run=source, queue=True)
        self.assertIsNotNone(queued.queue_info)
        with runtime.journal.transaction() as connection:
            connection.execute("DELETE FROM run_checkpoints WHERE run_id=?", (source.id,))
        runtime.runs.pop(source.id)
        self.finish(runtime, holder)
        runtime.run_ended(holder)
        self.assertIn(queued.id, runtime.active_runs)
        self.worker.stop()
        try:
            with patch.object(runtime, "smart_client", return_value=SimpleNamespace(complete=lambda *a, **k: None)), \
                    patch("mobile_agent.engines.release_client"), \
                    patch.object(runtime, "open_driver", side_effect=AssertionError("phone opened")) as opened:
                runtime.work(queued)
                opened.assert_not_called()
        finally:
            self.worker.start()
        self.assertEqual((queued.summary["status"], queued.summary["stop_code"]),
                         ("blocked", "pickup_checkpoint_missing"))
        self.assertFalse(queued.summary["actions_may_have_run"])
        self.assertNotIn("app_launch_started", [event["event"] for event in queued.events])

    def test_queued_pickup_stops_before_phone_if_workflow_scope_is_lost(self):
        runtime = self.runtime()
        source, _ = runtime.create("messages", "Read Sam's message", "live",
                                   idempotency_key="workflow:aaaaaaaaaaaa:manual:request", origin="app")
        source.workflow_id = "a" * 12
        checkpoints.save(runtime.journal, source.id, {"step": 1, "plan": "Read Sam", "history": []})
        self.finish(runtime, source, "stopped")
        holder = runtime.create("messages", "Open Sam's chat", "live", origin="app")
        queued, _ = runtime.create(source.app["id"], source.goal, "live", "queued-workflow-pickup", origin="app",
                                   extras={"resumeFrom": source.id}, _resume_run=source, queue=True)
        source.workflow_id = None  # trusted source context disappeared after admission
        self.finish(runtime, holder)
        runtime.run_ended(holder)
        self.worker.stop()
        try:
            with patch.object(runtime, "smart_client", return_value=SimpleNamespace(complete=lambda *a, **k: None)), \
                    patch("mobile_agent.engines.release_client"), \
                    patch.object(runtime, "open_driver", side_effect=AssertionError("phone opened")) as opened:
                runtime.work(queued)
                opened.assert_not_called()
        finally:
            self.worker.start()
        self.assertEqual((queued.summary["status"], queued.summary["stop_code"]),
                         ("blocked", "pickup_checkpoint_missing"))
        self.assertFalse(queued.summary["actions_may_have_run"])

    def test_pickup_stops_before_phone_if_its_checkpoint_track_is_disabled_after_admission(self):
        runtime = self.runtime()
        source = runtime.create("messages", "Read Sam's message", "live", origin="app")
        checkpoints.save(runtime.journal, source.id, {"step": 1, "plan": "Read Sam", "history": []})
        self.finish(runtime, source, "stopped")
        resumed, _ = checkpoints.pick_up(runtime, source.id, idempotency_key="disabled-pickup", origin="app")
        harness_api.disable("harness")
        self.worker.stop()
        try:
            with patch.object(runtime, "smart_client", return_value=SimpleNamespace(complete=lambda *a, **k: None)), \
                    patch("mobile_agent.engines.release_client"), \
                    patch.object(runtime, "open_driver", side_effect=AssertionError("phone opened")) as opened:
                runtime.work(resumed)
                opened.assert_not_called()
        finally:
            self.worker.start()
        self.assertEqual((resumed.summary["status"], resumed.summary["stop_code"]),
                         ("blocked", "pickup_checkpoint_missing"))
        self.assertFalse(resumed.summary["actions_may_have_run"])

    def unplugged_run(self, runtime):
        run = runtime.create("messages", "Text Sam I'm running late", "live", origin="app")
        log = []
        phone = UnpluggingPhone([COMPOSE] * 8, log)
        seen = []
        thread = answer_approvals(run, True, seen)
        script = Script([[("TAP", "Message", None)], [("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)]],
                        [item()], answer="Sent.")
        self.work_on(runtime, run, script, phone)
        thread.join(5)
        return run, phone

    def test_after_an_unplug_the_picked_up_task_looks_first_and_still_asks_before_sending(self):
        runtime = self.runtime()
        run, phone = self.unplugged_run(runtime)
        self.assertEqual((run.status, run.summary.get("code")), ("blocked", "unplugged"))
        self.assertIn(("act", "TAP", "Send"), phone.log)
        self.assertIsNotNone(checkpoints.load(runtime.journal, run.id))

        handler = make_handler(runtime)
        status, data, _ = request(handler, "POST", f"/api/runs/{run.id}/pickup", {},
                                  headers={"Idempotency-Key": "pickup-1", "X-Mobster-Origin": "app"})
        self.assertEqual(status, 201, data)
        new = runtime.runs[data["run"]["id"]]
        self.assertEqual((new.goal, new.app["id"], new.extras["resumeFrom"], new.origin, new.engine),
                         (run.goal, run.app["id"], run.id, "app", "smart"))

        log = []
        again = UnpluggingPhone([COMPOSE] * 8, log)
        seen = []
        thread = answer_approvals(new, False, seen)
        script = Logging([[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)], [("DONE", None, None)],
                          [("DONE", None, None)]], [item()], answer="Sent.", log=log)
        self.work_on(runtime, new, script, again)
        thread.join(5)
        # It read the screen before deciding anything, and decided before acting.
        self.assertEqual(log[0], "observe")
        self.assertLess(log.index("decide"), min([i for i, entry in enumerate(log) if isinstance(entry, tuple)],
                                                 default=len(log)))
        # The Send still asked, and declined, it never ran again. It asked saying the first one may have gone through.
        self.assertEqual(len(seen), 1)
        self.assertIn("may have gone through", json.dumps(seen[0]))
        self.assertNotIn(("act", "TAP", "Send"), log)
        self.assertEqual(new.status, "approval_denied")
        prompt = script.prompts[0]
        self.assertIn(PICKUP_FEEDBACK, prompt)
        self.assertIn("the user said OK to: step 1: sending a message", prompt)
        self.assertIn("T0: TAP 'Message'", prompt)            # the old history goes on, as the turn log's first block
        self.assertIn("Turns used: 1 of 51", prompt)           # 50 more turns on top of the one it used
        picked = next(e for e in new.events if e["event"] == "picked_up")
        self.assertEqual((picked["from"], picked["step"]), (run.id, 1))

        # The same key replays; another is refused: it was picked up already.
        status, data, _ = request(handler, "POST", f"/api/runs/{run.id}/pickup", {},
                                  headers={"Idempotency-Key": "pickup-1"})
        self.assertEqual((status, data["replayed"], data["run"]["id"]), (200, True, new.id))
        status, data, _ = request(handler, "POST", f"/api/runs/{run.id}/pickup", {},
                                  headers={"Idempotency-Key": "pickup-2"})
        self.assertEqual((status, data["code"], data["runId"]), (409, "picked_up", new.id))

    def test_the_secret_filter_runs_on_the_writers_thread_never_the_agents(self):
        # Redacting a turn's state (60 history lines through every secret pattern, about 2 ms, more on a busy Mac) on
        # the agent's thread would sit between each action and the next model call of every task. The writer does it;
        # what is saved is just as clean.
        from mobile_agent import frontier
        runtime = self.runtime()
        run = runtime.create("messages", "Text Sam the code", "live", origin="app")
        phone = UnpluggingPhone([COMPOSE] * 8, [])
        thread = answer_approvals(run, True, [])
        script = Script([[("TYPE", "Message", "Your code is 482913")], [("TAP", "Send", None)]], [item()],
                        answer="Sent.")
        on_agent, kept, keep = [], [], checkpoints.keep
        real = frontier._redacted
        with patch("mobile_agent.frontier.hooks._redacted",
                   side_effect=lambda value: (on_agent.append(1), real(value))[1]), \
                patch.object(checkpoints, "keep",
                             side_effect=lambda ctx, state: (kept.append(state), keep(ctx, state))[1]):
            self.work_on(runtime, run, script, phone)
        thread.join(5)
        self.assertTrue(checkpoints.resumable(run), run.status)
        self.assertEqual(on_agent, [])
        self.assertTrue(kept)
        self.assertIn("482913", json.dumps(kept))   # handed over as it was masked, with nothing else done to it
        saved = checkpoints.load(runtime.journal, run.id)
        self.assertIsNotNone(saved)
        self.assertNotIn("482913", json.dumps(saved))

    def test_a_finished_task_keeps_no_checkpoint(self):
        runtime = self.runtime()
        run = runtime.create("messages", "Read Sam's message", "live")
        script = self.script(steps=[[("TAP", "Message", None)], [("DONE", None, None)], [("DONE", None, None)]])
        self.work(runtime, run, script)
        checkpoints.writer.flush(5)
        self.assertEqual(run.status, "completed")
        self.assertIsNone(checkpoints.load(runtime.journal, run.id))
        status, data, _ = request(make_handler(runtime), "POST", f"/api/runs/{run.id}/pickup", {})
        self.assertEqual((status, data["code"]), (409, "not_resumable"))

    def test_refusals(self):
        runtime = self.runtime()
        handler = make_handler(runtime)
        running = runtime.create("messages", "Text Sam", "live")
        status, data, _ = request(handler, "POST", f"/api/runs/{running.id}/pickup", {})
        self.assertEqual((status, data["code"]), (409, "run_active"))
        self.finish(runtime, running, "interrupted")
        status, data, _ = request(handler, "POST", f"/api/runs/{running.id}/pickup", {})
        self.assertEqual((status, data["code"]), (409, "no_checkpoint"))
        self.assertEqual(request(handler, "POST", "/api/runs/0123456789ab/pickup", {})[0], 404)
        self.assertEqual(request(handler, "POST", f"/api/runs/{running.id}/pickup", {"x": 1})[0], 400)
        status, data, _ = request(handler, "POST", "/api/runs", {"appId": "messages", "goal": "Text Sam",
                                                                 "resumeFrom": running.id})
        self.assertEqual(status, 400)
        self.assertIn("nothing to pick up", data["error"])

    def test_a_task_interrupted_by_a_restart_can_be_picked_up_after_it(self):
        first = self.runtime()
        run = first.create("messages", "Text Sam I'm running late", "live")
        checkpoints.save(first.journal, run.id, {"step": 2, "plan": "Send it", "notes": [], "history": ["1.0: TYPE"],
                                                 "cost_usd": .01})
        config = first.config
        for item_ in first.runs.values():
            if item_.lease:
                item_.lease.close()
                item_.lease = None
        first.journal.close()
        with patch("mobile_agent.server.WdaVideo"), patch("mobile_agent.server.ManualControl"):
            second = Runtime(config)
        self.runtimes.append(second)
        from unittest.mock import Mock

        from mobile_agent.tests.test_server_engine import READY as STATUS_READY
        second.target_status = Mock(return_value=dict(STATUS_READY))
        second.apps = first.apps
        reloaded = second.runs[run.id]
        self.assertEqual(reloaded.status, "interrupted")
        status, data, _ = request(make_handler(second), "POST", f"/api/runs/{run.id}/pickup", {})
        self.assertEqual(status, 201, data)
        self.assertEqual(second.runs[data["run"]["id"]].extras["resumeFrom"], run.id)


class HeldActTests(unittest.TestCase):

    def run_held(self, held):
        import os

        from mobile_agent.frontier import FrontierAgent
        from mobile_agent.state import Element
        from mobile_agent.tests import test_frontier as F
        from mobile_agent.tests.test_turn_exits import CLEAN, Model
        send = Element("1", "Send", "Button", (.8, .9, .15, .05))
        field = dict(id="2", label="Message", role="TextField", rect=(.05, .9, .7, .05), editable=True,
                     actions=("TAP", "TYPE"))
        sms = "com.apple.MobileSMS"
        driver = F.Driver([F.screen(Element(**field), send, bundle=sms)]
                          + [F.screen(Element(**field, value="I'm on my way"), send, bundle=sms)] * 8)
        item = {"kind": "COMMIT", "app": "Messages", "what": "a message to Sam", "payload": "I'm on my way",
                "act": "send_message", "count": "1", "condition": "", "quote": "Text Sam"}
        model = Model([("TYPE", "Message", "I'm on my way"), ("TAP", "Send", None)], [("DONE", None, None)],
                      items=[item])
        events = []
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            FrontierAgent(driver, model, emit=events.append, screenshots=False, settle_seconds=0,
                          apps={sms: "Messages"}).run("Text Sam that I'm on my way",
                                                      initial=harness_api.InitialState(held_acts=held))
        return driver.actions, [e.get("label") for e in events if e["event"] == "frontier_refused"]

    def test_a_held_send_is_refused_where_nobody_can_say_ok(self):
        actions, refused = self.run_held(("send_message",))
        self.assertEqual(actions, [("TYPE", "Message", "I'm on my way")])
        self.assertEqual(refused, ["outcome_unknown"])

    def test_an_act_the_earlier_run_never_did_is_not_held(self):
        for held in ((), ("post",)):
            with self.subTest(held=held):
                actions, refused = self.run_held(held)
                self.assertIn(("TAP", "Send", None), actions)
                self.assertEqual(refused, [])


URL = "http://127.0.0.1:8203"
SMS = "com.apple.MobileSMS"


def chat(bundle=SMS):
    elements = [Element("0", "Sam", "StaticText", (.1, .1, .6, .04)),
                Element("1", "Send", "Button", (.8, .9, .1, .04))]
    return Snapshot(elements, "Sam\nSend", 400, 800, "synthetic_fixture", bundle_id=bundle)


class Clock:

    def __init__(self, changed_at=0.0):
        self.changed_at, self.closed = changed_at, False

    def still_for(self):
        return time.monotonic() - self.changed_at

    def close(self):
        self.closed = True


class Phone(Driver):
    capabilities = Driver.capabilities | {"observe_ready", "frame_clock", "tune", "call", "url", "prefix"}

    def __init__(self, url, session, screens, observe_s=0.0, delay=time.sleep, action_clock=time.monotonic):
        super().__init__(screens)
        self.url, self.prefix, self.frame_clock = url, "/session/" + session, None
        self.observe_s, self.reads, self.tuned, self.closed = observe_s, 0, None, False
        self.delay, self.action_clock = delay, action_clock
        self.first_action_at, self.last_image = None, None

    def tune(self, rich=True, glide=True, **kwargs):
        self.tuned = (rich, glide)
        return {}

    def observe_ready(self, timeout=10):
        first = self.observe(timeout)    # two agreeing reads
        second = self.observe(timeout)
        second.read_started = first.read_started
        return second

    def observe(self, timeout=10):
        started = time.monotonic()
        self.delay(self.observe_s)
        self.reads += 1
        snapshot = super().observe(timeout)
        snapshot.read_started = started
        return snapshot

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        if self.first_action_at is None:
            self.first_action_at = self.action_clock()
        super().execute(operation, target, snapshot, text, timeout)

    def call(self, *args, **kwargs):
        return None

    def close(self):
        self.closed = True


class Fakes:

    def __init__(self, testcase, *, screens=None, changed_at=0.0, session_error=None, lease_free=True):
        self.drivers, self.clocks = [], []
        self.screens = screens or [chat()]

        def build(wda_url=None, session=None, **kwargs):
            driver = Phone(wda_url, session, self.screens)
            self.drivers.append(driver)
            return driver

        def attach(driver, video=None, mode=None):
            clock = Clock(changed_at)
            driver.frame_clock = clock
            self.clocks.append(clock)
            return clock
        for target, value in (("build_target_driver", build), ("attach_frame_clock", attach),
                              ("_lease_free", lambda url: lease_free)):
            p = patch.object(prewarm, target, value)
            p.start()
            testcase.addCleanup(p.stop)
        testcase.addCleanup(prewarm.reset_for_tests)

        def session():
            if session_error is not None:
                raise session_error
            return "s1"
        self.phone = SimpleNamespace(wda_url=URL, wda_session=session, video=object(), primary=True, key="p")
        self.busy = None
        self.runtime = SimpleNamespace(resolve_device=lambda device: self.phone, run_on=lambda phone: self.busy)


def ctx(bundle=SMS):
    return harness_api.RunContext(run_id="a" * 12, goal="g", engine="smart", origin="app", app_bundle=bundle,
                                  device_id=None, device_kind="usb", extras=harness_api.frozen_mapping({}),
                                  data_dir=None, emit=lambda e: None, clarify=lambda r: "denied",
                                  cancelled=lambda: False, runtime=None)


class PrewarmTests(unittest.TestCase):
    def setUp(self):
        prewarm.reset_for_tests()

    def test_it_warms_the_session_and_one_screen_and_the_first_read_reuses_it(self):
        fakes = Fakes(self)
        self.assertEqual(prewarm.prewarm(fakes.runtime), {"warmed": ["wda", "screen"], "pending": False})
        warm_driver = fakes.drivers[0]
        self.assertTrue(warm_driver.closed)
        self.assertIsNone(warm_driver.frame_clock)   # the clock outlives the driver, for 3 s
        self.assertEqual(warm_driver.tuned, (True, True))
        run_driver = Phone(URL, "s1", [chat()])
        taken = prewarm.take(run_driver, ctx())
        self.assertIsNotNone(taken)
        snapshot, age = taken
        self.assertEqual(snapshot.bundle_id, SMS)
        self.assertLess(age, 1)
        self.assertTrue(fakes.clocks[0].closed)
        self.assertIsNone(prewarm.take(run_driver, ctx()))   # once

    def test_reuse_needs_an_unchanged_screen_the_same_session_and_app_and_a_fresh_read(self):
        cases = {"another session": (Phone(URL, "s2", [chat()]), ctx(), None),
                 "another app": (Phone(URL, "s1", [chat()]), ctx("com.apple.Preferences"), None),
                 "home screen start": (Phone(URL, "s1", [chat()]), ctx(None), None),
                 "too old": (Phone(URL, "s1", [chat()]), ctx(), time.monotonic() + prewarm.SCREEN_TTL + 1),
                 "another phone": (Phone("http://127.0.0.1:8100", "s1", [chat()]), ctx(), None)}
        for name, (driver, context, now) in cases.items():
            with self.subTest(name):
                prewarm.reset_for_tests()
                fakes = Fakes(self)
                prewarm.prewarm(fakes.runtime)
                self.assertIsNone(prewarm.take(driver, context, now=now))
        prewarm.reset_for_tests()
        fakes = Fakes(self, changed_at=time.monotonic() + 3600)   # the stream changed after the read began
        prewarm.prewarm(fakes.runtime)
        self.assertIsNone(prewarm.take(Phone(URL, "s1", [chat()]), ctx()))
        self.assertTrue(all(clock.closed for clock in fakes.clocks))

    def test_an_owner_who_picks_up_the_phone_mid_warm_up_gets_no_session(self):
        fakes = Fakes(self)
        with patch.object(prewarm, "_owner_active", side_effect=[False, True]):
            answer = prewarm.prewarm(fakes.runtime)
        self.assertEqual((answer["warmed"], answer["reason"]), ([], "owner_active"))
        self.assertEqual(fakes.drivers, [])
        self.assertEqual(prewarm._screens, {})

    def test_a_busy_phone_is_left_alone(self):
        fakes = Fakes(self)
        fakes.busy = "b" * 12
        self.assertEqual(prewarm.prewarm(fakes.runtime), {"warmed": [], "pending": False, "reason": "busy"})
        prewarm.reset_for_tests()
        fakes = Fakes(self, lease_free=False)   # another Mobster process drives it
        self.assertEqual(prewarm.prewarm(fakes.runtime)["warmed"], [])
        self.assertEqual(fakes.drivers, [])

    def test_the_frontier_takes_the_warm_read_as_its_first_screen(self):
        fakes = Fakes(self)
        prewarm.prewarm(fakes.runtime)
        driver = Phone(URL, "s1", [chat()] * 4)
        events = []
        FrontierAgent(driver, FrontierScript([("TAP", "Sam", None), ("DONE", None, None), ("DONE", None, None)]),
                      emit=events.append, screenshots=False, settle_seconds=0, skills=(),
                      run_context=ctx()).run("Open Sam's chat")
        used = [e for e in events if e["event"] == "prewarm_used"]
        self.assertEqual(len(used), 1)
        self.assertIsInstance(used[0]["age_ms"], int)
        kinds = [e["event"] for e in events]
        self.assertLess(kinds.index("prewarm_used"), kinds.index("frontier_decision"))


class RouteTests(SmartBase):
    def test_the_route_answers_202_and_never_5xx_for_a_cold_phone(self):
        isolate(self)
        runtime = self.runtime()
        handler = make_handler(runtime)
        with patch("mobile_agent.runtime_driver.resolve_wda_session", side_effect=ConnectionRefusedError("down")), \
                patch.object(prewarm, "_lease_free", lambda url: True):
            status, data, _ = request(handler, "POST", "/api/prewarm", {})
        self.assertEqual(status, 202)
        self.assertEqual(data["warmed"], [])
        self.assertEqual(request(handler, "POST", "/api/prewarm", {"device": 3})[0], 400)
        self.assertEqual(request(handler, "POST", "/api/prewarm", {"x": 1})[0], 400)
        prewarm.reset_for_tests()


# Assumed latencies, in seconds at full scale (stated, not measured on a phone; the owner's test plan measures them):
# WDA creating a session after the helper starts, /status when one exists, configure on a new process (settings and
# /wda/screen) and after, a settled read (two agreeing reads), the task contract call and a decision (B3, 5 Oct).
LATENCY = {"session_cold": 1.5, "session_warm": .05, "configure_cold": .6, "configure_warm": .05,
           "observe_ready": .8, "contract": 2.6, "decision": 2.4}


class SlowModel:
    def __init__(self, delay):
        self.delay = delay
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}
        self.model, self.prompts = "gpt-5.6-sol", []

    def complete(self, messages, schema, timeout=60, **kwargs):
        planning = "items" in schema["properties"] and "actions" not in schema["properties"]
        self.delay(LATENCY["contract" if planning else "decision"])
        if planning:
            return {"items": [{"kind": "READ", "app": "Messages", "what": "Sam", "payload": "", "act": "none",
                               "count": "", "condition": "", "quote": "Sam"}]}, {}
        self.usage["calls"] += 1
        prompt = prompt_text(messages)
        self.prompts.append(prompt)
        rows = prompt.split("Screen elements:\n", 1)[1].splitlines()
        first = self.usage["calls"] == 1
        target = next((r.split()[0] for r in rows if '"Sam"' in r), None)
        return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [],
                "actions": [{"operation": "TAP" if first else "DONE", "target": target if first else None,
                             "target_label": None, "text": None, "app": None}],
                "answer": None if first else "Sam's chat is open."}, {}


class FirstActionTests(SmartBase):
    def first_action(self, runtime, *, cold, warm_first):
        state = {"session": not cold, "size": not cold}
        drivers = []
        elapsed = [0.0]

        def delay(seconds):
            elapsed[0] += seconds  # count only fake work; runtime freshness and deadlines keep their real clocks

        def resolve(url, preferred=None, timeout=5, create=True):
            delay(LATENCY["session_warm" if state["session"] else "session_cold"])
            state["session"] = True
            return "s1"

        def build(wda_url=None, session=None, **kwargs):
            delay(LATENCY["configure_warm" if state["size"] else "configure_cold"])
            state["size"] = True
            driver = Phone(wda_url, session, [chat()] * 6, observe_s=LATENCY["observe_ready"] / 2,
                           delay=delay, action_clock=lambda: elapsed[0])
            drivers.append(driver)
            return driver

        def attach(driver, video=None, mode=None):
            driver.frame_clock = Clock(0.0)   # nothing on screen moves in this harness
            return driver.frame_clock
        model = SlowModel(delay)
        patches = [patch("mobile_agent.runtime_driver.resolve_wda_session", side_effect=resolve),
                   patch("mobile_agent.runtime_driver.build_target_driver", side_effect=build),
                   patch.object(prewarm, "build_target_driver", side_effect=build),
                   patch.object(prewarm, "attach_frame_clock", side_effect=attach),
                   patch.object(prewarm, "_lease_free", lambda url: True),
                   patch("mobile_agent.runtime_driver.prepare_wda_phone"),
                   patch("mobile_agent.engines.build_client", return_value=model),
                   patch("mobile_agent.frontier.screen.video_frame", return_value=None)]
        for p in patches:
            p.start()
        try:
            if warm_first:
                self.assertIn("wda", prewarm.prewarm(runtime, wait=5)["warmed"])
            run = runtime.create("messages", "Open Sam's chat", "live", origin="app")
            self.worker.stop()
            started = elapsed[0]
            try:
                runtime.work(run)
            finally:
                self.worker.start()
        finally:
            for p in reversed(patches):
                p.stop()
            prewarm.reset_for_tests()
        if run.lease:
            run.lease.close()
            run.lease = None
        runtime.listeners.flush(5)
        phone = drivers[-1]
        self.assertIsNotNone(phone.first_action_at, [e["event"] for e in run.events])
        return phone.first_action_at - started, run

    def test_prewarm_removes_cold_session_configuration_and_first_read_work(self):
        runtime = self.runtime()
        without, _ = self.first_action(runtime, cold=True, warm_first=False)
        with_prewarm, run = self.first_action(self.runtime(), cold=True, warm_first=True)
        self.assertIn("prewarm_used", [e["event"] for e in run.events])
        saved = (LATENCY["session_cold"] - LATENCY["session_warm"]
                 + LATENCY["configure_cold"] - LATENCY["configure_warm"] + LATENCY["observe_ready"])
        self.assertAlmostEqual(without - with_prewarm, saved)


def context(origin="app", extras=None, runtime=None, clarify=lambda request: "answer:x"):
    return harness_api.RunContext(run_id="a" * 12, goal="Text Sam", engine="smart", origin=origin, app_bundle=None,
                                  device_id=None, device_kind="usb", extras=harness_api.frozen_mapping(extras or {}),
                                  data_dir=None, emit=lambda event: None, clarify=clarify,
                                  cancelled=lambda: False, runtime=runtime)


class Tool:

    def __init__(self, op, *, available=True, interactive_only=False, prompt=None):
        self.op, self.needs_target, self.interactive_only = op, False, interactive_only
        self.prompt = prompt if prompt is not None else f"- {op}: does a thing."
        self._available = available

    def available(self, ctx):
        if isinstance(self._available, Exception):
            raise self._available
        return self._available

    def prepare(self, ctx, act, snapshot):
        return Prepared(None)

    def perform(self, ctx, prepared, act, snapshot):
        return SkillResult("done")


class PolicyTests(unittest.TestCase):
    def test_defaults_pass_and_registered_tools_follow_the_rules_in_order(self):
        defaults = tuple(default_skills())
        tools = (Tool("READ_ATTACHMENT"), Tool("TAP"), Tool("READ_ATTACHMENT"), Tool("bad op"),
                 Tool("PUT_FILE", available=False), Tool("SAVE_SCREENSHOT", available=RuntimeError("x")),
                 Tool("ASK_ME", interactive_only=True), Tool("TWO_LINES", prompt="- TWO_LINES: a\nb"),
                 Tool("WRONG", prompt="- OTHER: not mine"), Tool("NO_DASH", prompt="NO_DASH: fine too"))
        kept = offered(defaults + tools, run_context=context("mcp"), driver=Driver([]))
        self.assertEqual([s.op for s in kept], [s.op for s in defaults] + ["READ_ATTACHMENT", "NO_DASH"])
        kept = offered(defaults + tools, run_context=context("app"), driver=Driver([]))
        self.assertIn("ASK_ME", [s.op for s in kept])

    def test_a_tools_line_is_in_the_prompt_only_when_it_is_offered(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-000000000000"}):
            client = engines.build_client(key="sk-test-000000000000")
            watched = engines.build_frontier(Driver([]), client, apps={}, emit=lambda e: None,
                                             skills=tuple(default_skills()) + (AskUser(),), run_context=context("app"))
            unattended = engines.build_frontier(Driver([]), client, apps={}, emit=lambda e: None,
                                                skills=tuple(default_skills()) + (AskUser(),),
                                                run_context=context("workflow"))
        self.assertIn("ASK_USER", [s.op for s in watched.skills])
        self.assertIn(PROMPT, system_prompt(watched.skills))
        self.assertNotIn("ASK_USER", [s.op for s in unattended.skills])
        self.assertEqual(system_prompt(unattended.skills), system_prompt(default_skills()))


class AskUserUnitTests(unittest.TestCase):
    def test_answers_become_feedback(self):
        choices = [{"id": "c1", "label": "Sam Lee"}]
        self.assertEqual(feedback_for("answer:the one from work", choices)[0],
                         "The user answered your question: 'the one from work'. Go on with that.")
        self.assertEqual(feedback_for("choice:c1", choices)[0],
                         "The user answered your question: 'Sam Lee'. Go on with that.")
        self.assertEqual(feedback_for("timeout", choices), (NO_ANSWER, "approval_timeout"))
        self.assertEqual(feedback_for("stopped", choices)[1], "stopped")
        self.assertIn("chose not to answer", feedback_for("denied", choices)[0])

    def test_at_most_two_questions_and_never_without_an_asker(self):
        asked = []
        tool = AskUser()
        ctx = SimpleNamespace(clarify=lambda request: asked.append(request) or "answer:Lee")
        for _ in range(3):
            result = tool.perform(ctx, None, {"text": "Which Sam? | Sam Lee | Sam Ortiz"}, None)
        self.assertEqual(len(asked), 2)
        self.assertIn("already asked the user twice", result.feedback)
        self.assertEqual((asked[0]["kind"], asked[0]["operation"], asked[0]["label"], asked[0]["allow_text"]),
                         ("clarify", "ASK_USER", "Which Sam?", True))
        self.assertIn("can't ask",
                      AskUser().perform(SimpleNamespace(clarify=None), None, {"text": "Q?"}, None).feedback)
        self.assertIn("needs the question", AskUser().perform(ctx, None, {"text": "  "}, None).feedback)

    def test_a_secret_never_goes_into_the_question(self):
        asked = []
        AskUser().perform(SimpleNamespace(clarify=lambda r: asked.append(r) or "denied"), None,
                          {"text": "Is the code 482913 the right one?"}, None)
        self.assertNotIn("482913", repr(asked))


class OfferTests(Base):
    def setUp(self):
        super().setUp()
        isolate(self)
        self.app = self.runtime()

    def test_offered_only_where_a_person_watches_and_the_setting_allows(self):
        shows = {"askUser": True}   # the surface can show a question
        for origin in ("app", "tui", "cli"):
            self.assertIsInstance(factory(context(origin, shows, runtime=self.app)), AskUser)
        for origin in ("mcp", "workflow", "schedule", "api"):
            self.assertIsNone(factory(context(origin, shows, runtime=self.app)))
        self.assertIsNone(factory(context("app", {"askUser": False}, runtime=self.app)))
        self.assertIsNone(factory(context("app", runtime=self.app)))   # a surface that can't show one never gets one
        settings.save(self.app, {"askUser": False})
        self.assertIsNone(factory(context("app", shows, runtime=self.app)))
        settings.save(self.app, {"askUser": True})
        with patch.dict(os.environ, {"MOBSTER_ASK_USER": "off"}):
            self.assertIsNone(factory(context("app", shows, runtime=self.app)))
        self.assertIsInstance(factory(context("app", shows, runtime=self.app)), AskUser)

    def test_the_setting_route(self):
        handler = make_handler(self.app)
        status, data, _ = request(handler, "GET", "/api/harness")
        self.assertEqual((status, data["askUser"], data["askUserLimit"], data["pauseLimitSeconds"]),
                         (200, True, 2, 600))
        self.assertEqual((data["ledger"], data["earlyLaunch"], data["longRun"]), (False, False, None))
        status, data, _ = request(handler, "POST", "/api/harness", {"askUser": False})
        self.assertEqual((status, data["askUser"]), (200, False))
        self.assertFalse(settings.ask_user(self.app))
        for bad in ({}, {"askUser": "no"}, {"askUser": True, "other": 1}):
            self.assertEqual(request(handler, "POST", "/api/harness", bad)[0], 400)


class FrontierQuestionTests(unittest.TestCase):
    def test_an_answer_never_approves_the_send_after_it(self):
        body = Element("1", "Message", "TextField", (.1, .8, .6, .05), editable=True,
                       actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        send = Element("2", "Send", "Button", (.8, .8, .1, .05))
        rows = [screen(body, send, bundle="com.apple.MobileSMS")] * 10
        approvals = []
        driver = Driver(rows)
        script = FrontierScript([("ASK_USER", None, "Which Sam? | Sam Lee | Sam Ortiz"),
                                 [("TYPE", "Message", "On my way")], ("TAP", "Send", None), ("DONE", None, None),
                                 ("DONE", None, None)])
        result = FrontierAgent(driver, script, screenshots=False, settle_seconds=0, skills=(AskUser(),),
                               apps={"com.apple.MobileSMS": "Messages"},
                               clarify=lambda request: "answer:yes send it",
                               approve=lambda request: approvals.append(request) or "denied").run(
            "Send Sam a message that I'm on my way")
        self.assertEqual(len(approvals), 1)                      # the send still asked
        self.assertEqual(approvals[0]["label"], "Send")
        self.assertNotIn("Send", [a[1] for a in driver.actions])  # and was declined, so never tapped
        self.assertEqual(result["status"], "approval_denied")

    def test_waiting_for_an_answer_never_spends_the_tasks_time_and_no_answer_stops_safely(self):
        thing = screen(Element("1", "Thing", "StaticText", (.1, .3, .8, .05)))

        def slow(request):
            time.sleep(.4)
            return "timeout"
        events = []
        result = FrontierAgent(Driver([thing] * 4), FrontierScript([("ASK_USER", None, "Which one?")]),
                               emit=events.append, screenshots=False, settle_seconds=0, skills=(AskUser(),),
                               clarify=slow).run("Open the thing")
        self.assertEqual((result["status"], result["reason"]), ("approval_timeout", NO_ANSWER))
        self.assertLess(result["elapsed"], bound(.3))   # the .4 s wait isn't the task's time
        self.assertIn(("skill_finished", False), [(e["event"], e.get("ok")) for e in events
                                                  if e["event"] == "skill_finished"])


class SmartRunQuestionTests(SmartBase):
    def test_a_question_in_a_task_from_the_app_goes_through_the_run_and_its_answer_stays_out_of_the_journal(self):
        runtime = self.runtime()
        run = runtime.create("messages", "Text Sam I'm on my way", "live", origin="app", extras={"askUser": True})

        def answer():
            for _ in range(500):
                pending = run.public()["approval"]
                if pending:
                    run.answer_approval(pending["id"], True, choice="c2")
                    return
                time.sleep(.01)
        thread = threading.Thread(target=answer)
        thread.start()
        script = Script([[("ASK_USER", None, "Which Sam? | Sam Lee | Sam Ortiz")], [("DONE", None, None)],
                         [("DONE", None, None)]], [item(kind="READ", act="none", what="Sam", quote="Sam")],
                        answer="Asked.")
        self.work(runtime, run, script, screens=[COMPOSE] * 8)
        thread.join(5)
        asked = next(e for e in run.events if e["event"] == "approval_requested")
        self.assertEqual((asked["kind"], asked["operation"], asked["label"], asked["choices"]),
                         ("clarify", "ASK_USER", "Which Sam?", ["c1", "c2"]))
        self.assertIn("The user answered your question: 'Sam Ortiz'", script.prompts[1])
        resolved = next(e for e in run.events if e["event"] == "approval_resolved")
        self.assertEqual(resolved["decision"], "choice:c2")
        steps = [e["text"] for e in run.events if e["event"] == "step"]
        self.assertIn("Asked you a question", steps)

    def test_no_question_tool_where_nobody_can_answer_one(self):
        for origin, extras in (("api", {"askUser": True}), ("app", None)):
            with self.subTest(origin=origin, extras=extras):
                runtime = self.runtime()
                run = runtime.create("messages", "Read Sam's message", "live", origin=origin, extras=extras)
                script = self.script()
                systems = []
                complete = script.complete
                script.complete = lambda messages, schema, timeout=60, complete=complete, systems=systems: (
                    systems.append(messages[0]["content"]), complete(messages, schema, timeout))[1]
                self.work(runtime, run, script)
                self.assertTrue(systems)
                self.assertTrue(all("ASK_USER" not in system for system in systems))


if __name__ == "__main__":
    unittest.main()

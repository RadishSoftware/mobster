"""Track harness, checkpoints and picking up (SPEC §3.1 F6): a checkpoint per turn (masked, redacted, at most 64 KB),
dropped when the task can't be picked up; POST /api/runs/{id}/pickup starts a new run that reads the screen before
its first decision, never repeats an action without one, and still asks before a send. Offline, on fakes."""

import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import harness_api
from mobile_agent.agent_hooks import READY
from mobile_agent.harness import checkpoints
from mobile_agent.harness.checkpoints import PICKUP_FEEDBACK
from mobile_agent.journal import Journal
from mobile_agent.server import Run, Runtime, make_handler
from mobile_agent.tests.seam_support import request
from mobile_agent.tests.seam_tasks import COMPOSE, SmartBase, Script, item
from mobile_agent.tests.test_server_hardening import APP


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


class UnpluggingPhone(SmartBase.Phone):
    """A phone that is unplugged by the action that taps Send: every read after it fails as a lost runner does."""

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

    def unplugged_run(self, runtime):
        """A task the cable came out of right after the person approved its Send."""
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
        # The Send still asked, and declined, it never ran again.
        self.assertEqual(len(seen), 1)
        self.assertNotIn(("act", "TAP", "Send"), log)
        self.assertEqual(new.status, "approval_denied")
        prompt = script.prompts[0]
        self.assertIn(PICKUP_FEEDBACK, prompt)
        self.assertIn("the user said OK to: step 1: sending a message", prompt)
        self.assertIn("0.0: TAP 'Message'", prompt)            # the old history goes on
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
        with patch("mobile_agent.frontier._redacted", side_effect=lambda value: (on_agent.append(1), real(value))[1]), \
                patch.object(checkpoints, "keep", side_effect=lambda ctx, state: (kept.append(state), keep(ctx, state))[1]):
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


class PreRunTests(unittest.TestCase):
    def test_no_checkpoint_means_no_handoff(self):
        events = []
        runtime = SimpleNamespace(journal=None, runs={})
        ctx = harness_api.RunContext(run_id="b" * 12, goal="g", engine="smart", origin="app", app_bundle=None,
                                     device_id=None, device_kind=None,
                                     extras=harness_api.frozen_mapping({"resumeFrom": "a" * 12}), data_dir=None,
                                     emit=events.append, clarify=lambda r: "denied", cancelled=lambda: False,
                                     runtime=runtime)
        self.assertIsNone(checkpoints.pre_run(ctx, None, None))
        self.assertEqual(events, [{"event": "context_error", "key": "pickup", "error": "no_checkpoint"}])
        self.assertEqual(checkpoints.frontier_options(ctx), {})


if __name__ == "__main__":
    unittest.main()

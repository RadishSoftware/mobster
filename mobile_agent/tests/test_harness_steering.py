import threading
import time
import unittest
from unittest.mock import patch

from mobile_agent.agent_hooks import READY, GuardVerdict
from mobile_agent.api_errors import APIError
from mobile_agent.frontier import PAUSED_FEEDBACK, STEER_FEEDBACK, STOPPED_AFTER, FrontierAgent, prompt_text
from mobile_agent.harness import control
from mobile_agent.journal import Journal
from mobile_agent.run import Run
from mobile_agent.run_executor import Cancelled, RunExecutor
from mobile_agent.server import make_handler
from mobile_agent.state import Element
from mobile_agent.steering import SteeringQueue
from mobile_agent.tests.seam_support import isolate, request
from mobile_agent.tests.test_frontier import Driver, Script, screen
from mobile_agent.tests.test_server_engine import Base
from mobile_agent.tests.test_server_hardening import APP
from mobile_agent.tests.timing import bound


def rows(n=8):
    return [screen(Element("1", f"Row {i}", "Button", (.1, .2, .8, .05))) for i in range(n)]


def recording(script):
    prompts, complete = [], script.complete
    script.complete = lambda messages, schema, timeout=60: (
        prompts.append(prompt_text(messages)) if "actions" in schema["properties"] else None,
        complete(messages, schema))[1]
    return prompts


class Watched(Driver):
    def __init__(self, screens):
        super().__init__(screens)
        self.log = []

    def observe(self, timeout=10):
        self.log.append(("observe", time.monotonic()))
        return super().observe(timeout)

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.log.append(("act", time.monotonic()))
        super().execute(operation, target, snapshot, text, timeout)


def agent(driver, script, queue, events=None, **kwargs):
    return FrontierAgent(driver, script, emit=(events.append if events is not None else None), screenshots=False,
                         settle_seconds=0, skills=(), steering=queue, **kwargs)


class PauseTests(unittest.TestCase):
    def test_a_pause_holds_at_the_next_boundary_touches_nothing_and_looks_again(self):
        queue = SteeringQueue()
        control.set_paused(queue, True)
        driver = Watched(rows())
        script = Script([("TAP", "Row 0", None), ("DONE", None, None), ("DONE", None, None)])
        prompts = recording(script)
        events = []
        resumed = {}

        def go_on():
            time.sleep(.3)
            resumed["at"] = time.monotonic()
            control.set_paused(queue, False)
        thread = threading.Thread(target=go_on)
        thread.start()
        result = agent(driver, script, queue, events).run("Tap the first row")
        thread.join(5)
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds.count("paused"), 1)
        self.assertEqual(kinds.count("continued"), 1)
        self.assertLess(kinds.index("paused"), kinds.index("continued"))
        self.assertLess(kinds.index("continued"), kinds.index("frontier_decision"))
        during = [kind for kind, at in driver.log if 0 < resumed["at"] - at < .25]
        self.assertEqual(during, [])
        after = [kind for kind, at in driver.log if at >= resumed["at"]]
        self.assertEqual(after[0], "observe")
        self.assertIn(PAUSED_FEEDBACK, prompts[0])
        self.assertIn("This turn so far: the user paused you, then let you go on", prompts[0])
        self.assertEqual(result["status"], "completed")
        self.assertLess(result["elapsed"], bound(.25))

    def test_a_pause_mid_chain_stops_the_chain_and_the_model_decides_again(self):
        queue = SteeringQueue()

        class Pausing(Driver):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                super().execute(operation, target, snapshot, text, timeout)
                if len(self.actions) == 1:
                    control.set_paused(queue, True)
                    threading.Timer(.15, control.set_paused, args=(queue, False)).start()
        driver = Pausing(rows())
        script = Script([[("TAP", "Row 0", None), ("TAP", "Row 1", None)], ("DONE", None, None),
                         ("DONE", None, None)])
        events = []
        agent(driver, script, queue, events).run("Tap two rows")
        self.assertEqual([a[:2] for a in driver.actions], [("TAP", "Row 0")])
        self.assertIn("pause", [e.get("reason") for e in events if e["event"] == "frontier_chunk_stop"])
        self.assertEqual([e["event"] for e in events if e["event"] in ("paused", "continued")], ["paused", "continued"])

    def test_ten_minutes_paused_stops_safely(self):
        queue = SteeringQueue()
        control.set_paused(queue, True)
        driver = Watched(rows())
        with patch.object(control, "PAUSE_LIMIT_SECONDS", .2):
            result = agent(driver, Script([("TAP", "Row 0", None)]), queue).run("Tap the first row")
        self.assertEqual(result["status"], "stopped")
        self.assertIn("Paused for 10 minutes", result["reason"])
        self.assertEqual([kind for kind, _ in driver.log if kind == "act"], [])

    def test_after_a_long_pause_the_guard_looks_before_anything_is_read_or_done(self):
        class LockedGuard:
            def __init__(self):
                self.causes = []

            def check(self, driver, *, cause):
                self.causes.append(cause)
                if cause == "resume":
                    return GuardVerdict("stop", "phone_locked", "Your iPhone is locked. Unlock it and try again.")
                return READY

            def attached(self):
                return True

            def allows_app(self, bundle_id):
                return True

            def finish(self, driver):
                pass

        for paused_for, locked in ((.3, True), (.05, False)):
            with self.subTest(paused_for=paused_for):
                queue = SteeringQueue()
                control.set_paused(queue, True)
                threading.Timer(paused_for, control.set_paused, args=(queue, False)).start()
                driver, guard = Watched(rows()), LockedGuard()
                with patch("mobile_agent.frontier.interrupts.RESUME_AFTER_SECONDS", .2):
                    result = agent(driver, Script([("TAP", "Row 0", None), ("DONE", None, None),
                                                   ("DONE", None, None)]), queue, guard=guard).run("Tap the first row")
                if locked:
                    self.assertIn("resume", guard.causes)
                    self.assertEqual((result["status"], result["code"]), ("blocked", "phone_locked"))
                    self.assertEqual(driver.actions, [])
                else:
                    self.assertNotIn("resume", guard.causes)
                    self.assertEqual(result["status"], "completed")

    def test_stop_while_paused_stops_at_once(self):
        queue = SteeringQueue()
        control.set_paused(queue, True)
        stop = threading.Event()
        threading.Timer(.1, stop.set).start()
        started = time.monotonic()
        result = agent(Driver(rows()), Script([("TAP", "Row 0", None)]), queue, cancelled=stop.is_set).run("Tap")
        self.assertEqual(result["status"], "stopped")
        self.assertLess(time.monotonic() - started, bound(1.0))


class StopAfterStepTests(unittest.TestCase):
    def test_the_step_in_progress_finishes_and_nothing_new_starts(self):
        queue = SteeringQueue()

        class Asking(Driver):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                if not self.actions:
                    control.request_stop_after(queue)
                super().execute(operation, target, snapshot, text, timeout)
        driver = Asking(rows())
        script = Script([[("TAP", "Row 0", None), ("TAP", "Row 1", None)], ("TAP", "Row 2", None),
                         ("DONE", None, None)])
        events = []
        result = agent(driver, script, queue, events).run("Tap three rows")
        self.assertEqual([a[:2] for a in driver.actions], [("TAP", "Row 0")])
        self.assertEqual((result["status"], result["reason"]), ("stopped", STOPPED_AFTER["reason"]))
        self.assertEqual(script.usage["calls"], 1)
        self.assertEqual(len([e for e in events if e["event"] == "frontier_decision"]), 1)

    def test_it_ends_a_pause_too(self):
        queue = SteeringQueue()
        control.set_paused(queue, True)
        threading.Timer(.1, control.request_stop_after, args=(queue,)).start()
        result = agent(Driver(rows()), Script([("TAP", "Row 0", None)]), queue).run("Tap")
        self.assertEqual(result["reason"], STOPPED_AFTER["reason"])

    def test_a_stop_asked_before_the_first_step_never_acts(self):
        queue = SteeringQueue()
        control.request_stop_after(queue)
        driver = Driver(rows())
        result = agent(driver, Script([("DONE", None, None), ("DONE", None, None)]), queue).run("What's on screen?")
        self.assertEqual(driver.actions, [])
        self.assertEqual(result["status"], "stopped")


class OrderTests(unittest.TestCase):
    def test_new_messages_are_never_dropped_before_the_model_reads_them(self):
        queue = SteeringQueue()
        first, second = "a" * 1500, "b" * 1500
        queue.put(first)
        queue.put(second)
        script = Script([("TAP", "Row 0", None), ("TAP", "Row 1", None), ("DONE", None, None), ("DONE", None, None)])
        prompts = recording(script)
        agent(Driver(rows()), script, queue).run("Tap rows")
        section = prompts[0].split("Messages from the user while you work (newest last):\n", 1)[1]
        self.assertLess(section.index(first), section.index(second))
        later = prompts[1].split("Messages from the user while you work (newest last):\n", 1)[1]
        self.assertIn(second, later)
        self.assertNotIn(first, later)

class RouteTests(Base):
    def setUp(self):
        super().setUp()
        isolate(self)
        import os
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        self.app = self.runtime()
        self.handler = make_handler(self.app)
        self.run_ = self.app.create("messages", "Text Sam I'm running late", "live", engine="smart", origin="app")

    def call(self, path, body, origin=None):
        headers = {"X-Mobster-Origin": origin} if origin else None
        return request(self.handler, "POST", path, body, headers=headers)

    def test_a_message_is_queued_tagged_and_journaled(self):
        status, data, _ = self.call(f"/api/runs/{self.run_.id}/messages", {"text": "  Say 7pm, not 6 "}, "app")
        self.assertEqual(status, 202)
        self.assertEqual((data["message"]["text"], data["message"]["source"]), ("Say 7pm, not 6", "app"))
        self.assertRegex(data["message"]["id"], r"^[0-9a-f]{12}$")
        self.assertEqual(self.run_.steering.pending(), 1)
        status, data, _ = self.call(f"/api/runs/{self.run_.id}/messages", {"text": "Make it short", "source": "voice"},
                                    "app")
        self.assertEqual((status, data["message"]["source"]), (202, "voice"))
        status, data, _ = self.call(f"/api/runs/{self.run_.id}/messages", {"text": "hi", "source": "voice"}, "tui")
        self.assertEqual(status, 400)
        status, data, _ = self.call(f"/api/runs/{self.run_.id}/messages", {"text": "from a script"})
        self.assertEqual((status, data["message"]["source"]), (202, "cli"))
        self.assertEqual([e["source"] for e in self.run_.events if e["event"] == "user_message"],
                         ["app", "voice", "cli"])

    def test_yes_send_it_while_an_approval_waits_approves_nothing_and_queues_nothing(self):
        self.run_.approval = {"id": "a" * 12, "kind": "commit", "operation": "TAP", "label": "Send"}
        status, data, _ = self.call(f"/api/runs/{self.run_.id}/messages", {"text": "yes send it"}, "app")
        self.assertEqual((status, data["code"]), (409, "approval_pending"))
        self.assertIsNone(self.run_.approval_answer)
        self.assertEqual(self.run_.steering.pending(), 0)
        self.assertNotIn("user_message", [e["event"] for e in self.run_.events])
        self.run_.approval = {"id": "b" * 12, "kind": "clarify", "operation": "ASK_USER", "label": "Which Sam?"}
        self.assertEqual(self.call(f"/api/runs/{self.run_.id}/messages", {"text": "Sam Lee"}, "app")[0], 202)

    def test_a_full_queue_is_429_and_a_finished_task_409(self):
        for index in range(SteeringQueue.MAX_PENDING):
            self.run_.steer(f"m{index}", "app")
        status, data, _ = self.call(f"/api/runs/{self.run_.id}/messages", {"text": "one more"}, "app")
        self.assertEqual((status, data["code"]), (429, "steering_full"))
        self.finish(self.app, self.run_, "completed")
        status, data, _ = self.call(f"/api/runs/{self.run_.id}/messages", {"text": "late"}, "app")
        self.assertEqual((status, data["code"]), (409, "run_not_active"))
        status, data, _ = self.call(f"/api/runs/{self.run_.id}/pause", {"paused": True}, "app")
        self.assertEqual((status, data["code"]), (409, "run_not_active"))
        status, _, _ = self.call("/api/runs/0123456789ab/messages", {"text": "hi"}, "app")
        self.assertEqual(status, 404)

    def test_only_mobsters_agent_on_a_phone_takes_messages_and_pauses_and_the_run_says_so(self):
        self.assertIs(self.run_.public()["steerable"], True)
        status, _, _ = self.call(f"/api/runs/{self.run_.id}/pause", {"paused": True}, "app")
        self.assertEqual(status, 200)
        for engine, mode in (("codex", "live"), ("claude_code", "live"), ("smart", "demo"), ("fast", "live")):
            with self.subTest(engine=engine, mode=mode):
                other = Run(dict(APP), "Open Settings", mode, engine=engine)
                self.app.runs[other.id] = other
                self.assertIs(other.public()["steerable"], False)
                status, data, _ = self.call(f"/api/runs/{other.id}/messages", {"text": "hi"}, "app")
                self.assertEqual((status, data["code"]), (409, "steer_unsupported"))
                status, data, _ = self.call(f"/api/runs/{other.id}/pause", {"paused": True}, "app")
                self.assertEqual((status, data["code"]), (409, "pause_unsupported"))
                self.assertEqual(other.steering.pending(), 0)

    def test_pause_continue_and_stop_after_step(self):
        status, data, _ = self.call(f"/api/runs/{self.run_.id}/pause", {"paused": True}, "app")
        self.assertEqual((status, data["paused"], data["run"]["id"]), (200, True, self.run_.id))
        self.assertTrue(self.run_.steering.paused.is_set())
        self.assertEqual(self.call(f"/api/runs/{self.run_.id}/pause", {"paused": False})[0], 200)
        self.assertFalse(self.run_.steering.paused.is_set())
        for bad in ({}, {"paused": "yes"}, {"paused": True, "x": 1}):
            self.assertEqual(self.call(f"/api/runs/{self.run_.id}/pause", bad)[0], 400)
        status, data, _ = self.call(f"/api/runs/{self.run_.id}/messages", {"control": "stop_after_step"}, "app")
        self.assertEqual((status, data), (202, {"control": "stop_after_step"}))
        self.assertTrue(control.stop_after_requested(self.run_.steering))
        self.assertEqual(self.run_.events[-1]["event"], "stop_after_step_requested")
        for bad in ({"control": "stop"}, {"control": "stop_after_step", "text": "x"}):
            self.assertEqual(self.call(f"/api/runs/{self.run_.id}/messages", bad)[0], 400)

    def test_stop_reaches_what_the_run_started_and_nothing_new_starts(self):
        workers = RunExecutor(self.run_.stop)
        helper = workers.submit("contract", workers.token.wait, bound(5))
        self.assertEqual(self.call(f"/api/runs/{self.run_.id}/stop", {}, "app")[0], 200)
        self.assertTrue(helper.result(bound(5)))
        with self.assertRaises(Cancelled):
            workers.submit("verify", print)
        self.assertTrue(workers.join(bound(5)))


def make_run(origin="app"):
    db = Journal(None)
    run = Run(dict(APP), "Find a table for two", "live", journal=db, engine="smart", origin=origin)
    db.create(run.metadata())
    return run, db


class SteeringQueueTests(unittest.TestCase):
    def test_limits(self):
        queue = SteeringQueue()
        for bad in ("", "   ", "x" * 2001):
            with self.subTest(text=bad[:10]), self.assertRaises(ValueError):
                queue.put(bad)
        with self.assertRaises(ValueError):
            queue.put("hi", source="robot")

    def test_close_returns_the_unread_and_refuses_more(self):
        queue = SteeringQueue()
        queue.put("first")
        queue.drain()
        queue.put("second", source="voice")
        unread = queue.close()
        self.assertEqual([(m.text, m.source) for m in unread], [("second", "voice")])
        self.assertEqual(queue.close(), [])
        with self.assertRaises(ValueError):
            queue.put("third")
        self.assertFalse(queue.paused.is_set())


class RunSteerTests(unittest.TestCase):
    def test_origins(self):
        run, db = make_run(origin="mcp")
        self.addCleanup(db.close)
        run.steer("from the agent", "mcp")
        run.steer("from the Mac app", "app")
        for source in ("tui", "cli", "voice"):
            with self.subTest(source=source), self.assertRaises(APIError) as caught:
                run.steer("hi", source)
            self.assertEqual(caught.exception.code, "steer_not_allowed")
        other, db2 = make_run(origin="app")
        self.addCleanup(db2.close)
        with self.assertRaises(APIError):
            other.steer("hi", "mcp")
        for source in ("app", "tui", "cli", "voice"):
            other.steer(f"hi from {source}", source)

    def test_a_finished_run_takes_no_message_and_reports_the_unread(self):
        run, db = make_run()
        self.addCleanup(db.close)
        first = run.steer("one", "app")
        second = run.steer("two", "tui")
        run.finish({"event": "result", "status": "completed"})
        kinds = [e["event"] for e in run.events]
        self.assertEqual(kinds[-2:], ["steer_unread", "run_finished"])
        self.assertEqual(run.events[-2]["ids"], [first["id"], second["id"]])


class FrontierSteeringTests(unittest.TestCase):
    def test_a_message_is_read_at_the_next_turn_and_kept_in_view(self):
        queue = SteeringQueue()
        queue.put("Only the ones from Kate Bell")
        script = Script([("TAP", "Row 0", None), ("TAP", "Row 1", None), ("DONE", None, None), ("DONE", None, None)])
        prompts = recording(script)
        events = []
        agent(Driver(rows(6)), script, queue, events).run("Tap the rows from my inbox")
        applied = [e for e in events if e["event"] == "steer_applied"]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["step"], 0)
        self.assertIn("Feedback on your last action: " + STEER_FEEDBACK, prompts[0])
        for prompt in prompts:
            self.assertIn("Messages from the user while you work (newest last):\n- Only the ones from Kate Bell",
                          prompt)
        self.assertNotIn(STEER_FEEDBACK, prompts[1])
        self.assertIn("0: the user wrote to you (1 message)", prompts[1])

    def test_a_message_breaks_a_chain_before_its_next_action(self):
        body = screen(Element("1", "Body", "TextView", (.1, .2, .8, .5), editable=True,
                              actions=("TAP", "TYPE", "TYPE_SUBMIT")))
        queue = SteeringQueue()

        class Writing(Driver):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                super().execute(operation, target, snapshot, text, timeout)
                if len(self.actions) == 1:
                    queue.put("Stop, wrong note")

        driver = Writing([body] * 6)
        script = Script([[("TAP", "Body", None), ("TYPE", "Body", "Q2 note")], ("DONE", None, None),
                         ("DONE", None, None)])
        events = []
        agent(driver, script, queue, events).run("Add a note about Q2 planning.")
        self.assertEqual([a[0] for a in driver.actions], ["TAP"])
        self.assertIn("steer", [e.get("reason") for e in events if e["event"] == "frontier_chunk_stop"])
        self.assertEqual(len([e for e in events if e["event"] == "steer_applied"]), 1)


if __name__ == "__main__":
    unittest.main()

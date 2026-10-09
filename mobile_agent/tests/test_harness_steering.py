"""Track harness, steering (SPEC §3.1 F1): pause and continue, stop after this step, messages in order with none lost,
the messages and pause routes, and approvals winning over anything typed. Offline, on fakes."""

import threading
import time
import unittest
from unittest.mock import patch

from mobile_agent import harness_api
from mobile_agent.agent_hooks import READY, GuardVerdict
from mobile_agent.frontier import PAUSED_FEEDBACK, STOPPED_AFTER, FrontierAgent, prompt_text
from mobile_agent.harness import control
from mobile_agent.server import Run, make_handler
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
    """The decision prompts ``script`` answers, in order."""
    prompts, complete = [], script.complete
    script.complete = lambda messages, schema, timeout=60: (
        prompts.append(prompt_text(messages)) if "actions" in schema["properties"] else None,
        complete(messages, schema))[1]
    return prompts


class Watched(Driver):
    """A Driver that logs reads and actions with their times."""

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
        # Nothing was read or done while paused, and the screen was read again after Continue.
        during = [kind for kind, at in driver.log if 0 < resumed["at"] - at < .25]
        self.assertEqual(during, [])
        after = [kind for kind, at in driver.log if at >= resumed["at"]]
        self.assertEqual(after[0], "observe")
        self.assertIn(PAUSED_FEEDBACK, prompts[0])
        self.assertIn("0: the user paused you, then let you go on", prompts[0])
        self.assertEqual(result["status"], "completed")
        # The wait never spends the task's time.
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
        # The phone may lock while the person has it paused (Auto-Lock is 30 s at its shortest): like after a long
        # approval, the phone guard looks first, and a locked phone stops the task (blocked, so it can be picked up).
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
                with patch("mobile_agent.frontier.RESUME_AFTER_SECONDS", .2):
                    result = agent(driver, Script([("TAP", "Row 0", None), ("DONE", None, None),
                                                   ("DONE", None, None)]), queue, guard=guard).run("Tap the first row")
                if locked:
                    self.assertIn("resume", guard.causes)
                    self.assertEqual((result["status"], result["code"]), ("blocked", "phone_locked"))
                    self.assertEqual(driver.actions, [])
                else:
                    self.assertNotIn("resume", guard.causes)   # a short pause costs no check
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
                    control.request_stop_after(queue)   # asked while the first action runs
                super().execute(operation, target, snapshot, text, timeout)
        driver = Asking(rows())
        script = Script([[("TAP", "Row 0", None), ("TAP", "Row 1", None)], ("TAP", "Row 2", None),
                         ("DONE", None, None)])
        result = agent(driver, script, queue).run("Tap three rows")
        self.assertEqual([a[:2] for a in driver.actions], [("TAP", "Row 0")])
        self.assertEqual((result["status"], result["reason"]), ("stopped", STOPPED_AFTER["reason"]))
        self.assertEqual(script.usage["calls"], 1)  # no model call after it either

    def test_it_ends_a_pause_too(self):
        queue = SteeringQueue()
        control.set_paused(queue, True)
        threading.Timer(.1, control.request_stop_after, args=(queue,)).start()
        result = agent(Driver(rows()), Script([("TAP", "Row 0", None)]), queue).run("Tap")
        self.assertEqual(result["reason"], STOPPED_AFTER["reason"])

    def test_a_finished_answer_still_finishes(self):
        queue = SteeringQueue()
        control.request_stop_after(queue)
        driver = Driver(rows())
        result = agent(driver, Script([("DONE", None, None), ("DONE", None, None)]), queue).run("What's on screen?")
        # Asked before anything ran: it stops at the first boundary, never acting.
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
        self.assertLess(section.index(first), section.index(second))   # in order, both shown
        # Later turns keep the newest within the budget.
        later = prompts[1].split("Messages from the user while you work (newest last):\n", 1)[1]
        self.assertIn(second, later)
        self.assertNotIn(first, later)

    def test_messages_arrive_in_order_across_turns(self):
        queue = SteeringQueue()
        texts = [f"note {i}" for i in range(4)]

        class Writing(Driver):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                super().execute(operation, target, snapshot, text, timeout)
                if len(self.actions) <= 2:
                    queue.put(texts[2 * len(self.actions) - 2])
                    queue.put(texts[2 * len(self.actions) - 1])
        script = Script([("TAP", "Row 0", None), ("TAP", "Row 1", None), ("TAP", "Row 2", None),
                         ("DONE", None, None), ("DONE", None, None)])
        prompts = recording(script)
        agent(Writing(rows()), script, queue).run("Tap rows")
        shown = prompts[-1].split("Messages from the user while you work (newest last):\n", 1)[1]
        self.assertEqual([line[2:] for line in shown.splitlines()[:4]], texts)


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
        status, data, _ = self.call(f"/api/runs/{self.run_.id}/messages", {"text": "Say 7pm, not 6"}, "app")
        self.assertEqual(status, 202)
        self.assertEqual((data["message"]["text"], data["message"]["source"]), ("Say 7pm, not 6", "app"))
        self.assertRegex(data["message"]["id"], r"^[0-9a-f]{12}$")
        self.assertEqual(self.run_.steering.pending(), 1)
        status, data, _ = self.call(f"/api/runs/{self.run_.id}/messages", {"text": "Make it short", "source": "voice"},
                                    "app")
        self.assertEqual((status, data["message"]["source"]), (202, "voice"))
        status, data, _ = self.call(f"/api/runs/{self.run_.id}/messages", {"text": "hi", "source": "voice"}, "tui")
        self.assertEqual(status, 400)  # voice comes only from the Mac app
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

    def test_quick_mode_takes_no_message_and_no_pause(self):
        quick = Run(dict(APP), "Open Settings", "live", engine="fast")
        self.app.runs[quick.id] = quick
        status, data, _ = self.call(f"/api/runs/{quick.id}/messages", {"text": "hi"}, "app")
        self.assertEqual((status, data["code"]), (409, "steer_unsupported"))
        status, data, _ = self.call(f"/api/runs/{quick.id}/pause", {"paused": True}, "app")
        self.assertEqual((status, data["code"]), (409, "pause_unsupported"))

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

    def test_the_routes_are_registered_by_the_track(self):
        self.assertEqual(harness_api.disabled("harness"), False)
        from mobile_agent import tracks
        self.assertEqual(tracks.STATUS.get("harness"), "ok")


if __name__ == "__main__":
    unittest.main()

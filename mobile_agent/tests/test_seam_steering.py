"""Seam S5/S7.2/S8: messages the user sends a run while it works. The queue's limits, Run.steer's rules (approvals
win, origins), and the frontier's default use of them. Offline."""

import unittest

from mobile_agent.frontier import STEER_FEEDBACK, FrontierAgent, prompt_text
from mobile_agent.journal import Journal
from mobile_agent.server import APIError, Run
from mobile_agent.state import Element
from mobile_agent.steering import SteeringQueue
from mobile_agent.tests.test_frontier import Driver, Script, screen
from mobile_agent.tests.test_server_hardening import APP


def make_run(origin="app"):
    db = Journal(None)
    run = Run(dict(APP), "Find a table for two", "live", journal=db, engine="smart", origin=origin)
    db.create(run.metadata())
    return run, db


class QueueTests(unittest.TestCase):
    def test_limits(self):
        queue = SteeringQueue()
        for bad in ("", "   ", "x" * 2001):
            with self.subTest(text=bad[:10]), self.assertRaises(ValueError):
                queue.put(bad)
        with self.assertRaises(ValueError):
            queue.put("hi", source="robot")
        for index in range(SteeringQueue.MAX_PENDING):
            queue.put(f"message {index}")
        with self.assertRaisesRegex(ValueError, "hasn't read"):
            queue.put("one more")
        self.assertEqual(queue.pending(), 10)
        drained = queue.drain()
        self.assertEqual([m.text for m in drained], [f"message {i}" for i in range(10)])
        self.assertEqual(queue.pending(), 0)
        self.assertRegex(drained[0].id, r"^[0-9a-f]{12}$")

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
    def test_a_message_is_journaled_and_queued(self):
        run, db = make_run()
        self.addCleanup(db.close)
        message = run.steer("  Only places with outdoor seating ", "app")
        self.assertEqual(message["text"], "Only places with outdoor seating")
        event = run.events[-1]
        self.assertEqual((event["event"], event["id"], event["source"]), ("user_message", message["id"], "app"))
        self.assertEqual(run.steering.pending(), 1)

    def test_an_approval_waiting_wins_and_nothing_is_queued(self):
        run, db = make_run()
        self.addCleanup(db.close)
        run.approval = {"id": "a" * 12, "kind": "commit", "operation": "TAP", "label": "Send"}
        with self.assertRaises(APIError) as caught:
            run.steer("Actually say 7pm", "app")
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "approval_pending"))
        self.assertIn("Don't send", str(caught.exception))
        self.assertEqual(run.steering.pending(), 0)
        self.assertNotIn("user_message", [e["event"] for e in run.events])
        run.approval = {"id": "b" * 12, "kind": "clarify", "operation": "ASK_USER", "label": "Which Sam?"}
        run.steer("Sam Lee", "app")  # a clarifying question doesn't block a message
        self.assertEqual(run.steering.pending(), 1)

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
        with self.assertRaises(APIError) as caught:
            run.steer("three", "app")
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "run_not_active"))


class FrontierSteeringTests(unittest.TestCase):
    def test_a_message_is_read_at_the_next_turn_and_kept_in_view(self):
        rows = [screen(Element("1", f"Row {i}", "Button", (.1, .2, .8, .05))) for i in range(6)]
        queue = SteeringQueue()
        queue.put("Only the ones from Kate Bell")
        script = Script([("TAP", "Row 0", None), ("TAP", "Row 1", None), ("DONE", None, None), ("DONE", None, None)])
        prompts, complete = [], script.complete
        script.complete = lambda messages, schema, timeout=60: (
            prompts.append(prompt_text(messages)) if "actions" in schema["properties"] else None,
            complete(messages, schema))[1]
        events = []
        FrontierAgent(Driver(rows), script, emit=events.append, screenshots=False, settle_seconds=0, skills=(),
                      steering=queue).run("Tap the rows from my inbox")
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
        rows = [screen(Element("1", "Body", "TextView", (.1, .2, .8, .5), editable=True,
                               actions=("TAP", "TYPE", "TYPE_SUBMIT")))] * 6
        queue = SteeringQueue()

        class Writing(Driver):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                super().execute(operation, target, snapshot, text, timeout)
                if len(self.actions) == 1:
                    queue.put("Stop, wrong note")

        driver = Writing(rows)
        script = Script([[("TAP", "Body", None), ("TYPE", "Body", "Q2 note")], ("DONE", None, None),
                         ("DONE", None, None)])
        events = []
        FrontierAgent(driver, script, emit=events.append, screenshots=False, settle_seconds=0, skills=(),
                      steering=queue).run("Add a note about Q2 planning.")
        self.assertEqual([a[0] for a in driver.actions], ["TAP"])
        self.assertIn("steer", [e.get("reason") for e in events if e["event"] == "frontier_chunk_stop"])
        self.assertEqual(len([e for e in events if e["event"] == "steer_applied"]), 1)


if __name__ == "__main__":
    unittest.main()

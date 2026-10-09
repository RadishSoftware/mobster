"""The terminal UI's engine (tui.session) and the narration it draws (narrate). No UI library needed."""

import os
import threading
import unittest
from unittest import mock

from mobile_agent.narrate import Approval, Narrator, Outcome, Step, completion_message, seconds, status_label, usd
from mobile_agent.tui.session import Session

ASK = {"MOBSTER_ASK_BEFORE_ACTING": "1", "MOBSTER_BYPASS_CHECKS": "0"}


def run_to_end(session, app_id, goal, answer=None):
    run = session.start(app_id, goal)
    events = []

    def seen(event):
        events.append(event)
        if event["event"] == "approval_requested" and answer is not None:
            threading.Thread(target=session.answer, args=(event["approval_id"], answer), kwargs={"run": run}).start()
    session.follow(run, seen)
    return run, events


@mock.patch.dict(os.environ, ASK)
class DemoSessionTests(unittest.TestCase):
    def setUp(self):
        self.session = Session(demo=True, pace=0)

    def tearDown(self):
        self.session.close()

    def test_the_runtime_asks_before_send_and_acts_on_yes(self):
        run, events = run_to_end(self.session, "messages", "Text Alex 'on my way'", answer=True)
        kinds = [e["event"] for e in events]
        self.assertEqual(run.status, "completed_unverified")
        self.assertIn("approval_requested", kinds)
        self.assertLess(kinds.index("approval_resolved"), len(kinds) - 1 - kinds[::-1].index("action_started"))
        self.assertEqual(kinds[-1], "run_finished")

    def test_no_means_nothing_is_sent(self):
        run, events = run_to_end(self.session, "messages", "Text Alex 'on my way'", answer=False)
        self.assertEqual(run.status, "approval_denied")
        started = [e for e in events if e["event"] == "action_started"]
        self.assertEqual(len(started), 2)  # the tap on Alex and the typing, never Send

    def test_without_ask_before_acting_it_does_not_ask(self):
        self.session.set_setting("askBeforeActing", False)
        run, events = run_to_end(self.session, "messages", "Text Alex 'on my way'")
        self.assertEqual(run.status, "completed_unverified")
        self.assertNotIn("approval_requested", [e["event"] for e in events])

    def test_history_is_newest_first_and_back_to_back_tasks_start(self):
        first, _ = run_to_end(self.session, "settings", "Turn on Dark Mode")
        second, _ = run_to_end(self.session, "safari", "Search for espresso")
        self.assertEqual([r.id for r in self.session.history()], [second.id, first.id])
        self.assertIs(self.session.find(second.id[:6]), second)

    def test_the_phone_preview_is_a_png(self):
        self.assertTrue(self.session.preview_image().startswith(b"\x89PNG"))
        run, _ = run_to_end(self.session, "settings", "Turn on Dark Mode")
        self.assertTrue(self.session.preview_image(run).startswith(b"\x89PNG"))

    def test_spend_cap_is_validated(self):
        self.session.set_spend_cap(0.5)
        self.assertEqual(self.session.spend_cap_usd, 0.5)
        with self.assertRaises(ValueError):
            self.session.set_spend_cap(-1)
        self.session.set_spend_cap(None)
        self.assertIsNone(self.session.spend_cap_usd)


@mock.patch.dict(os.environ, ASK)
class NarratorTests(unittest.TestCase):
    def test_a_demo_run_narrates_as_steps(self):
        session = Session(demo=True, pace=0)
        try:
            run, events = run_to_end(session, "messages", "Text Alex 'on my way'", answer=True)
        finally:
            session.close()
        narrator = Narrator()
        for event in events:
            narrator.feed(event)
        steps = [item for item in narrator.items if isinstance(item, Step)]
        self.assertEqual([s.title for s in steps[:3]], ["Tapped “Alex Rivera”", "Typed into “Message”", "Tapped “Send”"])
        self.assertTrue(all(s.state == "done" for s in steps))
        self.assertEqual([p for p, _ in steps[0].phases()], ["observe", "decide", "verify", "act"])
        approval = next(item for item in narrator.items if isinstance(item, Approval))
        self.assertEqual((approval.label, approval.decision), ("Send", "approved"))
        self.assertIsInstance(narrator.items[-1], Outcome)
        self.assertEqual(narrator.actions, 3)

    def test_costs_count_priced_and_unpriced_calls(self):
        narrator = Narrator()
        narrator.feed({"event": "inference_finished", "cost_nanodollars": 1_500_000})
        narrator.feed({"event": "inference_finished", "cost_nanodollars": None})
        self.assertEqual((narrator.cost_nanodollars, narrator.priced_calls, narrator.unpriced_calls),
                         (1_500_000, 1, 1))
        self.assertEqual(usd(narrator.cost_nanodollars), "$0.0015")

    def test_targets_resolve_against_the_preceding_screen_only(self):
        narrator = Narrator()
        narrator.feed({"event": "observation", "step": 0, "elements": [{"id": "3", "label": "Wi-Fi"}]})
        narrator.feed({"event": "decision", "step": 0, "operation": "TAP", "target": "3"})
        narrator.feed({"event": "observation", "step": 1, "elements": [{"id": "3", "label": "Bluetooth"}]})
        self.assertEqual(narrator.items[0].target, "Wi-Fi")

    def test_a_decision_never_sent_says_why_instead_of_spinning(self):
        narrator = Narrator()
        narrator.feed({"event": "observation", "step": 0, "elements": [{"id": "1", "label": "General"}]})
        narrator.feed({"event": "decision", "step": 0, "operation": "TAP", "target": "1"})
        narrator.feed({"event": "observation", "step": 1, "elements": []})
        first = narrator.items[0]
        self.assertEqual(first.state, "skipped")
        self.assertIn("decided again", first.note)
        narrator.feed({"event": "decision", "step": 1, "operation": "TAP", "target": "1"})
        narrator.feed({"event": "run_finished", "status": "preview", "summary": {"status": "preview"}})
        self.assertEqual(narrator.items[1].state, "skipped")
        self.assertIn("preview", narrator.items[1].note)

    def test_steps_that_did_not_act_collapse_into_one_calm_line(self):
        narrator = Narrator()
        for step in range(3):
            narrator.feed({"event": "observation", "step": step, "elements": []})
            narrator.feed({"event": "decision", "step": step, "operation": "BLOCKED" if step != 1 else "WAIT"})
            narrator.feed({"event": "helper", "purpose": "recovery"})
        steps = [item for item in narrator.items if isinstance(item, Step)]
        self.assertEqual(len(steps), 1)
        self.assertEqual((steps[0].tries, steps[0].state, steps[0].title), (3, "stalled", "Looking for a way forward"))
        self.assertEqual(sum(1 for item in narrator.items if not isinstance(item, Step)), 1)  # one helper note
        narrator.feed({"event": "run_finished", "status": "blocked",
                       "summary": {"status": "blocked", "reason": "No supported progress"}})
        outcome = narrator.items[-1]
        self.assertEqual((outcome.label, outcome.tone), ("Needs attention", "error"))  # the Mac app's label
        self.assertIn("Nothing on the screen moved the task forward", outcome.message)

    def test_route_notes_show_once(self):
        narrator = Narrator()
        for step in range(3):
            narrator.feed({"event": "route_decision", "step": step})
        self.assertEqual(len(narrator.items), 1)

    def test_words(self):
        self.assertEqual(status_label("completion_not_confirmed"), "Take a look")
        self.assertEqual(seconds(950), "950 ms")
        self.assertEqual(seconds(12_300), "12.3 s")
        self.assertIn("declined", completion_message("approval_denied"))


class RuntimeVideoPortTests(unittest.TestCase):
    def test_the_live_view_pairs_with_the_wda_port(self):
        from mobile_agent.server import Runtime
        from mobile_agent.tui.session import serve_config
        for wda, mjpeg in [("http://127.0.0.1:8100", "http://127.0.0.1:9100"),
                           ("http://127.0.0.1:8203", "http://127.0.0.1:9203")]:
            runtime = Runtime(serve_config(wda_url=wda, state_db=None))
            try:
                self.assertEqual(runtime.video.mjpeg_url, mjpeg)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()

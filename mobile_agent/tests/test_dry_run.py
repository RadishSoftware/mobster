"""``mobster run --dry-run`` and dryRun on POST /api/runs: the plan, and nothing sent."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from mobile_agent import dry_run, explain, smart_run
from mobile_agent.catalog import APPS
from mobile_agent.permissions import ASK_IMPORTANT, DONT_ASK
from mobile_agent.permissions import project_policy as PP

# The three starter workflows of Workflows (dashboard/src/components/workflow-ideas.tsx).
STARTERS = (
    "Go through my Instagram DMs and sort them into needs a reply, can wait and spam. Draft a one-line reply for each "
    "one that needs a reply. Don’t send anything.",
    "Read the comments on my last 5 Instagram posts and draft a reply to each question. Don’t post anything.",
    "Read last week’s Instagram insights and write a report: followers, reach, and the best post with its numbers",
)
NOW = 1_792_000_000
INSTAGRAM = {"id": "instagram", "name": "Instagram", "bundleId": "com.burbn.instagram"}


def plan(goal, **fields):
    fields.setdefault("estimator", lambda g, names: {"lowUsd": .02, "highUsd": .12, "model": "m"})
    return dry_run.plan(goal, apps=[*APPS, INSTAGRAM], **fields)


class PlanTests(unittest.TestCase):

    def test_the_starters_name_their_app_and_ask_about_nothing_they_forbid(self):
        for goal in STARTERS:
            result = plan(goal)
            self.assertEqual([a["name"] for a in result["apps"]], ["Instagram"], goal)
            self.assertEqual(result["asks"], [], goal)
            self.assertTrue(result["dryRun"] and result["sent"] is False)
            self.assertEqual((result["cost"]["lowUsd"], result["cost"]["highUsd"]), (.02, .12))

    def test_an_act_the_words_ask_for_is_listed_with_why_it_asks(self):
        result = plan("Open Messages and send Sam the address", mode=ASK_IMPORTANT)
        self.assertEqual([(a["act"], a["by"]) for a in result["asks"]], [("send_message", "mode")])
        self.assertEqual(plan("Open Messages and send Sam the address", mode=DONT_ASK)["asks"], [])

    def test_a_policy_adds_an_ask_in_full_access_and_the_dry_run_counts_nothing(self):
        policy = PP.parse("rules:\n  - action: allow\n    kind: message\n    max_per_day: 1\n")
        goal = "Open Messages and send Sam the address"
        self.assertEqual(plan(goal, mode=DONT_ASK, policy=policy, now=NOW)["asks"], [])
        for _ in range(3):
            plan(goal, mode=DONT_ASK, policy=policy, now=NOW)
        self.assertEqual(plan(goal, mode=DONT_ASK, policy=policy, now=NOW)["asks"], [])
        policy.take(frozenset({"message"}), "com.apple.MobileSMS", now=NOW)
        asked = plan(goal, mode=DONT_ASK, policy=policy, now=NOW)["asks"]
        self.assertEqual([(a["by"], "limit" in a["why"]) for a in asked], [("policy", True)])

    def test_text_for_a_terminal_says_nothing_was_sent(self):
        text = "\n".join(dry_run.lines(plan(STARTERS[0])))
        self.assertIn("Nothing was sent to your iPhone", text)
        self.assertIn("Instagram", text)
        self.assertIn("$0.02 to $0.12", text)


class NothingSentTests(unittest.TestCase):

    def test_a_dry_run_of_the_starters_on_the_scripted_phone_leaves_the_journal_empty(self):
        from mobile_agent.tui.session import Session
        session = Session(demo=True)
        runtime = session.runtime
        try:
            with patch.object(type(runtime), "open_driver", side_effect=AssertionError("the phone was reached")):
                for goal in STARTERS:
                    result = runtime.dry_run({"goal": goal})
                    self.assertTrue(result["dryRun"] and not result["sent"])
            self.assertEqual(runtime.journal.load(), [])
            self.assertEqual(dict(runtime.runs), {})
        finally:
            session.close()

    def test_the_cli_dry_run_makes_no_journal_and_prints_one_json_line(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "terminal.sqlite3"
            for goal in STARTERS:
                args = NS(goal=goal, json=True, execute=False, state_db=db, dry_run=True, demo=False,
                          spend_cap_usd=None)
                out = io.StringIO()
                with redirect_stdout(out), patch.dict(os.environ, {}, clear=False):
                    self.assertEqual(smart_run.run_smart(args), 0)
                line = json.loads(out.getvalue())
                self.assertEqual((line["event"], line["sent"]), ("dry_run", False))
            self.assertEqual(os.listdir(folder), [])


class ExplainTests(unittest.TestCase):

    def test_one_sentence_says_what_happens_and_why(self):
        line = explain.explain({"kind": "commit", "operation": "TAP", "label": "Send", "app_name": "Messages",
                                "recipient": "Kate Bell", "reason": "Sends this"})
        self.assertEqual(line, "Mobster will tap “Send” in Messages to Kate Bell, and asks because sends this.")

    def test_a_policy_reason_and_an_app_card_and_questions(self):
        line = explain.explain({"kind": "commit", "operation": "TAP", "label": "Post", "app_name": "Instagram",
                                "reason": "Your project policy asks before this"})
        self.assertIn("because your project policy asks before this", line)
        self.assertIn("haven't allowed it yet", explain.explain(
            {"kind": "commit", "operation": "OPEN_APP", "act": "use app", "apps": ["Things"]}))
        self.assertIsNone(explain.explain({"kind": "clarify", "operation": "ASK_USER"}))
        self.assertIsNone(explain.explain({"kind": "plan", "operation": "PLAN"}))

    def test_the_approval_the_run_publishes_and_the_mcp_status_carry_it(self):
        import threading

        from mobile_agent.run import Run
        from mobile_agent.threads import mcp
        events, run = [], None
        run = Run.__new__(Run)
        run.__dict__.update(NS(condition=threading.Condition(), stop=threading.Event(), approval=None,
                               approval_answer=None, approval_deadline=None, remember_spec=None, preapproved=[],
                               app={"name": "Messages"}, frames=None, plan_terms={}, emit=events.append,
                               _remember=lambda *a: None).__dict__)
        request = {"kind": "commit", "operation": "TAP", "label": "Send", "text": "hi", "recipient": "Kate Bell",
                   "reason": "Sends this", "act": "send"}
        answer = []
        thread = threading.Thread(target=lambda: answer.append(run.request_approval(request, timeout=5)))
        thread.start()
        for _ in range(100):
            if run.approval:
                break
            threading.Event().wait(.02)
        pending = dict(run.approval)
        run.approval_answer = "approved"
        with run.condition:
            run.condition.notify_all()
        thread.join()
        self.assertIn("to Kate Bell", pending["explain"])
        asked = next(e for e in events if e["event"] == "approval_requested")
        self.assertNotIn("Kate Bell", asked["explain"], "who it goes to is never journaled")
        state = mcp.describe({"finishedAt": None, "approval": pending})
        self.assertEqual(state["explain"], pending["explain"])
        self.assertIn(pending["explain"], mcp.sentence(state, "a" * 12))
        remote = mcp.sentence(state, "a" * 12, origin="remote")
        self.assertNotIn(pending["explain"], remote, "Remote words stay as they were")

    def test_the_terminal_prints_it_before_the_question(self):
        class Renderer:
            pass
        request = {"id": "x", "kind": "commit", "operation": "TAP", "label": "Send", "act": "send",
                   "explain": "Mobster will tap “Send” in Messages, and asks because sends this."}
        session = NS(pending_approval=lambda run: request, answer=lambda *a, **k: None, stop=lambda r: None)
        out = io.StringIO()
        with redirect_stdout(out), patch("builtins.input", return_value="n"):
            smart_run.Approver(session, NS(), Renderer()).ask({})
        self.assertIn(request["explain"], out.getvalue())


if __name__ == "__main__":
    unittest.main()

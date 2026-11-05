import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent.bench import vague_run
from mobile_agent.longform.notebook import Item


class HeldOut(unittest.TestCase):
    def test_the_frozen_manifest_matches_the_tasks(self):
        self.assertEqual(vague_run.task_for("h.inbox.gmail")[0], "com.google.Gmail")
        self.assertFalse(set(vague_run.HELDOUT) & set(vague_run.TASKS))

    def test_a_held_out_task_changed_after_freezing_refuses_to_run(self):
        changed = {**vague_run.HELDOUT, "h.feed.travel": ("com.burbn.instagram", "Watch 40 Reels about travel")}
        with patch.object(vague_run, "HELDOUT", changed), self.assertRaises(RuntimeError):
            vague_run.task_for("h.feed.travel")


class InboxCounts(unittest.TestCase):
    def test_an_inbox_sweep_counts_its_drafts_and_sends_none(self):
        chat = Item(id="c1", kind="chat", app="com.apple.MobileSMS",
                    extract={"title": "Sam", "needs_reply": True, "draft": "Yes, 7 works.", "last_message": "7?"})
        session = SimpleNamespace(request="Go through my messages and draft replies to anything that needs one",
                                  notebook=SimpleNamespace(items=[chat]))
        self.assertEqual(vague_run.inbox_counts(session), {"read": 1, "needs_reply": 1, "drafts": 1, "sent": 0})

    def test_a_feed_has_no_inbox_numbers(self):
        session = SimpleNamespace(request="Watch 30 Reels about travel and tell me what you learned",
                                  notebook=SimpleNamespace(items=[Item(id="r1", kind="reel")]))
        self.assertIsNone(vague_run.inbox_counts(session))


if __name__ == "__main__":
    unittest.main()

import os
import time
from unittest.mock import patch

from mobile_agent.tests.test_server_engine import MESSAGES, Base, jpeg
from mobile_agent.tests.test_server_hardening import APP
from mobile_agent.workflows import brief

INSTAGRAM = {"id": "com.burbn.instagram", "name": "Instagram", "bundleId": "com.burbn.instagram", "installed": True}
QUEUE = {"v": 1, "blocks": [{"type": "review_queue", "id": "queue", "title": "Review queue: 3 DMs",
                             "items": [{"id": "c1", "to": "Sam Lee", "app": "Instagram",
                                        "bundleId": "com.burbn.instagram",
                                        "bucket": "reply", "draft": "Yes, Friday at 8 works.",
                                        "context": "Asked about Friday"},
                                       {"id": "c2", "to": "Kai", "bucket": "reply", "draft": "Thanks!"},
                                       {"id": "c3", "to": "Promo", "bucket": "spam"}]}]}


class HomeBriefTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-000000000000"  # admission only: no model is called
        self.runtime_ = self.runtime(apps=(APP, MESSAGES, INSTAGRAM))
        self.events = []
        original = self.runtime_.publish

        def publish(topic, event):
            self.events.append((topic, event))
            return original(topic, event)
        self.runtime_.publish = publish

    def done(self, run, answer, blocks=None, frame=None):
        summary = {"event": "result", "status": "completed", "outcome": "done", "answer": answer,
                   "independently_verified": True, "elapsed_ms": 30_000,
                   "proof": [{"quote": answer[:20], "app": "Instagram"}]}
        if blocks is not None:
            summary["blocks"] = blocks
        run.finish(summary)
        if run.lease:
            run.lease.close()
            run.lease = None
        self.runtime_.active_runs.clear()
        self.runtime_.finished(run)

    def next_run(self, seen, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            fresh = [run for run in self.runtime_.runs.values() if run.id not in seen and run.finished_at is None]
            if fresh:
                return fresh[0]
            time.sleep(0.01)
        self.fail("the next step didn't start")

    def ended(self, workflow, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            listed = self.runtime_.workflows.batch_runs(workflow["id"])
            if listed and listed[0]["status"] != "running":
                return listed[0]
            time.sleep(0.01)
        self.fail("the batch didn't end")

    def run_brief(self, with_frame=False):
        runtime = self.runtime_
        workflow = runtime.workflows.create({"name": "Morning brief", "appId": "com.burbn.instagram", "steps": [
            {"id": "dms", "kind": "task", "text": "Sort my DMs", "appId": "com.burbn.instagram"},
            {"id": "ideas", "kind": "task", "text": "Ideas from my saved posts", "appId": "com.burbn.instagram"},
            {"id": "brief", "kind": "brief"}]})
        runtime.workflows.start(workflow["id"], "k1")
        first = self.next_run(set())
        queue = QUEUE
        if with_frame:
            frame = runtime.frames.put(first.id, jpeg(60, 120))
            queue = {"v": 1,
                     "blocks": [{**QUEUE["blocks"][0], "items": [{**QUEUE["blocks"][0]["items"][0], "frameId": frame},
                                                                 *QUEUE["blocks"][0]["items"][1:]]}]}
        self.done(first, "3 DMs: 2 need a reply.", queue)
        second = self.next_run({first.id})
        self.done(second, "Try a 3-part series on one edit, start to finish.")
        record = self.ended(workflow)
        return workflow, record, first, second

    def test_the_morning_brief_template_saves_a_daily_read_only_job(self):
        status, body, _ = self.request(self.runtime_, "POST", "/api/workflows/templates/morning-brief",
                                       {"niche": "home {cooking}", "time": "07:30"})
        self.assertEqual(status, 201, body)
        saved = body["workflow"]
        self.assertEqual((saved["name"], saved["cron"], saved["enabled"]), ("Morning brief", "30 7 * * *", True))
        self.assertIsNotNone(saved["nextAt"])
        self.assertEqual([step["kind"] for step in saved["steps"]], ["task", "task", "task", "brief"])
        self.assertIn("Reels about home cooking", saved["steps"][1]["text"])  # braces never become a blank
        self.assertTrue(all("Don’t" in step["text"] for step in saved["steps"] if step["kind"] == "task"))
        self.assertEqual(saved["produces"], ["review_queue", "media_list", "markdown"])
        self.assertEqual(saved["limits"]["maxSpendUsd"], 2.0)
        for bad, words in (({"niche": ""}, "niche"), ({"niche": "x", "time": "25:00"}, "time like 08:00"),
                           ({"niche": "x", "extra": 1}, "takes niche")):
            status, body, _ = self.request(self.runtime_, "POST", "/api/workflows/templates/morning-brief", bad)
            self.assertEqual(status, 400, body)
            self.assertIn(words, body["error"])
        status, body, _ = self.request(self.runtime_, "POST", "/api/workflows/templates/nope", {"niche": "x"})
        self.assertEqual((status, body.get("code")), (404, "template_not_found"))

    def test_without_instagram_it_says_so(self):
        runtime = self.runtime(apps=(APP, MESSAGES))
        status, body, _ = self.request(runtime, "POST", "/api/workflows/templates/morning-brief", {"niche": "cooking"})
        self.assertEqual(status, 400, body)
        self.assertIn("doesn't see Instagram on your iPhone", body["error"])

    def test_the_brief_puts_the_tasks_results_together_as_one_finished_run(self):
        workflow, record, first, second = self.run_brief()
        self.assertEqual(record["status"], "completed", record)
        saved = self.runtime_.workflows.get(workflow["id"])
        made = self.runtime_.runs[saved["lastRunId"]]  # the workflow's last result is the brief
        self.assertNotIn(made.id, (first.id, second.id))
        summary = made.summary
        self.assertEqual((made.status, summary["outcome"], summary["independently_verified"]),
                         ("completed", "done", True))
        self.assertEqual(summary["brief"]["runs"], [first.id, second.id])
        cards = summary["blocks"]["blocks"]
        self.assertEqual([(card["type"], card["id"]) for card in cards],
                         [("review_queue", "s1-queue"), ("markdown", "s2-notes")])
        self.assertEqual(cards[1]["title"], "Ideas from my saved posts")
        self.assertEqual(summary["brief"]["sources"], {"s1-queue": [first.id, "queue"]})
        self.assertEqual(summary["answer"], "Your brief: 2 drafts to approve, 1 note.")
        self.assertIsNone(made.cost_usd)  # no model call: its cost is its tasks'
        notes = [event for topic, event in self.events if event.get("event") == "batch_notification"]
        self.assertEqual((notes[-1]["title"], notes[-1]["runId"]), ("Your brief is ready", made.id))
        self.assertEqual(notes[-1]["body"], summary["answer"])
        self.assertIsNone(made.device)
        self.assertIsNone(made.lease)

    def test_a_brief_with_nothing_before_it_stops_the_workflow(self):
        workflow = self.runtime_.workflows.create({"name": "Lonely", "steps": [{"id": "brief", "kind": "brief"}]})
        with patch.object(self.runtime_.workflows, "start", wraps=self.runtime_.workflows.start):
            self.runtime_.workflows.start(workflow["id"], "k2")
        record = self.ended(workflow)
        self.assertEqual(record["status"], "failed")
        self.assertIn("nothing to put in the brief", record["reason"])

    def test_a_brief_step_takes_only_a_title(self):
        with self.assertRaises(ValueError):
            brief.validate({"id": "b", "kind": "brief", "text": "Do things"}, None)
        self.assertEqual(brief.validate({"id": "b", "kind": "brief", "title": "  My   brief "}, None),
                         {"title": "My brief"})

    def test_a_decision_on_the_brief_is_the_tasks_own_so_nothing_sends_twice(self):
        workflow, record, first, second = self.run_brief()
        made = self.runtime_.runs[self.runtime_.workflows.get(workflow["id"])["lastRunId"]]
        status, body, _ = self.request(self.runtime_, "POST", f"/api/runs/{made.id}/blocks/s1-queue/items/c2",
                                       {"decision": "skip"})
        self.assertEqual(status, 200, body)
        self.assertEqual(first.block_state["queue/c2"]["state"], "skipped")  # recorded on the task that found it
        self.assertEqual(made.block_state["s1-queue/c2"]["state"], "skipped")  # and shown on the brief
        status, body, _ = self.request(self.runtime_, "POST", f"/api/runs/{first.id}/blocks/queue/items/c2",
                                       {"decision": "reopen"})
        self.assertEqual(status, 200, body)
        self.assertNotIn("s1-queue/c2", made.block_state)  # reopened on the task: the brief follows
        status, body, _ = self.request(self.runtime_, "POST", f"/api/runs/{made.id}/blocks/s1-queue/items/c1",
                                       {"decision": "approve"})
        self.assertEqual(status, 200, body)
        follow = body["runId"]
        self.assertIn("Sam Lee", self.runtime_.runs[follow].goal)
        status, again, _ = self.request(self.runtime_, "POST", f"/api/runs/{first.id}/blocks/queue/items/c1",
                                        {"decision": "approve"})
        self.assertEqual((status, again["runId"], again.get("replayed")), (200, follow, True))
        sends = [run for run in self.runtime_.runs.values() if "Sam Lee" in run.goal]
        self.assertEqual(len(sends), 1, [(run.id, run.goal, run.idempotency_key) for run in sends])

    def test_the_briefs_cards_show_their_tasks_screens(self):
        workflow, record, first, second = self.run_brief(with_frame=True)
        made = self.runtime_.runs[self.runtime_.workflows.get(workflow["id"])["lastRunId"]]
        frame = made.summary["blocks"]["blocks"][0]["items"][0]["frameId"]
        deadline = time.monotonic() + 3
        while self.runtime_.frame(made.id, frame) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(self.runtime_.frame(made.id, frame))
        self.assertIsNone(self.runtime_.frame(second.id, frame))  # only a brief reads its tasks' screens

    def test_home_lists_the_job_with_its_last_result_and_counts_honestly(self):
        before = round(time.time() * 1000) - 1000
        workflow, record, first, second = self.run_brief()
        status, body, _ = self.request(self.runtime_, "GET", f"/api/home?since={before}")
        self.assertEqual(status, 200, body)
        job = body["jobs"][0]
        self.assertEqual((job["id"], job["name"], job["drafts"]), (workflow["id"], "Morning brief", 2))
        self.assertTrue(job["last"]["brief"])
        self.assertEqual(job["last"]["outcome"], "done")
        self.assertEqual(job["last"]["preview"]["type"], "review_queue")
        preview = job["last"]["preview"]
        self.assertEqual((len(preview["items"]), preview["total"], preview["drafts"]), (3, 3, 2))
        self.assertEqual(job["last"]["answer"], "Your brief: 2 drafts to approve, 1 note.")
        self.assertEqual(body["since"],
                         {"at": before, "jobs": 1, "items": 3, "kinds": {"review_queue": 3}, "needYou": 2})
        self.assertEqual((body["week"]["jobs"], body["week"]["items"], body["week"]["partial"]), (1, 3, False))
        self.assertEqual(body["week"]["spentUsd"], 0.0)
        status, later, _ = self.request(self.runtime_, "GET", f"/api/home?since={round(time.time() * 1000)}")
        self.assertEqual(later["since"]["jobs"], 0)
        status, bad, _ = self.request(self.runtime_, "GET", "/api/home?since=soon")
        self.assertEqual((status, bad["code"]), (400, "invalid_since"))

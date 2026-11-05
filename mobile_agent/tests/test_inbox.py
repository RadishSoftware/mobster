import threading
import time
import unittest

from mobile_agent import inbox
from mobile_agent.run import Run
from mobile_agent.tests.test_server_engine import MESSAGES, Base
from mobile_agent.workflows import health as workflow_health

NOW = 1_800_000_000.0
MS = NOW * 1000
MIN = 60
PROOF = [{"quote": "Delivered"}]


def run(id, status="completed", goal="Text Sam I'm late", app="messages", at=NOW - 60 * MIN, summary=None,
        approval=None, origin="app", reviewed=False):
    task = Run({"id": app, "name": app.title()}, goal, "live", id=id, status=status, created_at=at,
               finished_at=None if status in ("running", "queued") else at + MIN, summary=summary, origin=origin,
               reviewed_ok=reviewed)
    task.approval = approval
    return task


def approval(id, kind="commit", expires=MS + 5 * 60_000, **fields):
    return {"id": id, "kind": kind, "operation": "TAP", "label": "Send", "app": "Messages", "requestedAt": MS - 60_000,
            "expiresAt": expires, **fields}


def failed(run_id=None, words="Last run couldn’t finish", detail=None):
    return {"lastSuccess": None, "attention": {"kind": "failed", "words": words, "detail": detail, "runId": run_id,
                                               "at": MS - 30 * 60_000}}


def workflow(id, last_run=None, last="completed", health=None, **fields):
    return {"id": id, "name": f"Workflow {id}", "goal": "Check the mail", "appId": "mail", "appName": "Mail",
            "enabled": True, "nextAt": MS + 3_600_000, "lastRunId": last_run, "lastOutcome": last,
            "health": health or {"lastSuccess": None, "attention": None}, **fields}


def kinds(items):
    return [(item["kind"], item.get("runId") or item["workflow"]["id"]) for item in items]


class OutcomeTests(unittest.TestCase):
    def test_done_needs_proof_or_a_persons_ok(self):
        outcome = inbox.run_outcome
        self.assertEqual(outcome(run("a", summary={"outcome": "done", "proof": PROOF})), "done")
        self.assertEqual(outcome(run("a", summary={"outcome": "done"})), "check")
        self.assertEqual(outcome(run("a", summary={"proof": PROOF})), "done")
        self.assertEqual(outcome(run("a", "completed_unverified", summary={"outcome": "check"})), "check")
        self.assertEqual(outcome(run("a", "completed_unverified", summary={"outcome": "check"}, reviewed=True)), "done")
        self.assertEqual(outcome(run("a", "error")), "couldnt_finish")
        self.assertEqual(outcome(run("a", "running")), None)


class CollectTests(unittest.TestCase):
    def collect(self, runs, workflows=()):
        return inbox.collect(runs, list(workflows), now=NOW)

    def test_an_approval_waiting_now_carries_who_it_acts_as_who_it_goes_to_the_words_and_what_it_does(self):
        pending = approval("a1", title="Send this message to Sam?", act="send_message", text="Running late",
                           actingAs="@getmobster on Messages", recipient="Sam", reason="Sends a message to Sam")
        items = self.collect([run("r1", "running", approval=pending)])
        self.assertEqual(kinds(items), [("approval", "r1")])
        self.assertEqual(items[0]["id"], "approval:a1")
        shown = items[0]["approval"]
        self.assertEqual((shown["actingAs"], shown["recipient"], shown["text"], shown["reason"]),
                         ("@getmobster on Messages", "Sam", "Running late", "Sends a message to Sam"))

    def test_a_question_is_its_own_kind_and_an_expired_one_is_gone(self):
        asked = approval("q1", kind="clarify", question="Which Sam?", allowText=True)
        items = self.collect([run("r1", "running", approval=asked),
                              run("r2", "running", approval=approval("q2", kind="question", expires=MS - 1))])
        self.assertEqual(kinds(items), [("question", "r1")])

    def test_a_result_to_look_at_is_listed_until_a_later_run_of_the_same_task_works(self):
        unsure = {"outcome": "check", "answer": "Probably sent"}
        self.assertEqual(kinds(self.collect([run("r1", summary=unsure)])), [("review", "r1")])
        later = run("r2", summary={"outcome": "done", "proof": PROOF}, at=NOW - 10 * MIN, goal="text  sam i'm late")
        self.assertEqual(self.collect([run("r1", summary=unsure), later]), [])
        other = run("r3", summary={"outcome": "done", "proof": PROOF}, at=NOW - 10 * MIN, goal="Text Kim")
        self.assertEqual(kinds(self.collect([run("r1", summary=unsure), other])), [("review", "r1")])
        self.assertEqual(self.collect([run("r1", summary=unsure, reviewed=True)]), [])

    def test_history_is_not_needs_you(self):
        old = run("r1", summary={"outcome": "check"}, at=NOW - 8 * 86_400)
        failed = run("r2", "error", summary={"outcome": "couldnt_finish", "reason": "The phone was locked"})
        done = run("r3", summary={"outcome": "done", "proof": PROOF})
        stopped = run("r4", "stopped")
        self.assertEqual(self.collect([old, failed, done, stopped]), [])

    def with_health(self, wf, attempts, runs):
        return {**wf, "health": workflow_health.workflow_health(wf, attempts, {task.id: task for task in runs})}

    def test_a_workflow_whose_last_run_failed_needs_repair(self):
        broke = run("r1", "error", goal="Check the mail", app="mail", origin="schedule",
                    summary={"outcome": "couldnt_finish", "reason": "Mail asked to sign in"})
        wf = self.with_health(workflow("w1", "r1", "error"), [("error", "r1", MS - 60_000)], [broke])
        items = self.collect([broke], [wf])
        self.assertEqual(kinds(items), [("repair", "r1")])
        self.assertEqual((items[0]["workflow"]["id"], items[0]["result"]["reason"]), ("w1", "Mail asked to sign in"))
        wf = self.with_health(workflow("w2", None, "dispatch_failed"), [("dispatch_failed", None, MS - 60_000)], [])
        items = self.collect([], [wf])
        self.assertEqual((kinds(items), items[0]["result"]["reason"]), ([("repair", "w2")], "Last run couldn’t start"))

    def test_a_failure_a_later_success_superseded_is_gone(self):
        broke = run("r1", "error", goal="Check the mail", app="mail", origin="schedule",
                    summary={"outcome": "couldnt_finish"}, at=NOW - 120 * MIN)
        worked = run("r2", goal="Check the mail", app="mail", origin="schedule",
                     summary={"outcome": "done", "proof": PROOF}, at=NOW - 30 * MIN)
        attempts = [("completed", "r2", MS - 30 * 60_000), ("error", "r1", MS - 120 * 60_000)]
        wf = self.with_health(workflow("w1", "r2", "completed"), attempts, [broke, worked])
        self.assertEqual(self.collect([broke, worked], [wf]), [])
        unsure = run("r3", goal="Check the mail", app="mail", origin="schedule", summary={"outcome": "check"},
                     at=NOW - 10 * MIN)
        older = run("r4", goal="Check the mail", app="mail", origin="schedule", summary={"outcome": "check"},
                    at=NOW - 200 * MIN)
        attempts = [("completed", "r3", MS - 10 * 60_000), ("error", "r1", MS - 120 * 60_000)]
        wf = self.with_health(workflow("w1", "r3", "completed"), attempts, [broke, unsure, older])
        items = self.collect([broke, unsure, older], [wf])
        self.assertEqual(kinds(items), [("review", "r3")])
        self.assertEqual(items[0]["workflow"]["id"], "w1")

    def test_a_failure_under_a_run_mobster_cant_read_still_needs_repair(self):
        broke = run("r1", "error", goal="Check the mail", app="mail", origin="schedule",
                    summary={"outcome": "couldnt_finish", "reason": "Mail asked to sign in"})
        wf = self.with_health(workflow("w1", "gone", "run_expired"),
                              [("run_expired", "gone", MS), ("error", "r1", MS - 60_000)], [broke])
        self.assertEqual(kinds(self.collect([broke], [wf])), [("repair", "r1")])

    def test_stopped_by_design_skipped_or_running_is_no_repair(self):
        timed_out = run("r1", "approval_timeout", origin="schedule")
        going = run("r2", "running", origin="schedule")
        runs = [timed_out, going]
        self.assertEqual(self.collect(runs, [
            self.with_health(workflow("w1", "r1", "approval_timeout"), [("approval_timeout", "r1", MS)], runs),
            self.with_health(workflow("w2", "r2", "started"), [("started", "r2", MS)], runs),
            self.with_health(workflow("w3", None, "skipped_busy"), [("skipped_busy", None, MS)], runs),
            # A run whose record is gone: Mobster can't say it failed, so nothing needs repair.
            self.with_health(workflow("w6", "gone", "run_expired"), [("run_expired", "gone", MS)], runs),
            self.with_health(workflow("w4", None, None), [], runs)]), [])
        self.assertEqual(self.collect([], [workflow("w5", health={"lastSuccess": None, "attention": {
            "kind": "phone", "words": "Skipped: your iPhone was busy", "detail": None, "runId": None, "at": MS}})]), [])

    def test_order_is_what_expires_first_then_newest(self):
        broke = run("r4", "error", summary={"outcome": "couldnt_finish"}, goal="Check the mail", origin="schedule",
                    at=NOW - 30 * MIN)
        items = self.collect([
            run("r1", "running", approval=approval("a1", expires=MS + 9 * 60_000)),
            run("r2", "running", approval=approval("a2", kind="clarify", expires=MS + 2 * 60_000)),
            run("r3", summary={"outcome": "check"}, goal="Find a flight", at=NOW - 300 * MIN), broke],
            [workflow("w1", "r4", "error", health=failed("r4"))])
        self.assertEqual(kinds(items), [("question", "r2"), ("approval", "r1"), ("repair", "r4"), ("review", "r3")])


def queue(*items, id="q1", action="send_message"):
    return {"type": "review_queue", "id": id, "action": action, "buckets": [], "items": list(items)}


def dm(id, to, draft="Friday at 8 works.", account="@getmobster", **fields):
    return {"id": id, "to": to, "app": "Instagram", "account": account, "context": f"{to} asked about Friday",
            "lastMessage": "are we still on?", "draft": draft, **fields}


class DraftTests(unittest.TestCase):
    """Drafts outlive the run that wrote them (ICP-UX row 4): each reply nobody has sent or skipped is its own item,
    carrying the profile it sends from, until it is decided or DRAFT_DAYS pass."""

    def collect(self, runs):
        return inbox.collect(runs, [], now=NOW)

    def triage(self, id, *items, at=NOW - 60 * MIN, decided=None):
        task = run(id, summary={"outcome": "done", "proof": PROOF, "blocks": [queue(*items)]}, goal="Sort my DMs",
                   app="com.burbn.instagram", at=at)
        task.block_state = dict(decided or {})
        return task

    def test_each_undecided_draft_is_an_item_with_its_profile_and_words(self):
        items = self.collect([self.triage("r1", dm("i1", "Sam Rivera"), dm("i2", "Priya", draft=None),
                                          dm("i3", "Jordan", draft="Thank you!"),
                                          decided={"q1/i3": {"state": "approved"}})])
        self.assertEqual([item["kind"] for item in items], ["draft"])
        item = items[0]
        self.assertEqual((item["id"], item["runId"]), ("draft:r1/q1/i1", "r1"))
        self.assertEqual({key: item["draft"][key] for key in ("blockId", "itemId", "action", "to", "account", "draft")},
                         {"blockId": "q1", "itemId": "i1", "action": "send_message", "to": "Sam Rivera",
                          "account": "@getmobster", "draft": "Friday at 8 works."})

    def test_a_skipped_draft_and_an_old_one_are_gone_and_a_running_task_has_none_yet(self):
        self.assertEqual(self.collect([self.triage("r1", dm("i1", "Sam"), decided={"q1/i1": {"state": "skipped"}})]),
                         [])
        self.assertEqual(self.collect([self.triage("r2", dm("i1", "Sam"), at=NOW - (inbox.DRAFT_DAYS + 1) * 86_400)]),
                         [])
        live = self.triage("r3", dm("i1", "Sam"))
        live.status, live.finished_at = "running", None
        self.assertEqual(self.collect([live]), [])

    def test_two_people_with_the_same_display_name_keep_their_distinct_drafts(self):
        first = {**dm("i1", "Sam"), "handle": "@sam.one"}
        second = {**dm("i2", "Sam"), "handle": "@sam.two"}
        items = self.collect([self.triage("r1", first, second)])
        self.assertEqual({item["draft"]["handle"] for item in items}, {"@sam.one", "@sam.two"})

    def test_a_later_draft_to_the_same_person_from_the_same_profile_replaces_the_earlier_one(self):
        items = self.collect([self.triage("r1", dm("i1", "Sam Rivera", draft="Old"), at=NOW - 300 * MIN),
                              self.triage("r2", dm("i9", "sam  rivera", draft="New"), at=NOW - 60 * MIN),
                              self.triage("r3", dm("i1", "Sam Rivera", account="@mobster.dev"), at=NOW - 120 * MIN)])
        self.assertEqual([(item["runId"], item["draft"]["draft"]) for item in items],
                         [("r2", "New"), ("r3", "Friday at 8 works.")])

    def test_a_briefs_copy_of_another_tasks_queue_is_listed_once(self):
        source = self.triage("r1", dm("i1", "Sam"))
        brief = run("r2", summary={"outcome": "done", "proof": PROOF, "blocks": [queue(dm("i1", "Sam"), id="c1")],
                                   "brief": {"sources": {"c1": ["r1", "q1"]}}}, at=NOW - 30 * MIN)
        self.assertEqual([item["runId"] for item in self.collect([source, brief])], ["r1"])
        # With the task gone from history, the brief's copy is the one record.
        self.assertEqual([item["runId"] for item in self.collect([brief])], ["r2"])


class InboxRouteTests(Base):
    def test_get_inbox_lists_a_waiting_approval_with_its_recipient_and_drops_it_once_answered(self):
        runtime = self.runtime()
        task = Run(dict(MESSAGES), "Text Sam I'm running late", "live", journal=runtime.journal)
        runtime.journal.create(task.metadata())
        runtime.runs[task.id] = task
        task.status = "running"
        answers = []
        thread = threading.Thread(target=lambda: answers.append(task.request_approval(
            {"kind": "commit", "operation": "TAP", "label": "Send", "text": "I'm running late",
             "title": "Send this message to Sam?", "act": "send_message", "app_name": "Messages",
             "recipient": "Sam", "acting_as": "@getmobster on Messages"}, timeout=5)))
        thread.start()
        for _ in range(200):
            if task.public()["approval"]:
                break
            time.sleep(.01)
        status, body, _ = self.request(runtime, "GET", "/api/inbox")
        self.assertEqual((status, body["count"], body["items"][0]["kind"]), (200, 1, "approval"))
        shown = body["items"][0]["approval"]
        self.assertEqual((shown["recipient"], shown["actingAs"], shown["text"]),
                         ("Sam", "@getmobster on Messages", "I'm running late"))
        requested = next(e for e in task.events if e["event"] == "approval_requested")
        self.assertNotIn("recipient", requested)
        self.request(runtime, "POST", f"/api/runs/{task.id}/approval", {"id": shown["id"], "approve": False})
        thread.join(3)
        self.assertEqual(answers, ["denied"])
        self.assertEqual(self.request(runtime, "GET", "/api/inbox")[1], {"items": [], "count": 0})


if __name__ == "__main__":
    unittest.main()

"""A graph schedule skips or runs once a slot the Mac slept through, and skips or queues an overlapping slot."""

from mobile_agent.tests.test_graph_run import GraphBase, edge, node, task

HOUR = 3600


class ScheduleTests(GraphBase):
    def scheduled(self, **rules):
        return self.workflows.create({"name": "Hourly", "enabled": True, "graph": {"version": 2, "nodes": [
            node("start", "trigger.manual"),
            node("clock", "trigger.schedule", {"cron": "0 * * * *", "timezone": "UTC", **rules}),
            task("a", "Hold A"), task("b", "Then B")],
            "edges": [edge("e1", "start", "a"), edge("e2", "clock", "a"), edge("e3", "a", "b")]}})

    def outcomes(self, workflow):
        return [outcome for outcome, _run, _at in self.workflows.attempts(workflow["id"])]

    def test_slots_the_mac_slept_through_are_skipped_or_run_once(self):
        self.phone.script = lambda goal, fields: ("completed", {"answer": "ok"}, 0.0)
        skipping, once = self.scheduled(), self.scheduled(missed="run_once")
        self.time = once["nextAt"] / 1000 + 3 * HOUR                # asleep through three slots
        self.workflows.tick()
        self.assertEqual(self.outcomes(skipping), ["skipped_missed"])
        # The run starts on its own thread and the scripted phone answers at once, so it may have ended already.
        self.assertIn(self.outcomes(once)[0], ("started", "completed"))
        self.wait(once)
        self.assertEqual(self.outcomes(once)[0], "completed")      # it ran (its outcome moves on when the run ends)
        self.workflows.tick()
        self.assertEqual(len([o for o in self.outcomes(once) if o != "skipped_missed"]), 1)   # once, not three times
        self.assertGreater(self.workflows.get(once["id"])["nextAt"], self.time * 1000)

    def test_a_slot_that_comes_round_mid_run_is_skipped_or_waits_its_turn(self):
        skipping, queued = self.scheduled(), self.scheduled(overlap="queue_one")
        first = queued["nextAt"] / 1000
        self.time = first + 1
        self.workflows.tick()
        self.until(lambda: len(self.phone.calls) == 2)              # both workflows' A holds
        self.time = first + HOUR + 1
        self.workflows.tick()
        self.assertEqual(self.outcomes(skipping)[0], "skipped_busy")
        self.assertEqual(self.outcomes(queued)[0], "queued")
        self.assertTrue(self.workflows.get(queued["id"])["queuedDispatch"].endswith(f":{round(first + HOUR) * 1000}"))
        for run in [r for r in list(self.phone.runs.values()) if r.goal == "Hold A"]:
            self.phone.finish(run, "completed", {"answer": "ok"}, 0.0)
        self.until(lambda: not self.workflows.running())
        self.phone.script = lambda goal, fields: ("completed", {"answer": "ok"}, 0.0)
        self.workflows.tick()
        self.until(lambda: self.outcomes(queued)[0] in ("started", "completed"))
        self.assertIsNone(self.workflows.get(queued["id"]).get("queuedDispatch"))
        self.assertEqual(self.wait(queued)["status"], "completed")


class WebhookTests(GraphBase):
    def test_a_webhooks_body_reaches_its_trigger_as_data(self):
        workflow = self.workflows.create({"name": "On a call", "graph": {"version": 2, "nodes": [
            node("start", "trigger.manual"), node("hook", "trigger.webhook"),
            task("a", "Thank the caller in the input", input="{= $node.hook.body }")],
            "edges": [edge("e1", "start", "a"), edge("e2", "hook", "a")]}})
        self.workflows.run(workflow["id"], "hook-1", None, origin="hook",
                           body={"caller": "Sam", "token": "sk-ant-api03-" + "a" * 40})
        record = self.wait(workflow)
        self.assertEqual(record["trigger"], "trigger.webhook")
        sent = self.phone.calls[0].extras["workflowInput"]
        self.assertIn('"caller": "Sam"', sent)
        self.assertNotIn("sk-ant-api03", sent)                         # masked before it is kept or shown

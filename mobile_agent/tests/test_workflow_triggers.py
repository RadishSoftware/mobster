import json

from mobile_agent.api_errors import APIError
from mobile_agent.graph import nodes as graph_nodes
from mobile_agent.tests.test_graph_run import GraphBase, edge, node, task
from mobile_agent.workflows.triggers import BUSY_RETRY_MS, NOTIFY_PER_HOUR, matches, notification_of

KEY = "sk-ant-api03-" + "a" * 40


def note(body="Order 1042 is ready", app="com.burbn.instagram", at=1):
    return {"app": app, "title": "Radish Software", "body": body, "receivedAt": at}


class NotificationTests(GraphBase):
    def setUp(self):
        super().setUp()
        graph_nodes.register()

    def listening(self, enabled=True, **config):
        return self.workflows.create({"name": "On a notification", "enabled": enabled, "graph": self.graph(
            [node("heard", "trigger.notification", {"app": "com.burbn.instagram", "contains": "order", **config}),
             task("a", "Save what the notification said", input="{= $input.body }")],
            [edge("e1", "start", "a"), edge("e2", "heard", "a")])})

    def test_a_notification_it_takes_starts_it_once_with_its_fields_as_data(self):
        workflow = self.listening()
        self.assertEqual(self.workflows.triggers.notify(note(f"Order 1042 is ready {KEY}")),
                         [(workflow["id"], "started")])
        record = self.wait(workflow)
        self.assertEqual((record["trigger"], record["status"]), ("trigger.notification", "completed"))
        call = self.phone.calls[0]
        self.assertEqual(call.goal, "Save what the notification said")
        self.assertIn("Order 1042 is ready", call.extras["workflowInput"])
        self.assertNotIn(KEY, call.extras["workflowInput"])
        self.assertEqual(self.workflows.triggers.notify(note(f"Order 1042 is ready {KEY}")), [])
        self.assertEqual(self.workflows.triggers.notify(note(app="com.apple.MobileSMS", at=2)), [])
        self.assertEqual(self.workflows.triggers.notify(note("Lunch?", at=3)), [])
        self.assertEqual(len(self.phone.calls), 1)

    def test_at_most_six_an_hour_and_never_while_turned_off(self):
        workflow, off = self.listening(), self.listening(enabled=False)
        outcomes = []
        for at in range(NOTIFY_PER_HOUR + 1):
            outcomes += self.workflows.triggers.notify(note(f"Order {at}", at=at))
            self.wait(workflow)
        self.assertEqual([o for _w, o in outcomes], ["started"] * NOTIFY_PER_HOUR + ["skipped_rate_limit"])
        self.assertNotIn(off["id"], {w for w, _o in outcomes})
        self.time += 3601
        self.assertEqual(self.workflows.triggers.notify(note("Order 99", at=99)), [(workflow["id"], "started")])

    def test_one_that_arrives_mid_run_waits_for_it_and_only_one_waits(self):
        workflow = self.listening()
        self.phone.script = lambda goal, fields: "hold"
        self.workflows.triggers.notify(note("Order 1", at=1))
        first = self.run_of("Save what the notification said")
        self.assertEqual(self.workflows.triggers.notify(note("Order 2", at=2)), [(workflow["id"], "queued")])
        self.assertEqual(self.workflows.triggers.notify(note("Order 3", at=3)), [(workflow["id"], "skipped_busy")])
        listed = next(w for w in self.workflows.list() if w["id"] == workflow["id"])
        self.assertNotIn("queuedEvent", listed)
        self.phone.script = lambda goal, fields: ("completed", {"answer": "ok"}, 0.0)
        self.phone.finish(first, "completed", {"answer": "ok"}, 0.0)
        self.wait(workflow)
        self.workflows.tick()
        self.wait(workflow)
        inputs = [c.extras["workflowInput"] for c in self.phone.calls]
        self.assertEqual(len(inputs), 2)
        self.assertIn("Order 2", inputs[1])
        self.assertEqual(self.workflows.batch_runs(workflow["id"])[0]["trigger"], "trigger.notification")
        for at in range(10, 10 + NOTIFY_PER_HOUR):
            self.workflows.triggers.notify(note(f"Order {at}", at=at))
            self.wait(workflow)
        self.assertEqual(len(self.phone.calls), NOTIFY_PER_HOUR)

    def test_a_trigger_turns_it_on_without_a_schedule_and_a_notification_is_checked(self):
        self.assertTrue(self.listening()["enabled"])
        with self.assertRaises(ValueError):
            self.workflows.create({"name": "Nothing to start it", "enabled": True,
                                   "graph": self.graph([task("a", "Read A")], [edge("e1", "start", "a")])})
        for bad in ({}, {"app": "x"}, {"app": "x", "receivedAt": True}, "a notification"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                notification_of(bad)
        heard = notification_of(note("  The ORDER  is in "))
        self.assertTrue(matches({"config": {"app": None, "contains": "order is"}}, heard))
        self.assertFalse(matches({"config": {"app": "com.apple.MobileSMS"}}, heard))

    def test_fire_starts_the_named_workflow_once_and_its_one_task_reads_the_notification_as_data(self):
        workflow = self.workflows.create({"name": "One task", "steps": [
            {"id": "a", "kind": "task", "text": "Save the order number", "appId": None}]})
        self.assertFalse(self.workflows.triggers.listening())
        self.assertEqual(self.workflows.triggers.fire(workflow["id"], note(f"Order 7 {KEY}")), "started")
        self.assertIsNone(self.workflows.triggers.fire(workflow["id"], note(f"Order 7 {KEY}")))
        call = self.phone.calls[0]
        self.assertEqual(call.goal, "Save the order number")
        self.assertTrue(json.loads(call.extras["workflowInput"])["body"].startswith("Order 7"))
        self.assertNotIn(KEY, call.extras["workflowInput"])
        self.assertNotIn("trigger", call.extras)
        with self.assertRaises(APIError):
            self.workflows.triggers.fire("0123456789ab", note())

    def test_the_settings_trigger_that_fired_it_is_named_on_its_task_for_the_run_view(self):
        from mobile_agent import harness_api
        from mobile_agent.triggers.register import validate_trigger
        saved = harness_api._snapshot()
        self.addCleanup(harness_api._restore, saved)
        if "trigger" not in harness_api.run_fields():
            with harness_api.owned_by("triggers"):
                harness_api.register_run_field("trigger", validate_trigger, public=True)
        workflow = self.workflows.create({"name": "One task", "steps": [
            {"id": "a", "kind": "task", "text": "Save the order number", "appId": None}]})
        who = {"id": "c" * 12, "name": "Orders"}
        self.assertEqual(self.workflows.triggers.fire(workflow["id"], note(), started_by=who), "started")
        self.assertEqual(self.phone.calls[0].extras["trigger"], who)

    def test_a_graph_without_a_notification_trigger_gets_it_at_its_manual_trigger(self):
        workflow = self.save_graph([task("a", "Read A", input="{= $input.input.title }"), task("b", "Then B")],
                                   [edge("e1", "start", "a"), edge("e2", "a", "b")])
        self.assertEqual(self.workflows.triggers.fire(workflow["id"], note()), "started")
        self.wait(workflow)
        self.assertEqual(self.phone.calls[0].extras["workflowInput"], "Radish Software")
        listening = self.workflows.create({"name": "Listens", "enabled": True, "graph": self.graph(
            [node("heard", "trigger.notification"), task("a", "Read A")],
            [edge("e1", "start", "a"), edge("e2", "heard", "a")])})
        self.assertTrue(self.workflows.triggers.listening())
        self.workflows.update(listening["id"], {"revision": listening["revision"], "enabled": False})
        self.assertFalse(self.workflows.triggers.listening())


class AppTests(GraphBase):
    def setUp(self):
        super().setUp()
        graph_nodes.register()
        self.rows, self.reads = [], []
        self.read_status = "completed"

        def script(goal, fields):
            spec = (fields.get("extras") or {}).get("packRead")
            if spec:
                self.reads.append(spec)
                return self.read_status, {"data": {"rows": list(self.rows), "columns": [], "title": "DMs"}}, 0.0
            return "completed", {"answer": f"Did: {goal}"}, 0.0
        self.phone.script = script

    def watching(self):
        return self.workflows.create({"name": "New DMs", "enabled": True, "graph": self.graph(
            [node("new", "trigger.app", {"app": "instagram", "reader": "dms", "key": "id", "every": 5}),
             task("a", "Thank the person who wrote", input="{= $input.row.from }")],
            [edge("e1", "start", "a"), edge("e2", "new", "a")])})

    def poll(self, minutes=5):
        self.time += minutes * 60
        self.workflows.tick()
        self.workflows.tick()

    def test_the_first_read_only_fills_the_seen_set_then_each_new_row_runs_once_oldest_first(self):
        workflow = self.watching()
        self.rows = [{"id": "m2", "from": "Kate"}, {"id": "m1", "from": "Sam"}]
        self.poll(0)
        self.assertEqual((len(self.reads), self.workflows.batch_runs(workflow["id"])), (1, []))
        self.assertTrue(self.phone.calls[0].key.startswith(f"workflow:{workflow['id']}:poll:new:"))
        self.rows = [{"id": "m4", "from": "Ana"}, {"id": "m3", "from": "Lee"}] + self.rows
        self.poll()
        self.wait(workflow)
        self.workflows.tick()
        self.wait(workflow)
        runs = self.workflows.batch_runs(workflow["id"])
        self.assertEqual([r["trigger"] for r in runs], ["trigger.app", "trigger.app"])
        self.assertEqual([c.extras["workflowInput"] for c in self.phone.calls if "packRead" not in c.extras],
                         ["Lee", "Ana"])
        self.poll()
        self.workflows.tick()
        self.assertEqual(len(self.workflows.batch_runs(workflow["id"])), 2)

    def test_a_read_that_fails_starts_nothing_and_a_busy_phone_is_tried_later_not_every_tick(self):
        workflow = self.watching()
        self.poll(0)
        self.rows, self.read_status = [{"id": "m9", "from": "Sam"}], "error"
        self.poll()
        self.assertEqual(self.workflows.batch_runs(workflow["id"]), [])
        self.read_status = "completed"

        def busy(*args, **fields):
            raise APIError("Your iPhone is busy.", 409, "device_busy")
        create, self.phone.create = self.phone.create, busy
        reads = len(self.reads)
        self.poll()
        self.workflows.tick()
        self.time += BUSY_RETRY_MS / 1000 / 2
        self.workflows.tick()
        self.assertEqual(len(self.reads), reads)
        self.phone.create = create
        self.time += BUSY_RETRY_MS / 1000
        self.workflows.tick()
        self.workflows.tick()
        self.wait(workflow)
        self.assertEqual(len(self.workflows.batch_runs(workflow["id"])), 1)

    def test_a_claimed_row_never_runs_twice_and_deleting_the_workflow_forgets_its_rows(self):
        workflow = self.watching()
        self.poll(0)
        self.rows = [{"id": "m1", "from": "Sam", "note": f"key {KEY}"}]
        self.poll()
        self.wait(workflow)
        kept = self.workflows.journal.connection.execute(
            "SELECT row, run_key FROM workflow_seen WHERE workflow_id=? AND key='m1'", (workflow["id"],)).fetchone()
        self.assertNotIn(KEY, kept[0])
        self.assertTrue(kept[1].startswith(f"workflow:{workflow['id']}:app:new:"))
        self.workflows.journal.connection.execute("UPDATE workflow_seen SET run_key=NULL WHERE key='m1'")
        self.workflows.tick()
        self.assertEqual(len(self.workflows.batch_runs(workflow["id"])), 1)
        self.workflows.delete(workflow["id"])
        self.assertEqual(self.workflows.journal.connection.execute(
            "SELECT count(*) FROM workflow_seen WHERE workflow_id=?", (workflow["id"],)).fetchone()[0], 0)
        self.assertEqual(json.loads(kept[0])["from"], "Sam")


class StartsItselfTests(GraphBase):
    """``startsItself`` reads what the tick and the triggers read, the published graph: a draft never changes it."""

    def setUp(self):
        super().setUp()
        graph_nodes.register()

    def redraft(self, workflow, nodes, edges, **fields):
        return self.workflows.update(workflow["id"], {"revision": workflow["revision"],
                                                      "graph": self.graph(nodes, edges), **fields})

    def test_a_draft_that_drops_its_trigger_or_schedule_leaves_it_starting_itself_even_paused(self):
        heard = self.workflows.create({"name": "Heard", "enabled": True, "graph": self.graph(
            [node("heard", "trigger.notification"), task("a", "Read A")],
            [edge("e1", "start", "a"), edge("e2", "heard", "a")])})
        clock = self.workflows.create({"name": "Hourly", "enabled": True, "graph": self.graph(
            [node("clock", "trigger.schedule", {"cron": "0 * * * *", "timezone": "UTC"}), task("b", "Read B"),
             task("c", "Then C")], [edge("e1", "start", "b"), edge("e2", "clock", "b"), edge("e3", "b", "c")])})
        heard = self.redraft(heard, [task("a", "Read A")], [edge("e1", "start", "a")])
        clock = self.redraft(clock, [task("b", "Read B"), task("c", "Then C")],
                             [edge("e1", "start", "b"), edge("e3", "b", "c")])
        self.assertEqual([(w["draft"], w["startsItself"]) for w in (heard, clock)], [(True, True), (True, True)])
        self.assertEqual([w["listensFor"] for w in (heard, clock)], ["notification", None])
        self.assertEqual(self.workflows.triggers.notify(note()), [(heard["id"], "started")])
        self.wait(heard)
        self.time = clock["nextAt"] / 1000 + 1
        self.workflows.tick()
        self.assertEqual(self.wait(clock)["trigger"], "trigger.schedule")
        paused = self.workflows.update(heard["id"], {"revision": heard["revision"], "enabled": False})
        self.assertEqual((paused["enabled"], paused["startsItself"]), (False, True))
        self.assertEqual(self.workflows.triggers.notify(note("Order 2", at=2)), [])

    def test_a_draft_that_adds_a_trigger_or_schedule_starts_nothing_and_nor_does_a_webhook(self):
        made = self.save_graph([task("a", "Read A")], [edge("e1", "start", "a")])
        self.assertFalse(made["startsItself"])
        heard = self.redraft(made, [node("heard", "trigger.notification"), task("a", "Read A")],
                             [edge("e1", "start", "a"), edge("e2", "heard", "a")], enabled=True)
        self.assertEqual((heard["enabled"], heard["draft"], heard["startsItself"], heard["listensFor"]),
                         (True, True, False, None))
        self.assertEqual(self.workflows.triggers.notify(note()), [])
        timed = self.redraft(self.save_graph([task("b", "Read B")], [edge("e1", "start", "b")]),
                             [node("clock", "trigger.schedule", {"cron": "0 * * * *", "timezone": "UTC"}),
                              task("b", "Read B")], [edge("e1", "start", "b"), edge("e2", "clock", "b")])
        self.assertEqual((timed["cron"], timed["startsItself"]), (None, False))
        published = self.workflows.update(timed["id"], {"revision": timed["revision"], "graph": timed["graph"],
                                                        "publish": True})
        self.assertEqual((published["cron"], published["startsItself"]), ("0 * * * *", True))
        hook = self.save_graph([node("hook", "trigger.webhook"), task("c", "Read C")],
                               [edge("e1", "start", "c"), edge("e2", "hook", "c")])
        never = self.workflows.create({"name": "Never published", "publish": False, "graph": self.graph(
            [node("heard", "trigger.notification"), task("d", "Read D")],
            [edge("e1", "start", "d"), edge("e2", "heard", "d")])})
        self.assertEqual([hook["startsItself"], never["startsItself"]], [False, False])

    def test_listens_for_names_the_published_trigger_when_the_draft_swaps_it(self):
        watching = self.workflows.create({"name": "New DMs", "enabled": True, "graph": self.graph(
            [node("new", "trigger.app", {"app": "instagram", "reader": "dms", "key": "id", "every": 5}),
             task("a", "Read A")], [edge("e1", "start", "a"), edge("e2", "new", "a")])})
        self.assertEqual(watching["listensFor"], "app")
        swapped = self.redraft(watching, [node("heard", "trigger.notification"), task("a", "Read A")],
                               [edge("e1", "start", "a"), edge("e2", "heard", "a")])
        self.assertEqual((swapped["draft"], swapped["listensFor"]), (True, "app"))
        published = self.workflows.update(swapped["id"], {"revision": swapped["revision"], "graph": swapped["graph"],
                                                          "publish": True})
        self.assertEqual(published["listensFor"], "notification")

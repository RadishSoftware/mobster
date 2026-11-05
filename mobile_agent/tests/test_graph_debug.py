import json

from mobile_agent.api_errors import APIError
from mobile_agent.tests.test_graph_run import GraphBase, edge, node, post, task


class DebugTests(GraphBase):
    def chain(self):
        return self.save_graph([task("a", "Read A"), task("b", "Use B"), task("c", "Then C")],
                               [edge("e1", "start", "a"), edge("e2", "a", "b"), edge("e3", "b", "c")])

    def goals(self, since=0):
        return [call.goal for call in self.phone.calls[since:]]

    def test_a_test_run_runs_the_draft_and_its_pins_and_other_runs_never_do(self):
        saved = self.chain()
        draft = json.loads(json.dumps(saved["graph"]))
        nodes = {n["id"]: n for n in draft["nodes"]}
        nodes["a"]["pin"] = {"out": {"answer": "pinned A", "data": None, "status": "completed", "runId": "x",
                                     "costUsd": 0}}
        nodes["c"]["config"]["text"] = "Then C, edited"
        self.workflows.update(saved["id"], {"revision": 1, "graph": draft})
        started = self.workflows.start(saved["id"], "t1", test=True)
        record = self.wait(saved)
        self.assertEqual(self.goals(), ["Use B", "Then C, edited"])
        self.assertEqual((record["test"], record["nodes"]["a"]["pinned"], record["status"]), (True, True, "completed"))
        self.assertEqual(json.loads(self.phone.calls[0].extras["workflowInput"])["answer"], "pinned A")
        self.assertIsNotNone(started["batch"])
        self.go(saved, key="k2")
        self.assertEqual(self.goals(2), ["Read A", "Use B", "Then C"])

    def test_a_test_run_starts_only_from_the_editor(self):
        saved = self.chain()
        with self.assertRaises(APIError) as caught:
            self.workflows.start(saved["id"], "t1", test=True, origin="mcp")
        self.assertEqual((caught.exception.code, self.phone.calls), ("test_from_editor", []))

    def test_a_rerun_from_a_node_reuses_what_ran_before_it(self):
        saved = self.chain()
        _, first = self.go(saved)
        self.workflows.start(saved["id"], "r1", rerun={"from": {"batchId": first["id"], "node": "b"}}, test=True)
        record = self.wait(saved)
        nodes = record["nodes"]
        self.assertEqual(self.goals(3), ["Use B", "Then C"])
        self.assertEqual((nodes["a"]["state"], nodes["a"]["reused"], nodes["b"].get("reused")), ("done", True, None))
        self.assertEqual(record["from"], {"batchId": first["id"], "from": "b"})
        self.assertEqual(json.loads(self.phone.calls[3].extras["workflowInput"])["answer"], "Did: Read A")

    def test_an_edited_input_and_only_one_node(self):
        saved = self.chain()
        _, first = self.go(saved)
        self.workflows.start(saved["id"], "r1", test=True,
                             rerun={"only": {"batchId": first["id"], "node": "b", "input": {"answer": "edited"}}})
        record = self.wait(saved)
        self.assertEqual(self.goals(3), ["Use B"])
        self.assertEqual(json.loads(self.phone.calls[3].extras["workflowInput"]), {"answer": "edited"})
        self.assertEqual(record["nodes"]["c"]["state"], "skipped")
        self.assertIn("Only “b” ran", record["nodes"]["c"]["reason"])

    def test_a_rerun_needs_what_it_reuses_to_have_finished(self):
        self.phone.script = lambda goal, fields: (("blocked", {"reason": "no signal"}, 0.0) if goal == "Read A"
                                                  else ("completed", {"answer": "ok"}, 0.0))
        saved = self.chain()
        _, first = self.go(saved)
        with self.assertRaises(APIError) as caught:
            self.workflows.start(saved["id"], "r1", test=True, rerun={"from": {"batchId": first["id"], "node": "b"}})
        self.assertEqual(caught.exception.code, "rerun_unavailable")
        self.assertIn("“a” didn't finish", str(caught.exception))
        with self.assertRaises(APIError) as gone:
            self.workflows.start(saved["id"], "r2", test=True, rerun={"from": {"batchId": "0" * 12, "node": "b"}})
        self.assertIn("isn't kept", str(gone.exception))

    def test_one_round_of_a_loop_reruns_alone_with_its_item(self):
        saved = self.save_graph(
            [node("list", "transform", {"fields": {"people": "{= ['Sam', 'Kate', 'Ana'] }"}}),
             node("each", "loop", {"over": "items", "collect": "say"}),
             task("say", "Say hello to the person in the input", input="{= $item }", parent="each"),
             task("after", "After the loop")],
            [edge("e1", "start", "list"), edge("e2", "list", "each", "out", "items", map="$input.people"),
             edge("e3", "each", "say", "item"), edge("e4", "each", "after")])
        _, first = self.go(saved)
        self.workflows.start(saved["id"], "r1", test=True,
                             rerun={"from": {"batchId": first["id"], "node": "each", "iteration": 1}})
        record = self.wait(saved)
        new = self.phone.calls[4:]
        self.assertEqual([call.extras["workflowInput"] for call in new], ["Kate"])
        self.assertEqual(record["nodes"]["say@1"]["state"], "done")
        self.assertEqual(record["nodes"]["after"]["state"], "skipped")

    def test_resume_runs_again_what_failed_without_acting_and_reuses_the_rest(self):
        failures = iter([("error", {"reason": "lost the screen"}, 0.0)])

        def script(goal, fields):
            if goal == "Use B":
                return next(failures, ("completed", {"answer": "B worked"}, 0.0))
            return "completed", {"answer": f"Did: {goal}"}, 0.0
        self.phone.script = script
        saved = self.chain()
        _, first = self.go(saved)
        self.assertEqual((first["status"], first["nodes"]["b"]["acted"]), ("error", False))
        self.assertEqual(self.workflows.resume_preview(saved["id"], first["id"]),
                         {"reuse": ["start", "a"], "run": ["b", "c"], "skip": []})
        self.workflows.start(saved["id"], "r1", rerun={"resume": first["id"]})
        record = self.wait(saved)
        self.assertEqual((record["status"], self.goals(2)), ("completed", ["Use B", "Then C"]))
        self.assertTrue(record["nodes"]["a"]["reused"])

    def test_resume_never_runs_again_a_step_that_may_have_acted(self):
        saved = self.chain()
        self.phone.script = lambda goal, fields: "hold" if goal == "Use B" else ("completed", {"answer": "ok"}, 0.0)
        self.workflows.start(saved["id"], "k1")
        run = self.run_of("Use B")
        run.events = [{"event": "approval_requested", "approval_id": "a1"}]
        self.phone.finish(run, "completion_not_confirmed", {"answer": "Sent, but not shown"}, 0.0)
        first = self.wait(saved)
        self.assertEqual(first["nodes"]["b"]["acted"], True)
        preview = self.workflows.resume_preview(saved["id"], first["id"])
        self.assertEqual([item["instance"] for item in preview["skip"]], ["b"])
        self.assertIn("may have acted", preview["skip"][0]["reason"])
        calls = len(self.phone.calls)
        self.workflows.start(saved["id"], "r1", rerun={"resume": first["id"]})
        record = self.wait(saved)
        self.assertEqual(len(self.phone.calls), calls)
        self.assertEqual((record["nodes"]["b"]["state"], record["nodes"]["c"]["state"]), ("failed", "blocked"))
        self.assertEqual(record["status"], "completion_not_confirmed")

    def test_an_expression_previews_against_a_past_runs_values(self):
        saved = self.chain()
        _, first = self.go(saved)
        status, data = post(self.phone, "/api/workflows/evaluate", {
            "expression": "{= upper($input.answer) } / {= $node.a.answer }", "workflowId": saved["id"],
            "batchId": first["id"], "node": "b"})
        self.assertEqual((status, data["value"]), (200, "DID: READ A / Did: Read A"))
        status, data = post(self.phone, "/api/workflows/evaluate", {"expression": "{= 1 }", "workflowId": saved["id"],
                                                                    "batchId": "0" * 12, "node": "b"})
        self.assertEqual((status, data["code"]), (409, "rerun_unavailable"))

    def test_a_prove_saves_the_draft_runs_it_and_pins_what_its_steps_really_did(self):
        saved = self.chain()
        graph = {**saved["graph"], "nodes": [{**n, "title": "Read it"} if n["id"] == "a" else n
                                             for n in saved["graph"]["nodes"]]}
        proved = self.workflows.prove(saved["id"], {"revision": saved["revision"], "graph": graph}, "p1")
        self.assertEqual((proved["workflow"]["revision"], proved["batch"]["id"] is not None), (2, True))
        record = self.wait(saved)
        self.assertEqual((record["test"], record["prove"], record["status"]), (True, True, "completed"))
        self.until(lambda: self.workflows.get(saved["id"]).get("proven"))
        after = self.workflows.get(saved["id"])
        self.assertEqual(after["proven"]["revision"], after["revision"])
        self.assertEqual((after["proven"]["batchId"], after["proven"]["pinned"]), (record["id"], ["a", "b", "c"]))
        pins = {n["id"]: n.get("pin") for n in after["graph"]["nodes"]}
        self.assertEqual(pins["a"]["out"]["answer"], "Did: Read A")
        self.assertIsNone(pins["start"])
        self.assertTrue(all("pin" not in n for n in self.workflows.live(self.workflows._get(saved["id"]))["nodes"]))
        calls = len(self.phone.calls)
        self.workflows.start(saved["id"], "t2", test=True)
        self.wait(saved)
        self.assertEqual(len(self.phone.calls), calls)

    def test_a_draft_saved_while_its_prove_runs_isnt_marked_and_only_the_editor_proves(self):
        saved = self.chain()
        self.phone.script = lambda goal, fields: "hold" if goal == "Use B" else ("completed", {"answer": "ok"}, 0.0)
        self.workflows.prove(saved["id"], {"revision": saved["revision"]}, "p1")
        run = self.run_of("Use B")
        self.workflows.update(saved["id"], {"revision": saved["revision"], "graph": saved["graph"], "name": "Moved on"})
        self.phone.finish(run, "completed", {"answer": "ok"}, 0.0)
        self.assertEqual(self.wait(saved)["status"], "completed")
        after = self.workflows.get(saved["id"])
        self.assertEqual((after.get("proven"), after["revision"]), (None, 2))
        self.assertTrue(all("pin" not in n for n in after["graph"]["nodes"]))
        with self.assertRaises(APIError) as refused:
            self.workflows.prove(saved["id"], {"revision": 1}, "p2")
        self.assertEqual(refused.exception.code, "revision_conflict")
        self.assertEqual(post(self.phone, f"/api/workflows/{saved['id']}/prove", {"revision": 2})[0], 403)
        with self.assertRaises(APIError):
            self.workflows.prove(saved["id"], {"revision": 2}, "p3", origin="mcp")

    def test_bulk_resume_resumes_each_run_in_order_and_refuses_what_it_cant(self):
        broken = {"on": True}

        def script(goal, fields):
            if goal == "Use B" and broken["on"]:
                return "error", {"reason": "lost the screen"}, 0.0
            return "completed", {"answer": f"Did: {goal}"}, 0.0
        self.phone.script = script
        saved = self.chain()
        _, first = self.go(saved, "k1")
        self.time += 60
        _, second = self.go(saved, "k2")
        broken["on"] = False
        _, fine = self.go(saved, "k3")
        answer = self.workflows.resume_all(saved["id"], [second["id"], fine["id"], first["id"]], "bulk")
        self.assertEqual([(item["batchId"], item["state"]) for item in answer["resumes"]],
                         [(first["id"], "queued"), (second["id"], "queued"), (fine["id"], "refused")])
        self.until(lambda: not self.workflows._resuming[saved["id"]].is_alive())
        resumed = [r for r in self.workflows.batch_runs(saved["id"]) if (r.get("from") or {}).get("resume")]
        self.assertEqual(sorted((r["from"]["batchId"], r["status"]) for r in resumed),
                         sorted([(first["id"], "completed"), (second["id"], "completed")]))
        queue = [e for _topic, e in self.phone.events if e.get("event") == "resume_queue"][-1]["resumes"]
        self.assertEqual([item["state"] for item in queue], ["ended", "ended", "refused"])
        with self.assertRaises(ValueError):
            self.workflows.resume_all(saved["id"], [], "bulk2")

    def test_the_timeline_lists_each_step_in_time_and_every_screen_it_kept(self):
        self.phone.script = lambda goal, fields: "hold"
        saved = self.chain()
        self.workflows.start(saved["id"], "k1")
        for goal, frames in (("Read A", [("f1", 1.0), ("f2", 3.0)]), ("Use B", [("f3", 2.0)]), ("Then C", [])):
            run = self.run_of(goal)
            run.events = [{"event": "frontier_step", "frameId": frame, "timestamp": at} for frame, at in frames]
            self.phone.finish(run, "completed", {"answer": "ok"}, 0.0)
        record = self.wait(saved)
        timeline = self.workflows.timeline(saved["id"], record["id"])
        self.assertEqual([step["instance"] for step in timeline["steps"]], ["a", "b", "c"])
        self.assertTrue(all(step["readyAt"] <= step["startedAt"] <= step["finishedAt"] for step in timeline["steps"]))
        self.assertEqual([(f["instance"], f["frameId"]) for f in timeline["frames"]],
                         [("a", "f1"), ("b", "f3"), ("a", "f2")])
        with self.assertRaises(APIError):
            self.workflows.timeline(saved["id"], "0123456789ab")

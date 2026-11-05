"""Published child admission and runtime Result contracts for reusable graph workflows."""

from mobile_agent.tests.test_graph_run import GraphBase, edge, node, task
from mobile_agent.workflows.runs import Dispatch


class ContractTests(GraphBase):
    def child(self, nodes, edges, name="Child", **extra):
        return self.workflows.create({"name": name, "graph": {"version": 2, "nodes": [node("start", "trigger.manual")]
                                                              + nodes, "edges": edges, **extra}})

    def test_an_unpublished_child_refuses_before_starting_its_phone_step(self):
        child = self.workflows.create({"name": "Draft child", "publish": False, "graph": self.graph(
            [task("act", "Do child work")], [edge("e1", "start", "act")])})
        parent = self.save_graph([node("call", "workflow.call", {"workflowId": child["id"]})],
                                 [edge("e1", "start", "call")])
        _, record = self.go(parent)
        self.assertEqual(record["nodes"]["call"]["state"], "failed")
        self.assertTrue(record["nodes"]["call"]["refused"])
        self.assertIn("Publish", record["nodes"]["call"]["reason"])
        self.assertEqual(self.phone.calls, [])
        self.assertEqual(self.workflows.batch_runs(child["id"]), [])

    def test_an_editor_test_can_preview_a_draft_child_without_publishing_it(self):
        grandchild = self.workflows.create({"name": "Draft grandchild", "publish": False, "graph": self.graph(
            [task("act", "Preview grandchild")], [edge("e1", "start", "act")])})
        child = self.workflows.create({"name": "Draft child", "publish": False, "graph": self.graph(
            [node("call", "workflow.call", {"workflowId": grandchild["id"]})],
            [edge("e1", "start", "call")])})
        parent = self.workflows.create({"name": "Draft parent", "publish": False, "graph": self.graph(
            [node("call", "workflow.call", {"workflowId": child["id"]})], [edge("e1", "start", "call")])})
        self.workflows.start(parent["id"], "preview", test=True, origin="app")
        self.assertEqual(self.wait(parent)["status"], "completed")
        self.assertEqual([call.goal for call in self.phone.calls], ["Preview grandchild"])
        self.assertTrue(self.workflows.batch_runs(child["id"])[0]["test"])
        self.assertTrue(self.workflows.batch_runs(grandchild["id"])[0]["test"])

    def test_a_child_missing_a_required_input_refuses_before_an_unrelated_phone_step(self):
        child = self.child([task("act", "Do unrelated work")], [edge("e1", "start", "act")],
                           inputs=[{"name": "code", "type": "string", "required": True}])
        parent = self.save_graph([node("call", "workflow.call", {"workflowId": child["id"]})],
                                 [edge("e1", "start", "call")])
        _, record = self.go(parent)
        self.assertEqual(record["nodes"]["call"]["status"], "missing_values")
        self.assertTrue(record["nodes"]["call"]["refused"])
        self.assertEqual(self.phone.calls, [])
        self.assertEqual(self.workflows.batch_runs(child["id"]), [])

    def test_a_child_accepts_required_false_and_zero_defaults(self):
        child = self.child([node("data", "transform", {"fields": {"active": "{= $inputs.active }",
                                                                "count": "{= $inputs.count }"}}),
                            node("out", "output", {"value": "{= $input }"})],
                           [edge("e1", "start", "data"), edge("e2", "data", "out")],
                           inputs=[{"name": "active", "type": "boolean", "required": True, "default": False},
                                   {"name": "count", "type": "number", "required": True, "default": 0}])
        parent = self.save_graph([node("call", "workflow.call", {"workflowId": child["id"]})],
                                 [edge("e1", "start", "call")])
        _, record = self.go(parent)
        self.assertEqual(record["nodes"]["call"]["output"], {"active": False, "count": 0})

    def test_a_child_row_loop_without_rows_refuses_before_its_phone_step(self):
        child = self.child([node("loop", "loop", {"over": "rows"}), task("act", "Do child work", parent="loop")],
                           [edge("e1", "start", "loop"), edge("e2", "loop", "act", "item")])
        parent = self.save_graph([node("call", "workflow.call", {"workflowId": child["id"]})],
                                 [edge("e1", "start", "call")])
        _, record = self.go(parent)
        self.assertEqual(record["nodes"]["call"]["status"], "missing_values")
        self.assertEqual(self.phone.calls, [])
        self.assertEqual(self.workflows.batch_runs(child["id"]), [])

    def test_a_pinned_child_needs_a_previously_published_version(self):
        child = self.child([task("act", "Published one")], [edge("e1", "start", "act")])
        saved = self.workflows.update(child["id"], {"revision": child["revision"], "publish": False,
                                                    "graph": self.graph([task("act", "Draft two")],
                                                                        [edge("e1", "start", "act")])})
        published_parent = self.save_graph([node("call", "workflow.call", {"workflowId": child["id"],
                                                                             "revision": 1})],
                                           [edge("e1", "start", "call")])
        _, record = self.go(published_parent)
        self.assertEqual(record["status"], "completed")
        self.assertEqual([call.goal for call in self.phone.calls], ["Published one"])
        draft_parent = self.save_graph([node("call", "workflow.call", {"workflowId": child["id"],
                                                                         "revision": saved["revision"]})],
                                       [edge("e1", "start", "call")])
        _, refused = self.go(draft_parent, key="draft-revision")
        self.assertEqual(refused["nodes"]["call"]["state"], "failed")
        self.assertTrue(refused["nodes"]["call"]["refused"])
        self.assertEqual([call.goal for call in self.phone.calls], ["Published one"])

    def test_an_unattended_graph_missing_required_input_refuses_before_a_batch(self):
        workflow = self.save_graph([task("act", "Do unrelated work")], [edge("e1", "start", "act")],
                                   inputs=[{"name": "code", "type": "string", "required": True}])
        key = f"workflow:{workflow['id']}:scheduled:123"
        with self.journal.lock:
            saved = self.workflows._get(workflow["id"])
        self.assertIsNone(self.workflows._claim(saved, key, None))
        with self.assertRaisesRegex(ValueError, "code"):
            self.workflows._dispatch(Dispatch(key, saved))
        self.assertEqual(self.phone.calls, [])
        self.assertEqual(self.workflows.batch_runs(workflow["id"]), [])

    def test_result_shape_refuses_bad_data_before_a_parent_can_forward_it(self):
        shape = {"type": "boolean"}
        child = self.child([node("out", "output", {"value": "{= $input }", "schema": shape})],
                           [edge("e1", "start", "out")])
        parent = self.save_graph([node("call", "workflow.call", {"workflowId": child["id"]}),
                                  task("later", "Act on child result")],
                                 [edge("e1", "start", "call"), edge("e2", "call", "later")])
        _, record = self.go(parent)
        self.assertEqual(record["nodes"]["call"]["state"], "failed")
        self.assertEqual(record["nodes"]["later"]["state"], "blocked")
        child_run = self.workflows.batch_runs(child["id"])[0]
        self.assertEqual(child_run["nodes"]["out"]["status"], "output_schema")
        self.assertEqual(self.phone.calls, [])

    def test_a_handled_bad_result_still_needs_a_valid_result_before_completion(self):
        shape = {"type": "boolean"}
        child = self.child([node("out", "output", {"value": "{= $input }", "schema": shape}),
                            node("recover", "transform", {"fields": {"message": "Not a result"}})],
                           [edge("e1", "start", "out"), edge("e2", "out", "recover", "error")])
        parent = self.save_graph([node("call", "workflow.call", {"workflowId": child["id"]}),
                                  task("later", "Act on child result")],
                                 [edge("e1", "start", "call"), edge("e2", "call", "later")])
        _, record = self.go(parent)
        self.assertEqual(record["nodes"]["call"]["state"], "failed")
        self.assertEqual(record["nodes"]["later"]["state"], "blocked")
        self.assertEqual(self.workflows.batch_runs(child["id"])[0]["status"], "output_schema")
        self.assertEqual(self.phone.calls, [])

    def test_a_branch_without_its_declared_result_does_not_complete(self):
        workflow = self.save_graph(
            [node("branch", "branch", {"cases": [{"id": "yes", "label": "Yes", "when": "false"}]}),
             node("out", "output", {"value": "{= true }", "schema": {"type": "boolean"}}),
             node("other", "transform", {"fields": {"message": "No result"}})],
            [edge("e1", "start", "branch"), edge("e2", "branch", "out", "yes"),
             edge("e3", "branch", "other", "else")])
        _, record = self.go(workflow)
        self.assertEqual(record["status"], "output_schema")
        self.assertEqual(record["nodes"]["out"]["state"], "skipped")

    def test_zero_loop_rounds_cannot_satisfy_a_declared_result(self):
        workflow = self.save_graph(
            [node("items", "transform", {"fields": {"list": "{= [] }"}}),
             node("each", "loop", {"over": "items", "collect": "item"}),
             node("item", "transform", {"fields": {"value": "{= $item }"}}, parent="each"),
             node("branch", "branch", {"cases": [{"id": "some", "label": "Some",
                                                    "when": "$input.count > 0"}]}),
             node("out", "output", {"value": "{= true }", "schema": {"type": "boolean"}}),
             node("other", "transform", {"fields": {"message": "No result"}})],
            [edge("e1", "start", "items"), edge("e2", "items", "each", "out", "items", map="$input.list"),
             edge("e3", "each", "item", "item"), edge("e4", "each", "branch"),
             edge("e5", "branch", "out", "some"), edge("e6", "branch", "other", "else")])
        _, record = self.go(workflow)
        self.assertEqual(record["status"], "output_schema")
        self.assertEqual(record["nodes"]["out"]["state"], "skipped")

    def test_result_shape_accepts_false_zero_and_null(self):
        for key, value, kind in (("false", "false", "boolean"), ("zero", "0", "integer"),
                                 ("null", "null", "null")):
            with self.subTest(value=key):
                workflow = self.save_graph([node("out", "output", {"value": "{= " + value + " }",
                                                                  "schema": {"type": kind}})],
                                           [edge("e1", "start", "out")])
                _, record = self.go(workflow, key=key)
                self.assertEqual(record["status"], "completed")
                self.assertEqual(record["nodes"]["out"]["state"], "done")

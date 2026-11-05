from mobile_agent.api_errors import APIError
from mobile_agent.tests.test_workflows import WorkflowCase


class HistoryLookupTests(WorkflowCase):
    def test_missing_versions_and_deleted_workflows_keep_distinct_errors(self):
        workflow = self.saved()
        history = self.workflows.history
        identifier = workflow["id"]
        operations = {
            "read": lambda: history.version(identifier, 999),
            "publish": lambda: history.publish(identifier, {"revision": 999}),
            "rollback": lambda: history.rollback(identifier, {"revision": 999, "current": 1}),
            "child call": lambda: history.call_graph(identifier, 999),
        }
        for deleted, code in ((False, "version_not_found"), (True, "not_found")):
            if deleted:
                self.workflows.delete(identifier)
            for name, operation in operations.items():
                with self.subTest(operation=name, deleted=deleted), self.assertRaises(APIError) as raised:
                    operation()
                self.assertEqual((raised.exception.status, raised.exception.code), (404, code))

    def test_child_calls_require_publication_of_the_requested_version(self):
        workflow = self.saved(publish=False)
        history = self.workflows.history
        identifier = workflow["id"]
        self.assertEqual(history.version(identifier, 1)["graph"], workflow["graph"])
        self.assertEqual(history.call_graph(identifier, 1, allow_draft=True), workflow["graph"])
        with self.assertRaises(APIError) as raised:
            history.call_graph(identifier, 1)
        self.assertEqual((raised.exception.status, raised.exception.code), (409, "not_published"))
        history.publish(identifier, {"revision": 1})
        self.assertEqual(history.call_graph(identifier, 1), workflow["graph"])

    def test_rollback_rejects_a_stale_revision_before_looking_up_the_target(self):
        workflow = self.saved()
        with self.assertRaises(APIError) as raised:
            self.workflows.history.rollback(workflow["id"], {"revision": 999, "current": 0})
        self.assertEqual((raised.exception.status, raised.exception.code), (409, "revision_conflict"))
        self.assertEqual(self.workflows.get(workflow["id"])["revision"], 1)

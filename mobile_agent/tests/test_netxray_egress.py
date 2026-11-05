import io
import json
import unittest
from email.message import Message as Headers
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import harness_api, share
from mobile_agent.api_errors import APIError
from mobile_agent.feedback import bundle
from mobile_agent.outlets import service as outlets
from mobile_agent.runtime_create import ANY_APP
from mobile_agent.server import make_handler
from mobile_agent.tests.test_netxray import SESSION
from mobile_agent.tests.test_server_engine import Base

DONE = {"event": "result", "status": "completed", "outcome": "done", "answer": "In 59 s 2 apps contacted 15 domains.",
        "reason": None, "independently_verified": True, "actions": 0, "decisions": 0, "elapsed_ms": 0, "model": None,
        "proof": [{"quote": "Read your iPhone's network traffic.", "app": "Any app"}]}
TOKEN = "t" * 40


class Case(Base):
    def setUp(self):
        super().setUp()
        self.runtime_ = self.runtime()
        self.ordinary = self.compose("ordinary", {})
        self.local = self.compose("network-xray", {harness_api.STAY_ON_MAC: True})

    def compose(self, key, extras):
        return self.runtime_.compose_result(app=dict(ANY_APP), goal=f"Result {key}", summary=DONE, idempotency_key=key,
                                            origin="app", extras=extras)

    def call(self, path, session=None):
        handler = object.__new__(make_handler(self.runtime_, app_session=SESSION))
        handler.headers = Headers()
        handler.headers["Host"] = "127.0.0.1:8765"
        if session:
            handler.headers["X-Mobster-App-Session"] = session
        handler.command, handler.path = "GET", path
        handler.rfile, handler.wfile = io.BytesIO(b""), io.BytesIO()
        handler.connection = Mock()
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.do_GET()
        return statuses[-1], json.loads(handler.wfile.getvalue() or b"{}")


class TheField(unittest.TestCase):
    def test_only_the_field_set_to_true_keeps_a_run_on_this_mac(self):
        self.assertTrue(harness_api.stays_on_mac({"stayOnMac": True}))
        for extras in ({}, None, {"stayOnMac": "yes"}, {"stayOnMac": 1}, {"threadId": "a" * 12}):
            self.assertFalse(harness_api.stays_on_mac(extras), extras)


class Share(Case):
    def test_no_link_is_made_of_a_report_that_stays_on_this_mac(self):
        with patch.object(share, "create", side_effect=AssertionError("nothing may be uploaded")):
            for attempt in (lambda: self.runtime_.share_run(self.local.id, TOKEN),
                            lambda: self.runtime_.share_preview(self.local.id)):
                with self.assertRaises(APIError) as caught:
                    attempt()
                self.assertEqual((caught.exception.status, caught.exception.code), (403, "stays_on_mac"))
        self.assertNotIn("shares", self.local.summary)

    def test_an_ordinary_result_still_gets_its_link(self):
        made = {"id": "s" * 22, "url": "https://mobster.dev/s/x", "title": "Result ordinary", "createdAt": 1,
                "expiresAt": 2}
        with patch.object(share, "create", return_value=made) as create:
            answer = self.runtime_.share_run(self.ordinary.id, TOKEN)
        create.assert_called_once()
        self.assertEqual(answer["share"]["url"], "https://mobster.dev/s/x")


class Destinations(unittest.TestCase):
    def service(self):
        rows = [{"id": "every", "config": {"scope": "all"}}, {"id": "named", "config": {}}]
        return SimpleNamespace(store=SimpleNamespace(destinations=lambda: rows))

    def test_a_report_that_stays_on_this_mac_goes_to_no_destination_even_one_it_names(self):
        run = SimpleNamespace(extras={"stayOnMac": True, "sendTo": {"destination": "named"}})
        for event in ("run.started", "run.finished", "needs_you"):
            self.assertEqual(outlets.subscribers(self.service(), run, event), [], event)

    def test_an_ordinary_result_still_goes_to_every_destination_that_takes_all_results(self):
        run = SimpleNamespace(extras={"sendTo": {"destination": "named"}})
        self.assertEqual(outlets.subscribers(self.service(), run, "run.finished"), ["named", "every"])


class Feedback(Case):
    def test_a_feedback_report_never_carries_a_report_that_stays_on_this_mac(self):
        self.assertIsNone(bundle.last_run(self.runtime_, self.local.id))
        self.assertEqual(bundle.last_run(self.runtime_)["id"], self.ordinary.id)
        text = bundle.build(self.runtime_)["text"]
        self.assertNotIn(self.local.id, text)
        self.assertNotIn("Result network-xray", text)


class Reads(Case):
    def test_only_the_mac_app_reads_a_report_that_stays_on_this_mac(self):
        for path in (f"/api/runs/{self.local.id}", f"/api/runs/{self.local.id}/usage",
                     f"/api/runs/{self.local.id}/screenshot"):
            self.assertEqual(self.call(path)[0], 404, path)
        status, body = self.call(f"/api/runs/{self.local.id}", session=SESSION)
        self.assertEqual((status, body["run"]["id"]), (200, self.local.id))
        listed = [row["id"] for row in self.call("/api/runs")[1]["runs"]]
        self.assertEqual(listed, [self.ordinary.id])
        listed = [row["id"] for row in self.call("/api/runs", session=SESSION)[1]["runs"]]
        self.assertEqual(listed, [self.local.id, self.ordinary.id])

    def test_an_ordinary_result_is_still_read_with_a_token(self):
        status, body = self.call(f"/api/runs/{self.ordinary.id}")
        self.assertEqual((status, body["run"]["id"]), (200, self.ordinary.id))


if __name__ == "__main__":
    unittest.main()

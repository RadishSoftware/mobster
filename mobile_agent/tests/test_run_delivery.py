import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import run_delivery
from mobile_agent.api_errors import APIError
from mobile_agent.connectors import client as mcp

GITHUB = {"id": "a" * 12, "name": "GitHub", "preset": "github", "enabled": True, "modes": {},
          "tools": [{"name": "issue_write"}, {"name": "search_issues"}]}
LINEAR = {"id": "b" * 12, "name": "Linear", "preset": "linear", "enabled": True, "modes": {},
          "tools": [{"name": "save_issue"}]}
TARGET = {"owner": "getmobster", "repo": "app"}


def make_run():
    return SimpleNamespace(
        id="c" * 12, goal="Check out with the saved card", status="failed", created_at=1.0, finished_at=2.0,
        device_name="Studio iPhone", issues=[], condition=threading.Condition(), journal=Mock(),
        summary={"status": "failed", "outcome": "failed", "answer": "The pay button never enabled.",
                 "reason": "Pay stayed disabled after the card was chosen."})


class Connectors:
    def __init__(self, rows, answer=None):
        self.rows, self.answer, self.calls = rows, answer, []
        self.store = SimpleNamespace(all=lambda: self.rows)

    def call(self, row, tool, arguments, timeout=None):
        self.calls.append((row["name"], tool, arguments))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer or {"content": [{"type": "text", "text": "Created https://github.com/mozilla-mobile/app/issues/7."}]}


class IssueTests(unittest.TestCase):
    def file(self, rows, body=None, answer=None, run=None):
        service = Connectors(rows, answer)
        run = run or make_run()
        runtime = SimpleNamespace(publish=Mock())
        with patch.object(run_delivery.connector_service, "service_for", lambda runtime: service):
            try:
                return service, run, run_delivery.file_issue(runtime, run, {"target": TARGET, **(body or {})})
            except APIError as error:
                return service, run, error

    def test_the_result_is_filed_through_the_connected_tracker(self):
        service, run, answer = self.file([GITHUB])
        [(name, tool, arguments)] = service.calls
        self.assertEqual((name, tool), ("GitHub", "issue_write"))
        self.assertEqual((arguments["method"], arguments["owner"], arguments["repo"]), ("create", "getmobster", "app"))
        self.assertIn("Pay stayed disabled", arguments["body"])
        self.assertIn("Studio iPhone", arguments["body"])
        self.assertEqual((answer["issue"]["status"], answer["issue"]["url"]),
                         ("filed", "https://github.com/mozilla-mobile/app/issues/7"))
        # the attempt is on the run before the tool is called, and again once it ended
        states = [call.args[1]["issues"][0]["status"] for call in run.journal.annotate.call_args_list]
        self.assertEqual(states, ["filing", "filed"])

    def test_it_refuses_with_a_reason_when_nothing_suitable_is_connected(self):
        for rows, code in (([], "no_tracker"), ([{**GITHUB, "enabled": False}], "no_tracker"),
                           ([GITHUB, LINEAR], "tracker_ambiguous"),
                           ([{**GITHUB, "tools": [{"name": "search_issues"}]}], "tool_missing"),
                           ([{**GITHUB, "modes": {"issue_write": "off"}}], "tool_blocked")):
            with self.subTest(code=code, rows=len(rows)):
                service, run, error = self.file(rows)
                self.assertEqual((error.code, service.calls, run.issues), (code, [], []))
        service, run, answer = self.file([GITHUB, LINEAR], {"tracker": "linear"})
        self.assertEqual(service.calls[0][:2], ("Linear", "save_issue"))

    def test_an_answer_that_never_came_is_not_sent_again(self):
        service, run, answer = self.file([GITHUB], answer=mcp.OutcomeUnknown("It timed out."))
        self.assertEqual(answer["issue"]["status"], "unknown")
        self.assertEqual(len(service.calls), 1)
        for body in ({}, {"again": True}):
            again = Connectors([GITHUB])
            with patch.object(run_delivery.connector_service, "service_for", lambda runtime: again):
                with self.assertRaises(APIError) as caught:
                    run_delivery.file_issue(SimpleNamespace(publish=Mock()), run, {"target": TARGET, **body})
            self.assertEqual((caught.exception.code, again.calls), ("outcome_unknown", []))

    def test_a_crash_after_the_note_leaves_filing_and_that_is_never_retried_either(self):
        run = make_run()
        run.issues = [{"id": "d" * 12, "connector": GITHUB["id"], "status": "filing"}]
        service, _, error = self.file([GITHUB], run=run)
        self.assertEqual((error.code, service.calls), ("outcome_unknown", []))

    def test_a_filed_result_is_not_filed_twice_unless_asked(self):
        service, run, _ = self.file([GITHUB])
        again, _, error = self.file([GITHUB], run=run)
        self.assertEqual((error.code, again.calls), ("already_filed", []))
        again, _, answer = self.file([GITHUB], {"again": True}, run=run)
        self.assertEqual(len(again.calls), 1)

    def test_a_server_error_is_a_failure_with_its_words(self):
        service, run, answer = self.file(
            [GITHUB], answer={"isError": True, "content": [{"type": "text", "text": "Not Found"}]})
        self.assertEqual((answer["issue"]["status"], answer["issue"]["error"]), ("failed", "Not Found"))

    def test_the_target_holds_only_known_keys(self):
        with self.assertRaises(ValueError):
            self.file([GITHUB], {"target": {"token": "x"}})


class SendTests(unittest.TestCase):
    def service(self, destinations):
        sent = []
        service = SimpleNamespace(
            store=SimpleNamespace(destinations=lambda: destinations),
            find=lambda wanted: next((d for d in destinations if wanted in (d["id"], d["name"])), None) or
            (_ for _ in ()).throw(ValueError(f"No destination is called {wanted}.")),
            enqueue=lambda run_id, destination_id: sent.append((run_id, destination_id)) or {"id": "e" * 12},
            public_delivery=lambda row: {"id": row["id"], "status": "sending"})
        return service, sent

    def send(self, destinations, wanted):
        service, sent = self.service(destinations)
        with patch.object(run_delivery.outlet_service, "service_for", lambda runtime: service):
            try:
                return run_delivery.send(SimpleNamespace(), make_run(), wanted), sent
            except APIError as error:
                return error, sent

    def test_the_result_goes_to_the_named_destination(self):
        answer, sent = self.send([{"id": "f" * 12, "name": "#qa"}], "#qa")
        self.assertEqual((answer["delivery"]["status"], sent), ("sending", [("c" * 12, "f" * 12)]))

    def test_it_refuses_with_a_reason_and_sends_nothing(self):
        for destinations, wanted, code in (([], "#qa", "no_destination"), ([{"id": "f" * 12, "name": "#qa"}], "#ops",
                                                                         "destination_not_found")):
            with self.subTest(code=code):
                error, sent = self.send(destinations, wanted)
                self.assertEqual((error.code, sent), (code, []))

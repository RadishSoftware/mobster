import json
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import api_routes
from mobile_agent.connectors import client as mcp
from mobile_agent.outlets import net
from mobile_agent.outlets import service as outlet_service
from mobile_agent.testkit import notify, register
from mobile_agent.tests.test_outlets import CHANNELS, Connected, Connectors, reply

POST = ("POST", "https://slack.com/api/chat.postMessage")


def suite_doc(*statuses, healed=0, quarantined=False):
    checks = []
    for number, status in enumerate(statuses):
        result = {"status": status, "device": "iPhone 17 Pro", "healed": [{}] * healed if number == 0 else [],
                  "reason": None if status == "passed" else {"message": "Expected 'Total $12' and found 'Total $0'"},
                  "quarantined": quarantined}
        checks.append({"name": f"checkout {number}", "results": [result]})
    summary = {"passed": statuses.count("passed"), "failed": statuses.count("failed")}
    return {"checks": checks, "summary": summary, "durationMs": 61000}


class RulesTests(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.root = Path(tempfile.mkdtemp(prefix="mobster-notify-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / ".mobster").mkdir()

    def write(self, text):
        (self.root / ".mobster" / notify.RULES_FILE).write_text(text, encoding="utf-8")

    def test_no_file_means_no_rules_and_a_file_reads_its_destination_and_when(self):
        self.assertIsNone(notify.load_rules(self.root))
        self.write("slack:\n  destination: Team channel\n")
        rules = notify.load_rules(self.root)
        self.assertEqual((rules.destination, rules.on), ("Team channel", ("fail",)))
        self.write("slack:\n  destination: Team\n  on: [fix, always]\n")
        self.assertEqual(notify.load_rules(self.root).on, ("always", "fix"))

    def test_a_wrong_file_says_what_to_write(self):
        for text, fragment in (("slack: 3\n", "holds a `slack:` section"),
                               ("slack:\n  on: fail\n", "needs `destination:`"),
                               ("slack:\n  destination: T\n  on: sometimes\n", "fail, always or fix"),
                               ("slack:\n  destination: T\n  channel: C1\n", "slack has no `channel`"),
                               ("email: a@b.c\n", "holds a `slack:` section"),
                               ("slack: [\n", "isn't valid YAML")):
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaises(notify.RulesError) as caught:
                    notify.load_rules(self.root)
                self.assertIn(fragment, str(caught.exception))

    def test_which_suites_ask_for_a_message(self):
        def rules(*on):
            return notify.Rules("Team", on)
        red, green = notify.summarize(suite_doc("passed", "failed")), notify.summarize(suite_doc("passed"))
        healed = notify.summarize(suite_doc("passed", healed=2))
        self.assertTrue(notify.wanted(rules("fail"), red, None))
        self.assertFalse(notify.wanted(rules("fail"), green, False))
        self.assertTrue(notify.wanted(rules("fail"), healed, None), "a healed step waits for review")
        self.assertTrue(notify.wanted(rules("always"), green, None))
        self.assertTrue(notify.wanted(rules("fix"), green, False))
        self.assertFalse(notify.wanted(rules("fix"), green, True), "it was green already")
        self.assertFalse(notify.wanted(rules("fix"), green, None), "no suite before it")
        self.assertFalse(notify.wanted(rules("fix"), red, False), "still red")
        quarantined = notify.summarize(suite_doc("failed", quarantined=True))
        self.assertTrue(quarantined["green"], "a quarantined check never decides the suite")


class MessageTests(unittest.TestCase):
    def test_the_failing_checks_the_review_notice_and_the_report(self):
        summary = notify.summarize(suite_doc("passed", "failed", healed=1))
        message = notify.report(summary, project="shop", link="https://reports.mobster.dev/r/abc#k", folder="/x")
        self.assertEqual(message["title"], "shop: 1 check didn't pass")
        self.assertIn("Failed: checkout 1 on iPhone 17 Pro: Expected 'Total $12' and found 'Total $0'",
                      message["lines"])
        self.assertTrue(any(line.startswith("Needs review: 1 healed step.") and "mobster test heal" in line
                            for line in message["lines"]))
        self.assertEqual(message["lines"][-1], "Report: https://reports.mobster.dev/r/abc#k")
        local = notify.report(summary, project="shop", folder="/work/shop/.mobster/test-results/s")
        self.assertEqual(local["lines"][-1], "Report: /work/shop/.mobster/test-results/s")
        fixed = notify.report(notify.summarize(suite_doc("passed")), project="shop", fixed=True)
        self.assertEqual(fixed["title"], "shop: the checks pass again")

    def test_it_is_cut_to_a_readable_length(self):
        doc = suite_doc(*["failed"] * 12)
        message = notify.report(notify.summarize(doc), project="shop")
        self.assertEqual(len(message["lines"]), 1 + notify.MAX_ROWS + 1)
        self.assertEqual(message["lines"][-1], "4 more in the report.")


class Posting(Connected):
    def setUp(self):
        super().setUp()
        self.connectors = Connectors()
        self.http.routes = {("GET", "https://slack.com/api/auth.test"): [reply(200, {"ok": True,
            "team": "Radish Software"})],
                            ("GET", "https://slack.com/api/users.conversations?"): [CHANNELS]}
        found = self.service.connect({"kind": "slack"})["connect"]
        self.slack = self.service.add({"name": "Team channel", "kind": "slack", "connect": found["id"],
                                       "target": "C0123TEAM"})
        self.folder_of_suite = self.folder / "suite"
        self.folder_of_suite.mkdir()
        patcher = mock.patch.object(outlet_service, "service_for", lambda runtime: self.service)
        patcher.start()
        self.addCleanup(patcher.stop)

    def route(self, body):
        request = api_routes.Request(runtime=self.runtime, method="POST", path="/api/tests/notify", params={},
                                     query={}, headers={}, body=body, content_type="application/json", origin="cli",
                                     app_session=False)
        response = register.notify(request)
        return response.status, response.value

    def message(self, **extra):
        return {"title": "shop: 1 check didn't pass",
                "lines": ["1 passed, 1 failed in 1 min 01 s", "Failed: checkout <!channel> & @here",
                          "Report: https://reports.mobster.dev/r/abc#k"], "link": "https://reports.mobster.dev/r/abc#k",
                **extra}

    def test_it_posts_the_suite_as_you_with_mentions_escaped_and_the_report_linked(self):
        self.http.routes = {POST: [reply(200, {"ok": True, "ts": "1.2"})]}
        status, value = self.route({"destination": "team channel", "report": self.message()})
        self.assertEqual((status, value["ok"]), (200, True))
        [body] = self.http.bodies("POST")
        self.assertEqual((body["channel"], body["link_names"]), ("C0123TEAM", False))
        section = body["blocks"][0]["text"]["text"]
        self.assertTrue(section.startswith("*shop: 1 check didn't pass*\n"))
        self.assertIn("&lt;!channel&gt; &amp; @here", section)
        self.assertNotIn("<!channel>", json.dumps(body))
        self.assertIn("<https://reports.mobster.dev/r/abc#k|Report>", section)

    def test_a_secret_in_a_failure_is_masked(self):
        self.http.routes = {POST: [reply(200, {"ok": True})]}
        secret = "sk-ant-api03-" + "A" * 40
        lines = ["1 failed", f"Failed: login: found token {secret}"]
        self.route({"destination": self.slack["id"], "report": self.message(lines=lines)})
        self.assertNotIn(secret, json.dumps(self.http.bodies("POST")))

    def test_it_refuses_without_a_slack_sign_in_and_posts_nothing(self):
        self.connectors.refused = mcp.AuthNeeded("Slack needs you to sign in again.")
        status, value = self.route({"destination": "Team channel", "report": self.message()})
        self.assertEqual((status, value["ok"], value["unsent"]), (200, False, True))
        self.assertIn("Slack", value["message"])
        self.assertEqual(self.http.bodies("POST"), [])

    def test_it_refuses_a_destination_that_is_missing_or_not_slack(self):
        self.service.add({"name": "Hook", "kind": "webhook", "url": "https://example.com/hook"})
        for name, status, code in (("Nobody", 404, "destination_not_found"), ("Hook", 409, "not_slack")):
            with self.subTest(name=name):
                answer, value = self.route({"destination": name, "report": self.message()})
                self.assertEqual((answer, value["code"]), (status, code))
        for body in ({"destination": "Team channel"}, {"destination": "Team channel", "report": {"title": "x"}},
                     {"destination": "Team channel", "report": self.message(), "channel": "C1"}):
            with self.subTest(body=body):
                self.assertEqual(self.route(body)[0], 400)
        self.assertEqual(self.http.bodies("POST"), [])

    def test_a_post_that_may_have_arrived_is_not_sent_again(self):
        self.http.routes = {POST: [reply(502)]}
        status, value = self.route({"destination": "Team channel", "report": self.message()})
        self.assertEqual((status, value["ok"], value["unsent"]), (200, False, False))
        self.assertEqual(len(self.http.bodies("POST")), 1, "one try, even with a retry-able answer")

    def test_a_dropped_connection_after_the_post_began_is_unknown(self):
        self.http.routes = {POST: [net.NetError("The connection dropped.", retry=False, unsent=False)]}
        status, value = self.route({"destination": "Team channel", "report": self.message()})
        self.assertEqual((status, value["ok"], value["unsent"]), (200, False, False))


class SlackBudgetTests(unittest.TestCase):
    def test_title_and_body_share_the_section_limit(self):
        from mobile_agent.outlets.slack import SECTION, Slack
        message = Slack.suite_message({"report": {"title": "T" * 300, "lines": ["X" * 1000] * 20}})
        self.assertLessEqual(len(message["blocks"][0]["text"]["text"]), SECTION)


class Told(unittest.TestCase):

    def setUp(self):
        import shutil
        import tempfile
        self.root = Path(tempfile.mkdtemp(prefix="mobster-notify-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / ".mobster").mkdir()
        (self.root / ".mobster" / notify.RULES_FILE).write_text("slack:\n  destination: Team\n  on: fail\n")
        self.folder = self.root / ".mobster" / "test-results" / "20261017-120000-abcd"
        self.folder.mkdir(parents=True)
        self.sent = []

    def tell(self, doc=None, ask=None, previous=lambda root, folder: None):
        def default(destination, message):
            self.sent.append((destination, message))
            return {"ok": True, "unsent": False, "message": ""}
        return notify.notify(self.root, self.folder, doc or suite_doc("failed"), ask=ask or default,
                             previous=previous)

    def test_a_failing_suite_is_told_once(self):
        self.assertEqual(self.tell(), "mobster test: told Team on Slack.")
        [(destination, message)] = self.sent
        self.assertEqual(destination, "Team")
        self.assertEqual(message["title"], f"{self.root.name}: 1 check didn't pass")
        self.assertIn("already told", self.tell())
        self.assertEqual(len(self.sent), 1, "the marker keeps a second command from posting it again")

    def test_a_green_suite_and_a_project_without_rules_post_nothing(self):
        self.assertIsNone(self.tell(suite_doc("passed")))
        (self.root / ".mobster" / notify.RULES_FILE).unlink()
        self.assertIsNone(self.tell())
        self.assertEqual(self.sent, [])

    def test_a_request_that_never_left_can_be_made_again(self):
        def refused(destination, message):
            raise notify.NotSent("Open Mobster, so it can post.")
        self.assertIn("Slack wasn't told. Open Mobster", self.tell(ask=refused))
        self.assertFalse((self.folder / notify.MARKER).exists())
        self.assertEqual(self.tell(), "mobster test: told Team on Slack.")

    def test_unsent_by_the_destination_can_be_made_again(self):
        answer = {"ok": False, "unsent": True, "message": "Slack signed Mobster out."}
        self.assertIn("Slack wasn't told. Slack signed Mobster out.", self.tell(ask=lambda d, m: answer))
        self.assertFalse((self.folder / notify.MARKER).exists())

    def test_no_answer_after_the_request_left_is_never_resent(self):
        def silent(destination, message):
            raise TimeoutError("no answer")
        self.assertIn("may have been told", self.tell(ask=silent))
        self.assertTrue((self.folder / notify.MARKER).exists())
        self.assertIn("already told", self.tell())
        self.assertEqual(self.sent, [])

    def test_an_unknown_outcome_from_slack_is_never_resent(self):
        answer = {"ok": False, "unsent": False, "message": "Slack answered 502."}
        self.assertIn("doesn't post it again", self.tell(ask=lambda d, m: answer))
        self.assertEqual(json.loads((self.folder / notify.MARKER).read_text())["state"], "unknown")
        self.assertIn("already told", self.tell())
        self.assertEqual(self.sent, [])

    def test_a_wrong_rules_file_is_one_line_and_the_suite_is_untouched(self):
        (self.root / ".mobster" / notify.RULES_FILE).write_text("slack: 3\n")
        self.assertIn("holds a `slack:` section", self.tell())
        self.assertEqual(self.sent, [])

    def test_connection_preflight_refusal_can_be_retried(self):
        from mobile_agent.threads.client import Client, NoService, ServiceError
        for error in (NoService("Open Mobster."), ServiceError(401, {"error": "Sign in."})):
            with self.subTest(error=type(error).__name__), mock.patch.object(Client, "connect", side_effect=error):
                self.assertIn("Slack wasn't told", self.tell(ask=notify.ask_mobster))
                self.assertFalse((self.folder / notify.MARKER).exists())

    def test_http_server_error_after_post_is_unknown(self):
        from mobile_agent.threads.client import Client, ServiceError
        client = mock.Mock()
        client.call.side_effect = ServiceError(500, {"error": "Internal error."})
        with mock.patch.object(Client, "connect", return_value=client):
            self.assertIn("may have been told", self.tell(ask=notify.ask_mobster))
        self.assertTrue((self.folder / notify.MARKER).exists())
        self.assertIn("already told", self.tell())
        self.assertEqual(self.sent, [])

    def test_on_fix_needs_the_suite_before_to_have_failed(self):
        (self.root / ".mobster" / notify.RULES_FILE).write_text("slack:\n  destination: Team\n  on: fix\n")
        self.assertIsNone(self.tell(suite_doc("passed"), previous=lambda root, folder: True))
        self.assertIn("told Team", self.tell(suite_doc("passed"), previous=lambda root, folder: False))
        self.assertEqual(self.sent[0][1]["title"], f"{self.root.name}: the checks pass again")


if __name__ == "__main__":
    unittest.main()

import unittest
from types import SimpleNamespace
from unittest import mock

from mobile_agent.graph import phone_steps
from mobile_agent.graph.schema import GraphError, validate
from mobile_agent.skills.notifications import Row
from mobile_agent.tests.test_graph_run import GraphBase, edge, node


def ctx(**extras):
    return SimpleNamespace(extras=extras, emit=lambda event: None, cancelled=lambda: False, clarify=None,
                           runtime=None, run_id="r1")


class Driver:
    def __init__(self, bundle="com.apple.mobilesafari"):
        self.opened = []
        self.bundle = bundle

    def observe(self, timeout=10):
        return SimpleNamespace(bundle_id=self.bundle, elements=[], width=390, height=844)


class OpenUrlTests(unittest.TestCase):
    def run_hook(self, url, approve=None, runtime=None):
        driver = Driver()
        context = ctx(openUrl={"url": url})
        context.runtime = runtime
        with mock.patch.object(phone_steps.deeplinks, "open_link",
                               lambda d, u, bundle=None, **k: driver.opened.append(u)
                               or {"url": u, "bundleId": bundle}):
            return phone_steps.open_url_hook(context, driver, approve).result, driver

    def test_a_web_address_opens_and_reports_where_it_landed(self):
        result, driver = self.run_hook("https://example.com/hours")
        self.assertEqual((result["status"], driver.opened), ("completed", ["https://example.com/hours"]))
        self.assertEqual(result["data"]["landedApp"], "com.apple.mobilesafari")

    def test_a_link_that_acts_asks_and_is_not_opened_without_an_answer_or_on_a_no(self):
        for approve in (None, lambda request: "denied"):
            result, driver = self.run_hook("sms:+15555550123", approve)
            self.assertEqual((result["status"], driver.opened), ("completion_not_confirmed", []))
        asked = []
        result, driver = self.run_hook("sms:+15555550123", lambda request: asked.append(request["title"]) or "approved")
        self.assertEqual((result["status"], len(driver.opened), len(asked)), ("completed", 1, 1))

    def test_a_scheme_mobster_never_opens_is_refused_before_a_run_and_at_save(self):
        with self.assertRaises(ValueError):
            phone_steps.validate_open_url({"url": "javascript:alert(1)"}, None)
        with self.assertRaises(GraphError):
            validate({"version": 2, "nodes": [node("start", "trigger.manual"),
                                              node("go", "phone.open_url", {"url": "javascript:alert(1)"})],
                      "edges": [edge("e1", "start", "go")]})

    def test_an_app_the_person_has_not_allowed_is_not_opened(self):
        runtime = SimpleNamespace(allows_app=lambda run_id, bundle: False)
        result, driver = self.run_hook("https://example.com", runtime=runtime)
        self.assertEqual((result["status"], driver.opened), ("completion_not_confirmed", []))


class FakeCenter:
    def __init__(self, batches, tap_ok=True):
        self.batches, self.tapped, self.tap_ok = list(batches), [], tap_ok

    def read(self, **kwargs):
        return self.batches.pop(0) if self.batches else []

    def open_notification(self, text, **kwargs):
        self.tapped.append(text)
        return self.tap_ok


def row(text, app="Messages", sender="Sam", body="running late"):
    return Row(text=text, app=app, sender=sender, age_s=5, body=body)


class NotificationTests(unittest.TestCase):
    def wait(self, center, **spec):
        spec = {"app": None, "match": None, "timeoutMinutes": 1, "tap": True, **spec}
        ticks = iter(range(0, 10_000, 20))
        return phone_steps.notification_hook(ctx(waitNotification=spec), Driver(), None, clock=lambda: next(ticks),
                                            sleep=lambda s: None, center=center).result

    def test_a_matching_notification_is_read_and_tapped_once(self):
        center = FakeCenter([[row("Messages, Sam, running late", body="running late")]])
        result = self.wait(center, match="running late", app="com.apple.MobileSMS")
        self.assertEqual((result["status"], result["data"]["title"], result["data"]["tapped"]),
                         ("completed", "Sam", True))
        self.assertEqual(center.tapped, ["Messages, Sam, running late"])

    def test_it_waits_through_other_notifications_and_taps_nothing_when_tap_is_off(self):
        center = FakeCenter([[row("Mail, Ana, hello", app="Mail")], [row("Messages, Sam, code 4242")]])
        result = self.wait(center, app="com.apple.MobileSMS", tap=False)
        self.assertEqual((result["data"]["app"], center.tapped), ("Messages", []))

    def test_none_in_time_is_a_timeout_not_a_failure_and_taps_nothing(self):
        center = FakeCenter([])
        result = self.wait(center, match="never")
        self.assertEqual((result["status"], result["data"], center.tapped), ("completed", {"timeout": True}, []))

    def test_a_notification_gone_before_the_tap_is_not_tapped_again(self):
        center = FakeCenter([[row("Messages, Sam, hi")]], tap_ok=False)
        result = self.wait(center)
        self.assertEqual((result["status"], len(center.tapped)), ("completion_not_confirmed", 1))

    def test_an_app_that_is_not_allowed_is_not_waited_for_and_the_fields_are_checked(self):
        context = ctx(waitNotification={"app": "com.apple.MobileSMS", "match": None, "timeoutMinutes": 1, "tap": True})
        context.runtime = SimpleNamespace(allows_app=lambda run_id, bundle: False)
        self.assertEqual(phone_steps.notification_hook(context, Driver(), None).result["status"],
                         "completion_not_confirmed")
        for bad in ({"timeoutMinutes": 0}, {"timeoutMinutes": 241}, {"tap": "yes"}, {"x": 1}):
            with self.assertRaises(ValueError):
                phone_steps.validate_wait_notification(bad, None)


class GraphRunTests(GraphBase):
    def setUp(self):
        super().setUp()
        self.phone.script = self.script

    def script(self, goal, fields):
        extras = fields.get("extras") or {}
        if "openUrl" in extras:
            return "completed", {"data": {"url": extras["openUrl"]["url"], "landedApp": "com.apple.mobilesafari",
                                          "screen": "Hours"}}, 0.0
        if "waitNotification" in extras:
            if extras["waitNotification"]["match"] == "never":
                return "completed", {"data": {"timeout": True}}, 0.0
            return "completed", {"data": {"title": "Sam", "body": "hi", "app": "Messages", "at": None}}, 0.0
        return "completed", {"answer": f"Did: {goal}"}, 0.0

    def test_open_a_link_then_wait_for_the_push_then_tap_it(self):
        workflow = self.save_graph(
            [node("link", "phone.open_url", {"url": "https://example.com/{= 'x' }"}),
             node("push", "phone.notification", {"match": "hi", "timeoutMinutes": 2}),
             node("after", "output", {"value": "{= $input.title }"})],
            [edge("e1", "start", "link"), edge("e2", "link", "push"), edge("e3", "push", "after")])
        self.workflows.start(workflow["id"], "k1")
        record = self.wait(workflow)
        self.assertEqual(record["status"], "completed", record)
        self.assertEqual(record["nodes"]["link"]["output"]["landedApp"], "com.apple.mobilesafari")
        self.assertEqual(record["nodes"]["push"]["output"]["title"], "Sam")
        self.assertEqual(record["result"], "Sam")
        for call in self.phone.calls:                       # no model and no retry: each step is one task
            self.assertIn("openUrl" if "link" in call.goal.lower() else "waitNotification", call.extras)

    def test_no_notification_in_time_takes_the_timeout_path(self):
        workflow = self.save_graph(
            [node("push", "phone.notification", {"match": "never", "timeoutMinutes": 1}),
             node("late", "output", {"value": "late"}), node("got", "transform", {"fields": {"x": "1"}})],
            [edge("e1", "start", "push"), edge("e2", "push", "got"), edge("e3", "push", "late", "timeout")])
        self.workflows.start(workflow["id"], "k1")
        record = self.wait(workflow)
        self.assertEqual((record["status"], record["result"]), ("completed", "late"))
        self.assertNotEqual(record["nodes"]["got"]["state"], "done")

    def test_a_refused_link_fails_the_step_and_is_not_retried(self):
        self.phone.script = lambda goal, fields: ("completed", {"reason": "nobody approved"}, 0.0)
        workflow = self.save_graph([node("link", "phone.open_url", {"url": "sms:+15555550123"})],
                                   [edge("e1", "start", "link")])
        self.workflows.start(workflow["id"], "k1")
        record = self.wait(workflow)
        self.assertEqual(record["nodes"]["link"]["state"], "failed")
        self.assertEqual(len(self.phone.calls), 1)


if __name__ == "__main__":
    unittest.main()

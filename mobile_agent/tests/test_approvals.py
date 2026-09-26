"""Ask before acting: consequential actions pause for the user's answer. Offline."""

from dataclasses import replace
import threading
import time
import unittest

from mobile_agent.agent import Agent
from mobile_agent.demo import DemoDriver, DemoHelper, DemoModel
from mobile_agent.server import Run
from mobile_agent.task_policy import ActionSupport, RiskTier, needs_approval


class RiskyModel(DemoModel):
    """The demo flow, with every tap scored as a likely side effect."""

    def decide(self, snapshot, goal, history, **kwargs):
        decision = super().decide(snapshot, goal, history, **kwargs)
        return replace(decision, side_effect_risk=.95) if decision.operation == "TAP" else decision


class PolicyTests(unittest.TestCase):
    def test_commit_controls_and_side_effect_submits_ask(self):
        ask = lambda op, label, tier=RiskTier.NAVIGATION.value, risk=0.: needs_approval(
            op, label, risk_tier=tier, side_effect_risk=risk)
        for label in ("Send", "Buy now", "Place Order", "Delete Message", "Post", "Pay with Apple Pay", "Follow"):
            self.assertTrue(ask("TAP", label), label)
        self.assertTrue(ask("SUBMIT", "Message", RiskTier.SIDE_EFFECT.value))
        self.assertTrue(ask("TAP", "Continue", risk=.8))

    def test_navigation_typing_and_scrolling_never_ask(self):
        ask = lambda op, label, tier=RiskTier.NAVIGATION.value, risk=0.: needs_approval(
            op, label, risk_tier=tier, side_effect_risk=risk)
        for label in ("General", "About", "Done", "Back", "Wi-Fi", "Search"):
            self.assertFalse(ask("TAP", label), label)
        self.assertFalse(ask("SUBMIT", "Address", RiskTier.TYPE.value))
        self.assertFalse(ask("TYPE", "Send a message", RiskTier.SIDE_EFFECT.value, .99))
        self.assertFalse(ask("SWIPE_UP", "Send", risk=.99))
        self.assertFalse(ask("TAP", "Continue", risk=.5))


class AgentApprovalTests(unittest.TestCase):
    def run_with(self, answer):
        driver, requests = DemoDriver(), []

        def approve(request):
            requests.append(request)
            return answer

        result = Agent(driver, RiskyModel(), DemoHelper(), approve=approve).run("Search coffee", execute=True)
        return result, driver, requests

    def test_a_declined_action_is_never_dispatched(self):
        result, driver, requests = self.run_with("denied")
        self.assertEqual(result["status"], "approval_denied")
        self.assertEqual(driver.actions, [])
        self.assertEqual(requests[0]["operation"], "TAP")
        self.assertEqual(requests[0]["label"], "Search")

    def test_no_answer_ends_the_task_without_acting(self):
        result, driver, _ = self.run_with("timeout")
        self.assertEqual(result["status"], "approval_timeout")
        self.assertEqual(driver.actions, [])

    def test_an_approved_action_runs(self):
        result, driver, requests = self.run_with("approved")
        self.assertEqual(driver.actions[0], "TAP")
        self.assertEqual(len(requests), 1)

    def test_stopping_while_waiting_stops_the_task(self):
        result, driver, _ = self.run_with("stopped")
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(driver.actions, [])

    def test_waiting_for_the_user_does_not_spend_the_run_deadline(self):
        driver = DemoDriver()

        def slow(_request):
            time.sleep(1.2)
            return "approved"

        result = Agent(driver, RiskyModel(), DemoHelper(), approve=slow, max_seconds=1).run(
            "Search coffee", execute=True)
        self.assertNotEqual(result["status"], "timeout")
        self.assertEqual(driver.actions[0], "TAP")

    def test_without_a_handler_nothing_asks(self):
        driver = DemoDriver()
        Agent(driver, RiskyModel(), DemoHelper()).run("Search coffee", execute=True)
        self.assertEqual(driver.actions[0], "TAP")


class SureSendModel(DemoModel):
    """The demo flow, where Jev is fairly sure of every tap but the side-effect floor gates it to WAIT."""

    def decide(self, snapshot, goal, history, **kwargs):
        decision = super().decide(snapshot, goal, history, **kwargs)
        if decision.operation != "TAP":
            return decision
        return replace(decision, operation="WAIT", target=None, confidence=.7, demoted_from="TAP",
                       risk_tier=RiskTier.SIDE_EFFECT.value, side_effect_risk=.95,
                       approvable_target=decision.target)


class UnsureCheckModel(RiskyModel):
    """The action checker cannot establish any action's effect."""

    def verify_action(self, *args, **kwargs):
        return ActionSupport.UNCLEAR


class HesitantActionTests(unittest.TestCase):
    """A step the model or its checker hesitates on goes to the user, or through with bypass."""

    def run_with(self, model, approve=None, bypass=False):
        driver, requests, events = DemoDriver(), [], []

        def handler(request):
            requests.append(request)
            return approve

        result = Agent(driver, model, DemoHelper(), approve=handler if approve else None, bypass=bypass,
                       emit=lambda event: events.append(event["event"])).run("Search coffee", execute=True)
        return result, driver, requests, events

    def test_a_fairly_sure_commit_is_put_to_the_user_instead_of_waiting(self):
        _, driver, requests, events = self.run_with(SureSendModel(), approve="approved")
        self.assertEqual(driver.actions[0], "TAP")
        self.assertEqual(requests[0]["operation"], "TAP")
        self.assertIn("demotion_lifted", events)

    def test_declining_it_ends_the_task_without_acting(self):
        result, driver, _, _ = self.run_with(SureSendModel(), approve="denied")
        self.assertEqual((result["status"], driver.actions), ("approval_denied", []))

    def test_with_nobody_to_ask_and_no_bypass_it_still_waits(self):
        _, driver, _, events = self.run_with(SureSendModel())
        self.assertEqual(driver.actions, [])
        self.assertNotIn("demotion_lifted", events)

    def test_bypass_takes_it_without_asking(self):
        _, driver, requests, events = self.run_with(SureSendModel(), bypass=True)
        self.assertEqual(driver.actions[0], "TAP")
        self.assertEqual(requests, [])
        self.assertIn("demotion_lifted", events)

    def test_an_unclear_check_is_put_to_the_user(self):
        _, driver, requests, events = self.run_with(UnsureCheckModel(), approve="approved")
        self.assertEqual(driver.actions[0], "TAP")
        self.assertIn("unclear_put_to_user", events)
        self.assertEqual(requests[0]["operation"], "TAP")

    def test_bypass_lets_an_unclear_check_through(self):
        _, driver, requests, events = self.run_with(UnsureCheckModel(), bypass=True)
        self.assertEqual(driver.actions[0], "TAP")
        self.assertIn("unclear_bypassed", events)
        self.assertEqual(requests, [])

    def test_without_either_an_unclear_check_still_stops_the_task(self):
        result, driver, _, _ = self.run_with(UnsureCheckModel())
        self.assertEqual((result["status"], driver.actions), ("needs_clarification", []))


class RunApprovalTests(unittest.TestCase):
    def run_obj(self):
        return Run({"id": "a", "name": "Messages", "bundleId": "com.apple.MobileSMS"}, "Text Alex hi", "live")

    def ask_in_background(self, run, timeout=5):
        answers = []
        thread = threading.Thread(target=lambda: answers.append(run.request_approval(
            {"step": 3, "operation": "TYPE_SUBMIT", "label": "Message", "text": "hi"}, timeout=timeout)))
        thread.start()
        for _ in range(100):
            if run.public()["approval"]:
                break
            time.sleep(.01)
        return thread, answers

    def test_the_pending_request_is_visible_and_answered_once(self):
        run = self.run_obj()
        thread, answers = self.ask_in_background(run)
        pending = run.public()["approval"]
        self.assertEqual((pending["label"], pending["text"], pending["app"]), ("Message", "hi", "Messages"))
        self.assertFalse(run.answer_approval("wrong", True))
        self.assertTrue(run.answer_approval(pending["id"], True))
        self.assertFalse(run.answer_approval(pending["id"], False))
        thread.join(2)
        self.assertEqual(answers, ["approved"])
        self.assertIsNone(run.public()["approval"])

    def test_the_text_to_send_never_enters_the_event_log(self):
        run = self.run_obj()
        thread, _ = self.ask_in_background(run)
        run.answer_approval(run.public()["approval"]["id"], False)
        thread.join(2)
        events = [e for e in run.events if e["event"].startswith("approval_")]
        self.assertEqual([e["event"] for e in events], ["approval_requested", "approval_resolved"])
        self.assertEqual(events[1]["decision"], "denied")
        self.assertTrue(events[0]["text_present"])
        self.assertNotIn("hi", str([{k: v for k, v in e.items() if k not in {"label", "event"}} for e in events]))

    def test_timeout_and_stop(self):
        run = self.run_obj()
        self.assertEqual(run.request_approval({"operation": "TAP", "label": "Send"}, timeout=.05), "timeout")
        thread, answers = self.ask_in_background(run)
        run.stop.set()
        with run.condition:
            run.condition.notify_all()
        thread.join(3)
        self.assertEqual(answers, ["stopped"])


if __name__ == "__main__":
    unittest.main()


class ApprovalRouteTests(unittest.TestCase):
    def setUp(self):
        import http.client, json, os, tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from mobile_agent.server import BoundedServer, Runtime, make_handler
        self.http, self.json = http.client, json
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MOBSTER_ASK_BEFORE_ACTING", None)
        os.environ.pop("MOBSTER_BYPASS_CHECKS", None)
        self.env_file = os.path.join(tempfile.mkdtemp(), "agent.env")
        runtime = object.__new__(Runtime)
        runtime.config, runtime.setup = SimpleNamespace(port=8765, env_file=self.env_file), None
        runtime.runs, runtime.lock = {}, threading.Lock()
        self.run = RunApprovalTests.run_obj(self)
        runtime.runs[self.run.id] = self.run
        self.server = BoundedServer(("127.0.0.1", 0), make_handler(runtime))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None):
        connection = self.http.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        payload = self.json.dumps(body) if body is not None else None
        connection.request(method, path, payload, {"Host": "127.0.0.1:8765", "Content-Type": "application/json"})
        response = connection.getresponse()
        data = self.json.loads(response.read())
        connection.close()
        return response.status, data

    def test_ask_before_acting_defaults_on_and_persists_when_turned_off(self):
        self.assertEqual(self.call("GET", "/api/settings"), (200, {"askBeforeActing": True, "bypassChecks": False}))
        self.assertEqual(self.call("POST", "/api/settings", {"askBeforeActing": False}),
                         (200, {"askBeforeActing": False, "bypassChecks": False}))
        with open(self.env_file) as stream:
            self.assertIn("MOBSTER_ASK_BEFORE_ACTING=0", stream.read())
        self.assertEqual(self.call("POST", "/api/settings", {"askBeforeActing": "no"})[0], 400)
        self.assertEqual(self.call("POST", "/api/settings", {"other": True})[0], 400)

    def test_bypass_defaults_off_and_persists_when_turned_on(self):
        self.assertEqual(self.call("POST", "/api/settings", {"bypassChecks": True}),
                         (200, {"askBeforeActing": True, "bypassChecks": True}))
        with open(self.env_file) as stream:
            self.assertIn("MOBSTER_BYPASS_CHECKS=1", stream.read())
        self.assertEqual(self.call("POST", "/api/settings", {"bypassChecks": 1})[0], 400)

    def test_answering_a_pending_request(self):
        thread, answers = RunApprovalTests.ask_in_background(self, self.run)
        pending = self.run.public()["approval"]
        path = f"/api/runs/{self.run.id}/approval"
        self.assertEqual(self.call("POST", path, {"id": pending["id"]})[0], 400)
        status, body = self.call("POST", path, {"id": pending["id"], "approve": True})
        self.assertEqual(status, 200)
        thread.join(2)
        self.assertEqual(answers, ["approved"])
        self.assertEqual(self.call("POST", path, {"id": pending["id"], "approve": True})[0], 409)

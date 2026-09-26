"""Multi-app switching: bundle validation, driver launch, agent allowlist, prompt, server."""

import io
import json
import tempfile
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from mobile_agent.agent import Agent
from mobile_agent.api_errors import APIError
from mobile_agent.demo import DemoDriver
from mobile_agent.drivers import WDA
from mobile_agent.models import Decision
from mobile_agent.server import Run, Runtime, make_handler
from mobile_agent.state import Element, Snapshot, from_wda, validate_bundle_id
from mobile_agent.task_policy import RiskTier, StopGate, action_risk_tier

SETTINGS = "com.apple.Preferences"
NOTES = "com.apple.mobilenotes"
XML = ('<XCUIElementTypeApplication width="400" height="800">'
       '<XCUIElementTypeButton label="Search" x="1" y="1" width="100" height="40"/>'
       '</XCUIElementTypeApplication>')


class BundleValidationTests(unittest.TestCase):
    def test_valid_bundles_accepted(self):
        for bundle in ("com.apple.Preferences", "a.b", "com.x.y2-3_z",
                       "x." + "y" * 250 + ".z"):
            self.assertEqual(validate_bundle_id(bundle), bundle)

    def test_invalid_bundles_rejected(self):
        for bundle in (None, "", "nodots", ".leading", "trailing.", "has space.x",
                       "bad!char.x", "x", 123, ["com.a.b"], "a." + "b" * 300):
            with self.subTest(bundle=bundle), self.assertRaises(ValueError):
                validate_bundle_id(bundle)

    def test_launch_decision_requires_a_bundle(self):
        with self.assertRaises(ValueError):
            Decision("LAUNCH_APP", None, 1, 0, 0, "test", 0, {}, StopGate.CONTINUE)
        decision = Decision("LAUNCH_APP", NOTES, 1, 0, 0, "test", 0, {}, StopGate.CONTINUE)
        self.assertEqual(decision.target, NOTES)

    def test_launch_is_navigation_tier(self):
        self.assertIs(action_risk_tier("LAUNCH_APP", NOTES, "Copy to Notes"), RiskTier.NAVIGATION)


class WDALaunchTests(unittest.TestCase):
    def test_launch_activates_the_bundle(self):
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        self.assertEqual(driver.execute("LAUNCH_APP", NOTES, from_wda(XML)), {})
        driver.call.assert_called_once_with("POST", "/wda/apps/activate", {"bundleId": NOTES}, ANY)

    def test_launch_rejects_malformed_bundles_before_any_request(self):
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        with self.assertRaises(ValueError):
            driver.execute("LAUNCH_APP", "not-a-bundle", from_wda(XML))
        driver.call.assert_not_called()


class AgentLaunchTests(unittest.TestCase):
    def test_allowlist_validated_at_construction(self):
        for bad in (["nodots"], [], ["a.b"] * 33, "com.a.b", [None], [123]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Agent(DemoDriver(), Mock(), allowed_bundles=bad)
        agent = Agent(DemoDriver(), Mock(), allowed_bundles=["b.app", "a.app"])
        self.assertEqual(agent.allowed_bundles, frozenset({"a.app", "b.app"}))
        self.assertIsNone(Agent(DemoDriver(), Mock()).allowed_bundles)

    def launch_run(self, operation, target, allowed_bundles, observe=None):
        state = observe or Snapshot(
            [Element("stable", "Query", "RCTTextField", (.1, .1, .5, .1), True, "stable", "", ("TAP", "TYPE"))],
            "Query", 400, 800, "synthetic_fixture", revision="session:1", bundle_id="test.app")
        driver = DemoDriver()
        driver.observe = Mock(return_value=state)
        model = Mock()
        model.decide.side_effect = [
            Decision(operation, target, 1, 0, 0, "test", 0, {}, StopGate.CONTINUE),
            Decision("DONE", None, 1, 1, 0, "test", 0, {}, StopGate.CONTINUE),
            Decision("DONE", None, 1, 1, 0, "test", 0, {}, StopGate.CONTINUE)]
        result = Agent(driver, model, allowed_bundles=allowed_bundles).run("Use Notes", execute=True)
        return result, driver, model

    def test_launch_rejected_without_an_allowlist_or_membership(self):
        for allowed in (None, frozenset({"other.app"})):
            result, driver, _ = self.launch_run("LAUNCH_APP", NOTES, allowed)
            self.assertEqual(result["status"], "invalid_action")
            self.assertEqual(driver.actions, [])

    def test_launch_to_current_app_skips_with_a_hint(self):
        result, driver, model = self.launch_run("LAUNCH_APP", "test.app", {"test.app", NOTES})
        self.assertEqual(driver.actions, [])
        self.assertEqual(result["status"], "completed_unverified")
        self.assertIn("Already in the requested app", model.decide.call_args_list[1].kwargs["hint"])

    def test_allowed_launch_dispatches_with_bundle_target(self):
        result, driver, _ = self.launch_run("LAUNCH_APP", NOTES, {"test.app", NOTES})
        self.assertEqual(driver.actions, ["LAUNCH_APP"])
        self.assertEqual(result["attempted_actions"], 1)
        self.assertEqual(result["status"], "completed_unverified")

    def test_decide_receives_the_allowlist(self):
        _, _, model = self.launch_run("LAUNCH_APP", NOTES, {"test.app", NOTES})
        self.assertEqual(model.decide.call_args.kwargs["allowed_bundles"],
                         frozenset({"test.app", NOTES}))


class ParkedWorker:
    starts = 0

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        type(self).starts += 1

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


class ServerLaunchTests(unittest.TestCase):
    def setUp(self):
        ParkedWorker.starts = 0
        self.thread_patch = patch("mobile_agent.server.threading.Thread", ParkedWorker)
        self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtimes = []
        self.addCleanup(self.close_runtimes)

    def close_runtimes(self):
        for runtime in self.runtimes:
            runtime.close()

    def runtime(self, persisted=False):
        config = SimpleNamespace(wda_url=None, session=None,
                                 ocr=False, enable_live=False, port=8765,
                                 state_db=f"{self.temporary.name}/runs.sqlite3" if persisted else None)
        runtime = Runtime(config)
        self.runtimes.append(runtime)
        return runtime

    def test_create_defaults_to_single_app(self):
        run = self.runtime().create("settings", "Open About", "demo")
        self.assertIsNone(run.allowed_bundles)
        self.assertIsNone(run.public()["allowedBundles"])

    def test_create_stores_allowlist_with_current_app_included(self):
        run = self.runtime().create("settings", "Copy to Notes", "demo",
                                    allowed_bundles=[NOTES])
        self.assertEqual(run.allowed_bundles, frozenset({SETTINGS, NOTES}))
        self.assertEqual(run.public()["allowedBundles"], [SETTINGS, NOTES])

    def test_create_rejects_unknown_or_malformed_bundles(self):
        runtime = self.runtime()
        with self.assertRaises(ValueError):
            runtime.create("settings", "goal", "demo", allowed_bundles=["com.evil.app"])
        with self.assertRaises(ValueError):
            runtime.create("settings", "goal", "demo", allowed_bundles=["nodots"])
        with self.assertRaises(ValueError):
            runtime.create("settings", "goal", "demo", allowed_bundles="com.a.b")
        with self.assertRaises(ValueError):
            runtime.create("settings", "goal", "demo", allowed_bundles=["a.b"] * 32)

    def test_allowlist_is_part_of_request_identity(self):
        runtime = self.runtime()
        first, _ = runtime.create("settings", "Copy to Notes", "demo", "key-1",
                                  allowed_bundles=[NOTES])
        second, replayed = runtime.create("settings", "Copy to Notes", "demo", "key-1",
                                          allowed_bundles=[NOTES])
        self.assertTrue(replayed)
        self.assertIs(first, second)
        with self.assertRaises(APIError):
            runtime.create("settings", "Copy to Notes", "demo", "key-1",
                           allowed_bundles=["com.apple.mobilenotes", "com.apple.mobilecal"])

    def test_allowlist_survives_journal_recovery(self):
        config = SimpleNamespace(wda_url=None, session=None,
                                 ocr=False, enable_live=False, port=8765,
                                 state_db=f"{self.temporary.name}/runs.sqlite3")
        first = Runtime(config)
        self.runtimes.append(first)
        run = first.create("settings", "Copy to Notes", "demo", allowed_bundles=[NOTES])
        run_id = run.id
        first.close()
        second = Runtime(config)
        self.runtimes.append(second)
        restored = second.runs[run_id]
        self.assertEqual(restored.allowed_bundles, frozenset({SETTINGS, NOTES}))

    def test_post_accepts_allowed_bundles_field(self):
        runtime = Mock()
        run = Run({"id": "settings", "name": "Settings", "bundleId": SETTINGS},
                  "Copy to Notes", "demo")
        runtime.create.return_value = run
        handler = object.__new__(make_handler(runtime))
        raw = json.dumps({"appId": "settings", "goal": "Copy to Notes",
                          "allowedBundles": [NOTES]}).encode()
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(raw))
        handler.path = "/api/runs"
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.connection = Mock()
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        handler.do_POST()
        self.assertEqual(statuses[-1], 201)
        self.assertEqual(runtime.create.call_args.kwargs["allowed_bundles"], [NOTES])

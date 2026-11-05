import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from mobile_agent.api_errors import APIError
from mobile_agent.drivers import WDA
from mobile_agent.frontier import FrontierAgent
from mobile_agent.journal import Lease
from mobile_agent.server import Runtime, make_handler
from mobile_agent.state import from_wda, validate_bundle_id
from mobile_agent.task_policy import RiskTier, action_risk_tier
from mobile_agent.tests.seam_support import request
from mobile_agent.tests.test_frontier_lock_loop import REMINDERS, Model, Phone, agent

SETTINGS = "com.apple.Preferences"
NOTES = "com.apple.mobilenotes"
READY_PHONE = {"device": True, "ready": True, "health": None, "can_act": True, "driver": "wda", "screen_reading": False}
XML = ('<XCUIElementTypeApplication width="400" height="800">'
       '<XCUIElementTypeButton label="Search" x="1" y="1" width="100" height="40"/>'
       '</XCUIElementTypeApplication>')


class BundleValidationTests(unittest.TestCase):
    def test_valid_bundles_are_accepted_and_invalid_ones_refused(self):
        for bundle in ("com.apple.Preferences", "a.b", "com.x.y2-3_z",
                       "x." + "y" * 250 + ".z"):
            self.assertEqual(validate_bundle_id(bundle), bundle)
        for bundle in (None, "", "nodots", ".leading", "trailing.", "has space.x",
                       "bad!char.x", "x", 123, ["com.a.b"], "a." + "b" * 300):
            with self.subTest(bundle=bundle), self.assertRaises(ValueError):
                validate_bundle_id(bundle)

    def test_launch_is_navigation_tier(self):
        self.assertIs(action_risk_tier("LAUNCH_APP", NOTES, "Copy to Notes"), RiskTier.NAVIGATION)


class WDALaunchTests(unittest.TestCase):
    def test_launch_activates_a_valid_bundle_and_sends_nothing_for_a_malformed_one(self):
        driver = WDA("http://localhost:8100", "test")
        driver.call = Mock()
        self.assertEqual(driver.execute("LAUNCH_APP", NOTES, from_wda(XML)), {})
        driver.call.assert_called_once_with("POST", "/wda/apps/activate", {"bundleId": NOTES}, ANY)
        driver.call.reset_mock()
        with self.assertRaises(ValueError):
            driver.execute("LAUNCH_APP", "not-a-bundle", from_wda(XML))
        driver.call.assert_not_called()


class AgentLaunchTests(unittest.TestCase):
    def test_an_unlisted_app_is_refused_and_a_listed_one_opens(self):
        phone = Phone(locked=False)
        model = Model([("LAUNCH_APP", NOTES, None)], [("LAUNCH_APP", REMINDERS, None)], [("DONE", None, None)],
                      [("DONE", None, None)])
        frontier, _ = agent(phone, model)
        result = frontier.run("Add a reminder to buy milk.")
        self.assertEqual(phone.actions, [("LAUNCH_APP", REMINDERS, None)])
        self.assertIn("LAUNCH_APP needs one of the listed apps.", model.prompts[1])
        self.assertEqual(result["status"], "completed")

    def test_with_no_apps_no_launch_reaches_the_phone(self):
        phone = Phone(locked=False)
        model = Model([("LAUNCH_APP", REMINDERS, None)], [("DONE", None, None)], [("DONE", None, None)])
        FrontierAgent(phone, model, apps={}, screenshots=False, settle_seconds=0, skills=()).run(
            "Add a reminder to buy milk.")
        self.assertEqual(phone.actions, [])


REAL_THREAD = threading.Thread


class ParkedWorker:
    """Stands in for threading.Thread. It parks only a task's worker (Runtime.work): the listener queue's dispatcher
    and every other thread are real, so close() can finish them and release the journal's lease."""
    starts = 0

    def __new__(cls, *args, **kwargs):
        if getattr(kwargs.get("target"), "__name__", "") != "work":
            return REAL_THREAD(*args, **kwargs)
        return super().__new__(cls)

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
        env = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
        env.start()
        self.addCleanup(env.stop)
        lease = patch("mobile_agent.journal.Lease.device",
                      side_effect=lambda _: Lease(Path(self.temporary.name) / "device.lock"))
        lease.start()
        self.addCleanup(lease.stop)
        self.runtimes = []
        self.addCleanup(self.close_runtimes)

    def close_runtimes(self):
        for runtime in self.runtimes:
            runtime.close()

    def runtime(self, persisted=False):
        config = SimpleNamespace(wda_url="http://127.0.0.1:8203", session=None,
                                 ocr=False, enable_live=True, port=8765, env_file=f"{self.temporary.name}/agent.env",
                                 state_db=f"{self.temporary.name}/runs.sqlite3" if persisted else None)
        return self.start(config)

    def start(self, config):
        with patch("mobile_agent.server.WdaVideo"), patch("mobile_agent.server.ManualControl"):
            runtime = Runtime(config)
        runtime.target_status = Mock(return_value=dict(READY_PHONE))
        self.runtimes.append(runtime)
        return runtime

    def test_create_refuses_any_mode_but_live(self):
        runtime = self.runtime()
        for mode in ("demo", "", None, 1):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                runtime.create("settings", "Open About", mode)
        self.assertEqual(runtime.runs, {})

    def test_create_defaults_to_single_app(self):
        self.assertIsNone(self.runtime().create("settings", "Open About", "live").public()["allowedBundles"])

    def test_create_stores_allowlist_with_current_app_included(self):
        run = self.runtime().create("settings", "Copy to Notes", "live",
                                    allowed_bundles=[NOTES])
        self.assertEqual(run.allowed_bundles, frozenset({SETTINGS, NOTES}))
        self.assertEqual(run.public()["allowedBundles"], [SETTINGS, NOTES])

    def test_create_rejects_unknown_or_malformed_bundles(self):
        runtime = self.runtime()
        with self.assertRaises(ValueError):
            runtime.create("settings", "goal", "live", allowed_bundles=["com.evil.app"])
        with self.assertRaises(ValueError):
            runtime.create("settings", "goal", "live", allowed_bundles=["nodots"])
        with self.assertRaises(ValueError):
            runtime.create("settings", "goal", "live", allowed_bundles="com.a.b")
        with self.assertRaises(ValueError):
            runtime.create("settings", "goal", "live", allowed_bundles=["a.b"] * 32)

    def test_allowlist_is_part_of_request_identity(self):
        runtime = self.runtime()
        first, _ = runtime.create("settings", "Copy to Notes", "live", "key-1",
                                  allowed_bundles=[NOTES])
        second, replayed = runtime.create("settings", "Copy to Notes", "live", "key-1",
                                          allowed_bundles=[NOTES])
        self.assertTrue(replayed)
        self.assertIs(first, second)
        with self.assertRaises(APIError):
            runtime.create("settings", "Copy to Notes", "live", "key-1",
                           allowed_bundles=["com.apple.mobilenotes", "com.apple.mobilecal"])

    def test_allowlist_survives_journal_recovery(self):
        config = SimpleNamespace(wda_url="http://127.0.0.1:8203", session=None,
                                 ocr=False, enable_live=True, port=8765, env_file=f"{self.temporary.name}/agent.env",
                                 state_db=f"{self.temporary.name}/runs.sqlite3")
        first = self.start(config)
        run = first.create("settings", "Copy to Notes", "live", allowed_bundles=[NOTES])
        run_id = run.id
        first.close()
        second = self.start(config)
        restored = second.runs[run_id]
        self.assertEqual(restored.allowed_bundles, frozenset({SETTINGS, NOTES}))

    def test_post_accepts_allowed_bundles_field(self):
        runtime = self.runtime()
        status, data, _ = request(make_handler(runtime), "POST", "/api/runs",
                                  {"appId": "settings", "goal": "Copy to Notes", "allowedBundles": [NOTES]})
        self.assertEqual(status, 201)
        self.assertEqual(runtime.runs[data["run"]["id"]].allowed_bundles, frozenset({SETTINGS, NOTES}))

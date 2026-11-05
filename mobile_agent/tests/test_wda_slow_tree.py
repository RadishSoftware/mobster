import base64
import types
import unittest
from unittest.mock import patch

from mobile_agent import harness_api, lockscreen
from mobile_agent.drivers import WDA
from mobile_agent.mcp_server.direct import DeviceRun
from mobile_agent.state import pixel_read
from mobile_agent.tests.fakes import Clock
from mobile_agent.transport import TransportError
from mobile_agent.verify import runner
from mobile_agent.wda.observe import WDA_SLOW_TREE_SECONDS, WDA_TREE_BUDGET_SECONDS

TIKTOK = "com.zhiliaoapp.musically"
INSTAGRAM = "com.burbn.instagram"
SETTINGS = "com.apple.Preferences"
URL = "http://127.0.0.1:8190"
ROWS = [{"text": "For You", "confidence": .95, "rect": [.35, .06, .3, .03]},
        {"text": "Follow", "confidence": .9, "rect": [.8, .4, .15, .03]}]


def screen(bundle, label="Home"):
    return (f'<XCUIElementTypeApplication width="393" height="852" bundleId="{bundle}">'
            f'<XCUIElementTypeButton label="{label}" x="20" y="200" width="100" height="40"/>'
            "</XCUIElementTypeApplication>")


class Phone:
    def __init__(self, clock):
        self.clock, self.requests, self.front = clock, [], TIKTOK
        self.seconds = {"source": .2, "info": .2, "state": .007, "shot": .3, "other": .05}

    def request(self, method, path, body=None, timeout=10):
        path = path.removeprefix("/session/test")
        self.requests.append((method, path, timeout))
        if path.startswith("/source"):
            kind, value = "source", screen(self.front)
        elif path == "/wda/activeAppInfo":
            kind, value = "info", {"bundleId": self.front, "pid": 41}
        elif path == "/wda/apps/state":
            kind, value = "state", 4 if body["bundleId"] == self.front else 1
        elif path == "/screenshot":
            kind, value = "shot", base64.b64encode(b"not really a png").decode()
        else:
            kind, value = "other", None
            if path == "/wda/apps/activate":
                self.front = body["bundleId"]
        took = self.seconds[kind]
        if took > timeout:
            self.clock.now += timeout
            raise TransportError("WDA request timed out")
        self.clock.now += took
        return {"value": value}

    def asked(self, path):
        return [request for request in self.requests if request[1].startswith(path)]


class SlowTreeTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for target, replacement in (("mobile_agent.drivers.time.monotonic", self.clock),
                                    ("mobile_agent.transport.time.monotonic", self.clock),
                                    ("mobile_agent.drivers.time.sleep", self.clock.sleep)):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.phone = Phone(self.clock)
        self.driver = WDA(URL, "test")
        self.addCleanup(self.driver.close)
        self.driver.http.request = self.phone.request
        self.driver.ocr = lambda data: [dict(row) for row in ROWS]
        WDA._screen_sizes[URL] = (393, 852)
        self.addCleanup(WDA._screen_sizes.pop, URL, None)
        self.addCleanup(WDA._slow_trees.pop, URL, None)

    def spent(self, work):
        started = self.clock.now
        result = work()
        return result, self.clock.now - started


class LaunchTests(SlowTreeTestCase):
    def test_a_launch_is_confirmed_by_apps_state_while_active_app_info_hangs(self):
        self.phone.seconds.update(info=49.4, source=29.8)
        self.driver.mark_tree_slow(TIKTOK)
        (snapshot, info), spent = self.spent(lambda: self.driver.launch(TIKTOK))
        self.assertEqual((snapshot.bundle_id, snapshot.source), (TIKTOK, "wda_pixels"))
        self.assertLess(spent, 3)
        front, spent = self.spent(self.driver.active_app)
        self.assertEqual(front, TIKTOK)
        self.assertLess(spent, .05)
        self.assertEqual(self.phone.asked("/wda/activeAppInfo"), [])
        self.assertEqual(self.phone.asked("/source"), [])

    def test_the_first_launch_waits_one_budget_for_the_tree_and_the_next_read_does_not(self):
        self.phone.seconds["source"] = 29.8
        self.driver.call("POST", "/wda/apps/activate", {"bundleId": TIKTOK}, 10)
        first, spent = self.spent(lambda: self.driver.observe(timeout=10))
        self.assertEqual((first.bundle_id, first.source), (TIKTOK, "wda_pixels"))
        self.assertIn("For You", [element.label for element in first.elements])
        self.assertAlmostEqual(spent, WDA_TREE_BUDGET_SECONDS + .007 + .3, delta=.2)
        self.assertEqual([request[2] for request in self.phone.asked("/source")], [WDA_TREE_BUDGET_SECONDS])
        self.assertTrue(self.driver.tree_is_slow(TIKTOK))
        second, spent = self.spent(lambda: self.driver.observe(timeout=10))
        self.assertEqual(second.source, "wda_pixels")
        self.assertLess(spent, 1)
        self.assertEqual(len(self.phone.asked("/source")), 1)


class ReadTests(SlowTreeTestCase):
    def test_a_normal_app_still_uses_the_tree(self):
        self.phone.front = INSTAGRAM
        snapshot, spent = self.spent(lambda: self.driver.observe(timeout=10))
        self.assertEqual((snapshot.source, snapshot.bundle_id), ("wda", INSTAGRAM))
        self.assertLess(spent, .5)
        self.assertEqual(self.phone.asked("/screenshot"), [])
        self.assertEqual(self.phone.asked("/wda/apps/state"), [])
        self.assertFalse(self.driver.tree_is_slow(INSTAGRAM))

    def test_a_caller_deadline_shorter_than_the_budget_is_not_a_slow_tree(self):
        self.phone.seconds["source"] = 29.8
        with self.assertRaises(TransportError):
            self.driver.observe(timeout=2)
        self.assertFalse(self.driver.tree_is_slow(TIKTOK))
        self.assertEqual(self.phone.asked("/screenshot"), [])

    def test_the_engine_alone_has_no_pixel_reader_and_keeps_the_trees_error(self):
        self.phone.seconds["source"] = 29.8
        self.driver.call("POST", "/wda/apps/activate", {"bundleId": TIKTOK}, 10)
        with patch.object(harness_api.hooks(), "pixel_screen", harness_api.EngineAlone().pixel_screen):
            with self.assertRaises(TransportError) as raised:
                self.spent(lambda: self.driver.observe(timeout=10))
        self.assertIn("budget", str(raised.exception))
        self.assertEqual(self.phone.asked("/screenshot"), [])
        self.assertAlmostEqual(self.clock.now - 1000.0, WDA_TREE_BUDGET_SECONDS, delta=.5)

    def test_a_screen_no_one_can_read_is_an_error_and_the_app_stays_remembered_without_another_source_read(self):
        self.phone.seconds["source"] = 29.8
        self.driver.ocr = lambda data: None
        self.driver.call("POST", "/wda/apps/activate", {"bundleId": TIKTOK}, 10)
        with self.assertRaises(TransportError) as raised:
            self.driver.observe(timeout=10)
        self.assertIn("screenshot", str(raised.exception))
        self.assertTrue(self.driver.tree_is_slow(TIKTOK))
        sources = len(self.phone.asked("/source"))
        with self.assertRaises(TransportError):
            self.driver.observe(timeout=10)
        self.assertEqual(len(self.phone.asked("/source")), sources)

    def test_an_app_that_is_not_in_front_is_read_from_its_tree(self):
        self.driver.mark_tree_slow(TIKTOK)
        self.driver._last_bundle = TIKTOK
        self.phone.front = SETTINGS
        snapshot = self.driver.observe(timeout=10)
        self.assertEqual((snapshot.source, snapshot.bundle_id), ("wda", SETTINGS))
        self.assertEqual(self.phone.asked("/screenshot"), [])

    def test_the_tree_is_tried_again_when_the_memory_expires(self):
        self.driver.mark_tree_slow(TIKTOK)
        self.driver._last_bundle = TIKTOK
        self.clock.now += WDA_SLOW_TREE_SECONDS + 1
        self.assertFalse(self.driver.tree_is_slow(TIKTOK))
        self.assertEqual(self.driver.observe(timeout=10).source, "wda")

    def test_a_screen_read_from_pixels_is_a_pixel_read_in_points(self):
        self.driver.mark_tree_slow(TIKTOK)
        self.driver._last_bundle = TIKTOK
        snapshot = self.driver.observe(timeout=10)
        self.assertTrue(pixel_read(snapshot))
        self.assertEqual((snapshot.width, snapshot.height), (393, 852))
        again = self.driver.observe(timeout=10)
        self.assertEqual(again.fingerprint, snapshot.fingerprint)
        self.driver.ocr = lambda data: [{**ROWS[0], "text": "Following"}]
        self.assertNotEqual(self.driver.observe(timeout=10).fingerprint, snapshot.fingerprint)

    def test_the_hint_retry_still_rescues_a_read_that_fails_without_the_hint(self):
        self.driver._foreground_hint = SETTINGS
        reads = []
        original = self.phone.request

        def request(method, path, body=None, timeout=10):
            if path.startswith("/session/test/source"):
                reads.append(timeout)
                if len(reads) == 1:
                    self.clock.now += 8
                    raise TransportError("stale element")
            return original(method, path, body, timeout)
        self.driver.http.request = request
        self.phone.front = SETTINGS
        self.assertEqual(self.driver.observe(timeout=10).source, "wda")
        self.assertEqual(len(reads), 2)


class FrontAppTests(SlowTreeTestCase):
    def test_apps_state_says_whether_an_app_is_in_front(self):
        self.assertTrue(self.driver.in_front(TIKTOK))
        self.assertFalse(self.driver.in_front(INSTAGRAM))
        self.phone.seconds["state"] = 5
        self.assertIsNone(self.driver.in_front(TIKTOK, timeout=2))

    def test_a_front_app_that_is_unknown_is_never_waited_for_past_the_probe_bound(self):
        self.phone.seconds["info"] = 49.4
        with self.assertRaises(TransportError):
            self.spent(lambda: self.driver.active_app(timeout=30))
        self.assertEqual([request[2] for request in self.phone.asked("/wda/activeAppInfo")], [3.0])
        self.assertAlmostEqual(self.clock.now - 1000.0, 3.0, delta=.1)

    def test_an_app_that_is_not_in_front_does_not_stand_in_for_the_front_app(self):
        self.driver.mark_tree_slow(TIKTOK)
        self.driver._last_bundle = TIKTOK
        self.phone.front = SETTINGS
        self.assertEqual(self.driver.active_app(), SETTINGS)
        self.assertEqual(len(self.phone.asked("/wda/activeAppInfo")), 1)

    def test_the_lock_check_confirms_a_slow_app_without_active_app_info(self):
        self.phone.seconds["info"] = 49.4
        self.driver.mark_tree_slow(TIKTOK)
        self.driver._last_bundle = TIKTOK
        self.assertEqual(lockscreen.read_front(self.driver), TIKTOK)
        self.assertEqual(self.phone.asked("/wda/activeAppInfo"), [])
        self.driver.mark_tree_slow(INSTAGRAM)
        self.phone.seconds["info"] = .2
        self.phone.front = SETTINGS
        self.driver._last_bundle = INSTAGRAM
        self.assertEqual(lockscreen.read_front(self.driver, timeout=30), SETTINGS)
        self.assertEqual([request[2] for request in self.phone.asked("/wda/activeAppInfo")], [3.0])

    def test_the_dialog_check_does_not_ask_active_app_info_about_a_slow_app(self):
        from mobile_agent.frontier import FrontierAgent
        self.phone.seconds["info"] = 49.4
        self.driver.mark_tree_slow(TIKTOK)
        self.driver._last_bundle = TIKTOK
        snapshot = self.driver.observe(timeout=10)
        agent = types.SimpleNamespace(driver=self.driver)
        differs, spent = self.spent(lambda: FrontierAgent._front_app_differs(agent, snapshot))
        self.assertFalse(differs)
        self.assertLess(spent, .05)
        self.assertEqual(self.phone.asked("/wda/activeAppInfo"), [])

    def test_the_dialog_check_still_sees_springboard_over_a_normal_app(self):
        from mobile_agent.frontier import FrontierAgent
        self.phone.front = INSTAGRAM
        snapshot = self.driver.observe(timeout=10)
        self.phone.front = "com.apple.springboard"
        agent = types.SimpleNamespace(driver=self.driver)
        self.assertTrue(FrontierAgent._front_app_differs(agent, snapshot))

    def test_the_simulator_runner_never_waits_longer_than_the_probe_bound(self):
        timeouts = []

        def request_once(url, method, path, body=None, timeout=10):
            timeouts.append(timeout)
            return {"value": {"bundleId": TIKTOK, "pid": 7}}
        with patch.object(runner, "request_once", request_once):
            self.assertEqual(runner.front_app("http://127.0.0.1:1", timeout=15), (TIKTOK, 7))
            runner.front_app("http://127.0.0.1:1", timeout=1)
        self.assertEqual(timeouts, [3.0, 1])


class EvidenceReadTests(SlowTreeTestCase):
    def run_of_driver(self):
        run = DeviceRun.__new__(DeviceRun)
        run.driver = self.driver
        return run

    def test_a_slow_tree_is_read_once_within_the_budget_and_then_from_pixels(self):
        self.phone.seconds["source"] = 29.8
        self.driver.call("POST", "/wda/apps/activate", {"bundleId": TIKTOK}, 10)
        run = self.run_of_driver()
        tree, spent = self.spent(run.read_tree)
        self.assertEqual(tree.bundle_id, TIKTOK)
        self.assertIn("For You", [node.label for node in tree.nodes])
        self.assertTrue(all(node.path.startswith("/pixels/") for node in tree.nodes))
        self.assertLess(spent, WDA_TREE_BUDGET_SECONDS + 1.5)
        self.assertEqual([request[2] for request in self.phone.asked("/source")], [WDA_TREE_BUDGET_SECONDS])
        self.assertTrue(self.driver.tree_is_slow(TIKTOK))
        tree, spent = self.spent(run.read_tree)
        self.assertLess(spent, 1)
        self.assertEqual(len(self.phone.asked("/source")), 1)

    def test_a_normal_tree_is_the_evidence_read(self):
        self.phone.front = INSTAGRAM
        tree = self.spent(self.run_of_driver().read_tree)[0]
        self.assertEqual((tree.bundle_id, [node.label for node in tree.nodes if node.label]), (INSTAGRAM, ["Home"]))
        self.assertEqual(self.phone.asked("/screenshot"), [])

    def test_a_driver_without_pixel_reads_reads_the_tree_as_before(self):
        run = DeviceRun.__new__(DeviceRun)
        run.driver = types.SimpleNamespace(call=lambda method, path, body=None, timeout=None: screen(INSTAGRAM))
        self.assertEqual(run.read_tree().bundle_id, INSTAGRAM)


if __name__ == "__main__":
    unittest.main()

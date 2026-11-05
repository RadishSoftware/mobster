import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from mobile_agent import app_killer
from mobile_agent.drivers import WDA

BUNDLE = "com.example.feed"
APP_URL = "file:///private/var/containers/Bundle/Application/AAAA/Feed.app/"


class SimulatorKillerTests(unittest.TestCase):
    def test_simctl_terminate_closes_the_app_on_that_simulator(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        self.assertTrue(app_killer.simulator("SIM-1", runner=runner)(BUNDLE, 5))
        self.assertEqual(runner.call_args[0][0], ["xcrun", "simctl", "terminate", "SIM-1", BUNDLE])

    def test_a_failed_or_hung_simctl_is_not_a_close(self):
        self.assertFalse(app_killer.simulator("SIM-1", runner=Mock(return_value=SimpleNamespace(returncode=1)))(BUNDLE))
        hung = Mock(side_effect=subprocess.TimeoutExpired("xcrun", 5))
        self.assertFalse(app_killer.simulator("SIM-1", runner=hung)(BUNDLE))


class DeviceKillerTests(unittest.TestCase):
    def fake(self, apps, processes):
        calls = []

        def run(args, timeout):
            calls.append(args)
            if args[:3] == ["device", "info", "apps"]:
                return {"result": {"apps": apps}}
            if args[:3] == ["device", "info", "processes"]:
                return {"result": {"runningProcesses": processes}}
            return {"info": {"outcome": "success"}}
        return run, calls

    def test_only_the_apps_own_process_is_killed(self):
        run, calls = self.fake([{"bundleIdentifier": BUNDLE, "url": APP_URL}],
                               [{"executable": APP_URL + "Feed", "processIdentifier": 412},
                                {"executable": "file:///private/var/containers/Bundle/Application/BBBB/Mail.app/Mail",
                                 "processIdentifier": 77}])
        self.assertTrue(app_killer.device("PHONE-1", run_json=run)(BUNDLE, 10))
        self.assertEqual(calls[-1], ["device", "process", "terminate", "--device", "PHONE-1", "--pid", "412", "--kill"])
        self.assertFalse(any("77" in c for c in calls))

    def test_an_app_store_app_is_looked_up_among_all_apps(self):
        run, calls = self.fake([{"bundleIdentifier": BUNDLE, "url": APP_URL}],
                               [{"executable": APP_URL + "Feed", "processIdentifier": 412}])
        app_killer.device("PHONE-1", run_json=run)(BUNDLE, 10)
        self.assertIn("--include-all-apps", calls[0])
        self.assertEqual(calls[0][:4], ["device", "info", "apps", "--device"])

    def test_an_app_not_installed_or_not_running_is_not_killed(self):
        run, calls = self.fake([], [])
        self.assertFalse(app_killer.device("PHONE-1", run_json=run)(BUNDLE))
        run, calls = self.fake([{"bundleIdentifier": BUNDLE, "url": APP_URL}],
                               [{"executable": "file:///other/App.app/App", "processIdentifier": 5}])
        self.assertFalse(app_killer.device("PHONE-1", run_json=run)(BUNDLE))
        self.assertFalse(any(c[:3] == ["device", "process", "terminate"] for c in calls))

    def test_a_devicectl_failure_is_not_a_close(self):
        self.assertFalse(app_killer.device("PHONE-1", run_json=Mock(side_effect=RuntimeError("hang")))(BUNDLE))


class RelaunchFallbackTests(unittest.TestCase):
    def driver(self, killer):
        driver = WDA("http://localhost:8100", "test")
        driver.http = Mock()
        calls = []

        def call(method, path, body=None, timeout=10):
            calls.append(path)
            if path == "/wda/apps/terminate":
                raise TimeoutError("Operation deadline exceeded")
        driver.call = call
        driver.killer = killer
        return driver, calls

    def test_when_wda_cannot_close_the_app_the_killer_does_and_the_app_reopens(self):
        driver, calls = self.driver(Mock(return_value=True))
        self.assertTrue(driver.relaunch_app(BUNDLE))
        self.assertEqual(calls, ["/wda/apps/terminate", "/wda/apps/activate"])
        self.assertEqual(driver.last_relaunch, "out_of_band")

    def test_without_a_killer_or_when_it_fails_the_failure_stands(self):
        for killer in (None, Mock(return_value=False)):
            driver, calls = self.driver(killer)
            with self.assertRaises(TimeoutError):
                driver.relaunch_app(BUNDLE)
            self.assertEqual(calls, ["/wda/apps/terminate"])


if __name__ == "__main__":
    unittest.main()

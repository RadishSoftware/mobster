"""Each MCP client session leases its own simulator when it names no device."""

import contextlib
import tempfile
import unittest
from pathlib import Path

from mobile_agent.devices import record
from mobile_agent.mcp_server.protocol import Call
from mobile_agent.mcp_server.tools import ToolSet
from mobile_agent.sim import holders
from mobile_agent.tests.fakes import Clock
from mobile_agent.tests.test_mcp_devices import FakeDeviceRun
from mobile_agent.tests.test_mcp_tools import FakeAPI, fake_image
from mobile_agent.tests.test_sim_fakes import fake_manager
from mobile_agent.tests.test_sim_manager import second_process


class SimulatorRun(FakeDeviceRun):
    """A direct run that takes its lease from the real simulator manager (over fake simctl)."""

    def __init__(self, record, *, runs_dir, manager, acquire_timeout=None, **options):
        super().__init__(record, runs_dir=runs_dir, **options)
        self.manager, self._timeout = manager, acquire_timeout

    def prepare(self):
        self.lease = self.manager.acquire(timeout=self._timeout)
        self.target = self.lease.target
        super().prepare()


class SessionSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.first, self.simctl, *_ = self.stack.enter_context(fake_manager({"MOBSTER_MAX_SIMS": "2"}))
        self.clock = Clock()

    def session(self, client):
        manager = second_process(self.first)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        tools = ToolSet(runs_dir=Path(folder.name) / "runs", api=FakeAPI(), manager_factory=lambda progress: manager,
                        clock=self.clock, imager=fake_image, devices=lambda: [], device_run=SimulatorRun,
                        version="0.0.test")
        tools.client = {"name": client}
        self.addCleanup(tools.shutdown, "the test ended", 1.0)
        return tools

    @staticmethod
    def call(tools, tool, **arguments):
        return tools.call(tool, arguments, Call(1))

    def test_each_session_gets_its_own_simulator_and_keeps_it(self):
        a, b = self.session("Claude Code"), self.session("Codex")
        shot_a = self.call(a, "screen")
        self.assertFalse(shot_a.is_error, shot_a.text)
        self.assertFalse(self.call(b, "screen").is_error)
        self.assertEqual(len(self.simctl.simctl_calls("create")), 2)
        self.assertNotEqual(a._own_sim, b._own_sim)
        self.assertEqual(holders.read(self.first.data_dir, a._own_sim)["client"], "Claude Code")
        self.assertEqual(holders.read(self.first.data_dir, b._own_sim)["client"], "Codex")
        mine = a._own_sim
        self.call(a, "screen")
        self.assertEqual((a._own_sim, len(self.simctl.simctl_calls("create"))), (mine, 2))

    def test_a_third_session_hears_who_holds_them(self):
        a, b, c = self.session("Claude Code"), self.session("Codex"), self.session("Cursor")
        self.call(a, "screen"), self.call(b, "screen")
        refused = self.call(c, "screen")
        self.assertTrue(refused.is_error)
        self.assertIn("Every simulator is busy", refused.text)
        self.assertIn("Claude Code", refused.text)
        self.assertIn("Codex", refused.text)
        self.assertIn("idle for 30 minutes", refused.text)
        self.assertEqual(len(self.simctl.simctl_calls("create")), 2)

    def test_the_simulator_is_released_after_thirty_idle_minutes_and_another_session_takes_it(self):
        a, b, c = self.session("Claude Code"), self.session("Codex"), self.session("Cursor")
        self.call(a, "screen"), self.call(b, "screen")
        freed = a._own_sim
        self.clock.sleep(29 * 60)
        a.reap()
        self.assertEqual(a._own_sim, freed)
        self.clock.sleep(2 * 60)
        a.reap()
        self.assertIsNone(a._own_sim)
        self.assertIsNone(holders.read(self.first.data_dir, freed))
        self.assertFalse(self.call(c, "screen").is_error)
        self.assertEqual(c._own_sim, freed)

    def test_a_client_that_goes_away_frees_its_simulator(self):
        a, b, c = self.session("Claude Code"), self.session("Codex"), self.session("Cursor")
        self.call(a, "screen"), self.call(b, "screen")
        a.shutdown("the client disconnected", grace=1.0)
        self.assertFalse(self.call(c, "screen").is_error)

    def test_list_devices_marks_the_simulator_yours(self):
        a = self.session("Claude Code")
        self.call(a, "screen")
        a._devices_source = lambda: [record(
            id=a._own_sim, kind="simulator", name="Mobster · iPhone 17 Pro · iOS 26.4", udid=a._own_sim,
            model="iPhone 17 Pro", ios="26.4", state="ready", wda_url_value="http://127.0.0.1:8310")]
        listed = self.call(a, "list_devices")
        self.assertTrue(listed.structured["devices"][0]["yours"])
        self.assertIn(", yours", listed.text)

    def test_a_release_request_makes_the_holder_let_go(self):
        a = self.session("Claude Code")
        self.call(a, "screen")
        mine = a._own_sim
        holders.request_release(self.first.data_dir, mine)
        a.reap()
        self.assertIsNone(a._own_sim)

    def test_a_check_takes_back_the_simulator_the_session_leased(self):
        a = self.session("Claude Code")
        self.call(a, "screen")
        a._yield_session_simulator({})
        self.assertIsNone(a._own_sim)
        self.assertEqual(len(self.simctl.simctl_calls("create")), 1)
        self.call(a, "screen")
        self.assertEqual(len(self.simctl.simctl_calls("create")), 1)  # the same, booted simulator


if __name__ == "__main__":
    unittest.main()

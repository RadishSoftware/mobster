"""scripts/sim_smoke.py over the simulator fakes. The cleanup (review round 2): when a leg fails, the run still
deletes exactly the simulators it added, keeps the ones that were there before, and its last leg says so. The
second prepare (review round 3): its simulator is shut down before leg 15 boots two more. No Xcode, simulator
or network.

The public Mobster CLI copy leaves scripts/sim_smoke.py out, so these tests skip there."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mobile_agent.sim import SimulatorManager
from mobile_agent.sim.wda import build_dir
from mobile_agent.tests.test_sim_fakes import TOOLS, fake_manager

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sim_smoke.py"
CLEANUP_LEG = "cleanup deleted exactly the run's simulators"


def load_smoke():
    spec = importlib.util.spec_from_file_location("sim_smoke_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SKIP_WITHOUT_SCRIPT = unittest.skipUnless(SCRIPT.is_file(), "the simulator smoke is not part of this copy")


@SKIP_WITHOUT_SCRIPT
class SmokeCleanupTests(unittest.TestCase):
    def run_smoke(self, argv, fail_fresh=False):
        """main(argv) on the fakes, with a simulator of another device type already registered. Returns
        (exit code, the legs' results, the registry's UDIDs afterwards, simctl's UDIDs afterwards, the kept
        simulator's UDID)."""
        smoke = load_smoke()
        with fake_manager() as (first, simctl, wda, leases, root), tempfile.TemporaryDirectory() as folder:
            with first.acquire("iPhone Air") as kept:
                pass
            made = []

            def manager(*args, **kwargs):
                other = SimulatorManager(data_dir=first.data_dir, progress=kwargs.get("progress"))
                other.run, other.wda, other.lease = first.run, first.wda, first.lease
                other.clock, other.sleep, other._tools = first.clock, first.sleep, first._tools
                if fail_fresh and made:  # leg 2's fresh managers
                    other.acquire = mock.Mock(side_effect=RuntimeError("leg 2 broke"))
                made.append(other)
                return other
            report = Path(folder) / "smoke.json"
            with mock.patch("mobile_agent.sim.SimulatorManager", manager), \
                    mock.patch.object(smoke, "simctl_udids", lambda: set(simctl.devices)), \
                    mock.patch.dict(os.environ, {"MOBSTER_DATA_DIR": str(first.data_dir)}), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = smoke.main([*argv, "--repeat", "1", "--json", str(report)])
            results = json.loads(report.read_text())["results"]
            registered = [entry["udid"] for entry in first.registry.read()]
            return code, results, registered, set(simctl.devices), kept.udid

    def test_fail_after_deletes_only_the_run_simulator(self):
        code, results, registered, listed, kept = self.run_smoke(["--fail-after", "1"])
        self.assertEqual([result["leg"] for result in results],
                         ["prepare", "stopped after leg 1 (--fail-after)", CLEANUP_LEG])
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual(code, 0)
        self.assertEqual(registered, [kept])
        self.assertEqual(listed, {kept})
        self.assertIn("deleted 1 the run added", results[-1]["detail"])
        self.assertIn("kept 1 that were there before, lost: none", results[-1]["detail"])

    def test_an_unexpected_error_still_deletes_only_the_run_simulator(self):
        code, results, registered, listed, kept = self.run_smoke([], fail_fresh=True)
        self.assertEqual([(result["leg"], result["ok"]) for result in results],
                         [("prepare", True), ("unexpected error", False), (CLEANUP_LEG, True)])
        self.assertIn("leg 2 broke", results[1]["detail"])
        self.assertEqual(code, 1)
        self.assertEqual(registered, [kept])
        self.assertEqual(listed, {kept})

    def test_keep_keeps_and_skips_the_cleanup_leg(self):
        code, results, registered, listed, kept = self.run_smoke(["--fail-after", "1", "--keep"])
        self.assertEqual([result["leg"] for result in results], ["prepare", "stopped after leg 1 (--fail-after)"])
        self.assertEqual(len(registered), 2)
        self.assertIn(kept, registered)

    def test_the_registry_snapshot_is_never_reassigned(self):
        import ast
        tree = ast.parse(SCRIPT.read_text())
        assigned = [node.lineno for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AugAssign))
                    for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                    for name in ast.walk(target) if isinstance(name, ast.Name) and name.id == "registry_before"]
        self.assertEqual(len(assigned), 1, f"registry_before is assigned on lines {assigned}")


@SKIP_WITHOUT_SCRIPT
class SecondPrepareTests(unittest.TestCase):
    """Leg 14's --second-prepare, which the smoke's own instructions run: its simulator must not stay booted
    into leg 15, which boots two more (SPEC §0.3 allows two at once, briefly)."""

    def second_prepare(self, shutdown=True):
        smoke = load_smoke()
        with fake_manager() as (manager, simctl, wda, leases, root):
            products = build_dir(manager.data_dir, TOOLS.build) / "Build" / "Products"
            products.mkdir(parents=True)
            (products / "WebDriverAgentRunner_iphonesimulator26.4-arm64.xctestrun").write_text("")
            if not shutdown:
                manager.shutdown = lambda udid=None: None
            recorder = smoke.Smoke(1)
            with contextlib.redirect_stdout(io.StringIO()):
                ok = smoke.second_prepare(recorder, manager)
            udid, = [entry["udid"] for entry in manager.registry.read()]
            return ok, recorder.results, simctl.devices[udid]["state"], wda, leases, udid

    def test_the_second_prepare_is_shut_down_before_leg_15(self):
        ok, results, state, wda, leases, udid = self.second_prepare()
        self.assertTrue(ok, results)
        self.assertEqual([result["leg"] for result in results], ["second prepare reuses the WebDriverAgent build"])
        self.assertEqual(state, "Shutdown")
        self.assertIn(udid, wda.stopped)
        self.assertEqual(leases.held, set())
        self.assertIn("its simulator is Shutdown before leg 15", results[0]["detail"])

    def test_a_second_prepare_left_booted_fails_its_leg(self):
        ok, results, state, wda, leases, udid = self.second_prepare(shutdown=False)
        self.assertFalse(ok)
        self.assertEqual(state, "Booted")
        self.assertIn("its simulator is Booted before leg 15", results[0]["detail"])

    def test_leg_14_calls_it(self):
        text = SCRIPT.read_text()
        leg = text[text.index("# 14. list, shutdown, delete"):text.index("# 15. two prepares at once")]
        self.assertIn("second_prepare(smoke, manager)", leg)
        self.assertNotIn("manager.acquire()", leg)


if __name__ == "__main__":
    unittest.main()

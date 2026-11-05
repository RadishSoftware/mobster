"""Several agents on one Mac: who holds each simulator, the memory limit and the load governor."""

import json
import os
import unittest
from unittest import mock

from mobile_agent.sim import holders
from mobile_agent.sim.governor import Governor, Pressure
from mobile_agent.sim.models import SimError
from mobile_agent.tests.test_sim_fakes import fake_manager

CALM = Pressure(load=1.0, cores=8, free_gb=32.0)


def asking(manager, client, session):
    manager.who = lambda: {"client": client, "session": session}


class HolderTests(unittest.TestCase):
    def test_two_sessions_get_two_simulators_and_a_third_hears_who_holds_them(self):
        with fake_manager({"MOBSTER_MAX_SIMS": "2"}) as (manager, simctl, wda, leases, root):
            asking(manager, "Claude Code", "session a")
            first = manager.acquire()
            asking(manager, "Codex", "session b")
            second = manager.acquire()
            self.assertNotEqual(first.target.udid, second.target.udid)
            rows = {row["udid"]: row for row in manager.list()}
            self.assertEqual(rows[first.target.udid]["holder"]["client"], "Claude Code")
            self.assertEqual(rows[second.target.udid]["holder"]["session"], "session b")
            with self.assertRaises(SimError) as caught:
                manager.acquire(timeout=0)
            self.assertEqual(caught.exception.kind, "busy")
            self.assertIn("Claude Code (session a)", str(caught.exception))
            self.assertIn("Codex (session b)", str(caught.exception))
            self.assertIn("idle for 30 minutes", caught.exception.fix)
            self.assertEqual(len(simctl.simctl_calls("create")), 2)

    def test_releasing_clears_the_holder_and_the_next_session_gets_the_same_simulator(self):
        with fake_manager({"MOBSTER_MAX_SIMS": "1"}) as (manager, simctl, wda, leases, root):
            held = manager.acquire()
            held.release()
            self.assertIsNone(manager.list()[0]["holder"])
            self.assertIsNone(holders.read(manager.data_dir, held.target.udid))
            with manager.acquire() as target:
                self.assertEqual(target.udid, held.target.udid)

    def test_a_crashed_holder_is_ignored(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            held = manager.acquire()
            record = holders.read(manager.data_dir, held.target.udid)
            with mock.patch.object(holders, "alive", return_value=False):
                self.assertIsNone(holders.read(manager.data_dir, held.target.udid))
            self.assertEqual(record["pid"], os.getpid())

    def test_a_failed_boot_leaves_no_holder(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            with mock.patch.object(manager, "_make_ready", side_effect=SimError("simulator", "no")):
                with self.assertRaises(SimError):
                    manager.acquire()
            self.assertEqual([holders.read(manager.data_dir, entry["udid"]) for entry in manager.registry.read()],
                             [None])

    def test_release_mine_asks_only_holders_started_from_this_process_tree(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            mine = manager.acquire()
            other = manager.acquire()
            path = holders._path(manager.data_dir, other.target.udid)
            path.write_text(path.read_text().replace(f'"ppid": {os.getppid()}', '"ppid": 1'))
            with mock.patch.object(holders, "ancestors", return_value=[os.getpid(), os.getppid()]):
                self.assertEqual(manager.release_mine(), [mine.target.udid])
            self.assertTrue(holders.release_requested(manager.data_dir, mine.target.udid))
            self.assertFalse(holders.release_requested(manager.data_dir, other.target.udid))


class GovernorTests(unittest.TestCase):
    def test_memory_refuses_the_second_boot_at_once_and_names_the_holder(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            manager.governor = Governor(sample=lambda: Pressure(1.0, 8, 3.0))
            asking(manager, "Claude Code", "session a")
            first = manager.acquire()  # the first simulator always boots
            with self.assertRaises(SimError) as caught:
                manager.acquire(timeout=0)
            self.assertEqual(caught.exception.kind, "busy")
            self.assertIn("3.0 GB", str(caught.exception))
            self.assertIn("Claude Code (session a)", str(caught.exception))
            self.assertEqual(len(simctl.simctl_calls("create")), 1)
            first.release()
            manager.acquire().release()  # the booted one is free again: no new boot

    def test_memory_allows_a_second_boot_with_four_gigabytes_free(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            manager.governor = Governor(sample=lambda: Pressure(1.0, 8, 4.0))
            manager.acquire()
            manager.acquire()
            self.assertEqual(len(simctl.simctl_calls("create")), 2)

    def test_a_simulator_still_booting_keeps_its_memory_so_a_second_boot_at_once_is_refused(self):
        # Two sessions at once: the first has leased its simulator and is still booting it, so the free memory doesn't
        # show it yet. With room for one more simulator only, the second session is refused instead of booting.
        with fake_manager() as (manager, simctl, wda, leases, root):
            manager.governor = Governor(sample=lambda: Pressure(1.0, 8, 6.0))
            first = manager.acquire()
            simctl.devices[first.target.udid]["state"] = "Shutdown"   # leased, not booted yet
            with self.assertRaises(SimError) as caught:
                manager.acquire(timeout=0)
            self.assertEqual(caught.exception.kind, "busy")
            self.assertEqual(len(simctl.simctl_calls("create")), 1)

    def test_a_simulator_still_booting_leaves_room_for_another_when_there_is_memory_for_both(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            manager.governor = Governor(sample=lambda: Pressure(1.0, 8, 8.0))
            first = manager.acquire()
            simctl.devices[first.target.udid]["state"] = "Shutdown"
            second = manager.acquire()
            self.assertNotEqual(first.target.udid, second.target.udid)
            self.assertEqual(len(simctl.simctl_calls("create")), 2)

    def test_a_busy_mac_queues_a_boot_then_goes_ahead(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            readings = iter([Pressure(30.0, 8, 32.0), Pressure(30.0, 8, 32.0), CALM, CALM, CALM])
            said = []
            manager.governor = Governor(sample=lambda: next(readings, CALM), sleep=manager.sleep, clock=manager.clock)
            manager.progress = said.append
            manager.acquire().release()
            self.assertTrue(any("Waiting to boot" in line and "load 30" in line for line in said))
            self.assertEqual(len(simctl.simctl_calls("boot")), 1)

    def test_a_mac_that_stays_busy_refuses_and_starts_nothing(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            manager.governor = Governor(sample=lambda: Pressure(30.0, 8, 32.0), sleep=manager.sleep,
                                        clock=manager.clock)
            with self.assertRaises(SimError) as caught:
                manager.acquire()
            self.assertEqual(caught.exception.kind, "busy")
            self.assertIn("Nothing was started", str(caught.exception))
            self.assertEqual(simctl.simctl_calls("boot"), [])
            self.assertEqual([holders.read(manager.data_dir, e["udid"]) for e in manager.registry.read()], [None])

    def test_a_busy_wait_that_gives_up_before_any_boot_is_a_rejected_acquire(self):
        from mobile_agent.verify.actions import ActionJournal, ActionNotDispatched

        with fake_manager() as (manager, simctl, wda, leases, root):
            manager.acquire().release()   # a simulator already exists, so this acquire creates nothing
            booted, started = len(simctl.simctl_calls("boot")), list(wda.started)
            simctl.devices[manager.registry.read()[0]["udid"]]["state"] = "Shutdown"
            manager.governor = Governor(sample=lambda: Pressure(30.0, 8, 32.0), sleep=manager.sleep,
                                        clock=manager.clock)
            run_dir = root.parent / "busy-boot"
            run_dir.mkdir()
            journal = ActionJournal(run_dir)
            with self.assertRaises(ActionNotDispatched) as caught:
                with journal.action("ACQUIRE"):
                    manager.acquire()
            self.assertEqual(caught.exception.kind, "busy")
            self.assertIn("Nothing was started", str(caught.exception))
            self.assertIsNone(journal.klass)
            self.assertEqual(len(simctl.simctl_calls("create")), 1)   # the first acquire's, none since
            self.assertEqual(len(simctl.simctl_calls("boot")), booted)
            self.assertEqual(wda.started, started)
            rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
            self.assertEqual([row["event"] for row in rows], ["action_started", "action_finished"])
            self.assertEqual(rows[-1]["outcome"], "rejected")
            self.assertEqual(leases.held, set())

    def test_a_busy_wait_after_this_acquire_created_a_simulator_is_still_an_unknown_outcome(self):
        from mobile_agent.verify.actions import ActionJournal

        with fake_manager() as (manager, simctl, wda, leases, root):
            manager.governor = Governor(sample=lambda: Pressure(30.0, 8, 32.0), sleep=manager.sleep,
                                        clock=manager.clock)
            run_dir = root.parent / "busy-after-create"
            run_dir.mkdir()
            journal = ActionJournal(run_dir)
            with self.assertRaises(SimError) as caught:
                with journal.action("ACQUIRE"):
                    manager.acquire()
            self.assertEqual(caught.exception.kind, "busy")
            self.assertEqual(len(simctl.simctl_calls("create")), 1)   # an effect: the outcome is not "nothing happened"
            self.assertEqual(simctl.simctl_calls("boot"), [])
            self.assertEqual(journal.klass, "unknown_outcome")
            rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
            self.assertEqual(rows[-1]["outcome"], "unknown")
            self.assertEqual(leases.held, set())

    def test_a_boot_that_was_issued_and_gave_no_answer_is_still_an_unknown_outcome(self):
        from mobile_agent.sim.simctl import Result
        from mobile_agent.verify.actions import ActionJournal

        with fake_manager() as (manager, simctl, wda, leases, root):
            simctl.fail["bootstatus"] = [Result(None, "", "/usr/bin/xcrun did not finish in 240 s")]
            run_dir = root.parent / "boot-unknown"
            run_dir.mkdir()
            journal = ActionJournal(run_dir)
            with self.assertRaises(SimError) as caught:
                with journal.action("ACQUIRE"):
                    manager.acquire()
            self.assertEqual(caught.exception.kind, "simulator")
            self.assertEqual(len(simctl.simctl_calls("boot")), 1)
            self.assertEqual(journal.klass, "unknown_outcome")
            rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
            self.assertEqual(rows[-1]["outcome"], "unknown")

    def test_an_idle_booted_simulator_needs_no_calm(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            manager.acquire().release()
            manager.governor = Governor(sample=lambda: Pressure(30.0, 8, 0.5), sleep=manager.sleep,
                                        clock=manager.clock)
            with mock.patch("mobile_agent.sim.api.find_xctestrun", return_value="built"):  # the runner is built
                manager.acquire().release()

    def test_max_load_changes_when_the_mac_counts_as_busy(self):
        governor = Governor(sample=lambda: Pressure(12.0, 8, 32.0))
        self.assertTrue(governor.saturated())
        with mock.patch.dict(os.environ, {"MOBSTER_MAX_LOAD": "20"}):
            self.assertFalse(governor.saturated())
        with mock.patch.dict(os.environ, {"MOBSTER_MAX_LOAD": "x"}), self.assertRaises(SimError):
            governor.saturated()


if __name__ == "__main__":
    unittest.main()

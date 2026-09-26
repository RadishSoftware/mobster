"""Device allocation: exclusive, fair, and honest about health. No real devices."""

import threading
import time
import unittest
from unittest.mock import patch

from mobile_agent.journal import JournalError
from mobile_agent.pool import DevicePool, NoDeviceAvailable, WdaDeviceSpec


class FakeLease:
    def __init__(self, path=None):
        self.closed = False

    def close(self):
        self.closed = True


def pool(count=2, probe=None, **kwargs):
    specs = [WdaDeviceSpec(f"d{i}", f"http://127.0.0.1:{8100 + i}") for i in range(count)]
    return DevicePool(specs, probe=probe or (lambda url, timeout=3: True), **kwargs)


class DevicePoolTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("mobile_agent.pool.Lease.device", side_effect=lambda identity: FakeLease())
        self.lease = patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_task_at_a_time_per_device(self):
        p = pool(2)
        first = p.acquire("a", timeout=1)
        second = p.acquire("b", timeout=1)
        self.assertNotEqual(first.id, second.id)
        with self.assertRaises(NoDeviceAvailable):
            p.acquire("c", timeout=.3)
        p.release("a")
        third = p.acquire("c", timeout=1)
        self.assertEqual(third.id, first.id)
        p.close()

    def test_waiters_are_served_in_order(self):
        p = pool(1)
        p.acquire("holder", timeout=1)
        served, errors = [], []

        def waiter(name):
            try:
                p.acquire(name, timeout=5)
                served.append(name)
            except Exception as error:
                errors.append((name, error))

        threads = []
        for name in ("first", "second", "third"):
            thread = threading.Thread(target=waiter, args=(name,))
            thread.start()
            threads.append(thread)
            time.sleep(.15)  # establish queue order deterministically
        p.release("holder")
        time.sleep(.4)
        p.release("first")
        time.sleep(.4)
        p.release("second")
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(served, ["first", "second", "third"])
        p.close()

    def test_an_unreachable_device_is_withdrawn_and_returns_when_it_recovers(self):
        healthy = {"ok": False}

        def probe(socket, timeout=3):
            if not healthy["ok"]:
                raise RuntimeError("device is not ready")
            return True

        p = pool(1, probe=probe, health_interval=0)
        with self.assertRaises(NoDeviceAvailable):
            p.acquire("a", timeout=.3)
        self.assertEqual(p.public()[0]["status"], "unavailable")
        healthy["ok"] = True
        self.assertEqual(p.acquire("a", timeout=1).id, "d0")
        self.assertEqual(p.public()[0]["status"], "busy")
        p.close()

    def test_a_device_held_by_another_process_is_not_handed_out(self):
        p = pool(1)
        self.lease.side_effect = JournalError("another owner")
        with self.assertRaises(NoDeviceAvailable):
            p.acquire("a", timeout=.3)
        self.assertIn("another Mobster process", p.public()[0]["reason"])
        p.close()

    def test_releasing_frees_the_cross_process_lease(self):
        leases = []
        self.lease.side_effect = lambda identity: leases.append(FakeLease()) or leases[-1]
        p = pool(1)
        p.acquire("a", timeout=1)
        self.assertFalse(leases[0].closed)
        p.release("a")
        self.assertTrue(leases[0].closed)
        p.close()

    def test_a_failed_run_forces_a_fresh_health_check(self):
        probes = []

        def probe(socket, timeout=3):
            probes.append(socket)
            return True

        p = pool(1, probe=probe, health_interval=999)
        p.acquire("a", timeout=1)
        p.release("a", failed=True)
        p.acquire("b", timeout=1)
        self.assertEqual(len(probes), 2, "a failed device must be re-probed before reuse")
        self.assertEqual(p.public()[0]["failures"], 1)
        p.close()

    def test_fresh_pool_probes_despite_a_long_health_interval(self):
        probes = []
        p = pool(1, probe=lambda socket, timeout=3: probes.append(socket) or True,
                 health_interval=float("inf"))
        p.acquire("a", timeout=1)
        self.assertEqual(len(probes), 1, "first allocation must probe; uptime must not matter")
        p.close()

    def test_probe_result_is_discarded_if_device_taken_mid_probe(self):
        p = pool(1, health_interval=0)
        state = p.states["d0"]

        def probe(socket, timeout=3):
            state.owner = "intruder"  # another waiter took it while the lock was released
            raise RuntimeError("unreachable")

        p.probe = probe
        with p.condition:
            healthy = p._refresh(state, force=True)
        self.assertTrue(healthy)
        self.assertTrue(state.healthy)
        self.assertEqual(state.reason, "")
        p.close()

    def test_take_does_not_steal_a_device_taken_during_probe(self):
        p = pool(1, health_interval=0)
        state = p.states["d0"]

        def probe(socket, timeout=3):
            state.owner = "intruder"
            return True

        p.probe = probe
        with p.condition:
            self.assertIsNone(p._take("victim"))
        self.assertEqual(state.owner, "intruder")
        self.assertIsNone(state.lease)
        p.close()

    def test_one_owner_cannot_hold_two_devices(self):
        p = pool(2)
        p.acquire("a", timeout=1)
        with self.assertRaises(ValueError):
            p.acquire("a", timeout=1)
        p.close()

    def test_load_spreads_across_the_pool(self):
        p = pool(3)
        seen = []
        for index in range(6):
            owner = f"run{index}"
            seen.append(p.acquire(owner, timeout=1).id)
            p.release(owner)
        self.assertEqual(len(set(seen)), 3, "least-recently-used should rotate devices")
        p.close()

    def test_shutdown_wakes_waiters_instead_of_hanging(self):
        p = pool(1)
        p.acquire("holder", timeout=1)
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(
            self.assertRaises(NoDeviceAvailable, p.acquire, "waiter", 5)))
        thread.start()
        time.sleep(.2)
        p.close()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_pool_configuration_is_validated(self):
        with self.assertRaises(ValueError):
            DevicePool([])
        with self.assertRaises(ValueError):
            DevicePool([WdaDeviceSpec("a", "http://127.0.0.1:8100"), WdaDeviceSpec("a", "http://127.0.0.1:8101")])
        with self.assertRaises(ValueError):
            DevicePool([WdaDeviceSpec("a", "http://127.0.0.1:8100"), WdaDeviceSpec("b", "http://127.0.0.1:8100")])


if __name__ == "__main__":
    unittest.main()

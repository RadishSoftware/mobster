"""Local scenario admission, reservations, phase barriers and saved evidence."""

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import yaml

from mobile_agent.errors import SimError
from mobile_agent.sim import SimLease, SimTarget
from mobile_agent.testkit import capsule, scenario
from mobile_agent.testkit.execute import Attempts
from mobile_agent.verify.actions import ActionNotDispatched


def record(suffix):
    udid = f"11111111-2222-3333-4444-{suffix:012d}"
    return {"id": udid, "udid": udid, "kind": "simulator", "name": f"Lab {suffix}",
            "modelName": "iPhone 17 Pro", "ios": "26.4"}


class SimulatorPool:
    def __init__(self, *, busy=None):
        self.busy = busy
        self.acquired = []
        self.released = []

    def acquire_udid(self, udid, *, timeout=600, cancelled=None):
        if udid == self.busy:
            raise ActionNotDispatched(SimError("busy", "The actor is leased elsewhere."))
        self.acquired.append(udid)
        target = SimTarget(udid, "Lab", "iPhone 17 Pro", "iOS 26.4", "http://127.0.0.1:8310",
                           "http://127.0.0.1:9310", "test.xctestrun")
        owner = self

        class Lease:
            def close(self):
                owner.released.append(udid)

        return SimLease(target, [Lease()])


class ScriptedAttempts(Attempts):
    def __init__(self, root, pool, verdicts=None, stop=None):
        super().__init__(runs_dir=root / ".mobster" / "runs", replay="strict", stop=stop,
                         simulators=pool, parallel=2)
        self.verdicts = list(verdicts or [])
        self.calls = []

    def __call__(self, check, record, *, label, video_name=None):
        borrowed = self.manager_for(record)
        child = borrowed.acquire(cancelled=self.stop.is_set)
        child.release()
        self.calls.append((check.name, record["udid"], borrowed.root_lease.target.udid))
        verdict, klass = self.verdicts.pop(0) if self.verdicts else ("passed", None)
        return {"run_id": f"run-{len(self.calls)}", "verdict": verdict,
                "reason": {"class": klass, "message": verdict}, "seconds": 0.01,
                "device": {"name": record["name"], "type": "iPhone 17 Pro", "runtime": "iOS 26.4",
                           "udid": record["udid"]}, "frames": [], "run_dir": None, "report": None,
                "cost_usd": 0}


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        (self.root / ".mobster" / "checks").mkdir(parents=True)
        (self.root / ".mobster" / "scenarios").mkdir()
        self.records = [record(1), record(2)]
        self.by_id = {item["id"]: item for item in self.records}
        cap = mock.patch.object(scenario, "machine_parallel_cap", return_value=2)
        cap.start()
        self.addCleanup(cap.stop)

    def check(self, name, *, reset="data"):
        path = self.root / ".mobster" / "checks" / f"{name}.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "name": name, "app": {"bundle": "dev.example.app"},
                                        "reset": reset, "expect": [{"text": "Ready"}]}))
        return path.relative_to(self.root).as_posix()

    def manifest(self, phases, actors=None):
        path = self.root / ".mobster" / "scenarios" / "checkout.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "name": "Checkout", "actors": actors or {
            "buyer": self.records[0]["id"], "seller": self.records[1]["id"]}, "phases": phases},
            sort_keys=False))
        return path

    def plan(self, path):
        return scenario.plan_scenario(path, cwd=self.root, named_device=self.by_id.get)

    def test_both_actors_are_reserved_before_first_phase_and_sparse_rows_are_readable(self):
        first = self.check("buyer")
        second = self.check("seller")
        path = self.manifest([{"name": "Ready", "runs": [{"actor": "buyer", "check": first},
                                                       {"actor": "seller", "check": second}]}])
        planned = self.plan(path)
        pool = SimulatorPool()
        attempts = ScriptedAttempts(self.root, pool)
        doc, paths = scenario.execute_scenario(planned, attempts=attempts)
        self.assertEqual(doc["exitCode"], 0)
        self.assertEqual(len(pool.acquired), 2)
        self.assertEqual(len(pool.released), 2)
        self.assertEqual(len(attempts.calls), 2)
        self.assertEqual({row["scenario"]["actor"] for row in doc["checks"]}, {"buyer", "seller"})
        self.assertTrue((paths["folder"] / "results.json").is_file())
        self.assertEqual(capsule.read_completed_suite(paths["folder"])["exitCode"], 0)

    def test_busy_second_actor_releases_first_and_blocks_every_phase(self):
        first = self.check("buyer")
        path = self.manifest([{"name": "Start", "runs": [{"actor": "buyer", "check": first}]}])
        planned = self.plan(path)
        pool = SimulatorPool(busy=self.records[1]["udid"])
        attempts = ScriptedAttempts(self.root, pool)
        doc, paths = scenario.execute_scenario(planned, attempts=attempts)
        self.assertEqual(doc["exitCode"], 3)
        self.assertFalse(attempts.calls)
        self.assertEqual(pool.acquired, [self.records[0]["udid"]])
        self.assertEqual(pool.released, pool.acquired)
        self.assertEqual(doc["checks"][0]["results"][0]["status"], "couldnt_run")
        self.assertEqual(capsule.read_completed_suite(paths["folder"])["exitCode"], 3)

    def test_continuation_requires_no_reset_and_keeps_root_lease(self):
        first = self.check("first")
        second = self.check("second", reset="none")
        path = self.manifest([{"name": "Start", "runs": [{"actor": "buyer", "check": first}]},
                              {"name": "Continue", "runs": [{"actor": "buyer", "check": second}]}],
                             actors={"buyer": self.records[0]["id"]})
        planned = self.plan(path)
        pool = SimulatorPool()
        attempts = ScriptedAttempts(self.root, pool)
        doc, _ = scenario.execute_scenario(planned, attempts=attempts)
        self.assertEqual(doc["exitCode"], 0)
        self.assertEqual(len(attempts.calls), 2)
        self.assertEqual(pool.acquired, [self.records[0]["udid"]])
        self.assertEqual(pool.released, pool.acquired)
        self.assertNotEqual(doc["checks"][0]["name"], doc["checks"][1]["name"])

    def test_uncertain_phase_blocks_later_work_and_preserves_safety_exit(self):
        first = self.check("first")
        second = self.check("second", reset="none")
        path = self.manifest([{"name": "Start", "runs": [{"actor": "buyer", "check": first}]},
                              {"name": "Continue", "runs": [{"actor": "buyer", "check": second}]}],
                             actors={"buyer": self.records[0]["id"]})
        pool = SimulatorPool()
        attempts = ScriptedAttempts(self.root, pool, verdicts=[("couldnt_run", "unknown_outcome")])
        doc, _ = scenario.execute_scenario(self.plan(path), attempts=attempts)
        self.assertEqual(doc["stopReason"], "unknown_outcome")
        self.assertEqual(doc["exitCode"], 3)
        self.assertEqual(len(attempts.calls), 1)
        self.assertTrue(doc["checks"][1]["blocked"])

    def test_external_stop_during_only_phase_writes_stopped_not_green(self):
        first = self.check("first")
        path = self.manifest([{"name": "Only", "runs": [{"actor": "buyer", "check": first}]}],
                             actors={"buyer": self.records[0]["id"]})
        stop = threading.Event()

        class StoppingAttempts(ScriptedAttempts):
            def __call__(self, check, record, *, label, video_name=None):
                outcome = super().__call__(check, record, label=label, video_name=video_name)
                stop.set()
                return outcome

        attempts = StoppingAttempts(self.root, SimulatorPool(), stop=stop)
        doc, _ = scenario.execute_scenario(self.plan(path), attempts=attempts, stop=stop)
        self.assertEqual((doc["exitCode"], doc["stopReason"], doc["stopped"]), (130, "stopped", True))
        self.assertEqual(doc["checks"][0]["results"][0]["status"], "passed")

    def test_interrupt_drains_sibling_unknown_before_releasing_actor_leases(self):
        buyer = self.check("buyer")
        seller = self.check("seller")
        path = self.manifest([{"name": "Both", "runs": [{"actor": "buyer", "check": buyer},
                                                     {"actor": "seller", "check": seller}]}])
        seller_started = threading.Event()
        stop = threading.Event()
        pool = SimulatorPool()

        class UncertainAttempts(ScriptedAttempts):
            def __call__(self, check, record, *, label, video_name=None):
                seller_started.set()
                self.assert_lease_held = len(pool.released) == 0
                stop.wait(3)
                self.calls.append((check.name, record["udid"], record["udid"]))
                return {"run_id": "uncertain", "verdict": "couldnt_run",
                        "reason": {"class": "unknown_outcome", "message": "action may have completed"},
                        "seconds": 0.01, "device": {"type": "iPhone 17 Pro", "runtime": "iOS 26.4",
                                                     "udid": record["udid"]}, "frames": [], "cost_usd": 0}

        attempts = UncertainAttempts(self.root, pool, stop=stop)

        def interrupt_buyer(suite):
            if suite.entries[0].name == "buyer":
                if not seller_started.wait(3):
                    raise AssertionError("The sibling did not start")
                raise KeyboardInterrupt()

        doc, _ = scenario.execute_scenario(self.plan(path), attempts=attempts, stop=stop,
                                           on_suite=interrupt_buyer)
        self.assertEqual(doc["stopReason"], "unknown_outcome")
        self.assertEqual(doc["exitCode"], 3)
        self.assertTrue(any(cell["reason"]["class"] == "unknown_outcome"
                            for row in doc["checks"] for cell in row["results"]))
        self.assertTrue(attempts.assert_lease_held)
        self.assertEqual(len(pool.released), 2)

    def test_interrupt_during_submit_and_again_during_shutdown_keeps_leases_until_worker_closes(self):
        first = self.check("first")
        path = self.manifest([{"name": "Only", "runs": [{"actor": "buyer", "check": first}]}],
                             actors={"buyer": self.records[0]["id"]})
        stop, started, finished = threading.Event(), threading.Event(), threading.Event()
        actor_pool = SimulatorPool()

        class SlowAttempts(ScriptedAttempts):
            def __call__(self, check, record, *, label, video_name=None):
                started.set()
                self.saw_stop = stop.wait(3)
                self.lease_held_at_finish = not actor_pool.released
                finished.set()
                self.calls.append((check.name, record["udid"], record["udid"]))
                return {"run_id": "run-1", "verdict": "passed", "reason": {"class": None},
                        "seconds": 0.01, "device": {"type": "iPhone 17 Pro", "runtime": "iOS 26.4",
                                                     "udid": record["udid"]}, "frames": [], "cost_usd": 0}

        class InterruptingPool(ThreadPoolExecutor):
            shutdown_calls = 0

            def submit(self, fn, *args, **kwargs):
                super().submit(fn, *args, **kwargs)
                if not started.wait(3):
                    raise AssertionError("The scenario member did not start")
                raise KeyboardInterrupt()

            def shutdown(self, wait=True, *, cancel_futures=False):
                self.shutdown_calls += 1
                if self.shutdown_calls == 1:
                    raise KeyboardInterrupt()
                return super().shutdown(wait=wait, cancel_futures=cancel_futures)

        attempts = SlowAttempts(self.root, actor_pool, stop=stop)
        with mock.patch.object(scenario, "ThreadPoolExecutor", InterruptingPool):
            doc, _ = scenario.execute_scenario(self.plan(path), attempts=attempts, stop=stop)
        self.assertTrue(finished.is_set())
        self.assertTrue(attempts.saw_stop)
        self.assertTrue(attempts.lease_held_at_finish)
        self.assertEqual(actor_pool.released, actor_pool.acquired)
        self.assertEqual((doc["exitCode"], doc["stopReason"]), (3, "unknown_outcome"))

    def test_post_dispatch_busy_is_unknown_not_a_retryable_reservation_refusal(self):
        first = self.check("first")
        path = self.manifest([{"name": "Only", "runs": [{"actor": "buyer", "check": first}]}],
                             actors={"buyer": self.records[0]["id"]})

        class PostEffectPool(SimulatorPool):
            def acquire_udid(self, udid, *, timeout=600, cancelled=None):
                raise SimError("busy", "The simulator became busy after readiness began.")

        pool = PostEffectPool()
        attempts = ScriptedAttempts(self.root, pool)
        doc, _ = scenario.execute_scenario(self.plan(path), attempts=attempts)
        self.assertEqual((doc["exitCode"], doc["stopReason"]), (3, "unknown_outcome"))
        self.assertFalse(attempts.calls)

    def test_changed_later_check_blocks_next_phase_before_mutation(self):
        first = self.check("first")
        second = self.check("second", reset="none")
        path = self.manifest([{"name": "Start", "runs": [{"actor": "buyer", "check": first}]},
                              {"name": "Continue", "runs": [{"actor": "buyer", "check": second}]}],
                             actors={"buyer": self.records[0]["id"]})

        class ChangingAttempts(ScriptedAttempts):
            def __call__(self, check, record, *, label, video_name=None):
                outcome = super().__call__(check, record, label=label, video_name=video_name)
                if check.name == "first":
                    saved = self.root / second
                    saved.write_text(saved.read_text().replace("Ready", "Changed"))
                return outcome

        attempts = ChangingAttempts(self.root, SimulatorPool())
        attempts.root = self.root
        doc, _ = scenario.execute_scenario(self.plan(path), attempts=attempts)
        self.assertEqual((doc["exitCode"], doc["stopReason"]), (3, "refused"))
        self.assertEqual(len(attempts.calls), 1)
        self.assertTrue(doc["checks"][1]["blocked"])

    def test_alias_and_destructive_continuation_refuse_at_plan_time(self):
        first = self.check("first")
        second = self.check("second")
        alias = dict(self.records[0], id=self.records[1]["id"])
        path = self.manifest([{"name": "Start", "runs": [{"actor": "buyer", "check": first}]}])
        with mock.patch.dict(self.by_id, {self.records[1]["id"]: alias}):
            with self.assertRaises(scenario.UsageError):
                self.plan(path)
        path = self.manifest([{"name": "Start", "runs": [{"actor": "buyer", "check": first}]},
                              {"name": "Continue", "runs": [{"actor": "buyer", "check": second}]}],
                             actors={"buyer": self.records[0]["id"]})
        with self.assertRaisesRegex(scenario.UsageError, "reset: none"):
            self.plan(path)

    def test_saved_link_and_accessibility_context_are_frozen_before_reservation(self):
        check = self.check("open-cart")
        check_path = self.root / check
        saved = yaml.safe_load(check_path.read_text())
        saved["steps"] = ["open cart"]
        saved["a11y"] = "strict"
        check_path.write_text(yaml.safe_dump(saved))
        links = self.root / ".mobster" / "links.yaml"
        links.write_text("links:\n  - {name: cart, url: 'daybreak://cart'}\n")
        baseline = self.root / ".mobster" / "a11y-baseline.json"
        baseline.write_text(json.dumps({"schema": "mobster.a11y-baseline/1", "findings": ["old-key"]}))
        path = self.manifest([{"name": "Open", "runs": [{"actor": "buyer", "check": check}]}],
                             actors={"buyer": self.records[0]["id"]})
        planned = self.plan(path)
        prepared = planned.phases[0].members[0].entry.check
        self.assertEqual(prepared.steps, ("Open the link daybreak://cart",))
        self.assertEqual(prepared.a11y_baseline, frozenset({"old-key"}))
        self.assertEqual(prepared.a11y_scope, check)
        links.write_text("links:\n  - {name: cart, url: 'daybreak://different'}\n")
        pool = SimulatorPool()
        attempts = ScriptedAttempts(self.root, pool)
        doc, _ = scenario.execute_scenario(planned, attempts=attempts)
        self.assertEqual(doc["stopReason"], "refused")
        self.assertEqual(doc["exitCode"], 3)
        self.assertEqual(pool.acquired, [])
        self.assertEqual(attempts.calls, [])

    def test_an_app_that_cannot_be_pinned_refuses_the_scenario_at_plan_time(self):
        relative = self.check("app")
        saved = self.root / relative
        saved.write_text(yaml.safe_dump({"version": 1, "name": "app", "reset": "data", "expect": [{"text": "Ready"}],
                                         "app": {"bundle": "dev.example.app", "path": str(self.root / "Gone.app")}}))
        path = self.manifest([{"name": "Start", "runs": [{"actor": "buyer", "check": relative}]}],
                             actors={"buyer": self.records[0]["id"]})
        with self.assertRaisesRegex(scenario.UsageError, "unreadable"):
            self.plan(path)

    def test_accessibility_baseline_change_refuses_before_reservation(self):
        check = self.check("a11y")
        path = self.manifest([{"name": "Audit", "runs": [{"actor": "buyer", "check": check}]}],
                             actors={"buyer": self.records[0]["id"]})
        planned = self.plan(path)
        (self.root / ".mobster" / "a11y-baseline.json").write_text(json.dumps({
            "schema": "mobster.a11y-baseline/1", "findings": ["new-key"]}))
        pool = SimulatorPool()
        doc, _ = scenario.execute_scenario(planned, attempts=ScriptedAttempts(self.root, pool))
        self.assertEqual((doc["exitCode"], doc["stopReason"]), (3, "refused"))
        self.assertFalse(pool.acquired)

    def test_manifest_change_after_admission_refuses_before_reservation(self):
        check = self.check("one")
        path = self.manifest([{"name": "Start", "runs": [{"actor": "buyer", "check": check}]}],
                             actors={"buyer": self.records[0]["id"]})
        planned = self.plan(path)
        path.write_text(path.read_text().replace("name: Checkout", "name: Changed"))
        pool = SimulatorPool()
        doc, _ = scenario.execute_scenario(planned, attempts=ScriptedAttempts(self.root, pool))
        self.assertEqual((doc["exitCode"], doc["stopReason"]), (3, "refused"))
        self.assertFalse(pool.acquired)


if __name__ == "__main__":
    unittest.main()

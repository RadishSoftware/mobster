"""Verification effects persist intent before dispatch and stop on uncertain outcomes."""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent.journal import append_jsonl
from mobile_agent.sim.models import SimError
from mobile_agent.verify.actions import ActionError, ActionJournal, ActionNotDispatched
from mobile_agent.verify.conditions import Applier, ConditionError


class ActionJournalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.actions = ActionJournal(self.root)

    def rows(self):
        path = self.root / "actions.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_action_import_keeps_simulator_optional_until_needed(self):
        script = ("import sys\n"
                  "from mobile_agent.verify.actions import ActionNotDispatched\n"
                  "from mobile_agent.verify.runner import VerifyRun\n"
                  "from mobile_agent.errors import SimError\n"
                  "assert 'mobile_agent.sim' not in sys.modules\n"
                  "assert issubclass(ActionNotDispatched, SimError)\n"
                  "from mobile_agent.sim.models import SimError as Exported\n"
                  "assert Exported is SimError\n")
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
                                capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_intent_is_durable_before_mutation_and_acknowledged_after(self):
        observed = []
        with self.actions.action("BUSINESS_SEED", operation_id="run1:seed"):
            observed.extend(self.rows())
        self.assertEqual([row["event"] for row in observed], ["action_started"])
        self.assertEqual([(row["event"], row.get("outcome")) for row in self.rows()],
                         [("action_started", None), ("action_finished", "acknowledged")])
        self.assertEqual(self.rows()[0]["operation_id"], "run1:seed")

    def test_intent_write_failure_refuses_before_body(self):
        called = []
        with patch("mobile_agent.verify.actions.append_jsonl", side_effect=OSError("disk")):
            with self.assertRaises(ActionError) as caught:
                with self.actions.action("BUSINESS_SEED"):
                    called.append(True)
        self.assertEqual(caught.exception.klass, "journal")
        self.assertEqual(called, [])
        self.assertTrue(self.actions.blocked)
        with self.assertRaises(ActionError):
            with self.actions.action("BUSINESS_SEED"):
                called.append(True)
        self.assertEqual(called, [])

    def test_dispatch_or_ack_uncertainty_is_sticky(self):
        with self.assertRaises(TimeoutError):
            with self.actions.action("BUSINESS_SEED"):
                raise TimeoutError("no reply")
        self.assertEqual(self.actions.klass, "unknown_outcome")
        self.assertEqual(self.rows()[-1]["outcome"], "unknown")
        with self.assertRaises(ActionError):
            with self.actions.action("BUSINESS_CLEANUP"):
                self.fail("cleanup would risk retrying an uncertain effect")

        fresh = ActionJournal(self.root / "fresh")
        (fresh.path.parent).mkdir(exist_ok=True)
        writes = []
        real = append_jsonl

        def ack_fails(path, event):
            writes.append(event["event"])
            if event["event"] == "action_finished":
                raise OSError("ack failed")
            real(path, event)

        with patch("mobile_agent.verify.actions.append_jsonl", side_effect=ack_fails):
            with self.assertRaises(ActionError) as caught:
                with fresh.action("BUSINESS_SEED"):
                    pass
        self.assertEqual(caught.exception.klass, "unknown_outcome")
        self.assertEqual(writes, ["action_started", "action_finished"])
        self.assertTrue(fresh.blocked)

    def test_crash_recovery_refuses_pending_or_unknown_action(self):
        append_jsonl(self.actions.path, {"event": "action_started", "id": 1, "operation": "BUSINESS_SEED"})
        resumed = ActionJournal(self.root)
        self.assertEqual(resumed.klass, "unknown_outcome")
        with self.assertRaises(ActionError):
            with resumed.action("BUSINESS_SEED"):
                self.fail("a pending intent was replayed")
        self.assertEqual(len(self.rows()), 1)

    def test_recovery_after_acknowledged_action_can_dispatch(self):
        with self.actions.action("BUSINESS_SEED"):
            pass
        resumed = ActionJournal(self.root)
        with resumed.action("BUSINESS_CLEANUP"):
            pass
        self.assertEqual([row["id"] for row in self.rows()], [1, 1, 2, 2])
        self.assertFalse(resumed.blocked)

    def test_cancel_seals_admission_before_and_during_dispatch(self):
        self.assertFalse(self.actions.request_stop())
        with self.assertRaises(ActionError) as caught:
            with self.actions.action("BUSINESS_SEED"):
                self.fail("cancelled action dispatched")
        self.assertEqual(caught.exception.klass, "stopped")
        self.assertEqual(self.rows(), [])

        active = ActionJournal(self.root / "active")
        active.path.parent.mkdir()
        begun = threading.Event()
        finish = threading.Event()

        def dispatch():
            with active.action("BUSINESS_SEED"):
                begun.set()
                finish.wait(2)

        thread = threading.Thread(target=dispatch)
        thread.start()
        self.assertTrue(begun.wait(2))
        self.assertTrue(active.request_stop())
        finish.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(active.blocked)
        with self.assertRaises(ActionError):
            with active.action("BUSINESS_CLEANUP"):
                self.fail("cancelled cleanup dispatched")

    def test_known_acquire_rejection_does_not_poison_later_actions(self):
        with self.assertRaises(ActionNotDispatched):
            with self.actions.action("ACQUIRE"):
                raise ActionNotDispatched(SimError("busy", "lease held", "Try later."))
        self.assertEqual(self.rows()[-1]["outcome"], "rejected")
        with self.actions.action("LAUNCH"):
            pass
        self.assertFalse(self.actions.blocked)

    def test_http_wrapper_covers_mutations_and_restores_fake_driver(self):
        class Driver:
            def __init__(self):
                self.calls = []

            def call(self, method, path, *args, **kwargs):
                self.calls.append((method, path))
                if path == "/uncertain":
                    raise TimeoutError("lost reply")

        driver = Driver()
        original = driver.call
        self.actions.wrap_http(driver)
        driver.call("GET", "/status")
        driver.call("POST", "/tap")
        self.assertEqual([(row["event"], row["operation"]) for row in self.rows()],
                         [("action_started", "HTTP_POST"), ("action_finished", "HTTP_POST")])
        with self.assertRaises(TimeoutError):
            driver.call("POST", "/uncertain")
        with self.assertRaises(ActionError):
            driver.call("POST", "/tap")
        self.assertEqual(driver.calls, [("GET", "/status"), ("POST", "/tap"), ("POST", "/uncertain")])
        self.actions.unwrap_http(driver)
        self.assertEqual(driver.call, original)

    def test_driver_guard_covers_recovery_callbacks_without_double_wrapping(self):
        class Driver:
            def __init__(self):
                self.calls = []

            def call(self, method, path):
                self.calls.append((method, path))

            def new_session(self):
                self.calls.append("new_session")

            def restarter(self):
                self.calls.append("restarter")

        driver = Driver()
        original_call, original_session, original_restarter = driver.call, driver.new_session, driver.restarter
        self.assertIs(self.actions.guard_driver(driver), driver)
        self.actions.guard_driver(driver)
        driver.new_session()
        driver.restarter()
        driver.call("POST", "/tap")
        self.assertEqual([row["operation"] for row in self.rows() if row["event"] == "action_started"],
                         ["NEW_SESSION", "WDA_RESTART", "HTTP_POST"])
        self.actions.unwrap_http(driver)
        self.assertEqual((driver.call, driver.new_session, driver.restarter),
                         (original_call, original_session, original_restarter))
        self.assertFalse(self.actions.blocked)

    def test_a_driver_without_wda_has_each_named_effect_journaled_and_one_with_nothing_is_refused(self):
        class Adb:
            effects = ("execute", "tap_point")

            def execute(self, *args, **kwargs):
                return "done"

            def tap_point(self, x, y):
                return None

            def observe(self):
                return "screen"

        driver = Adb()
        originals = (driver.execute, driver.tap_point)
        self.actions.guard_driver(driver)
        self.assertEqual(driver.execute("TAP", None, None), "done")
        driver.tap_point(.5, .5)
        driver.observe()
        self.assertEqual([row["operation"] for row in self.rows() if row["event"] == "action_started"],
                         ["DRIVER_EXECUTE", "DRIVER_TAP_POINT"])
        self.actions.unwrap_http(driver)
        self.assertEqual((driver.execute, driver.tap_point), originals)

        class Bare:
            def execute(self, *args, **kwargs):
                raise AssertionError("an unguarded driver acted")
        with self.assertRaises(TypeError):
            self.actions.guard_driver(Bare())
        self.assertEqual([row["operation"] for row in self.rows() if row["event"] == "action_started"],
                         ["DRIVER_EXECUTE", "DRIVER_TAP_POINT"])

    def test_restore_checks_each_effect_and_stops_on_uncertain_result(self):
        calls = []

        def simctl(step):
            calls.append(step)
            return 1, "unconfirmed"

        conditions = Applier({}, udid="SIM", bundle="com.example.test", run=simctl)
        conditions._undo = [["location", "SIM", "clear"], ["status_bar", "SIM", "clear"]]
        with self.assertRaises(ConditionError):
            conditions.restore(self.actions)
        self.assertEqual(calls, [["status_bar", "SIM", "clear"]])
        self.assertEqual(self.actions.klass, "unknown_outcome")
        self.assertEqual(self.rows()[-1]["outcome"], "unknown")
        with self.assertRaises(ActionError):
            conditions.restore(self.actions)
        self.assertEqual(calls, [["status_bar", "SIM", "clear"]])

    def test_restore_acknowledges_each_confirmed_effect(self):
        calls = []

        def simctl(step):
            calls.append(step)
            return 0, ""

        conditions = Applier({}, udid="SIM", bundle="com.example.test", run=simctl)
        conditions._undo = [["location", "SIM", "clear"], ["status_bar", "SIM", "clear"]]
        conditions.restore(self.actions)
        self.assertEqual(calls, [["status_bar", "SIM", "clear"], ["location", "SIM", "clear"]])
        self.assertEqual([row.get("outcome") for row in self.rows() if row["event"] == "action_finished"],
                         ["acknowledged", "acknowledged"])
        self.assertFalse(self.actions.blocked)

    def test_condition_abort_between_writes_does_not_dispatch_the_next_one(self):
        calls = []

        def simctl(step):
            calls.append(step)
            self.actions.request_stop()
            return 0, ""

        conditions = Applier({"permissions": {"photos": "grant", "contacts": "grant"}}, udid="SIM",
                             bundle="com.example.test", run=simctl, actions=self.actions)
        with self.assertRaises(ActionError) as caught:
            conditions.before_launch()
        self.assertEqual(caught.exception.klass, "stopped")
        self.assertEqual(calls, [["privacy", "SIM", "grant", "photos", "com.example.test"]])
        self.assertEqual([row["operation"] for row in self.rows() if row["event"] == "action_started"],
                         ["CONDITION_SET"])

    def test_condition_write_without_a_journal_refuses_before_simctl(self):
        calls = []
        conditions = Applier({"location": {"latitude": 1, "longitude": 2}}, udid="SIM",
                             bundle="com.example.test", run=lambda step: calls.append(step))
        with self.assertRaises(ConditionError):
            conditions.before_launch()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

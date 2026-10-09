"""`mobster test`'s scheduler and verdicts (testkit/suite.py) with fake attempt runners: parallel slots never share
a simulator, a named device runs one check at a time, retries, flakes, repeats and pass rates, quarantine, --strict,
the exit code's order, refusals before anything runs, and a stop. Offline."""

from pathlib import Path
import threading
import time
import unittest

from mobile_agent.testkit import targets as T
from mobile_agent.testkit.discover import Entry
from mobile_agent.testkit.suite import RETRY_CLASSES, Suite
from mobile_agent.verify.checks import check_from_dict

BUNDLE = "dev.mobster.daybreak"
PHONE = {"id": "usb-00008140", "udid": "00008130-001A2B3C4D5E6F70", "kind": "usb", "name": "Sam's iPhone"}


def entry(name, quarantined=None, error=None, **data):
    check = None if error else check_from_dict({"bundle_id": BUNDLE, "name": name, "expect": [{"text": "Hi"}], **data})
    return Entry(path=Path(f"/p/.mobster/checks/{name}.yaml"), file=f".mobster/checks/{name}.yaml", check=check,
                 error=error, quarantined=quarantined, classname=f"checks.{name}")


def result(verdict, klass=None, message=None, device=None, seconds=1.0, run_id=None):
    return {"run_id": run_id or f"20261007-120000-{abs(hash((verdict, time.monotonic()))) % 65536:04x}",
            "verdict": verdict, "reason": {"class": klass, "message": message or verdict, "fix": None},
            "seconds": seconds, "device": device or {"name": "Mobster · iPhone 17 Pro · iOS 26.4",
                                                     "type": "iPhone 17 Pro", "runtime": "iOS 26.4", "udid": "U"},
            "frames": [], "run_dir": None, "report": None, "cost_usd": 0.01}


class Script:
    """An attempt runner that answers each check's attempts from a script: {check name: [verdict, ...]}."""

    def __init__(self, script, default="passed"):
        self.script = {name: list(verdicts) for name, verdicts in script.items()}
        self.default, self.calls, self.lock = default, [], threading.Lock()

    def __call__(self, check, record, *, label, video_name=None):
        with self.lock:
            self.calls.append((check.name, check.device, (record or {}).get("id"), label))
            queue = self.script.get(check.name)
            verdict = queue.pop(0) if queue else self.default
        if isinstance(verdict, tuple):
            return result(*verdict)
        return result(verdict, "assertion" if verdict == "failed" else None)


class Pool:
    """A simulator manager's leases: acquire() hands out a free simulator of the type, as SimulatorManager does
    (each lease is exclusive); the test records every overlap."""

    def __init__(self, sims_per_type=4):
        self.free = {}
        self.sims_per_type = sims_per_type
        self.in_use, self.overlaps, self.most = set(), [], 0
        self.lock = threading.Lock()

    def acquire(self, device):
        with self.lock:
            free = self.free.setdefault(device, [f"{device}#{n}" for n in range(self.sims_per_type)])
            sim = free.pop(0)
            if sim in self.in_use:
                self.overlaps.append(sim)
            self.in_use.add(sim)
            self.most = max(self.most, len(self.in_use))
            return sim

    def release(self, device, sim):
        with self.lock:
            self.in_use.discard(sim)
            self.free[device].append(sim)


class SchedulingTests(unittest.TestCase):
    def test_parallel_slots_never_share_a_simulator_and_respect_the_limit(self):
        pool = Pool()
        busy = {"now": 0, "most": 0}
        lock = threading.Lock()

        def attempt(check, record, *, label, video_name=None):
            sim = pool.acquire(check.device)
            with lock:
                busy["now"] += 1
                busy["most"] = max(busy["most"], busy["now"])
            time.sleep(.02)
            with lock:
                busy["now"] -= 1
            pool.release(check.device, sim)
            return result("passed")
        entries = [entry(f"check{n}") for n in range(6)]
        targets = [T.simulator("iPhone 17 Pro"), T.simulator("iPhone SE (3rd generation)")]
        suite = Suite(entries, targets, attempt=attempt, parallel=3)
        doc = suite.run()
        self.assertEqual(pool.overlaps, [])
        self.assertEqual(busy["most"], 3)
        self.assertEqual(doc["summary"]["passed"], 12)
        self.assertEqual(doc["exitCode"], 0)

    def test_a_named_device_runs_one_check_at_a_time_beside_the_simulators(self):
        running = {"phone": 0, "most_phone": 0, "sims": 0, "together": False}
        lock = threading.Lock()

        def attempt(check, record, *, label, video_name=None):
            key = "phone" if record else "sims"
            with lock:
                running[key] += 1
                running["most_phone"] = max(running["most_phone"], running["phone"])
                running["together"] |= running["phone"] > 0 and running["sims"] > 0
            time.sleep(.03)
            with lock:
                running[key] -= 1
            return result("passed")
        gate = type("Gate", (), {"admit": lambda self, check, record: (check, [], record)})()
        targets = [T.simulator("iPhone 17 Pro"), T.Target("device:usb", "device", record=PHONE)]
        Suite([entry(f"c{n}") for n in range(4)], targets, attempt=attempt, parallel=2, gate=gate).run()
        self.assertEqual(running["most_phone"], 1)
        self.assertTrue(running["together"])

    def test_targets_set_the_device_and_the_label_names_the_attempt(self):
        script = Script({"paywall": ["failed", "passed"]})
        Suite([entry("paywall")], [T.simulator("iPhone 16e", "iOS 26.4")], attempt=script, retries=2).run()
        self.assertEqual([call[1] for call in script.calls], ["iPhone 16e", "iPhone 16e"])
        self.assertEqual(script.calls[1][3], "paywall · iPhone 16e · iOS 26.4 (attempt 2 of 3)")


class VerdictTests(unittest.TestCase):
    def run_suite(self, script, entries=None, **kwargs):
        entries = entries or [entry(name) for name in script]
        suite = Suite(entries, kwargs.pop("targets", [T.DEFAULT]), attempt=Script(script), **kwargs)
        return suite.run()

    def results(self, doc):
        return {check["name"]: check["results"][0] for check in doc["checks"]}

    def test_a_pass_on_retry_is_flaky_and_passes_unless_strict(self):
        doc = self.run_suite({"a": ["failed", "passed"], "b": ["passed"]})
        a = self.results(doc)["a"]
        self.assertEqual((a["status"], a["flaky"], a["attempts"]), ("passed", True, 2))
        self.assertEqual(a["flakyReason"]["class"], "assertion")
        self.assertEqual(doc["summary"]["flaky"], 1)
        self.assertEqual(doc["exitCode"], 0)
        self.assertEqual(self.run_suite({"a": ["failed", "passed"]}, strict=True)["exitCode"], 1)

    def test_retries_stop_at_the_limit_and_only_for_what_a_retry_can_fix(self):
        doc = self.run_suite({"a": ["failed"] * 5}, retries=2)
        self.assertEqual(self.results(doc)["a"]["attempts"], 3)
        self.assertEqual(doc["exitCode"], 1)
        for klass in ("usage", "model", "budget", "install", "stopped"):
            doc = self.run_suite({"a": [("couldnt_run", klass)] * 3}, retries=2)
            with self.subTest(klass=klass):
                self.assertEqual(self.results(doc)["a"]["attempts"], 1)
        for klass in sorted(RETRY_CLASSES):
            doc = self.run_suite({"a": [("couldnt_run", klass), "passed"]}, retries=1)
            with self.subTest(klass=klass):
                self.assertEqual(self.results(doc)["a"]["attempts"], 2)
        doc = self.run_suite({"a": ["needs_review", "passed"]})
        self.assertEqual(self.results(doc)["a"]["attempts"], 1)
        self.assertEqual(self.run_suite({"a": ["failed", "passed"]}, retries=0)["exitCode"], 1)

    def test_repeat_gives_a_pass_rate_and_fails_unless_every_run_passed(self):
        doc = self.run_suite({"a": ["passed", "failed", "passed", "passed", "passed"], "b": ["passed"] * 5},
                             repeat=5, retries=0)
        a, b = self.results(doc)["a"], self.results(doc)["b"]
        self.assertEqual((a["status"], a["flaky"], a["passRate"], a["attempts"]), ("failed", True, .8, 5))
        self.assertEqual((b["status"], b["flaky"], b["passRate"]), ("passed", False, 1.0))
        self.assertEqual([run["repetition"] for run in a["runs"]], [1, 2, 3, 4, 5])
        self.assertEqual(doc["exitCode"], 1)

    def test_the_exit_code_order_failed_then_couldnt_run_then_needs_review(self):
        self.assertEqual(self.run_suite({"a": ["needs_review"], "b": [("couldnt_run", "usage")],
                                         "c": ["failed", "failed"]})["exitCode"], 1)
        self.assertEqual(self.run_suite({"a": ["needs_review"], "b": [("couldnt_run", "usage")]})["exitCode"], 3)
        self.assertEqual(self.run_suite({"a": ["needs_review"], "b": ["passed"]})["exitCode"], 2)

    def test_quarantined_checks_run_and_are_reported_but_never_fail_the_run(self):
        entries = [entry("a", quarantined="Flaky on iOS 26.4"), entry("b")]
        doc = self.run_suite({"a": ["failed", "failed"], "b": ["passed"]}, entries=entries)
        self.assertEqual(self.results(doc)["a"]["status"], "failed")
        self.assertTrue(self.results(doc)["a"]["quarantined"])
        self.assertEqual(doc["summary"], {"passed": 1, "failed": 0, "needsReview": 0, "couldntRun": 0, "flaky": 0,
                                          "quarantined": 1, "total": 2, "costUsd": 0.03})
        self.assertEqual(doc["exitCode"], 0)
        self.assertEqual(doc["checks"][0]["quarantineReason"], "Flaky on iOS 26.4")

    def test_a_broken_check_and_a_refused_target_couldnt_run_without_an_attempt(self):
        script = Script({})
        entries = [entry("broken", error="broken.yaml: version 2 is not supported"),
                   entry("social", bundle_id="com.burbn.instagram")]
        doc = Suite(entries, [T.DEFAULT], attempt=script).run()
        self.assertEqual(script.calls, [])
        broken, social = (check["results"][0] for check in doc["checks"])
        self.assertEqual((broken["status"], broken["reason"]["class"], broken["attempts"]), ("couldnt_run", "usage", 0))
        self.assertEqual(social["reason"]["class"], "not_unattended")
        self.assertEqual(doc["checks"][0]["error"], "broken.yaml: version 2 is not supported")
        self.assertEqual(doc["exitCode"], 3)

    def test_devices_are_named_after_the_simulator_each_run_reported(self):
        doc = self.run_suite({"a": ["passed"]}, targets=[T.DEFAULT, T.simulator("iPhone 16e")])
        self.assertEqual([device["name"] for device in doc["devices"]],
                         ["iPhone 17 Pro · iOS 26.4"])  # both ran on the fake's iPhone 17 Pro · iOS 26.4

    def test_a_stop_leaves_the_rest_not_run(self):
        stop = threading.Event()

        def attempt(check, record, *, label, video_name=None):
            stop.set()
            return result("passed")
        doc = Suite([entry("a"), entry("b"), entry("c")], [T.DEFAULT], attempt=attempt, stop=stop).run()
        statuses = [check["results"][0]["status"] for check in doc["checks"]]
        self.assertEqual(statuses, ["passed", "couldnt_run", "couldnt_run"])
        self.assertEqual(doc["checks"][1]["results"][0]["reason"]["class"], "stopped")

    def test_ctrl_c_while_waiting_stops_the_runs_in_flight(self):
        started, aborted = threading.Event(), []

        def attempt(check, record, *, label, video_name=None):
            started.set()
            suite.stop.wait(5)
            return result("couldnt_run", "stopped")
        suite = Suite([entry("a"), entry("b")], [T.DEFAULT], attempt=attempt,
                      on_interrupt=lambda: aborted.append(True))

        def interrupt_once(original=threading.Thread.join):
            calls = {"n": 0}

            def join(self, timeout=None):
                if self.name.startswith("mobster-test-") and calls["n"] == 0:
                    started.wait(5)
                    calls["n"] += 1
                    raise KeyboardInterrupt
                return original(self, timeout)
            return join
        from unittest import mock
        with mock.patch.object(threading.Thread, "join", interrupt_once()):
            doc = suite.run()
        self.assertIsInstance(suite.interrupted, KeyboardInterrupt)
        self.assertEqual(aborted, [True])
        self.assertEqual([check["results"][0]["status"] for check in doc["checks"]], ["couldnt_run", "couldnt_run"])


if __name__ == "__main__":
    unittest.main()

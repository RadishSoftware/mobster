"""A suite across several simulators at once (`--sim` repeated, a matrix file, `--parallel`): every check runs on every
simulator, never more at once than asked or than the Mac can hold, and each simulator has its own results in
results.json, JUnit, the HTML report and the history. Offline: fake attempts on simulator device types only."""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from mobile_agent.testkit import history
from mobile_agent.testkit import reports as R
from mobile_agent.testkit import targets as T
from mobile_agent.testkit.suite import MAX_PARALLEL, Suite, machine_parallel_cap
from mobile_agent.tests.test_testing import Pool, result, validate_junit
from mobile_agent.tests.test_testing import suite_entry as entry

TYPES = ("iPhone 17 Pro", "iPhone SE (3rd generation)", "iPhone 17 Pro Max", "iPad Pro 13-inch (M4)")
GB = 1024 ** 3


class Fleet:
    """Attempts on simulators: each takes a free simulator of its type for a moment and counts how many are busy."""

    def __init__(self, fail=()):
        self.pool, self.fail = Pool(sims_per_type=4), set(fail)
        self.now = self.most = 0
        self.lock = threading.Lock()
        self.by_type = {}

    def __call__(self, check, record, *, label, video_name=None):
        sim = self.pool.acquire(check.device)
        with self.lock:
            self.now += 1
            self.most = max(self.most, self.now)
            self.by_type.setdefault(check.device, []).append(check.name)
        time.sleep(.03)
        with self.lock:
            self.now -= 1
        self.pool.release(check.device, sim)
        verdict = "failed" if (check.name, check.device) in self.fail else "passed"
        return result(verdict, "assertion" if verdict == "failed" else None,
                      device={"name": f"{check.device} · iOS 26.4", "type": check.device,
                              "runtime": "iOS 26.4", "udid": f"U-{check.device}"})


def run(parallel, cap, fail=(), checks=6, types=TYPES, progress=None):
    fleet = Fleet(fail)
    suite = Suite([entry(f"check{n}") for n in range(checks)], [T.simulator(t) for t in types], attempt=fleet,
                  parallel=parallel, cap=cap, retries=0, progress=progress)
    return fleet, suite.run()


class ParallelTests(unittest.TestCase):
    def test_every_check_runs_on_every_simulator_and_up_to_the_asked_number_run_at_once(self):
        fleet, doc = run(parallel=4, cap=8)
        self.assertEqual(doc["summary"]["passed"], 6 * len(TYPES))
        self.assertEqual(fleet.pool.overlaps, [])
        self.assertEqual(fleet.most, 4)
        self.assertTrue(all(len(names) == 6 for names in fleet.by_type.values()))
        self.assertEqual(doc["exitCode"], 0)

    def test_the_machines_limit_wins_over_parallel_and_the_run_says_so(self):
        lines = []
        fleet, doc = run(parallel=8, cap=2, progress=lines.append)
        self.assertEqual(fleet.most, 2)
        self.assertEqual(doc["summary"]["passed"], 6 * len(TYPES))
        self.assertTrue(any("runs 2" in line and "MOBSTER_TEST_MAX_PARALLEL" in line for line in lines), lines)

    def test_a_limit_the_request_is_under_says_nothing(self):
        lines = []
        run(parallel=2, cap=4, progress=lines.append)
        self.assertEqual(lines and [line for line in lines if "MOBSTER_TEST_MAX_PARALLEL" in line], [])

    def test_the_limit_follows_cores_and_memory_and_an_override(self):
        self.assertEqual(machine_parallel_cap(10, 16 * GB, {}), 4)       # 16 GB holds four simulators
        self.assertEqual(machine_parallel_cap(10, 64 * GB, {}), 5)       # then cores: one per two
        self.assertEqual(machine_parallel_cap(32, 192 * GB, {}), MAX_PARALLEL)
        self.assertEqual(machine_parallel_cap(2, 2 * GB, {}), 1)         # never zero
        self.assertEqual(machine_parallel_cap(10, 16 * GB, {"MOBSTER_TEST_MAX_PARALLEL": "6"}), 6)
        self.assertEqual(machine_parallel_cap(10, 16 * GB, {"MOBSTER_TEST_MAX_PARALLEL": "99"}), MAX_PARALLEL)
        self.assertEqual(machine_parallel_cap(10, 16 * GB, {"MOBSTER_TEST_MAX_PARALLEL": "nope"}), 4)


class PerDeviceResultTests(unittest.TestCase):
    def setUp(self):
        self.fleet, self.doc = run(parallel=3, cap=8, fail={("check1", "iPhone SE (3rd generation)")}, checks=3)
        self.report = R.document(self.doc, suite_id="s1", options={}, version="9")

    def test_results_json_has_a_cell_and_a_device_for_each_simulator(self):
        self.assertEqual([device["name"] for device in self.doc["devices"]],
                         [f"{name} · iOS 26.4" for name in TYPES])
        self.assertEqual(self.doc["summary"]["failed"], 1)
        self.assertEqual(self.doc["exitCode"], 1)
        bad = [(check["name"], cell["device"]) for check in self.doc["checks"] for cell in check["results"]
               if cell["status"] == "failed"]
        self.assertEqual(bad, [("check1", "iPhone SE (3rd generation) · iOS 26.4")])
        self.assertEqual(len({cell["deviceKey"] for check in self.doc["checks"] for cell in check["results"]}), 4)

    def test_junit_has_one_testsuite_per_simulator_and_the_failure_sits_in_its_own(self):
        data = R.junit(self.report)
        validate_junit(data)
        import xml.etree.ElementTree as ET
        suites = ET.fromstring(data).findall("testsuite")
        self.assertEqual(len(suites), 4)
        failing = [suite.get("name") for suite in suites if suite.get("failures") != "0"]
        self.assertEqual(failing, ["iPhone SE (3rd generation) · iOS 26.4"])
        self.assertTrue(all(suite.get("tests") == "3" for suite in suites))

    def test_the_html_report_names_every_simulator(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = R.render_html(self.report, tmp)
        for name in TYPES:
            self.assertIn(name, page)

    def test_the_history_keeps_each_simulators_runs_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            history.record(Path(tmp), self.report)
            found, ok = history.load(Path(tmp))
        self.assertTrue(ok)
        self.assertEqual(len(found), 3 * len(TYPES))
        small = [item for item in found.values() if item["device"].startswith("iPhone SE")]
        self.assertEqual(len(small), 3)
        runs = {item["name"]: item["runs"][-1]["status"] for item in small}
        self.assertEqual(runs["check1"], "failed")
        self.assertEqual(runs["check0"], "passed")


if __name__ == "__main__":
    unittest.main()

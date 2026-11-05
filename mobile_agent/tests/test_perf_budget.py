import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "perf_budget.py"


def load():
    spec = importlib.util.spec_from_file_location("perf_budget", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(SCRIPT.is_file(), "the CI scripts aren't in this checkout")
class PerfBudgetTests(unittest.TestCase):
    def test_the_bundle_check_counts_what_index_html_loads_up_front_and_nothing_lazy(self):
        budget = load()
        with tempfile.TemporaryDirectory() as folder:
            dist = Path(folder)
            (dist / "assets").mkdir()
            (dist / "assets/index-a.js").write_bytes(b"a" * 1000)
            (dist / "assets/react-b.js").write_bytes(b"b" * 400)
            (dist / "assets/index-c.css").write_bytes(b"c" * 300)
            (dist / "assets/lazy-d.js").write_bytes(b"d" * 99999)
            (dist / "index.html").write_text(
                '<script type="module" crossorigin src="/assets/index-a.js"></script>'
                '<link rel="modulepreload" crossorigin href="/assets/react-b.js">'
                '<link rel="stylesheet" crossorigin href="/assets/index-c.css">'
                '<link rel="icon" href="/favicon.png">')
            measured = budget.bundle(dist)
        self.assertEqual(measured["dashboard_startup_js_bytes"], 1400)
        self.assertEqual(measured["dashboard_startup_css_bytes"], 300)
        self.assertEqual(measured["dashboard_largest_startup_chunk_bytes"], 1000)

    def test_a_value_over_its_ceiling_is_reported_and_one_under_it_is_not(self):
        budget = load()
        ceilings = {"agent_import_cpu_ms": 100, "agent_ready_seconds": 2}
        with mock.patch.dict(os.environ, {"MOBSTER_TEST_TIME_SCALE": "1"}):  # an idle Mac: no ceiling stretches
            self.assertEqual(budget.check({"agent_import_cpu_ms": 101, "agent_ready_seconds": 1}, ceilings),
                             ["agent_import_cpu_ms"])
            self.assertEqual(budget.check({"agent_import_cpu_ms": 100, "agent_ready_seconds": 2}, ceilings), [])

    def test_a_busy_mac_allows_cpu_time_one_and_a_half_times_and_never_more_modules(self):
        budget = load()
        ceilings = {"agent_import_modules": 300, "agent_import_cpu_ms": 320}
        with mock.patch.dict(os.environ, {"MOBSTER_TEST_TIME_SCALE": "4"}):
            self.assertEqual(budget.check({"agent_import_modules": 300, "agent_import_cpu_ms": 480}, ceilings), [])
            self.assertEqual(budget.check({"agent_import_modules": 301, "agent_import_cpu_ms": 481}, ceilings),
                             ["agent_import_modules", "agent_import_cpu_ms"])
        with mock.patch.dict(os.environ, {"MOBSTER_TEST_TIME_SCALE": "1"}):
            self.assertEqual(budget.check({"agent_import_cpu_ms": 321}, ceilings), ["agent_import_cpu_ms"])

    def test_every_measured_name_has_a_ceiling(self):
        budget = load()
        for name in ("dashboard_startup_js_bytes", "dashboard_startup_js_gzip_bytes", "dashboard_startup_css_bytes",
                     "dashboard_largest_startup_chunk_bytes", "agent_import_modules", "agent_import_cpu_ms",
                     "agent_ready_seconds"):
            self.assertIn(name, budget.BUDGETS)


if __name__ == "__main__":
    unittest.main()

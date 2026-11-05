import json
import tempfile
import unittest
from pathlib import Path

from mobile_agent.testkit import history
from mobile_agent.testkit.cli import history as history_command
from mobile_agent.testkit.pr_report import comment
from mobile_agent.verify import perf
from mobile_agent.verify.checks import CheckError, check_from_dict, check_to_dict, dump_check, load_check


def measured(launch=800, memory=200.0, hangs=0):
    return {"launch": {"interactiveMs": launch, "foregroundMs": launch - 100},
            "memory": {"peakMb": memory, "p95Mb": memory - 5, "cpuPeakPct": 30.0, "samples": 20},
            "hangs": {"count": hangs, "longestMs": 300 if hangs else 0, "thresholdMs": 250}}


class MeasureTests(unittest.TestCase):
    def test_ps_output_is_parsed_and_anything_else_is_not_a_sample(self):
        self.assertEqual(perf.parse_ps(" 219136  12.5\n"), (214.0, 12.5))
        for text in ("", "219136", "abc 1.0", "0 1.0", "100 nan", "1 2 3"):
            with self.subTest(text=text):
                self.assertIsNone(perf.parse_ps(text))

    def test_memory_is_the_peak_and_the_nearest_rank_p95(self):
        samples = [(float(mb), 1.0) for mb in range(1, 101)]
        found = perf.memory_summary(samples)
        self.assertEqual((found["peakMb"], found["p95Mb"], found["samples"]), (100.0, 95.0, 100))
        self.assertIsNone(perf.memory_summary([]))

    def test_the_sampler_reads_the_current_process_and_survives_a_failing_read(self):
        pids, reads = iter([7, 7, 9, 9, 9, 9]), []

        def sample(pid):
            reads.append(pid)
            if len(reads) == 2:
                raise OSError("ps failed")
            return (100.0 + pid, 2.0)
        sampler = perf.Sampler(lambda: next(pids), sample=sample, every=.01)
        sampler.start()
        while len(reads) < 4:
            pass
        found = sampler.stop()
        self.assertEqual(reads[:1], [7])
        self.assertGreaterEqual(found["samples"], 2)
        self.assertEqual(found["peakMb"], 109.0)

    def test_a_fast_run_still_gets_a_reading_when_the_sampler_stops(self):
        # 26 Oct 2026: a launch-only check of about 1.3 s got 0 samples once, as the app's pid wasn't known at the start
        pids = iter([None, 7])
        sampler = perf.Sampler(lambda: next(pids), sample=lambda pid: (120.0, 3.0), every=60)
        sampler.start()
        self.assertEqual(sampler.stop(), {"peakMb": 120.0, "p95Mb": 120.0, "cpuPeakPct": 3.0, "samples": 1})

    def test_a_process_that_has_gone_is_no_sample(self):
        self.assertIsNone(perf.ps_sample(2 ** 22 + 1,
                                         run=lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})()))

    def test_hangs_count_only_reports_of_250_ms_or_longer(self):
        lines = ["Hang detected: 0.52s (debugger attached, not reporting)", "Hang detected: 0.249s",
                 "Hang detected: 0.250s", "nothing here", "[x] Hang detected: 1.5 s"]
        self.assertEqual(perf.hang_summary(lines), {"count": 3, "longestMs": 1500, "thresholdMs": 250})
        self.assertEqual(perf.hang_summary([])["count"], 0)

    def test_what_a_simulator_cannot_measure_is_named(self):
        sim = perf.build(launch={}, sampler_summary=None, hangs=None, simulator=True)
        self.assertEqual(set(sim["notMeasured"]), {"firstFrame", "droppedFrames"})
        phone = perf.build(launch={}, sampler_summary=None, hangs=None, simulator=False)
        self.assertIn("iphoneMemory", phone["notMeasured"])


class BudgetTests(unittest.TestCase):
    def test_a_limit_is_broken_only_above_it(self):
        breaches, unmeasured = perf.judge({"launch_ms": 800, "memory_mb": 200, "hangs": 0}, measured())
        self.assertEqual((breaches, unmeasured), ([], []))
        breaches, _ = perf.judge({"launch_ms": 799, "memory_mb": 199, "hangs": 0}, measured(hangs=1))
        self.assertEqual([item.key for item in breaches], ["launch_ms", "memory_mb", "hangs"])
        self.assertIn("800 ms", breaches[0].message)

    def test_a_limit_on_a_number_that_was_not_measured_never_passes(self):
        nothing = perf.build(launch={}, sampler_summary=None, hangs=None, simulator=True)
        breaches, unmeasured = perf.judge({"launch_ms": 5000, "memory_mb": 900, "hangs": 3}, nothing)
        self.assertEqual(breaches, [])
        self.assertEqual([item.key for item in unmeasured], ["launch_ms", "memory_mb", "hangs"])
        self.assertEqual(perf.judge({}, nothing), ([], []))
        self.assertEqual(perf.judge({"hangs": 1}, None)[1][0].key, "hangs")

    def test_keys_parse_dump_and_load_back(self):
        data = {"app": {"bundle": "com.example.app"}, "budget": {"launch_ms": 1500, "memory_mb": 250.5, "hangs": 0,
                                                                   "max_usd": 0.5}}
        check = check_from_dict(data)
        self.assertEqual(check.perf_budget, {"launch_ms": 1500, "memory_mb": 250.5, "hangs": 0})
        self.assertEqual(check_to_dict(check)["budget"], {"max_usd": 0.5, **check.perf_budget})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.yaml"
            path.write_text(dump_check(check), encoding="utf-8")
            self.assertEqual(load_check(path).perf_budget, check.perf_budget)

    def test_bad_limits_are_errors_that_name_the_key(self):
        for budget, word in (({"launch_ms": 0}, "budget.launch_ms"), ({"launch_ms": "fast"}, "budget.launch_ms"),
                             ({"memory_mb": -1}, "budget.memory_mb"), ({"hangs": 1.5}, "budget.hangs"),
                             ({"hangs": True}, "budget.hangs"), ({"hangs": -1}, "budget.hangs"),
                             ({"cpu": 3}, "cpu")):
            with self.subTest(budget=budget), self.assertRaises(CheckError) as caught:
                check_from_dict({"app": {"bundle": "com.example.app"}, "budget": budget})
            self.assertIn(word, str(caught.exception))

    def test_the_limits_are_for_check_files_not_tool_arguments(self):
        with self.assertRaises(CheckError):
            check_from_dict({"bundle_id": "com.example.app", "launch_ms": 500})


def doc(suite, at, results):
    return {"suite": suite, "startedAt": at, "checks": [{"name": "Feed", "file": "feed.yaml", "results": results}]}


def result(status="passed", perf_=None, **extra):
    return {"status": status, "deviceKey": "d", "device": "iPhone", "durationMs": 1000, "attempts": 1,
            "perf": perf_, **extra}


class TrendTests(unittest.TestCase):
    def run_history(self, root, launches, status="passed"):
        last = None
        for index, launch in enumerate(launches):
            last = history.record(root, doc(f"s{index:02d}", 1000 + index, [result(status, measured(launch=launch))]))
        return last

    def test_a_launch_20_percent_over_the_last_passes_is_flagged(self):
        with tempfile.TemporaryDirectory() as root:
            last = self.run_history(root, [800] * 6 + [961])
            trend = last["checks"][0]["results"][0]["history"]["perf"]
            self.assertEqual(trend["launchMs"]["arrow"], "↑")
            self.assertEqual(trend["launchMs"]["baseline"], 800)
            self.assertEqual(trend["memoryMb"]["arrow"], "→")

    def test_exactly_20_percent_is_steady_and_faster_is_marked(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(self.run_history(root, [800] * 5 + [960])["checks"][0]["results"][0]
                             ["history"]["perf"]["launchMs"]["arrow"], "→")
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(self.run_history(root, [800] * 5 + [500])["checks"][0]["results"][0]
                             ["history"]["perf"]["launchMs"]["arrow"], "↓")

    def test_fewer_than_three_earlier_passes_gives_no_arrow(self):
        with tempfile.TemporaryDirectory() as root:
            last = self.run_history(root, [800, 800, 2000])
            self.assertEqual(last["checks"][0]["results"][0]["history"]["perf"], {})

    def test_failed_runs_are_not_the_baseline(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_history(root, [3000] * 4, status="failed")
            for index in range(4):
                history.record(root, doc(f"p{index}", 2000 + index, [result("passed", measured(launch=800))]))
            last = history.record(root, doc("last", 3000, [result("passed", measured(launch=820))]))
            self.assertEqual(last["checks"][0]["results"][0]["history"]["perf"]["launchMs"]["arrow"], "→")

    def test_more_hangs_than_the_median_is_a_regression_and_fewer_is_not(self):
        runs = [{"status": "passed", "perf": {"hangs": 0}}] * 4
        self.assertEqual(perf.trend(runs + [{"status": "passed", "perf": {"hangs": 1}}])["hangs"]["arrow"], "↑")
        self.assertEqual(perf.trend(runs + [{"status": "passed", "perf": {"hangs": 0}}])["hangs"]["arrow"], "→")

    def test_a_budget_the_run_could_not_measure_is_no_flake(self):
        missed = result("needs_review", reason={"class": "budget_unmeasured", "message": "Memory was not measured."})
        broken = result("failed", reason={"class": "assertion", "message": "x"})
        with tempfile.TemporaryDirectory() as root:
            for index, item in enumerate([result(), result(), missed, result()]):
                history.record(root, doc(f"s{index}", index, [item]))
            found = history.stats(next(iter(history.load(root)[0].values()))["runs"])
            self.assertEqual((found["flakeScore"], found["passRate"], found["runs"], found["unmeasured"]),
                             (0.0, 1.0, 3, 1))
            self.assertEqual(found["outcomes"], ["passed", "passed", "needs_review", "passed"])
        with tempfile.TemporaryDirectory() as root:
            for index, item in enumerate([result(), result(), broken, result()]):
                history.record(root, doc(f"s{index}", index, [item]))
            self.assertGreater(history.stats(next(iter(history.load(root)[0].values()))["runs"])["flakeScore"], 0)

    def test_history_keeps_only_measured_finite_numbers_from_a_hand_edited_file(self):
        with tempfile.TemporaryDirectory() as root:
            history.record(root, doc("s1", 1, [result("passed", measured())]))
            path = history.path_of(root)
            data = json.loads(path.read_text())
            run = next(iter(data["checks"].values()))["runs"][0]
            run["perf"] = {"launchMs": "fast", "memoryMb": -4, "hangs": True, "other": 1}
            path.write_text(json.dumps(data))
            loaded, ok = history.load(root)
            self.assertEqual(next(iter(loaded.values()))["runs"][0]["perf"], {})
            self.assertTrue(ok)

    def test_the_line_shows_the_numbers_and_the_arrows(self):
        trend = {"launchMs": {"arrow": "↑"}, "memoryMb": {"arrow": "→"}}
        self.assertEqual(perf.line({"launchMs": 812, "memoryMb": 214.4, "hangs": 0}, trend),
                         "launch 812 ms ↑, memory 214 MB →, hangs 0")
        self.assertEqual(perf.line({}), "")

    def test_the_history_command_and_the_pr_comment_show_the_line(self):
        import io
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / ".mobster").mkdir()
            self.run_history(root, [800] * 6 + [1000])
            out = io.StringIO()
            history_command(SimpleNamespace(json=False), [root], out=out)
            self.assertIn("Last run: launch 1,000 ms ↑, memory 200 MB →, hangs 0 →.", out.getvalue())
        row = {"check": "Feed", "device": "iPhone", "status": "passed", "perf": "launch 1,000 ms ↑"}
        text = comment({"headline": "ok", "rows": [row]})
        self.assertIn("- Feed on iPhone: launch 1,000 ms ↑", text)


if __name__ == "__main__":
    unittest.main()

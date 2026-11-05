import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "soak_fleet.py"
if not SCRIPT.is_file():
    raise unittest.SkipTest("scripts/soak_fleet.py is not part of this copy")
spec = importlib.util.spec_from_file_location("soak_fleet", SCRIPT)
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)


class Percentiles(unittest.TestCase):
    def test_nearest_rank(self):
        values = list(range(1, 101))
        self.assertEqual((soak.percentile(values, .5), soak.percentile(values, .9), soak.percentile(values, .99)),
                         (50, 90, 99))
        self.assertEqual(soak.percentile([7], .99), 7)
        self.assertIsNone(soak.percentile([], .5))


class DryRun(unittest.TestCase):
    def run_main(self, *args):
        out = io.StringIO()
        code = soak.main(["--dry-run", *args], out=out)
        return code, out.getvalue()

    def test_a_fake_fleet_of_n_phones_gets_a_row_per_phone_and_step(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "soak.json"
            code, table = self.run_main("--phones", "7", "--duration", "60", "--interval", "5", "--json", str(path))
            report = json.loads(path.read_text())
        self.assertEqual(code, 0)
        self.assertEqual(report["meta"]["phones"], 7)
        self.assertEqual(len(report["phones"]), 7)
        for phone in report["phones"]:
            steps = {row["step"]: row for row in phone["steps"]}
            self.assertEqual(steps["locked"]["n"], 12)
            self.assertLessEqual(steps["locked"]["p50_ms"], steps["locked"]["p90_ms"])
            self.assertLessEqual(steps["locked"]["p90_ms"], steps["locked"]["p99_ms"])
            self.assertEqual(phone["watchdog_restarts"], 0)
        self.assertIn("| Phone | Step | n | p50 ms | p90 ms | p99 ms | Failure rate | Watchdog restarts |", table)

    def test_the_flaky_phone_shows_a_failure_rate(self):
        _, table = self.run_main("--phones", "5", "--duration", "600", "--interval", "5")
        flaky = [line for line in table.splitlines() if line.startswith("| fake-5 | screenshot")][0]
        healthy = [line for line in table.splitlines() if line.startswith("| fake-1 | screenshot")][0]
        self.assertNotIn("| 0.0% |", flaky)
        self.assertIn("| 0.0% |", healthy)

    def test_the_same_seed_gives_the_same_numbers(self):
        self.assertEqual(self.run_main("--phones", "3", "--duration", "30")[1].split("\n", 1)[1],
                         self.run_main("--phones", "3", "--duration", "30")[1].split("\n", 1)[1])

    def test_a_bad_count_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_main("--phones", "0")


class Lock(unittest.TestCase):
    def test_the_lock_is_taken_and_released(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = soak.PhoneLock(Path(folder) / "phone")
            self.assertTrue(lock.acquire())
            self.assertTrue((Path(folder) / "phone").is_dir())
            self.assertFalse(soak.PhoneLock(Path(folder) / "phone").acquire())
            lock.release()
            self.assertFalse((Path(folder) / "phone").exists())

    def test_a_held_lock_stops_the_run_before_any_read(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "phone").mkdir()
            reads = []
            with patch.object(soak.urllib.request, "urlopen", side_effect=lambda *a, **k: reads.append(a)):
                code = soak.main(["--phone", "alex=http://127.0.0.1:8100", "--duration", "1",
                                  "--lock-dir", str(Path(folder) / "phone")], out=io.StringIO())
            self.assertEqual(code, 3)
            self.assertEqual(reads, [])
            self.assertTrue((Path(folder) / "phone").exists())


class ReadOnly(unittest.TestCase):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def test_a_real_phone_makes_only_get_requests_with_bounded_timeouts(self):
        sent = []

        def urlopen(request, timeout=None):
            sent.append((request.get_method(), request.full_url, timeout, request.data))
            return self.Response(json.dumps({"value": False, "sessionId": "S1"}).encode())

        with tempfile.TemporaryDirectory() as folder, patch.object(soak.urllib.request, "urlopen", urlopen):
            code = soak.main(["--phone", "alex=http://127.0.0.1:8100", "--duration", "0.2", "--interval", "0.1",
                              "--lock-dir", str(Path(folder) / "phone")], out=io.StringIO())
        self.assertEqual(code, 0)
        self.assertTrue(sent)
        self.assertEqual({method for method, *_ in sent}, {"GET"})
        self.assertTrue(all(data is None for *_, data in sent))
        self.assertTrue(all(timeout is not None and 0 < timeout <= 10 for _, _, timeout, _ in sent))
        paths = {url.split("8100", 1)[1] for _, url, _, _ in sent}
        self.assertTrue(paths <= {"/status", "/wda/locked", "/wda/activeAppInfo", "/screenshot",
                                  "/session/S1/source?format=json"}, paths)

    def test_no_session_means_no_source_read_and_none_is_created(self):
        phone = soak.RealPhone("alex", "http://127.0.0.1:8100")
        self.assertEqual(phone.step("source"), (None, None))

    def test_only_loopback_addresses_are_accepted(self):
        with self.assertRaises(Exception):
            soak.parse_phone("x=http://192.0.2.1:8100")


class Owner(unittest.TestCase):
    def test_a_phone_whose_owner_picks_it_up_is_dropped_at_once(self):
        class Picked(soak.FakePhone):
            def __init__(self):
                super().__init__("alex", 1)
                self.passes = 0

            def step(self, name):
                if name == "locked":
                    self.passes += 1
                    return True, self.passes < 3
                return super().step(name)

        clock, sleep = soak.virtual_clock()
        phone = Picked()
        stats = soak.run([phone], 600, 5, clock=clock, sleep=sleep, timer=soak.fake_timer)[0]
        self.assertEqual(stats.stopped, "owner_active")
        self.assertEqual(phone.passes, 3)
        self.assertIn("stopped: owner_active", soak.markdown([stats.report()], {
            "when": "x", "phones": 1, "duration_s": 600, "interval_s": 5, "mode": "fake"}))


class Discovery(unittest.TestCase):
    def test_serve_lists_usb_phones_primary_first(self):
        records = [{"kind": "usb", "state": "ready", "name": "B"}, {"kind": "simulator", "state": "ready", "name": "S"},
                   {"kind": "usb", "state": "busy", "name": "A", "primary": True},
                   {"kind": "usb", "state": "disconnected", "name": "C"}]

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch.object(soak.urllib.request, "urlopen", lambda *a, **k: Response(json.dumps(records).encode())):
            self.assertEqual(soak.discover("http://127.0.0.1:8765"),
                             [("A", "http://127.0.0.1:8100"), ("B", "http://127.0.0.1:8101")])


if __name__ == "__main__":
    unittest.main()

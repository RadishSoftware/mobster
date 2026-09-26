"""USB-phone pool specs and the per-device work queue. No real devices or network."""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent.tests.timing import bound
from mobile_agent.pool import (DevicePool, NoDeviceAvailable, WdaDeviceSpec, default_mjpeg_url,
                               run_on_devices, wda_specs_from_config, wda_specs_from_env)

PRO = "00008130-001A2B3C4D5E6F70"
XR = "00008020-000A1B2C3D4E5F60"


class FakeLease:
    opened = []

    def __init__(self, identity):
        self.identity = identity
        self.closed = False
        FakeLease.opened.append(identity)

    def close(self):
        self.closed = True


def phones(probe=None, **kwargs):
    specs = [WdaDeviceSpec("15pro", "http://127.0.0.1:8100", udid=PRO, truth="iphone15pro"),
             WdaDeviceSpec("xr", "http://127.0.0.1:8101/", udid=XR)]
    return DevicePool(specs, probe=probe or (lambda url, timeout=3: True), **kwargs)


class WdaSpecTests(unittest.TestCase):
    def test_mjpeg_port_pairs_with_the_wda_port(self):
        self.assertEqual(default_mjpeg_url("http://127.0.0.1:8100"), "http://127.0.0.1:9100")
        self.assertEqual(default_mjpeg_url("http://127.0.0.1:8101"), "http://127.0.0.1:9101")
        spec = WdaDeviceSpec("xr", "http://127.0.0.1:8101/")
        self.assertEqual(spec.wda_url, "http://127.0.0.1:8101")
        self.assertEqual(spec.mjpeg_url, "http://127.0.0.1:9101")
        self.assertEqual(spec.identity, "http://127.0.0.1:8101")

    def test_a_wda_phone_is_its_own_kind(self):
        self.assertEqual(WdaDeviceSpec("a", "http://127.0.0.1:8100").kind, "wda")

    def test_public_view_carries_no_url_or_udid(self):
        public = WdaDeviceSpec("15pro", "http://127.0.0.1:8100", udid=PRO, truth="iphone15pro").public()
        self.assertEqual(public, {"id": "15pro", "label": "15pro", "kind": "wda", "truth": "iphone15pro"})

    def test_rejects_bad_urls_and_udids(self):
        for url in ("ftp://x:1", "http://user:pw@127.0.0.1:8100", "127.0.0.1:8100", "http://127.0.0.1:8100/?a=1"):
            with self.assertRaises(ValueError):
                WdaDeviceSpec("a", url)
        with self.assertRaises(ValueError):
            WdaDeviceSpec("a", "http://127.0.0.1:8100", udid="not a udid")

    def test_env_parsing(self):
        specs = wda_specs_from_env(f"id=15pro,udid={PRO},wda=http://127.0.0.1:8100,truth=iphone15pro;"
                                   f" id=xr,udid={XR},wda=http://127.0.0.1:8101,mjpeg=http://127.0.0.1:9201;")
        self.assertEqual([s.id for s in specs], ["15pro", "xr"])
        self.assertEqual(specs[0].mjpeg_url, "http://127.0.0.1:9100")
        self.assertEqual(specs[1].mjpeg_url, "http://127.0.0.1:9201")
        self.assertEqual(specs[0].truth, "iphone15pro")
        self.assertEqual(wda_specs_from_env(""), [])
        for bad in ("udid=x", "wda=http://127.0.0.1:8100,wda=http://127.0.0.1:8101", "wda", "port=1,wda=http://a:1"):
            with self.assertRaises(ValueError):
                wda_specs_from_env(bad)

    def test_config_devices_with_wda_urls(self):
        config = SimpleNamespace(devices=[{"id": "a", "wdaUrl": "http://127.0.0.1:8100"},
                                          {"id": "b", "wdaUrl": "http://127.0.0.1:8101", "udid": XR}])
        self.assertEqual([s.wda_url for s in wda_specs_from_config(config)],
                         ["http://127.0.0.1:8100", "http://127.0.0.1:8101"])
        with patch.dict("os.environ", {"MOBSTER_WDA_DEVICES": "id=e,wda=http://127.0.0.1:8102"}):
            self.assertEqual([s.id for s in wda_specs_from_config(SimpleNamespace(devices=None))], ["e"])


class WdaPoolTests(unittest.TestCase):
    def setUp(self):
        FakeLease.opened = []
        patcher = patch("mobile_agent.pool.Lease.device", side_effect=FakeLease)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_leases_by_the_url_the_server_and_harness_use(self):
        pool = phones()
        first, second = pool.acquire("a", timeout=1), pool.acquire("b", timeout=1)
        self.assertEqual({first.id, second.id}, {"15pro", "xr"})
        self.assertEqual(sorted(FakeLease.opened), ["http://127.0.0.1:8100", "http://127.0.0.1:8101"])
        self.assertEqual({first.wda_url, second.wda_url}, {"http://127.0.0.1:8100", "http://127.0.0.1:8101"})
        pool.close()

    def test_duplicate_urls_or_udids_are_refused(self):
        with self.assertRaises(ValueError):
            DevicePool([WdaDeviceSpec("a", "http://127.0.0.1:8100"), WdaDeviceSpec("b", "http://127.0.0.1:8100/")])
        with self.assertRaises(ValueError):
            DevicePool([WdaDeviceSpec("a", "http://127.0.0.1:8100", udid=PRO),
                        WdaDeviceSpec("b", "http://127.0.0.1:8101", udid=PRO)])

    def test_probe_gets_the_url(self):
        seen = []
        pool = phones(probe=lambda url, timeout=3: seen.append(url))
        pool.acquire_device("a", "xr", timeout=1)
        self.assertEqual(seen, ["http://127.0.0.1:8101"])
        pool.close()

    def test_default_probe_speaks_wda(self):
        pool = DevicePool([WdaDeviceSpec("a", "http://127.0.0.1:8100")])
        with patch("mobile_agent.pool.probe_wda", return_value={"ready": True}) as wda:
            self.assertEqual(pool.acquire("x", timeout=1).id, "a")
        wda.assert_called_once_with("http://127.0.0.1:8100")
        pool.close()

    def test_probe_wda_reads_status_only(self):
        from mobile_agent import pool as module

        class FakeHTTP:
            calls = []

            def __init__(self, url):
                self.url = url

            def request(self, method, path, body=None, timeout=20):
                FakeHTTP.calls.append((method, path))
                return {"value": {"ready": FakeHTTP.ready}, "sessionId": None}

            def close(self):
                pass

        with patch("mobile_agent.transport.HTTP", FakeHTTP):
            FakeHTTP.ready = True
            module.probe_wda("http://127.0.0.1:8100")
            FakeHTTP.ready = False
            with self.assertRaises(RuntimeError):
                module.probe_wda("http://127.0.0.1:8100")
        self.assertEqual(FakeHTTP.calls, [("GET", "/status")] * 2)

    def test_acquire_device_waits_for_that_phone_only(self):
        pool = phones()
        pool.acquire_device("a", "15pro", timeout=1)
        with self.assertRaises(NoDeviceAvailable):
            pool.acquire_device("b", "15pro", timeout=.2)
        self.assertEqual(pool.acquire_device("b", "xr", timeout=.2).id, "xr")
        threading.Timer(.2, pool.release, args=("a",)).start()
        self.assertEqual(pool.acquire_device("c", "15pro", timeout=2).id, "15pro")
        with self.assertRaises(ValueError):
            pool.acquire_device("d", "nope")
        pool.close()

    def test_acquire_device_fails_fast_on_an_unreachable_phone(self):
        def probe(url, timeout=3):
            if url.endswith("8101"):
                raise RuntimeError("WebDriverAgent is not ready")

        pool = phones(probe=probe)
        started = time.monotonic()
        with self.assertRaises(NoDeviceAvailable):
            pool.acquire_device("a", "xr", timeout=30)
        self.assertLess(time.monotonic() - started, bound(1))
        pool.close()


class RunOnDevicesTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("mobile_agent.pool.Lease.device", side_effect=FakeLease)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_phones_run_concurrently_and_never_share(self):
        pool = phones()
        holding, overlap, peak, lock = {}, [], [0], threading.Lock()

        def work(spec, job):
            with lock:
                self.assertNotIn(spec.id, holding)
                holding[spec.id] = job
                peak[0] = max(peak[0], len(holding))
            time.sleep(.05)
            with lock:
                del holding[spec.id]
            return job * 10

        # Overlap is proven by peak == 2; wall-clock bounds flaked on shared CI runners.
        results = run_on_devices(pool, list(range(8)), work)
        self.assertEqual(sorted(r for _, _, r in results), [j * 10 for j in range(8)])
        self.assertEqual(peak[0], 2)
        self.assertEqual({spec.id for _, spec, _ in results}, {"15pro", "xr"})
        pool.close()

    def test_jobs_run_only_on_eligible_phones(self):
        pool = phones()
        # Ground truth exists only for the 15 Pro: truth-graded jobs stay on it.
        eligible = lambda spec, job: job["any"] or spec.truth == "iphone15pro"
        jobs = [{"id": i, "any": i % 2 == 0} for i in range(6)]
        results = run_on_devices(pool, jobs, lambda spec, job: spec.id, eligible=eligible)
        for job, spec, outcome in results:
            if not job["any"]:
                self.assertEqual(outcome, "15pro")
        self.assertEqual(len(results), 6)
        pool.close()

    def test_a_job_no_phone_can_run_is_skipped(self):
        pool = phones()
        results = run_on_devices(pool, ["x"], lambda spec, job: 1, eligible=lambda spec, job: False)
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0][1])
        self.assertIsInstance(results[0][2], NoDeviceAvailable)
        pool.close()

    def test_a_dead_phone_hands_its_jobs_to_the_other(self):
        def probe(url, timeout=3):
            if url.endswith("8101"):
                raise RuntimeError("WebDriverAgent is not ready")

        pool = phones(probe=probe)
        results = run_on_devices(pool, list(range(5)), lambda spec, job: spec.id)
        self.assertEqual(sorted(job for job, _, _ in results), list(range(5)))
        self.assertTrue(all(outcome == "15pro" for _, _, outcome in results))
        pool.close()

    def test_a_failing_job_is_recorded_and_the_phone_is_reprobed(self):
        probes = []
        pool = phones(probe=lambda url, timeout=3: probes.append(url), health_interval=3600)

        def work(spec, job):
            if job == 0:
                raise RuntimeError("boom")
            return "ok"

        results = run_on_devices(pool, [0, 1, 2], work, eligible=lambda spec, job: spec.id == "15pro")
        outcomes = {job: outcome for job, _, outcome in results}
        self.assertIsInstance(outcomes[0], RuntimeError)
        self.assertEqual(outcomes[1], "ok")
        self.assertGreaterEqual(probes.count("http://127.0.0.1:8100"), 2)
        pool.close()


if __name__ == "__main__":
    unittest.main()

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent.pool import (
    DevicePool,
    WdaDeviceSpec,
    default_mjpeg_url,
    probe_wda,
    wda_specs_from_config,
    wda_specs_from_env,
)
from mobile_agent.tests.fakes import DeviceLease

PRO = "00008130-001A2B3C4D5E6F70"
XR = "00008020-000A1B2C3D4E5F60"


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
        patcher = patch("mobile_agent.pool.Lease.device", side_effect=DeviceLease)
        self.lease = patcher.start()
        self.addCleanup(patcher.stop)

    def test_leases_by_the_url_the_server_and_harness_use(self):
        pool = phones()
        first, second = pool.acquire("a", timeout=1), pool.acquire("b", timeout=1)
        self.assertEqual({first.id, second.id}, {"15pro", "xr"})
        self.assertEqual(sorted(call.args[0] for call in self.lease.call_args_list),
                         ["http://127.0.0.1:8100", "http://127.0.0.1:8101"])
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
        pool = DevicePool([WdaDeviceSpec("xr", "http://127.0.0.1:8101/", udid=XR)],
                          probe=lambda url, timeout=3: seen.append(url))
        pool.acquire("a", timeout=1)
        self.assertEqual(seen, ["http://127.0.0.1:8101"])
        pool.close()

    def test_the_default_probe_reads_wda_status_only(self):
        calls = []

        class FakeHTTP:
            ready = True

            def __init__(self, url):
                self.url = url

            def request(self, method, path, body=None, timeout=20):
                calls.append((self.url, method, path))
                return {"value": {"ready": FakeHTTP.ready}, "sessionId": None}

            def close(self):
                pass

        with patch("mobile_agent.transport.HTTP", FakeHTTP):
            pool = DevicePool([WdaDeviceSpec("a", "http://127.0.0.1:8100")])
            self.assertEqual(pool.acquire("x", timeout=1).id, "a")
            pool.close()
            FakeHTTP.ready = False
            with self.assertRaises(RuntimeError):
                probe_wda("http://127.0.0.1:8100")
        self.assertEqual(calls, [("http://127.0.0.1:8100", "GET", "/status")] * 2)


if __name__ == "__main__":
    unittest.main()

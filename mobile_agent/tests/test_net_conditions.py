"""Network conditions (verify/netproxy.py, netstubs.py, netcerts.py, netshim.py, netrun.py, conditions.py): the proxy
shaping and answering real loopback connections, the check keys, the shim build and the Applier's cleanup. Loopback
sockets and a local openssl only: no simulator, phone or network."""

import contextlib
import json
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from mobile_agent.tests.test_a11y_levels import CLEAN
from mobile_agent.tests.test_verify_runner import BUNDLE, Driver, Manager, RunnerCase
from mobile_agent.verify import conditions, netcerts, netrun, netshim, netstubs, runner
from mobile_agent.verify.assertions import CheckError
from mobile_agent.verify.checks import check_from_dict, dump_check
from mobile_agent.verify.netproxy import PROFILES, Profile, Proxy
from mobile_agent.verify.runner import VerifyRun

STUB = {"match": {"host": "api.example.com", "path_prefix": "/v1"}, "respond": {"status": 200, "json": {"items": []}}}


class Origin:
    """A loopback server that answers every request with 'origin' and records what it was sent."""

    def __init__(self, tls=None):
        self.seen, self.tls = [], tls
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        try:
            if self.tls:
                conn = self.tls.wrap_socket(conn, server_side=True)
            data = b""
            while b"\r\n\r\n" not in data:
                more = conn.recv(4096)
                if not more:
                    return
                data += more
            self.seen.append(data.split(b"\r\n")[0].decode())
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\norigin")
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.sock.close()


def exchange(port, payload, *, tls=None, host=None, connect=None):
    """Send ``payload`` through the proxy (after a CONNECT when given) and return everything it answers."""
    conn = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        if connect:
            conn.sendall(f"CONNECT {connect} HTTP/1.1\r\nHost: {connect}\r\n\r\n".encode())
            reply = b""
            while b"\r\n\r\n" not in reply:
                reply += conn.recv(4096)
            if not reply.startswith(b"HTTP/1.1 200"):
                return reply
        if tls:
            conn = tls.wrap_socket(conn, server_hostname=host)
        conn.sendall(payload)
        out = b""
        while True:
            more = conn.recv(65536)
            if not more:
                return out
            out += more
    except (ConnectionError, ssl.SSLError):
        return out if "out" in dir() else b""
    finally:
        conn.close()


def trusting(ca):
    """A client that trusts ``ca`` and checks certificates strictly, as Python 3.13 and later do by default."""
    context = ssl.create_default_context(cafile=str(ca.cert))
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    return context


def get(port, host, path="/v1/x", origin=None):
    return exchange(port, f"GET http://{host}:80{path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())


def settled(proxy):
    """The first row once it is closed: a tunnel's row ends when the relay does, a moment after the client sees EOF."""
    for _ in range(100):
        if proxy.rows()[0]["via"]:
            break
        time.sleep(0.02)
    return proxy.rows()[0]


class Running(unittest.TestCase):
    def serve(self, profile=None, stubs=(), **extra):
        proxy = Proxy(profile, [dict(s, **{}) for s in stubs], **extra)
        self.addCleanup(proxy.stop)
        return proxy, proxy.start()


class ProfileTests(unittest.TestCase):
    def test_every_name_a_check_may_use_is_a_profile(self):
        self.assertEqual(set(conditions.NETWORKS), set(PROFILES))
        self.assertTrue(PROFILES["offline"].offline)
        self.assertEqual(PROFILES["slow-3g"].rtt_ms, 400)


class ProxyTests(Running):
    def setUp(self):
        self.origin = Origin()
        self.addCleanup(self.origin.close)
        self.resolve = {"api.example.com": ("127.0.0.1", self.origin.port)}

    def test_passes_a_request_to_its_origin_and_records_a_row(self):
        proxy, port = self.serve(resolve=self.resolve)
        answer = get(port, "api.example.com")
        self.assertTrue(answer.startswith(b"HTTP/1.1 200") and answer.endswith(b"origin"))
        self.assertEqual(self.origin.seen, ["GET /v1/x HTTP/1.1"])
        row = proxy.rows()[0]
        self.assertEqual((row["method"], row["host"], row["path"], row["status"], row["via"]),
                         ("GET", "api.example.com", "/v1/x", 200, "pass"))

    def test_the_query_string_never_reaches_a_row(self):
        proxy, port = self.serve(resolve=self.resolve)
        get(port, "api.example.com", "/v1/x?token=secret")
        self.assertEqual(self.origin.seen, ["GET /v1/x?token=secret HTTP/1.1"])
        self.assertNotIn("secret", json.dumps(proxy.rows()))

    def test_a_stub_answers_and_the_origin_sees_nothing(self):
        proxy, port = self.serve(stubs=netstubs.parse([STUB]), resolve=self.resolve)
        answer = get(port, "api.example.com")
        self.assertIn(b"HTTP/1.1 200", answer)
        self.assertTrue(answer.endswith(b'{"items": []}'))
        self.assertIn(b"Content-Type: application/json", answer)
        self.assertEqual(self.origin.seen, [])
        self.assertEqual(proxy.rows()[0]["via"], "stub")

    def test_a_request_no_stub_names_passes_through(self):
        _, port = self.serve(stubs=netstubs.parse([STUB]), resolve=self.resolve)
        self.assertTrue(get(port, "api.example.com", "/v2/x").endswith(b"origin"))
        self.assertEqual(self.origin.seen, ["GET /v2/x HTTP/1.1"])

    def test_stub_method_and_delay(self):
        stubs = netstubs.parse([{"match": {"host": "api.example.com", "method": "post"},
                                 "respond": {"status": 503, "delay_ms": 300}}])
        _, port = self.serve(stubs=stubs, resolve=self.resolve)
        self.assertTrue(get(port, "api.example.com").endswith(b"origin"))
        started = time.monotonic()
        answer = exchange(port, b"POST http://api.example.com/v1 HTTP/1.1\r\nHost: api.example.com\r\n"
                                b"Content-Length: 2\r\n\r\nhi")
        self.assertIn(b"HTTP/1.1 503", answer)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_offline_resets_every_connection_and_names_it_refused(self):
        proxy, port = self.serve(PROFILES["offline"], resolve=self.resolve)
        self.assertEqual(get(port, "api.example.com"), b"")
        self.assertEqual(exchange(port, b"", connect="api.example.com:443"), b"")
        self.assertEqual(self.origin.seen, [])
        self.assertEqual({row["via"] for row in proxy.rows()}, {"refused"})

    def test_latency_is_paid_for_each_round_trip(self):
        _, fast = self.serve(resolve=self.resolve)
        started = time.monotonic()
        get(fast, "api.example.com")
        baseline = time.monotonic() - started
        _, slow = self.serve(Profile(rtt_ms=200), resolve=self.resolve)
        started = time.monotonic()
        get(slow, "api.example.com")
        self.assertGreaterEqual(time.monotonic() - started - baseline, 0.35)   # handshake 200 + two turns of 100

    def test_bandwidth_limits_the_download(self):
        class Big(Origin):
            def _one(self, conn):
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b"x" * 20000)
                conn.close()
        big = Big()
        self.addCleanup(big.close)
        _, port = self.serve(Profile(down_kbps=400), resolve={"api.example.com": ("127.0.0.1", big.port)})
        started = time.monotonic()
        answer = get(port, "api.example.com")
        self.assertGreaterEqual(len(answer), 20000)
        self.assertGreaterEqual(time.monotonic() - started, 0.35)   # 20 kB at 400 kbit/s is 0.4 s

    def test_flaky_resets_a_share_of_connections_the_same_way_each_run(self):
        def outcomes():
            _, port = self.serve(Profile(reset_pct=50), resolve=self.resolve, seed=7)
            return [bool(get(port, "api.example.com")) for _ in range(12)]
        first = outcomes()
        self.assertEqual(first, outcomes())
        self.assertTrue(any(first) and not all(first))

    def test_a_tunnel_relays_bytes_untouched_and_records_a_connect_row(self):
        proxy, port = self.serve(resolve=self.resolve)
        answer = exchange(port, b"GET /t HTTP/1.1\r\nHost: api.example.com\r\n\r\n", connect="api.example.com:443")
        self.assertTrue(answer.endswith(b"origin"))   # the origin here is plain, so the 'tls' bytes are http
        row = settled(proxy)
        self.assertEqual((row["method"], row["via"], row["path"]), ("CONNECT", "tunnel", ""))

    def test_stop_ends_the_listener(self):
        proxy = Proxy()
        port = proxy.start()
        proxy.stop()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2)
        proxy.stop()   # twice is fine


class HttpsStubTests(Running):
    def setUp(self):
        self.ca = netcerts.Authority()
        self.addCleanup(self.ca.close)
        self.trust = trusting(self.ca)
        origin_tls = self.ca.context("api.example.com")
        self.origin = Origin(tls=origin_tls)
        self.addCleanup(self.origin.close)
        self.resolve = {"api.example.com": ("127.0.0.1", self.origin.port)}

    def test_a_stubbed_https_host_is_answered_with_the_runs_certificate(self):
        proxy, port = self.serve(stubs=netstubs.parse([STUB]), authority=self.ca, resolve=self.resolve,
                                 upstream_ssl=self.trust)
        answer = exchange(port, b"GET /v1/items HTTP/1.1\r\nHost: api.example.com\r\n\r\n", tls=self.trust,
                          host="api.example.com", connect="api.example.com:443")
        self.assertTrue(answer.endswith(b'{"items": []}'), answer)
        self.assertEqual(self.origin.seen, [])
        self.assertEqual((proxy.rows()[0]["via"], proxy.rows()[0]["path"]), ("stub", "/v1/items"))

    def test_the_rest_of_that_host_goes_on_to_the_real_origin_over_tls(self):
        _, port = self.serve(stubs=netstubs.parse([STUB]), authority=self.ca, resolve=self.resolve,
                             upstream_ssl=self.trust)
        answer = exchange(port, b"GET /v2/other HTTP/1.1\r\nHost: api.example.com\r\n\r\n", tls=self.trust,
                          host="api.example.com", connect="api.example.com:443")
        self.assertTrue(answer.endswith(b"origin"), answer)
        self.assertEqual(self.origin.seen, ["GET /v2/other HTTP/1.1"])

    def test_a_host_without_a_stub_is_tunnelled_and_never_sees_the_runs_certificate(self):
        proxy, port = self.serve(stubs=netstubs.parse([STUB]), authority=self.ca,
                                 resolve={"other.example.com": ("127.0.0.1", self.origin.port)})
        other = trusting(self.ca)
        other.check_hostname = False   # the origin presents api.example.com's certificate; what matters is whose
        answer = exchange(port, b"GET / HTTP/1.1\r\nHost: other.example.com\r\n\r\n", tls=other,
                          host="other.example.com", connect="other.example.com:443")
        self.assertTrue(answer.endswith(b"origin"))
        self.assertEqual(self.origin.seen, ["GET / HTTP/1.1"])
        self.assertEqual(settled(proxy)["via"], "tunnel")
        self.assertNotIn("other.example.com", self.ca._contexts)   # no certificate was made for it

    def test_the_authority_deletes_its_key_when_it_closes(self):
        folder = self.ca.dir
        self.assertTrue((folder / "ca.key").exists())
        self.ca.close()
        self.assertFalse(folder.exists())


class StubParseTests(unittest.TestCase):
    def test_a_stub_round_trips_through_a_check_file(self):
        check = check_from_dict({"app": {"bundle": "com.example.app"}, "stubs": [STUB],
                                 "conditions": {"network": "slow-3g"}})
        self.assertEqual(check.conditions, {"network": "slow-3g"})
        self.assertEqual(check.stubs[0]["host"], "api.example.com")
        again = check_from_dict(yaml.safe_load(dump_check(check)))
        self.assertEqual(again.stubs, check.stubs)

    def test_a_file_body_is_checked_and_kept_relative(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "list.json").write_text("[]")
            stubs = netstubs.parse([{"match": {"host": "*.example.com"},
                                     "respond": {"file": "list.json"}}], folder)
            self.assertEqual(netstubs.answer(stubs[0]), (200, "application/json", b"[]"))
            self.assertEqual(netstubs.to_dict(stubs, folder)[0]["respond"]["file"], "list.json")
            with self.assertRaises(CheckError) as caught:
                netstubs.parse([{"match": {"host": "a.com"}, "respond": {"file": "gone.json"}}], folder)
            self.assertIn("doesn't exist", str(caught.exception))

    def test_mistakes_say_what_to_write(self):
        for bad, words in (
                ({"match": {"host": "https://a.com"}, "respond": {}}, "host name"),
                ({"match": {"hots": "a.com"}, "respond": {}}, "did you mean host"),
                ({"match": {"host": "a.com"}, "respond": {"status": 99}}, "100 to 599"),
                ({"match": {"host": "a.com"}, "respond": {"json": {}, "file": "x"}}, "Keep one"),
                ({"match": {"host": "a.com", "path_prefix": "v1"}, "respond": {}}, "start with /"),
                ({"match": {"host": "a.com", "method": "FETCH"}, "respond": {}}, "one of GET"),
                ({"match": {"host": "a.com"}, "respond": {"delay_ms": 99999}}, "0 to 30000")):
            with self.assertRaises(CheckError, msg=bad) as caught:
                netstubs.parse([bad])
            self.assertIn(words, str(caught.exception))

    def test_offline_beside_stubs_is_a_contradiction(self):
        with self.assertRaises(CheckError) as caught:
            check_from_dict({"app": {"bundle": "com.example.app"}, "stubs": [STUB],
                             "conditions": {"network": "offline"}})
        self.assertIn("refuses them all", str(caught.exception))

    def test_a_network_name_that_is_not_one_suggests_the_closest(self):
        with self.assertRaises(CheckError) as caught:
            conditions.parse({"network": "slow3g"})
        self.assertIn("did you mean slow-3g", str(caught.exception))
        self.assertEqual(conditions.parse({"network": "lte"}), {"network": "lte"})


class ShimTests(unittest.TestCase):
    def fake(self, calls, fail=None):
        def run(command, **_):
            calls.append(command)
            if command[0] == "xcrun":
                Path(command[command.index("-o") + 1]).write_bytes(b"dylib")
            return subprocess.CompletedProcess(command, 1 if fail == command[0] else 0, "", "boom: it broke\n")
        return run

    def test_builds_once_for_the_simulator_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as cache:
            calls = []
            first = netshim.ensure_built(cache=cache, run=self.fake(calls))
            second = netshim.ensure_built(cache=cache, run=self.fake(calls))
        self.assertEqual(first, second)
        self.assertEqual([c[0] for c in calls], ["xcrun", "codesign"])
        self.assertIn("iphonesimulator", calls[0])
        self.assertIn("-mios-simulator-version-min=15.0", calls[0])

    def test_a_failed_build_names_the_reason_and_leaves_nothing_cached(self):
        with tempfile.TemporaryDirectory() as cache:
            with self.assertRaises(netshim.ShimError) as caught:
                netshim.ensure_built(cache=cache, run=self.fake([], fail="xcrun"))
            self.assertIn("it broke", caught.exception.message)
            self.assertEqual(list(Path(cache).rglob("*.dylib")) + list(Path(cache).rglob("*.tmp")), [])

    def test_the_shim_reads_only_the_proxy_variables(self):
        source = netshim.SOURCE.read_text()
        self.assertEqual(sorted(set(__import__("re").findall(r'getenv\("(\w+)"\)', source))),
                         ["MOBSTER_PROXY_HOST", "MOBSTER_PROXY_PORT"])


class FakeProxy:
    def __init__(self, profile, stubs, authority):
        self.profile, self.stubs, self.authority = profile, stubs, authority
        self.port, self.total, self.stopped = 4321, 0, 0

    def start(self):
        return self.port

    def stop(self):
        self.stopped += 1

    def rows(self):
        return []


class FakeAuthority:
    cert = Path("/tmp/ca.pem")
    closed = 0

    def close(self):
        FakeAuthority.closed += 1


class NetRunTests(unittest.TestCase):
    def net(self, stubs=(), run=None, build=None, profile="slow-3g"):
        calls = []
        net = netrun.NetRun(profile, stubs, udid="U", run=run or (lambda args: calls.append(args) or (0, "")),
                            build=build or (lambda: "/cache/netshim.dylib"), authority=FakeAuthority, proxy=FakeProxy)
        return net, calls

    def test_start_hands_back_the_environment_that_points_the_app_at_the_proxy(self):
        net, calls = self.net()
        self.assertEqual(net.start(), {"DYLD_INSERT_LIBRARIES": "/cache/netshim.dylib",
                                       "MOBSTER_PROXY_HOST": "127.0.0.1", "MOBSTER_PROXY_PORT": "4321"})
        self.assertEqual(calls, [])   # a profile alone adds no certificate to the simulator

    def test_stubs_add_the_runs_certificate_to_the_simulator_and_stop_deletes_the_key(self):
        FakeAuthority.closed = 0
        net, calls = self.net(stubs=netstubs.parse([STUB]))
        net.start()
        self.assertEqual(calls, [["keychain", "U", "add-root-cert", "/tmp/ca.pem"]])
        net.stop()
        net.stop()
        self.assertEqual((FakeAuthority.closed, net.summary()["profile"]), (1, "slow-3g"))

    def test_a_certificate_the_simulator_refuses_stops_the_run_with_a_fix(self):
        FakeAuthority.closed = 0
        net, _ = self.net(stubs=netstubs.parse([STUB]), run=lambda args: (1, "no such device\n"))
        with self.assertRaises(netrun.NetError) as caught:
            net.start()
        self.assertIn("no such device", caught.exception.message)
        self.assertEqual(FakeAuthority.closed, 1)

    def test_a_shim_that_will_not_build_stops_the_run(self):
        def build():
            raise netshim.ShimError("Mobster couldn't build its network shim: nope.")
        net, _ = self.net(build=build)
        with self.assertRaises(netrun.NetError):
            net.start()

    def test_a_run_nobody_called_says_so(self):
        net, _ = self.net()
        net.start()
        net.stop()
        self.assertIn("No request from the app reached the proxy", net.summary()["note"])


class ApplierTests(unittest.TestCase):
    def applier(self, spec, *, device=None, stubs=(), nets=None):
        nets = [] if nets is None else nets

        class Net(FakeNet):
            instances = nets
        return conditions.Applier(spec, udid="U", bundle="com.example.app", device=device, run=lambda a: (0, ""),
                                  actions=SimpleNamespace(action=lambda _operation: contextlib.nullcontext()),
                                  stubs=stubs, net=Net)

    def test_the_network_condition_starts_a_run_and_gives_the_launch_environment(self):
        nets = []
        applier = self.applier({"network": "lte"}, nets=nets)
        applier.before_launch()
        self.assertEqual(applier.launch_env(), {"MOBSTER_PROXY_PORT": "1"})
        self.assertEqual(nets[0].args, ("lte", ()))
        self.assertIn("network", applier.report()["applied"])
        self.assertEqual(applier.report()["network"]["profile"], "lte")

    def test_restore_stops_the_proxy_even_when_it_raises(self):
        nets = []
        applier = self.applier({"network": "lte"}, nets=nets)
        applier.before_launch()
        nets[0].raises = True
        applier.restore(applier.actions)
        self.assertEqual(nets[0].stops, 1)

    def test_stubs_alone_start_an_unshaped_run(self):
        nets = []
        applier = self.applier({}, stubs=netstubs.parse([STUB]), nets=nets)
        applier.before_launch()
        self.assertEqual(nets[0].args[0], None)
        self.assertIn("stubs", applier.report()["applied"])

    def test_a_real_iphone_is_refused_not_skipped(self):
        for spec, stubs in (({"network": "offline"}, ()), ({}, netstubs.parse([STUB]))):
            applier = self.applier(spec, device=object(), stubs=stubs)
            with self.assertRaises(conditions.ConditionError) as caught:
                applier.before_launch()
            self.assertIn("Run the check on a simulator", caught.exception.message + caught.exception.fix)
            self.assertEqual(applier.launch_env(), {})

    def test_a_proxy_that_will_not_start_is_a_condition_error(self):
        applier = self.applier({"network": "lte"}, nets=[])
        applier._net_factory = FailingNet
        with self.assertRaises(conditions.ConditionError) as caught:
            applier.before_launch()
        self.assertEqual(caught.exception.fix, "do this")


class FakeNet:
    instances = []
    raises = False

    def __init__(self, profile, stubs, *, udid, run):
        self.args, self.stops = (profile, tuple(stubs)), 0
        type(self).instances.append(self)

    def start(self):
        return {"MOBSTER_PROXY_PORT": "1"}

    def stop(self):
        self.stops += 1
        if self.raises:
            raise RuntimeError("boom")

    def summary(self):
        return {"profile": self.args[0], "requests": 0, "rows": []}

    def write(self, directory):
        Path(directory, "network.json").write_text(json.dumps(self.summary()))


class FailingNet(FakeNet):
    def start(self):
        raise netrun.NetError("it didn't start", "do this")


class RunTests(RunnerCase):
    def run_check(self, manager, **extra):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "expect": [{"text": "Account"}], **extra},
                                base_dir=self.root)
        driver = Driver([CLEAN])
        self.watch(driver, manager)
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager, simctl=lambda args: (0, ""))
        with mock.patch.object(runner, "build_driver", lambda *a: driver), \
                mock.patch.object(netrun, "NetRun", FakeNet):
            try:
                run.prepare()
            except runner.CouldntRun:
                pass
            outcome = run.finish()
        return run, outcome

    def setUp(self):
        super().setUp()
        FakeNet.instances = []

    def test_the_app_launches_with_the_shim_and_the_check_cannot_turn_it_off(self):
        manager = Manager()
        run, outcome = self.run_check(manager, conditions={"network": "slow-3g"},
                                      launch={"env": {"MOBSTER_PROXY_PORT": "9", "KEEP": "yes"}})
        self.assertEqual(outcome["verdict"], "passed")
        launch = next(call for call in manager.calls if call[0] == "launch")
        self.assertEqual(launch[3], {"MOBSTER_PROXY_PORT": "1", "KEEP": "yes"})
        self.assertEqual(FakeNet.instances[0].args, ("slow-3g", ()))
        self.assertEqual(FakeNet.instances[0].stops, 1)   # stopped when the run released the device
        self.assertEqual(json.loads((run.run_dir / "network.json").read_text())["profile"], "slow-3g")
        self.assertEqual(outcome["conditions"]["network"]["profile"], "slow-3g")

    def test_a_relaunch_keeps_the_shim(self):
        manager = Manager()
        check = check_from_dict({"app": {"bundle": BUNDLE}, "expect": [{"text": "Account"}],
                                 "conditions": {"network": "lte"}}, base_dir=self.root)
        driver = Driver([CLEAN])
        self.watch(driver, manager)
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager, simctl=lambda args: (0, ""))
        with mock.patch.object(runner, "build_driver", lambda *a: driver), mock.patch.object(netrun, "NetRun", FakeNet):
            run.prepare()
            run.relaunch()
            run.finish()
        launches = [call for call in manager.calls if call[0] == "launch"]
        self.assertEqual([call[3] for call in launches], [{"MOBSTER_PROXY_PORT": "1"}] * 2)

    def test_stubs_alone_launch_through_an_unshaped_proxy(self):
        run, outcome = self.run_check(Manager(), stubs=[STUB])
        self.assertEqual(outcome["verdict"], "passed")
        self.assertEqual(FakeNet.instances[0].args[0], None)
        self.assertEqual(FakeNet.instances[0].args[1][0]["host"], "api.example.com")

    def test_a_real_iphone_ends_as_couldnt_run_and_the_app_never_launches(self):
        phone = Manager()
        phone.real_device = True
        _, outcome = self.run_check(phone, conditions={"network": "offline"})
        self.assertEqual((outcome["verdict"], outcome["reason"]["class"]), ("couldnt_run", "environment"))
        self.assertIn("Run the check on a simulator", outcome["reason"]["message"] + outcome["reason"]["fix"])
        self.assertNotIn("launch", phone.names())
        self.assertEqual(FakeNet.instances, [])


if __name__ == "__main__":
    unittest.main()

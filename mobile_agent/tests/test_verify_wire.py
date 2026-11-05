"""Wire assertions (verify/wire.py, wirestore.py): expect request, no_request and storage, judged on recorded proxy
captures, a loopback proxy and fake app containers. No simulator, phone, model or network."""

import json
import os
import plistlib
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from mobile_agent.tests.test_a11y_levels import CLEAN
from mobile_agent.tests.test_net_conditions import Origin, Running, exchange, trusting
from mobile_agent.tests.test_verify_runner import BUNDLE, Driver, Manager, RunnerCase
from mobile_agent.verify import netcerts, netrun, runner, wire, wirestore
from mobile_agent.verify.assertions import CheckError
from mobile_agent.verify.checks import check_from_dict, dump_check
from mobile_agent.verify.runner import VerifyRun

ON = {wire.FLAG: "1"}


def parse(*items):
    with mock.patch.dict(os.environ, ON):
        return wire.parse(list(items))


def capture(method="POST", host="api.example.com", path="/v1/cart", status=200, body=b"", headers=(), response=b"",
            at=4200):
    return {"atMs": at, "method": method, "host": host, "path": path, "headers": list(headers), "body": body,
            "status": status, "response": response}


def tunnel(host):
    return {"method": "CONNECT", "host": host, "path": "", "via": "tunnel", "atMs": 100}


class FakeSeen:
    def __init__(self, captures=(), rows=()):
        self.captures, self.rows = list(captures), list(rows)

    def seen(self):
        return self.captures, self.rows


def judge(item, captures=(), rows=()):
    return wire.Judge(parse(item), run=None, udid="U", bundle=BUNDLE).judge(FakeSeen(captures, rows))[0]


class ParseTests(unittest.TestCase):
    def test_off_by_default_and_says_which_flag(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(wire.FLAG, None)
            with self.assertRaises(CheckError) as caught:
                wire.parse([{"request": "GET /x"}])
        self.assertIn("MOBSTER_EXPERIMENTAL_NETWORK=1", str(caught.exception))

    def test_a_check_without_wire_assertions_needs_no_flag(self):
        self.assertEqual(wire.parse([{"text": "Hi"}]), ())

    def test_shorthand_and_mapping_mean_the_same(self):
        short, full = parse({"request": "POST api.example.com/v1/cart"},
                            {"request": {"method": "post", "host": "api.example.com", "path": "/v1/cart"}})
        self.assertEqual(short, full)
        self.assertEqual(short.render(), "request POST api.example.com/v1/cart")

    def test_path_only_and_host_only_shorthand(self):
        path, host = parse({"no_request": "/v1/track*"}, {"no_request": "*.tracker.example"})
        self.assertEqual((path.host, str(path.path.text)), (None, "/v1/track*"))
        self.assertEqual((host.host, host.path), ("*.tracker.example", None))

    def test_mistakes_say_what_to_write(self):
        for bad, said in (({"request": {}}, "at least a method"),
                          ({"request": {"mehtod": "GET"}}, "isn't a setting"),
                          ({"request": {"path": "v1"}}, "must start with /"),
                          ({"request": {"method": "FETCH"}}, "method must be one of"),
                          ({"request": {"path": "/x", "status": 99}}, "status must be"),
                          ({"no_request": {"path": "/x", "status": 200}}, "takes no status"),
                          ({"request": 5}, "must be a mapping"),
                          ({"storage": {}}, "one of file"),
                          ({"storage": {"file": "/etc/passwd"}}, "inside the app's container"),
                          ({"storage": {"file": "../x"}}, "inside the app's container"),
                          ({"storage": {"sqlite": "a.db", "query": "DELETE FROM t", "at_least": 1}}, "one SELECT"),
                          ({"storage": {"sqlite": "a.db", "query": "select 1"}}, "exactly one of equals"),
                          ({"storage": {"file": "a", "changed": "yes"}}, "true or false")):
            with self.subTest(bad), self.assertRaises(CheckError) as caught:
                parse(bad)
            self.assertIn(said, str(caught.exception))

    def test_watch_hosts_name_only_the_hosts_whose_https_must_be_opened(self):
        items = parse({"request": "POST api.example.com/v1/cart"}, {"no_request": "*.tracker.example"},
                      {"storage": {"file": "x"}})
        self.assertEqual(wire.watch_hosts(items), ("api.example.com",))   # a host alone is visible in its CONNECT
        self.assertIsNone(wire.watch_hosts(parse({"storage": {"file": "x"}})))
        self.assertEqual(wire.watch_hosts(parse({"no_request": "/x"})), ())

    def test_a_check_file_round_trips(self):
        data = {"app": {"bundle": BUNDLE}, "steps": ["Add to cart"],
                "expect": [{"text": "Cart"}, {"request": {"method": "POST", "host": "api.example.com",
                                                          "path": "/v1/cart", "status": "2xx"}},
                           {"no_request": {"host": "*.tracker.example"}},
                           {"storage": {"file": "Library/Preferences/app.plist", "changed": False}},
                           {"storage": {"sqlite": "Library/app.db", "query": "select 1 from cart", "at_least": 1}}]}
        with mock.patch.dict(os.environ, ON):
            check = check_from_dict(data)
            again = check_from_dict(yaml.safe_load(dump_check(check)))
        self.assertEqual(len(check.expect), 1)
        self.assertEqual([item.kind for item in check.wire], ["request", "no_request", "storage", "storage"])
        self.assertEqual(again.wire, check.wire)

    def test_a_wire_assertion_is_not_allowed_in_throughout(self):
        with mock.patch.dict(os.environ, ON), self.assertRaises(CheckError):
            check_from_dict({"app": {"bundle": BUNDLE}, "throughout": [{"request": "GET /x"}]})


class RequestTests(unittest.TestCase):
    def test_a_matching_request_is_the_evidence_with_secrets_masked(self):
        sent = capture(body=b'{"sku":"a1","card":"4111 1111 1111 1111"}',
                       headers=[("Authorization", "Bearer abc.def.ghi"), ("Content-Type", "application/json")],
                       response=b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}')
        result = judge({"request": {"method": "POST", "host": "api.example.com", "path": "/v1/*", "status": 200,
                                    "body": "/\"sku\":\"a1\"/", "response": "ok"}}, [sent])
        self.assertTrue(result.ok)
        self.assertEqual(result.observed, "POST api.example.com/v1/cart -> 200 at 4.2 s")
        proof = result.matches[0]
        self.assertIn(["Authorization", "••••••"], proof["headers"])
        self.assertIn(["Content-Type", "application/json"], proof["headers"])
        self.assertNotIn("4111", proof["body"])
        self.assertEqual(proof["response"], '{"ok":true}')

    def test_each_part_must_match(self):
        sent = [capture()]
        for missed in ({"method": "GET"}, {"host": "other.example.com"}, {"path": "/v2/*"}, {"status": 500},
                       {"status": "4xx"}, {"body": "nope"}):
            with self.subTest(missed):
                result = judge({"request": {"host": "api.example.com", **missed}}, sent)
                self.assertFalse(result.ok)
                self.assertIn("no request matched; saw POST api.example.com/v1/cart", result.observed)

    def test_status_class_and_regex_path(self):
        self.assertTrue(judge({"request": {"path": "/^\\/v1\\/c/", "status": "2xx"}}, [capture(status=201)]).ok)

    def test_the_query_string_is_matched_only_when_the_pattern_names_it(self):
        sent = [capture(path="/v1/cart?id=7")]
        self.assertTrue(judge({"request": "/v1/cart"}, sent).ok)
        self.assertTrue(judge({"request": "/v1/cart?id=*"}, sent).ok)
        self.assertFalse(judge({"request": "/v1/cart?id=8"}, sent).ok)

    def test_a_gzip_chunked_response_body_is_read(self):
        import gzip
        packed = gzip.compress(b'{"total": 12}')
        raw = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Encoding: gzip\r\n\r\n"
               + f"{len(packed):x}\r\n".encode() + packed + b"\r\n0\r\n\r\n")
        self.assertTrue(judge({"request": {"path": "/v1/*", "response": "\"total\": 12"}}, [capture(response=raw)]).ok)

    def test_nothing_reached_the_proxy_says_so(self):
        result = judge({"request": "POST api.example.com/v1/cart"})
        self.assertFalse(result.ok)
        self.assertIn("no request from the app reached the proxy", result.observed)

    def test_a_path_asked_of_an_unopened_https_host_says_to_name_the_host(self):
        result = judge({"request": "/v1/cart"}, rows=[tunnel("api.example.com")])
        self.assertFalse(result.ok)
        self.assertIn("api.example.com wasn't opened, so name the host", result.observed)

    def test_no_request_passes_when_the_app_was_seen_and_nothing_matched(self):
        result = judge({"no_request": {"host": "*.tracker.example"}}, [capture()], [tunnel("cdn.example.com")])
        self.assertTrue(result.ok)
        self.assertEqual(result.observed, "none of 2 requests matched")

    def test_no_request_fails_on_an_opened_request_with_the_line_as_proof(self):
        result = judge({"no_request": {"host": "*.tracker.example", "path": "/collect*"}},
                       [capture("GET", "px.tracker.example", "/collect?id=1", 204)])
        self.assertFalse(result.ok)
        self.assertEqual(result.observed, "left the app: GET px.tracker.example/collect?id=1 -> 204 at 4.2 s")

    def test_no_request_sees_a_connection_to_the_host_without_opening_it(self):
        result = judge({"no_request": "*.tracker.example"}, [capture()], [tunnel("px.tracker.example")])
        self.assertFalse(result.ok)
        self.assertIn("CONNECT px.tracker.example", result.observed)

    def test_a_refused_request_still_left_the_app(self):
        refused = {"method": "GET", "host": "px.tracker.example", "path": "/a", "via": "refused", "atMs": 5}
        self.assertFalse(judge({"no_request": "*.tracker.example"}, [], [refused]).ok)

    def test_no_request_fails_closed_when_the_proxy_saw_nothing(self):
        result = judge({"no_request": {"host": "*.tracker.example"}})
        self.assertFalse(result.ok)
        self.assertIn("can't tell", result.observed)

    def test_a_path_only_no_request_fails_closed_beside_an_unopened_connection(self):
        result = judge({"no_request": "/collect*"}, [], [tunnel("cdn.example.com")])
        self.assertFalse(result.ok)
        self.assertIn("name the host", result.observed)

    def test_a_run_without_a_proxy_fails_closed(self):
        result = wire.Judge(parse({"request": "GET /x"}), run=None, udid="U", bundle=BUNDLE).judge(None)[0]
        self.assertFalse(result.ok)


class ProxyCaptureTests(Running):
    def setUp(self):
        self.origin = Origin()
        self.addCleanup(self.origin.close)
        self.resolve = {"api.example.com": ("127.0.0.1", self.origin.port)}

    def test_plain_http_is_captured_whole(self):
        proxy, port = self.serve(resolve=self.resolve, watch=())
        body = b'{"sku":"a1"}'
        exchange(port, (b"POST http://api.example.com:80/v1/cart?x=1 HTTP/1.1\r\nHost: api.example.com\r\n"
                        b"Authorization: Bearer tok\r\nContent-Length: %d\r\n\r\n" % len(body)) + body)
        item = proxy.captures()[0]
        self.assertEqual((item["method"], item["host"], item["path"], item["status"], item["body"]),
                         ("POST", "api.example.com", "/v1/cart?x=1", 200, body))
        self.assertTrue(item["response"].endswith(b"origin"))
        self.assertTrue(wire.Judge(parse({"request": "POST api.example.com/v1/cart?x=*"}), run=None, udid="U",
                                   bundle=BUNDLE).judge(FakeSeen(proxy.captures(), proxy.rows()))[0].ok)

    def test_a_proxy_without_watch_keeps_no_captures(self):
        proxy, port = self.serve(resolve=self.resolve)
        exchange(port, b"GET http://api.example.com:80/a HTTP/1.1\r\nHost: api.example.com\r\n\r\n")
        self.assertEqual(proxy.captures(), [])

    def test_a_watched_https_host_is_opened_and_others_stay_tunnels(self):
        ca = netcerts.Authority()
        self.addCleanup(ca.close)
        trust = trusting(ca)
        secure = Origin(tls=ca.context("api.example.com"))
        self.addCleanup(secure.close)
        proxy, port = self.serve(authority=ca, resolve={"api.example.com": ("127.0.0.1", secure.port),
                                                        "cdn.example.com": ("127.0.0.1", secure.port)},
                                 upstream_ssl=trust, watch=("api.example.com",))
        answer = exchange(port, b"POST /v1/cart HTTP/1.1\r\nHost: api.example.com\r\nContent-Length: 2\r\n\r\nhi",
                          tls=trust, host="api.example.com", connect="api.example.com:443")
        self.assertTrue(answer.endswith(b"origin"), answer)
        self.assertEqual([(c["method"], c["path"], c["body"], c["status"]) for c in proxy.captures()],
                         [("POST", "/v1/cart", b"hi", 200)])
        other = trusting(ca)
        other.check_hostname = False
        exchange(port, b"GET / HTTP/1.1\r\nHost: cdn.example.com\r\n\r\n", tls=other, host="cdn.example.com",
                 connect="cdn.example.com:443")
        self.assertEqual(len(proxy.captures()), 1)
        self.assertNotIn("cdn.example.com", ca._contexts)

    def test_an_open_tunnel_is_seen_by_no_request_before_it_closes(self):
        proxy, port = self.serve(resolve={"px.tracker.example": ("127.0.0.1", self.origin.port)}, watch=())
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(client.close)
        client.sendall(b"CONNECT px.tracker.example:443 HTTP/1.1\r\nHost: px.tracker.example:443\r\n\r\n")
        self.assertIn(b"200", client.recv(256))
        result = wire.Judge(parse({"no_request": "*.tracker.example"}), run=None, udid="U",
                            bundle=BUNDLE).judge(FakeSeen(proxy.captures(), proxy.rows()))[0]
        self.assertFalse(result.ok)
        self.assertIn("px.tracker.example", result.observed)


class StorageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "Library" / "Preferences").mkdir(parents=True)
        self.prefs = self.root / "Library" / "Preferences" / "app.plist"
        self.prefs.write_bytes(plistlib.dumps({"cart": 0, "token": "sk_live_abcdefghijklmnop"},
                                              fmt=plistlib.FMT_BINARY))

    def run_storage(self, spec, change=lambda: None):
        item = parse({"storage": spec})[0]
        before = wirestore.take(self.root, item)
        change()
        return wirestore.judge(item, self.root, before)

    def bump(self):
        self.prefs.write_bytes(plistlib.dumps({"cart": 2, "token": "sk_live_abcdefghijklmnop"},
                                              fmt=plistlib.FMT_BINARY))

    def test_a_changed_plist_shows_the_diff_and_masks_secrets(self):
        result = self.run_storage({"file": "Library/Preferences/app.plist"}, self.bump)
        self.assertTrue(result.ok)
        self.assertIn("Library/Preferences/app.plist changed:", result.observed)
        self.assertIn('- "cart": 0', result.observed)
        self.assertIn('+ "cart": 2', result.observed)
        self.assertNotIn("sk_live_abcdefghijklmnop", result.observed)

    def test_an_untouched_file_fails_changed_and_holds_unchanged(self):
        self.assertFalse(self.run_storage({"file": "Library/Preferences/*.plist"}).ok)
        self.assertTrue(self.run_storage({"file": "Library/Preferences/*.plist", "changed": False}).ok)
        self.assertFalse(self.run_storage({"file": "Library/Preferences/*.plist", "changed": False}, self.bump).ok)

    def test_a_new_file_is_added_and_contains_is_checked(self):
        def write(name):
            return lambda: (self.root / "Library" / name).write_text("order 41 placed")
        found = self.run_storage({"file": "Library/*.txt", "contains": "order 41"}, write("a.txt"))
        self.assertTrue(found.ok)
        self.assertIn("Library/a.txt added", found.observed)
        miss = self.run_storage({"file": "Library/*.log", "contains": "order 99"}, write("b.log"))
        self.assertFalse(miss.ok)
        self.assertIn('no matching file contains "order 99"', miss.observed)

    def test_a_missing_file_says_where_it_looked(self):
        result = self.run_storage({"file": "Documents/none.json"})
        self.assertEqual(result.observed, "no file in the container matches Documents/none.json")

    def test_no_baseline_is_not_a_guess(self):
        item = parse({"storage": {"file": "Library/Preferences/app.plist"}})[0]
        self.assertFalse(wirestore.judge(item, self.root, None).ok)

    def make_db(self):
        path = self.root / "Library" / "app.db"
        db = sqlite3.connect(path)
        db.execute("create table cart (sku text, qty int)")
        db.commit()
        db.close()
        return path

    def test_a_sqlite_query_counts_rows_on_a_copy(self):
        path = self.make_db()

        def add():
            db = sqlite3.connect(path)
            db.execute("insert into cart values ('a1', 2)")
            db.commit()
            db.close()
        spec = {"sqlite": "Library/app.db", "query": "select sku, qty from cart where qty >= 2", "at_least": 1}
        result = self.run_storage(spec, add)
        self.assertTrue(result.ok)
        self.assertEqual(result.observed, "1 rows (was 0): ('a1', 2)")
        none = self.run_storage({"sqlite": "Library/app.db", "query": "select * from cart", "equals": 5}, add)
        self.assertFalse(none.ok)

    def test_a_query_that_writes_is_refused_even_past_the_parser(self):
        path = self.make_db()
        item = parse({"storage": {"sqlite": "Library/app.db", "query": "select 1", "at_least": 1}})[0]
        evil = item.__class__(**{**item.__dict__, "query": "with x as (select 1) insert into cart values ('z', 1)"})
        with self.assertRaises(wirestore.StoreError):
            wirestore.rows(self.root, evil)
        self.assertEqual(sqlite3.connect(path).execute("select count(*) from cart").fetchone()[0], 0)

    def test_a_bad_query_and_a_missing_database_are_failures_not_crashes(self):
        self.make_db()
        bad = self.run_storage({"sqlite": "Library/app.db", "query": "select * from nope", "at_least": 1})
        self.assertFalse(bad.ok)
        self.assertIn("query on Library/app.db failed", bad.observed)
        gone = self.run_storage({"sqlite": "Library/none.db", "query": "select 1", "at_least": 1})
        self.assertIn("isn't in the app's container", gone.observed)

    def test_the_container_comes_from_simctl_and_a_failure_names_the_reason(self):
        calls = []

        def run(args):
            calls.append(args)
            return 0, f"{self.root}\n"
        self.assertEqual(wirestore.container(run, "U", BUNDLE), self.root)
        self.assertEqual(calls, [["get_app_container", "U", BUNDLE, "data"]])
        with self.assertRaises(wirestore.StoreError):
            wirestore.container(lambda args: (1, "No such app"), "U", BUNDLE)


class RunNet:
    """Stands in for netrun.NetRun: it hands the run recorded captures."""
    instances = []

    def __init__(self, profile, stubs, *, udid, run, watch=None):
        self.watch, self.captures, self.rows = watch, [], []
        type(self).instances.append(self)

    def start(self):
        return {"MOBSTER_PROXY_PORT": "1"}

    def stop(self):
        pass

    def seen(self):
        return self.captures, self.rows

    def summary(self):
        return {"profile": None, "requests": len(self.captures), "rows": []}

    def write(self, directory):
        Path(directory, "network.json").write_text(json.dumps(self.summary()))


class RunTests(RunnerCase):
    def run_check(self, expect, *, captures=(), manager=None, simctl=None):
        with mock.patch.dict(os.environ, ON):
            check = check_from_dict({"app": {"bundle": BUNDLE}, "steps": ["Add to cart"], "expect": expect},
                                    base_dir=self.root)
        manager = manager or Manager()
        driver = Driver([CLEAN])
        self.watch(driver, manager)
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager,
                        simctl=simctl or (lambda args: (0, str(self.root))))
        RunNet.instances = []
        with mock.patch.object(runner, "build_driver", lambda *a: driver), mock.patch.object(netrun, "NetRun", RunNet):
            try:
                run.prepare()
                for net in RunNet.instances:
                    net.captures = list(captures)
            except runner.CouldntRun:
                pass
            outcome = run.finish()
        return run, outcome

    def test_a_request_the_app_made_passes_and_the_report_holds_the_line(self):
        run, outcome = self.run_check([{"text": "Account"}, {"request": "POST api.example.com/v1/cart"}],
                                      captures=[capture()])
        self.assertEqual(outcome["verdict"], "passed", outcome["reason"])
        listed = outcome["assertions"][1]
        self.assertEqual((listed["text"], listed["ok"]), ("request POST api.example.com/v1/cart", True))
        self.assertEqual(listed["observed"], "POST api.example.com/v1/cart -> 200 at 4.2 s")
        saved = json.loads((run.run_dir / "wire.json").read_text())
        self.assertEqual(saved[0]["matches"][0]["line"], listed["observed"])

    def test_a_missing_request_fails_the_run_as_an_assertion(self):
        _, outcome = self.run_check([{"text": "Account"}, {"request": "POST api.example.com/v1/cart"}],
                                    captures=[capture("GET", "api.example.com", "/v1/items")])
        self.assertEqual((outcome["verdict"], outcome["reason"]["class"]), ("failed", "assertion"))
        self.assertIn("1 of 2 expectations failed", outcome["reason"]["message"])

    def test_the_hosts_a_check_names_are_opened_by_the_proxy(self):
        self.run_check([{"text": "Account"}, {"request": "POST api.example.com/v1/cart"}], captures=[capture()])
        self.assertEqual(RunNet.instances[0].watch, ("api.example.com",))

    def test_storage_is_judged_on_the_container_after_the_flow(self):
        (self.root / "store.txt").write_text("empty")
        manager = Manager(on={"launch": lambda: None})
        original = runner.VerifyRun._take_baseline

        def flow(run):
            original(run)
            (self.root / "store.txt").write_text("cart: 1")
        with mock.patch.object(runner.VerifyRun, "_take_baseline", flow):
            run, outcome = self.run_check([{"text": "Account"}, {"storage": {"file": "store.txt",
                                                                             "contains": "cart: 1"}}],
                                          manager=manager)
        self.assertEqual(outcome["verdict"], "passed", outcome["reason"])
        self.assertIn("store.txt changed", outcome["assertions"][1]["observed"])

    def test_a_check_with_only_wire_expectations_is_decided_by_them(self):
        _, outcome = self.run_check([{"request": "POST api.example.com/v1/cart"}], captures=[capture()])
        self.assertEqual(outcome["verdict"], "passed", outcome["reason"])

    def test_a_real_iphone_ends_as_couldnt_run(self):
        phone = Manager()
        phone.real_device = True
        _, outcome = self.run_check([{"text": "Account"}, {"request": "POST api.example.com/v1/cart"}],
                                    manager=phone)
        self.assertEqual((outcome["verdict"], outcome["reason"]["class"]), ("couldnt_run", "environment"))
        self.assertIn("doesn't set up the proxy on a real iPhone", outcome["reason"]["message"])
        self.assertNotIn("launch", phone.names())


if __name__ == "__main__":
    unittest.main()

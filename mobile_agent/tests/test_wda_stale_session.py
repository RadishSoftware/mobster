import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mobile_agent.state import Element
from mobile_agent.tests import test_frontier as F
from mobile_agent.tests.test_turn_exits import Model, run
from mobile_agent.transport import HTTP, NotSent, StaleSession, TransportError


def serve(status, body):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class TransportTests(unittest.TestCase):
    def ask(self, status, body):
        server = serve(status, body)
        client = HTTP(f"http://127.0.0.1:{server.server_address[1]}", timeout=5)
        try:
            return client.request("GET", "/session/old/source")
        finally:
            client.close()
            server.shutdown()
            server.server_close()

    def test_a_dropped_session_is_stale_and_nothing_was_sent(self):
        with self.assertRaises(StaleSession) as caught:
            self.ask(404, {"value": {"error": "invalid session id", "message": "Session does not exist"}})
        self.assertIsInstance(caught.exception, NotSent)
        self.assertIn("StaleSession", str(caught.exception))

    def test_any_other_refusal_stays_an_ordinary_failure(self):
        for status, body in ((404, {"value": {"error": "no such element", "message": "x"}}), (404, {"value": "x"}),
                             (500, {"value": {"error": "invalid session id"}})):
            with self.subTest(status=status, body=body), self.assertRaises(TransportError) as caught:
                self.ask(status, body)
            self.assertNotIsInstance(caught.exception, NotSent)


class Restarted(F.Driver):
    capabilities = F.Driver.capabilities | {"recover"}

    def __init__(self, screen, op="TAP", reads_stale=0):
        super().__init__([screen] * 12)
        self.op, self.sent, self.recovered, self.reads_stale = op, [], 0, reads_stale
        self.stale = True

    def observe(self, timeout=10):
        if self.reads_stale and self.stale:
            self.reads_stale -= 1
            raise StaleSession("HTTP 404 StaleSession: invalid session id; nothing sent; not retried")
        return super().observe(timeout)

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.sent.append((operation, getattr(target, "label", target)))
        if self.stale and operation == self.op:
            raise StaleSession("HTTP 404 StaleSession: invalid session id; nothing sent; not retried")
        return super().execute(operation, target, snapshot, text, timeout)

    def recover(self, timeout=None):
        self.recovered += 1
        self.stale = False


ARCHIVE = Element("1", "Archive", "Button", (.4, .1, .2, .05))


class FrontierTests(unittest.TestCase):
    def test_an_action_refused_on_a_dropped_session_is_sent_again_after_the_reattach(self):
        driver = Restarted(F.screen(ARCHIVE))
        model = Model([("TAP", "Archive", None)], [("DONE", None, None)])
        result, events = run(driver, model, "Archive the QuickBite receipt in Mail.")
        self.assertEqual((driver.sent, driver.recovered), ([("TAP", "Archive"), ("TAP", "Archive")], 1))
        self.assertEqual([e for e in events if e["event"] == "frontier_failed"], [])
        self.assertTrue(any(e["event"] == "frontier_wda_recovery" and e["ok"] for e in events))
        self.assertNotEqual(result["status"], "stuck")

    def test_a_read_on_a_dropped_session_reattaches_and_reads_again(self):
        driver = Restarted(F.screen(ARCHIVE), op=None, reads_stale=1)
        model = Model([("DONE", None, None)])
        result, events = run(driver, model, "What's on the screen?")
        self.assertEqual(driver.recovered, 1)
        self.assertTrue(any(e["event"] == "frontier_wda_recovery" and e["ok"] for e in events))
        self.assertNotIn(result["status"], ("stuck", "error"))

    def test_any_other_404_is_not_sent_again(self):
        class Refusing(Restarted):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                self.sent.append((operation, getattr(target, "label", target)))
                raise TransportError("HTTP 404; request not retried")
        driver = Refusing(F.screen(ARCHIVE))
        model = Model([("TAP", "Archive", None)], [("DONE", None, None)])
        run(driver, model, "Archive the QuickBite receipt in Mail.")
        self.assertEqual((driver.sent, driver.recovered), ([("TAP", "Archive")], 0))

    def test_a_session_still_stale_after_the_reattach_is_sent_once_more_at_most(self):
        class StillStale(Restarted):
            def recover(self, timeout=None):
                self.recovered += 1
        driver = StillStale(F.screen(ARCHIVE))
        model = Model([("TAP", "Archive", None)], [("DONE", None, None)])
        run(driver, model, "Archive the QuickBite receipt in Mail.")
        self.assertLessEqual(driver.recovered, 3)
        self.assertLessEqual(len(driver.sent), 2 * driver.recovered)


if __name__ == "__main__":
    unittest.main()

"""HTTP boundary checks without a device or model provider."""
import io
import json
import threading
from email.message import Message
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from ..server import Run, make_handler


class HTTPBoundaryTests(unittest.TestCase):
    def handler(self, raw=b'{}', path='/api/runs'):
        run = Run({'id': 'settings', 'name': 'Settings'}, 'Open About', 'live',
                  id='a123456789ab', status='blocked', finished_at=1)
        run.emit({'event': 'run_started'})
        run.emit({'event': 'run_finished', 'status': 'blocked'})
        runtime = SimpleNamespace(config=SimpleNamespace(port=8765), runs={run.id: run},
            lock=threading.Lock(), streams=threading.BoundedSemaphore(1), create=Mock())
        handler = object.__new__(make_handler(runtime))
        handler.headers = Message()
        handler.headers['Host'] = '127.0.0.1:8765'
        handler.headers['Content-Type'] = 'application/json'
        handler.headers['Content-Length'] = str(len(raw))
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.path = path
        handler.statuses = []
        handler.send_response = handler.statuses.append
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        return handler, runtime

    def test_duplicate_json_keys_rejected(self):
        handler, runtime = self.handler(b'{"goal":"a","goal":"b"}')
        handler.do_POST()
        self.assertEqual(handler.statuses[-1], 400)
        runtime.create.assert_not_called()

    def test_nonfinite_json_rejected(self):
        for value in (b'NaN', b'Infinity', b'1e999'):
            handler, runtime = self.handler(b'{"goal":' + value + b'}')
            handler.do_POST()
            self.assertEqual(handler.statuses[-1], 400)
            runtime.create.assert_not_called()

    def test_ambiguous_framing_rejected_and_connection_closed(self):
        for name, value in [('Content-Length', '2'), ('Transfer-Encoding', 'chunked')]:
            handler, runtime = self.handler()
            handler.headers[name] = value
            handler.do_POST()
            self.assertEqual(handler.statuses[-1], 400)
            self.assertTrue(handler.close_connection)
            runtime.create.assert_not_called()

    def test_untrusted_origin_and_host_rejected(self):
        for name, value in [('Origin', 'https://example.com'), ('Host', 'localhost.evil.example')]:
            handler, runtime = self.handler()
            if name in handler.headers:
                del handler.headers[name]
            handler.headers[name] = value
            handler.do_POST()
            self.assertEqual(handler.statuses[-1], 403)
            runtime.create.assert_not_called()

    def test_truncated_body_rejected(self):
        handler, runtime = self.handler()
        handler.headers.replace_header('Content-Length', '100')
        handler.do_POST()
        self.assertEqual(handler.statuses[-1], 400)
        runtime.create.assert_not_called()

    def test_future_sse_cursor_recovers_real_events(self):
        handler, runtime = self.handler(path='/api/runs/a123456789ab/events')
        handler.headers['Last-Event-ID'] = '9999'
        handler.do_GET()
        self.assertEqual(handler.statuses[-1], 200)
        self.assertIn(b'id: 0\n', handler.wfile.getvalue())
        self.assertIn(b'id: 1\n', handler.wfile.getvalue())
        self.assertTrue(runtime.streams.acquire(blocking=False))

    def test_sse_header_disconnect_releases_capacity(self):
        handler, runtime = self.handler(path='/api/runs/a123456789ab/events')
        handler.end_headers = Mock(side_effect=BrokenPipeError())
        handler.do_GET()
        self.assertTrue(runtime.streams.acquire(blocking=False))

    def test_history_is_lightweight_detail_keeps_events(self):
        handler, _ = self.handler(path='/api/runs')
        handler.do_GET()
        run = json.loads(handler.wfile.getvalue())['runs'][0]
        self.assertEqual(run['events'], [])
        self.assertEqual(run['eventCount'], 2)

    def test_v1_aliases_serve_the_same_handlers(self):
        handler, _ = self.handler(path='/v1/runs')
        handler.do_GET()
        self.assertEqual(handler.statuses[-1], 200)
        self.assertEqual(json.loads(handler.wfile.getvalue())['runs'][0]['id'], 'a123456789ab')
        handler, _ = self.handler(path='/v1/runs/a123456789ab/events')
        handler.do_GET()
        self.assertEqual(handler.statuses[-1], 200)
        self.assertIn(b'id: 0\n', handler.wfile.getvalue())

    def test_v1_unknown_paths_still_404(self):
        for path in ('/v1/bogus', '/api/bogus'):
            handler, _ = self.handler(path=path)
            handler.do_GET()
            self.assertEqual(handler.statuses[-1], 404)

    def test_v1_run_creation_matches_unversioned(self):
        body = b'{"appId":"settings","goal":"Open About"}'
        handler, runtime = self.handler(body, path='/v1/runs')
        runtime.create.return_value = runtime.runs['a123456789ab']
        handler.do_POST()
        self.assertEqual(handler.statuses[-1], 201)
        self.assertEqual(json.loads(handler.wfile.getvalue())['run']['id'], 'a123456789ab')
        runtime.create.assert_called_once()


if __name__ == '__main__':
    unittest.main()

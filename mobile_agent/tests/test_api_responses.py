"""JSON replies retain the byte-response headers and conditional GET contract."""

import io
import json
import unittest
from unittest.mock import Mock

from mobile_agent.api_http import ApiBase, etag


class JsonResponseTests(unittest.TestCase):
    def handler(self, method="GET", headers=None):
        handler = object.__new__(ApiBase)
        handler.command = method
        handler.headers = headers or {}
        handler.wfile = io.BytesIO()
        handler.close_connection = False
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        return handler

    def test_json_replies_preserve_body_headers_and_etag_scope(self):
        value = {"message": "hello"}
        data = json.dumps(value).encode()
        for method, status in (("GET", 200), ("POST", 200), ("GET", 400)):
            with self.subTest(method=method, status=status):
                handler = self.handler(method)
                handler.send_json(value, status)
                handler.send_response.assert_called_once_with(status)
                headers = dict(call.args for call in handler.send_header.call_args_list)
                expected = {"Content-Type": "application/json", "Content-Length": str(len(data)),
                            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
                if method == "GET" and status == 200:
                    expected["ETag"] = etag(data)
                self.assertEqual(headers, expected)
                self.assertEqual(handler.wfile.getvalue(), data)
                self.assertFalse(handler.close_connection)

    def test_matching_etag_has_no_body_or_content_length(self):
        value = {"message": "hello"}
        tag = etag(json.dumps(value).encode())
        handler = self.handler(headers={"If-None-Match": tag})
        handler.send_json(value)
        handler.send_response.assert_called_once_with(304)
        self.assertEqual(dict(call.args for call in handler.send_header.call_args_list),
                         {"ETag": tag, "Cache-Control": "no-store"})
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_write_failure_closes_the_connection_without_retry(self):
        for failure in (BrokenPipeError(), TimeoutError()):
            with self.subTest(failure=type(failure).__name__):
                handler = self.handler()
                handler.wfile = Mock()
                handler.wfile.write.side_effect = failure
                handler.send_json({"ok": True})
                self.assertTrue(handler.close_connection)
                handler.wfile.write.assert_called_once()

"""Shared fakes for the seam tests (test_seam_*.py): a clean set of registries per test, and a fake HTTP request."""

import io
import json
from email.message import Message
from unittest.mock import Mock

from mobile_agent import api_routes, harness_api, journal, tracks
from mobile_agent.mcp_server import registry as mcp_registry


def isolate(testcase):
    """Every seam registry as it is now, restored after the test; the test starts with them empty."""
    saved = (harness_api._snapshot(), list(api_routes._routes), dict(journal.SCHEMAS), dict(journal.SCHEMA_OWNERS),
             list(mcp_registry._providers), set(harness_api._disabled), dict(tracks.STATUS), tracks._loaded)

    def restore():
        harness_api._restore(saved[0])
        api_routes._routes[:] = saved[1]
        journal.SCHEMAS.clear()
        journal.SCHEMAS.update(saved[2])
        journal.SCHEMA_OWNERS.clear()
        journal.SCHEMA_OWNERS.update(saved[3])
        mcp_registry._providers[:] = saved[4]
        harness_api._disabled.clear()
        harness_api._disabled.update(saved[5])
        tracks.STATUS.clear()
        tracks.STATUS.update(saved[6])
        tracks._loaded = saved[7]

    testcase.addCleanup(restore)
    tracks._loaded = False  # a Runtime built in the test registers the tracks (the stubs) afresh
    tracks.STATUS.clear()
    harness_api._restore(((), (), {}, (), (), (), (), (), ()))
    api_routes._routes.clear()
    journal.SCHEMAS.clear()
    journal.SCHEMA_OWNERS.clear()
    mcp_registry._providers.clear()
    harness_api._disabled.clear()


def request(handler_class, method, path, body=None, *, headers=None, raw=None, content_type=None):
    """Drive one request through a make_handler class without a socket. Returns (status, json-or-bytes, headers)."""
    handler = object.__new__(handler_class)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    handler.headers = Message()
    handler.headers["Host"] = "127.0.0.1:8765"
    if body is not None or raw is not None:
        handler.headers["Content-Type"] = content_type or "application/json"
        handler.headers["Content-Length"] = str(len(data))
    for name, value in (headers or {}).items():
        handler.headers[name] = value
    handler.command, handler.path = method, path
    handler.rfile, handler.wfile = io.BytesIO(data), io.BytesIO()
    handler.connection = Mock()
    statuses, sent = [], {}
    handler.send_response = statuses.append
    handler.send_header = lambda name, value: sent.__setitem__(name, value)
    handler.end_headers = lambda: None
    getattr(handler, f"do_{method}")()
    out = handler.wfile.getvalue()
    if sent.get("Content-Type") == "application/json":
        out = json.loads(out)
    return (statuses[-1] if statuses else None), out, sent

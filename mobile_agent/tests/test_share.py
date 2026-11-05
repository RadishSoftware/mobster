import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from mobile_agent import result_blocks as rb
from mobile_agent import share
from mobile_agent.tests.test_server_engine import MESSAGES, Base

FIXTURE = Path(__file__).resolve().parents[2] / "services" / "license-api" / "tests" / "fixtures" / "share-page.html"
JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")
ENVELOPE = rb.envelope([
    rb.review_queue([{"id": "c1", "to": "Sam Rivera", "handle": "@samrivera", "app": "Instagram",
                      "lastMessage": "Sam, still on for Friday?", "draft": "Yes! See you at 8.",
                      "frameId": "f0000000001"}],
                    title="Review queue", id="queue"),
    rb.checklist([{"label": "Plans", "status": "fail", "observed": "The price reads $4,999", "frameId": "f0000000001"}],
                 id="checks"),
])
RUN = {"id": "abcdef012345", "goal": "Sort Sam's DMs", "appName": "Instagram", "finishedAt": None,
       "summary": {"answer": "One DM needs a reply.", "blocks": ENVELOPE}}


class PageTests(unittest.TestCase):
    def test_never_indexable_and_people_masked_by_default(self):
        page = share.page(RUN, frames={"f0000000001": JPEG}.get, title="DMs to answer")
        head, body = page.split("</style>", 1)
        self.assertIn('<meta name="robots" content="noindex, nofollow, noarchive, nosnippet, noimageindex">', head)
        self.assertIn("<title>DMs to answer</title>", head)
        self.assertNotIn("og:", page)
        for name in ("Sam", "samrivera"):
            self.assertNotIn(name, body)
        self.assertNotIn("data:image/jpeg", body)

    def test_screens_and_names_when_chosen(self):
        page = share.page(RUN, frames={"f0000000001": JPEG}.get, people="show", screens=True)
        body = page.split("</style>", 1)[1]
        self.assertIn("Sam Rivera", body)
        self.assertIn("data:image/jpeg", body)

    def test_the_services_fixture_is_this_renderers_page(self):
        page = share.page(RUN, title="DMs to answer")
        if os.environ.get("MOBSTER_UPDATE_FIXTURES") == "1":
            FIXTURE.write_text(page, encoding="utf-8")
        self.assertEqual(FIXTURE.read_text(encoding="utf-8"), page,
                         "re-record with MOBSTER_UPDATE_FIXTURES=1 and run the license API's tests")


class Service(BaseHTTPRequestHandler):
    seen = []
    answer = (201, {"share": {"id": "A" * 22, "url": "https://mobster.dev/s/" + "A" * 22, "title": "t",
                              "createdAt": "2026-10-14T06:00:00.000Z", "expiresAt": "2026-11-13T06:00:00.000Z",
                              "live": True}})

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        Service.seen.append({"method": self.command, "path": self.path,
                             "authorization": self.headers.get("Authorization"),
                             "body": body})
        status, value = Service.answer
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = do_DELETE = _reply

    def log_message(self, *args):
        pass


class EncodedBodyTests(unittest.TestCase):
    def test_a_preencoded_body_reaches_the_connection_unchanged(self):
        body = b'{"message": "caf\xc3\xa9"}'
        connection = mock.Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = b'{"ok": true}'
        with mock.patch.object(share, "api_base", return_value="https://example.com"), \
                mock.patch.object(share.pinning, "PinnedHTTPSConnection", return_value=connection):
            self.assertEqual(share.call("POST", "/api/feedback", "test-token", body, limit=64000),
                             (200, {"ok": True}))
        request = connection.request.call_args
        self.assertIs(request.kwargs["body"], body)
        self.assertEqual(request.kwargs["headers"]["Content-Type"], "application/json")
        connection.getresponse.return_value.read.assert_called_once_with(64000)
        connection.close.assert_called_once()


class ClientTests(unittest.TestCase):
    def setUp(self):
        Service.seen = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        env = mock.patch.dict(os.environ, {"MOBSTER_ACCOUNT_API": f"http://127.0.0.1:{self.server.server_port}"})
        env.start()
        self.addCleanup(env.stop)

    def test_create_sends_the_page_with_the_token_only_in_the_header(self):
        made = share.create("token-0123456789abcdef", title="DMs", html="<!doctype html><html></html>", expires_days=7)
        self.assertEqual(made["id"], "A" * 22)
        call = Service.seen[0]
        self.assertEqual((call["method"], call["path"]), ("POST", "/api/shares"))
        self.assertEqual(call["authorization"], "Bearer token-0123456789abcdef")
        self.assertEqual(call["body"], {"title": "DMs", "html": "<!doctype html><html></html>", "expiresDays": 7})

    def test_refusals_read_as_sentences(self):
        Service.answer = (401, {"error": "unauthorized", "message": "Sign in again."})
        try:
            with self.assertRaises(share.ShareError) as caught:
                share.create("token-0123456789abcdef", title="x", html="<!doctype html>")
            self.assertEqual((caught.exception.code, caught.exception.status), ("session", 401))
            Service.answer = (409, {"error": "too_many_shares", "message": "You have 200 links that still work."})
            with self.assertRaises(share.ShareError) as caught:
                share.create("token-0123456789abcdef", title="x", html="<!doctype html>")
            self.assertEqual(str(caught.exception), "You have 200 links that still work.")
            for status, code, message in ((403, "plan_required", "Sharing needs a Mobster plan or a running trial."),
                                          (409, "share_space_full", "Your links that still work hold 50 MB.")):
                Service.answer = (status, {"error": code, "message": message})
                with self.assertRaises(share.ShareError) as caught:
                    share.create("token-0123456789abcdef", title="x", html="<!doctype html>")
                self.assertEqual((caught.exception.code, caught.exception.status, str(caught.exception)),
                                 (code, status, message))
        finally:
            Service.answer = ClientTests.default

    def test_unreachable(self):
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaises(share.ShareError) as caught:
            share.revoke("token-0123456789abcdef", "A" * 22)
        self.assertEqual(caught.exception.code, "unreachable")

    def test_an_unknown_scheme_falls_back_to_the_real_service(self):
        with mock.patch.dict(os.environ, {"MOBSTER_ACCOUNT_API": "http://evil.test"}), \
                mock.patch.object(share, "ACCOUNT_API", "https://api.mobster.dev"):
            self.assertEqual(share.api_base(), "https://api.mobster.dev")


ClientTests.default = Service.answer


class RuntimeTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-000000000000"

    def finished(self, runtime):
        run = runtime.create(MESSAGES["id"], "Sort Sam's DMs", "live")
        run.finish({"event": "result", "status": "completed", "answer": "One DM needs a reply.", "blocks": ENVELOPE})
        if run.lease:
            run.lease.close()
            run.lease = None
        runtime.active_runs.clear()
        return run

    def test_share_records_the_link_and_never_the_token(self):
        runtime = self.runtime()
        run = self.finished(runtime)
        made = {"id": "B" * 22, "url": "https://mobster.dev/s/"
                + "B" * 22, "title": "DMs", "createdAt": "x", "expiresAt": "y"}
        with mock.patch("mobile_agent.share.create", return_value=made) as create:
            status, body, _ = self.request(runtime, "POST", f"/api/runs/{run.id}/share",
                                           {"accessToken": "token-0123456789abcdef", "title": "DMs", "expiresDays": 7})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["share"]["url"], made["url"])
        args = create.call_args
        self.assertEqual(args.args[0], "token-0123456789abcdef")
        self.assertEqual(args.kwargs["expires_days"], 7)
        self.assertIn('content="noindex', args.kwargs["html"])
        self.assertNotIn("Sam", args.kwargs["html"].split("</style>", 1)[1])
        saved = {item["id"]: item for item in runtime.journal.load()}[run.id]
        self.assertEqual(saved["shares"][0]["id"], "B" * 22)
        self.assertNotIn("token-0123456789abcdef", json.dumps(saved))
        self.assertNotIn("token-0123456789abcdef", json.dumps(run.public()))

        with mock.patch("mobile_agent.share.revoke",
                        return_value={"id": "B" * 22, "revokedAt": "z", "live": False}) as revoke:
            status, body, _ = self.request(runtime, "POST", f"/api/runs/{run.id}/share/{'B' * 22}/revoke",
                                           {"accessToken": "token-0123456789abcdef"})
        self.assertEqual((status, body["share"]["revokedAt"]), (200, "z"))
        self.assertEqual(revoke.call_args.args, ("token-0123456789abcdef", "B" * 22))

    def test_refusals(self):
        runtime = self.runtime()
        run = self.finished(runtime)
        for body, code in (({"title": "x"}, 401), ({"accessToken": "token-0123456789abcdef", "expiresDays": 365}, 400),
                           ({"accessToken": "token-0123456789abcdef", "people": "everyone"}, 400)):
            with self.subTest(body=body), mock.patch("mobile_agent.share.create") as create:
                self.assertEqual(self.request(runtime, "POST", f"/api/runs/{run.id}/share", body)[0], code)
                create.assert_not_called()
        status, _, _ = self.request(runtime, "POST", f"/api/runs/{run.id}/share/{'C' * 22}/revoke",
                                    {"accessToken": "token-0123456789abcdef"})
        self.assertEqual(status, 404)

    def test_preview_is_the_page_as_an_image(self):
        runtime = self.runtime()
        run = self.finished(runtime)
        with mock.patch("mobile_agent.artifacts.render", return_value=b"\x89PNG fake") as render:
            status, body, _ = self.request(runtime, "POST", f"/api/runs/{run.id}/share/preview", {"people": "mask"})
        self.assertEqual(status, 200)
        self.assertTrue(body["image"].startswith("data:image/png;base64,"))
        self.assertIn('content="noindex', render.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

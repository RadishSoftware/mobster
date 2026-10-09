"""Seam S6.6: with the Mac app's session configured, approving, choosing, the two safety settings, putting a file on
the phone and resetting the Wi-Fi link need X-Mobster-App-Session; declines, stops, reads and answers to clarifying
questions stay token-only; X-Mobster-Origin grants nothing; with no session (`mobster serve`) nothing changes.
Offline."""

import os
import threading
import time
import unittest
from unittest.mock import patch

from mobile_agent import api_routes, server, tracks
from mobile_agent.api_routes import Response, Route
from mobile_agent.server import make_handler
from mobile_agent.tests.seam_support import isolate, request
from mobile_agent.tests.test_server_engine import Base

SESSION = "s" * 40
APP = {"X-Mobster-App-Session": SESSION}


class AppSessionTests(Base):
    def setUp(self):
        super().setUp()
        isolate(self)
        tracks._loaded = True  # the seam's own stand-in routes, not the tracks' real ones under the same paths
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        self.app = self.runtime(managed=True)
        self.with_session = make_handler(self.app, app_session=SESSION)
        self.without = make_handler(self.app)
        self.put = []
        api_routes.register(Route("files", "POST", "/api/phone/files/put",
                                  lambda r: self.put.append(r.app_session) or Response.json({"ok": True})))
        api_routes.register(Route("wireless", "POST", "/api/wireless/reset", lambda r: Response.json({"ok": True})))
        api_routes.register(Route("files", "GET", "/api/phone/files", lambda r: Response.json({"files": []})))

    def pending(self, kind="commit", **extra):
        """A run waiting on an approval of ``kind``; returns (run, approval id, the waiting thread, its answer)."""
        run = self.app.create("messages", "Text Sam", "live")
        answer = []
        question = {"kind": kind, "operation": "ASK_USER" if kind == "clarify" else "TAP",
                    "label": "Which Sam?" if kind == "clarify" else "Send", "text": None, "title": "Send?", **extra}
        thread = threading.Thread(target=lambda: answer.append(run.request_approval(question, timeout=5)))
        thread.start()
        for _ in range(500):
            if run.public()["approval"]:
                break
            time.sleep(.005)
        self.addCleanup(lambda: (run.stop.set(), thread.join(5), self.finish(self.app, run)))
        return run, run.public()["approval"]["id"], thread, answer

    def test_approving_needs_the_app(self):
        run, approval, thread, answer = self.pending()
        path = f"/api/runs/{run.id}/approval"
        for headers in ({}, {"X-Mobster-Origin": "app"}, {"X-Mobster-App-Session": "wrong" * 8}):
            with self.subTest(headers=headers):
                status, data, _ = request(self.with_session, "POST", path, {"id": approval, "approve": True},
                                          headers=headers)
                self.assertEqual((status, data["code"]), (403, "app_only"))
                self.assertEqual(data["error"], "Do this in the Mobster app.")
        status, _, _ = request(self.with_session, "POST", path, {"id": approval, "approve": True}, headers=APP)
        self.assertEqual(status, 200)
        thread.join(5)
        self.assertEqual(answer, ["approved"])

    def test_declines_redirects_and_stops_stay_token_only(self):
        run, approval, thread, answer = self.pending()
        status, _, _ = request(self.with_session, "POST", f"/api/runs/{run.id}/approval",
                               {"id": approval, "approve": False, "instruction": "Say 7pm instead"})
        self.assertEqual(status, 200)
        thread.join(5)
        self.assertEqual(answer, ["redirected:Say 7pm instead"])
        status, _, _ = request(self.with_session, "POST", f"/api/runs/{run.id}/stop", {})
        self.assertEqual(status, 200)

    def test_a_choice_on_a_loop_question_needs_the_app(self):
        run, approval, thread, answer = self.pending(kind="question", choices=[{"id": "all", "label": "All"}])
        path = f"/api/runs/{run.id}/approval"
        status, _, _ = request(self.with_session, "POST", path, {"id": approval, "approve": True, "choice": "all"})
        self.assertEqual(status, 403)
        status, _, _ = request(self.with_session, "POST", path, {"id": approval, "approve": True, "choice": "all"},
                               headers=APP)
        self.assertEqual(status, 200)
        thread.join(5)
        self.assertEqual(answer, ["choice:all"])

    def test_answers_to_clarifying_questions_stay_token_only(self):
        run, approval, thread, answer = self.pending(kind="clarify", question="Which Sam?")
        status, _, _ = request(self.with_session, "POST", f"/api/runs/{run.id}/approval",
                               {"id": approval, "approve": True, "answer": "Sam Lee"})
        self.assertEqual(status, 200)
        thread.join(5)
        self.assertEqual(answer, ["answer:Sam Lee"])

    def test_the_safety_settings_need_the_app_and_the_rest_do_not(self):
        for body in ({"askBeforeActing": False}, {"bypassChecks": True}, {"askBeforeActing": True, "videoFps": 30}):
            with self.subTest(body=body):
                status, data, _ = request(self.with_session, "POST", "/api/settings", body)
                self.assertEqual((status, data["code"]), (403, "app_only"))
        self.assertEqual(request(self.with_session, "POST", "/api/settings", {"askBeforeActing": False},
                                 headers=APP)[0], 200)
        self.assertEqual(request(self.with_session, "POST", "/api/settings", {"defaultEngine": "smart"})[0], 200)
        self.assertEqual(request(self.with_session, "GET", "/api/settings")[0], 200)

    def test_file_put_and_wireless_reset_need_the_app(self):
        self.assertEqual(request(self.with_session, "POST", "/api/phone/files/put", {"name": "menu.pdf"})[0], 403)
        self.assertEqual(self.put, [])
        self.assertEqual(request(self.with_session, "POST", "/api/phone/files/put", {"name": "menu.pdf"},
                                 headers=APP)[0], 200)
        self.assertEqual(self.put, [True])
        self.assertEqual(request(self.with_session, "POST", "/api/wireless/reset", {"device": "x"})[0], 403)
        self.assertEqual(request(self.with_session, "POST", "/api/wireless/reset", {"device": "x"},
                                 headers=APP)[0], 200)
        self.assertEqual(request(self.with_session, "GET", "/api/phone/files")[0], 200)  # reads stay token-only

    def test_without_a_session_the_api_is_unchanged(self):
        run, approval, thread, answer = self.pending()
        status, _, _ = request(self.without, "POST", f"/api/runs/{run.id}/approval",
                               {"id": approval, "approve": True}, headers={"X-Mobster-App-Session": "anything"})
        self.assertEqual(status, 200)
        thread.join(5)
        self.assertEqual(answer, ["approved"])
        self.assertEqual(request(self.without, "POST", "/api/settings", {"askBeforeActing": False})[0], 200)
        self.assertEqual(request(self.without, "POST", "/api/phone/files/put", {"name": "menu.pdf"})[0], 200)
        self.assertEqual(self.put, [False])

    def test_the_secret_comes_from_the_shell_and_leaves_the_environment(self):
        with patch.dict(os.environ, {"MOBSTER_APP_SESSION": SESSION}):
            self.assertEqual(server.app_session_secret(), SESSION)
            self.assertNotIn("MOBSTER_APP_SESSION", os.environ)
        self.assertIsNone(server.app_session_secret())
        with patch.dict(os.environ, {"MOBSTER_APP_SESSION": "short"}), self.assertRaises(ValueError):
            server.app_session_secret()


if __name__ == "__main__":
    unittest.main()

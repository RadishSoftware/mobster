"""The server's engines: status, admission, settings, costs and limits, frames, redirects, review and delete,
and a whole Smart task through Runtime.work. Offline: no network, phone or model."""

import io
import json
import os
from email.message import Message
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from mobile_agent import engines
from mobile_agent.journal import Lease
from mobile_agent.server import ANY_APP, APIError, FrameStore, Run, Runtime, make_handler
from mobile_agent.tests.test_engines import CLEAN, COMPOSE, Script, item, sent
from mobile_agent.tests.test_server_hardening import APP

READY = {"device": True, "ready": True, "health": None, "can_act": True, "driver": "wda", "screen_reading": False,
         "notes": []}
MESSAGES = {"id": "messages", "name": "Messages", "bundleId": "com.apple.MobileSMS", "installed": True}


def jpeg(width=1178, height=2556):
    out = io.BytesIO()
    Image.new("RGB", (width, height), (40, 90, 160)).save(out, "JPEG")
    return out.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-engines-"))
        self.env = patch.dict(os.environ, dict(CLEAN), clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        # Admission only: the task worker does nothing (a test calls Runtime.work itself). Threads stay real:
        # the loop's contract compile, the frame store and approvals need them.
        self.worker = patch.object(Runtime, "work", lambda runtime, run: None)
        self.worker.start()
        self.addCleanup(self.worker.stop)
        lease = patch("mobile_agent.server.Lease.device", side_effect=lambda _: Lease(self.root / "device.lock"))
        lease.start()
        self.addCleanup(lease.stop)
        self.runtimes = []
        self.addCleanup(self.close)

    def close(self):
        for runtime in self.runtimes:
            for run in runtime.runs.values():
                if run.lease:
                    run.lease.close()
                    run.lease = None
            runtime.close()

    def runtime(self, managed=False, apps=(APP, MESSAGES)):
        state = self.root if not self.runtimes else self.root / f"r{len(self.runtimes)}"
        config = SimpleNamespace(wda_url="http://127.0.0.1:8203", session=None, enable_live=True, port=8765,
                                 state_db=str(state / "runs.sqlite3"), env_file=str(state / "agent.env"))
        with patch("mobile_agent.server.WdaVideo"), patch("mobile_agent.server.ManualControl"):
            runtime = Runtime(config)
        runtime.target_status = Mock(return_value=dict(READY))
        runtime.apps = Mock(return_value=[dict(app) for app in apps])
        if managed:
            runtime.setup = SimpleNamespace(env_file=config.env_file)
        self.runtimes.append(runtime)
        return runtime

    def finish(self, runtime, run, status="completed"):
        run.finish({"event": "result", "status": status})
        if run.lease:
            run.lease.close()
            run.lease = None
        runtime.active_runs.clear()

    def request(self, runtime, method, path, body=None):
        handler = object.__new__(make_handler(runtime))
        raw = json.dumps(body).encode() if body is not None else b""
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        if body is not None:
            handler.headers["Content-Type"] = "application/json"
            handler.headers["Content-Length"] = str(len(raw))
        handler.command, handler.path = method, path
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.connection = Mock()
        statuses, headers = [], {}
        handler.send_response = statuses.append
        handler.send_header = lambda name, value: headers.__setitem__(name, value)
        handler.end_headers = lambda: None
        getattr(handler, f"do_{method}")()
        data = handler.wfile.getvalue()
        return statuses[-1], (json.loads(data) if headers.get("Content-Type") == "application/json" else data), headers


class EngineRuntimeTests(Base):
    # -- status -----------------------------------------------------------------------------------

    def test_a_new_user_defaults_to_smart_and_is_told_to_add_an_openai_key(self):
        status = self.runtime(managed=True).status()
        self.assertEqual(status["defaultEngine"], "smart")
        self.assertEqual(status["engines"]["smart"], {"available": False, "reason": "Add your OpenAI key in Setup",
                                                      "model": "gpt-5.6-sol", "provider": "OpenAI"})
        self.assertEqual(status["engines"]["fast"]["reason"], "Add your Jev key in Setup")
        self.assertEqual(status["limitations"][0], "Add your OpenAI key in Setup")
        self.assertNotIn("Text/recovery helper is not configured", status["limitations"])
        self.assertFalse(status["live_enabled"])
        self.assertEqual(self.runtime().status()["limitations"][0],
                         "Add OPENAI_API_KEY or ANTHROPIC_API_KEY to the agent's env file")

    def test_a_jev_only_user_stays_on_fast(self):
        os.environ["TYPESAFE_API_KEY"] = "jev-key-000000000000"
        status = self.runtime(managed=True).status()
        self.assertEqual(status["defaultEngine"], "fast")
        self.assertTrue(status["live_enabled"])
        self.assertTrue(status["jev_configured"])
        self.assertNotIn("Add your Jev key in Setup", status["limitations"])

    def test_a_key_that_cannot_reach_the_model_is_said_and_never_swapped_for_fast(self):
        os.environ.update(OPENAI_API_KEY="sk-test-000000000000", TYPESAFE_API_KEY="jev-key-000000000000")
        runtime = self.runtime(managed=True)
        runtime.model_reach.record("sk-test-000000000000", False, "Your OpenAI key can't use gpt-5.6-sol.")
        status = runtime.status()
        self.assertEqual(status["engines"]["smart"]["reason"], "Your OpenAI key can't use gpt-5.6-sol.")
        self.assertEqual(status["limitations"][0], "Your OpenAI key can't use gpt-5.6-sol.")
        with self.assertRaises(APIError) as refused:
            runtime.create("settings", "Open About", "live")
        self.assertEqual((refused.exception.status, refused.exception.code), (409, "engine_unavailable"))
        self.assertEqual(runtime.runs, {})
        run = runtime.create("settings", "Open About", "live", engine="fast")  # chosen explicitly
        self.assertEqual(run.engine, "fast")

    # -- admission --------------------------------------------------------------------------------

    def test_smart_runs_anywhere_and_carries_its_engine_and_estimate(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        runtime = self.runtime()
        run = runtime.create(None, "What iOS version is this iPhone on?", "live")
        self.assertEqual((run.engine, run.app), ("smart", ANY_APP))
        self.assertTrue(0 < run.estimate_usd < .2)
        public = run.public()
        self.assertEqual((public["engine"], public["appId"], public["costUsd"], public["reviewedOk"]),
                         ("smart", "any", None, False))
        self.finish(runtime, run)
        started_in = runtime.create("messages", "Text Sam hi", "live", engine="smart")
        self.assertEqual(started_in.app["bundleId"], "com.apple.MobileSMS")
        self.finish(runtime, started_in)
        with self.assertRaisesRegex(ValueError, "Choose an app"):
            runtime.create(None, "Open About", "live", engine="fast")
        with self.assertRaisesRegex(ValueError, "smart or fast"):
            runtime.create("settings", "Open About", "live", engine="turbo")
        with self.assertRaisesRegex(ValueError, "dry run"):
            runtime.create("settings", "Open About", "live", engine="smart", dry_run=True)

    def test_an_explicit_engine_is_part_of_the_request_identity(self):
        os.environ.update(OPENAI_API_KEY="sk-test-000000000000", TYPESAFE_API_KEY="jev-key-000000000000")
        runtime = self.runtime()
        run, _ = runtime.create("settings", "Open About", "live", "key-1", engine="fast")
        self.finish(runtime, run)
        with self.assertRaises(APIError) as conflict:
            runtime.create("settings", "Open About", "live", "key-1", engine="smart")
        self.assertEqual(conflict.exception.code, "idempotency_conflict")

    def test_new_tasks_stop_at_the_monthly_limit(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        runtime = self.runtime()
        run = runtime.create(None, "Open Settings", "live")
        for index, kind in enumerate(("inference_started", "inference_finished")):
            run.emit({"event": kind, "provider": "openai", "call_id": "c1", "model": "gpt-5.6-sol",
                      **({"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                          "cost_nanodollars": 2_500_000_000} if index else {})})
        self.assertEqual(run.public()["costUsd"], 2.5)
        self.finish(runtime, run)
        self.assertEqual(runtime.month_usd(), 2.5)
        runtime.update_settings({"monthlyLimitUsd": 2})
        with self.assertRaises(APIError) as refused:
            runtime.create(None, "Open Settings", "live")
        self.assertEqual((refused.exception.code, refused.exception.details["monthUsd"]), ("monthly_limit_reached", 2.5))
        runtime.update_settings({"monthlyLimitUsd": None})
        self.finish(runtime, runtime.create(None, "Open Settings", "live"))

    # -- settings ---------------------------------------------------------------------------------

    def test_engine_and_limit_settings_persist_and_validate(self):
        runtime = self.runtime()
        settings = runtime.settings()
        self.assertEqual({k: settings[k] for k in ("defaultEngine", "taskCostLimitUsd", "monthlyLimitUsd")},
                         {"defaultEngine": "smart", "taskCostLimitUsd": 1.0, "monthlyLimitUsd": None})
        changed = runtime.update_settings({"defaultEngine": "fast", "taskCostLimitUsd": 0.5, "monthlyLimitUsd": 20})
        self.assertEqual((changed["defaultEngine"], changed["taskCostLimitUsd"], changed["monthlyLimitUsd"]),
                         ("fast", .5, 20.0))
        saved = Path(runtime.config.env_file).read_text()
        for line in ("MOBSTER_DEFAULT_ENGINE=fast", "MOBSTER_TASK_COST_LIMIT=0.50", "MOBSTER_MONTHLY_LIMIT=20.00"):
            self.assertIn(line, saved)
        self.assertEqual(runtime.task_cap(), .5)
        off = runtime.update_settings({"taskCostLimitUsd": None})
        self.assertIsNone(off["taskCostLimitUsd"])
        self.assertIsNone(runtime.task_cap())
        runtime.config.spend_cap_usd = 3.0
        self.assertEqual(runtime.task_cap(), 3.0)
        for bad in ({"defaultEngine": "turbo"}, {"taskCostLimitUsd": 0}, {"taskCostLimitUsd": "1"},
                    {"monthlyLimitUsd": 10**6}, {"taskCostLimitUsd": True}):
            with self.assertRaises(ValueError):
                runtime.update_settings(bad)

    # -- HTTP ---------------------------------------------------------------------------------------

    def test_estimates_name_both_engines(self):
        os.environ["TYPESAFE_API_KEY"] = "jev-key-000000000000"
        runtime = self.runtime(managed=True)
        status, body, _ = self.request(runtime, "POST", "/api/usage/estimate",
                                       {"goal": "Text Sam I'm running late in Messages", "engine": "smart"})
        self.assertEqual(status, 200)
        self.assertEqual(body["basis"], "planning_scenario")  # the Fast planning scenario stays as it was
        self.assertEqual(body["engine"], "smart")
        smart, fast = body["engines"]["smart"], body["engines"]["fast"]
        self.assertEqual((smart["available"], smart["reason"], smart["provider"]), (False, "Add your OpenAI key in Setup", "OpenAI"))
        self.assertTrue(0 < smart["lowUsd"] < smart["highUsd"] < 1)
        self.assertEqual((fast["available"], fast["provider"], fast["model"]), (True, "TypeSafe", "jev-latest"))
        self.assertEqual(self.request(runtime, "POST", "/api/usage/estimate", {"goal": "x", "engine": "turbo"})[0], 400)
        status, body, _ = self.request(runtime, "GET", "/api/usage/estimate")
        self.assertIn("smart", body["engines"])

    def test_smart_prices_the_structure_it_builds_for_csv_json_and_yaml(self):
        """Result CSV, JSON or YAML with no schema costs Smart the same small call a schema does."""
        runtime = self.runtime(managed=True)
        estimate = lambda **extra: self.request(runtime, "POST", "/api/usage/estimate", {"goal": "Export my reminders", **extra})
        plain = engines.midpoint(estimate()[1]["engines"]["smart"])
        for fmt in ("csv", "json", "yaml"):
            status, body, _ = estimate(outputFormat=fmt)
            self.assertEqual(status, 200, body)
            self.assertAlmostEqual(engines.midpoint(body["engines"]["smart"]) - plain, engines.SMART_STRUCTURE_USD, delta=.001)
        for fmt in ("auto", "text", "markdown"):
            self.assertAlmostEqual(engines.midpoint(estimate(outputFormat=fmt)[1]["engines"]["smart"]), plain, delta=.0001)
        self.assertEqual(estimate(outputFormat="xml")[0], 400)
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        run = runtime.create(None, "Export my reminders", "live", output_format="csv")
        self.assertAlmostEqual(run.estimate_usd - plain, engines.SMART_STRUCTURE_USD, delta=.001)

    def test_usage_reports_this_months_spend(self):
        runtime = self.runtime()
        runtime.update_settings({"monthlyLimitUsd": 25})
        status, body, _ = self.request(runtime, "GET", "/api/usage")
        self.assertEqual((status, body["monthUsd"], body["monthlyLimitUsd"]), (200, 0.0, 25.0))
        self.assertIn("totals", body)

    def test_runs_take_an_engine_and_bad_ones_are_refused(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        runtime = self.runtime()
        status, body, _ = self.request(runtime, "POST", "/api/runs", {"goal": "Open Settings", "engine": "smart"})
        self.assertEqual((status, body["run"]["engine"], body["run"]["appName"]), (201, "smart", "Any app"))
        self.finish(runtime, runtime.runs[body["run"]["id"]])
        status, body, _ = self.request(runtime, "POST", "/api/runs", {"appId": "settings", "goal": "Open Settings",
                                                                      "engine": "fast"})
        self.assertEqual((status, body["code"]), (409, "engine_unavailable"))
        self.assertEqual(body["error"], "Add TYPESAFE_API_KEY to the agent's env file")

    def test_a_redirect_reaches_the_agent_but_not_the_journal(self):
        runtime = self.runtime()
        run = Run(dict(MESSAGES), "Text Sam I'm running late", "live", journal=runtime.journal)
        runtime.journal.create(run.metadata())
        runtime.runs[run.id] = run
        answers = []
        thread = threading.Thread(target=lambda: answers.append(run.request_approval(
            {"kind": "commit", "operation": "TAP", "label": "Send", "text": "I'm running late",
             "title": "Send this message to Sam?", "act": "send_message", "app_name": "Messages",
             "target": {"x": .8, "y": .9, "w": .1, "h": .04}}, timeout=5)))
        thread.start()
        for _ in range(200):
            if run.public()["approval"]:
                break
            time.sleep(.01)
        pending = run.public()["approval"]
        self.assertEqual((pending["kind"], pending["title"], pending["act"], pending["target"], pending["app"]),
                         ("commit", "Send this message to Sam?", "send_message", {"x": .8, "y": .9, "w": .1, "h": .04},
                          "Messages"))
        path = f"/api/runs/{run.id}/approval"
        self.assertEqual(self.request(runtime, "POST", path, {"id": pending["id"], "approve": True,
                                                              "instruction": "say ten"})[0], 400)
        status, _, _ = self.request(runtime, "POST", path, {"id": pending["id"], "approve": False,
                                                            "instruction": "  say ten minutes late "})
        thread.join(3)
        self.assertEqual((status, answers), (200, ["redirected:say ten minutes late"]))
        requested = next(e for e in run.events if e["event"] == "approval_requested")
        self.assertEqual((requested["kind"], requested["title"], requested["act"]),
                         ("commit", "Send this message to Sam?", "send_message"))
        resolved = next(e for e in run.events if e["event"] == "approval_resolved")
        self.assertEqual(resolved["decision"], "redirected")
        self.assertNotIn("ten minutes", json.dumps(run.events))

    def test_review_and_delete(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        runtime = self.runtime()
        run = runtime.create(None, "Open Settings", "live")
        self.assertEqual(self.request(runtime, "POST", f"/api/runs/{run.id}/review", {"ok": True})[1]["code"], "run_active")
        self.assertEqual(self.request(runtime, "DELETE", f"/api/runs/{run.id}")[0], 409)
        run.emit({"event": "step", "n": 1, "text": "Opened Settings", "_frame": jpeg()})
        self.finish(runtime, run)
        status, body, _ = self.request(runtime, "POST", f"/api/runs/{run.id}/review", {"ok": True})
        self.assertEqual((status, body["run"]["reviewedOk"]), (200, True))
        self.assertTrue(next(r for r in runtime.journal.load() if r["id"] == run.id)["reviewedOk"])
        self.assertEqual(self.request(runtime, "POST", f"/api/runs/{run.id}/review", {"ok": "yes"})[0], 400)
        runtime.frames.worker.submit(lambda: None).result()  # the frame is on disk
        self.assertTrue((self.root / "frames" / run.id).exists())
        status, body, _ = self.request(runtime, "DELETE", f"/api/runs/{run.id}")
        self.assertEqual((status, body), (200, {"deleted": run.id}))
        self.assertNotIn(run.id, runtime.runs)
        self.assertNotIn(run.id, [r["id"] for r in runtime.journal.load()])
        self.assertFalse((self.root / "frames" / run.id).exists())
        self.assertEqual(self.request(runtime, "DELETE", f"/api/runs/{run.id}")[0], 404)

    def test_a_frame_is_stored_once_and_served_small(self):
        runtime = self.runtime()
        run = Run(dict(APP), "Open About", "live", journal=runtime.journal, frames=runtime.frames)
        runtime.journal.create(run.metadata())
        runtime.runs[run.id] = run
        event = {"event": "step", "n": 1, "text": "Tapped General", "_frame": jpeg()}
        run.emit(event)
        frame_id = run.events[-1]["frameId"]
        self.assertEqual(event["frameId"], frame_id)  # the caller learns the id too
        self.assertNotIn("_frame", run.events[-1])
        status, data, headers = self.request(runtime, "GET", f"/api/runs/{run.id}/frames/{frame_id}")
        self.assertEqual((status, headers["Content-Type"]), (200, "image/jpeg"))
        self.assertLessEqual(max(Image.open(io.BytesIO(data)).size), 480)
        self.assertEqual(self.request(runtime, "GET", f"/api/runs/{run.id}/frames/f0000000000")[0], 404)
        self.assertEqual(self.request(runtime, "GET", "/api/runs/aaaaaaaaaaaa/frames/f0000000000")[0], 404)
        store = FrameStore(None)
        self.addCleanup(store.close)
        frame = store.put("r", jpeg(300, 200))
        self.assertEqual(max(Image.open(io.BytesIO(store.get("r", frame))).size), 300)
        self.assertIsNone(store.get("r", "../../etc"))


class SmartTaskTests(Base):
    """One Smart task end to end through Runtime.work, with a scripted model and phone."""

    class Phone:
        def __init__(self, screens):
            self.screens, self.index, self.actions, self.calls = list(screens), 0, [], []
            self.last_image = None

        def observe(self, timeout=10):
            return self.screens[min(self.index, len(self.screens) - 1)]

        def execute(self, operation, target, snapshot, text=None, timeout=10):
            self.actions.append((operation, getattr(target, "label", target), text))
            self.index += 1

        def call(self, *args, **kwargs):
            self.calls.append(args)

        def close(self):
            pass

    def setUp(self):
        super().setUp()
        # The config's WDA URL is a simulator port on a benchmark Mac: never ask a real runner for a session.
        session = patch("mobile_agent.server.resolve_wda_session", return_value="s")
        session.start()
        self.addCleanup(session.stop)

    def test_a_smart_task_asks_at_send_and_finishes_with_proof(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        runtime = self.runtime()
        run = runtime.create("messages", "Text Sam I'm running late", "live")
        phone = self.Phone([COMPOSE, COMPOSE, sent("I'm running late"), sent("I'm running late")])
        script = Script([[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)], [("DONE", None, None)]],
                        [item()], answer="Sent Sam “I'm running late”.")
        frames = iter([jpeg()] * 20)

        def approve_when_asked():
            for _ in range(500):
                pending = run.public()["approval"]
                if pending:
                    run.answer_approval(pending["id"], True)
                    return
                time.sleep(.01)

        helper = threading.Thread(target=approve_when_asked)
        helper.start()
        with patch("mobile_agent.server.build_target_driver", return_value=phone), \
                patch("mobile_agent.server.prepare_wda_phone"), \
                patch("mobile_agent.engines.build_client", return_value=script), \
                patch("mobile_agent.frontier.video_frame", side_effect=lambda *a, **k: next(frames)):
            self.worker.stop()
            try:
                runtime.work(run)
            finally:
                self.worker.start()
        helper.join(5)
        if run.lease:
            run.lease.close()
            run.lease = None
        self.assertEqual(phone.calls[0], ("POST", "/wda/apps/activate", {"bundleId": "com.apple.MobileSMS"}))
        self.assertEqual(phone.actions, [("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)])
        summary = run.summary
        self.assertEqual((run.status, summary["outcome"], summary["engine"]), ("completed", "done", "smart"))
        self.assertEqual(summary["answer"], "Sent Sam “I'm running late”.")
        self.assertEqual([p["quote"] for p in summary["proof"]], ["I'm running late"])  # as sent, once
        self.assertRegex(summary["proof"][0]["frameId"], r"^f[0-9a-f]{10}$")
        self.assertEqual(script.prompts[0].split("\n")[0], "Request: In Messages: Text Sam I'm running late")
        kinds = [e["event"] for e in run.events]
        for kind in ("plan", "contract_item", "step", "receipt", "cost", "inference_finished", "approval_requested"):
            self.assertIn(kind, kinds)
        self.assertEqual(kinds.count("approval_requested"), 1)
        self.assertNotIn("frontier_prompt", kinds)
        requested = next(e for e in run.events if e["event"] == "approval_requested")
        self.assertEqual((requested["kind"], requested["target"]), ("commit", {"x": .8, "y": .9, "w": .1, "h": .04}))
        # 3 model calls (the contract and two turns) at list price, recorded as the run's cost.
        self.assertEqual(kinds.count("inference_finished"), 3)
        self.assertAlmostEqual(summary["costUsd"], run.cost_usd)
        self.assertGreater(run.cost_usd, 0)
        step = next(e for e in run.events if e["event"] == "step")
        status, data, _ = self.request(runtime, "GET", f"/api/runs/{run.id}/frames/{step['frameId']}")
        self.assertEqual(status, 200)
        self.assertEqual(runtime.journal.usage()["providers"][0]["provider"], "openai")

    def test_stop_ends_a_running_smart_task_before_its_next_action(self):
        """Stop (⌘.) pressed after the first of several typing actions: nothing more reaches the phone."""
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        runtime = self.runtime()
        run = runtime.create("messages", "Type hello five times in the message field", "live")
        Phone = self.Phone

        class StoppingPhone(Phone):
            def execute(self, *args, **kwargs):
                super().execute(*args, **kwargs)
                if len(self.actions) == 1:
                    runtime.stop_run(run.id)

        phone = StoppingPhone([COMPOSE] * 12)
        steps = [[("TYPE", "Message", f"hello {i}")] for i in range(5)] + [[("DONE", None, None)]]
        script = Script(steps, [item(kind="READ", app="Messages", what="Sam", act="none", quote="Sam")], answer="Typed.")
        frames = iter([jpeg()] * 50)
        with patch("mobile_agent.server.build_target_driver", return_value=phone), \
                patch("mobile_agent.server.prepare_wda_phone"), \
                patch("mobile_agent.engines.build_client", return_value=script), \
                patch("mobile_agent.frontier.video_frame", side_effect=lambda *a, **k: next(frames)):
            self.worker.stop()
            try:
                runtime.work(run)
            finally:
                self.worker.start()
        if run.lease:
            run.lease.close()
            run.lease = None
        self.assertEqual(phone.actions, [("TYPE", "Message", "hello 0")])
        self.assertEqual((run.status, run.summary["outcome"], run.summary["status"]), ("stopped", "stopped", "stopped"))
        self.assertEqual(len(script.prompts), 1)

    def test_a_smart_task_that_cannot_reach_the_model_says_so(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        runtime = self.runtime()
        run = runtime.create(None, "Open Settings", "live")

        class Missing(Script):
            def complete(self, messages, schema, timeout=60):
                raise RuntimeError('OpenAI HTTP 404: {"error": {"message": "The model gpt-5.6-sol does not exist"}}')

        phone = self.Phone([COMPOSE])
        with patch("mobile_agent.server.build_target_driver", return_value=phone), \
                patch("mobile_agent.server.prepare_wda_phone"), \
                patch("mobile_agent.engines.build_client", return_value=Missing([], [])):
            self.worker.stop()
            try:
                runtime.work(run)
            finally:
                self.worker.start()
        if run.lease:
            run.lease.close()
            run.lease = None
        self.assertEqual(phone.calls, [])  # no start app: the task begins on the Home Screen
        self.assertEqual((run.summary["outcome"], run.summary["reason"]),
                         ("couldnt_finish", "Your OpenAI key can't use gpt-5.6-sol."))
        self.assertEqual(runtime.status()["engines"]["smart"]["available"], False)

    def work(self, runtime, run, script):
        with patch("mobile_agent.server.build_target_driver", return_value=self.Phone([COMPOSE])), \
                patch("mobile_agent.server.prepare_wda_phone"), \
                patch("mobile_agent.engines.build_client", return_value=script), \
                patch("mobile_agent.frontier.video_frame", return_value=None):
            self.worker.stop()
            try:
                runtime.work(run)
            finally:
                self.worker.start()
        if run.lease:
            run.lease.close()
            run.lease = None

    def test_an_account_out_of_credit_makes_smart_unavailable_until_it_recovers(self):
        """QA-1: after a Smart task failed on an empty OpenAI account (HTTP 429 insufficient_quota), Smart stayed
        on offer ("about 4¢") and every Smart task failed the same way."""
        key = "sk-test-000000000000"
        os.environ.update(OPENAI_API_KEY=key, TYPESAFE_API_KEY="jev-key-000000000000")
        runtime = self.runtime(managed=True)
        run = runtime.create(None, "What iOS version is this?", "live")

        class Empty(Script):
            def complete(self, messages, schema, timeout=60):
                raise RuntimeError('OpenAI HTTP 429: {"error": {"message": "You exceeded your current quota, please '
                                   'check your plan and billing details.", "type": "insufficient_quota", '
                                   '"code": "insufficient_quota"}}')

        self.work(runtime, run, Empty([], []))
        self.assertEqual((run.summary["outcome"], run.summary["reason"]), ("couldnt_finish", engines.NO_CREDIT))
        smart = runtime.status()["engines"]["smart"]
        self.assertEqual((smart["available"], smart["reason"]), (False, engines.NO_CREDIT))
        status, body, _ = self.request(runtime, "POST", "/api/usage/estimate", {"goal": "What iOS version is this?"})
        self.assertEqual((body["engines"]["smart"]["available"], body["engines"]["smart"]["reason"]), (False, engines.NO_CREDIT))
        with self.assertRaises(APIError) as refused:
            runtime.create(None, "What iOS version is this?", "live")
        self.assertEqual((refused.exception.code, str(refused.exception)), ("engine_unavailable", engines.NO_CREDIT))
        # The background check (a free read of the model, blind to credit) never clears it, however stale.
        probe = Mock(return_value=(True, None))
        runtime.model_reach.probe, runtime.model_reach.enabled = probe, True
        with patch.object(engines.time, "time", return_value=time.time() + 7 * 3600):
            self.assertFalse(runtime.status()["engines"]["smart"]["available"])
        runtime.model_reach._check(key, runtime.model_reach.digest(key))
        self.assertFalse(runtime.status()["engines"]["smart"]["available"])
        probe.assert_called_once()  # only the direct call above
        # A key test that generates and passes clears it: Smart runs again, no restart.
        with patch("mobile_agent.server.Keys.test", return_value={"ok": True, "message": "OpenAI accepted the key."}):
            self.assertTrue(runtime.check_key("openai")["ok"])
        self.assertEqual(runtime.status()["engines"]["smart"], {"available": True, "reason": None,
                                                                "model": "gpt-5.6-sol", "provider": "OpenAI"})
        # A test that finds no credit says so for the engine too.
        empty = {"ok": False, "message": "Your OpenAI account has no credit.", "problem": "no_credit"}
        with patch("mobile_agent.server.Keys.test", return_value=empty):
            runtime.check_key("openai")
        self.assertEqual(runtime.status()["engines"]["smart"]["reason"], engines.NO_CREDIT)

    def test_a_structuring_call_that_finds_no_credit_makes_smart_unavailable(self):
        """Review of #41: the task's own calls went through, then Result CSV's structuring call found the account
        empty. That call's error is what learn_reach reads."""
        key = "sk-test-000000000000"
        os.environ["OPENAI_API_KEY"] = key
        runtime = self.runtime()
        run = runtime.create(None, "Export my reminders", "live", output_format="csv")

        class EmptyAtTheEnd(Script):
            def complete(self, messages, schema, timeout=60):
                if schema is engines.TABLE:
                    raise RuntimeError('OpenAI HTTP 429: {"error": {"message": "You exceeded your current quota, '
                                       'please check your plan and billing details. For more information on this '
                                       'error, read the docs.", "type": "insufficient_quota", "code": '
                                       '"insufficient_quota"}}')
                return super().complete(messages, schema, timeout)

        self.work(runtime, run, EmptyAtTheEnd([[("DONE", None, None)]] * 3, [], answer="You have 2 reminders."))
        # The run view's line, word for word: curly apostrophes, like every string around it (review 2 of #41).
        self.assertEqual(run.summary["output_note"], "Your OpenAI account ran out of credit before Mobster could turn "
                                                     "this answer into CSV, so it’s shown as text.")
        smart = runtime.status()["engines"]["smart"]
        self.assertEqual((smart["available"], smart["reason"]), (False, engines.NO_CREDIT))

    def test_a_smart_task_whose_model_calls_go_through_clears_a_credit_refusal(self):
        key = "sk-test-000000000000"
        os.environ["OPENAI_API_KEY"] = key
        runtime = self.runtime()
        run = runtime.create(None, "What iOS version is this?", "live")
        runtime.model_reach.record(key, False, engines.NO_CREDIT)  # another task found the account empty meanwhile
        script = Script([[("DONE", None, None)]] * 3, [], answer="iOS 26.4.")
        self.work(runtime, run, script)
        self.assertGreater(run.cost_usd, 0)
        self.assertEqual(runtime.status()["engines"]["smart"]["available"], True)
        # A plain rate limit says nothing about the account.
        run = runtime.create(None, "What iOS version is this?", "live")

        class Busy(Script):
            def complete(self, messages, schema, timeout=60):
                raise RuntimeError('OpenAI HTTP 429: {"error": {"code": "rate_limit_exceeded"}}')

        self.work(runtime, run, Busy([], []))
        self.assertIn("rate limiting", run.summary["reason"])
        self.assertEqual(runtime.status()["engines"]["smart"]["available"], True)


if __name__ == "__main__":
    unittest.main()

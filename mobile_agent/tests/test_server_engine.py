import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from mobile_agent import costs, drivers, engines, smart_result
from mobile_agent.api_errors import APIError
from mobile_agent.journal import Lease
from mobile_agent.run import Run
from mobile_agent.runtime_create import ANY_APP
from mobile_agent.server import Runtime, make_handler
from mobile_agent.tests.seam_support import request
from mobile_agent.tests.test_engines import CLEAN, COMPOSE, Script, item, sent
from mobile_agent.tests.test_server_hardening import APP
from mobile_agent.wda.frame_store import FrameStore

READY = {"device": True, "ready": True, "health": None, "can_act": True, "driver": "wda", "screen_reading": False}
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
        self.worker = patch.object(Runtime, "work", lambda runtime, run: None)
        self.worker.start()
        self.addCleanup(self.worker.stop)
        lease = patch("mobile_agent.journal.Lease.device", side_effect=lambda _: Lease(self.root / "device.lock"))
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

    def request(self, runtime, method, path, body=None, handler=None, extra_headers=None):
        return request(handler or make_handler(runtime), method, path, body, headers=extra_headers)


class EngineRuntimeTests(Base):
    def test_a_new_user_defaults_to_smart_and_is_told_to_add_a_key(self):
        status = self.runtime(managed=True).status()
        self.assertEqual(status["engines"], {"smart": {"available": False, "reason": "Connect your AI account in Setup",
                                                       "model": "claude-sonnet-5-5", "provider": "Anthropic",
                                                       "billing": "key"}})
        self.assertEqual(status["limitations"][0], "Connect your AI account in Setup")
        self.assertFalse(status["live_enabled"])
        self.assertEqual(self.runtime().status()["limitations"][0],
                         "Add OPENAI_API_KEY or ANTHROPIC_API_KEY to the agent's env file")

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
        for engine in ("fast", "quick"):
            with self.assertRaises(APIError) as refused:
                runtime.create("settings", "Open About", "live", engine=engine)
            self.assertEqual(refused.exception.code, "engine_unavailable")
        self.assertEqual(runtime.runs, {})

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

    def test_an_explicit_engine_is_part_of_the_request_identity(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        runtime = self.runtime()
        run, _ = runtime.create("settings", "Open About", "live", "key-1", engine="smart")
        self.finish(runtime, run)
        self.assertEqual(runtime.create("settings", "Open About", "live", "key-1", engine="fast"), (run, True))
        with self.assertRaises(APIError) as conflict:
            runtime.create("settings", "Open About", "live", "key-1")
        self.assertEqual(conflict.exception.code, "idempotency_conflict")

    def test_spend_limits_change_only_with_the_mac_apps_session(self):
        runtime = self.runtime()
        handler = make_handler(runtime, "t" * 43, app_session="s" * 43)
        token = {"Authorization": "Bearer " + "t" * 43}
        for change in ({"taskCostLimitUsd": None}, {"taskCostLimitUsd": 100}, {"monthlyLimitUsd": 10000},
                       {"monthlyLimitUsd": None, "hearPhone": False}):
            with self.subTest(change=change):
                status, body, _ = self.request(runtime, "POST", "/api/settings", change, handler=handler,
                                               extra_headers=token)
                self.assertEqual((status, body.get("code")), (403, "app_only"))
        self.assertEqual((runtime.task_cap(), runtime.settings()["monthlyLimitUsd"]), (1.0, None))
        status, body, _ = self.request(runtime, "POST", "/api/settings", {"taskCostLimitUsd": 5, "monthlyLimitUsd": 40},
                                       handler=handler, extra_headers={**token, "X-Mobster-App-Session": "s" * 43})
        self.assertEqual((status, body["taskCostLimitUsd"], body["monthlyLimitUsd"]), (200, 5.0, 40.0))
        status, _, _ = self.request(runtime, "POST", "/api/settings", {"phoneBatteryPause": None}, handler=handler,
                                    extra_headers=token)
        self.assertEqual(status, 200)

    def test_engine_and_limit_settings_persist_and_validate(self):
        runtime = self.runtime()
        settings = runtime.settings()
        self.assertEqual({k: settings[k] for k in ("taskCostLimitUsd", "monthlyLimitUsd")},
                         {"taskCostLimitUsd": 1.0, "monthlyLimitUsd": None})
        changed = runtime.update_settings({"taskCostLimitUsd": 0.5, "monthlyLimitUsd": 20})
        self.assertEqual((changed["taskCostLimitUsd"], changed["monthlyLimitUsd"]), (.5, 20.0))
        saved = Path(runtime.config.env_file).read_text()
        for line in ("MOBSTER_TASK_COST_LIMIT=0.50", "MOBSTER_MONTHLY_LIMIT=20.00"):
            self.assertIn(line, saved)
        self.assertEqual(runtime.task_cap(), .5)
        off = runtime.update_settings({"taskCostLimitUsd": None})
        self.assertIsNone(off["taskCostLimitUsd"])
        self.assertIsNone(runtime.task_cap())
        runtime.config.spend_cap_usd = 3.0
        self.assertEqual(runtime.task_cap(), 3.0)
        for bad in ({"defaultEngine": "fast"}, {"bypassChecks": True}, {"taskCostLimitUsd": 0},
                    {"taskCostLimitUsd": "1"}, {"monthlyLimitUsd": 10**6}, {"taskCostLimitUsd": True}):
            with self.assertRaises(ValueError):
                runtime.update_settings(bad)

    def test_estimates_name_mobsters_agent(self):
        runtime = self.runtime(managed=True)
        for engine in ("smart", "fast"):
            status, body, _ = self.request(runtime, "POST", "/api/usage/estimate",
                                           {"goal": "Text Sam I'm running late in Messages", "engine": engine})
            self.assertEqual(status, 200)
            self.assertEqual((body["engine"], list(body["engines"])), ("smart", ["smart"]))
            smart = body["engines"]["smart"]
            self.assertEqual((smart["available"], smart["reason"], smart["provider"]),
                             (False, "Connect your AI account in Setup", "Anthropic"))
            self.assertTrue(0 < smart["lowUsd"] < smart["highUsd"] < 1)
        for bad in ({"goal": "x", "engine": "turbo"}, {"goal": "x", "helperModel": "gemini-3.5-flash-lite"}):
            self.assertEqual(self.request(runtime, "POST", "/api/usage/estimate", bad)[0], 400)
        status, body, _ = self.request(runtime, "GET", "/api/usage/estimate")
        self.assertEqual(list(body), ["engines"])
        self.assertEqual(list(body["engines"]), ["smart"])

    def test_smart_prices_the_structure_it_builds_for_csv_json_and_yaml(self):
        runtime = self.runtime(managed=True)
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        estimate = lambda **extra: self.request(runtime, "POST", "/api/usage/estimate",
                                                {"goal": "Export my reminders", **extra})
        plain = costs.midpoint(estimate()[1]["engines"]["smart"])
        for fmt in ("csv", "json", "yaml"):
            status, body, _ = estimate(outputFormat=fmt)
            self.assertEqual(status, 200, body)
            self.assertAlmostEqual(costs.midpoint(body["engines"]["smart"]) - plain, costs.SMART_STRUCTURE_USD,
                                   delta=.001)
        for fmt in ("auto", "text", "markdown"):
            self.assertAlmostEqual(costs.midpoint(estimate(outputFormat=fmt)[1]["engines"]["smart"]), plain,
                                   delta=.0001)
        self.assertEqual(estimate(outputFormat="xml")[0], 400)
        run = runtime.create(None, "Export my reminders", "live", output_format="csv")
        self.assertAlmostEqual(run.estimate_usd - plain, costs.SMART_STRUCTURE_USD, delta=.001)

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
        self.assertEqual((status, body["run"]["engine"], body["run"]["appName"]), (201, "smart", "Settings"))
        self.finish(runtime, runtime.runs[body["run"]["id"]])
        status, body, _ = self.request(runtime, "POST", "/api/runs", {"goal": "Open Settings", "engine": "turbo"})
        self.assertEqual(status, 400)
        status, body, _ = self.request(runtime, "POST", "/api/runs", {"goal": "Open Settings", "dryRun": True})
        self.assertEqual((status, body["dryRun"]["dryRun"], body["dryRun"]["sent"]), (200, True, False))
        status, body, _ = self.request(runtime, "POST", "/api/runs", {"goal": "Open Settings", "dryRun": False})
        self.assertEqual(status, 400)
        self.assertEqual(len(runtime.runs), 2)

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
        self.assertEqual(self.request(runtime, "POST", f"/api/runs/{run.id}/review", {"ok": True})[1]["code"],
                         "run_active")
        self.assertEqual(self.request(runtime, "DELETE", f"/api/runs/{run.id}")[0], 409)
        run.emit({"event": "step", "n": 1, "text": "Opened Settings", "_frame": jpeg()})
        self.finish(runtime, run)
        status, body, _ = self.request(runtime, "POST", f"/api/runs/{run.id}/review", {"ok": True})
        self.assertEqual((status, body["run"]["reviewedOk"]), (200, True))
        self.assertTrue(next(r for r in runtime.journal.load() if r["id"] == run.id)["reviewedOk"])
        self.assertEqual(self.request(runtime, "POST", f"/api/runs/{run.id}/review", {"ok": "yes"})[0], 400)
        runtime.frames.worker.submit(lambda: None).result()
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
        self.assertEqual(event["frameId"], frame_id)
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

    def test_saved_screenshots_are_owner_only_down_to_the_frames_folder(self):
        import stat
        root = Path(tempfile.mkdtemp(prefix="mobster-frames-")) / "state" / "frames"
        self.addCleanup(shutil.rmtree, root.parent.parent, True)
        store = FrameStore(root)
        frame = store.put("r", jpeg(300, 200))
        store.close()
        mode = lambda path: stat.S_IMODE(os.stat(path).st_mode)  # noqa: E731
        self.assertEqual(mode(root), 0o700)
        self.assertEqual(mode(root / "r"), 0o700)
        self.assertEqual(mode(root / "r" / f"{frame}.jpg"), 0o600)


class FrameDeletionTests(unittest.TestCase):
    def test_delete_waits_for_an_in_flight_frame_before_removing_its_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FrameStore(folder)
            entered, release, deleted = threading.Event(), threading.Event(), threading.Event()
            run_id = "a" * 12

            def downscale(data):
                entered.set()
                release.wait(5)
                return data

            try:
                with patch.object(store, "downscale", side_effect=downscale):
                    store.put(run_id, jpeg(200, 300))
                    self.assertTrue(entered.wait(2))
                    thread = threading.Thread(target=lambda: (store.delete(run_id), deleted.set()))
                    thread.start()
                    self.assertFalse(deleted.wait(.05))
                    release.set()
                    thread.join(3)
                    self.assertTrue(deleted.is_set())
                self.assertNotIn(run_id, store.memory)
                self.assertFalse((Path(folder) / run_id).exists())
            finally:
                release.set()
                store.close()


class SmartTaskTests(Base):
    class Phone(drivers.Driver):
        capabilities = frozenset({"call"})

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
        session = patch("mobile_agent.runtime_driver.resolve_wda_session", return_value="s")
        session.start()
        self.addCleanup(session.stop)

    def work(self, runtime, run, script, phone=None, frames=None):
        with patch("mobile_agent.runtime_driver.build_target_driver", return_value=phone or self.Phone([COMPOSE])), \
                patch("mobile_agent.runtime_driver.prepare_wda_phone"), \
                patch("mobile_agent.engines.build_client", return_value=script), \
                patch("mobile_agent.frontier.screen.video_frame",
                      side_effect=lambda *a, **k: next(frames) if frames else None):
            self.worker.stop()
            try:
                runtime.work(run)
            finally:
                self.worker.start()
        if run.lease:
            run.lease.close()
            run.lease = None

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
        self.work(runtime, run, script, phone, frames)
        helper.join(5)
        self.assertEqual(phone.calls[0], ("POST", "/wda/apps/activate", {"bundleId": "com.apple.MobileSMS"}))
        self.assertEqual(phone.actions, [("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)])
        summary = run.summary
        self.assertEqual((run.status, summary["outcome"], summary["engine"]), ("completed", "done", "smart"))
        self.assertEqual(summary["answer"], "Sent Sam “I'm running late”.")
        self.assertEqual([p["quote"] for p in summary["proof"]], ["I'm running late"])
        self.assertRegex(summary["proof"][0]["frameId"], r"^f[0-9a-f]{10}$")
        self.assertEqual(script.prompts[0].split("\n")[0], "Request: In Messages: Text Sam I'm running late")
        kinds = [e["event"] for e in run.events]
        for kind in ("plan", "contract_item", "step", "receipt", "cost", "inference_finished", "approval_requested"):
            self.assertIn(kind, kinds)
        self.assertEqual(kinds.count("approval_requested"), 1)
        self.assertNotIn("frontier_prompt", kinds)
        requested = next(e for e in run.events if e["event"] == "approval_requested")
        self.assertEqual((requested["kind"], requested["target"]), ("commit", {"x": .8, "y": .9, "w": .1, "h": .04}))
        self.assertEqual(kinds.count("inference_finished"), 3)
        self.assertAlmostEqual(summary["costUsd"], run.cost_usd)
        self.assertGreater(run.cost_usd, 0)
        step = next(e for e in run.events if e["event"] == "step")
        status, data, _ = self.request(runtime, "GET", f"/api/runs/{run.id}/frames/{step['frameId']}")
        self.assertEqual(status, 200)
        self.assertEqual(runtime.journal.usage()["providers"][0]["provider"], "openai")

    def test_stop_ends_a_running_smart_task_before_its_next_action(self):
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
        script = Script(steps, [item(kind="READ", app="Messages", what="Sam", act="none", quote="Sam")],
                        answer="Typed.")
        self.work(runtime, run, script, phone, iter([jpeg()] * 50))
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
        self.work(runtime, run, Missing([], []), phone)
        self.assertEqual(phone.calls, [])
        self.assertEqual((run.summary["outcome"], run.summary["reason"]),
                         ("couldnt_finish", "Your OpenAI key can't use gpt-5.6-sol."))
        self.assertEqual(runtime.status()["engines"]["smart"]["available"], False)

    def test_an_account_out_of_credit_makes_smart_unavailable_until_it_recovers(self):
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
        self.assertEqual((body["engines"]["smart"]["available"], body["engines"]["smart"]["reason"]),
                         (False, engines.NO_CREDIT))
        with self.assertRaises(APIError) as refused:
            runtime.create(None, "What iOS version is this?", "live")
        self.assertEqual((refused.exception.code, str(refused.exception)), ("engine_unavailable", engines.NO_CREDIT))
        probe = Mock(return_value=(True, None))
        runtime.model_reach.probe, runtime.model_reach.enabled = probe, True
        with patch.object(engines.time, "time", return_value=time.time() + 7 * 3600):
            self.assertFalse(runtime.status()["engines"]["smart"]["available"])
        runtime.model_reach._check(key, runtime.model_reach.digest(key))
        self.assertFalse(runtime.status()["engines"]["smart"]["available"])
        probe.assert_called_once()
        with patch("mobile_agent.keys.Keys.test", return_value={"ok": True, "message": "OpenAI accepted the key."}):
            self.assertTrue(runtime.check_key("openai")["ok"])
        self.assertEqual(runtime.status()["engines"]["smart"], {"available": True, "reason": None,
                                                                "model": "gpt-5.6-sol", "provider": "OpenAI",
                                                                "billing": "key"})
        empty = {"ok": False, "message": "Your OpenAI account has no credit.", "problem": "no_credit"}
        with patch("mobile_agent.keys.Keys.test", return_value=empty):
            runtime.check_key("openai")
        self.assertEqual(runtime.status()["engines"]["smart"]["reason"], engines.NO_CREDIT)

    def test_a_structuring_call_that_finds_no_credit_makes_smart_unavailable(self):
        key = "sk-test-000000000000"
        os.environ["OPENAI_API_KEY"] = key
        runtime = self.runtime()
        run = runtime.create(None, "Export my reminders", "live", output_format="csv")

        class EmptyAtTheEnd(Script):
            def complete(self, messages, schema, timeout=60):
                if schema is smart_result.TABLE:
                    raise RuntimeError('OpenAI HTTP 429: {"error": {"message": "You exceeded your current quota, '
                                       'please check your plan and billing details. For more information on this '
                                       'error, read the docs.", "type": "insufficient_quota", "code": '
                                       '"insufficient_quota"}}')
                return super().complete(messages, schema, timeout)

        self.work(runtime, run, EmptyAtTheEnd([[("DONE", None, None)]] * 3, [], answer="You have 2 reminders."))
        self.assertEqual(run.summary["output_note"], "Your OpenAI account ran out of credit before Mobster could turn "
                                                     "this answer into CSV, so it’s shown as text.")
        smart = runtime.status()["engines"]["smart"]
        self.assertEqual((smart["available"], smart["reason"]), (False, engines.NO_CREDIT))

    def test_a_smart_task_whose_model_calls_go_through_clears_a_credit_refusal(self):
        key = "sk-test-000000000000"
        os.environ["OPENAI_API_KEY"] = key
        runtime = self.runtime()
        run = runtime.create(None, "What iOS version is this?", "live")
        runtime.model_reach.record(key, False, engines.NO_CREDIT)
        script = Script([[("DONE", None, None)]] * 3, [], answer="iOS 26.4.")
        self.work(runtime, run, script)
        self.assertGreater(run.cost_usd, 0)
        self.assertEqual(runtime.status()["engines"]["smart"]["available"], True)
        run = runtime.create(None, "What iOS version is this?", "live")

        class Busy(Script):
            def complete(self, messages, schema, timeout=60):
                raise RuntimeError('OpenAI HTTP 429: {"error": {"code": "rate_limit_exceeded"}}')

        self.work(runtime, run, Busy([], []))
        self.assertIn("rate limiting", run.summary["reason"])
        self.assertEqual(runtime.status()["engines"]["smart"]["available"], True)


class MonthlyLimitTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"

    def spend(self, runtime, usd):
        run = runtime.create(None, "Open Settings", "live")
        for index, kind in enumerate(("inference_started", "inference_finished")):
            run.emit({"event": kind, "provider": "openai", "call_id": "c1", "model": "gpt-5.6-sol",
                      **({"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                          "cost_nanodollars": round(usd * 1e9)} if index else {})})
        self.finish(runtime, run)

    def test_the_refusal_names_the_settings_section_that_exists(self):
        runtime = self.runtime()
        self.spend(runtime, 2.5)
        runtime.update_settings({"monthlyLimitUsd": 2})
        with self.assertRaises(APIError) as refused:
            runtime.create(None, "Open Settings", "live")
        self.assertEqual((refused.exception.status, refused.exception.code, refused.exception.details["monthUsd"]),
                         (409, "monthly_limit_reached", 2.5))
        self.assertEqual(str(refused.exception), "This month's spend has reached your $2.00 limit. Raise it in "
                                                 "Settings › Models to run more tasks.")
        runtime.update_settings({"monthlyLimitUsd": None})
        self.finish(runtime, runtime.create(None, "Open Settings", "live"))

    def test_a_task_gets_only_what_is_left_of_the_month_and_the_task_limit_when_more_is_left(self):
        runtime = self.runtime()
        self.spend(runtime, 4.6)
        runtime.update_settings({"monthlyLimitUsd": 5})
        run = runtime.create(None, "Open Settings", "live")
        self.assertAlmostEqual(run.cap_usd, 0.4, places=6)
        self.assertTrue(run.cap_from_month)
        self.assertAlmostEqual(runtime.run_cap(run), 0.4, places=6)
        self.finish(runtime, run)
        runtime.update_settings({"monthlyLimitUsd": 50})
        run = runtime.create(None, "Open Settings", "live")
        self.assertEqual((run.cap_usd, run.cap_from_month, runtime.run_cap(run)), (1.0, False, 1.0))
        self.finish(runtime, run)
        runtime.update_settings({"monthlyLimitUsd": None})
        run = runtime.create(None, "Open Settings", "live")
        self.assertEqual((run.cap_usd, runtime.run_cap(run)), (None, 1.0))
        self.finish(runtime, run)

    def test_the_smart_loop_is_given_the_tasks_own_cap_and_phone_guard(self):
        runtime = self.runtime()
        self.spend(runtime, 4.6)
        runtime.update_settings({"monthlyLimitUsd": 5})
        run = runtime.create(None, "Open Settings and read the first row", "live")
        guard = Mock(name="guard")
        seen = {}

        def build(driver, client, **options):
            seen.update(options)
            raise RuntimeError("the loop is not run here")

        with patch("mobile_agent.runtime_driver.resolve_wda_session", return_value="s"), \
                patch("mobile_agent.runtime_driver.build_target_driver", return_value=SmartTaskTests.Phone([])), \
                patch("mobile_agent.runtime_driver.prepare_wda_phone"), \
                patch("mobile_agent.engines.build_client", return_value=Mock(usage={}, model_failed=False)), \
                patch.object(runtime, "make_guard", return_value=guard), \
                patch("mobile_agent.engines.build_frontier", side_effect=build):
            self.worker.stop()
            try:
                runtime.work(run)
            finally:
                self.worker.start()
        self.assertAlmostEqual(seen["max_cost_usd"], 0.4, places=6)
        self.assertIs(seen["guard"], guard)
        if run.lease:
            run.lease.close()
            run.lease = None

    def test_tasks_running_at_once_share_what_is_left(self):
        runtime = self.runtime()
        self.spend(runtime, 4.0)
        other = Run(dict(APP), "Another task", "live", status="running", cap_usd=1.0,
                    cost_nanodollars=250_000_000, cost_known=True)
        runtime.runs[other.id], runtime.active_runs[other.id] = other, "phone-b"
        self.addCleanup(lambda: (runtime.runs.pop(other.id, None), runtime.active_runs.clear()))
        with runtime.lock:
            cap, from_month = runtime.monthly_cap(5)
        self.assertAlmostEqual(cap, 0.25, places=6)
        self.assertTrue(from_month)
        runtime.active_runs.pop(other.id)
        other.phone_finished = True  # report writing still holds the reserved model spend
        with runtime.lock:
            cap, from_month = runtime.monthly_cap(5)
        self.assertAlmostEqual(cap, 0.25, places=6)
        self.assertTrue(from_month)
        other.cost_nanodollars = 0
        with runtime.lock, self.assertRaises(APIError) as held:
            runtime.monthly_cap(5)
        self.assertEqual((held.exception.code, held.exception.details["monthlyLimitUsd"]), ("monthly_limit_reached", 5))
        self.assertEqual(str(held.exception), "The task running now could use the rest of this month's $5.00 limit. "
                                              "Wait for it to finish, or raise the limit in Settings › Models.")

    def test_an_unreadable_month_refuses_a_start_under_a_limit_and_only_then(self):
        runtime = self.runtime()
        runtime.update_settings({"monthlyLimitUsd": 5})
        with patch.object(runtime.journal, "spend_since", side_effect=OSError("disk I/O error")):
            with self.assertRaises(APIError) as refused:
                runtime.create(None, "Open Settings", "live")
            self.assertEqual((refused.exception.status, refused.exception.code), (503, "usage_unavailable"))
            self.assertIn("couldn't read this month's spending", str(refused.exception))
            self.assertEqual(runtime.month_usd(), 0.0)
            self.assertEqual(runtime.runs, {})
            runtime.update_settings({"monthlyLimitUsd": None})
            run = runtime.create(None, "Open Settings", "live")
        self.finish(runtime, run)

    def test_a_task_stopped_by_the_months_limit_says_so(self):
        runtime = self.runtime()
        runtime.update_settings({"monthlyLimitUsd": 5})
        run = Run({"id": "settings", "name": "Settings", "bundleId": "com.apple.Preferences"}, "Open About", "live")
        run.cap_usd, run.cap_from_month = 0.4, True
        result = runtime.note_limit(run, {"status": "spend_cap", "reason": "The task reached its cost limit."})
        self.assertEqual(result, {"status": "spend_cap", "spend_limit": "monthly",
                                  "reason": "The task stopped at this month's $5.00 spending limit. Raise it in "
                                            "Settings › Models to run more tasks."})
        run.cap_from_month = False
        self.assertEqual(runtime.note_limit(run, {"status": "spend_cap", "reason": "x"}),
                         {"status": "spend_cap", "reason": "x"})


class PhoneGuardAfterErrorTests(Base):
    class Guard:
        def __init__(self, attached, lost=False):
            self.attached_value, self.lost = attached, lost

        def attached(self):
            return self.attached_value

        def wifi_lost(self):
            return self.lost

        def finish(self, driver):
            pass

    class Driver:
        last_image = None

        def __init__(self, locked=False):
            self.locked = locked
            self.http = SimpleNamespace(request=lambda *a, **k: {"value": self.locked})

        def close(self):
            pass

    def run_failing(self, guard, driver):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-000000000000"
        runtime = self.runtime()
        run = runtime.create("settings", "Open About", "live")

        def open_driver(_runtime, run_, trace, setup):
            run_.guard = guard
            return setup.hold(driver)

        self.attempts = 0
        test = self

        class Failing:
            def run(self, *args, **kwargs):
                test.attempts += 1
                raise ConnectionResetError("Connection reset by peer")

        with patch.object(Runtime, "smart_client", lambda _runtime: Script([[("DONE", None, None)]], [])), \
                patch.object(Runtime, "open_driver", open_driver), \
                patch("mobile_agent.engines.build_frontier", lambda *a, **k: Failing()):
            self.worker.stop()
            try:
                runtime.work(run)
            finally:
                self.worker.start()
        if run.lease:
            run.lease.close()
            run.lease = None
        return run.summary

    def test_a_failed_step_says_what_happened_to_the_phone_and_is_not_retried(self):
        from mobile_agent import lockscreen
        service = "The task stopped after a service error. An in-flight action may have run; it was not retried."
        cases = ((self.Guard(False), self.Driver(), "unplugged",
                  "Your iPhone was unplugged. Plug it back in and try again."),
                 (self.Guard(True), self.Driver(locked=True), "phone_locked",
                  "Your iPhone locked during the task, so Mobster stopped. Unlock it and try again."),
                 (self.Guard(None, lost=True), self.Driver(), "unplugged", lockscreen.WIFI_LOST),
                 (self.Guard(True), self.Driver(locked=False), None, service),
                 (self.Guard(None), self.Driver(), None, service))
        for guard, driver, code, reason in cases:
            with self.subTest(code=code, reason=reason):
                summary = self.run_failing(guard, driver)
                self.assertEqual((summary["status"], summary.get("stop_code"), summary["reason"]),
                                 ("error", code, reason))
                self.assertTrue(summary["actions_may_have_run"])
                self.assertEqual(self.attempts, 1)


class CloseOrderTests(Base):
    def test_tasks_stop_before_the_runner_and_relay_are_closed(self):
        runtime = self.runtime()
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        run = runtime.create(None, "Open Settings", "live")
        order = []
        second = SimpleNamespace(begin_close=lambda: order.append(("begin", "phone-b", run.stop.is_set())),
                                 close=lambda: order.append(("close", "phone-b")))
        runtime.manager = SimpleNamespace(
            begin_close=lambda: order.append(("begin", "primary", run.stop.is_set())),
            close=lambda: order.append(("close", "primary", run.stop.is_set())), watching=threading.Event())
        runtime.fleet = SimpleNamespace(phones={"b": SimpleNamespace(manager=second)},
                                        close=lambda keep_runner=False: second.close())
        if run.lease:
            run.lease.close()
            run.lease = None
        runtime.close(timeout=1)
        self.runtimes.remove(runtime)
        self.assertEqual(order[:2], [("begin", "primary", True), ("begin", "phone-b", True)])
        self.assertEqual(order[2:], [("close", "phone-b"), ("close", "primary", True)])

    def start_a_simulator_runner(self):
        """A stand-in for xcodebuild that runs until it is killed, started as the simulator manager starts it."""
        from mobile_agent.sim import wda as sim_wda
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        fake = Path(folder) / "xcodebuild"
        fake.write_text("#!/bin/sh\nsleep 300\n")
        fake.chmod(0o755)
        plan = Path(folder) / "wda.xctestrun"
        plan.write_text("")
        process = sim_wda.SimulatorWDA("UDID", 8200, 9200, plan,
                                       tools=SimpleNamespace(xcodebuild=str(fake), env=dict(os.environ))).start()
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertIsNone(process.poll())
        return process

    def test_closing_the_runtime_stops_the_simulator_runner_it_started(self):
        runtime = self.runtime()
        process = self.start_a_simulator_runner()
        runtime.close(timeout=1)
        self.runtimes.remove(runtime)
        self.assertIsNotNone(process.wait(timeout=10))

    def test_a_development_reload_leaves_the_simulator_runner_running(self):
        runtime = self.runtime()
        runtime.config.keep_runner = True
        process = self.start_a_simulator_runner()
        runtime.close(timeout=1)
        self.runtimes.remove(runtime)
        self.assertIsNone(process.poll())
        from mobile_agent.sim import wda as sim_wda
        sim_wda.stop_started()
        process.wait(timeout=10)

    def test_a_development_reload_keeps_the_runner(self):
        runtime = self.runtime()
        runtime.config.keep_runner = True
        manager = Mock(watching=threading.Event())
        runtime.manager = manager
        runtime.close(timeout=1)
        self.runtimes.remove(runtime)
        manager.begin_close.assert_not_called()
        manager.close.assert_not_called()
        self.assertTrue(manager.watching.is_set())


def worker_until_stopped(runtime, run):
    run.stop.wait(5)
    run.finish({"status": "stopped", "outcome": "stopped", "independently_verified": False,
                "reason": "You stopped this task."})


class QuitWordingTests(Base):
    def quit_with_a_task(self, stop_first=False):
        runtime = self.runtime()
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        with patch.object(Runtime, "work", worker_until_stopped):
            run = runtime.create(None, "Open Settings", "live")
            if stop_first:
                runtime.stop_run(run.id)
                run.condition.acquire()
                run.condition.wait_for(lambda: run.finished_at is not None, 5)
                run.condition.release()
            if run.lease:
                run.lease.close()
                run.lease = None
            self.assertTrue(runtime.close(timeout=5))
        self.runtimes.remove(runtime)
        return run

    def test_a_task_mobster_quit_under_says_so_and_what_to_do(self):
        run = self.quit_with_a_task()
        self.assertEqual(run.status, "interrupted")
        self.assertEqual(run.summary["reason"], "Mobster quit while this task was running. Run it again to finish.")
        self.assertEqual((run.summary["outcome"], run.summary["interrupted_by"]), ("couldnt_finish", "quit"))
        self.assertNotIn("You stopped", json.dumps(run.summary))

    def test_a_task_stopped_before_the_quit_still_says_you_stopped_it(self):
        run = self.quit_with_a_task(stop_first=True)
        self.assertEqual(run.status, "stopped")
        self.assertEqual(run.summary["reason"], "You stopped this task.")
        self.assertNotIn("interrupted_by", run.summary)


if __name__ == "__main__":
    unittest.main()

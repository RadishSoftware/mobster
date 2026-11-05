import importlib.util
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

from mobile_agent.tests import sota_world as world
from mobile_agent.tests.seam_support import isolate
from mobile_agent.tests.test_engines import CLEAN

LIVE_DEMO = Path(__file__).resolve().parents[2] / "dashboard" / "qa" / "live_demo.py"


def load_live_demo():
    spec = importlib.util.spec_from_file_location("live_demo_under_test", LIVE_DEMO)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(LIVE_DEMO.exists(), "the dashboard isn't checked out here")
class LiveDemo(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-live-demo-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        env = patch.dict(os.environ, {**CLEAN, "HOME": str(self.root / "home"), "MOBSTER_NO_DEVICES": "1"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        # The device lease's older half sits in the temporary folder every process of this user shares: keep this
        # test's locks out of it, so it never touches a demo running on this Mac.
        (self.root / "tmp").mkdir()
        tmp = patch.object(tempfile, "tempdir", str(self.root / "tmp"))
        tmp.start()
        self.addCleanup(tmp.stop)
        isolate(self)
        from mobile_agent import engines
        kwargs = engines.frontier_kwargs
        # The scripted phone changes at once: no 0.6 s settle after each action.
        settle = patch.object(engines, "frontier_kwargs", lambda *a, **kw: {**kwargs(*a, **kw), "settle_seconds": 0})
        settle.start()
        self.addCleanup(settle.stop)
        for name in ("smart_key", "acquire_client", "release_client"):  # build() replaces these; put them back
            patcher = patch.object(engines, name, getattr(engines, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        pace = patch.dict(world.PACE, {"value": 0.0})
        pace.start()
        self.addCleanup(pace.stop)
        self.demo = load_live_demo()

    def start_world(self, home, phone_name=None, long=False):
        os.environ["HOME"] = str(self.root / home)
        httpd, runtime, url, token_path = self.demo.build(0, 0, phone_name=phone_name, long=long)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(runtime.close)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return url, token_path.read_text().strip(), runtime

    def call(self, url, token, method, path, body=None):
        headers = {"Authorization": f"Bearer {token}", "X-Mobster-Origin": "app"}
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers.update({"Content-Type": "application/json", "Idempotency-Key": uuid.uuid4().hex})
        request = urllib.request.Request(url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    def until(self, check, what, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(.02)
        self.fail(f"timed out waiting for {what}")

    def finished(self, url, token, run_id):
        def run():
            status, data = self.call(url, token, "GET", f"/api/runs/{run_id}")
            self.assertEqual(status, 200, data)
            return data["run"] if data["run"].get("finishedAt") else None
        return self.until(run, f"task {run_id} to finish")

    def test_long_finishes_a_reels_digest_and_a_shortlist_with_items_in_both(self):
        from mobile_agent import model_clients
        from mobile_agent.tests import longform_service
        from mobile_agent.tests import longform_world as lw
        restore = patch.object(model_clients, "chat_client", model_clients.chat_client)   # build(long=True) scripts it
        restore.start()
        self.addCleanup(restore.stop)
        stack = longform_service.fast()
        self.addCleanup(stack.close)
        url, token, runtime = self.start_world("long", long=True)
        self.demo.seed_long(self.demo.Client(url, token))
        runs = [run for run in runtime.runs.values() if getattr(run, "longform", None)]
        self.assertEqual(len(runs), 2)
        for run in runs:
            report = (run.summary or {}).get("report") or {}
            self.assertEqual(run.summary.get("status"), "completed", run.goal)
            self.assertGreater(report["stats"]["items"], 0, run.goal)
        shortlist = next(run.summary["report"] for run in runs if "Hinge" in run.goal)
        self.assertGreaterEqual(len(shortlist["ranked"]), 3)
        self.assertEqual(lw.person_taps(runtime.scripted_phone), [])

    def test_the_seed_fills_the_history_and_a_later_task_uses_the_remembered_gym(self):
        url, token, runtime = self.start_world("home")
        self.demo.seed(self.demo.Client(url, token), runtime.scripted_phone)
        _, data = self.call(url, token, "GET", "/api/runs")
        runs = {run["goal"]: run for run in data["runs"]}
        self.assertEqual(len(runs), 7, sorted(runs))
        self.assertEqual({goal: run["status"] for goal, run in runs.items() if run["status"] != "completed"}, {})
        # "Remember that …" started no task: it is a note, and the memory it suggested was accepted and then used.
        self.assertFalse(any(goal.startswith("Remember") for goal in runs))
        _, memory = self.call(url, token, "GET", "/api/memory/facts")
        self.assertIn("My gym is Equinox on 5th Street", [fact["text"] for fact in memory["facts"]])
        self.assertIn("Equinox", runs["When does my gym close today?"]["summary"]["answer"])
        self.assertEqual(runtime.scripted_phone["sent"], {"Alex Rivera": ["Running 15 minutes late, sorry!"]})

    def task_while_the_other_holds_its_phone(self, first, second):
        url, token, runtime = first
        gate = world.hold_at(runtime.scripted_phone, "TAP", "General")
        status, data = self.call(url, token, "POST", "/api/runs", {"goal": "What iOS version is this iPhone on?",
                                                                  "mode": "live"})
        self.assertEqual(status, 201, data)
        held = data["run"]["id"]
        self.until(gate["reached"].is_set, "the first demo's task to reach General")
        try:
            return self.call(second[0], second[1], "POST", "/api/runs",
                             {"goal": "What iOS version is this iPhone on?", "mode": "live"})
        finally:
            gate["release"].set()
            self.assertEqual(self.finished(url, token, held)["status"], "completed")

    def test_each_demo_names_its_own_phone_so_two_at_once_both_run(self):
        first, second = self.start_world("a"), self.start_world("b")
        self.assertNotEqual(first[2].config.wda_url, second[2].config.wda_url)
        self.assertTrue(first[2].config.wda_url.startswith("demo://phone/"))
        status, data = self.task_while_the_other_holds_its_phone(first, second)
        self.assertEqual(status, 201, data)
        self.assertEqual(self.finished(second[0], second[1], data["run"]["id"])["status"], "completed")

    def test_two_demos_given_one_phone_name_still_exclude_each_other(self):
        name = f"shared-{uuid.uuid4().hex[:8]}"
        first, second = self.start_world("a", name), self.start_world("b", name)
        self.assertEqual(first[2].config.wda_url, f"demo://phone/{name}")
        status, data = self.task_while_the_other_holds_its_phone(first, second)
        self.assertEqual((status, data.get("code")), (409, "device_busy"), data)
        self.assertEqual(data["error"], "Your iPhone is busy in another Mobster process")


if __name__ == "__main__":
    unittest.main()

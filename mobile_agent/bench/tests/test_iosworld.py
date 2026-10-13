"""The iOSWorld adapter writes trajectories in iOSWorld's own format. Offline."""

import json
import pathlib
import plistlib
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from mobile_agent.bench import iosworld
from mobile_agent.bench.iosworld import RecordingDriver, answer_text, describe, task_cost_cap


class FakeShots:
    def __init__(self):
        self.taken = []

    def take(self, name):
        self.taken.append(name)
        return f"/shots/{name}.png"


class FakeDriver:
    def __init__(self):
        self.calls = []

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.calls.append(("execute", operation))

    def call(self, method, path, body=None, timeout=10):
        self.calls.append(("call", path))


class IOSWorldAdapterTests(unittest.TestCase):
    def test_each_dispatch_is_a_step_with_its_pre_action_screenshot(self):
        driver = RecordingDriver(FakeDriver(), FakeShots())
        button = SimpleNamespace(label="Log Food", role="Button")
        driver.call("POST", "/wda/apps/activate", {"bundleId": "com.example.caltrack"})
        driver.execute("TAP", button, None)
        driver.execute("TYPE_SUBMIT", SimpleNamespace(label="Search", role="SearchField"), None, text="oatmeal")
        driver.call("GET", "/source")
        self.assertEqual([s["actions"][0]["type"] for s in driver.steps], ["launch_app", "tap", "type"])
        self.assertEqual(driver.steps[1]["actions"][0]["element"], {"label": "Log Food", "role": "Button"})
        self.assertEqual(driver.steps[2]["actions"][0]["text"], "oatmeal")
        self.assertEqual([s["screenshot"] for s in driver.steps],
                         ["/shots/step-001.png", "/shots/step-002.png", "/shots/step-003.png"])
        self.assertEqual(driver._driver.calls[-1], ("call", "/source"))

    def test_the_judge_screenshot_does_not_age_the_observation(self):
        from mobile_agent.state import Element, Snapshot
        button = Element("1", "Log Food", "Button", (.1, .1, .2, .05))
        snapshot = Snapshot([button], "Log Food", 400, 800, "wda", captured_at=100.0)
        shots, seen = FakeShots(), []
        shots.take = lambda name: time.sleep(.05) or "/shots/x.png"
        inner = FakeDriver()
        inner.execute = lambda operation, target, snap, text=None, timeout=10: seen.append(snap)
        RecordingDriver(inner, shots).execute("TAP", button, snapshot)
        self.assertGreaterEqual(seen[0].captured_at, 100.05)
        self.assertEqual(seen[0].elements, snapshot.elements)
        self.assertEqual(snapshot.captured_at, 100.0)

    def test_swipes_keys_and_answers(self):
        self.assertEqual(describe("SWIPE_UP"), {"type": "swipe", "direction": "up"})
        self.assertEqual(describe("HOME"), {"type": "home"})
        self.assertEqual(answer_text({"data": "312 kcal"}), "312 kcal")
        self.assertEqual(answer_text({"data": {"calories": "312"}}), '{"calories": "312"}')
        self.assertIsNone(answer_text({"data": None}))


if __name__ == "__main__":
    unittest.main()


class TaskCostCapTests(unittest.TestCase):
    """The spend cap buys every model the same turns: set in terra dollars, scaled by input price."""

    def test_a_terra_task_keeps_its_cap(self):
        self.assertAlmostEqual(task_cost_cap(["a"], "gpt-5.6-terra"), 0.18)
        self.assertAlmostEqual(task_cost_cap(["a"] * 9, "gpt-5.6-terra"), 0.60)

    def test_a_pricier_model_gets_a_proportionally_larger_cap(self):
        self.assertAlmostEqual(task_cost_cap(["a"], "gpt-5.6-sol"), 0.36)
        self.assertAlmostEqual(task_cost_cap(["a"] * 9, "gpt-5.6-sol"), 1.20)

    def test_a_cheaper_or_unpriced_model_is_never_cut_below_it(self):
        for model in ("gpt-5.6-luna", "an-unpriced-model", None):
            self.assertAlmostEqual(task_cost_cap(["a", "b"], model), 0.26)


class SessionModeTests(unittest.TestCase):
    def test_every_task_gets_a_new_session_with_the_products_capabilities_by_default(self):
        import json as _json
        import os
        from unittest import mock
        from mobile_agent.bench import iosworld
        sent = []

        class Reply:
            def __init__(self, body):
                self.body = body

            def read(self):
                return _json.dumps(self.body).encode()

        def urlopen(request, timeout=None):
            sent.append(_json.loads(request.data))
            return Reply({"value": {"sessionId": "new"}})
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch.dict(os.environ, {"MOBSTER_BENCH_SESSION": ""}):
            self.assertEqual(iosworld.wda_session("http://x"), "new")
            self.assertEqual(iosworld.wda_session("http://x", mode="legacy"), "new")
            with self.assertRaises(ValueError):
                iosworld.wda_session("http://x", mode="fast")
        self.assertFalse(sent[0]["capabilities"]["alwaysMatch"]["shouldWaitForQuiescence"])
        self.assertEqual(sent[1]["capabilities"]["alwaysMatch"], {})


class ResetModeTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from unittest import mock
        from mobile_agent.bench import iosworld
        self.iosworld, self.tmp = iosworld, tempfile.TemporaryDirectory()
        self.calls = []
        self.patches = [mock.patch.object(iosworld, "iosworld_reset", lambda *a: self.calls.append("iosworld")),
                        mock.patch.dict(os.environ, {"MOBSTER_BENCH_CHECKPOINTS": self.tmp.name})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_the_default_is_iosworlds_own_reset(self):
        info = self.iosworld.reset("repo", "U", {}, mode="iosworld")
        self.assertEqual((self.calls, info["mode"]), (["iosworld"], "iosworld"))

    def test_restore_seeds_once_then_restores_and_falls_back_on_a_digest_mismatch(self):
        import json as _json
        import pathlib
        from unittest import mock
        from mobile_agent.bench.evals import checkpoint
        manifest = {"notes": {"bundle_id": "com.x.notes"}}

        def save(udid, bundles, dest):
            pathlib.Path(dest).mkdir(parents=True)
            (pathlib.Path(dest) / "checkpoint.json").write_text(_json.dumps({"digest": "d"}))
        with mock.patch.object(checkpoint, "save", save), \
                mock.patch.object(checkpoint, "restore", mock.Mock(side_effect=[(4.5, True), (4.4, False)])):
            first = self.iosworld.reset("repo", "U", manifest, mode="restore")
            second = self.iosworld.reset("repo", "U", manifest, mode="restore")
            third = self.iosworld.reset("repo", "U", manifest, mode="restore")
        self.assertTrue(first["seeded"])
        self.assertEqual((second["digest_ok"], second["seconds"]), (True, 4.5))
        self.assertTrue(third["fell_back"])
        self.assertEqual(self.calls, ["iosworld", "iosworld"])  # the seed, then the fallback

    def test_an_unknown_mode_is_refused(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"MOBSTER_BENCH_RESET": "fast"}), self.assertRaises(ValueError):
            self.iosworld.reset_mode()


class EndStateTests(unittest.TestCase):
    """After a task, the apps' data is copied into the run folder as state/, from fake container folders."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.object(iosworld, "SIM_DEVICES", self.tmp / "Devices")
        patch.start()
        self.addCleanup(patch.stop)
        self.containers = self.tmp / "Devices/SIM/data/Containers"

        def container(kind, name, identifier, files):
            folder = self.containers / kind / name
            folder.mkdir(parents=True)
            (folder / iosworld.CONTAINER_METADATA).write_bytes(plistlib.dumps({"MCMMetadataIdentifier": identifier}))
            for rel, content in files.items():
                (folder / rel).parent.mkdir(parents=True, exist_ok=True)
                (folder / rel).write_bytes(content if isinstance(content, bytes) else content.encode())

        container("Data/Application", "A1", "com.iosworld.benchmark.caltrack",
                  {"Documents/fitnesssim_state.json": '{"log": [1]}', "Library/Caches/big.bin": "x",
                   "Library/Preferences/com.iosworld.benchmark.caltrack.plist": plistlib.dumps(
                       {"goal": 2450, "openai_api_key": "sk-test", "authToken": "t"})})
        container("Data/Application", "A2", "com.iosworld.benchmark.notes",
                  {"Library/Application Support/default.store": "db",
                   "Library/Application Support/default.store-wal": "w"})
        container("Data/Application", "A3", "com.iosworld.benchmark.mail", {"Documents/other-task.json": "{}"})
        container("Shared/AppGroup", "G1", "group.com.iosworld.benchmark.workspacesuite",
                  {"WorkspaceSuite/workspace_envelope.json": '{"seededData": {}}'})
        container("Shared/AppGroup", "G2", "group.com.apple.system", {"secret.db": "no"})

    def test_the_tasks_apps_and_their_vendors_app_groups_are_copied(self):
        bundles = ["com.iosworld.benchmark.caltrack", "com.iosworld.benchmark.notes", "com.iosworld.benchmark.weather"]
        iosworld.save_end_state("SIM", bundles, self.tmp / "run", started=0)
        state = self.tmp / "run/state"
        caltrack, notes = state / "apps/com.iosworld.benchmark.caltrack", state / "apps/com.iosworld.benchmark.notes"
        self.assertEqual((caltrack / "Documents/fitnesssim_state.json").read_text(), '{"log": [1]}')
        prefs = plistlib.loads((caltrack / "Library/Preferences/com.iosworld.benchmark.caltrack.plist").read_bytes())
        self.assertEqual(prefs, {"goal": 2450})  # keys never reach the run folder
        self.assertFalse((caltrack / "Library/Caches").exists())
        self.assertEqual((notes / "Library/Application Support/default.store-wal").read_text(), "w")
        self.assertFalse((state / "apps/com.iosworld.benchmark.mail").exists())
        envelope = state / "groups/group.com.iosworld.benchmark.workspacesuite/WorkspaceSuite/workspace_envelope.json"
        self.assertTrue(envelope.exists())
        self.assertFalse((state / "groups/group.com.apple.system").exists())
        record = json.loads((state / "state.json").read_text())
        self.assertEqual(record["missing"], ["com.iosworld.benchmark.weather"])
        self.assertEqual(record["groups"], ["group.com.iosworld.benchmark.workspacesuite"])
        self.assertEqual(record["started"], "1970-01-01T00:00:00Z")
        self.assertEqual(sorted(record["apps"]["com.iosworld.benchmark.caltrack"]),
                         ["Documents", "Library/Preferences/com.iosworld.benchmark.caltrack.plist"])
        self.assertFalse((state / "ERROR").exists())

    def test_the_app_is_sent_home_and_the_apps_closed_before_the_copy(self):
        order, settle = [], iosworld.STATE_SETTLE_SECONDS
        with mock.patch.object(iosworld.urllib.request, "urlopen", side_effect=lambda request, timeout: order.append(
                request.full_url)), mock.patch.object(iosworld.time, "sleep", side_effect=order.append), \
                mock.patch.object(iosworld.subprocess, "run",
                                  side_effect=lambda command, **_: order.append(command[2:])), \
                mock.patch("shutil.copytree", side_effect=lambda *a, **k: order.append("copy")):
            iosworld.save_end_state("SIM", ["com.iosworld.benchmark.notes"], self.tmp / "run", wda_url="http://wda")
        self.assertEqual(order[:5], ["http://wda/wda/homescreen", settle,
                                     ["terminate", "SIM", "com.iosworld.benchmark.notes"], settle, "copy"])

    def test_an_unreachable_wda_still_copies(self):
        with mock.patch.object(iosworld.urllib.request, "urlopen", side_effect=OSError("refused")), \
                mock.patch.object(iosworld.subprocess, "run"), mock.patch.object(iosworld.time, "sleep"):
            iosworld.save_end_state("SIM", ["com.iosworld.benchmark.notes"], self.tmp / "run", wda_url="http://wda")
        self.assertTrue((self.tmp / "run/state/apps/com.iosworld.benchmark.notes/Library/Application Support"
                         "/default.store").exists())

    def test_an_error_leaves_a_note_and_never_raises(self):
        with mock.patch("shutil.copytree", side_effect=OSError("disk full")):
            iosworld.save_end_state("SIM", ["com.iosworld.benchmark.notes"], self.tmp / "run")
        self.assertIn("disk full", (self.tmp / "run/state/ERROR").read_text())
        self.assertFalse((self.tmp / "run/state/state.json").exists())

    def test_a_missing_simulator_saves_nothing_and_raises_nothing(self):
        iosworld.save_end_state("GONE", ["com.iosworld.benchmark.notes"], self.tmp / "run")
        record = json.loads((self.tmp / "run/state/state.json").read_text())
        self.assertEqual((record["apps"], record["missing"]), ({}, ["com.iosworld.benchmark.notes"]))

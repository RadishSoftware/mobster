"""The editor extension's routes on `mobster serve` (testkit/editor.py): checks, suites, heals."""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent.testkit import editor, prrun
from mobile_agent.testkit.jobs import Suites

CHECK = """version: 1
name: Today lists the habits
app:
  bundle: org.mozilla.ios.Firefox
launch:
  url: firefox://today
expect:
  - text: [Drink water]
  - visible: { id: streak_count }
"""
VIEW = """import SwiftUI

struct TodayView: View {
    var body: some View {
        Text("Drink water")
            .accessibilityIdentifier("streak_count")
    }
}
"""


def finished_doc(root):
    frame = root / ".mobster" / "runs" / "r1" / "frames" / "verdict.jpg"
    return ({"suite": "x", "exitCode": 1, "summary": {"total": 1, "passed": 0, "failed": 1},
             "checks": [{"name": "Today lists the habits", "file": ".mobster/checks/today.yaml",
                         "results": [{"device": "iPhone 17", "status": "failed", "replayed": True,
                                      "reason": {"class": "assertion", "message": "Drink water isn't on screen",
                                                 "fix": None},
                                      "frame": str(frame), "report": "runs/r1.html",
                                      "assertions": [{"text": "text Drink water", "ok": False, "observed": "Water"},
                                                     {"text": "visible streak_count", "ok": True}],
                                      "healed": [{"step": 2, "from": "Tap Start", "to": "Tap Begin", "why": "label",
                                                  "old": {}, "new": {}}]}]}]},
            {"folder": root / ".mobster" / "test-results" / "x", "html": root / ".mobster" / "test-results" / "x" /
             "index.html"})


class EditorRoutesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        (self.root / ".mobster" / "checks").mkdir(parents=True)
        self.check = self.root / ".mobster" / "checks" / "today.yaml"
        self.check.write_text(CHECK)
        self.view = self.root / "App" / "TodayView.swift"
        self.view.parent.mkdir()
        self.view.write_text(VIEW)
        self.runtime = SimpleNamespace(editor_suites=Suites(), plan_gate=None)
        self.addCleanup(self.runtime.editor_suites.close)

    def get(self, handler, **query):
        return handler(SimpleNamespace(runtime=self.runtime, query={k: [v] for k, v in query.items()}, params={}))

    def post(self, handler, body, **params):
        return handler(SimpleNamespace(runtime=self.runtime, body=body, query={}, params=params))

    def wait(self, suite_id):
        self.assertTrue(self.runtime.editor_suites.get(suite_id).wait(5))
        return editor.suite(SimpleNamespace(runtime=self.runtime, params={"suite": suite_id}))

    # ---------------------------------------------------------------------------------------------- project

    def test_the_project_lists_its_checks_and_the_ones_that_cover_a_screen(self):
        answer = self.get(editor.project, root=str(self.root), file=str(self.view))
        self.assertEqual(answer.status, 200)
        rows = answer.value["checks"]
        self.assertEqual([(row["file"], row["name"], row["app"]) for row in rows],
                         [(".mobster/checks/today.yaml", "Today lists the habits", "org.mozilla.ios.Firefox")])
        self.assertIsNone(answer.value["problem"])
        covering = answer.value["covering"]
        self.assertEqual(covering["screen"], "TodayView")
        self.assertEqual([item["path"] for item in covering["checks"]], [str(self.check)])
        self.assertIn("expects id streak_count", covering["checks"][0]["reasons"])

    def test_a_folder_without_checks_says_how_to_get_one(self):
        with tempfile.TemporaryDirectory() as empty:
            answer = self.get(editor.project, root=empty)
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.value["checks"], [])
        self.assertEqual(answer.value["problem"], editor.NO_CHECKS)

    def test_a_relative_root_or_a_file_outside_the_project_is_refused(self):
        self.assertEqual(self.get(editor.project, root="project").status, 400)
        with tempfile.NamedTemporaryFile(suffix=".swift") as outside:
            answer = self.get(editor.project, root=str(self.root), file=outside.name)
        self.assertEqual(answer.status, 400)

    def test_a_file_that_shows_no_screen_covers_nothing(self):
        notes = self.root / "README.md"
        notes.write_text("Drink water")
        self.assertIsNone(self.get(editor.project, root=str(self.root), file=str(notes)).value["covering"])

    # ------------------------------------------------------------------------------------------------ suites

    def test_a_suite_runs_the_named_check_on_simulators_and_answers_each_result(self):
        seen = {}

        def plan(options, **_):
            seen["options"] = options
            return SimpleNamespace(root=self.root)

        def execute(planned, **_):
            return finished_doc(self.root)
        with mock.patch("mobile_agent.testkit.api.plan", plan), mock.patch("mobile_agent.testkit.api.execute", execute):
            answer = self.post(editor.start, {"root": str(self.root), "checks": [str(self.check)], "sim": "iPhone 17"})
            self.assertEqual(answer.status, 202)
            done = self.wait(answer.value["suite"]["suite"])
        self.assertEqual(seen["options"].paths, (str(self.check),))
        self.assertEqual((seen["options"].sims, seen["options"].devices, seen["options"].no_heal),
                         (("iPhone 17",), (), False))
        suite = done.value["suite"]
        self.assertEqual((suite["status"], suite["exitCode"]), ("finished", 1))
        result = suite["checks"][0]["results"][0]
        self.assertEqual(result["frame"], str(self.root / ".mobster" / "runs" / "r1" / "frames" / "verdict.jpg"))
        self.assertEqual(result["report"], str(self.root / ".mobster" / "test-results" / "x" / "runs" / "r1.html"))
        self.assertEqual(result["failed"], [{"text": "text Drink water", "observed": "Water"}])
        self.assertEqual(result["healed"], [{"step": 2, "from": "Tap Start", "to": "Tap Begin", "why": "label"}])
        listed = self.get(editor.project, root=str(self.root)).value["suite"]
        self.assertEqual(listed["suite"], suite["suite"])

    def test_test_on_save_runs_the_checks_for_what_changed_since_the_last_commit(self):
        seen = {}

        def test_pr(pr_options, options, *, root, **_):
            seen.update(pr=pr_options, options=options, root=root)
            doc, paths = finished_doc(self.root)
            return prrun.PrResult({"headline": "Mobster checks: 0 passed, 1 failed", "plan": {"screens": [
                {"name": "TodayView"}]}}, doc, paths, 1)
        with mock.patch("mobile_agent.testkit.prrun.test_pr", test_pr):
            answer = self.post(editor.start, {"root": str(self.root), "changed": True, "heal": False})
            done = self.wait(answer.value["suite"]["suite"])
        self.assertEqual((seen["pr"].base, seen["pr"].drafts, seen["pr"].github, seen["pr"].build),
                         ("HEAD", "off", "off", True))
        self.assertEqual((seen["options"].devices, seen["options"].no_heal, seen["root"]), ((), True, self.root))
        suite = done.value["suite"]
        self.assertEqual((suite["kind"], suite["message"], suite["screens"]),
                         ("changes", "Mobster checks: 0 passed, 1 failed", ["TodayView"]))

    def test_one_suite_at_a_time_and_stop_ends_it(self):
        release = threading.Event()

        def execute(planned, *, stop, **_):
            release.wait(5)
            return finished_doc(self.root)
        with mock.patch("mobile_agent.testkit.api.plan", lambda options, **_: None), \
                mock.patch("mobile_agent.testkit.api.execute", execute):
            first = self.post(editor.start, {"root": str(self.root)}).value["suite"]["suite"]
            busy = self.post(editor.start, {"root": str(self.root)})
            self.assertEqual((busy.status, busy.value["code"]), (409, "suite_running"))
            stopped = self.post(editor.stop, {}, suite=first)
            self.assertTrue(self.runtime.editor_suites.get(first).stop.is_set())
            self.assertEqual(stopped.value["suite"]["status"], "running")
            release.set()
            self.wait(first)

    def test_a_suite_is_refused_before_it_starts_when_the_request_is_wrong_or_the_plan_says_no(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml") as outside:
            cases = [{"root": "relative"}, {"root": str(self.root), "checks": [outside.name]},
                     {"root": str(self.root), "checks": [str(self.check)], "changed": True},
                     {"root": str(self.root), "device": "My iPhone"}, {"root": str(self.root), "heal": "no"}]
            for body in cases:
                with self.subTest(body=body):
                    self.assertEqual(self.post(editor.start, body).status, 400)
        self.runtime.plan_gate = SimpleNamespace(refusal=lambda: "Choose a plan or start your free trial.")
        answer = self.post(editor.start, {"root": str(self.root)})
        self.assertEqual((answer.status, answer.value["code"]), (403, "plan_required"))
        self.assertIsNone(self.runtime.editor_suites.latest())

    def test_an_unknown_suite_is_not_found(self):
        missing = editor.suite(SimpleNamespace(runtime=self.runtime, params={"suite": "20261024-101010-abcd"}))
        self.assertEqual(missing.status, 404)

    # ------------------------------------------------------------------------------------------------- heals

    def test_heals_are_listed_with_the_project_and_accepting_settles_them(self):
        from mobile_agent.replay_core import heal as heal_file
        from mobile_agent.replay_core.checks import cache_dir_for, identity
        from mobile_agent.replay_core.path import new_path
        from mobile_agent.replay_core.store import Cache, env_of, revision
        from mobile_agent.verify.checks import load_check
        check = load_check(self.check)
        cache = Cache(cache_dir_for(self.root / ".mobster" / "runs"))
        approved = new_path([{"op": "HOME"}] * 3, kind="check", identity=identity(check),
                            env=env_of("1.0 (1)", "iOS 26.4", "simulator", device_type="iPhone 17 Pro"))
        file = cache.save(approved)
        candidate = {**approved, "steps": [{"op": "HOME"}]}
        heal_file.record(self.check, [{"step": 2, "old": [{"op": "HOME"}], "new": [], "from": "Tap Start",
                                       "to": "Tap Begin", "why": "the label changed",
                                       "frames": {"after": ".mobster/runs/r1/frames/after.jpg"}}],
                         run_id="r1", cache_file=file, candidate=candidate, base_revision=revision(approved),
                         check_sha256=heal_file._check_digest(self.check),
                         effective_sha256=heal_file.effective_digest(check), proof=heal_file.proof_assets(check))
        cache.save({**candidate, "review": {"proposal": heal_file.read(self.check)["proposal"], "decision": "accept"}})
        listed = self.get(editor.project, root=str(self.root)).value["heals"]
        self.assertEqual([(row["check"], row["heals"][0]["to"]) for row in listed], [(str(self.check), "Tap Begin")])
        answer = self.post(editor.heals, {"root": str(self.root), "action": "accept", "checks": [str(self.check)]})
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.value["settled"], [{"check": str(self.check), "steps": 1}])
        self.assertEqual(answer.value["heals"], [])
        self.assertFalse(heal_file.heal_file(self.check).exists())

    def test_a_heal_decision_needs_accept_or_reject(self):
        self.assertEqual(self.post(editor.heals, {"root": str(self.root), "action": "maybe"}).status, 400)


if __name__ == "__main__":
    unittest.main()

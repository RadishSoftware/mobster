import json
import os
import re
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from mobile_agent import engines
from mobile_agent.replay_core import engine
from mobile_agent.replay_core import heal as heals
from mobile_agent.replay_core.engine import Heal, Replayer, match
from mobile_agent.replay_core.path import PathError, describe, new_path, validate
from mobile_agent.replay_core.recorder import Recorder, literal_text
from mobile_agent.replay_core.store import KEEP, Cache, env_of, ios_major, revision
from mobile_agent.tests import replay_world as W
from mobile_agent.verify import runner
from mobile_agent.verify.checks import dump_check, load_check


class Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)


class Project:

    def __init__(self, case, *, check=None, version="1.0 (1)", runtime="iOS 26.4"):
        folder = tempfile.TemporaryDirectory()
        case.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.checks = self.root / ".mobster" / "checks"
        self.checks.mkdir(parents=True)
        self.file = self.checks / "signup.yaml"
        self.file.write_text(dump_check(check or W.check()), encoding="utf-8")
        self.runs = self.root / ".mobster" / "runs"
        self.state = W.new_state()
        self.phone = W.LumenPhone(self.state)
        self.manager = W.Manager(self.state, version=version, runtime=runtime)
        self.models = []
        self.clock = Clock()  # verify's settle loop waits on this clock, not the wall's

    @property
    def calls(self):
        return sum(model.calls for model in self.models)

    def client(self, key=None, **_):
        model = W.LumenModel()
        self.models.append(model)
        return model

    def run(self, replay="auto", key="sk-test-never-sent", pin=None, overlay=None, before_prepare=None):
        check = load_check(self.file)
        if overlay is not None:
            check = overlay(check)
        run = runner.VerifyRun(check, mode="smart", runs_dir=self.runs, manager=self.manager, key=key,
                               replay=replay)
        if pin is not None:
            run.replay.pin = pin
        if before_prepare is not None:
            before_prepare()
        kwargs = engines.frontier_kwargs
        with mock.patch.object(engines, "frontier_kwargs",
                               lambda *a, **kw: {**kwargs(*a, **kw), "settle_seconds": 0}), \
                mock.patch.object(engine, "MIN_WAIT", .05), mock.patch.object(engine, "POLL_SECONDS", .01), \
                mock.patch.object(runner, "_now", self.clock.time), mock.patch.object(runner, "_pause",
                                                                                      self.clock.sleep), \
                mock.patch.object(runner, "build_driver", lambda *args: self.phone), \
                mock.patch.object(engines, "build_client", self.client), \
                mock.patch.object(engines, "smart_key", lambda env=None: key), \
                mock.patch.object(runner, "front_app", lambda url, timeout=5: (W.BUNDLE, self.manager.pid)):
            try:
                run.prepare()
                run.run_flow()
            except runner.CouldntRun:
                pass
            return run.finish()


class CheckReplayTests(unittest.TestCase):
    def test_a_real_phone_replay_stays_in_its_app_and_never_captures_another_apps_frame(self):
        from mobile_agent.replay_core.checks import identity
        for leaves in (False, True, "unknown"):
            with self.subTest(leaves=leaves):
                project = Project(self, check=W.check(steps=["Tap Continue"], expect=[{"text": "Create your account"}]))
                project.manager.real_device = True
                check = load_check(project.file)
                cache = Cache(project.root / ".mobster" / "cache" / "replay")
                cache.save(new_path([{"op": "TAP", "app": W.BUNDLE,
                                      "target": {"label": "Continue", "role": "Button"}}], kind="check",
                                    identity=identity(check), env=env_of("1.0 (1)", "iOS 26.4", "iphone",
                                                                        device_type=project.manager.target.device_type)))
                observe = project.phone.observe
                execute = project.phone.execute
                def read(timeout=10):
                    snapshot = observe(timeout)
                    if leaves and project.state["log"]:
                        snapshot.bundle_id = "com.apple.MobileSMS"
                    return snapshot
                def act(*args, **kwargs):
                    answer = execute(*args, **kwargs)
                    if leaves == "unknown":
                        raise TimeoutError("lost after dispatch")
                    return answer
                with mock.patch.object(project.phone, "observe", read), \
                        mock.patch.object(project.phone, "execute", act), \
                        mock.patch.object(project.phone, "active_app", lambda **kw: "com.apple.MobileSMS"
                                          if leaves and project.state["log"] else W.BUNDLE), \
                        mock.patch.object(project.manager, "screenshot",
                                          wraps=project.manager.screenshot) as screenshot:
                    result = project.run(key=None)
                if leaves:
                    self.assertEqual((result["verdict"], result["reason"]["class"]),
                                     ("couldnt_run", "unknown_outcome" if leaves == "unknown" else "usage"))
                    if leaves != "unknown":
                        self.assertIn("com.apple.MobileSMS", result["reason"]["message"])
                    self.assertEqual(screenshot.call_count, 1, "only the launch screen may be captured")
                    self.assertFalse(any("verdict" in frame for frame in result["frames"]))
                else:
                    self.assertEqual(result["verdict"], "passed", result["reason"])
                    self.assertEqual(result["replay"]["replayed"], 1)
                self.assertEqual(project.calls, 0)

    def test_a_failed_or_raising_healer_never_captures_another_apps_frame(self):
        from types import SimpleNamespace

        from mobile_agent.replay_core.checks import identity
        for raises in (False, True):
            with self.subTest(raises=raises):
                project = Project(self)
                project.manager.real_device = True
                project.state["build"] = 2
                check = load_check(project.file)
                Cache(project.root / ".mobster" / "cache" / "replay").save(new_path(
                    [{"op": "TAP", "app": W.BUNDLE, "target": {"label": "Continue", "role": "Button"}},
                     {"op": "HOME"}, {"op": "HOME"}], kind="check", identity=identity(check),
                    env=env_of("1.0 (1)", "iOS 26.4", "iphone",
                               device_type=project.manager.target.device_type)))
                def build(driver, client, **kwargs):
                    def run(*args, **options):
                        kwargs["guard"].allows_app("com.apple.MobileSMS")
                        if raises:
                            raise runner.CouldntRun("model", "failed after leaving the app")
                        return {"status": "blocked", "reason": "left the app"}
                    return SimpleNamespace(run=run)
                with mock.patch.object(engines, "build_frontier", build), \
                        mock.patch.object(project.manager, "screenshot",
                                          wraps=project.manager.screenshot) as screenshot:
                    result = project.run()
                self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "usage"))
                self.assertEqual(screenshot.call_count, 2, "only launch and the own-app heal-before frame")
                self.assertFalse(any("verdict" in frame or "heal-after" in frame for frame in result["frames"]))

    def test_a_passing_run_records_and_the_next_replays_with_no_model(self):
        project = Project(self)
        first = project.run()
        self.assertEqual(first["verdict"], "passed", first["reason"])
        self.assertEqual(first["replay"]["mode"], "live")
        self.assertTrue(first["replay"]["saved"])
        calls = project.calls
        self.assertGreater(calls, 0)
        second = project.run()
        self.assertEqual(second["verdict"], "passed", second["reason"])
        self.assertEqual(project.calls, calls, "a replay calls no model")
        self.assertEqual(second["replay"]["mode"], "replayed")
        self.assertEqual(second["replay"]["replayed"], 3)
        self.assertEqual(second["cost_usd"], 0)
        self.assertEqual([step["text"] for step in second["steps"]],
                         ['Replayed: Tap "Continue"', 'Replayed: Type in "Email"', 'Replayed: Tap "Create account"'])
        self.assertEqual(second["healed"], [])
        self.assertEqual(project.state["email"], "sam@example.com")

    def test_a_different_device_model_does_not_replay_an_approved_check(self):
        project = Project(self)
        self.assertEqual(project.run()["verdict"], "passed")
        calls = project.calls
        project.manager.target.device_type = "iPhone 16 Pro"
        result = project.run()
        self.assertEqual(result["replay"]["mode"], "live")
        self.assertIn("device model changed", result["replay"]["reason"])
        self.assertGreater(project.calls, calls)

    def test_the_verdict_still_comes_from_the_tree(self):
        project = Project(self)
        project.run()
        data = json.loads(json.dumps(W.check(expect=[{"text": "Signed in as someone@example.com"}])
                                     .expect[0].to_dict()))
        text = project.file.read_text().replace("Signed in as sam@example.com", data["text"])
        project.file.write_text(text)  # expectations aren't part of the path: it still replays
        result = project.run()
        self.assertEqual(result["replay"]["mode"], "replayed")
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "assertion"))

    def test_a_changed_label_stages_a_diff_without_activating_it(self):
        project = Project(self)
        project.run()
        cache_file = next((project.root / ".mobster" / "cache" / "replay").glob("*.json"))
        approved = json.loads(cache_file.read_text())
        approved_revision = revision(approved)
        project.state["build"] = 2  # "Continue" is "Next" now
        before = project.calls
        healed = project.run()
        self.assertEqual(healed["verdict"], "passed", healed["reason"])
        self.assertEqual(healed["replay"]["mode"], "healed")
        self.assertEqual(project.calls - before, 1, "Smart heals that one step, then the replay takes over")
        [item] = healed["healed"]
        self.assertEqual((item["step"], item["from"], item["to"]), (1, 'Tap "Continue"', 'Tap "Next"'))
        self.assertEqual(item["old"], [{"op": "TAP", "label": "Continue", "role": "Button"}])
        self.assertEqual(item["new"], [{"op": "TAP", "label": "Next", "role": "Button"}])
        self.assertTrue(Path(item["frames"]["before"]).is_file() and Path(item["frames"]["after"]).is_file())
        diff = heals.read(project.file)
        self.assertEqual(diff["heals"][0]["to"], 'Tap "Next"')
        self.assertTrue(diff["heals"][0]["frames"]["before"].startswith(".mobster/runs/"))
        self.assertIn("mobster test heal --accept", heals.heal_file(project.file).read_text())
        self.assertEqual(load_check(project.file).steps, W.check().steps, "a heal never edits the check")
        self.assertEqual(json.loads(cache_file.read_text()), approved)
        self.assertEqual(revision(json.loads(cache_file.read_text())), approved_revision)
        self.assertEqual(len(list(cache_file.parent.glob("*.proposal.json"))), 1)
        again = project.run()
        self.assertEqual(project.calls - before, 1)
        self.assertEqual((again["verdict"], again["reason"]["class"], again["replay"]["pendingHeals"]),
                         ("failed", "drift", 1))
        self.assertEqual(json.loads(cache_file.read_text()), approved)

    def test_suite_only_scope_does_not_block_reviewable_heal(self):
        project = Project(self)
        overlay = lambda check: replace(check, a11y_scope="signup.yaml")
        self.assertEqual(project.run(overlay=overlay)["verdict"], "passed")
        project.state["build"] = 2
        result = project.run(overlay=overlay)
        self.assertEqual(result["verdict"], "passed", result.get("reason"))
        self.assertEqual(result["replay"]["pendingHeals"], 1)
        self.assertTrue(heals.heal_file(project.file).exists())

    def test_saved_assertion_edit_after_admission_refuses_fixed_replay(self):
        project = Project(self)
        self.assertEqual(project.run()["verdict"], "passed")
        cache_file = next((project.root / ".mobster" / "cache" / "replay").glob("*.json"))
        approved = cache_file.read_bytes()
        def edit():
            project.file.write_text(project.file.read_text().replace(
                "Signed in as sam@example.com", "Signed in as changed@example.com"))
        result = project.run(replay="strict", before_prepare=edit)
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "replay"))
        self.assertEqual(cache_file.read_bytes(), approved)
        self.assertFalse(heals.heal_file(project.file).exists())

    def test_a_replay_error_keeps_the_approved_path(self):
        project = Project(self)
        project.run()
        cache_file = next((project.root / ".mobster" / "cache" / "replay").glob("*.json"))
        approved = cache_file.read_bytes()
        send = project.phone.execute
        calls = []

        def failing(operation, target, snapshot, text=None, timeout=10):
            calls.append(operation)
            if len(calls) == 2:
                raise TimeoutError("no answer from the phone")
            return send(operation, target, snapshot, text=text, timeout=timeout)

        with mock.patch.object(project.phone, "execute", failing):
            failed = project.run()
        self.assertNotEqual(failed["verdict"], "passed")
        self.assertEqual(failed["replay"]["status"], "error")
        self.assertEqual(cache_file.read_bytes(), approved)
        project.state.update(W.new_state())
        again = project.run()
        self.assertEqual(again["replay"]["mode"], "replayed")

    def test_accept_keeps_the_heal_and_reject_keeps_the_check_failing(self):
        for choice in ("accept", "reject"):
            with self.subTest(choice):
                project = Project(self)
                project.run()
                project.state["build"] = 2
                project.run()
                cache = project.root / ".mobster" / "cache" / "replay"
                count = (heals.accept if choice == "accept" else heals.reject)(project.file, cache)
                self.assertEqual(count, 1)
                self.assertFalse(heals.heal_file(project.file).exists())
                calls = project.calls
                result = project.run()
                self.assertEqual(project.calls, calls, "neither choice calls a model")
                if choice == "accept":
                    self.assertEqual((result["verdict"], result["replay"]["pendingHeals"]), ("passed", 0))
                else:
                    self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "drift"))
                    self.assertIn("keep this check failing", result["reason"]["message"])

    def test_proposal_write_failure_keeps_approved_steps_and_refuses_a_green_run(self):
        project = Project(self)
        project.run()
        cache_file = next((project.root / ".mobster" / "cache" / "replay").glob("*.json"))
        approved = cache_file.read_bytes()
        project.state["build"] = 2
        with mock.patch.object(heals, "_write", side_effect=OSError("interrupted proposal")):
            result = project.run()
        self.assertEqual((result["verdict"], result["reason"]["class"], result["replay"]["status"]),
                         ("couldnt_run", "replay", "proposal_unavailable"))
        self.assertEqual(cache_file.read_bytes(), approved)
        self.assertFalse(heals.heal_file(project.file).exists())

    def test_review_refuses_changed_check_or_candidate_without_mutating_approved_path(self):
        for change in ("check", "candidate"):
            with self.subTest(change=change):
                project = Project(self)
                project.run()
                cache_file = next((project.root / ".mobster" / "cache" / "replay").glob("*.json"))
                approved = cache_file.read_bytes()
                project.state["build"] = 2
                project.run()
                if change == "check":
                    project.file.write_text(project.file.read_text() + "\n# edited after the heal\n")
                else:
                    next(cache_file.parent.glob("*.proposal.json")).write_text("{}")
                with self.assertRaises(heals.HealError):
                    heals.accept(project.file, cache_file.parent)
                self.assertEqual(cache_file.read_bytes(), approved)
                self.assertTrue(heals.heal_file(project.file).exists())

    def test_review_refuses_changed_inherited_fixture_after_staging(self):
        project = Project(self)
        fixtures = project.root / ".mobster" / "fixtures"
        fixtures.mkdir()
        fixture = fixtures / "base.yaml"
        fixture.write_text("launch:\n  env:\n    TEST_MODE: first\n")
        project.file.write_text(dump_check(replace(load_check(project.file), fixture=("base",))))
        self.assertEqual(project.run()["verdict"], "passed")
        cache_file = next((project.root / ".mobster" / "cache" / "replay").glob("*.json"))
        approved = cache_file.read_bytes()
        project.state["build"] = 2
        self.assertEqual(project.run()["verdict"], "passed")
        self.assertTrue(heals.heal_file(project.file).exists())
        fixture.write_text("launch:\n  env:\n    TEST_MODE: second\n")
        with self.assertRaises(heals.HealError):
            heals.accept(project.file, cache_file.parent)
        self.assertEqual(cache_file.read_bytes(), approved)

    def test_audit_a11y_baseline_is_part_of_the_replay_identity(self):
        from mobile_agent.replay_core.checks import identity
        project = Project(self)
        project.file.write_text(dump_check(replace(load_check(project.file), audit=("a11y",))))
        baseline = project.root / ".mobster" / "a11y-baseline.json"
        baseline.write_text('{"schema":"mobster.a11y-baseline/1","findings":["first"]}')
        check = load_check(project.file)
        first = heals.proof_assets(check)
        baseline.write_text('{"schema":"mobster.a11y-baseline/1","findings":["second"]}')
        second = heals.proof_assets(check)
        self.assertNotEqual(first["a11ySha256"], second["a11ySha256"])
        self.assertEqual(identity(check, asset_proof=first), identity(check, asset_proof=second),
                         "an assertion-only change keeps the same action path")

    def test_replaced_media_fifo_refuses_before_a_blocking_read(self):
        project = Project(self)
        media = project.root / "receipt.jpg"
        os.mkfifo(media)
        check = replace(load_check(project.file), conditions={"media": [str(media)]})
        with self.assertRaises(heals.HealError):
            heals.proof_assets(check)

    def test_accept_converges_after_cache_commit_even_when_candidate_is_shorter(self):
        from mobile_agent.replay_core.checks import identity
        project = Project(self)
        check = load_check(project.file)
        cache = Cache(project.root / ".mobster" / "cache" / "replay")
        approved = new_path([{"op": "HOME"}] * 3, kind="check", identity=identity(check),
                            env=env_of("1.0 (1)", "iOS 26.4", "simulator", device_type="iPhone 17 Pro"))
        file = cache.save(approved)
        candidate = {**approved, "steps": [{"op": "HOME"}]}
        heals.record(project.file, [{"step": 3, "old": [{"op": "HOME"}], "new": [],
                                    "from": "Go home", "to": "nothing", "why": "removed"}],
                     run_id="fake-run", cache_file=file, candidate=candidate,
                     base_revision=revision(approved), check_sha256=heals._check_digest(project.file),
                     effective_sha256=heals.effective_digest(check), proof=heals.proof_assets(check))
        proposal = heals.read(project.file)["proposal"]
        cache.save({**candidate, "review": {"proposal": proposal, "decision": "accept"}})
        self.assertEqual(heals.accept(project.file, cache.directory), 1)
        self.assertFalse(heals.heal_file(project.file).exists())
        self.assertEqual(len(cache.load(approved["identity"], approved["env"])["steps"]), 1)

    def test_no_heal_fails_on_a_change_without_a_model_call(self):
        project = Project(self)
        project.run()
        project.state["build"] = 2
        calls = project.calls
        result = project.run(replay="strict")
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "drift"))
        self.assertIn('"Continue" isn\'t on the screen', result["reason"]["message"])
        self.assertEqual(project.calls, calls)
        self.assertFalse(heals.heal_file(project.file).exists())

    def test_the_circuit_breaker_stops_when_most_steps_would_heal(self):
        project = Project(self)
        project.run()
        project.state["build"] = 3  # every control renamed
        result = project.run()
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "drift"))
        self.assertIn("More than half the recorded steps would heal", result["reason"]["message"])
        self.assertFalse(heals.heal_file(project.file).exists())

    def test_a_new_app_version_or_ios_major_records_again(self):
        project = Project(self)
        project.run()
        project.manager.version = "1.1 (2)"
        calls = project.calls
        result = project.run()
        self.assertGreater(project.calls, calls)
        self.assertEqual(result["replay"]["mode"], "live")
        self.assertIn("the app changed (recorded on 1.0 (1), this is 1.1 (2))", result["replay"]["reason"])
        project.manager.target.runtime = "iOS 27.0"
        result = project.run()
        self.assertIn("iOS changed (recorded on iOS 26, this is iOS 27)", result["replay"]["reason"])
        project.manager.target.runtime = "iOS 27.1"  # a minor update keeps the path
        calls = project.calls
        self.assertEqual(project.run()["replay"]["mode"], "replayed")
        self.assertEqual(project.calls, calls)

    def test_fresh_records_again_and_off_records_nothing(self):
        project = Project(self)
        project.run()
        calls = project.calls
        self.assertEqual(project.run(replay="fresh")["replay"]["reason"], "--fresh")
        self.assertGreater(project.calls, calls)
        other = Project(self)
        result = other.run(replay="off")
        self.assertNotIn("replay", result)
        self.assertFalse((other.root / ".mobster" / "cache" / "replay").exists())

    def test_text_the_model_wrote_is_a_live_step_it_types_each_run(self):
        check = W.check(steps=["Tap Continue", "Sign up with the test account"],
                        expect=[{"text": f"Signed in as {W.DEFAULT_EMAIL}"}])
        project = Project(self, check=check)
        project.run()
        calls = project.calls
        result = project.run()
        self.assertEqual(result["verdict"], "passed", result["reason"])
        self.assertEqual(result["replay"]["live"], 1)
        self.assertGreater(project.calls, calls, "a live step costs a model call")
        cache = json.loads(next((project.root / ".mobster" / "cache" / "replay").glob("*.json")).read_text())
        typed = cache["steps"][1]
        self.assertEqual((typed["op"], typed["text"], typed["live"]), ("TYPE", None, True),
                         "text the model wrote is never replayed")

    def test_a_replay_needs_no_key_and_without_one_a_change_fails(self):
        project = Project(self)
        project.run()
        calls = project.calls
        self.assertEqual(project.run(key=None)["verdict"], "passed")
        project.state["build"] = 2
        result = project.run(key=None)
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "drift"))
        self.assertEqual(project.calls, calls)

    def test_the_cache_is_private_to_this_user(self):
        project = Project(self)
        project.run()
        folder = project.root / ".mobster" / "cache" / "replay"
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for file in folder.iterdir():
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)

    def test_recording_again_replaces_a_waiting_heal(self):
        project = Project(self)
        project.run()
        project.state["build"] = 2
        project.run()
        self.assertTrue(heals.heal_file(project.file).exists())
        result = project.run(replay="fresh")
        self.assertTrue(result["replay"]["saved"])
        self.assertFalse(heals.heal_file(project.file).exists())
        self.assertEqual(project.run()["replay"]["pendingHeals"], 0)

    def test_a_heal_can_add_a_step(self):
        project = Project(self)
        project.run()
        project.state["build"] = 4  # a "What's new" sheet over the welcome screen
        result = project.run()
        self.assertEqual(result["verdict"], "passed", result["reason"])
        [item] = result["healed"]
        self.assertEqual((item["step"], item["old"], item["to"]), (1, [], 'Tap "Not now"'))
        self.assertEqual(result["replay"]["replayed"], 3, "the recorded steps all replayed after the added one")


class EngineTests(unittest.TestCase):
    def path(self, steps):
        return new_path(steps, kind="check", identity={"x": 1}, env={"app_version": "1"})

    def test_protected_steps_follow_insertion_and_deletion(self):
        missing = {"op": "TAP", "target": {"label": "Missing", "role": "Button"}}
        for resume, added, expected in ((1, [{"op": "HOME"}, {"op": "HOME"}], [4]),
                                        (2, [], [1])):
            with self.subTest(resume=resume):
                path = self.path([missing, {"op": "HOME"}, {"op": "HOME"}])
                path["strict"] = [3]
                result = Replayer(W.LumenPhone(W.new_state()), wait_seconds=0, breaker=1,
                                  heal=lambda *_: Heal(True, steps=added, resume_at=resume)).run(path)
                self.assertEqual(result.status, "done", result.reason)
                self.assertEqual(result.strict, expected)

    def recorded(self):
        project = Project(self)
        project.run()
        cache = project.root / ".mobster" / "cache" / "replay"
        return json.loads(next(cache.glob("*.json")).read_text()), project

    def test_a_commit_asks_and_a_no_stops_before_the_tap(self):
        path, project = self.recorded()
        delete = {"op": "TAP", "app": W.BUNDLE, "target": {"label": "Delete account", "role": "Button", "id": None,
                                                           "frame": None},
                  "screen": {"hash": "0" * 16, "signature": None}, "text": None, "param": None, "commit": None,
                  "point": None, "live": False}
        path["steps"].append(delete)
        for answer, status in (("denied", "declined"), ("approved", "done")):
            with self.subTest(answer):
                state = W.new_state()
                asked = []
                replayer = Replayer(W.LumenPhone(state), approve=lambda request: asked.append(request) or answer,
                                    wait_seconds=.2, sleep=lambda s: None)
                result = replayer.run(path)
                self.assertEqual(result.status, status, result.reason)
                self.assertEqual([request["act"] for request in asked], ["delete"])
                self.assertTrue(asked[0]["replayed"])
                self.assertEqual(state["deleted"], answer == "approved")

    def learned_gate(self, mode, runtime=True):
        from types import SimpleNamespace

        from mobile_agent.learned.workflows import gate_for
        from mobile_agent.permissions.gate import PermissionGate
        rt = SimpleNamespace(runs={"r1": object()},
                             permission_gate=lambda run: PermissionGate(mode)) if runtime else None
        return gate_for(SimpleNamespace(runtime=rt, run_id="r1", goal="Sign up to Lumen"))

    def test_a_replay_asks_what_the_runs_gate_asks_live_not_only_recorded_commits(self):
        from mobile_agent.permissions import policy
        path, _ = self.recorded()
        state, asked = W.new_state(), []
        result = Replayer(W.LumenPhone(state), approve=lambda request: asked.append(request) or "approved",
                          decide=self.learned_gate(policy.ASK_ALL), wait_seconds=.2, sleep=lambda s: None).run(path)
        self.assertEqual(result.status, "done", result.reason)
        self.assertEqual([(request["label"], request["act"]) for request in asked], [("Create account", "change")])
        state, asked = W.new_state(), []
        result = Replayer(W.LumenPhone(state), approve=lambda request: asked.append(request) or "denied",
                          decide=self.learned_gate(policy.ASK_ALL), wait_seconds=.2, sleep=lambda s: None).run(path)
        self.assertEqual(result.status, "declined")
        self.assertEqual(state["screen"], "signup", "a declined step is never tapped")

    def test_in_the_default_mode_a_plain_replay_still_runs_without_a_card(self):
        from mobile_agent.permissions import policy
        path, _ = self.recorded()
        state, asked = W.new_state(), []
        result = Replayer(W.LumenPhone(state), approve=lambda request: asked.append(request) or "approved",
                          decide=self.learned_gate(policy.ASK_IMPORTANT), wait_seconds=.2,
                          sleep=lambda s: None).run(path)
        self.assertEqual((result.status, asked), ("done", []), result.reason)
        self.assertEqual(state["screen"], "home")

    def test_with_no_gate_to_ask_a_replay_hands_off_before_its_first_act(self):
        from mobile_agent.permissions import policy
        path, _ = self.recorded()
        state = W.new_state()
        result = Replayer(W.LumenPhone(state), approve=None, on_commit="handoff",
                          decide=self.learned_gate(policy.ASK_IMPORTANT, runtime=False), wait_seconds=.2,
                          sleep=lambda s: None).run(path)
        self.assertEqual((result.status, state["screen"], state["log"]), ("handoff", "welcome", []), result.reason)
        def broken(*_):
            raise RuntimeError("unreadable step")
        state = W.new_state()
        result = Replayer(W.LumenPhone(state), approve=None, on_commit="handoff", decide=broken, wait_seconds=.2,
                          sleep=lambda s: None).run(path)
        self.assertEqual((result.status, state["log"]), ("handoff", []))

    def test_a_read_that_fails_after_an_action_ends_the_run_and_one_before_any_raises(self):
        path, _ = self.recorded()
        path["steps"].append({**path["steps"][-1], "target": {"label": "Delete account", "role": "Button",
                                                              "id": None, "frame": None},
                              "screen": {"hash": "0" * 16, "signature": None}})
        total = len(path["steps"])

        class Dropped(W.LumenPhone):
            def __init__(self, state, *, after):
                super().__init__(state)
                self.after, self.reads = after, 0

            def observe(self, timeout=10):
                self.reads += 1
                if self.after(self):
                    raise OSError("WebDriverAgent timed out")
                return super().observe(timeout)

        state = W.new_state()
        phone = Dropped(state, after=lambda p: state["deleted"])
        result = Replayer(phone, approve=lambda request: "approved", wait_seconds=.2, sleep=lambda s: None).run(path)
        self.assertTrue(state["deleted"])
        self.assertEqual((result.status, result.completed, result.at), ("error", total, total), result.reason)
        self.assertEqual(len(result.steps), total, "the acted steps are kept, none twice")
        with self.assertRaises(OSError):
            Replayer(Dropped(W.new_state(), after=lambda p: True), approve=None, wait_seconds=.2,
                     sleep=lambda s: None).run(path)

    def test_with_no_one_to_ask_a_commit_hands_off_or_stops(self):
        path, _ = self.recorded()
        path["steps"].append({**path["steps"][-1], "target": {"label": "Delete account", "role": "Button",
                                                              "id": None, "frame": None},
                              "screen": {"hash": "0" * 16, "signature": None}})
        for on_commit, status in (("handoff", "handoff"), ("deny", "declined")):
            state = W.new_state()
            result = Replayer(W.LumenPhone(state), approve=None, on_commit=on_commit, wait_seconds=.2,
                              sleep=lambda s: None).run(path)
            self.assertEqual(result.status, status)
            self.assertFalse(state["deleted"])

    def test_a_failing_action_is_an_error_at_that_step_and_is_not_sent_again(self):
        path, _ = self.recorded()
        executed = []

        class Failing(W.LumenPhone):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                executed.append(operation)
                if len(executed) == 2:
                    raise TimeoutError("no answer from the phone")
                return super().execute(operation, target, snapshot, text=text, timeout=timeout)

        result = Replayer(Failing(W.new_state()), approve=None, wait_seconds=.2, sleep=lambda s: None).run(path)
        self.assertEqual((result.status, result.at), ("error", 1), result.reason)
        self.assertIn("TimeoutError", result.reason)
        self.assertEqual(len(executed), 2, "the failed step went out once and nothing after it")

    def test_a_step_never_matches_a_partial_label_or_an_ambiguous_one(self):
        state = W.new_state()
        phone = W.LumenPhone(state)
        step = {"op": "TAP", "app": W.BUNDLE, "target": {"label": "Contin", "role": "Button"},
                "screen": {"hash": "x", "signature": [["Button", "Continue"]]}}
        ok, _, why = match(step, phone.observe(), settled=True)
        self.assertFalse(ok)
        self.assertIn("isn't on the screen", why)
        step["target"]["label"] = "Continue"
        self.assertTrue(match(step, phone.observe(), settled=True)[0])
        other = {**step, "app": "com.example.other"}
        self.assertFalse(match(other, phone.observe(), settled=True)[0])

    def test_typed_text_comes_only_from_the_check(self):
        policy = literal_text(["Sign up with sam@example.com"])
        self.assertEqual(policy("sam@example.com"), ("literal", "sam@example.com"))
        self.assertEqual(policy("hunter2"), ("live", None))

    def test_a_point_tap_never_retries_an_internal_type_error(self):
        snapshot = W.LumenPhone(W.new_state()).observe()
        for wrap in (False, True):
            with self.subTest(wrap=wrap):
                phone = mock.Mock()
                def tap(x, y, screen, timeout=10, purpose=None):
                    raise TypeError("failed after dispatch")
                phone.tap_point.side_effect = tap
                driver = Recorder().wrap(phone) if wrap else phone
                with self.assertRaisesRegex(TypeError, "failed after dispatch"):
                    Replayer(driver).act({"op": "TAP_XY", "point": [.5, .5]}, None, snapshot)
                self.assertEqual(phone.tap_point.call_count, 1)

    def test_a_failed_dispatch_reports_an_unknown_outcome(self):
        phone = W.LumenPhone(W.new_state())
        with mock.patch.object(phone, "execute", side_effect=TimeoutError("lost after dispatch")) as execute:
            result = Replayer(phone).run(self.path([{"op": "HOME"}]))
        self.assertEqual((result.status, result.model_status), ("error", "unknown_outcome"))
        self.assertEqual(execute.call_count, 1)

    def test_a_failed_completion_observer_preserves_an_unknown_action_result(self):
        phone = W.LumenPhone(W.new_state())
        execute = phone.execute
        sent = []
        def dispatch_then_fail(*args, **kwargs):
            execute(*args, **kwargs)
            sent.append(args[0])
            raise TimeoutError("lost after dispatch")
        def observe(event):
            if event["event"] == "replay_finished":
                raise OSError("event storage unavailable")
        with mock.patch.object(phone, "execute", dispatch_then_fail), \
                self.assertLogs("mobile_agent.replay_core.engine", level="ERROR"):
            result = Replayer(phone, emit=observe).run(self.path([{"op": "HOME"}]))
        self.assertEqual((result.status, result.model_status), ("error", "unknown_outcome"))
        self.assertEqual(sent, ["HOME"])

    def test_a_model_callback_that_raises_after_acting_has_an_unknown_outcome(self):
        for live in (False, True):
            with self.subTest(live=live):
                phone = W.LumenPhone(W.new_state())
                def act_then_fail(*args):
                    phone.execute("HOME", None, phone.observe())
                    raise TimeoutError("lost after dispatch")
                step = {"op": "TAP", "target": {"label": "Missing", "role": "Button"}, "live": live}
                with mock.patch.object(phone, "execute", wraps=phone.execute) as execute:
                    result = Replayer(phone, heal=act_then_fail, live=act_then_fail, breaker=1,
                                      wait_seconds=0).run(self.path([step]))
                self.assertEqual((result.status, result.model_status), ("error", "unknown_outcome"))
                self.assertEqual(execute.call_count, 1)

    def test_a_screen_read_that_fails_after_an_action_has_an_unknown_outcome(self):
        phone = W.LumenPhone(W.new_state())
        with mock.patch.object(Replayer, "settle", side_effect=TimeoutError("lost after dispatch")), \
                mock.patch.object(phone, "execute", wraps=phone.execute) as execute:
            result = Replayer(phone).run(self.path([{"op": "HOME"}, {"op": "HOME"}]))
        self.assertEqual((result.status, result.model_status), ("error", "unknown_outcome"))
        self.assertEqual(execute.call_count, 1)

    def test_a_read_that_fails_before_anything_was_sent_still_raises(self):
        phone = W.LumenPhone(W.new_state())
        with mock.patch.object(phone, "observe", side_effect=TimeoutError("no screen")):
            with self.assertRaises(TimeoutError):
                Replayer(phone).run(self.path([{"op": "HOME"}]))

    def test_unconfirmed_text_stops_before_the_next_action_without_retrying(self):
        for op, receipt, confirmed in (("TYPE", {"matches": True}, True),
                                        ("TYPE", {"matches": False}, False),
                                        ("TYPE", {"matches": None}, False),
                                        ("TYPE_SUBMIT", {"matches": True, "submitted": True}, True),
                                        ("TYPE_SUBMIT", {"matches": True, "submitted": False}, False)):
            with self.subTest(op=op, receipt=receipt):
                state = W.new_state()
                state["screen"] = "signup"
                phone = W.LumenPhone(state)
                path = self.path([
                    {"op": op, "app": W.BUNDLE, "target": {"label": "Email", "role": "TextField"},
                     "text": "sam@example.com"},
                    {"op": "TAP", "app": W.BUNDLE, "target": {"label": "Create account", "role": "Button"}},
                ])

                def write(target, snapshot, text, **kwargs):
                    phone.execute("SET_TEXT", target, snapshot, text=text if confirmed else "wrong@example.com")
                    return receipt

                with mock.patch.object(phone, "write_text", side_effect=write, create=True) as write_text:
                    result = Replayer(phone, wait_seconds=0, approve=lambda request: "approved").run(path)
                write_text.assert_called_once()
                self.assertEqual(state["screen"], "home" if confirmed else "signup")
                self.assertEqual(result.status, "done" if confirmed else "error")
                if not confirmed:
                    self.assertEqual(result.model_status, "unknown_outcome")
                    self.assertEqual(result.replayed, 0)

    def test_a_failed_seek_dispatch_has_an_unknown_outcome(self):
        phone = W.LumenPhone(W.new_state())
        with mock.patch.object(phone, "execute", side_effect=TimeoutError("lost after swipe")) as execute:
            result = Replayer(phone).run(self.path([{"op": "TAP", "seek": True,
                                                    "target": {"label": "Missing", "role": "Button"}}]))
        self.assertEqual((result.status, result.model_status), ("error", "unknown_outcome"))
        self.assertEqual(execute.call_count, 1)

    def test_stop_while_approval_waits_prevents_dispatch(self):
        phone = W.LumenPhone(W.new_state())
        stopped = []
        def approve(request):
            stopped.append(True)
            return "approved"
        replayer = Replayer(phone, approve=approve, cancelled=lambda: bool(stopped))
        with mock.patch.object(phone, "execute", wraps=phone.execute) as execute:
            result = replayer.run(self.path([{"op": "HOME", "commit": "other"}]))
        self.assertEqual(result.status, "stopped")
        execute.assert_not_called()

    def test_a_legacy_point_tap_is_bound_before_dispatch(self):
        snapshot = W.LumenPhone(W.new_state()).observe()
        for wrap in (False, True):
            with self.subTest(wrap=wrap):
                calls = []
                class LegacyPhone:
                    def tap_point(self, x, y, screen, timeout=10):
                        calls.append((x, y, screen))
                driver = LegacyPhone()
                if wrap:
                    driver = Recorder().wrap(driver)
                Replayer(driver).act({"op": "TAP_XY", "point": [.5, .5]}, None, snapshot)
                self.assertEqual(calls, [(.5, .5, snapshot)])

    def test_stop_during_precondition_wait_prevents_dispatch(self):
        phone = W.LumenPhone(W.new_state())
        clock = Clock()
        cancelled = []
        def pause(seconds):
            clock.sleep(seconds)
            cancelled.append(True)
        step = {"op": "TAP", "app": W.BUNDLE, "target": {"label": "Continue", "role": "Button"}}
        replayer = Replayer(phone, cancelled=lambda: bool(cancelled), sleep=pause, clock=clock.time)
        with mock.patch.object(phone, "execute", wraps=phone.execute) as execute:
            result = replayer.run(self.path([step]))
        self.assertEqual(result.status, "stopped")
        execute.assert_not_called()

    def test_a_stored_path_is_checked_before_it_is_trusted(self):
        good = self.path([{"op": "TAP", "target": {"label": "Continue", "role": "Button"}}])
        self.assertIs(validate(good), good)
        for bad in ({"op": "TAP"}, {"op": "rm -rf"}, {"op": "LAUNCH_APP", "app": "nope"},
                    {"op": []}, {"op": {}},
                    {"op": "TYPE", "target": {"label": "Email", "role": "TextField"}},
                    {"op": "TAP_XY", "point": [2, 2]}):
            with self.subTest(bad=bad), self.assertRaises(PathError):
                validate(self.path([bad]))
        self.assertEqual(describe({"op": "TAP", "target": {"label": "Next"}}), 'Tap "Next"')


class StoreTests(unittest.TestCase):
    def test_keys_by_app_version_ios_major_and_device_and_keeps_the_newest(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cache = Cache(folder.name)
        identity = {"kind": "check", "steps": ["a"]}
        step = [{"op": "HOME"}]
        envs = [env_of(f"1.{n}", "iOS 26.4", "simulator") for n in range(KEEP + 2)]
        for env in envs:
            cache.save(new_path(step, kind="check", identity=identity, env=env))
        self.assertIsNone(cache.load(identity, envs[0]), "older environments go")
        self.assertIsNotNone(cache.load(identity, envs[-1]))
        self.assertEqual(len(list(Path(folder.name).glob("*.json"))), KEEP)
        self.assertEqual(cache.load(identity, envs[-1])["env"]["ios"], "26")
        self.assertIsNone(cache.load(identity, env_of("1.5", "iOS 26.4", "iphone")))
        self.assertEqual([ios_major(v) for v in ("iOS 26.4", "26", "com.apple.CoreSimulator.SimRuntime.iOS-26-4",
                                                 None)], ["26", "26", "26", None])

    def test_a_corrupt_file_is_ignored(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cache = Cache(folder.name)
        identity, env = {"a": 1}, env_of("1", "26", "simulator")
        cache.save(new_path([{"op": "HOME"}], kind="check", identity=identity, env=env))
        file = next(Path(folder.name).glob("*.json"))
        file.write_text("{not json")
        self.assertIsNone(cache.load(identity, env))


class SuiteTests(unittest.TestCase):
    def suite(self, project, **options):
        import io

        from mobile_agent.testkit.api import Options, execute, plan
        from mobile_agent.testkit.execute import Attempts
        planned = plan(Options(paths=(str(project.checks),), retries=options.pop("retries", 0),
                               command="mobster test", **options),
                       cwd=project.root)
        attempts = Attempts(runs_dir=project.root / ".mobster" / "runs", key="sk-test-never-sent",
                            simulators=project.manager, replay=planned.options.replay_mode)
        kwargs = engines.frontier_kwargs
        with mock.patch.object(engines, "frontier_kwargs",
                               lambda *a, **kw: {**kwargs(*a, **kw), "settle_seconds": 0}), \
                mock.patch.object(engine, "MIN_WAIT", .05), mock.patch.object(engine, "POLL_SECONDS", .01), \
                mock.patch.object(runner, "_now", project.clock.time), \
                mock.patch.object(runner, "_pause", project.clock.sleep), \
                mock.patch.object(runner, "build_driver", lambda *args: project.phone), \
                mock.patch.object(engines, "build_client", project.client), \
                mock.patch.object(engines, "smart_key", lambda env=None: "sk-test-never-sent"), \
                mock.patch.object(runner, "front_app", lambda url, timeout=5: (W.BUNDLE, project.manager.pid)):
            return execute(planned, attempts=attempts, cwd=project.root, progress=io.StringIO().write)

    def test_a_lost_screen_after_dispatch_never_retries_the_check(self):
        project = Project(self)
        self.suite(project)
        calls = project.calls
        with mock.patch.object(engine.Replayer, "settle", side_effect=TimeoutError("lost after dispatch")), \
                mock.patch.object(project.phone, "execute", wraps=project.phone.execute) as execute:
            doc, _ = self.suite(project, retries=1)
        cell = doc["checks"][0]["results"][0]
        self.assertEqual(cell["attempts"], 1)
        self.assertEqual((cell["status"], cell["reason"]["class"]), ("couldnt_run", "unknown_outcome"))
        self.assertEqual([call.args[0] for call in execute.call_args_list].count("TAP"), 1)
        self.assertEqual(project.calls, calls)

    def test_a_heal_that_ends_with_an_unknown_action_never_retries_the_check(self):
        for live in (False, True):
            with self.subTest(live=live):
                project = Project(self)
                self.suite(project)
                if live:
                    cache = next((project.root / ".mobster" / "cache" / "replay").glob("*.json"))
                    path = json.loads(cache.read_text())
                    path["steps"][0]["live"] = True
                    cache.write_text(json.dumps(path))
                else:
                    project.state["build"] = 2
                def build(driver, client, **kwargs):
                    def act_then_lose(*args, **options):
                        driver.execute("HOME", None, driver.observe())
                        kwargs["emit"]({"event": "frontier_failed", "operation": "TAP", "outcome": "unknown"})
                        return {"status": "blocked", "reason": "The tap outcome is unknown."}
                    return mock.Mock(run=act_then_lose)
                with mock.patch.object(engines, "build_frontier", build):
                    doc, _ = self.suite(project, retries=1)
                cell = doc["checks"][0]["results"][0]
                self.assertEqual(cell["attempts"], 1)
                self.assertEqual((cell["status"], cell["reason"]["class"]), ("couldnt_run", "unknown_outcome"))

    def test_a_known_model_refusal_keeps_its_replay_failure_class(self):
        project = Project(self)
        self.suite(project)
        project.state["build"] = 2
        agent = mock.Mock()
        agent.run.return_value = {"status": "blocked", "code": "phone_locked", "reason": "The phone is locked"}
        with mock.patch.object(engines, "build_frontier", return_value=agent):
            doc, _ = self.suite(project)
        cell = doc["checks"][0]["results"][0]
        self.assertEqual((cell["status"], cell["reason"]["class"]), ("couldnt_run", "replay"))
        self.assertIn("phone is locked", cell["reason"]["message"])

    def test_results_json_reports_the_replay_and_the_heal_for_review(self):
        project = Project(self)
        doc, _ = self.suite(project)
        self.assertEqual(doc["summary"]["replayed"], 0)
        doc, paths = self.suite(project)
        cell = doc["checks"][0]["results"][0]
        self.assertTrue(cell["replayed"])
        self.assertEqual((cell["replay"]["mode"], cell["costUsd"]), ("replayed", 0))
        self.assertEqual((doc["summary"]["replayed"], doc["summary"]["cacheHitRate"]), (1, 1.0))
        page = Path(paths["html"]).read_text()
        self.assertIn("1 replayed with no model", page)
        project.state["build"] = 2
        doc, paths = self.suite(project)
        cell = doc["checks"][0]["results"][0]
        [item] = cell["healed"]
        self.assertEqual((item["step"], item["from"], item["to"]), (1, 'Tap "Continue"', 'Tap "Next"'))
        diff = (Path(paths["folder"]) / item["diff"]).resolve()
        self.assertEqual(diff, heals.heal_file(project.file).resolve(), "diff is relative to the suite folder")
        self.assertEqual(doc["summary"]["healed"], 1)
        page = Path(paths["html"]).read_text()
        self.assertIn("Healed: &quot;Continue&quot; is now &quot;Next&quot;", page)
        self.assertIn("mobster test heal --accept signup", re.sub(r"<[^>]+>", "", page))
        self.assertIn("1 healed step to review", page)
        run_page = Path(cell["report"] if Path(cell["report"]).is_absolute()
                        else Path(paths["folder"]) / cell["report"]).read_text()
        self.assertIn("Replayed in", run_page)

    def test_no_heal_suite_fails_on_the_change(self):
        project = Project(self)
        self.suite(project)
        project.state["build"] = 2
        doc, _ = self.suite(project, no_heal=True)
        self.assertEqual(doc["exitCode"], 1)
        self.assertEqual(doc["options"]["replay"], "strict")
        self.assertEqual(doc["checks"][0]["results"][0]["reason"]["class"], "drift")

    def test_heal_command_lists_accepts_and_rejects(self):
        import io
        from types import SimpleNamespace

        from mobile_agent.testkit import cli
        project = Project(self)
        self.suite(project)
        project.state["build"] = 2
        self.suite(project)
        args = SimpleNamespace(accept=False, reject=False, json=False)
        out = io.StringIO()
        self.assertEqual(cli.heal(args, [], cwd=project.root, out=out, err=io.StringIO()), 0)
        self.assertIn('step 1  Tap "Continue"  →  Tap "Next"', out.getvalue())
        self.assertIn("1 heal waits for review", out.getvalue())
        out = io.StringIO()
        cli.heal(SimpleNamespace(accept=False, reject=False, json=True), [], cwd=project.root, out=out)
        self.assertEqual(json.loads(out.getvalue())["heals"][0]["heals"][0]["to"], 'Tap "Next"')
        out = io.StringIO()
        cli.heal(SimpleNamespace(accept=True, reject=False, json=False), ["signup"], cwd=project.root, out=out)
        self.assertIn("Accepted: .mobster/checks/signup.yaml (1 step)", out.getvalue().replace(str(project.root) + "/",
                                                                                                ""))
        out = io.StringIO()
        cli.heal(args, [], cwd=project.root, out=out)
        self.assertEqual(out.getvalue().strip(), "No heals wait for review.")
        self.assertEqual(cli.heal(SimpleNamespace(accept=True, reject=True, json=False), [], cwd=project.root,
                                  out=io.StringIO(), err=io.StringIO()), 3)


if __name__ == "__main__":
    unittest.main()


class SearchTests(unittest.TestCase):

    def record(self, text, role="SearchField", label="Search"):
        from types import SimpleNamespace

        from mobile_agent.replay_core.recorder import Recorder
        recorder = Recorder(text_policy=literal_text(["Find a ramen place open late"]))
        return recorder.step_from(SimpleNamespace(op="TYPE", changed=True, screen="", app=W.BUNDLE, text=text,
                                                  commit=None, target={"label": label, "role": role, "id": None,
                                                                       "frame": [0, 0, 1, .1]}))

    def test_a_search_query_replays_and_other_made_up_text_does_not(self):
        step = self.record("ramen near me")
        self.assertEqual((step["text"], step["live"], step["search"]), ("ramen near me", False, True))
        step = self.record("ramen near me", role="TextField", label="Note")
        self.assertEqual((step["text"], step["live"]), (None, True))
        step = self.record("code ••••••")
        self.assertEqual((step["text"], step["live"]), (None, True), "a masked query is never replayed")

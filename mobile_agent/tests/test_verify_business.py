"""Saved business checks, one app-produced snapshot, and the run-owned fixture lifecycle."""

import hashlib
import json
import math
import re
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent.sim.models import SimError
from mobile_agent.tests.test_verify_runner import BUNDLE, KEY, PLANS, Driver, Manager, RunnerCase
from mobile_agent.verify import fixtures as yaml_fixtures
from mobile_agent.verify import runner
from mobile_agent.verify.actions import ActionJournal
from mobile_agent.verify.business import (
    BusinessBinding,
    BusinessExpectation,
    BusinessSpec,
    evaluate_business,
    load_business_binding,
)
from mobile_agent.verify.business_state import FILENAME, AppContainerFixture, FixtureError
from mobile_agent.verify.check_schema import schema
from mobile_agent.verify.checks import CheckError, check_from_dict, dump_check, load_check
from mobile_agent.verify.runner import CouldntRun, VerifyRun

REGISTRATION = {"version": 1, "adapters": {"firefox": {"kind": "app_container_json", "bundle": BUNDLE,
                "fields": ["reminder.saved"], "fixtures": {"fresh-reminder": {"reminder.saved": False}}}}}
SPEC = {"adapter": "firefox", "fixture": "fresh-reminder",
        "expect": [{"field": "reminder.saved", "equals": True}]}


def saved_check(root, *, steps=(), expect=None, business=None):
    folder = root / ".mobster"
    (folder / "checks").mkdir(parents=True, exist_ok=True)
    (folder / "fixtures.json").write_text(json.dumps(REGISTRATION), encoding="utf-8")
    check = check_from_dict({"app": {"bundle": BUNDLE}, "steps": list(steps), "expect": expect or [],
                             "business": SPEC if business is None else business})
    path = folder / "checks" / "business.yaml"
    path.write_text(dump_check(check), encoding="utf-8")
    return load_check(path)


class BusinessSchemaTests(unittest.TestCase):
    def test_schema_and_yaml_fixture_namespace_coexist(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            check = saved_check(root)
            yaml_folder = root / ".mobster" / "fixtures"
            yaml_folder.mkdir()
            (yaml_folder / "signed-out.yaml").write_text("launch:\n  args: ['-SignedOut', 'YES']\n")
            source = Path(check.source)
            source.write_text(dump_check(check).replace("business:\n", "fixture: signed-out\nbusiness:\n"))
            loaded = load_check(source)
            effective = yaml_fixtures.with_fixtures(loaded, root)
            self.assertEqual(effective.business, loaded.business)
            self.assertEqual(effective.launch_args, ("-SignedOut", "YES"))
            source.write_text(dump_check(effective))
            self.assertEqual(load_check(source).fixture, ("signed-out",))
            business_shape = schema()["else"]["properties"]["business"]["anyOf"][0]
            self.assertTrue(re.fullmatch(business_shape["properties"]["adapter"]["pattern"], "firefox"))
            self.assertNotIn("\\Z", business_shape["properties"]["adapter"]["pattern"])

    def test_file_round_trip_and_enrolled_binding_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            check = saved_check(root)
            binding = load_business_binding(check, root)
            self.assertEqual(check.business.to_dict(), SPEC)
            self.assertEqual(binding.bundle, BUNDLE)
            self.assertEqual(binding.seed_state(), {"reminder.saved": False})
            self.assertFalse(evaluate_business(binding, {"reminder.saved": False})[0]["ok"])
            self.assertTrue(evaluate_business(binding, {"reminder.saved": True})[0]["ok"])
            self.assertFalse(evaluate_business(binding, {"reminder.saved": 1})[0]["ok"])
            self.assertNotEqual(binding.digest, load_business_binding(
                check_from_dict({"app": {"bundle": BUNDLE}, "business": {**SPEC, "expect": []}}), root).digest)

    def test_json_numbers_match_without_confusing_booleans(self):
        spec = BusinessSpec("numbers", "start", (BusinessExpectation("count", 1.0),))
        binding = BusinessBinding(spec, BUNDLE, ("count",), (("count", 0),), "x" * 64)
        self.assertTrue(evaluate_business(binding, {"count": 1})[0]["ok"])
        self.assertFalse(evaluate_business(binding, {"count": True})[0]["ok"])

    def test_invalid_business_cannot_weaken_the_check(self):
        for business in ({"adapter": "firefox", "fixture": "fresh-reminder", "expect": [{"field": "x"}]},
                         {**SPEC, "expect": [{"field": "reminder.saved", "equals": 1},
                                               {"field": "reminder.saved", "equals": True}]},
                         {**SPEC, "expect": [{"field": "reminder.saved", "equals": math.inf}]},
                         {**SPEC, "command": "echo unsafe"},
                         {**SPEC, "fixture": "../other"}):
            with self.subTest(business=business), self.assertRaises(CheckError):
                check_from_dict({"app": {"bundle": BUNDLE}, "business": business})
        with self.assertRaises(CheckError):
            check_from_dict({"bundle_id": BUNDLE, "business": SPEC})

    def test_unregistered_bundle_field_and_duplicate_manifest_refuse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            check = saved_check(root)
            manifest = root / ".mobster" / "fixtures.json"
            for changed in ({**REGISTRATION, "adapters": {}},
                            {**REGISTRATION, "adapters": {"firefox": {**REGISTRATION["adapters"]["firefox"],
                                                                        "bundle": "dev.other.app"}}}):
                manifest.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(CheckError):
                    load_business_binding(check, root)
            manifest.write_text('{"version":1,"version":1,"adapters":{}}', encoding="utf-8")
            with self.assertRaisesRegex(CheckError, "duplicate registration key"):
                load_business_binding(check, root)


class ContainerFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.check = saved_check(self.root)
        self.binding = load_business_binding(self.check, self.root)
        # guard_container returns a physical path; /tmp is a symlink on macOS.
        self.container = (self.root / "container").resolve()
        (self.container / "Documents").mkdir(parents=True)
        self.manager = SimpleNamespace(real_device=False,
                                       simctl=lambda *args, **kwargs: SimpleNamespace(ok=True, out=str(self.container)))
        self.target = SimpleNamespace(udid="fake")
        self.actions = ActionJournal(self.root)
        self.guard = mock.patch("mobile_agent.sim.apps.guard_container", return_value=self.container)
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.fixture = AppContainerFixture(self.binding, self.manager, self.target, self.actions,
                                           "20261024-120000-abcd", "a" * 32)

    def test_seed_is_not_evidence_and_app_output_decides(self):
        self.fixture.setup()
        path = self.container / "Documents" / FILENAME
        seeded = json.loads(path.read_text())
        self.assertEqual((seeded["generation"], seeded["state"]), (0, {"reminder.saved": False}))
        with self.assertRaises(FixtureError):
            self.fixture.observe()
        seeded["generation"] = 1
        path.write_text(json.dumps(seeded))
        state, generation, digest = self.fixture.observe()
        self.assertEqual(generation, 1)
        self.assertFalse(evaluate_business(self.binding, state)[0]["ok"])
        self.assertEqual(len(digest), 64)
        seeded["state"]["reminder.saved"] = True
        path.write_text(json.dumps(seeded))
        self.assertTrue(evaluate_business(self.binding, self.fixture.observe()[0])[0]["ok"])
        self.assertEqual(self.fixture.remove(), "removed")
        self.assertFalse(path.exists())
        actions = (self.root / "actions.jsonl").read_text()
        rows = [json.loads(line) for line in actions.splitlines()]
        self.assertTrue(any(row.get("operation_id") == "20261024-120000-abcd:business:setup"
                            and row["event"] == "action_started" for row in rows))
        self.assertNotIn("reminder.saved", actions)
        self.assertNotIn("a" * 32, actions)

    def test_existing_file_and_symlink_refuse_without_overwrite(self):
        path = self.container / "Documents" / FILENAME
        path.write_text("existing")
        with self.assertRaisesRegex(FixtureError, "already exists"):
            self.fixture.setup()
        self.assertEqual(path.read_text(), "existing")
        path.unlink()
        elsewhere = self.root / "outside"
        elsewhere.write_text("private")
        path.symlink_to(elsewhere)
        with self.assertRaises(OSError):
            self.fixture.setup()
        self.assertEqual(elsewhere.read_text(), "private")

    def test_unsafe_container_refuses_before_seed(self):
        with mock.patch("mobile_agent.sim.apps.guard_container",
                        side_effect=SimError("simulator", "outside simulator")):
            with self.assertRaisesRegex(FixtureError, "not safely accessible"):
                self.fixture.setup()
        self.assertFalse((self.container / "Documents" / FILENAME).exists())

    def test_stale_token_refuses_and_sticky_journal_skips_cleanup(self):
        self.fixture.setup()
        path = self.container / "Documents" / FILENAME
        stale = json.loads(path.read_text())
        stale.update(runToken="b" * 32, generation=1)
        path.write_text(json.dumps(stale))
        with self.assertRaises(FixtureError):
            self.fixture.observe()
        with self.assertRaises(FixtureError):
            self.fixture.remove()
        self.assertTrue(path.exists())
        self.assertEqual(self.fixture.cleanup, "needs_reconciliation")

    def test_duplicate_observed_field_never_passes(self):
        self.fixture.setup()
        path = self.container / "Documents" / FILENAME
        path.write_text('{"schema":"mobster.business/1","runToken":"' + self.fixture.token +
                        '","generation":1,"state":{"reminder.saved":false,"reminder.saved":true}}')
        with self.assertRaisesRegex(FixtureError, "valid JSON"):
            self.fixture.observe()

    def test_failed_quiescence_keeps_fixture_for_reconciliation(self):
        self.fixture.setup()
        self.manager.simctl = lambda *args, **kwargs: (
            SimpleNamespace(ok=False, code=None, out="", err="timed out") if args[0] == "terminate"
            else SimpleNamespace(ok=True, code=0, out=str(self.container), err=""))
        with self.assertRaises(FixtureError):
            self.fixture.remove()
        self.assertEqual(self.fixture.cleanup, "needs_reconciliation")
        self.assertTrue((self.container / "Documents" / FILENAME).exists())
        self.assertEqual(self.actions.klass, "unknown_outcome")


class BusinessRunnerTests(RunnerCase):
    def test_yaml_directory_seed_cannot_claim_business_snapshot(self):
        check = saved_check(self.root)
        seed_folder = self.root / ".mobster" / "fixtures" / "state"
        seed_folder.mkdir(parents=True)
        (seed_folder / "Mobster-Business.JSON").write_text("{}")
        check = replace(check, seed=yaml_fixtures.Seed(files=((seed_folder, "Documents"),)))
        manager = Manager()
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager)
        with self.assertRaisesRegex(CouldntRun, "reserved app-state file"):
            run.prepare()
        self.assertEqual(manager.calls, [])

    def test_strict_temporal_check_refuses_before_device_effects(self):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "steps": ["Open settings"],
                                 "throughout": [{"no_text": "Error"}]})
        manager = Manager()
        run = VerifyRun(check, mode="smart", replay="strict", runs_dir=self.runs, manager=manager)
        with self.assertRaisesRegex(CouldntRun, "throughout or somewhere"):
            run.prepare()
        self.assertEqual(manager.calls, [])

    def test_strict_secret_check_refuses_before_device_effects(self):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "steps": ["Sign in with ${TEST_PASSWORD}"]})
        manager = Manager()
        run = VerifyRun(check, mode="smart", replay="strict", runs_dir=self.runs, manager=manager)
        with self.assertRaisesRegex(CouldntRun, "project secrets"):
            run.prepare()
        self.assertEqual(manager.calls, [])

    def test_typed_unknown_smart_action_cannot_pass_or_cleanup(self):
        check = self.check(steps=["Tap reminder"], expect=[{"text": "Choose your plan"}])
        result, _ = self.verify(check, Driver([PLANS]), mode="smart", key=KEY,
                                agent={"outcome": {"status": "blocked", "code": "unknown_outcome",
                                                   "reason": "gesture outcome unknown"}, "events": []})
        self.assertEqual((result["verdict"], result["reason"]["class"]),
                         ("couldnt_run", "unknown_outcome"))

    def _run(self, observations, *, cleanup_error=False, steps=(), expect=None, business=None, pre_error=None,
             finding=None):
        check = saved_check(self.root, steps=steps, expect=expect, business=business)
        manager, driver = Manager(), Driver([PLANS])
        manager.terminate = lambda *args: None
        self.watch(driver, manager)
        self.remove_calls = []
        remove_calls = self.remove_calls

        class FakeFixture:
            def __init__(self, binding, manager, target, actions, run_id, token):
                self.binding, self.token, self.seeded = binding, token, False
                self.cleanup = "not_needed"
                self.reads = iter(observations)

            def setup(self):
                self.seeded = True
                self.cleanup = "pending"

            def quiesce(self):
                pass

            def observe(self):
                value = next(self.reads)
                if isinstance(value, BaseException):
                    raise value
                state, generation = value if isinstance(value, tuple) else (value, 1)
                digest = hashlib.sha256(json.dumps({"state": state, "generation": generation}).encode()).hexdigest()
                return {"reminder.saved": state}, generation, digest

            def remove(self):
                remove_calls.append(True)
                if cleanup_error:
                    self.cleanup = "needs_reconciliation"
                    raise FixtureError("cannot remove")
                self.quiesce()
                self.cleanup = "removed"
                return self.cleanup

        with mock.patch("mobile_agent.verify.business_state.AppContainerFixture", FakeFixture), \
                mock.patch.object(runner, "build_driver", lambda *args: driver):
            run = VerifyRun(check, mode="keyless", runs_dir=self.runs, manager=manager)
            run.prepare()
            if pre_error is not None:
                run._fail(CouldntRun(pre_error, "The flow has an unknown action outcome"))
            if finding is not None:
                run.findings.append(finding)
            return run.finish()

    def test_business_only_success_mismatch_and_missing_evidence(self):
        passed = self._run([True])
        self.assertEqual((passed["verdict"], passed["business"]["cleanup"]), ("passed", "removed"))
        report = Path(passed["report"]).read_text()
        self.assertIn("Persisted app state", report)
        self.assertIn("reminder.saved", report)
        self.assertIn("Cleanup", report)
        failed = self._run([False])
        self.assertEqual((failed["verdict"], failed["reason"]["class"]), ("failed", "business"))
        missing = self._run([FixtureError("stale")])
        self.assertEqual((missing["verdict"], missing["reason"]["class"]),
                         ("couldnt_run", "business_evidence"))

    def test_baseline_held_and_cleanup_preserves_primary_verdict(self):
        held = self._run([True, True], steps=["Turn on reminder"])
        self.assertEqual((held["verdict"], held["reason"]["class"]), ("needs_review", "held_before_flow"))
        changed = self._run([False, (True, 2)], steps=["Turn on reminder"])
        self.assertEqual(changed["verdict"], "passed")
        stale = self._run([(False, 3), (True, 1)], steps=["Turn on reminder"])
        self.assertEqual(stale["reason"]["class"], "business_evidence")
        cleanup = self._run([False], cleanup_error=True)
        self.assertEqual((cleanup["verdict"], cleanup["reason"]["class"]),
                         ("couldnt_run", "business_cleanup"))
        self.assertEqual(cleanup["business"]["primary"]["class"], "business")

    def test_accessibility_and_business_both_have_to_hold(self):
        ax_ok_business_bad = self._run([False], expect=[{"text": "Choose your plan"}])
        self.assertTrue(ax_ok_business_bad["assertions"][0]["ok"])
        self.assertEqual(ax_ok_business_bad["reason"]["class"], "business")
        business_ok_ax_bad = self._run([True], expect=[{"text": "Text not on the screen"}])
        self.assertTrue(business_ok_ax_bad["business"]["assertions"][0]["ok"])
        self.assertEqual(business_ok_ax_bad["reason"]["class"], "assertion")
        setup_only = self._run([False], business={**SPEC, "expect": []})
        self.assertEqual(setup_only["reason"]["class"], "no_assertions")
        review_with_mismatch = self._run([False], finding={"level": "review", "message": "needs inspection"})
        self.assertEqual(review_with_mismatch["reason"]["class"], "business")

    def test_unknown_flow_effect_skips_fixture_cleanup(self):
        result = self._run([True], pre_error="unknown_outcome")
        self.assertEqual(result["reason"]["class"], "unknown_outcome")
        self.assertEqual(result["business"]["cleanup"], "needs_reconciliation")
        self.assertEqual(self.remove_calls, [])

    def test_reset_none_refuses_before_device_acquisition(self):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "reset": "none", "business": SPEC})
        manager = Manager()
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager)
        with self.assertRaises(CouldntRun):
            run.prepare()
        self.assertEqual(manager.calls, [])

    def test_real_device_refuses_before_device_acquisition(self):
        check = saved_check(self.root)
        manager = Manager()
        manager.real_device = True
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager)
        with self.assertRaisesRegex(CouldntRun, "managed simulator"):
            run.prepare()
        self.assertEqual(manager.calls, [])

    def test_uncertain_preseed_quiescence_reports_unknown_outcome(self):
        check = saved_check(self.root)
        manager = Manager()

        class BadFixture:
            seeded = False

            def __init__(self, *args):
                pass

            def quiesce(self):
                raise FixtureError("simctl did not confirm termination")

        with mock.patch("mobile_agent.verify.business_state.AppContainerFixture", BadFixture):
            run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager)
            with self.assertRaises(CouldntRun) as caught:
                run.prepare()
            self.assertEqual(caught.exception.klass, "unknown_outcome")
            self.assertEqual(run.finish()["reason"]["class"], "unknown_outcome")
        self.assertNotIn("launch", manager.names())

    def test_abort_during_setup_reports_unresolved_fixture(self):
        check = saved_check(self.root)
        manager, driver = Manager(), Driver([PLANS])
        self.watch(driver, manager)
        entered, release = threading.Event(), threading.Event()

        class PausedFixture:
            def __init__(self, binding, manager, target, actions, run_id, token):
                self.actions, self.seeded = actions, False

            def quiesce(self):
                pass

            def setup(self):
                with self.actions.action("BUSINESS_SETUP"):
                    entered.set()
                    release.wait(2)
                    self.seeded = True

            def remove(self):
                with self.actions.action("BUSINESS_CLEANUP"):
                    raise AssertionError("cleanup must not dispatch after stop")

        with mock.patch("mobile_agent.verify.business_state.AppContainerFixture", PausedFixture), \
                mock.patch.object(runner, "build_driver", lambda *args: driver):
            run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager)
            failure = []
            worker = threading.Thread(target=lambda: _prepare_capture(run, failure), daemon=True)
            worker.start()
            self.assertTrue(entered.wait(2))
            aborted = run.abort("stopped", "Stopped by caller")
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(aborted["reason"]["class"], "unknown_outcome")
        self.assertEqual(aborted["business"]["cleanup"], "needs_reconciliation")
        self.assertEqual(json.loads(Path(aborted["run_dir"], "result.json").read_text())["business"]["cleanup"],
                         "needs_reconciliation")
        self.assertNotIn("launch", manager.names())

    def test_abort_during_first_condition_mutation_reports_reconciliation(self):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "conditions": {"appearance": "dark"}})
        manager = Manager()
        entered, release = threading.Event(), threading.Event()
        restored = []

        class PausedConditions:
            applied = []
            skipped = []

            def __init__(self, *args, **kwargs):
                self.actions = kwargs["actions"]

            def before_launch(self):
                with self.actions.action("CONDITION_SET"):
                    entered.set()
                    release.wait(2)

            def launch_args(self):
                return ()

            def restore(self, actions):
                restored.append(True)

            def report(self):
                return {"applied": [], "skipped": []}

        with mock.patch("mobile_agent.verify.conditions.Applier", PausedConditions):
            run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager)
            failure = []
            worker = threading.Thread(target=lambda: _prepare_capture(run, failure), daemon=True)
            worker.start()
            self.assertTrue(entered.wait(2))
            aborted = run.abort("stopped", "Stopped by caller")
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(aborted["reason"]["class"], "unknown_outcome")
        self.assertEqual(aborted["conditions"]["cleanup"], "needs_reconciliation")
        self.assertEqual(restored, [])

    def test_abort_preserves_a_latched_unknown_after_dispatch_ends(self):
        check = check_from_dict({"app": {"bundle": BUNDLE}})
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=Manager())
        run.state = "preparing"
        with self.assertRaises(TimeoutError):
            with run.actions.action("RESET"):
                raise TimeoutError("no confirmation")
        result = run.abort("stopped", "Stopped by caller")
        self.assertEqual(result["reason"]["class"], "unknown_outcome")
        self.assertEqual(json.loads((run.run_dir / "result.json").read_text())["reason"]["class"],
                         "unknown_outcome")


class YamlSeedJournalTests(unittest.TestCase):
    def test_unconfirmed_keychain_reset_stops_before_pasteboard(self):
        with tempfile.TemporaryDirectory() as folder:
            actions = ActionJournal(folder)
            sent = []

            def simctl(args):
                sent.append(args[0])
                return 1, "not confirmed"

            with self.assertRaises(yaml_fixtures.FixtureError):
                yaml_fixtures.seed_simulator(yaml_fixtures.Seed(keychain=True, pasteboard="secret"),
                                             udid="sim", bundle=BUNDLE, run=simctl,
                                             pbcopy=lambda *_: sent.append("pasteboard"), actions=actions)
            self.assertEqual(sent, ["keychain"])
            self.assertEqual(actions.klass, "unknown_outcome")
            rows = [json.loads(line) for line in (Path(folder) / "actions.jsonl").read_text().splitlines()]
            self.assertEqual([(row["operation"], row["event"], row.get("outcome")) for row in rows],
                             [("FIXTURE_KEYCHAIN", "action_started", None),
                              ("FIXTURE_KEYCHAIN", "action_finished", "unknown")])
            self.assertNotIn("secret", (Path(folder) / "actions.jsonl").read_text())


def _prepare_capture(run, failure):
    try:
        run.prepare()
    except BaseException as error:
        failure.append(error)

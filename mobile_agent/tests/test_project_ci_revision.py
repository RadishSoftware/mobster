"""An approved prebuilt project revision is the sole source of a CI suite's device effects."""

import plistlib
import threading
import time
import uuid
from dataclasses import replace
from unittest import mock

from mobile_agent.api_errors import APIError
from mobile_agent.api_routes import Request
from mobile_agent.projects import routes
from mobile_agent.projects import service as project_service
from mobile_agent.tests.test_projects import Base
from mobile_agent.tests.test_testing import FakeAttempts
from mobile_agent.tests.timing import bound


class ApprovedCIRevisionTests(Base):
    def setUp(self):
        super().setUp()
        self.path = self.app_folder(mobster=False)
        self.project, _ = self.service.add({"path": str(self.path)})
        self.service.setup(self.project["id"], {})
        self.app = self.path / ".mobster" / "build" / "Dailies.app"
        self.app.mkdir(parents=True)
        with (self.app / "Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleIdentifier": "com.example.firefox-ios", "DTPlatformName": "iphonesimulator"},
                          stream)
        self.simulator = {"id": "SIM-UDID", "udid": "SIM-UDID", "kind": "simulator",
                          "name": "Mobster Sim", "state": "ready"}
        find = mock.patch.object(self.service, "_find_ci_simulator", return_value=self.simulator)
        policy = mock.patch.object(self.service, "_ci_policy", return_value=True)
        find.start()
        policy.start()
        self.addCleanup(find.stop)
        self.addCleanup(policy.stop)
        self.approval = {"checks": [".mobster/checks/app-opens.yaml"],
                         "appPath": ".mobster/build/Dailies.app", "simulatorId": "SIM-UDID", "reviewed": True}
        self.owner = "ci:" + str(uuid.uuid4())
        self.grant = "grant-one"

    def authorize(self, project_id, revision, grant_id, connection=None):
        if (project_id, revision, grant_id) != (self.project["id"], self.revision, self.grant):
            raise APIError("The local grant changed.", 403, "forbidden")

    def wait_for(self, run_id):
        until = time.monotonic() + bound(5)
        while time.monotonic() < until:
            status = self.service.ci_status(run_id, owner=self.owner, grant_id=self.grant,
                                            authorize=self.authorize)["suite"]
            if status["status"] not in ("admitted", "running", "stopping"):
                return status
            time.sleep(.01)
        self.fail("the approved suite did not finish")

    def test_revision_is_exact_immutable_and_not_an_implicit_build(self):
        first = self.service.approve_test_revision(self.project["id"], self.approval)
        self.revision = first["revision"]
        self.assertEqual((self.revision, first["created"]), (1, True))
        self.assertFalse(self.service.approve_test_revision(self.project["id"], self.approval)["created"])
        binding = self.service.ci_binding(self.project["id"], self.revision)
        self.assertEqual((binding["revision"], binding["simulatorId"]), (1, "SIM-UDID"))
        self.assertEqual(self.service.ci_candidates()["projects"][0]["revision"], 1)
        (self.app / "Info.plist").write_bytes((self.app / "Info.plist").read_bytes() + b"edited")
        with self.assertRaises(APIError) as changed:
            self.service.ci_binding(self.project["id"], self.revision)
        self.assertEqual(changed.exception.code, "revision_changed")

    def test_a_fixtures_dialog_answer_is_part_of_the_approved_revision(self):
        fixture = self.path / ".mobster" / "fixtures" / "permissions.yaml"
        fixture.parent.mkdir(parents=True)
        fixture.write_text("dialogs: {permissions: deny}\n")
        check = self.path / ".mobster" / "checks" / "app-opens.yaml"
        check.write_text(check.read_text() + "fixture: permissions\n")
        self.revision = self.service.approve_test_revision(self.project["id"], self.approval)["revision"]
        self.assertEqual(self.service.ci_binding(self.project["id"], self.revision)["revision"], self.revision)
        fixture.write_text("dialogs: {permissions: allow}\n")   # the check file is unchanged; its answer is not
        with self.assertRaises(APIError) as changed:
            self.service.ci_binding(self.project["id"], self.revision)
        self.assertEqual(changed.exception.code, "revision_changed")

    def test_real_parsed_check_executes_on_approved_simulator_and_exports_bound_identity(self):
        from mobile_agent.testkit import api, provenance

        self.revision = self.service.approve_test_revision(self.project["id"], self.approval)["revision"]
        prepared = self.service._prepared_revision(self.project["id"], self.revision)
        self.assertEqual(prepared.planned.entries[0].check.app_path, str(self.app))
        frozen = provenance.case_provenance(prepared.planned.entries[0], self.path)

        def execute(planned, **kwargs):
            return api.execute(planned, attempts=FakeAttempts(self.path, {}), **kwargs)

        self.service._run_parts["execute"] = execute
        admitted = self.service.ci_start(self.project["id"], "canonical", owner=self.owner,
                                         grant_id=self.grant, revision=self.revision,
                                         authorize=self.authorize)
        done = self.wait_for(admitted["suite"]["id"])
        self.assertEqual((done["status"], done["exitCode"], done["resultsAvailable"]),
                         ("completed", 0, True))
        exported = self.service.ci_results(done["id"], owner=self.owner, grant_id=self.grant,
                                           authorize=self.authorize)
        self.assertEqual(len(exported["artifacts"]["artifacts"]), 2)
        row = self.service.store.ci_run(done["id"], owner=self.owner, with_artifacts=True)
        import json
        safe = json.loads(row["artifacts"]["json"])
        self.assertEqual(safe["checks"][0]["checkSpecDigest"], frozen["checkSpecDigest"])
        self.assertEqual(safe["checks"][0]["appDigest"], frozen["appDigest"])

    def run_suite(self, request_id, during_attempt):
        """An approved suite through the real api.execute, with ``during_attempt`` run inside its one attempt."""
        from mobile_agent.testkit import api

        class Attempts(FakeAttempts):
            def __call__(self, check, record, **kwargs):
                during_attempt()   # after the per-attempt recheck, as a build or an editor writing the folder would
                return super().__call__(check, record, **kwargs)

        self.service._run_parts["execute"] = lambda planned, **kwargs: api.execute(
            planned, attempts=Attempts(self.path, {}), **kwargs)
        admitted = self.service.ci_start(self.project["id"], request_id, owner=self.owner, grant_id=self.grant,
                                         revision=self.revision, authorize=self.authorize)
        done = self.wait_for(admitted["suite"]["id"])
        return done, self.service.store.ci_run(done["id"], owner=self.owner)["run"]["code"]

    def test_an_app_rebuilt_during_the_attempt_is_never_exported_under_the_approved_identity(self):
        self.revision = self.service.approve_test_revision(self.project["id"], self.approval)["revision"]
        plist = self.app / "Info.plist"
        done, code = self.run_suite("rebuilt-in-attempt", lambda: plist.write_bytes(plist.read_bytes() + b"rebuilt"))
        self.assertEqual((done["status"], done["exitCode"], done["resultsAvailable"], code),
                         ("failed", 3, False, "inputs_changed"))
        with self.assertRaises(APIError) as nothing:
            self.service.ci_results(done["id"], owner=self.owner, grant_id=self.grant, authorize=self.authorize)
        self.assertEqual(nothing.exception.code, "results_unavailable")

    def stub_check(self, body_path):
        check = self.path / ".mobster" / "checks" / "app-opens.yaml"
        check.write_text(check.read_text() + "stubs:\n- match: {host: api.example.com, path_prefix: /v1/plan}\n"
                         f"  respond: {{status: 200, file: '{body_path}'}}\n")

    def test_a_stub_body_is_pinned_and_must_sit_inside_the_project(self):
        body = self.path / ".mobster" / "stubs" / "plan.json"
        body.parent.mkdir(parents=True)
        body.write_text('{"plan": "free"}')
        self.stub_check(body)
        self.revision = self.service.approve_test_revision(self.project["id"], self.approval)["revision"]
        self.assertEqual(self.service.ci_binding(self.project["id"], self.revision)["revision"], self.revision)
        body.write_text('{"plan": "pro"}')   # same path, another answer: the check could turn green
        with self.assertRaises(APIError) as changed:
            self.service.ci_binding(self.project["id"], self.revision)
        self.assertEqual(changed.exception.code, "revision_changed")
        outside = self.root / "plan.json"
        outside.write_text('{"plan": "free"}')
        linked = self.path / ".mobster" / "stubs" / "linked.json"
        linked.symlink_to(outside)
        for path in (outside, linked):   # outside the project, directly or through a link
            with self.subTest(path=path.name):
                self.stub_check(path)
                with self.assertRaises(APIError) as refused:
                    self.service.approve_test_revision(self.project["id"], self.approval)
                self.assertEqual(refused.exception.code, "revision_unavailable")

    def test_a_close_that_listed_a_suite_never_writes_its_ending_so_a_drift_still_fails_it(self):
        from mobile_agent.projects import revisions
        from mobile_agent.testkit import api
        self.revision = self.service.approve_test_revision(self.project["id"], self.approval)["revision"]
        plist = self.app / "Info.plist"
        listed, ended, closed = threading.Event(), threading.Event(), threading.Event()

        class Attempts(FakeAttempts):
            def __call__(self, check, record, **kwargs):
                plist.write_bytes(plist.read_bytes() + b"rebuilt")   # the app is rebuilt during the attempt
                listed.wait(bound(5))                                # while close lists the suite as going
                return super().__call__(check, record, **kwargs)

        real = revisions.revalidate

        def revalidate(prepared, **kwargs):
            if not next(iter(self.service._runs.values())).going:   # the check after the suite: close runs now
                ended.set()
                closed.wait(bound(5))
            return real(prepared, **kwargs)

        def too_late():   # the suite is sealed, so Stop is refused and the worker writes the ending
            listed.set()
            ended.wait(bound(5))
            return False

        self.service._run_parts["execute"] = lambda planned, **kwargs: api.execute(
            planned, attempts=Attempts(self.path, {}), **kwargs)
        with mock.patch.object(revisions, "revalidate", revalidate):
            run_id = self.service.ci_start(self.project["id"], "closed-at-the-end", owner=self.owner,
                                           grant_id=self.grant, revision=self.revision,
                                           authorize=self.authorize)["suite"]["id"]
            run = self.service._runs[run_id]
            run.stop = too_late
            closer = threading.Thread(target=self.service.begin_close)
            closer.start()
            closer.join(bound(10))
            closed.set()
            run.join(bound(10))
        row = self.service.store.ci_run(run_id, owner=self.owner)
        self.assertEqual((row["run"]["state"], row["run"]["exitCode"], row["run"]["code"], row["available"]),
                         ("failed", 3, "inputs_changed", False))

    def test_an_unexpected_error_in_the_check_after_the_suite_fails_it(self):
        from mobile_agent.projects import revisions
        self.revision = self.service.approve_test_revision(self.project["id"], self.approval)["revision"]
        real = revisions.revalidate

        def revalidate(prepared, **kwargs):
            if not next(iter(self.service._runs.values())).going:
                raise RuntimeError("the simulator list broke")
            return real(prepared, **kwargs)

        with mock.patch.object(revisions, "revalidate", revalidate):
            done, code = self.run_suite("revalidate-broke", lambda: None)
        self.assertEqual((done["status"], done["exitCode"], done["resultsAvailable"], code),
                         ("failed", 3, False, "inputs_changed"))

    def snapshot_check(self):
        check = self.path / ".mobster" / "checks" / "home.yaml"
        check.write_text("version: 1\nname: Home\napp: {bundle: com.example.firefox-ios}\n"
                         "expect:\n  - text: Home\n  - snapshot: home\n")
        self.approval = {**self.approval, "checks": [".mobster/checks/home.yaml"]}
        return self.path / ".mobster" / "snapshots" / "home"

    def test_the_baseline_the_simulators_run_compares_with_is_pinned_and_a_ci_suite_never_records_one(self):
        folder = self.snapshot_check()
        folder.mkdir(parents=True)
        other = folder / "home@iphone-17-pro-default-default-default.png"   # another device's baseline
        other.write_bytes(b"recorded on an iPhone 17 Pro")
        with self.assertRaises(APIError) as missing:   # the approved simulator's run would find none
            self.service.approve_test_revision(self.project["id"], self.approval)
        self.assertEqual(missing.exception.code, "revision_unavailable")
        self.assertIn("has no baseline for Mobster Sim", str(missing.exception))
        pinned = folder / "home@iphone-default-default-default.png"
        pinned.write_bytes(b"baseline")
        self.revision = self.service.approve_test_revision(self.project["id"], self.approval)["revision"]
        prepared = self.service._prepared_revision(self.project["id"], self.revision)
        self.assertFalse(prepared.planned.entries[0].check.snapshot_record)
        (folder / ".DS_Store").write_bytes(b"Finder")   # what the run doesn't read changes nothing
        other.write_bytes(b"recorded again")
        self.assertEqual(self.service.ci_binding(self.project["id"], self.revision)["revision"], self.revision)
        pinned.write_bytes(b"recorded later")
        with self.assertRaises(APIError) as changed:
            self.service.ci_binding(self.project["id"], self.revision)
        self.assertEqual(changed.exception.code, "revision_changed")
        elsewhere = self.root / "snapshots-elsewhere"
        folder.rename(elsewhere)
        folder.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(APIError) as linked:
            self.service.approve_test_revision(self.project["id"], self.approval)
        self.assertEqual(linked.exception.code, "revision_unavailable")

    def test_a_stub_body_changed_during_the_attempt_fails_the_suite_without_results(self):
        body = self.path / ".mobster" / "stubs" / "plan.json"
        body.parent.mkdir(parents=True)
        body.write_text('{"plan": "free"}')
        self.stub_check(body)
        self.revision = self.service.approve_test_revision(self.project["id"], self.approval)["revision"]
        done, code = self.run_suite("stub-in-attempt", lambda: body.write_text('{"plan": "pro"}'))
        self.assertEqual((done["status"], done["resultsAvailable"], code), ("failed", False, "inputs_changed"))

    def test_preparing_a_suite_never_holds_the_service_lock_and_a_changed_manifest_refuses(self):
        from mobile_agent.testkit import api
        self.revision = self.service.approve_test_revision(self.project["id"], self.approval)["revision"]
        self.service._run_parts["execute"] = lambda planned, **kwargs: api.execute(
            planned, attempts=FakeAttempts(self.path, {}), **kwargs)
        real, free = self.service._prepared_revision, []

        def prepared(project_id, revision):
            def probe():
                free.append(self.service._lock.acquire(blocking=False))
                if free[-1]:
                    self.service._lock.release()
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(bound(5))
            return real(project_id, revision)

        with mock.patch.object(self.service, "_prepared_revision", prepared):
            done = self.wait_for(self.service.ci_start(self.project["id"], "unlocked", owner=self.owner,
                                                       grant_id=self.grant, revision=self.revision,
                                                       authorize=self.authorize)["suite"]["id"])
        self.assertEqual((free, done["status"]), ([True], "completed"))

        def stale(project_id, revision):
            found = real(project_id, revision)
            return replace(found, manifest={**found.manifest, "replay": "0" * 64})

        with mock.patch.object(self.service, "_prepared_revision", stale), self.assertRaises(APIError) as changed:
            self.service.ci_start(self.project["id"], "stale", owner=self.owner, grant_id=self.grant,
                                  revision=self.revision, authorize=self.authorize)
        self.assertEqual(changed.exception.code, "revision_changed")
        self.assertIsNone(self.service.store.ci_run(owner=self.owner, request_id="stale"))

    def test_every_live_grant_is_named_so_each_suite_can_be_stopped(self):
        from types import SimpleNamespace
        self.service._runs = {"a": SimpleNamespace(going=True, owner="ci:a", grant_id="grant-b"),
                              "b": SimpleNamespace(going=True, owner="ci:b", grant_id="grant-a"),
                              "c": SimpleNamespace(going=False, owner="ci:c", grant_id="grant-ended"),
                              "d": SimpleNamespace(going=True, owner="local:x", grant_id=None)}
        self.assertEqual(self.service.active_grant_ids(), ["grant-a", "grant-b"])
        self.service._runs = {}

    def test_output_symlink_or_hardlinked_scratch_refuses_approval(self):
        outside = self.root / "outside"
        outside.mkdir()
        output = self.path / ".mobster" / "test-results"
        output.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(APIError) as refused:
            self.service.approve_test_revision(self.project["id"], self.approval)
        self.assertEqual(refused.exception.code, "revision_unavailable")
        output.unlink()
        scratch = self.path / ".mobster" / ".gitignore.tmp"
        (outside / "other").write_text("outside")
        scratch.hardlink_to(outside / "other")
        with self.assertRaises(APIError) as linked:
            self.service.approve_test_revision(self.project["id"], self.approval)
        self.assertEqual(linked.exception.code, "revision_unavailable")

    def test_prebuilt_app_tree_refuses_a_symlink_to_a_file_outside_the_project(self):
        outside = self.root / "outside.plist"
        outside.write_text("private")
        (self.app / "Extra.plist").symlink_to(outside)
        with self.assertRaises(APIError) as refused:
            self.service.approve_test_revision(self.project["id"], self.approval)
        self.assertEqual(refused.exception.code, "revision_unavailable")

    def test_claim_and_replay_are_owner_grant_and_revision_bound_without_a_second_effect(self):
        self.revision = self.service.approve_test_revision(self.project["id"], self.approval)["revision"]
        effects = []

        def execute(planned, *, progress, stop, suite_id, on_suite, cwd, before_attempt):
            before_attempt(planned.entries[0], planned.entries[0].check, self.simulator)
            effects.append(suite_id)
            return {"schema": "mobster.test/1", "suite": suite_id, "summary": {"total": 1, "passed": 1},
                    "exitCode": 0, "stopped": False,
                    "checks": [{"file": planned.entries[0].file, "results": []}],
                    "devices": [{"key": planned.targets[0].key, "kind": "simulator"}]}, {}

        self.service._run_parts["execute"] = execute
        with mock.patch("mobile_agent.testkit.hosted_results.build",
                        return_value={"json": b'{"summary":{"total":1,"passed":1}}',
                                      "junit": b"<x/>"}):
            first = self.service.ci_start(self.project["id"], "request-1", owner=self.owner,
                                          grant_id=self.grant, revision=self.revision,
                                          authorize=self.authorize)
            done = self.wait_for(first["suite"]["id"])
        self.assertEqual((done["status"], done["exitCode"], done["resultsAvailable"]),
                         ("completed", 0, True))
        replay = self.service.ci_start(self.project["id"], "request-1", owner=self.owner,
                                       grant_id=self.grant, revision=self.revision,
                                       authorize=self.authorize)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["suite"]["id"], first["suite"]["id"])
        self.assertEqual(len(effects), 1)
        with self.assertRaises(APIError) as rotated:
            self.service.ci_start(self.project["id"], "request-1", owner=self.owner,
                                  grant_id="new-local-grant", revision=self.revision,
                                  authorize=lambda *_args: None)
        self.assertEqual(rotated.exception.code, "idempotency_conflict")
        self.assertEqual(len(effects), 1)
        with self.assertRaises(APIError) as wrong_grant:
            self.service.ci_status(done["id"], owner=self.owner, grant_id="grant-two",
                                   authorize=self.authorize)
        self.assertEqual(wrong_grant.exception.code, "suite_not_found")

    def test_an_input_that_changes_after_admission_refuses_the_attempt_before_any_effect(self):
        from mobile_agent.testkit.targets import Refused
        self.revision = self.service.approve_test_revision(self.project["id"], self.approval)["revision"]
        effects, refusals = [], []

        def execute(planned, *, progress, stop, suite_id, on_suite, cwd, before_attempt):
            entry = planned.entries[0]
            other = {**self.simulator, "id": "OTHER-UDID", "udid": "OTHER-UDID"}
            (self.app / "Info.plist").write_bytes((self.app / "Info.plist").read_bytes() + b"rebuilt")
            for record in (other, self.simulator):   # another simulator, then the approved one with a rebuilt app
                try:
                    before_attempt(entry, entry.check, record)
                    effects.append(record["id"])
                except Refused as error:
                    refusals.append(error.klass)
            return {"suite": suite_id, "summary": {"total": 1}, "exitCode": 3, "stopReason": "refused"}, {}

        self.service._run_parts["execute"] = execute
        admitted = self.service.ci_start(self.project["id"], "rebuilt", owner=self.owner, grant_id=self.grant,
                                         revision=self.revision, authorize=self.authorize)
        done = self.wait_for(admitted["suite"]["id"])
        self.assertEqual((effects, refusals), ([], ["blocked", "blocked"]))
        self.assertEqual((done["status"], done["exitCode"], done["resultsAvailable"]), ("failed", 3, False))
        with self.assertRaises(APIError) as nothing:
            self.service.ci_results(done["id"], owner=self.owner, grant_id=self.grant, authorize=self.authorize)
        self.assertEqual(nothing.exception.code, "results_unavailable")

    def test_saved_device_rules_admit_an_allowed_app_and_refuse_a_damaged_or_narrower_rule(self):
        import json
        from types import SimpleNamespace

        from mobile_agent.devices import DeviceStore
        from mobile_agent.projects.service import ProjectService

        rules = self.root / "device-rules"
        rules.mkdir()
        store = DeviceStore(str(rules))
        self.runtime.fleet = SimpleNamespace(prefs_store=store)

        def approve(data):
            (rules / "devices.json").write_text(json.dumps({**store._empty(), **data}))
            with mock.patch.object(self.service, "_ci_policy", ProjectService._ci_policy.__get__(self.service)):
                return self.service.approve_test_revision(self.project["id"], self.approval)

        self.assertEqual(approve({"prefs": {"SIM-UDID": {"apps": ["com.example.firefox-ios"]}}})["revision"], 1)
        for data in ({"prefs": {"SIM-UDID": {"apps": ["com.example.other"]}}},   # this app isn't allowed there
                     {"disconnected": ["SIM-UDID"]},                             # turned off in Devices
                     {"prefs": {"SIM-UDID": {"apps": "all"}}},                    # damaged, not an allow-all
                     {"version": 2}):
            with self.subTest(data=data):
                with self.assertRaises(APIError) as refused:
                    approve(data)
                self.assertEqual(refused.exception.code, "revision_unavailable")

    def test_grant_revoked_inside_claim_transaction_admits_no_worker_or_row(self):
        self.revision = self.service.approve_test_revision(self.project["id"], self.approval)["revision"]
        called = []

        def revoked(project_id, revision, grant_id, connection=None):
            called.append(connection is not None)
            if connection is not None:
                raise APIError("The grant was revoked.", 403, "forbidden")

        with self.assertRaises(APIError) as refused:
            self.service.ci_start(self.project["id"], "revoked", owner=self.owner,
                                  grant_id=self.grant, revision=self.revision, authorize=revoked)
        self.assertEqual(refused.exception.code, "forbidden")
        self.assertEqual(called, [False, True])
        self.assertIsNone(self.service.store.ci_run(owner=self.owner, request_id="revoked"))

    def test_local_approval_route_requires_the_app_session(self):
        project_service._services[id(self.runtime)] = self.service
        self.addCleanup(project_service._services.pop, id(self.runtime), None)
        request = Request(runtime=self.runtime, method="POST", path="", params={"id": self.project["id"]},
                          query={}, headers={}, body=self.approval, content_type="application/json",
                          origin="cli", app_session=False)
        with self.assertRaises(APIError) as refused:
            routes.approve_test_revision(request)
        self.assertEqual(refused.exception.code, "app_only")
        request.app_session = True
        self.assertEqual(routes.approve_test_revision(request).status, 201)

    def test_local_options_show_bounded_project_choices_without_approving_them(self):
        project_service._services[id(self.runtime)] = self.service
        self.addCleanup(project_service._services.pop, id(self.runtime), None)
        request = Request(runtime=self.runtime, method="GET", path="", params={"id": self.project["id"]},
                          query={}, headers={}, body=None, content_type=None, origin="cli", app_session=False)
        with self.assertRaises(APIError) as refused:
            routes.test_revision_options(request)
        self.assertEqual(refused.exception.code, "app_only")
        request.app_session = True
        with mock.patch("mobile_agent.devices.simulator_records", return_value=[self.simulator]):
            choices = routes.test_revision_options(request).value
        self.assertEqual(choices["checks"][0]["path"], self.approval["checks"][0])
        self.assertEqual(choices["apps"], [{"path": self.approval["appPath"],
                                           "bundle": "com.example.firefox-ios"}])
        self.assertEqual(choices["simulators"][0]["id"], "SIM-UDID")
        self.assertIsNone(choices["latest"])
        revision = self.service.approve_test_revision(self.project["id"], self.approval)["revision"]
        with mock.patch("mobile_agent.devices.simulator_records", return_value=[self.simulator]):
            self.assertEqual(routes.test_revision_options(request).value["latest"]["revision"], revision)

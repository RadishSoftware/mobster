"""A project's approved CI suites: the revision a local app session approves (exact checks, one prebuilt simulator
.app, one managed simulator), and the suite a local CI grant runs from it. A part of ProjectService (service.py), which
owns the lock, the store and the runs; nothing here starts a run except ci_start, through the same durable claim."""

import json
import uuid
from pathlib import Path

from ..api_errors import APIError
from . import folder, revisions
from . import runs as checkruns
from .runs import MAX_RUNS, REQUEST_ID


class ApprovedSuites:
    def _find_ci_simulator(self, simulator_id):
        from ..devices import simulator_records
        return next((row for row in simulator_records(probe=False) if row.get("id") == simulator_id), None)

    def _ci_policy(self, simulator_id, bundles):
        try:
            saved = self.runtime.fleet.prefs_store.read_strict()
        except (AttributeError, OSError, ValueError):
            raise revisions.InputError("Saved simulator rules cannot be trusted") from None
        if simulator_id in saved["disconnected"]:
            return False
        allowed = (saved["prefs"].get(simulator_id) or {}).get("apps")
        return allowed is None or all(bundle in allowed for bundle in bundles)

    def approve_test_revision(self, project_id, body):
        """The Mac app reviews exact prebuilt inputs; a grant can later name only this immutable revision."""
        if (not isinstance(body, dict) or set(body) != {"checks", "appPath", "simulatorId", "reviewed"} or
                body["reviewed"] is not True):
            raise ValueError("Review selected checks, one prebuilt app and one managed simulator")
        self._admit()
        record = self._ready(project_id)
        try:   # hashes the app, up to 2 GB: never under the service lock
            manifest, _ = revisions.inspect(record["path"], body["checks"], body["appPath"], body["simulatorId"],
                                            find_simulator=self._find_ci_simulator, policy=self._ci_policy)
        except revisions.InputError as error:
            raise APIError(str(error), 409, "revision_unavailable") from None
        with self._lock:
            self._admit()
            revision, created = self.store.approve_revision(project_id, manifest, revisions.fingerprint(manifest))
            return {"projectId": project_id, "revision": revision,
                    "simulatorName": manifest["simulator"]["name"], "checks": len(manifest["checks"]),
                    "created": created}

    def _prepared_revision(self, project_id, revision):
        if type(revision) is not int or revision <= 0:
            raise APIError("Choose an approved test revision.", 404, "revision_not_found")
        record = self._record(project_id)
        manifest = self.store.revision(project_id, revision)
        if manifest is None or manifest.get("root") != record["path"]:
            raise APIError("That approved test revision is unavailable.", 404, "revision_not_found")
        try:
            return revisions.prepare(project_id, revision, manifest, find_simulator=self._find_ci_simulator,
                                     policy=self._ci_policy)
        except (OSError, ValueError):
            raise APIError("An approved check, app, simulator or policy changed. Approve a new revision locally.",
                           409, "revision_changed") from None

    def ci_binding(self, project_id, revision):
        """The trusted local grant issuer checks exact readiness; no caller receives a path or device ID."""
        prepared = self._prepared_revision(project_id, revision)
        return {"projectId": project_id, "revision": revision,
                "simulatorId": prepared.manifest["simulator"]["id"],
                "provenance": revisions.fingerprint(prepared.manifest)}

    def ci_candidates(self):
        """The Mac app's display list; grant issuance independently rechecks its selected binding."""
        out = []
        for project_id, revision in self.store.latest_revisions():
            try:
                record = self._record(project_id)
                manifest = self.store.revision(project_id, revision)
            except APIError:
                continue
            if (not isinstance(manifest, dict) or manifest.get("root") != record["path"] or
                    not isinstance(manifest.get("simulator"), dict) or
                    not isinstance(manifest["simulator"].get("name"), str)):
                continue
            out.append({"projectId": project_id, "name": record["name"], "revision": revision,
                        "simulatorName": manifest["simulator"]["name"]})
        return {"projects": out}

    def ci_revision_options(self, project_id):
        """Bounded display choices; approval and grant issuance recheck every selected input."""
        from ..devices import simulator_records

        record = self._ready(project_id)
        root = Path(record["path"])
        checks = [{"path": path, "name": Path(path).stem} for path in folder.checks(root, 100)]
        apps = revisions.prebuilt_apps(root)
        simulators = [{"id": row["id"], "name": row.get("name") or row["id"], "state": row["state"]}
                      for row in simulator_records(probe=False)[:100] if row.get("kind") == "simulator" and
                      row.get("id") == row.get("udid")]
        revision = next((value for identifier, value in self.store.latest_revisions()
                         if identifier == project_id), None)
        latest = revisions.latest(revision, self.store.revision(project_id, revision)) if revision else None
        return {"checks": checks, "apps": apps, "simulators": simulators, "latest": latest}

    @staticmethod
    def _ci_owner(owner, grant_id, authorize):
        try:
            valid = (isinstance(owner, str) and owner.startswith("ci:") and
                     str(uuid.UUID(owner[3:])) == owner[3:])
        except ValueError:
            valid = False
        if (not valid or not isinstance(grant_id, str) or not 1 <= len(grant_id) <= 128 or
                not callable(authorize)):
            raise APIError("This suite needs an active local CI grant.", 403, "test_not_allowed")

    @staticmethod
    def _ci_public(row):
        run = row["run"]
        state = run["state"]
        status = ("stopping" if state in ("building", "running") and run["stopRequested"] else
                  {"building": "admitted", "done": "completed"}.get(state, state))
        return {"id": run["id"], "projectId": row["project_id"], "revision": row["revision"],
                "status": status, "createdAt": run["startedAt"], "startedAt": run["startedAt"],
                "finishedAt": run["endedAt"], "done": run["done"], "total": run["total"],
                "exitCode": run["exitCode"], "summary": run["summary"],
                "resultsAvailable": row["available"]}

    def ci_projects(self, project_id, revision, *, grant_id, owner, authorize):
        self._ci_owner(owner, grant_id, authorize)
        authorize(project_id, revision, grant_id)
        prepared = self._prepared_revision(project_id, revision)
        return {"projects": [{"id": project_id, "revision": revision,
                              "name": self._record(project_id)["name"],
                              "simulator": prepared.manifest["simulator"]["name"],
                              "checks": [{"id": row["id"], "name": Path(row["path"]).stem}
                                         for row in prepared.manifest["checks"]]}]}

    def ci_start(self, project_id, request_id, *, owner, grant_id, revision, authorize):
        """The local signed grant and durable claim share a journal transaction before any device work."""
        self._ci_owner(owner, grant_id, authorize)
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ValueError("requestId must be 1 to 128 letters, numbers, _, ., : or -")
        self._admit()
        authorize(project_id, revision, grant_id)
        # Preparing hashes the app, up to 2 GB: never under the service lock. A replay needs none.
        prepared = None if self.store.ci_run(owner=owner, request_id=request_id, now=self.clock()) else \
            self._prepared_revision(project_id, revision)
        with self._lock:   # the claim's own transaction authorizes again before any row is written
            prior = self.store.ci_run(owner=owner, request_id=request_id, now=self.clock())
            if prior is not None:
                if (prior["project_id"], prior["grant_id"], prior["revision"]) != (
                        project_id, grant_id, revision):
                    raise APIError("That request ID belongs to another approved suite.", 409,
                                   "idempotency_conflict")
                self._record(project_id)
                return {"suite": self._ci_public(prior), "replayed": True}
            if self._closed or getattr(self.runtime, "closing", False):
                raise APIError("Mobster is closing. Start the suite after it restarts.", 503, "closing")
            self._admit()
            finished = [run for run in self._runs.values() if run.owner.startswith("ci:") and
                        not run.going and not run._thread.is_alive()]
            for old in sorted(finished, key=lambda item: item.started_at)[:-100]:
                self._runs.pop(old.id, None)
            going = [run for run in self._runs.values() if run.going]
            if len(going) >= MAX_RUNS or any(run.project_id == project_id for run in going):
                raise APIError("A check run is using this project's simulator. Wait for it to finish.", 409,
                               "suite_running")
            if prepared is None or self._record(project_id)["path"] != prepared.manifest["root"] or \
                    self.store.revision(project_id, revision) != prepared.manifest:
                raise APIError("An approved check, app, simulator or policy changed. Approve a new revision locally.",
                               409, "revision_changed")

            def before_attempt(_entry, _check, _record):
                from ..testkit.targets import Refused
                try:
                    pinned = prepared.manifest["simulator"]
                    if (not isinstance(_record, dict) or _record.get("kind") != "simulator" or
                            _record.get("id") != pinned["id"] or _record.get("udid") != pinned["udid"]):
                        raise revisions.InputError("The selected simulator changed")
                    self._record(project_id)
                    if self.store.revision(project_id, revision) != prepared.manifest:
                        raise revisions.InputError("The approved revision changed")
                    revisions.revalidate(prepared, find_simulator=self._find_ci_simulator, policy=self._ci_policy)
                    authorize(project_id, revision, grant_id)
                except (APIError, OSError, ValueError):
                    raise Refused("The approved project or local CI grant changed before this check ran.",
                                  klass="blocked") from None

            run = checkruns.CheckRun(uuid.uuid4().hex[:12], project_id, prepared.planned.root, mode="strict",
                                     request_id=request_id, owner=owner, grant_id=grant_id, revision=revision,
                                     prepared=prepared, before_attempt=before_attempt,
                                     on_change=lambda changed: self._ci_changed(changed, prepared),
                                     clock=self.clock, home=self.home, execute=self._run_parts.get("execute"))
            previous_mode, accepted, created = self.store.claim_run(
                run.public(), owner=owner, grant_id=grant_id, revision=revision, authorize=authorize)
            if not created:
                found = self.store.ci_run(owner=owner, request_id=request_id, now=self.clock())
                if found is None or (found["project_id"], found["grant_id"], found["revision"]) != (
                        project_id, grant_id, revision):
                    raise APIError("That request ID belongs to another approved suite.", 409,
                                   "idempotency_conflict")
                return {"suite": self._ci_public(found), "replayed": True}
            self._runs[run.id] = run
            try:
                run.start()
            except (OSError, RuntimeError):
                run._fail("worker_unavailable", "Mobster could not start the approved suite.",
                          "Restart Mobster, then submit a new request ID.")
            self._prune(project_id)
            found = self.store.ci_run(run.id, owner=owner)
        return {"suite": self._ci_public(found), "replayed": False}

    def _ci_changed(self, run, prepared):
        changed = False
        if not run.going and run.doc is not None:
            try:   # what the last attempt used, read after it: never under the service lock
                revisions.revalidate(prepared, find_simulator=self._find_ci_simulator, policy=self._ci_policy)
            except Exception:  # noqa: BLE001 -- inputs that can't be shown unchanged are changed
                changed = True
        with self._lock:
            public = run.public()
            if run.going:
                self.store.save_run(public)
            else:
                artifacts, error = None, "no_results"
                if run.doc is not None:
                    from ..testkit import hosted_results
                    try:
                        if changed:
                            raise hosted_results.InputsChanged("An approved input changed during the suite")
                        expected = prepared.planned.targets[0].key
                        devices = run.doc.get("devices") if isinstance(run.doc, dict) else None
                        if (not isinstance(devices, list) or len(devices) != 1 or
                                not isinstance(devices[0], dict) or
                                devices[0].get("key") != expected or
                                devices[0].get("kind") != "simulator"):
                            raise hosted_results.ExportError("The admitted simulator result is missing")
                        identity = revisions.identity(run.project_id, run.revision, prepared.manifest,
                                                      prepared.planned)
                        artifacts = hosted_results.build(run.doc, run.suite_id, identity=identity)
                        run.summary = json.loads(artifacts["json"])["summary"]
                        public = run.public()
                        error = None
                    except hosted_results.InputsChanged:
                        run.state, run.exit_code, run.code, run.summary = "failed", 3, "inputs_changed", None
                        run.error = "A check, its app or a file it uses changed during the suite. Nothing was exported."
                        public, error = run.public(), "inputs_changed"
                    except (hosted_results.ExportError, ValueError, KeyError):
                        run.state, run.exit_code, run.code = "failed", 3, "results_unavailable"
                        run.error = "Mobster could not safely export this suite's results."
                        run.summary = None
                        public, error = run.public(), "export_failed"
                self.store.complete_ci(public, artifacts, error)
                run.done, run.total = public["done"], public["total"]
                run.doc = None
                run._suite = None
                run._prepared = None
                run._before_attempt = None
                run._on_change = None
        self._publish("run_changed" if run.going else "run_ended", projectId=run.project_id, run=public)

    def _ci_owned(self, *, owner, grant_id, authorize, run_id=None, project_id=None, request_id=None,
                  with_artifacts=False):
        self._ci_owner(owner, grant_id, authorize)
        # By id or request, a removed suite answers its summary with no results (410 results_expired).
        row = self.store.ci_run(run_id, owner=owner, request_id=request_id, with_artifacts=with_artifacts,
                                now=self.clock())
        if (row is None or row["grant_id"] != grant_id or project_id is not None and
                row["project_id"] != project_id):
            raise APIError("That approved suite was not found.", 404, "suite_not_found")
        authorize(row["project_id"], row["revision"], grant_id)
        self._record(row["project_id"])
        return row

    def ci_status(self, run_id, *, owner, grant_id, authorize):
        return {"suite": self._ci_public(self._ci_owned(run_id=run_id, owner=owner, grant_id=grant_id,
                                                        authorize=authorize))}

    def ci_lookup(self, project_id, request_id, *, owner, grant_id, authorize):
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ValueError("Give a valid request ID")
        return {"suite": self._ci_public(self._ci_owned(project_id=project_id, request_id=request_id,
                                                        owner=owner, grant_id=grant_id, authorize=authorize))}

    def ci_stop(self, run_id, *, owner, grant_id, authorize):
        with self._lock:
            self._ci_owned(run_id=run_id, owner=owner, grant_id=grant_id, authorize=authorize)
            run = self._runs.get(run_id)
            if run is not None and run.owner == owner and run.grant_id == grant_id and run.stop():
                self._save_going(run)
        if run is not None:
            run.join(5)
        return self.ci_status(run_id, owner=owner, grant_id=grant_id, authorize=authorize)

    def active_grant_ids(self):
        """The grant of every approved suite still going: two projects' suites can run at once."""
        with self._lock:
            return sorted({run.grant_id for run in self._runs.values() if run.going and run.owner.startswith("ci:")})

    def stop_grant_if(self, grant_id, predicate):
        """A local expiry/revoke check and Stop share the suite admission/terminal lock."""
        with self._lock:
            run = next((run for run in self._runs.values() if run.going and run.grant_id == grant_id), None)
            if run is None or not predicate():
                return False
            accepted = run.stop()
            if accepted:
                self._save_going(run)
            return accepted

    def ci_results(self, run_id, *, owner, grant_id, authorize):
        row = self._ci_owned(run_id=run_id, owner=owner, grant_id=grant_id, authorize=authorize,
                             with_artifacts=True)
        if not row["available"]:
            code = "results_expired" if row["result_error"] == "expired" else "results_unavailable"
            raise APIError("This suite has no safe results to download.", 410 if code == "results_expired" else 503,
                           code)
        from ..testkit import hosted_results
        run = row["run"]
        return {"suite": self._ci_public(row), "results": {"summary": run["summary"],
                "exitCode": run["exitCode"], "stopped": run["state"] == "stopped"},
                "artifacts": hosted_results.manifest(row["artifacts"])}

    def ci_artifact(self, run_id, artifact_id, *, owner, grant_id, authorize, offset=0, limit=32768):
        row = self._ci_owned(run_id=run_id, owner=owner, grant_id=grant_id, authorize=authorize,
                             with_artifacts=True)
        if not row["available"]:
            raise APIError("This suite has no safe artifact to download.", 410,
                           "results_expired" if row["result_error"] == "expired" else "results_unavailable")
        from ..testkit import hosted_results
        try:
            return hosted_results.chunk(row["artifacts"], artifact_id, offset=offset, limit=limit)
        except (hosted_results.ArtifactNotFound, ValueError):
            raise APIError("That suite artifact or offset was not found.", 404, "artifact_not_found") from None

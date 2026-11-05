"""One VerifyRun's registered app-state fixture and evidence lifecycle."""

import secrets
from pathlib import Path

from .assertions import CheckError
from .business import FILENAME, evaluate_business, load_business_binding
from .checks import project_root
from .verdict import CouldntRun


def _seeds_reserved_file(seed):
    reserved = ("documents", FILENAME.casefold())
    for source, destination in seed.files:
        parts = tuple(part.casefold() for part in Path(destination).parts)
        if parts[:2] == reserved:
            return True
        if not source.is_dir() or parts != reserved[:len(parts)]:
            continue
        current = source
        for component in reserved[len(parts):]:
            current = next((child for child in current.iterdir() if child.name.casefold() == component), None)
            if current is None:
                break
        if current is not None:
            return True
    return False


class BusinessRun:
    def _load_business(self):
        if self.check.business is None:
            return
        if self.check.reset_level() == "none":
            raise CouldntRun("business", "App-state fixtures need reset: data or reinstall.",
                             "Set reset: data or reset: reinstall in the saved check.")
        if self.check.source is None:
            raise CouldntRun("business", "An app-state check must be a saved project check file.",
                             "Save the check under a project with .mobster/fixtures.json, then run again.")
        try:
            collision = _seeds_reserved_file(self.check.seed)
        except OSError:
            raise CouldntRun("business", "A YAML fixture's seed files could not be inspected before reset.",
                             "Fix the YAML fixture, then run the check again.") from None
        if collision:
            raise CouldntRun("business", "A YAML fixture seeds the reserved app-state file.",
                             "Remove that file from the YAML fixture before running this check.")
        try:
            current = load_business_binding(self.check, project_root(self.check.source))
        except CheckError as error:
            raise CouldntRun("business", str(error), "Fix .mobster/fixtures.json, then run again.") from None
        if self.business_binding is not None and current.digest != self.business_binding.digest:
            raise CouldntRun("business", "The enrolled app-state fixture changed before the run.",
                             "Re-enroll the project fixture, then run again.")
        self.business_binding = current
        self.business_result = {"adapter": current.spec.adapter, "fixture": current.spec.fixture,
                                "status": "unavailable", "schemaVersion": "mobster.business/1",
                                "bindingSha256": current.digest, "assertions": [],
                                "setup": "not_started", "cleanup": "not_needed"}

    def _setup_business(self, manager):
        if self.business_binding is None:
            return
        from .business_state import AppContainerFixture, FixtureError
        self.business_fixture = AppContainerFixture(self.business_binding, manager, self.target, self.actions,
                                                    self.run_id, secrets.token_hex(16))
        with self.actions.action("BUSINESS_QUIESCE", operation_id=f"{self.run_id}:business:preseed"):
            self.business_fixture.quiesce()
        self.business_result["setup"] = "pending"
        try:
            self.business_fixture.setup()
        except (FixtureError, OSError) as error:
            self.business_result["setup"] = "needs_reconciliation" if self.actions.blocked else "failed"
            raise CouldntRun("business", f"The app-state fixture could not be seeded ({type(error).__name__}).",
                             "Inspect the fixture file and simulator, then start a new run.") from None
        self.business_result["setup"] = "seeded"

    def _take_business_baseline(self):
        if self.business_fixture is None:
            return
        from .business_state import FixtureError
        try:
            state, generation, digest = self.business_fixture.observe()
        except (FixtureError, OSError) as error:
            raise CouldntRun("business_evidence", f"The app-state baseline is unavailable ({type(error).__name__}).",
                             "Check that the app publishes its persisted state after launch.") from None
        self._business_baseline = (generation, digest)
        self.baseline["businessHeld"] = [item["ok"] for item in evaluate_business(self.business_binding, state)]

    def _observe_business(self, decided):
        if self.business_fixture is None:
            return None
        from .business_state import FixtureError
        try:
            state, generation, digest = self.business_fixture.observe()
        except (FixtureError, OSError) as error:
            self.business_result["error"] = "App-produced state was missing, stale or malformed"
            return {**decided, "verdict": "couldnt_run", "klass": "business_evidence",
                    "message": f"The app-state observation is unavailable ({type(error).__name__}).",
                    "fix": "Check the app's fixture producer and run a new check."}
        if self._business_baseline is not None:
            before_generation, before_digest = self._business_baseline
            if generation < before_generation or generation == before_generation and digest != before_digest:
                self.business_result["error"] = "The app-state generation moved backward or changed in place"
                return {**decided, "verdict": "couldnt_run", "klass": "business_evidence",
                        "message": "The app-state observation has a stale generation.",
                        "fix": "Make the app increment generation for every persisted snapshot."}
        self.business_result.update(status="observed", generation=generation, sourceSha256=digest,
                                    assertions=evaluate_business(self.business_binding, state))
        return None

    def _cleanup_business(self):
        if self.business_fixture is None or self._business_cleanup_done:
            return None
        self._business_cleanup_done = True
        if self.actions.blocked or self.error is not None and self.error["klass"] == "unknown_outcome":
            self.business_result["cleanup"] = "needs_reconciliation"
            return None
        try:
            status = self.business_fixture.remove()
        except BaseException as error:
            self.business_result["cleanup"] = "needs_reconciliation"
            failure = self._action_failure()
            return failure or CouldntRun("business_cleanup",
                                         f"The app-state fixture was not confirmed removed ({type(error).__name__}).",
                                         "Inspect the simulator before another run.")
        self.business_result["cleanup"] = status
        if status == "needs_reconciliation":
            return CouldntRun("business_cleanup", "The app-state fixture was not confirmed removed.",
                             "Inspect the simulator before another run.")
        return None

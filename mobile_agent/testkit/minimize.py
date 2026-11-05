"""Find a smaller local reproducer by running bounded, isolated check variants.

Only steps change. The saved failure, app bytes, registered fixture, assertions and
resolved simulator stay fixed. A draft is never an approved check or a proof of
external backend isolation.
"""

import json
import os
import re
import stat
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path

from ..verify.checks import CheckError, dump_check
from . import capsule, comparison
from .api import Options, UsageError, execute, plan
from .discover import Entry
from .provenance import app_digest, case_provenance, require_case_provenance

SCHEMA = "mobster.test-minimize/1"
MAX_ATTEMPTS = 8
MAX_SECONDS = 1800
MAX_USD = 2
REF = re.compile(r"[0-9a-f]{12}\Z")
MAX_DRAFT_BYTES = 1_000_000


class MinimizationError(ValueError):
    """The saved failure cannot safely enter a new reduction experiment."""


class _Draft:
    def __init__(self, root, folder, root_chain, descriptors):
        self.root = root
        self.folder = folder
        self.root_chain = root_chain
        self.descriptors = descriptors
        self.fd = descriptors[-1]

    def attached(self):
        try:
            paths = (*self.root_chain, (".mobster", self.descriptors[0], self.descriptors[1]),
                     ("drafts", self.descriptors[1], self.descriptors[2]),
                     ("repro", self.descriptors[2], self.descriptors[3]),
                     (self.folder.name, self.descriptors[3], self.descriptors[4]))
            for name, parent, held_fd in paths:
                found = os.stat(name, dir_fd=parent, follow_symlinks=False)
                held = os.fstat(held_fd)
                if not stat.S_ISDIR(found.st_mode) or (found.st_dev, found.st_ino) != (held.st_dev, held.st_ino):
                    return False
        except OSError:
            return False
        return True

    def _bytes(self, data):
        if len(data) > MAX_DRAFT_BYTES or not self.attached():
            raise MinimizationError("The draft directory changed or its output is too large. Review this attempt.")
        return data

    def write_new(self, name, content):
        try:
            capsule._write_at(self.fd, name, self._bytes(content.encode("utf-8")))
            os.fsync(self.fd)
        except (OSError, capsule.CapsuleError) as error:
            raise MinimizationError(
                "The draft file already exists or its directory changed. Review this attempt."
            ) from error

    def save(self, value):
        data = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
        try:
            capsule._atomic_bytes(self.fd, "result.json", self._bytes(data))
        except (OSError, capsule.CapsuleError) as error:
            raise MinimizationError("The draft result could not be saved. Review this attempt.") from error


@contextmanager
def _draft_directory(root, name):
    folder = root / ".mobster" / "drafts" / "repro" / name
    with ExitStack() as stack:
        descriptors = []
        root_chain = []

        def opened(path, parent=None, create=False):
            if create:
                try:
                    os.mkdir(path, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, fd)
            return fd

        try:
            root_fd = opened("/")
            root_chain.append(("/", None, root_fd))
            for part in root.parts[1:]:
                child = opened(part, root_fd)
                root_chain.append((part, root_fd, child))
                root_fd = child
            descriptors.append(root_fd)
            mobster_fd = opened(".mobster", root_fd)
            descriptors.append(mobster_fd)
            drafts_fd = opened("drafts", mobster_fd, create=True)
            descriptors.append(drafts_fd)
            repro_fd = opened("repro", drafts_fd, create=True)
            descriptors.append(repro_fd)
            try:
                os.mkdir(name, 0o700, dir_fd=repro_fd)
            except FileExistsError as error:
                raise MinimizationError("This reduction already started. Review its draft and suite results before "
                                        "a new attempt.") from error
            descriptors.append(opened(name, repro_fd))
            for fd in descriptors[:-1]:
                os.fsync(fd)
        except OSError as error:
            raise MinimizationError("The draft output directory must be a physical project directory.") from error
        draft = _Draft(root, folder, root_chain, descriptors)
        if not draft.attached():
            raise MinimizationError("The draft directory changed before the experiment started.")
        yield draft


def _selected(doc, case_ref, target_ref):
    found = [(check, cell) for check in doc.get("checks", ()) if isinstance(check, dict)
             for cell in (check.get("results") if isinstance(check.get("results"), list) else ())
             if isinstance(cell, dict)
             and comparison._ref(check.get("caseId") or check.get("file") or "") == case_ref
             and comparison._ref(cell.get("deviceKey") or "") == target_ref]
    if len(found) != 1:
        raise MinimizationError("Select one exact case and simulator from each saved suite.")
    return found[0]


def _app(root, supplied, expected):
    try:
        path = Path(supplied).expanduser()
        path = (path if path.is_absolute() else root / path).resolve(strict=True)
        path.relative_to(root)
        if path.suffix != ".app" or not path.is_dir() or app_digest(path) != expected:
            raise ValueError("app digest mismatch")
    except (OSError, TypeError, ValueError) as error:
        raise MinimizationError("Keep both exact saved .app builds under this project and pass their paths.") from error
    return path


def _source(results_root, bad_suite_id, control_suite_id, case_ref, target_ref,
            bad_app, control_app, isolated, max_attempts):
    from .. import __version__
    if isolated is not True:
        raise MinimizationError("Pass --isolated only after resetting the external fixture yourself.")
    if type(max_attempts) is not int or not 5 <= max_attempts <= MAX_ATTEMPTS:
        raise MinimizationError("--max-attempts must be between 5 and 8 total suite runs.")
    if not isinstance(case_ref, str) or not REF.fullmatch(case_ref) or \
            not isinstance(target_ref, str) or not REF.fullmatch(target_ref):
        raise MinimizationError("Choose the exact caseRef and targetRef from a comparison report.")
    try:
        bad = comparison._read(results_root, bad_suite_id)
        control = comparison._read(results_root, control_suite_id)
    except comparison.ComparisonError as error:
        raise MinimizationError(str(error)) from error
    paired = comparison.compare(bad, control)
    selected = [row for row in paired["cases"] if row["caseRef"] == case_ref and row["targetRef"] == target_ref]
    if len(selected) != 1 or not selected[0]["verifiedFix"] or \
            selected[0]["proof"]["scope"] != "controlled_app_state":
        raise MinimizationError("This exact bad/control case needs a controlled app-state verified comparison.")
    if any(len(doc["checks"]) != 1 or len(doc["checks"][0]["results"]) != 1
           for doc in (bad, control)):
        raise MinimizationError("Use paired suites with one check on one simulator for this reduction.")
    policy = comparison._policy(bad)
    if (policy is None or policy["replay"] != "fresh" or policy["keyless"] or policy["retries"] != 0 or
            policy["repeat"] != 1 or policy["mobster"] != __version__):
        raise MinimizationError("Use paired single-attempt fresh Smart suites from this Mobster version.")
    check_row, cell = _selected(bad, case_ref, target_ref)
    control_row, control_cell = _selected(control, case_ref, target_ref)
    proof = comparison._business_fields(check_row, cell)
    env = comparison._environment(cell)
    if (cell.get("status") != "failed" or comparison._reason(cell) != "business" or
            not proof or not proof[1] or not env or env["kind"] != "simulator" or
            cell.get("deviceKey") != f"sim:{env['model']}@{env['runtime']}" or
            not comparison._digest(cell.get("appDigest")) or
            cell["runs"][0].get("class") != "business" or cell.get("flaky") is not False or
            cell.get("quarantined") is not False or cell.get("replayed") is not False or
            cell.get("healed") != [] or cell.get("safetyStop") or
            not comparison._supporting_evidence(cell, check_row.get("visualBaselineSha256") is not None) or
            check_row.get("provenanceDrift") or not comparison._digest(check_row.get("checkSpecDigest"))):
        raise MinimizationError("The source needs one clean, registered app-state assertion failure.")
    relative = check_row.get("file")
    results_root = Path(results_root)
    if results_root.name != "test-results" or results_root.parent.name != ".mobster" or results_root.is_symlink():
        raise MinimizationError("Choose this project's physical .mobster/test-results directory.")
    root = results_root.resolve().parent.parent
    checks = root / ".mobster" / "checks"
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise MinimizationError("The saved check path is not a project check.")
    path = root / relative
    options = bad.get("options")
    control_options = control.get("options")
    if (not isinstance(options, dict) or type(options.get("strict")) is not bool or
            not isinstance(control_options, dict) or control_options.get("strict") is not options["strict"]):
        raise MinimizationError("The paired suites need the same frozen action policy.")
    source_options = Options(paths=(str(path),), sims=(f"{env['model']}@{env['runtime']}",),
                             retries=0, fresh=True, strict=options["strict"],
                             assert_timeout=policy["assertTimeout"])
    try:
        if checks.resolve(strict=True) != checks or path.resolve(strict=True) != path:
            raise ValueError("source check is linked")
        path.relative_to(checks)
        source_plan = plan(source_options, cwd=root)
        if source_plan.root != root or len(source_plan.entries) != 1 or len(source_plan.targets) != 1:
            raise ValueError("unexpected source plan")
        source_entry = source_plan.entries[0]
        check = source_entry.check
        if (check is None or source_entry.quarantined is not None or source_entry.file != relative or
                source_entry.path.resolve() != path.resolve()):
            raise ValueError("source refused")
        frozen = case_provenance(source_entry, root)
        if frozen["sourceDigest"] is None:
            raise ValueError("source check unreadable")
    except (OSError, ValueError, CheckError, UsageError) as error:
        raise MinimizationError("The saved check or registered fixture is unavailable.") from error
    identity = ("checkSpecDigest", "baselineDigest", "visualBaselineSha256", "fixtureDigest",
                "businessBindingSha256")
    if any(frozen[field] != check_row.get(field) for field in identity):
        raise MinimizationError("The check, baseline or enrolled fixture changed since the saved failure.")
    if (check.business is None or check.reset_level() not in ("data", "reinstall") or
            not check.steps or
            check.max_seconds * max_attempts > MAX_SECONDS or check.max_usd * max_attempts > MAX_USD):
        raise MinimizationError("Use a resettable app check with steps and a bounded run budget.")
    common = {field: frozen[field] for field in identity}
    if common != {field: check_row[field] for field in common} or \
            common != {field: control_row[field] for field in common}:
        raise MinimizationError("The check, baseline or enrolled fixture changed since the paired suites.")
    bad_path = _app(root, bad_app, cell["appDigest"])
    control_path = _app(root, control_app, control_cell["appDigest"])
    if bad_path == control_path:
        raise MinimizationError("The bad and control builds must be distinct preserved app artifacts.")
    if any(replace(check, app_path=str(path)).reset_level() != check.reset_level()
           for path in (bad_path, control_path)):
        raise MinimizationError("Adding a saved app build would change the check's reset policy.")
    return root, check, frozen, env, proof, policy, options, (bad_path, cell["appDigest"]), \
        (control_path, control_cell["appDigest"])


def _outcome(doc, expected, expected_policy, env, fields, failed, arm):
    """True: expected bad failure or clean control pass; False: known mismatch; None: uncertain effect."""
    if (not isinstance(doc, dict) or doc.get("schema") != "mobster.test/1" or
            not isinstance(doc.get("suite"), str) or not capsule.SUITE_ID.fullmatch(doc["suite"]) or
            type(doc.get("finishedAt")) is not int or doc.get("stopReason") or doc.get("stopped") or
            type(doc.get("exitCode")) is not int or doc["exitCode"] not in (0, 1) or
            comparison._policy(doc) != expected_policy or
            not isinstance(doc.get("checks"), list) or len(doc["checks"]) != 1):
        return None
    row = doc["checks"][0]
    cells = row.get("results") if isinstance(row, dict) else None
    if (not isinstance(cells, list) or len(cells) != 1 or
            any(row.get(field) != expected[field] for field in
                ("checkSpecDigest", "fixtureDigest", "businessBindingSha256", "baselineDigest",
                 "visualBaselineSha256")) or row.get("provenanceDrift")):
        return None
    cell = cells[0]
    if (not isinstance(cell, dict) or comparison._environment(cell) != env or
            cell.get("deviceKey") != f"sim:{env['model']}@{env['runtime']}" or
            cell.get("appDigest") != expected["appDigest"] or cell.get("status") not in ("failed", "passed") or
            cell.get("flaky") is not False or cell.get("quarantined") is not False or
            cell.get("replayed") is not False or cell.get("healed") != [] or cell.get("safetyStop")):
        return None
    if doc["exitCode"] != (1 if cell["status"] == "failed" else 0):
        return None
    if not comparison._supporting_evidence(cell, expected["visualBaselineSha256"] is not None):
        return None
    proof = comparison._business_fields(row, cell)
    if not proof or proof[0] != fields:
        return None
    if cell["status"] == "failed":
        if comparison._reason(cell) != "business" or cell["runs"][0].get("class") != "business":
            return False
        return arm == "bad" and set(proof[1]) == set(failed)
    if cell["runs"][0].get("class") is not None or cell.get("reason") is not None or proof[1]:
        return None
    return arm == "control"


def _canonical_evaluate(root, path, options, key, stop, frozen):
    planned = plan(options, cwd=root)
    if (planned.root != root or len(planned.entries) != 1 or
            planned.entries[0].path.resolve() != path.resolve() or
            planned.entries[0].quarantined is not None or
            case_provenance(planned.entries[0], root) != frozen):
        raise MinimizationError("The trial check changed before it was admitted. Review the draft before retrying.")
    planned.entries[0].check = replace(planned.entries[0].check, draft_trial=True)
    doc, _ = execute(planned, cwd=root, key=key, stop=stop,
                     before_attempt=lambda entry, check, record: require_case_provenance(entry, root, frozen))
    return doc


def minimize(results_root, bad_suite_id, control_suite_id, *, case_ref, target_ref, bad_app, control_app,
             isolated=False, max_attempts=8, key=None, evaluator=None, stop=None):
    """Run a finite experiment and save a separate draft; never retry an interrupted experiment."""
    admission = _source(
        results_root, bad_suite_id, control_suite_id, case_ref, target_ref,
        bad_app, control_app, isolated, max_attempts)
    name = f"{bad_suite_id}-{control_suite_id}-{case_ref}-{target_ref}"
    with _draft_directory(admission[0], name) as draft:
        return _run(admission, draft, bad_suite_id, control_suite_id, case_ref, target_ref,
                    max_attempts, key, evaluator, stop)


def _run(admission, draft, bad_suite_id, control_suite_id, case_ref, target_ref,
         max_attempts, key, evaluator, stop):
    root, check, frozen, env, proof, policy, source_options, bad_build, control_build = admission
    folder = draft.folder
    result = {"schema": SCHEMA, "badSuite": bad_suite_id, "controlSuite": control_suite_id,
              "caseRef": case_ref, "targetRef": target_ref,
              "status": "running", "originalSteps": len(check.steps), "candidateSteps": None,
              "attemptsUsed": 0, "candidateCheck": None, "reason": None, "trials": []}
    def save():
        draft.save(result)

    save()  # Durable intent precedes the first action. An interrupted run is not retried automatically.
    deadline = time.monotonic() + MAX_SECONDS
    stop = stop or threading.Event()
    options = Options(sims=(f"{env['model']}@{env['runtime']}",), retries=0, repeat=1,
                      fresh=True, keyless=False, strict=source_options["strict"],
                      assert_timeout=policy["assertTimeout"])

    def trial(steps, arm):
        if stop.is_set():
            result.update(status="stopped", reason="Stopped before the next trial started.")
            save()
            return None
        if result["attemptsUsed"] >= max_attempts or time.monotonic() + check.max_seconds > deadline:
            result.update(status="budget_exhausted", reason="The configured trial budget ended before dispatch.")
            save()
            return None
        source_entry = Entry(Path(check.source), Path(check.source).relative_to(root).as_posix(), check)
        current = case_provenance(source_entry, root)
        if any(current[field] != frozen[field] for field in frozen):
            result.update(status="stopped_unknown", reason="The source check or admitted assets changed.")
            save()
            return None
        app_path, expected_app_digest = bad_build if arm == "bad" else control_build
        try:
            if app_digest(app_path) != expected_app_digest:
                raise ValueError("build changed")
        except (OSError, ValueError):
            result.update(status="stopped_unknown", reason="A preserved app build changed.")
            save()
            return None
        number = result["attemptsUsed"] + 1
        path = folder / f"trial-{number}.yaml"
        trial_file = path.relative_to(root).as_posix()
        intended = replace(check, steps=tuple(steps), app_path=str(app_path), source=str(path),
                           a11y_scope=trial_file)
        draft.write_new(path.name, dump_check(intended, project_root=root))
        trial_frozen = case_provenance(Entry(path, trial_file, intended), root)
        if (not comparison._digest(trial_frozen["checkSpecDigest"]) or
                not comparison._digest(trial_frozen["sourceDigest"]) or
                any(trial_frozen[field] != frozen[field] for field in
                    ("baselineDigest", "visualBaselineSha256", "fixtureDigest", "businessBindingSha256")) or
                trial_frozen["appDigest"] != expected_app_digest):
            result.update(status="stopped_unknown", reason="The draft's admitted assets changed.")
            save()
            return None
        options.paths = (str(path),)
        result["attemptsUsed"] = number
        result["trials"].append({"suite": None, "arm": arm, "steps": len(steps), "outcome": "unknown"})
        save()
        if not draft.attached():
            raise MinimizationError("The draft directory changed before trial admission. Review this attempt.")
        doc = evaluator(path, options) if evaluator else _canonical_evaluate(
            root, path, options, key, stop, trial_frozen)
        outcome = _outcome(doc, trial_frozen, policy, env, proof[0], proof[1], arm)
        snapshot = doc["checks"][0]["results"][0]["business"]["sourceSha256"] if outcome is not None else None
        result["trials"][-1].update({"suite": doc.get("suite") if isinstance(doc, dict) else None,
                                      "snapshotSha256": snapshot,
                                      "outcome": (("same_failure" if arm == "bad" else "clean_pass")
                                                  if outcome is True else
                                                  "different" if outcome is False else "unknown")})
        save()
        return outcome

    try:
        bad_original = trial(check.steps, "bad")
        if bad_original is None:
            if result["status"] == "running":
                result.update(status="stopped_unknown", reason="The original bad run has an unknown outcome.")
        elif bad_original is False:
            result.update(status="original_not_reproduced", reason="The saved bad failure did not recur.")
        else:
            control_original = trial(check.steps, "control")
            if control_original is None:
                if result["status"] == "running":
                    result.update(status="stopped_unknown", reason="The original control run has an unknown outcome.")
            elif control_original is False:
                result.update(status="control_not_reproduced", reason="The saved clean control did not pass.")
            elif result["trials"][-2]["snapshotSha256"] == result["trials"][-1]["snapshotSha256"]:
                result.update(status="stopped_unknown", reason="Bad and control reported identical app-state bytes.")
            else:
                steps = list(check.steps)
                index = 0
                while index < len(steps) and result["attemptsUsed"] + 3 <= max_attempts:
                    smaller = steps[:index] + steps[index + 1:]
                    bad_first = trial(smaller, "bad")
                    if bad_first is None:
                        if result["status"] == "running":
                            result.update(status="stopped_unknown", reason="A bad trial has an unknown outcome.")
                        break
                    if bad_first is False:
                        index += 1
                        continue
                    clean = trial(smaller, "control")
                    if clean is None:
                        if result["status"] == "running":
                            result.update(status="stopped_unknown", reason="A control trial has an unknown outcome.")
                        break
                    if clean is False:
                        index += 1
                        continue
                    bad_second = trial(smaller, "bad")
                    if bad_second is None:
                        if result["status"] == "running":
                            result.update(status="stopped_unknown", reason="A confirming bad trial is unknown.")
                        break
                    if result["trials"][-2]["snapshotSha256"] in (
                            result["trials"][-3]["snapshotSha256"], result["trials"][-1]["snapshotSha256"]):
                        result.update(
                            status="stopped_unknown",
                            reason="Bad and control reported identical app-state bytes.",
                        )
                        break
                    if bad_second:
                        steps = smaller
                    else:
                        index += 1
            if result["status"] in ("running", "budget_exhausted"):
                if control_original is True and len(steps) < len(check.steps):
                    source_entry = Entry(Path(check.source), Path(check.source).relative_to(root).as_posix(), check)
                    current = case_provenance(source_entry, root)
                    if any(current[field] != frozen[field] for field in frozen):
                        result.update(status="stopped_unknown", reason="The source check or admitted assets changed.")
                        save()
                        return result
                    try:
                        builds_stable = all(app_digest(path) == digest
                                            for path, digest in (bad_build, control_build))
                    except (OSError, ValueError):
                        builds_stable = False
                    if not builds_stable:
                        result.update(status="stopped_unknown", reason="A preserved app build changed.")
                        save()
                        return result
                    candidate = folder / "candidate.yaml"
                    draft.write_new(candidate.name, dump_check(replace(check, steps=tuple(steps),
                                                                  app_path=str(bad_build[0])), project_root=root))
                    result.update(status="smaller_reproducer", candidateSteps=len(steps),
                                  candidateCheck=candidate.relative_to(root).as_posix(),
                                  reason=("The configured budget ended after this confirmed reduction."
                                          if result["status"] == "budget_exhausted" else None))
                elif control_original is True and result["status"] == "running":
                    result.update(status="no_smaller_reproducer",
                                  reason="No tested deletion kept the bad failure and clean control within the budget.")
    except BaseException:
        # The result remains 'running' with the in-flight trial marked unknown for manual reconciliation.
        raise
    save()
    return result

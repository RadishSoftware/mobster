"""Ordered local simulator checks with an exclusive lease for every actor across all phases."""

import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from ..devkit import links
from ..verify.checks import project_root
from ..verify.fixtures import FixtureError, with_fixtures
from . import a11y_baseline
from .api import UsageError, prepare_entries
from .discover import DiscoveryError, discover
from .input_files import inside, read_bytes
from .provenance import case_provenance, require_case_provenance
from .suite import machine_parallel_cap
from .targets import Refused, TargetError, resolve, validate_record

MAX_MANIFEST_BYTES = 64_000
MAX_ACTORS = 4
MAX_PHASES = 12
_ROLE = re.compile(r"[a-z][a-z0-9_-]{0,39}\Z")


def _manifest_bytes(path):
    content = read_bytes(path, MAX_MANIFEST_BYTES)
    return content, hashlib.sha256(content).hexdigest()


def _manifest_digest(path):
    return _manifest_bytes(path)[1]


@dataclass(frozen=True)
class Member:
    actor: str
    entry: object
    target: object
    provenance: dict


@dataclass(frozen=True)
class Phase:
    name: str
    members: tuple[Member, ...]


@dataclass(frozen=True)
class ScenarioPlan:
    root: Path
    path: Path
    name: str
    actors: dict
    phases: tuple[Phase, ...]
    manifest_digest: str


def plan_scenario(path, *, cwd=None, named_device=None):
    """Admit one bounded scenario of saved checks and distinct named Mobster simulators."""
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            pairs = self.construct_pairs(node, deep=True)
            result = {}
            for key, value in pairs:
                if not isinstance(key, str) or key in result:
                    raise UsageError("Scenario keys must be unique text.")
                result[key] = value
            return result

    cwd = Path(cwd or Path.cwd())
    path = (cwd / path).absolute() if not Path(path).is_absolute() else Path(path).absolute()
    root = project_root(path)
    try:
        path = inside(root, path, prefix=(".mobster", "scenarios"))
        if path.suffix.lower() not in (".yaml", ".yml") or not path.is_file() \
                or path.stat().st_size > MAX_MANIFEST_BYTES:
            raise UsageError("Save a scenario YAML file under .mobster/scenarios/ (at most 64 KB).")
        content, manifest_digest = _manifest_bytes(path)
        data = yaml.load(content.decode("utf-8"), Loader=UniqueLoader)
        if not isinstance(data, dict) or set(data) != {"version", "name", "actors", "phases"} \
                or type(data["version"]) is not int or data["version"] != 1:
            raise UsageError("A scenario needs version: 1, name, actors and phases.")
        name = data["name"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise UsageError("A scenario name must be 1–80 characters.")
        declared = data["actors"]
        if not isinstance(declared, dict) or not 1 <= len(declared) <= MAX_ACTORS:
            raise UsageError(f"A scenario needs 1–{MAX_ACTORS} named simulator actors.")
        actors = {}
        udids = set()
        for role, sim_id in declared.items():
            if not isinstance(role, str) or not _ROLE.fullmatch(role) or not isinstance(sim_id, str) \
                    or not 1 <= len(sim_id) <= 128:
                raise UsageError("Each actor needs a lowercase role and an exact named simulator ID.")
            target = resolve(devices=(sim_id,), root=root, named_device=named_device)[0]
            record = target.record
            if record is None or record.get("kind") != "simulator" or record.get("id") != sim_id:
                raise UsageError(f"Actor {role!r} must name the exact ID of a managed simulator.")
            validate_record(record)
            if record["udid"].casefold() in udids:
                raise UsageError("Two scenario actors resolve to the same simulator. Give each a distinct one.")
            udids.add(record["udid"].casefold())
            actors[role] = replace(target, record=dict(record))
        cap = machine_parallel_cap()
        if len(actors) > cap:
            raise UsageError(f"This Mac runs at most {cap} simulators at once; the scenario names {len(actors)}. "
                             "Use fewer actors or run the scenario on a Mac with enough free capacity.")
        declared_phases = data["phases"]
        if not isinstance(declared_phases, list) or not 1 <= len(declared_phases) <= MAX_PHASES:
            raise UsageError(f"A scenario needs 1–{MAX_PHASES} ordered phases.")
        baseline = a11y_baseline.load(root)
        phases = []
        seen_actors = set()
        for index, item in enumerate(declared_phases, 1):
            if not isinstance(item, dict) or set(item) != {"name", "runs"} or \
                    not isinstance(item["name"], str) or not 1 <= len(item["name"].strip()) <= 80 or \
                    not isinstance(item["runs"], list) or not 1 <= len(item["runs"]) <= len(actors):
                raise UsageError(f"Phase {index} needs a name and 1–{len(actors)} runs.")
            members = []
            phase_actors = set()
            for run in item["runs"]:
                if not isinstance(run, dict) or set(run) != {"actor", "check"} or \
                        not isinstance(run["actor"], str) or run["actor"] not in actors or \
                        run["actor"] in phase_actors or \
                        not isinstance(run["check"], str):
                    raise UsageError(f"Phase {index} needs one saved check per known actor.")
                actor = run["actor"]
                phase_actors.add(actor)
                check_path = inside(root, run["check"], prefix=(".mobster", "checks"))
                if not check_path.is_file() or check_path.suffix.lower() not in (".yaml", ".yml", ".json"):
                    raise UsageError(f"Phase {index} must name one saved check file under .mobster/checks/.")
                entries = discover((str(check_path),), root, cwd=root)
                if len(entries) != 1:
                    raise UsageError(f"Phase {index} must name exactly one saved check.")
                prepare_entries(entries, root, baseline=baseline)
                entry = entries[0]
                if entry.check is None:
                    raise UsageError(f"{entry.file} cannot run: {entry.error}")
                effective = with_fixtures(entry.check, root)
                if actor in seen_actors and entry.check.reset != "none":
                    raise UsageError(f"{entry.file} follows an earlier {actor} phase; set reset: none explicitly.")
                if entry.check.business is not None and actor in seen_actors:
                    raise UsageError(f"{entry.file} uses an app-state fixture after an earlier {actor} phase. "
                                     "Put that fixture in the actor's first check; continuation cannot reseed it.")
                if actor in seen_actors and (effective.seed or effective.conditions):
                    raise UsageError(f"{entry.file} would seed files, keychain, pasteboard or conditions after "
                                     f"{actor} began. Remove destructive setup from continuation checks.")
                if entry.check.business is not None and entry.check.reset_level() == "none":
                    raise UsageError(f"{entry.file} needs a data or reinstall reset before its app-state fixture.")
                frozen = case_provenance(entry, root)
                if frozen["sourceDigest"] is None or frozen["checkSpecDigest"] is None or \
                        (entry.check.app_path and frozen["appDigest"] is None) \
                        or frozen["baselineDigest"] is None or frozen["fixtureDigest"] is None:
                    raise UsageError(f"{entry.file} or its declared app, baseline or fixture is unreadable. Fix it "
                                     "before running the scenario.")
                members.append(Member(actor, entry, actors[actor], frozen))
            seen_actors.update(phase_actors)
            phases.append(Phase(item["name"].strip(), tuple(members)))
        return ScenarioPlan(root, path, name.strip(), actors, tuple(phases), manifest_digest)
    except UsageError:
        raise
    except (OSError, ValueError, DiscoveryError, TargetError, Refused, FixtureError,
            a11y_baseline.BaselineError, links.LinksError,
            yaml.YAMLError) as error:
        raise UsageError(f"The scenario cannot be admitted: {error}") from None


def _row(member, phase, number, result):
    """Give a saved check separate phase evidence each time it appears."""
    entry = member.entry
    row = dict(result)
    row["name"] = f"{phase.name} · {member.actor} · {entry.name}"
    row["classname"] = f"scenario.{number}.{member.actor}"
    row["scenario"] = {"phase": number, "phaseName": phase.name, "actor": member.actor}
    row["checkSpecDigest"] = member.provenance["checkSpecDigest"]
    row["baselineDigest"] = member.provenance["baselineDigest"]
    row["fixtureDigest"] = member.provenance["fixtureDigest"]
    row["visualBaselineSha256"] = member.provenance["visualBaselineSha256"]
    row["businessBindingSha256"] = member.provenance["businessBindingSha256"]
    row["quarantined"] = False  # a phase prerequisite cannot be waived by quarantine
    for cell in row["results"]:
        cell["appDigest"] = member.provenance["appDigest"]
    return row


def _blocked(member, phase, number, klass, message):
    target = member.target
    row = {"name": member.entry.name, "file": member.entry.file, "results": [{
        "device": target.label, "deviceKey": target.key, "status": "couldnt_run", "attempts": 0,
        "durationMs": 0, "flaky": False, "reason": {"class": klass, "message": message},
        "quarantined": False, "runs": [], "environment": {"kind": "simulator", "model": None,
                                                        "runtime": None}}]}
    row = _row(member, phase, number, row)
    row["blocked"] = True
    return row


def execute_scenario(planned, *, progress=None, stop=None, key=None, attempts=None, suite_id=None,
                     on_suite=None):
    """Reserve every actor, run ordered phase barriers, and write one terminal suite artifact."""
    from .. import __version__
    from ..verify.actions import ActionJournal
    from ..verify.runs import ensure_gitignore
    from .execute import Attempts
    from .reports import document, make_suite_dir, write_all
    from .suite import SAFETY_EXIT_STOPS, STOP_RANK, Suite

    progress = progress or (lambda line: None)
    stop = stop or getattr(attempts, "stop", None) or threading.Event()
    root = planned.root
    ensure_gitignore(root / ".mobster" / "runs")
    suite_id, folder = make_suite_dir(root / ".mobster" / "test-results", suite_id)
    attempts = attempts or Attempts(runs_dir=root / ".mobster" / "runs", key=key,
                                    replay="strict", parallel=len(planned.actors), progress=progress, stop=stop)
    if not isinstance(attempts, Attempts) or attempts.replay != "strict" or attempts.keyless \
            or attempts.stop is not stop:
        raise UsageError("Scenario attempts must own leased simulator reservations.")
    if len(planned.actors) > machine_parallel_cap():
        raise UsageError("This Mac can no longer reserve every scenario actor. Use fewer actors.")
    journal_dir = folder / "reservation"
    journal_dir.mkdir(mode=0o700)
    journal = ActionJournal(journal_dir)
    rows = []
    devices = {target.key: {**target.public(), "key": target.key, "name": target.label}
               for target in planned.actors.values()}
    started = time.time()
    stop_reason = None
    block = None
    phase_started = False

    def mark_stop(candidate):
        nonlocal stop_reason
        if candidate in STOP_RANK and (stop_reason is None or STOP_RANK[candidate] > STOP_RANK[stop_reason]):
            stop_reason = candidate

    def current_entries(members):
        paths = list(dict.fromkeys(member.entry.path for member in members))
        entries = discover(tuple(map(str, paths)), root, cwd=root)
        prepare_entries(entries, root, baseline=a11y_baseline.load(root))
        return {entry.path: entry for entry in entries}

    def check_member(member, entries, asset_cache=None):
        current = entries.get(member.entry.path)
        if current is None or current.check != member.entry.check:
            from .targets import Refused
            raise Refused("A saved check's links or accessibility baseline changed. Start a new scenario.",
                          "refused")
        require_case_provenance(current, root, member.provenance, asset_cache=asset_cache)

    def validate(phase=None):
        if _manifest_digest(planned.path) != planned.manifest_digest:
            raise UsageError("The scenario file changed during the run. Start a new scenario.")
        asset_cache = {}
        members = [member for current in ((phase,) if phase is not None else planned.phases)
                   for member in current.members]
        entries = current_entries(members)
        for member in members:
            check_member(member, entries, asset_cache)

    def run_member(member):
        def check_entry(entry, check, record):
            if _manifest_digest(planned.path) != planned.manifest_digest:
                from .targets import Refused
                raise Refused("The scenario file changed during admission. Start a new scenario.", "refused")
            check_member(member, current_entries((member,)))
        suite = Suite([member.entry], [member.target], attempt=attempts, retries=0, repeat=1,
                      parallel=1, stop=stop, on_cancel=attempts.abort_all,
                      before_attempt=check_entry, progress=progress)
        if on_suite is not None:
            on_suite(suite)
        suite.run()
        suite.seal()
        return suite.results()

    try:
        validate()
        records = [target.record for target in planned.actors.values()]
        with attempts.reserve_simulators(records, journal):
            for number, phase in enumerate(planned.phases, 1):
                if stop.is_set():
                    if stop_reason is None:
                        stop_reason = "stopped"
                    block = ("stopped", "The scenario was stopped before this phase.")
                if block is None:
                    validate(phase)
                    phase_started = True
                    pool = ThreadPoolExecutor(max_workers=len(phase.members))
                    futures, outcomes = [], []
                    interrupted = False
                    try:
                        for member in phase.members:
                            futures.append(pool.submit(run_member, member))
                    except (KeyboardInterrupt, Exception) as error:
                        # submit may have queued a member before returning its Future. No later phase can assume
                        # that untracked member was effect-free.
                        stop.set()
                        attempts.abort_all()
                        interrupted = isinstance(error, KeyboardInterrupt)
                        outcomes.extend([error] * (len(phase.members) - len(futures)))
                    try:
                        completed = []
                        for future in futures:
                            while True:
                                try:
                                    completed.append(future.result())
                                    break
                                except KeyboardInterrupt as error:
                                    stop.set()
                                    attempts.abort_all()
                                    interrupted = True
                                    try:
                                        child_error = future.exception() if future.done() else None
                                    except BaseException:
                                        child_error = None
                                    if isinstance(child_error, KeyboardInterrupt):
                                        completed.append(error)
                                        break
                                except Exception as error:
                                    stop.set()
                                    attempts.abort_all()
                                    completed.append(error)
                                    break
                        outcomes = completed + outcomes
                    finally:
                        # ThreadPoolExecutor.__exit__ can itself be interrupted. Keep all root actor leases until
                        # every member thread has closed, even after repeated ctrl+c.
                        while True:
                            try:
                                pool.shutdown(wait=True, cancel_futures=stop.is_set())
                                break
                            except KeyboardInterrupt:
                                stop.set()
                                attempts.abort_all()
                                interrupted = True
                    phase_started = False
                    for member, outcome in zip(phase.members, outcomes):
                        if isinstance(outcome, BaseException):
                            klass = getattr(outcome, "klass", None)
                            if klass not in SAFETY_EXIT_STOPS:
                                klass = "unknown_outcome"
                            mark_stop(klass)
                            rows.append(_blocked(member, phase, number, klass, str(outcome)[:300]))
                            continue
                        row = _row(member, phase, number, outcome["checks"][0])
                        rows.append(row)
                        for device in outcome["devices"]:
                            devices[device["key"]] = device
                        mark_stop(outcome.get("stopReason"))
                    if interrupted or stop.is_set():
                        mark_stop("stopped")
                    if any(row["results"][0]["status"] != "passed" for row in rows[-len(phase.members):]):
                        block = ("prerequisite", f"Phase {number} did not pass; later phases cannot run.")
                    if stop_reason in STOP_RANK:
                        block = (stop_reason, f"The scenario stopped after phase {number}.")
                else:
                    rows.extend(_blocked(member, phase, number, *block) for member in phase.members)
    except KeyboardInterrupt:
        stop.set()
        attempts.abort_all()
        if journal.klass in SAFETY_EXIT_STOPS:
            stop_reason = journal.klass
            block = (journal.klass, "Simulator acquisition may have taken effect before interruption.")
        elif stop_reason is None:
            stop_reason = "stopped"
            block = ("stopped", "The scenario was stopped with ctrl+c.")
    except Exception as error:
        klass = journal.klass or getattr(error, "klass", None) or getattr(error, "kind", None)
        if klass not in ("journal", "unknown_outcome", "stopped", "busy"):
            klass = "unknown_outcome" if phase_started or journal.blocked else "refused"
        stop_reason = klass if klass in SAFETY_EXIT_STOPS or klass == "stopped" else "refused"
        block = (klass, str(error)[:300])
    finally:
        journal.request_stop()
    for number, phase in enumerate(planned.phases, 1):
        recorded = {row["scenario"]["actor"] for row in rows if row["scenario"]["phase"] == number}
        rows.extend(_blocked(member, phase, number, *(block or ("prerequisite", "An earlier phase did not pass.")))
                    for member in phase.members if member.actor not in recorded)
    try:
        manifest_drift = _manifest_digest(planned.path) != planned.manifest_digest
    except (OSError, ValueError):
        manifest_drift = True
    if manifest_drift and stop_reason not in ("unknown_outcome", "journal"):
        stop_reason = "refused"
    members = {(number, member.actor): member for number, phase in enumerate(planned.phases, 1)
               for member in phase.members}
    final_cache = {}
    try:
        latest = current_entries(tuple(members.values()))
    except (OSError, ValueError, DiscoveryError, a11y_baseline.BaselineError, links.LinksError):
        latest = {}
    provenance_drift = manifest_drift
    for row in rows:
        source = members[row["scenario"]["phase"], row["scenario"]["actor"]]
        fresh = latest.get(source.entry.path)
        if manifest_drift or fresh is None or fresh.check != source.entry.check or \
                case_provenance(fresh, root, asset_cache=final_cache) != source.provenance:
            provenance_drift = True
            row["provenanceDrift"] = True
            row.update({name: None for name in ("checkSpecDigest", "baselineDigest", "fixtureDigest",
                                                "visualBaselineSha256", "businessBindingSha256")})
            row["results"][0]["appDigest"] = None
    if provenance_drift and stop_reason not in ("unknown_outcome", "journal"):
        stop_reason = "refused"
    cells = [row["results"][0] for row in rows]
    summary = {"passed": sum(cell["status"] == "passed" for cell in cells),
               "failed": sum(cell["status"] == "failed" for cell in cells),
               "needsReview": sum(cell["status"] == "needs_review" for cell in cells),
               "couldntRun": sum(cell["status"] == "couldnt_run" for cell in cells),
               "flaky": 0, "quarantined": 0, "total": len(cells),
               "costUsd": round(sum(cell.get("costUsd") or 0 for cell in cells), 5)}
    exit_code = 130 if stop_reason == "stopped" else \
        3 if stop_reason in SAFETY_EXIT_STOPS else 1 if summary["failed"] else \
        3 if summary["couldntRun"] else 2 if summary["needsReview"] else 0
    now = time.time()
    results = {"checks": rows, "devices": list(devices.values()), "summary": summary,
               "stopReason": stop_reason, "exitCode": exit_code,
               "startedAt": round(started * 1000), "finishedAt": round(now * 1000),
               "durationMs": round((now - started) * 1000)}
    doc = document(results, suite_id=suite_id,
                   options={"scenario": True, "retries": 0, "repeat": 1, "parallel": len(planned.actors),
                            "replay": "strict", "strict": True}, version=__version__)
    if stop_reason == "stopped":
        doc["stopped"] = True
    doc["executionPolicy"] = {"retries": 0, "repeat": 1, "replay": "strict", "keyless": False,
                              "assertTimeout": 5, "mobster": __version__}
    doc["scenario"] = {"version": 1, "name": planned.name,
                       "file": planned.path.relative_to(root).as_posix(),
                       "manifestDigest": planned.manifest_digest,
                       "provenanceDrift": provenance_drift,
                       "actors": {role: target.record["id"] for role, target in planned.actors.items()},
                       "phases": [{"name": phase.name, "index": index} for index, phase in
                                  enumerate(planned.phases, 1)]}
    paths = write_all(doc, folder)
    paths["folder"] = folder
    return doc, paths

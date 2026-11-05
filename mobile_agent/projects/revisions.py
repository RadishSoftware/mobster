"""Locally approved, content-pinned project checks for a prebuilt simulator CI run.

The project row owns the folder. A revision owns one exact app, check selection and managed simulator;
the runner retains the parsed plan and rechecks its inputs before every attempt.
"""

import hashlib
import json
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path

from ..replay_core.heal import HealError, effective_digest, proof_assets
from ..testkit import api, provenance, prrun
from ..testkit.input_files import MAX_APP_BYTES, MAX_CACHE_BYTES, InputError, digest, inside
from ..testkit.targets import bundle_of, device_build
from ..verify.checks import FILE_LIMIT
from ..verify.netstubs import BODY_LIMIT

MAX_CHECKS = 100


@dataclass(frozen=True)
class Prepared:
    project_id: str
    revision: int
    manifest: dict
    planned: api.Planned


def _simulator(simulator_id, find_simulator):
    if not isinstance(simulator_id, str) or not simulator_id or len(simulator_id) > 128:
        raise InputError("Choose one managed simulator by its ID")
    try:
        record = find_simulator(simulator_id)
    except (LookupError, OSError, ValueError):
        record = None
    if (not isinstance(record, dict) or record.get("kind") != "simulator" or
            record.get("id") != simulator_id or record.get("udid") != simulator_id):
        raise InputError("That managed simulator is no longer available")
    return record


def _asset_paths(root, check):
    condition = check.conditions or {}
    assets = [*condition.get("media", ())]
    if condition.get("push"):
        assets.append(condition["push"])
    assets.extend(source for source, _ in getattr(check.seed, "files", ()))
    if check.business is not None:
        assets.append(root / ".mobster" / "fixtures.json")
    if check.a11y or "a11y" in check.audit:
        baseline = root / ".mobster" / "a11y-baseline.json"
        if baseline.exists() or baseline.is_symlink():
            assets.append(baseline)
    for asset in assets:
        inside(root, asset)


def _snapshot_baseline(root, check, simulator):
    """The digest of the one baseline a snapshot check's CI run on ``simulator`` compares against (snapshot_run.take
    looks it up by the simulator's device type and the check's conditions). None for a check with no snapshot."""
    if check.snapshot is None:
        return None
    from ..verify import snapshots
    baseline = snapshots.baseline_file(root, snapshots.check_key(check.source, root, check.name), check.snapshot.name,
                                       simulator.get("model") or "iphone", check.conditions)
    if not (baseline.exists() or baseline.is_symlink()):
        raise InputError(f"{check.name} has no baseline for {simulator.get('name') or 'this simulator'} yet. Record "
                         "it on that simulator with mobster test --update-snapshots, then approve again")
    return digest(inside(root, baseline), MAX_CACHE_BYTES)


def _output_roots(root):
    """Canonical report destinations may be absent, but never redirect through links."""
    mobster = root / ".mobster"
    for path in (mobster, mobster / "runs", mobster / "test-results"):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            raise InputError("A project output folder cannot be inspected") from None
        if not stat.S_ISDIR(mode):
            raise InputError("A project output folder must be a real directory")
    for name in (".gitignore", ".gitignore.tmp"):
        path = mobster / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise InputError("A project output file cannot be inspected") from None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InputError("A project output file must be a regular file")


def inspect(root, checks, app, simulator_id, *, find_simulator, policy):
    """Return (manifest without revision, parsed Planned), refusing every unpinned route or input."""
    root = Path(root)
    try:
        if not stat.S_ISDIR(root.lstat().st_mode) or root.resolve(strict=True) != root:
            raise InputError("The saved project folder changed")
    except OSError:
        raise InputError("The saved project folder is unavailable") from None
    _output_roots(root)
    if not isinstance(checks, (list, tuple)) or not 1 <= len(checks) <= MAX_CHECKS or not all(
            isinstance(value, str) for value in checks):
        raise InputError(f"Choose 1 to {MAX_CHECKS} reviewed check files")
    selected = [inside(root, value, prefix=(".mobster", "checks")) for value in checks]
    if len(set(selected)) != len(selected) or any(path.suffix.lower() not in (".yaml", ".yml", ".json")
                                             or not path.is_file() for path in selected):
        raise InputError("Choose distinct reviewed check files")
    if not isinstance(app, str) or not app.endswith(".app"):
        raise InputError("Choose one prebuilt simulator .app inside the project")
    app_path = inside(root, app)
    if app_path.suffix != ".app" or not app_path.is_dir():
        raise InputError("The approved app must be a prebuilt simulator .app")
    app_sha = digest(app_path, MAX_APP_BYTES)
    try:
        simulator_build = device_build(app_path)
        bundle = bundle_of(app_path)
    except (OSError, ValueError):
        raise InputError("The prebuilt app has unreadable metadata") from None
    if simulator_build is not False:
        raise InputError("The approved app must be a prebuilt simulator .app")
    if not bundle:
        raise InputError("The prebuilt app has no bundle ID")
    record = _simulator(simulator_id, find_simulator)
    options = api.Options(paths=tuple(str(path) for path in selected), devices=(record["id"],),
                          retries=0, repeat=1, parallel=1, strict=True, no_heal=True,
                          command="approved project CI suite")
    try:
        planned = api.plan(options, cwd=root, named_device=lambda name: record if name == record["id"] else None)
    except api.UsageError as error:
        raise InputError(str(error)) from None
    if (planned.root != root or len(planned.entries) != len(selected) or
            {entry.path for entry in planned.entries} != set(selected) or
            any(entry.error or entry.check is None for entry in planned.entries) or
            len(planned.targets) != 1 or (planned.targets[0].record or {}).get("udid") != record["udid"]):
        raise InputError("The approved checks or simulator no longer resolve exactly")
    planned.entries = prrun.with_build(planned.entries, {"path": str(app_path), "bundle": bundle}, set())
    rows = []
    for entry in planned.entries:
        check = entry.check
        if check.bundle_id != bundle or check.app_path not in (None, str(app_path)):
            raise InputError("Each approved check must target the prebuilt app's bundle")
        _asset_paths(root, check)
        try:
            assets = proof_assets(check)
        except HealError as error:
            raise InputError(str(error)) from None
        frozen = provenance.case_provenance(entry, root)
        if (frozen["sourceDigest"] is None or frozen["checkSpecDigest"] is None or
                frozen["appDigest"] != app_sha):
            raise InputError("An approved check or prebuilt app cannot be frozen")
        relative = entry.path.relative_to(root).as_posix()
        # A stub's body and a snapshot's baseline are read during the run, so their bytes are pinned, not only paths.
        stubs = [digest(inside(root, stub["file"]), BODY_LIMIT) for stub in check.stubs if stub["file"]]
        baseline = _snapshot_baseline(root, check, record)
        if check.snapshot is not None:
            entry.check = replace(check, snapshot_record=False)   # a CI suite compares; it never records a baseline
        rows.append({"id": hashlib.sha256(relative.encode()).hexdigest()[:12], "path": relative,
                     "raw": digest(entry.path, FILE_LIMIT), "effective": effective_digest(check),
                     "assets": assets, "stubs": stubs, "snapshot": baseline, "canonical": frozen,
                     "quarantined": entry.quarantined})
    quarantine = root / ".mobster" / "quarantine.yaml"
    quarantine_sha = (digest(inside(root, quarantine), FILE_LIMIT)
                      if quarantine.exists() or quarantine.is_symlink() else None)
    replay_cache = root / ".mobster" / "cache" / "replay"
    replay_sha = (digest(inside(root, replay_cache), MAX_CACHE_BYTES)
                  if replay_cache.exists() or replay_cache.is_symlink() else None)
    if not policy(record["id"], (bundle,)):
        raise InputError("The simulator's app rules do not allow this approved app")
    manifest = {"root": str(root), "checks": rows,
                "app": {"path": app_path.relative_to(root).as_posix(), "sha256": app_sha, "bundle": bundle},
                "simulator": {"id": record["id"], "udid": record["udid"],
                               "name": record.get("name") or record["id"]},
                "quarantine": quarantine_sha, "replay": replay_sha}
    return manifest, planned


def prebuilt_apps(root, most=20):
    """The simulator .app folders under the project's .mobster/build, for the Mac app to offer (approval rechecks)."""
    apps, seen = [], 0
    build = root / ".mobster" / "build"
    if not (build.exists() or build.is_symlink()):
        return apps
    try:
        inside(root, build)
        for parent, directories, _ in os.walk(build, followlinks=False):
            seen += len(directories)
            if seen > 5_000:
                break
            for name in list(directories):
                path = Path(parent) / name
                if path.is_symlink():
                    directories.remove(name)
                elif name.endswith(".app"):
                    directories.remove(name)
                    if len(apps) < most:
                        try:
                            inside(root, path / "Info.plist")
                            if device_build(path) is False and (bundle := bundle_of(path)):
                                apps.append({"path": path.relative_to(root).as_posix(), "bundle": bundle})
                        except (InputError, OSError, ValueError):
                            continue
    except (InputError, OSError, ValueError):
        return []
    return apps


def latest(revision, manifest):
    """What the Mac app shows of an approved revision, or None for a manifest it can't read."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("app"), dict) or \
            not isinstance(manifest.get("simulator"), dict) or not isinstance(manifest.get("checks"), list):
        return None
    return {"revision": revision, "appPath": manifest["app"].get("path"),
            "simulatorId": manifest["simulator"].get("id"),
            "checks": [row.get("path") for row in manifest["checks"] if isinstance(row, dict)]}


def fingerprint(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identity(project_id, revision, manifest, planned):
    """The opaque admitted case order that a safe hosted export binds to its canonical document."""
    by_path = {row["path"]: row for row in manifest["checks"]}
    cases = []
    for entry in planned.entries:
        row = by_path[entry.file]
        frozen = row["canonical"]
        cases.append({"id": row["id"], "sourceFile": entry.file,
                      "checkSpecDigest": frozen["checkSpecDigest"], "appDigest": frozen["appDigest"],
                      "baselineDigest": None if frozen["baselineDigest"] == "none" else frozen["baselineDigest"],
                      "fixtureDigest": None if frozen["fixtureDigest"] == "none" else frozen["fixtureDigest"]})
    return {"projectId": project_id, "revision": revision, "provenance": fingerprint(manifest), "cases": cases}


def prepare(project_id, revision, manifest, *, find_simulator, policy):
    current, planned = inspect(manifest["root"], [row["path"] for row in manifest["checks"]],
                               manifest["app"]["path"], manifest["simulator"]["id"],
                               find_simulator=find_simulator, policy=policy)
    if current != manifest:
        raise InputError("An approved check, app, fixture, replay or simulator changed")
    return Prepared(project_id, revision, manifest, planned)


def revalidate(prepared, *, find_simulator, policy):
    prepare(prepared.project_id, prepared.revision, prepared.manifest,
            find_simulator=find_simulator, policy=policy)

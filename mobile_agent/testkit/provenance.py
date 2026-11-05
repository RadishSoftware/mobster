"""Frozen local check inputs for comparing completed suite evidence."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from ..devkit import links
from ..replay_core.heal import HealError, InputChanged, proof_assets
from ..verify.checks import FILE_LIMIT, check_to_dict
from ..verify.fixtures import FixtureError, with_fixtures

FIELDS = ("checkSpecDigest", "baselineDigest", "fixtureDigest", "visualBaselineSha256",
          "businessBindingSha256", "appDigest")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _source_digest(path):
    from .input_files import digest
    return digest(Path(path), FILE_LIMIT)


def _app_digest(path, cache):
    if path is None:
        return None
    path = Path(path)
    if cache is not None and path in cache:
        return cache[path]
    # Project revisions use the same bounded, streaming file/tree digest for immutable prebuilt apps.
    from .input_files import MAX_APP_BYTES, digest
    try:
        value = digest(path, MAX_APP_BYTES)
    except (OSError, ValueError):
        value = None
    if cache is not None:
        cache[path] = value
    return value


def app_digest(path):
    """Bounded fingerprint of a locally preserved app artifact, or unknown."""
    return _app_digest(path, None)


def case_provenance(entry, root, *, asset_cache=None):
    """Capture the effective parsed check and external proof at this admission boundary.

    `app.path` bytes are an intentional comparison variable; launch and assertion semantics remain in the spec.
    Each input freezes on its own: one that can't be read is null rather than an invented comparable identity, and the
    others stay pinned. `inputChanged` says an input differs from what the check was admitted with, which refuses it.
    """
    unknown = {**dict.fromkeys(FIELDS), "sourceDigest": None, "inputChanged": False}
    check = entry.check
    if check is None:
        return unknown
    try:
        source_digest = _source_digest(entry.path)
    except (OSError, ValueError):
        source_digest = None
    frozen = {**unknown, "sourceDigest": source_digest, "appDigest": _app_digest(check.app_path, asset_cache)}
    try:
        effective = with_fixtures(check, root)
        if check.fixture_origin is not None:
            # A prepared Check retains its source. Re-resolve only fixture-owned fields here: PR builds may
            # intentionally replace app.path, while fixture YAML and link-registry edits must still refuse.
            try:
                current = with_fixtures(check.fixture_origin, root)
                if any(links.OPEN_STEP.match(step) for step in current.steps):
                    current = replace(current,
                                      steps=links.expand_steps(current.steps, links.discover(root, deep=False)))
            except (FixtureError, links.LinksError) as error:
                raise InputChanged(str(error)) from None  # it resolved when the check was prepared
            if (current.steps, current.reset, current.launch_args, current.launch_env, current.seed) != (
                    check.steps, check.reset, check.launch_args, check.launch_env, check.seed):
                raise InputChanged("A fixture or linked step changed during this suite")
        spec = check_to_dict(replace(effective, fixture_origin=None), project_root=root)
        spec["app"].pop("path", None)
        seed = effective.seed
        spec["effectiveSeed"] = {"files": [(str(Path(source).relative_to(root)) if Path(source).is_relative_to(root)
                                            else str(source), destination)
                                           for source, destination in seed.files],
                                 "keychain": seed.keychain, "pasteboard": seed.pasteboard,
                                 "risky": list(seed.risky)}
        spec["l10n"] = effective.l10n
        spec["a11yScope"] = effective.a11y_scope
        proof = proof_assets(effective)
        baseline = {key: proof[key] for key in ("visualSha256", "mediaSha256", "pushSha256", "a11ySha256")}
        fixture = {key: proof[key] for key in ("businessSha256", "seedSha256")}
        baseline_digest = _hash(baseline) if any(value is not None for value in baseline.values()) else "none"
        fixture_digest = _hash(fixture) if effective.business is not None or effective.seed else "none"
        return {**frozen, "checkSpecDigest": _hash(spec), "baselineDigest": baseline_digest,
                "visualBaselineSha256": proof["visualSha256"],
                "fixtureDigest": fixture_digest, "businessBindingSha256": proof["businessSha256"]}
    except InputChanged:
        return {**frozen, "inputChanged": True}
    except (OSError, ValueError, TypeError, FixtureError, HealError, links.LinksError):
        return frozen


def unpinned(frozen, check):
    """What of a check was never frozen, as a note names it, so its report can't claim that identity."""
    return [name for name, missing in (("check file", frozen["sourceDigest"] is None),
                                       ("fixtures or assets", frozen["checkSpecDigest"] is None),
                                       ("app", bool(check.app_path) and frozen["appDigest"] is None)) if missing]


def require_case_provenance(entry, root, frozen, *, asset_cache=None):
    """Refuse an input known to have changed, before another physical effect. A field that could not be read pins
    nothing: the check runs, and its null keeps the report and `mobster test compare` from claiming an identity."""
    from .targets import Refused
    current = case_provenance(entry, root, asset_cache=asset_cache)
    if frozen["inputChanged"] or any(pinned is not None and current[field] != pinned
                                     for field, pinned in frozen.items()):
        raise Refused(f"{entry.file}, its app, or a fixture or assertion asset it uses changed during this suite. "
                      "Start a new suite.", "refused")

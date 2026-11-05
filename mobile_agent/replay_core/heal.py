"""Heals as reviewable diffs: ``<check>.heal.yaml`` next to the check, Accept and Keep failing.

A heal is written the moment a run heals, never applied silently:

- the run's results.json lists it in ``healed[]`` (step, from, to, why, frames before and after);
- ``<check>.heal.yaml`` holds every heal not yet reviewed, beside the check, so it shows in a pull request;
- the approved cached path stays unchanged until Accept; the full proposal stays private in the replay cache.

Review:

- **Accept** (``mobster test heal --accept``): the proposed steps become approved; the .heal.yaml file goes.
- **Keep failing** (``--reject``): the approved steps stay and become strict at the changed positions: when the
  screen differs there again, the check fails instead of healing. Use it when the change is a bug.

A heal never touches the check file: its steps and its expectations stay as you wrote them.
"""

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

from .path import PathError, validate
from .store import NAME, Cache, atomic_write, file_name, revision, sync_directory

VERSION = 2
SUFFIX = ".heal.yaml"
PROPOSAL = re.compile(r"[0-9a-f]{64}")


class HealError(Exception):
    """A heal file or its cached path that can't be reviewed: one plain sentence."""


class InputChanged(HealError):
    """A check's input differs from what the check was admitted with, as opposed to one that can't be read."""


def heal_file(check_file):
    """``<checks>/paywall.heal.yaml`` for ``<checks>/paywall.yaml``."""
    check_file = Path(check_file)
    return check_file.with_name(check_file.name.rsplit(".", 1)[0] + SUFFIX)


def _header(check_name):
    return ("# Mobster heal diff: steps of this check changed on screen and Smart healed them. Review them.\n"
            "# A heal never changes an expectation. The check file itself is untouched.\n"
            f"#   Accept:        mobster test heal --accept {check_name}\n"
            f"#   Keep failing:  mobster test heal --reject {check_name}\n")


def read(check_file):
    """The heal document for ``check_file``, or None when nothing waits for review. Raises HealError when the file
    exists but can't be read."""
    path = heal_file(check_file)
    if not path.is_file():
        return None
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise HealError(f"{path.name} can't be read ({type(error).__name__})") from None
    if not isinstance(data, dict) or not isinstance(data.get("heals"), list):
        raise HealError(f"{path.name} isn't a heal diff Mobster wrote")
    return data


def _write(path, document):
    import yaml
    text = _header(Path(document.get("check") or path.name).name) + yaml.safe_dump(
        document, sort_keys=False, allow_unicode=True, default_flow_style=False, width=110)
    atomic_write(path, text)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def effective_digest(check):
    """Bind fixture-flattened execution and assertions without storing their text in the proposal."""
    from .checks import _proof
    return _digest(_proof(check))


def _proposal_file(cache_dir, cache_name, proposal):
    if not isinstance(cache_name, str) or not NAME.fullmatch(cache_name) or not isinstance(proposal, str) \
            or not PROPOSAL.fullmatch(proposal):
        raise HealError("the heal file doesn't name a valid recorded proposal")
    return Path(cache_dir) / f"{cache_name[:-5]}-{proposal}.proposal.json"


def _check_digest(check_file):
    try:
        return hashlib.sha256(Path(check_file).read_bytes()).hexdigest()
    except OSError as error:
        raise HealError(f"the check file can't be read ({type(error).__name__})") from None


def _file_digest(path, limit):
    """Hash one regular asset without following its final symlink or accepting an in-place rewrite."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise HealError("an external check asset is not a bounded regular file")
            digest = hashlib.sha256()
            size = 0
            while chunk := os.read(fd, min(65536, limit + 1 - size)):
                size += len(chunk)
                if size > limit:
                    raise HealError("an external check asset exceeds its proof limit")
                digest.update(chunk)
            after = os.fstat(fd)
            identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            if size != before.st_size or identity(after) != identity(before):
                raise HealError("an external check asset changed while it was read")
            return size, digest.hexdigest()
        finally:
            os.close(fd)
    except OSError as error:
        raise HealError(f"an external check asset can't be read ({type(error).__name__})") from None


def _assets(files, *, limit, count_limit):
    rows, total = [], 0
    for label, path in files:
        size, digest = _file_digest(path, limit - total)
        total += size
        rows.append((label, size, digest))
        if len(rows) > count_limit:
            raise HealError("the check has too many external assets to freeze")
    return _digest(rows)


def proof_assets(check, *, include_a11y=False):
    """Digests of external evidence the parsed check uses to judge a run."""
    from ..verify import conditions, fixtures
    seed = getattr(check, "seed", None)
    seed_files = []
    for source, destination in getattr(seed, "files", ()):
        source = Path(source)
        if source.is_symlink():
            raise HealError("a seed asset is a symlink; the approved path was kept")
        if source.is_file():
            seed_files.append((destination, source))
        elif source.is_dir():
            paths = []
            for path in source.rglob("*"):
                paths.append(path)
                if len(paths) > fixtures.FILES_LIMIT * 8:
                    raise HealError("the seed directory has too many entries to freeze")
            for path in sorted(paths):
                if path.is_symlink():
                    raise HealError("a seed asset is a symlink; the approved path was kept")
                if path.is_file():
                    seed_files.append((str(Path(destination) / path.relative_to(source)), path))
                elif not path.is_dir():
                    raise HealError("a seed asset is not a regular file")
        else:
            raise HealError("a seed asset is unavailable; the approved path was kept")
    seed_sha = _assets(seed_files, limit=fixtures.BYTES_LIMIT, count_limit=fixtures.FILES_LIMIT * 4)
    condition = getattr(check, "conditions", None) or {}
    media = condition.get("media") or ()
    media_sha = _assets(((str(index), Path(path)) for index, path in enumerate(media)),
                        limit=128 * 1024 * 1024, count_limit=conditions.MEDIA_LIMIT)
    push = condition.get("push")
    push_sha = _file_digest(push, conditions.PAYLOAD_LIMIT)[1] if push else None
    a11y_sha = None
    if include_a11y or getattr(check, "a11y", None) or "a11y" in getattr(check, "audit", ()) \
            or getattr(check, "a11y_refresh", False):
        if getattr(check, "a11y_refresh", False):
            if check.a11y_baseline:
                raise HealError("the accessibility refresh did not apply an empty baseline")
            baseline = check.a11y_baseline  # the applied empty set; the old disk baseline is being replaced
        else:
            from ..testkit import a11y_baseline
            from ..verify.checks import project_root
            try:
                baseline = a11y_baseline.load(project_root(check.source))
            except a11y_baseline.BaselineError as error:
                raise HealError(f"the accessibility baseline can't be read ({type(error).__name__})") from None
            if getattr(check, "a11y_scope", None) and baseline != check.a11y_baseline:
                raise InputChanged("the accessibility baseline changed since check admission")
        a11y_sha = _digest(sorted(baseline))
    business_sha = None
    if getattr(check, "business", None) is not None:
        from ..verify.assertions import CheckError
        from ..verify.business import load_business_binding
        from ..verify.checks import project_root
        try:
            business_sha = load_business_binding(check, project_root(check.source)).digest
        except (CheckError, OSError) as error:
            raise HealError(f"the app-state fixture can't be read ({type(error).__name__})") from None
    return {"visualSha256": None, "businessSha256": business_sha, "seedSha256": seed_sha,
            "mediaSha256": media_sha, "pushSha256": push_sha, "a11ySha256": a11y_sha}


def discard(check_file, cache_dir, cache_name):
    """An explicit fresh recording supersedes a proposal for this cache key."""
    document = read(check_file)
    if document is None or document.get("cache") != cache_name:
        return False
    private = None
    if document.get("version") == VERSION:
        private = _proposal_file(cache_dir, cache_name, document.get("proposal"))
    heal_file(check_file).unlink()
    sync_directory(heal_file(check_file).parent)
    if private is not None:
        private.unlink(missing_ok=True)
        sync_directory(private.parent)
    return True


def record(check_file, heals, *, run_id, cache_file, candidate, base_revision, check_sha256,
           effective_sha256, proof,
           app=None, ios=None, device=None):
    """Stage a private candidate, then publish its review diff. The approved path is untouched."""
    path = heal_file(check_file)
    if path.exists():
        raise HealError(f"{path.name} already waits for review")
    validate(candidate)
    cache_file = Path(cache_file)
    if file_name(candidate["identity"], candidate["env"]) != cache_file.name \
            or not isinstance(base_revision, str) or not PROPOSAL.fullmatch(base_revision):
        raise HealError("the healed path doesn't match its approved recording")
    from ..verify.checks import project_root
    root = project_root(check_file)
    at = time.strftime("%Y-%m-%dT%H:%M:%S")
    entries = []
    for heal in heals:
        entry = {key_: heal.get(key_) for key_ in ("step", "from", "to", "why", "old", "new")}
        entry["frames"] = {name: _from_root(value, root) for name, value in (heal.get("frames") or {}).items()}
        entries.append(entry | {"run": run_id, "at": at})
    check_sha = _check_digest(check_file)
    if check_sha != check_sha256:
        raise HealError("the check changed during healing; the approved path was kept")
    from .checks import _saved_check
    try:
        current_check = _saved_check(check_file)
        current_proof = proof_assets(current_check, include_a11y=proof["a11ySha256"] is not None)
    except (OSError, ValueError) as error:
        raise HealError(f"the check can't be read ({type(error).__name__})") from None
    if current_proof != proof or effective_digest(current_check) != effective_sha256:
        raise HealError("the check's evidence changed during healing; the approved path was kept")
    payload = {"candidate": candidate, "baseRevision": base_revision, "checkSha256": check_sha,
               "effectiveSha256": effective_sha256, "proof": proof, "heals": entries}
    proposal = _digest(payload)
    private = _proposal_file(cache_file.parent, cache_file.name, proposal)
    atomic_write(private, json.dumps(payload, ensure_ascii=False))
    document = {"version": VERSION, "check": Path(check_file).name, "run": run_id,
                "cache": cache_file.name, "proposal": proposal,
                "baseRevision": base_revision, "candidateRevision": revision(candidate),
                "checkSha256": check_sha, "app": app, "ios": ios, "device": device, "heals": entries}
    _write(path, document)
    return path


def _from_root(value, root):
    """A frame's path relative to the project (``.mobster/runs/<run>/frames/...``), else as it is."""
    if not value:
        return None
    try:
        return Path(value).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return str(value)


def _review(check_file, cache_dir, decision):
    cache = Cache(cache_dir)
    with cache.locked():
        return _review_locked(check_file, cache, decision)


def _review_locked(check_file, cache, decision):
    document = read(check_file)
    if document is None:
        return 0
    if document.get("version") != VERSION:
        raise HealError("this older heal has no approved baseline; record the check again with --fresh")
    name, proposal = document.get("cache"), document.get("proposal")
    private = _proposal_file(cache.directory, name, proposal)
    if (not isinstance(document.get("baseRevision"), str)
            or not PROPOSAL.fullmatch(document["baseRevision"])
            or not isinstance(document.get("checkSha256"), str)
            or not PROPOSAL.fullmatch(document["checkSha256"])
            or not isinstance(document.get("candidateRevision"), str)
            or not PROPOSAL.fullmatch(document["candidateRevision"])):
        raise HealError("the heal diff is invalid; review evidence was kept")
    try:
        payload = json.loads(private.read_text(encoding="utf-8"))
        candidate = validate(payload["candidate"])
        bound_name = file_name(candidate["identity"], candidate["env"])
    except (OSError, ValueError, KeyError, TypeError, PathError):
        raise HealError("the heal proposal is missing or invalid; review evidence was kept") from None
    if (_digest(payload) != proposal or payload.get("baseRevision") != document.get("baseRevision")
            or payload.get("checkSha256") != document.get("checkSha256")
            or payload.get("heals") != document.get("heals")
            or revision(candidate) != document.get("candidateRevision")
            or bound_name != name or _check_digest(check_file) != document["checkSha256"]):
        raise HealError("the heal proposal or check changed; review evidence was kept")
    from .checks import _saved_check
    try:
        current_check = _saved_check(check_file)
        expected_proof = payload.get("proof")
        if not isinstance(expected_proof, dict):
            raise HealError("the heal proposal has no evidence proof; review evidence was kept")
        current_proof = proof_assets(current_check, include_a11y=expected_proof.get("a11ySha256") is not None)
    except (OSError, ValueError) as error:
        raise HealError(f"the check can't be read ({type(error).__name__}); review evidence was kept") from None
    if current_proof != expected_proof or effective_digest(current_check) != payload.get("effectiveSha256"):
        raise HealError("the check's evidence changed; review evidence was kept")
    approved = cache.load(candidate["identity"], candidate["env"])
    if approved is None:
        raise HealError("the approved recording is missing or invalid; review evidence was kept")
    receipt = approved.get("review")
    if receipt is not None and not isinstance(receipt, dict):
        raise HealError("the approved recording has an invalid review receipt; review evidence was kept")
    receipt = receipt or {}
    if receipt.get("proposal") == proposal:
        if receipt.get("decision") != decision:
            raise HealError("this heal already received the opposite decision")
    else:
        if revision(approved) != document["baseRevision"]:
            raise HealError("the approved recording changed; review evidence was kept")
        for heal in document["heals"]:
            if (not isinstance(heal, dict) or not isinstance(heal.get("step"), int)
                    or isinstance(heal["step"], bool) or not 1 <= heal["step"] <= len(approved["steps"])
                    or not isinstance(heal.get("old"), list)):
                raise HealError("the heal diff has an invalid step; review evidence was kept")
        if decision == "accept":
            chosen = dict(candidate)
        else:
            chosen = dict(approved)
            strict = set(chosen.get("strict") or ())
            for heal in document["heals"]:
                start, old = heal["step"], heal["old"]
                count = max(1, len(old))
                strict.update(range(start, min(start + count, len(chosen["steps"]) + 1)))
            chosen["strict"] = sorted(strict)
        chosen["review"] = {"proposal": proposal, "decision": decision}
        cache.save(chosen)
    heal_file(check_file).unlink()
    sync_directory(heal_file(check_file).parent)
    private.unlink(missing_ok=True)
    sync_directory(private.parent)
    return len(document["heals"])


def accept(check_file, cache_dir):
    """Make the reviewed proposal the active recording, after verifying its approved base."""
    return _review(check_file, cache_dir, "accept")


def reject(check_file, cache_dir):
    """Keep the approved steps and protect the replaced interval from later healing."""
    return _review(check_file, cache_dir, "reject")


__all__ = ["HealError", "heal_file", "read", "record", "accept", "reject", "SUFFIX"]

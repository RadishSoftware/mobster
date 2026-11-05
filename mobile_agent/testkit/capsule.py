"""Completion-time suite hashes and an explicitly selected local evidence export.

The inventory proves a saved report has not changed since capture; it does not authenticate the runner.
Raw reports and pixels stay on this Mac unless the caller selects their kind for export.
"""

import hashlib
import json
import os
import re
import stat
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path

from ..transport import decode_json
from . import reports

SCHEMA = "mobster.test.capsule/1"
INVENTORY_SCHEMA = "mobster.test.evidence/1"
SUITE_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{4}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
REPORT = re.compile(r"runs/[A-Za-z0-9_.-]{1,128}\.html\Z")
VISUAL = re.compile(r"runs/[A-Za-z0-9_.-]{1,128}/visual/[A-Za-z0-9_.-]{1,128}\.png\Z")
KINDS = frozenset({"junit", "report", "visual"})
STATUSES = frozenset({"passed", "failed", "needs_review", "couldnt_run"})
MAX_DOC = 16 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_SIDECAR = 256 * 1024
MAX_FILES = 512


class CapsuleError(ValueError):
    """A suite or evidence file cannot be attributed to its completed run."""


@contextmanager
def _directory(path):
    try:
        with ExitStack() as opened:
            location = Path(path).absolute()
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            opened.callback(os.close, fd)
            for part in location.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                opened.callback(os.close, child)
                fd = child
            yield fd
    except OSError as error:
        raise CapsuleError("Suite or output folder is unavailable or is a symlink") from error


def _read_at(folder_fd, relative, limit):
    parts = relative.split("/")
    if any(not NAME.fullmatch(part) or part in (".", "..") for part in parts):
        raise CapsuleError("Invalid suite evidence path")
    parent = os.dup(folder_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                raise CapsuleError("Suite evidence is not a bounded regular file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(limit + 1)
            after = os.fstat(fd)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
            if len(data) > limit or len(data) != before.st_size or any(
                    getattr(before, field) != getattr(after, field) for field in fields):
                raise CapsuleError("Suite evidence changed or exceeded its limit while reading")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _json_at(folder_fd, name, limit):
    try:
        return decode_json(_read_at(folder_fd, name, limit))
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise CapsuleError(f"Invalid or unavailable {name}") from error


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _atomic_bytes(folder_fd, name, data):
    temporary = "." + name + "." + os.urandom(8).hex()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder_fd)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
        os.replace(temporary, name, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
        os.fsync(folder_fd)
    finally:
        os.close(fd)
        try:
            os.unlink(temporary, dir_fd=folder_fd)
        except FileNotFoundError:
            pass


def _references(doc):
    paths = {("junit", "junit.xml"), ("report", "index.html")}
    checks = doc.get("checks")
    if not isinstance(checks, list):
        raise CapsuleError("Invalid suite checks")
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("results"), list):
            raise CapsuleError("Invalid suite check results")
        for cell in check["results"]:
            if not isinstance(cell, dict):
                raise CapsuleError("Invalid suite cell")
            runs = cell.get("runs", [])
            if not isinstance(runs, list) or any(not isinstance(row, dict) for row in runs):
                raise CapsuleError("Invalid suite attempts")
            rows = [cell, *runs]
            for row in rows:
                report = row.get("report")
                if isinstance(report, str) and REPORT.fullmatch(report):
                    paths.add(("report", report))
                visual = row.get("visual")
                if isinstance(visual, dict):
                    for key in ("baseline", "actual", "diff"):
                        value = visual.get(key)
                        if isinstance(value, str) and VISUAL.fullmatch(value):
                            paths.add(("visual", value))
    if len(paths) > MAX_FILES:
        raise CapsuleError("Too many suite evidence files")
    return sorted(paths)


def _inventory(folder_fd, suite):
    inventory = _json_at(folder_fd, "evidence-inventory.json", MAX_SIDECAR)
    if not isinstance(inventory, dict) or set(inventory) != {"schema", "suite", "source", "files"} or \
            inventory["schema"] != INVENTORY_SCHEMA or inventory["suite"] != suite or \
            not isinstance(inventory["files"], list) or len(inventory["files"]) > MAX_FILES:
        raise CapsuleError("Invalid suite evidence inventory")
    source = inventory["source"]
    if not isinstance(source, dict) or set(source) != {"size", "sha256"} or \
            type(source["size"]) is not int or not 0 < source["size"] <= MAX_DOC or \
            not isinstance(source["sha256"], str) or not DIGEST.fullmatch(source["sha256"]):
        raise CapsuleError("Invalid suite evidence inventory")
    seen = set()
    for row in inventory["files"]:
        if not isinstance(row, dict) or not isinstance(row.get("kind"), str) or row["kind"] not in KINDS or \
                not isinstance(row.get("path"), str) or \
                (row["kind"] == "junit" and row["path"] != "junit.xml") or \
                (row["kind"] == "report" and row["path"] != "index.html" and not REPORT.fullmatch(row["path"])) or \
                (row["kind"] == "visual" and not VISUAL.fullmatch(row["path"])) or \
                row.get("state") not in ("captured", "unavailable", "limit") or \
                (row["kind"], row["path"]) in seen:
            raise CapsuleError("Invalid suite evidence inventory")
        seen.add((row["kind"], row["path"]))
        if row["state"] == "captured":
            if set(row) != {"kind", "path", "state", "size", "sha256"} or \
                    type(row["size"]) is not int or not 0 <= row["size"] <= MAX_FILE or \
                    not isinstance(row["sha256"], str) or not DIGEST.fullmatch(row["sha256"]):
                raise CapsuleError("Invalid suite evidence inventory")
        elif set(row) != {"kind", "path", "state"}:
            raise CapsuleError("Invalid suite evidence inventory")
    return inventory


def _source(folder_fd, folder):
    source = _read_at(folder_fd, "results.json", MAX_DOC)
    try:
        doc = decode_json(source)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise CapsuleError("Invalid suite result") from error
    if not isinstance(doc, dict) or doc.get("schema") != reports.SCHEMA or \
            not isinstance(doc.get("suite"), str) or not SUITE_ID.fullmatch(doc["suite"]) or \
            Path(folder).name != doc["suite"] or not isinstance(doc.get("checks"), list):
        raise CapsuleError("Invalid suite result")
    try:
        inventory = _inventory(folder_fd, doc["suite"])
    except CapsuleError as error:
        if isinstance(error.__cause__, FileNotFoundError):
            raise CapsuleError("Suite has no completion-time evidence inventory") from error
        raise
    if inventory["source"] != {"size": len(source), "sha256": _hash(source)}:
        raise CapsuleError("Suite result changed since completion")
    return doc, inventory


def read_completed_suite(folder):
    """Read exact saved results only when their completion-time inventory still matches."""
    with _directory(folder) as folder_fd:
        doc, _ = _source(folder_fd, folder)
    return doc


def mark_complete(folder, doc):
    """Write the completion marker after the canonical reports; without it comparison and export refuse."""
    if SUITE_ID.fullmatch(folder.name):
        with suppress(CapsuleError, OSError):
            capture(folder, doc)


def capture(folder, doc):
    """Write the completion marker after all canonical reports and evidence copies exist."""
    if not isinstance(doc, dict) or not isinstance(doc.get("suite"), str) or \
            not SUITE_ID.fullmatch(doc["suite"]) or Path(folder).name != doc["suite"]:
        raise CapsuleError("Invalid suite result")
    with _directory(folder) as folder_fd:
        source = _read_at(folder_fd, "results.json", MAX_DOC)
        try:
            if decode_json(source) != doc:
                raise CapsuleError("Suite result changed before evidence capture")
        except (UnicodeError, ValueError, RecursionError) as error:
            raise CapsuleError("Invalid suite result before evidence capture") from error
        entries, total = [], 0
        for kind, path in _references(doc):
            if total >= MAX_TOTAL:
                entries.append({"kind": kind, "path": path, "state": "limit"})
                continue
            try:
                data = _read_at(folder_fd, path, MAX_FILE)
            except (OSError, CapsuleError):
                entries.append({"kind": kind, "path": path, "state": "unavailable"})
                continue
            total += len(data)
            if total > MAX_TOTAL:
                entries.append({"kind": kind, "path": path, "state": "limit"})
                continue
            entries.append({"kind": kind, "path": path, "state": "captured",
                            "size": len(data), "sha256": _hash(data)})
        inventory = {"schema": INVENTORY_SCHEMA, "suite": doc["suite"],
                     "source": {"size": len(source), "sha256": _hash(source)}, "files": entries}
        payload = (json.dumps(inventory, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(payload) > MAX_SIDECAR:
            raise CapsuleError("Suite evidence inventory is too large")
        _atomic_bytes(folder_fd, "evidence-inventory.json", payload)
    return inventory


def _selected(include):
    if not isinstance(include, (tuple, list)) or any(not isinstance(kind, str) or kind not in KINDS
                                                     for kind in include) or \
            len(include) != len(set(include)):
        raise CapsuleError("Select each of junit, report or visual evidence at most once")
    return frozenset(include)


def _safe(doc):
    devices = doc.get("devices")
    checks = doc.get("checks")
    if not isinstance(devices, list) or len(devices) > 20 or len(checks) > 500 or \
            type(doc.get("exitCode")) is not int or not 0 <= doc["exitCode"] <= 255:
        raise CapsuleError("Invalid suite verdict")
    targets = {}
    for number, device in enumerate(devices, 1):
        if not isinstance(device, dict) or not isinstance(device.get("key"), str) or device["key"] in targets:
            raise CapsuleError("Invalid suite target")
        targets[device["key"]] = f"target-{number}"
    rows = []
    for number, check in enumerate(checks, 1):
        if not isinstance(check, dict) or not isinstance(check.get("results"), list) or \
                len(check["results"]) > len(targets) or type(check.get("quarantined", False)) is not bool:
            raise CapsuleError("Invalid suite check")
        cells, seen = [], set()
        for cell in check["results"]:
            if not isinstance(cell, dict) or not isinstance(cell.get("deviceKey"), str) or \
                    cell["deviceKey"] not in targets or \
                    cell["deviceKey"] in seen or not isinstance(cell.get("status"), str) or \
                    cell["status"] not in STATUSES or \
                    type(cell.get("flaky")) is not bool:
                raise CapsuleError("Invalid suite cell")
            seen.add(cell["deviceKey"])
            cells.append({"target": targets[cell["deviceKey"]], "status": cell["status"],
                          "flaky": cell["flaky"]})
        rows.append({"check": f"check-{number}", "quarantined": check.get("quarantined", False),
                     "results": cells})
    return {"suite": doc["suite"], "exitCode": doc["exitCode"],
            "checks": rows, "targets": list(targets.values())}


def _inspect(folder, include, *, copies=None):
    selected = _selected(include)
    with _directory(folder) as folder_fd:
        doc, inventory = _source(folder_fd, folder)
        safe = _safe(doc)
        prior = {(row["kind"], row["path"]): row for row in inventory["files"]}
        evidence, total = [], 0
        for kind, path in _references(doc):
            recorded = prior.get((kind, path))
            row = {"kind": kind, "id": _hash(path.encode())[:24], "selected": kind in selected,
                   "redaction": "raw_unredacted" if kind in selected else "omitted_raw"}
            if recorded and recorded["state"] == "captured":
                row.update({"size": recorded["size"], "sha256": recorded["sha256"]})
            if kind in selected:
                try:
                    data = _read_at(folder_fd, path, MAX_FILE)
                except (OSError, CapsuleError):
                    row["state"] = "missing"
                else:
                    total += len(data)
                    if total > MAX_TOTAL:
                        raise CapsuleError("Selected suite evidence exceeds 128 MiB")
                    if recorded is None:
                        row["state"] = "unrecorded"
                    elif recorded["state"] != "captured":
                        row["state"] = "unavailable_at_capture"
                    elif len(data) != recorded["size"] or _hash(data) != recorded["sha256"]:
                        row["state"] = "changed"
                    else:
                        row["state"] = "matched"
                        row["file"] = f"evidence/{path}"
                        if copies is not None:
                            copies[row["file"]] = data
            else:
                row["state"] = recorded["state"] if recorded else "unrecorded"
            evidence.append(row)
    return {"schema": SCHEMA, "suite": safe["suite"], "results": safe,
            "evidence": evidence, "selection": sorted(selected), "omittedKinds": ["video", "log", "crash"]}


def inspect(folder, *, include=()):
    """Return bounded verdicts and hashes without raw report text or pixels."""
    return _inspect(folder, include)


def _write_at(folder_fd, relative, data):
    parts = relative.split("/")
    if any(not NAME.fullmatch(part) or part in (".", "..") for part in parts):
        raise CapsuleError("Invalid capsule output path")
    parent = os.dup(folder_fd)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def export(folder, output_dir, *, include=()):
    """Create a new private capsule folder; selected raw files remain local and unredacted."""
    copies = {}
    descriptor = _inspect(folder, include, copies=copies)
    if any(row["selected"] and row["state"] != "matched" for row in descriptor["evidence"]):
        raise CapsuleError("Selected evidence changed or was unavailable at suite completion")
    output = Path(output_dir).absolute()
    if not NAME.fullmatch(output.name) or output.name in (".", ".."):
        raise CapsuleError("Invalid capsule output folder")
    try:
        parent = output.parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CapsuleError("Capsule output parent is unavailable") from error
    with _directory(parent) as parent_fd:
        def attached():
            try:
                current = os.stat(output.parent)
            except OSError as error:
                raise CapsuleError("Capsule output parent changed") from error
            held = os.fstat(parent_fd)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise CapsuleError("Capsule output parent changed")

        attached()
        try:
            os.mkdir(output.name, 0o700, dir_fd=parent_fd)
        except FileExistsError as error:
            raise CapsuleError("Capsule output already exists") from error
        os.fsync(parent_fd)
        attached()
        folder_fd = os.open(output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            for path, data in copies.items():
                _write_at(folder_fd, path, data)
            payload = (json.dumps(descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
            if len(payload) > MAX_DOC:
                raise CapsuleError("Capsule descriptor is too large")
            _write_at(folder_fd, "capsule.json", payload)
            os.fsync(folder_fd)
        finally:
            os.close(folder_fd)
        attached()
    return descriptor

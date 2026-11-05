"""The corpus on disk: ``corpus/records.jsonl`` in the data folder (0700 folder, 0600 files), capped and rotated.

``records.jsonl`` takes appends until it reaches FILE_BYTES; then it becomes ``records.1.jsonl``, that one
``records.2.jsonl``, and the oldest is deleted. The corpus never holds more than FILES * FILE_BYTES.
``sanitize`` is the one gate: a record is written, and exported, only if every field has the shape below.
"""

import fcntl
import json
import os
import re
import stat
from pathlib import Path

from ..paths import user_data_dir
from ..secret_filter import redact_deep
from .skeleton import MAX_NODES, ROLE, TOKEN

FILE_BYTES = 4_000_000
FILES = 3
SCHEMA = 1
OPS = re.compile(r"[A-Z_]{2,24}")
HASH = re.compile(r"[0-9a-f]{16}")
BUNDLE = re.compile(r"[A-Za-z0-9.-]{1,155}")
VERSION = re.compile(r"[0-9A-Za-z.+-]{1,40}")


class CorpusError(Exception):
    pass


def directory():
    return user_data_dir() / "corpus"


def _private(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise CorpusError("the corpus folder must be a folder you own")


def _files():
    root = directory()
    return [root / ("records.jsonl" if n == 0 else f"records.{n}.jsonl") for n in range(FILES)]


def _node(node):
    return (isinstance(node, list) and len(node) == 2 and isinstance(node[0], str) and ROLE.fullmatch(node[0])
            and isinstance(node[1], str) and TOKEN.fullmatch(node[1]))


def sanitize(record):
    """``record`` as the schema allows it, or None. Every string is a fixed-shape token or passes secret_filter."""
    try:
        screen, action, outcome = record["screen"], record["action"], record["outcome"]
        target = action["target"]
        ok = (record["v"] == SCHEMA and HASH.fullmatch(record["run"]) and isinstance(record["step"], int)
              and 0 <= record["step"] < 10_000 and BUNDLE.fullmatch(record["app"])
              and (record["version"] is None or VERSION.fullmatch(record["version"]))
              and HASH.fullmatch(screen["fp"]) and isinstance(screen["nodes"], list)
              and len(screen["nodes"]) <= MAX_NODES and all(_node(n) for n in screen["nodes"])
              and OPS.fullmatch(action["op"]) and isinstance(action["typed"], bool)
              and (target is None or (isinstance(target["i"], int) and 0 <= target["i"] < len(screen["nodes"])
                                      and _node([target["role"], target["label"]])))
              and isinstance(outcome["changed"], bool) and outcome["run"] == "done")
    except (KeyError, TypeError):
        return None
    return record if ok and redact_deep(record) == record else None


def append(records):
    """Append ``records`` (already sanitized) as one write; returns how many were written."""
    lines = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records if sanitize(r))
    if not lines:
        return 0
    root = directory()
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(mode=0o700, exist_ok=True)
    _private(root)
    files = _files()
    lock = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if files[0].exists() and files[0].stat().st_size + len(lines) > FILE_BYTES:
            files[-1].unlink(missing_ok=True)
            for newer, older in reversed(list(zip(files, files[1:]))):
                if newer.exists():
                    os.replace(newer, older)
        fd = os.open(files[0], os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(lines)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(lock)
    return lines.count("\n")


def read():
    """Every stored record that still passes ``sanitize``, oldest file first."""
    for path in reversed(_files()):
        try:
            handle = open(path, encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    record = sanitize(json.loads(line))
                except ValueError:
                    continue
                if record:
                    yield record


def stats():
    """Counts for ``mobster corpus stats``: records, runs, steps per app, bytes, files."""
    records, runs, apps = 0, set(), {}
    for record in read():
        records += 1
        runs.add(record["run"])
        apps[record["app"]] = apps.get(record["app"], 0) + 1
    present = [p for p in _files() if p.exists()]
    return {"records": records, "runs": len(runs), "bytes": sum(p.stat().st_size for p in present),
            "files": len(present), "capBytes": FILE_BYTES * FILES, "apps": dict(sorted(apps.items()))}


def export(out):
    """Write the masked bundle to ``out`` (a new file, 0600): a header line, then one record per line."""
    out = Path(out).expanduser()
    records = list(read())
    header = {"bundle": "mobster-corpus", "v": SCHEMA, "records": len(records),
              "note": "roles, hashed labels and actions only; no screen text, no typed text"}
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise CorpusError(f"{out} exists; choose a path that doesn't") from None
    except OSError as error:
        raise CorpusError(f"{out} can't be written ({error.strerror})") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for item in [header, *records]:
            handle.write(json.dumps(item, separators=(",", ":")) + "\n")
    return len(records)

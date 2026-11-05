"""``expect: storage``: what the app wrote under its own container, read on the Mac after the steps.

A simulator's app container is a folder on this Mac, so nothing is pulled over a wire: ``take`` hashes the files an
assertion names before the flow, ``judge`` compares them after, and a sqlite query runs on a copy so the app's
database is never opened for writing. Evidence is a short diff or the first rows, with secrets masked."""

import difflib
import hashlib
import json
import plistlib
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from .. import secret_filter
from .assertions import AssertionResult

FILES_LIMIT = 2000
HASH_LIMIT = 16 << 20
TEXT_LIMIT = 64 << 10
DIFF_LINES = 8
ROWS_SHOWN = 3
QUERY_SECONDS = 5
READ_ONLY = re.compile(r"\s*(select|with)\b", re.I)


class StoreError(Exception):
    pass


def read_only_query(sql):
    """One SELECT or WITH statement (the authorizer in ``rows`` refuses writes whatever this accepts)."""
    body = sql.strip().rstrip(";").strip()
    return bool(READ_ONLY.match(body)) and ";" not in body and len(body) <= 2000


def container(run, udid, bundle):
    """The app's data container on this Mac, from ``simctl get_app_container <udid> <bundle> data``."""
    code, output = run(["get_app_container", udid, bundle, "data"])
    path = Path(output.splitlines()[-1].strip()) if output.strip() else None
    if code != 0 or path is None or not path.is_dir():
        raise StoreError("Mobster couldn't find the app's container on the simulator"
                         + (f" ({output.splitlines()[-1][:120]})" if output.strip() else "") + ".")
    return path


def _files(root, pattern):
    """Files under ``root`` matching a glob, as {relative path: (size, sha256, text or None)}."""
    found = {}
    for path in sorted(root.glob(pattern)):
        if len(found) >= FILES_LIMIT:
            break
        if not path.is_file() or path.is_symlink():
            continue
        size = path.stat().st_size
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            kept = handle.read(HASH_LIMIT)
            digest.update(kept)
        found[path.relative_to(root).as_posix()] = (size, digest.hexdigest(),
                                                    _readable(path, kept) if size <= TEXT_LIMIT else None)
    return found


def _readable(path, raw):
    """A file's content as text for a diff: a plist as JSON-like lines, text as is, anything else None."""
    if raw[:6] == b"bplist" or raw[:5] == b"<?xml":
        try:
            return json.dumps(plistlib.loads(raw), indent=1, sort_keys=True, default=str)
        except (plistlib.InvalidFileException, ValueError, OverflowError):
            return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def take(root, assertion):
    """What the assertion looks at before the flow: its files, or its query's row count."""
    if assertion.file:
        return {"files": _files(root, assertion.file)}
    try:
        return {"rows": len(rows(root, assertion))}
    except StoreError:
        return {"rows": None}


def rows(root, assertion):
    """The query's rows from a copy of the database (and its -wal and -shm), never the file itself."""
    source = root / assertion.sqlite
    if not source.is_file():
        raise StoreError(f"{assertion.sqlite} isn't in the app's container.")
    with tempfile.TemporaryDirectory(prefix="mobster-wire-") as scratch:
        copy = Path(scratch, "db.sqlite")
        shutil.copyfile(source, copy)
        for suffix in ("-wal", "-shm"):
            if Path(f"{source}{suffix}").is_file():
                shutil.copyfile(f"{source}{suffix}", f"{copy}{suffix}")
        deadline = time.monotonic() + QUERY_SECONDS
        connection = sqlite3.connect(str(copy))
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in (
                sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE)
                else sqlite3.SQLITE_DENY)
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            return connection.execute(assertion.query).fetchall()
        except sqlite3.Error as error:
            raise StoreError(f"the query on {assertion.sqlite} failed: {str(error)[:120]}.") from None
        finally:
            connection.close()


def _show(text, limit=160):
    text = secret_filter.redact(str(text))
    return text if len(text) <= limit else text[:limit] + "…"


def _diff(before, after):
    old = (before[2] or "").splitlines() if before else []
    new = (after[2] or "").splitlines() if after else []
    if (before and before[2] is None) or (after and after[2] is None):
        return f"{before[0] if before else 0} -> {after[0] if after else 0} bytes (not text)"
    lines = [line for line in difflib.unified_diff(old, new, lineterm="", n=0) if line[:1] in "+-" and line[:3] not in
             ("+++", "---")]
    shown = "; ".join(_show(line, 80) for line in lines[:DIFF_LINES])
    return shown + (f"; and {len(lines) - DIFF_LINES} more lines" if len(lines) > DIFF_LINES else "")


def judge(assertion, root, before):
    text = assertion.render()
    if assertion.sqlite:
        return _judge_rows(assertion, text, root, before)
    after = _files(root, assertion.file)
    if before is None:
        return AssertionResult(False, text, "Mobster took no baseline before the flow, so it can't tell what changed")
    old = before["files"]
    moved = [(name, old.get(name), after.get(name)) for name in sorted({*old, *after})
             if (old.get(name) or (0, ""))[1] != (after.get(name) or (0, ""))[1]]
    if assertion.contains:
        holds = [name for name, new in sorted(after.items()) if new[2] and assertion.contains.contains(new[2])]
    if not assertion.changed:
        if moved:
            name, was, now = moved[0]
            return AssertionResult(False, text, f"{name} changed: {_diff(was, now)}")
        return AssertionResult(True, text, f"{len(after)} matching files unchanged" if after else
                               "no file matches, so none changed")
    if not moved:
        return AssertionResult(False, text, f"none of {len(after)} matching files changed" if after
                               else f"no file in the container matches {assertion.file}")
    name, was, now = moved[0]
    kind = "added" if was is None else "deleted" if now is None else "changed"
    if assertion.contains and not holds:
        return AssertionResult(False, text, f"{name} {kind}, but no matching file contains "
                                            f'"{assertion.contains.render()}"')
    return AssertionResult(True, text, f"{name} {kind}: {_diff(was, now)}" + (f" and {len(moved) - 1} more files"
                                                                            if len(moved) > 1 else ""),
                           ({"file": name, "diff": _diff(was, now)},))


def _judge_rows(assertion, text, root, before):
    try:
        found = rows(root, assertion)
    except StoreError as error:
        return AssertionResult(False, text, str(error))
    count = len(found)
    ok = (count == assertion.equals if assertion.equals is not None
          else count >= assertion.at_least if assertion.at_least is not None else count <= assertion.at_most)
    was = (before or {}).get("rows")
    shown = "; ".join(_show(row) for row in found[:ROWS_SHOWN])
    observed = f"{count} rows" + (f" (was {was})" if was is not None else "") + (f": {shown}" if shown else "")
    shown_rows = [[_show(cell) for cell in row] for row in found[:ROWS_SHOWN]]
    return AssertionResult(ok, text, observed, ({"rows": shown_rows},))

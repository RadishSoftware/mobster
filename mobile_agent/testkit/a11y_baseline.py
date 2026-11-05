"""The project's accessibility baseline (.mobster/a11y-baseline.json), so CI fails only on new findings."""

import fcntl
import hashlib
import json
import os
from pathlib import Path

from ..paths import write_text_atomic
from .input_files import read_bytes

SCHEMA = "mobster.a11y-baseline/1"
FILE = "a11y-baseline.json"
MAX_BYTES = 1_000_000


class BaselineError(Exception):
    """The baseline file can't be used: one sentence that says what to do."""


def path(root):
    return Path(root) / ".mobster" / FILE


def snapshot(root):
    """The old bytes, even when their JSON is malformed. A refresh must not overwrite a changed file."""
    file = path(root)
    if not file.exists() and not file.is_symlink():
        return "missing"
    try:
        return hashlib.sha256(read_bytes(file, MAX_BYTES)).hexdigest()
    except (OSError, ValueError):
        raise BaselineError(f".mobster/{FILE} cannot be safely read for replacement. Fix the file first.") from None


def refresh_error(doc, entries):
    """A replacement needs complete observations from every requested accessibility cell."""
    if doc.get("stopped") or doc.get("stopReason"):
        return "The suite stopped before the accessibility baseline could be updated."
    rows = doc.get("checks") or ()
    if len(rows) != len(entries):
        return "The suite did not report every check needed for the accessibility baseline."
    audited = 0
    for entry, row in zip(entries, rows):
        cells = row.get("results") or ()
        if not cells or any(cell.get("status") in ("couldnt_run", "needs_review") for cell in cells):
            return "A check could not complete, so the accessibility baseline was not updated."
        check = entry.check
        if check is None:
            return "A saved check could not be read, so the accessibility baseline was not updated."
        if check.a11y or "a11y" in check.audit:
            audited += len(cells)
            for cell in cells:
                if cell.get("status") == "failed" and not (
                        (cell.get("reason") or {}).get("class") == "finding" and
                        any(item.get("audit") == "a11y" and item.get("level") == "fail"
                            for item in cell.get("findings") or () if isinstance(item, dict))):
                    return "An accessibility check failed before its findings were complete. The baseline was kept."
                if len({run.get("repetition") for run in cell.get("runs") or ()}) != 1:
                    return ("An accessibility check ran under --repeat or did not start, so the baseline was "
                            "not updated. Run it again without --repeat.")
                coverage = cell.get("screenCoverage") or {}
                if coverage.get("complete") is not True or type(coverage.get("a11yScreens")) is not int or \
                        coverage["a11yScreens"] < 1:
                    return "An accessibility scan was incomplete, so the baseline was not updated."
    if not audited:
        return "No check scanned accessibility, so the baseline was not updated."
    return None


def load(root):
    """The baseline's keys (a frozenset); empty without a file."""
    file = path(root)
    if not file.is_file():
        return frozenset()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BaselineError(f".mobster/{FILE} can't be read as JSON. Fix it, or run `mobster test --a11y "
                            "--update-a11y-baseline` to write it again.") from None
    # Another schema stops the run: it would otherwise hide nothing and fail everything, or the reverse.
    found = data.get("findings") if isinstance(data, dict) and data.get("schema") == SCHEMA else None
    if not isinstance(found, list) or not all(isinstance(key, str) for key in found):
        raise BaselineError(f".mobster/{FILE} isn't a {SCHEMA} file. Run `mobster test --a11y "
                            "--update-a11y-baseline` to write it again.")
    return frozenset(found)


def keys(doc):
    """Every a11y finding's key in a results.json document, sorted."""
    out = set()
    for check in doc.get("checks") or ():
        for result in check.get("results") or ():
            out.update(item["key"] for item in result.get("findings") or ()
                       if isinstance(item, dict) and item.get("audit") == "a11y" and isinstance(item.get("key"), str))
    return sorted(out)


def write(root, doc, *, expected_snapshot=None):
    """Write the document's findings as the project's baseline. Returns how many keys it holds."""
    found = keys(doc)
    payload = json.dumps({"schema": SCHEMA, "findings": found}, indent=1, ensure_ascii=False) + "\n"
    if expected_snapshot is None:
        write_text_atomic(path(root), payload)
        return len(found)
    lock = path(root).with_name(".a11y-baseline.lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise BaselineError("The accessibility baseline cannot be locked for replacement. Fix its folder.") from None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if snapshot(root) != expected_snapshot:
            raise BaselineError("The accessibility baseline changed during this suite. Start a new run.")
        write_text_atomic(path(root), payload)
    except OSError:
        raise BaselineError("The accessibility baseline could not be written. Fix its folder and run again.") from None
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    return len(found)


def counts(doc):
    """{"new", "known", "warnings"}: a11y findings by level in a document (new = fail), for the report."""
    out = {"new": 0, "known": 0, "warnings": 0}
    for check in doc.get("checks") or ():
        for result in check.get("results") or ():
            for item in result.get("findings") or ():
                if isinstance(item, dict) and item.get("audit") == "a11y":
                    out[{"fail": "new", "known": "known"}.get(item.get("level"), "warnings")] += 1
    return out

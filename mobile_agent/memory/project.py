"""A project's facts and the file that carries them: ``.mobster/memory.yaml``.

The file holds what Mobster learned about the repo (its scheme and bundle, the screens a check walks, the heals it
accepted), so it travels with the code and shows in pull requests. Personal facts never go in it: only project-scope
facts whose source is the repo, a check, a run or a lesson, never what a person wrote or a profile holds, and never
a text with a home folder or a device ID in it. A teammate's file comes back in as ``repo`` facts at lower
confidence. It is data: whatever it says, nothing in it becomes a rule or a preference.
"""

import hashlib
import os
import re
from pathlib import Path

from .. import paths
from ..env_file import writer
from .model import KINDS

FILE = "memory.yaml"
SHARED_SOURCES = ("repo", "check", "run", "lesson")
MAX_BYTES = 65_536
MAX_ENTRIES = 200
IMPORTED_CONFIDENCE = 0.6
HEADER = ("# What Mobster learned about this project. It travels with the repo; personal facts never go here.\n"
          "# Edit or delete a line, or forget it in Mobster. Mobster reads this file as notes, never as orders.\n")
PERSONAL = re.compile(r"/Users/|/home/|/private/|~/|\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}\b|\b[0-9A-Fa-f]{40}\b"
                      r"|\b[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\b")


def derived_id(path):
    """The id a folder has before the Projects page gives it one (its resolved path, hashed)."""
    return "dir-" + hashlib.sha1(os.path.realpath(path).encode()).hexdigest()[:10]


def scope_of(store, path):
    """"project:<id>" for the folder at ``path``: the id the Projects page linked, else the derived one."""
    path = os.path.realpath(path)
    return f"project:{store.project_id_for(path) or derived_id(path)}"


def shareable(fact):
    return (fact["scopeKind"] == "project" and fact["source"] in SHARED_SOURCES
            and not PERSONAL.search(fact["text"]) and not PERSONAL.search(fact.get("key") or ""))


def export_file(store, project_id):
    """Write the project's shareable facts to ``<folder>/.mobster/memory.yaml``; delete the file when there are none
    left. Only where the folder already has a ``.mobster``, and only when the content changes. Returns the path
    written, or None."""
    import yaml
    folder = store.project_path(project_id)
    if not folder or not (Path(folder) / ".mobster").is_dir():
        return None
    target = Path(folder) / ".mobster" / FILE
    with writer(target):
        facts = sorted((fact for fact in store.list_facts(scope=f"project:{project_id}") if shareable(fact)),
                       key=lambda fact: (fact["kind"], fact["key"] or "", fact["text"]))
        if not facts:
            target.unlink(missing_ok=True)
            return None
        entries = [{"kind": fact["kind"], "text": fact["text"], **({"key": fact["key"]} if fact["key"] else {}),
                    "source": fact["source"], "confidence": round(fact["confidence"], 2)} for fact in facts]
        text = HEADER + yaml.safe_dump({"version": 1, "facts": entries}, sort_keys=False, allow_unicode=True, width=120)
        if target.exists() and target.read_text(encoding="utf-8", errors="replace") == text:
            return target
        paths.write_text_atomic(target, text)
        return target


def import_file(store, project_id):
    """Read ``<folder>/.mobster/memory.yaml`` into the project's facts as ``repo`` facts. A bad file adds nothing.
    Returns how many facts were added or updated."""
    import yaml
    folder = store.project_path(project_id)
    source = Path(folder) / ".mobster" / FILE if folder else None
    if source is None or not source.is_file() or source.is_symlink() or source.stat().st_size > MAX_BYTES:
        return 0
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return 0
    entries = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return 0
    changed = 0
    for entry in entries[:MAX_ENTRIES]:
        if not isinstance(entry, dict) or entry.get("kind") not in KINDS or PERSONAL.search(str(entry.get("text"))):
            continue
        confidence = entry.get("confidence")
        confidence = min(IMPORTED_CONFIDENCE, confidence) if isinstance(confidence, (int, float)) \
            and not isinstance(confidence, bool) and 0 <= confidence <= 1 else IMPORTED_CONFIDENCE
        key = entry.get("key") if isinstance(entry.get("key"), str) else None
        result = store.learn(entry.get("text"), entry["kind"], f"project:{project_id}", "repo",
                             ref={"repo": f".mobster/{FILE}"}, key=key, confidence=confidence)
        changed += result["status"] in ("added", "updated")
    return changed

"""Which checks a `mobster test` run covers: discovery under `.mobster/checks/` or the paths given, the quarantine
list, tags and sharding.

A check file is verify's format (docs/checks.md). A file that can't be read is kept, with its error, so the suite
reports it as couldn't run instead of skipping it quietly.
"""

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Optional

CHECK_SUFFIXES = (".yaml", ".yml", ".json")
# Files that sit beside checks but aren't checks.
NOT_CHECKS = frozenset({"matrix.yaml", "matrix.yml", "quarantine.yaml", "quarantine.yml"})
# What Mobster writes in .mobster/ (run results, reports, builds, the cache): never checks, though it holds .json.
OUTPUTS = frozenset({"runs", "test-results", "build", "cache"})
MAX_CHECKS = 500
SHARD = re.compile(r"(\d{1,3})/(\d{1,3})")
QUARANTINE_FILE = "quarantine.yaml"
QUARANTINE_LIMIT = 200


class DiscoveryError(Exception):
    """A usage problem with the paths, the quarantine file, tags or the shard: one plain sentence."""


@dataclass
class Entry:
    """One check file in the suite."""
    path: Path                    # absolute
    file: str                     # as reports show it: relative to the project root when inside it
    check: object = None          # verify.checks.Check, or None when the file can't be read
    error: Optional[str] = None   # why it can't be read
    quarantined: Optional[str] = None  # None, or the reason it is quarantined ("" when none was given)
    classname: str = ""
    keys: tuple = field(default=())  # every name the quarantine file may use for it

    @property
    def name(self):
        return self.check.name if self.check is not None else Path(self.file).stem

    @property
    def tags(self):
        return tuple(getattr(self.check, "tags", ()) or ())


def project_root(start=None):
    """The folder holding `.mobster/`, walking up from ``start`` (default the working directory); else ``start``."""
    start = Path(start or Path.cwd()).resolve()
    if start.is_file():
        start = start.parent
    for folder in (start, *start.parents):
        if (folder / ".mobster").is_dir():
            return folder
    return start


def _relative(path, root):
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _classname(path, root):
    """``checks.<file>``: the file's path without its suffix, from the checks folder when it is in one."""
    checks = root / ".mobster" / "checks"
    try:
        stem = path.relative_to(checks)
    except ValueError:
        try:
            stem = path.relative_to(root)
        except ValueError:
            stem = Path(path.name)
    parts = [re.sub(r"[^A-Za-z0-9_-]+", "_", part) for part in stem.with_suffix("").parts]
    return "checks." + ".".join(part for part in parts if part)


def _files_in(folder):
    out = []
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if not path.is_file() or path.suffix.lower() not in CHECK_SUFFIXES:
            continue
        if path.name in NOT_CHECKS or path.name.endswith((".heal.yaml", ".heal.yml")):
            continue
        if any(parent == ".mobster" and child in OUTPUTS for parent, child in zip(path.parts, path.parts[1:])):
            continue
        out.append(path)
    return out


def discover(paths, root, *, cwd=None, loader=None):
    """Every check under ``paths`` (files or folders), else under ``root``/.mobster/checks, as Entries sorted by
    file. Raises DiscoveryError for a path that doesn't exist, a file that isn't a check file, or no checks."""
    if loader is None:
        from ..verify.checks import load_check as loader
    from ..verify.checks import CheckError
    root = Path(root).resolve()
    cwd = Path(cwd or Path.cwd())
    files = []
    if not paths:
        folder = root / ".mobster" / "checks"
        if not folder.is_dir():
            raise DiscoveryError(f"There's no .mobster/checks folder in {root}. Save a check there with "
                                 "`mobster verify … --save NAME`, or pass a check file or folder.")
        files = _files_in(folder)
        if not files:
            raise DiscoveryError(f"{_relative(folder, cwd.resolve())} has no checks (.yaml, .yml or .json files).")
    for given in paths or ():
        path = Path(given).expanduser()
        path = (path if path.is_absolute() else cwd / path).resolve()
        if not path.exists():
            raise DiscoveryError(f"{given} doesn't exist.")
        if path.is_dir():
            found = _files_in(path)
            if not found:
                raise DiscoveryError(f"{given} has no checks (.yaml, .yml or .json files).")
            files.extend(found)
        elif path.suffix.lower() not in CHECK_SUFFIXES:
            raise DiscoveryError(f"{given} isn't a check file: checks are .yaml, .yml or .json.")
        else:
            files.append(path)
    unique = list(dict.fromkeys(path.resolve() for path in files))
    if len(unique) > MAX_CHECKS:
        raise DiscoveryError(f"That's {len(unique)} checks; a run takes at most {MAX_CHECKS}. Pass fewer paths, "
                             "or split them with --shard.")
    entries = []
    for path in unique:
        file = _relative(path, root)
        try:
            check, error = loader(path), None
        except CheckError as problem:
            check, error = None, str(problem)
        except (OSError, ValueError, RecursionError) as problem:
            check, error = None, f"{file} can't be read ({type(problem).__name__})"
        entry = Entry(path=path, file=file, check=check, error=error, classname=_classname(path, root))
        checks = root / ".mobster" / "checks"
        keys = {file, path.stem, Path(file).with_suffix("").as_posix()}
        try:
            keys.add(path.relative_to(checks).with_suffix("").as_posix())
        except ValueError:
            pass
        if check is not None:
            keys.add(check.name)
        entry.keys = tuple(sorted(key.casefold() for key in keys))
        entries.append(entry)
    entries.sort(key=lambda entry: entry.file)
    return entries


# -- quarantine ------------------------------------------------------------------------------------------------

def load_quarantine(path, *, required=False):
    """{key (casefolded): reason} from a quarantine file: a list of checks, each a check's file name, path or name,
    or {check: …, reason: …}; or the same list under ``quarantine:``. A missing file is {} unless ``required``."""
    path = Path(path)
    if not path.exists():
        if required:
            raise DiscoveryError(f"{path} doesn't exist.")
        return {}
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise DiscoveryError(f"{path} can't be read ({type(error).__name__}).") from None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = f" line {mark.line + 1}" if mark is not None else ""
        raise DiscoveryError(f"{path}{line}: {getattr(error, 'problem', None) or 'not valid YAML'}") from None
    if data is None:
        return {}
    if isinstance(data, dict):
        unknown = [key for key in data if key != "quarantine"]
        if unknown:
            raise DiscoveryError(f"{path} has an unknown key {unknown[0]!r}; list the checks under quarantine:")
        data = data.get("quarantine") or []
    if not isinstance(data, list):
        raise DiscoveryError(f"{path} must be a list of checks, such as - paywall")
    if len(data) > QUARANTINE_LIMIT:
        raise DiscoveryError(f"{path} lists more than {QUARANTINE_LIMIT} checks")
    out = {}
    for index, item in enumerate(data):
        reason = ""
        if isinstance(item, dict):
            unknown = [key for key in item if key not in ("check", "reason")]
            if unknown or not isinstance(item.get("check"), str):
                raise DiscoveryError(f"{path} item {index + 1}: write {{check: NAME, reason: WHY}}")
            reason = item.get("reason") or ""
            if not isinstance(reason, str):
                raise DiscoveryError(f"{path} item {index + 1}: reason must be text")
            item = item["check"]
        if not isinstance(item, str) or not item.strip():
            raise DiscoveryError(f"{path} item {index + 1} must name a check: its file name, path or name")
        out[item.strip().casefold()] = " ".join(reason.split())[:300]
    return out


def apply_quarantine(entries, listed):
    """Mark the entries ``listed`` names; returns the names that matched no check."""
    used = set()
    for entry in entries:
        for key in entry.keys:
            if key in listed:
                entry.quarantined = listed[key]
                used.add(key)
                break
    return [key for key in listed if key not in used]


# -- tags and shards -------------------------------------------------------------------------------------------

def with_tags(entries, tags):
    """The entries carrying any of ``tags``; a check that can't be read is kept, so its error still shows."""
    if not tags:
        return list(entries)
    wanted = {tag.strip().casefold() for tag in tags}
    return [entry for entry in entries if entry.check is None or wanted & {tag.casefold() for tag in entry.tags}]


def parse_shard(text):
    """``i/n`` as (i, n), 1 <= i <= n <= 100. Raises DiscoveryError."""
    match = SHARD.fullmatch(str(text or "").strip())
    if not match:
        raise DiscoveryError(f"--shard {text!r}: write I/N, such as 2/4 for the second of four shards")
    index, count = int(match[1]), int(match[2])
    if not 1 <= index <= count <= 100:
        raise DiscoveryError(f"--shard {text!r}: I is from 1 to N, and N at most 100")
    return index, count


def shard(entries, index, count):
    """The ``index``-th of ``count`` shards (1-based): every count-th check in file order, so the shards split the
    checks evenly and every machine that runs the same files agrees on them."""
    return [entry for position, entry in enumerate(sorted(entries, key=lambda e: e.file))
            if position % count == index - 1]

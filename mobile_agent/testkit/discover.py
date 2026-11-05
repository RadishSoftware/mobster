"""Which checks a `mobster test` run covers: discovery, the quarantine list, tags and shards."""

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

CHECK_SUFFIXES = (".yaml", ".yml", ".json")
NOT_CHECKS = frozenset({"matrix.yaml", "matrix.yml", "quarantine.yaml", "quarantine.yml"})
# What Mobster writes in .mobster/ (run results, reports, builds, the cache): never checks, though it holds .json.
OUTPUTS = frozenset({"runs", "test-results", "build", "cache"})
MAX_CHECKS = 500
SHARD = re.compile(r"(\d{1,3})/(\d{1,3})")
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
    locale: Optional[str] = None  # `--locale`: the language this copy of the check runs in

    @property
    def name(self):
        return self.check.name if self.check is not None else Path(self.file).stem

    @property
    def tags(self):
        return tuple(getattr(self.check, "tags", ()) or ())


def in_locales(entries, locales):
    out = []
    for entry in entries:
        if entry.check is None:
            out.append(entry)
            continue
        for locale in locales:
            check = replace(entry.check, name=f"{entry.check.name} [{locale}]", l10n=True,
                            conditions={**(entry.check.conditions or {}), "locale": locale})
            out.append(replace(entry, check=check, locale=locale, classname=f"{entry.classname}.{locale}"))
    return out


def in_matrix(entries):
    """A check whose conditions list appearances, text sizes or locales becomes one entry per combination."""
    from ..verify import conditions
    out = []
    for entry in entries:
        if entry.check is None or not conditions.is_matrix(entry.check.conditions):
            out.append(entry)
            continue
        for combo in conditions.combinations(entry.check.conditions):
            tag = ", ".join(str(combo[key]) for key in conditions.MATRIX_KEYS
                            if key in combo and isinstance(entry.check.conditions[key], list))
            slug = re.sub(r"[^A-Za-z0-9_-]+", "_", tag.replace(", ", "."))
            out.append(replace(entry, check=replace(entry.check, name=f"{entry.check.name} [{tag}]",
                                                    conditions=combo),
                               classname=f"{entry.classname}.{slug}"))
    return out


def with_names(entries, names):
    """The entries whose check name or file name (without its suffix) is one of ``names``; a check that can't be read
    is kept, so its error still shows."""
    if not names:
        return list(entries)
    wanted = {name.strip().casefold() for name in names}
    return [entry for entry in entries if entry.check is None
            or wanted & {entry.check.name.casefold(), Path(entry.file).stem.casefold(),
                         Path(entry.file).with_suffix("").as_posix().casefold()}]


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


def check_files(folder):
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
    """Every check under ``paths`` (files or folders), else ``root``/.mobster/checks, as Entries sorted by file."""
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
        files = check_files(folder)
        if not files:
            raise DiscoveryError(f"{_relative(folder, cwd.resolve())} has no checks (.yaml, .yml or .json files).")
    for given in paths or ():
        path = Path(given).expanduser()
        path = (path if path.is_absolute() else cwd / path).resolve()
        if not path.exists():
            raise DiscoveryError(f"{given} doesn't exist.")
        if path.is_dir():
            found = check_files(path)
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
            # A file that can't be read is kept with its error, so the suite reports it as couldn't run, not skipped.
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


def load_quarantine(path, *, required=False):
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
    # Every count-th check in file order: the shards split evenly and every machine running the same files agrees.
    return [entry for position, entry in enumerate(sorted(entries, key=lambda e: e.file))
            if position % count == index - 1]

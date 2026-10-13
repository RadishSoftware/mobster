"""Frozen L1 sets: versioned, hashed, split by task into a tune half and a report half.

A set lives in its own folder, outside the repository (its items hold task text and screens):

    <root>/<name>/<version>/items.jsonl    one item per line, sorted by id; each has "id", "task", "half"
    <root>/<name>/<version>/assets/        the screenshots items name, by content hash
    <root>/<name>/<version>/MANIFEST.json  counts, sources, the builder's commit, and "sha256"

``sha256`` covers items.jsonl and every asset's bytes. ``load`` refuses a set whose files no longer match it, so a
number reported on a set is a number on exactly that set. A change tuned on the tune half is reported on the report
half; the halves split tasks, not items, so no task's screens leak from one half into the other.

The halves come from one task-to-half map shared by every set (``make_halves``): tasks are dealt alternately within
strata (core or not, category) in the order of a seeded hash. Tasks in a split named in ``forbidden`` (val, test)
never enter a set.
"""

import datetime
import hashlib
import json
import pathlib
import subprocess

SEED = "20261010"
HALVES = ("tune", "report")


def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()


def make_halves(strata, seed=SEED):
    """{task: "tune" | "report"} from {task: stratum}: within each stratum, tasks in seeded-hash order alternate."""
    by = {}
    for task, stratum in strata.items():
        by.setdefault(str(stratum), []).append(task)
    out = {}
    for stratum in sorted(by):
        for i, task in enumerate(sorted(by[stratum], key=lambda t: _h(f"{seed}:{stratum}:{t}"))):
            out[task] = HALVES[i % 2]
    return out


def halves_from_splits(splits, extra=None, seed=SEED):
    """The halves of every dev task in a tuning splits file (core and category strata), plus ``extra``
    {task: stratum} for tasks outside it (another benchmark's)."""
    dev = splits["splits"]["dev"]
    core = set(splits["splits"].get("dev_core") or ())
    tasks = splits.get("tasks") or {}
    strata = {t: f"{'core' if t in core else 'dev'}:{(tasks.get(t) or {}).get('category')}" for t in dev}
    strata.update({t: f"extra:{s}" for t, s in (extra or {}).items()})
    return make_halves(strata, seed)


def forbidden_tasks(splits, names=("val", "test")):
    return {t for name in names for t in (splits["splits"].get(name) or ())}


def _git_head():
    try:
        here = pathlib.Path(__file__).resolve().parent
        return subprocess.run(["git", "-C", str(here), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class SetWriter:
    """Collects items and assets, then writes a frozen set. ``asset(path)`` copies a file in and returns its name."""

    def __init__(self, root, name, version, halves, forbidden=()):
        self.dir = pathlib.Path(root).expanduser() / name / version
        if (self.dir / "MANIFEST.json").exists():
            raise FileExistsError(f"{self.dir} is frozen; build a new version instead")
        self.name, self.version, self.halves, self.forbidden = name, version, halves, set(forbidden)
        self.items, self.assets, self.skipped = [], {}, {"forbidden": 0, "no_half": 0}

    def asset(self, path):
        if not path:
            return None
        path = pathlib.Path(path)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        name = hashlib.sha256(data).hexdigest()[:20] + path.suffix.lower()
        self.assets[name] = data
        return f"assets/{name}"

    def add(self, item):
        task = item["task"]
        if task in self.forbidden:
            self.skipped["forbidden"] += 1
            return False
        half = self.halves.get(task)
        if half is None:
            self.skipped["no_half"] += 1
            return False
        self.items.append({**item, "half": half})
        return True

    def write(self, sources=(), notes=""):
        ids = [i["id"] for i in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate item ids")
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "assets"
        tmp.mkdir(exist_ok=True)
        for name, data in self.assets.items():
            (tmp / name).write_bytes(data)
        lines = "".join(json.dumps(i, ensure_ascii=False, sort_keys=True) + "\n" for i in sorted(self.items,
                                                                                               key=lambda i: i["id"]))
        (self.dir / "items.jsonl").write_text(lines, encoding="utf-8")
        manifest = {"name": self.name, "version": self.version,
                    "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                    "builder_commit": _git_head(), "items": len(self.items),
                    "halves": {h: sum(i["half"] == h for i in self.items) for h in HALVES},
                    "tasks": {h: len({i["task"] for i in self.items if i["half"] == h}) for h in HALVES},
                    "assets": len(self.assets), "skipped": self.skipped, "sources": list(sources), "notes": notes,
                    "sha256": digest(self.dir)}
        (self.dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
        return manifest


def digest(set_dir):
    """sha256 over items.jsonl and every asset's name and bytes."""
    set_dir = pathlib.Path(set_dir)
    h = hashlib.sha256((set_dir / "items.jsonl").read_bytes())
    assets = set_dir / "assets"
    for path in sorted(assets.iterdir()) if assets.is_dir() else ():
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def load(set_dir, half=None, verify=True):
    """(manifest, items) of a frozen set, refusing one whose files no longer match its hash."""
    set_dir = pathlib.Path(set_dir).expanduser()
    manifest = json.loads((set_dir / "MANIFEST.json").read_text())
    if verify and digest(set_dir) != manifest["sha256"]:
        raise ValueError(f"{set_dir} no longer matches its MANIFEST sha256: rebuild it as a new version")
    items = [json.loads(line) for line in (set_dir / "items.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if half:
        items = [i for i in items if i["half"] == half]
    return manifest, items


def asset_path(set_dir, name):
    return pathlib.Path(set_dir).expanduser() / name if name else None

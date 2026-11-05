"""Device fixtures the operator sets up once (image sets and app records), with ground truth frozen in code."""

import hashlib
import json
import random
import shutil
from dataclasses import dataclass
from pathlib import Path

VISION_DIR = Path(__file__).resolve().parent.parent / "evals" / "vision"
ROOT_FOLDER = "MobsterBench"
FILES_PATH = ("Browse", "On My iPhone", ROOT_FOLDER)


@dataclass(frozen=True)
class ImageSetSpec:
    key: str             # e.g. "files.dogs12"
    folder: str          # folder under On My iPhone/MobsterBench
    predicate: str       # vision manifest predicate
    picks: tuple         # ((label, difficulty, count), ...)
    album: str = ""      # Photos album holding the same images, if any


IMAGE_SETS = (
    ImageSetSpec("files.dogs12", "dogs12", "dog",
                 (("yes", "normal", 4), ("yes", "hard", 1), ("no", "normal", 4), ("no", "hard", 3)),
                 album="MobsterBench Dogs"),
    ImageSetSpec("files.receipts12", "receipts12", "receipt",
                 (("yes", "normal", 5), ("yes", "hard", 1), ("no", "normal", 3), ("no", "hard", 3)),
                 album="MobsterBench Receipts"),
    ImageSetSpec("files.sunglasses12", "sunglasses12", "sunglasses",
                 (("yes", "normal", 4), ("yes", "hard", 2), ("no", "normal", 3), ("no", "hard", 3))),
    # 4 answerable eye colours and 4 where the only correct answer is "unsure".
    ImageSetSpec("files.eyes8", "eyes8", "eye_colour",
                 (("blue", "normal", 1), ("brown", "normal", 2), ("green", "normal", 1),
                  ("unsure", "normal", 2), ("unsure", "hard", 2))),
)

# Typed in by the operator. Content is deliberately fictional (555-01xx numbers
# are reserved for fiction) and contains no send/buy/call style wording that a
# safety rule could confuse with an action.
RECORDS = {
    "notes.bench": {
        "app": "notes", "title": "MobsterBench Note",
        "body": ["MobsterBench Note", "Locker code: 4817", "https://en.m.wikipedia.org/wiki/Alan_Turing"],
        "code": "4817",
        "verify": ("MobsterBench Note",),
        "setup": "Notes: create a note in the default 'Notes' folder (iCloud or On My iPhone) whose three "
                 "lines are exactly the body lines. Do not pin or lock it.",
    },
    "contacts.bench": {
        "app": "contacts", "first": "Bench", "last": "Tester", "company": "Mobster Bench Labs",
        "phone": "(555) 010-4477", "city": "Cupertino",
        "verify": ("Bench Tester",),
        "setup": "Contacts: new contact First 'Bench', Last 'Tester', Company 'Mobster Bench Labs', "
                 "phone (mobile) '(555) 010-4477', address city 'Cupertino'.",
    },
    "reminders.bench": {
        "app": "reminders", "list": "MobsterBench",
        "items": ["Water the plants", "Return library books", "Pick up dry cleaning"],
        "count": "3", "count_x7": "21",
        "verify": ("Water the plants", "Return library books", "Pick up dry cleaning"),
        "path": ("MobsterBench",),
        "setup": "Reminders: new list 'MobsterBench' with exactly these three incomplete reminders, "
                 "no dates, no flags.",
    },
    "calendar.bench": {
        "app": "calendar", "title": "MobsterBench Review", "date": "2026-10-15", "start": "14:00",
        "end": "15:00", "location": "Room 42",
        "verify": ("MobsterBench Review",),
        "setup": "Calendar: event 'MobsterBench Review' on Thursday 15 October 2026, 14:00-15:00 local "
                 "time, location 'Room 42', no invitees, no alerts.",
    },
}


def _vision_manifest():
    return json.loads((VISION_DIR / "manifest.json").read_text())


def select(spec, manifest=None):
    """Deterministic pick + order for one set. Items sharing an entity are avoided."""
    manifest = manifest or _vision_manifest()
    pool = sorted((item for item in manifest["items"]
                   if item["predicate"] == spec.predicate and item.get("entity") is None),
                  key=lambda item: item["id"])
    chosen = []
    for label, difficulty, count in spec.picks:
        group = [item for item in pool if item["label"] == label and item["difficulty"] == difficulty]
        if len(group) < count:
            raise ValueError(f"{spec.key}: need {count} {label}/{difficulty}, have {len(group)}")
        chosen.extend(group[:count])
    random.Random(f"mobsterbench-v1:{spec.key}").shuffle(chosen)
    rows = []
    for index, item in enumerate(chosen, 1):
        # A neutral file name, so the name on the phone never gives its label away.
        rows.append({"file": f"{spec.folder}-{index:02d}.jpg", "source_id": item["id"],
                     "source_file": item["file"], "label": item["label"], "accept": list(item["accept"]),
                     "sha256": hashlib.sha256((VISION_DIR / item["file"]).read_bytes()).hexdigest()})
    return rows


class Fixtures:
    """Frozen fixture ground truth. ``value(key, part)`` feeds the checks."""

    def __init__(self, manifest=None):
        manifest = manifest or _vision_manifest()
        self.sets = {spec.key: (spec, select(spec, manifest)) for spec in IMAGE_SETS}
        self.records = RECORDS

    def value(self, key, part):
        if key in self.sets:
            spec, rows = self.sets[key]
            positive = {"dog": "yes", "receipt": "yes", "sunglasses": "yes"}.get(spec.predicate)
            if part == "files":
                return [row["file"] for row in rows]
            if part == "positives":
                return [row["file"] for row in rows if row["label"] == positive]
            if part == "positive_count":
                return sum(1 for row in rows if row["label"] == positive)
            if part == "labels":
                return {row["file"]: row["label"] for row in rows}
            if part == "accept":
                return {row["file"]: row["accept"] for row in rows}
            if part.startswith("item:"):
                _, index, *attribute = part.split(":")
                row = rows[int(index) - 1]
                return row[attribute[0]] if attribute else row
        if key in self.records and part in self.records[key]:
            return self.records[key][part]
        raise KeyError(f"no fixture value {key}.{part}")

    def frozen(self):
        """Canonical description for the suite hash (labels + image digests + records)."""
        return {"image_sets": {key: {"folder": spec.folder, "album": spec.album,
                                     "rows": rows} for key, (spec, rows) in sorted(self.sets.items())},
                "records": {key: {k: v for k, v in rec.items()} for key, rec in sorted(self.records.items())}}

    def verify_path(self, key):
        """Labels the harness taps (read-only navigation) to reach a fixture, and strings it expects."""
        if key in self.sets:
            spec, rows = self.sets[key]
            return "files", (*FILES_PATH, spec.folder), [row["file"].rsplit(".", 1)[0] for row in rows]
        if key.startswith("photos."):
            spec = next(spec for spec, _ in self.sets.values() if spec.album and
                        spec.key.split(".", 1)[1] == key.split(".", 1)[1])
            return "photos", (spec.album,), [spec.album]
        record = self.records[key]
        return record["app"], tuple(record.get("path", ())), list(record["verify"])

    def build(self, out_dir):
        """Write the folders to copy to the phone, plus a labels file kept OFF the phone."""
        out = Path(out_dir)
        phone_root = out / "copy-to-phone" / ROOT_FOLDER
        for key, (spec, rows) in self.sets.items():
            folder = phone_root / spec.folder
            folder.mkdir(parents=True, exist_ok=True)
            for row in rows:
                shutil.copyfile(VISION_DIR / row["source_file"], folder / row["file"])
        albums = {spec.album: spec.folder for spec, _ in self.sets.values() if spec.album}
        (out / "labels-do-not-copy.json").write_text(json.dumps(self.frozen(), indent=1))
        lines = ["MobsterBench fixture setup (see mobile_agent/bench/README.md)", "",
                 f"1. Copy the folder copy-to-phone/{ROOT_FOLDER} to Files > On My iPhone (AirDrop or Finder).",
                 "2. In Photos, create these albums from the same images (import order does not matter):"]
        lines += [f"   - {album}: every image in {ROOT_FOLDER}/{folder}" for album, folder in albums.items()]
        lines += ["3. Create these records by hand:"]
        for rec in self.records.values():
            lines.append(f"   - {rec['setup']}")
            # The exact contents the setup sentence refers to.
            for label, key in (("Note lines", "body"), ("Reminders", "items")):
                if rec.get(key):
                    lines.append(f"     {label}:")
                    lines += [f"       {value}" for value in rec[key]]
        lines += ["4. Never copy labels-do-not-copy.json to the phone: it is the answer key."]
        (out / "SETUP.txt").write_text("\n".join(lines) + "\n")
        return out

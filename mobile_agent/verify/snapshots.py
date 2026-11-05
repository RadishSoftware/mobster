"""Screenshot baselines (docs/testing.mdx, "Screenshots that must not change"): a perceptual pixel diff, no model.

A check's ``snapshot:`` compares the screen it ends on to a PNG under ``.mobster/snapshots/``. Both images are blurred
by 2 px and a pixel counts as changed only when a colour channel moves by more than TOLERANCE, so antialiasing and
sub-pixel text never fail a check. Masked regions count in neither the changed pixels nor the total."""

import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .assertions import CheckError, parse_selector, reject_unknown_keys

BLUR_PX = 2
TOLERANCE = 24            # of 255, per colour channel, after the blur
DEFAULT_THRESHOLD = 0.1   # percent of the unmasked pixels that may differ
MASK_FILL = (128, 128, 128)
CELL_PX = 16              # regions are found on a grid this coarse
REGION_PAD_PX = 4
MAX_REGIONS = 20
STATUS_BAR_POINTS = 59    # the top of a Dynamic Island iPhone's screen, when the tree names no status bar
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,59}")
MASK_KEYS = ("id", "label", "rect")
MASK_LIMIT = 20
SNAPSHOT_STATUS_BAR = {"time": "9:41", "data_network": "wifi", "wifi_mode": "active", "wifi_bars": 3,
                       "cellular_mode": "active", "cellular_bars": 4, "battery_state": "charged",
                       "battery_level": 100}
TEXT_ENTRY = frozenset({"TextField", "SecureTextField", "SearchField", "TextView"})
FOLDER = "snapshots"

MATCHED, CHANGED, SIZE_CHANGED, RECORDED, UPDATED = "matched", "changed", "size_changed", "recorded", "updated"


@dataclass(frozen=True)
class Mask:
    """A region the diff ignores: the elements a selector matches, or a rectangle in points (x, y, width, height)."""
    selector: Optional[object] = None
    rect: Optional[tuple] = None

    def to_dict(self):
        return {"rect": list(self.rect)} if self.rect is not None else self.selector.to_dict()


@dataclass(frozen=True)
class Snapshot:
    """The ``snapshot:`` entry of a check's ``expect``."""
    name: str
    masks: tuple = ()
    threshold: float = DEFAULT_THRESHOLD   # percent

    def to_dict(self):
        out = {"snapshot": self.name}
        if self.masks:
            out["mask"] = [mask.to_dict() for mask in self.masks]
        if self.threshold != DEFAULT_THRESHOLD:
            out["threshold"] = self.threshold
        return out


def parse_mask(obj, where):
    if not isinstance(obj, dict):
        raise CheckError(f"{where} must be an object such as {{id: clock}}, {{label: Last sync}} or "
                         "{rect: [0, 100, 390, 40]}")
    reject_unknown_keys(obj, MASK_KEYS, where)
    if "rect" in obj:
        if len(obj) != 1:
            raise CheckError(f"{where}: rect goes alone, in points as [x, y, width, height]")
        rect = obj["rect"]
        if (not isinstance(rect, list) or len(rect) != 4 or any(
                isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
                for item in rect) or rect[2] <= 0 or rect[3] <= 0):
            raise CheckError(f"{where}.rect must be four numbers in points: x, y, width and height (the last two "
                             "above 0)")
        return Mask(rect=tuple(float(item) for item in rect))
    if not obj:
        raise CheckError(f"{where} needs id, label or rect")
    return Mask(selector=parse_selector(obj, where))


def parse_snapshot(obj, where="snapshot"):
    """The ``snapshot`` entry of ``expect``. Raises CheckError."""
    reject_unknown_keys(obj, ("snapshot", "mask", "threshold"), where)
    name = obj.get("snapshot")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise CheckError(f"{where}.snapshot is the screen's name, 1 to 60 of a-z, 0-9, - and _, such as home; it "
                         "names the baseline file")
    masks = obj.get("mask", [])
    if not isinstance(masks, list):
        raise CheckError(f"{where}.mask must be a list, such as [{{id: clock}}]")
    if len(masks) > MASK_LIMIT:
        raise CheckError(f"{where}.mask has more than {MASK_LIMIT} entries")
    threshold = obj.get("threshold", DEFAULT_THRESHOLD)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 100 \
            or not math.isfinite(threshold):
        raise CheckError(f"{where}.threshold is the percent of the screen that may change, from 0 to 100 "
                         f"(default {DEFAULT_THRESHOLD:g})")
    return Snapshot(name, tuple(parse_mask(item, f"{where}.mask[{index}]") for index, item in enumerate(masks)),
                    float(threshold))


# -- where baselines live ---------------------------------------------------------------------------------------

def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "default"


def check_key(source, root, name=""):
    """The folder a check's baselines live in: its file's path under .mobster/checks (or the project), without the
    suffix; the check's name for a check with no file."""
    if source and root:
        path = Path(source)
        for base in (Path(root) / ".mobster" / "checks", Path(root)):
            try:
                return path.resolve().relative_to(base.resolve()).with_suffix("").as_posix()
            except ValueError:
                continue
    return Path(source).stem if source else slug(name)


def variant(device, conditions):
    """``iphone-17-pro-dark-accessibility-large-de_DE``: the device and the three conditions a baseline is for."""
    conditions = conditions or {}
    parts = [slug(device)] + [str(conditions.get(key) or "default") for key in ("appearance", "content_size", "locale")]
    return "-".join(part.replace("/", "_") for part in parts)


def baseline_file(root, key, name, device, conditions):
    return Path(root) / ".mobster" / FOLDER / key / f"{name}@{variant(device, conditions)}.png"


# -- the diff ---------------------------------------------------------------------------------------------------

@dataclass
class Comparison:
    status: str
    changed_pixels: int = 0
    compared_pixels: int = 0
    regions: list = field(default_factory=list)   # [x, y, w, h] in pixels
    diff: object = None                           # a PIL image, only for a change
    seconds: float = 0.0

    @property
    def changed_percent(self):
        return 100.0 * self.changed_pixels / self.compared_pixels if self.compared_pixels else 0.0


def compare(baseline, current, masks=(), threshold=DEFAULT_THRESHOLD):
    """Compare two PIL images. ``masks`` are [x, y, w, h] pixel rectangles. Status: matched, changed or size_changed."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    started = time.perf_counter()
    if baseline.size != current.size:
        return Comparison(SIZE_CHANGED, seconds=time.perf_counter() - started)
    first, second = baseline.convert("RGB"), current.convert("RGB")
    counted = Image.new("L", current.size, 255)
    for x, y, w, h in masks:
        box = [round(x), round(y), round(x + w) - 1, round(y + h) - 1]
        for image in (first, second):
            ImageDraw.Draw(image).rectangle(box, fill=MASK_FILL)   # the same in both, so the blur leaves no halo
        ImageDraw.Draw(counted).rectangle(box, fill=0)
    red, green, blue = ImageChops.difference(first.filter(ImageFilter.GaussianBlur(BLUR_PX)),
                                             second.filter(ImageFilter.GaussianBlur(BLUR_PX))).split()
    moved = ImageChops.lighter(ImageChops.lighter(red, green), blue).point([255 if v > TOLERANCE else 0
                                                                           for v in range(256)])
    changed = moved.histogram()[255]
    compared = counted.histogram()[255]
    percent = 100.0 * changed / compared if compared else 0.0
    if percent <= threshold:
        return Comparison(MATCHED, changed, compared, seconds=time.perf_counter() - started)
    regions = _regions(moved)
    return Comparison(CHANGED, changed, compared, regions, _diff_image(current, moved, regions),
                      time.perf_counter() - started)


def _regions(moved):
    """Boxes around the changed pixels: neighbouring cells of a CELL_PX grid join, each box padded a little."""
    from PIL import Image
    width, height = moved.size
    columns, rows = -(-width // CELL_PX), -(-height // CELL_PX)
    grid = moved.resize((columns, rows), Image.BOX).point([255 if v else 0 for v in range(256)])
    data = grid.load()
    cells = {(x, y) for y in range(rows) for x in range(columns) if data[x, y]}
    boxes = []
    while cells:
        start = cells.pop()
        stack, low, high = [start], list(start), list(start)
        while stack:
            cx, cy = stack.pop()
            for nx in (cx - 1, cx, cx + 1):
                for ny in (cy - 1, cy, cy + 1):
                    if (nx, ny) in cells:
                        cells.discard((nx, ny))
                        stack.append((nx, ny))
            low[0], low[1] = min(low[0], cx), min(low[1], cy)
            high[0], high[1] = max(high[0], cx), max(high[1], cy)
        left, top = max(0, low[0] * CELL_PX - REGION_PAD_PX), max(0, low[1] * CELL_PX - REGION_PAD_PX)
        right = min(width, (high[0] + 1) * CELL_PX + REGION_PAD_PX)
        bottom = min(height, (high[1] + 1) * CELL_PX + REGION_PAD_PX)
        boxes.append([left, top, right - left, bottom - top])
    boxes.sort(key=lambda box: (box[1], box[0]))
    if len(boxes) > MAX_REGIONS:
        left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
        boxes = [[left, top, max(b[0] + b[2] for b in boxes) - left, max(b[1] + b[3] for b in boxes) - top]]
    return boxes


def _diff_image(current, moved, regions):
    """The current screen faded, the changed pixels red, a box around each region."""
    from PIL import Image, ImageDraw
    faded = Image.blend(current.convert("RGB"), Image.new("RGB", current.size, "white"), .6)
    faded.paste(Image.new("RGB", current.size, (230, 30, 30)), mask=moved)
    draw = ImageDraw.Draw(faded)
    line = max(2, current.width // 300)
    for x, y, w, h in regions:
        draw.rectangle([x, y, x + w - 1, y + h - 1], outline=(230, 30, 30), width=line)
    return faded


# -- masks from the tree ----------------------------------------------------------------------------------------

def mask_rects(snapshot, tree, pixel_width, *, status_bar_set):
    """Every masked region as [x, y, w, h] pixels: the check's masks, the keyboard (and the fields, whose caret blinks,
    while it shows) and, when the status bar isn't set, the status bar."""
    scale = pixel_width / tree.size[0] if tree.size and tree.size[0] else 1.0
    shown = tree.shown()
    rects = []

    def add(rect):
        x, y, w, h = rect
        rects.append([x * scale, y * scale, w * scale, h * scale])
    for mask in snapshot.masks:
        if mask.rect is not None:
            add(mask.rect)
        else:
            for node in shown:
                if mask.selector.matches(node):
                    add(node.rect)
    if any(node.role == "Keyboard" for node in shown):
        for node in shown:
            if node.role == "Keyboard" or node.role in TEXT_ENTRY:
                add(node.rect)
    if not status_bar_set:
        bars = [node for node in shown if node.role == "StatusBar"]
        for node in bars:
            add(node.rect)
        if not bars:
            add((0, 0, tree.size[0], STATUS_BAR_POINTS))
    return rects


def save_png(image, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    image.save(temporary, format="PNG")
    os.replace(temporary, path)


# -- listing baselines ------------------------------------------------------------------------------------------

BASELINE_NAME = re.compile(r"(?P<name>[a-z0-9][a-z0-9_-]*)@(?P<variant>.+)\.png")


def baselines(root):
    """Every baseline under .mobster/snapshots: [{file, check, name, variant}], sorted."""
    base = Path(root) / ".mobster" / FOLDER
    out = []
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*.png")):
        found = BASELINE_NAME.fullmatch(path.name)
        if found is None:
            continue
        out.append({"file": path.relative_to(root).as_posix(),
                    "check": path.parent.relative_to(base).as_posix(), "name": found["name"],
                    "variant": found["variant"]})
    return out


def audit(root, checks):
    """Mark each baseline in a project. ``checks``: [(check key, Check)] of the project's checks.
    orphaned: no check has that key or snapshot name. stale: the check has the snapshot, but not for this appearance,
    text size or locale (a check without conditions lists "default" in each)."""
    from . import conditions as conditions_module
    wanted = {}
    for key, check in checks:
        spec = getattr(check, "snapshot", None)
        if spec is None:
            continue
        combos = conditions_module.combinations(check.conditions)
        wanted.setdefault((key, spec.name), set()).update(
            "-".join(str(combo.get(k) or "default") for k in ("appearance", "content_size", "locale"))
            for combo in combos)
    out = []
    for item in baselines(root):
        sets = wanted.get((item["check"], item["name"]))
        if sets is None:
            state = "orphaned"
        elif not any(item["variant"].endswith("-" + tail) for tail in sets):
            state = "stale"
        else:
            state = "current"
        out.append({**item, "state": state})
    return out

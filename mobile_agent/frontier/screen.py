"""The screen as the agent reads it: its picture, its rows, the lists on it and its signature."""

import base64
import io
import json
import os
import re
import threading

from .. import contract_screen as proof_screen
from ..task_policy import is_keypad_key
from .protocol import TYPED_OPS

SCREENSHOT_SIDE = 448
MAX_ELEMENTS = 220
VERIFY_TIMEOUT = 10


# Never a frame older than ``since``: the picture is never older than the tree it goes with.
def video_frame(driver, still_seconds=0.0, since=None):
    clock = getattr(driver, "frame_clock", None)
    if clock is None:
        return None
    try:
        still = clock.still_for() if callable(getattr(clock, "still_for", None)) else None
        latest = getattr(getattr(clock, "video", None), "latest", None)
        frame = latest() if callable(latest) and still is not None and still >= still_seconds else None
    except Exception:
        return None
    if isinstance(frame, (tuple, list)) and len(frame) > 2 and frame[1] == "image/jpeg":
        if since is not None and not (len(frame) > 4 and isinstance(frame[4], (int, float)) and frame[4] >= since):
            return None
        return frame[2]
    return None


def screen_image(driver, since=None):
    data = video_frame(driver, since=since)
    capture = getattr(driver, "capture_preview", None)
    if data is None and (not callable(capture) or getattr(driver, "ax_only_steps", 0)):
        return None
    try:
        if data is None:
            data = capture(timeout=5)
        if isinstance(data, str):
            data = base64.b64decode(data.split(",", 1)[1] if data.startswith("data:") else data)
        from PIL import Image
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None


def screenshot_part(driver, side=SCREENSHOT_SIDE, since=None, blur=()):
    image = screen_image(driver, since)
    if image is None:
        return None
    try:
        image.thumbnail((side, side * 2.2))
        if blur:
            image = blurred(image, blur)
        out = io.BytesIO()
        image.save(out, "JPEG", quality=70)
    except Exception:
        return None
    url = "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()
    return {"type": "image_url", "image_url": {"url": url, "detail": "low"}}


POINT_CROP = (1, .18)  # its whole width, so a typed line shows whole
POINT_CROP_SIDE = 360


def point_crop(driver, x, y, since=None, blur=()):
    image = screen_image(driver, since)
    if image is None:
        return None
    try:
        from PIL import ImageDraw
        if blur:
            image = blurred(image, blur)
        width, height = image.size
        w, h = POINT_CROP[0] * width, POINT_CROP[1] * height
        left = min(max(x * width - w / 2, 0), width - w)
        top = min(max(y * height - h / 2, 0), height - h)
        image = image.crop((int(left), int(top), int(left + w), int(top + h)))
        scale = POINT_CROP_SIDE / max(image.size)
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))))
        cx, cy = (x * width - left) * scale, (y * height - top) * scale
        ring = max(10, round(POINT_CROP_SIDE * .045))
        draw = ImageDraw.Draw(image)
        draw.ellipse((cx - ring - 2, cy - ring - 2, cx + ring + 2, cy + ring + 2), outline=(255, 255, 255), width=2)
        draw.ellipse((cx - ring, cy - ring, cx + ring, cy + ring), outline=(255, 92, 72), width=3)
        out = io.BytesIO()
        image.save(out, "JPEG", quality=80)
        return out.getvalue()
    except Exception:
        return None


def blurred(image, rects):
    from PIL import ImageStat
    width, height = image.size
    for x, y, w, h in rects:
        box = (max(0, int((x - .01) * width)), max(0, int((y - .005) * height)),
               min(width, int((x + w + .01) * width) + 1), min(height, int((y + h + .005) * height) + 1))
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        mean = tuple(int(v) for v in ImageStat.Stat(image.crop(box)).mean[:3])
        image.paste(mean, box)
    return image


# The driver takes one request at a time: nothing else uses it until result() joins this read.
class _Verify:
    def __init__(self, driver):
        self.snapshot = None
        self._thread = threading.Thread(target=self._read, args=(driver,), name="frontier-verify", daemon=True)
        self._thread.start()

    def _read(self, driver):
        try:
            self.snapshot = driver.observe(timeout=VERIFY_TIMEOUT)
        except Exception:
            pass

    def result(self):
        self._thread.join()
        return self.snapshot


# An app's own keypad is listed: left out as keyboard keys, Calculator's keys could not be tapped (B5).
def lean_skip(elements, keyboard=None):
    app_keys = keyboard is not None and keyboard != "visible" and any(
        element.role == "Key" and is_keypad_key("TAP", "Key", element.label) for element in elements)
    return set() if app_keys else {index for index, element in enumerate(elements) if element.role == "Key"}


KEYBOARD_ROW = "(keyboard up: its keys are not listed; TYPE and SET_TEXT type, SUBMIT presses Return)"


def context_on(name):
    return os.environ.get(f"MOBSTER_CTX_{name}", "off").strip().casefold() == "on"


FIELD_PROMPT = re.compile(r"^(search|ask|type|write|enter|where to)\b|\bsearch\b", re.I)
UNDER_BAR_MARK = "(hidden under the bottom bar)"
BAR_ROLES = frozenset({"TabBar", "Toolbar", "NavigationBar"})


def looks_like_field(element):
    words = (element.label or "").split()
    return (element.role == "Button" and not element.editable and 2 <= len(words) <= 8
            and bool(FIELD_PROMPT.search(" ".join(words))))


def _bars(elements, width, height):
    from ..state import chrome_bands
    nodes = [(f"/{index}", e.role, e.label, e.value,
              (e.rect[0] * width, e.rect[1] * height,
               (e.rect[0] + e.rect[2]) * width, (e.rect[1] + e.rect[3]) * height),
              e.enabled) for index, e in enumerate(elements)]
    return nodes, (chrome_bands(nodes, width, height) if width >= 100 else ())  # bars are found in points


# A row listed before a bottom bar is drawn behind it, and taps on it do nothing (multi-087).
def under_bars(elements, width, height):
    from ..state import visible_span
    nodes, bands = _bars(elements, width, height)
    return {elements[index].id for index, node in enumerate(nodes)
            if bands and elements[index].role not in BAR_ROLES
            and visible_span(node[0], index, *node[4], bands) is None}


def chrome_ids(elements, width, height):
    nodes, bands = _bars(elements, width, height)
    owners = {int(path[1:]) for band in bands for path in band[0] if path[1:].isdigit()}
    frames = [e.rect for e in elements if e.role in BAR_ROLES] + [elements[i].rect for i in owners]
    out = {elements[i].id for i in owners}
    for e in elements:
        cx, cy = e.rect[0] + e.rect[2] / 2, e.rect[1] + e.rect[3] / 2
        if e.role in BAR_ROLES or any(x <= cx <= x + w and y <= cy <= y + h for x, y, w, h in frames):
            out.add(e.id)
    return out


WORDY = re.compile(r"[^\W\d_]{2}")


def content_rows(elements, width, height):
    chrome = chrome_ids(elements, width, height)
    out = {}
    for e in elements:
        label = " ".join((e.label or "").split())
        if e.id not in chrome and not e.editable and WORDY.search(label):
            out[label] = out.get(label, 0) + 1
    return out


LIST_MIN_ROWS, LIST_MIN_WIDTH = 3, .4
LIST_ROLES = frozenset({"Cell", "Button", "StaticText", "Link", "Other"})


def list_rows(elements, width, height):
    rows = content_rows(elements, width, height)
    groups, place = {}, {}
    for e in elements:
        label = " ".join((e.label or "").split())
        if label in rows and e.role in LIST_ROLES:
            place.setdefault(label, e.rect[1])
            if e.rect[2] >= LIST_MIN_WIDTH:
                shape = (e.role, round(e.rect[0] * 50), round(e.rect[2] * 50))
                groups.setdefault(shape, {}).setdefault(label, e.rect[1])
    stacked = [group for group in groups.values() if len({round(y * 100) for y in group.values()}) >= LIST_MIN_ROWS]
    if not stacked:
        return []
    top, bottom = min(min(g.values()) for g in stacked), max(max(g.values()) for g in stacked)
    return [label for label, y in sorted(place.items(), key=lambda kv: kv[1]) if top <= y <= bottom]


def list_name(snapshot, width, height):
    title = proof_screen.screen_title(snapshot)
    chrome = chrome_ids(snapshot.elements, width, height)
    chosen = next((" ".join(e.label.split()) for e in snapshot.elements if e.selected and e.id not in chrome
                   and e.role == "Button" and (e.label or "").strip() and e.label.strip() != title), None)
    return f"{title} › {chosen}" if title and chosen else title


def view_of(app, snapshot):
    return app or "", proof_screen.screen_title(snapshot)


def names_act(op, element, text=None):
    if op in ("TAP", "LONG_PRESS", "TAP_XY"):
        return bool(proof_screen.ACT_LABEL.search((element.label if element is not None else text) or ""))
    if op in TYPED_OPS or op == "SUBMIT":
        return element is None or not proof_screen.is_search_field(element)
    return False


_DONE = (r"(added|saved|sent|booked|confirmed|created|updated|deleted|removed|completed|scheduled|posted|submitted|"
         r"reserved|paid|placed)")
STATUS = re.compile(rf"\bsuccess(ful|fully)?\b|^{_DONE}( (to|in|for|on)\b.*)?[.!]?$|^\w+ {_DONE}[.!]?$", re.I)
PROVENANCE_MARK = "(on screen since this page opened, before any action of yours here)"


# A page can open showing a status the agent never wrote ("Added successfully", multi-069).
class Provenance:
    def __init__(self):
        self.view, self.texts, self.with_page = None, frozenset(), frozenset()

    def observe(self, view, elements, acted):
        texts = frozenset(" ".join((e.label or "").split()) for e in elements if e.label)
        self.with_page = (frozenset() if acted else texts - self.texts) if view != self.view else self.with_page & texts
        self.view, self.texts = view, texts

    def marks(self, elements):
        out = {}
        for e in elements:
            label = " ".join((e.label or "").split())
            if (e.role in ("StaticText", "Other") and label in self.with_page and 2 <= len(label.split()) <= 6
                    and STATUS.search(label)):
                out[e.id] = PROVENANCE_MARK
        return out


READBACK_ROWS = 4


# An app can leave the list a commit changed; reading it back stops a second add (caltrack-001).
def readback(title, before, after):
    changes = []
    for label in [*after, *(label for label in before if label not in after)]:
        now, was = after.get(label, 0), before.get(label, 0)
        if now != was:
            changes.append(f"{json.dumps(label[:60], ensure_ascii=False)} {now} row{'' if now == 1 else 's'} "
                           f"({was} before)")
    if not changes or len(changes) > READBACK_ROWS:
        return ""  # a scroll changes every row: no write to read back
    return f"read back from {title}: " + "; ".join(changes)


KEPT_ROWS, KEPT_CHARS, KEPT_LISTS = 5, 30, 3
IDENTIFIER = re.compile(r"^\S*_\S*(?:\s+\d+)?$|^[a-z][a-z0-9]*(?:\.[a-z0-9]+)+$")  # mail_row_x 1, gearshape.fill


def kept_list(title, rows):
    rows = [row for row in dict.fromkeys(" ".join(str(row).split()) for row in rows)
            if row != title and len(row.split()) >= 2 and not IDENTIFIER.match(row)]
    cut = [row[:KEPT_CHARS] + ("…" if len(row) > KEPT_CHARS else "") for row in rows[:KEPT_ROWS]]
    more = f"; {len(rows) - KEPT_ROWS} more" if len(rows) > KEPT_ROWS else ""
    return f"{title or 'a list'}: " + "; ".join(cut) + more if cut else ""


def text_status(receipt):
    if receipt.get("value") is None:
        return "not read back"
    chars = f"{len(receipt['value']):,} chars"
    if receipt.get("matches"):
        return chars + (", matches what you wrote (line breaks became spaces: one-line field)"
                        if receipt.get("flattened") else ", matches what you wrote")
    return chars + ", DIFFERS from what you wrote: " + text_difference(receipt["value"], receipt.get("expected") or "")


def text_difference(value, expected):
    index = next((i for i, (a, b) in enumerate(zip(value, expected)) if a != b), min(len(value), len(expected)))
    return (f"from character {index + 1} it reads {value[index:index + 40]!r} where you wrote "
            f"{expected[index:index + 40]!r} ({len(value)} vs {len(expected)} characters)")


def receipt_for(receipts, element):
    receipt = (receipts or {}).get(element.locator)
    written = receipt.get("value") if receipt else None
    if written is None:
        return None
    from ..state import truncated_field_value
    shown = element.text if element.placeholder else (element.value or "")
    same = reads_back(shown, written) if truncated_field_value(shown) else shown == written
    return receipt if same else None


# WDA reads a long value as its head and tail around " … ": both must be the text's.
def reads_back(shown, text):
    from ..state import FIELD_HEAD, FIELD_TAIL, truncated_field_value
    if truncated_field_value(shown):
        return (len(text) > FIELD_HEAD + FIELD_TAIL and text.startswith(shown[:FIELD_HEAD])
                and text.endswith(shown[-FIELD_TAIL:]))
    return _plain(shown) == _plain(text)


QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"', "‟": '"'})


def _plain(text):
    return " ".join(text.casefold().translate(QUOTES).split())


def screen_rows(snapshot, receipts=None, marks=None):
    from ..state import redundant_ids
    skip = redundant_ids(snapshot.public().get("elements") or ())
    shown = [e for e in snapshot.elements if e.id not in skip]
    lean = lean_skip(shown, getattr(snapshot, "keyboard", None))
    marking = context_on("MARKS")
    under = under_bars(shown, snapshot.width, snapshot.height) if marking else frozenset()
    aliases, lines = {}, []
    for element in [e for index, e in enumerate(shown) if index not in lean][:MAX_ELEMENTS]:
        alias = f"e{len(aliases) + 1}"
        aliases[alias] = element
        x, y, w, h = (round(v * 100) for v in element.rect)
        value = (element.text if element.placeholder else element.value or "").strip()
        if element.role in ("Switch", "Toggle") and value in ("0", "1"):
            value = "on" if value == "1" else "off"
        parts = [alias, element.role, json.dumps((element.label or "")[:160], ensure_ascii=False)
                 if element.label else "(no label: find it by position in the screenshot)"]
        receipt = receipt_for(receipts, element) if element.editable else None
        if receipt is not None and len(value) > 160:
            # A read-back field shows its status, not a cut window: a cut value got a note retyped 9 times (mem-030).
            parts.append("value=" + json.dumps(value[:80] + " …", ensure_ascii=False))
        elif value and value != element.label:
            if element.editable and len(value) > 400:
                # Typing lands at a long field's end, so its end is shown (clouddocs-004).
                value = value[:120] + " … " + value[-280:]
            else:
                value = value[:400]
            parts.append("value=" + json.dumps(value, ensure_ascii=False))
        elif element.editable and element.placeholder and not value:
            parts.append("empty (placeholder " + json.dumps(element.placeholder[:60], ensure_ascii=False) + ")")
        if receipt is not None:
            parts.append("(" + text_status(receipt) + ")")
        if not element.enabled:
            parts.append("disabled")
        if element.selected:
            parts.append("selected")
        if element.editable:
            parts.append("editable")
        elif marking and looks_like_field(element):
            parts.append("(not editable)")
        if element.id in under:
            parts.append(UNDER_BAR_MARK)
        if marks and element.id in marks:
            parts.append(marks[element.id])
        parts.append(f"@{x},{y},{w},{h}")
        lines.append(" ".join(parts))
    if any(shown[index].role == "Key" for index in lean):
        lines.append(KEYBOARD_ROW)
    return aliases, lines


def screen_shape_of(snapshot):
    from ..state import screen_shape, signature
    try:
        return screen_shape(signature(snapshot))
    except Exception:
        return []


def screen_pairs(snapshot):
    from ..state import signature
    try:
        return frozenset(tuple(pair) for pair in signature(snapshot))
    except Exception:
        return None


def screen_signature(snapshot):
    from ..state import signature, signature_hash
    try:
        pairs = signature(snapshot)
    except Exception:
        return ""
    return signature_hash(pairs)


def editable_in(snapshot, element):
    x, y, w, h = element.rect
    inside = [e for e in snapshot.elements if e.editable and x <= e.center[0] <= x + w and y <= e.center[1] <= y + h]
    if len(inside) == 1:
        return inside[0]
    fields = [e for e in snapshot.elements if e.editable]
    return fields[0] if len(fields) == 1 else None

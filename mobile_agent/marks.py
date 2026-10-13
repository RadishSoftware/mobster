"""Set-of-Mark: numbered boxes on the screenshot, one per element a decision model may pick.

A decision model answers "which mark" and "which gesture"; code turns the mark back into the exact element (its
AX frame's centre when it taps), so the model never writes a coordinate. Surfaces without an accessibility tree
(a map, a canvas) get a numbered grid instead (``grid_marks``): a cell is a choice like any mark.

``Mark`` keeps the element; ``mark_lines`` is the same list as text, for a text-only decider (Jev) and so both
deciders get identical options.
"""

import base64
import io
from dataclasses import dataclass

# Roles a decider may target. Static text is readable, not a target; containers hold targets.
TARGET_ROLES = frozenset({"Button", "Cell", "Link", "TextField", "SearchField", "SecureTextField", "TextView",
                          "Switch", "Toggle", "Tab", "TabBar", "MenuItem", "Image", "Icon", "SegmentedControl",
                          "Key", "PickerWheel", "Slider", "Stepper", "Other", "StaticText"})
MAX_MARKS = 120
GRID = (4, 8)   # columns, rows of the fallback grid


@dataclass(frozen=True)
class Mark:
    id: str
    element: object          # the element, or None for a grid cell
    rect: tuple              # x, y, w, h as screen fractions
    label: str
    role: str

    @property
    def center(self):
        x, y, w, h = self.rect
        return x + w / 2, y + h / 2


def element_marks(elements, limit=MAX_MARKS):
    """Marks for the targetable elements on screen, top to bottom, ids "1", "2"...; at most ``limit``."""
    out = []
    for element in elements:
        if len(out) >= limit:
            break
        x, y, w, h = element.rect
        if w <= 0 or h <= 0 or y >= 1 or y + h <= 0:
            continue
        if element.role not in TARGET_ROLES and not getattr(element, "editable", False):
            continue
        out.append(Mark(str(len(out) + 1), element, tuple(element.rect), (element.label or "")[:120], element.role))
    return out


def grid_marks(columns=GRID[0], rows=GRID[1]):
    """A numbered grid over the whole screen: "g1".."gN", left to right, top to bottom."""
    w, h = 1 / columns, 1 / rows
    return [Mark(f"g{r * columns + c + 1}", None, (c * w, r * h, w, h), f"grid cell row {r + 1} column {c + 1}", "Cell")
            for r in range(rows) for c in range(columns)]


def mark_lines(marks):
    """The marks as text: ``7 Button "Search" @4,14,92,5`` (frame in percent)."""
    lines = []
    for mark in marks:
        x, y, w, h = (round(v * 100) for v in mark.rect)
        label = f'"{mark.label}"' if mark.label else "(no label)"
        value = getattr(mark.element, "value", "") if mark.element is not None else ""
        value = f" value={str(value)[:40]!r}" if value and value != mark.label else ""
        lines.append(f"{mark.id} {mark.role} {label}{value} @{x},{y},{w},{h}")
    return lines


def options(marks, *, abstain=True):
    """A choice question's options for these marks (value = mark id), plus "none"."""
    out = {mark.id: line for mark, line in zip(marks, mark_lines(marks))}
    if abstain:
        out["none"] = "None of the marked elements is right"
    return out


def draw(jpeg, marks, side=None):
    """The screenshot (JPEG bytes) with each mark's box and number drawn; JPEG bytes back. ``side``: downscale so
    the width is at most ``side`` pixels first (390 for the token diet)."""
    from PIL import Image, ImageDraw, ImageFont
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if side and image.width > side:
        image = image.resize((side, round(image.height * side / image.width)))
    canvas = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=max(10, image.width // 36))
    except TypeError:
        font = ImageFont.load_default()
    W, H = image.size
    for mark in marks:
        x, y, w, h = mark.rect
        box = (int(x * W), int(y * H), int((x + w) * W), int((y + h) * H))
        colour = (255, 40, 120) if mark.element is not None else (40, 120, 255)
        canvas.rectangle(box, outline=colour, width=2)
        text = mark.id
        tw, th = canvas.textbbox((0, 0), text, font=font)[2:]
        tag = (box[0], max(0, box[1] - th - 2), box[0] + tw + 4, max(th + 2, box[1]))
        canvas.rectangle(tag, fill=colour)
        canvas.text((tag[0] + 2, tag[1]), text, fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=70)
    return out.getvalue()


def data_url(jpeg):
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()

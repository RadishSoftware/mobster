"""Explicit screenshot-coordinate gestures shared by the native planner and MCP surfaces."""

import base64
import io
from dataclasses import dataclass
from typing import Literal

_COORDINATE = {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 100,
               "description": "screen percentage from the latest screenshot"}
_LABEL = {"type": "string", "minLength": 1, "maxLength": 200,
          "description": "the visible control at this endpoint"}
_ENDPOINTS = {**{name: dict(_COORDINATE) for name in ("start_x", "start_y", "end_x", "end_y")},
              **{name: dict(_LABEL) for name in ("start_label", "end_label")}}


def _schema(properties, required):
    return {"type": "object", "additionalProperties": False, "required": list(required), "properties": properties}


HOLD_SCHEMA = _schema({"x": dict(_COORDINATE), "y": dict(_COORDINATE), "label": dict(_LABEL),
                       "duration": {"type": "number", "minimum": .1, "maximum": 10,
                                    "description": "hold duration in seconds (default 1.5)"}}, ("x", "y", "label"))
FLICK_SCHEMA = _schema({**_ENDPOINTS,
                        "duration": {"type": "number", "minimum": .08, "maximum": .3,
                                     "description": "movement duration in seconds (default 0.12)"}}, _ENDPOINTS)
DRAG_SCHEMA = _schema({**_ENDPOINTS,
                       "duration": {"type": "number", "minimum": .1, "maximum": 2,
                                    "description": "movement duration in seconds (default 1)"},
                       "hold_seconds": {"type": "number", "minimum": 0, "maximum": 2,
                                        "description": "hold before moving in seconds (default 0.1)"}}, _ENDPOINTS)
GESTURE_SCHEMAS = {"hold": HOLD_SCHEMA, "flick": FLICK_SCHEMA, "drag": DRAG_SCHEMA}
GESTURE_OPS = {"HOLD_XY": "hold", "FLICK_XY": "flick", "DRAG_XY": "drag"}


@dataclass(frozen=True)
class PointGesture:
    kind: Literal["hold", "flick", "drag"]
    start: tuple[float, float]
    end: tuple[float, float] | None
    start_label: str
    end_label: str | None
    duration: float
    hold_seconds: float = 0

    @property
    def points(self):
        return (self.start,) if self.end is None else (self.start, self.end)

    @property
    def label(self):
        return self.start_label if self.end is None else f"{self.start_label} to {self.end_label}"

    @property
    def operation(self):
        return self.kind.upper() + "_XY"


def _number(value, minimum, maximum, name):
    # Chained comparisons reject NaN and infinities without coercing booleans or strings.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g} seconds.")
    return float(value)


def validate_gesture_request(kind, value):
    """Normalize explicit percentages once, with a stationary hold represented by no end point."""
    schema = GESTURE_SCHEMAS[kind]
    if (not isinstance(value, dict) or not set(schema["required"]) <= value.keys()
            or value.keys() - schema["properties"].keys()):
        raise ValueError(f"A {kind} needs {', '.join(schema['required'])}; coordinates are screen percentages.")
    coordinate_keys = ("x", "y") if kind == "hold" else ("start_x", "start_y", "end_x", "end_y")
    coordinates = [value[name] for name in coordinate_keys]
    if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not 0 < n < 100 for n in coordinates):
        raise ValueError("Gesture coordinates must be finite screen percentages strictly between 0 and 100.")
    label_keys = ("label",) if kind == "hold" else ("start_label", "end_label")
    labels = [value[name] for name in label_keys]
    if any(not isinstance(label, str) or not label.strip() or len(label) > 200 for label in labels):
        raise ValueError("Name each visible gesture endpoint in 1 to 200 characters.")
    bounds = schema["properties"]["duration"]
    default = {"hold": 1.5, "flick": .12, "drag": 1}[kind]
    duration = _number(value.get("duration", default), bounds["minimum"], bounds["maximum"], "Duration")
    hold = _number(value.get("hold_seconds", .1), 0, 2, "Hold before moving") if kind == "drag" else 0
    start = tuple(n / 100 for n in coordinates[:2])
    end = tuple(n / 100 for n in coordinates[2:]) if kind != "hold" else None
    if start == end:
        raise ValueError(f"A {kind} needs different start and end points.")
    return PointGesture(kind, start, end, labels[0].strip(), labels[1].strip() if end else None, duration, hold)


def _crosses(start, end, rect):
    """Intersect a line segment and closed rectangle, including its endpoints."""
    lower, upper = 0.0, 1.0
    for origin, destination, edge, size in zip(start, end, rect[:2], rect[2:]):
        delta = destination - origin
        if not delta:
            if not edge <= origin <= edge + size:
                return False
            continue
        first, last = sorted(((edge - origin) / delta, (edge + size - origin) / delta))
        lower, upper = max(lower, first), min(upper, last)
        if lower > upper:
            return False
    return True


def check_gesture_path(snapshot, gesture):
    """Refuse known committing or sensitive controls at a hold point or anywhere along a movement."""
    from .. import contract_screen
    from .gestures import DRAG_SENSITIVE_ROLES
    for element in snapshot.elements:
        sensitive = (element.role in contract_screen.SWITCH_ROLES
                     or element.role in DRAG_SENSITIVE_ROLES or element.role == "SecureTextField"
                     or getattr(element, "editable", False)
                     or contract_screen.label_verbs(element.label, element.role))
        if sensitive and _crosses(gesture.start, gesture.end or gesture.start, element.rect):
            raise ValueError("The gesture crosses a committing or sensitive control. Use its listed action instead.")


# The top of the screen where the clock and indicators animate (frame_clock.STATUS_BAR_FRACTION; the engine imports
# no frame_clock).
STATUS_BAR_FRACTION = .05


def same_point_image(before, after, x, y):
    """Whether two screenshot parts show the same pixels, block by block and around (x, y); unreadable isn't."""
    from PIL import Image, ImageChops

    try:
        images = []
        for part in (before, after):
            url = part["image_url"]["url"]
            if not isinstance(url, str) or not url.startswith("data:image/jpeg;base64,"):
                return False
            data = base64.b64decode(url.split(",", 1)[1], validate=True)
            with Image.open(io.BytesIO(data)) as image:
                images.append(image.convert("RGB"))
        a, b = images
        if a.size != b.size:
            return False
        difference = ImageChops.difference(a, b)
        px, py = round(a.width * x / 100), round(a.height * y / 100)
        target = difference.crop((max(0, px - 4), max(0, py - 4), min(a.width, px + 4), min(a.height, py + 4)))
        if max(target.resize((1, 1), Image.Resampling.BOX).tobytes(), default=255) > 24:
            return False
        top = int(a.height * STATUS_BAR_FRACTION) if y / 100 > STATUS_BAR_FRACTION else 0
        difference = difference.crop((0, top, a.width, a.height))
        # Per-channel block means keep colour changes that a greyscale comparison would hide.
        return max(difference.reduce(4).tobytes(), default=255) <= 24
    except (KeyError, TypeError, ValueError, OSError):
        return False

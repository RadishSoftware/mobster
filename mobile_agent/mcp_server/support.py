"""What every MCP tool module shares: the error types, the text and result helpers, and the words for a change."""

import base64
import io
import json
import os
import sys
import time

INLINE_WIDTH, INLINE_QUALITY = 390, 70


TEXT_LIMIT = 6000

SHUTTING_DOWN = ("This Mobster server is shutting down, so it didn't start anything. Reconnect Mobster in your MCP "
                 "client, then try again.")


class ToolError(Exception):
    pass


class Busy(ToolError):
    pass


class Phases:
    def __init__(self):
        self.last = time.monotonic()
        self.spent = {}

    def __call__(self, name):
        now = time.monotonic()
        self.spent[name] = round(self.spent.get(name, 0) + now - self.last, 3)
        self.last = now


def binary_stamp():
    # Updating Mobster replaces this file under a running server, which then fails on files it expected.
    if not getattr(sys, "frozen", False):
        return None
    try:
        info = os.stat(sys.executable)
    except OSError:
        return "missing"
    return (info.st_ino, info.st_size, info.st_mtime_ns)


def is_check_error(error):
    return isinstance(error, ValueError) and type(error).__name__ == "CheckError"


def sentence(text):
    text = " ".join(str(text).split())
    if not text:
        return text
    text = text[:1].upper() + text[1:]
    return text if text.endswith(("?", "!", "…")) else text.rstrip(".") + "."


def jsonable(value):
    return json.loads(json.dumps(value, default=str, allow_nan=False))


def cap_text(text, limit=TEXT_LIMIT):
    if len(text) < limit:
        return text
    note = "\n… (cut to stay under 6,000 characters)"
    return text[:limit - len(note) - 1] + note


def inline_jpeg(path=None, data=None, width=INLINE_WIDTH, quality=INLINE_QUALITY):
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data) if data is not None else path) as image:
            image = image.convert("RGB")
            if image.width > width:
                image = image.resize((width, max(1, round(image.height * width / image.width))), Image.BILINEAR)
            out = io.BytesIO()
            image.save(out, "JPEG", quality=quality)
        return base64.b64encode(out.getvalue()).decode("ascii")
    except Exception:
        return None


def quote(text, limit=60):
    text = " ".join(str(text or "").split())
    return "“" + (text if len(text) <= limit else text[:limit - 1] + "…") + "”"


def screen_changed(before, after):
    if after is None:
        return None
    return getattr(after, "content_fingerprint", None) != getattr(before, "content_fingerprint", 0)


def screen_fingerprint(snapshot):
    return getattr(snapshot, "content_fingerprint", None) if snapshot is not None else None


def changed_words(changed):
    return {True: "; the screen changed.", False: "; the screen didn't change."}.get(changed, ".")


def element_at(snapshot, path):
    return next((element for element in getattr(snapshot, "elements", ()) if element.locator == path), None)


def hit_point(node, tree):
    width, height = tree.size
    x, y, w, h = node.rect
    return (min(.999, max(.001, (x + w / 2) / width)), min(.999, max(.001, (y + h / 2) / height)))


def describe_selector(selector):
    return ", ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in selector.items())


def navbar_hint(data):
    for kind in ("visible", "value", "count"):
        selector = data.get(kind) if isinstance(data, dict) else None
        if (isinstance(selector, dict) and str(selector.get("role", "")).lower() == "navbar"
                and isinstance(selector.get("label"), str) and "id" not in selector):
            fixed = {key: value for key, value in selector.items() if key != "label"}
            fixed["id"] = selector["label"]
            return ("A navigation bar's title is its id, not its label: try "
                    + json.dumps({**data, kind: fixed}, ensure_ascii=False) + ".")
    return None


def describe_assertion(data):
    parts = []
    for kind in ("text", "no_text", "visible", "absent", "value", "count"):
        if kind in data:
            item = data[kind]
            parts.append(f"{kind} " + (describe_selector(item) if isinstance(item, dict)
                                       else json.dumps(item, ensure_ascii=False)))
    for bound in ("equals", "at_least", "at_most"):
        if bound in data:
            parts.append(f"{bound} {json.dumps(data[bound], ensure_ascii=False)}")
    return " ".join(parts) or "the expectation"


def device_request(driver):
    return lambda method, path, body, timeout: driver.call(method, path, body, timeout=timeout)

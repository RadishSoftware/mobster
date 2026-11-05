"""A scripted phone for the app-pack tests that moves between recorded screens as actions say."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from mobile_agent import drivers
from mobile_agent.state import Element, Snapshot, from_wda

FIXTURES = Path(__file__).parent / "fixtures" / "packs"


def el(label, role="Button", rect=(0, 0, .2, .05), **extra):
    return {"label": label, "role": role, "rect": rect, **extra}


def snap(bundle, rows, revision=""):
    elements = []
    for index, row in enumerate(rows):
        extra = {k: v for k, v in row.items() if k not in ("label", "role", "rect")}
        if extra.get("editable"):
            extra.setdefault("actions", ("TAP", "TYPE", "TYPE_SUBMIT"))
        elements.append(Element(str(index), row["label"], row["role"], row["rect"], **extra))
    text = "\n".join(e.label for e in elements if e.label)
    return Snapshot(elements, text, 390, 844, "wda", bundle_id=bundle, revision=revision)


def json_to_xml(node, bundle):
    """WDA's JSON source (GET /source?format=json) as its XML source, so a recorded tree goes through the real parser
    (state.from_wda) as the phone's own read does."""
    def frame(value):
        nums = [float(n) for n in str(value).replace("{", " ").replace("}", " ").replace(",", " ").split()]
        return nums if len(nums) == 4 else [0, 0, 0, 0]

    def build(item):
        kind = item.get("type") or "Other"
        x, y, w, h = frame(item.get("frame"))
        attrs = {"type": f"XCUIElementType{kind}", "enabled": "true" if item.get("isEnabled") in ("1", True)
                 else "false",
                 "visible": "true" if item.get("isVisible") in ("1", True) else "false",
                 "x": str(int(x)), "y": str(int(y)), "width": str(int(w)), "height": str(int(h))}
        for key, src in (("name", item.get("name") or item.get("rawIdentifier")), ("label", item.get("label")),
                         ("value", item.get("value"))):
            if src not in (None, ""):
                attrs[key] = str(src)
        if kind == "Application":
            attrs["bundleId"] = bundle
        node_ = ET.Element(f"XCUIElementType{kind}", attrs)
        for child in item.get("children") or ():
            node_.append(build(child))
        return node_

    root = ET.Element("AppiumAUT")
    root.append(build(node.get("value", node)))
    return ET.tostring(root, encoding="unicode")


def recorded(name, bundle):
    """A recorded (scrubbed) tree from tests/fixtures/packs, read by the real parser."""
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return from_wda(json_to_xml(data, bundle))


def rows_file(name, bundle):
    """A screen saved as element rows (role, label, value, rect in screen fractions), as a Snapshot."""
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return snap(bundle, [el(r["label"], r["role"], tuple(r["rect"]),
                            **({"value": r["value"]} if r.get("value") else {}),
                            **({"editable": True} if r.get("editable") else {}),
                            **({"selected": True} if r.get("selected") else {})) for r in data["rows"]])


class Phone(drivers.Driver):
    """A fake driver. ``screens``: name -> Snapshot (or a function -> Snapshot). ``moves``: (screen, op, label) ->
    the next screen's name; a label of None matches any target. ``links``: url prefix -> screen name."""

    capabilities = frozenset({"call"})

    def __init__(self, screens, moves, start, links=None, home="springboard"):
        self.screens, self.moves, self.at = screens, moves, start
        self.links = links or {}
        self.home = home
        self.actions = []
        self.calls = []

    def observe(self, timeout=10):
        screen = self.screens[self.at]
        return screen() if callable(screen) else screen

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        label = getattr(target, "label", target)
        self.actions.append((operation, label, text))
        for key in ((self.at, operation, label), (self.at, operation, None)):
            if key in self.moves:
                self.at = self.moves[key]
                return {}
        if operation == "LAUNCH_APP" and ("*", "LAUNCH_APP", label) in self.moves:
            self.at = self.moves[("*", "LAUNCH_APP", label)]
        return {}

    def call(self, method, path, body=None, timeout=10):
        self.calls.append((method, path, body))
        if path == "/url":
            for prefix, screen in self.links.items():
                if body["url"].startswith(prefix):
                    self.at = screen
                    break
            return {}
        if path.startswith("/alert"):
            raise RuntimeError("no alert")
        return {}

    @property
    def taps(self):
        return [label for op, label, _ in self.actions if op == "TAP"]

    def close(self):
        pass


def ocr_file(name, bundle):
    """A screen saved as a Vision reading (``<name>.ocr.json``: rows of text, confidence and rect, and ``icons``: the
    controls drawn without words that the icon namer labelled, as [label, [x, y, w, h]]), as the image-only connections
    see it (perception.snapshot_from, source "mirroring")."""
    from mobile_agent import mirroring, perception  # noqa: F401 -- mirroring registers its source
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    icons = [(tuple(rect), label) for label, rect in data.get("icons", ())]
    return perception.snapshot_from(data, source=mirroring.SOURCE, bundle=bundle, icons=icons)

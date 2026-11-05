"""uiautomator's hierarchy as the XML the iOS side reads, so verify, replay and the agent need no Android branch.

Roles take XCUIElementType names, resource-id becomes the identifier, text and content-desc the label, and frames are
dp (pixels over density/160), so the point-based thresholds downstream mean the same thing on both platforms.
"""

import re
from xml.etree import ElementTree as ET

from ..state import _parse_wda

BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
SYSTEM_UI = "com.android.systemui"
# Longest class suffix first wins; an unknown class is "Other".
ROLES = (
    ("EditText", "TextField"), ("AutoCompleteTextView", "TextField"), ("SearchView", "SearchField"),
    ("ImageButton", "Button"), ("Button", "Button"), ("Spinner", "Button"),
    ("CheckBox", "Switch"), ("RadioButton", "Switch"), ("Switch", "Switch"), ("ToggleButton", "Switch"),
    ("TextView", "StaticText"), ("ImageView", "Image"), ("SeekBar", "Slider"), ("ProgressBar", "ProgressIndicator"),
    ("WebView", "WebView"), ("RecyclerView", "ScrollView"), ("ScrollView", "ScrollView"), ("ListView", "Table"),
    ("GridView", "CollectionView"), ("TabWidget", "TabBar"), ("Toolbar", "Toolbar"),
)
TOGGLE_CLASSES = ("CheckBox", "RadioButton", "Switch", "ToggleButton")


def role_of(cls, password=False):
    if password:
        return "SecureTextField"
    short = (cls or "").rsplit(".", 1)[-1]
    for suffix, role in ROLES:
        if short.endswith(suffix):
            return role
    return "Other"


def identifier(resource_id):
    """"com.android.settings:id/search_action_bar" is "search_action_bar"."""
    return (resource_id or "").rsplit("/", 1)[-1]


def front_package(root):
    """The package of the app in front: the first one that is not System UI, else System UI."""
    packages = [node.get("package") for node in root.iter("node") if node.get("package")]
    return next((name for name in packages if name != SYSTEM_UI), packages[0] if packages else "")


def to_wda(xml, width_px, height_px, density):
    """The hierarchy XML as WDA-shaped XML text. ``density`` is dpi; raises ValueError on an unusable tree."""
    root = _parse_wda(xml)
    if root.tag != "hierarchy":
        raise ValueError("Not a uiautomator hierarchy")
    scale = max(density, 1) / 160
    out = ET.Element("XCUIElementTypeApplication", {
        "type": "XCUIElementTypeApplication", "bundleId": front_package(root), "x": "0", "y": "0",
        "width": _num(width_px / scale), "height": _num(height_px / scale)})
    window = ET.SubElement(out, "XCUIElementTypeWindow", {"x": "0", "y": "0", "width": out.get("width"),
                                                         "height": out.get("height")})
    for child in root.findall("node"):
        _convert(child, window, scale)
    return ET.tostring(out, encoding="unicode")


def _num(value):
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _convert(node, parent, scale):
    cls = node.get("class") or ""
    password = node.get("password") == "true"
    role = role_of(cls, password)
    match = BOUNDS.fullmatch(node.get("bounds") or "")
    left, top, right, bottom = (int(n) for n in match.groups()) if match else (0, 0, 0, 0)
    text, desc = node.get("text") or "", node.get("content-desc") or ""
    editable = role in {"TextField", "SecureTextField", "SearchField"}
    toggle = cls.rsplit(".", 1)[-1].endswith(TOGGLE_CLASSES)
    attrs = {"x": _num(left / scale), "y": _num(top / scale), "width": _num(max(right - left, 0) / scale),
             "height": _num(max(bottom - top, 0) / scale),
             "enabled": "true" if node.get("enabled") != "false" else "false"}
    attrs["label"] = desc or ("" if editable else text)
    # An unlabelled icon's id ("icon") would be read as its label, so only text-bearing nodes keep a bare id.
    attrs["name"] = attrs["label"] if role == "Image" and not attrs["label"] else (
        identifier(node.get("resource-id")) or attrs["label"])
    if editable:
        attrs["value"] = "" if password else text
        if node.get("hint"):
            attrs["placeholderValue"] = node.get("hint")
    elif toggle:
        attrs["value"] = "1" if node.get("checked") == "true" else "0"
    traits = ["Selected"] if node.get("selected") == "true" or (toggle and node.get("checked") == "true") else []
    if traits:
        attrs["traits"] = ", ".join(traits)
    if node.get("clickable") != "true" and role == "Other" and not attrs["label"]:
        attrs["accessible"] = "false"
    element = ET.SubElement(parent, f"XCUIElementType{role}", attrs)
    for child in node.findall("node"):
        _convert(child, element, scale)

"""The assertion language of `mobster verify` (docs/checks.md): what a check expects, decided on one read of the
app's accessibility tree, deterministically. No model, OCR or pixel comparison is involved.

An assertion is an object with exactly one kind key:

    text: "Choose your plan"                    some shown node's label or value contains the text
    no_text: "Loading"                          no shown node's label or value contains it
    visible: {label: Restore Purchases}         at least one shown node matches the selector
    absent: {id: spinner}                       no shown node matches it
    value: {id: plan_annual}, equals: X         exactly one shown node matches, and its value equals X
    count: {id: /^plan_/}, equals: 3            the number of shown nodes that match meets the bound
                                                (or at_least: N, at_most: N)

A selector names one or more of id, label, value, role, enabled and selected; all of them must match. A string
written /pattern/ or /pattern/i is a regular expression (re.search on the normalized text).

``evaluate_on(assertions, tree)`` is pure: it reads only the tree it is given.
"""

from dataclasses import dataclass, field
import difflib
import math
import re
import unicodedata
from typing import Any, Optional


class CheckError(ValueError):
    """The check or one of its assertions is invalid: the run can't start (couldnt_run, class "usage").
    ``checks.CheckError`` is this class."""


# -- normalization -----------------------------------------------------------------------------------------------

_QUOTES = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "´": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-"})
_INVISIBLE = re.compile("[​-‏‪-‮⁠﻿­]")
_SPACE = re.compile(r"\s+")


def normalize(text):
    """NFKC; curly quotes and dashes as ASCII; invisible marks removed; every run of whitespace (NBSP too) as
    one space; stripped. "Don’t allow" and "Don't allow" normalize to the same string."""
    if text is None:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).translate(_QUOTES)
    return _SPACE.sub(" ", _INVISIBLE.sub("", text)).strip()


def folded(text):
    return normalize(text).casefold()


# -- text matchers -----------------------------------------------------------------------------------------------

REGEX_MAX = 500
_REGEX = re.compile(r"^/(.*)/(i?)$", re.S)


@dataclass(frozen=True)
class Matcher:
    """One string field: an exact (normalized) string, or a /regex/ (``pattern`` set)."""
    raw: str
    text: str = ""
    pattern: Optional[re.Pattern] = None

    def exact(self, value):
        """Selector rule: exact and case-sensitive after normalization, or the regex."""
        value = normalize(value)
        return bool(self.pattern.search(value)) if self.pattern is not None else value == self.text

    def contains(self, value):
        """``text``/``no_text`` rule: a case-insensitive substring, or the regex."""
        if self.pattern is not None:
            return bool(self.pattern.search(normalize(value)))
        return bool(self.text) and self.text.casefold() in folded(value)

    @property
    def is_regex(self):
        return self.pattern is not None

    def render(self):
        return self.raw if self.is_regex else self.text


def parse_matcher(value, where):
    """A string field of a selector or a text assertion. ``where`` names it in errors."""
    if isinstance(value, bool) or not isinstance(value, str):
        hint = "; quote it" if isinstance(value, (bool, int, float)) else ""
        raise CheckError(f"{where} must be text, not {_type_name(value)}{hint}")
    found = _REGEX.match(value) if len(value) >= 2 else None
    if found is None:
        text = normalize(value)
        if not text:
            raise CheckError(f"{where} is empty")
        if len(value) > 2000:
            raise CheckError(f"{where} is longer than 2,000 characters")
        return Matcher(value, text)
    pattern, flags = found.group(1), found.group(2)
    if not pattern:
        raise CheckError(f"{where} is an empty regex (//), which matches everything")
    if len(pattern) > REGEX_MAX:
        raise CheckError(f"{where}: the regex is longer than {REGEX_MAX} characters")
    try:
        compiled = re.compile(pattern, re.IGNORECASE if flags == "i" else 0)
    except re.error as error:
        raise CheckError(f"{where}: {value} is not a valid regex ({error})") from None
    return Matcher(value, "", compiled)


# -- roles -------------------------------------------------------------------------------------------------------

FRIENDLY_ROLES = {
    "button": frozenset({"Button"}),
    "text": frozenset({"StaticText"}),
    "field": frozenset({"TextField", "SecureTextField", "SearchField", "TextView"}),
    "switch": frozenset({"Switch", "Toggle"}),
    "toggle": frozenset({"Switch", "Toggle"}),
    "cell": frozenset({"Cell"}),
    "image": frozenset({"Image", "Icon"}),
    "link": frozenset({"Link"}),
    "tab": frozenset({"Tab"}),                 # plus a Button inside a TabBar (see Selector.role_matches)
    "slider": frozenset({"Slider"}),
    "stepper": frozenset({"Stepper"}),
    "picker": frozenset({"Picker", "PickerWheel", "DatePicker"}),
    "segment": frozenset({"SegmentedControl"}),
    "navbar": frozenset({"NavigationBar"}),
    "alert": frozenset({"Alert"}),
}
# XCUIElementType names (Xcode 26), for `role: StaticText` or `role: XCUIElementTypeStaticText`.
ELEMENT_TYPES = frozenset("""
Any Other Application Group Window Sheet Drawer Alert Dialog Button RadioButton RadioGroup CheckBox DisclosureTriangle
PopUpButton ComboBox MenuButton ToolbarButton Popover Keyboard Key NavigationBar TabBar TabGroup Toolbar StatusBar
Table TableRow TableColumn Outline OutlineRow Browser CollectionView Slider PageIndicator ProgressIndicator
ActivityIndicator SegmentedControl Picker PickerWheel Switch Toggle Link Image Icon SearchField ScrollView ScrollBar
StaticText TextField SecureTextField DatePicker TextView Menu MenuItem MenuBar MenuBarItem Map WebView IncrementArrow
DecrementArrow Timeline RatingIndicator ValueIndicator SplitGroup Splitter RelevanceIndicator ColorWell HelpTag Matte
DockItem Ruler RulerMarker Grid LevelIndicator Cell LayoutArea LayoutItem Handle Stepper Tab TouchBar StatusItem
""".split())
_TYPES_FOLDED = {name.casefold(): name for name in ELEMENT_TYPES}
# The friendly name a report shows for a raw role.
ROLE_NAMES = {"StaticText": "text", "Button": "button", "TextField": "field", "SecureTextField": "field",
              "SearchField": "field", "TextView": "field", "Switch": "switch", "Toggle": "switch", "Cell": "cell",
              "Image": "image", "Icon": "image", "Link": "link", "Tab": "tab", "Slider": "slider",
              "Stepper": "stepper", "Picker": "picker", "PickerWheel": "picker", "DatePicker": "picker",
              "SegmentedControl": "segment", "NavigationBar": "navbar", "Alert": "alert"}


def friendly_role(role):
    return ROLE_NAMES.get(role, role)


def parse_role(value, where):
    """(the role as written, the XCUIElementType names it matches, whether it is the friendly ``tab``)."""
    if isinstance(value, bool) or not isinstance(value, str) or not value.strip():
        raise CheckError(f"{where} must be a role name such as button, text or field")
    name = value.strip()
    if name in FRIENDLY_ROLES:
        return name, FRIENDLY_ROLES[name], name == "tab"
    bare = name.removeprefix("XCUIElementType")
    known = _TYPES_FOLDED.get(bare.casefold())
    if known is None:
        close = difflib.get_close_matches(name.casefold(), list(FRIENDLY_ROLES) + sorted(_TYPES_FOLDED), n=1)
        hint = f"; did you mean {close[0]}?" if close else ""
        raise CheckError(f"{where}: {name!r} is not a role{hint} Use one of {', '.join(FRIENDLY_ROLES)}, "
                         "or an XCUIElementType name")
    return name, frozenset({known}), False


# -- selectors ---------------------------------------------------------------------------------------------------

SELECTOR_KEYS = ("id", "label", "value", "role", "enabled", "selected")


@dataclass(frozen=True)
class Selector:
    id: Optional[Matcher] = None
    label: Optional[Matcher] = None
    value: Optional[Matcher] = None
    role: Optional[str] = None
    roles: frozenset = frozenset()
    tab: bool = False
    enabled: Optional[bool] = None
    selected: Optional[bool] = None

    def role_matches(self, node):
        if self.role is None:
            return True
        if node.role in self.roles:
            return True
        # The friendly `tab`: a tab bar's items are Buttons inside the TabBar.
        return self.tab and node.role == "Button" and "/XCUIElementTypeTabBar[" in node.path

    def matches(self, node):
        return ((self.id is None or (bool(node.identifier) and self.id.exact(node.identifier)))
                and (self.label is None or self.label.exact(node.label))
                and (self.value is None or self.value.exact(node.value))
                and self.role_matches(node)
                and (self.enabled is None or node.enabled == self.enabled)
                and (self.selected is None or node.selected == self.selected))

    def render(self):
        parts = []
        for key in ("id", "label", "value"):
            matcher = getattr(self, key)
            if matcher is not None:
                parts.append(f"{key}={matcher.render()}")
        if self.role is not None:
            parts.append(f"role={self.role}")
        for key in ("enabled", "selected"):
            flag = getattr(self, key)
            if flag is not None:
                parts.append(f"{key}={'true' if flag else 'false'}")
        return " ".join(parts)

    def to_dict(self):
        out = {}
        for key in ("id", "label", "value"):
            matcher = getattr(self, key)
            if matcher is not None:
                out[key] = matcher.raw
        if self.role is not None:
            out["role"] = self.role
        for key in ("enabled", "selected"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        return out


def parse_selector(obj, where="selector"):
    """A selector object: at least one of id, label, value, role, enabled and selected. Raises CheckError."""
    if not isinstance(obj, dict):
        raise CheckError(f"{where} must be an object such as {{label: Continue}}, not {_type_name(obj)}")
    _no_unknown_keys(obj, SELECTOR_KEYS, where)
    if not obj:
        raise CheckError(f"{where} needs at least one of {', '.join(SELECTOR_KEYS)}")
    fields = {}
    for key in ("id", "label", "value"):
        if key in obj:
            fields[key] = parse_matcher(obj[key], f"{where}.{key}")
    if "role" in obj:
        fields["role"], fields["roles"], fields["tab"] = parse_role(obj["role"], f"{where}.role")
    for key in ("enabled", "selected"):
        if key in obj:
            if not isinstance(obj[key], bool):
                raise CheckError(f"{where}.{key} must be true or false, not {obj[key]!r}")
            fields[key] = obj[key]
    return Selector(**fields)


# -- assertions --------------------------------------------------------------------------------------------------

KINDS = ("text", "no_text", "visible", "absent", "value", "count")
BOUNDS = ("equals", "at_least", "at_most")
NAME_MAX = 120


@dataclass(frozen=True)
class Assertion:
    kind: str
    needle: Optional[Matcher] = None      # text, no_text
    selector: Optional[Selector] = None   # visible, absent, value, count
    equals: Any = None                    # value: text, a number or true/false; count: a whole number
    at_least: Optional[int] = None
    at_most: Optional[int] = None
    name: Optional[str] = None

    def render(self):
        """One line, such as ``count id=/^plan_/ == 3`` or ``text "Choose your plan"``."""
        if self.kind in ("text", "no_text"):
            shown = self.needle.raw if self.needle.is_regex else f'"{self.needle.text}"'
            return f"{self.kind} {shown}"
        selector = self.selector.render()
        if self.kind == "value":
            return f"value {selector} == {_render_expected(self.equals)}"
        if self.kind == "count":
            if self.equals is not None:
                return f"count {selector} == {self.equals}"
            if self.at_least is not None:
                return f"count {selector} >= {self.at_least}"
            return f"count {selector} <= {self.at_most}"
        return f"{self.kind} {selector}"

    def to_dict(self):
        """The assertion as a check file writes it (the canonical form)."""
        out = {}
        if self.kind in ("text", "no_text"):
            out[self.kind] = self.needle.raw
        else:
            out[self.kind] = self.selector.to_dict()
        for key in BOUNDS:
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        if self.name:
            out["name"] = self.name
        return out


def _render_expected(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    return f'"{value}"'


def parse_assertion(obj, where="assertion"):
    """One assertion object (§3.1). ``text`` and ``no_text`` take one string here; a check file's list form is
    expanded by ``expand_assertion``. Raises CheckError."""
    if not isinstance(obj, dict):
        raise CheckError(f"{where} must be an object such as {{text: Continue}}, not {_type_name(obj)}")
    kinds = [key for key in KINDS if key in obj]
    _no_unknown_keys(obj, KINDS + BOUNDS + ("name",), where)
    if len(kinds) != 1:
        raise CheckError(f"{where} needs exactly one of {', '.join(KINDS)}"
                         + (f"; it has {', '.join(kinds)}" if kinds else ""))
    kind = kinds[0]
    name = obj.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > NAME_MAX):
        raise CheckError(f"{where}.name must be text of 1 to {NAME_MAX} characters")
    bounds = {key: obj[key] for key in BOUNDS if key in obj}
    fields = {"kind": kind, "name": name.strip() if name else None}
    if kind in ("text", "no_text"):
        if isinstance(obj[kind], list):
            raise CheckError(f"{where}.{kind} takes one string here; a check file may list several")
        if bounds:
            raise CheckError(f"{where}: {', '.join(bounds)} goes with value or count, not {kind}")
        fields["needle"] = parse_matcher(obj[kind], f"{where}.{kind}")
    else:
        fields["selector"] = parse_selector(obj[kind], f"{where}.{kind}")
        if kind in ("visible", "absent") and bounds:
            raise CheckError(f"{where}: {', '.join(bounds)} goes with value or count, not {kind}")
        if kind == "value":
            if set(bounds) != {"equals"}:
                raise CheckError(f"{where}: value needs equals (the text, number or true/false it must show)"
                                 + (f", and takes no {', '.join(k for k in bounds if k != 'equals')}"
                                    if set(bounds) - {"equals"} else ""))
            fields["equals"] = _parse_expected(bounds["equals"], f"{where}.equals")
        if kind == "count":
            if len(bounds) != 1:
                raise CheckError(f"{where}: count needs exactly one of equals, at_least and at_most")
            (key, number), = bounds.items()
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                raise CheckError(f"{where}.{key} must be a whole number of at least 0, not {number!r}")
            fields[key] = number
    return Assertion(**fields)


def expand_assertion(obj, where="assertion"):
    """A check file's assertion: ``text`` and ``no_text`` may list several strings, one assertion each."""
    if isinstance(obj, dict):
        for kind in ("text", "no_text"):
            if isinstance(obj.get(kind), list):
                items = obj[kind]
                if not items:
                    raise CheckError(f"{where}.{kind} is an empty list")
                rest = {key: value for key, value in obj.items() if key != kind}
                return [parse_assertion({**rest, kind: item}, f"{where}.{kind}[{index}]")
                        for index, item in enumerate(items)]
    return [parse_assertion(obj, where)]


def _parse_expected(value, where):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise CheckError(f"{where} must be a finite number")
        return value
    if isinstance(value, str):
        if len(value) > 2000:
            raise CheckError(f"{where} is longer than 2,000 characters")
        return value
    raise CheckError(f"{where} must be text, a number or true/false, not {_type_name(value)}")


def _no_unknown_keys(obj, allowed, where):
    for key in obj:
        if key not in allowed:
            close = difflib.get_close_matches(str(key), allowed, n=1)
            hint = f"; did you mean {close[0]}?" if close else f"; use {', '.join(allowed)}"
            raise CheckError(f"{where} has an unknown key {key!r}{hint}")


def _type_name(value):
    return {dict: "an object", list: "a list", bool: "true/false", type(None): "nothing",
            int: "a number", float: "a number", str: "text"}.get(type(value), type(value).__name__)


# -- equals ------------------------------------------------------------------------------------------------------

ON = frozenset({"1", "true", "on", "yes"})
OFF = frozenset({"0", "false", "off", "no"})
# The first number in a value: thousands groups of exactly three digits (1,234.5 or 1 234), else plain digits.
_NUMBER = re.compile(r"-?\d{1,3}(?:[,   ]\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")


def first_number(text):
    found = _NUMBER.search(normalize(text))
    if found is None:
        return None
    try:
        return float(re.sub(r"[,   ]", "", found.group(0)))
    except ValueError:
        return None


def same_number(got, expected):
    """Equal at the expected number's own precision (the rule of bench/checks.py:_same_number): 3.4 reads as 3
    against ``equals: 3``, 39.99 does not read as 39."""
    want = float(expected)
    places = 0
    if isinstance(expected, float):
        text = repr(expected)
        places = len(text.split(".")[1].rstrip("0")) if "." in text and "e" not in text else 0
    return got == want or round(got, places) == want


def value_equals(node_value, expected):
    """(ok, how the value reads) for ``value … equals: expected``."""
    if isinstance(expected, bool):
        word = folded(node_value)
        if word in ON:
            return expected is True, "on"
        if word in OFF:
            return expected is False, "off"
        return False, f'"{normalize(node_value)}" (neither on nor off)'
    if isinstance(expected, (int, float)):
        number = first_number(node_value)
        if number is None:
            return False, f'"{normalize(node_value)}" (no number)'
        return same_number(number, expected), f'"{normalize(node_value)}"'
    return normalize(node_value) == normalize(expected), f'"{normalize(node_value)}"'


# -- evaluation --------------------------------------------------------------------------------------------------

MATCHES_KEPT = 10
NEAR_MISSES = 5


@dataclass(frozen=True)
class AssertionResult:
    ok: bool
    text: str
    observed: str
    matches: tuple = ()              # of dicts {id, label, value, role, rect}
    paths: tuple = field(default=(), compare=False)   # the matched nodes' paths (the overlay outlines them)


def node_dict(node):
    return {"id": node.identifier, "label": node.label, "value": node.value, "role": node.role,
            "rect": [round(n, 1) for n in node.rect]}


def node_name(node, tree=None):
    """How a node is named in ``observed``: its identifier, else its quoted label, else its role. A node with
    neither, such as a Settings row (a Cell whose text sits on a child Button or StaticText), is named by its role
    and the first labelled node inside it in ``tree``: 'cell "General"'."""
    if node.identifier:
        return node.identifier
    if node.label:
        return f'"{_short(node.label, 60)}"'
    role = friendly_role(node.role)
    inner = _labelled_descendant(node, tree) if tree is not None else None
    if inner is None:
        return role
    return f'{role} "{_short(inner.label, 60)}"' if inner.label else f"{role} {inner.identifier}"


def _labelled_descendant(node, tree):
    """The first node inside ``node`` (in document order) with a label or an identifier, else None."""
    prefix = node.path + "/"
    return next((inner for inner in tree.nodes if inner.path.startswith(prefix) and (inner.label or inner.identifier)),
                None)


def field_value(node):
    """A node's value as ``value`` compares it: a field showing its placeholder is empty."""
    if node.placeholder and node.value == node.placeholder and friendly_role(node.role) == "field":
        return ""
    return node.value


def _short(text, limit):
    """The app's text as shown (whitespace collapsed, invisible marks removed), cut at ``limit``."""
    text = _SPACE.sub(" ", _INVISIBLE.sub("", str(text or ""))).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _names(nodes, limit=5, tree=None):
    names = [node_name(node, tree) for node in nodes[:limit]]
    more = len(nodes) - limit
    return ", ".join(names) + (f" and {more} more" if more > 0 else "")


def _close(needle, candidates):
    """Up to NEAR_MISSES of ``candidates`` closest to ``needle`` (difflib). With nothing close, the three most
    alike, so a failure still shows what the screen had."""
    folded_map = {}
    for candidate in candidates:
        text = normalize(candidate)
        if text and len(text) <= 300:
            folded_map.setdefault(text.casefold(), candidate)
    target = folded(needle)
    close = difflib.get_close_matches(target, list(folded_map), n=NEAR_MISSES, cutoff=.6)
    if not close:
        ranked = sorted(folded_map, key=lambda key: -difflib.SequenceMatcher(None, target, key).ratio())
        close = [key for key in ranked[:3] if difflib.SequenceMatcher(None, target, key).ratio() >= .3]
    return [folded_map[key] for key in close]


def _closest_text(needle, candidates):
    close = _close(needle, candidates)
    return "closest: " + ", ".join(f'"{_short(text, 60)}"' for text in close) if close else "nothing like it"


def _near_selector(selector, shown):
    """Near misses for a selector that matched nothing: by its id, else label, else value."""
    for key, attribute in (("id", "identifier"), ("label", "label"), ("value", "value")):
        matcher = getattr(selector, key)
        if matcher is not None and not matcher.is_regex:
            return _closest_text(matcher.text, [getattr(node, attribute) for node in shown])
    return None


def _hidden_matches(selector, tree, shown_paths):
    """A note on nodes that match but aren't shown (hidden, or off screen)."""
    hidden = [node for node in tree.nodes if node.path not in shown_paths and selector.matches(node)
              and node.role not in ("Application", "Window")]
    if not hidden:
        return None
    off = [node for node in hidden if node.visible and not tree.on_screen(node)]
    if off:
        return f"{len(off)} more off screen: swipe to {'it' if len(off) == 1 else 'them'}"
    return f"{len(hidden)} more not visible"


def _result(assertion, ok, observed, nodes=()):
    return AssertionResult(ok, assertion.render(), observed, tuple(node_dict(n) for n in nodes[:MATCHES_KEPT]),
                           tuple(n.path for n in nodes[:MATCHES_KEPT]))


def evaluate_one(assertion, tree, shown=None):
    shown = tree.shown() if shown is None else shown
    if assertion.kind in ("text", "no_text"):
        needle = assertion.needle
        hits = [node for node in shown if needle.contains(node.label) or needle.contains(node.value)]
        if assertion.kind == "text":
            if hits:
                first = hits[0]
                where = first.label if needle.contains(first.label) else first.value
                more = f" and {len(hits) - 1} more" if len(hits) > 1 else ""
                return _result(assertion, True, f'found in {friendly_role(first.role)} "{_short(where, 80)}"{more}',
                               hits)
            candidates = [node.label for node in shown] + [node.value for node in shown]
            return _result(assertion, False, "not on screen; " + (
                _closest_text(needle.text, candidates) if not needle.is_regex else "no label or value matches"))
        if hits:
            return _result(assertion, False, f"on screen in {_names(hits, tree=tree)}", hits)
        return _result(assertion, True, "not on screen")

    selector = assertion.selector
    matched = [node for node in shown if selector.matches(node)]
    shown_paths = {node.path for node in shown}
    if assertion.kind == "visible":
        if matched:
            return _result(assertion, True, f"found {len(matched)}: {_names(matched, tree=tree)}", matched)
        notes = [note for note in (_hidden_matches(selector, tree, shown_paths),
                                   _near_selector(selector, shown)) if note]
        return _result(assertion, False, "no shown element matches" + ("; " + "; ".join(notes) if notes else ""))
    if assertion.kind == "absent":
        if matched:
            return _result(assertion, False, f"found {len(matched)}: {_names(matched, tree=tree)}", matched)
        return _result(assertion, True, "none shown")
    if assertion.kind == "value":
        if not matched:
            notes = [note for note in (_hidden_matches(selector, tree, shown_paths),
                                       _near_selector(selector, shown)) if note]
            return _result(assertion, False, "not found" + ("; " + "; ".join(notes) if notes else ""))
        if len(matched) > 1:
            return _result(assertion, False, f"ambiguous: {len(matched)} matches ({_names(matched, tree=tree)})",
                           matched)
        node = matched[0]
        ok, reads = value_equals(field_value(node), assertion.equals)
        return _result(assertion, ok, reads if ok else f"was {reads}", matched)
    # count
    count = len(matched)
    if assertion.equals is not None:
        ok = count == assertion.equals
    elif assertion.at_least is not None:
        ok = count >= assertion.at_least
    else:
        ok = count <= assertion.at_most
    observed = f"found {count}" + (f": {_names(matched, tree=tree)}" if matched else "")
    if not ok and not matched:
        hidden = _hidden_matches(selector, tree, shown_paths)
        observed += f"; {hidden}" if hidden else ""
    return _result(assertion, ok, observed, matched)


def evaluate_on(assertions, tree):
    """Every assertion on one read of the tree, in order: a list of AssertionResult. Pure."""
    shown = tree.shown()
    return [evaluate_one(assertion, tree, shown) for assertion in assertions]


def matches_selector(selector, node):
    return selector.matches(node)

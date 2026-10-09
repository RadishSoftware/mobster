"""Set or remove one member of a JSON or JSONC file and leave every other byte as it was.

Editors keep their settings in JSON with comments and trailing commas (VS Code's mcp.json, Zed's settings.json,
opencode.jsonc), and people format them by hand. Loading and dumping such a file would drop the comments and
reflow it, so these functions edit the text instead: they parse it into a tree that remembers where each value
starts and ends, splice in the new member, and parse the result again. The edit is kept only when the new
document equals the old one with exactly that change; anything else raises ``JsoncError``, and the caller writes
nothing.
"""

import copy
import json
import re

NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")
LITERALS = {"true": True, "false": False, "null": None}
MAX_DEPTH = 200


class JsoncError(ValueError):
    """The text isn't JSON (or JSONC) that can be edited safely. The message says where, and what to do."""


class Node:
    """A parsed value: its Python ``value`` and its span ``start``..``end`` in the text. An object node keeps its
    ``members`` as (key, key_start, value_node)."""

    __slots__ = ("kind", "start", "end", "value", "members")

    def __init__(self, kind, start, end, value, members=None):
        self.kind, self.start, self.end, self.value = kind, start, end, value
        self.members = members or []

    def member(self, key):
        found = [m for m in self.members if m[0] == key]
        if len(found) > 1:
            raise JsoncError(f'"{key}" appears {len(found)} times in one object; remove the extra copies by hand')
        return found[0] if found else None


class _Parser:
    def __init__(self, text, pos=0):
        self.text, self.pos = text, pos

    def fail(self, message):
        line = self.text.count("\n", 0, self.pos) + 1
        column = self.pos - (self.text.rfind("\n", 0, self.pos) + 1) + 1
        raise JsoncError(f"{message} at line {line}, column {column}")

    def skip(self):
        """Move past whitespace and comments."""
        text, n = self.text, len(self.text)
        while self.pos < n:
            if text[self.pos] in " \t\r\n﻿":
                self.pos += 1
            elif text.startswith("//", self.pos):
                end = text.find("\n", self.pos)
                self.pos = n if end < 0 else end
            elif text.startswith("/*", self.pos):
                end = text.find("*/", self.pos + 2)
                if end < 0:
                    self.fail("unclosed comment")
                self.pos = end + 2
            else:
                return

    def peek(self):
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def value(self, depth=0):
        if depth > MAX_DEPTH:
            self.fail("nested too deep")
        self.skip()
        ch, start = self.peek(), self.pos
        if not ch:
            self.fail("unexpected end of file")
        if ch == "{":
            return self.obj(depth)
        if ch == "[":
            return self.array(depth)
        if ch == '"':
            end = self.string()
            return Node("string", start, end, self.decode(start, end))
        match = NUMBER.match(self.text, self.pos)
        if match:
            self.pos = match.end()
            return Node("number", start, self.pos, json.loads(match.group()))
        for word, literal in LITERALS.items():
            if self.text.startswith(word, self.pos):
                self.pos += len(word)
                return Node("literal", start, self.pos, literal)
        self.fail(f"unexpected {ch!r}")

    def decode(self, start, end):
        try:
            return json.loads(self.text[start:end])
        except ValueError:
            self.pos = start
            self.fail("a string with a character JSON doesn't allow")

    def string(self):
        """Move past one string; returns where it ends."""
        text, pos = self.text, self.pos + 1
        while pos < len(text):
            ch = text[pos]
            if ch == "\\":
                pos += 2
            elif ch == '"':
                self.pos = pos + 1
                return self.pos
            elif ch == "\n":
                break
            else:
                pos += 1
        self.fail("unclosed string")

    def obj(self, depth):
        start = self.pos
        self.pos += 1
        members, value = [], {}
        while True:
            self.skip()
            ch = self.peek()
            if ch == "}":
                self.pos += 1
                return Node("object", start, self.pos, value, members)
            if ch != '"':
                self.fail("expected a quoted key or '}'")
            key_start = self.pos
            key = self.decode(key_start, self.string())
            self.skip()
            if self.peek() != ":":
                self.fail("expected ':'")
            self.pos += 1
            node = self.value(depth + 1)
            members.append((key, key_start, node))
            value[key] = node.value
            self.skip()
            if self.peek() == ",":
                self.pos += 1
            elif self.peek() != "}":
                self.fail("expected ',' or '}'")

    def array(self, depth):
        start = self.pos
        self.pos += 1
        items = []
        while True:
            self.skip()
            if self.peek() == "]":
                self.pos += 1
                return Node("array", start, self.pos, items)
            items.append(self.value(depth + 1).value)
            self.skip()
            if self.peek() == ",":
                self.pos += 1
            elif self.peek() != "]":
                self.fail("expected ',' or ']'")


def parse(text):
    """The root Node of ``text``, or None when it holds nothing but whitespace and comments."""
    parser = _Parser(text)
    parser.skip()
    if parser.pos >= len(text):
        return None
    root = parser.value()
    parser.skip()
    if parser.pos < len(text):
        parser.fail("unexpected text after the end")
    return root


def loads(text):
    """The value of a JSON or JSONC document; {} for an empty one."""
    root = parse(text)
    return {} if root is None else root.value


def indent_unit(text):
    """The file's indent: the leading whitespace of the first indented line that starts with a key ("  " when no
    line does)."""
    match = re.search(r"^([ \t]+)\"", text, re.M)
    if not match:
        return "  "
    run = match.group(1)
    return "\t" if run.startswith("\t") else " " * min(len(run), 8)


def dump(value, unit="  ", level=0):
    """``value`` as JSON the way editors write it: one member per line, short arrays of scalars on one line."""
    pad, inner = unit * level, unit * (level + 1)
    if isinstance(value, dict):
        if not value:
            return "{}"
        body = ",\n".join(f"{inner}{json.dumps(k, ensure_ascii=False)}: {dump(v, unit, level + 1)}"
                          for k, v in value.items())
        return "{\n" + body + "\n" + pad + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        flat = "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in value) + "]"
        if all(not isinstance(v, (dict, list)) for v in value) and len(flat) <= 100:
            return flat
        return "[\n" + ",\n".join(inner + dump(v, unit, level + 1) for v in value) + "\n" + pad + "]"
    return json.dumps(value, ensure_ascii=False)


def _render(value, unit, base):
    """``dump(value)`` with each continuation line indented by ``base`` too."""
    return dump(value, unit).replace("\n", "\n" + base)


def _line_indent(text, pos):
    """The whitespace that starts the line holding ``pos``."""
    start = text.rfind("\n", 0, pos) + 1
    return re.match(r"[ \t]*", text[start:]).group(0)


def _own_line(text, pos):
    """Whether only whitespace precedes ``pos`` on its line."""
    return text[text.rfind("\n", 0, pos) + 1:pos].strip() == ""


def _after(text, pos):
    """The position of the first character at or after ``pos`` that isn't whitespace or a comment."""
    parser = _Parser(text, pos)
    parser.skip()
    return parser.pos


def _last_content(text, start, end):
    """The end of the last character in ``start``..``end`` that isn't whitespace (``start`` when there is none)."""
    pos = end
    while pos > start and text[pos - 1] in " \t\r\n":
        pos -= 1
    return pos


def _apply(text, edits):
    """Apply (start, end, insert) edits whose spans don't overlap, from the last to the first."""
    for start, end, insert in sorted(edits, key=lambda edit: edit[0], reverse=True):
        text = text[:start] + insert + text[end:]
    return text


def _insert_member(text, obj, key, value, unit):
    """``text`` with ``"key": value`` added as the last member of the object node ``obj``."""
    close = obj.end - 1
    if obj.members and _own_line(text, obj.members[-1][1]):
        indent = _line_indent(text, obj.members[-1][1])
    else:
        indent = _line_indent(text, obj.start) + unit
    closing = _line_indent(text, close) if _own_line(text, close) else _line_indent(text, obj.start)
    where = _last_content(text, obj.start + 1, close)
    member = "\n" + indent + json.dumps(key, ensure_ascii=False) + ": " + _render(value, unit, indent) + "\n" + closing
    comma = ""
    edits = []
    if obj.members:
        last = obj.members[-1][2]
        if text[_after(text, last.end)] != ",":
            if where == last.end:
                comma = ","
            else:
                edits.append((last.end, last.end, ","))
    edits.append((where, close, comma + member))
    return _apply(text, edits)


def _replace_value(text, member, value, unit):
    """``text`` with the member's value replaced, its lines indented like the member's."""
    _key, key_start, node = member
    return text[:node.start] + _render(value, unit, _line_indent(text, key_start)) + text[node.end:]


def _remove_member(text, obj, index):
    """``text`` without the object's member at ``index``, and without the comma that separated it."""
    _key, start, node = obj.members[index]
    end = node.end
    edits = []
    after = _after(text, end)
    if text[after] == ",":
        end = after + 1
    elif index > 0:
        comma = _after(text, obj.members[index - 1][2].end)
        if text[comma] == ",":
            edits.append((comma, comma + 1, ""))
    if _own_line(text, start):
        rest = re.match(r"[ \t]*(?:\r?\n|$)", text[end:])
        if rest:
            start = text.rfind("\n", 0, start) + 1
            end += rest.end()
    edits.append((start, end, ""))
    return _apply(text, edits)


def _with(value, path, key, new):
    """A deep copy of ``value`` with ``path``/``key`` set to ``new`` (None removes it)."""
    result = copy.deepcopy(value)
    target = result
    for part in path:
        target = target.setdefault(part, {})
    if new is None:
        target.pop(key, None)
    else:
        target[key] = copy.deepcopy(new)
    return result


def _checked(new_text, expected):
    try:
        actual = loads(new_text)
    except JsoncError as error:
        raise JsoncError(f"the edit would leave the file unreadable ({error})") from None
    if actual != expected:
        raise JsoncError("the edit would change more than Mobster's entry")
    return new_text


def set_member(text, path, key, value):
    """``text`` with the object at ``path`` (keys from the root, created as needed) holding ``key: value``.
    Returns ``text`` itself when it already holds exactly that value."""
    root = parse(text)
    unit = indent_unit(text)
    if root is None:
        nested = {key: value}
        for part in reversed(path):
            nested = {part: nested}
        kept = text.rstrip()
        return (kept + "\n" if kept else "") + dump(nested, unit) + "\n"
    if root.kind != "object":
        raise JsoncError("the file holds something other than one JSON object")
    node = root
    for part in path:
        member = node.member(part)
        if member is None:
            break
        if member[2].kind != "object":
            raise JsoncError(f'"{part}" is not an object')
        node = member[2]
    expected = _with(root.value, path, key, value)
    node = root
    for depth, part in enumerate(path):
        member = node.member(part)
        if member is None:
            nested = {key: value}
            for inner in reversed(path[depth + 1:]):
                nested = {inner: nested}
            return _checked(_insert_member(text, node, part, nested, unit), expected)
        node = member[2]
    member = node.member(key)
    if member is not None:
        if member[2].value == value:
            return text
        return _checked(_replace_value(text, member, value, unit), expected)
    return _checked(_insert_member(text, node, key, value, unit), expected)


def remove_member(text, path, key):
    """``text`` without ``key`` in the object at ``path``; ``text`` itself when there is no such key. A container
    left with nothing in it, not even a comment, goes too, so removing what `set_member` added gives back the
    file it started from."""
    root = parse(text)
    if root is None or root.kind != "object":
        return text
    node = root
    for part in path:
        member = node.member(part)
        if member is None or member[2].kind != "object":
            return text
        node = member[2]
    if node.member(key) is None:
        return text
    index = next(i for i, m in enumerate(node.members) if m[0] == key)
    expected = _with(root.value, path, key, None)
    new_text = _remove_member(text, node, index)
    while path:
        parent = parse(new_text)
        for part in path[:-1]:
            parent = parent.member(part)[2]
        position = next(i for i, m in enumerate(parent.members) if m[0] == path[-1])
        target = parent.members[position][2]
        if target.members or new_text[target.start + 1:target.end - 1].strip():
            break
        new_text = _remove_member(new_text, parent, position)
        expected = _with(expected, path[:-1], path[-1], None)
        path = path[:-1]
    return _checked(new_text, expected)


def get_member(text, path, key):
    """The value at ``path``/``key``, or None."""
    value = loads(text)
    for part in path:
        value = value.get(part) if isinstance(value, dict) else None
    return value.get(key) if isinstance(value, dict) else None

"""Set or remove one table of a TOML file, or one entry of a YAML mapping, as a text edit.

Codex keeps its servers in TOML (``[mcp_servers.<name>]``) and Goose in YAML (``extensions: <name>:``). The
standard library reads TOML but can't write it, and PyYAML drops comments when it writes, so both edits work on
the lines: they cut out the old section, put the new one in its place (or at the end), and parse the result
again. The edit is kept only when the new document equals the old one with exactly that change; anything else
raises ``EditError`` and the caller writes nothing.
"""

import copy
import json
import re
import tomllib


class EditError(ValueError):
    """The file can't be edited safely. The message says why."""


# ---------------------------------------------------------------------------------------------------- TOML

HEADER = re.compile(r"^\s*(\[\[?)\s*(.+?)\s*(\]\]?)\s*(?:#.*)?$")


def _toml_key_path(raw):
    """``a."b c".d`` as ["a", "b c", "d"]; None when it can't be read."""
    parts, pos = [], 0
    raw = raw.strip()
    while pos < len(raw):
        if raw[pos] == '"':
            end = pos + 1
            while end < len(raw) and raw[end] != '"':
                end += 2 if raw[end] == "\\" else 1
            try:
                parts.append(json.loads(raw[pos:end + 1]))
            except ValueError:
                return None
            pos = end + 1
        elif raw[pos] == "'":
            end = raw.find("'", pos + 1)
            if end < 0:
                return None
            parts.append(raw[pos + 1:end])
            pos = end + 1
        else:
            match = re.match(r"[A-Za-z0-9_-]+", raw[pos:])
            if not match:
                return None
            parts.append(match.group())
            pos += match.end()
        rest = re.match(r"\s*(\.)?\s*", raw[pos:])
        pos += rest.end()
        if not rest.group(1) and pos < len(raw):
            return None
    return parts


def toml_string(value):
    """A TOML basic string."""
    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007F")


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
    return toml_string(value)


def _toml_key(key):
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else toml_string(key)


def toml_table(path, value):
    """``[a.b]`` with ``value``'s scalar and list keys, then a sub-table for each dict value."""
    lines = ["[" + ".".join(_toml_key(part) for part in path) + "]"]
    tables = []
    for key, item in value.items():
        if isinstance(item, dict):
            tables.append((key, item))
        else:
            lines.append(f"{_toml_key(key)} = {toml_value(item)}")
    for key, item in tables:
        lines.append("")
        lines.extend(toml_table([*path, key], item).splitlines())
    return "\n".join(lines) + "\n"


def toml_loads(text):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise EditError(f"it isn't valid TOML ({error})") from None


def _dig(data, path):
    for part in path:
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def _toml_cut(text, path):
    """``text`` without the table at ``path`` and its sub-tables; and the line index where the first one began."""
    lines = text.splitlines(keepends=True)
    kept, cut_at, cutting, in_string = [], None, False, None
    for line in lines:
        # A header inside a multi-line string isn't a header.
        if in_string is None:
            match = HEADER.match(line)
            if match:
                key = _toml_key_path(match.group(2))
                cutting = key is not None and match.group(1) == "[" and key[:len(path)] == list(path)
                if cutting and cut_at is None:
                    cut_at = len(kept)
        for quote in ('"""', "'''"):
            count = line.count(quote)
            if count % 2 == 1 and in_string in (None, quote):
                in_string = None if in_string == quote else quote
        if not cutting:
            kept.append(line)
    if cut_at is not None:
        # Drop the blank lines the cut table left behind it.
        while cut_at > 0 and not kept[cut_at - 1].strip() and (cut_at >= len(kept) or not kept[cut_at].strip()):
            kept.pop(cut_at - 1)
            cut_at -= 1
    return "".join(kept), cut_at


def _pruned(data, path):
    """``data`` without the tables along ``path`` that are left empty: a table that existed only because
    ``[a.b]`` named it goes away with that header."""
    data = copy.deepcopy(data)
    for depth in range(len(path), 0, -1):
        parent = _dig(data, path[:depth - 1])
        if isinstance(parent, dict) and parent.get(path[depth - 1]) == {}:
            del parent[path[depth - 1]]
    return data


def toml_set_table(text, path, value):
    """``text`` with the table at ``path`` (such as ("mcp_servers", "mobster")) holding exactly ``value``.
    Returns ``text`` itself when it already does."""
    data = toml_loads(text)
    if _dig(data, path) == value:
        return text
    expected = copy.deepcopy(data)
    target = expected
    for part in path[:-1]:
        target = target.setdefault(part, {})
    target[path[-1]] = copy.deepcopy(value)
    cut, cut_at = _toml_cut(text, path)
    if _dig(toml_loads(cut), path) is not None:
        raise EditError(f"{'.'.join(path)} is written inline or with dotted keys; remove it by hand first")
    table = toml_table(path, value)
    if cut_at is None:
        body = cut.rstrip("\n")
        new_text = (body + "\n\n" if body else "") + table
    else:
        lines = cut.splitlines(keepends=True)
        before, after = "".join(lines[:cut_at]), "".join(lines[cut_at:])
        if before and not before.endswith("\n"):
            before += "\n"
        if before.strip() and not before.endswith("\n\n"):
            before += "\n"
        new_text = before + table + ("\n" + after if after.strip() else after)
    if toml_loads(new_text) != expected:
        raise EditError("the edit would change more than Mobster's entry")
    return new_text


def toml_remove_table(text, path):
    """``text`` without the table at ``path``; ``text`` itself when there is none."""
    data = toml_loads(text)
    if _dig(data, path) is None:
        return text
    expected = copy.deepcopy(data)
    del _dig(expected, path[:-1])[path[-1]]
    new_text, _ = _toml_cut(text, path)
    new_text = re.sub(r"\n{3,}", "\n\n", new_text)
    try:
        ok = toml_loads(new_text) in (expected, _pruned(expected, path[:-1]))
    except EditError:
        ok = False
    if not ok:
        raise EditError(f"{'.'.join(path)} is written inline or with dotted keys; remove it by hand")
    return new_text


# ---------------------------------------------------------------------------------------------------- YAML

def yaml_loads(text):
    import yaml
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as error:
        raise EditError(f"it isn't valid YAML ({str(error).splitlines()[0]})") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise EditError("it holds something other than a YAML mapping")
    return data


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _content(line):
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _yaml_entry(key, value, indent):
    import yaml
    dumped = yaml.safe_dump({key: value}, sort_keys=False, default_flow_style=False, allow_unicode=True, width=10_000)
    return "".join(" " * indent + line + "\n" for line in dumped.splitlines())


def _yaml_find(lines, section, key):
    """(section line, end of the section, the child indent, the key's start, the key's end) by line index."""
    top = re.compile(rf"^{re.escape(section)}\s*:\s*(?:#.*)?$")
    head = next((i for i, line in enumerate(lines) if top.match(line.rstrip("\n"))), None)
    if head is None:
        return None
    end, child = len(lines), None
    for i in range(head + 1, len(lines)):
        if _content(lines[i]) and _indent(lines[i]) == 0:
            end = i
            break
        if child is None and _content(lines[i]):
            child = _indent(lines[i])
    start = stop = None
    if child:
        pattern = re.compile(rf"^ {{{child}}}{re.escape(key)}\s*:\s*(?:#.*)?$")
        for i in range(head + 1, end):
            if start is None and pattern.match(lines[i].rstrip("\n")):
                start = i
            elif start is not None and _content(lines[i]) and _indent(lines[i]) <= child:
                stop = i
                break
        if start is not None and stop is None:
            stop = end
            while stop > start + 1 and not _content(lines[stop - 1]):
                stop -= 1
    return head, end, child, start, stop


def yaml_set_entry(text, section, key, value):
    """``text`` with ``section: {key: value}`` at the top level (Goose's ``extensions:``). Returns ``text``
    itself when it already holds exactly that value."""
    data = yaml_loads(text)
    existing = data.get(section)
    if existing is not None and not isinstance(existing, dict):
        raise EditError(f"{section} is not a mapping")
    if isinstance(existing, dict) and existing.get(key) == value:
        return text
    expected = copy.deepcopy(data)
    expected.setdefault(section, None)
    expected[section] = dict(expected[section] or {})
    expected[section][key] = copy.deepcopy(value)
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    found = _yaml_find(lines, section, key)
    empty = re.compile(rf"^{re.escape(section)}\s*:\s*\{{\s*\}}\s*(?:#.*)?$")
    if found is None and data.get(section) == {}:
        lines = [f"{section}:\n" if empty.match(line.rstrip("\n")) else line for line in lines]
        found = _yaml_find(lines, section, key)
    if found is None:
        if section in data:
            raise EditError(f"{section} is written in a form this installer doesn't edit; edit it by hand")
        body = "".join(lines)
        new_text = body + ("" if not body or body.endswith("\n") else "\n") + f"{section}:\n" + _yaml_entry(key, value, 2)
    else:
        head, end, child, start, stop = found
        indent = child or 2
        entry = _yaml_entry(key, value, indent)
        if start is not None:
            lines[start:stop] = [entry]
        else:
            at = end
            while at > head + 1 and not _content(lines[at - 1]):
                at -= 1
            lines[at:at] = [entry]
        new_text = "".join(lines)
    if yaml_loads(new_text) != expected:
        raise EditError("the edit would change more than Mobster's entry")
    return new_text


def yaml_remove_entry(text, section, key):
    """``text`` without ``key`` under ``section``; ``text`` itself when there is none."""
    data = yaml_loads(text)
    if not isinstance(data.get(section), dict) or key not in data[section]:
        return text
    expected = copy.deepcopy(data)
    del expected[section][key]
    lines = text.splitlines(keepends=True)
    found = _yaml_find(lines, section, key)
    if found is None or found[3] is None:
        raise EditError(f"{section}.{key} is written in a form this installer doesn't edit; remove it by hand")
    head, _end, _child, start, stop = found
    del lines[start:stop]
    if not expected[section]:
        lines[head] = f"{section}: {{}}\n"
    new_text = "".join(lines)
    if yaml_loads(new_text) != expected:
        raise EditError("the edit would change more than Mobster's entry")
    return new_text

"""Read a plugin folder: the Claude Code plugin layout, so one folder works in Claude Code and in Mobster."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import yaml

NAME = r"^[a-z0-9][a-z0-9-]{0,47}$"
MAX_SKILLS, MAX_WORKFLOWS, MAX_DESTINATIONS, MAX_SERVERS = 20, 20, 10, 10
SKILL_CHARS = 20_000           # a SKILL.md longer than this is refused
JSON_BYTES = 256_000
PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]{0,63})\}")
ROOT_VARIABLE = "CLAUDE_PLUGIN_ROOT"
RESERVED = frozenset({"preview", "preview-zip", "install"})   # /api/plugins/<name> would read as these routes

MANIFEST = {"type": "object", "required": ["name"], "properties": {
    "name": {"type": "string", "pattern": NAME},
    "version": {"type": "string", "maxLength": 40},
    "description": {"type": "string", "maxLength": 500},
    "author": {"anyOf": [{"type": "string", "maxLength": 200},
                         {"type": "object", "properties": {"name": {"type": "string", "maxLength": 200}}}]},
    "homepage": {"type": "string", "maxLength": 500}}}
SERVER = {"type": "object", "oneOf": [
    {"required": ["command"], "properties": {
        "type": {"enum": ["stdio"]}, "command": {"type": "string", "minLength": 1, "maxLength": 1000},
        "args": {"type": "array", "maxItems": 40, "items": {"type": "string", "maxLength": 1000}},
        "env": {"type": "object", "maxProperties": 20, "additionalProperties": {"type": "string", "maxLength": 4000}}}},
    {"required": ["url"], "properties": {
        "type": {"enum": ["http", "streamable-http"]}, "url": {"type": "string", "maxLength": 2000},
        "headers": {"type": "object", "maxProperties": 10,
                    "additionalProperties": {"type": "string", "maxLength": 8000}}}}]}
MCP = {"type": "object", "required": ["mcpServers"], "properties": {
    "mcpServers": {"type": "object", "maxProperties": MAX_SERVERS,
                   "propertyNames": {"pattern": r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$"},
                   "additionalProperties": SERVER}}}
WORKFLOW = {"type": "object", "required": ["name", "goal"], "properties": {
    "name": {"type": "string", "minLength": 1, "maxLength": 120},
    "goal": {"type": "string", "minLength": 1, "maxLength": 4000},
    "appId": {"type": ["string", "null"], "maxLength": 200}, "appName": {"type": ["string", "null"], "maxLength": 120},
    "engine": {"type": ["string", "null"]}, "outputFormat": {"type": "string"}, "outputSchema": {},
    "cron": {"type": ["string", "null"], "maxLength": 100}, "timezone": {"type": "string", "maxLength": 64},
    "enabled": {"type": "boolean"},
    # The result blocks it hands back (docs/results.mdx), shown on the Apps page as a preview.
    "produces": {"type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True,
                 "items": {"enum": ["review_queue", "table", "media_list", "evidence", "checklist", "markdown",
                                    "chart"]}}}}
DESTINATION = {"type": "object", "required": ["name", "url"], "properties": {
    "name": {"type": "string", "minLength": 1, "maxLength": 60}, "url": {"type": "string", "maxLength": 2000},
    "secret": {"type": "string", "maxLength": 200}, "screenshot": {"type": "boolean"}}}
# What a plugin's workflow keeps of its file: never a device, an account or a destination of this Mac.
WORKFLOW_FIELDS = ("name", "goal", "appId", "appName", "engine", "outputFormat", "outputSchema", "cron", "timezone",
                   "produces")


class PluginError(ValueError):
    """A plugin that can't be installed, with one sentence that says why."""


@dataclass
class Plugin:
    name: str
    version: str
    description: str
    root: Path
    author: str = ""                                   # who makes it, from plugin.json's author (a name, or {"name"})
    # {"name", "transport", "command"?, "args"?, "env"?, "url"?, "token"?}
    servers: list = field(default_factory=list)
    skills: list = field(default_factory=list)         # {"name", "description", "file", "chars"}
    workflows: list = field(default_factory=list)      # create bodies (enabled false)
    destinations: list = field(default_factory=list)   # {"name", "url", "secret"?, "screenshot"?}
    asks: list = field(default_factory=list)           # {"key", "label", "secret"}

    def summary(self):
        """What the API shows before and after install: never a value."""
        return {"name": self.name, "version": self.version, "description": self.description, "author": self.author,
                "connectors": [{"name": s["name"], "transport": s["transport"],
                                "where": s.get("url") or " ".join([Path(s["command"]).name, *s.get("args", ())])[:300]}
                               for s in self.servers],
                "skills": [{"name": s["name"], "description": s["description"]} for s in self.skills],
                "workflows": [{"name": w["name"], "goal": w["goal"][:300],
                               **({"produces": w["produces"]} if w.get("produces") else {})} for w in self.workflows],
                "destinations": [{"name": d["name"]} for d in self.destinations],
                "asks": list(self.asks)}


def _json(path, schema, what):
    try:
        if path.stat().st_size > JSON_BYTES:
            raise PluginError(f"{what} is too large.")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise PluginError(f"Mobster couldn't read {what}.") from None
    except ValueError:
        raise PluginError(f"{what} isn't valid JSON.") from None
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError as error:
        where = "/".join(str(p) for p in error.absolute_path)
        raise PluginError(f"{what} doesn't fit the plugin format" + (f" at {where}" if where else "") + ".") from None
    return value


def frontmatter(text):
    """({key: value}, body) of a SKILL.md: a leading ``---`` block."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    block, body = text[3:end], text[end + 4:].lstrip("\n")
    try:
        loaded = yaml.safe_load(block)
    except yaml.YAMLError:
        loaded = None
    if isinstance(loaded, dict):
        return {str(key).strip().lower(): str(value).strip() for key, value in loaded.items()
                if isinstance(value, (str, int, float))}, body
    meta = {}
    for line in block.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            meta[key.strip().lower()] = value.strip().strip('"').strip("'")
    return meta, body


def _ask(asks, key, label, secret):
    if not any(a["key"] == key for a in asks):
        asks.append({"key": key, "label": label, "secret": secret})


def _placeholders(value, asks, label, secret=True):
    for name in PLACEHOLDER.findall(value):
        if name != ROOT_VARIABLE:
            _ask(asks, name, label, secret)



def _author(value):
    """plugin.json's author as one line: the string itself, or the object's name. Empty when it names no one."""
    name = value.get("name") if isinstance(value, dict) else value
    return " ".join(str(name or "").split())[:200]

def read(root):
    root = Path(root)
    manifest_path = root / ".claude-plugin" / "plugin.json"
    if not manifest_path.is_file():
        raise PluginError("This isn't a plugin: it has no .claude-plugin/plugin.json.")
    manifest = _json(manifest_path, MANIFEST, "plugin.json")
    if manifest["name"] in RESERVED:
        raise PluginError(f"A plugin can't be called {manifest['name']}. Rename it in plugin.json.")
    plugin = Plugin(name=manifest["name"], version=str(manifest.get("version") or ""),
                    description=" ".join(str(manifest.get("description") or "").split()), root=root,
                    author=_author(manifest.get("author")))

    mcp_path = root / ".mcp.json"
    if mcp_path.is_file():
        servers = _json(mcp_path, MCP, ".mcp.json")["mcpServers"]
        for key, spec in servers.items():
            if "command" in spec:
                env = dict(spec.get("env") or {})
                for name, value in env.items():
                    if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", name):
                        raise PluginError(f"{key}'s environment name {name[:40]} isn't in capitals.")
                    _placeholders(value, plugin.asks, f"Used by the connector {key}")
                for arg in [spec["command"], *spec.get("args", ())]:
                    _placeholders(arg, plugin.asks, f"Used by the connector {key}")
                plugin.servers.append({"name": key, "transport": "stdio", "command": spec["command"],
                                       "args": list(spec.get("args", ())), "env": env})
            else:
                headers = dict(spec.get("headers") or {})
                token = None
                for header, value in headers.items():
                    if header.lower() != "authorization":
                        raise PluginError(f"{key} sends a {header[:40]} header; Mobster sends only Authorization.")
                    match = re.fullmatch(r"\s*Bearer\s+(\S+)\s*", value)
                    if not match:
                        raise PluginError(f"{key}'s Authorization header must be Bearer and a token.")
                    token = match.group(1)
                    _placeholders(token, plugin.asks, f"Used by the connector {key}")
                if PLACEHOLDER.search(spec["url"]):
                    raise PluginError(f"{key}'s URL can't hold a ${{…}} value. Put the secret in its token.")
                plugin.servers.append({"name": key, "transport": "http", "url": spec["url"], "token": token})

    skills_dir = root / "skills"
    if skills_dir.is_dir():
        for folder in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            file = folder / "SKILL.md"
            if not file.is_file():
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                raise PluginError(f"Mobster couldn't read skills/{folder.name}/SKILL.md.") from None
            if len(text) > SKILL_CHARS:
                raise PluginError(f"skills/{folder.name}/SKILL.md is longer than {SKILL_CHARS:,} characters.")
            meta, body = frontmatter(text)
            description = " ".join(meta.get("description", "").split())
            if not description:
                raise PluginError(f"skills/{folder.name}/SKILL.md needs a description in its frontmatter.")
            plugin.skills.append({"name": (meta.get("name") or folder.name)[:60], "description": description[:500],
                                  "file": str(file.relative_to(root)), "chars": len(body)})
        if len(plugin.skills) > MAX_SKILLS:
            raise PluginError(f"A plugin has at most {MAX_SKILLS} skills.")

    for kind, schema, limit, target in (("workflows", WORKFLOW, MAX_WORKFLOWS, plugin.workflows),
                                         ("destinations", DESTINATION, MAX_DESTINATIONS, plugin.destinations)):
        folder = root / "mobster" / kind
        if not folder.is_dir():
            continue
        files = sorted(folder.glob("*.json"))
        if len(files) > limit:
            raise PluginError(f"A plugin has at most {limit} {kind}.")
        for file in files:
            value = _json(file, schema, f"mobster/{kind}/{file.name}")
            if kind == "workflows":
                target.append({k: value[k] for k in WORKFLOW_FIELDS if k in value})
            else:
                if PLACEHOLDER.search(value["name"]):
                    raise PluginError(f"mobster/destinations/{file.name}'s name can't hold a ${{…}} value.")
                _placeholders(value["url"], plugin.asks, f"Used by the destination {value['name']}")
                if value.get("secret"):
                    _placeholders(value["secret"], plugin.asks, f"Used by the destination {value['name']}")
                target.append({k: value[k] for k in ("name", "url", "secret", "screenshot") if k in value})

    if not (plugin.servers or plugin.skills or plugin.workflows or plugin.destinations):
        raise PluginError("This plugin adds nothing Mobster uses: no MCP servers, skills, workflows or destinations.")
    return plugin


def fill(text, values, root):
    """``text`` with ``${NAME}`` replaced by the value given at install, and ``${CLAUDE_PLUGIN_ROOT}`` by ``root``."""
    def one(match):
        name = match.group(1)
        if name == ROOT_VARIABLE:
            return str(root)
        if name not in values or not isinstance(values[name], str) or not values[name].strip():
            raise PluginError(f"Fill in {name} to install this plugin.")
        return values[name].strip()
    return PLACEHOLDER.sub(one, text)

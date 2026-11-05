"""Import the MCP servers you already set up in Claude Desktop, Claude Code, Cursor or Codex: Settings ›
Connectors lists each one with an Add button (GET /api/connectors/imports), and Add makes it a connector (POST
/api/connectors/imports) without retyping its command, URL or keys."""

import json
import os
from pathlib import Path

from ..integrations import clients as agent_clients
from ..integrations import jsonc, textedit
from ..secret_filter import redact
from . import client as mcp

SOURCES = (("claude-desktop", agent_clients.ClaudeDesktop), ("claude-code", agent_clients.ClaudeCode),
           ("cursor", agent_clients.Cursor), ("codex", agent_clients.Codex))
MAX_SERVERS = 60              # per file
MAX_BYTES = 4_000_000         # a config larger than this is skipped (~/.claude.json can grow; it holds history too)
SELF = ("mobster", "mobster-agent")


def _read(path):
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _servers(client, path):
    """{name: entry} from one app's file, or {} when it has none or can't be read."""
    text = _read(path)
    if text is None:
        return {}
    try:
        if isinstance(client, agent_clients.Codex):
            servers = textedit.toml_loads(text).get("mcp_servers")
        else:
            try:
                data = json.loads(text)      # fast for a large ~/.claude.json
            except ValueError:
                data = jsonc.loads(text)     # comments and trailing commas (Cursor allows them)
            servers = data
            for key in client.container:
                servers = servers.get(key) if isinstance(servers, dict) else None
    except (ValueError, jsonc.JsoncError, textedit.EditError):
        return {}
    if not isinstance(servers, dict):
        return {}
    return {str(k): v for k, v in list(servers.items())[:MAX_SERVERS] if isinstance(v, dict)}


def _is_mobster(name, entry):
    if name.casefold() in SELF:
        return True
    command = entry.get("command")
    if isinstance(command, str) and os.path.basename(command) in SELF:
        return True
    args = entry.get("args") if isinstance(entry.get("args"), list) else []
    return "mobile_agent" in args and "mcp" in args


def _token_of(entry):
    """A bearer token from an entry's headers (Authorization: Bearer …), else None; and whether other headers were left
    out."""
    headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}
    token, others = None, False
    for key, value in headers.items():
        if str(key).casefold() == "authorization" and isinstance(value, str) and value.startswith("Bearer "):
            token = value[7:].strip() or None
        else:
            others = True
    if isinstance(entry.get("bearer_token"), str):   # Codex's inline form
        token = entry["bearer_token"].strip() or token
    return token, others


def describe(entry):
    """What Add would make of ``entry``: {"transport", "command", "args", "url", "envNames", "token", "headers",
    "problem"}."""
    command, url = entry.get("command"), entry.get("url") or entry.get("serverUrl")
    kind = str(entry.get("type") or entry.get("transport") or "").casefold()
    out = {"transport": None, "command": None, "args": [], "url": None, "envNames": [], "token": False,
           "headers": False, "problem": None}
    if isinstance(command, str) and command.strip():
        args = entry.get("args") if isinstance(entry.get("args"), list) else []
        env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
        out.update(transport="stdio", command=command.strip(), args=[str(a) for a in args],
                   envNames=sorted(str(k) for k in env))
        return out
    if isinstance(url, str) and url.strip():
        out["url"] = url.strip()
        if kind == "sse" or url.rstrip("/").endswith("/sse"):
            out["problem"] = "It uses SSE, an older MCP transport Mobster doesn't speak. Use the server's /mcp URL."
            return out
        try:
            mcp.validate_url(url)
        except ValueError as error:
            out["problem"] = f"{error}."
            return out
        token, others = _token_of(entry)
        out.update(transport="http", token=bool(token), headers=others)
        return out
    out["problem"] = "It has no command or URL Mobster can use."
    return out


def found(env=None):
    """Every server the other apps have, as (source id, source name, name, entry) tuples, Mobster left out."""
    env = env or agent_clients.Env.current()
    out = []
    for source, cls in SOURCES:
        client = cls()
        for path in client.paths(env):
            for name, entry in _servers(client, Path(path)).items():
                if not _is_mobster(name, entry):
                    out.append((source, client.name, name, entry))
    return out


def public(found_servers, connectors):
    """The list for GET /api/connectors/imports. ``connectors``: the existing rows (to say which are added). It never
    carries a value: env values and tokens stay on this Mac and go straight to the Keychain on Add."""
    rows = []
    for source, source_name, name, entry in found_servers:
        info = describe(entry)
        added = next((c for c in connectors if (info["transport"] == "stdio" and c["transport"] == "stdio" and
                                                os.path.basename(c.get("command") or "") ==
                                                os.path.basename(info["command"] or "") and
                                                list(c.get("args") or []) == info["args"])
                      or (info["transport"] == "http" and c.get("url") and info["url"] and
                          mcp.hint(c["url"]) == mcp.hint(info["url"]))), None)
        rows.append({
            "source": source, "sourceName": source_name, "key": name[:80], "name": suggested_name(name),
            "transport": info["transport"],
            "command": os.path.basename(info["command"]) if info["command"] else None,
            "args": [redact(a)[:200] for a in info["args"][:40]],
            "url": mcp.hint(info["url"]) if info["url"] and not info["problem"] else None,
            "envNames": info["envNames"][:20], "token": info["token"], "headers": info["headers"],
            "problem": info["problem"], "added": added["id"] if added else None})
    return rows


def suggested_name(key):
    """A connector name from a config's key: "brave-search" stays, cut to 40 characters."""
    text = " ".join(str(key).split()) or "Server"
    return text[:40]


def body_for(found_servers, source, key, name=None):
    """The POST /api/connectors body for one found server, with its secrets (env values, a header's bearer token) read
    now from the app's own file."""
    match = next((entry for src, _, k, entry in found_servers if src == source and k == key), None)
    if match is None:
        raise ValueError("That server isn't in the app's settings any more. Look again")
    info = describe(match)
    if info["problem"]:
        raise ValueError(info["problem"].rstrip("."))
    body = {"name": name or suggested_name(key)}
    if info["transport"] == "stdio":
        env = match.get("env") if isinstance(match.get("env"), dict) else {}
        body.update(command=info["command"], args=info["args"])
        if env:
            body["env"] = {str(k): str(v) for k, v in env.items()}
    else:
        body["url"] = info["url"]
        token, _ = _token_of(match)
        if token:
            body["token"] = token
    return body

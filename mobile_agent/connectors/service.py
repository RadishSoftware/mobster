"""Connectors and their live sessions: one ConnectorService per Runtime (harness_api.register_service)."""

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict

from ..api_errors import APIError
from ..service_support import ServiceRegistry, ms
from . import client as mcp
from . import oauth, picker, shared
from . import presets as preset_list
from . import store as connector_store
from . import vault as connector_vault

log = logging.getLogger("mobster.connectors")

IDLE_SECONDS = 300.0
NAME = re.compile(r"[^\s].{0,39}", re.S)
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(?::\d{1,5})?")
MAX_ARGS, ARG_CHARS, MAX_ENV, ENV_CHARS, TOKEN_CHARS = 40, 1000, 20, 4000, 8000
DESCRIPTION_CHARS = 4000      # what is kept of a tool's description (the list shows a line; a lookup all of it)
SCHEMA_CHARS = 24_000         # a tool's input schema up to this is kept whole, a bigger one without its words
LEAN_KEYS = frozenset({"description", "examples", "title", "$comment", "$schema"})
NAMED_SCHEMAS = frozenset({"properties", "patternProperties", "$defs", "definitions"})
EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
TEST_CONNECT, TEST_LIST = 9.0, 5.0
KEYCHAIN = ("Mobster couldn't read this connector's sign-in from the Keychain. Unlock your Mac's "
            "Keychain, then try again.")

_services = ServiceRegistry()


def service_for(runtime):
    return _services.get(runtime)


def start(runtime):
    _services.add(runtime, ConnectorService(runtime))


def close(runtime):
    service = _services.pop(runtime)
    if service is not None:
        service.close()


def slug(name):
    """How the model names a connector in CALL_TOOL: lower case, words joined by "_"
    ("Google Drive" -> google_drive)."""
    return re.sub(r"[^a-z0-9]+", "_", str(name).casefold()).strip("_")[:40] or "server"


def lean_schema(schema):
    """A JSON schema without its words (descriptions, examples, titles): the shape a call must match, whole."""
    if isinstance(schema, list):
        return [lean_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    lean = {}
    for key, value in schema.items():
        if key in NAMED_SCHEMAS and isinstance(value, dict):
            lean[key] = {name: lean_schema(sub) for name, sub in value.items()}
        elif key not in LEAN_KEYS:
            lean[key] = lean_schema(value)
    return lean


def trim_tool(tool):
    """A tools/list entry as the store keeps it: name, description, input schema, read-only hint."""
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {"type": "object"}
    if len(json.dumps(schema)) > SCHEMA_CHARS:
        schema = lean_schema(schema)
    if len(json.dumps(schema)) > SCHEMA_CHARS:
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        schema = {"type": "object", "required": [r for r in schema.get("required") or () if isinstance(r, str)][:20],
                  "properties": {k: {"type": (v or {}).get("type", "string") if isinstance(v, dict) else "string"}
                                 for k, v in list(properties.items())[:40]}}
    annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
    title = tool.get("title") or annotations.get("title")
    return {"name": str(tool["name"])[:128], "title": " ".join(str(title).split())[:80] if title else None,
            "description": " ".join(str(tool.get("description") or "").split())[:DESCRIPTION_CHARS],
            "inputSchema": schema, "readOnly": annotations.get("readOnlyHint") is True}


def stdio_env(saved_env):
    """The environment a stdio server starts with: a few basics and its own saved values, nothing of Mobster's."""
    folders = dict.fromkeys([*os.environ.get("PATH", "").split(os.pathsep), *EXTRA_PATH])
    path = os.pathsep.join(folder for folder in folders if folder)
    env = {"PATH": path}
    for name in ("HOME", "LANG", "USER", "TMPDIR", "LC_ALL"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    env.update({k: str(v) for k, v in (saved_env or {}).items() if ENV_NAME.fullmatch(str(k))})
    return env


def validate_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value.strip()):
        raise ValueError("A connector's name is 1 to 40 characters")
    if slug(value) == "tools":
        # CALL_TOOL tools.search is the agent's tool search (skill.SEARCH).
        raise ValueError("Mobster keeps the name Tools for its tool search. Give this connector another name")
    return " ".join(value.split())


def validate_command(command, args):
    if not isinstance(command, str) or not command.strip() or len(command) > ARG_CHARS or "\n" in command:
        raise ValueError("A server's command is the program to run, such as npx")
    if args is None:
        args = []
    if not isinstance(args, list) or len(args) > MAX_ARGS or not all(
            isinstance(a, str) and len(a) <= ARG_CHARS and "\n" not in a for a in args):
        raise ValueError(f"A server's arguments are at most {MAX_ARGS} short words")
    return command.strip(), list(args)


def validate_env(env):
    if env is None:
        return {}
    if not isinstance(env, dict) or len(env) > MAX_ENV or not all(
            isinstance(k, str) and ENV_NAME.fullmatch(k) and isinstance(v, str) and len(v) <= ENV_CHARS
            for k, v in env.items()):
        raise ValueError(f"A server's environment is at most {MAX_ENV} NAME=value pairs, names in capitals")
    return dict(env)


def preset_url(preset, host):
    """Where a preset connects: its own URL, or ``host``, your own server of that vendor, at the preset's
    ``instance_path`` (GitLab self-managed)."""
    if host is None:
        return preset["url"]
    if not preset.get("instance_path"):
        raise ValueError(f"{preset['name']} has one address. Leave the host out")
    if not isinstance(host, str):
        raise ValueError("That isn't a server's address. Type the host alone, such as git.example.com")
    text = host.strip().lower()
    if text.startswith("http://"):
        raise ValueError("That address is plain http, and Mobster signs in only over https. Type the host alone, such "
                         "as git.example.com")
    text = text.removeprefix("https://").rstrip("/")
    if len(text) > 253 or not HOST.fullmatch(text):
        raise ValueError("That isn't a server's address. Type the host alone, such as git.example.com")
    return mcp.validate_url(f"https://{text}{preset['instance_path']}")


def validate_token(token):
    if token is None:
        return None
    if not isinstance(token, str) or len(token.strip()) > TOKEN_CHARS or any(c in token.strip() for c in "\r\n "):
        raise ValueError("That token isn't valid. Paste it again")
    return token.strip() or None


class ToolGone(mcp.NotSent):
    """The server no longer lists the tool: nothing was sent, and the connection is fine."""


class ConnectorService:
    def __init__(self, runtime, *, vault=None, clock=time.time, connect=mcp.connect, sign_in=oauth.SignIn,
                 refresh=oauth.refresh, revoke=oauth.revoke, import_env=None):
        self.runtime = runtime
        self.apps = shared.Apps()     # Mobster's own OAuth apps (Slack, Google Drive)
        self.import_env = import_env  # integrations.clients.Env for imports.py (tests); None: this Mac's
        self.store = connector_store.ConnectorStore(runtime.journal)
        self.vault = vault if vault is not None else connector_vault.backend()
        self.clock, self.connect, self.sign_in_class, self.refresh_tokens = clock, connect, sign_in, refresh
        self.revoke_tokens = revoke
        self._sessions = {}           # connector id -> [session, last used]
        self._locks = {}              # connector id -> threading.Lock (one request at a time per session)
        self._refreshing = {}         # connector id -> threading.Lock (one token renewal at a time)
        self._lock = threading.Lock()
        self._credential_versions = {}  # connector id -> generation of its current credentials/browser attempt
        # connector id -> {"state", "message", "flow"}. state: waiting (for the browser), done, failed (message says
        # why) or awaiting_approval (the browser never came back from a vendor whose admin approves each client).
        self._sign_ins = {}
        self._connecting = set()      # connector ids a test or reconnect is connecting now
        self._closed = False

    def close(self):
        with self._lock:
            self._closed = True
            sessions, self._sessions = list(self._sessions.values()), {}
            flows = [s.get("flow") for s in self._sign_ins.values()]
        for session, _ in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
        for flow in flows:
            if flow is not None:
                flow.cancel()

    def _credentials_current(self, connector_id, version):
        # Called under _lock; network work never owns credentials across a sign-out or replacement.
        if (self._closed or self._credential_versions.get(connector_id, 0) != version
                or not self.store.get(connector_id)):
            raise mcp.NotSent("This connector's sign-in changed. Try again.")

    def _invalidate_credentials(self, connector_id):
        self._credential_versions[connector_id] = self._credential_versions.get(connector_id, 0) + 1
        previous = self._sign_ins.pop(connector_id, None)
        return previous.get("flow") if previous else None, self._sessions.pop(connector_id, None)

    @staticmethod
    def _close_session(entry):
        if entry is not None:
            try:
                entry[0].close()
            except Exception:  # noqa: BLE001
                pass

    def _drop(self, connector_id):
        with self._lock:
            entry = self._sessions.pop(connector_id, None)
        self._close_session(entry)

    def _reap(self):
        now = time.monotonic()
        with self._lock:
            idle = [cid for cid, (_, used) in self._sessions.items() if now - used > IDLE_SECONDS]
        for connector_id in idle:
            self._drop(connector_id)

    def public(self, row):
        test = row.get("last_test") if isinstance(row.get("last_test"), dict) else None
        flow = self._sign_ins.get(row["id"])
        preset = preset_list.preset(row["preset"]) if row.get("preset") else None
        needs = row["auth"] == "none" and (preset is not None or bool(test and test.get("code") == "auth"))
        return {
            "id": row["id"], "name": row["name"], "slug": slug(row["name"]), "transport": row["transport"],
            "url": mcp.hint(row["url"]) if row.get("url") else None,
            "command": row.get("command") if row["transport"] == "stdio" else None,
            "args": row.get("args") if row["transport"] == "stdio" else None,
            "auth": row["auth"], "needsSignIn": needs, "enabled": row["enabled"], "preset": row.get("preset"),
            "plugin": row.get("plugin"), "picksFiles": bool((preset or {}).get("picker")),
            "tools": [{"name": t["name"], "title": t.get("title"), "description": t.get("description", "")[:300],
                       "mode": connector_store.mode_of(row, t["name"]), "readOnly": bool(t.get("readOnly"))}
                      for t in row.get("tools") or ()],
            "toolsAt": ms(row.get("tools_at")),
            "lastTest": {"ok": bool(test.get("ok")), "at": ms(test.get("at")), "message": test.get("message"),
                         "code": test.get("code")} if test else None,
            "signIn": {"state": flow["state"], "message": flow.get("message")} if flow else None,
            "health": self.health(row, needs, test, flow),
            "createdAt": ms(row["created_at"]),
        }

    def health(self, row, needs_sign_in, test, flow):
        """One word for the row's dot: ``connecting`` (signing in or reconnecting), ``needs_sign_in``, ``connected`` (a
        live session answers), ``failed`` (the last connect or call didn't work), or ``idle`` (no session open now;
        Mobster connects when a task needs it)."""
        if (flow and flow.get("state") == "waiting") or row["id"] in self._connecting:
            return "connecting"
        if needs_sign_in or (test and not test.get("ok") and test.get("code") == "auth"):
            return "needs_sign_in"
        with self._lock:
            entry = self._sessions.get(row["id"])
        if entry is not None and entry[0].ready:
            return "connected"
        if test and not test.get("ok"):
            return "failed"
        return "idle"

    def list(self):
        return [self.public(row) for row in self.store.all()]

    def presets(self):
        return preset_list.public(self.apps)

    def presets_not_yet(self):
        return preset_list.not_yet(self.apps)

    def _get(self, connector_id):
        row = self.store.get(connector_id) if isinstance(connector_id, str) else None
        if row is None:
            raise APIError("That connector was removed.", 404, "not_found")
        return row

    def find(self, wanted):
        """A connector by id, name or slug (case ignored). ValueError when none or several match."""
        if not isinstance(wanted, str) or not wanted.strip():
            raise ValueError("Name a connector, or give its id")
        wanted = wanted.strip()
        rows = self.store.all()
        found = [r for r in rows if r["id"] == wanted.lower()] or [
            r for r in rows if r["name"].casefold() == " ".join(wanted.split()).casefold()
            or slug(r["name"]) == slug(wanted)]
        if not found:
            raise ValueError(f"No connector is called {wanted[:40]}. Add it in Settings › Connectors.")
        if len(found) > 1:
            raise ValueError(f"Several connectors are called {wanted[:40]}. Use its id.")
        return found[0]

    def _unique(self, name, but=None):
        for row in self.store.all():
            if row["id"] != but and (row["name"].casefold() == name.casefold() or slug(row["name"]) == slug(name)):
                raise ValueError(f"You already have a connector called {row['name']}. Give this one another name.")

    def add(self, body, *, plugin=None):
        """``body``: {"preset", "host"?} or {"name", "url"} or {"name", "command", "args"?}, with "token"?, "env"?,
        "clientId"?, "clientSecret"? and "modes"?. Secrets go to the Keychain; the row never holds one."""
        allowed = {"name", "preset", "host", "url", "command", "args", "env", "token", "clientId", "clientSecret",
                   "modes", "enabled"}
        if not isinstance(body, dict) or set(body) - allowed:
            raise ValueError("A connector has a name and a url or a command, and optionally a token or env values")
        preset = None
        if body.get("preset") is not None:
            preset = preset_list.preset(body["preset"])
            if preset is None:
                raise ValueError("There's no such preset")
            refused = preset_list.refusal(preset, self.apps)
            if refused:
                raise ValueError(refused)
        name = validate_name(body.get("name") or (preset or {}).get("name"))
        if preset is not None:
            transport, url, command, args = "http", preset_url(preset, body.get("host")), None, []
        elif body.get("host") is not None:
            raise ValueError("A host goes with a preset")
        elif body.get("url") is not None:
            transport, url, command, args = "http", mcp.validate_url(body["url"]), None, []
            if body.get("command") is not None:
                raise ValueError("A connector has a url or a command, not both")
        elif body.get("command") is not None:
            transport, url = "stdio", None
            command, args = validate_command(body["command"], body.get("args"))
        else:
            raise ValueError("Give the server's URL, or the command that starts it")
        env = validate_env(body.get("env")) if transport == "stdio" else {}
        if transport == "http" and body.get("env"):
            raise ValueError("Only a command takes environment values")
        token = validate_token(body.get("token"))
        if token and transport == "stdio":
            raise ValueError("A command's keys go in its environment values")
        client_id = validate_token(body.get("clientId"))
        client_secret = validate_token(body.get("clientSecret"))
        modes = self._clean_modes(body.get("modes"))
        enabled = body.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError("enabled is true or false")
        self._unique(name)
        if transport == "stdio" and plugin is None:
            self._must_be_mcp(name, command, args, env)
        if self.store.count() >= connector_store.MAX_CONNECTORS:
            raise APIError(f"You have {connector_store.MAX_CONNECTORS} connectors. Remove one first.", 409,
                           "connector_limit")
        connector_id = uuid.uuid4().hex[:12]
        secrets = {"token": token, "env": env or None,
                   "client": {"client_id": client_id, "client_secret": client_secret} if client_id else None}
        try:
            self.vault.put(connector_id, secrets)
        except connector_vault.VaultError as error:
            raise APIError(f"{error}. Nothing was saved.", 503, "keychain_unavailable") from None
        now = self.clock()
        try:
            row = self.store.add({"id": connector_id, "name": name, "transport": transport, "command": command,
                                  "args": args, "url": url, "auth": "token" if token else "none", "enabled": enabled,
                                  "preset": preset["key"] if preset else None, "plugin": plugin, "tools": [],
                                  "modes": modes, "created_at": now, "updated_at": now})
        except Exception:
            self._forget_secrets(connector_id)
            raise
        return self.public(row)

    def importable(self):
        from . import imports
        return imports.public(imports.found(self.import_env), self.store.all())

    def add_import(self, body):
        """{"source", "key", "name"?}: add one server another app has, its env values and token read from that
        app's file now (never from the request)."""
        from . import imports
        if (not isinstance(body, dict) or set(body) - {"source", "key", "name"}
                or not isinstance(body.get("source"), str) \
                or not isinstance(body.get("key"), str)):
            raise ValueError("Name the app and the server to import")
        name = body.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError("A connector's name is 1 to 40 characters")
        draft = imports.body_for(imports.found(self.import_env), body["source"], body["key"], name)
        return self.add(draft)

    def _clean_modes(self, modes):
        if modes is None:
            return {}
        if not isinstance(modes, dict) or len(modes) > mcp.MAX_TOOLS or not all(
                isinstance(k, str) and 0 < len(k) <= 128 and v in connector_store.MODES for k, v in modes.items()):
            raise ValueError('modes is {"tool name": "read" | "ask" | "off"}')
        return dict(modes)

    def update(self, connector_id, body):
        """{"name"?, "enabled"?, "url"? or "command" and "args"? (your own server's), "token"? (null signs out), "env"?,
        "modes"? (merged), "clientId"?, "clientSecret"?}."""
        row = self._get(connector_id)
        allowed = {"name", "enabled", "url", "command", "args", "token", "env", "modes", "clientId", "clientSecret"}
        if not isinstance(body, dict) or set(body) - allowed:
            raise ValueError("A connector's name, address, enabled, token, env and modes can change")
        address = self._new_address(row, body)
        if address and row["transport"] == "stdio":
            env = validate_env(body["env"]) if "env" in body else self._secrets(row).get("env")
            self._must_be_mcp(row["name"], address["command"], address["args"], env)
        changes = {}
        if "name" in body:
            name = validate_name(body["name"])
            self._unique(name, but=connector_id)
            changes["name"] = name
        if "enabled" in body:
            if type(body["enabled"]) is not bool:
                raise ValueError("enabled is true or false")
            changes["enabled"] = body["enabled"]
        if "modes" in body:
            changes["modes"] = {**row["modes"], **self._clean_modes(body["modes"])}
        secret_keys = {"token", "env", "clientId", "clientSecret"} & set(body)
        flow, session, issued = None, None, None
        if secret_keys or address:
            try:
                with self._lock:
                    row = self._get(connector_id)
                    if self._closed:
                        raise APIError("Mobster is quitting.", 503, "unavailable")
                    try:
                        saved = self.vault.get(connector_id)
                    except connector_vault.VaultError:
                        raise APIError(KEYCHAIN, 503, "keychain_unavailable") from None
                    address = self._new_address(row, body)   # again, under the lock: another change may have landed
                    if address:
                        # A new address ends the session and sign-in. The old server issued its OAuth tokens: revoked
                        # there below, and never sent to the new one. On a new host, a pasted token and OAuth app go
                        # too.
                        issued = saved.pop("oauth", None)
                        if row["transport"] == "http" and shared.origin(address["url"]) != shared.origin(row["url"]):
                            saved["token"], saved["client"] = None, None
                        changes.update(address, last_test=None, auth="token" if saved.get("token") else "none")
                    if "token" in body:
                        if row["transport"] == "stdio" and body["token"]:
                            raise ValueError("A command's keys go in its environment values")
                        saved["token"] = validate_token(body["token"])
                        saved.pop("oauth", None)
                        changes["auth"] = "token" if saved["token"] else "none"
                    if "env" in body:
                        if row["transport"] != "stdio":
                            raise ValueError("Only a command takes environment values")
                        saved["env"] = validate_env(body["env"]) or None
                    if "clientId" in body or "clientSecret" in body:
                        client = dict(saved.get("client") or {})
                        if "clientId" in body:
                            client["client_id"] = validate_token(body["clientId"])
                        if "clientSecret" in body:
                            client["client_secret"] = validate_token(body["clientSecret"])
                        saved["client"] = client if client.get("client_id") else None
                    try:
                        self.vault.put(connector_id, saved)
                    except connector_vault.VaultError:
                        raise APIError(KEYCHAIN, 503, "keychain_unavailable") from None
                    flow, session = self._invalidate_credentials(connector_id)
                    if changes:
                        changes["updated_at"] = self.clock()
                        row = self.store.update(connector_id, **changes)
                        changes = {}
            finally:
                self._close_session(session)
                if flow is not None:
                    flow.cancel()
            try:
                if issued:
                    self.revoke_tokens(issued)
            except Exception:  # noqa: BLE001 -- moved on this Mac whatever the old server says
                pass
        if changes:
            changes["updated_at"] = self.clock()
            row = self.store.update(connector_id, **changes)
        if changes.get("enabled") is False:
            self._drop(connector_id)
        return self.public(row)

    @staticmethod
    def _new_address(row, body):
        """The url, or command and args, ``body`` moves your own server to, or None; a preset's or plugin's stay put."""
        if not {"url", "command", "args"} & set(body):
            return None
        if row.get("preset") or row.get("plugin"):
            raise ValueError("Only your own server's address can change")
        if row["transport"] == "http":
            if "command" in body or "args" in body:
                raise ValueError("A server with a URL can't take a command")
            url = mcp.validate_url(body["url"])
            return {"url": url} if url != row["url"] else None
        if "url" in body:
            raise ValueError("A command's server can't take a URL")
        command, args = validate_command(body.get("command", row["command"]), body.get("args", row.get("args")))
        unchanged = (command, args) == (row["command"], row.get("args") or [])
        return None if unchanged else {"command": command, "args": args}

    def remove(self, connector_id):
        flow, session, tokens = None, None, None
        try:
            with self._lock:
                self._get(connector_id)
                flow, session = self._invalidate_credentials(connector_id)
                try:
                    tokens = self.vault.get(connector_id).get("oauth")
                except connector_vault.VaultError:
                    tokens = None
                self._forget_secrets(connector_id)
                self.store.delete(connector_id)
        finally:
            self._close_session(session)
            if flow is not None:
                flow.cancel()
        # Like Disconnect: gone here first, then the server that issued the sign-in is asked to revoke it.
        try:
            if tokens:
                self.revoke_tokens(tokens)
        except Exception:  # noqa: BLE001 -- removed on this Mac whatever the server says
            pass
        return connector_id

    def _forget_secrets(self, connector_id):
        try:
            self.vault.delete(connector_id)
        except connector_vault.VaultError:
            log.warning("connectors: couldn't remove a connector's Keychain item")

    def _secrets(self, row):
        try:
            return self.vault.get(row["id"])
        except connector_vault.VaultError:
            raise mcp.NotSent(KEYCHAIN) from None

    def _token(self, row, saved, force_refresh=False):
        """Read current credentials; serialize renewals without blocking fresh tokens on an older refresh."""
        connector_id = row["id"]
        with self._lock:
            version = self._credential_versions.get(connector_id, 0)
            self._credentials_current(connector_id, version)
            original = saved.get("oauth") or {}
            current = self._secrets(row)
            saved.clear()
            saved.update(current)
            if saved.get("token"):
                return saved["token"]
            tokens = saved.get("oauth")
            if not tokens:
                if original:
                    raise mcp.AuthNeeded(f"{row['name']} isn't signed in.")
                return None
            renewed = tokens.get("access_token") != original.get("access_token")
            if not oauth.expiring(tokens, self.clock) and (not force_refresh or renewed):
                return tokens.get("access_token")
        with self._refresh_lock(connector_id):
            with self._lock:
                self._credentials_current(connector_id, version)
                stored = self._secrets(row).get("oauth")
                if not stored:
                    raise mcp.AuthNeeded(f"{row['name']} isn't signed in.")
                # A sibling may have renewed while this caller waited for the refresh lock.
                if stored != tokens and not oauth.expiring(stored, self.clock):
                    saved["oauth"] = stored
                    return stored.get("access_token")
                tokens = stored
            try:
                refreshed = self.refresh_tokens(tokens, self.clock)
            except oauth.OAuthError as error:
                if error.retry:
                    raise mcp.NotSent(f"Mobster couldn't renew {row['name']}'s sign-in. {error.message}") from None
                raise mcp.AuthNeeded(f"{row['name']} needs you to sign in again.") from None
            with self._lock:
                self._credentials_current(connector_id, version)
                current = self._secrets(row)
                if current.get("oauth") != tokens:
                    raise mcp.NotSent("This connector's sign-in changed. Try again.")
                current["oauth"] = refreshed
                try:
                    self.vault.put(connector_id, current)
                except connector_vault.VaultError:
                    raise mcp.NotSent(KEYCHAIN) from None
                saved.clear()
                saved.update(current)
                return refreshed.get("access_token")

    def _refresh_lock(self, connector_id):
        with self._lock:
            return self._refreshing.setdefault(connector_id, threading.Lock())

    def bearer(self, connector_id, refresh=False):
        """The access token a signed-in connector sends, renewed first when it's expiring or ``refresh``: a Slack
        destination posts with the Slack connector's sign-in (outlets/slack.py)."""
        row = self._get(connector_id)
        saved = self._secrets(row)
        if not saved.get("oauth"):
            raise mcp.AuthNeeded(f"{row['name']} isn't signed in.")
        return self._token(row, saved, force_refresh=refresh)

    def preset_bearer(self, preset):
        """The bearer token of the first enabled connector made from ``preset`` that has one (a pasted token, or its
        sign-in, refreshed when it's expiring), or None."""
        for row in self.store.all():
            if row.get("preset") != preset or not row["enabled"]:
                continue
            try:
                token = self._token(row, self._secrets(row))
            except mcp.AuthNeeded:
                continue
            except mcp.NotSent as error:
                raise APIError(str(error), 503, "keychain") from None
            if token:
                return token
        return None

    @staticmethod
    def _stdio_spec(command, args, saved_env):
        """The spec and environment that start a command: a bare name is looked up on the PATH the server will get."""
        env = stdio_env(saved_env)
        if os.sep not in command:
            found = shutil.which(command, path=env["PATH"])
            if found is None:
                hint = " Install Node.js from nodejs.org, then test again." if command in ("npx", "node") else \
                    " Install it, or check the command."
                raise mcp.NotSent(f"Mobster couldn't find {command}.{hint}")
            command = found
        return {"transport": "stdio", "command": command, "args": list(args), "url": None}, env

    def _must_be_mcp(self, name, command, args, env):
        """Starts a command you typed and runs the MCP handshake before it is saved."""
        try:
            spec, stdio = self._stdio_spec(command, args, env)
            session = self.connect(spec, env=stdio, name=name, timeout=TEST_CONNECT)
        except mcp.SlowToAnswer:
            return
        except mcp.ConnectorError as error:
            raise APIError(f"{error.message} Mobster saved nothing. Check the command, then try again.", 422,
                           "not_an_mcp_server") from None
        self._close_session([session, 0])

    def _open(self, row, timeout=mcp.CONNECT_TIMEOUT, force_refresh=False):
        """A new initialized session for ``row``. A 401 with OAuth tokens refreshes once (nothing was sent)."""
        saved = self._secrets(row)
        spec = {"transport": row["transport"], "command": row.get("command"), "args": row.get("args") or [],
                "url": row.get("url")}
        if row["transport"] == "stdio":
            spec, env = self._stdio_spec(row["command"], row.get("args") or [], saved.get("env"))
            return self.connect(spec, env=env, name=row["name"], timeout=timeout)
        try:
            return self.connect(spec, token=self._token(row, saved, force_refresh=force_refresh and bool(
                (saved.get("oauth") or {}).get("refresh_token"))), name=row["name"], timeout=timeout)
        except mcp.AuthNeeded:
            if force_refresh:
                raise
            if not saved.get("oauth") or not saved["oauth"].get("refresh_token"):
                raise
            return self.connect(spec, token=self._token(row, saved, force_refresh=True), name=row["name"],
                                timeout=timeout)

    def _lock_for(self, connector_id):
        with self._lock:
            return self._locks.setdefault(connector_id, threading.Lock())

    def _session(self, row, timeout=mcp.CONNECT_TIMEOUT, force_refresh=False):
        self._reap()
        with self._lock:
            version = self._credential_versions.get(row["id"], 0)
            self._credentials_current(row["id"], version)
            entry = self._sessions.get(row["id"])
            if entry is not None and entry[0].ready:
                entry[1] = time.monotonic()
                return entry[0]
        session = self._open(row, timeout, force_refresh=force_refresh)
        try:
            with self._lock:
                self._credentials_current(row["id"], version)
                self._sessions[row["id"]] = [session, time.monotonic()]
        except mcp.NotSent:
            self._close_session([session, 0])
            raise
        return session

    def _remember_tools(self, row, tools):
        trimmed = [trim_tool(t) for t in tools]
        return self.store.update(row["id"], tools=trimmed, tools_at=self.clock(), updated_at=self.clock())

    def test(self, connector_id, keep=False, *, credential_version=None):
        """Connect (a fresh session), ping and cache tools. ``keep`` leaves the session open (Reconnect)."""
        try:
            with self._lock:
                version = (self._credential_versions.get(connector_id, 0) if credential_version is None
                           else credential_version)
                self._credentials_current(connector_id, version)
            with self._lock_for(connector_id):
                with self._lock:
                    self._credentials_current(connector_id, version)
                    row = self._get(connector_id)
                    entry = self._sessions.pop(connector_id, None)
                    self._connecting.add(connector_id)
                self._close_session(entry)
                try:
                    # Within the app's 15 s request: a command that needs longer the first time answers the next test.
                    session = self._session(row, TEST_CONNECT)
                    session.ping(timeout=TEST_LIST)
                    tools = session.list_tools(timeout=TEST_LIST)
                    session._tools_seen = True
                    session.tools_changed = False
                except mcp.ConnectorError as error:
                    with self._lock:
                        self._credentials_current(connector_id, version)
                        entry = self._sessions.pop(connector_id, None)
                        row = self._failed(connector_id, error)
                        self._connecting.discard(connector_id)
                    self._close_session(entry)
                    return {"ok": False, "message": error.message, "connector": self.public(row)}
                finally:
                    self._connecting.discard(connector_id)
                with self._lock:
                    self._credentials_current(connector_id, version)
                    entry = self._sessions.pop(connector_id, None) if not keep else None
                    row = self._remember_tools(row, tools)
                    message = f"Connected. {len(row['tools'])} tool{'s' if len(row['tools']) != 1 else ''}."
                    row = self.store.update(connector_id,
                                            last_test={"ok": True, "at": self.clock(), "message": message})
                self._close_session(entry)
                return {"ok": True, "message": message, "connector": self.public(row)}

        except mcp.NotSent as error:
            raise APIError(error.message, 409, "sign_in_changed") from None

    def reconnect(self, connector_id):
        """Close the session, connect again and keep it open: Reconnect on the row. {ok, message, connector}."""
        return self.test(connector_id, keep=True)

    def _failed(self, connector_id, error):
        """Record a failed connect or call on the row (its health says so until one works again)."""
        code = "auth" if isinstance(error, mcp.AuthNeeded) else "failed"
        return self.store.update(connector_id, last_test={"ok": False, "at": self.clock(), "message": error.message,
                                                          "code": code})

    def _worked(self, row):
        """A call worked after the row said it failed: the row says connected again."""
        test = row.get("last_test") if isinstance(row.get("last_test"), dict) else None
        if test and not test.get("ok"):
            count = len(row.get("tools") or ())
            self.store.update(row["id"], last_test={"ok": True, "at": self.clock(),
                                                    "message": f"Connected. {count} tool{'s' if count != 1 else ''}."})

    def begin_sign_in(self, connector_id):
        """Start OAuth for an HTTP connector: {"authorizeUrl"} for the Mac app to open in the browser."""
        row = self._get(connector_id)
        if row["transport"] != "http":
            raise ValueError("Only a server with a URL signs in. A command takes its keys as environment values")
        with self._lock:
            if self._closed:
                raise APIError("Mobster is quitting.", 503, "unavailable")
            self._get(connector_id)
            try:
                saved = self.vault.get(connector_id)
            except connector_vault.VaultError:
                raise APIError(KEYCHAIN, 503, "keychain_unavailable") from None
            previous, session = self._invalidate_credentials(connector_id)
            version = self._credential_versions[connector_id]
            attempt = {"state": "waiting", "flow": None, "message": None}
            self._sign_ins[connector_id] = attempt
        self._close_session(session)
        if previous is not None:
            previous.cancel()
        flow = None
        try:
            client = saved.get("client") or {}
            preset = preset_list.preset(row["preset"]) if row.get("preset") else None
            # The person's own app first, then Mobster's own for a vendor that lets no client register itself, else the
            # server registers Mobster now.
            if client.get("client_id"):
                app = oauth.OwnApp(client["client_id"], client.get("client_secret"))
            elif preset and preset["auth"] == "mobster_app" and row["url"] == preset["url"]:
                # Mobster's own app is registered at the vendor's own service; a server of your own (GitLab
                # self-managed) is another one, where the server registers Mobster itself.
                app = self.apps.get(preset["key"])
                if isinstance(app, shared.Unavailable) and preset.get("needs_app"):
                    raise ValueError(f"{shared.why(preset['name'], app.reason)}. Try again later")
                if isinstance(app, shared.Unavailable):
                    own = ("an OAuth app of your own" if preset["fallback"] == "oauth_client"
                           else f"a {preset['token_label']}")
                    raise ValueError(f"{shared.why(preset['name'], app.reason)}. Remove {row['name']}, then connect it "
                                     f"again with {own}, or try again later")
                # Mobster's app trades the sign-in through your Mobster account: without one, the browser's sign-in
                # would come back to a refusal (ICP walkthrough, 24 Oct: "HTTP 401" and no way to sign in).
                if not shared.has_account():
                    raise APIError(f"{preset['name']} connects through Mobster's own app, which needs your Mobster "
                                   f"account. Sign in to Mobster, then connect {row['name']} again.", 409,
                                   "account_needed")
            else:
                app = None
            resource_metadata = None if isinstance(app, oauth.MobsterApp) else self._resource_metadata(row)

            # A vendor whose admin approves each client (the preset's ``approval``) gets the client it approved again.
            approval = (preset or {}).get("approval")
            registration = oauth.Registration.parse(saved.get("registration")) if approval else None

            def done(ended, tokens, message):
                self._signed_in(connector_id, version, attempt, ended, tokens, message)

            # Google Drive goes on to Google's Picker in the same tab, so the files you pick are there from the start.
            then = {"then": picker.after_sign_in(self, connector_id)} if (preset or {}).get("picker") and \
                isinstance(app, oauth.MobsterApp) else {}
            flow = self.sign_in_class(row["url"], done, name=row["name"], app=app, scopes=(preset or {}).get("scopes"),
                                      redirect_host=(preset or {}).get("redirect_host", oauth.LOOPBACK_HOSTS[0]),
                                      resource_metadata=resource_metadata, registration=registration, **then)
            with self._lock:
                self._credentials_current(connector_id, version)
                attempt["flow"] = flow
                attempt["message"] = (
                    f"Finish signing in in your browser. If {row['name']} asks for an admin's approval, they approve "
                    f"Mobster in {approval}.") if approval else None
            url = flow.begin()
            with self._lock:
                self._credentials_current(connector_id, version)
                if approval and flow.registration is not None and flow.registration != registration:
                    # The callback may already have saved tokens; merge the registration into the current item.
                    saved = self.vault.get(connector_id)
                    self.vault.put(connector_id, {**saved, "registration": asdict(flow.registration)})
        except (ValueError, oauth.OAuthError, connector_vault.VaultError, mcp.NotSent) as error:
            with self._lock:
                if self._credential_versions.get(connector_id) == version and not self._closed:
                    attempt.update(state="failed", message=KEYCHAIN if isinstance(error, connector_vault.VaultError)
                                   else str(error), flow=None)
            if flow is not None:
                flow.cancel()
            if isinstance(error, ValueError):
                raise
            if isinstance(error, connector_vault.VaultError):
                raise APIError(KEYCHAIN, 503, "keychain_unavailable") from None
            raise APIError(str(error), 502, "sign_in_failed") from None
        return {"authorizeUrl": url}

    @staticmethod
    def _resource_metadata(row):
        """Where the server's 401 says its sign-in is described (MCP discovery), or None."""
        probe = mcp.HttpSession(row["url"], name=row["name"])
        try:
            probe.initialize(timeout=5)
            probe.close()
        except mcp.AuthNeeded:
            return probe.resource_metadata
        except mcp.ConnectorError:
            pass
        return None

    def _signed_in(self, connector_id, version, attempt, ended, tokens, message):
        with self._lock:
            if (self._closed or self._credential_versions.get(connector_id) != version
                    or self._sign_ins.get(connector_id) is not attempt or attempt["state"] != "waiting"):
                return
            row = self.store.get(connector_id)
            if row is None:
                return
            if tokens is None:
                approval = (preset_list.preset(row["preset"]) or {}).get("approval") if row.get("preset") else None
                if ended is oauth.Ended.UNANSWERED and approval:
                    attempt.update(state="awaiting_approval", flow=None, message=(
                        f"{row['name']} didn't send you back. If it asked an admin to approve Mobster, connect again "
                        f"once they approve it in {approval}."))
                else:
                    attempt.update(state="failed", message=message, flow=None)
                return
            try:
                saved = self.vault.get(connector_id)
                saved.pop("token", None)
                saved["oauth"] = tokens
                self.vault.put(connector_id, saved)
                self.store.update(connector_id, auth="oauth", updated_at=self.clock())
            except Exception:  # noqa: BLE001 -- don't let a failed Keychain/store write escape the browser callback
                attempt.update(state="failed", message=KEYCHAIN, flow=None)
                return
            attempt.update(state="done", message=None, flow=None)
        try:
            self.test(connector_id, credential_version=version)
        except Exception:  # noqa: BLE001
            log.warning("connectors: the test after sign-in failed")

    def sign_out(self, connector_id):
        """Disconnect: the tokens leave the Keychain and the session closes, then the server that issued them
        is asked to revoke them, when it offers that (oauth.revoke)."""
        flow, session, tokens = None, None, None
        try:
            with self._lock:
                row = self._get(connector_id)
                try:
                    saved = self.vault.get(connector_id)
                    saved.pop("token", None)
                    tokens = saved.pop("oauth", None)
                    self.vault.put(connector_id, saved)
                except connector_vault.VaultError:
                    raise APIError(KEYCHAIN, 503, "keychain_unavailable") from None
                flow, session = self._invalidate_credentials(connector_id)
                row = self.store.update(row["id"], auth="none", updated_at=self.clock())
        finally:
            self._close_session(session)
            if flow is not None:
                flow.cancel()
        public = self.public(row)
        try:
            revoked = self.revoke_tokens(tokens) if tokens else None
        except Exception:  # noqa: BLE001 -- disconnected on this Mac whatever the server says
            revoked = False
        return public, revoked

    def call(self, row, tool, arguments, timeout=None):
        """One tools/call on ``row``'s live session. Reconnects at most once, and only when nothing ran (NotSent, or
        AuthNeeded after a refresh): OutcomeUnknown and a server's own error are never sent again."""
        with self._lock_for(row["id"]):
            for attempt in (1, 2):
                with self._lock:
                    reused = row["id"] in self._sessions
                try:
                    return self._call_once(row, tool, arguments, timeout, refresh=attempt == 2)
                except ToolGone:
                    raise
                except mcp.AuthNeeded as error:
                    self._drop(row["id"])
                    saved = self._saved_quietly(row)
                    # A fresh session already refreshed once while connecting (_open); a session that was open
                    # meets an expired token here.
                    if attempt == 1 and reused and saved.get("oauth") and saved["oauth"].get("refresh_token"):
                        continue
                    self._failed(row["id"], error)
                    raise
                except mcp.NotSent as error:
                    self._drop(row["id"])
                    if attempt == 1 and reused:
                        continue
                    self._failed(row["id"], error)
                    raise
                except mcp.OutcomeUnknown:
                    self._drop(row["id"])
                    raise

    def _saved_quietly(self, row):
        try:
            return self.vault.get(row["id"]) or {}
        except connector_vault.VaultError:
            return {}

    def _call_once(self, row, tool, arguments, timeout, refresh=False):
        if refresh:
            self._drop(row["id"])
        session = self._session(row, force_refresh=refresh)
        if not getattr(session, "_tools_seen", False) or session.tools_changed:
            # A new session lists its tools first (and again after the server said they changed), so the cache
            # follows what the server offers now.
            session.tools_changed = False
            tools = session.list_tools()
            session._tools_seen = True
            row = self._remember_tools(row, tools)
            if not any(t["name"] == tool for t in row["tools"]):
                raise ToolGone(f"{row['name']} no longer has a tool called {tool}.")
        result = session.call_tool(tool, arguments, timeout=timeout or mcp.CALL_TIMEOUT)
        self._worked(row)
        return result


def fingerprint(connector_id, tool, arguments):
    raw = json.dumps([connector_id, tool, arguments], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()

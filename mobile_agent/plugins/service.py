"""Installed plugins: one PluginService per Runtime (harness_api.register_service)."""

import logging
import secrets as random
import shutil
import tempfile
import threading
import time
from pathlib import Path

from ..api_errors import APIError
from ..service_support import ServiceRegistry, ms
from . import install as sources
from . import manifest as plugin_manifest
from . import store as plugin_store

log = logging.getLogger("mobster.plugins")

PREVIEW_SECONDS = 900.0
MAX_PREVIEWS = 8

_services = ServiceRegistry()


def service_for(runtime):
    return _services.get(runtime)


def start(runtime):
    _services.add(runtime, PluginService(runtime))


def close(runtime):
    service = _services.pop(runtime)
    if service is not None:
        service.close()


def shown_path(path):
    """``path`` as the person would write it: ``~/Code/my-plugin`` under their home folder."""
    path = Path(path).expanduser()
    try:
        return ("~/" + str(path.resolve().relative_to(Path.home().resolve())))[:500]
    except (ValueError, OSError):
        return str(path)[:500]


def plugins_dir(runtime):
    state_db = getattr(getattr(runtime, "config", None), "state_db", None)
    if state_db:
        return Path(state_db).parent / "plugins"
    return None


class PluginService:
    def __init__(self, runtime, *, clock=time.time, opener=None, folder=None):
        self.runtime = runtime
        self.store = plugin_store.PluginStore(runtime.journal)
        self.clock, self.opener = clock, opener
        self._own_folder = None
        self.folder = Path(folder) if folder else plugins_dir(runtime)
        if self.folder is None:
            self._own_folder = Path(tempfile.mkdtemp(prefix="mobster-plugins-"))
            self.folder = self._own_folder
        self._previews = {}      # token -> {"staging", "plugin", "source", "at"}
        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            previews, self._previews = list(self._previews.values()), {}
        for preview in previews:
            shutil.rmtree(preview["staging"], ignore_errors=True)
        if self._own_folder is not None:
            shutil.rmtree(self._own_folder, ignore_errors=True)

    def _connectors(self):
        try:
            from ..connectors import service as connector_service
        except ImportError:
            return None
        return connector_service.service_for(self.runtime)

    def _outlets(self):
        try:
            from ..outlets import service as outlet_service
        except ImportError:
            return None
        return outlet_service.service_for(self.runtime)

    def _workflows(self):
        return getattr(self.runtime, "workflows", None)

    def public(self, row):
        return {"name": row["name"], "version": row["version"], "description": row["description"],
                "author": row.get("author") or "", "source": row["source"], "enabled": row["enabled"],
                "skills": [{"name": s["name"], "description": s["description"]} for s in row["skills"]],
                "connectors": len(row["added"]["connectors"]), "connectorIds": list(row["added"]["connectors"]),
                "workflows": len(row["added"]["workflows"]) if row["enabled"] else row["added"].get("paused", 0),
                "destinations": len(row["added"]["destinations"]), "installedAt": ms(row["created_at"])}

    def list(self):
        return [self.public(row) for row in self.store.all()]

    def _get(self, name):
        row = self.store.get(name) if isinstance(name, str) else None
        if row is None:
            raise APIError("That plugin was removed.", 404, "not_found")
        return row

    def _expire(self):
        now = self.clock()
        with self._lock:
            old = [token for token, p in self._previews.items() if now - p["at"] > PREVIEW_SECONDS]
            while len(self._previews) - len(old) >= MAX_PREVIEWS:
                oldest = min((t for t in self._previews if t not in old), key=lambda t: self._previews[t]["at"])
                old.append(oldest)
            dropped = [self._previews.pop(token) for token in old]
        for preview in dropped:
            shutil.rmtree(preview["staging"], ignore_errors=True)

    def preview(self, body=None, *, zip_data=None):
        """``body``: {"path": a folder or a .zip on this Mac} or {"github": "owner/repo[@ref]"}; or ``zip_data``, an
        uploaded .zip."""
        self._expire()
        staging = sources.staging()
        try:
            if zip_data is not None:
                root, source = sources.from_zip(zip_data, staging), "a .zip file"
            elif (not isinstance(body, dict) or len(set(body) & {"path", "github"}) != 1
                  or set(body) - {"path", "github"}):
                raise plugin_manifest.PluginError("Give a folder or a .zip on this Mac, or a GitHub repository.")
            elif "github" in body:
                owner, repo, ref = sources.parse_repo(body["github"])
                root = sources.from_github(body["github"], staging, self.opener)
                source = f"github:{owner}/{repo}" + (f"@{ref}" if ref != "HEAD" else "")
            else:
                path = Path(str(body["path"])).expanduser()
                if path.suffix.lower() == ".zip" and not path.is_dir():
                    if not path.is_file():
                        raise plugin_manifest.PluginError("There's no .zip at that path. Check it and try again.")
                    if path.stat().st_size > sources.MAX_ARCHIVE:
                        raise plugin_manifest.PluginError("This archive is larger than 20 MB.")
                    root = sources.from_zip(path.read_bytes(), staging)
                else:
                    root = sources.from_folder(path, staging)
                source = shown_path(path)
            plugin = plugin_manifest.read(root)
        except plugin_manifest.PluginError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            raise plugin_manifest.PluginError("Mobster couldn't read that plugin.") from None
        token = random.token_hex(12)
        with self._lock:
            self._previews[token] = {"staging": staging, "plugin": plugin, "source": source, "at": self.clock()}
        return {"preview": token, "plugin": plugin.summary(), "source": source,
                "installed": self.store.get(plugin.name) is not None}

    def install(self, body):
        """{"preview": token, "values": {NAME: value}}: install what the preview showed. 201 {plugin}."""
        if not isinstance(body, dict) or set(body) - {"preview", "values"} or not isinstance(body.get("preview"), str):
            raise ValueError("Install a plugin from its preview")
        values = body.get("values") or {}
        if not isinstance(values, dict) or not all(isinstance(k, str) and isinstance(v, str) and len(v) <= 8000
                                                   for k, v in values.items()):
            raise ValueError("values is {NAME: text}")
        with self._lock:
            preview = self._previews.get(body["preview"])
        if preview is None:
            raise APIError("That preview ran out. Choose the plugin again.", 410, "preview_expired")
        plugin = preview["plugin"]
        if self.store.get(plugin.name) is not None:
            raise APIError(f"{plugin.name} is already installed. Remove it first to install this copy.", 409,
                           "plugin_installed")
        if len(self.store.all()) >= plugin_store.MAX_PLUGINS:
            raise APIError(f"You have {plugin_store.MAX_PLUGINS} plugins. Remove one first.", 409, "plugin_limit")
        missing = [a["key"] for a in plugin.asks if not str(values.get(a["key"], "")).strip()]
        if missing:
            raise ValueError(f"Fill in {', '.join(missing[:4])} to install this plugin")
        home = self.folder / plugin.name
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if home.exists():
            shutil.rmtree(home)
        shutil.copytree(plugin.root, home)
        added = {"connectors": [], "workflows": [], "destinations": []}
        try:
            self._add_connectors(plugin, values, home, added)
            self._add_workflows(plugin, added)
            self._add_destinations(plugin, values, home, added)
        except BaseException:
            self._undo(added)
            shutil.rmtree(home, ignore_errors=True)
            raise
        now = self.clock()
        row = self.store.put({"name": plugin.name, "version": plugin.version, "description": plugin.description,
                              "author": plugin.author, "source": preview["source"], "enabled": True, "path": str(home),
                              "skills": plugin.skills, "added": added, "created_at": now, "updated_at": now})
        with self._lock:
            self._previews.pop(body["preview"], None)
        shutil.rmtree(preview["staging"], ignore_errors=True)
        self._test_connectors(added["connectors"])
        return self.public(row)

    def _add_connectors(self, plugin, values, home, added):
        if not plugin.servers:
            return
        connectors = self._connectors()
        if connectors is None:
            raise APIError("Connectors aren't available right now, so this plugin's MCP servers can't be added.", 503,
                           "unavailable")
        for server in plugin.servers:
            if server["transport"] == "stdio":
                body = {"name": server["name"], "command": plugin_manifest.fill(server["command"], values, home),
                        "args": [plugin_manifest.fill(a, values, home) for a in server["args"]],
                        "env": {k: plugin_manifest.fill(v, values, home) for k, v in server["env"].items()} or None}
            else:
                body = {"name": server["name"], "url": server["url"]}
                if server.get("token"):
                    body["token"] = plugin_manifest.fill(server["token"], values, home)
            added["connectors"].append(connectors.add(body, plugin=plugin.name)["id"])

    def _add_workflows(self, plugin, added):
        if not plugin.workflows:
            return
        workflows = self._workflows()
        if workflows is None:
            raise APIError("Workflows aren't available right now, so this plugin can't be installed.", 503,
                           "unavailable")
        for flow in plugin.workflows:
            # A plugin never starts anything by itself: its workflows are saved paused.
            added["workflows"].append(workflows.create({**flow, "enabled": False})["id"])

    def _add_destinations(self, plugin, values, home, added):
        if not plugin.destinations:
            return
        outlets = self._outlets()
        if outlets is None:
            raise APIError("Destinations aren't available right now, so this plugin can't be installed.", 503,
                           "unavailable")
        for destination in plugin.destinations:
            body = {"name": destination["name"], "url": plugin_manifest.fill(destination["url"], values, home),
                    "screenshot": bool(destination.get("screenshot", False))}
            if destination.get("secret"):
                body["secret"] = plugin_manifest.fill(destination["secret"], values, home)
            added["destinations"].append(outlets.add(body)["id"])

    def _test_connectors(self, ids):
        connectors = self._connectors()
        for connector_id in ids:
            try:
                connectors.test(connector_id)
            except Exception:  # noqa: BLE001 -- a server that doesn't answer yet is tested again from Settings
                log.info("plugins: a connector's first test failed")

    def _undo(self, added):
        connectors, workflows, outlets = self._connectors(), self._workflows(), self._outlets()
        for kind, owner, remove in (("connectors", connectors, "remove"), ("workflows", workflows, "delete"),
                                    ("destinations", outlets, "delete")):
            for identifier in added.get(kind, ()):
                try:
                    getattr(owner, remove)(identifier)
                except Exception:  # noqa: BLE001 -- already gone
                    pass

    def set_enabled(self, name, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled is true or false")
        row = self._get(name)
        if row["enabled"] == enabled:
            return self.public(row)
        added = dict(row["added"])
        connectors, workflows = self._connectors(), self._workflows()

        def flip_connectors():
            for connector_id in added["connectors"]:
                try:
                    connectors.update(connector_id, {"enabled": enabled})
                except APIError:
                    pass  # removed in Settings meanwhile

        if enabled:
            plugin = plugin_manifest.read(row["path"])
            fresh = {"connectors": [], "workflows": [], "destinations": []}
            try:
                self._add_workflows(plugin, fresh)
                flip_connectors()
            except BaseException:
                self._undo(fresh)
                raise
            added["workflows"] = fresh["workflows"]
            added.pop("paused", None)
        else:
            added["paused"] = len(added["workflows"])
            for workflow_id in added["workflows"]:
                try:
                    workflows.delete(workflow_id)
                except Exception:  # noqa: BLE001 -- deleted by hand meanwhile
                    pass
            added["workflows"] = []
            flip_connectors()
        row = self.store.put({**row, "enabled": enabled, "added": added, "updated_at": self.clock()})
        return self.public(row)

    def uninstall(self, name):
        row = self._get(name)
        added = row["added"]
        self._undo({"connectors": added["connectors"], "workflows": added["workflows"],
                    "destinations": added["destinations"]})
        if row["path"] and Path(row["path"]).resolve().parent == self.folder.resolve():
            shutil.rmtree(row["path"], ignore_errors=True)
        self.store.delete(name)
        return name

    def skills(self):
        """(plugin name, skill dict, folder) of every enabled plugin's skills."""
        out = []
        for row in self.store.all():
            if row["enabled"]:
                for skill in row["skills"]:
                    out.append((row["name"], skill, Path(row["path"])))
        return out

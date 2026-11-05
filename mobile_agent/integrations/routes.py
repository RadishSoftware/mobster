"""Settings › For developers in the Mac app."""

import os
from pathlib import Path

from ..api_errors import APIError
from ..api_routes import Response, Route, register
from . import clients as registry
from .cli import RESTART, check_env_file

APP_ONLY = "Do this in the Mobster app."
MOVED = "Move Mobster to your Applications folder, open it from there, then add it again."
NOT_INSTALLED = "{name} isn't installed on this Mac. Install it, then add Mobster."
CLIENT = r"(?P<client>[a-z][a-z-]{1,31})"
# Apps opened from the Dock get launchd's PATH: these are where `claude` and `codex` usually live, so their own
# commands edit their configs (Claude Code rewrites ~/.claude.json all the time).
EXTRA_PATH = (".local/bin", ".claude/local", ".npm-global/bin", "/opt/homebrew/bin", "/usr/local/bin")


def environment():
    """The clients' Env for this Mac, with the usual command folders on PATH."""
    env = registry.Env.current()
    folders = [part for part in (env.environ.get("PATH") or "").split(os.pathsep) if part]
    for extra in EXTRA_PATH:
        folder = extra if extra.startswith("/") else str(env.home / extra)
        if folder not in folders:
            folders.append(folder)
    env.environ["PATH"] = os.pathsep.join(folders)
    return env


def env_file(runtime):
    """The settings file the app's agent reads its keys from (Runtime.keys's), or None."""
    setup = getattr(runtime, "setup", None)
    path = getattr(setup, "env_file", None) if setup is not None else None
    path = path or getattr(getattr(runtime, "config", None), "env_file", None)
    return Path(path).expanduser().absolute() if path else None


def server(runtime, env):
    """The entry's command: this agent, with ``--env-file`` when its settings file is private and readable."""
    extra = []
    path = env_file(runtime)
    if path is not None and check_env_file(path) is None:
        extra = ["--env-file", str(path)]
    return registry.server_for(extra, env)


def movable(command):
    """True when ``command`` lives somewhere that disappears (App Translocation, a mounted disk image)."""
    text = os.path.realpath(command)
    return "/AppTranslocation/" in text or text.startswith("/Volumes/")


def needs_app(request):
    """Fails closed: the Mac app's session, and an agent that has one."""
    local = getattr(request.runtime, "local_api", None) or {}
    if not (local.get("app_session") and request.app_session):
        raise APIError(APP_ONLY, 403, "app_only")


def client_of(request):
    client = registry.BY_ID.get(request.params.get("client", ""))
    if client is None:
        raise APIError("Mobster doesn't know that app.", 404, "unknown_client")
    return client


def same_command(left, right):
    try:
        return bool(left and right) and os.path.realpath(left) == os.path.realpath(right)
    except (OSError, ValueError):
        return False


def row(client, env, wanted):
    found, _ = client.detected(env)
    status = client.status(env)
    out = {"id": client.id, "name": client.name, "detected": bool(found), "restart": client.id in RESTART}
    if status.get("error"):
        return {**out, "state": "error", "message": status["error"]}
    if not status.get("configured"):
        return {**out, "state": "absent"}
    command = status.get("command")
    if same_command(command, wanted.command):
        return {**out, "state": "added"}
    return {**out, "state": "other", "command": registry._home(command) if command else None}


def listed(request):
    needs_app(request)
    env = environment()
    wanted = server(request.runtime, env)
    return Response.json({"clients": [row(client, env, wanted) for client in registry.CLIENTS],
                          "command": [registry._home(part) for part in wanted.argv()]})


def _change(request, remove):
    needs_app(request)
    client = client_of(request)
    env = environment()
    wanted = server(request.runtime, env)
    if not remove:
        if movable(wanted.command):
            raise APIError(MOVED, 409, "app_moved")
        if not client.detected(env)[0]:
            raise APIError(NOT_INSTALLED.format(name=client.name), 409, "client_missing")
        # Add from the app deliberately selects its private settings file, while retaining the saved phone and
        # TestFlight restrictions and any other client environment.
        overrides = {"--env-file"} if "--env-file" in wanted.args else frozenset()
        changes = client.install(env, wanted, overrides=overrides)
    else:
        changes = client.remove(env)
    failed = next((change for change in changes if change.action == "error"), None)
    if failed is not None:
        raise APIError(f"{failed.message}.", 409, "client_config")
    return Response.json({"client": row(client, env, wanted), "changes": [change.json() for change in changes]})


def add(request):
    return _change(request, remove=False)


def remove(request):
    return _change(request, remove=True)


ROUTES = (Route("integrations", "GET", "/api/integrations/clients", listed, body="none"),
          Route("integrations", "POST", "/api/integrations/clients/" + CLIENT, add, body="none"),
          Route("integrations", "DELETE", "/api/integrations/clients/" + CLIENT, remove, body="none"))


def register_routes():
    for route in ROUTES:
        register(route)

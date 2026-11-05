"""Static registration of the SOTA tracks (seam S1). Static imports so PyInstaller follows them.

``load()`` imports each track's ``register`` module and calls ``register(api)`` (api = harness_api) inside
``_staged()``: every seam registry (harness_api's, api_routes, journal.SCHEMAS, mcp_server.registry) is
snapshotted first and restored if register raises, so a track that raises leaves nothing registered. It is
recorded in STATUS and skipped: Mobster still starts. ``load`` does no I/O beyond imports.

Callers: server.Runtime.__init__ (first line), mcp_server.cli at startup, and each new CLI command.
"""

import threading
from contextlib import contextmanager

from . import harness_api as api

TRACKS = ("harness", "threads", "memory", "attachments", "wireless", "testkit", "remote", "accounts", "learned",
          "longform", "outlets", "connectors", "plugins", "permissions", "packs", "app_api", "waiting", "netxray",
          "content_queue", "feedback", "ios", "teams", "xray", "triggers", "projects", "corpus")
STATUS: dict = {}
_loaded = False
_lock = threading.Lock()


def _importers():
    """(track, importer) pairs. Each importer is a plain ``from .x import register`` (PyInstaller follows it)."""
    def harness():
        from .harness import register
        return register

    def threads():
        from .threads import register
        return register

    def memory():
        from .memory import register
        return register

    def attachments():
        from .attachments import register
        return register

    def wireless():
        from .wireless import register
        return register

    def testkit():
        from .testkit import register
        return register

    def remote():
        from .remote import register
        return register

    def accounts():
        from .accounts import register
        return register

    def learned():
        from .learned import register
        return register

    def longform():
        from .longform import register
        return register

    def outlets():
        from .outlets import register
        return register

    def connectors():
        from .connectors import register
        return register

    def plugins():
        from .plugins import register
        return register

    def permissions():
        from .permissions import register
        return register

    def packs():
        from .packs import register
        return register

    def app_api():
        from .app_api import register
        return register

    def waiting():
        from .waiting import register
        return register

    def netxray():
        from .netxray import register
        return register

    def content_queue():
        from .content_queue import register
        return register

    def feedback():
        from .feedback import register
        return register

    def ios():
        from .ios import register
        return register

    def triggers():
        from .triggers import register
        return register

    def teams():
        from .teams import register
        return register

    def xray():
        from .xray import register
        return register

    def projects():
        from .projects import register
        return register

    def corpus():
        from .corpus import register
        return register

    return (("harness", harness), ("threads", threads), ("memory", memory), ("attachments", attachments),
            ("wireless", wireless), ("testkit", testkit), ("remote", remote), ("accounts", accounts),
            ("learned", learned), ("longform", longform), ("outlets", outlets),
            ("connectors", connectors), ("plugins", plugins),
            ("permissions", permissions), ("packs", packs), ("app_api", app_api),
            ("waiting", waiting), ("netxray", netxray),
            ("content_queue", content_queue), ("feedback", feedback), ("ios", ios), ("teams", teams), ("xray", xray),
            ("triggers", triggers), ("projects", projects), ("corpus", corpus))


def _registries():
    """(snapshot, restore) pairs of every seam registry a track's register() may touch."""
    from . import api_routes, journal
    from .mcp_server import registry as mcp_registry
    pairs = [(api._snapshot, api._restore)]
    pairs.append((lambda: list(api_routes._routes), lambda saved: _replace(api_routes._routes, saved)))
    pairs.append((lambda: dict(journal.SCHEMAS), lambda saved: _replace(journal.SCHEMAS, saved)))
    pairs.append((lambda: dict(journal.SCHEMA_OWNERS), lambda saved: _replace(journal.SCHEMA_OWNERS, saved)))
    pairs.append((lambda: list(mcp_registry._providers), lambda saved: _replace(mcp_registry._providers, saved)))
    return pairs


def _replace(target, saved):
    target.clear()
    if isinstance(target, dict):
        target.update(saved)
    else:
        target.extend(saved)


@contextmanager
def _staged(track):
    """Registrations inside belong to ``track``; if the body raises, every seam registry goes back to how it was."""
    pairs = _registries()
    saved = [(snapshot(), restore) for snapshot, restore in pairs]
    try:
        with api.owned_by(track):
            yield
    except BaseException:
        for value, restore in saved:
            restore(value)
        raise


def register_one(track, register):
    """Run one track's ``register(api)`` staged; record STATUS. Returns True when it registered."""
    try:
        with _staged(track):
            register(api)
    except Exception as error:  # noqa: BLE001 -- a track never stops Mobster from starting
        STATUS[track] = f"error: {type(error).__name__}"
        return False
    STATUS[track] = "ok"
    return True


def load() -> None:
    """Idempotent: register every track once per process."""
    global _loaded
    with _lock:
        if _loaded:
            return
        _loaded = True
        for track, importer in _importers():
            try:
                module = importer()
            except ModuleNotFoundError as error:
                # A track left out of this build (Remote isn't in the open-source tree) is absent, not broken.
                own = error.name == f"{__package__}.{track}"
                STATUS[track] = "absent" if own else f"error: {type(error).__name__}"
                continue
            except Exception as error:  # noqa: BLE001
                STATUS[track] = f"error: {type(error).__name__}"
                continue
            register_one(track, module.register)
        try:    # GET /api/access/full-disk belongs to no track, so it never answers 503 (full_disk_access.py)
            from .full_disk_access import register_route
            register_route()
        except Exception:  # noqa: BLE001 -- the route is an extra; Mobster still starts
            pass
        try:  # /api/project/env belongs to no track either
            from .project_env_routes import register_routes
            register_routes()
        except Exception:  # noqa: BLE001
            pass


def disable(track, reason) -> None:
    """Turn ``track`` off (journal.register_schema: "error: newer data" or "error: migration")."""
    STATUS[track] = reason
    api.disable(track)


def reset_for_tests() -> None:
    """Forget every registration and allow ``load`` again."""
    global _loaded
    from . import api_routes, journal
    from .mcp_server import registry as mcp_registry
    with _lock:
        _loaded = False
        STATUS.clear()
        api.reset_for_tests()
        api_routes._routes.clear()
        journal.SCHEMAS.clear()
        journal.SCHEMA_OWNERS.clear()
        mcp_registry._providers.clear()

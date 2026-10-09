"""Static registration of the SOTA tracks (seam S1). Static imports so PyInstaller follows them.

``load()`` imports each track's ``register`` module and calls ``register(api)`` (api = harness_api) inside
``_staged()``: every seam registry (harness_api's, api_routes, journal.SCHEMAS, mcp_server.registry) is
snapshotted first and restored if register raises, so a track that raises leaves nothing registered. It is
recorded in STATUS and skipped: Mobster still starts. ``load`` does no I/O beyond imports.

Callers: server.Runtime.__init__ (first line), mcp_server.cli at startup, tui.session.Session.__init__, and each
new CLI command.
"""

from contextlib import contextmanager
import threading

from . import harness_api as api

TRACKS = ("harness", "threads", "memory", "attachments", "wireless", "testkit")
STATUS: dict = {}          # track -> "ok" | "error: <ExceptionType>" | "error: newer data" | "error: migration"
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

    return (("harness", harness), ("threads", threads), ("memory", memory), ("attachments", attachments),
            ("wireless", wireless), ("testkit", testkit))


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
            except Exception as error:  # noqa: BLE001
                STATUS[track] = f"error: {type(error).__name__}"
                continue
            register_one(track, module.register)


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

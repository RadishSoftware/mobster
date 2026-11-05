"""The coding agents Mobster can run a task on, one instance each per process (so their status is cached)."""

import threading

from .claude_code import ClaudeCode
from .codex import Codex
from .gemini import Gemini

CLASSES = {"codex": Codex, "claude_code": ClaudeCode, "gemini": Gemini}
_instances = {}
_lock = threading.Lock()


def ids():
    return tuple(CLASSES)


def get(agent_id):
    """The agent for ``agent_id`` (codex, claude_code ...). KeyError for another."""
    cls = CLASSES[agent_id]
    with _lock:
        if agent_id not in _instances:
            _instances[agent_id] = cls()
        return _instances[agent_id]


def statuses(max_age=60.0):
    """{agent id: status} for every agent (cli.CliAgent.status), read in parallel."""
    out, threads = {}, []

    def read(agent_id):
        out[agent_id] = get(agent_id).status(max_age=max_age)
    for agent_id in CLASSES:
        thread = threading.Thread(target=read, args=(agent_id,), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join(15)
    return {agent_id: out.get(agent_id) or {"id": agent_id, "ready": False, "installed": False}
            for agent_id in CLASSES}


def ready(env=None):
    """The id of the coding agent a task that needs a model runs on when Mobster's own agent has no key."""
    import os
    names = {name for name, state in snapshot(max_age=60, wait=True).items() if state.get("ready")}
    preferred = str((os.environ if env is None else env).get("MOBSTER_PREFERRED_AGENT", "")).strip()
    return next((name for name in (preferred, *CLASSES) if name in names and name in CLASSES), None)


def reset_for_tests():
    with _lock:
        _instances.clear()
    with _snapshot_lock:
        _snapshot.update(value=None, at=0.0, refreshing=False, generation=_snapshot["generation"] + 1)


_snapshot = {"value": None, "at": 0.0, "refreshing": False, "generation": 0}
_snapshot_lock = threading.Lock()


def snapshot(max_age=60.0, wait=False):
    """The agents' statuses from the last check, for GET /api/status (polled often)."""
    import time
    with _snapshot_lock:
        value, at, busy = _snapshot["value"], _snapshot["at"], _snapshot["refreshing"]
        generation = _snapshot["generation"]
        stale = value is None or time.monotonic() - at >= max_age
        if stale and not busy and not wait:
            _snapshot["refreshing"] = True
    if stale and wait:
        fresh = statuses(max_age=max_age)
        with _snapshot_lock:
            _snapshot.update(value=fresh, at=time.monotonic())
        return public(fresh)
    if stale and not busy:
        def refresh():
            try:
                fresh = statuses(max_age=max_age)
                with _snapshot_lock:
                    # A check begun before a reset (the agents' paths changed) is dropped, not kept.
                    if _snapshot["generation"] == generation:
                        _snapshot.update(value=fresh, at=time.monotonic())
            finally:
                with _snapshot_lock:
                    if _snapshot["generation"] == generation:
                        _snapshot["refreshing"] = False
        threading.Thread(target=refresh, daemon=True, name="mobster-agents-status").start()
    return public(value or {})


def public(states):
    """What the app shows of each agent's status: never its path's home folder beyond the file name."""
    keep = ("id", "name", "plan", "vendor", "installed", "signedIn", "ready", "version", "account", "billing", "hint",
            "installUrl", "installCommand", "signInCommand")
    return {agent_id: {key: state.get(key) for key in keep} for agent_id, state in (states or {}).items()}

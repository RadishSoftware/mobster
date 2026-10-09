"""Harness settings, kept in the run journal's ``harness_settings`` table (schema ``harness``).

One today: ``askUser``, "Mobster may ask me questions" (Settings › Advanced), on by default. ``MOBSTER_ASK_USER=off``
turns the question tool off for the whole process (the owner's paired A/B), whatever the setting says.
"""

import json
import os
import threading
import weakref

DEFAULTS = {"askUser": True}
ASK_USER_LIMIT = 2          # questions a task may ask
_cache = weakref.WeakKeyDictionary()   # journal -> {key: value}
_lock = threading.Lock()


def ask_user_switch():
    """The process-wide switch: on unless MOBSTER_ASK_USER says off."""
    return os.environ.get("MOBSTER_ASK_USER", "on").strip().casefold() not in ("off", "0", "false", "no")


def _journal(runtime):
    return getattr(runtime, "journal", None)


def load(runtime):
    """Every setting, the saved value over the default."""
    journal = _journal(runtime)
    values = dict(DEFAULTS)
    if journal is None or getattr(journal, "connection", None) is None:
        return values
    with _lock:
        cached = _cache.get(journal)
    if cached is not None:
        return {**values, **cached}
    saved = {}
    try:
        with journal.transaction() as connection:
            rows = connection.execute("SELECT key, value FROM harness_settings").fetchall()
        for key, raw in rows:
            if key in DEFAULTS:
                try:
                    saved[key] = json.loads(raw)
                except ValueError:
                    pass
    except Exception:  # noqa: BLE001 -- no table (the track is off) or no journal: the defaults
        saved = {}
    with _lock:
        _cache[journal] = saved
    return {**values, **saved}


def save(runtime, changes):
    """Save ``changes`` (validated by the caller). Returns every setting."""
    journal = _journal(runtime)
    if journal is None or getattr(journal, "connection", None) is None:
        raise ValueError("Settings can't be saved without Mobster's task history")
    with journal.transaction() as connection:
        for key, value in changes.items():
            connection.execute("INSERT INTO harness_settings(key, value) VALUES (?, ?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
    with _lock:
        _cache.pop(journal, None)
    return load(runtime)


def ask_user(runtime):
    """Whether Mobster's agent may ask clarifying questions in tasks a person watches."""
    return ask_user_switch() and load(runtime).get("askUser") is not False

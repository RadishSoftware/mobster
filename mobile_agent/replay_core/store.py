"""The replay cache: one JSON file per path, on this machine only.

A path is found by two keys:

- ``identity``: what it replays (a check's steps, app and launch; a workflow's app and goal template);
- ``env``: where it was recorded: the app's version and build, the iOS major version, the kind of device
  (simulator or iPhone) and its model.

A new app build, iOS major version or device model finds no path, so the next run records a fresh one (the run says
why); a minor iOS update keeps it, and the heal step absorbs what changed. Files are ``<identity 16>-<env 8>.json``;
an identity keeps its newest ``KEEP`` environments.

Paths hold screen labels, so the folder is 0700 and each file 0600, written atomically. The checks' cache lives in
`.mobster/cache/replay/`, which `.mobster/.gitignore` already ignores.
"""

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .path import PathError, validate

KEEP = 4
PROGRAM_ID = re.compile(r"[0-9a-f]{16}-[0-9a-f]{8}")
NAME = re.compile(PROGRAM_ID.pattern + r"\.json")
_MISSING = object()


def ios_major(runtime):
    """"26" from "iOS 26.4", "26.4" or "com.apple.CoreSimulator.SimRuntime.iOS-26-4"; None when unknown."""
    if not runtime:
        return None
    found = re.search(r"(\d+)(?:[.\-]\d+)*\s*$", str(runtime).strip())
    return found.group(1) if found else None


def env_of(app_version, runtime, device_kind, *, device_type=_MISSING):
    env = {"app_version": str(app_version) if app_version else None, "ios": ios_major(runtime),
           "device": device_kind or None}
    if device_type is not _MISSING:
        model = str(device_type).strip() if device_type is not None else ""
        env["model"] = model or None
    return env


def _digest(value, length):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:length]


def file_name(identity, env):
    return f"{_digest(identity, 16)}-{_digest(env, 8)}.json"


def revision(path):
    """The approved execution contract; run counters and review receipts do not change it."""
    fields = ("version", "kind", "identity", "env", "steps", "final", "strict")
    return hashlib.sha256(json.dumps({name: path.get(name) for name in fields}, sort_keys=True,
                                     ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def atomic_write(path, content):
    """Replace a file durably; callers keep the mutation lock through any related state change."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync_directory(directory):
    handle = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


class Cache:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _file(self, identity, env):
        return self.directory / file_name(identity, env)

    @contextmanager
    def locked(self):
        """Serialize short check-path mutations without holding a lock during device or model work."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = os.open(self.directory / ".mutation.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)

    def load(self, identity, env):
        """The stored path for ``identity`` in ``env``, or None (none, unreadable or not valid: never an error)."""
        try:
            data = json.loads(self._file(identity, env).read_text(encoding="utf-8"))
            path = validate(data)
            return path if path.get("identity") == identity and path.get("env") == env else None
        except (OSError, ValueError, PathError):
            return None

    def others(self, identity):
        """The environments other paths for ``identity`` were recorded in, newest first."""
        prefix = _digest(identity, 16)
        found = []
        for path in self._siblings(prefix):
            try:
                data = validate(json.loads(path.read_text(encoding="utf-8")))
                env = data.get("env")
                if isinstance(env, dict):
                    found.append((path.stat().st_mtime, env))
            except (OSError, ValueError):
                continue
        return [env for _, env in sorted(found, key=lambda item: -item[0])]

    def _siblings(self, prefix):
        try:
            return [path for path in self.directory.iterdir() if path.name.startswith(prefix + "-")
                    and NAME.fullmatch(path.name)]
        except OSError:
            return []

    def save(self, path):
        """Write ``path`` (validated); keeps the identity's newest KEEP environments. Returns the file."""
        validate(path)
        identity, env = path["identity"], path["env"]
        target = self._file(identity, env)
        atomic_write(target, json.dumps(path, ensure_ascii=False))
        siblings = sorted(self._siblings(_digest(identity, 16)), key=_mtime, reverse=True)
        for stale in siblings[KEEP:]:
            try:
                stale.unlink()
            except OSError:
                pass
        return target

    def forget(self, identity, env):
        try:
            self._file(identity, env).unlink()
            return True
        except OSError:
            return False


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


__all__ = ["Cache", "env_of", "ios_major", "file_name", "revision", "atomic_write", "sync_directory",
           "PROGRAM_ID", "KEEP"]

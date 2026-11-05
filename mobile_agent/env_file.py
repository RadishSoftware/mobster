"""Private folders and files, and the KEY=value lines of an env file (agent.env)."""

import fcntl
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path


def private_dir(path):
    """A folder only this user can open (the app data folder, logs), also when it exists."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def open_private(path, mode):
    """A file opened for writing ("w", "ab") with mode 0600, also when it exists."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_APPEND if "a" in mode else os.O_TRUNC)
    stream = open(os.open(path, flags, 0o600), mode)
    os.fchmod(stream.fileno(), 0o600)
    return stream


def check_changes(changes):
    for key, value in changes.items():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) or value is not None and (
                not isinstance(value, str) or any(c in "\r\n" or "\ud800" <= c <= "\udfff" for c in value)):
            raise ValueError("Invalid environment entry")


@contextmanager
def writer(path):
    """Own the file for a whole read, change and write, in any process. Keychain migrations hold it too."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Replacing the env file changes its inode, so lock a stable sibling for the entire read/change/write.
    with os.fdopen(os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600),
                   "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path


def write_locked(path, changes):
    """Rewrite while ``writer`` owns the file, including a Keychain migration."""
    # A line that is not UTF-8 is kept byte for byte, so Setup can still save a key into such a file (and
    # a byte-order mark goes, as load_env_file ignores it: a key on the first line is still replaced).
    text = path.read_bytes().decode("utf-8", "surrogateescape").removeprefix("\ufeff") if path.exists() else ""
    lines = text.splitlines()
    for key in changes:
        lines = [line for line in lines if not re.match(rf"\s*(export\s+)?{re.escape(key)}\s*=", line)]
    lines += [f"{key}={value}" for key, value in changes.items() if value is not None]
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".env-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", errors="surrogateescape") as stream:
            stream.write("\n".join(lines) + ("\n" if lines else ""))
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise

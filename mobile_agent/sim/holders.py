"""Who holds each simulator: one small file per simulator, written while a process holds its lease.

The lease itself is a file lock the system frees when its owner dies, so a record whose process is gone is ignored.
"""

import contextlib
import json
import os
import time
from pathlib import Path

FOLDER = "holders"
RELEASE_SUFFIX = ".release"


def _path(data_dir, udid, suffix=".json"):
    return Path(data_dir) / FOLDER / f"{udid}{suffix}"


def alive(pid):
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def claim(data_dir, udid, who):
    """Record this process as the holder. ``who``: {"client", "session"}."""
    record = {"pid": os.getpid(), "ppid": os.getppid(), "client": str(who.get("client") or "mobster")[:40],
              "session": str(who.get("session") or f"pid {os.getpid()}")[:80], "since": time.time()}
    with contextlib.suppress(OSError):
        folder = _path(data_dir, udid).parent
        folder.mkdir(parents=True, exist_ok=True)
        _path(data_dir, udid).write_text(json.dumps(record))
        _path(data_dir, udid, RELEASE_SUFFIX).unlink(missing_ok=True)
    return record


def read(data_dir, udid):
    """The live holder's record, or None."""
    try:
        record = json.loads(_path(data_dir, udid).read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and alive(record.get("pid")) else None


def clear(data_dir, udid):
    """Forget the holder, only when it is this process."""
    record = read(data_dir, udid)
    if record is not None and record.get("pid") != os.getpid():
        return
    with contextlib.suppress(OSError):
        _path(data_dir, udid).unlink(missing_ok=True)
        _path(data_dir, udid, RELEASE_SUFFIX).unlink(missing_ok=True)


def words(record):
    minutes = max(0, int((time.time() - float(record.get("since") or time.time())) // 60))
    return f"{record.get('client')} ({record.get('session')}), {minutes} min"


def ancestors(pid=None):
    """The process ids from ``pid`` up to launchd, found with ps."""
    import subprocess
    chain, pid = [], pid or os.getpid()
    while pid and pid > 1 and len(chain) < 32:
        chain.append(pid)
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=5).stdout.strip()
            pid = int(out)
        except (OSError, ValueError, subprocess.SubprocessError):
            break
    return chain


def request_release(data_dir, udid):
    """Ask the holder to let go; its server notices at its next sweep."""
    with contextlib.suppress(OSError):
        _path(data_dir, udid, RELEASE_SUFFIX).write_text("")


def release_requested(data_dir, udid):
    return _path(data_dir, udid, RELEASE_SUFFIX).exists() and read(data_dir, udid) is not None

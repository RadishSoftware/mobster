"""The files a client's config lives in: read as text, shown with ~, backed up, and written in one step."""

import os
import shutil
import tempfile
from pathlib import Path

from . import textedit


def _read(path):
    """The file's text, or None when there is no file."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        raise textedit.EditError("the file isn't UTF-8 text") from None


def _home(path):
    """``path`` with the home folder written as ~."""
    text, home = str(path), os.environ.get("HOME", "")
    return "~" + text[len(home):] if home and text.startswith(home + "/") else text


BACKUP_SUFFIX = ".mobster-backup"


def backup(path):
    """Copy ``path`` to ``path`` + ".mobster-backup" (the copy before Mobster's latest change). No file, no copy."""
    path = Path(os.path.realpath(path))
    if path.is_file():
        target = path.with_name(path.name + BACKUP_SUFFIX)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{target.name}.", delete=False) as handle:
            temp = Path(handle.name)
            try:
                shutil.copy2(path, temp)
                os.chmod(temp, path.stat().st_mode & 0o777)
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)


def write_atomic(path, text, keep_backup=True):
    """Write ``text`` to ``path`` in one step: a temporary file beside it, flushed, then renamed over it."""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if keep_backup:
        backup(path)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600 if path.parent.stat().st_mode & 0o077 == 0 else 0o644
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.mobster-", suffix=".tmp", delete=False) as handle:
        temp = Path(handle.name)
        try:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            os.chmod(temp, mode)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

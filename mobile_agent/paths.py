"""Where Mobster writes."""

import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
IDENTIFIER = "app.mobster.desktop"
# The folder the shell hands its agent (`serve --data-dir`): `main()` exports it, so every child process agrees.
DATA_DIR_ENV = "MOBSTER_APP_DATA_DIR"
# A source checkout's data root when nothing names one.
CHECKOUT_DATA = PACKAGE / ".state" / "data"


def write_text_atomic(path, text):
    """Write ``text`` to ``path`` (UTF-8) through a sibling .tmp file, so a reader never sees half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def installed_data_dir():
    """The installed Mac app's folder."""
    return Path.home() / "Library" / "Application Support" / IDENTIFIER


def user_data_dir():
    """The data root every store (memory, connectors, settings, queue, ...) resolves under."""
    value = os.environ.get(DATA_DIR_ENV, "").strip()
    if value:
        return Path(value).expanduser().resolve()
    if source_checkout():
        return CHECKOUT_DATA
    if os.environ.get("MOBSTER_DEV_SHELL"):
        return installed_data_dir().with_name(IDENTIFIER + ".dev")
    return installed_data_dir()


def machine_data_dir():
    """What the whole Mac shares by design, whichever copy of Mobster runs."""
    return installed_data_dir()


def use_data_dir(path):
    """`serve --data-dir`: make `path` the data root for this process and the ones it starts."""
    os.environ[DATA_DIR_ENV] = str(Path(path).expanduser().resolve())


def keychain_service(service):
    """A Keychain service name for this run."""
    root = user_data_dir().resolve()
    installed = installed_data_dir().resolve()
    if root == installed:
        return service
    name = root.name
    tag = name[len(IDENTIFIER) + 1:] if root.parent == installed.parent and name.startswith(IDENTIFIER + ".") else ""
    if not re.fullmatch(r"dev[0-9]*|qa", tag):
        tag = ""
    return f"{service}.{tag or 'dev-' + hashlib.sha256(str(root).encode()).hexdigest()[:8]}"


def dev_data_dir():
    """The developer tools' folder (`mobster verify`, `mobster sim`, `mobster mcp`)."""
    value = os.environ.get("MOBSTER_DATA_DIR", "").strip()
    return Path(value).expanduser() if value else machine_data_dir() / "dev"


def source_checkout():
    return not getattr(sys, "frozen", False) and (PACKAGE.parent / "pyproject.toml").is_file()


def default_history_db():
    """The terminal's own journal beside `serve`'s: a journal has one owner (journal.Lease), so both can run."""
    return (PACKAGE / ".state" if source_checkout() else user_data_dir() / "state") / "terminal.sqlite3"


def build_dir():
    """Compiled helpers (`mobster build-ocr`)."""
    return PACKAGE / ".build" if source_checkout() else user_data_dir() / "build"


def write_private(path, text, binary=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, "wb" if binary else "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)

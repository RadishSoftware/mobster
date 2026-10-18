"""Where Mobster writes. A source checkout keeps its files beside the package; an installed or frozen copy
uses the user's data folder, never site-packages or the signed app bundle."""

import hashlib
import os
from pathlib import Path
import re
import sys

PACKAGE = Path(__file__).resolve().parent
IDENTIFIER = "app.mobster.desktop"
# The folder the shell hands its agent (`serve --data-dir`): `main()` exports it, so every child process agrees.
DATA_DIR_ENV = "MOBSTER_APP_DATA_DIR"
# A source checkout's data root when nothing names one.
CHECKOUT_DATA = PACKAGE / ".state" / "data"


def write_text_atomic(path, text):
    """Write ``text`` to ``path`` (UTF-8) through a sibling .tmp file, so a reader never sees half a file. Makes
    the parent folders; raises OSError."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return path


def installed_data_dir():
    """The installed Mac app's folder. Only the installed app writes here, except for what the whole Mac shares
    (see `machine_data_dir`)."""
    return Path.home() / "Library" / "Application Support" / IDENTIFIER


def user_data_dir():
    """The data root: the one decision every store (memory, connectors, outlets, profiles, learned steps, settings,
    queue, attachments, ...) resolves under.

    The installed app and its agent use the installed folder, so one iPhone setup serves both. A dev window slot
    (`app.mobster.desktop.devN`) uses its own folder, handed down as $MOBSTER_APP_DATA_DIR. A source checkout without
    one keeps its data beside the package (`mobile_agent/.state/data`), as it does `mobster.sqlite3`; a development
    shell without one gets the `.dev` folder. A development run never lands in the installed app's folder."""
    value = os.environ.get(DATA_DIR_ENV, "").strip()
    if value:
        return Path(value).expanduser().resolve()
    if source_checkout():
        return CHECKOUT_DATA
    if os.environ.get("MOBSTER_DEV_SHELL"):
        return installed_data_dir().with_name(IDENTIFIER + ".dev")
    return installed_data_dir()


def machine_data_dir():
    """What the whole Mac shares by design, whichever copy of Mobster runs: the device leases (one physical phone,
    one driver), compiled helper binaries (named by their source hash) and the simulators `mobster sim` manages."""
    return installed_data_dir()


def use_data_dir(path):
    """`serve --data-dir`: make `path` the data root for this process and the ones it starts."""
    os.environ[DATA_DIR_ENV] = str(Path(path).expanduser().resolve())


def keychain_service(service):
    """A Keychain service name for this run. The installed app's names are unchanged; any other data root gets its
    own (`<service>.dev2`), so a dev slot never reads or overwrites the installed app's items."""
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
    """The developer tools' folder (`mobster verify`, `mobster sim`, `mobster mcp`): simulators, the
    WebDriverAgent build and logs. $MOBSTER_DATA_DIR when set, else the `dev` folder of the data folder."""
    value = os.environ.get("MOBSTER_DATA_DIR", "").strip()
    return Path(value).expanduser() if value else machine_data_dir() / "dev"


def source_checkout():
    return not getattr(sys, "frozen", False) and (PACKAGE.parent / "pyproject.toml").is_file()


def default_history_db():
    """The terminal's own journal, beside `serve`'s but a separate file.

    A journal has one owner at a time (``journal.Lease``); a separate file lets the terminal UI run while
    `mobster serve` or the Mac app holds theirs.
    """
    return (PACKAGE / ".state" if source_checkout() else user_data_dir() / "state") / "terminal.sqlite3"


def build_dir():
    """Compiled helpers (`mobster build-ocr`, `vision_judge build-t0`)."""
    return PACKAGE / ".build" if source_checkout() else user_data_dir() / "build"


def write_private(path, text, binary=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, "wb" if binary else "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)

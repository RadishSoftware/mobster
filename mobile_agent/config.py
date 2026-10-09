"""Typed run budgets. One validated place for every loop bound.

``Agent`` keeps its keyword arguments and its ``ValueError`` contract; it
validates through this module so the CLI, the server, and the agent can share
one definition instead of re-checking magic numbers at each layer.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .state import finite


@dataclass(frozen=True)
class RunBudgets:
    """Bounds for one agent run. All limits are inclusive upper bounds."""

    max_steps: int = 30
    max_seconds: float = 120
    max_helper_calls: int = 4
    settle_seconds: float = .6
    max_undispatched: int = 4
    max_extraction_retries: int = 1
    spend_cap_usd: float | None = None

    def __post_init__(self):
        if (type(self.max_steps) is not int or not 1 <= self.max_steps <= 10000
                or not finite(self.max_seconds) or self.max_seconds <= 0
                or not finite(self.settle_seconds) or not 0 <= self.settle_seconds <= 5
                or type(self.max_helper_calls) is not int or not 0 <= self.max_helper_calls <= 10000
                or type(self.max_undispatched) is not int or not 0 <= self.max_undispatched <= 100
                or type(self.max_extraction_retries) is not int or not 0 <= self.max_extraction_retries <= 10):
            raise ValueError("Invalid run budget")
        if (self.spend_cap_usd is not None
                and (not finite(self.spend_cap_usd) or self.spend_cap_usd <= 0)):
            raise ValueError("Spend cap must be a positive USD amount")


class EnvFile(NamedTuple):
    """What ``load_env_file`` did: the names it set (never the values) and one warning per
    line it skipped (by line number, never the line's text)."""

    loaded: list
    warnings: list


def missing_env_file(path):
    """The warning for an env file that doesn't exist: one sentence naming it (~ for the home folder)."""
    shown = str(path)
    home = str(Path.home())
    if shown == home or shown.startswith(home + "/"):
        shown = "~" + shown[len(home):]
    return f"{shown} does not exist; no keys or settings were loaded from it"


def load_env_file(path, missing_ok=False):
    """Load KEY=VALUE lines into the environment. Variables already set win.

    Explicit, never implicit: the desktop app passes its private env file, and
    developers can pass mobile_agent/.env. Blank lines and # comments are
    skipped; surrounding single or double quotes are removed. A missing file
    loads nothing and, unless ``missing_ok``, says so in ``warnings``: a
    mistyped --env-file would otherwise look like a missing key. `serve`
    passes ``missing_ok``: the Mac app's setup flow creates the file when the
    first value is saved.

    One bad line never stops the agent: people edit this file by hand, and the
    Mac app's Setup and Settings, which could fix it, need the agent running. A
    line that is not KEY=VALUE, has an invalid name or is not UTF-8 is skipped
    and reported in ``warnings``; so is a file that cannot be read.
    """
    import os
    import re
    loaded, warnings = [], []
    name = Path(path).name
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        if not missing_ok:
            warnings.append(missing_env_file(path))
        return EnvFile(loaded, warnings)
    except OSError as error:
        warnings.append(f"{name} could not be read ({type(error).__name__}); no settings were loaded from it")
        return EnvFile(loaded, warnings)
    for number, raw in enumerate(data.removeprefix(b"\xef\xbb\xbf").splitlines(), 1):
        try:
            line = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            warnings.append(f"{name} line {number} is not UTF-8 text; skipped it")
            continue
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            warnings.append(f"{name} line {number} is not NAME=value; skipped it")
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        key = key.removeprefix("export ").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            warnings.append(f"{name} line {number} has a name with characters other than letters, digits "
                            "and _; skipped it")
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return EnvFile(loaded, warnings)


# Where each key and setting this process loaded came from: {name: "env-file" | "mac-app"}. A name set in the shell
# is not here. doctor and `mobster login status` read it to say "from Mobster for Mac".
SOURCES = {}


def app_env_file():
    """The file Mobster for Mac (and `mobster login`) keep keys and settings in: agent.env in the data folder, 0600."""
    from .paths import user_data_dir
    return user_data_dir() / "agent.env"


def load_env_layers(env_file=None):
    """Load an explicit --env-file (or $MOBSTER_ENV_FILE), then the keys Mobster for Mac saved, without overriding
    anything already set. Precedence, highest first: the process environment, ``env_file``, the app's agent.env.
    Returns the warnings (a missing or unreadable --env-file, bad lines), never a value."""
    warnings = []
    if env_file:
        loaded = load_env_file(env_file)
        SOURCES.update({name: "env-file" for name in loaded.loaded})
        warnings += loaded.warnings
    path = app_env_file()
    if env_file is None or Path(env_file).expanduser().resolve() != path.resolve():
        loaded = load_env_file(path, missing_ok=True)
        SOURCES.update({name: "mac-app" for name in loaded.loaded})
        # The app's own file is never the user's to fix line by line: its bad lines are skipped quietly.
    return warnings

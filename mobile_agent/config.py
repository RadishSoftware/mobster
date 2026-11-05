"""Typed run budgets."""

from pathlib import Path
from typing import NamedTuple


class EnvFile(NamedTuple):
    """What ``load_env_file`` did."""

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
    """Load KEY=VALUE lines into the environment. Variables already set win; a bad line is a warning, never a stop."""
    import os

    from . import env_secrets
    values, warnings = _parse_env_file(path, missing_ok)
    loaded = []
    # Settings can choose the secret store (MOBSTER_VAULT=file). App secrets come only from its locked read,
    # never this plaintext snapshot, which may precede a key replacement or logout.
    for key, value in values.items():
        if key not in env_secrets.SECRET_NAMES and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    secrets, problems = env_secrets.secrets_for(path, values)
    warnings += problems
    if not env_secrets.applies(path):
        secrets = {key: value for key, value in values.items() if key in env_secrets.SECRET_NAMES}
    for key, value in secrets.items():
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return EnvFile(loaded, warnings)


def read_env_values(path):
    """{NAME: value} for the file's valid lines, without touching the environment ({} when it can't be read)."""
    return _parse_env_file(path, missing_ok=True)[0]


def _parse_env_file(path, missing_ok):
    import re
    values, warnings = {}, []
    name = Path(path).name
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        if not missing_ok:
            warnings.append(missing_env_file(path))
        return values, warnings
    except OSError as error:
        warnings.append(f"{name} could not be read ({type(error).__name__}); no settings were loaded from it")
        return values, warnings
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
        if "\x00" in value:
            warnings.append(f"{name} line {number} contains a null character; skipped it")
            continue
        values.setdefault(key, value)  # the first line for a name wins, as it did when each line set the environment
    return values, warnings


# Where each key and setting this process loaded came from: {name: "env-file" | "mac-app"}. A name set in the shell
# is not here. doctor and `mobster login status` read it to say "from Mobster for Mac".
SOURCES = {}


def app_env_file():
    """The file Mobster for Mac (and `mobster login`) keep keys and settings in: agent.env in the data folder, 0600."""
    from .paths import user_data_dir
    return user_data_dir() / "agent.env"


def load_env_layers(env_file=None):
    """Load an explicit --env-file, then the keys Mobster for Mac saved, never over a variable already set."""
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

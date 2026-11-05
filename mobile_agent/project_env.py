"""Per-project environment and secrets: ``.mobster/env.example`` names them, the Keychain holds their values.

``.mobster/env.example`` is committed and lists names only (``TEST_EMAIL``, ``TEST_PASSWORD``; a ``=…`` after a name
is ignored). The values are stored per project in the login Keychain (one item per project folder), never in the
repository and never printed. Off a Mac, in a scratch HOME or with ``MOBSTER_VAULT=file`` they live in a 0600 file in
Mobster's data folder instead. A name that is set in the process environment also resolves (CI has no Keychain); the
Keychain wins when both exist.

A check writes ``${NAME}`` in ``launch.args``, ``launch.env``, ``launch.url`` and its steps. A run expands them when it
starts and masks every value in its log, events, result, report and step text (verify/runner.py). A name the check uses
that isn't in ``env.example``, or that has no value, refuses the run before a simulator is touched: it never runs with
an empty value. Nothing here logs or returns a value except ``ProjectEnv.resolve``.
"""

import base64
import hashlib
import html
import json
import os
import re
from pathlib import Path

from . import env_file, env_secrets, keychain_acl

NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
REF = re.compile(r"\$\{(" + NAME.pattern + r")\}")
EXAMPLE = Path(".mobster") / "env.example"
SERVICE = "app.mobster.project-env"
MAX_VALUE = 4000


class EnvError(Exception):
    """A request Mobster refuses; the message says what happened and what to do."""


class Missing(EnvError):
    """Names a run needs that have no value (``names``) or aren't listed in env.example (``undeclared``)."""

    def __init__(self, names, undeclared=()):
        self.names, self.undeclared = sorted(names), sorted(undeclared)
        parts = []
        if self.undeclared:
            parts.append(f"{', '.join(self.undeclared)} isn't listed in {EXAMPLE}; add the name there")
        if self.names:
            parts.append(f"{', '.join(self.names)} has no value for this project; run "
                         f"`mobster env set {self.names[0]}`")
        super().__init__("This check needs " + (" and ".join(parts) if parts else "a value") + ".")


def refs(*texts):
    """The ``${NAME}`` names in the given strings (or lists and dicts of them), sorted."""
    found = set()

    def walk(value):
        if isinstance(value, str):
            found.update(REF.findall(value))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
    walk(texts)
    return sorted(found)


def expand(value, values):
    """``value`` (a string, or a list or dict of them) with each ``${NAME}`` replaced from ``values``."""
    if isinstance(value, str):
        return REF.sub(lambda match: values[match.group(1)], value)
    if isinstance(value, dict):
        return {key: expand(item, values) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(expand(item, values) for item in value)
    return value


def validate_root(value, runtime=None):
    """The ``projectEnv`` run field: the project folder whose secrets a task's ``${NAME}`` come from."""
    path = Path(value) if isinstance(value, str) and value else None
    if path is None or not path.is_absolute() or not path.is_dir():
        raise ValueError("projectEnv must be the absolute path of a project folder that exists")
    return str(path)


def for_task(root, goal):
    """{NAME: value} for the ``${NAME}``s in a task's goal (EnvError when one has no value or isn't listed in the
    project's env.example): a saved task that needs a secret is refused before it starts, never run with a blank."""
    names = refs(goal)
    return ProjectEnv(root).resolve(names) if names else {}


def mask_text(text, values):
    from .agent_hooks import mask
    secrets = list(values.values()) if isinstance(values, dict) else list(values)
    forms = {form for value in secrets if value for form in
             (value, json.dumps(value, ensure_ascii=False)[1:-1], html.escape(value), html.escape(value, quote=False))}
    return mask(text, forms)


def mask_deep(value, values):
    """``value`` (nested dicts, lists, strings) with every secret value replaced by the mask."""
    secrets = [v for v in (values.values() if isinstance(values, dict) else values) if v]
    if not secrets:
        return value
    if isinstance(value, str):
        return mask_text(value, secrets)
    if isinstance(value, dict):
        return {mask_text(key, secrets) if isinstance(key, str) else key: mask_deep(item, secrets)
                for key, item in value.items()}
    if isinstance(value, list):
        return [mask_deep(item, secrets) for item in value]
    return value


def sweep_files(folder, values, suffixes=(".json", ".jsonl", ".log", ".yaml", ".yml", ".html", ".xml", ".txt", ".md",
                                         ".csv")):
    """Replace every secret value in the text files under ``folder``. Returns how many files changed."""
    secrets = [v for v in values.values() if v]
    changed = 0
    if not secrets:
        return changed
    for path in Path(folder).rglob("*"):
        if path.suffix.lower() not in suffixes or path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        masked = mask_text(text, secrets)
        if masked != text:
            _rewrite(path, masked)
            changed += 1
    return changed


def _rewrite(path, text):
    temporary = path.with_name(path.name + ".tmp")
    with env_file.open_private(temporary, "w") as handle:
        handle.write(text)
    os.replace(temporary, path)


def declared(root):
    """The names in ``<root>/.mobster/env.example``, in file order. Lines are ``NAME`` or ``NAME=…``; ``#`` comments."""
    path = Path(root) / EXAMPLE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as error:
        raise EnvError(f"{path} can't be read ({type(error).__name__}). Check the file.") from None
    names = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("=", 1)[0].strip().removeprefix("export ").strip()
        if not NAME.fullmatch(name):
            raise EnvError(f"{path} lists {name[:40]!r}, which isn't a variable name (A-Z, 0-9 and _). Fix that line.")
        if name not in names:
            names.append(name)
    return names


class _KeychainRecord:
    def __init__(self, account):
        self.account = account
        self.store = keychain_acl.Store(SERVICE)

    def load(self):
        try:
            word = self.store.get(self.account)
        except keychain_acl.KeychainError as error:
            raise EnvError(f"The Keychain didn't give Mobster this project's values ({error}). Unlock the login "
                           "keychain, then try again.") from None
        if word is None:
            return {}
        try:
            data = json.loads(base64.b64decode(word.strip()))
        except (ValueError, TypeError):
            raise EnvError("This project's values in the Keychain aren't readable. Set them again with "
                           "`mobster env set`.") from None
        return {k: v for k, v in data.items() if NAME.fullmatch(k) and isinstance(v, str)} \
            if isinstance(data, dict) else {}

    def save(self, record):
        try:
            if record:
                self.store.set(self.account, base64.b64encode(json.dumps(record, sort_keys=True).encode()).decode())
            else:
                self.store.delete(self.account)
        except keychain_acl.KeychainError:
            raise EnvError("The Keychain didn't take this project's values, so nothing was saved. Unlock the login "
                           "keychain, then try again.") from None


class _FileRecord:
    def __init__(self, account):
        from .paths import user_data_dir
        self.path = user_data_dir() / "project-env" / f"{account}.json"

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise EnvError(f"{self.path} isn't readable. Set the values again with `mobster env set`.") from None
        return {k: v for k, v in data.items() if NAME.fullmatch(k) and isinstance(v, str)} \
            if isinstance(data, dict) else {}

    def save(self, record):
        if not record:
            self.path.unlink(missing_ok=True)
            return
        env_file.private_dir(self.path.parent)
        _rewrite(self.path, json.dumps(record, sort_keys=True))


def account_for(root):
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:32]


class ProjectEnv:
    """One project's names and values. ``record`` (load/save of {name: value}) is injectable for tests."""

    def __init__(self, root, record=None):
        self.root = Path(root).resolve()
        if record is None:
            account = account_for(self.root)
            record = _KeychainRecord(account) if env_secrets.backend() is not None else _FileRecord(account)
        self.record = record
        from .paths import user_data_dir
        self.lock_path = user_data_dir() / "project-env" / (account_for(self.root) + ".json")

    def names(self):
        return declared(self.root)

    def status(self):
        """[{name, set, source}] for each declared name: source is keychain, environment or None. No values."""
        stored = self.record.load()
        rows = []
        for name in self.names():
            source = "keychain" if stored.get(name) else "environment" if os.environ.get(name) else None
            rows.append({"name": name, "set": source is not None, "source": source})
        return rows

    def set(self, name, value):
        """Store ``value`` under ``name`` for this project, adding the name to env.example (names only)."""
        if not NAME.fullmatch(name or ""):
            raise EnvError(f"{str(name)[:40]!r} isn't a variable name. Use A-Z, 0-9 and _, starting with a letter.")
        if not isinstance(value, str) or not value or any(c in "\r\n" for c in value) or len(value) > MAX_VALUE:
            raise EnvError(f"The value for {name} must be one non-empty line of at most {MAX_VALUE} characters.")
        with env_file.writer(self.lock_path):
            record = self.record.load()
            record[name] = value
            self.record.save(record)
            self._declare(name)

    def unset(self, name):
        """Remove the stored value. Returns whether there was one. The name stays in env.example."""
        with env_file.writer(self.lock_path):
            record = self.record.load()
            if name not in record:
                return False
            del record[name]
            self.record.save(record)
            return True

    def _declare(self, name):
        if name in self.names():
            return
        path = self.root / EXAMPLE
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        _rewrite(path, text + ("" if not text or text.endswith("\n") else "\n") + name + "\n")

    def resolve(self, wanted):
        """{name: value} for the names in ``wanted``. Raises Missing for a name that isn't declared or has no value."""
        wanted = sorted(set(wanted))
        if not wanted:
            return {}
        declared_names = set(self.names())
        undeclared = [name for name in wanted if name not in declared_names]
        stored = self.record.load() if len(undeclared) < len(wanted) else {}
        values = {name: stored.get(name) or os.environ.get(name) or "" for name in wanted if name in declared_names}
        empty = [name for name, value in values.items() if not value]
        if undeclared or empty:
            raise Missing(empty, undeclared)
        return values

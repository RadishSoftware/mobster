"""The secrets in Mobster for Mac's settings file live in the login Keychain, never in the file."""

import base64
import json
import os
import subprocess
from pathlib import Path

from . import env_file, keychain_acl
from .ai_plans.vault import on_mac

SECRET_NAMES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TEXT_MODEL_API_KEY", "MOBSTER_ALERT_WEBHOOK_URL")
MARKER = "MOBSTER_SECRETS_IN_KEYCHAIN"
SERVICE = "app.mobster.desktop.settings"
ACCOUNT = "agent.env"


REFUSED = ("Mobster couldn't save this in your Keychain, so nothing was saved. Open Keychain Access, unlock the login "
           "keychain, then try again.")


class KeychainError(OSError):
    """The Keychain didn't read or take the secrets."""


class Keychain:
    """The secrets as one generic password: base64 of a JSON object."""

    def __init__(self, service=SERVICE, account=ACCOUNT, runner=subprocess.run, bridge=None):
        self.service, self.account = service, account
        self.store = keychain_acl.Store(
            service, bridge=bridge,
            runner=lambda argv, data: runner(argv, input=data, capture_output=True, timeout=10))

    def load(self):
        """{name: value}; {} when there is no item. KeychainError when there is one it can't read."""
        try:
            word = self.store.get(self.account)
        except keychain_acl.KeychainError as error:
            raise KeychainError(f"The Keychain didn't give Mobster its settings ({error})") from None
        if word is None:
            return {}
        try:
            data = json.loads(base64.b64decode(word.strip()))
        except (ValueError, TypeError):
            raise KeychainError("Mobster's settings in the Keychain aren't readable") from None
        return {k: v for k, v in data.items() if k in SECRET_NAMES and isinstance(v, str)} if isinstance(data, dict) \
            else {}

    def save(self, record):
        secret = base64.b64encode(json.dumps(record, sort_keys=True).encode()).decode()
        try:
            self.store.set(self.account, secret)
        except keychain_acl.KeychainError:
            raise KeychainError("The Keychain didn't take Mobster's settings") from None
        finally:
            secret = None  # noqa: F841 -- the secret's only other copy goes with this frame

    def clear(self):
        try:
            self.store.delete(self.account)
        except keychain_acl.KeychainError:
            pass


def backend():
    """The Keychain, or None where secrets stay in the file (off a Mac, a scratch HOME, MOBSTER_VAULT=file)."""
    if os.environ.get("MOBSTER_VAULT") == "file" or not on_mac():
        return None
    return Keychain()


def applies(path):
    """Whether ``path`` is the Mac app's agent.env and its secrets go to the Keychain."""
    if path is None:
        return False
    from .config import app_env_file
    try:
        if Path(path).expanduser().resolve() != app_env_file().resolve():
            return False
    except OSError:
        return False
    return backend() is not None


def split(path, changes):
    """The file's own changes: each secret goes to the Keychain first; KeychainError if refused, never plain text."""
    names = [name for name in changes if name in SECRET_NAMES]
    if not names or not applies(path):
        return changes
    store = backend()
    record = store.load()
    for name in names:
        if changes[name] is None:
            record.pop(name, None)
        else:
            record[name] = changes[name]
    if record:
        store.save(record)
    else:
        store.clear()
    rest = {name: value for name, value in changes.items() if name not in SECRET_NAMES}
    return {**rest, **{name: None for name in names}, MARKER: "1" if record else None}


def secrets_for(path, values):
    """(secrets, warnings) for ``path`` (lines ``values``): a secret in the file moves to the Keychain, or stays."""
    if not applies(path):
        return {}, []
    if not any(values.get(name) for name in SECRET_NAMES) and values.get(MARKER) != "1":
        return {}, []
    from .config import read_env_values
    store = backend()
    # The caller's read can precede a save or logout. Only the current file may initiate a migration or fallback.
    try:
        with env_file.writer(path) as path:
            values = read_env_values(path)
            in_file = {name: values[name] for name in SECRET_NAMES if values.get(name)}
            if not in_file and values.get(MARKER) != "1":
                return {}, []
            try:
                record = store.load()
            except KeychainError as error:
                return in_file, [f"{error}; Mobster's keys weren't loaded from it"]
            if in_file:
                try:
                    store.save({**record, **in_file})
                    env_file.write_locked(path, {**{name: None for name in in_file}, MARKER: "1"})
                except OSError:
                    return {**record, **in_file}, ["Mobster couldn't move its keys to the "
                                                   "Keychain; they stay in agent.env"]
                record = {**record, **in_file}
            return record, []
    except OSError as error:
        return {}, [f"Mobster couldn't read its saved keys ({type(error).__name__}); no keys "
                    "were loaded from the Keychain"]


def load(path):
    """The secrets saved for the file at ``path`` ({} when none, or not on a Mac)."""
    from .config import read_env_values
    return secrets_for(path, read_env_values(path))[0]


def update_env_file(path, changes):
    """Set (str) or remove (None) several KEY=value lines in one private, atomic rewrite."""
    env_file.check_changes(changes)
    with env_file.writer(path) as path:
        env_file.write_locked(path, split(path, changes))

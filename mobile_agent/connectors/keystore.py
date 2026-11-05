"""The two raw stores under the connector and destination vaults: a 0600 JSON file, and generic passwords in the login
Keychain (keychain_acl.py)."""

import json
import os
from pathlib import Path

from .. import keychain_acl


class VaultError(Exception):
    """The Keychain refused or couldn't be read. Never carries a secret."""


class FileStore:
    """``files``: what the file holds, in "Mobster couldn't read its <files> file"."""
    files = ""

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            value = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise VaultError(f"Mobster couldn't read its {self.files} file") from None
        return value if isinstance(value, dict) else {}

    def _save(self, items):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(items, stream)

    def _set(self, account, word):
        items = self._load()
        items[account] = word
        self._save(items)

    def _get(self, account):
        value = self._load().get(account)
        return value if isinstance(value, str) else None

    def _delete(self, account):
        items = self._load()
        if items.pop(account, None) is not None:
            self._save(items)


class KeychainStore:
    """Generic passwords readable only by Mobster's own programs."""
    service = noun = word = None

    def __init__(self, run=None, bridge=None):
        self.store = keychain_acl.Store(self.service, bridge=bridge, runner=run)

    def _set(self, account, word):
        if not self.word.fullmatch(word):
            raise VaultError(f"Mobster can't keep that {self.noun} in the Keychain")
        try:
            self.store.set(account, word)
        except keychain_acl.KeychainError:
            raise VaultError(f"The Keychain didn't save the {self.noun}") from None

    def _get(self, account):
        try:
            return self.store.get(account)
        except keychain_acl.KeychainError:
            raise VaultError(f"The Keychain didn't give Mobster the {self.noun}") from None

    def _delete(self, account):
        try:
            self.store.delete(account)
        except keychain_acl.KeychainError:
            raise VaultError(f"The Keychain didn't remove the {self.noun}") from None


def backend(env, file_vault, keychain_vault):
    spec = os.environ.get(env, "").strip()
    if spec.startswith("file:") and len(spec) > 5:
        return file_vault(Path(spec[5:]).expanduser())
    return keychain_vault()

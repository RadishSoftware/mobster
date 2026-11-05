"""Where a connector's secrets live: one generic password per connector in the login Keychain, readable only
by Mobster's own programs (keychain_acl.py), as outlets/vault.py keeps a destination's address."""

import base64
import json
import re

from . import keystore

SERVICE = "app.mobster.desktop.connectors"
ENV = "MOBSTER_CONNECTORS_KEYCHAIN"
ACCOUNT = re.compile(r"connector:[0-9a-f]{12}")
WORD = re.compile(r"[A-Za-z0-9_-]{1,16000}")
VaultError = keystore.VaultError


def account_for(connector_id):
    account = f"connector:{connector_id}"
    if not ACCOUNT.fullmatch(account):
        raise ValueError("A connector's id is 12 hex characters")
    return account


def encode(value):
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(word):
    try:
        value = json.loads(base64.urlsafe_b64decode(word + "=" * (-len(word) % 4)))
    except (ValueError, TypeError):
        raise VaultError("Mobster couldn't read a connector's sign-in") from None
    if not isinstance(value, dict):
        raise VaultError("Mobster couldn't read a connector's sign-in")
    return value


class Vault:
    """``put(id, secrets)``, ``get(id) -> dict`` ({} when none) and ``delete(id)`` over a raw store."""

    def put(self, connector_id, secrets):
        secrets = {k: v for k, v in (secrets or {}).items() if v}
        if not secrets:
            self.delete(connector_id)
            return
        self._set(account_for(connector_id), encode(secrets))

    def get(self, connector_id):
        word = self._get(account_for(connector_id))
        return decode(word) if word else {}

    def update(self, connector_id, **changes):
        current = self.get(connector_id)
        current.update(changes)
        self.put(connector_id, current)
        return current

    def delete(self, connector_id):
        self._delete(account_for(connector_id))


class FileVault(keystore.FileStore, Vault):
    files = "connectors"


class KeychainVault(keystore.KeychainStore, Vault):
    service, noun, word = SERVICE, "sign-in", WORD


def backend():
    return keystore.backend(ENV, FileVault, KeychainVault)

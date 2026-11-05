"""Where a destination's address, secret and token live: one generic password per destination in the login Keychain,
readable only by Mobster's own programs (keychain_acl.py), as remote/keychain.py keeps Mobster Remote's device key."""

import base64
import json
import re

from ..connectors import keystore

SERVICE = "app.mobster.desktop.outlets"
ENV = "MOBSTER_OUTLETS_KEYCHAIN"
ACCOUNT = re.compile(r"destination:[0-9a-f]{12}")
WORD = re.compile(r"[A-Za-z0-9_-]{1,8000}")
VaultError = keystore.VaultError


def account_for(destination_id):
    account = f"destination:{destination_id}"
    if not ACCOUNT.fullmatch(account):
        raise ValueError("A destination's id is 12 hex characters")
    return account


EXTRA = {"token": str, "to": str, "oauth": dict}


def encode(url, secret, extra=None):
    item = {"url": url, "secret": secret or None}
    for key, kind in EXTRA.items():
        if isinstance((extra or {}).get(key), kind) and extra[key]:
            item[key] = extra[key]
    raw = json.dumps(item, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(word):
    """{"url", "secret", "token"?, "to"?, "oauth"?}: ``url`` and ``secret`` may be None (a Notion, Sheets or email
    destination)."""
    try:
        raw = base64.urlsafe_b64decode(word + "=" * (-len(word) % 4))
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise VaultError("Mobster couldn't read a saved destination") from None
    kinds = {"url": str, **EXTRA}
    if not isinstance(value, dict) or not any(isinstance(value.get(key), kind) and value[key]
                                              for key, kind in kinds.items()):
        raise VaultError("Mobster couldn't read a saved destination")
    out = {"url": None, "secret": None}
    for key, kind in {"url": str, "secret": str, **EXTRA}.items():
        found = value.get(key)
        if isinstance(found, kind) and found:
            out[key] = found
    return out


class Vault:
    """``put(id, url, secret, extra)``, ``get(id) -> {"url", "secret", ...} | None`` and ``delete(id)`` over
    a raw store."""

    def put(self, destination_id, url, secret, extra=None):
        self._set(account_for(destination_id), encode(url, secret, extra))

    def get(self, destination_id):
        word = self._get(account_for(destination_id))
        return decode(word) if word else None

    def delete(self, destination_id):
        self._delete(account_for(destination_id))


class FileVault(keystore.FileStore, Vault):
    files = "destinations"


class KeychainVault(keystore.KeychainStore, Vault):
    service, noun, word = SERVICE, "destination", WORD


def backend():
    return keystore.backend(ENV, FileVault, KeychainVault)

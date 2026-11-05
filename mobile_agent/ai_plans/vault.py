"""Where Mobster keeps a plan's tokens and a provider's API key: the macOS Keychain, never a plaintext file."""

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import keychain_acl

SERVICE = "Mobster AI"


def write_private_json(path, data):
    """Write ``data`` to ``path`` atomically, owner-only (0600 in a 0700 folder)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, indent=1, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class FileVault:
    """A record in a 0600 JSON file (off macOS, and in tests)."""

    kind = "file"

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, record):
        write_private_json(self.path, record)

    def clear(self):
        self.path.unlink(missing_ok=True)


class KeychainVault:
    """A record (JSON, base64) as one generic password in the login Keychain."""

    kind = "keychain"

    def __init__(self, service=SERVICE, account="default", runner=subprocess.run, bridge=None):
        self.service, self.account = service, account
        self.store = keychain_acl.Store(
            service, bridge=bridge,
            runner=lambda argv, data: runner(argv, input=data, capture_output=True, timeout=10))

    def load(self):
        try:
            word = self.store.get(self.account)
        except keychain_acl.KeychainError:
            return {}
        if not word:
            return {}
        try:
            data = json.loads(base64.b64decode(word.strip()))
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, record):
        secret = base64.b64encode(json.dumps(record, sort_keys=True).encode()).decode()
        try:
            self.store.set(self.account, secret)
        except keychain_acl.KeychainError:
            raise OSError("The Keychain didn't take the sign-in") from None

    def clear(self):
        try:
            self.store.delete(self.account)
        except keychain_acl.KeychainError:
            pass


def on_mac():
    """A Mac, with HOME the account's own home folder: a scratch HOME keeps its secrets in files,
    never in the Keychain."""
    if sys.platform != "darwin" or not Path("/usr/bin/security").exists():
        return False
    try:
        import pwd
        return Path(os.path.expanduser("~")).resolve() == Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except (KeyError, OSError):
        return False


def vault_for(name, directory=None):
    """The vault for one secret ("provider-xai" ...): the Keychain on a Mac, else a 0600 file."""
    if on_mac() and os.environ.get("MOBSTER_VAULT") != "file":
        return KeychainVault(account=name)
    if directory is None:
        from ..paths import user_data_dir
        directory = user_data_dir() / "ai-plans"
    return FileVault(Path(directory) / (name.replace(":", "-") + ".json"))

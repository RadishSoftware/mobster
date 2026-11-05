"""The App API's tokens: one per client you let call it, created and revoked by you (``mobster api tokens``)."""

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import time
from pathlib import Path

FILE = "app-api-tokens.json"
LOCK = "app-api-tokens.lock"
PREFIX = "mob_"
TOKEN = re.compile(r"mob_[A-Za-z0-9_-]{43}")
ID = re.compile(r"[a-f0-9]{12}")
NAME = re.compile(r"[^\x00-\x1f\x7f]{1,40}")
MAX_TOKENS = 50
# A token's last use is written at most this often (a read per request would rewrite the file each time).
USE_EVERY = 60


class TokenError(ValueError):
    """A name or id the store can't take, or a store it can't trust: one plain sentence."""


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def public(record):
    """A token as `mobster api tokens list` shows it: never its hash."""
    return {key: record.get(key) for key in ("id", "name", "start", "createdAt", "lastUsedAt")}


class TokenStore:
    def __init__(self, data_dir, clock=time.time):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / FILE
        self.lock_path = self.data_dir / LOCK
        self.clock = clock
        self._used = {}

    def trusted(self):
        """Whether the file is one only this user could have written (a missing file is: it holds no token). An
        untrusted file refuses every token, so a file someone else could edit never lets a request in."""
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022

    def _read(self):
        if not self.trusted():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        items = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return None
        return [t for t in items if isinstance(t, dict) and isinstance(t.get("id"), str) and ID.fullmatch(t["id"])
                and isinstance(t.get("hash"), str) and len(t["hash"]) == 64 and isinstance(t.get("name"), str)]

    def all(self):
        """Every token, as ``public`` shows it. TokenError when the file can't be trusted or read."""
        tokens = self._read()
        if tokens is None:
            raise TokenError(f"Mobster can't trust {self.path}: only you may write it. Remove it, then make your "
                             "tokens again with mobster api tokens create.")
        return [public(t) for t in tokens]

    @contextlib.contextmanager
    def _edit(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            # Read, change and write under one lock, so two Mobster processes never lose each other's changes.
            fcntl.flock(fd, fcntl.LOCK_EX)
            tokens = self._read()
            if tokens is None:
                raise TokenError(f"Mobster can't trust {self.path}: only you may write it. Remove it, then make "
                                 "your tokens again with mobster api tokens create.")
            yield tokens
            handle, temporary = tempfile.mkstemp(dir=self.data_dir, prefix=".app-api-tokens.")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as out:
                    os.fchmod(out.fileno(), 0o600)
                    json.dump({"version": 1, "tokens": tokens}, out, indent=1)
                os.replace(temporary, self.path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                raise
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def create(self, name):
        """A new token for the client ``name``: (its public record, the token). The token is never kept."""
        name = " ".join(str(name or "").split())
        if not NAME.fullmatch(name):
            raise TokenError("A token's name is 1 to 40 characters, such as \"n8n\" or \"my script\".")
        token = PREFIX + secrets.token_urlsafe(32)
        with self._edit() as tokens:
            if any(t["name"].casefold() == name.casefold() for t in tokens):
                raise TokenError(f"A token is already named “{name}”. Revoke it first, or pick another name.")
            if len(tokens) >= MAX_TOKENS:
                raise TokenError(f"You have {MAX_TOKENS} tokens. Revoke one you no longer use first.")
            ids = {t["id"] for t in tokens}
            ident = secrets.token_hex(6)
            while ident in ids:
                ident = secrets.token_hex(6)
            record = {"id": ident, "name": name, "start": token[:8], "hash": _digest(token),
                      "createdAt": int(self.clock()), "lastUsedAt": None}
            tokens.append(record)
        return public(record), token

    def revoke(self, which):
        """Remove the token ``which`` names (its id or its name): its public record. TokenError when none does."""
        which = " ".join(str(which or "").split())
        with self._edit() as tokens:
            found = [t for t in tokens if t["id"] == which or t["name"].casefold() == which.casefold()]
            if not found:
                raise TokenError(f"No token is named “{which[:40]}”. mobster api tokens list shows them.")
            tokens[:] = [t for t in tokens if t is not found[0]]
        self._used.pop(found[0]["id"], None)
        return public(found[0])

    def verify(self, token):
        """The public record of the token ``token`` is, or None: a malformed token, an unknown or revoked one, or a file
        Mobster can't trust."""
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        tokens = self._read()
        if not tokens:
            return None
        digest = _digest(token).encode()
        found = None
        for record in tokens:
            if hmac.compare_digest(digest, record["hash"].encode()):
                found = record
        if found is not None:
            self._note_use(found["id"])
        return public(found) if found else None

    def _note_use(self, ident):
        now = self.clock()
        if now - self._used.get(ident, 0) < USE_EVERY:
            return
        self._used[ident] = now
        try:
            with self._edit() as tokens:
                for record in tokens:
                    if record["id"] == ident:
                        record["lastUsedAt"] = int(now)
        except (OSError, TokenError):
            pass  # the last use is a note: a full disk never refuses a request

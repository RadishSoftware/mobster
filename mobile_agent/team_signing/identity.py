"""The signing key and certificate on this Mac, in a Keychain only Mobster opens.

Mobster makes the key pair here and sends Apple only a certificate signing request (CSR). The private key is made in
a private temporary folder, imported into a keychain file of Mobster's own (``mobster-signing.keychain-db`` in the
signing folder, not on the login keychain's search list), and the temporary file is deleted. That keychain's password is
random and kept in the login Keychain (keychain_acl, readable only by Mobster's own programs); it is never put on a
command line. Apple's certificate comes back as a public file, and ``security import`` pairs it with the key.

``run`` is the one place a command is executed, so a test records the commands and needs no Keychain.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..devkit import DevError

SECURITY = "/usr/bin/security"
OPENSSL = "/usr/bin/openssl"
KEYCHAIN_FILE = "mobster-signing.keychain-db"
SERVICE = "Mobster Team Signing"
ACCOUNT = "keychain"
SUBJECT = "/CN=Mobster helper signing"
KEY_BITS = 2048
HEX40 = re.compile(r"[0-9A-F]{40}")


def _run(argv, stdin=None, timeout=60):
    try:
        done = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        return 127, "", type(error).__name__
    return done.returncode, done.stdout, done.stderr


def _store():
    from ..keychain_acl import Store

    return Store(SERVICE)


class Identity:
    """The key pair and certificate of this Mac's signing for one folder (``team_signing.state_dir()``)."""

    def __init__(self, folder, *, store=None, run=_run):
        self.folder, self.run = Path(folder), run
        self._store = store
        self.keychain = self.folder / KEYCHAIN_FILE
        self.state_path = self.folder / "identity.json"

    # -- what is on disk ------------------------------------------------------------------------------------

    def state(self):
        try:
            value = json.loads(self.state_path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, **changes):
        from .. import env_file

        merged = {**self.state(), **changes}  # read before the file is opened for writing, which empties it
        env_file.private_dir(self.folder)
        with env_file.open_private(self.state_path, "w") as stream:
            stream.write(json.dumps(merged, indent=1))

    # -- the keychain ---------------------------------------------------------------------------------------

    def _password(self, create=False):
        store = self._store if self._store is not None else _store()
        self._store = store
        stored = store.get(ACCOUNT)
        if stored is None and create:
            stored = secrets.token_urlsafe(32)
            store.set(ACCOUNT, stored)
        return stored

    def _security(self, *commands):
        """Commands for security(1)'s interactive mode, fed on stdin: a password never reaches an argument list."""
        code, out, err = self.run([SECURITY, "-i"], "\n".join(commands) + "\n")
        if code != 0:
            raise DevError(
                "macOS's Keychain tool refused Mobster's signing keychain.",
                "Unlock your Mac's login Keychain and try again.",
            )
        return out

    def _search_list(self):
        code, out, _ = self.run([SECURITY, "list-keychains", "-d", "user"])
        return [line.strip().strip('"') for line in out.splitlines() if line.strip()] if code == 0 else []

    def ensure_keychain(self):
        """Create Mobster's signing keychain if it isn't there, and unlock it. It never stays on the search list."""
        from .. import env_file

        env_file.private_dir(self.folder)
        password = self._password(create=not self.keychain.exists())
        if password is None:
            raise DevError(
                "Mobster's signing keychain is missing its password, so it can't open.",
                "Remove the team-signing folder in Mobster's data folder to start again.",
            )
        quoted = _quote(password)
        if not self.keychain.exists():
            before = self._search_list()
            self._security(f"create-keychain -p {quoted} {_quote(str(self.keychain))}")
            if before and str(self.keychain) in " ".join(self._search_list()):
                self.run([SECURITY, "list-keychains", "-d", "user", "-s", *before])
            self._security(f"set-keychain-settings {_quote(str(self.keychain))}")  # no lock timeout
            self._save(intermediate=None, pending_key=None, certificate_sha1=None)
        self._security(f"unlock-keychain -p {quoted} {_quote(str(self.keychain))}")
        if not self.state().get("intermediate"):
            self._import_intermediate()
        return password

    def _import_intermediate(self):
        """Apple's WWDR G3 certificate, so codesign can build the chain on a Mac without Xcode (apple_ca.py)."""
        from .. import env_file
        from . import apple_ca

        certificate = self.folder / "AppleWWDRCAG3.cer"
        with env_file.open_private(certificate, "wb") as stream:
            stream.write(apple_ca.wwdr_g3())
        try:
            self._security(f"import {_quote(str(certificate))} -k {_quote(str(self.keychain))}")
        finally:
            certificate.unlink(missing_ok=True)
        self._save(intermediate=apple_ca.SHA256)

    # -- the request ----------------------------------------------------------------------------------------

    def request(self):
        """The CSR (PEM) for the key in the signing keychain; a pending one is returned again, so the same card
        shows the same request. The private key is made here and never leaves this Mac."""
        pending = self.folder / "pending.csr"
        if pending.exists() and self.state().get("pending_key") == "imported":
            return pending.read_text()
        password = self.ensure_keychain()
        workspace = Path(tempfile.mkdtemp(prefix="mobster-csr-"))
        try:
            os.chmod(workspace, 0o700)
            key, csr = workspace / "key.pem", workspace / "request.csr"
            key.touch(mode=0o600)  # openssl keeps this mode when it writes the key into the file
            code, _, _ = self.run(
                [
                    OPENSSL,
                    "req",
                    "-new",
                    "-newkey",
                    f"rsa:{KEY_BITS}",
                    "-nodes",
                    "-keyout",
                    str(key),
                    "-out",
                    str(csr),
                    "-subj",
                    SUBJECT,
                ]
            )
            if code != 0 or not key.exists() or not csr.exists():
                raise DevError(
                    "Mobster couldn't make a certificate signing request on this Mac.",
                    "Check that /usr/bin/openssl runs.",
                )
            self._security(f"import {_quote(str(key))} -k {_quote(str(self.keychain))} -x -T /usr/bin/codesign")
            self._security(
                f"set-key-partition-list -S apple-tool:,apple:,codesign: -s -k {_quote(password)} "
                f"{_quote(str(self.keychain))}"
            )
            text = csr.read_text()
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        from .. import env_file

        with env_file.open_private(pending, "w") as stream:
            stream.write(text)
        self._save(pending_key="imported")
        return text

    # -- the certificate ------------------------------------------------------------------------------------

    def add_certificate(self, certificate_content, serial=None):
        """Keep the certificate Apple made (``certificateContent``, base64 DER) beside its key. Returns
        {sha1, serial}. DevError when it isn't a certificate or the key isn't here."""
        try:
            der = base64.b64decode(certificate_content, validate=True)
        except (ValueError, TypeError):
            raise DevError("Apple's certificate wasn't readable.") from None
        if len(der) < 64 or der[:1] != b"\x30":
            raise DevError("Apple's certificate wasn't readable.")
        sha1 = hashlib.sha1(der).hexdigest().upper()
        self.ensure_keychain()
        from .. import env_file

        env_file.private_dir(self.folder)
        certificate = self.folder / "certificate.cer"
        with env_file.open_private(certificate, "wb") as stream:
            stream.write(der)
        self._security(f"import {_quote(str(certificate))} -k {_quote(str(self.keychain))} -T /usr/bin/codesign")
        if sha1 not in self.identities():
            raise DevError(
                "Apple's certificate doesn't match the key on this Mac.",
                "Remove the team-signing folder in Mobster's data folder and start again.",
            )
        (self.folder / "pending.csr").unlink(missing_ok=True)
        self._save(pending_key=None, certificate_sha1=sha1, certificate_serial=serial)
        return {"sha1": sha1, "serial": serial}

    def identities(self):
        """SHA-1s of the code-signing identities (a certificate with its key) in the signing keychain."""
        if not self.keychain.exists():
            return []
        self.ensure_keychain()
        code, out, _ = self.run([SECURITY, "find-identity", "-p", "codesigning", str(self.keychain)])
        return [match for match in re.findall(r"\b([0-9A-F]{40})\b", out)] if code == 0 else []

    def signing_identity(self):
        """{sha1, serial} of the certificate Mobster made here, while its key is still in the keychain; else None."""
        state = self.state()
        sha1 = state.get("certificate_sha1")
        if sha1 and HEX40.fullmatch(sha1) and sha1 in self.identities():
            return {"sha1": sha1, "serial": state.get("certificate_serial")}
        return None

    def forget(self):
        """Delete the signing keychain and its password, e.g. after the test certificate is revoked."""
        password_store = self._store if self._store is not None else _store()
        if self.keychain.exists():
            self.run([SECURITY, "delete-keychain", str(self.keychain)])
        for name in ("pending.csr", "certificate.cer", "identity.json"):
            (self.folder / name).unlink(missing_ok=True)
        try:
            password_store.delete(ACCOUNT)
        except Exception:  # noqa: BLE001 -- nothing stored is fine
            pass


def _quote(value):
    """One word for security(1)'s interactive parser: double-quoted, with \\ and " escaped."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

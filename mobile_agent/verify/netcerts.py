"""A throwaway certificate authority for one run: the proxy answers a stubbed https host with a certificate this signs.

The private key lives in a 0700 folder and is deleted when the run ends, so the public certificate that stays in the
simulator's trust store can sign nothing."""

import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path

OPENSSL = "/usr/bin/openssl"   # LibreSSL ships with macOS; a Homebrew openssl on PATH is not assumed
DAYS = "2"
TIMEOUT = 30


class CertError(Exception):
    pass


class Authority:
    def __init__(self, run=subprocess.run):
        self._run = run
        self.dir = Path(tempfile.mkdtemp(prefix="mobster-ca-"))
        self.dir.chmod(0o700)
        self.cert = self.dir / "ca.pem"
        self._key = self.dir / "ca.key"
        self._contexts = {}
        # LibreSSL writes explicit curve parameters unless it is handed a named one.
        self._curve = self.dir / "curve.pem"
        self._openssl("ecparam", "-name", "prime256v1", "-out", self._curve)
        self._openssl("req", "-x509", "-newkey", f"ec:{self._curve}", "-nodes",
                      "-keyout", self._key, "-out", self.cert, "-sha256", "-days", DAYS,
                      "-subj", "/CN=Mobster check CA", "-addext", "basicConstraints=critical,CA:TRUE",
                      "-addext", "keyUsage=critical,keyCertSign", "-addext", "subjectKeyIdentifier=hash")

    def _openssl(self, *args):
        try:
            done = self._run([OPENSSL, *map(str, args)], capture_output=True, text=True, timeout=TIMEOUT,
                             stdin=subprocess.DEVNULL, cwd=self.dir)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CertError(f"openssl didn't run ({type(error).__name__}).") from None
        if done.returncode != 0:
            last = ((done.stderr or "").strip().splitlines() or ["failed"])[-1]
            raise CertError("openssl couldn't make a certificate: " + last)

    def context(self, host):
        """A server-side SSL context for ``host``, with a certificate this authority signed."""
        if host not in self._contexts:
            stem = self.dir / f"leaf-{len(self._contexts)}"
            extensions = stem.with_suffix(".ext")
            # The key identifiers: strict verification (Python 3.13 and later by default) refuses a leaf without them.
            extensions.write_text(f"subjectAltName=DNS:{host}\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n"
                                  "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n")
            self._openssl("req", "-new", "-newkey", f"ec:{self._curve}", "-nodes",
                          "-keyout", stem.with_suffix(".key"), "-out", stem.with_suffix(".csr"), "-subj", f"/CN={host}")
            self._openssl("x509", "-req", "-in", stem.with_suffix(".csr"), "-CA", self.cert, "-CAkey", self._key,
                          "-CAcreateserial", "-out", stem.with_suffix(".pem"), "-sha256", "-days", DAYS,
                          "-extfile", extensions)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(stem.with_suffix(".pem"), stem.with_suffix(".key"))
            context.set_alpn_protocols(["http/1.1"])
            self._contexts[host] = context
        return self._contexts[host]

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

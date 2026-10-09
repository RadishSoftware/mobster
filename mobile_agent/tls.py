"""Where HTTPS certificates are verified from.

Python checks a server against the CA file its OpenSSL was built to read. The Mac app's
agent is frozen from a Python built on the CI runner, and that file does not exist on a
customer's Mac: every model call failed certificate verification ("unable to get local
issuer certificate", 27 Sep 2026). A python.org install has the same gap until its
"Install Certificates" step runs.

macOS keeps its own CA bundle at /etc/ssl/cert.pem and updates it with the OS. The frozen
app always verifies against it; a Python install uses it only when its own file is
missing. SSL_CERT_FILE or SSL_CERT_DIR set by the user always win.
"""

import os
import ssl
import sys

SYSTEM_BUNDLE = "/etc/ssl/cert.pem"


def _own_paths_exist():
    paths = ssl.get_default_verify_paths()
    if paths.cafile and os.path.isfile(paths.cafile):
        return True
    return bool(paths.capath and os.path.isdir(paths.capath) and os.listdir(paths.capath))


def ensure_ca_bundle(environ=None, frozen=None, bundle=SYSTEM_BUNDLE):
    """Point OpenSSL at macOS's bundle when this Python's own can't be trusted to exist.

    Sets SSL_CERT_FILE in ``environ`` (default os.environ), which every TLS context made
    afterwards reads, and returns the path it set, or None when it left things alone.
    """
    environ = os.environ if environ is None else environ
    if environ.get("SSL_CERT_FILE") or environ.get("SSL_CERT_DIR"):
        return None
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen and _own_paths_exist():
        return None
    if not os.path.isfile(bundle):
        return None
    environ["SSL_CERT_FILE"] = bundle
    return bundle


def report():
    """For `mobster version --json`: the CA file TLS verifies against and how many CAs load from it."""
    context = ssl.create_default_context()
    return {"caFile": ssl.get_default_verify_paths().cafile,
            "caCerts": context.cert_store_stats().get("x509_ca", 0)}

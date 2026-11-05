"""Where HTTPS certificates are verified from: macOS's bundle for the frozen app or a Python missing its own."""

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
    """Point OpenSSL at macOS's bundle when this Python's own can't be trusted to exist."""
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


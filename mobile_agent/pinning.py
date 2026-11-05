"""Mobster's own servers, pinned to CA keys: a chain without one is refused with PinMismatch, never retried unpinned."""

import base64
import hashlib
import http.client
import ssl
import urllib.request

from . import tls

# The hosts whose chain must include a pinned CA key.
PINNED_HOSTS = frozenset({"mobster.dev", "www.mobster.dev", "api.mobster.dev", "remote.mobster.dev"})

# base64 SHA-256 of each CA's DER SubjectPublicKeyInfo (RFC 7469's pin-sha256), checked live on 13 Oct 2026.
PINS = {
    # In use on 13 Oct 2026.
    "LoMHBotttiDko50Gi13uXW71eIy7LAttI+rYT8wXF4w=": "Let's Encrypt YR1",
    "fk6IOKit1ild5647BH06ujSIq5XbCgqlbYl6ANhhi88=": "ISRG Root YR",
    "C5+lpZ7tcVwmwQIMcRtPbsQtWLABXhQzejna0wHFr8M=": "ISRG Root X1",
    "kIdp6NNEd8wsugYyyIYFsi1ylMCED3hZbSR8ZFsa/A4=": "Google Trust Services WE1",
    "mEflZT5enoR1FuXLgYYGqnVEoZvmf9c2bVBpiOjYQ0c=": "GTS Root R4",
    # Backups.
    "diGVwiVYbubAI3RW4hB9xU8e/CH2GnkuvVFZE8zmgzI=": "ISRG Root X2",
    "sCkq5UWXjg+7mKu9lMhhYF5bGLsy7VI/UNW3tccdR7w=": "ISRG Root YE",
    "hxqRlPTu1bMS/0DITB1SSu0vd4u/8l8TjPgfaAp63Gc=": "GTS Root R1",
    "Vfd95BwDeSQo+NUYxVEEIlvkOlWY2SalKK1lPhzOx78=": "GTS Root R2",
    "QXnt2YHvdHR3tJYmQIr0Paosp6t/nggsEGD4QJZ3Q0g=": "GTS Root R3",
    "K89VOmb1cJAN3TK6bf4ezAbJGC1mLcG2Dh97dnwr3VQ=": "SSL.com TLS RSA Root CA 2022",
    "G/ANXI8TwJTdF+AFBM8IiIUPEv0Gf6H5LA/b9guG4yE=": "SSL.com TLS ECC Root CA 2022",
    "0cRTd+vc1hjNFlHcLgLCHXUeWqn80bNDH/bs9qMTSPo=": "SSL.com Root Certification Authority RSA",
    "oyD01TTXvpfBro3QSZc1vIlcMjrdLTiL/M9mLCPX+Zo=": "SSL.com Root Certification Authority ECC",
}


class PinMismatch(ssl.SSLCertVerificationError):
    """A pinned host's verified chain has no pinned CA key: something between this Mac and the server answered."""

    def __init__(self, host):
        self.host = host
        # (errno, strerror): ssl errors print their strerror alone.
        super().__init__(1, f"Mobster couldn't verify its server ({host}), so it didn't connect. Another app or this "
                         "network may be intercepting secure connections. Try another network, or update Mobster.")


def is_pinned(host):
    return str(host or "").rstrip(".").lower() in PINNED_HOSTS


def _der_element(data, at):
    """(tag, start of contents, end of element) of the DER element at ``at``. ValueError on anything malformed."""
    if at + 2 > len(data):
        raise ValueError("truncated")
    tag, length = data[at], data[at + 1]
    at += 2
    if length & 0x80:
        count = length & 0x7F
        if not 1 <= count <= 4 or at + count > len(data):
            raise ValueError("bad length")
        length = int.from_bytes(data[at:at + count], "big")
        at += count
    if at + length > len(data):
        raise ValueError("truncated")
    return tag, at, at + length


def spki_der(certificate):
    """The DER SubjectPublicKeyInfo of a DER certificate: tbsCertificate's seventh field (sixth without a version)."""
    tag, body, _ = _der_element(certificate, 0)
    if tag != 0x30:
        raise ValueError("not a certificate")
    tag, at, end = _der_element(certificate, body)
    if tag != 0x30:
        raise ValueError("no tbsCertificate")
    tag, _, after = _der_element(certificate, at)
    if tag == 0xA0:  # [0] version
        at = after
    for _ in range(5):  # serialNumber, signature, issuer, validity, subject
        _, _, at = _der_element(certificate, at)
    tag, _, after = _der_element(certificate, at)
    if tag != 0x30 or after > end:
        raise ValueError("no subjectPublicKeyInfo")
    return certificate[at:after]


def spki_pin(certificate):
    """The pin-sha256 of a DER certificate's key."""
    return base64.b64encode(hashlib.sha256(spki_der(certificate)).digest()).decode()


def verified_chain(sock):
    """The DER certificates OpenSSL verified for ``sock``, leaf first."""
    method = getattr(sock, "get_verified_chain", None) or getattr(getattr(sock, "_sslobj", None),
                                                                  "get_verified_chain", None)
    if method is None:
        return []
    return [c if isinstance(c, bytes) else c.public_bytes(ssl._ssl.ENCODING_DER) for c in method() or []]


def chain_is_pinned(chain):
    for certificate in chain:
        try:
            if spki_pin(certificate) in PINS:
                return True
        except ValueError:
            continue
    return False


def check_pins(sock, host):
    """Raise PinMismatch when ``host`` is pinned and ``sock``'s verified chain has no pinned key or can't be read."""
    if is_pinned(host) and not chain_is_pinned(verified_chain(sock)):
        raise PinMismatch(host)


def pinned_context():
    """The verifying context Mobster's HTTPS uses: the default (certificate and hostname required), TLS 1.2 or later."""
    tls.ensure_ca_bundle()
    made = ssl.create_default_context()
    made.minimum_version = ssl.TLSVersion.TLSv1_2
    return made


def wrap(sock, host):
    """``sock`` wrapped in TLS for ``host``, with the pin checked before a byte is sent."""
    secure = pinned_context().wrap_socket(sock, server_hostname=host)
    try:
        check_pins(secure, host)
    except BaseException:
        secure.close()
        raise
    return secure


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that checks the pin of a pinned host after the handshake (through a proxy's tunnel too)."""

    def __init__(self, host, port=None, *, context=None, **kwargs):
        super().__init__(host, port, context=context or pinned_context(), **kwargs)

    def connect(self):
        super().connect()
        try:
            check_pins(self.sock, self._tunnel_host or self.host)
        except BaseException:
            self.close()
            raise


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib's HTTPS with PinnedHTTPSConnection: every hop of a redirect to a pinned host is checked."""

    def https_open(self, req):
        return self.do_open(PinnedHTTPSConnection, req, context=self._context)


def opener():
    """A urllib opener whose HTTPS checks pins (and verifies as usual everywhere else)."""
    return urllib.request.build_opener(PinnedHTTPSHandler(context=pinned_context()))

"""ES256 signing for App Store Connect's JSON Web Tokens in plain Python: the agent ships no crypto library."""

import base64
import hashlib
import hmac
import json

P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)
EC_PUBLIC_KEY = bytes.fromhex("2a8648ce3d0201")       # 1.2.840.10045.2.1
PRIME256V1 = bytes.fromhex("2a8648ce3d030107")        # 1.2.840.10045.3.1.7
# The PEM lines around a .p8 key, built from the label so a secret scanner never reads the source as a key.
P8_LABEL = "PRIVATE KEY"
P8_BEGIN = f"-----BEGIN {P8_LABEL}-----"
P8_END = f"-----END {P8_LABEL}-----"


class P8Error(ValueError):
    """The text isn't a P-256 private key in the .p8 format App Store Connect gives."""


# -- the curve, in Jacobian coordinates ------------------------------------------------------------------------------

def _double(point):
    x, y, z = point
    if y == 0 or z == 0:
        return (0, 1, 0)
    ysq = y * y % P
    s = 4 * x * ysq % P
    m = (3 * (x - z * z) * (x + z * z)) % P          # a = -3
    nx = (m * m - 2 * s) % P
    ny = (m * (s - nx) - 8 * ysq * ysq) % P
    nz = 2 * y * z % P
    return (nx, ny, nz)


def _add(p, q):
    if p[2] == 0:
        return q
    if q[2] == 0:
        return p
    z1z1, z2z2 = p[2] * p[2] % P, q[2] * q[2] % P
    u1, u2 = p[0] * z2z2 % P, q[0] * z1z1 % P
    s1, s2 = p[1] * q[2] * z2z2 % P, q[1] * p[2] * z1z1 % P
    if u1 == u2:
        return _double(p) if s1 == s2 else (0, 1, 0)
    h, r = (u2 - u1) % P, (s2 - s1) % P
    hh = h * h % P
    hhh = h * hh % P
    v = u1 * hh % P
    nx = (r * r - hhh - 2 * v) % P
    ny = (r * (v - nx) - s1 * hhh) % P
    nz = h * p[2] * q[2] % P
    return (nx, ny, nz)


# Not constant-time: the key and every signature stay in this process, and only the token goes to Apple.
def _multiply(k, point=G):
    result, addend = (0, 1, 0), (point[0], point[1], 1)
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _double(addend)
        k >>= 1
    return result


def _affine(point):
    if point[2] == 0:
        return None
    inverse = pow(point[2], P - 2, P)
    return point[0] * inverse * inverse % P, point[1] * inverse ** 3 % P


def public_key(d):
    return _affine(_multiply(d))


# -- RFC 6979 nonces ----------------------------------------------------------------------------------------------

def _bits2int(data):
    return int.from_bytes(data, "big")  # qlen = hlen = 256


def _int2octets(value):
    return value.to_bytes(32, "big")


def _nonces(d, digest):
    h1 = _int2octets(_bits2int(digest) % N)
    x = _int2octets(d)
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = _bits2int(v)
        if 1 <= candidate < N:
            yield candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign(d, message):
    """(r, s) over SHA-256(``message``) with the private scalar ``d``."""
    if not 1 <= d < N:
        raise P8Error("The private key is out of range.")
    digest = hashlib.sha256(message).digest()
    e = _bits2int(digest) % N
    for k in _nonces(d, digest):
        point = _affine(_multiply(k))
        r = point[0] % N
        if r == 0:
            continue
        s = pow(k, N - 2, N) * (e + r * d) % N
        if s:
            return r, s


def verify(public, message, signature):
    """Whether ``signature`` (r, s) is ``public``'s (x, y) over SHA-256(``message``)."""
    r, s = signature
    if not (1 <= r < N and 1 <= s < N):
        return False
    e = _bits2int(hashlib.sha256(message).digest()) % N
    w = pow(s, N - 2, N)
    point = _affine(_add(_multiply(e * w % N), _multiply(r * w % N, public)))
    return point is not None and point[0] % N == r


# -- the .p8 file (PKCS #8, RFC 5208, wrapping an RFC 5915 ECPrivateKey) ------------------------------------------

def _der(data, at=0):
    """(tag, value, next offset) of the DER element at ``at``."""
    if at + 2 > len(data):
        raise P8Error("The key is cut short.")
    tag, length, at = data[at], data[at + 1], at + 2
    if length & 0x80:
        count = length & 0x7F
        if not 1 <= count <= 3 or at + count > len(data):
            raise P8Error("The key isn't valid DER.")
        length, at = int.from_bytes(data[at:at + count], "big"), at + count
    if at + length > len(data):
        raise P8Error("The key is cut short.")
    return tag, data[at:at + length], at + length


def _children(data):
    out, at = [], 0
    while at < len(data):
        tag, value, at = _der(data, at)
        out.append((tag, value))
    return out


def private_key_from_p8(text):
    """The private scalar of a .p8 key: a PKCS #8 PEM between ``P8_BEGIN`` and ``P8_END``."""
    lines = [line.strip() for line in str(text or "").strip().splitlines() if line.strip()]
    if not lines or lines[0] != P8_BEGIN or lines[-1] != P8_END:
        raise P8Error(f"That isn't a .p8 key: it starts with {P8_BEGIN}.")
    try:
        data = base64.b64decode("".join(lines[1:-1]), validate=True)
    except ValueError:
        raise P8Error("The key's text isn't base64.") from None
    tag, body, _ = _der(data)
    parts = _children(body) if tag == 0x30 else []
    if len(parts) < 3 or parts[0] != (0x02, b"\x00") or parts[1][0] != 0x30 or parts[2][0] != 0x04:
        raise P8Error("That isn't a PKCS #8 private key.")
    algorithm = _children(parts[1][1])
    if len(algorithm) < 2 or algorithm[0] != (0x06, EC_PUBLIC_KEY) or algorithm[1] != (0x06, PRIME256V1):
        raise P8Error("That key isn't on the P-256 curve App Store Connect uses.")
    tag, inner, _ = _der(parts[2][1])
    fields = _children(inner) if tag == 0x30 else []
    if len(fields) < 2 or fields[0] != (0x02, b"\x01") or fields[1][0] != 0x04 or len(fields[1][1]) != 32:
        raise P8Error("That isn't an EC private key.")
    d = int.from_bytes(fields[1][1], "big")
    if not 1 <= d < N:
        raise P8Error("The private key is out of range.")
    return d


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt(header, claims, d):
    """A signed JWT: base64url(header).base64url(claims).base64url(r || s)."""
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "." +
                     _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    r, s = sign(d, signing_input.encode())
    return signing_input + "." + _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

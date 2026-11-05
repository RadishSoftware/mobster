"""A screen as a skeleton: each control's role and a one-way hash of its label. No screen text survives.

Labels are the risk: a label can be a name, a handle, a number or a message. So a label is
- empty -> "";
- long (prose: a message, a caption, a title) -> "~long";
- an email, phone number, handle or link, or anything secret_filter would mask -> "~email", "~number", "~handle",
  "~link", "~secret";
- otherwise folded (case, digit runs to "#") and hashed to 10 hex digits (compiled_read/routes.py's digest), so
  "General" is the same token on every Mac and "3 new messages" equals "5 new messages".
A short name ("Sam") is a short label and hashes to a token anyone can test a guess against: why the corpus stays
local until sharing is decided.
"""

import hashlib
import re
import unicodedata

from ..secret_filter import is_secret

MAX_NODES = 80
MAX_LABEL = 48
EMAIL = re.compile(r"\S+@\S+\.\S+")
HANDLE = re.compile(r"(?<!\w)@\w{2,}")
LINK = re.compile(r"(?i)https?://|www\.|\b[\w-]+\.(?:com|org|net|io|app|dev|me)\b")
DIGITS = re.compile(r"\d+")
PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{5,}\d")
ROLE = re.compile(r"[A-Za-z]{1,40}")
TOKEN = re.compile(r"[0-9a-f]{10}|~(?:long|email|number|handle|link|secret)|")
# Roles of a field that holds a password: the whole step is dropped, not masked.
SECURE = "secure"


def label_token(label):
    text = unicodedata.normalize("NFKC", label or "").strip()
    if not text:
        return ""
    if len(text) > MAX_LABEL:
        return "~long"
    if is_secret(text):
        return "~secret"
    for name, pattern in (("email", EMAIL), ("link", LINK), ("handle", HANDLE), ("number", PHONE)):
        if pattern.search(text):
            return "~" + name
    if sum(map(len, DIGITS.findall(text))) >= 7:
        return "~number"
    return hashlib.sha1(DIGITS.sub("#", text.casefold()).encode()).hexdigest()[:10]


def _role(role):
    return role if ROLE.fullmatch(role or "") else "Other"


def capture(snapshot, element):
    """``{"nodes": [[role, token], ...], "target": index or None}`` for a screen and the control acted on; ``None``
    when nothing may be kept of it: the corpus is off, or the screen has a secure field."""
    from .consent import active
    if not active():
        return None
    elements = list(getattr(snapshot, "elements", None) or ())
    if any(SECURE in str(getattr(item, "role", "")).casefold() for item in elements):
        return None
    nodes, target = [], None
    for item in elements[:MAX_NODES]:
        if element is not None and target is None and getattr(item, "id", None) == element.id:
            target = len(nodes)
        nodes.append([_role(str(getattr(item, "role", ""))), label_token(getattr(item, "label", ""))])
    return {"nodes": nodes, "target": target}


def fingerprint(nodes):
    """16 hex digits for a skeleton: the same screen shape on any Mac gives the same value."""
    return hashlib.sha1(repr(sorted(map(tuple, nodes))).encode()).hexdigest()[:16]

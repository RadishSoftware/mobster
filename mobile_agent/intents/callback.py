"""x-callback-url: ask an app to hand control back to Mobster's own scheme, so the answer needs no screen read.

``with_callback(url, token)`` adds ``x-success``, ``x-error`` and ``x-cancel`` to a link that is already allowed by
``deeplinks.decide``; ``parse(url)`` reads what came back. Receiving the callback needs the runner to own the
``mobster-callback`` scheme; that part is pending a proof on a simulator. Mobster never opens a callback link
itself: it only reads one the runner reports.
"""

import re
from urllib.parse import parse_qsl, quote, urlsplit

from ..deeplinks import LinkError, decide

SCHEME = "mobster-callback"
TOKEN = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
STATUSES = ("success", "error", "cancel")


def with_callback(url, token):
    """``url`` with the three return addresses added. LinkError for a link Mobster never opens, a web page (it has no
    callback) or a bad token. The link keeps its decision: ``decide(result)`` asks exactly when ``decide(url)`` did."""
    if not TOKEN.match(str(token or "")):
        raise LinkError("A callback token is 6 to 64 letters, digits, - or _.")
    decision = decide(url)
    scheme = decision.url.partition(":")[0].casefold()
    if scheme in ("http", "https"):
        raise LinkError("A web page can't return to Mobster. Use an app's link.")
    base, _, query = decision.url.partition("?")
    returns = "&".join(f"x-{status}={quote(f'{SCHEME}://{token}/{status}', safe='')}" for status in STATUSES)
    return f"{base}?{query}&{returns}" if query else f"{base}?{returns}"


def parse(url):
    """{"token", "status", "params"} for a callback address, or None when it isn't one."""
    parts = urlsplit(str(url or "").strip())
    status = parts.path.strip("/")
    if parts.scheme.casefold() != SCHEME or status not in STATUSES or not TOKEN.match(parts.netloc):
        return None
    return {"token": parts.netloc, "status": status, "params": dict(parse_qsl(parts.query))}


def shortcut_run(name, token):
    """The x-callback link that runs a shortcut and returns. It acts, so it asks every time (``decide``)."""
    return with_callback("shortcuts://x-callback-url/run-shortcut?name=" + quote(str(name).strip(), safe=""), token)

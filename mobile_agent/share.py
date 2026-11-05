"""Share links (docs/results.mdx › Share a result): one task's result as a page anyone with the link can read, at
mobster.dev/s/<id>, until its owner turns it off or it expires."""

import http.client
import json
import os
import socket
import ssl
from urllib.parse import urlsplit

from . import artifacts, pinning
from .transport import user_agent

ACCOUNT_API = "https://api.mobster.dev"
TIMEOUT = 20.0
EXPIRY_DAYS = (1, 7, 30, 90)
MAX_PAGE_BYTES = 4_000_000


class ShareError(Exception):
    """The account service refused or couldn't be reached."""

    def __init__(self, code, status, message):
        super().__init__(message)
        self.code, self.status = code, status


def api_base():
    """The account service: MOBSTER_ACCOUNT_API (development: http on this Mac) or https://api.mobster.dev."""
    value = (os.environ.get("MOBSTER_ACCOUNT_API") or ACCOUNT_API).rstrip("/")
    parts = urlsplit(value)
    if parts.scheme == "https" or parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost"):
        return value
    return ACCOUNT_API


def page(run, *, frames=None, people="mask", screens=False, title=None):
    """The page as it's shared: see the module docstring."""
    return artifacts.document(run, frames=frames, people=people, screens=screens, share=True, title=title)


def call(method, path, token, body=None, timeout=TIMEOUT, limit=256_000):
    """(HTTP status, the JSON object answered or {}) of one request to the account service, ``token`` its Bearer, with
    ``body`` as JSON (bytes go as they are)."""
    base = api_base()
    parts = urlsplit(base)
    connection = (pinning.PinnedHTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout)
                  if parts.scheme == "https"
                  else http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout))
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": user_agent()}
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, f"{parts.path.rstrip('/')}{path}", body=payload, headers=headers)
        response = connection.getresponse()
        data = response.read(limit)
    except pinning.PinMismatch:
        raise ShareError("untrusted_server", 503, "Mobster couldn't verify its server, so it didn't connect.") from None
    except (OSError, socket.timeout, http.client.HTTPException, ssl.SSLError):
        raise ShareError("unreachable", 503, "Mobster couldn't reach its account service. Check your connection and "
                         "try again.") from None
    finally:
        headers = None  # noqa: F841 -- the token's only other reference goes with this frame
        connection.close()
    try:
        value = json.loads(data) if data else {}
    except ValueError:
        value = {}
    return response.status, value if isinstance(value, dict) else {}


def _request(method, path, token, body=None, timeout=TIMEOUT):
    status, value = call(method, path, token, body, timeout)
    if 200 <= status < 300:
        return value
    message = value.get("message") if isinstance(value.get("message"), str) else None
    code = value.get("error") if isinstance(value.get("error"), str) else "error"
    if status == 401:
        raise ShareError("session", 401, "Sign in to Mobster again to share.")
    if status in (400, 403, 404, 409, 413):
        raise ShareError(code, status, message or "Mobster's account service refused that link.")
    raise ShareError("error", 502,
                     message or "Mobster's account service couldn't make the link. Try again in a minute.")


def create(token, *, title, html, expires_days=30):
    """The new link: {id, url, title, createdAt, expiresAt, live, bytes}."""
    if expires_days is not None and expires_days not in EXPIRY_DAYS:
        raise ValueError("expiresDays is 1, 7, 30, 90 or null")
    if len(html.encode("utf-8")) > MAX_PAGE_BYTES:
        raise ShareError("too_large", 413, "That page is over 4 MB. Leave out the screens, or share fewer cards.")
    value = _request("POST", "/api/shares", token, {"title": title, "html": html, "expiresDays": expires_days})
    share = value.get("share")
    if not isinstance(share, dict) or not isinstance(share.get("id"), str) or not isinstance(share.get("url"), str):
        raise ShareError("error", 502, "Mobster's account service answered without a link. Try again.")
    return share


def revoke(token, share_id):
    """Turns a link off; its page is deleted at once. The link as it is now."""
    value = _request("DELETE", f"/api/shares/{share_id}", token)
    return value.get("share") if isinstance(value.get("share"), dict) else {"id": share_id, "live": False}

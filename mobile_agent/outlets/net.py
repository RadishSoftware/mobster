"""One HTTP request for the destinations that talk to a web service (webhook, Discord, Sheets, Notion), with the
standard library only, as the frozen app ships it."""

import http.client
import json
import socket
import ssl
from urllib.parse import urlsplit

TIMEOUT = 10.0
READ_LIMIT = 65_536


class Reply:
    def __init__(self, status, location=None, body=b"", retry_after=None):
        self.status, self.location, self.body, self.retry_after = status, location, body, retry_after

    def json_object(self):
        """The body as a JSON object, or None when it isn't one."""
        try:
            value = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def __repr__(self):
        return f"Reply({self.status}, location={self.location!r}, {len(self.body)} bytes)"


class NetError(Exception):
    """No answer. ``unsent``: the request never left this Mac. ``message`` names the host, never the address."""

    def __init__(self, message, unsent, retry=True):
        super().__init__(message)
        self.message, self.unsent, self.retry = message, unsent, retry


def host_of(url):
    return (urlsplit(url).hostname or "").lower()


def connection(url, timeout):
    parts = urlsplit(url)
    if parts.scheme.lower() == "https":
        return http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout,
                                           context=ssl.create_default_context())
    return http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)


def call(method, url, data=None, headers=None, timeout=TIMEOUT, connect=connection, read_limit=READ_LIMIT):
    """``method`` ``url`` once: a Reply, or NetError. Redirects are answers, never followed. NetError says whether the
    request surely never left this Mac (``unsent``) or may have arrived."""
    host = host_of(url)
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    try:
        link = connect(url, timeout)
    except Exception:  # noqa: BLE001
        raise NetError("Mobster couldn't open that address.", unsent=True, retry=False) from None
    try:
        # Connecting first, on its own, tells "never sent" from "sent, then no answer".
        try:
            link.connect()
        except ssl.SSLCertVerificationError:
            raise NetError(f"{host}'s certificate couldn't be verified.", unsent=True, retry=False) from None
        except (socket.timeout, TimeoutError):
            raise NetError(f"{host} didn't answer within {int(timeout)} seconds.", unsent=True) from None
        except (OSError, http.client.HTTPException):
            raise NetError(f"{host} couldn't be reached.", unsent=True) from None
        try:
            link.request(method, path, body=data, headers=headers or {})
            response = link.getresponse()
            body = response.read(read_limit)
            status = int(response.status)
            location = response.getheader("Location")
            retry_after = response.getheader("Retry-After")
        except (socket.timeout, TimeoutError):
            raise NetError(f"{host} didn't answer within {int(timeout)} seconds.", unsent=False) from None
        except (OSError, http.client.HTTPException):
            raise NetError(f"{host} couldn't be reached.", unsent=False) from None
    finally:
        try:
            link.close()
        except Exception:  # noqa: BLE001
            pass
    return Reply(status, location, body, retry_after)

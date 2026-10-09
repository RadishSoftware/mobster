"""Icons for the phone's own apps, from Apple's public App Store lookup.

The catalog's icons ship with the dashboard. An app found on the phone has none,
so GET /api/apps/icon asks Apple's lookup service for it: the request carries
only the bundle ID (``https://itunes.apple.com/lookup?bundleId=<id>``), then
the artwork is downloaded from Apple's image CDN. Nothing about the phone, the
user or their tasks is sent.

Icons are kept in the agent's data folder, so each app is looked up once. An app
the store does not know (in-house, TestFlight, removed from the store) is
remembered too, and not asked about again for a week. A failure that may be
temporary (offline, rate limited) is not remembered.
"""

import hashlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import quote, urlsplit
import urllib.error
import urllib.request

from .state import validate_bundle_id

LOOKUP_URL = "https://itunes.apple.com/lookup?bundleId="
LOOKUP_LIMIT = 512_000
IMAGE_LIMIT = 1_000_000
TIMEOUT = 8
MISS_SECONDS = 7 * 86_400
# Apple rate-limits the lookup service (roughly 20 requests a minute): after a refusal, wait.
BACKOFF_SECONDS = 60.0
# Hosts the lookup and its artwork are served from; a redirect anywhere else is refused.
APPLE_HOSTS = ("itunes.apple.com",)
ARTWORK_SUFFIX = ".mzstatic.com"
PNG, JPEG = b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"


class IconUnavailable(RuntimeError):
    """The icon could not be fetched right now (offline, rate limited); try again later."""


def _apple_url(url, artwork=False):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and not parts.username and not parts.password and (
        host.endswith(ARTWORK_SUFFIX) if artwork else host in APPLE_HOSTS or host.endswith(ARTWORK_SUFFIX))


class _AppleRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        if not _apple_url(url):
            raise urllib.error.HTTPError(url, code, "Redirect away from Apple refused", headers, response)
        return super().redirect_request(request, response, code, message, headers, url)


def image_type(data):
    """The MIME type of a PNG or JPEG, else None."""
    return "image/png" if data.startswith(PNG) else "image/jpeg" if data.startswith(JPEG) else None


class AppIcons:
    """A folder of icons keyed by bundle ID, filled from the App Store lookup on demand.

    ``allowed`` (optional) returns the bundle IDs that may be looked up: the apps
    the picker shows. A bundle outside it is answered from the cache or not at all.
    """

    def __init__(self, directory, opener=None, clock=time.time, allowed=None, concurrency=2):
        self.directory = Path(directory)
        self.open = opener or urllib.request.build_opener(_AppleRedirects).open
        self.clock, self.allowed = clock, allowed
        self.lock = threading.Lock()
        self.flights = {}
        self.gate = threading.BoundedSemaphore(concurrency)
        self.backoff_until = 0.0

    # -- cache -----------------------------------------------------------------------

    def _key(self, bundle):
        return hashlib.sha256(bundle.encode()).hexdigest()[:32]

    def _cached(self, bundle):
        key = self._key(bundle)
        for suffix, mime in ((".png", "image/png"), (".jpg", "image/jpeg")):
            try:
                data = (self.directory / (key + suffix)).read_bytes()
            except OSError:
                continue
            if image_type(data) == mime and len(data) <= IMAGE_LIMIT:
                return data, mime
        return None

    def _misses(self):
        try:
            value = json.loads((self.directory / "misses.json").read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _missed(self, bundle):
        stamp = self._misses().get(self._key(bundle))
        return isinstance(stamp, (int, float)) and self.clock() - stamp < MISS_SECONDS

    def _write(self, name, data):
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        temporary = self.directory / f".{name}.{threading.get_ident()}.tmp"
        temporary.write_bytes(data)
        os.replace(temporary, self.directory / name)

    def _remember_miss(self, bundle):
        with self.lock:
            now = self.clock()
            misses = {key: stamp for key, stamp in self._misses().items()
                      if isinstance(stamp, (int, float)) and now - stamp < MISS_SECONDS}
            misses[self._key(bundle)] = now
            self._write("misses.json", json.dumps(misses).encode())

    # -- lookup ----------------------------------------------------------------------

    def get(self, bundle):
        """(bytes, mime) of the app's icon, or None when there is none.

        Raises ValueError for an invalid bundle ID and IconUnavailable when the
        store could not be reached (not remembered as a miss).
        """
        bundle = validate_bundle_id(bundle)
        cached = self._cached(bundle)
        if cached or self._missed(bundle):
            return cached
        if self.allowed is not None and bundle not in self.allowed():
            return None
        # One lookup per bundle at a time: the picker and the Apps page ask together.
        with self.lock:
            flight = self.flights.get(bundle)
            leader = flight is None
            if leader:
                flight = self.flights[bundle] = threading.Event()
        if not leader:
            flight.wait(TIMEOUT * 2 + 1)
            cached = self._cached(bundle)
            if cached or self._missed(bundle):
                return cached
            raise IconUnavailable("The icon is not available right now")
        try:
            with self.gate:
                return self._fetch(bundle)
        finally:
            with self.lock:
                self.flights.pop(bundle, None)
            flight.set()

    def _read(self, url, limit):
        if self.clock() < self.backoff_until:
            raise IconUnavailable("The App Store asked Mobster to slow down")
        request = urllib.request.Request(url, headers={"User-Agent": "Mobster", "Accept": "*/*"})
        try:
            with self.open(request, timeout=TIMEOUT) as response:
                data = response.read(limit + 1)
                content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        except urllib.error.HTTPError as error:
            if error.code in (403, 429, 503):
                self.backoff_until = self.clock() + BACKOFF_SECONDS
            if error.code == 404:
                return None, None
            raise IconUnavailable(f"The App Store answered {error.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise IconUnavailable(f"The App Store could not be reached ({type(error).__name__})") from None
        if len(data) > limit:
            return None, None
        return data, content_type

    def _fetch(self, bundle):
        cached = self._cached(bundle)  # another request may have finished it meanwhile
        if cached:
            return cached
        body, _ = self._read(LOOKUP_URL + quote(bundle, safe=""), LOOKUP_LIMIT)
        artwork = None
        try:
            results = json.loads(body)["results"] if body else []
            match = next((item for item in results if isinstance(item, dict)
                          and str(item.get("bundleId", "")).lower() == bundle.lower()), None)
            if match:
                artwork = next((url for url in (match.get("artworkUrl100"), match.get("artworkUrl512"))
                                if isinstance(url, str) and _apple_url(url, artwork=True)), None)
        except (ValueError, KeyError, TypeError):
            artwork = None
        if not artwork:
            self._remember_miss(bundle)
            return None
        data, content_type = self._read(artwork, IMAGE_LIMIT)
        mime = image_type(data or b"")
        if not mime or content_type not in {"image/png", "image/jpeg", "image/jpg", ""}:
            self._remember_miss(bundle)
            return None
        self._write(self._key(bundle) + (".png" if mime == "image/png" else ".jpg"), data)
        return data, mime

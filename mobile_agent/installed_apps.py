"""The apps installed on the USB iPhone, read with ideviceinstaller.

WebDriverAgent can launch any app by bundle ID, so the app picker offers every
user app on the phone, not only the built-in catalog. This module only ever
runs ``ideviceinstaller list``: it never installs, uninstalls or upgrades.

A listing takes a second or two over USB, so it is cached (about a minute) and
refreshed in the background: GET /api/apps is polled every few seconds and must
never wait on the phone. When the tool, the phone or the pairing is missing the
inventory is unknown (``None``) and the reason is logged once.
"""

from dataclasses import dataclass
import logging
import plistlib
import re
import subprocess
import threading
import time

from .state import validate_bundle_id

log = logging.getLogger("mobster.apps")

CACHE_SECONDS = 60.0
# A failed listing (phone just plugged in, not yet trusted) is retried sooner.
RETRY_SECONDS = 10.0
# A listing that fails keeps the last good one this long (the phone is still there, the call hiccupped).
STALE_SECONDS = 600.0
LIST_TIMEOUT = 20.0
# The first read waits this long for a listing; later reads never wait.
FIRST_WAIT = 4.0
MAX_OUTPUT = 16_000_000
MAX_APPS = 3000
NAME_LIMIT = 60
UDID_PATTERN = re.compile(r"[0-9A-Fa-f-]{24,40}")
# Only these keys are requested: the full records carry entitlements and container paths.
ATTRIBUTES = ("CFBundleIdentifier", "CFBundleDisplayName", "CFBundleName", "CFBundleShortVersionString")
# The WebDriverAgent runner Mobster installs (and stock or UI-test runners) is not an app to automate.
HIDDEN = re.compile(r"(?:app\.mobster\.wda\.|com\.facebook\.WebDriverAgent).*|.*\.xctrunner", re.I)


class ListingError(RuntimeError):
    """The phone's apps could not be listed; the message is the reason, for the log."""


def _clean(value):
    """A display string without control characters, collapsed and bounded, else None."""
    if not isinstance(value, str):
        return None
    text = " ".join(re.sub(r"[\x00-\x1f\x7f​-‏ -‮⁦-⁩]", " ", value).split())
    return text[:NAME_LIMIT] or None


def parse_listing(data):
    """[{"bundleId", "name", "version"}] from ``ideviceinstaller list --xml`` output, sorted by name.

    Raises ListingError when the output is not a plist array. Entries without a
    valid bundle identifier are skipped; the name falls back to the bundle.
    """
    if isinstance(data, str):
        data = data.encode()
    start = data.find(b"<?xml")
    if start < 0:
        start = data.find(b"<plist")
    if start < 0:
        raise ListingError("ideviceinstaller printed no property list")
    try:
        items = plistlib.loads(data[start:])
    except Exception as error:
        raise ListingError(f"ideviceinstaller printed an unreadable property list ({type(error).__name__})") from None
    if not isinstance(items, list):
        raise ListingError("ideviceinstaller printed a property list that is not an app list")
    apps, seen = [], set()
    for item in items[:MAX_APPS]:
        if not isinstance(item, dict):
            continue
        try:
            bundle = validate_bundle_id(item.get("CFBundleIdentifier"))
        except ValueError:
            continue
        if bundle in seen:
            continue
        seen.add(bundle)
        name = _clean(item.get("CFBundleDisplayName")) or _clean(item.get("CFBundleName")) or bundle[:NAME_LIMIT]
        apps.append({"bundleId": bundle, "name": name, "version": _clean(item.get("CFBundleShortVersionString"))})
    apps.sort(key=lambda app: (app["name"].casefold(), app["bundleId"]))
    return apps


def list_apps(tool, udid, kind="user", runner=subprocess.run, timeout=LIST_TIMEOUT):
    """The phone's user or system apps (parse_listing). Raises ListingError with the reason."""
    if kind not in {"user", "system"}:
        raise ValueError("kind must be user or system")
    if not tool:
        raise ListingError("ideviceinstaller is not installed (brew install ideviceinstaller)")
    if not isinstance(udid, str) or not UDID_PATTERN.fullmatch(udid):
        raise ListingError("no iPhone is chosen and connected")
    command = [tool, "-u", udid, "list", f"--{kind}", "--xml"]
    for attribute in ATTRIBUTES:
        command += ["-a", attribute]
    try:
        completed = runner(command, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ListingError(f"ideviceinstaller took longer than {timeout:.0f} s") from None
    except (OSError, subprocess.SubprocessError) as error:
        raise ListingError(f"ideviceinstaller could not run ({type(error).__name__})") from None
    output = completed.stdout or b""
    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode(errors="replace").strip().splitlines()
        text = (detail[-1] if detail else "")[:160]
        if re.search(r"lockdown|pair|trust|password", text, re.I):
            raise ListingError(f"the iPhone is not paired with this Mac or is locked ({text})")
        if re.search(r"no device|not found|could not connect", text, re.I):
            raise ListingError(f"the iPhone is not connected ({text})")
        raise ListingError(f"ideviceinstaller failed with exit code {completed.returncode} ({text or 'no output'})")
    if len(output) > MAX_OUTPUT:
        raise ListingError("ideviceinstaller printed more than expected")
    return parse_listing(output)


@dataclass(frozen=True)
class Inventory:
    """One reading of the phone: user apps, and the bundle IDs of its system apps.

    ``system`` is None when only the user list could be read.
    """
    udid: str
    user: tuple
    system: frozenset | None
    at: float


class InstalledApps:
    """The chosen phone's installed apps, cached and refreshed off the request thread.

    ``device`` returns the chosen, connected phone ({"udid", "trusted"}) or None.
    """

    def __init__(self, device, tool=None, runner=subprocess.run, clock=time.monotonic,
                 ttl=CACHE_SECONDS, retry=RETRY_SECONDS, first_wait=FIRST_WAIT):
        self.device, self.runner, self.clock = device, runner, clock
        self.ttl, self.retry, self.first_wait = ttl, retry, first_wait
        self._tool = tool
        self.lock = threading.Lock()
        self.inventory = None      # the last good Inventory
        self.checked_at = None     # when the last refresh finished, good or not
        self.failed = False
        self.worker = None
        self.last_reason = None

    def tool(self):
        if self._tool is None:
            from .device_manager import tool
            return tool("ideviceinstaller")
        return self._tool

    def refresh(self):
        """Read the phone now. Returns the inventory, or None when it cannot be read."""
        try:
            device = self.device()
        except Exception as error:
            device, reason = None, f"the phone could not be looked up ({type(error).__name__})"
        else:
            reason = None if device else "no iPhone is chosen and connected"
        if device and not device.get("trusted", True):
            device, reason = None, "the iPhone has not trusted this Mac yet"
        inventory = None
        if device:
            udid = device.get("udid")
            try:
                user = list_apps(self.tool(), udid, "user", self.runner)
            except ListingError as error:
                reason = str(error)
            else:
                try:
                    system = frozenset(app["bundleId"] for app in list_apps(self.tool(), udid, "system", self.runner))
                except ListingError as error:
                    system = None
                    self._note(f"system apps unavailable: {error}")
                inventory = Inventory(udid, tuple(user), system, self.clock())
        with self.lock:
            now = self.clock()
            self.checked_at, self.failed = now, inventory is None
            if inventory is not None:
                self.inventory = inventory
            elif not device:
                self.inventory = None  # a different phone, or none: never show the old phone's apps
            elif self.inventory and (self.inventory.udid != device.get("udid") or now - self.inventory.at > STALE_SECONDS):
                self.inventory = None
            current = self.inventory
        if reason:
            self._note(reason)
        elif inventory is not None:
            self.last_reason = None
        return current

    def _note(self, reason):
        if reason != self.last_reason:
            self.last_reason = reason
            log.warning("mobster: installed apps: %s", reason)

    def snapshot(self):
        """The cached inventory (possibly a little stale), starting a background refresh when due.

        Only the very first read waits, briefly, for the phone.
        """
        with self.lock:
            due = self.checked_at is None or self.clock() - self.checked_at > (self.retry if self.failed else self.ttl)
            if due and self.worker is None:
                self.worker = threading.Thread(target=self._refresh_in_background, name="mobster-installed-apps",
                                               daemon=True)
                self.worker.start()
            worker, first = self.worker, self.checked_at is None
        if first and worker is not None:
            worker.join(self.first_wait)
        with self.lock:
            return self.inventory

    def _refresh_in_background(self):
        try:
            self.refresh()
        finally:
            with self.lock:
                self.worker = None


def merge(catalog, inventory):
    """The catalog marked against the phone, then every user app the catalog lacks.

    Without an inventory the catalog is returned unchanged (installation unknown).
    """
    apps = [dict(app) for app in catalog]
    if inventory is None:
        return apps
    user = {app["bundleId"]: app for app in inventory.user}
    known = set(user) | set(inventory.system or ())
    for app in apps:
        bundle = app["bundleId"]
        if bundle in user:
            app.update(installed=True, availability="installed", foundOnPhone=True)
        elif inventory.system is not None:
            app.update(installed=bundle in known, availability="installed" if bundle in known else "unverified")
    listed = {app["bundleId"] for app in apps}
    for item in inventory.user:
        if item["bundleId"] in listed or HIDDEN.fullmatch(item["bundleId"]):
            continue
        # The bundle ID is the id: stable across listings, and it can never collide with a
        # catalog slug (bundle IDs always contain a dot, slugs never do).
        apps.append({"id": item["bundleId"], "name": item["name"], "bundleId": item["bundleId"],
                     "available": True, "installed": True, "availability": "installed",
                     "automationVerified": False, "foundOnPhone": True, "version": item.get("version")})
    return apps

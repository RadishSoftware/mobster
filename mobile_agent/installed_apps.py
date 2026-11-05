"""The apps installed on the USB iPhone, read with ideviceinstaller.

WebDriverAgent can launch any app by bundle ID, so the app picker offers every
user app on the phone, not only the built-in catalog. This module only ever
runs ``ideviceinstaller list``: it never installs, uninstalls or upgrades.

A listing takes a second or two over USB, so it is cached (about a minute) and
refreshed in the background: GET /api/apps is polled every few seconds and must
never wait on the phone. When the tool, the phone or the pairing is missing the
inventory is unknown (``None``) and the reason is logged once.
"""

import json
import logging
import os
import plistlib
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .devices import UDID_PATTERN
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
# Only these keys are requested: the full records carry entitlements and container paths.
ATTRIBUTES = ("CFBundleIdentifier", "CFBundleDisplayName", "CFBundleName", "CFBundleShortVersionString",
              "CFBundleExecutable")
# The WebDriverAgent runner Mobster installs (and stock or UI-test runners) is not an app to automate.
HIDDEN = re.compile(r"(?:app\.mobster\.wda\.|com\.facebook\.WebDriverAgent).*|.*\.xctrunner", re.I)


class ListingError(RuntimeError):
    """The phone's apps could not be listed; the message is the reason, for the log."""


def _clean(value):
    """A display string without control characters, collapsed and bounded, else None."""
    if not isinstance(value, str):
        return None
    text = " ".join(re.sub(r"[\x00-\x1f\x7f\u200b-\u200f\u2028-\u202e\u2066-\u2069]", " ", value).split())
    return text[:NAME_LIMIT] or None


def parse_listing(data):
    """The apps in ``ideviceinstaller list --xml`` output, sorted by name; ListingError if it isn't a plist array."""
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
        app = {"bundleId": bundle, "name": name, "version": _clean(item.get("CFBundleShortVersionString"))}
        if executable := _clean(item.get("CFBundleExecutable")):
            app["executable"] = executable    # the process name in a packet capture (Network X-ray)
        apps.append(app)
    apps.sort(key=lambda app: (app["name"].casefold(), app["bundleId"]))
    return apps



def list_apps_via_coredevice(udid, kind="user"):
    """The same listing through CoreDevice, which reaches a phone over Wi-Fi where ideviceinstaller (usbmux) can't."""
    from .harness_api import hooks
    read = hooks().coredevice_apps
    if not isinstance(udid, str) or not UDID_PATTERN.fullmatch(udid):
        raise ListingError("no iPhone is chosen and connected")
    try:
        return read(udid, kind)
    except Exception as error:
        raise ListingError(f"devicectl could not list the apps ({error})") from None


def list_apps(tool, udid, kind="user", runner=subprocess.run, timeout=LIST_TIMEOUT, network=False):
    """The phone's user or system apps (parse_listing)."""
    if kind not in {"user", "system"}:
        raise ValueError("kind must be user or system")
    if not tool:
        raise ListingError("ideviceinstaller is not installed (brew install ideviceinstaller)")
    if not isinstance(udid, str) or not UDID_PATTERN.fullmatch(udid):
        raise ListingError("no iPhone is chosen and connected")
    command = [tool, "-u", udid, *(["-n"] if network else []), "list", f"--{kind}", "--xml"]
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
    """One reading of the phone."""
    udid: str
    user: tuple
    system: frozenset | None
    at: float
    # Wall-clock seconds when it was read (``at`` is the monotonic clock, for the cache's age): what "As of" shows.
    wall: float | None = None


class InventoryCache:
    """The last inventory per phone in ``<folder>/<udid>.json``: bundle IDs, names and versions only."""
    def __init__(self, folder):
        self.folder = Path(folder) if folder else None

    def _path(self, udid):
        return self.folder / f"{udid}.json"

    def save(self, inventory):
        if self.folder is None or not UDID_PATTERN.fullmatch(inventory.udid or ""):
            return
        try:
            self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            data = {"udid": inventory.udid, "wall": inventory.wall, "user": list(inventory.user),
                    "system": sorted(inventory.system) if inventory.system is not None else None}
            for path, value in ((self._path(inventory.udid), data),
                                (self.folder / "last.json", {"udid": inventory.udid})):
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(value))
                os.replace(temporary, path)
        except OSError as error:
            log.warning("mobster: installed apps: could not keep the listing (%s)", type(error).__name__)

    def load(self, udid=None):
        """The kept inventory of ``udid``, else of the phone read last; None when there is none."""
        if self.folder is None:
            return None
        try:
            if udid is None:
                udid = json.loads((self.folder / "last.json").read_text()).get("udid")
            if not isinstance(udid, str) or not UDID_PATTERN.fullmatch(udid):
                return None
            data = json.loads(self._path(udid).read_text())
            user = tuple({"bundleId": validate_bundle_id(app["bundleId"]),
                          "name": _clean(app.get("name")) or app["bundleId"],
                          "version": _clean(app.get("version"))} for app in data.get("user", [])[:MAX_APPS])
            system = (frozenset(validate_bundle_id(bundle) for bundle in data["system"][:MAX_APPS])
                      if data.get("system") is not None else None)
            wall = data.get("wall") if isinstance(data.get("wall"), (int, float)) else None
            return Inventory(udid, user, system, 0.0, wall)
        except (OSError, ValueError, KeyError, TypeError):
            return None


class InstalledApps:
    """The chosen phone's installed apps, cached and refreshed off the request thread."""

    def __init__(self, device, tool=None, runner=subprocess.run, clock=time.monotonic,
                 ttl=CACHE_SECONDS, retry=RETRY_SECONDS, first_wait=FIRST_WAIT, cache=None, wall=time.time,
                 over_wifi=None):
        self.device, self.over_wifi, self.runner, self.clock, self.wall = device, over_wifi, runner, clock, wall
        # The last listing of each phone, on disk (InventoryCache), shown while that phone is unplugged.
        self.cache = cache
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
            from .mac_tools import tool
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
        wifi = None
        if not device and self.over_wifi is not None:
            try:
                wifi = self.over_wifi()
            except Exception:
                wifi = None
            if wifi:
                device, reason = {"udid": wifi}, None
        if device and not device.get("trusted", True):
            device, reason = None, "the iPhone has not trusted this Mac yet"
        inventory = None
        if device:
            udid = device.get("udid")
            read = (list_apps_via_coredevice if wifi else
                    lambda who, kind: list_apps(self.tool(), who, kind, self.runner))
            try:
                user = read(udid, "user")
            except ListingError as error:
                reason = str(error)
            else:
                try:
                    system = frozenset(app["bundleId"] for app in read(udid, "system"))
                except ListingError as error:
                    system = None
                    self._note(f"system apps unavailable: {error}")
                inventory = Inventory(udid, tuple(user), system, self.clock(), self.wall())
        with self.lock:
            now = self.clock()
            self.checked_at, self.failed = now, inventory is None
            if inventory is not None:
                self.inventory = inventory
            elif not device:
                self.inventory = None  # a different phone, or none: never show the old phone's apps
            elif (self.inventory
                  and (self.inventory.udid != device.get("udid") or now - self.inventory.at > STALE_SECONDS)):
                self.inventory = None
            current = self.inventory
        if inventory is not None and self.cache is not None:
            self.cache.save(inventory)
        if reason:
            self._note(reason)
        elif inventory is not None:
            self.last_reason = None
        return current

    def kept(self):
        """The listing kept on disk for the phone read last (it may be unplugged now), or None."""
        return self.cache.load() if self.cache is not None else None

    def _note(self, reason):
        if reason != self.last_reason:
            self.last_reason = reason
            log.warning("mobster: installed apps: %s", reason)

    def snapshot(self):
        """The cached inventory, starting a background refresh when due; only the very first read waits, briefly."""
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


def merge(catalog, inventory, hide_missing=False):
    """The catalog marked against the phone, then every user app the catalog lacks; no inventory claims nothing."""
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
    if hide_missing:
        apps = [app for app in apps if app.get("installed") is not False]
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


def apps_view(catalog, installed):
    """What GET /api/apps shows for one phone: {"apps", "inventory": {"state": live|cached|none|unknown, "at"}}."""
    if installed is None:
        return {"apps": merge(catalog, None), "inventory": {"state": "unknown", "at": None}}
    live = installed.snapshot()
    if live is not None:
        return {"apps": merge(catalog, live, hide_missing=True),
                "inventory": {"state": "live", "at": _millis(live.wall)}}
    kept = installed.kept()
    if kept is not None:
        return {"apps": merge(catalog, kept, hide_missing=True),
                "inventory": {"state": "cached", "at": _millis(kept.wall)}}
    return {"apps": merge(catalog, None), "inventory": {"state": "none", "at": None}}


def _millis(seconds):
    return round(seconds * 1000) if isinstance(seconds, (int, float)) else None

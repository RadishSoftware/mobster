"""Which phones may be reached over Wi-Fi: ``wifi: {enabled, enabledAt, via}`` per phone (SPEC §3.7). ``via`` says
who turned on the phone's own switch (Connect via network); with Wi-Fi off it is kept only while that switch is one
Mobster turned on and couldn't turn off yet, so a later `mobster wifi off` over the cable still can."""

import time
from pathlib import Path

VIA = ("mobster", "xcode")
OFF = {"enabled": False, "enabledAt": None, "via": None}


def _clean(value):
    if not isinstance(value, dict):
        return dict(OFF)
    via = value.get("via") if value.get("via") in VIA else None
    if value.get("enabled") is not True:
        return {**OFF, "via": via}
    at = value.get("enabledAt")
    return {"enabled": True, "enabledAt": at if isinstance(at, int) and not isinstance(at, bool) else None,
            "via": via}


def _primary(data_dir):
    from ..cli_constants import DEFAULT_WDA_URL
    from ..device_manager import DeviceManager
    return DeviceManager(Path(data_dir), DEFAULT_WDA_URL)


def _same(a, b):
    return isinstance(a, str) and isinstance(b, str) and a.upper() == b.upper()


def get(data_dir, udid):
    """{"enabled", "enabledAt", "via"} for ``udid``; off when it isn't set up here or never was turned on."""
    from ..devices import DeviceStore
    entry = next((item for item in DeviceStore(data_dir).phones() if _same(item["udid"], udid)), None)
    if entry is not None:
        return _clean(entry.get("wifi"))
    settings = _primary(data_dir).settings() if Path(data_dir, "device.json").exists() else {}
    wifi = settings.get("wifi")
    # the primary's entry keeps the UDID it belongs to, so choosing another primary phone never carries it over
    if _same(settings.get("udid"), udid) and isinstance(wifi, dict) and _same(wifi.get("udid"), udid):
        return _clean(wifi)
    return dict(OFF)


def enabled(data_dir, udid):
    return get(data_dir, udid)["enabled"]


def is_set_up(data_dir, udid):
    """True when ``udid`` is the phone chosen in Setup or one added besides it."""
    from ..devices import DeviceStore
    if any(_same(item["udid"], udid) for item in DeviceStore(data_dir).phones()):
        return True
    return _same(_primary(data_dir).settings().get("udid"), udid)


def put(data_dir, udid, on, via=None):
    from ..devices import DeviceStore
    if via is not None and via not in VIA:
        raise ValueError("via is mobster or xcode")
    value = ({"enabled": True, "enabledAt": round(time.time() * 1000), "via": via} if on else {**OFF, "via": via})
    kept = on or via is not None
    store = DeviceStore(data_dir)
    if any(_same(item["udid"], udid) for item in store.phones()):
        store.set_wifi(udid, value if kept else None)
        return _clean(value)
    manager = _primary(data_dir)
    if not _same(manager.settings().get("udid"), udid):
        raise LookupError("not_set_up")
    manager.save_settings(wifi={**value, "udid": udid} if kept else None)
    return _clean(value)


def enabled_udids(data_dir):
    """The UDIDs (upper case) with Wi-Fi on, of every phone set up here."""
    from ..devices import DeviceStore
    found = {item["udid"].upper() for item in DeviceStore(data_dir).phones() if _clean(item.get("wifi"))["enabled"]}
    if Path(data_dir, "device.json").exists():
        settings = _primary(data_dir).settings()
        wifi = settings.get("wifi")
        if isinstance(wifi, dict) and _same(settings.get("udid"), wifi.get("udid")) and _clean(wifi)["enabled"]:
            found.add(settings["udid"].upper())
    return found

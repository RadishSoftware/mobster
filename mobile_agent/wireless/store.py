"""Which phones may be reached over Wi-Fi: ``wifi: {enabled, enabledAt, via}`` per phone (SPEC §3.7, data model).

A phone set up besides the primary keeps it on its devices.json entry. The primary phone (slot 0) has no entry
there: it keeps it in its device.json, with the UDID it belongs to, so choosing another primary phone in Setup never
carries it over. Both files are rewritten whole by older Mobster builds without losing it (they keep unknown keys).

``via`` says who turned the phone's lockdown flag on: "mobster" (mobster-wifi-pair) or "xcode" (the person ticked
Connect via network); ``off`` clears the flag only when Mobster set it.
"""

from pathlib import Path
import time

VIA = ("mobster", "xcode")
OFF = {"enabled": False, "enabledAt": None, "via": None}


def _clean(value):
    if not isinstance(value, dict) or value.get("enabled") is not True:
        return dict(OFF)
    at = value.get("enabledAt")
    via = value.get("via")
    return {"enabled": True, "enabledAt": at if isinstance(at, int) and not isinstance(at, bool) else None,
            "via": via if via in VIA else None}


def _primary(data_dir):
    from ..device_manager import DeviceManager
    return DeviceManager(Path(data_dir), "http://127.0.0.1:8100")


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
    if _same(settings.get("udid"), udid) and isinstance(wifi, dict) and _same(wifi.get("udid"), udid):
        return _clean(wifi)
    return dict(OFF)


def enabled(data_dir, udid):
    return get(data_dir, udid)["enabled"]


def put(data_dir, udid, on, via=None):
    """Turn Wi-Fi on or off for ``udid``; returns its new value. LookupError when the phone isn't set up here (it is
    neither the phone chosen in Setup nor one added besides it)."""
    from ..devices import DeviceStore
    if via is not None and via not in VIA:
        raise ValueError("via is mobster or xcode")
    value = ({"enabled": True, "enabledAt": round(time.time() * 1000), "via": via} if on else dict(OFF))
    store = DeviceStore(data_dir)
    if any(_same(item["udid"], udid) for item in store.phones()):
        store.set_wifi(udid, value if on else None)
        return _clean(value)
    manager = _primary(data_dir)
    if not _same(manager.settings().get("udid"), udid):
        raise LookupError("not_set_up")
    manager.save_settings(wifi={**value, "udid": udid} if on else None)
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

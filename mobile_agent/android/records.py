"""Android devices as the device records `list_devices` and the device picker use (devices.discover's shape)."""

from . import enabled


def records(data_dir=None):
    """One record per adb device, [] when Android is off or adb fails. ``emulator`` marks a device that is not
    someone's phone: it is handled like an iOS simulator everywhere a record's kind is read."""
    if not enabled():
        return []
    from ..paths import dev_data_dir
    from .emulator import EmulatorManager
    try:
        rows = EmulatorManager(data_dir or dev_data_dir()).devices()
    except Exception:
        return []
    out = []
    for row in rows:
        state = {"device": "ready", "offline": "disconnected",
                 "unauthorized": "needs_setup"}.get(row["state"], "unknown")
        out.append({"id": f"android:{row['serial']}", "kind": "android", "platform": "android", "name": row["name"],
                    "model": row["name"], "serial": row["serial"], "avd": row["avd"] or None, "state": state,
                    "emulator": row["kind"] == "emulator",
                    "reason": "unlock the phone and accept the USB debugging prompt"
                              if row["state"] == "unauthorized" else None})
    return out


def is_phone(record):
    """Someone's own device: anything that is not an iOS simulator or an Android emulator."""
    return record.get("kind") != "simulator" and not record.get("emulator")

"""Small, cached device identity (USB lockdown). No serial, UDID or account identifiers."""

import re
import threading
import time

# Marketing name mapping verified against the libimobiledevice/libirecovery
# device table. Unknown models retain their exact identifier, never a guess.
# https://github.com/libimobiledevice/libirecovery/blob/master/src/libirecovery.c
MODEL_NAMES = {
    "iPhone10,1": "iPhone 8", "iPhone10,4": "iPhone 8", "iPhone10,2": "iPhone 8 Plus", "iPhone10,5": "iPhone 8 Plus",
    "iPhone10,3": "iPhone X", "iPhone10,6": "iPhone X", "iPhone11,2": "iPhone XS", "iPhone11,4": "iPhone XS Max",
    "iPhone11,6": "iPhone XS Max", "iPhone11,8": "iPhone XR", "iPhone12,1": "iPhone 11", "iPhone12,3": "iPhone 11 Pro",
    "iPhone12,5": "iPhone 11 Pro Max", "iPhone12,8": "iPhone SE (2nd gen)", "iPhone13,1": "iPhone 12 mini",
    "iPhone13,2": "iPhone 12", "iPhone13,3": "iPhone 12 Pro", "iPhone13,4": "iPhone 12 Pro Max",
    "iPhone14,2": "iPhone 13 Pro", "iPhone14,3": "iPhone 13 Pro Max", "iPhone14,4": "iPhone 13 mini",
    "iPhone14,5": "iPhone 13", "iPhone14,6": "iPhone SE (3rd gen)", "iPhone14,7": "iPhone 14",
    "iPhone14,8": "iPhone 14 Plus", "iPhone15,2": "iPhone 14 Pro", "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone15,4": "iPhone 15", "iPhone15,5": "iPhone 15 Plus", "iPhone16,1": "iPhone 15 Pro",
    "iPhone16,2": "iPhone 15 Pro Max", "iPhone17,1": "iPhone 16 Pro", "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone17,3": "iPhone 16", "iPhone17,4": "iPhone 16 Plus", "iPhone17,5": "iPhone 16e",
    "iPhone18,1": "iPhone 17 Pro", "iPhone18,2": "iPhone 17 Pro Max", "iPhone18,3": "iPhone 17",
    "iPhone18,4": "iPhone Air", "iPhone18,5": "iPhone 17e", "iPhone19,2": "iPhone 18 Pro",
    # Regional variants share one marketing name.
    "iPhone19,3": "iPhone 18 Pro Max", "iPhone19,7": "iPhone 18 Pro Max",
}


def usb_info(device):
    """Identity of a USB iPhone as its lockdown service reports it (no UDID or serial)."""
    model = device.get("model")
    version = device.get("ios")
    if not isinstance(model, str) or not re.fullmatch(r"iPhone[0-9]{1,3},[0-9]{1,3}", model):
        model = None
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]{1,2}(?:\.[0-9]{1,3}){1,2}", version):
        version = None
    return {"modelIdentifier": model, "modelName": MODEL_NAMES.get(model), "iosVersion": version,
            "buildVersion": None, "virtual": False, "source": "usb",
            "verifiedAt": time.time() * 1000, "stale": False}


class DeviceInfo:
    """The served phone's identity, re-read at most once a minute.

    ``manager`` is the USB device manager (``device_manager.DeviceManager``). A
    subclass for another kind of target overrides ``read``.
    """
    virtual = False

    def __init__(self, manager=None):
        self.manager = manager
        self.lock = threading.Lock()
        self.checked_at = None
        self.value = {"modelIdentifier": None, "modelName": None, "iosVersion": None,
                      "buildVersion": None, "virtual": self.virtual,
                      "source": None, "verifiedAt": None, "stale": False}

    def read(self):
        """The identity now, or None when it cannot be read."""
        if self.manager is None:
            return None
        device = self.manager.device()
        return usb_info(device) if device is not None and device.get("trusted") else None

    def snapshot(self):
        with self.lock:
            if self.checked_at is not None and time.monotonic() - self.checked_at < 60:
                return dict(self.value)
            self.checked_at = time.monotonic()
            try:
                value = self.read()
            except Exception:
                value = None
            if value is not None:
                self.value = value
            elif self.manager is not None or type(self).read is not DeviceInfo.read:
                self.value = {**self.value, "stale": self.value["verifiedAt"] is not None}
            return dict(self.value)

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
    # iPads from the same table, from the iPad Pro (2nd gen, 2017) on. Wi-Fi and cellular models share one name.
    "iPad7,1": "iPad Pro 12.9-inch (2nd gen)", "iPad7,2": "iPad Pro 12.9-inch (2nd gen)",
    "iPad7,3": "iPad Pro 10.5-inch", "iPad7,4": "iPad Pro 10.5-inch", "iPad7,5": "iPad (6th gen)",
    "iPad7,6": "iPad (6th gen)", "iPad7,11": "iPad (7th gen)", "iPad7,12": "iPad (7th gen)",
    "iPad8,1": "iPad Pro 11-inch (1st gen)", "iPad8,2": "iPad Pro 11-inch (1st gen)",
    "iPad8,3": "iPad Pro 11-inch (1st gen)", "iPad8,4": "iPad Pro 11-inch (1st gen)",
    "iPad8,5": "iPad Pro 12.9-inch (3rd gen)", "iPad8,6": "iPad Pro 12.9-inch (3rd gen)",
    "iPad8,7": "iPad Pro 12.9-inch (3rd gen)", "iPad8,8": "iPad Pro 12.9-inch (3rd gen)",
    "iPad8,9": "iPad Pro 11-inch (2nd gen)", "iPad8,10": "iPad Pro 11-inch (2nd gen)",
    "iPad8,11": "iPad Pro 12.9-inch (4th gen)", "iPad8,12": "iPad Pro 12.9-inch (4th gen)",
    "iPad11,1": "iPad mini (5th gen)", "iPad11,2": "iPad mini (5th gen)", "iPad11,3": "iPad Air (3rd gen)",
    "iPad11,4": "iPad Air (3rd gen)", "iPad11,6": "iPad (8th gen)", "iPad11,7": "iPad (8th gen)",
    "iPad12,1": "iPad (9th gen)", "iPad12,2": "iPad (9th gen)", "iPad13,1": "iPad Air (4th gen)",
    "iPad13,2": "iPad Air (4th gen)", "iPad13,4": "iPad Pro 11-inch (3rd gen)",
    "iPad13,5": "iPad Pro 11-inch (3rd gen)", "iPad13,6": "iPad Pro 11-inch (3rd gen)",
    "iPad13,7": "iPad Pro 11-inch (3rd gen)", "iPad13,8": "iPad Pro 12.9-inch (5th gen)",
    "iPad13,9": "iPad Pro 12.9-inch (5th gen)", "iPad13,10": "iPad Pro 12.9-inch (5th gen)",
    "iPad13,11": "iPad Pro 12.9-inch (5th gen)", "iPad13,16": "iPad Air (5th gen)", "iPad13,17": "iPad Air (5th gen)",
    "iPad13,18": "iPad (10th gen)", "iPad13,19": "iPad (10th gen)", "iPad14,1": "iPad mini (6th gen)",
    "iPad14,2": "iPad mini (6th gen)", "iPad14,3": "iPad Pro 11-inch (4th gen)",
    "iPad14,4": "iPad Pro 11-inch (4th gen)", "iPad14,5": "iPad Pro 12.9-inch (6th gen)",
    "iPad14,6": "iPad Pro 12.9-inch (6th gen)", "iPad14,8": "iPad Air 11-inch (M2)",
    "iPad14,9": "iPad Air 11-inch (M2)", "iPad14,10": "iPad Air 13-inch (M2)", "iPad14,11": "iPad Air 13-inch (M2)",
    "iPad15,3": "iPad Air 11-inch (M3)", "iPad15,4": "iPad Air 11-inch (M3)", "iPad15,5": "iPad Air 13-inch (M3)",
    "iPad15,6": "iPad Air 13-inch (M3)", "iPad15,7": "iPad (A16)", "iPad15,8": "iPad (A16)",
    "iPad16,1": "iPad mini (A17 Pro)", "iPad16,2": "iPad mini (A17 Pro)", "iPad16,3": "iPad Pro 11-inch (M4)",
    "iPad16,4": "iPad Pro 11-inch (M4)", "iPad16,5": "iPad Pro 13-inch (M4)", "iPad16,6": "iPad Pro 13-inch (M4)",
    "iPad16,8": "iPad Air 11-inch (M4)", "iPad16,9": "iPad Air 11-inch (M4)", "iPad16,10": "iPad Air 13-inch (M4)",
    "iPad16,11": "iPad Air 13-inch (M4)", "iPad17,1": "iPad Pro 11-inch (M5)", "iPad17,2": "iPad Pro 11-inch (M5)",
    "iPad17,3": "iPad Pro 13-inch (M5)", "iPad17,4": "iPad Pro 13-inch (M5)",
}


# The phone's finish as lockdown reports it (DeviceEnclosureColor): a small number on recent iPhones, a hex colour on
# older ones. It names Apple's own art for that finish; anything else is dropped,
# so the app shows the model's neutral one.
ENCLOSURE_PATTERN = re.compile(r"#?([0-9A-Fa-f]{1,6})")


def enclosure_code(value):
    """DeviceEnclosureColor as the dashboard keys it ("8", "e1ccb7"), or None when it isn't one."""
    match = ENCLOSURE_PATTERN.fullmatch((value or "").strip())
    return match.group(1).lower() if match else None


def usb_info(device):
    """Identity of a USB iPhone or iPad as its lockdown service reports it (no UDID or serial)."""
    model = device.get("model")
    version = device.get("ios")
    if not isinstance(model, str) or not re.fullmatch(r"(?:iPhone|iPad)[0-9]{1,3},[0-9]{1,3}", model):
        model = None
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]{1,2}(?:\.[0-9]{1,3}){1,2}", version):
        version = None
    return {"modelIdentifier": model, "modelName": MODEL_NAMES.get(model), "iosVersion": version,
            "buildVersion": None, "virtual": False, "source": "usb",
            "verifiedAt": time.time() * 1000, "stale": False}


class DeviceInfo:
    """The served phone's identity, re-read at most once a minute."""
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

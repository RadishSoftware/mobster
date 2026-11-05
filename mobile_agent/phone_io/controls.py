"""Hardware buttons and system controls, for developers testing their app: what WebDriverAgent offers on a real
iPhone and a simulator.

- Buttons (POST /wda/pressButton): home, volume_up and volume_down (a real iPhone only: a simulator has no volume
  buttons). The Action and Camera buttons are left out on purpose: each does whatever its owner set it to.
- Lock (POST /wda/lock): locks the screen. Mobster can't unlock an iPhone and never enters a passcode, so after a lock
  the owner unlocks it (lockscreen.CAN_UNLOCK is False). On a simulator the lock screen has no passcode.
- Appearance (POST /wda/device/appearance, light or dark): changes the setting for the whole phone; the previous
  value (GET /wda/device/info's userInterfaceStyle) comes back so it can be set back.
- Orientation (POST /orientation): portrait, landscape_left, landscape_right or portrait_upside_down. XCTest turns the
  device the way a hand would (developer.apple.com/documentation/xcuiautomation/xcuidevice/orientation); an app that
  supports one orientation stays in it.

Every call reports what it did in one plain sentence, and ``state`` reads appearance and orientation without
changing anything.
"""

from . import PhoneIOError

BUTTONS = {"home": "home", "volume_up": "volumeup", "volume_down": "volumedown"}
APPEARANCES = ("light", "dark")
ORIENTATIONS = {"portrait": "PORTRAIT", "landscape_left": "LANDSCAPE",
                "landscape_right": "UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT",
                "portrait_upside_down": "UIA_DEVICE_ORIENTATION_PORTRAIT_UPSIDEDOWN"}
ORIENTATION_NAMES = {"PORTRAIT": "portrait", "LANDSCAPE": "landscape_left",
                     "UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT": "landscape_right",
                     "UIA_DEVICE_ORIENTATION_PORTRAIT_UPSIDEDOWN": "portrait_upside_down"}
LOCKED_AFTER = "Mobster can't unlock it again: the owner unlocks it."


def press(call, button, *, simulator=False):
    """Press a hardware button. ``call(method, path, body, timeout)`` is a WebDriverAgent request."""
    name = BUTTONS.get(button)
    if name is None:
        raise PhoneIOError(f"Mobster presses {', '.join(BUTTONS)}; not {button}.", "usage")
    if simulator and button != "home":
        raise PhoneIOError("A simulator has no volume buttons.", "usage")
    call("POST", "/wda/pressButton", {"name": name}, 10)
    return {"button": button}


def lock(call):
    call("POST", "/wda/lock", {}, 15)
    return {"locked": True}


def state(call):
    """{"appearance": light | dark | None, "orientation": portrait | landscape_left | ... | None}."""
    appearance = orientation = None
    try:
        info = call("GET", "/wda/device/info", None, 5)
        style = (info or {}).get("userInterfaceStyle") if isinstance(info, dict) else None
        appearance = style if style in APPEARANCES else None
    except Exception:
        pass
    try:
        value = call("GET", "/orientation", None, 5)
        orientation = ORIENTATION_NAMES.get(value) if isinstance(value, str) else None
    except Exception:
        pass
    return {"appearance": appearance, "orientation": orientation}


def set_appearance(call, value):
    if value not in APPEARANCES:
        raise PhoneIOError("Appearance is light or dark.", "usage")
    before = state(call)["appearance"]
    call("POST", "/wda/device/appearance", {"name": value}, 10)
    return {"appearance": value, "before": before}


def set_orientation(call, value):
    wanted = ORIENTATIONS.get(value)
    if wanted is None:
        raise PhoneIOError("Orientation is " + ", ".join(ORIENTATIONS) + ".", "usage")
    before = state(call)["orientation"]
    call("POST", "/orientation", {"orientation": wanted}, 15)
    return {"orientation": value, "before": before}

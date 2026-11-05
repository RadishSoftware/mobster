"""What a phone and this Mac say about power and heat: battery, charging, thermal state."""

import plistlib
import re
import subprocess
import time

from . import native_helpers, phone_ready

# How long a phone's or the Mac's reading stands before it is read again.
PHONE_TTL = 30.0
MAC_TTL = 20.0
READ_TIMEOUT = 4.0

THERMAL = ("nominal", "fair", "serious", "critical")
# UIDevice.BatteryState: 0 unknown, 1 unplugged, 2 charging, 3 full.
WDA_BATTERY_STATE = {1: "unplugged", 2: "charging", 3: "full"}
# Battery states that put power in: a paused task goes on at these.
CHARGING = frozenset({"charging", "full"})
# Battery states that drain it: plugged into something that doesn't charge it counts.
DRAINING = frozenset({"unplugged", "not_charging"})

PMSET = "/usr/bin/pmset"
POWER_SOURCE = re.compile(r"Now drawing from '([^']+)'")
MAC_BATTERY = re.compile(r"InternalBattery-\d+[^\t\n]*\t\s*(\d{1,3})%;\s*([^;\n]+)")
SOURCES = {"AC Power": "ac", "Battery Power": "battery", "UPS Power": "ups"}


def _run(argv, timeout=READ_TIMEOUT):
    """stdout bytes of a short command, or None when it failed."""
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


# -- the Mac --------------------------------------------------------------------------------------------------------

def parse_pmset(text):
    text = text if isinstance(text, str) else ""
    source = POWER_SOURCE.search(text)
    value = {"power": SOURCES.get(source.group(1)) if source else None}
    battery = MAC_BATTERY.search(text)
    if battery:
        percent = int(battery.group(1))
        if 0 <= percent <= 100:
            value["battery"] = percent
            state = battery.group(2).strip().lower()
            value["charging"] = state == "charging" or state == "finishing charge"
    return value


def read_mac(run=_run):
    """This Mac's power now (parse_pmset), {"power": None} when pmset can't say."""
    output = run([PMSET, "-g", "batt"])
    return parse_pmset(output.decode("utf-8", "replace") if output else "")


# -- the phone --------------------------------------------------------------------------------------------------------

def _plist(data):
    try:
        value = plistlib.loads(data) if data else None
    except Exception:  # noqa: BLE001 -- a value no source gives is left out
        return None
    return value if isinstance(value, dict) else None


def lockdown_battery(values):
    if not isinstance(values, dict):
        return None
    level = values.get("BatteryCurrentCapacity")
    if type(level) is not int or not 0 <= level <= 100 or values.get("HasBattery") is False:
        return None
    battery = {"level": level}
    charging, plugged, full = (values.get(key) for key in ("BatteryIsCharging", "ExternalConnected", "FullyCharged"))
    if full is True and plugged is not False:
        battery["state"] = "full"
    elif charging is True:
        battery["state"] = "charging"
    elif plugged is True and charging is False:
        battery["state"] = "not_charging"
    elif plugged is False:
        battery["state"] = "unplugged"
    return battery


def wda_battery(value):
    """{level, state?} from WDA's batteryInfo, or None (a simulator's -1, or no answer)."""
    if not isinstance(value, dict):
        return None
    level, state = value.get("level"), value.get("state")
    if type(level) not in (int, float) or not 0 <= level <= 1:
        return None
    battery = {"level": round(level * 100)}
    if type(state) is int and state in WDA_BATTERY_STATE:
        battery["state"] = WDA_BATTERY_STATE[state]
    return battery


def wda_thermal(value):
    """The thermal state's word from WDA's device info, or None."""
    state = value.get("thermalState") if isinstance(value, dict) else None
    return THERMAL[state] if type(state) is int and 0 <= state < len(THERMAL) else None


# No free storage: on a phone Settings showed 0.8 GB free and lockdown's disk_usage said 87 GB and 0.2 GB.
def read_lockdown(udid, run=_run, tool=None):
    """The battery of the USB iPhone ``udid`` ({level, state?}), or None, through ideviceinfo."""
    if tool is None:
        from .mac_tools import tool as find_tool
        tool = find_tool("ideviceinfo")
    if not tool or not udid:
        return None
    return lockdown_battery(_plist(run([tool, "-u", udid, "-q", "com.apple.mobile.battery", "-x"])))


def read_wda(url, *, battery=True, timeout=2.0):
    from .drivers import resolve_wda_session
    from .transport import read_kept  # the helper's kept connection: one a read ran WDA out of descriptors
    found_battery = thermal = None
    try:
        info = read_kept(url, "/wda/device/info", timeout=timeout).get("value")
    except Exception:  # noqa: BLE001
        info = None
    # a simulator's thermal state would be this Mac's
    if isinstance(info, dict) and info.get("isSimulator") is True:
        return None, None
    thermal = wda_thermal(info)
    if battery:
        try:
            session = resolve_wda_session(url, timeout=timeout, create=False)
            if session:
                answer = read_kept(url, f"/session/{session}/wda/batteryInfo", timeout=timeout)
                found_battery = wda_battery(answer.get("value"))
        except Exception:  # noqa: BLE001
            found_battery = None
    return found_battery, thermal


def read_phone(kind, *, udid=None, wda_url=None, lockdown=read_lockdown, wda=read_wda, clock=time.time):
    status = {}
    if not native_helpers.devices_allowed() or kind == "simulator":
        return {"readAt": round(clock() * 1000)}
    if kind == "usb" and udid:
        battery = lockdown(udid)
        if battery:
            status["battery"] = {**battery, "source": "lockdown"}
    if wda_url:
        battery, thermal = wda(wda_url, battery="battery" not in status)
        if battery and "battery" not in status:
            status["battery"] = {**battery, "source": "wda"}
        if thermal:
            status["thermal"] = thermal
        low = phone_ready.low_power(wda_url) if phone_ready.enabled() else None
        if low is not None:
            status["lowPowerMode"] = low
    status["readAt"] = round(clock() * 1000)
    return status


# -- words (the CLI's `mobster devices status` and MCP's device_status) ------------------------------------------------

BATTERY_WORDS = {"charging": "charging", "full": "charged", "not_charging": "plugged in, not charging",
                 "unplugged": "not charging"}
POWER_WORDS = {"ac": "on power", "battery": "on battery", "ups": "on a UPS"}


def rows(phone, mac):
    out = []
    battery = (phone or {}).get("battery")
    if battery:
        state = BATTERY_WORDS.get(battery.get("state"))
        out.append(("Battery", f"{battery['level']}%" + (f", {state}" if state else "")))
    if (phone or {}).get("thermal"):
        out.append(("Thermal", phone["thermal"]))
    if (phone or {}).get("lowPowerMode") is True:
        out.append(("Low Power Mode", "on, so it locks after 30 seconds"))
    if not out:
        out.append(("Battery", "not reported"))
    power = POWER_WORDS.get((mac or {}).get("power"))
    if power:
        level = mac.get("battery")
        out.append(("This Mac", power + (f", battery {level}%" if type(level) is int else "")))
    return out


def describe(name, phone, mac):
    """Sentences for MCP: the phone's readings, then the Mac's."""
    found = rows(phone, mac)
    mine = [f"{label} {value}" for label, value in found if label != "This Mac"]
    sentences = [f"{name}: " + "; ".join(mine) + "."]
    sentences += [f"This Mac is {value}." for label, value in found if label == "This Mac"]
    return sentences

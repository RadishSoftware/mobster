"""Every sentence Wi-Fi says, in plain words (the app copy's "Without the cable" block holds the same strings).

Each problem is one sentence that says what's wrong and one fix the person can do. ``{name}`` is the phone's name
("Sam's iPhone"), or "your iPhone" when Mobster doesn't know it.
"""

# code -> (what's wrong, the one fix)
PROBLEMS = {
    "needs_cable": ("Plug {name} into this Mac with a cable to turn on Wi-Fi for it.",
                    "Plug it in, unlock it and tap Trust if it asks, then try again."),
    "not_trusted": ("{name} doesn't trust this Mac yet.",
                    "Unlock it and tap Trust, then try again."),
    "not_set_up": ("{name} isn't set up in Mobster yet.",
                   "Open Setup with it plugged in and finish the steps, then turn on Wi-Fi."),
    "needs_xcode": ("Mobster couldn't turn on Wi-Fi for {name} by itself.",
                    "In Xcode, open Window › Devices and Simulators, select {name} and tick Connect via network."),
    "refused": ("{name} didn't let Mobster turn on Wi-Fi.",
                "Unlock it and try again, or tick Connect via network for it in Xcode › Devices and Simulators."),
    "not_seen": ("{name} isn't reachable over Wi-Fi.",
                 "Keep it unlocked and on the same Wi-Fi as this Mac, or plug it in."),
    "tunnel_down": ("The encrypted link to {name} isn't up yet.",
                    "Unlock it and keep it on the same Wi-Fi as this Mac; Mobster tries again by itself."),
    "address": ("Wi-Fi isn't available for this iPhone yet.",
                "Plug it in to use it now."),
    "devicectl_hang": ("Xcode's device service stopped answering.",
                       "Plug {name} in once, or restart this Mac if it keeps happening."),
    "no_xcode": ("Using an iPhone over Wi-Fi needs Xcode on this Mac.",
                 "Install Xcode from the App Store and open it once."),
    "runner": ("Mobster's helper didn't start on {name} over Wi-Fi.",
               "Keep it unlocked and on the charger, or plug it in."),
    "port_busy": ("Another program is using {name}'s connection on this Mac.",
                  "Quit the other program, or plug the iPhone in."),
    "busy": ("A task is running on {name}.",
             "Stop it first, or wait for it to finish."),
    "off": ("Wi-Fi for iPhones is off in this Mobster.",
            "Add MOBSTER_WIFI_TRANSPORT=1 to your env file to try it."),
    # This Mac's network (network.peer_check), when the phone can't be found over Wi-Fi.
    "network_blocked": ("This network stops devices from seeing each other.",
                        "Use the cable or your phone's hotspot."),
    "no_local_network": ("macOS doesn't let Mobster reach devices on your network.",
                         "Turn on Mobster in System Settings › Privacy & Security › Local Network."),
    "vpn": ("A VPN on this Mac can keep it and {name} apart.",
            "Turn the VPN off, or use the cable."),
}

# network.peer_check's state -> the problem it is, when a phone can't be found over Wi-Fi.
NETWORK_PROBLEMS = {"blocked": "network_blocked", "no_permission": "no_local_network", "vpn": "vpn"}

# The words a phone's connection shows (CLI `mobster wifi status`; the app's v2 Settings row uses the same).
ON_WIFI = "On Wi-Fi · encrypted"
ON_CABLE = "On cable"
UNREACHABLE = "Not reachable over Wi-Fi"
OFF = "Wi-Fi off"

# The run stops with this when the phone goes out of reach mid-task (lockscreen.py).
WIFI_LOST = ("Your iPhone went out of reach over Wi-Fi, so Mobster stopped. Keep it unlocked and on the same "
             "Wi-Fi as this Mac, or plug it in, then try again.")


def problem(code, name=None):
    """{"code", "message", "fix"} for a problem code, with the phone's name filled in."""
    message, fix = PROBLEMS[code]
    who = name or "your iPhone"
    return {"code": code, "message": _sentence(message.format(name=who)), "fix": _sentence(fix.format(name=who))}


def _sentence(text):
    """A sentence that starts with the phone's name ("your iPhone") starts with a capital."""
    return text[:1].upper() + text[1:] if text else text

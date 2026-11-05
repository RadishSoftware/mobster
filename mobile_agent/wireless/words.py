
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
    "route": ("Mobster stopped using Wi-Fi for {name}: this Mac no longer reaches it through the encrypted link.",
              "Turn off any VPN on this Mac, or plug {name} in."),
    "tunnel_down": ("The encrypted link to {name} isn't up yet.",
                    "Unlock it and keep it on the same Wi-Fi as this Mac; Mobster tries again by itself."),
    "address": ("Wi-Fi isn't available for this iPhone yet.",
                "Plug it in to use it now."),
    "devicectl_hang": ("Xcode's device service stopped answering.",
                       "Plug {name} in once, or restart this Mac if it keeps happening."),
    "no_xcode": ("Using an iPhone over Wi-Fi needs Xcode on this Mac.",
                 "Install Xcode from the App Store and open it once."),
    "identity": ("CoreDevice gave Mobster another iPhone's Wi-Fi address for {name}, so Mobster didn't use it.",
                 "Plug {name} in; Mobster tries Wi-Fi again by itself."),
    "runner": ("Mobster's helper didn't start on {name} over Wi-Fi.",
               "Keep it unlocked and on the charger, or plug it in."),
    "port_busy": ("Another program is using {name}'s connection on this Mac.",
                  "Quit the other program, or plug the iPhone in."),
    "busy": ("A task is running on {name}.",
             "Stop it first, or wait for it to finish."),
    "plugged_in": ("{name} is plugged in, so a Wi-Fi check says nothing about it.",
                   "Unplug the cable, keep it unlocked on the same Wi-Fi as this Mac, then check again."),
    "probe_running": ("Mobster is already checking Wi-Fi for {name}.",
                      "Wait for that check to finish."),
    "probe_timeout": ("The Wi-Fi check for {name} took too long.",
                      "Keep it unlocked on the same Wi-Fi as this Mac, then check again."),
    "off": ("Wi-Fi for iPhones is off in this Mobster.",
            "Turn it on in Settings, or set MOBSTER_WIFI_TRANSPORT=1 in your env file."),
    # Wi-Fi turned off in Mobster, with the phone's own switch (Connect via network) left on (switch.turn_off).
    "still_on": ("{name} still accepts Wi-Fi connections from Macs it trusts: Mobster turns its Connect via network "
                 "switch off only over the cable.",
                 "Plug {name} in and unlock it, then turn off Wi-Fi for it again."),
    "off_failed": ("Mobster couldn't turn off {name}'s Connect via network switch, so it still accepts Wi-Fi "
                   "connections from Macs it trusts.",
                   "Unlock {name}, keep it plugged in and turn off Wi-Fi for it again, or untick Connect via network "
                   "for it in Xcode › Window › Devices and Simulators."),
    "xcode_on": ("{name} may still accept Wi-Fi connections from Macs it trusts: Connect via network was turned on "
                 "for it in Xcode, so Mobster left it on.",
                 "In Xcode, open Window › Devices and Simulators, select {name} and untick Connect via network."),
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
ON_WIFI = "On Wi-Fi, encrypted"
ON_CABLE = "On cable"
UNREACHABLE = "Not reachable over Wi-Fi"
OFF = "Wi-Fi off"


def problem(code, name=None):
    """{"code", "message", "fix"} for a problem code, with the phone's name filled in."""
    message, fix = PROBLEMS[code]
    who = name or "your iPhone"
    return {"code": code, "message": _sentence(message.format(name=who)), "fix": _sentence(fix.format(name=who))}


def _sentence(text):
    """A sentence that starts with the phone's name ("your iPhone") starts with a capital."""
    return text[:1].upper() + text[1:] if text else text

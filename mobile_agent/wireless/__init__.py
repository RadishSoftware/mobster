"""Track `wireless`: Wi-Fi after one cable pairing, through Apple's encrypted CoreDevice tunnel (SPEC §3.7)."""

import os

# What an unset MOBSTER_WIFI_TRANSPORT means. This is the one line to flip, and only after `mobster wifi probe`
# passes on a phone with its cable out (read_screen, launch_app, install_app and unlock_status over Wi-Fi too).
DEFAULT_ON = False
GATE = "MOBSTER_WIFI_TRANSPORT"
ON_WORDS = ("1", "true", "yes", "on")
OFF_WORDS = ("0", "false", "no", "off")


def transport_on(env=None):
    value = str((os.environ if env is None else env).get(GATE, "")).strip().lower()
    return True if value in ON_WORDS else False if value in OFF_WORDS else DEFAULT_ON

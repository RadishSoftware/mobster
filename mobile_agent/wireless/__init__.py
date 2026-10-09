"""Track `wireless`. Wi-Fi after one cable pairing, through the encrypted tunnel (SPEC §3.7).

After Setup with a cable, Mobster can reach an iPhone over Wi-Fi when it's unplugged. Traffic goes only through
Apple's encrypted CoreDevice tunnel between this Mac and the phone: WebDriverAgent listens on the phone's tunnel
address (an fd00::/8 address the tunnel gives it), never on its Wi-Fi address, and this Mac's side is a relay bound
to 127.0.0.1 on the phone's usual ports. So the WDA address, the device lease, the live view and manual control are
the same on the cable and on Wi-Fi.

- ``pair``: ``mobster-wifi-pair`` (desktop/iphone-tools), which sets the phone's lockdown flag over USB, once.
- ``store``: which phones may be reached over Wi-Fi (``wifi: {enabled, enabledAt, via}`` per phone).
- ``devicectl``: what CoreDevice sees (``xcrun devicectl``, with a timeout and a kill: it can hang).
- ``xctestrun``: the per-launch copy of the runner's test plan with ``USE_IP`` set to the tunnel address.
- ``relay``: loopback-only TCP forwarders, 8100+N -> [tunnel]:8100 and 9100+N -> [tunnel]:9100.
- ``transport``: choosing cable or Wi-Fi, the backoff, and each phone's link state.
- ``monitor``: the service that keeps it all going in the Mac app (``mobster serve --manage-device``).
- ``api``, ``cli``, ``probe``: ``/api/wireless``, ``mobster wifi`` and the owner's probe.

It is all behind ``MOBSTER_WIFI_TRANSPORT`` (the hidden ``wifiTransport`` setting): off, Mobster runs exactly as it
did, on the cable only. ``mobster wifi probe`` works either way: it is how the switch gets decided.
"""

import os

# The hidden setting (SPEC §3.7 item 9): off by default, so the cable path is byte-for-byte unchanged.
GATE = "MOBSTER_WIFI_TRANSPORT"
ON_WORDS = ("1", "true", "yes", "on")


def transport_on(env=None):
    """Whether Mobster may run phones over Wi-Fi in this process (``MOBSTER_WIFI_TRANSPORT``)."""
    return str((os.environ if env is None else env).get(GATE, "")).strip().lower() in ON_WORDS

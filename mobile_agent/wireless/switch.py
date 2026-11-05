"""Turning Wi-Fi on or off for one phone: what `mobster wifi on|off` and POST /api/devices/{device}/wifi share."""

from . import pair, store

# What the pair tool says when the phone or this Mac said no before anything changed on the phone.
SAID_NO = ("needs_cable", "not_trusted", "refused")


class SwitchRefused(Exception):
    """The switch didn't happen; ``code`` is a ``words.PROBLEMS`` key."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def turn_on(data_dir, udid, listing):
    if listing is None:
        raise SwitchRefused("needs_cable")
    if not listing.get("trusted"):
        raise SwitchRefused("not_trusted")
    if not store.is_set_up(data_dir, udid):
        raise SwitchRefused("not_set_up")
    before = noted(data_dir, udid)
    via = "mobster"
    try:
        pair.run(udid, "enable")
    except LookupError:
        via = "xcode"   # no pair tool: nothing was sent, the person ticks it in Xcode
    except pair.PairError as error:
        if error.code in SAID_NO:
            put_back(data_dir, udid, before)
        raise SwitchRefused(error.code if error.code in SAID_NO else "refused") from None
    except RuntimeError:
        put_back(data_dir, udid, before)   # devices are off in this process: nothing was sent
        raise
    store.put(data_dir, udid, True, via)
    return via


def noted(data_dir, udid):
    """Mobster's list notes that Mobster may turn on the phone's switch, before the pair tool runs: an exit or crash
    before the answer still leaves the record a later `mobster wifi off` clears it from. Returns the record as it was,
    for put_back. LookupError when the phone isn't set up here."""
    before = store.get(data_dir, udid)
    if not before["enabled"]:
        store.put(data_dir, udid, False, "mobster")
    return before


def put_back(data_dir, udid, before):
    """The phone said no, or nothing was sent: the record goes back to what ``noted`` found."""
    if not before["enabled"]:
        store.put(data_dir, udid, False, before["via"])


def turn_off(data_dir, udid, listing):
    """Wi-Fi off for the phone in Mobster at once, then the phone's own switch when Mobster turned that on, over the
    cable with the phone trusting this Mac. Returns None when no switch is left on that Mobster knows of, else the
    ``words.PROBLEMS`` key of the one left on: "still_on" (not on the cable, or not trusted), "off_failed" (the pair
    tool couldn't) or "xcode_on" (turned on in Xcode, which Mobster leaves alone)."""
    via = store.get(data_dir, udid)["via"]
    try:
        store.put(data_dir, udid, False, "mobster" if via == "mobster" else None)
    except LookupError:
        raise SwitchRefused("not_set_up") from None
    if via != "mobster":
        return "xcode_on" if via == "xcode" else None
    if listing is None or not listing.get("trusted"):
        return "still_on"
    try:
        if pair.run(udid, "disable") is not False:
            return "off_failed"
    except (LookupError, RuntimeError):   # no tool, devices off in this process, or the tool said no (PairError)
        return "off_failed"
    store.put(data_dir, udid, False)
    return None

"""Pack pins: a team can pin each app pack to the app version it was checked on (``Verified.version``, the version
``GET /api/packs`` shows), or turn one off. A Mac with a pin uses a pack only when this build's pack matches it; any
other pack steps aside, and the general agent does the step, as it does for a pack that is stale. A pin never makes a
pack do more: packs only move around and read, so a pin is about every Mac in a team doing the same thing, not about
safety. The team track (teams/control_plane.py) sets the pins it read; with none set, every pack works as shipped.
"""

OFF = "off"
_pins = {}  # pack key -> OFF, or the app version the team pinned the pack to


def set_pins(pins):
    """Replace the pins with ``pins`` ({pack key: OFF or a version}); {} clears them."""
    _pins.clear()
    _pins.update(
        {key: value for key, value in dict(pins or {}).items() if isinstance(key, str) and isinstance(value, str)}
    )


def current():
    return dict(_pins)


def aside(pack):
    """Why this Mac's ``pack`` steps aside under the team's pin, in a sentence, or None when it may be used."""
    pin = _pins.get(pack.key)
    if pin is None:
        return None
    if pin == OFF:
        return f"Your team turned off the {pack.name} pack."
    built = pack.verified.version if pack.verified else None
    if built == pin:
        return None
    on = f"the pack checked on {pack.name} {built}" if built else "a pack with no checked version"
    return (
        f"Your team pinned the {pack.name} pack to {pack.name} {pin}, and this Mobster has {on}. Update Mobster, "
        "or ask an Admin to change the pin."
    )


def state(pack):
    """What this Mac reports about ``pack`` to its team: the version it has and whether it is stepping aside."""
    return {
        "key": pack.key,
        "version": pack.verified.version if pack.verified else None,
        "aside": aside(pack) is not None,
    }

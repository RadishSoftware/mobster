"""Whether a phone can take a queued post now: the lock, Photos access and Low Power Mode, read from Mobster's helper.

Off unless ``MOBSTER_QUEUE_PREFLIGHT=1`` (``enabled``). With it on, the content queue (content_queue/runner.py)

- keeps a due post ``ready`` while the phone is locked, or while the lock can't be read, instead of failing it;
- refuses to start a queue, and fails a post before any file is sent, when Photos access is off or still unanswered on
  the profile's phone, so no file waits the 60 s the first save would wait for an answer nobody gives;
- warns when Low Power Mode is on, because it makes the phone lock itself after 30 seconds.

Every read here is a sessionless GET the helper answers in milliseconds: ``/wda/locked`` (stock),
``/mobster/photos/status`` and ``/mobster/power`` (wda.patches.PREFLIGHT_PATCHES, which the same flag adds to the
helper). They take no lease and send no input. Nothing here keeps the phone awake,
unlocks it or presses a Photos grant: a person answers those on the iPhone.
A read that fails is "unknown", and unknown never counts as unlocked or as granted.
"""

import os

from .phone_io import PhoneIOError
from .phone_io.photos import STATUS_WORDS
from .wda.timeouts import timeout_for

ENV = "MOBSTER_QUEUE_PREFLIGHT"
ON_WORDS = ("1", "true", "yes", "on")
GRANTED = ("authorized", "limited")   # "limited" (Limit Access) is enough to add to Photos

HOLD_LOCKED = "Waiting for the phone: it's locked."
HOLD_UNREADABLE = "Waiting for the phone: Mobster can't tell whether it's locked."
PHOTOS_UNREADABLE = ("Mobster couldn't read Photos access on {name}. Keep it unlocked and connected, and rebuild "
                     "Mobster's helper in Setup if it was built before this check, then try again.")
NOT_ASKED = ("Mobster hasn't asked {name} for Photos yet. In Setup, press Allow beside “Let Mobster add to Photos” and "
             "answer on the iPhone (Limit Access is enough), then try again.")
LOW_POWER = ("Low Power Mode is on for {name}, so it locks itself after 30 seconds. Turn it off in Settings › "
             "Battery before {when}.")


def enabled(env=None):
    """Whether the queue's phone checks (and the helper routes they read) are on in this process."""
    return str((os.environ if env is None else env).get(ENV, "")).strip().lower() in ON_WORDS


def read(wda_url, path):
    """The value of a sessionless GET ``path`` on the helper at ``wda_url``, or None when it didn't answer in time or
    has no such route (a helper built before this check)."""
    from .transport import read_kept
    try:
        return read_kept(wda_url, path, timeout=timeout_for("GET", path)).get("value")
    except Exception:  # noqa: BLE001 - an unanswered phone is "unknown", never an error
        return None


def hold(wda_url):
    """Why a post waits for the phone now, in a sentence, or None when the phone is unlocked. Locked and unknown both
    hold: a phone Mobster can't read is never treated as unlocked."""
    locked = read(wda_url, "/wda/locked")
    if locked is False:
        return None
    return HOLD_LOCKED if locked is True else HOLD_UNREADABLE


def photos_status(wda_url):
    """The helper's Photos access for its phone: authorized, limited, denied, restricted or not_determined; None when
    the helper didn't say (it isn't running, or was built without the check)."""
    value = read(wda_url, "/mobster/photos/status")
    status = value.get("status") if isinstance(value, dict) else None
    return status if status in GRANTED or status in STATUS_WORDS else None


def photos_refusal(wda_url, name):
    """A PhoneIOError (code ``photos_denied``, ``photos_restricted``, ``photos_not_determined`` or
    ``photos_unreadable``) when Mobster's helper can't add to Photos on this phone yet, else None. Denied and
    restricted use the transfer's own words (photos.STATUS_WORDS); not asked yet points to Setup's Allow."""
    status = photos_status(wda_url)
    if status in GRANTED:
        return None
    if status in STATUS_WORDS:
        words = NOT_ASKED if status == "not_determined" else STATUS_WORDS[status]
        return PhoneIOError(words.format(name=name), "photos_" + status)
    return PhoneIOError(PHOTOS_UNREADABLE.format(name=name), "photos_unreadable")


def low_power(wda_url):
    """True or False as the helper says Low Power Mode is, None when it can't say (a helper built before this check)."""
    value = read(wda_url, "/mobster/power")
    flag = value.get("lowPowerMode") if isinstance(value, dict) else None
    return flag if isinstance(flag, bool) else None


def low_power_warning(wda_url, name, when):
    """The warning sentence when Low Power Mode is on, else None. ``when`` finishes "before ...": "the first post"."""
    return LOW_POWER.format(name=name, when=when) if low_power(wda_url) is True else None

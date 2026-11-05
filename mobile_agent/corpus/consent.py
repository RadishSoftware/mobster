"""The opt-in: off until the person has been shown ``TEXT`` and said yes, and off again when the text changes.

The setting is ``corpus/consent.json`` in the data folder, which Mobster for Mac and ``mobster corpus on`` write the
same way. No file, an unreadable file or an older text version is off.
"""

import json
import time

from ..paths import user_data_dir, write_text_atomic

TEXT_VERSION = 1
TITLE = "Help Mobster learn from your runs"
TEXT = f"""{TITLE}

When this is on, Mobster keeps a private record of each task that finishes, one line per step, on this Mac only.
Nothing is uploaded or shared. You can read everything it keeps with `mobster corpus export`, and turn it off at
any time.

What a record holds:
  - the shape of the screen: each control's kind (Button, Cell, TextField) and a one-way hash of its label, with
    numbers folded; a label that looks like an email, phone number, handle or link is replaced by its kind
  - the action: tap, type, swipe (not the text typed), and which control it hit
  - the app's bundle id and version, whether the screen changed, and a random id for the run

What it never holds: screenshots, text you typed, names, handles, numbers, messages, or any screen text. Steps in
Passwords, Wallet and Health, on a screen with a password field, and in a task you redirected or that did not finish
are not kept."""


def _path():
    return user_data_dir() / "corpus" / "consent.json"


def on():
    """True when the person opted in to the current ``TEXT``."""
    try:
        state = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(state, dict) and state.get("optedIn") is True and state.get("textVersion") == TEXT_VERSION


def set_on(value):
    """Record the answer to ``TEXT``; turning it on is only ever done after the text was shown."""
    state = {"optedIn": bool(value), "textVersion": TEXT_VERSION, "at": int(time.time())}
    _path().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_text_atomic(_path(), json.dumps(state))
    _path().chmod(0o600)


def active():
    """The flag is on and the person opted in: the only state in which anything is captured or written."""
    from . import enabled
    return enabled() and on()

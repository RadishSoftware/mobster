"""The phone's clipboard through WebDriverAgent (/wda/setPasteboard and /wda/getPasteboard), text only.

Setting it is offered to scripts and MCP (Mac to phone, at most 64 KB). Reading it is offered only on the CLI,
printed for the person at the terminal: the clipboard may hold a password, so no MCP tool reads it. To read it,
WebDriverAgent's runner comes to the front for a moment (iOS lets only the front app read the clipboard), so the
app on screen goes to the background and back.
"""

import base64

from . import PhoneIOError

LIMIT = 64 * 1024


def encode(text):
    """``text`` as WDA's base64 content; PhoneIOError (usage) when it is empty, not text or over 64 KB."""
    if not isinstance(text, str) or not text:
        raise PhoneIOError("Give the clipboard some text.", "usage")
    if "\x00" in text:
        raise PhoneIOError("The clipboard takes text, not binary data.", "usage")
    data = text.encode("utf-8")
    if len(data) > LIMIT:
        raise PhoneIOError(f"That is {len(data):,} bytes; the phone's clipboard takes at most 64 KB from Mobster.",
                           "usage")
    return base64.b64encode(data).decode("ascii")


def read_text_file(path):
    """A file's text for `clipboard set --file`: UTF-8, at most 64 KB."""
    from pathlib import Path
    file = Path(path).expanduser()
    try:
        size = file.stat().st_size
    except OSError:
        raise PhoneIOError(f"There is no file at {file}.", "usage") from None
    if size > LIMIT:
        raise PhoneIOError(f"{file.name} is {size:,} bytes; the phone's clipboard takes at most 64 KB from Mobster.",
                           "usage")
    try:
        return file.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise PhoneIOError(f"{file.name} isn't UTF-8 text; the clipboard takes text only.", "usage") from None


def set_text(request, text):
    """Put ``text`` on the phone's clipboard. ``request`` is phone_io.wda's (or a driver's call)."""
    request("POST", "/wda/setPasteboard", {"content": encode(text), "contentType": "plaintext"}, 15)
    return len(text)


def get_text(request):
    """The phone's clipboard as text ("" when it holds none)."""
    value = request("POST", "/wda/getPasteboard", {"contentType": "plaintext"}, 20)
    if not value:
        return ""
    try:
        return base64.b64decode(value).decode("utf-8", "replace")
    except (ValueError, TypeError):
        raise PhoneIOError("The phone's clipboard answer wasn't readable.") from None

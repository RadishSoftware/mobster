"""The phone's clipboard through WebDriverAgent; reading it is CLI only, as it may hold a password."""

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


IMAGE_LIMIT = 10 * 1024 * 1024


def set_text(request, text, driver=None):
    """Put ``text`` on the phone's clipboard; ``driver`` is noted, so the Allow Paste that follows is Mobster's."""
    request("POST", "/wda/setPasteboard", {"content": encode(text), "contentType": "plaintext"}, 15)
    if driver is not None:
        from ..wda.system_dialogs import note_paste
        note_paste(driver)
    return len(text)


def set_image(request, path):
    """Put the PNG or JPEG at ``path`` on the phone's clipboard. Returns {"name", "bytes"}."""
    from pathlib import Path
    file = Path(path).expanduser()
    try:
        data = file.read_bytes() if file.stat().st_size <= IMAGE_LIMIT else None
    except OSError:
        raise PhoneIOError(f"There is no file at {file}.", "usage") from None
    if data is None:
        raise PhoneIOError(f"{file.name} is over 10 MB; the clipboard takes pictures up to 10 MB from Mobster.",
                           "usage")
    if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")):
        raise PhoneIOError(f"{file.name} isn't a PNG or a JPEG.", "usage")
    request("POST", "/wda/setPasteboard", {"content": base64.b64encode(data).decode("ascii"), "contentType": "image"},
            30)
    return {"name": file.name, "bytes": len(data)}


def get_text(request):
    """The phone's clipboard as text ("" when it holds none)."""
    value = request("POST", "/wda/getPasteboard", {"contentType": "plaintext"}, 20)
    if not value:
        return ""
    try:
        return base64.b64decode(value).decode("utf-8", "replace")
    except (ValueError, TypeError):
        raise PhoneIOError("The phone's clipboard answer wasn't readable.") from None

"""Saving what a task exports or renders into ~/Downloads: a result as text, a GIF, an MP4, a PDF."""

import re
from pathlib import Path

from .api_errors import APIError

EXPORT_MAX_BYTES = 8_000_000
EXPORT_TYPES = {"json", "csv", "yaml", "yml", "md", "txt"}
DOWNLOADS_DENIED = ("Mobster can’t save to Downloads. Allow it in System Settings › Privacy & Security › "
                    "Files and Folders.")


def save_export(body, directory=None):
    """Save a result the dashboard exports into ~/Downloads, never overwriting."""
    if not isinstance(body, dict) or set(body) != {"filename", "text"}:
        raise ValueError("An export needs a filename and text")
    name, text = body["filename"], body["text"]
    if not isinstance(name, str) or not isinstance(text, str) or len(text.encode()) > EXPORT_MAX_BYTES:
        raise ValueError("An export needs a filename and at most 8 MB of text")
    stem, dot, extension = Path(name).name.rpartition(".")
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "-", stem).strip(" .-")[:80]
    if not dot or extension.lower() not in EXPORT_TYPES or not stem:
        raise ValueError("Exports are .json, .csv, .yaml, .md or .txt files")
    return _save_new(directory, stem, extension.lower(), text.encode("utf-8"))


# Files the agent renders itself (a task's GIF and MP4): a name made from the task, never one a request chose.
MEDIA_TYPES = {"gif", "mp4", "pdf", "png", "md"}
def save_export_bytes(stem, extension, data, directory=None):
    """Save rendered media (``extension`` gif or mp4) into ~/Downloads as save_export does: a cleaned name (letters in
    any script, no path separators), never overwriting, a 403 when macOS refuses."""
    if extension not in MEDIA_TYPES or not isinstance(data, (bytes, bytearray)):
        raise ValueError("Only a .gif, .mp4, .pdf, .png or .md is saved this way")
    stem = re.sub(r"[^\w .,'’()&+–-]+", "-", str(stem or "")).strip(" .-")[:80] or "Mobster task"
    return _save_new(directory, stem, extension, bytes(data))


def _save_new(directory, stem, extension, data):
    """Write ``data`` as "{stem}.{extension}" in ``directory`` (default ~/Downloads), or "{stem} (1)…" when that name is
    taken: an existing file is never touched."""
    folder = Path(directory) if directory else Path.home() / "Downloads"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(1000):
            target = folder / (f"{stem}.{extension}" if index == 0 else f"{stem} ({index}).{extension}")
            try:
                with open(target, "xb") as stream:
                    stream.write(data)
                return {"name": target.name, "path": str(target)}
            except FileExistsError:
                continue
    except PermissionError:
        raise APIError(DOWNLOADS_DENIED, 403, "downloads_denied") from None
    raise ValueError("Too many files with that name in Downloads")

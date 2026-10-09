"""What Mobster derives from an attached file once, when it arrives, so a task never waits on it.

- **Images**: ``thumb.jpg`` (at most 480 px, for the app) and ``model.jpg`` (at most 1,024 px, what the agent sees),
  with Pillow (a HEIC photo first becomes a JPEG with ``sips``), turned upright and without the photo's metadata
  (no location); and the text in the picture, read with Vision by the doc-text helper.
- **PDFs**: each page's text (PDFKit, through the doc-text helper; pages without a text layer, such as a scan, are
  read with Vision, up to 20), and the first page as ``thumb.jpg``.
- **CSV**: the header and the first 200 rows; the row count.
- **Text**: the text itself.

The text goes in ``text.txt``, pages separated by a form feed. Nothing here leaves this Mac. A helper that is missing
(no Xcode tools in a source checkout) leaves the file attached with ``textAvailable: false``: the agent is told it
couldn't be read rather than guessing.
"""

import csv
import io
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess

from . import sniff

log = logging.getLogger("mobster.attachments")

THUMB_PX = 480
MODEL_PX = 1024
MAX_PIXELS = 60_000_000
CSV_ROWS = 200
OCR_EMPTY_PAGES = 20
HELPER_TIMEOUT = 90
TEXT_LIMIT = 8_000_000          # characters of text.txt kept (a 25 MB text file is read whole up to here)
PAGE = "\f"


class TooLarge(ValueError):
    code = "too_large"


class Locked(ValueError):
    code = "locked_pdf"


def _helper_runner(argv, timeout):
    """Run the doc-text helper; its one JSON line."""
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    lines = [line for line in (done.stdout or "").splitlines() if line.startswith("{")]
    if done.returncode != 0 or not lines:
        raise RuntimeError(f"doc-text exited {done.returncode}")
    return lines[-1]


# Tests replace these: ``helper`` -> the helper's path (or None), ``run_helper(argv, timeout)`` -> its JSON line.
def helper_path():
    from .. import native_helpers
    found = native_helpers.find("doc-text")
    if found is not None:
        return found
    try:
        return native_helpers.build("doc_text")
    except (LookupError, FileNotFoundError, OSError, subprocess.SubprocessError) as error:
        log.info("doc-text helper unavailable: %s", type(error).__name__)
        return None


run_helper = _helper_runner


def _call_helper(*args):
    path = helper_path()
    if path is None:
        return None
    try:
        answer = json.loads(run_helper([str(path), *map(str, args)], HELPER_TIMEOUT))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        log.warning("doc-text failed: %s", type(error).__name__)
        return None
    return answer if isinstance(answer, dict) else None


def derive(folder, original, file_type):
    """Write the derived files for ``original`` (in ``folder``) and return what they say about it:
    {"pages"?, "rows"?, "width"?, "height"?, "textChars", "textAvailable", "thumb", "model", "ocr"}.
    Raises TooLarge (an image over 60 megapixels) or Locked (a PDF that needs a password)."""
    folder = Path(folder)
    if file_type.kind == "image":
        return _image(folder, original, file_type)
    if file_type.kind == "pdf":
        return _pdf(folder, original)
    text = sniff.decode_text(Path(original).read_bytes()) or ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if file_type.kind == "csv":
        return _csv(folder, text, file_type)
    _write_text(folder, text)
    return {"textChars": min(len(text), TEXT_LIMIT), "textAvailable": True, "thumb": False, "model": False}


def _write_text(folder, text):
    data = text[:TEXT_LIMIT]
    fd = os.open(folder / "text.txt", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(data)


def _csv(folder, text, file_type):
    delimiter = "\t" if file_type.ext == "tsv" else ","
    if file_type.ext == "csv":
        try:
            delimiter = csv.Sniffer().sniff(text[:20_000], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    head = rows[:CSV_ROWS + 1]
    out = io.StringIO()
    writer = csv.writer(out, delimiter=delimiter, lineterminator="\n")
    writer.writerows(head)
    excerpt = out.getvalue()
    if len(rows) > len(head):
        excerpt += f"… {len(rows) - len(head):,} more rows\n"
    _write_text(folder, excerpt)
    return {"rows": max(0, len(rows) - 1), "columns": len(rows[0]) if rows else 0, "textChars": len(excerpt),
            "textAvailable": True, "thumb": False, "model": False}


def _pdf(folder, original):
    answer = _call_helper("pdf", original, folder / "text.txt", "--thumb", folder / "thumb.jpg", "--size", THUMB_PX,
                          "--ocr-empty", OCR_EMPTY_PAGES)
    if answer is None:
        return {"textAvailable": False, "textChars": 0, "thumb": False, "model": False}
    if not answer.get("ok"):
        if answer.get("error") == "locked":
            raise Locked("This PDF is locked with a password. Open it, save a copy without the password, then "
                         "attach that.")
        raise sniff.Unsupported("This PDF couldn't be opened. It may be damaged.")
    text = (folder / "text.txt").read_text(errors="replace") if (folder / "text.txt").is_file() else ""
    if len(text) > TEXT_LIMIT:
        _write_text(folder, text)
    return {"pages": int(answer.get("pages") or 0), "textChars": min(len(text), TEXT_LIMIT),
            "textAvailable": bool(text.replace(PAGE, "").strip()), "thumb": (folder / "thumb.jpg").is_file(),
            "model": False, "ocrPages": int(answer.get("ocrPages") or 0)}


def _image(folder, original, file_type):
    info = {"thumb": False, "model": False, "textAvailable": False, "textChars": 0}
    if file_type.mime == "image/heic":
        info.update(_sips(original, folder))
    else:
        info.update(_pillow(original, folder))
    answer = _call_helper("ocr", original, folder / "text.txt")
    if answer is not None and answer.get("ok") and (folder / "text.txt").is_file():
        text = (folder / "text.txt").read_text(errors="replace")
        info.update(textChars=len(text), textAvailable=bool(text.strip()), ocr=True)
    return info


def _pillow(original, folder):
    from PIL import Image, ImageOps
    try:
        with Image.open(original) as image:
            width, height = image.size
            if width * height > MAX_PIXELS:
                raise TooLarge(f"This image is {width:,} × {height:,} pixels. Mobster reads images up to 60 "
                               "megapixels.")
            image.seek(0)
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            if image.mode in ("RGBA", "LA", "P"):
                rgba = image.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.split()[-1])
                image = flat
            else:
                image = image.convert("RGB")
            for name, size, quality in (("model.jpg", MODEL_PX, 85), ("thumb.jpg", THUMB_PX, 80)):
                copy = image.copy()
                copy.thumbnail((size, size))
                _save_jpeg(copy, folder / name, quality)
    except TooLarge:
        raise
    except Exception as error:  # noqa: BLE001 -- a damaged image is refused with a sentence
        raise sniff.Unsupported(f"This image couldn't be opened ({type(error).__name__}). It may be damaged.") from None
    return {"width": width, "height": height, "thumb": True, "model": True}


def _save_jpeg(image, path, quality):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        image.save(handle, "JPEG", quality=quality, optimize=True)


def sips_path():
    return shutil.which("sips") or ("/usr/bin/sips" if Path("/usr/bin/sips").is_file() else None)


def run_sips(argv, timeout=60):
    return subprocess.run(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL).returncode


def _sips(original, folder):
    """HEIC through macOS's own image tool (Pillow can't read HEIC): one full-size JPEG, then Pillow makes the two
    pictures from it as from any JPEG. sips keeps the photo's metadata, its location included, and leaves the
    pixels unturned with an orientation tag; Pillow turns them upright and writes no metadata, so a photo's
    location never goes to the AI account with the picture. The full-size JPEG is deleted."""
    tool = sips_path()
    if tool is None:
        return {}
    converted = folder / "heic-converted.jpg"
    try:
        try:
            code = run_sips([tool, "-s", "format", "jpeg", "-s", "formatOptions", "92", str(original),
                             "--out", str(converted)])
        except (OSError, subprocess.SubprocessError):
            code = 1
        if code != 0 or converted.is_symlink() or not converted.is_file():
            return {}
        return _pillow(converted, folder)
    finally:
        try:
            converted.unlink()
        except FileNotFoundError:
            pass


def pages_of(text):
    """The pages of ``text.txt``: a PDF's pages (form feeds), else one page."""
    return text.split(PAGE) if PAGE in text else [text]

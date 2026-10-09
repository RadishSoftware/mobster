"""What a file is, from its first bytes, never from its name: the attachment allowlist.

Mobster takes PNG, JPEG, HEIC, GIF, WebP, PDF, and UTF-8 text (plain, Markdown, JSON) or CSV. Anything else is
refused with one sentence, whatever its name says: a program renamed ``menu.pdf`` is still a program. Text is text
only when it decodes as UTF-8 and holds no NUL and almost no control characters; its name then says which text it is
(``.csv`` is CSV, ``.json`` that parses is JSON, ``.md`` is Markdown).

``clean_name`` is the one rule for a file's name, here and on the phone: no folders, no ``..``, no control or
direction-override characters, at most 120 characters with the extension kept.
"""

from dataclasses import dataclass
import json
import os
import re
import unicodedata

KINDS = ("image", "pdf", "text", "csv")
NAME_LIMIT = 120
# Characters a name never keeps: C0/C1 controls, and the invisible marks that make a name read differently.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]")
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1", b"avif"}
TEXT_EXTENSIONS = {".txt", ".text", ".md", ".markdown", ".json", ".csv", ".tsv", ".log", ""}


class Unsupported(ValueError):
    """The file isn't one Mobster reads. ``code``: unsupported_type."""

    code = "unsupported_type"


@dataclass(frozen=True)
class FileType:
    mime: str
    kind: str        # image | pdf | text | csv
    ext: str         # the extension Mobster stores it under (no dot)
    label: str       # a word for people: "PDF", "PNG image", "CSV"


IMAGE_TYPES = {
    "image/png": FileType("image/png", "image", "png", "PNG image"),
    "image/jpeg": FileType("image/jpeg", "image", "jpg", "JPEG image"),
    "image/gif": FileType("image/gif", "image", "gif", "GIF image"),
    "image/webp": FileType("image/webp", "image", "webp", "WebP image"),
    "image/heic": FileType("image/heic", "image", "heic", "HEIC photo"),
}
PDF = FileType("application/pdf", "pdf", "pdf", "PDF")
PLAIN = FileType("text/plain", "text", "txt", "Text")
MARKDOWN = FileType("text/markdown", "text", "md", "Markdown")
JSON_TYPE = FileType("application/json", "text", "json", "JSON")
CSV = FileType("text/csv", "csv", "csv", "CSV")
TSV = FileType("text/tab-separated-values", "csv", "tsv", "TSV")

UNSUPPORTED = "Mobster reads images, PDFs, text and CSV files. This file is none of those."


def image_type(head):
    """The image type ``head`` (a file's first bytes) starts with, or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return IMAGE_TYPES["image/png"]
    if head.startswith(b"\xff\xd8\xff"):
        return IMAGE_TYPES["image/jpeg"]
    if head.startswith((b"GIF87a", b"GIF89a")):
        return IMAGE_TYPES["image/gif"]
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return IMAGE_TYPES["image/webp"]
    if head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return IMAGE_TYPES["image/heic"]
    return None


def is_pdf(head):
    """A PDF's header ``%PDF-`` within its first 1 KB (some writers put junk before it, as readers allow)."""
    return b"%PDF-" in head[:1024]


def decode_text(data):
    """``data`` as text, or None when it isn't UTF-8 text: it must decode, hold no NUL, and at most 1 in 1,000 of
    its characters may be a control character other than tab, newline, carriage return or form feed."""
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.startswith("﻿"):
        text = text[1:]
    controls = sum(1 for ch in text if (ord(ch) < 32 and ch not in "\t\n\r\f") or ord(ch) == 127)
    if controls > max(1, len(text) // 1000):
        return None
    return text


def sniff(data, name=""):
    """The FileType of ``data`` (the whole file), or Unsupported. The name only says which text a text file is."""
    head = bytes(data[:4096])
    found = image_type(head)
    if found is not None:
        return found
    if is_pdf(head):
        return PDF
    text = decode_text(bytes(data))
    if text is None:
        raise Unsupported(UNSUPPORTED)
    ext = os.path.splitext(str(name or "").lower())[1]
    if ext == ".csv":
        return CSV
    if ext == ".tsv":
        return TSV
    if ext == ".json":
        try:
            json.loads(text)
            return JSON_TYPE
        except ValueError:
            return PLAIN
    if ext in (".md", ".markdown"):
        return MARKDOWN
    return PLAIN


def clean_name(name, fallback="file"):
    """A file name that is safe to show and to write anywhere: the last path part only, Unicode NFC, without
    control or direction-override characters, spaces collapsed, never "", "." or "..", never starting with "-" or
    ".", at most NAME_LIMIT characters with its extension kept."""
    text = unicodedata.normalize("NFC", str(name or ""))
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = _UNSAFE.sub("", text)
    text = " ".join(text.split()).strip(" .")
    text = text.lstrip("-").strip()
    if not text or text in (".", ".."):
        text = fallback
    if len(text) > NAME_LIMIT:
        stem, ext = os.path.splitext(text)
        ext = ext if len(ext) <= 12 else ""
        text = stem[:NAME_LIMIT - len(ext)].rstrip(" .") + ext
    return text


def with_extension(name, file_type):
    """``name`` ending in an extension that matches ``file_type`` (``scan`` + PDF -> ``scan.pdf``); a name that
    already has a fitting one keeps it (``photo.jpeg`` stays)."""
    stem, ext = os.path.splitext(name)
    ext = ext.lower().lstrip(".")
    fitting = {"jpg": {"jpg", "jpeg"}, "heic": {"heic", "heif"}, "txt": {"txt", "text", "log", ""},
               "md": {"md", "markdown"}, "csv": {"csv"}, "tsv": {"tsv"}, "json": {"json"}}
    if ext == file_type.ext or ext in fitting.get(file_type.ext, set()):
        return name if ext or file_type.kind not in ("image", "pdf") else f"{name}.{file_type.ext}"
    if file_type.kind == "text" and file_type.ext == "txt":
        return name  # a .log or .yaml file keeps its name; it is read as plain text
    return clean_name(f"{name}.{file_type.ext}")


def size_words(count):
    """``412 KB``, ``3.4 MB``: a file's size for people (1 KB = 1,000 bytes, as Finder counts)."""
    count = int(count or 0)
    if count < 1000:
        return f"{count} byte" + ("" if count == 1 else "s")
    for unit, step in (("KB", 1e3), ("MB", 1e6), ("GB", 1e9)):
        value = count / step
        if value < 999.5 or unit == "GB":
            return f"{value:.0f} {unit}" if value >= 10 or unit == "KB" else f"{value:.1f} {unit}"
    return f"{count} bytes"

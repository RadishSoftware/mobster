"""What makes two feed items the same item (DESIGN.md § 7.2)."""

import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass, field

# Bits two first frames may differ by and still be one item (DESIGN.md § 7.2).
REPEAT_DISTANCE = 6
# Bits two frames of one item may differ by and still be the same picture: a still video (DESIGN.md § 7.1).
STILL_DISTANCE = 3
CAPTION_CHARS = 120
_PUNCT = re.compile(r"[^\w\s@#]+", re.UNICODE)


def fold(text):
    """``text`` for comparison: NFKC, case-folded, punctuation and emoji gone, whitespace collapsed."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "S")   # emoji, symbols
    return " ".join(_PUNCT.sub(" ", text).split())


def text_identity(creator=None, caption=None, audio=None):
    """The item's text identity, or None when neither a creator nor a caption was read."""
    who, said = fold(creator).lstrip("@"), fold(caption)[:CAPTION_CHARS]
    if not who and not said:
        return None
    key = f"{who}|{said}|{fold(audio)}"
    return "t:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def dhash(image, size=8):
    """The 64-bit difference hash of ``image`` (JPEG or PNG bytes, or a PIL image), or None when it can't be read."""
    try:
        from PIL import Image
        picture = image if hasattr(image, "convert") else Image.open(io.BytesIO(bytes(image)))
        small = picture.convert("L").resize((size + 1, size), Image.BILINEAR)
    except Exception:
        return None
    pixels = small.tobytes()
    value = 0
    for row in range(size):
        for column in range(size):
            left, right = pixels[row * (size + 1) + column], pixels[row * (size + 1) + column + 1]
            value = (value << 1) | (1 if left > right else 0)
    return value


def distance(a, b):
    """Hamming distance between two dHashes; 64 when either is unknown."""
    if a is None or b is None:
        return 64
    return bin(a ^ b).count("1")


def picture_identity(hash_value):
    return None if hash_value is None else f"d:{hash_value:016x}"


@dataclass
class Seen:
    identity: str
    text: str | None
    picture: int | None


@dataclass
class Deduper:
    """The identities of the items a run kept, in order. ``check`` answers (repeat_of identity or None, identity)."""
    kept: list = field(default_factory=list)

    def check(self, text=None, picture=None):
        identity = text or picture_identity(picture)
        for seen in self.kept:
            if text and seen.text == text:
                return seen.identity, identity
            if (picture is not None and distance(picture, seen.picture) <= REPEAT_DISTANCE
                    and not (text and seen.text and text != seen.text)):
                return seen.identity, identity
        return None, identity

    def keep(self, identity, text=None, picture=None):
        if identity:
            self.kept.append(Seen(identity, text, picture))

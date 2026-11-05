"""Frames for the extractor and the report (DESIGN.md § 7.1, § 7.5)."""

import base64
import io

TILE_WIDTH = 384
TILE_QUALITY = 70
KEY_WIDTH = 384
KEY_QUALITY = 75
MAX_TILED = 3


def as_bytes(data):
    """``data`` (bytes, a data URL or base64 text) as bytes, or None."""
    if data is None:
        return None
    if isinstance(data, (bytes, bytearray)):
        return bytes(data) or None
    if isinstance(data, str):
        try:
            return base64.b64decode(data.split(",", 1)[1] if data.startswith("data:") else data) or None
        except (ValueError, IndexError):
            return None
    return None


def _open(data):
    from PIL import Image
    picture = Image.open(io.BytesIO(data))
    picture.load()
    return picture.convert("RGB")


def _scaled(picture, width):
    from PIL import Image
    if picture.width == width:
        return picture
    height = max(1, round(picture.height * width / picture.width))
    return picture.resize((width, height), Image.LANCZOS)


def tile(frames, width=None, quality=TILE_QUALITY):
    """JPEG bytes of ``frames`` (at most MAX_TILED) side by side, each ``width`` (TILE_WIDTH) px wide."""
    width = width or TILE_WIDTH
    frames = [frame for frame in (as_bytes(f) for f in frames or ()) if frame][:MAX_TILED]
    if not frames:
        return None
    try:
        from PIL import Image
        pictures = [_scaled(_open(frame), width) for frame in frames]
    except Exception:
        return frames[0]
    height = max(p.height for p in pictures)
    sheet = Image.new("RGB", (width * len(pictures) + 4 * (len(pictures) - 1), height), (0, 0, 0))
    for index, picture in enumerate(pictures):
        sheet.paste(picture, (index * (width + 4), 0))
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()


def key_frame(frame, width=KEY_WIDTH, quality=KEY_QUALITY):
    """``frame`` as a JPEG ``width`` px wide for the report and the app, or None."""
    data = as_bytes(frame)
    if not data:
        return None
    try:
        picture = _scaled(_open(data), width)
    except Exception:
        return data
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()


def data_url(jpeg):
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


def image_part(jpeg):
    """A Chat-style image part (AnthropicChat sends it as a base64 image block)."""
    return {"type": "image_url", "image_url": {"url": data_url(jpeg), "detail": "low"}}

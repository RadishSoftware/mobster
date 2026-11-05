"""A short recipe clip as the phone shows it; fixtures/watch/ holds what the real helpers read of it."""

import io
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "watch"
SIZE = (390, 844)
SCENES = [((214, 92, 52), "Step 1: brown the butter"), ((56, 120, 200), "Step 2: whisk in two eggs"),
          ((70, 160, 90), "Step 3: fold in the flour"), ((150, 70, 160), "Bake 12 minutes at 180")]


def frame(scene, quality=80):
    from PIL import Image, ImageDraw, ImageFont
    colour, caption = SCENES[scene]
    image = Image.new("RGB", SIZE, colour)
    draw = ImageDraw.Draw(image)
    big, small = ImageFont.load_default(size=26), ImageFont.load_default(size=20)
    draw.text((20, 12), "9:41", fill="white", font=small)                       # the status bar
    draw.text((110, 60), "Following    For You", fill="white", font=small)      # the app, the whole time
    for index in range(scene + 2):                                              # the picture itself
        draw.ellipse((40 + 60 * index, 260 + 40 * scene, 120 + 60 * index, 340 + 40 * scene), fill=(250, 240, 220))
    draw.rectangle((20, 600, 370, 650), fill=(0, 0, 0))
    draw.text((32, 610), caption, fill="white", font=big)                       # the caption
    draw.text((20, 760), "@chef.mo", fill="white", font=small)                  # the app, the whole time
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality)
    return out.getvalue()


def lines():
    """doc-text lines' answer for frame(0) to frame(3), as recorded."""
    return json.loads((FIXTURES / "lines.json").read_text())


def heard():
    """phone-audio's answer for the spoken clip, as recorded (a listen answer: transcribe's plus its timings)."""
    return json.loads((FIXTURES / "heard.json").read_text())


def sounds():
    """phone-audio classify's answer for the narration over a music bed, as recorded."""
    return json.loads((FIXTURES / "sounds.json").read_text())

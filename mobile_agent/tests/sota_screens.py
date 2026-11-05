"""Pictures of sota_world's screens, each element drawn inside its own accessibility frame."""

import hashlib
import io
import os
import random

from PIL import Image, ImageDraw, ImageFont

W, H, K = 390, 844, 2            # points, and pixels per point
BLUE, GRAY_BUBBLE, SEPARATOR, GROUPED = (0, 122, 255), (233, 233, 235), (224, 224, 229), (242, 242, 247)
SECONDARY, TERTIARY, GREEN, YELLOW = (60, 60, 67), (142, 142, 147), (52, 199, 89), (229, 168, 0)
AVATARS = [((255, 159, 10), (255, 99, 71)), ((100, 210, 255), (10, 132, 255)), ((191, 90, 242), (94, 92, 230)),
           ((48, 209, 88), (0, 168, 140))]
SETTINGS_ICONS = {"Wi-Fi": (0, 122, 255), "Bluetooth": (0, 122, 255), "General": (142, 142, 147),
                  "Display & Brightness": (0, 122, 255), "About": (142, 142, 147),
                  "Software Update": (142, 142, 147)}
_FONTS = {}


def font(size, weight=400):
    key = (size, weight)
    if key not in _FONTS:
        loaded = None
        if os.path.exists("/System/Library/Fonts/SFNS.ttf"):
            try:
                loaded = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", size * K)
                values = []
                for axis in loaded.get_variation_axes():
                    name = axis["name"].decode() if isinstance(axis["name"], bytes) else axis["name"]
                    values.append(weight if name == "Weight" else max(axis["minimum"], min(axis["maximum"], size))
                                  if name == "Optical Size" else axis["default"])
                loaded.set_variation_by_axes(values)
            except (OSError, AttributeError):
                loaded = None
        if loaded is None:
            try:
                loaded = ImageFont.load_default(size=size * K)
            except TypeError:
                loaded = ImageFont.load_default()
        _FONTS[key] = loaded
    return _FONTS[key]


def box(rect):
    x, y, w, h = rect
    return x * W * K, y * H * K, (x + w) * W * K, (y + h) * H * K


def p(value):
    return value * K


def lw(value):
    return max(1, round(value * K))


def fit(draw, text, size, weight, width):
    face = font(size, weight)
    if face.getlength(text) <= width:
        return text
    while text and face.getlength(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


def status_bar(draw, dark=False):
    ink = (255, 255, 255) if dark else (0, 0, 0)
    draw.text((p(52), p(26)), "9:41", font=font(17, 600), fill=ink, anchor="mm")
    x = p(W - 36)
    draw.rounded_rectangle((x - p(13), p(20), x + p(12), p(32)), p(3.5), outline=ink, width=lw(1))
    draw.rounded_rectangle((x - p(11), p(22), x + p(8), p(30)), p(2), fill=ink)
    draw.rectangle((x + p(13), p(24), x + p(14.5), p(28)), fill=ink)
    for i in range(4):
        bx = p(W - 98 + i * 6)
        draw.rounded_rectangle((bx, p(31 - 3 - i * 2.5), bx + p(4), p(31)), p(1), fill=ink)
    cx, cy = p(W - 64), p(31)
    for r in (11, 7, 3):
        draw.pieslice((cx - p(r), cy - p(r), cx + p(r), cy + p(r)), 225, 315, fill=ink)
        if r > 3:
            draw.pieslice((cx - p(r - 2), cy - p(r - 2), cx + p(r - 2), cy + p(r - 2)), 225, 315,
                          fill=(255, 255, 255) if not dark else (0, 0, 0))


def avatar(image, draw, center, radius, name, index):
    top, bottom = AVATARS[index % len(AVATARS)]
    disc = Image.new("RGB", (2 * radius, 2 * radius), top)
    shade = Image.linear_gradient("L").resize(disc.size)
    disc.paste(Image.new("RGB", disc.size, bottom), (0, 0), shade)
    mask = Image.new("L", disc.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, disc.width - 1, disc.height - 1), fill=255)
    image.paste(disc, (int(center[0] - radius), int(center[1] - radius)), mask)
    initials = "".join(part[0] for part in name.split()[:2]).upper()
    draw.text(center, initials, font=font(int(radius / K * 0.8), 600), fill=(255, 255, 255), anchor="mm")


def app_icon(image, draw, rect, name):
    x0, y0, x1, _ = box(rect)
    side = min(x1 - x0, p(62))
    cx = (x0 + x1) / 2
    left, top = int(cx - side / 2), int(y0)
    colors = {"Messages": ((94, 236, 112), (40, 196, 74)), "Notes": ((255, 255, 255), (245, 245, 245)),
              "Maps": ((180, 230, 160), (120, 200, 120)), "Settings": ((170, 170, 178), (120, 120, 128))}
    top_color, bottom_color = colors.get(name, ((200, 200, 200), (160, 160, 160)))
    tile = Image.new("RGB", (int(side), int(side)), top_color)
    tile.paste(Image.new("RGB", tile.size, bottom_color), (0, 0), Image.linear_gradient("L").resize(tile.size))
    glyph = ImageDraw.Draw(tile)
    s = side / 62
    if name == "Messages":
        glyph.ellipse((12 * s, 14 * s, 50 * s, 44 * s), fill=(255, 255, 255))
        glyph.polygon([(18 * s, 38 * s), (14 * s, 50 * s), (28 * s, 42 * s)], fill=(255, 255, 255))
    elif name == "Notes":
        glyph.rectangle((0, 0, side, 16 * s), fill=(255, 204, 0))
        for row in range(4):
            glyph.line((10 * s, (26 + row * 8) * s, 52 * s, (26 + row * 8) * s), fill=(220, 220, 220), width=int(2 * s))
    elif name == "Maps":
        glyph.line((0, 40 * s, 62 * s, 22 * s), fill=(255, 255, 255), width=int(6 * s))
        glyph.line((30 * s, 0, 36 * s, 62 * s), fill=(255, 214, 10), width=int(5 * s))
        glyph.ellipse((22 * s, 18 * s, 38 * s, 34 * s), fill=(10, 132, 255))
    elif name == "Settings":
        glyph.ellipse((12 * s, 12 * s, 50 * s, 50 * s), outline=(70, 70, 76), width=int(6 * s))
        glyph.ellipse((24 * s, 24 * s, 38 * s, 38 * s), fill=(70, 70, 76))
    mask = Image.new("L", tile.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, tile.width - 1, tile.height - 1), int(14 * s), fill=255)
    image.paste(tile, (left, top), mask)
    draw.text((cx, top + side + p(13)), name, font=font(12, 500), fill=(255, 255, 255), anchor="mm")


_GRADIENTS = {}


def _gradient(size):
    """Image.linear_gradient("L") at ``size``, made once per size."""
    if size not in _GRADIENTS:
        _GRADIENTS[size] = Image.linear_gradient("L").resize(size)
    return _GRADIENTS[size]


def _reel(snapshot):
    """The reel a Reels viewer shows (longform_world.FeedItem), or None."""
    if snapshot.bundle_id != "com.burbn.instagram":
        return None
    cell = next((e for e in snapshot.elements if e.role == "Cell" and e.label.startswith("Reel by @")
                 and e.rect[2] * e.rect[3] > .9), None)
    if cell is None:
        return None
    from mobile_agent.tests import longform_world
    return longform_world.item_by_handle(cell.label.removeprefix("Reel by @"))


def _centered(draw, y, text, size, weight, fill, width=W * K - p(48)):
    text = fit(draw, text, size, weight, width)
    draw.text((W * K / 2, y), text, font=font(size, weight), fill=fill, anchor="mm")


def draw_reel(image, draw, item):
    """The video frame of ``item``: a background of its own (so pictures of different reels differ), the title card,
    the key stat and the call to action. The background is tiles on the 9 by 8 grid identity.dhash reads, so two reels
    stay far apart whatever Pillow's resampling rounds (13.0 took two gradient backgrounds from 7 bits apart to 6)."""
    seed = int(hashlib.sha1(item.key.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    width, height = image.size
    for row in range(8):
        for column in range(9):
            draw.rectangle((column * width // 9, row * height // 8, (column + 1) * width // 9, (row + 1) * height // 8),
                           fill=tuple(rng.randint(30, 225) for _ in range(3)))
    for _ in range(4):
        r = p(rng.randint(40, 120))
        cx, cy = p(rng.randint(0, W)), p(rng.randint(80, H - 200))
        shade = rng.choice([(235, 235, 235), (15, 15, 15), tuple(rng.randint(0, 255) for _ in range(3))])
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=shade)
    if item.kind == "ad":
        draw.rounded_rectangle((p(70), p(300), p(W - 70), p(470)), p(24), fill=(250, 250, 250))
        _centered(draw, p(360), item.title, 34, 800, (20, 20, 20))
        _centered(draw, p(410), item.stat, 20, 600, (90, 90, 90))
        return
    draw.rounded_rectangle((p(24), p(250), p(W - 24), p(330)), p(14), fill=(0, 0, 0))
    _centered(draw, p(290), item.title, 30, 800, (255, 255, 255))
    draw.rounded_rectangle((p(40), p(345), p(W - 40), p(395)), p(12), fill=(255, 214, 10))
    _centered(draw, p(370), item.stat, 19, 700, (20, 20, 20))
    draw.rounded_rectangle((p(90), p(560), p(W - 90), p(600)), p(20), fill=(255, 255, 255))
    _centered(draw, p(580), item.cta, 15, 600, (20, 20, 20))


def draw_dark_overlay(draw, e):
    x0, y0, x1, y1 = box(e.rect)
    label = e.value or e.label
    if e.role == "Alert" or e.role == "Sheet":
        return
    size, weight = (14, 600) if label.startswith("@") or e.role == "Button" else (14, 400)
    if label == "Sponsored":
        size, weight = 12, 600
    text = fit(draw, label, size, weight, x1 - x0 + p(8))
    draw.text((x0 + p(1), (y0 + y1) / 2 + p(1)), text, font=font(size, weight), fill=(0, 0, 0), anchor="lm")
    draw.text((x0, (y0 + y1) / 2), text, font=font(size, weight), fill=(255, 255, 255), anchor="lm")


def draw_panel(draw, e):
    x0, y0, x1, y1 = box(e.rect)
    draw.rounded_rectangle((x0, y0, x1, y1), p(14), fill=(250, 250, 250), outline=SEPARATOR, width=lw(1))
    lines, line = [], ""
    for word in e.label.split():
        if font(15, 600).getlength((line + " " + word).strip()) > x1 - x0 - p(24):
            lines.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    lines.append(line)
    for index, text in enumerate(lines[:3]):
        draw.text(((x0 + x1) / 2, y0 + p(22) + index * p(20)), text, font=font(15, 600), fill=(0, 0, 0), anchor="mm")


def draw_image(draw, e):
    x0, y0, x1, y1 = box(e.rect)
    draw.rounded_rectangle((x0, y0, x1, y1), p(14), fill=(214, 220, 228))
    w, h = x1 - x0, y1 - y0
    draw.polygon([(x0 + w * .15, y1 - h * .2), (x0 + w * .45, y0 + h * .35), (x0 + w * .75, y1 - h * .2)],
                 fill=(160, 172, 186))
    draw.ellipse((x0 + w * .68, y0 + h * .18, x0 + w * .8, y0 + h * .18 + w * .12), fill=(250, 220, 120))


def render(snapshot, typed=""):
    """PNG bytes (780 × 1688) of ``snapshot``, a ScriptedPhone screen."""
    out = io.BytesIO()
    draw_screen(snapshot).save(out, "PNG")
    return out.getvalue()


def draw_screen(snapshot):
    """``snapshot``, a ScriptedPhone screen, drawn (a 780 × 1688 RGB image)."""
    app = snapshot.bundle_id or ""
    home = app == "com.apple.springboard"
    grouped = app == "com.apple.Preferences"
    image = Image.new("RGB", (W * K, H * K), GROUPED if grouped else (255, 255, 255))
    draw = ImageDraw.Draw(image)
    reel = _reel(snapshot)
    if reel is not None:
        draw_reel(image, draw, reel)
        status_bar(draw, dark=True)
        for e in snapshot.elements:
            if e.role in ("Alert", "Sheet"):
                draw_panel(draw, e)
            elif e.role != "Cell":
                draw_dark_overlay(draw, e)
        draw.rounded_rectangle((p(W / 2 - 67), p(H - 13), p(W / 2 + 67), p(H - 8)), p(3), fill=(255, 255, 255))
        return image
    if home:
        top, bottom = (126, 104, 255), (255, 145, 120)
        image.paste(Image.new("RGB", image.size, top))
        image.paste(Image.new("RGB", image.size, bottom), (0, 0), _gradient(image.size))
    if app == "com.apple.Maps":
        image.paste((236, 232, 222), (0, 0, W * K, H * K))
        for y in range(0, H * K, p(90)):
            draw.line((0, y, W * K, y + p(40)), fill=(255, 255, 255), width=lw(9))
        for x in range(p(40), W * K, p(120)):
            draw.line((x, 0, x - p(30), H * K), fill=(255, 255, 255), width=lw(7))
        draw.rectangle((p(200), p(250), p(330), p(360)), fill=(198, 226, 178))
    status_bar(draw, dark=home)
    elements = list(snapshot.elements)
    chat = app == "com.apple.MobileSMS" and any(e.role == "Button" and e.label == "Back" for e in elements)
    cells = [e for e in elements if e.role == "Cell"]
    for e in elements:
        x0, y0, x1, y1 = box(e.rect)
        label = e.value or e.label
        if e.role == "Icon":
            app_icon(image, draw, e.rect, e.label)
        elif e.role == "Image":
            draw_image(draw, e)
        elif e.role in ("Alert", "Sheet"):
            draw_panel(draw, e)
        elif e.role == "NavigationBar" and chat:
            avatar(image, draw, ((x0 + x1) / 2, y0 + p(14)), p(17), e.label, 0)
            draw.text(((x0 + x1) / 2, y0 + p(42)), e.label, font=font(12, 500), fill=(0, 0, 0), anchor="mm")
            draw.line((0, y1 + p(6), W * K, y1 + p(6)), fill=SEPARATOR, width=1)
        elif e.role == "NavigationBar":
            draw.text((p(16), y1 - p(2)), e.label, font=font(34, 700), fill=(0, 0, 0), anchor="ls")
        elif e.role == "Button" and e.label == "Back":
            draw.line((p(22), y0 + p(4), p(14), (y0 + y1) / 2, p(22), y1 - p(4)), fill=BLUE, width=lw(3), joint="curve")
        elif e.role == "Button" and e.label == "Send":
            r = min(x1 - x0, y1 - y0) / 2
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=BLUE)
            draw.line((cx, cy + r * .5, cx, cy - r * .5), fill=(255, 255, 255), width=lw(2.5))
            draw.line((cx - r * .4, cy - r * .1, cx, cy - r * .5, cx + r * .4, cy - r * .1), fill=(255, 255, 255),
                      width=lw(2.5), joint="curve")
        elif e.role == "Button" and e.label == "New Note":
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            draw.rounded_rectangle((cx - p(11), cy - p(10), cx + p(9), cy + p(10)), p(4), outline=YELLOW, width=lw(2))
            draw.line((cx - p(2), cy + p(2), cx + p(12), cy - p(12)), fill=YELLOW, width=lw(2.5))
        elif e.role == "Button":
            color = YELLOW if app == "com.apple.mobilenotes" else BLUE
            draw.text((max(x0, p(16)), (y0 + y1) / 2), e.label, font=font(17, 400 if e.label != "Done" else 600),
                      fill=color, anchor="lm")
        elif e.role == "SearchField":
            draw.rounded_rectangle((x0, y0, x1, y1), p(11), fill=(238, 238, 240))
            cx, cy = x0 + p(16), (y0 + y1) / 2
            draw.ellipse((cx - p(6), cy - p(7), cx + p(5), cy + p(4)), outline=TERTIARY, width=lw(2))
            draw.line((cx + p(4), cy + p(3), cx + p(8), cy + p(7)), fill=TERTIARY, width=lw(2))
            value = e.value if e.value and e.value != e.label else ""
            draw.text((x0 + p(30), cy), value or e.label, font=font(17), fill=(0, 0, 0) if value else TERTIARY,
                      anchor="lm")
        elif e.role == "TextField":
            draw.rounded_rectangle((x0, y0, x1, y1), (y1 - y0) / 2, outline=(200, 200, 205), width=lw(1))
            cx, cy = p(24), (y0 + y1) / 2
            draw.ellipse((cx - p(15), cy - p(15), cx + p(15), cy + p(15)), fill=(233, 233, 235))
            draw.line((cx - p(7), cy, cx + p(7), cy), fill=TERTIARY, width=lw(2))
            draw.line((cx, cy - p(7), cx, cy + p(7)), fill=TERTIARY, width=lw(2))
            text = fit(draw, e.value, 17, 400, x1 - x0 - p(28)) if e.value else e.label
            draw.text((x0 + p(14), cy), text, font=font(17), fill=(0, 0, 0) if e.value else TERTIARY, anchor="lm")
        elif e.role == "TextView":
            lines = (e.value or "").split("\n") or [""]
            y = y0 + p(10)
            if not e.value:
                draw.line((x0 + p(4), y, x0 + p(4), y + p(26)), fill=YELLOW, width=lw(2))
            for index, line in enumerate(lines[:12]):
                face = font(28 if index == 0 else 17, 700 if index == 0 else 400)
                draw.text((x0 + p(4), y), fit(draw, line, 28 if index == 0 else 17, 700 if index == 0 else 400,
                                                x1 - x0 - p(8)), font=face, fill=(0, 0, 0))
                y += p(38 if index == 0 else 24)
        elif e.role == "Cell":
            index = cells.index(e)
            if grouped:
                first, last = index == 0, index == len(cells) - 1
                left, right = p(16), W * K - p(16)
                draw.rounded_rectangle((left, y0, right, y1), p(12) if first or last else 0, fill=(255, 255, 255),
                                       corners=(first, first, last, last))
                color = SETTINGS_ICONS.get(e.label, (142, 142, 147))
                iy = (y0 + y1) / 2
                draw.rounded_rectangle((left + p(14), iy - p(15), left + p(44), iy + p(15)), p(7), fill=color)
                draw.text((left + p(58), iy), e.label, font=font(17), fill=(0, 0, 0), anchor="lm")
                draw.line((right - p(22), iy - p(6), right - p(16), iy, right - p(22), iy + p(6)), fill=(196, 196, 199),
                          width=lw(2), joint="curve")
                if not last:
                    draw.line((left + p(58), y1, right, y1), fill=SEPARATOR, width=1)
            elif app == "com.apple.MobileSMS":
                avatar(image, draw, (p(42), (y0 + y1) / 2), p(24), e.label, index)
                draw.text((p(78), y0 + p(12)), e.label, font=font(17, 600), fill=(0, 0, 0), anchor="la")
                when = ("9:12 AM", "Yesterday", "Tuesday", "Monday")[index % 4]
                draw.text((W * K - p(30), y0 + p(14)), when, font=font(15), fill=TERTIARY, anchor="ra")
                draw.line((W * K - p(20), y0 + p(17), W * K - p(16), y0 + p(21), W * K - p(20), y0 + p(25)),
                          fill=(196, 196, 199), width=lw(2))
                draw.line((p(78), y1, W * K, y1), fill=SEPARATOR, width=1)
            elif app == "com.apple.Maps":
                draw.rounded_rectangle((p(12), y0 - p(8), W * K - p(12), y1 + p(40)), p(16), fill=(255, 255, 255))
                draw.text((p(28), y0 + p(10)), e.label, font=font(20, 700), fill=(0, 0, 0), anchor="la")
            else:
                draw.text((p(20), y0 + p(10)), e.label, font=font(17, 600), fill=(0, 0, 0), anchor="la")
                draw.text((p(20), y0 + p(34)), "Today", font=font(15), fill=TERTIARY, anchor="la")
                draw.line((p(20), y1, W * K, y1), fill=SEPARATOR, width=1)
        elif e.role == "StaticText" and chat and e.label == "Delivered":
            draw.text((x1, y0), "Delivered", font=font(11, 500), fill=TERTIARY, anchor="ra")
        elif e.role == "StaticText" and chat:
            mine = e.rect[0] > .08
            face = font(16)
            text = fit(draw, label, 16, 400, x1 - x0 - p(28))
            width = face.getlength(text) + p(28)
            height = y1 - y0
            left = x1 - width if mine else x0
            draw.rounded_rectangle((left, y0, left + width, y0 + height), p(18), fill=BLUE if mine else GRAY_BUBBLE)
            draw.text((left + p(14), y0 + height / 2), text, font=face, fill=(255, 255, 255) if mine else (0, 0, 0),
                      anchor="lm")
        elif e.role == "StaticText" and app == "com.apple.MobileSMS":
            draw.text((p(78), y0 + p(2)), fit(draw, label, 15, 400, W * K - p(110)), font=font(15), fill=TERTIARY,
                      anchor="la")
        elif e.role == "StaticText" and app == "com.apple.Maps":
            draw.text((p(28), y0 + p(2)), label, font=font(15, 600), fill=(36, 138, 61), anchor="la")
        elif e.role == "StaticText":
            draw.text((x0, (y0 + y1) / 2), label, font=font(17), fill=TERTIARY if e.rect[0] > .5 else (0, 0, 0),
                      anchor="lm")
    if home:
        draw.rounded_rectangle((p(W / 2 - 67), p(H - 13), p(W / 2 + 67), p(H - 8)), p(3), fill=(255, 255, 255))
    else:
        draw.rounded_rectangle((p(W / 2 - 67), p(H - 13), p(W / 2 + 67), p(H - 8)), p(3), fill=(0, 0, 0))
    return image


_JPEGS = {}


def jpeg(snapshot, side=960):
    """The screen as a JPEG at most ``side`` px tall, as the phone's video stream would send it. The same screen is
    drawn once (the long-form feed asks for two frames of every reel)."""
    key = (snapshot.bundle_id, side, tuple((e.role, e.label, e.value, e.rect) for e in snapshot.elements))
    cached = _JPEGS.get(key)
    if cached is not None:
        return cached
    image = draw_screen(snapshot)
    # LANCZOS, as on main: a short task's screenshot is byte for byte what it was, so the short-task gate compares
    # the engine, not the picture (BILINEAR made the model see a softer Home Screen).
    image.thumbnail((side, side), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    if len(_JPEGS) >= 256:
        _JPEGS.clear()
    _JPEGS[key] = out.getvalue()
    return _JPEGS[key]

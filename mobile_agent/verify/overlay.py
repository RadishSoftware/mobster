from pathlib import Path

from .. import brand as tokens


def _between(light, dark):
    return tuple(round((a + b) / 2) for a, b in zip(tokens.rgb(light), tokens.rgb(dark)))


# The brand's light green and coral hold white text at 4.5:1; outlines sit halfway between light and dark tones.
GREEN, RED = tokens.rgb(tokens.LIGHT["green"]), tokens.rgb(tokens.LIGHT["coral"])
GREEN_LINE = _between(tokens.LIGHT["green"], tokens.DARK["green"])
RED_LINE = _between(tokens.LIGHT["coral"], tokens.DARK["coral"])
SHOWN = tokens.rgb(tokens.LIGHT["violet"])  # violet: every element Mobster read
SHOWN_ALPHA = round(255 * .3)
TINT_ALPHA = round(255 * .10)
FONT_CANDIDATES = ("/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc",
                   "/Library/Fonts/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
FRAME_CSS_WIDTH = 280   # CSS px: the verify report's frame; the suite report's is 264
MAX_PILLS = 6


def _font(size):
    from PIL import ImageFont
    for path in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size)
        except OSError:
            continue
        try:
            font.set_variation_by_name("Bold")  # SF is a variable font; others keep their one weight
        except Exception:
            pass
        return font
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def pill_numbers(items):
    """Which assertions get a pill: ``items`` is [(ok, has_node)] in the check's order."""
    ranked = sorted((index for index, (ok, has_node) in enumerate(items) if has_node),
                    key=lambda index: (bool(items[index][0]), index))
    return set(ranked[:MAX_PILLS])


def draw_overlay(frame_path, tree, asserted, out_path):
    """The accessibility overlay: ``asserted`` is [(AssertionResult, [node paths])] in the check's order."""
    from PIL import Image, ImageDraw, ImageFilter
    with Image.open(frame_path) as source:
        base = source.convert("RGBA")
    width, height = base.size
    scale = width / (tree.size[0] or 1)
    k = width / FRAME_CSS_WIDTH  # image px per CSS px at the report's size
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    def box(rect, grow=0):
        x, y, w, h = rect
        left, top = max(0, x * scale - grow), max(0, y * scale - grow)
        right, bottom = min(width - 1, (x + w) * scale + grow), min(height - 1, (y + h) * scale + grow)
        return (left, top, right, bottom) if right > left and bottom > top else None

    def rounded(area, radius, **style):
        radius = max(0, min(radius, (area[2] - area[0]) / 2, (area[3] - area[1]) / 2))
        draw.rounded_rectangle(area, radius=radius, **style)

    shown = tree.shown()
    for node in shown:
        area = box(node.rect)
        # Not the containers that fill the screen: their edges would only rule lines across it.
        if area and _area(area) < .2 * width * height:
            rounded(area, 3 * k, outline=SHOWN + (SHOWN_ALPHA,), width=max(1, round(.75 * k)))
    grow, stroke = round(2.5 * k), max(2, round(1.5 * k))
    items = []
    for result, paths in asserted:
        nodes = [node for node in (tree.by_path(path) for path in paths) if node]
        areas = sorted({area for area in (box(node.rect, grow) for node in nodes) if area},
                       key=lambda area: (area[1], area[0]))
        # An element inside another the same assertion matched (a switch's own label) is outlined once, outside.
        areas = [area for area in areas if not any(other != area and _contains(other, area) for other in areas)]
        items.append((result, areas))
    # Held first, failed last, so a red outline is never under a green one.
    for result, areas in sorted(items, key=lambda item: item[0].ok, reverse=True):
        line = (GREEN_LINE if result.ok else RED_LINE) + (255,)
        tint = (GREEN_LINE if result.ok else RED_LINE) + (TINT_ALPHA,)
        for area in areas:
            rounded(area, 7 * k, fill=tint, outline=line, width=stroke)
    numbered = sorted(pill_numbers([(result.ok, bool(areas)) for result, areas in items]),
                      key=lambda index: (items[index][0].ok, index))
    asserted_areas = [area for _, areas in items for area in areas]
    edges = base.convert("L").filter(ImageFilter.FIND_EDGES)
    font = _font(max(10, round(12 * k)))
    placed = []
    jobs = [(index, 0) for index in numbered] + [(index, n) for index in numbered if not items[index][0].ok
                                                   for n in range(1, min(3, len(items[index][1])))]
    for index, n in jobs:
        if n and len(placed) >= MAX_PILLS + 2:
            break
        result, areas = items[index]
        _pill(draw, font, index + 1, result.ok, areas[n], (width, height), k, placed,
              [area for area in asserted_areas if area != areas[n]], edges)
    Image.alpha_composite(base, layer).convert("RGB").save(out_path, "JPEG", quality=85)
    return Path(out_path)


def _overlap(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def _area(a):
    return max(0, a[2] - a[0]) * max(0, a[3] - a[1])


def _contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _pill(draw, font, number, ok, area, size, k, placed, others, edges):
    from PIL import ImageStat
    text = str(number)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font, anchor="ls")
    pill_h = round(18 * k)
    pad, mark, gap, ring = round(5.5 * k), round(7.5 * k), round(3 * k), max(1, round(k))
    pill_w = pad + mark + gap + (right - left) + pad
    space = round(1.5 * k)  # outside, the pill touches the outline, so it reads as the outline's own label
    x0, y0, x1, y1 = area
    middle = (y0 + y1) / 2 - pill_h / 2 if y1 - y0 < 2 * pill_h else y0
    inset = round(8 * k)
    candidates = [(x0, y0 - space - pill_h, 0), (x0 - space - pill_w, middle, 0), (x1 + space, middle, 0),
                  (x0, y1 + space, 0), (x1 - pill_w, y0 - space - pill_h, 0), (x1 - pill_w, y1 + space, 0),
                  (x0 + inset, y0 - pill_h / 2, 2), (x1 - inset - pill_w, y0 - pill_h / 2, 2),
                  (x0 + space, y0 + space, 4), (x1 - space - pill_w, y0 + space, 4)]
    best = None
    for order, (x, y, own_cost) in enumerate(candidates):
        if x < ring or y < ring or x + pill_w > size[0] - ring or y + pill_h > size[1] - ring:
            continue
        rect = (x - ring, y - ring, x + pill_w + ring, y + pill_h + ring)
        if any(_overlap(rect, other) for other in placed):
            continue
        whole = _area(rect)
        busy = ImageStat.Stat(edges.crop(tuple(round(v) for v in rect))).mean[0]
        cost = (busy + 60 * sum(_overlap(rect, other) for other in others) / whole
                + own_cost * _overlap(rect, area) / whole + .3 * order)
        if best is None or cost < best[0]:
            best = (cost, x, y)
        if busy < 3 and not any(_overlap(rect, other) for other in others) and own_cost == 0:
            break
    if best is None:  # nowhere free: the top corner inside, clamped to the frame
        x = min(max(ring, x0 + space), size[0] - pill_w - ring)
        y = min(max(ring, y0 + space), size[1] - pill_h - ring)
        for _ in range(12):
            rect = (x - ring, y - ring, x + pill_w + ring, y + pill_h + ring)
            if not any(_overlap(rect, other) for other in placed):
                break
            y = min(y + pill_h + 2 * ring, size[1] - pill_h - ring)
    else:
        _, x, y = best
    placed.append((x - ring, y - ring, x + pill_w + ring, y + pill_h + ring))
    radius = pill_h / 2
    draw.rounded_rectangle((x - ring, y - ring, x + pill_w + ring, y + pill_h + ring), radius=radius + ring,
                           fill=(255, 255, 255, 235))
    draw.rounded_rectangle((x, y, x + pill_w, y + pill_h), radius=radius, fill=(GREEN if ok else RED) + (255,))
    white, line = (255, 255, 255, 255), max(2, round(1.5 * k))
    gx, gy = x + pad, y + (pill_h - mark) / 2
    if ok:
        draw.line([(gx + .1 * mark, gy + .55 * mark), (gx + .4 * mark, gy + .82 * mark),
                   (gx + .92 * mark, gy + .2 * mark)], fill=white, width=line, joint="curve")
    else:
        draw.line([(gx + .12 * mark, gy + .12 * mark), (gx + .88 * mark, gy + .88 * mark)], fill=white, width=line)
        draw.line([(gx + .88 * mark, gy + .12 * mark), (gx + .12 * mark, gy + .88 * mark)], fill=white, width=line)
    draw.text((gx + mark + gap - left, y + pill_h / 2 - (top + bottom) / 2), text, font=font, fill=white,
              anchor="ls")

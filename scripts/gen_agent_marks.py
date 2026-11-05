"""Write every surface's copy of the marks Mobster shows from docs/brand/marks, the one source: the agents, assistants
and AI providers Mobster works with, the services it connects to (connectors, destinations, plugins), and the companies
Network X-ray names.

    python3 scripts/gen_agent_marks.py             write what changed, and remove copies the source no longer has
    python3 scripts/gen_agent_marks.py --check     write nothing; list what differs and exit 1 if anything does
    python3 scripts/gen_agent_marks.py --measure   also measure each mark's box and optical scale into marks.json
                                                   (needs rsvg-convert and Pillow; --check doesn't measure)

docs/brand/marks/marks.json lists each mark: its file in docs/brand/marks/svg/ (the maker's own artwork), whose it is,
where it came from and when, whether it is one colour (`ink`: drawn in the page's ink, so it reads in light and dark) or
an icon that is its own tile (`fill`),
the surfaces that show it, the companies it stands for in Network X-ray (`companies`), and its box and optical scale.
Every surface draws a mark the same way (docs/brand/marks/README.md): on one neutral tile from the tokens, with the app
icon's corner radius and ring. Each copy is the whole tile's
square, the mark centred in a box 9/16 of it at its optical scale, so a solid mark and a thin one read the same size and
a surface only fills the tile with the copy.

What it writes, for the surfaces in this tree (the public tree has none of them):
- each mark on its tile's square (the tile itself is drawn by each surface), nothing else changed:
  dashboard/public/marks/ and relay/public/assets/marks/ (Remote's pages can't draw a mask under their CSP, so a
  one-colour mark there is two files, in the light and the dark ink from docs/brand/tokens.json), with a mark's version
  for dark grounds beside it; site/public/marks/ holds the app's tile copies and site/public/marks-bare/ bare ones, the
  mark's own square at its optical scale;
- the list each surface reads: dashboard/src/lib/marks.gen.ts, relay/src/pages/marks.gen.ts and site/lib/marks.gen.ts,
  with every glyph (a partner's own small-size icon, for a button beside its name: GitHub's octicon) as its paths;
- the page the browser shows when a connector's sign-in comes back (mobile_agent/connectors/callback_page.py) can't load
  a file, so its copies are inside mobile_agent/connectors/marks.py, as Remote's are (two inks for a one-colour mark),
  with every name a mark goes by;
- the docs site's copies, each the mark's bare square: docs/images/marks/ (a mark in its own colours, and its version
  for dark grounds) and docs/images/marks-ink/ (a one-colour mark in the light ink, which the docs' style sheet inverts
  on a dark page);
- the sources table in docs/brand/marks/README.md, from marks.json.

A mark's SVG may hold only shapes, gradients, clip paths, masks, filters and a picture embedded as data: the
generator refuses anything else (a script, a style sheet, an outside link), so a copy is never more than the drawing.
Standard library only, but for --measure.
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "brand" / "marks"
MANIFEST = SOURCE / "marks.json"
TOKENS = ROOT / "docs" / "brand" / "tokens.json"
SVG_NS = "http://www.w3.org/2000/svg"
XLINK = "{http://www.w3.org/1999/xlink}href"
ET.register_namespace("", SVG_NS)

# Every surface a mark may name, and where the copies of the ones this generator writes go, with their list.
NAMES = {"app", "remote", "site", "callback", "docs"}
SURFACES = {
    "app": {"marks": ROOT / "dashboard" / "public" / "marks",
            "list": ROOT / "dashboard" / "src" / "lib" / "marks.gen.ts"},
    "remote": {"marks": ROOT / "relay" / "public" / "assets" / "marks",
               "list": ROOT / "relay" / "src" / "pages" / "marks.gen.ts"},
    # The site draws the tile with the app's own LogoTile (components/ui, generated), so its public/marks are the app's
    # tile copies; a logo beside a name (a table's, a heading's) takes the bare copy in public/marks-bare, the mark's
    # own square at its optical scale, without the tile's margin.
    "site": {"marks": ROOT / "site" / "public" / "marks", "bare": ROOT / "site" / "public" / "marks-bare",
             "list": ROOT / "site" / "lib" / "marks.gen.ts"},
    # The sign-in's page back from the browser: every copy inside one Python module (it loads nothing from outside).
    "callback": {"marks": None, "list": ROOT / "mobile_agent" / "connectors" / "marks.py"},
}
# The docs site (Mintlify) draws a page's or a card's icon as an <img>, so each copy is the mark's bare square: a
# one-colour mark in the light ink in marks-ink/, which docs/style.css inverts on a dark page into the dark ink, and any
# other mark as it is in marks/, with its version for dark grounds beside it when it has one.
DOCS_MARKS = ROOT / "docs" / "images" / "marks"
DOCS_INK = ROOT / "docs" / "images" / "marks-ink"
HEADER = (
    "Generated by scripts/gen_agent_marks.py from docs/brand/marks/marks.json. Don't edit: "
    "change the source and run it.")

# The mark's box, as a share of the tile, before its optical scale.
BOX = 9 / 16
# The only elements and attributes a mark may have: shapes and their paint.
ELEMENTS = {"svg", "g", "path", "rect", "circle", "ellipse", "polygon", "polyline", "line", "defs", "linearGradient",
            "radialGradient", "stop", "clipPath", "mask", "use", "filter", "feGaussianBlur", "feOffset", "feBlend",
            "feFlood", "feComposite", "feColorMatrix", "feMerge", "feMergeNode", "image"}
# A picture inside a mark (Gemini's gradient is one) only as data in the file itself.
PICTURE = re.compile(r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=\s]+")
ATTRIBUTES = {
    "id", "d", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "fx", "fy", "width", "height", "points",
    "viewBox", "transform", "fill", "fill-rule", "fill-opacity", "stroke", "stroke-width", "stroke-linecap",
    "stroke-linejoin", "stroke-miterlimit", "stroke-opacity", "opacity", "clip-path", "clip-rule", "mask", "offset",
    "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "spreadMethod", "clipPathUnits", "maskUnits",
    "maskContentUnits", "href", "style", "filter", "filterUnits", "primitiveUnits", "color-interpolation-filters",
    "stdDeviation", "dx", "dy", "in", "in2", "result", "mode", "operator", "k1", "k2", "k3", "k4", "type", "values",
    "flood-color", "flood-opacity", "mask-type", "preserveAspectRatio",
}
# Inside a style attribute, only paint properties (an outside url() is refused below).
STYLE = re.compile(
    r"^\s*(fill|fill-opacity|fill-rule|stroke|stroke-width|stroke-opacity|stroke-linecap|stroke-linejoin|"
                   r"stop-color|stop-opacity|opacity|clip-rule|clip-path|mix-blend-mode|mask-type)\s*:\s*[^;:]+$")
PAINTED = ("fill", "stroke")


class MarkError(ValueError):
    pass


def load():
    data = json.loads(MANIFEST.read_text())
    seen = set()
    for mark in data["marks"]:
        if mark["id"] in seen:
            raise MarkError(f"{mark['id']}: listed twice")
        seen.add(mark["id"])
        unknown = set(mark["surfaces"]) - NAMES
        if unknown:
            raise MarkError(f"{mark['id']}: no surface {sorted(unknown)}")
        if mark.get("dark") and mark["ink"]:
            raise MarkError(f"{mark['id']}: a one-colour mark takes the ink; it has no dark file")
        if mark.get("glyph") and not (SOURCE / "svg" / mark["glyph"]).exists():
            raise MarkError(f"{mark['id']}: no glyph file {mark['glyph']}")
        if mark.get("fill") and (mark["ink"] or mark.get("dark")):
            raise MarkError(f"{mark['id']}: an icon that fills its tile keeps its own colours, in light and dark")
    for alias, target in data.get("aliases", {}).items():
        if target not in seen:
            raise MarkError(f"alias {alias}: no mark {target}")
        if alias in seen:
            raise MarkError(f"alias {alias}: also a mark's id, so it can't name {target}")
    named = {}
    for mark in data["marks"]:
        for company in mark.get("companies", ()):
            if not isinstance(company, str) or not company.strip():
                raise MarkError(f"{mark['id']}: a company is a name")
            if company.casefold() in named:
                raise MarkError(f"{company}: named by {named[company.casefold()]} and {mark['id']}")
            named[company.casefold()] = mark["id"]
    return data


def _local(tag):
    return tag.split("}", 1)[-1]


def _clean(element, name):
    """Refuse anything but shapes and their paint; drop editor metadata."""
    for child in list(element):
        tag = _local(child.tag)
        if tag in {"title", "desc",
                   "metadata"} or child.tag.startswith("{http://www.inkscape") or "sodipodi" in child.tag:
            element.remove(child)
            continue
        if tag not in ELEMENTS:
            raise MarkError(f"{name}: <{tag}> isn't a shape")
        _clean(child, name)
    for key in list(element.attrib):
        local = "href" if key == XLINK else _local(key)
        if key.startswith("{") and key != XLINK:
            del element.attrib[key]  # editor namespaces (sodipodi, inkscape)
            continue
        if local in {"version", "xml:space", "data-name", "class", "enable-background",
                     "role"} or local.startswith("data-"):
            del element.attrib[key]
            continue
        if local not in ATTRIBUTES:
            raise MarkError(f"{name}: attribute {local} on <{_local(element.tag)}>")
        value = element.attrib[key]
        if (local == "href" and not value.startswith("#")
                and not (_local(element.tag) == "image" and PICTURE.fullmatch(value))):
            raise MarkError(f"{name}: a link outside the file ({value[:60]})")
        for target in re.findall(r"url\(([^)]*)\)", value):
            if not re.fullmatch(r"\s*['\"]?#[\w.-]+['\"]?\s*", target):
                raise MarkError(f"{name}: url() outside the file ({value})")
        if local == "style":
            parts = [part for part in value.split(";") if part.strip()]
            bad = [part for part in parts if not STYLE.match(part)]
            if bad:
                raise MarkError(f"{name}: style {bad[0].strip()!r} isn't paint")


def _number(value):
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _paint(element, colour):
    """A one-colour mark in one colour: every fill and stroke that paints becomes `colour`."""
    for node in element.iter():
        for attr in PAINTED:
            value = node.attrib.get(attr)
            if value is not None and value != "none":
                node.attrib[attr] = colour
        style = node.attrib.get("style")
        if style:
            node.attrib["style"] = re.sub(r"\b(fill|stroke)\s*:\s*(?!none)[^;]+", lambda m: f"{m.group(1)}:{colour}",
                                          style)
    root = element
    if root.attrib.get("fill") is None:
        root.attrib["fill"] = colour


def render(mark, colour=None, source=None, dark=False, bare=False):
    """The mark's SVG (`source`, else its file, or its dark variant's) on its tile's square, 64 px: the mark centred,
    its longer side BOX of the tile times its optical scale. A dark variant is the same drawing in other colours, so it
    shares the box. A bare copy is the mark's own square at its optical scale (never wider than the square), without
    the tile's margin. A logo that is an icon (`fill`: Jira's, Stripe's) is the whole square, as is, bare or not."""
    name = mark["id"]
    file = mark["dark"] if dark else mark["file"]
    tree = ET.fromstring(source if source is not None else (SOURCE / "svg" / file).read_text())
    if _local(tree.tag) != "svg":
        raise MarkError(f"{name}: not an SVG")
    _clean(tree, name)
    x, y, w, h = mark["box"]
    if mark.get("fill"):
        side = max(w, h)
    else:
        side = max(w, h) / min(mark["scale"], 1) if bare else max(w, h) / mark["scale"] / BOX
    tree.attrib.pop("width", None)
    tree.attrib.pop("height", None)
    tree.attrib.pop("style", None)
    tree.set("viewBox", " ".join(_number(v) for v in (x + w / 2 - side / 2, y + h / 2 - side / 2, side, side)))
    tree.set("width", "64")
    tree.set("height", "64")
    if colour:
        _paint(tree, colour)
    text = ET.tostring(tree, encoding="unicode", short_empty_elements=True)
    text = re.sub(r"\s+", " ", re.sub(r">\s+<", "><", text)).strip()
    return text + "\n"


def ink():
    colours = json.loads(TOKENS.read_text())["color"]
    return colours["light"]["ink"], colours["dark"]["ink"]


def _ts_list(data, surface):
    marks = [m for m in data["marks"] if surface in m["surfaces"]]
    aliases = {a: t for a, t in sorted(data.get("aliases", {}).items()) if any(m["id"] == t for m in marks)}
    lines = [f"// {HEADER}",
             "/** Each mark this surface shows, by its id. `ink`: one colour, drawn in the page's ink. `dark`: it has"
             " a <id>-dark.svg for dark grounds."]
    if any(mark.get("fill") for mark in marks):
        lines[-1] += " `fill`: an icon that is its own tile."
    if surface == "site":
        lines[-1] += " `owner`: whose trademark it is; `source`: where the file came from; `rules`: its owner's rules."
    lines[-1] += " */"
    lines.append("export const MARKS = {")
    for mark in marks:
        # `dark`: the surface has a <id>-dark.svg for dark grounds (Remote's pages draw a one-colour mark that way too).
        dark = bool(mark.get("dark")) or (surface == "remote" and mark["ink"])
        fields = [f"ink: {'true' if mark['ink'] else 'false'}", f"dark: {'true' if dark else 'false'}"]
        if mark.get("fill"):
            fields.append("fill: true")
        if surface == "site":
            fields += [f"name: {json.dumps(mark['name'])}", f"owner: {json.dumps(mark['owner'])}",
                       f"source: {json.dumps(mark['source'])}", f"rules: {json.dumps(mark.get('rules'))}"]
        lines.append(f"  {json.dumps(mark['id'])}: {{ {', '.join(fields)} }},")
    lines.append("} as const")
    lines.append("export type MarkId = keyof typeof MARKS")
    lines.append("/** The other names a mark goes by on this surface (a client's key, a provider's id). */")
    lines.append("export const MARK_ALIASES: Record<string, MarkId> = {")
    lines += [f"  {json.dumps(alias)}: {json.dumps(target)}," for alias, target in aliases.items()]
    lines.append("}")
    glyphs = [mark for mark in data["marks"] if mark.get("glyph")]
    if glyphs:
        lines.append(
            "/** Each partner's glyph, for a button or a menu beside its name: its owner's own small-size icon, drawn "
            "in the"
                     " text colour. */")
        lines.append("export const GLYPHS = {")
        for mark in glyphs:
            view, paths = glyph(mark)
            lines.append(f"  {json.dumps(mark['id'])}: {{ viewBox: {json.dumps(view)}, d: {json.dumps(paths)} }},")
        lines.append("} as const")
    companies = sorted((company.casefold(), mark["id"]) for mark in marks for company in mark.get("companies", ()))
    if companies:
        lines.append(
            "/** The mark of each company Network X-ray names (mobile_agent/netxray/trackers.py), by its name in lower "
            "case. */")
        lines.append("export const COMPANY_MARKS: Record<string, MarkId> = {")
        lines += [f"  {json.dumps(company)}: {json.dumps(target)}," for company, target in companies]
        lines.append("}")
    return "\n".join(lines) + "\n"


def glyph(mark):
    """A mark's glyph, for a button or a menu beside its name: its owner's own small-size icon (`glyph` in marks.json,
    a file in svg/), drawn in the text colour. Its viewBox and paths; a glyph's file holds only paths."""
    tree = ET.fromstring((SOURCE / "svg" / mark["glyph"]).read_text())
    _clean(tree, mark["id"])
    paths = []
    for node in tree.iter():
        tag = _local(node.tag)
        if tag == "path":
            paths.append(node.attrib["d"])
        elif tag not in {"svg", "g"} or (tag == "g" and node.attrib.get("transform")):
            raise MarkError(f"{mark['id']}: a glyph holds only paths, not <{tag}>")
    if not paths or "viewBox" not in tree.attrib:
        raise MarkError(f"{mark['id']}: a glyph needs a viewBox and a path")
    return tree.attrib["viewBox"], paths


def slug(name):
    """A name as a key, as callback_page.mark_of reads one: "monday.com" is monday-com, "Google Drive" google-drive."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _py_module(data, light, dark):
    """mobile_agent/connectors/marks.py: each mark on its tile's square for a light page (LIGHT) and, where a dark page
    draws another, for a dark one (DARK), and each name a mark goes by (NAMES: its id, name, products and aliases)."""
    marks = [m for m in data["marks"] if "callback" in m["surfaces"]]
    ids = {m["id"] for m in marks}
    names = {}
    for mark in marks:
        for name in [mark["id"], mark["name"], *mark["products"]]:
            names.setdefault(slug(name), mark["id"])
    for alias, target in data.get("aliases", {}).items():
        if target in ids:
            names.setdefault(slug(alias), target)
    lines = [f"# {HEADER}",
             '"""Each service\'s mark on its tile\'s square, '
             'for connectors/callback_page.py (which loads nothing from outside):',
             "LIGHT for a light page, DARK where a dark page draws another (a one-colour mark in the dark ink, a "
             "mark\'s version",
             'for dark grounds), and NAMES, every name a mark goes by, as a key."""', "", "LIGHT = {"]
    lines += [f"    {json.dumps(m['id'])}: {json.dumps(render(m, light if m['ink'] else None).strip())},"
              for m in marks]
    lines += ["}", "DARK = {"]
    for mark in marks:
        if mark["ink"] or mark.get("dark"):
            copy = render(mark, dark) if mark["ink"] else render(mark, dark=True)
            lines.append(f"    {json.dumps(mark['id'])}: {json.dumps(copy.strip())},")
    lines += ["}", "NAMES = {"]
    lines += [f"    {json.dumps(name)}: {json.dumps(target)}," for name, target in sorted(names.items())]
    lines.append("}")
    return "\n".join(lines) + "\n"


SOURCES_BEGIN, SOURCES_END = "<!-- marks:sources -->", "<!-- /marks:sources -->"


def _sources(data, readme):
    """README.md with the sources table, from marks.json, between its markers."""
    rows = [SOURCES_BEGIN, f"<!-- {HEADER} -->", "",
            "| Mark | Shows | Owner | Colour | Taken from | Taken | Owner's rules |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for mark in data["marks"]:
        shows = ", ".join(mark["products"])
        colour = ("one colour, in the page's ink" if mark["ink"] else "its own icon, the whole tile" if mark.get("fill")
                  else "its own colours")
        rules = mark.get("rules") or ""
        source = mark["source"] + (f" ({mark['note']})" if mark.get("note") else "")
        files = f"`svg/{mark['file']}`" + (f", `svg/{mark['dark']}`" if mark.get("dark") else "")
        if mark.get("glyph") and mark["glyph"] != mark["file"]:
            files += f", `svg/{mark['glyph']}` (glyph)"
        rows.append(f"| {files} | {shows} | {mark['owner']} | {colour} | {source} | {mark['taken']} | {rules} |")
    rows.append(SOURCES_END)
    if SOURCES_BEGIN not in readme or SOURCES_END not in readme:
        raise MarkError(f"docs/brand/marks/README.md has no {SOURCES_BEGIN} region")
    head, rest = readme.split(SOURCES_BEGIN, 1)
    return head + "\n".join(rows) + rest.split(SOURCES_END, 1)[1]


def plan(data):
    """Every generated file and its bytes, plus the folders that hold nothing else."""
    readme = SOURCE / "README.md"
    files = {readme: _sources(data, readme.read_text()).encode()}
    folders = []
    light, dark = ink()
    if DOCS_MARKS.parent.exists():
        folders += [DOCS_MARKS, DOCS_INK]
        for mark in data["marks"]:
            if "docs" not in mark["surfaces"]:
                continue
            if mark["ink"]:
                files[DOCS_INK / f"{mark['id']}.svg"] = render(mark, light, bare=True).encode()
                continue
            files[DOCS_MARKS / f"{mark['id']}.svg"] = render(mark, bare=True).encode()
            if mark.get("dark"):
                files[DOCS_MARKS / f"{mark['id']}-dark.svg"] = render(mark, dark=True, bare=True).encode()
    for surface, where in SURFACES.items():
        if not where["list"].parent.exists():
            continue  # a surface that isn't in this tree
        if where["marks"] is None:
            files[where["list"]] = _py_module(data, light, dark).encode()
            continue
        folders.append(where["marks"])
        if "bare" in where:
            folders.append(where["bare"])
        files[where["list"]] = _ts_list(data, surface).encode()
        for mark in data["marks"]:
            if surface not in mark["surfaces"]:
                continue
            if surface == "remote" and mark["ink"]:
                files[where["marks"] / f"{mark['id']}.svg"] = render(mark, light).encode()
                files[where["marks"] / f"{mark['id']}-dark.svg"] = render(mark, dark).encode()
                continue
            for folder, bare in ((where["marks"], False), (where.get("bare"), True)):
                if folder is None:
                    continue
                files[folder / f"{mark['id']}.svg"] = render(mark, bare=bare).encode()
                if mark.get("dark"):
                    files[folder / f"{mark['id']}-dark.svg"] = render(mark, dark=True, bare=bare).encode()
    return files, folders


def measure(data, everything=False):
    """Each new mark's box (its painted extent, in its own units) and optical scale,
    from a render (rsvg-convert, Pillow).

    The optical scale shrinks a dense mark inside its box so its weight matches a light one's: the mark's coverage of
    its own square is compared with the set's median, and a mark that covers more is drawn smaller, by the fourth root
    of the ratio, never under 0.78. A square mark never grows past its box; a wide one (a name for a logo, as Amp's and
    Wix's are) may, up to 4/3, into the tile's margin. `scale_fixed` overrides it for a mark the eye judges
    differently, and an icon that fills its tile has none. Only a mark without a box is measured, so a new mark doesn't
    move the ones already drawn; `everything` measures them all again."""
    from PIL import Image

    coverage, aspect, fresh = {}, {}, {}
    with tempfile.TemporaryDirectory() as folder:
        for mark in data["marks"]:
            tree = ET.fromstring((SOURCE / "svg" / mark["file"]).read_text())
            _clean(tree, mark["id"])
            view = [float(v) for v in re.split(r"[\s,]+", tree.attrib["viewBox"].strip())]
            png = Path(folder) / f"{mark['id']}.png"
            size = 512
            subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(round(size * view[3] / view[2])),
                            str(SOURCE / "svg" / mark["file"]), "-o", str(png)], check=True)
            alpha = Image.open(png).convert("RGBA").getchannel("A")
            left, top, right, bottom = alpha.getbbox()
            unit = view[2] / alpha.width
            box = [view[0] + left * unit, view[1] + top * unit, (right - left) * unit, (bottom - top) * unit]
            if everything or "box" not in mark:
                fresh[mark["id"]] = [round(v, 3) for v in box]
            crop = alpha.crop((left, top, right, bottom))
            side = max(crop.width, crop.height)
            weights = crop.histogram()
            coverage[mark["id"]] = sum(i * n for i, n in enumerate(weights)) / 255 / (side * side)
            aspect[mark["id"]] = max(crop.width, crop.height) / min(crop.width, crop.height)
    ordered = sorted(coverage.values())
    median = ordered[len(ordered) // 2]
    for mark in data["marks"]:
        if mark["id"] not in fresh:
            continue
        mark["box"] = fresh[mark["id"]]
        if mark.get("fill"):
            mark["scale"] = 1
        elif "scale_fixed" in mark:
            mark["scale"] = mark["scale_fixed"]
        else:
            ratio = median / coverage[mark["id"]]
            widest = 4 / 3 if aspect[mark["id"]] >= 1.5 else 1.0
            mark["scale"] = round(min(widest, max(0.78, ratio ** 0.25)), 3)
    MANIFEST.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    parser.add_argument("--check", action="store_true", help="list stale copies instead of writing them")
    parser.add_argument("--measure", action="store_true", help="measure each new mark's box and optical scale first")
    parser.add_argument("--everything", action="store_true",
                        help="with --measure, measure the marks already measured too")
    args = parser.parse_args(argv)
    data = load()
    if args.measure and not args.check:
        measure(data, args.everything)
        data = load()
    files, folders = plan(data)
    stale = [path for path, body in files.items() if not path.exists() or path.read_bytes() != body]
    extra = [path for folder in folders if folder.exists() for path in sorted(folder.iterdir()) if path not in files]
    for path in stale:
        print(("differs from docs/brand/marks: " if args.check else "wrote ") + str(path.relative_to(ROOT)))
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(files[path])
    for path in extra:
        print(("not in docs/brand/marks: " if args.check else "removed ") + str(path.relative_to(ROOT)))
        if not args.check:
            path.unlink()
    if args.check and (stale or extra):
        print("run: python3 scripts/gen_agent_marks.py")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

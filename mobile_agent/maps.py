"""Places on a map, for a result's ``map`` block and a table's Map view (docs/results.mdx).

``snapshot(places, ...)`` draws them on an Apple Maps snapshot made on this Mac by the ``map`` helper
(helper_map.swift: MapKit off screen, no window): {"image": PNG bytes at 2x, "width", "height" (points), "places":
[{"id", "lat", "lon", "x", "y"}], "missing": [ids]}. A place that has no coordinates is found by its address, then
by its name near ``near``, with Apple's geocoder. The page draws its own numbered pins over the picture (x and y are
where each one goes), so a pin is a button that names its place and says where it is.

Snapshots are kept on disk by what they show (the places, the appearance and the size), so a result opened again
draws at once and asks Apple's geocoder nothing. What reaches the helper is a place's name and address or
coordinates: the same words the result already shows.

``places_of(block)``: a ``map`` block's places, or a table's rows as places (a Name column, an Address or Location
column, or Latitude and Longitude columns).
"""

import hashlib
import json
import re
import subprocess
import tempfile
import threading
from pathlib import Path

SECONDS = 45
KEEP = 200
MAX_PLACES = 30
_lock = threading.BoundedSemaphore(2)

NAME = re.compile(r"^(name|place|title|restaurant|store|shop|venue|business|cafe|café|spot)$", re.I)
ADDRESS = re.compile(r"^(address|location|where|street|addr\.?)$", re.I)
LAT = re.compile(r"^(lat|latitude)$", re.I)
LON = re.compile(r"^(lon|lng|long|longitude)$", re.I)


class MapError(RuntimeError):
    """The map couldn't be drawn; the message says why, in plain words."""


def _column(columns, pattern):
    return next((c["key"] for c in columns if pattern.match(str(c.get("label") or "").strip())), None)


def places_of(block):
    """[{"id", "name", "address"?, "lat"?, "lon"?}] for a map block or a table with places, else []."""
    if not isinstance(block, dict):
        return []
    if block.get("type") == "map":
        return [{key: place[key] for key in ("id", "name", "address", "lat", "lon") if key in place}
                for place in block.get("places") or ()][:MAX_PLACES]
    if block.get("type") != "table":
        return []
    columns = block.get("columns") or []
    address, lat, lon = _column(columns, ADDRESS), _column(columns, LAT), _column(columns, LON)
    if address is None and (lat is None or lon is None):
        return []
    name = _column(columns, NAME) or next((c["key"] for c in columns if c["key"] not in (address, lat, lon)), None)
    places = []
    for index, row in enumerate(block.get("rows") or ()):
        cells = row.get("cells") or {}
        place = {"id": f"r{index}", "name": str(cells.get(name) or f"Place {index + 1}")[:120]}
        if address is not None and isinstance(cells.get(address), str) and cells[address].strip():
            place["address"] = cells[address].strip()[:200]
        if lat is not None and lon is not None:
            try:
                y, x = float(cells.get(lat)), float(cells.get(lon))
                if -90 <= y <= 90 and -180 <= x <= 180:
                    place["lat"], place["lon"] = y, x
            except (TypeError, ValueError):
                pass
        if "address" in place or "lat" in place:
            places.append(place)
        if len(places) >= MAX_PLACES:
            break
    return places


def _cache_dir():
    from .paths import user_data_dir
    folder = Path(user_data_dir()) / "cache" / "maps"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def _prune(folder):
    files = sorted(folder.glob("*.png"), key=lambda path: path.stat().st_mtime)
    for old in files[:-KEEP]:
        for path in (old, old.with_suffix(".json")):
            path.unlink(missing_ok=True)


def snapshot(places, *, near=None, dark=False, width=720, height=360, helper=None, cache_dir=None,
             timeout=SECONDS):
    """The places on a map (see the module docstring). MapError when there's nothing to draw or no helper."""
    places = [p for p in places or () if isinstance(p, dict) and p.get("name")][:MAX_PLACES]
    if not places:
        raise MapError("There are no places to show on a map.")
    width, height = max(200, min(1200, int(width))), max(120, min(900, int(height)))
    request = {"places": places, "near": near, "width": width, "height": height, "dark": bool(dark)}
    key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:24]
    folder = Path(cache_dir) if cache_dir is not None else _cache_dir()
    image, meta = folder / f"{key}.png", folder / f"{key}.json"
    if image.is_file() and meta.is_file():
        try:
            return {**json.loads(meta.read_text()), "image": image.read_bytes()}
        except (OSError, ValueError):
            pass
    if helper is None:
        from . import native_helpers
        helper = native_helpers.find("map")
        if helper is None:
            try:
                helper = native_helpers.build("map")
            except (LookupError, OSError, subprocess.SubprocessError) as error:
                raise MapError("Mobster can't draw maps on this Mac: " + str(error)) from None
    if not _lock.acquire(timeout=timeout):
        raise MapError("Mobster is drawing other maps. Try again in a moment.")
    try:
        with tempfile.TemporaryDirectory(prefix="mobster-map-") as work:
            source, target = Path(work) / "places.json", Path(work) / "map.png"
            source.write_text(json.dumps(request), encoding="utf-8")
            try:
                done = subprocess.run([str(helper), str(source), str(target)], capture_output=True, text=True,
                                      timeout=timeout)
            except subprocess.TimeoutExpired:
                raise MapError("Mobster took too long to draw the map. Try again.") from None
            except OSError as error:
                raise MapError(f"Mobster couldn't draw the map: {error}") from None
            if done.returncode != 0 or not target.is_file():
                lines = (done.stderr or "").strip().splitlines()
                said = next((l for l in reversed(lines) if l.startswith("Mobster")), "Mobster couldn't draw the map")
                raise MapError(said if said.endswith(".") else said + ".")
            try:
                answer = json.loads((done.stdout or "").strip().splitlines()[-1])
            except (ValueError, IndexError):
                raise MapError("Mobster couldn't read where the places are.") from None
            data = target.read_bytes()
    finally:
        _lock.release()
    out = {"width": answer.get("width", width), "height": answer.get("height", height),
           "places": [p for p in answer.get("places") or () if isinstance(p, dict)],
           "missing": [m for m in answer.get("missing") or () if isinstance(m, str)]}
    try:
        image.write_bytes(data)
        meta.write_text(json.dumps(out))
        _prune(folder)
    except OSError:
        pass
    return {**out, "image": data}

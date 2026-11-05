"""Bulk queues in and posting reports out."""

import csv
import datetime
import io
import json
from pathlib import Path

from .store import CAPTION_CHARS, MAX_ITEMS, QueueError, caption_text

COLUMNS = ("media", "caption", "hashtags", "sound", "profile", "schedule", "format")
MEDIA_SUFFIXES = (".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".webp", ".mov", ".mp4", ".m4v")
REPORT_COLUMNS = ("post", "profile", "app", "phone", "scheduled", "state", "posted_at", "link", "media", "checked",
                  "route", "caption", "note", "run")


def _profile_id(value, profiles):
    from ..accounts.store import AccountNotFound, AmbiguousAccount, resolve
    if not value:
        return None
    try:
        return resolve(profiles, value.strip())["id"]
    except (AccountNotFound, AmbiguousAccount) as error:
        raise QueueError(str(error), "profile_not_found") from None


def _when(value, zone=None):
    if not value or not value.strip():
        return None
    try:
        stamp = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise QueueError(f"{value!r} isn't a time: write it like 2026-10-20T09:30.") from None
    if stamp.tzinfo is None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            stamp = stamp.replace(tzinfo=ZoneInfo(zone)) if zone else stamp.astimezone()
        except (ZoneInfoNotFoundError, ValueError):
            stamp = stamp.astimezone()
    return round(stamp.timestamp() * 1000)


def from_csv(text, base, profiles, zone_of=lambda profile: None):
    """Item bodies (store.clean_item's input) from CSV ``text``; relative media paths are under ``base``."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or "media" not in {name.strip().casefold() for name in reader.fieldnames}:
        raise QueueError("The CSV needs a header with a media column (and caption, hashtags, sound, profile, schedule, "
                         "format if you like).")
    bodies = []
    for number, row in enumerate(reader, start=2):
        row = {(k or "").strip().casefold(): (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue
        if len(bodies) >= MAX_ITEMS:
            raise QueueError(f"A queue holds at most {MAX_ITEMS} posts.", "too_many")
        paths = [p.strip() for p in row.get("media", "").replace(";", "|").split("|") if p.strip()]
        if not paths:
            raise QueueError(f"Row {number} names no photo or video.")
        media = [str((Path(base) / p).expanduser()) if not Path(p).expanduser().is_absolute()
                 else str(Path(p).expanduser()) for p in paths]
        try:
            profile = _profile_id(row.get("profile"), profiles)
            body = {"media": media, "caption": row.get("caption", ""), "hashtags": row.get("hashtags", ""),
                    "sound": row.get("sound", ""), "profile": profile,
                    "scheduledAt": _when(row.get("schedule"), zone_of(profile) if profile else None)}
        except QueueError as error:
            raise QueueError(f"Row {number}: {error}", error.code) from None
        if row.get("format"):
            body["format"] = row["format"].casefold()
        bodies.append(body)
    if not bodies:
        raise QueueError("The CSV has no posts in it.")
    return bodies


def from_folder(folder, profile=None):
    """Item bodies, one per photo or video in ``folder`` (not its subfolders), in name order."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise QueueError(f"There is no folder at {folder}.")
    files = sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES
                    and not p.name.startswith(".")), key=lambda p: p.name.casefold())
    if not files:
        raise QueueError(f"{folder.name} has no photos or videos.")
    if len(files) > MAX_ITEMS:
        raise QueueError(f"A queue holds at most {MAX_ITEMS} posts; {folder.name} has {len(files)}.", "too_many")
    return [body_for(file, profile) for file in files]


def body_for(file, profile=None):
    """One post for a media file: the caption is the .txt beside it, if there is one."""
    caption = ""
    sidecar = file.with_suffix(".txt")
    if sidecar.is_file():
        try:
            caption = sidecar.read_text(encoding="utf-8").strip()[:CAPTION_CHARS]
        except (OSError, UnicodeDecodeError):
            caption = ""
    body = {"media": [str(file)], "caption": caption}
    if profile:
        body["profile"] = profile
    return body


def report_rows(queue, profiles):
    by_id = {p["id"]: p for p in profiles}
    rows = []
    for number, item in enumerate(queue["items"], start=1):
        profile = by_id.get(item.get("profile")) or {}
        result = item.get("result") or {}
        transfer = item.get("transfer") or {}
        checked = transfer.get("items") or ()
        rows.append({
            "post": number,
            "profile": f"@{profile['handle']}" if profile else "",
            "app": (profile.get("app") or {}).get("name", ""),
            "phone": profile.get("deviceName") or "",
            "scheduled": _iso(item.get("scheduledAt")),
            "state": item["state"],
            "posted_at": _iso(result.get("postedAt") or result.get("markedAt")),
            "link": result.get("link") or "",
            "media": " | ".join(m["name"] for m in item["media"]),
            "checked": " | ".join(c.get("state", "") for c in checked),
            "route": transfer.get("route") or "",
            "caption": caption_text(item),
            "note": item.get("note") or "",
            "run": item.get("runId") or "",
        })
    return rows


def _iso(ms):
    if not ms:
        return ""
    return datetime.datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="minutes")


def report(queue, profiles, fmt="csv"):
    """(bytes, content type, file name) of the posting report."""
    rows = report_rows(queue, profiles)
    safe = "".join(c if c.isalnum() or c in " -_" else "-" for c in queue["name"]).strip() or "queue"
    if fmt == "json":
        body = json.dumps({"queue": queue["name"], "posts": rows}, ensure_ascii=False, indent=1).encode()
        return body, "application/json", f"{safe} report.json"
    if fmt != "csv":
        raise QueueError("The report is csv or json.")
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=REPORT_COLUMNS)
    writer.writeheader()
    for row in rows:
        # A cell a spreadsheet would run as a formula is written as text.
        writer.writerow({k: ("'" + v if isinstance(v, str) and v[:1] in "=+-@" else v) for k, v in row.items()})
    return out.getvalue().encode("utf-8"), "text/csv", f"{safe} report.csv"

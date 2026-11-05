"""The daily check for a new release, and `mobster update`."""

import json
import math
import os
import sys
import threading
import time

from .paths import source_checkout

DAY = 86400
QUIET_COMMANDS = {"mcp", "serve", "completion", "update", "tui", "help"}
_fetch = None


def _cache_path():
    from .paths import user_data_dir
    return user_data_dir() / "cli" / "update-check.json"


def enabled(command, args, stream=None):
    stream = stream or sys.stderr
    if os.environ.get("MOBSTER_NO_UPDATE_CHECK", "").strip() not in ("", "0") or os.environ.get("CI"):
        return False
    if command in QUIET_COMMANDS or getattr(args, "json", False) or inside_app():
        return False
    try:
        return stream.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _read_cache():
    try:
        value = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    checked = value.get("checkedAt")
    if type(checked) not in (int, float) or not math.isfinite(checked):
        value["checkedAt"] = 0
    if not isinstance(value.get("latest"), dict):
        value["latest"] = None
    if not isinstance(value.get("minimum"), str):
        value["minimum"] = None
    return value


def _write_cache(value):
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    except OSError:
        pass


def latest_release(timeout=1.0, opener=None):
    """{"version", "publishedAt"} of the newest release of Mobster for Mac, or None."""
    import urllib.request

    from . import links
    request = urllib.request.Request(links.RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                                                   "User-Agent": "mobster-cli"})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            data = json.loads(response.read(200_000))
    except Exception:  # noqa: BLE001 -- the check is a courtesy; it never fails a command
        return None
    if not isinstance(data, dict):
        return None
    tag = str(data.get("tag_name") or "").lstrip("v")
    if not tag or data.get("draft") or data.get("prerelease"):
        return None
    assets = data.get("assets")
    names = {str(asset.get("name") or "") for asset in assets
             if isinstance(asset, dict)} if isinstance(assets, list) else set()
    if links.APP_ASSET not in names:
        return None
    return {"version": tag, "publishedAt": data.get("published_at")}


def minimum_version(timeout=1.0, opener=None):
    """The update feed's `minimum_version`, or None (none set, offline, or mobster.dev's pin didn't match)."""
    import urllib.request

    from . import links
    request = urllib.request.Request(links.UPDATE_FEED,
                                     headers={"Accept": "application/json", "User-Agent": "mobster-cli"})
    if opener is None:
        from . import pinning
        opener = pinning.opener().open
    try:
        with opener(request, timeout=timeout) as response:
            data = json.loads(response.read(200_000))
    except Exception:  # noqa: BLE001 -- the check is a courtesy; it never fails a command
        return None
    value = str(data.get("minimum_version") or "").strip().lstrip("v") if isinstance(data, dict) else ""
    return value or None


def parse_version(text):
    parts = []
    for piece in str(text or "").split("."):
        digits = "".join(c for c in piece if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))[:3]


def newer(latest, current):
    return parse_version(latest) > parse_version(current)


def start(command, args):
    """Before the command runs: a fetch in the background when the cached answer is a day old."""
    global _fetch
    if not enabled(command, args):
        return
    cache = _read_cache()
    if time.time() - float(cache.get("checkedAt") or 0) < DAY:
        return

    def fetch():
        found = latest_release()
        value = {"checkedAt": time.time(), "latest": found, "minimum": minimum_version()}
        _write_cache(value)
    _fetch = threading.Thread(target=fetch, name="mobster-update-check", daemon=True)
    _fetch.start()


def unsupported(current, cache):
    """Whether the feed's minimum_version (in the cache) is above this version."""
    minimum = (cache or {}).get("minimum")
    return bool(minimum) and newer(minimum, current)


def notice(current, cache, now=None):
    """The one line, or "" when there's nothing newer. It points at Mobster for Mac, never at an installer."""
    from . import links
    latest = (cache or {}).get("latest") or {}
    version = latest.get("version")
    if unsupported(current, cache):
        return f"Mobster {current} is no longer supported. Download Mobster for Mac at {links.APP_DOWNLOAD}"
    if not version or not newer(version, current):
        return ""
    released = ""
    published = latest.get("publishedAt")
    if published:
        from datetime import datetime, timezone
        try:
            when = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
            days = int(((time.time() if now is None else now) - when.astimezone(timezone.utc).timestamp()) // DAY)
            released = ", released today" if days < 1 else f", released {days} day{'s' if days != 1 else ''} ago"
        except ValueError:
            released = ""
    return (
        f"Mobster {version} is out{released}, in Mobster for Mac (you have {current}). Download it at "
        f"{links.APP_DOWNLOAD}")


def pending_notice(current, wait):
    """The notice for a fetch `start` began: wait up to `wait` seconds for it, then read the cache. "" when none."""
    return notice(current, _cache_after_fetch(wait))


def _cache_after_fetch(wait):
    if _fetch is not None:
        _fetch.join(wait)
    return _read_cache()


def after_command(command, args):
    """After the command: wait up to the rest of 1 s for a fetch it started, then say so when a release is newer."""
    if not enabled(command, args):
        return
    from . import __version__
    cache = _cache_after_fetch(1.0)
    line = notice(__version__, cache)
    if line:
        from .style import palette
        paint = palette(sys.stderr)
        try:
            print(paint(line, "amber" if unsupported(__version__, cache) else "tertiary"), file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass


def inside_app():
    """Whether this is Mobster for Mac's own copy of mobster (the frozen sidecar in the .app bundle)."""
    return bool(getattr(sys, "frozen", False)) and ".app/Contents" in sys.executable


def run_update(args):
    """`mobster update`: whether a newer Mobster is out, and where to get it."""
    from . import __version__, links
    from .style import palette
    paint = palette(sys.stdout)
    if inside_app():
        print("This copy of Mobster belongs to Mobster for Mac, which updates itself.")
        return 0
    found = latest_release(timeout=5)
    cache = {"checkedAt": time.time(), "latest": found, "minimum": minimum_version(timeout=5)}
    _write_cache(cache)
    line = notice(__version__, cache)
    if line:
        print(paint(line, "amber" if unsupported(__version__, cache) else "accent"))
    elif found:
        print(paint("✓ ", "green") + f"Mobster {__version__} is the latest release.")
    else:
        print(f"Mobster couldn't check for a new release right now. New versions come in Mobster for Mac: "
              f"{links.APP_DOWNLOAD}")
        return 1 if args.check else 0
    if source_checkout() and line:
        print("This is a source checkout: update it with git (git pull).")
    elif line:
        print("This mobster keeps working.")
    return 0

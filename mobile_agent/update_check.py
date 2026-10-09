"""The daily check for a new release, and `mobster update`.

At most once a day, a command asks the public repository's releases API (1 s timeout, in a thread that runs while
the command does) and keeps the answer in the data folder (cli/update-check.json). When a newer release is out, one
line goes to stderr after the command:

    Mobster 0.3.0 is out, released 12 days ago (you have 0.2.0) · mobster update

Only on an interactive terminal. Never with --json, a pipe, CI, MOBSTER_NO_UPDATE_CHECK=1, `mcp` or `serve` (their
output is a protocol), and never as anything but that one line.
"""

import json
import os
import sys
import threading
import time

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
    if command in QUIET_COMMANDS or getattr(args, "json", False) or getattr(sys, "frozen", False) and \
            ".app/Contents" in sys.executable:
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
    return value if isinstance(value, dict) else {}


def _write_cache(value):
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    except OSError:
        pass


def latest_release(timeout=1.0, opener=None):
    """{"version", "publishedAt"} of the newest public release, or None (no release, offline, rate limited)."""
    import urllib.request
    from . import links
    request = urllib.request.Request(links.RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                                                   "User-Agent": "mobster-cli"})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            data = json.loads(response.read(200_000))
    except Exception:  # noqa: BLE001 -- the check is a courtesy; it never fails a command
        return None
    tag = str(data.get("tag_name") or "").lstrip("v")
    if not tag or data.get("draft") or data.get("prerelease"):
        return None
    return {"version": tag, "publishedAt": data.get("published_at")}


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
        value = {"checkedAt": time.time(), "latest": found}
        _write_cache(value)
    _fetch = threading.Thread(target=fetch, name="mobster-update-check", daemon=True)
    _fetch.start()


def notice(current, cache, now=None):
    """The one line, or "" when there's nothing newer."""
    latest = (cache or {}).get("latest") or {}
    version = latest.get("version")
    if not version or not newer(version, current):
        return ""
    released = ""
    published = latest.get("publishedAt")
    if published:
        from datetime import datetime, timezone
        try:
            when = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
            days = int(((now or time.time()) - when.astimezone(timezone.utc).timestamp()) // DAY)
            released = ", released today" if days < 1 else f", released {days} day{'s' if days != 1 else ''} ago"
        except ValueError:
            released = ""
    return f"Mobster {version} is out{released} (you have {current}) · mobster update"


def after_command(command, args, code):
    """After the command: wait up to the rest of 1 s for a fetch it started, then say so when a release is newer."""
    if not enabled(command, args):
        return
    if _fetch is not None:
        _fetch.join(1.0)
    from . import __version__
    line = notice(__version__, _read_cache())
    if line:
        from .style import palette
        paint = palette(sys.stderr)
        try:
            print(paint(line, "tertiary"), file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass


def install_method(executable=None, prefix=None):
    """How this Mobster was installed: brew, script, uv, pip, app (Mobster for Mac's own copy) or source."""
    from .paths import source_checkout
    executable = str(executable or sys.executable)
    prefix = str(prefix or sys.prefix)
    if ".app/Contents" in executable:
        return "app"
    if "/Cellar/mobster" in executable or "/opt/mobster/" in executable or "/Cellar/mobster" in prefix:
        return "brew"
    if "/.mobster/versions/" in executable or "/.mobster/versions/" in prefix:
        return "script"
    if "/uv/tools/" in prefix:
        return "uv"
    if source_checkout():
        return "source"
    return "pip"


def run_update(args):
    """`mobster update`: the latest release, the way Mobster was installed."""
    import subprocess
    from . import __version__, links
    from .style import palette
    paint = palette(sys.stdout)
    found = latest_release(timeout=5)
    _write_cache({"checkedAt": time.time(), "latest": found})
    if found and not newer(found["version"], __version__):
        print(paint("✓ ", "green") + f"Mobster {__version__} is the latest release.")
        return 0
    if found:
        print(notice(__version__, {"latest": found}).replace(" · mobster update", "."))
    elif args.check:
        print("Mobster couldn't check for a new release right now. Try again later.")
        return 1
    if args.check:
        return 0
    method = install_method()
    commands = {"brew": ["brew", "upgrade", "mobster"],
                "uv": ["uv", "tool", "upgrade", "mobster-cli"],
                "pip": [sys.executable, "-m", "pip", "install", "--upgrade", "mobster-cli"],
                "script": ["/bin/sh", "-c", f"curl -fsSL {links.INSTALL_SCRIPT} | sh"]}
    if method == "app":
        print("This copy of Mobster belongs to Mobster for Mac, which updates itself.")
        return 0
    if method == "source":
        print("This is a source checkout, so `mobster update` can't update it. Update it with git: git pull")
        return 1
    command = commands[method]
    shown = " ".join(command) if method != "script" else command[-1]
    print(paint("Updating with ", "tertiary") + paint(shown, "accent"))
    try:
        return subprocess.call(command)
    except FileNotFoundError:
        print(f"mobster update: {command[0]} isn't installed here. Install the latest release from "
              f"{links.page('getting-started')}", file=sys.stderr)
        return 1

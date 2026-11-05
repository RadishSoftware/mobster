"""The Expo dev loop's state: one Metro per project, whether it works, and what to reload on a save."""

import json
import os
import re
import signal
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import env_file
from . import DevError, expo

PORT_SPAN = 20
ERROR_LIMIT = 600
BUNDLE_WAIT = 90
IGNORED = frozenset({"node_modules", "ios", "android", ".git", ".mobster", ".expo", "Pods", "build", "dist"})
SOURCE = frozenset({".js", ".jsx", ".ts", ".tsx", ".json", ".mjs", ".cjs", ".css"})
MAX_FILES = 20_000
# Metro serves Expo's virtual entry for an Expo app; a plain React Native app has index.js.
ENTRY = {True: "/.expo/.virtual-metro-entry.bundle?platform=ios&dev=true&hot=false&minify=false",
         False: "/index.bundle?platform=ios&dev=true&minify=false"}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# What a dev client's error screen and Metro's log say when the bundle failed.
SCREEN_MARKERS = ("Unable to resolve module", "SyntaxError", "Unexpected token", "Bundling failed",
                  "Unable to load script", "Could not connect to development server",
                  "There was a problem running the requested app", "Invariant Violation", "Uncaught Error",
                  "TransformError", "Cannot find module")


# Metro's text is data from the project: cut short and never read as an instruction.
def _clean(text, limit=ERROR_LIMIT):
    flat = " ".join(ANSI.sub("", str(text or "")).split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def _alive(pid, kill=os.kill):
    try:
        kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _state_file(root, name):
    try:
        return int((Path(root) / expo.STATE_DIR / name).read_text().strip())
    except (OSError, ValueError):
        return None


def port_free(port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def pick_port(app, wanted=None, *, opener=None, free=port_free):
    first = int(wanted or _state_file(app.root, "metro.port") or expo.DEFAULT_PORT)
    for port in range(first, first + PORT_SPAN):
        if expo.metro_running(port, opener=opener, root=app.root) or free(port):
            return port
        if wanted:
            break
    raise DevError(f"Port {first} is taken" + ("." if wanted else f", and so are the {PORT_SPAN - 1} after it."),
                   "Stop what is using it, or pass another --port.")


def log_tail(app, limit=2000):
    try:
        with (Path(app.root) / expo.STATE_DIR / "metro.log").open("rb") as handle:
            handle.seek(max(0, os.fstat(handle.fileno()).st_size - limit))
            text = handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""
    return ANSI.sub("", text[-limit:]).strip()


def bundle_error(app, port, *, opener=None, wait=BUNDLE_WAIT):
    url = f"http://127.0.0.1:{int(port)}{ENTRY[bool(app.expo)]}"
    try:
        with (opener or urllib.request.urlopen)(url, timeout=wait) as response:
            response.read(1)
        return None
    except urllib.error.HTTPError as error:
        body = error.read(20_000).decode("utf-8", errors="replace")
    except Exception:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        data = {}
    first = next(iter(data.get("errors") or ()), None) if isinstance(data, dict) else None
    first = first if isinstance(first, dict) else {}
    message = (data.get("message") if isinstance(data, dict) else None) or first.get("description") or body
    line = first.get("lineNumber")
    return {"type": str(data.get("type") or "BundleError") if isinstance(data, dict) else "BundleError",
            "message": _clean(message), "file": first.get("filename") if isinstance(first.get("filename"), str)
            else None, "line": line if isinstance(line, int) else None}


def screen_error(texts):
    """The error on a dev client's error screen, from the text the screen shows, or None."""
    for text in texts:
        if any(marker in str(text) for marker in SCREEN_MARKERS):
            return _clean(text)
    return None


def status(app, *, opener=None, kill=os.kill, check_bundle=False):
    pid, port = _state_file(app.root, "metro.pid"), _state_file(app.root, "metro.port") or expo.DEFAULT_PORT
    live = pid is not None and expo.owns_metro(app.root, pid) and _alive(pid, kill)
    answering = expo.metro_running(port, opener=opener, root=app.root)
    info = {"state": "stopped", "port": port, "pid": pid if live else None, "error": None,
            "log": f"{expo.STATE_DIR}/metro.log"}
    if answering:
        info["state"] = "running"
        error = bundle_error(app, port, opener=opener) if check_bundle else None
        if error:
            info["state"], info["error"] = "failed", error
    elif live:
        info["state"] = "starting"
    elif pid is not None:
        tail = _clean(log_tail(app), ERROR_LIMIT)
        info["state"], info["error"] = "failed", {"type": "MetroExited", "file": None, "line": None,
                                                  "message": tail or "Metro exited before it answered."}
    return info


# The second value is True only when this call started Metro: only that caller stops it.
def ensure(app, port=None, *, start=None, opener=None, kill=os.kill, free=port_free, wait=expo.METRO_WAIT):
    with env_file.writer(Path(app.root) / expo.STATE_DIR / "admission"):
        port = pick_port(app, port, opener=opener, free=free)
        if expo.metro_running(port, opener=opener, root=app.root):
            return {**status(app, opener=opener, kill=kill), "port": port}, False
        if expo.metro_starting(app.root, kill=kill):
            raise DevError(f"Metro is still starting on port {port}.", "Try again in a few seconds.")
        pid = (start or expo.start_metro)(app, port, wait=wait, opener=opener, stop_on_fail=True)
        return {"state": "running", "port": port, "pid": pid, "error": None, "log": f"{expo.STATE_DIR}/metro.log"}, True


def stop(root, *, kill=os.kill, grace=5.0, sleep=time.sleep, clock=time.monotonic, expected_pid=None):
    with env_file.writer(Path(root) / expo.STATE_DIR / "admission"):
        return _stop_owned(root, kill=kill, grace=grace, sleep=sleep, clock=clock, expected_pid=expected_pid)


def _stop_owned(root, *, kill=os.kill, grace=5.0, sleep=time.sleep, clock=time.monotonic, expected_pid=None):
    """Stop and reconcile a process group while the caller owns the project admission lock."""
    pid = expo.stop_metro(root, kill=kill, expected_pid=expected_pid)
    if pid is None:
        return None
    deadline = clock() + grace
    while _alive(-pid, kill) and clock() < deadline:
        sleep(0.1)
    if _alive(-pid, kill):
        try:
            kill(-pid, signal.SIGKILL)
        except ProcessLookupError:
            return pid
        deadline = clock() + 2
        while _alive(-pid, kill) and clock() < deadline:
            sleep(0.1)
        if _alive(-pid, kill):
            raise DevError("Metro's process group did not stop.", "Stop it from its terminal before starting another.")
    return pid


class Watcher:
    """Polls a project's source files (modified times only) and says when one was saved, added or removed."""

    def __init__(self, root):
        self.root = Path(root)
        self.seen = self._scan()

    def _scan(self):
        found = {}
        for folder, names, files in os.walk(self.root):
            names[:] = [name for name in names if name not in IGNORED and not name.startswith(".")]
            for name in files:
                if os.path.splitext(name)[1] in SOURCE:
                    path = os.path.join(folder, name)
                    try:
                        found[path] = os.stat(path).st_mtime_ns
                    except OSError:
                        continue
                    if len(found) >= MAX_FILES:
                        return found
        return found

    def changed(self):
        now = self._scan()
        different = now != self.seen
        self.seen = now
        return different

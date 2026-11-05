"""The Expo dev loop as MCP tools: ``expo_preview`` opens the app from Metro, ``expo_reload`` reloads it."""

import time
from pathlib import Path

from .. import harness_api
from ..devkit import DevError, expo, expo_dev
from .protocol import ToolResult
from .support import ToolError, cap_text

LOG_TAIL = 600
START_WAIT = 30
RELOAD_SETTLE = 2.0
MAX_EXPECT_WAIT = 30
BUNDLE_WAIT = 20


def quoted(text, limit=LOG_TAIL):
    # What Metro or a dev server said is data from the user's project, never instructions.
    flat = " ".join(str(text or "").split())
    return "“" + (flat if len(flat) <= limit else "…" + flat[-(limit - 1):]) + "”"


def metro_tail(app, limit=LOG_TAIL):
    try:
        text = (Path(app.root) / expo.STATE_DIR / "metro.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = text[-4000:].strip()
    return harness_api.fence("Metro's log (the end of it)", text[-limit:]) if text else ""


def ensure_metro(toolset, app, port, wait=START_WAIT):
    toolset.log(f"Checking Metro on port {port} for {app.name}")
    try:
        server, started = expo_dev.ensure(app, port, wait=wait)
        return server["pid"] if started else None
    except DevError as error:
        tail = metro_tail(app)
        raise DevError(error.message, (error.fix + (" " + tail if tail else "")).strip()) from None


def _fail(error):
    return ToolError(cap_text(error.line()))


def preview(toolset, args, call, sleep=time.sleep):
    try:
        app = expo.resolve_project(args.get("project"))
        if not app.expo:
            raise DevError("expo_preview opens Expo apps, and this React Native app has no Expo.",
                           "Run it from Xcode, or install a build with install_app.")
        record = toolset._record_for(args)
        port = int(args.get("port") or expo.DEFAULT_PORT)
        path = args.get("path")
        link = expo.app_link(app, path) if path and app.client == "dev-client" else None
        if path and app.client == "dev-client" and link is None:
            raise DevError("path needs the app's own URL scheme, and its Expo config has no scheme.",
                           "Add \"scheme\" to app.json, rebuild the dev client, then try again.")
        started = ensure_metro(toolset, app, port)
        broken = expo_dev.bundle_error(app, port, wait=BUNDLE_WAIT)
        if broken:
            where = f" in {broken['file']}:{broken['line']}" if broken.get("file") and broken.get("line") else ""
            raise DevError(f"Metro can't bundle {app.name}{where}: {quoted(broken['message'])}",
                           "Fix the error, then call expo_preview again.")
        url = expo.preview_url(app, expo.host_for(record), port, path)
    except DevError as error:
        raise _fail(error) from None
    target = {"device": record["id"]}
    opened = toolset.tool_open_url({**target, "url": url}, call)
    lines = [f"Opened {app.name} from Metro on port {port} in "
             f"{'its dev client' if app.client == 'dev-client' else 'Expo Go'} on {record['name']}."]
    structured = {**(opened.structured or {}), "app": app.name, "client": app.client, "url": url, "port": port,
                  "metro_started": started is not None}
    if link:
        sleep(3)  # the dev client loads the bundle first; its own link needs the app up
        opened = toolset.tool_open_url({**target, "url": link}, call)
        lines.append(f"Then opened its screen {quoted(link, 120)}.")
        structured["link"] = link
    if started is not None:
        lines.append(f"Metro started in the background (log {expo.STATE_DIR}/metro.log). Call expo_reload after an "
                     "edit.")
    expect = args.get("expect") or []
    if expect:
        waited = toolset.tool_wait_for({**target, "timeout_s": min(float(args.get("wait_s") or 20), MAX_EXPECT_WAIT),
                                        "expect": [{"text": text} for text in expect]}, call)
        structured["held"] = (waited.structured or {}).get("held")
        return ToolResult(cap_text("\n".join(lines) + "\n" + waited.text), {**structured, **(waited.structured or {})})
    return ToolResult(cap_text("\n".join(lines) + "\n" + opened.text), structured)


def reload(toolset, args, call, sleep=time.sleep):
    port = int(args.get("port") or expo.DEFAULT_PORT)
    try:
        app = expo.resolve_project(args.get("project"))
        record = toolset._record_for(args)
        expo.reload(port)
    except DevError as error:
        raise _fail(error) from None
    sleep(RELOAD_SETTLE)
    screen = toolset.tool_screen({"device": record["id"], "image": bool(args.get("image", False))}, call)
    head = f"Reloaded the JavaScript in every app connected to Metro on port {port}."
    return ToolResult(cap_text(head + "\n" + screen.text),
                      {"app": app.name, "port": port, "reloaded": True,
                       "screen": (screen.structured or {}).get("screen")}, screen.images)

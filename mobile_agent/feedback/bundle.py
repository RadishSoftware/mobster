"""The diagnostics bundle a feedback report may carry (docs/feedback.mdx), built here so the Mac app can show it, word
for word, before anything is sent."""

import json
import platform
import re
from collections import deque
from pathlib import Path

from .. import harness_api, secret_filter

EVENTS = 150
LOG_LINES = 200
STRING_MAX = 400
MAX_BYTES = 500_000
MASK = "[masked]"
# Event fields that are pictures or trees, never sent.
DROP_KEYS = {"screenshot", "image", "frame", "frames", "png", "jpeg", "thumbnail", "elements", "tree", "source", "xml",
             "accessibility", "dataUrl", "data_url", "audio"}

_SCRUB = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), MASK),
    (
        re.compile(
            r"https?://[^\s\"'<>]*(?:/webhooks?/|[?&](?:token|sig|signature|key|secret|code|access_token)=)[^\s\"'<>]*",
            re.I), MASK),
    (re.compile(r"\b(?:access|refresh|session|api|auth)[_-]?(?:token|key|secret)\b(\s*[:=]\s*)[\"']?[^\s,;\"']{6,}",
                re.I), None),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}\b|\b[0-9a-fA-F]{40}\b"), "<UDID>"),
)


def scrub(text, emails=True):
    """``text`` masked: secret_filter.redact, then what a log holds besides: private key blocks, webhook and signed
    URLs, email addresses (``emails`` False keeps them), device UDIDs and the home folder's name."""
    if not isinstance(text, str) or not text:
        return text
    out = secret_filter.redact(text)
    for pattern, to in _SCRUB:
        if not emails and to == "<email>":
            continue
        out = pattern.sub(lambda m: m.group(0)[:m.start(1) - m.start(0)] + m.group(1) + MASK, out) if to is None \
            else pattern.sub(to, out)
    home = str(Path.home())
    if len(home) > 1:
        out = out.replace(home, "~")
    return out


def _clean(value, depth=0):
    """A JSON value with every string scrubbed and cut, pictures and trees dropped, and depth bounded."""
    if isinstance(value, str):
        if value.startswith("data:") or len(value) > 2000 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", value[:2000]):
            return "[omitted]"
        text = scrub(value)
        return text if len(text) <= STRING_MAX else text[:STRING_MAX] + "…"
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if depth >= 5:
        return "[omitted]"
    if isinstance(value, dict):
        return {scrub(str(k))[:80]: _clean(v, depth + 1) for k, v in list(value.items())[:60]
                if str(k) not in DROP_KEYS}
    if isinstance(value, (list, tuple)):
        return [_clean(v, depth + 1) for v in list(value)[:60]]
    return scrub(str(value))[:STRING_MAX]


def data_dir(runtime):
    """The app's data folder when this agent was given one (the Mac app passes --data-dir), else None: a test or a CLI
    agent never reads the real folder."""
    config = getattr(runtime, "config", None)
    value = getattr(config, "data_dir", None)
    return Path(value) if value else None


def connection_of(device):
    if not isinstance(device, dict):
        return None
    if device.get("transport") == "wifi":
        return "Wi-Fi"
    return {"usb": "USB", "simulator": "Simulator", "wda": "WebDriverAgent address"}.get(device.get("kind"),
                                                                                         device.get("kind"))


def phone_of(runtime, run=None):
    """{model, ios, connection} of the run's phone, else the default phone, else None."""
    fleet = getattr(runtime, "fleet", None)
    try:
        devices = fleet.public() if fleet is not None else []
    except Exception:  # noqa: BLE001 -- a fleet that can't answer leaves the phone out
        devices = []
    wanted = (run or {}).get("device")
    device = next((d for d in devices if wanted and d.get("id") == wanted), None) \
        or next((d for d in devices if d.get("default")), None) or (devices[0] if devices else None)
    if not device:
        return None
    return {"model": device.get("modelName") or device.get("model"), "ios": device.get("ios"),
            "connection": connection_of(device), "state": device.get("state")}


def macos():
    version = platform.mac_ver()[0]
    return f"macOS {version}" if version else platform.system() or None


def last_run(runtime, run_id=None):
    """The run's public dict (with events): ``run_id``'s, else the newest. A run that stays on this Mac
    (harness_api.stays_on_mac: Network X-ray's report) is never in a bundle."""
    lock = getattr(runtime, "lock", None)
    runs = getattr(runtime, "runs", None) or {}
    if lock is not None:
        with lock:
            items = dict(runs)
    else:
        items = dict(runs)
    items = {key: run for key, run in items.items() if not harness_api.stays_on_mac(getattr(run, "extras", None))}
    run = items.get(run_id) if run_id else max(items.values(), key=lambda r: getattr(r, "created_at", 0), default=None)
    if run is None:
        return None
    return run.public(include_events=True)


def log_lines(directory, count=LOG_LINES):
    """The last ``count`` lines of agent.log, scrubbed; [] when there's no log."""
    if directory is None:
        return []
    path = directory / "logs" / "agent.log"
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - 400_000))
            tail = deque(handle.read().decode("utf-8", "replace").splitlines(), maxlen=count)
    except OSError:
        return []
    return [_clean(line) for line in tail if line.strip()]


def build(runtime, run_id=None, app_version=None):
    """The bundle: app, phone, run and log, each masked, with no screenshot, frame or element tree. {text, bytes,
    summary}."""
    run = last_run(runtime, run_id)
    if run_id and run is None:
        from ..api_errors import APIError
        raise APIError("That task isn't on this Mac anymore.", 404, "not_found")
    bundle = {
        "app": {"version": _clean(app_version) if app_version else None, "agent": _agent_version(),
                "macos": macos(), "arch": platform.machine() or None},
        "phone": phone_of(runtime, run),
        "run": None,
        "log": log_lines(data_dir(runtime)),
    }
    if run is not None:
        events = list(run.get("events") or [])[-EVENTS:]
        bundle["run"] = {"id": run.get("id"), "status": run.get("status"), "goal": _clean(run.get("goal")),
                         "app": _clean(run.get("appName")), "engine": run.get("engine"),
                         "createdAt": run.get("createdAt"), "finishedAt": run.get("finishedAt"),
                         "eventCount": run.get("eventCount"), "events": [_clean(e) for e in events]}
    text = json.dumps(bundle, indent=2, ensure_ascii=False)
    # Over the limit: drop log lines, then events, from the oldest, until it fits.
    while len(text.encode("utf-8")) > MAX_BYTES and (bundle["log"] or (bundle["run"] and bundle["run"]["events"])):
        if bundle["log"]:
            del bundle["log"][: max(1, len(bundle["log"]) // 4)]
        else:
            del bundle["run"]["events"][: max(1, len(bundle["run"]["events"]) // 4)]
        text = json.dumps(bundle, indent=2, ensure_ascii=False)
    return {"text": text, "bytes": len(text.encode("utf-8")),
            "summary": {"events": len(bundle["run"]["events"]) if bundle["run"] else 0, "logLines": len(bundle["log"]),
                        "runId": bundle["run"]["id"] if bundle["run"] else None, "phone": bundle["phone"],
                        "macos": bundle["app"]["macos"]}}


def _agent_version():
    from .. import __version__
    return __version__


def check(text):
    """The bundle text a report sends: the one the app showed, scrubbed again (idempotent), at most MAX_BYTES,
    and a JSON object."""
    if not isinstance(text, str) or not text:
        raise ValueError("The diagnostics are the bundle Mobster showed")
    try:
        value = json.loads(text)
    except ValueError:
        raise ValueError("The diagnostics aren't the bundle Mobster showed") from None
    if not isinstance(value, dict):
        raise ValueError("The diagnostics aren't the bundle Mobster showed")
    cleaned = json.dumps(_scrub_all(value), indent=2, ensure_ascii=False)
    if len(cleaned.encode("utf-8")) > MAX_BYTES:
        raise ValueError("The diagnostics are over 500 KB")
    return cleaned


def _scrub_all(value):
    """Every string in a JSON value scrubbed again; nothing cut or dropped (the app showed exactly this)."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {scrub(str(k)): _scrub_all(v) for k, v in value.items() if str(k) not in DROP_KEYS}
    if isinstance(value, list):
        return [_scrub_all(v) for v in value]
    return value

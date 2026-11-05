
import json
import os
import tempfile
import time
from pathlib import Path

from .probe import FAIL, WARN

FILE = "wifi-probe.json"
NOT_RUN = {"state": "not_run", "at": None, "reason": None}
KEEP = 20
UNFINISHED = "The check stopped before it could tell. Run it again with the phone unplugged."


def _path(data_dir):
    return Path(data_dir) / FILE


def _read(data_dir):
    try:
        value = json.loads(_path(data_dir).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _clean(value):
    if not isinstance(value, dict) or value.get("state") not in ("passed", "failed"):
        return dict(NOT_RUN)
    at, reason = value.get("at"), value.get("reason")
    return {"state": value["state"], "at": at if isinstance(at, int) and not isinstance(at, bool) else None,
            "reason": reason[:300] if isinstance(reason, str) and reason else None}


def reason_of(steps):
    """Why a probe did not pass, in the words of the step that failed (else warned): "<title>: <detail>"."""
    for state in (FAIL, WARN):
        step = next((item for item in steps if item.get("state") == state), None)
        if step is not None:
            detail = str(step.get("detail") or "").strip()
            return f"{step.get('title')}: {detail}" if detail else str(step.get("title"))
    return UNFINISHED


def record(data_dir, udid, public, now=None):
    steps = public.get("steps") or []
    usb = next((item for item in steps if item.get("key") == "usb"), None)
    # a probe made with the cable plugged in says nothing about Wi-Fi, so it isn't kept
    if usb is not None and str(usb.get("detail", "")).startswith("plugged in"):
        return None
    entry = {"state": "passed", "at": None, "reason": None} if public.get("ok") else \
        {"state": "failed", "at": None, "reason": reason_of(steps)}
    entry["at"] = int((time.time() if now is None else now) * 1000)
    kept = _read(data_dir)
    kept[str(udid).upper()] = entry
    for old in sorted(kept, key=lambda key: _clean(kept[key])["at"] or 0)[:-KEEP]:
        del kept[old]
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=str(data_dir), prefix=".wifi-probe-")
    try:
        with os.fdopen(handle, "w") as out:
            json.dump(kept, out, separators=(",", ":"))
        os.chmod(temp, 0o600)
        os.replace(temp, _path(data_dir))
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        return None
    return entry


def get(data_dir, udid):
    """``udid``'s last probe, or not_run."""
    return _clean(_read(data_dir).get(str(udid).upper()))


def latest(data_dir):
    """This Mac's most recent probe of any phone, with the phone's udid; not_run when none was kept."""
    kept = {udid: _clean(value) for udid, value in _read(data_dir).items()}
    kept = {udid: value for udid, value in kept.items() if value["state"] != "not_run"}
    if not kept:
        return dict(NOT_RUN)
    udid = max(kept, key=lambda key: kept[key]["at"] or 0)
    return {**kept[udid], "udid": udid}

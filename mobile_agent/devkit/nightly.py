"""`mobster test nightly`: every check overnight on this Mac, and a morning report waiting for you."""

import datetime
import hashlib
import json
import os
import plistlib
import re
import subprocess
from pathlib import Path

from . import DevError, agent_argv, self_argv

LABEL = "dev.mobster.nightly"
FOLDER = ".mobster/nightly"
TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


def label_for(root):
    root = Path(root).resolve()
    slug = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-")[:30] or "project"
    return f"{LABEL}.{slug}-{hashlib.sha256(str(root).encode()).hexdigest()[:8]}"


# A per-user LaunchAgent, not a daemon: it reaches the person's simulators, Keychain and plugged-in iPhones.
def plist_path(root, home=None):
    return Path(home or Path.home()) / "Library" / "LaunchAgents" / f"{label_for(root)}.plist"


def parse_time(text):
    match = TIME.fullmatch(str(text or "").strip())
    if not match:
        raise DevError(f"--at {text} isn't a time.", "Give it as HH:MM on a 24-hour clock, such as 02:00.")
    return int(match.group(1)), int(match.group(2))


def turn_on(root, at, flags, *, home=None, run=None, uid=None):
    """Write and load the LaunchAgent. Returns its plist path."""
    hour, minute = parse_time(at)
    root = Path(root).resolve()
    argv, env = agent_argv()
    path = plist_path(root, home)
    (root / FOLDER).mkdir(parents=True, exist_ok=True)
    data = {"Label": label_for(root), "ProgramArguments": [*argv, "test", "nightly", "run", *flags],
            "WorkingDirectory": str(root), "StartCalendarInterval": {"Hour": hour, "Minute": minute},
            "StandardOutPath": str(root / FOLDER / "launchd.log"),
            "StandardErrorPath": str(root / FOLDER / "launchd.log"),
            "ProcessType": "Background", "LowPriorityIO": True, "Nice": 5}
    if env:
        data["EnvironmentVariables"] = env
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(data))
    run = run or _launchctl
    domain = f"gui/{os.getuid() if uid is None else uid}"
    run(["launchctl", "bootout", domain, str(path)])
    if run(["launchctl", "bootstrap", domain, str(path)]) != 0:
        raise DevError("launchctl didn't load the nightly run.", f"Load it yourself: launchctl bootstrap {domain} "
                                                                 f"{path}")
    return path


def turn_off(root, *, home=None, run=None, uid=None):
    path = plist_path(root, home)
    if not path.is_file():
        return False
    (run or _launchctl)(["launchctl", "bootout", f"gui/{os.getuid() if uid is None else uid}", str(path)])
    path.unlink()
    return True


def _launchctl(argv):
    try:
        return subprocess.run(argv, capture_output=True, timeout=30).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


def status(root, *, home=None):
    """{"on", "at", "flags", "last"}: the schedule and last night's latest.json."""
    path = plist_path(root, home)
    out = {"on": path.is_file(), "at": None, "flags": [], "last": None}
    if path.is_file():
        try:
            data = plistlib.loads(path.read_bytes())
            when = data.get("StartCalendarInterval") or {}
            out["at"] = f"{int(when.get('Hour', 0)):02d}:{int(when.get('Minute', 0)):02d}"
            arguments = data.get("ProgramArguments") or []
            out["flags"] = arguments[arguments.index("run") + 1:] if "run" in arguments else []
        except (ValueError, plistlib.InvalidFileException):
            pass
    try:
        out["last"] = json.loads((Path(root) / FOLDER / "latest.json").read_text())
    except (OSError, ValueError):
        pass
    return out


def morning(doc, folder, date):
    """morning.md: the verdict first, then each check that didn't pass with why and the command to run it again."""
    from ..testkit.reports import counts_text
    from ..verify.report import dots
    summary = doc.get("summary") or {}
    lines = [f"# Overnight checks, {date}", "", f"**{counts_text(summary)}**" + (
        f" in {round((doc.get('durationMs') or 0) / 60000)} min" if doc.get("durationMs") else ""), ""]
    bad = [(check, result) for check in doc.get("checks") or () for result in check.get("results") or ()
           if result.get("status") != "passed" or result.get("flaky")]
    for check, result in bad:
        word = "flaky" if result.get("status") == "passed" else str(result.get("status")).replace("_", " ")
        reason = (result.get("reason") or result.get("flakyReason") or {}).get("message") or ""
        lines.append(f"- **{word}**: {check.get('name')} on {dots(result.get('device') or '')}"
                     + (f". {reason}" if reason else ""))
        if check.get("file"):
            lines.append(f"  Run it again: `mobster test {check['file']}`")
    if not bad:
        lines.append("Every check passed.")
    lines += ["", f"Report: {folder}/index.html"]
    return "\n".join(lines) + "\n"


def run_night(root, flags, *, runner=None, notifier=None, alert=None, today=None, environ=None):
    """One night's run. Returns (exit code, morning.md's text)."""
    environ = os.environ if environ is None else environ
    root = Path(root).resolve()
    date = (today or datetime.date.today()).isoformat()
    folder = root / FOLDER / date
    folder.mkdir(parents=True, exist_ok=True)
    argv, env = self_argv()
    argv = argv + ["test", "--video", "--json", "--html", str(folder), "--junit", str(folder / "junit.xml"), *flags]
    if runner is None:
        def runner(argv, cwd, env):
            done = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, text=True, env=env)
            return done.returncode, done.stdout
    code, out = runner(argv, str(root), {**environ, **env})
    try:
        doc = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        doc = {"summary": {}, "checks": [], "exitCode": code}
    text = morning(doc, str(folder.relative_to(root)), date)
    (folder / "morning.md").write_text(text, encoding="utf-8")
    from ..testkit.reports import counts_text
    headline = counts_text(doc.get("summary") or {}) or "couldn't run"
    (root / FOLDER / "latest.json").write_text(json.dumps({"date": date, "exitCode": code, "headline": headline,
                                                           "morning": str(folder / "morning.md")}, indent=1))
    from .xcode import notify
    (notifier or notify)(f"Overnight checks: {root.name}", headline)
    if code != 0:
        from .. import alerts
        url = alerts.configured_url(environ)
        if url:
            (alert or alerts.deliver)(url, alerts.payload(f"Overnight checks: {root.name}",
                                                          "failed" if code == 1 else "error"))
    return code, text

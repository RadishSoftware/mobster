"""Install a build on a device: `xcrun devicectl device install app` on a USB iPhone, `xcrun simctl install` on
a Mobster simulator. A local signed .app or .ipa only (an .ipa is unpacked first: devicectl installs app
bundles), run without a shell, with plain errors for the usual failures: not signed for this iPhone, Developer
Mode off, the iPhone locked, the device gone.
"""

import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile

from . import PhoneIOError

TIMEOUT = 600
UNPACK_LIMIT = 8 * 1024 ** 3

PROBLEMS = (
    ("developer_mode", re.compile(r"developer ?mode|0xe8000094|DeveloperModeDisabled", re.I),
     "Developer Mode is off on the iPhone.",
     "Turn it on in Settings › Privacy & Security › Developer Mode, restart the iPhone, then try again."),
    ("not_signed", re.compile(r"0xe80080(?:15|16|18|1c)|ApplicationVerificationFailed|provisioning profile|"
                              r"could not be verified|not been signed|code ?sign|signature|unsigned|"
                              r"not (?:authorized|trusted)|integrity|untrusted developer", re.I),
     "The build isn't signed for this iPhone.",
     "Sign it with a development profile that includes this iPhone (Xcode › Signing & Capabilities), build it "
     "for a device, then try again."),
    ("locked", re.compile(r"device is locked|while (?:the device is )?locked|0xe80000e2", re.I),
     "The iPhone is locked.", "Unlock it, then try again."),
    ("no_device", re.compile(r"no devices? (?:matched|are booted|found)|unable to (?:find|locate)|not connected|"
                             r"is not available|disconnected|invalid device|state: shutdown", re.I),
     "The device isn't reachable.", "Plug it in (or boot the simulator) and try again."),
)


def check_path(path):
    """``path`` as a local .app folder or .ipa file that exists; PhoneIOError (usage) otherwise."""
    text = str(path or "").strip()
    if not text or "://" in text:
        raise PhoneIOError("Pass the path of a local .app or .ipa, not a link.", "usage")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    if candidate.suffix.lower() == ".ipa":
        if not candidate.is_file() or not zipfile.is_zipfile(candidate):
            raise PhoneIOError(f"{candidate} isn't an .ipa file.", "usage")
        return candidate
    if candidate.suffix.lower() == ".app":
        if not (candidate.is_dir() and (candidate / "Info.plist").is_file()):
            raise PhoneIOError(f"{candidate} isn't an app bundle (no Info.plist).", "usage")
        return candidate
    raise PhoneIOError(f"{candidate.name} isn't an .app or an .ipa.", "usage")


def unpack(ipa, folder):
    """The .app inside ``ipa`` (Payload/X.app), unpacked into ``folder``; no entry may leave it."""
    root = Path(folder).resolve()
    with zipfile.ZipFile(ipa) as archive:
        total = 0
        for info in archive.infolist():
            target = (root / info.filename).resolve()
            if root not in target.parents and target != root:
                raise PhoneIOError(f"{Path(ipa).name} has an entry outside its folder; Mobster won't unpack it.",
                                   "usage")
            total += info.file_size
            if total > UNPACK_LIMIT:
                raise PhoneIOError(f"{Path(ipa).name} unpacks to more than 8 GB.", "usage")
        archive.extractall(root)
    apps = sorted((root / "Payload").glob("*.app"))
    if len(apps) != 1:
        raise PhoneIOError(f"{Path(ipa).name} has no single app in its Payload folder.", "usage")
    return apps[0]


def bundle_id(app):
    try:
        with open(Path(app) / "Info.plist", "rb") as handle:
            value = plistlib.load(handle).get("CFBundleIdentifier")
        return value if isinstance(value, str) else None
    except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
        return None


def install_argv(record, app, json_path=None):
    """The command that installs ``app`` (an .app) on ``record``'s device. No shell: a list of arguments."""
    kind, udid = record.get("kind"), record.get("udid") or record.get("id")
    if kind == "usb":
        argv = ["xcrun", "devicectl", "device", "install", "app", "--device", str(udid), str(app)]
        if json_path:
            argv += ["--json-output", str(json_path)]
        return argv
    if kind == "simulator":
        return ["xcrun", "simctl", "install", str(udid), str(app)]
    raise PhoneIOError(f"Mobster can't install a build on “{record.get('name')}”: it's a WebDriverAgent address, "
                       "not an iPhone or simulator it knows.", "usage", "Install with Xcode instead.")


def explain(output, returncode):
    """PhoneIOError for a failed install, from the tool's output."""
    for code, pattern, message, fix in PROBLEMS:
        if pattern.search(output or ""):
            return PhoneIOError(message, code, fix)
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    last = next((line for line in reversed(lines) if line.lower().startswith(("error", "an error"))),
                lines[-1] if lines else "")
    return PhoneIOError(f"The install failed (exit {returncode}){': ' + last[:300] if last else ''}.", "failed")


def run_streaming(argv, progress=None, timeout=TIMEOUT):
    """(exit code, output) of ``argv``, each line handed to ``progress`` as it comes."""
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   text=True, errors="replace")
    except FileNotFoundError:
        raise PhoneIOError("Xcode's command-line tools aren't installed.", "tools",
                           "Install Xcode, open it once, then try again.") from None
    lines = []

    def pump():
        # Its own thread: a tool that hangs without printing must still meet the time limit.
        for line in process.stdout:
            lines.append(line)
            if progress is not None and line.strip():
                try:
                    progress(line.rstrip())
                except Exception:
                    pass
    reader = threading.Thread(target=pump, daemon=True, name="mobster-install-output")
    reader.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        limit = f"{timeout / 60:g} minutes" if timeout >= 60 else f"{timeout:g} seconds"
        raise PhoneIOError(f"The install took over {limit}; Mobster stopped it.", "failed") from None
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        reader.join(5)
        if not reader.is_alive():
            process.stdout.close()
    return code, "".join(lines)


def installed_bundle(json_path):
    try:
        data = json.loads(Path(json_path).read_text())
    except (OSError, ValueError):
        return None
    apps = ((data.get("result") or {}).get("installedApplications") or []) if isinstance(data, dict) else []
    for app in apps:
        if isinstance(app, dict) and isinstance(app.get("bundleID"), str):
            return app["bundleID"]
    return None


def install(record, path, *, runner=None, progress=None):
    """Install the .app or .ipa at ``path`` on ``record``'s device. Returns {ok, device, name, path, bundle_id,
    seconds}; raises PhoneIOError."""
    source = check_path(path)
    real = runner is None
    runner = runner or (lambda argv: run_streaming(argv, progress))
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mobster-install-") as folder:
        app = unpack(source, folder) if source.suffix.lower() == ".ipa" else source
        json_path = Path(folder) / "devicectl.json"
        argv = install_argv(record, app, json_path)
        if real and shutil.which(argv[0]) is None:
            raise PhoneIOError("Xcode's command-line tools aren't installed.", "tools",
                               "Install Xcode, open it once, then try again.")
        if progress is not None:
            progress(f"Installing {app.name} on {record.get('name') or record.get('id')}…")
        code, output = runner(argv)
        if code != 0:
            raise explain(output, code)
        bundle = installed_bundle(json_path) or bundle_id(app)
    return {"ok": True, "device": record.get("id"), "name": record.get("name"), "path": str(source),
            "bundle_id": bundle, "seconds": round(time.monotonic() - started, 1)}


def describe_path(path):
    """A path for messages: the user's home as ~."""
    home = os.path.expanduser("~")
    text = str(path)
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text

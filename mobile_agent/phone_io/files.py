"""Files to and from an iPhone app's Documents folder, the one Files shows under On My iPhone › <App>.

Only apps that share files (``UIFileSharingEnabled``: Pages, Numbers, Keynote, VLC, Documents…) have one; ``apps``
lists them from the phone's installed apps (ideviceinstaller). ``put``, ``get`` and ``ls`` go through Apple's file
service for those folders (AFC house_arrest, "VendDocuments") with ``afcclient``: the copy bundled in the Mac app,
else Homebrew's. Nothing else on the phone is reachable: not other apps' data, not the camera roll.

Rules every caller gets:
- **USB only.** A phone on Wi-Fi answers ``needs_cable`` until the wireless probe shows the file service runs
  through the encrypted tunnel. afcclient is never given ``-n``/``--network``.
- **Names**: sniff.clean_name (no ``/``, ``..`` or control characters), so nothing lands outside the app's folder;
  remote paths are always ``/<name>`` at the folder's top.
- **100 MB** a file. A put never overwrites: a name that is taken becomes "menu 2.pdf" (the CLI's ``--replace``
  overwrites on purpose). Each put is checked by listing the folder after.
- Under ``MOBSTER_NO_DEVICES=1`` (local CI) the real tools never run: RuntimeError.

Who approves: the agent's PUT_FILE asks first (its title), the Mac app's "Put on iPhone…" is the user's own click
(and needs the app session), ``mobster phone file put`` is the user's own command, and MCP's put_file needs
``mobster mcp --allow-files``.
"""

import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import time

from . import PhoneIOError

MAX_BYTES = 100 * 1024 * 1024
TIMEOUT = 120
LIST_TIMEOUT = 30
BUNDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,154}")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
LINE = re.compile(r"^([-dlfbcs])[rwx-]{9}\s+\d+\s+mobile\s+mobile\s+(\d+)\s+(\d{2} \S{3} \d{4} \d{2}:\d{2}:\d{2}) (.+)$")
NO_SHARING = re.compile(r"UIFileSharingEnabled|InstallationLookupFailed|not present on the device", re.I)
NEEDS_TOOLS = ("Install the iPhone tools: brew install libimobiledevice", "tools")
NEEDS_CABLE = "Files move only over the cable for now. Plug the iPhone into this Mac, then try again."


def afc_tool():
    """afcclient: the app's own copy, then the usual folders, then PATH; None when there is none."""
    from ..device_manager import TOOL_DIRS, bundled_tools_dir, executable
    bundled = bundled_tools_dir()
    if bundled is not None and (found := executable(bundled, "afcclient")):
        return found
    for directory in TOOL_DIRS:
        if found := executable(directory, "afcclient"):
            return found
    return shutil.which("afcclient")


def installer_tool():
    from ..device_manager import tool
    return tool("ideviceinstaller")


def _run(argv, timeout):
    """(exit code, standard output and error) of ``argv``, no shell, no colours, the C locale."""
    from ..native_helpers import require_devices
    require_devices()
    env = dict(os.environ, NO_COLOR="1", LC_ALL="C", LANG="C")
    try:
        done = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        raise PhoneIOError(f"The iPhone didn't answer within {timeout} seconds.", "failed",
                           "Keep it unlocked and plugged in, then try again.") from None
    except OSError as error:
        raise PhoneIOError(f"The iPhone tools couldn't run ({type(error).__name__}).", "tools") from None
    return done.returncode, (done.stdout or "") + (done.stderr or "")


class PhoneFiles:
    """The file service for one device ``record`` (devices.py's shape: kind, udid, name, transport). ``runner``
    replaces the tools in tests: ``runner(argv, timeout) -> (code, output)``."""

    def __init__(self, record, runner=None, afc=None, installer=None):
        self.record = record or {}
        self._runner = runner
        self._afc_path, self._installer = afc, installer
        if self.record.get("kind") != "usb":
            what = "a simulator" if self.record.get("kind") == "simulator" else "not an iPhone on a cable"
            raise PhoneIOError(f"“{self.name}” is {what}. Mobster moves files to iPhones plugged into this Mac.",
                               "usage")
        if self.record.get("transport") == "wifi":
            raise PhoneIOError(f"“{self.name}” is on Wi-Fi. " + NEEDS_CABLE, "needs_cable")
        udid = str(self.record.get("udid") or "")
        if not re.fullmatch(r"[0-9A-Fa-f-]{24,40}", udid):
            raise PhoneIOError(f"Mobster doesn't know which iPhone “{self.name}” is yet.", "no_device",
                               "Set it up in the Mobster app.")
        self.udid = udid

    @property
    def name(self):
        return self.record.get("name") or self.record.get("id") or "the iPhone"

    def run(self, argv, timeout):
        if self._runner is not None:
            return self._runner(argv, timeout)
        return _run(argv, timeout)

    def _afcclient(self):
        found = self._afc_path or afc_tool()
        if not found:
            raise PhoneIOError("Mobster needs the iPhone tools to move files.", NEEDS_TOOLS[1], NEEDS_TOOLS[0])
        return found

    # -- the apps that share files -------------------------------------------------------------------------------

    def apps(self):
        """[{"bundleId", "name"}] of the apps whose Documents folder shows in Files, sorted by name."""
        tool = self._installer or installer_tool()
        if not tool:
            raise PhoneIOError("Mobster needs the iPhone tools to list the apps.", NEEDS_TOOLS[1], NEEDS_TOOLS[0])
        argv = [tool, "-u", self.udid, "list", "--all", "--xml", "-a", "CFBundleIdentifier", "-a",
                "CFBundleDisplayName", "-a", "CFBundleName", "-a", "UIFileSharingEnabled"]
        code, output = self.run(argv, LIST_TIMEOUT)
        if code != 0:
            raise _explain(output, self.name)
        start = output.find("<?xml")
        try:
            items = plistlib.loads(output[start if start >= 0 else 0:].encode())
        except Exception:  # noqa: BLE001
            raise PhoneIOError("The iPhone's app list couldn't be read.", "failed") from None
        apps = []
        for item in items if isinstance(items, list) else ():
            if not isinstance(item, dict) or item.get("UIFileSharingEnabled") is not True:
                continue
            bundle = item.get("CFBundleIdentifier")
            if not isinstance(bundle, str) or not BUNDLE.fullmatch(bundle):
                continue
            name = item.get("CFBundleDisplayName") or item.get("CFBundleName") or bundle
            apps.append({"bundleId": bundle, "name": " ".join(str(name).split())[:60]})
        return sorted(apps, key=lambda app: (app["name"].casefold(), app["bundleId"]))

    # -- one app's folder ---------------------------------------------------------------------------------------

    def _afc(self, app, *command, timeout=TIMEOUT):
        if not isinstance(app, str) or not BUNDLE.fullmatch(app):
            raise PhoneIOError("Name the app by its bundle ID, such as com.apple.Pages.", "usage")
        # "--" ends afcclient's own options: macOS's getopt_long reorders arguments, so without it the command's
        # flags ("ls -l", "put -f") would be read as afcclient's and refused as unknown options.
        argv = [self._afcclient(), "-u", self.udid, "--documents", app, "--", *map(str, command)]
        code, output = self.run(argv, timeout)
        output = ANSI.sub("", output)
        if NO_SHARING.search(output):
            raise PhoneIOError(f"{app} doesn't keep files you can see in Files, or it isn't on {self.name}.",
                               "no_file_sharing", "Choose an app that does, such as Pages, Numbers or Keynote.")
        if code != 0:
            raise _explain(output, self.name)
        errors = [line for line in output.splitlines() if line.startswith(("Error:", "ERROR:"))]
        return output, errors

    def ls(self, app):
        """[{"name", "bytes", "modifiedAt" (ms), "folder"}] at the top of ``app``'s folder."""
        output, errors = self._afc(app, "ls", "-l", "/", timeout=LIST_TIMEOUT)
        if errors:
            raise _explain("\n".join(errors), self.name)
        entries = []
        for line in output.splitlines():
            found = LINE.match(line.rstrip("\n"))
            if not found:
                continue
            kind, size, stamp, name = found.groups()
            try:
                modified = round(time.mktime(time.strptime(stamp, "%d %b %Y %H:%M:%S")) * 1000)
            except (ValueError, OverflowError):
                modified = None
            entries.append({"name": name, "bytes": int(size), "modifiedAt": modified, "folder": kind == "d"})
        return sorted(entries, key=lambda e: e["name"].casefold())

    def put(self, app, local, name=None, replace=False):
        """Copy the Mac file ``local`` into ``app``'s folder as ``name`` (default: its own). Returns
        {"name", "path", "bytes", "app"}: the name it got ("menu 2.pdf" when "menu.pdf" was taken)."""
        from ..attachments.sniff import clean_name
        source = Path(local).expanduser()
        try:
            size = source.stat().st_size
        except OSError:
            raise PhoneIOError(f"There is no file at {source}.", "usage") from None
        if not source.is_file():
            raise PhoneIOError(f"{source.name} isn't a file.", "usage")
        if size > MAX_BYTES:
            raise PhoneIOError(f"{source.name} is over 100 MB; Mobster moves files up to 100 MB.", "usage")
        wanted = clean_name(name or source.name)
        existing = {entry["name"] for entry in self.ls(app)}
        target = wanted if replace or wanted not in existing else _free_name(wanted, existing)
        command = ["put", "-f"] if replace else ["put"]
        _, errors = self._afc(app, *command, str(source.resolve()), "/" + target)
        if errors:
            raise _explain("\n".join(errors), self.name)
        arrived = next((e for e in self.ls(app) if e["name"] == target and not e["folder"]), None)
        if arrived is None or arrived["bytes"] != size:
            raise PhoneIOError(f"{target} didn't arrive whole on {self.name}.", "failed",
                               "Keep the iPhone unlocked and plugged in, then try again.")
        return {"name": target, "path": "/" + target, "bytes": size, "app": app}

    def get(self, app, name, folder):
        """Copy ``name`` from ``app``'s folder into the Mac folder ``folder``. Returns the local Path."""
        from ..attachments.sniff import clean_name
        wanted = clean_name(name)
        if wanted != name:
            raise PhoneIOError(f"There is no file named {name!r} in {app}'s folder.", "usage")
        entry = next((e for e in self.ls(app) if e["name"] == wanted), None)
        if entry is None:
            raise PhoneIOError(f"There is no file named {wanted} in {app}'s folder on {self.name}.", "not_found",
                               "List the folder with `mobster phone file ls --app " + app + "`.")
        if entry["folder"]:
            raise PhoneIOError(f"{wanted} is a folder; Mobster copies files.", "usage")
        if entry["bytes"] > MAX_BYTES:
            raise PhoneIOError(f"{wanted} is over 100 MB; Mobster moves files up to 100 MB.", "usage")
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="mobster-get-", dir=folder) as scratch:
            partial = Path(scratch) / "file"
            _, errors = self._afc(app, "get", "/" + wanted, str(partial))
            if errors or not partial.is_file():
                raise _explain("\n".join(errors) or "Error: nothing arrived", self.name)
            if partial.stat().st_size != entry["bytes"]:
                raise PhoneIOError(f"{wanted} didn't arrive whole.", "failed", "Try again.")
            target = folder / wanted
            if target.exists():
                target = folder / _free_name(wanted, {p.name for p in folder.iterdir()})
            os.replace(partial, target)
        return target


def _free_name(name, taken):
    """``name`` made unique among ``taken``, as Finder does: "menu 2.pdf", "menu 3.pdf"…"""
    stem, ext = os.path.splitext(name)
    for number in range(2, 10_000):
        candidate = f"{stem} {number}{ext}"
        if candidate not in taken:
            return candidate
    raise PhoneIOError("That folder has too many files with this name.", "failed")


def _explain(output, name):
    """A PhoneIOError for a failed tool run, from its output."""
    text = " ".join((output or "").split())
    if re.search(r"No device found|Device .* not found|not connected", text, re.I):
        return PhoneIOError(f"{name} isn't plugged in.", "no_device", "Plug it into this Mac and unlock it.")
    if re.search(r"lockdown|pair|trust|password protected|PasswordProtected", text, re.I):
        return PhoneIOError(f"{name} is locked or doesn't trust this Mac.", "locked",
                            "Unlock it, tap Trust if it asks, then try again.")
    if re.search(r"Object not found|No such file", text, re.I):
        return PhoneIOError("That file isn't on the iPhone.", "not_found")
    return PhoneIOError(f"Moving the file failed{': ' + text[:200] if text else ''}.", "failed",
                        "Keep the iPhone unlocked and plugged in, then try again.")

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
import plistlib
import re
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from . import PhoneIOError

MAX_BYTES = 100 * 1024 * 1024
TIMEOUT = 120
LIST_TIMEOUT = 30
BUNDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,154}")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
LINE = re.compile(r"^([-dlfbcs])[rwx-]{9}\s+\d+\s+mobile\s+mobile\s+(\d+)\s+(\d{2} \S{3} \d{4} \d{2}:\d{2}:\d{2}) "
                  r"(.+)$")
NO_SHARING = re.compile(r"UIFileSharingEnabled|InstallationLookupFailed|not present on the device", re.I)
NEEDS_TOOLS = ("Install the iPhone tools: brew install libimobiledevice", "tools")
NEEDS_CABLE = "Files move only over the cable for now. Plug the iPhone into this Mac, then try again."


def installer_tool():
    from ..mac_tools import tool
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
    """The file service for one device ``record`` (kind, udid, name, transport)."""

    def __init__(self, record, runner=None, afc=None, installer=None, container=False):
        self.record = record or {}
        # container: the app's whole sandbox (house_arrest's VendContainer), its Documents folder inside; only a
        # development build of your own (signed with get-task-allow) opens it. Otherwise the folder Files shows.
        self.container = bool(container)
        self.base = "/Documents/" if self.container else "/"
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
        from ..mac_tools import tool
        found = self._afc_path or tool("afcclient")
        if not found:
            raise PhoneIOError("Mobster needs the iPhone tools to move files.", NEEDS_TOOLS[1], NEEDS_TOOLS[0])
        return found

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

    def _afc(self, app, *command, timeout=TIMEOUT):
        if not isinstance(app, str) or not BUNDLE.fullmatch(app):
            raise PhoneIOError("Name the app by its bundle ID, such as com.apple.Pages.", "usage")
        # "--" ends afcclient's own options: macOS's getopt_long reorders arguments, so without it the command's
        # flags ("ls -l", "put -f") would be read as afcclient's and refused as unknown options.
        argv = [self._afcclient(), "-u", self.udid, "--container" if self.container else "--documents", app, "--",
                *map(str, command)]
        code, output = self.run(argv, timeout)
        output = ANSI.sub("", output)
        if NO_SHARING.search(output) or (self.container and re.search(r"InstallationLookupFailed|not allowed|"
                                                                        r"Permission", output, re.I)):
            if self.container:
                raise PhoneIOError(f"{app}'s own folder opens only for a development build of yours on {self.name}.",
                                   "no_file_sharing", "Install a debug build from Xcode, or use its Documents folder.")
            raise PhoneIOError(f"{app} doesn't keep files you can see in Files, or it isn't on {self.name}.",
                               "no_file_sharing", "Choose an app that does, such as Pages, Numbers or Keynote.")
        if code != 0:
            raise _explain(output, self.name)
        errors = [line for line in output.splitlines() if line.startswith(("Error:", "ERROR:"))]
        return output, errors

    def ls(self, app):
        """[{"name", "bytes", "modifiedAt" (ms), "folder"}] at the top of ``app``'s folder."""
        output, errors = self._afc(app, "ls", "-l", self.base, timeout=LIST_TIMEOUT)
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

    def put(self, app, local, name=None, replace=False, *, before_copy=None):
        """Copy ``local`` into ``app``'s folder, never over a name already there; ``before_copy()`` may refuse."""
        from ..attachments.sniff import clean_name
        source = Path(local).expanduser()
        wanted = clean_name(name or source.name)
        with snapshot(source, MAX_BYTES) as staged:
            size = staged.stat().st_size
            existing = {entry["name"] for entry in self.ls(app)}
            target = wanted if replace or wanted not in existing else _free_name(wanted, existing)
            command = ["put", "-f"] if replace else ["put"]
            if before_copy is not None:
                before_copy()
            _, errors = self._afc(app, *command, str(staged), self.base + target)
            if errors:
                raise _explain("\n".join(errors), self.name)
            arrived = next((e for e in self.ls(app) if e["name"] == target and not e["folder"]), None)
            if arrived is None or arrived["bytes"] != size:
                raise PhoneIOError(f"{target} didn't arrive whole on {self.name}.", "failed",
                                   "Keep the iPhone unlocked and plugged in, then try again.")
            return {"name": target, "path": self.base + target, "bytes": size, "app": app}

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
            _, errors = self._afc(app, "get", self.base + wanted, str(partial))
            if errors or not partial.is_file():
                raise _explain("\n".join(errors) or "Error: nothing arrived", self.name)
            if partial.stat().st_size != entry["bytes"]:
                raise PhoneIOError(f"{wanted} didn't arrive whole.", "failed", "Try again.")
            return land(partial, folder, wanted)


def land(partial, folder, name, *, exact=False):
    """Move the finished ``partial`` into ``folder`` as ``name``, or a free name next to it ("menu 2.pdf") when ``name``
    is taken. Returns the local Path. The name is claimed with a hard link, which fails if the name exists, so two
    runs landing into one folder never replace each other's file; ``partial`` must be on the same filesystem.
    ``exact`` raises FileExistsError instead of choosing another name."""
    folder = Path(folder)
    stem, ext = os.path.splitext(name)
    for number in range(1, 10_000):
        target = folder / (name if number == 1 else f"{stem} {number}{ext}")
        try:
            os.link(partial, target)
        except FileExistsError:
            if exact:
                raise
            continue
        except OSError:  # a volume without hard links (exFAT, some network shares)
            if target.exists():
                if exact:
                    raise FileExistsError(target) from None
                continue
            os.replace(partial, target)
            return target
        Path(partial).unlink()
        return target
    raise PhoneIOError("That folder has too many files with this name.", "failed")


@contextmanager
def snapshot(source, limit):
    """A bounded, private copy of one regular source file, removed after its consumer finishes."""
    source = Path(source).expanduser()
    try:
        stream = open(source, "rb", opener=lambda path, flags: os.open(path, flags | os.O_NONBLOCK))
    except OSError:
        raise PhoneIOError(f"There is no file at {source}.", "usage") from None
    with stream, tempfile.TemporaryDirectory(prefix="mobster-upload-") as folder:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise PhoneIOError(f"{source.name} isn't a file.", "usage")
        staged = Path(folder) / source.name
        with open(staged, "xb", opener=lambda path, flags: os.open(path, flags, 0o600)) as out:
            size = 0
            while block := stream.read(min(1 << 20, limit + 1 - size)):
                size += len(block)
                if size > limit:
                    units = f"{limit / 1024 ** 3:g} GB" if limit >= 1024 ** 3 else f"{limit / 1024 ** 2:g} MB"
                    raise PhoneIOError(f"{source.name} is over {units}.", "usage")
                out.write(block)
        yield staged


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

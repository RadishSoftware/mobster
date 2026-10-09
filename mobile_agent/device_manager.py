"""The USB iPhone lifecycle, driven from the setup flow.

Everything the manual recipe in docs/usb-wda.md does, as a supervised service:
find the tools and the connected iPhone, find the user's Apple development
teams, fetch and build WebDriverAgent for that phone, and keep the runner and
the USB relay (API 8100, video 9100) alive until stopped.

Processes run in their own process groups so stopping them also stops the
children xcodebuild spawns. Build and runner output goes to log files; the API
exposes only a short tail and a plain-language error.
"""

import functools
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from . import setup_errors
from . import signing
from . import wda_source
from .device_info import MODEL_NAMES

# The relay and WDA listen here only. Without it iproxy listened on every interface
# of the Mac (lsof: *:8100) and WDA on the phone's Wi-Fi address; neither has auth.
LOOPBACK = "127.0.0.1"
TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/Applications/Xcode.app/Contents/Developer/usr/bin")
# The iPhone connection tools: libimobiledevice's and libusbmuxd's. Mobster for Mac carries its own build of them
# (desktop/scripts/build-iphone-tools.sh), found before any Homebrew copy, so nobody needs Homebrew or Terminal.
IPHONE_TOOLS = ("idevice_id", "ideviceinfo", "ideviceinstaller", "iproxy")
# /usr/bin/xcodebuild and /usr/bin/git exist on every Mac: they are stubs that forward to the active
# developer directory (and fail, or open an install dialog, without one). They never count as Xcode or
# git: both come from the Xcode app itself, git also from the Command Line Tools or Homebrew.
XCODE_SHIMS = frozenset({"xcodebuild", "git"})
CLT_DIR = "/Library/Developer/CommandLineTools"
XCODE_APP_DIRS = ("/Applications", str(Path.home() / "Applications"))
GIT_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", CLT_DIR + "/usr/bin")
# Xcode's state takes a few short xcodebuild calls: a working Xcode is re-checked rarely, one that
# needs fixing often enough that the fix shows up on its own.
XCODE_OK_TTL, XCODE_RETRY_TTL = 300.0, 10.0
TEAMS_TTL = 60.0
USB_TTL = 5.0
WDA_PORT, VIDEO_PORT = 8100, 9100
TEAM_PATTERN = re.compile(r"[A-Z0-9]{10}")
UDID_PATTERN = re.compile(r"[0-9A-Fa-f-]{24,40}")
RESTART_DELAY = 2.0
MAX_RESTART_DELAY = 30.0
# A run at least this long counts as healthy and resets the restart backoff.
HEALTHY_RUN_SECONDS = 60.0
WATCH_INTERVAL = 5.0
# WDA can wedge: /status still answers while every call that needs the phone's UI
# (screen reads, taps, video) hangs, e.g. with the phone locked or behind Control
# Center or the iOS 26 Siri glow (measured 2026-09-23: reads hung for minutes).
# /wda/locked is cheap when WDA is healthy, so it is the probe.
PROBE_INTERVAL = 10.0
PROBE_TIMEOUT = 3.0
WEDGED_RESTART_SECONDS = 90.0
MIN_RESTART_GAP = 180.0
LOG_TAIL = 40
# Rebranded so free/personal teams can sign them (the stock com.facebook ids fail). The runner's id
# also gets the team's suffix at build time (signing.runner_bundle_id): App IDs are unique across
# every Apple team, so a free account can't register an id someone else's team already has.
BUNDLE_REBRAND = {"com.facebook.WebDriverAgentRunner": "app.mobster.wda.runner",
                  "com.facebook.WebDriverAgentLib": "app.mobster.wda.lib"}
# The runner earlier versions installed under the plain id; removed after the first build with the suffix.
LEGACY_RUNNER = "app.mobster.wda.runner.xctrunner"
RUNNER_ID = re.compile(r"(?<![\w.])(?:com\.facebook\.WebDriverAgentRunner|app\.mobster\.wda\.runner(?:\.[a-z0-9]{10})?)(?=[\s\";])")
# A runner whose signature ends within this long is renewed (runner_renewal.py), and a build in this
# window first drops Xcode's cached profile so the new signature starts a fresh 7 days.
RENEW_WINDOW = 48 * 3600
# To prove a renewal on a real phone in one sitting, MOBSTER_RENEW_WINDOW_HOURS=170 (in the agent's
# env file, e.g. the Mac app's agent.env) widens the window past a fresh 7-day signature, so the
# runner renews within seconds of Mobster opening. Remove it afterwards: a signature is never older
# than 168 hours, so with it set the runner is always due (retried at most every 6 hours).
MAX_RENEW_WINDOW_HOURS = 170


def renew_window(env=None):
    """RENEW_WINDOW, or MOBSTER_RENEW_WINDOW_HOURS (1 to 170) when set for a verification run."""
    value = (os.environ if env is None else env).get("MOBSTER_RENEW_WINDOW_HOURS")
    try:
        hours = float(value) if value else None
    except ValueError:
        hours = None
    if hours is None or not 1 <= hours <= MAX_RENEW_WINDOW_HOURS:
        return RENEW_WINDOW
    return int(hours * 3600)
# xcodebuild failures users can act on, in their words.
BUILD_HINTS = (
    ("isn't registered in your developer account",
     "This iPhone isn't registered with your Apple team. Open Xcode › Settings › Accounts, select the team, "
     "and let Xcode register the device (or register it in the Apple Developer portal)."),
    ("No Accounts", "Sign in to Xcode with your Apple ID: Xcode › Settings › Accounts."),
    ("No signing certificate", "Xcode has no signing certificate for this team yet. Open Xcode › Settings › Accounts "
                               "and choose Manage Certificates › Apple Development."),
    ("Developer Mode", "Turn on Developer Mode on the iPhone: Settings › Privacy & Security › Developer Mode, then restart it."),
    ("is not available", "The iPhone isn't available to Xcode. Unlock it, keep it connected, and trust this Mac."),
    ("requires a development team", "Choose your Apple development team and build again."),
    ("requires Xcode, but active developer directory",
     "This Mac is set to use the Command Line Tools instead of Xcode. In Terminal, run "
     "sudo xcode-select -s /Applications/Xcode.app, then build again."),
    ("agreed to the Xcode", "Accept the Xcode license: open Xcode and agree, or run sudo xcodebuild -license accept "
                            "in Terminal. Then build again."),
    ("invalid active developer path", "Install Xcode from the Mac App Store and open it once, then build again."),
    ("Could not resolve host", "Mobster couldn't download WebDriverAgent. Check your internet connection and build again."),
    ("Unable to find a destination", "Xcode can't use your iPhone yet. Unlock it, keep it connected and turn on "
                                     "Developer Mode. If iOS is newer than your Xcode, update Xcode."),
)
# A free Apple account's signature lasts 7 days; after that iOS refuses to launch the runner.
EXPIRED_HINT = "Mobster's helper needs a refresh: its signature expired. Refresh it in Setup."
# Why a started runner is not answering yet, read from the tail of its log.
RUNNER_HINTS = (
    ("because the device is locked", "Unlock your iPhone. Mobster's helper starts as soon as it is unlocked."),
    ("Developer Mode", "Turn on Developer Mode on the iPhone: Settings › Privacy & Security › Developer Mode."),
    ("has not been explicitly trusted", "On the iPhone, open Settings › General › VPN & Device Management and trust "
                                        "your developer app."),
    ("invalid code signature", "On the iPhone, open Settings › General › VPN & Device Management and trust "
                               "your developer app."),
    ("is not available", "The iPhone isn't available to Xcode. Keep it connected, unlocked and trusting this Mac."),
)
RUNNER_LOG_WINDOW = 16384
# Loopback checks for the patched WDA: browsers always send Host, and Origin on POSTs and
# fetches, so a web page reaching the Mac's relay (directly or by DNS rebinding) is refused.
WDA_LOCAL_HOST = """
// Mobster: loopback clients only (the USB relay). Browsers always send Host; USB clients may not.
static BOOL FBMobsterLocalHost(NSString * _Nullable host)
{
  if (0 == host.length) {
    return YES;
  }
  NSString *name = (NSString * _Nonnull)host.lowercaseString;
  BOOL bracketed = [name hasPrefix:@"["];
  NSRange end = bracketed ? [name rangeOfString:@"]"] : [name rangeOfString:@":"];
  if (NSNotFound != end.location) {
    name = [name substringToIndex:end.location + (bracketed ? 1 : 0)];
  }
  return [name isEqualToString:@"127.0.0.1"] || [name isEqualToString:@"localhost"] || [name isEqualToString:@"[::1]"];
}
"""
WDA_LOCAL_STREAM = """
static BOOL FBMobsterLocalStreamRequest(NSData *data)
{
  NSString *head = [[NSString alloc] initWithData:data encoding:NSISOLatin1StringEncoding].lowercaseString;
  NSString *host = nil;
  for (NSString *line in [head componentsSeparatedByString:@"\\r\\n"]) {
    if ([line hasPrefix:@"origin:"]) {
      return NO;
    }
    if ([line hasPrefix:@"host:"]) {
      host = [[line substringFromIndex:5] stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
    }
  }
  return FBMobsterLocalHost(host);
}

"""
# Idempotent (file, anchor, patched) edits: every build applies them to the clone.
WDA_PATCHES = (
    # The MJPEG socket binds USE_IP like the API; upstream it always listens on every interface.
    ("WebDriverAgentLib/Routing/FBWebServer.m",
     "initWithPort:(uint16_t)FBConfiguration.sharedInstance.mjpegServerPort];\n",
     "initWithPort:(uint16_t)FBConfiguration.sharedInstance.mjpegServerPort];\n"
     "  // Mobster: the MJPEG socket listens where the API does (USE_IP), not on every interface.\n"
     "  self.screenshotsBroadcaster.interface = FBConfiguration.sharedInstance.bindingIPAddress;\n"),
    ("WebDriverAgentLib/Routing/FBHTTPServer.m",
     "static const NSUInteger FBMaxRequestHeaderSize = 16 * 1024;\n",
     "static const NSUInteger FBMaxRequestHeaderSize = 16 * 1024;\n" + WDA_LOCAL_HOST),
    ("WebDriverAgentLib/Routing/FBHTTPServer.m",
     "  NSUInteger contentLength = 0;\n  if (![self resolveBodyLength:",
     "  if (nil != requestHeaders[@\"origin\"] || !FBMobsterLocalHost(requestHeaders[@\"host\"])) {\n"
     "    RouteResponse *forbidden = [RouteResponse new];\n"
     "    [FBResponseWithStatus([FBCommandStatus invalidArgumentErrorWithMessage:@\"Local clients only\" traceback:nil])\n"
     "     dispatchWithResponse:forbidden];\n"
     "    [self failClient:client withResponse:forbidden];\n"
     "    return nil;\n"
     "  }\n"
     "  NSUInteger contentLength = 0;\n  if (![self resolveBodyLength:"),
    ("WebDriverAgentLib/Utilities/FBMjpegServer.m",
     "static NSString *const SERVER_NAME = @\"WDA MJPEG Server\";\n",
     "static NSString *const SERVER_NAME = @\"WDA MJPEG Server\";\n" + WDA_LOCAL_HOST + WDA_LOCAL_STREAM),
    ("WebDriverAgentLib/Utilities/FBMjpegServer.m",
     "  [FBLogger log:@\"Starting screenshots broadcast for the client\"];\n",
     "  if (!FBMobsterLocalStreamRequest(data)) {\n"
     "    [FBLogger log:@\"Refused a screenshots broadcast request from a browser\"];\n"
     "    nw_connection_cancel(client);\n"
     "    return;\n"
     "  }\n"
     "  [FBLogger log:@\"Starting screenshots broadcast for the client\"];\n"),
)


def executable(directory, name):
    path = Path(directory) / name
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def bundled_tools_dir():
    """Where the app's own iPhone connection tools are: MOBSTER_IPHONE_TOOLS, else the desktop app's
    Contents/Resources/iphone-tools/bin next to the frozen runtime. None outside the app (a source checkout)."""
    override = os.environ.get("MOBSTER_IPHONE_TOOLS")
    if override:
        return Path(override)
    frozen = getattr(sys, "_MEIPASS", None)
    if not frozen:
        return None
    for resources in (Path(frozen).parent / "Resources", Path(sys.executable).resolve().parent.parent / "Resources"):
        if (resources / "iphone-tools" / "bin").is_dir():
            return resources / "iphone-tools" / "bin"
    return None


def bundled_tool(name):
    """The app's own copy of an iPhone connection tool, or None."""
    directory = bundled_tools_dir() if name in IPHONE_TOOLS else None
    return executable(directory, name) if directory else None


def tool(name):
    if name in XCODE_SHIMS:
        return xcode_tool(name)
    if path := bundled_tool(name):
        return path
    for directory in TOOL_DIRS:
        if path := executable(directory, name):
            return path
    return shutil.which(name)


def is_xcode(path):
    """A developer directory inside an Xcode app (not the Command Line Tools)."""
    path = Path(path)
    return (path.name == "Developer" and path.parent.name == "Contents" and path.parent.parent.suffix == ".app"
            and executable(path / "usr" / "bin", "xcodebuild") is not None)


def developer_dir():
    """(xcode, selected): the Xcode developer directory to build with, and the active one.

    selected is what `xcode-select -p` reports: an Xcode, the Command Line Tools, or None when
    nothing is set. xcode is the selected directory when it is inside an Xcode app, else the
    first Xcode in /Applications or ~/Applications (Xcode.app before betas), else None.
    """
    selected = (run(["/usr/bin/xcode-select", "-p"], timeout=5) or "").strip().rstrip("/") or None
    if selected and is_xcode(selected):
        return selected, selected
    for root in XCODE_APP_DIRS:
        try:
            apps = sorted(Path(root).glob("Xcode*.app"), key=lambda app: (app.name != "Xcode.app", app.name))
        except OSError:
            continue
        for app in apps:
            if is_xcode(app / "Contents" / "Developer"):
                return str(app / "Contents" / "Developer"), selected
    return None, selected


def xcode_tool(name, developer=...):
    """xcodebuild or git from Xcode itself, never the /usr/bin stubs; git also from the CLT or Homebrew.

    developer is Xcode's developer directory (None: no Xcode); by default it is looked up.
    """
    developer = developer_dir()[0] if developer is ... else developer
    directories = ([str(Path(developer) / "usr" / "bin")] if developer else []) + (list(GIT_DIRS) if name == "git" else [])
    return next((path for directory in directories if (path := executable(directory, name))), None)


def execute(command, env=None, timeout=20):
    """(exit code, stdout and stderr) of a short command; (None, "") if it could not run."""
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env,
                                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def xcode_status():
    """Whether Xcode can build the runner and, if not, what to fix.

    problems, in the order to fix them: missing (no Xcode app), not_selected (the active
    developer directory is the Command Line Tools or nothing), license (not accepted),
    first_launch (its components are not installed), no_ios (no iOS platform) or broken
    (xcodebuild fails for another reason). fixes are the Terminal commands for them, in order.
    state is the first problem, or ok.
    """
    xcode, selected = developer_dir()
    status = {"state": "ok", "app": str(Path(xcode).parent.parent) if xcode else None, "developer_dir": xcode,
              "selected": selected, "version": None, "problems": [], "fixes": []}
    problems, fixes = status["problems"], status["fixes"]
    if not xcode:
        problems.append("missing")
    else:
        if selected != xcode:
            problems.append("not_selected")
            fixes.append(f"sudo xcode-select -s {shlex.quote(status['app'])}")
        xcodebuild, env = str(Path(xcode) / "usr" / "bin" / "xcodebuild"), {**os.environ, "DEVELOPER_DIR": xcode}
        code, output = execute([xcodebuild, "-version"], env)
        if code == 0:
            status["version"] = (output.strip().splitlines() or [None])[0]
            if execute([xcodebuild, "-checkFirstLaunchStatus"], env)[0] not in (0, None):
                problems.append("first_launch")
                fixes.append("sudo xcodebuild -runFirstLaunch")
            else:
                code, sdks = execute([xcodebuild, "-showsdks"], env, timeout=30)
                if code == 0 and "iphoneos" not in sdks:
                    problems.append("no_ios")
                    fixes.append("xcodebuild -downloadPlatform iOS")
        elif "license" in output.lower():
            problems.append("license")
            fixes.append("sudo xcodebuild -license accept")
        else:
            problems.append("broken")
    status["state"] = problems[0] if problems else "ok"
    return status


def count_iphones(items, simulated=False):
    """iPhones in a system_profiler USB tree; a virtual one on a simulated bus doesn't count."""
    total = 0
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict):
            continue
        on_simulated = simulated or item.get("USBKeyHardwareType") == "Simulated"
        name = str(item.get("_name") or "")
        vendor = str(item.get("USBDeviceKeyVendorID") or item.get("vendor_id") or "").lower()
        if name.startswith("iPhone") and "virtual" not in name.lower() and "0x05ac" in vendor and not on_simulated:
            total += 1
        total += count_iphones(item.get("_items"), on_simulated)
    return total


def run(command, timeout=10):
    """stdout of a short command, or None if it failed."""
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


# How long one USB listing answers ``usb_attached``: a pulled cable is known within this many seconds.
ATTACHED_TTL = 2.0
_attached_cache = {"at": None, "udids": None}
_attached_lock = threading.Lock()


def usb_attached(udid, now=None, ttl=ATTACHED_TTL):
    """Whether the iPhone ``udid`` is on this Mac's USB now: True, False, or None when that can't be told
    (no libimobiledevice, or its listing failed). One ``idevice_id -l`` (tens of ms) serves every caller for
    ``ttl`` seconds, so a run that asks after each failed call costs nothing while the phone is there."""
    if not isinstance(udid, str) or not UDID_PATTERN.fullmatch(udid):
        return None
    now = time.monotonic() if now is None else now
    with _attached_lock:
        at, udids = _attached_cache["at"], _attached_cache["udids"]
        if at is None or now - at >= ttl:
            listing = tool("idevice_id")
            output = run([listing, "-l"], timeout=3) if listing else None
            udids = None if output is None else {line.strip().upper() for line in output.splitlines() if line.strip()}
            _attached_cache.update(at=now, udids=udids)
    return None if udids is None else udid.upper() in udids


@functools.lru_cache(maxsize=4)
def iproxy_binds_loopback(path):
    """Whether this iproxy takes -s; an older one cannot be kept off the LAN."""
    try:
        completed = subprocess.run([path, "-h"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return "--source" in completed.stdout + completed.stderr


def relay_command(udid, wda_port=WDA_PORT, video_port=VIDEO_PORT):
    """iproxy for the API and video ports on 127.0.0.1 only, or LookupError when it can't be.

    WDA always listens on 8100 and 9100 on the phone; ``wda_port`` and ``video_port`` are this Mac's side, so a
    second phone's relay (8101 -> its 8100) never meets the first's (devices.py allocates them)."""
    iproxy = tool("iproxy")
    if not iproxy:
        raise LookupError("The iPhone connection tools aren't installed. Open Setup › Get your Mac ready.")
    if not iproxy_binds_loopback(iproxy):
        raise LookupError("This iproxy can't be limited to this Mac. Update it: brew upgrade libimobiledevice libusbmuxd")
    return [iproxy, "-s", LOOPBACK, "-u", udid, f"{wda_port}:{WDA_PORT}", f"{video_port}:{VIDEO_PORT}"]


def wifi_runner_command(xcodebuild, plan, udid):
    """xcodebuild for the runner over Wi-Fi: the per-launch test plan (wireless/xctestrun.py, USE_IP the phone's
    tunnel address) on the phone ``udid``, which CoreDevice reaches through its encrypted tunnel."""
    if not isinstance(udid, str) or not UDID_PATTERN.fullmatch(udid):
        raise ValueError("A UDID is 24 to 40 hex digits and dashes")
    return [xcodebuild, "test-without-building", "-xctestrun", str(plan), "-destination", f"id={udid}"]


def patch_wda(project):
    """Apply WDA_PATCHES to a clone. A source they don't fit fails the build rather than run open."""
    for name, anchor, patched in WDA_PATCHES:
        path = Path(project) / name
        text = path.read_text()
        if patched in text:
            continue
        if text.count(anchor) != 1:
            raise RuntimeError(f"WebDriverAgent is not {wda_source.REF}, so it can't be limited to USB. "
                               f"Delete {project} and build again.")
        path.write_text(text.replace(anchor, patched))


def pin_loopback(products):
    """USE_IP=127.0.0.1 in built test plans: a build made before it was passed serves every interface."""
    for path in Path(products).glob("*.xctestrun"):
        try:
            with open(path, "rb") as stream:
                plan = plistlib.load(stream)
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        targets = [value for key, value in plan.items() if not key.startswith("__") and isinstance(value, dict)]
        stale = [target for target in targets if target.get("EnvironmentVariables", {}).get("USE_IP") != LOOPBACK]
        for target in stale:
            target.setdefault("EnvironmentVariables", {})["USE_IP"] = LOOPBACK
        if stale:
            with open(path, "wb") as stream:
                plistlib.dump(plan, stream)


def build_hint(lines):
    text = "\n".join(lines)
    for needle, hint in BUILD_HINTS:
        if needle in text:
            return hint
    errors = [line.strip() for line in lines if "error:" in line]
    return errors[-1][:300] if errors else "The build failed. See the build log for details."


def private_dir(path):
    """A folder only this user can open (the app data folder, logs), also when it exists."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def open_private(path, mode):
    """A file opened for writing ("w", "ab") with mode 0600, also when it exists."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if "a" in mode else os.O_TRUNC)
    stream = open(os.open(path, flags, 0o600), mode)
    os.fchmod(stream.fileno(), 0o600)
    return stream


def save_env_value(path, key, value):
    """Set KEY=value in a private env file (created 0600), replacing any old line."""
    update_env_file(path, {key: value})


def update_env_file(path, changes):
    """Set (str) or remove (None) several KEY=value lines in one private, atomic rewrite."""
    for key, value in changes.items():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) or value is not None and (
                not isinstance(value, str) or any(c in "\r\n" or "\ud800" <= c <= "\udfff" for c in value)):
            raise ValueError("Invalid environment entry")
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # A line that is not UTF-8 is kept byte for byte, so Setup can still save a key into such a file (and
    # a byte-order mark goes, as load_env_file ignores it: a key on the first line is still replaced).
    text = path.read_bytes().decode("utf-8", "surrogateescape").removeprefix("\ufeff") if path.exists() else ""
    lines = text.splitlines()
    for key in changes:
        lines = [line for line in lines if not re.match(rf"\s*(export\s+)?{re.escape(key)}\s*=", line)]
    lines += [f"{key}={value}" for key, value in changes.items() if value is not None]
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".env-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", errors="surrogateescape") as stream:
            stream.write("\n".join(lines) + ("\n" if lines else ""))
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class Supervised:
    """One external process kept alive until stopped; restarts when it exits."""

    def __init__(self, name, command, log_path, env=None):
        self.name, self.command, self.log_path, self.env = name, command, log_path, env
        self.process = None
        self.stopping = threading.Event()
        self.thread = None
        self.restarts = 0
        self.failures = 0
        self.last_exit = None

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.running:
            return
        self.stopping.clear()
        self.thread = threading.Thread(target=self._loop, name=f"mobster-{self.name}", daemon=True)
        self.thread.start()

    def _loop(self):
        private_dir(Path(self.log_path).parent)
        while not self.stopping.is_set():
            with open_private(self.log_path, "ab") as log:
                try:
                    self.process = subprocess.Popen(self.command, stdout=log, stderr=subprocess.STDOUT,
                                                    stdin=subprocess.DEVNULL, start_new_session=True, env=self.env)
                except OSError as error:
                    self.last_exit = f"could not start: {error}"
                    self.stopping.wait(RESTART_DELAY * 5)
                    continue
                started = time.monotonic()
                while self.process.poll() is None and not self.stopping.is_set():
                    self.stopping.wait(.5)
                if self.stopping.is_set():
                    self._terminate()
                    return
                self.last_exit = self.process.returncode
                self.restarts += 1
            # Back off while it keeps failing fast (an unplugged or locked phone),
            # instead of relaunching xcodebuild every two seconds.
            self.failures = 0 if time.monotonic() - started >= HEALTHY_RUN_SECONDS else self.failures + 1
            self.stopping.wait(min(MAX_RESTART_DELAY, RESTART_DELAY * 2 ** max(0, self.failures - 1)))

    def _terminate(self):
        process = self.process
        if process is None or process.poll() is not None:
            return
        for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
            try:
                os.killpg(process.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                process.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue

    def stop(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=10)
        self._terminate()


# One build at a time across phones: every build rebrands and patches the shared WebDriverAgent clone, and
# two xcodebuilds at once would also double the heaviest job Mobster runs.
BUILD_LOCK = threading.Lock()


class DeviceManager:
    """One USB iPhone's lifecycle. The primary phone's (``udid`` None) lives where it always has: device.json,
    wda-build/ and logs/ in the data folder, relayed on 8100 and 9100, and follows the phone chosen in Setup.
    Another phone's is pinned to its ``udid`` and keeps its files in ``root`` (devices/<udid>/), relayed on the
    ports devices.py gave it; the WebDriverAgent clone is shared."""

    def __init__(self, data_dir, wda_url=f"http://127.0.0.1:{WDA_PORT}", *, udid=None, root=None,
                 video_port=VIDEO_PORT):
        self.data_dir = Path(data_dir)
        try:
            private_dir(self.data_dir)  # keys, journal, team id and logs live here
        except OSError:
            pass
        self.wda_url = wda_url.rstrip("/")
        from urllib.parse import urlsplit
        self.wda_port = urlsplit(self.wda_url).port or WDA_PORT
        self.video_port = video_port
        self.pinned = udid
        home = Path(root) if root is not None else self.data_dir
        self.project = self.data_dir / "WebDriverAgent"
        self.derived = home / "wda-build"
        self.logs = home / "logs"
        self.settings_path = home / "device.json"
        self.lock = threading.Lock()
        self.build = {"state": "idle", "error": None, "log_tail": []}
        self.runner_log_start = 0
        self.watching = threading.Event()  # set to stop watch()
        self.health = {"responsive": None, "locked": None, "since": None, "probed_at": 0.0}
        self.last_restart = float("-inf")
        # One start or stop at a time: the watcher and the setup flow can both start the runner.
        self.runner_lock = threading.RLock()
        self.runner = None
        self.relay = None
        # What the runner started last uses: "usb" (iproxy), "wifi" (the tunnel and wireless.relay) or None (stopped).
        # Only wireless/monitor.py starts it over Wi-Fi, with MOBSTER_WIFI_TRANSPORT on.
        self.transport = None
        self.wifi_address = None
        self.runner_line = None
        self.cache = {}
        # Rebuilds the runner before its signature runs out (runner_renewal.RunnerRenewal); set by SetupService.
        self.renewal = None
        # Phones other managers drive (devices.py): the primary never picks one as "the only phone connected".
        self.claimed = lambda: ()
        if udid is not None and self.settings_path.parent.exists() and self.settings().get("udid") != udid:
            try:
                self.save_settings(udid=udid)
            except OSError:
                pass

    # -- inspection ----------------------------------------------------------

    def settings(self):
        try:
            value = json.loads(self.settings_path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_settings(self, **changes):
        settings = {**self.settings(), **changes}
        private_dir(self.data_dir)
        private_dir(self.settings_path.parent)
        with open_private(self.settings_path, "w") as stream:
            stream.write(json.dumps(settings, indent=1))
        return settings

    def tools(self):
        """Paths of the tools setup needs (None when missing or unusable), and Xcode's state (xcode_status)."""
        cached = self.cache.get("xcode")
        if cached and time.monotonic() < cached[0]:
            xcode = cached[1]
        else:
            xcode = xcode_status()
            self.cache["xcode"] = (time.monotonic() + (XCODE_OK_TTL if xcode["state"] == "ok" else XCODE_RETRY_TTL),
                                   xcode)
        developer = xcode["developer_dir"]
        return {"xcodebuild": xcode_tool("xcodebuild", developer) if xcode["state"] == "ok" else None,
                "iproxy": tool("iproxy"), "idevice_id": tool("idevice_id"), "ideviceinfo": tool("ideviceinfo"),
                "git": xcode_tool("git", developer), "xcode": xcode}

    def refresh(self):
        """Forget the cached tool and team checks, so the next read sees a fix the user just made."""
        for key in ("xcode", "teams", "usb"):
            self.cache.pop(key, None)

    def xcode_env(self):
        """xcodebuild's environment: Xcode's developer directory, whatever xcode-select says."""
        developer = self.tools()["xcode"]["developer_dir"]
        return {**os.environ, "DEVELOPER_DIR": developer} if developer else None

    def devices(self):
        """USB iPhones, with whether this Mac is trusted (lockdown info readable)."""
        listing = tool("idevice_id")
        output = run([listing, "-l"]) if listing else None
        udids = []
        for line in (output or "").splitlines():
            udid = line.strip()
            # 0000FE01-… entries are not physical USB phones.
            if UDID_PATTERN.fullmatch(udid) and not udid.startswith("0000FE01") and udid not in udids:
                udids.append(udid)
        info_tool = tool("ideviceinfo")
        devices = []
        for udid in udids:
            # A trusted phone's identity is stable; untrusted ones are re-read so Trust shows at once.
            cached = self.cache.get(("info", udid))
            info = dict(cached[1]) if cached and time.monotonic() - cached[0] < 60 else {}
            if info_tool and not info:
                for key, field in (("DeviceName", "name"), ("ProductType", "model"), ("ProductVersion", "ios")):
                    value = run([info_tool, "-u", udid, "-k", key], timeout=5)
                    if value is not None:
                        info[field] = value.strip()
                if info:
                    self.cache[("info", udid)] = (time.monotonic(), dict(info))
            devices.append({"udid": udid, "name": info.get("name") or "iPhone", "model": info.get("model"),
                            "modelName": MODEL_NAMES.get(info.get("model")), "ios": info.get("ios"),
                            "trusted": bool(info)})
        return devices

    def usb_iphones(self):
        """How many iPhones are on this Mac's USB, read without libimobiledevice (System Information).

        Setup's first screen uses it to say the cable works before the iPhone tools are installed.
        None when System Information can't say. Only counts; nothing identifying is kept.
        """
        cached = self.cache.get("usb")
        if cached and time.monotonic() - cached[0] < USB_TTL:
            return cached[1]
        count = None
        for data_type in ("SPUSBHostDataType", "SPUSBDataType"):
            output = run(["/usr/sbin/system_profiler", data_type, "-json"], timeout=8)
            try:
                tree = json.loads(output) if output else None
            except ValueError:
                tree = None
            if isinstance(tree, dict) and isinstance(tree.get(data_type), list):
                count = count_iphones(tree[data_type])
                break
        self.cache["usb"] = (time.monotonic(), count)
        return count

    def device(self, devices=None):
        """The chosen iPhone while it is connected, else None.

        Never a different phone than the one chosen: a runner built and signed
        for one phone fails on another (measured: an unplugged iPhone 15 Pro
        made the runner restart in a loop on a second, unbuilt iPhone). With no
        choice yet, a single connected phone is the natural one.
        """
        devices = self.devices() if devices is None else devices
        chosen = self.pinned or self.settings().get("udid")
        if chosen:
            return next((d for d in devices if d["udid"] == chosen), None)
        claimed = set(self.claimed())
        free = [d for d in devices if d["udid"] not in claimed]
        return free[0] if len(free) == 1 else None

    def choose(self, udid):
        if not isinstance(udid, str) or not UDID_PATTERN.fullmatch(udid):
            raise ValueError("Choose a connected iPhone")
        if self.pinned and udid != self.pinned:
            raise LookupError("This device is one iPhone; set up another iPhone as its own device")
        if udid in set(self.claimed()):
            raise LookupError("That iPhone is already set up as another device. Choose a different iPhone.")
        if not any(d["udid"] == udid for d in self.devices()):
            raise LookupError("That iPhone is not connected")
        name = next(d["name"] for d in self.devices() if d["udid"] == udid)
        if udid != self.settings().get("udid"):
            self.stop_runner()
        self.save_settings(udid=udid, device_name=name)

    def teams(self):
        """Apple development teams, [{"id", "name", "personal"}]: the teams of the Apple IDs signed in to
        Xcode (a free account's Personal Team included), then any other team with a signing certificate."""
        if "teams" in self.cache and time.monotonic() - self.cache["teams"][0] < TEAMS_TTL:
            return self.cache["teams"][1]
        teams = signing.merge_teams(signing.account_teams(), signing.certificate_teams())
        self.cache["teams"] = (time.monotonic(), teams)
        return teams

    def built(self):
        """Built for the chosen phone (a build records the phone it was made for), and still signed."""
        settings = self.settings()
        built_for = settings.get("built_for", settings.get("udid"))
        return bool(built_for) and built_for == settings.get("udid") and any(
            self.derived.glob("Build/Products/*.xctestrun")) and not self.expired()

    def signature_expiry(self):
        """When the built runner's signature expires (epoch seconds), or None when unknown.

        A build records it; a runner built before that is read once from its profile.
        """
        settings = self.settings()
        if "expires_at" not in settings and any(self.derived.glob(signing.PROFILE_GLOB)):
            settings = self.save_settings(expires_at=signing.profile_expiry(self.derived))
        value = settings.get("expires_at")
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def expired(self, now=None):
        expires = self.signature_expiry()
        return expires is not None and (time.time() if now is None else now) >= expires

    def wda_ready(self, timeout=1.5):
        try:
            with urllib.request.urlopen(self.wda_url + "/status", timeout=timeout) as response:
                return json.load(response).get("value", {}).get("ready") is True
        except Exception:
            return False

    def wda_alive(self, attempts=3, timeout=4.0, gap=2.0):
        """Whether a runner answers, asked patiently before starting another one.

        Starting a runner while one is already serving (after a development
        reload, or one started by hand) replaces the running one mid-task. WDA
        serves one request at a time, so a busy runner can miss a single short
        /status; only several consecutive misses mean it is gone.
        """
        for attempt in range(attempts):
            if self.wda_ready(timeout=timeout):
                return True
            if attempt + 1 < attempts:
                time.sleep(gap)
        return False

    def attached(self, udid=None):
        """Whether the chosen (or ``udid``'s) iPhone is plugged in now; None when that can't be told."""
        return usb_attached(udid or self.pinned or self.settings().get("udid"))

    def health_error(self):
        """What to tell the user when WDA answers but cannot reach the phone's UI."""
        if self.health["locked"] is True:
            return "Your iPhone is locked. Unlock it to run tasks and see its screen."
        if self.health["responsive"] is False:
            return "Your iPhone isn't responding. Unlock it and close Control Center or Siri if they're open."
        return None

    def runner_state(self):
        if self.runner is None or not self.runner.running:
            # A runner started outside Mobster (scripts/usb-wda.sh, Xcode) serves just as well.
            if self.wda_ready():
                return {"state": "running", "error": self.health_error(), "external": True}
            return {"state": "stopped", "error": None}
        if self.wda_ready():
            return {"state": "running", "error": self.health_error()}
        failing = self.runner.restarts >= 3 and self.runner.last_exit not in (None, 0)
        hint = self.runner_hint()
        return {"state": "failed" if failing else "starting",
                "error": hint or ("Mobster's helper keeps stopping. Unlock your iPhone and check Developer Mode."
                                  if failing else None)}

    def runner_hint(self):
        """A step the user can take, from the newest lines of this start's runner log."""
        # An expired profile fails with the same "invalid code signature ... not explicitly trusted"
        # sentence as an untrusted one, so the log alone would give the wrong advice.
        if self.expired():
            return EXPIRED_HINT
        try:
            with open(self.logs / "wda-runner.log", "rb") as log:
                size = log.seek(0, os.SEEK_END)
                log.seek(max(self.runner_log_start, size - RUNNER_LOG_WINDOW))
                text = log.read().decode("utf-8", "replace")
        except OSError:
            return None
        # The newest matching line wins: an unlock prompt followed by success is stale.
        latest = None
        self.runner_line = None
        for line in text.splitlines():
            for needle, hint in RUNNER_HINTS:
                if needle in line:
                    latest, self.runner_line = hint, line
                    break
            if "ServerURLHere" in line:
                latest = self.runner_line = None
        return latest

    def runner_problem(self):
        """The runner's current failure as a one-line fix (setup_errors), from the line runner_hint matched."""
        if self.expired():
            return {"kind": "expired", "fix": "Refresh Mobster's helper", "action": "renew", "raw": EXPIRED_HINT}
        line = getattr(self, "runner_line", None)
        return setup_errors.translate(line) if line else None

    # -- actions ---------------------------------------------------------------

    def start_build(self, team, renewal=False):
        """Build and install the runner in the background. renewal marks one runner_renewal started."""
        if not TEAM_PATTERN.fullmatch(team or ""):
            raise ValueError("A team id is 10 letters and digits")
        device = self.device()
        if device is None:
            raise LookupError("Connect and choose your iPhone first")
        tools = self.tools()
        if tools["xcode"]["state"] != "ok" or not tools["git"]:
            raise LookupError("Finish installing Xcode first (Setup › Get your Mac ready)")
        with self.lock:
            if self.build["state"] == "running":
                return
            # The first build also fetches WebDriverAgent and prepares the phone, so it takes longest.
            self.build = {"state": "running", "error": None, "log_tail": [], "started_at": time.time(),
                          "first": not any(self.derived.glob("Build/Products/*.xctestrun")), "renewal": renewal}
        self.save_settings(team=team, udid=device["udid"], device_name=device["name"])
        threading.Thread(target=self._build, args=(team, device["udid"]), name="mobster-wda-build",
                         daemon=True).start()

    def _build(self, team, udid):
        log_path = self.logs / "wda-build.log"
        private_dir(self.logs)
        tail = []
        if not BUILD_LOCK.acquire(blocking=False):
            with self.lock:
                self.build["log_tail"] = ["Waiting for another iPhone's build to finish."]
            BUILD_LOCK.acquire()
        try:
            self._build_locked(team, udid, log_path, tail)
        finally:
            BUILD_LOCK.release()

    def _build_locked(self, team, udid, log_path, tail):
        try:
            with open_private(log_path, "w") as log:
                git = tool("git") or "git"
                if self.project.exists() and not wda_source.is_pinned(git, self.project):
                    shutil.rmtree(self.project)  # an older or unpinned checkout: fetch the pinned release
                if not self.project.exists():
                    self._step(wda_source.clone_command(git, self.project), log, tail)
                    wda_source.verify(git, self.project)
                log.write(f"WebDriverAgent {wda_source.REF} ({wda_source.COMMIT})\n")
                self._rebrand(team)
                self._drop_stale_profiles(team, log)
                try:
                    patch_wda(self.project)
                except RuntimeError as error:
                    tail.append(f"error: {error}")  # shown as the build's error
                    log.write(tail[-1] + "\n")
                    raise
                # USE_IP goes into the built test plan: WDA then listens on the phone's loopback,
                # which USB (usbmux) reaches, and not on its Wi-Fi address.
                self._step([tool("xcodebuild"), "-project", str(self.project / "WebDriverAgent.xcodeproj"),
                            "-scheme", "WebDriverAgentRunner", "-destination", f"id={udid}",
                            "-allowProvisioningUpdates", "-derivedDataPath", str(self.derived),
                            f"DEVELOPMENT_TEAM={team}", f"USE_IP={LOOPBACK}", "build-for-testing"], log, tail)
                self._remove_legacy_runner(team, udid, log)
            self.save_settings(built_for=udid, expires_at=signing.profile_expiry(self.derived))
            with self.lock:
                self.build = {**self.build, "state": "succeeded", "error": None, "problem": None,
                              "log_tail": tail[-LOG_TAIL:]}
        except Exception as error:
            text = "\n".join(tail) if tail else str(error)
            with self.lock:
                self.build = {**self.build, "state": "failed", "error": build_hint(tail) if tail else str(error)[:300],
                              "problem": setup_errors.problem(text), "log_tail": tail[-LOG_TAIL:]}

    def _step(self, command, log, tail):
        # git and xcodebuild come from Xcode, never the /usr/bin stubs, and run with its developer directory.
        if command[0] in XCODE_SHIMS:
            command = [tool(command[0]) or command[0], *command[1:]]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   stdin=subprocess.DEVNULL, start_new_session=True, env=self.xcode_env())
        for line in process.stdout:
            log.write(line)
            tail.append(line.rstrip()[:400])
            del tail[:-LOG_TAIL * 5]
            with self.lock:
                self.build["log_tail"] = tail[-LOG_TAIL:]
        if process.wait() != 0:
            raise RuntimeError(f"{Path(command[0]).name} exited with {process.returncode}")

    def _rebrand(self, team=None):
        """Mobster's bundle ids in the clone; the runner's carries the team's suffix (see BUNDLE_REBRAND)."""
        project = self.project / "WebDriverAgent.xcodeproj" / "project.pbxproj"
        text = project.read_text()
        for old, new in BUNDLE_REBRAND.items():
            if old != "com.facebook.WebDriverAgentRunner":
                text = text.replace(old, new)
        text = RUNNER_ID.sub(signing.runner_bundle_id(team), text)
        project.write_text(text)

    def _remove_legacy_runner(self, team, udid, log):
        """Once the team's own runner is built, uninstall the plain-id runner earlier versions put on this
        phone: it is a second identical icon, and on a free Apple ID it holds one of the 3 app slots.
        Exactly LEGACY_RUNNER is removed, once per phone, and a failure never fails the build."""
        if signing.runner_bundle_id(team) == signing.RUNNER_BUNDLE or udid in self.settings().get("legacy_removed", []):
            return
        xcrun = tool("xcrun")
        if not xcrun:
            return
        try:
            result = subprocess.run([xcrun, "devicectl", "device", "uninstall", "app", "--device", udid,
                                     LEGACY_RUNNER], capture_output=True, text=True, timeout=90, env=self.xcode_env())
        except (OSError, subprocess.SubprocessError) as error:
            log.write(f"Could not remove the old runner ({LEGACY_RUNNER}): {error}\n")
            return
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            log.write(f"Removed the old runner ({LEGACY_RUNNER}) from the iPhone.\n")
        elif re.search(r"not (installed|found)|no such app|couldn.t find|does not exist", output, re.I):
            log.write(f"The old runner ({LEGACY_RUNNER}) isn't on the iPhone.\n")
        else:
            log.write(f"Could not remove the old runner ({LEGACY_RUNNER}): {output[-300:]}\n")
            return
        self.save_settings(legacy_removed=[*self.settings().get("legacy_removed", []), udid][-8:])

    def _drop_stale_profiles(self, team, log):
        """Before renewing, remove Xcode's cached runner profiles that expire within the renewal window,
        so -allowProvisioningUpdates fetches a new one instead of signing with the old one again."""
        window = renew_window()
        expires = self.signature_expiry()
        if expires is None or expires - time.time() > window:
            return
        for path in signing.stale_runner_profiles(team, time.time() + window):
            try:
                path.unlink()
                log.write(f"Removed an expiring runner profile: {path.name}\n")
            except OSError:
                pass

    def start_runner(self):
        with self.runner_lock:
            if self.transport == "wifi" and self.wifi_address and self.attached() is not True:
                # A runner on Wi-Fi restarts on Wi-Fi (Setup's Start, a wedged runner), while the phone stays unplugged.
                self._start_wifi_runner(self.wifi_address)
                return
            self._start_runner()

    def _start_runner(self):
        # Every start (Setup's Start, autostart, probe()'s restart, a renewal's restart) runs here under
        # runner_lock, and close() sets ``watching`` before it takes that lock to stop the runner: a start
        # that gets here afterwards would undo that stop.
        if self.watching.is_set():
            raise LookupError("Mobster is quitting")
        settings = self.settings()
        device = self.device()
        if device is None:
            raise LookupError("Connect your iPhone first")
        if self.expired():
            raise LookupError(EXPIRED_HINT)
        if not self.built() or not settings.get("team"):
            raise LookupError("Build the runner first")
        udid = device["udid"]
        relay = relay_command(udid, self.wda_port, self.video_port)
        self.stop_runner()
        pin_loopback(self.derived / "Build" / "Products")
        try:
            self.runner_log_start = (self.logs / "wda-runner.log").stat().st_size
        except OSError:
            self.runner_log_start = 0
        # A new runner's health is unknown. ``watching`` and ``runner_lock`` stay the ones __init__ made: the
        # watch loop waits on that Event (close() sets it) and a second start waits on that lock.
        # ``last_restart`` stays too: probe() sets it just before it calls start_runner(), and MIN_RESTART_GAP
        # between wedged-runner restarts depends on it.
        self.health = {"responsive": None, "locked": None, "since": None, "probed_at": 0.0}
        self.runner = Supervised("wda-runner", [
            tool("xcodebuild"), "-project", str(self.project / "WebDriverAgent.xcodeproj"),
            "-scheme", "WebDriverAgentRunner", "-destination", f"id={udid}",
            "-derivedDataPath", str(self.derived), f"DEVELOPMENT_TEAM={settings['team']}",
            f"USE_IP={LOOPBACK}", "test-without-building"], self.logs / "wda-runner.log", env=self.xcode_env())
        self.relay = Supervised("usb-relay", relay, self.logs / "usb-relay.log")
        self.relay.start()
        self.runner.start()
        self.transport = "usb"
        self.save_settings(autostart=True)

    def start_wifi_runner(self, address):
        """Start the runner over Wi-Fi (SPEC §3.7 item 3): WebDriverAgent listens only on the phone's tunnel address
        ``address`` (fd00::/8, else refused), and this Mac's side is a relay bound to 127.0.0.1 on the phone's own
        ports, so its WDA address, lease and live view are the cable's. LookupError when it can't start now."""
        with self.runner_lock:
            self._start_wifi_runner(address)

    def _start_wifi_runner(self, address):
        from .wireless import relay as wifi_relay
        from .wireless import xctestrun
        if self.watching.is_set():
            raise LookupError("Mobster is quitting")
        settings = self.settings()
        udid = self.pinned or settings.get("udid")
        if not udid:
            raise LookupError("Choose your iPhone in Setup first")
        if self.expired():
            raise LookupError(EXPIRED_HINT)
        if not self.built() or not settings.get("team"):
            raise LookupError("Build the runner first")
        xcodebuild = tool("xcodebuild")
        if not xcodebuild:
            raise LookupError("Finish installing Xcode first (Setup › Get your Mac ready)")
        try:
            # A tunnel address (fd00::/8) that this Mac reaches through the CoreDevice tunnel, never through Wi-Fi.
            target = wifi_relay.require_tunnel(address)
            plan = xctestrun.write(self.derived / "Build" / "Products", self.derived / xctestrun.FOLDER, target)
        except ValueError as error:
            raise LookupError(str(error)) from None
        self._stop_runner(False)
        relay = wifi_relay.Relay(target, [(self.wda_port, WDA_PORT), (self.video_port, VIDEO_PORT)])
        try:
            relay.start()
        except OSError:
            raise LookupError(f"Another program is using port {self.wda_port} or {self.video_port} on this Mac") from None
        try:
            self.runner_log_start = (self.logs / "wda-runner.log").stat().st_size
        except OSError:
            self.runner_log_start = 0
        self.health = {"responsive": None, "locked": None, "since": None, "probed_at": 0.0}
        self.relay = relay
        self.runner = Supervised("wda-runner", wifi_runner_command(xcodebuild, plan, udid), self.logs / "wda-runner.log",
                                 env=self.xcode_env())
        self.runner.start()
        self.transport, self.wifi_address = "wifi", target

    def stop_runner(self, remember=False):
        with self.runner_lock:
            self._stop_runner(remember)

    def _stop_runner(self, remember):
        for service in (self.runner, self.relay):
            if service is not None:
                service.stop()
        self.runner = self.relay = None
        self.transport = None
        if remember:
            self.save_settings(autostart=False)

    def autostart(self):
        """Resume the runner if it was running before and can run now."""
        try:
            with self.runner_lock:
                if (self.settings().get("autostart") and (self.runner is None or not self.runner.running)
                        and self.built() and self.device() and not self.wda_alive()):
                    self._start_runner()
        except Exception:
            pass

    def probe(self, now=None):
        """Check that WDA can reach the phone's UI; restart a runner wedged for too long."""
        now = time.monotonic() if now is None else now
        if now - self.health["probed_at"] < PROBE_INTERVAL:
            return
        self.health["probed_at"] = now
        if not self.wda_ready():
            self.health.update(responsive=None, locked=None, since=None)
            return
        try:
            with urllib.request.urlopen(self.wda_url + "/wda/locked", timeout=PROBE_TIMEOUT) as response:
                locked = json.load(response).get("value")
            self.health.update(responsive=True, locked=locked is True, since=None)
            return
        except Exception:
            since = self.health["since"] or now
            self.health.update(responsive=False, locked=None, since=since)
        managed = self.runner is not None and self.runner.running
        if managed and now - since >= WEDGED_RESTART_SECONDS and now - self.last_restart >= MIN_RESTART_GAP:
            self.last_restart = now
            self.health.update(since=None)
            try:
                self.start_runner()  # stops the wedged runner and relay first
            except Exception:
                pass

    def watch(self, stop):
        """Keep the runner going: resume it at launch and when the chosen phone is plugged
        back in, and restart it when it stops reaching the phone's UI."""
        while not stop.is_set():
            self.autostart()
            try:
                self.probe()
            except Exception:
                pass
            if self.renewal is not None:
                try:
                    self.renewal.tick()
                except Exception:
                    pass
            stop.wait(WATCH_INTERVAL)

    def close(self):
        self.watching.set()
        self.stop_runner()

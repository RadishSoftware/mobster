"""The setup checklist the dashboard's onboarding flow renders (docs/setup-api.md)."""

from concurrent.futures import ThreadPoolExecutor
import os
import re
import subprocess
import threading
import time

from . import device_manager as dm
from . import signing
from .device_manager import save_env_value
from .keys import anthropic_key, openai_key
from .runner_renewal import RunnerRenewal

# The slow reads (Xcode's state, the USB phones and System Information) run side by side.
READS = ThreadPoolExecutor(max_workers=3, thread_name_prefix="mobster-setup-read")

KEY_PATTERN = re.compile(r"[A-Za-z0-9_\-]{20,200}")
INSTALL_TOOLS = "brew install libimobiledevice"
# What Install runs in the background when this build doesn't carry the tools itself (a source checkout with Homebrew):
# libimobiledevice, and ideviceinstaller for the Apps page. The shipped app never needs it (build-iphone-tools.sh).
INSTALL_FORMULAE = ("libimobiledevice", "ideviceinstaller")
# brew.sh's own install command, shown only when Homebrew is missing.
INSTALL_HOMEBREW = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
BREW_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
# What is wrong with Xcode, in the user's words (device_manager.xcode_status states). The step lists the
# Terminal commands that fix it, so these say what happened and any way to fix it without Terminal.
XCODE_DETAIL = {
    "missing": "Install Xcode from the Mac App Store. It's a large download and can take an hour, so start it "
               "now, then open Xcode once when it finishes.",
    "not_selected": "Xcode is installed, but this Mac isn't set to use it. Switch to it with the command below; "
                    "it asks for your Mac password.",
    "license": "Accept the Xcode license: open Xcode and agree, or use the command below.",
    "first_launch": "Xcode hasn't finished installing. Open it once and let it install its components, or use "
                    "the command below.",
    "no_ios": "Xcode doesn't have the iOS platform yet. Open Xcode › Settings › Components and get iOS, or use "
              "the command below.",
    "broken": "Xcode isn't responding. Open it once to finish installing it, then check again.",
}
CLT_SELECTED = ("Xcode is installed, but this Mac is set to use the Command Line Tools instead. Switch to Xcode "
                "with the command below; it asks for your Mac password.")
EXPIRED_DETAIL = ("Mobster's helper needs a refresh: a free Apple Account signs it for 7 days at a time. Refresh it to "
                  "keep using your iPhone.")
FIRST_BUILD = "The first install can take 10 minutes or more. Keep your iPhone unlocked and plugged in."
REBUILD = "Installing again is quicker than the first time. Keep your iPhone unlocked and plugged in."
DEVELOPER_MODE_TTL = (60.0, 5.0)  # re-read after this long when on, and when off or unknown
# The preflight's one-line reading of each Xcode state (device_manager.xcode_status).
XCODE_WORD = {"ok": "Ready", "missing": "Not installed", "not_selected": "Not selected", "license": "License",
              "first_launch": "Not finished", "no_ios": "No iOS platform", "broken": "Not responding"}
XCODE_PREFLIGHT = {
    "missing": "Free from the Mac App Store. It's a large download, so start it now.",
    "not_selected": "Installed, but this Mac is set to use other developer tools. Setup shows the fix.",
    "license": "Open Xcode once and accept its license.",
    "first_launch": "Open Xcode once so it can finish installing.",
    "no_ios": "Open Xcode › Settings › Components and get iOS.",
    "broken": "Open Xcode once so it can finish installing.",
}
# The AI account step (MESSAGING §11, Setup): whose account Mobster's agent uses, in the user's words.
KEY_DETAIL = {
    "openai": "Mobster's agent uses your OpenAI account. Your key is saved privately on this Mac.",
    "anthropic": "Mobster's agent uses your Claude account. Your key is saved privately on this Mac.",
    "jev": "Quick mode is ready with your Jev key. Connect Claude or OpenAI for everything else.",
    "none": "Mobster uses your own AI account, Claude or OpenAI. You pay them directly.",
}


class ToolsInstall:
    """Installs the iPhone connection tools with Homebrew in the background, one line of progress at a time.

    Only for a Mac whose Mobster doesn't carry the tools (Setup's Install button): the person never pastes a command.
    ``state`` is idle, running, succeeded or failed; ``line`` is what it's doing now, in words.
    """

    def __init__(self, popen=subprocess.Popen):
        self.popen = popen
        self.lock = threading.Lock()
        self.current = {"state": "idle", "line": None, "error": None}

    def status(self):
        with self.lock:
            return dict(self.current)

    def start(self, brew, on_done=None):
        with self.lock:
            if self.current["state"] == "running":
                return
            self.current = {"state": "running", "line": "Getting ready", "error": None}
        threading.Thread(target=self.run, args=(brew, on_done), daemon=True, name="mobster-tools-install").start()

    def update(self, **changes):
        with self.lock:
            self.current = {**self.current, **changes}

    def run(self, brew, on_done=None):
        env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ENV_HINTS": "1", "NONINTERACTIVE": "1"}
        tail = []
        try:
            process = self.popen([brew, "install", *INSTALL_FORMULAE], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, text=True, env=env)
            for raw in process.stdout:
                line = raw.strip()
                if line:
                    tail = (tail + [line])[-12:]
                words = install_words(line)
                if words:
                    self.update(line=words)
            code = process.wait()
        except OSError as error:
            self.update(state="failed", line=None, error=f"Homebrew didn't start ({error.strerror or error}).")
            return
        if code == 0:
            self.update(state="succeeded", line=None, error=None)
        else:
            last = next((line for line in reversed(tail) if line.lower().startswith("error")), tail[-1] if tail else "")
            self.update(state="failed", line=None,
                        error="The install didn't finish. Check your internet connection, then try again." +
                              (f" Homebrew said: {last[:200]}" if last else ""))
        if on_done is not None:
            on_done()


def install_words(line):
    """Homebrew's progress line in a person's words ("Downloading libplist"), or None for a line that says nothing new."""
    match = re.match(r"==> (Fetching|Pouring|Installing|Upgrading)\s+(?:dependencies for\s+)?([\w@.+-]+)", line)
    if not match:
        return None
    verb = {"Fetching": "Downloading", "Pouring": "Installing", "Installing": "Installing", "Upgrading": "Updating"}[match[1]]
    name = re.sub(r"(--|\.)[\w.]*bottle.*$", "", match[2]).rstrip(":")
    return f"{verb} {name}"


def task_running(runtime):
    """Whether a task is running now on the runtime's primary phone (the server's runtime.active is the oldest
    run's id; with several devices, Runtime.run_on says which runs on the primary)."""
    try:
        fleet = getattr(runtime, "fleet", None)
        if fleet is not None and callable(getattr(runtime, "run_on", None)):
            return runtime.run_on(fleet.primary) is not None
        return getattr(runtime, "active", None) is not None
    except Exception:
        return True


class SetupService:
    """``video`` and ``busy`` are a phone's own when it is not the runtime's primary phone (fleet.py): its live
    view (a callable returning it), and whether a task runs on that phone, which a renewal waits for."""

    def __init__(self, runtime, manager, env_file, video=None, busy=None):
        self.runtime, self.manager, self.env_file = runtime, manager, env_file
        self.video = video
        self.cache = {}
        self.tools_install = ToolsInstall()
        # The watch loop renews the runner before its signature runs out (runner_renewal.py).
        self.renewal = RunnerRenewal(manager, busy=busy or (lambda: task_running(runtime))) \
            if hasattr(manager, "start_build") else None
        if self.renewal is not None:
            manager.renewal = self.renewal

    def developer_mode(self, device):
        """Whether Developer Mode is on (True or False), or None when iOS or libimobiledevice can't say.

        Only a trusted phone answers. Turning it on restarts the phone, so an "off" is re-read soon.
        """
        if not device or not device.get("trusted") or not device.get("udid"):
            return None
        cached = self.cache.get(("developer_mode", device["udid"]))
        if cached and time.monotonic() - cached[0] < DEVELOPER_MODE_TTL[cached[1] is not True]:
            return cached[1]
        info = dm.tool("ideviceinfo")
        output = dm.run([info, "-u", device["udid"], "-q", "com.apple.security.mac.amfi", "-k",
                         "DeveloperModeStatus"], timeout=5) if info else None
        value = {"true": True, "false": False}.get((output or "").strip().lower())
        self.cache[("developer_mode", device["udid"])] = (time.monotonic(), value)
        return value

    def tools_step(self, tools):
        """The tools step's detail and commands: Xcode first (it takes longest), then the iPhone connection tools.

        The app carries the iPhone tools itself (``bundled``). Without them, Setup's Install runs Homebrew in the
        background (``install``); the commands are for Help › For developers, never Setup's main view.
        """
        xcode = tools["xcode"]
        usb_ok = bool(tools["iproxy"] and tools["idevice_id"])
        bundled = usb_ok and dm.bundled_tool("iproxy") == tools["iproxy"] and dm.bundled_tool("idevice_id") == tools["idevice_id"]
        homebrew = any(dm.executable(directory, "brew") for directory in BREW_DIRS)
        commands = [] if usb_ok else ([] if homebrew else [INSTALL_HOMEBREW]) + [INSTALL_TOOLS]
        # Homebrew's installer can switch the Mac to the Command Line Tools, so Xcode's fixes come after it.
        commands += xcode["fixes"]
        if xcode["state"] == "ok" and usb_ok:
            detail = f"{xcode['version'] or 'Xcode'} and the iPhone connection tools are installed."
        elif xcode["state"] == "not_selected" and xcode["selected"] == dm.CLT_DIR:
            detail = CLT_SELECTED
        elif xcode["state"] != "ok":
            detail = XCODE_DETAIL.get(xcode["state"], XCODE_DETAIL["broken"])
        elif homebrew:
            detail = "Install the iPhone connection tools, which let this Mac talk to your iPhone over the cable."
        else:
            detail = ("Install Homebrew, then the iPhone connection tools, which let this Mac talk to your iPhone over "
                      "the cable.")
        return detail, commands, {"xcode": {key: xcode[key] for key in ("state", "version", "app")},
                                  "libimobiledevice": usb_ok, "homebrew": homebrew, "bundled": bundled,
                                  "install": self.tools_install.status()}

    def preflight(self, tools, devices, device, usb):
        """Setup's first screen: Xcode, the cable, trust and Developer Mode, each with a word.

        state is ok, todo (the user has something to do), waiting (an earlier check comes first)
        or unknown (nothing can tell yet).
        """
        xcode = tools["xcode"]
        usb_tools = bool(tools["idevice_id"])
        connected = bool(devices) or bool(usb)

        def check(id_, title, state, word, detail):
            return {"id": id_, "title": title, "state": state, "word": word, "detail": detail}

        xcode_detail = (xcode["version"] or "Xcode is installed.") if xcode["state"] == "ok" else \
            XCODE_PREFLIGHT.get(xcode["state"], XCODE_PREFLIGHT["broken"])
        checks = [check("xcode", "Xcode", "ok" if xcode["state"] == "ok" else "todo", XCODE_WORD.get(xcode["state"], "Not responding"),
                        xcode_detail)]
        if connected:
            count = len(devices) or usb or 1
            name = device["name"] if device and device.get("trusted") else None
            checks.append(check("cable", "iPhone cable", "ok", "Connected",
                                f"“{name}” is connected." if name else
                                f"{count} iPhones are connected." if count > 1 else "Your iPhone is connected."))
        elif usb_tools or usb is not None:
            checks.append(check("cable", "iPhone cable", "todo", "Not connected",
                                "Plug your iPhone into this Mac with a cable that carries data."))
        else:
            checks.append(check("cable", "iPhone cable", "unknown", "Can't tell yet",
                                "Mobster sees the cable once the iPhone tools are installed."))
        trusted = bool(device and device.get("trusted")) or any(item.get("trusted") for item in devices)
        if trusted:
            checks.append(check("trust", "Trust this Mac", "ok", "Trusted", "Your iPhone trusts this Mac."))
        elif devices:
            checks.append(check("trust", "Trust this Mac", "todo", "Tap Trust",
                                "Unlock your iPhone, tap Trust and enter your passcode."))
        elif connected and not usb_tools:
            checks.append(check("trust", "Trust this Mac", "unknown", "Can't tell yet",
                                "Mobster checks this once the iPhone tools are installed."))
        else:
            checks.append(check("trust", "Trust this Mac", "waiting", "Waiting", "Connect your iPhone first."))
        mode = device.get("developerMode") if device else None
        if not trusted:
            checks.append(check("developer_mode", "Developer Mode", "waiting", "Waiting",
                                "Mobster reads it once your iPhone trusts this Mac."))
        elif mode is True:
            checks.append(check("developer_mode", "Developer Mode", "ok", "On", "Developer Mode is on."))
        elif mode is False:
            checks.append(check("developer_mode", "Developer Mode", "todo", "Off",
                                "On your iPhone, turn it on in Settings › Privacy & Security › Developer Mode."))
        else:
            checks.append(check("developer_mode", "Developer Mode", "unknown", "After the first build",
                                "The switch appears on your iPhone once Xcode has seen it. Setup asks when it's time."))
        return checks

    def state(self):
        manager, config = self.manager, self.runtime.config
        # Xcode's checks and the USB phones are independent: read them together. System Information
        # (a second of system_profiler) is asked only when libimobiledevice can't see a phone:
        # it isn't installed, or it lists nothing.
        tools_read = READS.submit(manager.tools)
        devices_read = READS.submit(manager.devices)
        tools = tools_read.result()
        usb_read = None
        if not tools["idevice_id"] and hasattr(manager, "usb_iphones"):
            usb_read = READS.submit(manager.usb_iphones)
        devices = devices_read.result() if tools["idevice_id"] else []
        if usb_read is None and not devices and hasattr(manager, "usb_iphones"):
            usb_read = READS.submit(manager.usb_iphones)
        try:
            usb = usb_read.result() if usb_read is not None else None
        except Exception:
            usb = None
        device = manager.device(devices)
        if device is not None:
            device = {**device, "developerMode": self.developer_mode(device)}
        settings = manager.settings()
        chosen = settings.get("udid")
        chosen_name = f"“{settings['device_name']}”" if settings.get("device_name") else "Your iPhone"
        chosen_in_sentence = chosen_name if settings.get("device_name") else "your iPhone"
        built_for = settings.get("built_for", chosen)
        build = dict(manager.build)
        runner = dict(manager.runner_state())
        stream = self.video() if self.video is not None else self.runtime.video
        video = stream.status() if hasattr(stream, "status") else {}
        has_openai, has_jev = bool(openai_key()[0]), bool(os.environ.get("TYPESAFE_API_KEY"))
        has_anthropic = bool(anthropic_key()[0])  # Smart runs on Claude with it (engines.smart_model)
        tools_ok = all(tools[name] for name in ("xcodebuild", "iproxy", "idevice_id", "git"))
        tools_detail, tools_commands, tool_states = self.tools_step(tools)
        phone_ok = bool(device and device["trusted"])
        built = manager.built()
        expired = manager.expired()
        expires_at = manager.signature_expiry()
        running = runner["state"] == "running"
        building = build["state"] == "running"
        first_build = build.get("first", not built and not expires_at)
        build.update(expires_at=round(expires_at * 1000) if expires_at else None, expired=expired,
                     started_at=round(build["started_at"] * 1000) if build.get("started_at") else None,
                     first=bool(first_build), renewal=bool(build.get("renewal")), problem=build.get("problem"),
                     team=settings.get("team"))
        # The runner's signature and its automatic renewal (runner_renewal.py), for Settings › iPhone.
        runner["expires_at"] = build["expires_at"]
        runner["renewal"] = self.renewal.status() if self.renewal is not None else None
        if runner.get("error") and hasattr(manager, "runner_problem"):
            runner["problem"] = manager.runner_problem()

        def step(id_, title, done, detail, action=None, blocked_by=None, working=False, **extra):
            # blocked_by names the step this one waits on; None when it can start now.
            waiting = None if done or working else blocked_by
            state = "done" if done else "working" if working else "blocked" if waiting else "todo"
            return {"id": id_, "title": title, "state": state, "detail": detail, "action": action,
                    "blocked_by": waiting, **extra}

        # Tools come first: the Xcode download takes longest, and the key can be added while it runs.
        steps = [
            step("tools", "Get your Mac ready", tools_ok, tools_detail, commands=tools_commands),
            step("api_key", "Connect your AI account", has_openai or has_anthropic or has_jev,
                 KEY_DETAIL["openai" if has_openai else "anthropic" if has_anthropic else "jev" if has_jev else "none"],
                 "save_key"),
            step("phone", "Connect your iPhone", phone_ok,
                 f"“{device['name']}” is connected and trusts this Mac." if phone_ok else
                 "Unlock your iPhone and tap Trust on it." if device else
                 f"{chosen_name} isn't connected. Plug it in with a cable and unlock it." if chosen else
                 "Choose which iPhone Mobster should use." if len(devices) > 1 else
                 "Plug your iPhone into this Mac with a cable and unlock it.",
                 "choose_phone" if len(devices) > 1 or chosen and not device else None,
                 blocked_by=None if tools_ok else "tools"),
            step("wda_build", "Install Mobster's helper on your iPhone", built,
                 "Mobster's helper is installed on your iPhone." if built else
                 (FIRST_BUILD if first_build else REBUILD) if building else
                 build["error"] or (EXPIRED_DETAIL if expired else None) or
                 (f"Mobster's helper was installed on another iPhone. Install it again on {chosen_in_sentence}."
                  if built_for and chosen and built_for != chosen else None) or
                 "Xcode signs Mobster's helper with your Apple Account and installs it on your iPhone. The first install "
                 "can take 10 minutes or more.",
                 "build", blocked_by=None if phone_ok else "phone", working=building,
                 problem=build.get("problem") if build["state"] == "failed" and not building else None),
            step("wda_running", "Start Mobster's helper", running,
                 (runner["error"] or "Mobster is connected to your iPhone.") if running else
                 runner["error"] or "Mobster's helper keeps the connection to your iPhone open while Mobster runs.",
                 "start", blocked_by="phone" if not phone_ok else None if built else "wda_build", working=runner["state"] == "starting",
                 problem=runner.get("problem") if runner["state"] in ("failed", "starting") and runner.get("error") else None),
            step("live", "Allow Mobster to tap and type", bool(config.enable_live),
                 "Tasks can tap, scroll and type on your iPhone." if config.enable_live else
                 "Turn this on so tasks can tap, scroll and type.", "enable_live"),
            step("video", "See your iPhone live", video.get("capturedAt") is not None,
                 "Your iPhone's screen is streaming." if video.get("capturedAt") else
                 "A live picture of your screen confirms everything works.", blocked_by=None if running else "wda_running"),
        ]
        teams = [{**team, "label": signing.team_label(team)} for team in manager.teams()]
        return {"complete": all(item["state"] == "done" for item in steps), "steps": steps,
                "device": device, "devices": devices, "chosen": chosen, "teams": teams,
                "tools": tool_states, "build": build, "runner": runner,
                "keys": {"openai": has_openai, "anthropic": has_anthropic, "jev": has_jev},
                "preflight": self.preflight(tools, devices, device, usb)}

    # -- actions -----------------------------------------------------------------

    def refresh(self):
        """Check the tools, teams and Developer Mode again now ("Check again")."""
        self.cache.clear()
        if hasattr(self.manager, "refresh"):
            self.manager.refresh()

    def install_tools(self):
        """Setup's Install: the iPhone connection tools, with Homebrew, in the background. Status reads its progress."""
        tools = self.manager.tools()
        if tools["iproxy"] and tools["idevice_id"]:
            return
        brew = next((path for directory in BREW_DIRS if (path := dm.executable(directory, "brew"))), None)
        if not brew:
            raise LookupError("Mobster needs Homebrew to install these on this Mac. Help › For developers shows how.")
        self.tools_install.start(brew, on_done=self.refresh)

    def open_xcode(self, opener=subprocess.Popen):
        """Setup's Open Xcode: Xcode finishes installing its components the first time it opens."""
        app = (self.manager.tools().get("xcode") or {}).get("app")
        if not app:
            raise LookupError("Xcode isn't installed yet. Get it from the App Store first.")
        opener(["/usr/bin/open", app], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.refresh()

    def save_key(self, key):
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key.strip()):
            raise ValueError("That doesn't look like a Jev key. Copy it again from TypeSafe without spaces.")
        key = key.strip()
        save_env_value(self.env_file, "TYPESAFE_API_KEY", key)
        os.environ["TYPESAFE_API_KEY"] = key

    def set_live(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false")
        save_env_value(self.env_file, "MOBSTER_ENABLE_LIVE", "1" if enabled else "0")
        self.runtime.config.enable_live = enabled

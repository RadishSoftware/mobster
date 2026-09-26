"""The setup checklist the dashboard's onboarding flow renders (docs/setup-api.md)."""

import os
import re
import time

from . import device_manager as dm
from .device_manager import save_env_value

KEY_PATTERN = re.compile(r"[A-Za-z0-9_\-]{20,200}")
INSTALL_TOOLS = "brew install libimobiledevice"
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
EXPIRED_DETAIL = ("Your runner's signature expired: with a free Apple account it lasts 7 days. Rebuild it to keep "
                  "using your iPhone.")
FIRST_BUILD = "The first build can take 10 minutes or more. Keep your iPhone unlocked and connected."
REBUILD = "Rebuilding is quicker than the first build. Keep your iPhone unlocked and connected."
DEVELOPER_MODE_TTL = (60.0, 5.0)  # re-read after this long when on, and when off or unknown


class SetupService:
    def __init__(self, runtime, manager, env_file):
        self.runtime, self.manager, self.env_file = runtime, manager, env_file
        self.cache = {}

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
        """The tools step's detail and commands: Xcode first (it takes longest), then libimobiledevice."""
        xcode = tools["xcode"]
        usb_ok = bool(tools["iproxy"] and tools["idevice_id"])
        homebrew = any(dm.executable(directory, "brew") for directory in BREW_DIRS)
        commands = [] if usb_ok else ([] if homebrew else [INSTALL_HOMEBREW]) + [INSTALL_TOOLS]
        # Homebrew's installer can switch the Mac to the Command Line Tools, so Xcode's fixes come after it.
        commands += xcode["fixes"]
        if xcode["state"] == "ok" and usb_ok:
            detail = f"{xcode['version'] or 'Xcode'} and libimobiledevice are installed."
        elif xcode["state"] == "not_selected" and xcode["selected"] == dm.CLT_DIR:
            detail = CLT_SELECTED
        elif xcode["state"] != "ok":
            detail = XCODE_DETAIL.get(xcode["state"], XCODE_DETAIL["broken"])
        elif homebrew:
            detail = "Install libimobiledevice, which lets this Mac talk to your iPhone over USB."
        else:
            detail = "Install Homebrew, then libimobiledevice, which lets this Mac talk to your iPhone over USB."
        return detail, commands, {"xcode": {key: xcode[key] for key in ("state", "version", "app")},
                                  "libimobiledevice": usb_ok, "homebrew": homebrew}

    def state(self):
        manager, config = self.manager, self.runtime.config
        tools = manager.tools()
        devices = manager.devices() if tools["idevice_id"] else []
        device = manager.device(devices)
        if device is not None:
            device = {**device, "developerMode": self.developer_mode(device)}
        settings = manager.settings()
        chosen = settings.get("udid")
        chosen_name = f"“{settings['device_name']}”" if settings.get("device_name") else "Your iPhone"
        built_for = settings.get("built_for", chosen)
        build = dict(manager.build)
        runner = manager.runner_state()
        video = self.runtime.video.status() if hasattr(self.runtime.video, "status") else {}
        has_key = bool(os.environ.get("TYPESAFE_API_KEY"))
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
                     first=bool(first_build))

        def step(id_, title, done, detail, action=None, blocked_by=None, working=False, **extra):
            # blocked_by names the step this one waits on; None when it can start now.
            waiting = None if done or working else blocked_by
            state = "done" if done else "working" if working else "blocked" if waiting else "todo"
            return {"id": id_, "title": title, "state": state, "detail": detail, "action": action,
                    "blocked_by": waiting, **extra}

        # Tools come first: the Xcode download takes longest, and the key can be added while it runs.
        steps = [
            step("tools", "Install the iPhone tools", tools_ok, tools_detail, commands=tools_commands),
            step("api_key", "Add your Jev key", has_key,
                 "Jev decides each step; the key is saved privately on this Mac. Add a helper model for typing "
                 "and answers in Settings › Models.", "save_key"),
            step("phone", "Connect your iPhone", phone_ok,
                 f"“{device['name']}” is connected and trusts this Mac." if phone_ok else
                 "Unlock your iPhone and tap Trust on it." if device else
                 f"{chosen_name} isn't connected. Plug it in with a cable and unlock it." if chosen else
                 "Choose which iPhone Mobster should use." if len(devices) > 1 else
                 "Plug your iPhone into this Mac with a cable and unlock it.",
                 "choose_phone" if len(devices) > 1 or chosen and not device else None,
                 blocked_by=None if tools_ok else "tools"),
            step("wda_build", "Install the Mobster runner on your iPhone", built,
                 "The runner is built for your iPhone." if built else
                 (FIRST_BUILD if first_build else REBUILD) if building else
                 build["error"] or (EXPIRED_DETAIL if expired else None) or
                 (f"The runner was built for another iPhone. Build it again for {chosen_name}."
                  if built_for and chosen and built_for != chosen else None) or
                 "Builds WebDriverAgent with your Apple development team. The first build can take 10 minutes or more.",
                 "build", blocked_by=None if phone_ok else "phone", working=building),
            step("wda_running", "Start the runner", running,
                 (runner["error"] or "Mobster is connected to your iPhone.") if running else
                 runner["error"] or "Keeps the connection to your iPhone open while Mobster is running.",
                 "start", blocked_by="phone" if not phone_ok else None if built else "wda_build", working=runner["state"] == "starting"),
            step("live", "Allow Mobster to act on your iPhone", bool(config.enable_live),
                 "Tasks can tap, scroll and type on your iPhone." if config.enable_live else
                 "Turn this on so tasks can tap, scroll and type.", "enable_live"),
            step("video", "See your iPhone live", video.get("capturedAt") is not None,
                 "Your iPhone's screen is streaming." if video.get("capturedAt") else
                 "A live picture of your screen confirms everything works.", blocked_by=None if running else "wda_running"),
        ]
        return {"complete": all(item["state"] == "done" for item in steps), "steps": steps,
                "device": device, "devices": devices, "chosen": chosen, "teams": manager.teams(),
                "tools": tool_states, "build": build, "runner": runner}

    # -- actions -----------------------------------------------------------------

    def refresh(self):
        """Check the tools, teams and Developer Mode again now ("Check again")."""
        self.cache.clear()
        if hasattr(self.manager, "refresh"):
            self.manager.refresh()

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

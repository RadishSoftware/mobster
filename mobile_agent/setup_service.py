"""The setup checklist the dashboard's onboarding flow renders (docs/setup-api.md)."""

import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import mac_tools, phone_ready, signing
from .env_secrets import update_env_file
from .keys import anthropic_key, openai_key
from .runner_renewal import RunnerRenewal

# The slow reads (Xcode's state, the USB phones and System Information) run side by side.
READS = ThreadPoolExecutor(max_workers=3, thread_name_prefix="mobster-setup-read")

INSTALL_TOOLS = "brew install libimobiledevice"
# What Install runs in the background when this build doesn't carry the tools itself (a source checkout with Homebrew):
# libimobiledevice, and ideviceinstaller for the Apps page. The shipped app never needs it (build-iphone-tools.sh).
INSTALL_FORMULAE = ("libimobiledevice", "ideviceinstaller")
# brew.sh's own install command, shown only when Homebrew is missing.
INSTALL_HOMEBREW = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
BREW_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
# What is wrong with Xcode, in the user's words (mac_tools.xcode_status states). The step lists the
# Terminal commands that fix it, so these say what happened and any way to fix it without Terminal.
XCODE_DETAIL = {
    "missing": "Install Xcode from the Mac App Store. It's a large download and can take an hour, so start it "
               "now, then open Xcode once when it finishes.",
    "license": "Accept the Xcode license: open Xcode and agree, or use the command below.",
    "first_launch": "Xcode hasn't finished installing. Open it once and let it install its components, or use "
                    "the command below.",
    "no_ios": "Xcode doesn't have the iOS platform yet. Open Xcode › Settings › Components and get iOS, or use "
              "the command below.",
    "broken": "Xcode isn't responding. Open it once to finish installing it, then check again.",
}
EXPIRED_DETAIL = ("Mobster's helper needs a refresh: a free Apple Account signs it for 7 days at a time. Refresh it to "
                  "keep using your iPhone.")
# Measured 17 Oct 2026 on this repository's M5 Max and an iPhone 15 Pro: 115 s to build, sign and install (a cold
# clone of the pinned release is 5 s and its unsigned build 52 s). docs/requirements.mdx says the same.
FIRST_BUILD = "The first install takes about 2 minutes. Keep your iPhone unlocked and plugged in."
REBUILD = "Installing again is quicker than the first time. Keep your iPhone unlocked and plugged in."
DEVELOPER_MODE_TTL = (60.0, 5.0)  # re-read after this long when on, and when off or unknown
# The preflight's one-line reading of each Xcode state (mac_tools.xcode_status).
XCODE_WORD = {"ok": "Ready", "missing": "Not installed", "license": "License",
              "first_launch": "Not finished", "no_ios": "No iOS platform", "broken": "Not responding"}
XCODE_PREFLIGHT = {
    "missing": "Free from the Mac App Store. It's a large download, so start it now.",
    "license": "Open Xcode once and accept its license.",
    "first_launch": "Open Xcode once so it can finish installing.",
    "no_ios": "Open Xcode › Settings › Components and get iOS.",
    "broken": "Open Xcode once so it can finish installing.",
}
def plan_ready():
    """Whether Mobster's agent runs a task typed in Mobster without an OpenAI or Anthropic key."""
    from . import engines
    try:
        return (engines.model_provider(engines.smart_model()) not in ("openai", "anthropic")
                and bool(engines.smart_key()))
    except Exception:  # noqa: BLE001 -- a check that fails leaves the step to the keys
        return False


def agents_ready():
    """The coding agents ready on this Mac (installed and signed in: Codex, Claude Code, Gemini CLI), by name."""
    from .ai_plans import agents
    try:
        return [state.get("name") or agent_id for agent_id,
                state in agents.snapshot(max_age=300).items() if state.get("ready")]
    except Exception:  # noqa: BLE001 -- a check that fails leaves the step to the keys
        return []


def agent_detail(names):
    """The AI step's line when a coding agent is what typed tasks run on."""
    listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"
    return f"Tasks you type run on {listed}, signed in on this Mac."


# The AI account step (MESSAGING §11, Setup): whose account Mobster's agent uses, in the user's words.
KEY_DETAIL = {
    "plan": "Mobster runs on a plan you already pay for. Settings › Models shows which.",
    "openai": "Mobster's agent uses your OpenAI account. Your key is saved privately on this Mac.",
    "anthropic": "Mobster's agent uses your Claude account. Your key is saved privately on this Mac.",
    "none": "Mobster's agent runs on a plan you already pay for or on your own key. "
    "Choose one to run tasks you type here.",
}


# The Photos step's line for each answer the helper gives (MOBSTER_QUEUE_PREFLIGHT, phone_ready.py). The person answers
# iOS's question on the iPhone; Mobster never presses it.
PHOTOS_DETAIL = {
    "authorized": "Mobster can add the photos and videos you queue to Photos.",
    "limited": "Mobster can add the photos and videos you queue to Photos. Limit Access is enough.",
    "not_determined": "Press Allow, then answer your iPhone's question. Limit Access is enough.",
    "denied": "Photos access is off for Mobster on your iPhone. Turn it on in Settings › Privacy & Security › Photos › "
              "WebDriverAgentRunner-Runner. Limit Access is enough.",
    "restricted": "Photos is restricted on your iPhone (Screen Time or a profile), so Mobster can't add to it.",
    None: "Mobster couldn't read Photos access on your iPhone yet. Check again in a moment.",
}


class ToolsInstall:
    """Installs the iPhone connection tools with Homebrew in the background, one line of progress at a time."""

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
    """Homebrew's progress line in a person's words ("Downloading libplist"),
    or None for a line that says nothing new."""
    match = re.match(r"==> (Fetching|Pouring|Installing|Upgrading)\s+(?:dependencies for\s+)?([\w@.+-]+)", line)
    if not match:
        return None
    verb = {"Fetching": "Downloading", "Pouring": "Installing", "Installing": "Installing",
            "Upgrading": "Updating"}[match[1]]
    name = re.sub(r"(--|\.)[\w.]*bottle.*$", "", match[2]).rstrip(":")
    return f"{verb} {name}"


def task_running(runtime):
    """Whether a task is running now on the runtime's primary phone."""
    try:
        fleet = getattr(runtime, "fleet", None)
        if fleet is not None and callable(getattr(runtime, "run_on", None)):
            return runtime.run_on(fleet.primary) is not None
        return getattr(runtime, "active", None) is not None
    except Exception:
        return True


class SetupService:
    """``video`` and ``busy`` are a phone's own when it is not the runtime's primary phone (fleet.py)."""

    def __init__(self, runtime, manager, env_file, video=None, busy=None, stop=None, idle=None):
        self.runtime, self.manager, self.env_file = runtime, manager, env_file
        # What the manager's watchdog and presence need from the runtime: whether a task runs on this phone, how to end
        # it with a plain sentence when its helper is dead (wda/watchdog.py), and what to let go of when the phone has
        # been idle (owner_presence.py: live view, prewarmed screen, awake hold).
        if hasattr(manager, "watchdog"):
            manager.busy = busy or (lambda: task_running(runtime))
            manager.end_task = stop or (lambda message: runtime.stop_phone_run(runtime.fleet.primary, message))
            if idle is not None:
                manager.idle_hooks.append(idle)
            elif callable(getattr(runtime, "release_idle", None)):
                manager.idle_hooks.append(lambda: runtime.release_idle(runtime.fleet.primary))
        self.video = video
        self.cache = {}
        self._team_signing = None
        self.tools_install = ToolsInstall()
        # The watch loop renews the runner before its signature runs out (runner_renewal.py).
        self.renewal = RunnerRenewal(manager, busy=busy or (lambda: task_running(runtime))) \
            if hasattr(manager, "start_build") else None
        if self.renewal is not None:
            manager.renewal = self.renewal

    def team_signing(self):
        """Signing without Xcode for a paid team (team_signing/), or None while MOBSTER_EXPERIMENTAL_NO_XCODE is off."""
        from . import team_signing
        if not team_signing.enabled():
            return None
        if self._team_signing is None:
            from .team_signing.service import TeamSigning
            self._team_signing = TeamSigning(self.manager)
        return self._team_signing

    def _signing(self):
        signing_service = self.team_signing()
        if signing_service is None:
            raise LookupError("Without Xcode is off in this Mobster")
        return signing_service

    def signing_card(self):
        self._signing().card()

    def signing_approve(self, digest):
        if not isinstance(digest, str) or not digest:
            raise ValueError("Approve the card by its digest")
        self._signing().approve(digest)

    def signing_resolve(self, digest, made):
        if not isinstance(digest, str) or type(made) is not bool:
            raise ValueError("Say which change, and whether it was made")
        self._signing().resolve(digest, made)

    def signing_reveal_developer_mode(self):
        """Makes the Developer Mode switch appear in Settings on the chosen iPhone. The person still turns it on."""
        from .team_signing import tooling
        device = self.manager.device()
        program = tooling.pmd3()
        if device is None or not device.get("trusted"):
            raise LookupError("Connect your iPhone and tap Trust first")
        if program is None:
            raise LookupError("pymobiledevice3 isn't installed: pipx install pymobiledevice3")
        tooling.run_short(tooling.reveal_developer_mode_command(program, device["udid"]), timeout=60)
        self.cache.pop(("developer_mode", device["udid"]), None)

    def developer_mode(self, device):
        """Whether Developer Mode is on (True or False), or None when iOS or libimobiledevice can't say."""
        if not device or not device.get("trusted") or not device.get("udid"):
            return None
        cached = self.cache.get(("developer_mode", device["udid"]))
        if cached and time.monotonic() - cached[0] < DEVELOPER_MODE_TTL[cached[1] is not True]:
            return cached[1]
        info = mac_tools.tool("ideviceinfo")
        output = mac_tools.run([info, "-u", device["udid"], "-q", "com.apple.security.mac.amfi", "-k",
                         "DeveloperModeStatus"], timeout=5) if info else None
        value = {"true": True, "false": False}.get((output or "").strip().lower())
        self.cache[("developer_mode", device["udid"])] = (time.monotonic(), value)
        return value

    def tools_step(self, tools):
        """The tools step's detail and commands: Xcode first (it takes longest), then the iPhone connection tools."""
        xcode = tools["xcode"]
        usb_ok = bool(tools["iproxy"] and tools["idevice_id"])
        bundled = (usb_ok and mac_tools.bundled_tool("iproxy") == tools["iproxy"]
                   and mac_tools.bundled_tool("idevice_id") == tools["idevice_id"])
        homebrew = any(mac_tools.executable(directory, "brew") for directory in BREW_DIRS)
        commands = [] if usb_ok else ([] if homebrew else [INSTALL_HOMEBREW]) + [INSTALL_TOOLS]
        # Homebrew's installer can switch the Mac to the Command Line Tools, so Xcode's fixes come after it.
        commands += xcode["fixes"]
        if xcode["state"] == "ok" and usb_ok:
            detail = f"{xcode['version'] or 'Xcode'} and the iPhone connection tools are installed."
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

    def preflight(self, tools, devices, device, usb, without_xcode=False):
        """Setup's first screen: Xcode, the cable, trust and Developer Mode, each with a word."""
        xcode = tools["xcode"]
        usb_tools = bool(tools["idevice_id"])
        connected = bool(devices) or bool(usb)

        def check(id_, title, state, word, detail):
            return {"id": id_, "title": title, "state": state, "word": word, "detail": detail}

        xcode_detail = (xcode["version"] or "Xcode is installed.") if xcode["state"] == "ok" else \
            XCODE_PREFLIGHT.get(xcode["state"], XCODE_PREFLIGHT["broken"])
        checks = [check("xcode", "Xcode", "ok" if xcode["state"] == "ok" else "todo",
                        XCODE_WORD.get(xcode["state"], "Not responding"),
                        xcode_detail)]
        if without_xcode:
            detail = "Mobster signs its helper on this Mac and installs it without Xcode (a paid Apple Developer team)."
            checks = [check("xcode", "Xcode", "ok", "Not needed", detail)]
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
        elif without_xcode:
            checks.append(check("developer_mode", "Developer Mode", "unknown", "Can't tell yet",
                                "Mobster reads it over the cable. If Settings › Privacy & Security has no Developer "
                                "Mode switch, Setup can show it."))
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
        has_openai = bool(openai_key()[0])
        has_anthropic = bool(anthropic_key()[0])  # Smart runs on Claude with it (engines.smart_model)
        has_plan = plan_ready()  # a plan or provider from Settings › Models, with its sign-in or key
        # Else a coding agent signed in on this Mac: typed tasks go to it (the composer's default, lib/agents).
        agents_on = [] if has_openai or has_anthropic or has_plan else agents_ready()
        # The account Smart plans on (engines.smart_model): with both keys, Claude's unless OpenAI was chosen.
        from . import engines
        claude = has_anthropic and (engines.model_provider(engines.smart_model()) == "anthropic" or not has_openai)
        team_signing = self.team_signing()
        # Without Xcode, the helper is signed here and installed by pymobiledevice3: Xcode and git aren't needed.
        tools_ok = all(tools[name] for name in (("iproxy", "idevice_id") if team_signing else
                                                ("xcodebuild", "iproxy", "idevice_id", "git")))
        tools_detail, tools_commands, tool_states = self.tools_step(tools)
        phone_ok = bool(device and device["trusted"])
        built = manager.built()
        expired = manager.expired()
        expires_at = manager.signature_expiry()
        running = runner["state"] == "running"
        photos = phone_ready.photos_status(manager.wda_url) if running and phone_ready.enabled() else None
        building = build["state"] == "running"
        first_build = build.get("first", not built and not expires_at)
        build.update(expires_at=round(expires_at * 1000) if expires_at else None, expired=expired,
                     started_at=round(build["started_at"] * 1000) if build.get("started_at") else None,
                     first=bool(first_build), renewal=bool(build.get("renewal")), problem=build.get("problem"),
                     team=build.get("team") or settings.get("team"))
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
            step("api_key", "Connect your AI account", has_openai or has_anthropic or has_plan or bool(agents_on),
                 agent_detail(agents_on) if agents_on else
                 KEY_DETAIL["anthropic" if claude else "openai" if has_openai else "plan" if has_plan else "none"],
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
                 "Xcode signs Mobster's helper with your Apple Account and installs it on your iPhone. "
                 "The first install takes about 2 minutes.",
                 "build", blocked_by=None if phone_ok else "phone", working=building,
                 problem=build.get("problem") if build["state"] == "failed" and not building else None),
            step("wda_running", "Start Mobster's helper", running,
                 (runner["error"] or "Mobster is connected to your iPhone.") if running else
                 runner["error"] or "Mobster's helper keeps the connection to your iPhone open while Mobster runs.",
                 "start", blocked_by="phone" if not phone_ok else None if built else "wda_build",
                 working=runner["state"] == "starting",
                 problem=(runner.get("problem") if runner["state"] in ("failed", "starting") and runner.get("error")
                          else None)),
            step("live", "Allow Mobster to tap and type", bool(config.enable_live),
                 "Tasks can tap, scroll and type on your iPhone." if config.enable_live else
                 "Turn this on so tasks can tap, scroll and type.", "enable_live"),
            *([step("photos", "Let Mobster add to Photos", photos in phone_ready.GRANTED, PHOTOS_DETAIL[photos],
                    "allow_photos" if photos == "not_determined" else None,
                    blocked_by=None if running else "wda_running", working=running and photos is None)]
              if phone_ready.enabled() else []),
            step("video", "See your iPhone live", video.get("capturedAt") is not None,
                 "Your iPhone's screen is streaming." if video.get("capturedAt") else
                 "A live picture of your screen confirms everything works.",
                 blocked_by=None if running else "wda_running"),
        ]
        teams = [{**team, "label": signing.team_label(team)} for team in manager.teams()]
        # Photos is for the content queue only: the rest of setup is complete without it.
        return {"complete": all(item["state"] == "done" for item in steps if item["id"] != "photos"), "steps": steps,
                "device": device, "devices": devices, "chosen": chosen, "teams": teams,
                "tools": tool_states, "build": build, "runner": runner,
                "keys": {"openai": has_openai, "anthropic": has_anthropic},
                "preflight": self.preflight(tools, devices, device, usb, without_xcode=team_signing is not None),
                **({"teamSigning": team_signing.status()} if team_signing is not None else {}),
                "wifi": self.wifi_step(device, built, running)}

    def wifi_step(self, device, built, running):
        """The Wi-Fi step of this phone's setup (wireless/onboarding.py): off unless Wi-Fi for iPhones is on."""
        from .wireless import onboarding
        facts = onboarding.Facts(attached=device is not None, trusted=bool(device and device.get("trusted")),
                                 built=built, running=running, ios=(device or {}).get("ios"))
        return onboarding.step(self._wifi_udid(device), getattr(self.manager, "data_dir", None), facts)

    def answer_wifi(self, device, yes):
        """`mobster setup`'s answer to "use this iPhone over Wi-Fi?" (state()'s ``wifi`` is ``asking``). LookupError
        when Mobster isn't asking about it now."""
        from .wireless import onboarding
        onboarding.answer(self._wifi_udid(device), yes)

    def _wifi_udid(self, device):
        return (getattr(self.manager, "pinned", None) or (device or {}).get("udid")
                or self.manager.settings().get("udid"))

    def refresh(self):
        """Check the tools, teams and Developer Mode again now ("Check again")."""
        self.cache.clear()
        if self._team_signing is not None:
            self._team_signing.refresh()
        if hasattr(self.manager, "refresh"):
            self.manager.refresh()

    def install_tools(self):
        """Setup's Install: the iPhone connection tools, with Homebrew, in the background. Status reads its progress."""
        tools = self.manager.tools()
        if tools["iproxy"] and tools["idevice_id"]:
            return
        brew = next((path for directory in BREW_DIRS if (path := mac_tools.executable(directory, "brew"))), None)
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

    def allow_photos(self, device=None):
        """Setup's Allow for Photos on ``device`` (None: the first phone): iOS shows its Photos question on the
        iPhone and the person answers there, for up to phone_io.photos.AUTHORIZE_WAIT_S seconds. Nothing is saved or
        pressed.
        LookupError (its sentence is the answer) when the check is off, a task has the phone, or the helper can't."""
        if not phone_ready.enabled():
            raise LookupError("Setup's Photos step is off in this Mobster (MOBSTER_QUEUE_PREFLIGHT).")
        from .phone_io import PhoneIOError
        from .phone_io.photos import authorize
        from .phone_io.transfer import phone_from_runtime
        try:
            phone = phone_from_runtime(self.runtime, device)
            if phone.runner is None:
                raise PhoneIOError("Mobster's helper isn't set up on this iPhone yet.", "no_device")
            with phone.runner() as request:
                authorize(request, name=phone.name, activate=lambda bundle: request(
                    "POST", "/wda/apps/activate", {"bundleId": bundle}, 15))
        except PhoneIOError as error:
            raise LookupError(str(error)) from None

    def set_live(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false")
        update_env_file(self.env_file, {"MOBSTER_ENABLE_LIVE": "1" if enabled else "0"})
        self.runtime.config.enable_live = enabled

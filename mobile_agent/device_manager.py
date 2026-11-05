"""The USB iPhone lifecycle, driven from the setup flow."""

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import env_file, mac_tools, owner_presence, setup_errors, signing, supervised
from .device_info import MODEL_NAMES, enclosure_code
from .devices import LOOPBACK, UDID_PATTERN
from .mac_tools import VIDEO_PORT, WDA_PORT, XCODE_OK_TTL, XCODE_RETRY_TTL, XCODE_SHIMS
from .owner_presence import OwnerPresence, Poller
from .runner_hints import (
    AUTOMATION_WAIT_HINT,
    AUTOMATION_WAIT_S,
    AUTOMATION_WAITING,
    EXPIRED_HINT,
    EXPOSED_HINT,
    LOCKED_START_HINT,
    PREARM_ASK,
    PREARM_MIN_IOS,
    RUNNER_HINTS,
    build_hint,
    runner_stamp,
)
from .supervised import BUILD_STEPS, stop_group
from .transport import read_kept
from .wda import gate as wda_gate
from .wda import patches as wda_patches
from .wda import source as wda_source
from .wda.patches import BUNDLE_REBRAND, LEGACY_RUNNER, RUNNER_ID
from .wda.watchdog import Watchdog

TEAMS_TTL = 60.0
USB_TTL = 5.0
TEAM_PATTERN = re.compile(r"[A-Z0-9]{10}")
WATCH_INTERVAL = 5.0
# WDA can wedge: /status still answers while every call that needs the phone's UI
# (screen reads, taps, video) hangs, e.g. with the phone locked or behind Control
# Center or the iOS 26 Siri glow (measured 2026-09-23: reads hung for minutes).
# /wda/locked is cheap when WDA is healthy, so it is the probe.
PROBE_INTERVAL = 3.0
# The watch loop looks at the probe this often; its other jobs (autostart, presence, exposure, a locked build's retry,
# the renewal) still run every WATCH_INTERVAL. A cause is named within engine_health.NAMED_WITHIN_S of when it begins:
# PROBE_TICK + PROBE_INTERVAL + a probe's time + the sampler's tick.
PROBE_TICK = 1.0
PROBE_TIMEOUT = 3.0
# A runner this soon after its start is still starting, not wedged (xcodebuild takes a while to answer /status).
STARTUP_GRACE_SECONDS = 120.0
# WEDGED_RESTART_SECONDS and MIN_RESTART_GAP live in wda/watchdog.py, which decides the restart.
LOG_TAIL = 40
# How long one CoreDevice lock read may take before the runner launches as it would without one.
HOLD_READ_TIMEOUT = 4.0
RUNNER_LOG_WINDOW = 16384
AUTOMATION_OK_KEPT = 32           # phones remembered as having answered once (prearm stops asking for them)


def wifi_runner_command(xcodebuild, plan, udid):
    if not isinstance(udid, str) or not UDID_PATTERN.fullmatch(udid):
        raise ValueError("A UDID is 24 to 40 hex digits and dashes")
    return [xcodebuild, "test-without-building", "-xctestrun", str(plan), "-destination", f"id={udid}"]


# One build at a time across phones: every build rebrands and patches the shared WebDriverAgent clone, and
# two xcodebuilds at once would also double the heaviest job Mobster runs.
BUILD_LOCK = threading.Lock()


class DeviceManager:
    """One USB iPhone's lifecycle."""

    build_step = None  # the git or xcodebuild process the build is running, if any

    def __init__(self, data_dir, wda_url=f"http://127.0.0.1:{WDA_PORT}", *, udid=None, root=None,
                 video_port=VIDEO_PORT):
        self.data_dir = Path(data_dir)
        try:
            env_file.private_dir(self.data_dir)  # keys, journal, team id and logs live here
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
        self.health = {"responsive": None, "locked": None, "since": None, "probed_at": 0.0, "ready": None}
        # Whether a task runs on this phone (SetupService sets it) and how a task ends when its helper is dead.
        self.busy = lambda: False
        self.end_task = None
        self.idle_hooks = []  # called once when the phone has been idle for owner_presence.IDLE_RELEASE_S
        self.runner_started_at = None
        self.watchdog = Watchdog(self._restart_wedged_runner, url=lambda: self.wda_url, busy=lambda: self.busy(),
                                 alive=self._runner_alive,
                                 on_dead_runner=lambda message: self.end_task(message) if callable(self.end_task)
                                 else None)
        # Whether the phone's owner is using it (owner_presence.py); polled by watch() while no task holds the phone.
        self.presence = OwnerPresence()
        self.presence_poller = Poller(self.presence, self.wda_url, busy=lambda: self.busy())
        owner_presence.register(self.wda_url, self.presence)
        # One start or stop at a time: the watcher and the setup flow can both start the runner.
        self.runner_lock = threading.RLock()
        self.runner = None
        self.relay = None
        # What the runner started last uses: "usb" (iproxy), "wifi" (the tunnel and wireless.relay) or None (stopped).
        # Only wireless/monitor.py starts it over Wi-Fi, with MOBSTER_WIFI_TRANSPORT on.
        self.transport = None
        self.wifi_address = None
        # The last start the exposure check (check_exposure) looked at, and what answered where it must not.
        self.exposure_checked = None
        self.exposure = None
        self.runner_line = self.runner_cause = None
        self._answered = set()           # phones whose helper answered (note_answered), so prearm reads no file
        # whether the chosen phone was on the cable at the last watch round (rearm_on_attach)
        self._attached_before = None
        self.locked_retry_for = None  # the started_at of the locked-phone build failure already tried again
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
        env_file.private_dir(self.data_dir)
        env_file.private_dir(self.settings_path.parent)
        with env_file.open_private(self.settings_path, "w") as stream:
            stream.write(json.dumps(settings, indent=1))
        return settings

    def tools(self):
        """Paths of the tools setup needs (None when missing or unusable),
        and Xcode's state (mac_tools.xcode_status)."""
        cached = self.cache.get("xcode")
        if cached and time.monotonic() < cached[0]:
            xcode = cached[1]
        else:
            xcode = mac_tools.xcode_status()
            self.cache["xcode"] = (time.monotonic() + (XCODE_OK_TTL if xcode["state"] == "ok" else XCODE_RETRY_TTL),
                                   xcode)
        developer = xcode["developer_dir"]
        return {"xcodebuild": mac_tools.xcode_tool("xcodebuild", developer) if xcode["state"] == "ok" else None,
                "iproxy": mac_tools.tool("iproxy"), "idevice_id": mac_tools.tool("idevice_id"),
                "ideviceinfo": mac_tools.tool("ideviceinfo"),
                "git": mac_tools.xcode_tool("git", developer), "xcode": xcode}

    def refresh(self):
        """Forget the cached tool and team checks, so the next read sees a fix the user just made."""
        for key in ("xcode", "teams", "usb"):
            self.cache.pop(key, None)

    def xcode_env(self):
        developer = self.tools()["xcode"]["developer_dir"]
        return mac_tools.xcode_env(developer) if developer else None

    def runner_env(self):
        return {**(self.xcode_env() or mac_tools.xcode_env()), **wda_gate.runner_env("phone")}

    def devices(self):
        """USB iPhones, with whether this Mac is trusted (lockdown info readable)."""
        listing = mac_tools.tool("idevice_id")
        output = mac_tools.run([listing, "-l"]) if listing else None
        udids = []
        for line in (output or "").splitlines():
            udid = line.strip()
            # 0000FE01-… entries are not physical USB phones.
            if UDID_PATTERN.fullmatch(udid) and not udid.startswith("0000FE01") and udid not in udids:
                udids.append(udid)
        info_tool = mac_tools.tool("ideviceinfo")
        devices = []
        for udid in udids:
            # A trusted phone's identity is stable; untrusted ones are re-read so Trust shows at once.
            cached = self.cache.get(("info", udid))
            info = dict(cached[1]) if cached and time.monotonic() - cached[0] < 60 else {}
            if info_tool and not info:
                for key, field in (("DeviceName", "name"), ("ProductType", "model"), ("ProductVersion", "ios"),
                                   ("DeviceEnclosureColor", "enclosure")):
                    value = mac_tools.run([info_tool, "-u", udid, "-k", key], timeout=5)
                    if value is not None:
                        info[field] = value.strip()
                if info:
                    self.cache[("info", udid)] = (time.monotonic(), dict(info))
            devices.append({"udid": udid, "name": info.get("name") or "iPhone", "model": info.get("model"),
                            "modelName": MODEL_NAMES.get(info.get("model")), "ios": info.get("ios"),
                            "enclosure": enclosure_code(info.get("enclosure")), "trusted": bool(info)})
        return devices

    def usb_iphones(self):
        cached = self.cache.get("usb")
        if cached and time.monotonic() - cached[0] < USB_TTL:
            return cached[1]
        count = None
        for data_type in ("SPUSBHostDataType", "SPUSBDataType"):
            output = mac_tools.run(["/usr/sbin/system_profiler", data_type, "-json"], timeout=8)
            try:
                tree = json.loads(output) if output else None
            except ValueError:
                tree = None
            if isinstance(tree, dict) and isinstance(tree.get(data_type), list):
                count = mac_tools.count_iphones(tree[data_type])
                break
        self.cache["usb"] = (time.monotonic(), count)
        return count

    def device(self, devices=None):
        # never a different phone than the one chosen: a runner signed for one phone fails on another
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
        if "teams" in self.cache and time.monotonic() - self.cache["teams"][0] < TEAMS_TTL:
            return self.cache["teams"][1]
        teams = signing.merge_teams(signing.account_teams(), signing.certificate_teams())
        self.cache["teams"] = (time.monotonic(), teams)
        return teams

    def driver(self):
        """What installs and launches the runner for this phone: "xcodebuild" (Xcode signs and runs it), or, when it was
        signed on this Mac with the team's profile (team_signing), "pymobiledevice3" or "go-ios"."""
        return self.settings().get("driver") or "xcodebuild"

    def built(self):
        """Built for the chosen phone (a build records the phone it was made for), and still signed."""
        settings = self.settings()
        built_for = settings.get("built_for", settings.get("udid"))
        if self.driver() != "xcodebuild":
            runner = (settings.get("signed_runner") or {}).get("path")
            return bool(built_for) and built_for == settings.get("udid") and bool(runner) and Path(runner).is_dir() \
                and not self.expired() and self.patched()
        return bool(built_for) and built_for == settings.get("udid") and any(
            self.derived.glob("Build/Products/*.xctestrun")) and not self.expired() and self.patched()

    def patched(self):
        """Whether the build was made with the WDA patches this Mobster has (wda_patches.patch_digest),
        not an older set."""
        return self.settings().get("built_patches") == wda_patches.patch_digest()

    def out_of_date(self):
        """A build is there, made with other WDA patches than this Mobster has: it has to be built again."""
        return any(self.derived.glob("Build/Products/*.xctestrun")) and not self.patched()

    def signature_expiry(self):
        settings = self.settings()
        if "expires_at" not in settings and any(self.derived.glob(signing.PROFILE_GLOB)):
            settings = self.save_settings(expires_at=signing.profile_expiry(self.derived))
        value = settings.get("expires_at")
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def expired(self, now=None):
        expires = self.signature_expiry()
        return expires is not None and (time.time() if now is None else now) >= expires

    def _wda_get(self, path, timeout):
        # one kept connection: a connection per read ran WDA out of descriptors
        return read_kept(self.wda_url, path, timeout)

    def wda_ready(self, timeout=1.5):
        try:
            return (self._wda_get("/status", timeout).get("value") or {}).get("ready") is True
        except Exception:
            return False

    def _runner_alive(self):
        """Whether the runner's child process runs now (not just its supervisor thread)."""
        runner = self.runner
        alive = getattr(runner, "alive", None)
        return bool(alive) if alive is not None else bool(runner is not None and runner.running)

    def runner_waiting(self):
        """"unlock" while the runner is held back because the phone is locked (``_runner_hold``), else None."""
        runner = self.runner
        return "unlock" if getattr(runner, "held", None) == "locked" and not self._runner_alive() else None

    def _runner_hold(self):
        # xcodebuild on a locked phone waits 60 s and fails, and each failure counts toward 'didn't start'
        from .wireless import devicectl
        udid = self.pinned or self.settings().get("udid")
        return "locked" if udid and devicectl.read_lock_state(udid, timeout=HOLD_READ_TIMEOUT) is True else None

    def retry_locked_build(self):
        build = self.build
        problem = build.get("problem") or {}
        key = build.get("started_at")
        if build.get("state") != "failed" or problem.get("kind") != "locked" or key is None \
                or key == self.locked_retry_for:
            return False
        settings = self.settings()
        udid, team = self.pinned or settings.get("udid"), build.get("team") or settings.get("team")
        if not udid or not team or self.device() is None:
            return False
        from .wireless import devicectl
        if devicectl.read_lock_state(udid, timeout=HOLD_READ_TIMEOUT) is not False:
            return False
        self.locked_retry_for = key
        self.start_build(team)
        return True

    def _relaunch_gate(self):
        # the watchdog admits no relaunch while an action is in flight or an outcome under a task is unknown
        if self.watching.is_set():
            return None
        if self.watchdog.admit(time.monotonic()) is not None:
            return None
        return lambda: self.watchdog.done(True)

    def wda_alive(self, attempts=3, timeout=4.0, gap=2.0):
        # WDA serves one request at a time, so a busy runner can miss one /status; a new one would replace it mid-task
        for attempt in range(attempts):
            if self.wda_ready(timeout=timeout):
                return True
            if attempt + 1 < attempts:
                time.sleep(gap)
        return False

    def attached(self, udid=None):
        """Whether the chosen (or ``udid``'s) iPhone is plugged in now; None when that can't be told."""
        return mac_tools.usb_attached(udid or self.pinned or self.settings().get("udid"))

    def locked(self):
        """WDA answers but the phone is locked (the last probe)."""
        return self.health["locked"] is True

    def health_error(self):
        """What to tell the user when WDA answers but cannot reach the phone's UI."""
        if self.locked():
            return "Your iPhone is locked. Unlock it to run tasks and see its screen."
        if self.health["responsive"] is False:
            return "Your iPhone isn't responding. Unlock it and close Control Center or Siri if they're open."
        return None

    def prearm(self, udid=None, ios=None):
        udid = udid or self.pinned or self.settings().get("udid")
        if not udid or udid in self._answered or udid in (self.settings().get("automation_ok") or []):
            return None
        if ios is None:   # from the phone list's cache (devices()): asked every second, it never runs a tool itself
            cached = self.cache.get(("info", udid))
            ios = cached[1].get("ios") if cached else None
        try:
            major = int(str(ios).split(".")[0])
        except (TypeError, ValueError):
            return None
        return "ui_automation" if major >= PREARM_MIN_IOS else None

    def note_answered(self):
        udid = self.pinned or self.settings().get("udid")
        if not udid or udid in self._answered:
            return
        kept = list(self.settings().get("automation_ok") or [])
        if udid not in kept:
            self.save_settings(automation_ok=(kept + [udid])[-AUTOMATION_OK_KEPT:])
        self._answered.add(udid)

    def runner_state(self, ready=None):
        if ready is None:
            ready = self.wda_ready()
        if ready:
            self.note_answered()
        state = self._runner_state(ready)
        if state["state"] != "running" and (asked := self.prearm()):
            state["prearm"] = {"kind": asked, "ask": PREARM_ASK}
        return state

    def _runner_state(self, ready):
        if self.runner is None or not self.runner.running:
            # A runner started outside Mobster (scripts/usb-wda.sh, Xcode) serves just as well.
            if ready:
                return {"state": "running", "error": self.health_error(), "external": True}
            if not self.exposure and self.settings().get("autostart") and self.built():
                return {"state": "starting", "error": None}   # the watch loop starts it within WATCH_INTERVAL
            return {"state": "stopped", "error": EXPOSED_HINT if self.exposure else None}
        if ready:
            return {"state": "running", "error": self.health_error()}
        if self.runner_waiting() == "unlock":
            return {"state": "starting", "error": LOCKED_START_HINT, "waiting": "unlock"}
        failing = self.runner.restarts >= 3 and self.runner.last_exit not in (None, 0)
        hint = self.runner_hint()
        return {"state": "failed" if failing else "starting",
                "error": hint or ("Mobster's helper keeps stopping. Unlock your iPhone and check Developer Mode."
                                  if failing else None)}

    def runner_hint(self, now=None):
        # An expired profile fails with the same "invalid code signature ... not explicitly trusted"
        # sentence as an untrusted one, so the log alone would give the wrong advice.
        self.runner_cause = None
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
        self.runner_line = self.runner_cause = None
        waiting_since = None              # when the runner said "Running tests..." with nothing after it yet
        for line in text.splitlines():
            if "Running tests..." in line:
                waiting_since = runner_stamp(line)
            elif "Test Suite " in line or "Failed to initialize" in line:
                waiting_since = None
            for needle, cause, hint in RUNNER_HINTS:
                if needle in line:
                    latest, self.runner_line, self.runner_cause = hint, line, cause
                    break
            if "ServerURLHere" in line:
                latest = self.runner_line = self.runner_cause = waiting_since = None
        if waiting_since is not None and (time.time() if now is None else now) - waiting_since >= AUTOMATION_WAIT_S:
            latest, self.runner_line, self.runner_cause = AUTOMATION_WAIT_HINT, AUTOMATION_WAITING, "ui_automation_off"
        return latest

    def runner_announced(self):
        # unlike /status, which the runner before it may still answer for a few seconds after a restart
        try:
            with open(self.logs / "wda-runner.log", "rb") as log:
                size = log.seek(0, os.SEEK_END)
                log.seek(max(self.runner_log_start, size - 4 * RUNNER_LOG_WINDOW))
                return b"ServerURLHere" in log.read()
        except OSError:
            return False

    def runner_problem(self):
        """The runner's current failure as a one-line fix (setup_errors), from the line runner_hint matched."""
        if self.expired():
            return {"kind": "expired", "fix": "Refresh Mobster's helper", "action": "renew", "raw": EXPIRED_HINT}
        if self.runner_waiting() == "unlock":
            # Nothing to press: it starts by itself once the phone is unlocked.
            return {"kind": "locked", "fix": LOCKED_START_HINT, "action": None, "raw": LOCKED_START_HINT}
        line = getattr(self, "runner_line", None)
        return setup_errors.translate(line) if line else None

    # -- actions ---------------------------------------------------------------

    def start_build(self, team=None, renewal=False):
        if team is None:
            # the saved team signs only while Xcode still lists it: a stale team never signs a build nobody picked
            team = self.settings().get("team")
            if not TEAM_PATTERN.fullmatch(team or "") or team not in {known["id"] for known in self.teams()}:
                raise ValueError("Choose your Apple Account")
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
                          "first": not any(self.derived.glob("Build/Products/*.xctestrun")), "renewal": renewal,
                          "team": team}
        self.save_settings(udid=device["udid"], device_name=device["name"])
        threading.Thread(target=self._build, args=(team, device["udid"]), name="mobster-wda-build",
                         daemon=True).start()

    def _build(self, team, udid):
        log_path = self.logs / "wda-build.log"
        env_file.private_dir(self.logs)
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
            with env_file.open_private(log_path, "w") as log:
                git = mac_tools.tool("git") or "git"
                if self.project.exists() and not wda_source.is_pinned(git, self.project):
                    shutil.rmtree(self.project)  # an older or unpinned checkout: fetch the pinned release
                if not self.project.exists():
                    self._step(wda_source.clone_command(git, self.project), log, tail)
                    wda_source.verify(git, self.project)
                log.write(f"WebDriverAgent {wda_source.REF} ({wda_source.COMMIT})\n")
                self._rebrand(team)
                self._drop_stale_profiles(team, log)
                try:
                    wda_patches.patch_wda(self.project)
                except RuntimeError as error:
                    tail.append(f"error: {error}")  # shown as the build's error
                    log.write(tail[-1] + "\n")
                    raise
                # USE_IP goes into the built test plan: WDA then listens on the phone's loopback,
                # which USB (usbmux) reaches, and not on its Wi-Fi address.
                self._step([mac_tools.tool("xcodebuild"), "-project", str(self.project / "WebDriverAgent.xcodeproj"),
                            "-scheme", "WebDriverAgentRunner", "-destination", f"id={udid}",
                            "-allowProvisioningUpdates", "-derivedDataPath", str(self.derived),
                            f"DEVELOPMENT_TEAM={team}", f"USE_IP={LOOPBACK}", "build-for-testing"], log, tail)
                self._remove_legacy_runner(team, udid, log)
            # autostart: the watch loop starts the new helper (autostart()), whichever window is open or none. Before,
            # only the Mac app's setup watcher started it, and it isn't mounted during the first run: after the first
            # install the helper stayed stopped with "Starting Mobster's helper" on screen (17 Oct).
            self.save_settings(team=team, built_for=udid, built_patches=wda_patches.patch_digest(),
                               expires_at=signing.profile_expiry(self.derived), autostart=True)
            with self.lock:
                self.build = {**self.build, "state": "succeeded", "error": None, "problem": None,
                              "log_tail": tail[-LOG_TAIL:]}
        except Exception as error:
            text = "\n".join(tail) if tail else str(error)
            with self.lock:
                self.build = {**self.build, "state": "failed", "error": build_hint(tail) if tail else str(error)[:300],
                              "problem": self._team_problem(setup_errors.problem(text), team),
                              "log_tail": tail[-LOG_TAIL:]}

    def _team_problem(self, problem, team):
        if not problem or problem.get("kind") != "xcode_signin" \
                or not re.search(r"no account for team", problem.get("raw") or "", re.I):
            return problem
        try:
            known = {item["id"] for item in self.teams()}
        except Exception:  # noqa: BLE001 -- unreadable: keep the sign-in advice
            return problem
        if not known or team in known:
            return problem
        return {**problem, "kind": "team_account", "action": "choose_team",
                "fix": f"Xcode has no Apple Account for team {team}. Choose another team"}

    def _step(self, command, log, tail):
        # git and xcodebuild come from Xcode, never the /usr/bin stubs, and run with its developer directory.
        if command[0] in XCODE_SHIMS:
            command = [mac_tools.tool(command[0]) or command[0], *command[1:]]
        name = Path(command[0]).name
        if self.watching.is_set():
            raise RuntimeError(f"Mobster is closing: {name} did not start")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   stdin=subprocess.DEVNULL, start_new_session=True, env=self.xcode_env())
        BUILD_STEPS.add(process)
        self.build_step = process
        started = time.monotonic()
        timer = threading.Timer(supervised.BUILD_STEP_LIMIT, stop_group, (process,))
        timer.daemon = True
        timer.start()
        try:
            for line in process.stdout:
                log.write(line)
                tail.append(line.rstrip()[:400])
                del tail[:-LOG_TAIL * 5]
                with self.lock:
                    self.build["log_tail"] = tail[-LOG_TAIL:]
            code = process.wait()
        finally:
            timer.cancel()
            process.stdout.close()
            BUILD_STEPS.discard(process)
            self.build_step = None
        if code != 0 and time.monotonic() - started >= supervised.BUILD_STEP_LIMIT:
            raise RuntimeError(f"{name} ran for {supervised.BUILD_STEP_LIMIT // 60} minutes and was stopped")
        if code != 0:
            raise RuntimeError(f"{name} exited with {code}")

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
        # the plain-id runner earlier versions installed is a second icon and takes one of a free Apple ID's 3 app slots
        if signing.runner_bundle_id(team) == signing.RUNNER_BUNDLE or udid in self.settings().get("legacy_removed", []):
            return
        xcrun = mac_tools.tool("xcrun")
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
        # otherwise -allowProvisioningUpdates signs with the cached, expiring profile again
        window = signing.renew_window()
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
        relay = mac_tools.relay_command(udid, self.wda_port, self.video_port)
        self.stop_runner()
        # Our own relay is stopped: a listener still on these ports is another Mobster's (DR-68). Never take it over,
        # and never drive whatever phone answers there.
        from .devices import listening
        if listening(self.wda_port) or listening(self.video_port):
            raise LookupError(f"Another Mobster is using port {self.wda_port} or {self.video_port} on this Mac, "
                              "so this iPhone's helper didn't start. Quit the other Mobster, then try again.")
        wda_patches.pin_loopback(self.derived / "Build" / "Products")
        try:
            self.runner_log_start = (self.logs / "wda-runner.log").stat().st_size
        except OSError:
            self.runner_log_start = 0
        # A new runner's health is unknown. ``watching`` and ``runner_lock`` stay the ones __init__ made: the
        # watch loop waits on that Event (close() sets it) and a second start waits on that lock.
        self.health = {"responsive": None, "locked": None, "since": None, "probed_at": 0.0, "ready": None}
        if self.driver() == "xcodebuild":
            command, env = [
                mac_tools.tool("xcodebuild"), "-project", str(self.project / "WebDriverAgent.xcodeproj"),
                "-scheme", "WebDriverAgentRunner", "-destination", f"id={udid}",
                "-derivedDataPath", str(self.derived), f"DEVELOPMENT_TEAM={settings['team']}",
                f"USE_IP={LOOPBACK}", "test-without-building"], self.runner_env()
        else:
            command, env = self._signed_runner_command(udid, settings)
        self.runner = supervised.Supervised("wda-runner", command, self.logs / "wda-runner.log", env=env,
                                            gate=self._relaunch_gate, hold=self._runner_hold)
        self.relay = supervised.Supervised("usb-relay", relay, self.logs / "usb-relay.log", gate=self._relaunch_gate)
        self.runner_started_at = time.monotonic()
        self.relay.start()
        self.runner.start()
        self.transport = "usb"
        self.save_settings(autostart=True)

    def _signed_runner_command(self, udid, settings):
        """The launch of a runner signed on this Mac (team_signing), with the environment it runs in: no Xcode's."""
        from .team_signing import tooling
        bundle_id = (settings.get("signed_runner") or {}).get("bundle_id")
        if not bundle_id:
            raise LookupError("Set the helper up again (Setup › Install Mobster's helper)")
        env = tooling.runner_env("phone")
        if self.driver() == "go-ios":
            program = tooling.go_ios()
            command = tooling.go_ios_launch_command(program, udid, bundle_id, env) if program else None
        else:
            program = tooling.pmd3()
            command = tooling.launch_command(program, udid, bundle_id, env) if program else None
        if command is None:
            raise LookupError(f"{self.driver()} isn't on this Mac, so Mobster's helper can't start")
        return command, {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}

    def start_wifi_runner(self, address, peer_udid):
        with self.runner_lock:
            if str(peer_udid or "").upper() != str(self.pinned or self.settings().get("udid") or "").upper():
                raise LookupError("CoreDevice reported a different iPhone for that Wi-Fi address, so Mobster didn't "
                                  "start on it")
            self._start_wifi_runner(address)

    def wifi_udid(self):
        """This device's UDID while its runner runs over Wi-Fi, else None."""
        running = self.runner is not None and self.runner.running
        return (self.pinned or self.settings().get("udid")) if running and self.transport == "wifi" else None

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
        xcodebuild = mac_tools.tool("xcodebuild")
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
            raise LookupError(f"Another program is using port {self.wda_port} or "
                              f"{self.video_port} on this Mac") from None
        try:
            self.runner_log_start = (self.logs / "wda-runner.log").stat().st_size
        except OSError:
            self.runner_log_start = 0
        self.health = {"responsive": None, "locked": None, "since": None, "probed_at": 0.0, "ready": None}
        self.relay = relay
        self.runner = supervised.Supervised("wda-runner", wifi_runner_command(xcodebuild, plan, udid),
                                            self.logs / "wda-runner.log", env=self.runner_env(),
                                            gate=self._relaunch_gate, hold=self._runner_hold)
        self.runner_started_at = time.monotonic()
        self.runner.start()
        self.transport, self.wifi_address = "wifi", target

    def stop_runner(self, remember=False):
        with self.runner_lock:
            self._stop_runner(remember)

    def check_exposure(self, hostnames=None):
        with self.runner_lock:
            runner = self.runner
            if runner is None or not runner.running or self.exposure_checked == self.runner_started_at:
                return
            if not self.wda_ready():
                return
            self.exposure_checked = self.runner_started_at
        from .wireless import devicectl, exposure
        udid = self.pinned or self.settings().get("udid")
        if hostnames is None:
            try:
                peer = devicectl.list_devices(timeout=4.0).get(str(udid).upper())
            except (devicectl.DevicectlError, RuntimeError):
                peer = None
            hostnames = peer.hostnames if peer is not None else ()
        _addresses, hits = exposure.exposed(hostnames)
        with self.runner_lock:
            if self.runner is not runner:
                return   # restarted while looking: the new start is checked on its own
            self.exposure = hits or None
            if hits:  # stays stopped until the person starts it again, which checks it again
                self._stop_runner(remember=True)

    def _stop_runner(self, remember):
        services = [service for service in (self.runner, self.relay) if service is not None]
        for service in services:
            signal_stop = getattr(service, "signal_stop", None)
            if callable(signal_stop):
                signal_stop()  # both at once: the second never waits out the first's grace
        for service in services:
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
                    first = self.runner is None and self.watchdog.last_restart_at is None
                    if not first and self.watchdog.admit(time.monotonic()) is not None:
                        return  # a relaunch goes through the same admission and backoff as the supervisor's
                    try:
                        self._start_runner()
                    finally:
                        if not first:
                            self.watchdog.done(True)
        except Exception:
            pass

    def rearm_on_attach(self):
        """The chosen phone was off the cable and is plugged in now: start the helper again at once (DR-77).

        A helper started while the phone was away is an ``xcodebuild`` that waits for its destination, and it looks
        for the phone only every half hour or so, so it never sees the cable go in. This sees the edge (the chosen
        phone's UDID, not any phone, listed after it was not) and starts the runner through the same admission and
        backoff as ``autostart``. It starts nothing when a helper already answers, when the phone was not seen away
        first (a launch is ``autostart``'s), or when what was plugged in is another phone. The helper is the chosen
        phone's by UDID, and every task is checked against the phone it names again before it acts
        (wda.identity.verify)."""
        settings = self.settings()
        udid = self.pinned or settings.get("udid")
        if not udid or not settings.get("autostart"):
            self._attached_before = None
            return False
        now_attached = self.attached(udid)
        before, self._attached_before = self._attached_before, now_attached
        if before is not False or now_attached is not True:
            return False
        try:
            with self.runner_lock:
                if not (self.built() and not self.expired() and self.device() and not self.wda_ready(timeout=1.5)):
                    return False
                if self.watchdog.admit(time.monotonic()) is not None:
                    return False
                try:
                    self._start_runner()
                finally:
                    self.watchdog.done(True)
            return True
        except Exception:  # noqa: BLE001 -- the next attach (or autostart) tries again
            return False

    def probe(self, now=None):
        """Check that WDA can reach the phone's UI; restart a runner wedged for too long."""
        now = time.monotonic() if now is None else now
        if now - self.health["probed_at"] < PROBE_INTERVAL:
            return
        self.health["probed_at"] = now
        ready = self.wda_ready()
        self.health["ready"] = ready
        if not ready:
            self.health.update(responsive=None, locked=None, since=None)
            # A runner Mobster started, whose process is up and past its start-up grace, that doesn't answer /status
            # is wedged. A runner stopped on purpose, still starting or not ours is not. Its start-up grace counts from
            # its newest launch: one held back while the phone was locked launches long after start_runner.
            started = self.runner_started_at
            launched = getattr(self.runner, "launched_at", None)
            if started is not None and isinstance(launched, (int, float)):
                started = max(started, launched)
            if (self.runner is not None and self._runner_alive() and started is not None
                    and time.monotonic() - started >= STARTUP_GRACE_SECONDS):
                self.watchdog.probe_result(False, now)
            else:
                self.watchdog.reset_failures()
            return
        try:
            locked = self._wda_get("/wda/locked", PROBE_TIMEOUT).get("value")
            self.health.update(responsive=True, locked=locked is True, since=None)
            self.watchdog.probe_result(True, now)
            return
        except Exception:
            since = self.health["since"] or now
            self.health.update(responsive=False, locked=None, since=since)
        if self.watchdog.probe_result(False, now):
            self.health.update(since=None)

    def _restart_wedged_runner(self):
        self.start_runner()  # stops the wedged runner and relay first

    def poll_presence(self):
        """One lazy, read-only poll of whether the owner is using the phone, and the idle release."""
        if not self.wda_ready():
            self.presence.observe(ok=False)
            return
        self.presence_poller.poll()
        if self.presence.should_release():
            for hook in list(self.idle_hooks):
                try:
                    hook()
                except Exception:  # noqa: BLE001
                    pass

    def watch(self, stop):
        next_round = 0.0
        while not stop.is_set():
            try:
                self.probe()
            except Exception:
                pass
            if time.monotonic() >= next_round:
                next_round = time.monotonic() + WATCH_INTERVAL
                self.watch_round()
            stop.wait(PROBE_TICK)

    def watch_round(self):
        """One pass of the watch loop's slower jobs. Each one's failure is its own."""
        for job in (self.autostart, self.rearm_on_attach, self.poll_presence, self.check_exposure,
                    self.retry_locked_build):
            try:
                job()
            except Exception:
                pass
        if self.renewal is not None:
            try:
                self.renewal.tick()
            except Exception:
                pass

    def engine_reading(self):
        from .wireless import transport
        settings = self.settings()
        udid = self.pinned or settings.get("udid")
        state = self.runner_state(ready=self.health["ready"])
        wifi = self.transport == "wifi"
        return {"chosen": bool(udid), "built": self.built(), "expired": self.expired(),
                "exposed": bool(self.exposure), "runner": state["state"], "holding": self.runner_waiting(),
                "hint": self.runner_cause if state["state"] != "running" else None,
                "locked": self.health["locked"], "responsive": self.health["responsive"],
                "usb": None if wifi else self.attached(), "wifi": wifi,
                # The link layer checks every 10 s (wireless.monitor.TICK); a Wi-Fi helper the probe finds silent is
                # named at the probe's pace instead, so the cause is said inside engine_health.NAMED_WITHIN_S.
                "wifi_lost": bool(wifi and (self.health["ready"] is False or udid and transport.lost(udid)))}

    def begin_close(self):
        self.watching.set()
        for service in (self.runner, self.relay):
            signal_stop = getattr(service, "signal_stop", None)
            if callable(signal_stop):
                signal_stop()
        step = self.build_step
        if step is not None:
            try:
                os.killpg(step.pid, signal.SIGTERM)
            except OSError:
                pass

    def close(self):
        self.watching.set()
        step = self.build_step
        if step is not None:
            stop_group(step)
        self.stop_runner()

"""The simulator manager (SPEC §12.1): headless simulators that Mobster creates, boots and keeps warm, each
with its own WebDriverAgent on 127.0.0.1.

`verify` and `mcp` code against the names in this module and nothing else:

    manager = SimulatorManager(progress=print)
    with manager.acquire() as target:          # a SimTarget; the simulator stays up afterwards
        app = manager.install(target, "Build/Products/Debug-iphonesimulator/Daybreak.app")
        manager.reset(target, app.bundle_id, "data")
        manager.launch(target, app.bundle_id, ["-DaybreakSkipOnboarding", "YES"])

Only simulators in the registry (`simulators.json` in `paths.dev_data_dir()`) are ever booted, reset, shut
down or deleted, and never Simulator.app: everything runs through `xcrun simctl` and `xcodebuild`.
"""

from dataclasses import dataclass
import datetime
import os
from pathlib import Path
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlsplit

SIM_KINDS = ("environment", "simulator", "wda", "install", "launch", "busy")


class SimError(RuntimeError):
    """A simulator problem. ``kind`` is one of SIM_KINDS; ``fix`` is one plain sentence (may be "")."""
    def __init__(self, kind: str, message: str, fix: str = ""):
        super().__init__(message)
        self.kind, self.fix = kind, fix


@dataclass(frozen=True)
class AppInfo:
    bundle_id: str
    name: str             # CFBundleDisplayName, else CFBundleName, else the bundle id
    version: str          # "1.0 (1)"
    path: Optional[str]   # the .app, or None for an app looked up by bundle id


@dataclass(frozen=True)
class SimTarget:
    udid: str
    name: str             # "Mobster · iPhone 17 Pro · iOS 26.4"
    device_type: str      # "iPhone 17 Pro"
    runtime: str          # "iOS 26.4"
    wda_url: str          # "http://127.0.0.1:8310"
    mjpeg_url: str        # "http://127.0.0.1:9310"
    xctestrun: str        # the built runner plan (for the restarter)


class SimLease:
    """Holds one simulator's device lease. ``release()`` is idempotent. Also a context manager yielding ``target``.

    ``timing`` holds the seconds each phase of the acquire took: create, boot, wda_build, wda_start, total."""
    target: SimTarget

    def __init__(self, target: SimTarget, leases=(), timing: Optional[dict] = None):
        self.target = target
        self.timing = dict(timing or {})
        self._leases = list(leases)
        self._lock = threading.Lock()

    @property
    def released(self):
        return not self._leases

    def release(self) -> None:
        with self._lock:
            leases, self._leases = self._leases, []
        for lease in leases:
            try:
                lease.close()
            except OSError:
                pass

    def __enter__(self) -> SimTarget:
        return self.target

    def __exit__(self, *exc) -> None:
        self.release()


from . import apps, simctl  # noqa: E402  (after the types: the helpers raise SimError through simctl.sim_error)
from .registry import Registry, free_slot, now, port_base, taken_ports  # noqa: E402
from .wda import (MOVE, RESTART, REUSE, SimRestarter, Tools, WebDriverAgent, build_dir, find_xctestrun,  # noqa: E402
                  heartbeat)

# The settings _configure writes after a boot, and again whenever the runner (re)starts.
KEYBOARD_DEFAULTS = ("KeyboardAutocorrection", "KeyboardPrediction", "SmartQuotesEnabled", "SmartDashesEnabled")
KEYBOARD_PREFERENCES = "com.apple.keyboard.preferences"
KEYBOARD_INTRO = (("DidShowContinuousPathIntroduction", "true"), ("DidShowGestureKeyboardIntroduction", "true"),
                  ("KeyboardDidShowProductivityTutorial", "true"), ("KeyboardContinuousPathEnabled", "false"))
STATUS_BAR = ("--time", "9:41", "--dataNetwork", "wifi", "--wifiBars", "3", "--cellularBars", "4",
              "--batteryState", "charged", "--batteryLevel", "100")
BOOT_TIMEOUT = 240
OPEN_PROMPT = "Open in"      # iOS 26's "Open in “<App>”?" after `simctl openurl` with a custom scheme
OPEN_BUTTON = "Open"
# iOS remembers an Open in com.apple.launchservices.schemeapproval as "<opener>--><scheme>" = <bundle id>, and
# `simctl openurl` opens links as CoreSimulatorBridge. Writing that answer when Mobster installs an app means
# its own links never stop at the prompt (measured on iOS 26.4, 28 Sep).
SCHEME_APPROVALS = "com.apple.launchservices.schemeapproval"
URL_OPENER = "com.apple.CoreSimulator.CoreSimulatorBridge"
OPEN_PROMPT_WAIT = 3.0       # how long open_url looks for that prompt, in reads the runner answered
OPEN_PROMPT_LIMIT = 30.0     # and at most, when the runner is slow to answer
ALERT_GONE_WAIT = 2.0        # how long an answered alert has to leave the screen
ALERT_POLL = 0.2
SWEEP_ROUNDS = 3             # alerts dismissed in a row before a simulator is handed out
DEFAULT_MAX_SIMS = 2
PRUNE_DAYS = 14
BUSY_POLL = 1.0
PROBE_RETRY = 0.2            # before creating another simulator: a list() probe holds a lease for milliseconds
LOOPBACK = "127.0.0.1"
XCRUN = "/usr/bin/xcrun"


def _device_lease(wda_url):
    from ..journal import Lease
    return Lease.device(wda_url)


def _url(port):
    return f"http://{LOOPBACK}:{int(port)}"


def _port(url):
    return urlsplit(url).port


def _parse_time(text):
    try:
        return datetime.datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


class SimulatorManager:
    """Creates, boots and keeps Mobster's simulators, and runs the app under test on them.

    Everything that touches the machine goes through a few attributes that tests replace: ``run`` (a
    command runner returning simctl.Result), ``wda`` (the WebDriverAgent service), ``lease`` (the device
    lease factory), ``sleep`` and ``clock``."""

    def __init__(self, data_dir: Optional[Path] = None, progress: Optional[Callable[[str], None]] = None):
        from ..paths import dev_data_dir
        self.data_dir = Path(data_dir).expanduser() if data_dir else dev_data_dir()
        self.progress = progress or (lambda message: None)
        self.registry = Registry(self.data_dir)
        self.run = simctl.run
        self.wda = WebDriverAgent(self.data_dir, log=self._log, progress=self._say)
        self.lease = _device_lease
        self.sleep = time.sleep
        self.clock = time.monotonic
        self._tools = None
        self._approved = {}   # udid -> {scheme}: links this manager knows open without the prompt

    # -- plumbing ------------------------------------------------------------------------------------------

    def _say(self, message):
        self._log(message)
        try:
            self.progress(message)
        except Exception:  # a progress sink must never break a run
            pass

    def _log(self, message):
        try:
            folder = self.data_dir / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            with open(folder / "sim.log", "a", encoding="utf-8") as stream:
                stream.write(f"{now()} {message}\n")
        except OSError:
            pass

    @property
    def max_sims(self):
        text = str(os.environ.get("MOBSTER_MAX_SIMS") or "").strip()
        if not text:
            return DEFAULT_MAX_SIMS
        try:
            value = int(text)
        except ValueError:
            value = 0
        if value < 1:
            raise SimError("environment", f"MOBSTER_MAX_SIMS is {text!r}; it must be a whole number of 1 or more.",
                           "Unset it to allow 2 simulators per device type and runtime.")
        return value

    def tools(self):
        """Xcode's tools, read once per manager. SimError("environment") without a usable Xcode."""
        if self._tools is not None:
            return self._tools
        from .. import device_manager
        xcode, selected = device_manager.developer_dir()
        if not xcode:
            raise SimError("environment", "Xcode isn't installed. Mobster runs simulators with it.",
                           "Install Xcode from the App Store, open it once, then run `mobster sim doctor`.")
        env = dict(os.environ)
        if selected != xcode:
            env["DEVELOPER_DIR"] = xcode  # the Command Line Tools are selected: use the Xcode found anyway
        version, build = xcode_version(xcode)
        xcodebuild = str(Path(xcode) / "usr" / "bin" / "xcodebuild")
        if not build:
            result = self.run([xcodebuild, "-version"], timeout=60, env=env)
            version, build = parse_xcode_version(result.out)
            if not result.ok or not build:
                text = result.out + result.err
                if "license" in text.lower():
                    raise SimError("environment", "Xcode's license isn't accepted yet.",
                                   "Run `sudo xcodebuild -license accept` in Terminal.")
                raise SimError("environment", f"xcodebuild didn't report its version: {result.last_line()}",
                               "Open Xcode once to finish its setup, then run `mobster sim doctor`.")
        git = device_manager.xcode_tool("git", developer=xcode) or ""
        self._tools = Tools(xcodebuild=xcodebuild, git=git, version=version, build=build, env=env)
        return self._tools

    def simctl(self, *args, timeout=60, env=None):
        result = self.run([XCRUN, "simctl", *map(str, args)], timeout=timeout, env=env or self.tools().env)
        if not result.ok:
            self._log(f"simctl {' '.join(map(str, args[:3]))}: exit {result.code}: {result.last_line()}")
        return result

    def _json(self, *args):
        import json
        result = self.simctl("list", *args, "-j", timeout=60)
        try:
            if not result.ok:
                raise ValueError
            return json.loads(result.out)
        except ValueError:
            raise SimError("environment", f"simctl couldn't list simulators: {result.last_line() or 'no output'}",
                           "Open Xcode once so it finishes installing its components, then run "
                           "`mobster sim doctor`.") from None

    def _catalog(self):
        data = self._json()
        return simctl.parse_runtimes(data), simctl.parse_device_types(data), simctl.parse_devices(data)

    def _devices(self):
        return simctl.parse_devices(self._json("devices"))

    def _choose(self, device=None, runtime=None, catalog=None):
        runtimes, types, devices = catalog or self._catalog()
        chosen_runtime = simctl.choose_runtime(runtimes, runtime)
        return simctl.choose_device_type(types, chosen_runtime, device), chosen_runtime, devices

    def _try_lease(self, wda_url):
        """The device lease for this WDA address, or None while another process holds it."""
        from ..journal import JournalError, LeaseHeld
        try:
            return self.lease(wda_url)
        except LeaseHeld:
            return None
        except JournalError as exc:
            raise SimError("simulator", f"Mobster couldn't take the simulator's lock: {exc}",
                           "Check that ~/Library/Application Support/app.mobster.desktop/device-leases belongs "
                           "to you and is private.") from None

    # -- acquire -------------------------------------------------------------------------------------------

    def acquire(self, device: Optional[str] = None, runtime: Optional[str] = None, *,
                timeout: float = 600) -> SimLease:
        """A booted Mobster simulator for this device type and runtime with WebDriverAgent ready, created
        the first time, and no system alert on screen. It stays booted, with its runner up, after the lease is
        released.

        Progress goes to ``progress`` in plain sentences, some of them from the thread that builds
        WebDriverAgent while the simulator boots, so the callback must be thread-safe. SimError("busy")
        after ``timeout`` seconds when every simulator for the pair is running a check."""
        started = self.clock()
        timing = {}
        tools = self.tools()
        deadline, waiting = started + max(0.0, float(timeout)), False
        while True:
            device_type, chosen_runtime, entry, state, lease = self._claim(device, runtime, timing)
            if lease is not None:
                break
            if self.clock() >= deadline:
                raise SimError("busy", f"All {self.max_sims} Mobster simulators for {device_type.name} with "
                                       f"{chosen_runtime.name} are running checks.",
                               "Wait for one to finish, or set MOBSTER_MAX_SIMS higher.")
            if not waiting:
                self._say(f"Waiting for a free simulator: all {self.max_sims} are running checks")
                waiting = True
            self.sleep(BUSY_POLL)
        held = [lease]
        try:
            target = self._make_ready(entry, state, held, tools, timing)
            self._sweep_alerts(target)
        except BaseException:
            for item in held:
                item.close()
            raise
        timing["total"] = round(self.clock() - started, 2)
        self._log(f"acquired {target.name} ({target.udid}) on {target.wda_url}: {timing}")
        return SimLease(target, held, timing)

    def acquire_udid(self, udid: str, *, timeout: float = 600) -> SimLease:
        """This one Mobster simulator, booted with WebDriverAgent ready, as acquire() hands out simulators: for a
        device named by UDID (`--device`, MCP's device, the server's device list). SimError("simulator") for a
        UDID not in the registry; SimError("busy") after ``timeout`` seconds while another process holds it."""
        started = self.clock()
        timing = {}
        tools = self.tools()
        deadline, waiting = started + max(0.0, float(timeout)), False
        while True:
            with self.registry.edit() as entries:
                entry = self._entry(udid, entries)
                present = {item.udid: item for item in self._devices()}
                if entry["udid"] not in present:
                    raise SimError("simulator", f"{entry['name']} no longer exists.", "Run `mobster sim prune`.")
                lease = self._try_lease(_url(entry["wda_port"]))
                if lease is not None:
                    entry["last_used_at"] = now()
                    entry, state = dict(entry), present[entry["udid"]].state
                    break
            if self.clock() >= deadline:
                raise SimError("busy", f"{entry['name']} is running a check.",
                               "Wait for it to finish, or stop it, then try again.")
            if not waiting:
                self._say(f"Waiting for {entry['name']}: it is running a check")
                waiting = True
            self.sleep(BUSY_POLL)
        held = [lease]
        try:
            target = self._make_ready(entry, state, held, tools, timing)
            self._sweep_alerts(target)
        except BaseException:
            for item in held:
                item.close()
            raise
        timing["total"] = round(self.clock() - started, 2)
        self._log(f"acquired {target.name} ({target.udid}) on {target.wda_url} by UDID: {timing}")
        return SimLease(target, held, timing)

    def _claim(self, device, runtime, timing):
        """Under the registry lock: the first free simulator for the pair, else a new one while fewer than
        MOBSTER_MAX_SIMS exist. Returns (device type, runtime, entry, state, lease); entry, state and lease are
        None when all are busy.

        simctl's list is taken under the lock too. Another process creates and registers its simulator under
        this lock, so a list taken before it can miss one registered since: the entry would be dropped as gone,
        its name given out again, and its simulator left booted in no list (review round 2, 28 Sep)."""
        with self.registry.edit() as entries:
            device_type, runtime, devices = self._choose(device, runtime)
            present = {item.udid: item for item in devices}
            for entry in [entry for entry in entries if entry["udid"] not in present]:
                entries.remove(entry)
                self._log(f"forgot {entry['udid']} ({entry['name']}): simctl no longer lists it")
            pair = [entry for entry in entries
                    if entry["device_type"] == device_type.name and entry["runtime"] == runtime.name]
            pair.sort(key=lambda entry: entry.get("last_used_at") or "", reverse=True)
            pair.sort(key=lambda entry: present[entry["udid"]].state != "Booted")
            entry, lease = self._first_free(pair)
            if lease is None and pair and len(pair) < self.max_sims:
                # Every lease was held, and a create (a cold boot, and 1.6 GB of disk) is next. `list()` and
                # doctor take and release each lease for milliseconds to see whether it is in use, while a run
                # holds its lease for the whole check. So look once more first (review round 3).
                self.sleep(PROBE_RETRY)
                entry, lease = self._first_free(pair)
            if lease is not None:
                entry["last_used_at"] = now()
                return device_type, runtime, dict(entry), present[entry["udid"]].state, lease
            if len(pair) >= self.max_sims:
                return device_type, runtime, None, None, None
            entry, lease = self._create(device_type, runtime, devices, entries, timing)
            return device_type, runtime, dict(entry), "Shutdown", lease

    def _first_free(self, entries):
        """(entry, lease) for the first of ``entries`` whose lease is free, else (None, None)."""
        for entry in entries:
            lease = self._try_lease(_url(entry["wda_port"]))
            if lease is not None:
                return entry, lease
        return None, None

    def _create(self, device_type, runtime, devices, entries, timing):
        name = simctl.sim_name(device_type.name, runtime.name,
                               {item.name for item in devices} | {entry["name"] for entry in entries})
        self._say(f"Creating simulator {name}")
        started = self.clock()
        result = self.simctl("create", name, device_type.identifier, runtime.identifier, timeout=120)
        udid = simctl.parse_created(result.out) if result.ok else None
        if not udid:
            raise SimError("simulator", f"simctl couldn't create {name}: {result.last_line() or 'no UDID'}",
                           "Run `mobster sim doctor`, then try again.")
        timing["create"] = round(self.clock() - started, 2)
        try:
            taken = taken_ports(entries)
            for _ in range(40):
                wda_port, mjpeg_port = free_slot(port_base(), taken, busy=self.wda.listening)
                lease = self._try_lease(_url(wda_port))
                if lease is not None:
                    break
                taken |= {wda_port, mjpeg_port}
            else:
                raise SimError("busy", "Every free WebDriverAgent port is locked by another Mobster process.",
                               "Wait for the other runs to finish, then try again.")
        except BaseException:
            self.simctl("delete", udid, timeout=60)  # just created, never registered: nothing else knows it
            raise
        stamp = now()
        entry = {"udid": udid, "name": name, "device_type": device_type.name, "runtime": runtime.name,
                 "wda_port": wda_port, "mjpeg_port": mjpeg_port, "created_at": stamp, "last_used_at": stamp}
        entries.append(entry)
        self._log(f"created {name} ({udid}) on ports {wda_port}/{mjpeg_port}")
        return entry, lease

    def _make_ready(self, entry, state, held, tools, timing):
        udid, name = entry["udid"], entry["name"]
        folder = apps.device_folder(udid)
        if not folder.is_dir():
            raise SimError("simulator", f"{name}'s folder {folder} is missing, so Mobster can't keep its apps "
                                        "apart from other simulators'.",
                           f"Delete it with `mobster sim delete {udid}`, then try again.")
        build = {}
        builder = None
        if find_xctestrun(build_dir(self.data_dir, tools.build)) is None:
            def build_runner():
                began = self.clock()
                try:
                    build["plan"] = self.wda.ensure_build(tools)
                    build["seconds"] = round(self.clock() - began, 2)
                except BaseException as exc:  # handed to the waiting thread below
                    build["error"] = exc
            # The build (once per Xcode version) runs while the simulator boots.
            builder = threading.Thread(target=build_runner, name="mobster-wda-build", daemon=True)
            builder.start()
        configurer = []

        def configure_once():
            """The status bar and keyboard settings (2–3 s), set while WebDriverAgent starts: after a boot, and
            whenever the runner (re)starts, so a simulator whose first boot timed out still gets them."""
            if configurer:
                return

            def configure():
                began = self.clock()
                try:
                    self._configure(udid)
                except Exception as exc:  # the run can go ahead; the log says what was skipped
                    self._log(f"settings for {udid} failed: {type(exc).__name__}: {exc}")
                    return
                timing["configure"] = round(self.clock() - began, 2)
            configurer.append(threading.Thread(target=configure, name="mobster-sim-configure", daemon=True))
            configurer[0].start()
        try:
            if state != "Booted":
                started = self.clock()
                self._say(f"Booting {name}")
                with heartbeat(self._say, f"booting {name}"):
                    self._boot(udid, name, state)
                timing["boot"] = round(self.clock() - started, 2)
                self._say(f"Simulator: {name} (booted in {timing['boot']:.1f} s)")
                configure_once()
            else:
                self._say(f"Simulator: {name}")
            if builder is not None:
                builder.join()
                if "error" in build:
                    raise build["error"]
                xctestrun = build["plan"]
                timing["wda_build"] = build["seconds"]
            else:
                xctestrun = self.wda.ensure_build(tools)
            entry = self._runner(entry, held, xctestrun, tools, timing, configure_once)
        except BaseException:
            if builder is not None:  # never leave xcodebuild behind a failed acquire (no-op once it finished)
                self.wda.cancel()
                builder.join(timeout=10)
            raise
        finally:
            for thread in configurer:
                thread.join()
        return SimTarget(udid=udid, name=name, device_type=entry["device_type"], runtime=entry["runtime"],
                         wda_url=_url(entry["wda_port"]), mjpeg_url=_url(entry["mjpeg_port"]),
                         xctestrun=str(xctestrun))

    def _boot(self, udid, name, state):
        """`simctl boot`, then `bootstatus -b` (240 s). On "Unable to boot in current state", shut it down and
        try once more. Headless: Simulator.app is never opened. Running out of time is not a failure of the
        simulator (it keeps booting, and the next acquire waits for it), so only a real `simctl` failure
        suggests erasing it."""
        if state != "Booting":
            result = self.simctl("boot", udid, timeout=BOOT_TIMEOUT)
            text = result.out + result.err
            if not result.ok and "current state: Booted" not in text:
                if result.timed_out:
                    raise self._still_booting(name)
                if "Unable to boot" not in text:
                    raise self._boot_failed(udid, name, result)
                self.simctl("shutdown", udid, timeout=60)
                self.sleep(1)
                result = self.simctl("boot", udid, timeout=BOOT_TIMEOUT)
                if not result.ok and "current state: Booted" not in result.out + result.err:
                    raise self._still_booting(name) if result.timed_out else self._boot_failed(udid, name, result)
        result = self.simctl("bootstatus", udid, "-b", timeout=BOOT_TIMEOUT)
        if not result.ok:
            if result.timed_out:
                raise self._still_booting(name)
            raise SimError("simulator", f"{name} didn't finish booting: {result.last_line() or 'no answer'}",
                           f"Erase it with `mobster sim erase {udid}`, then try again.")

    @staticmethod
    def _still_booting(name):
        return SimError("simulator", f"{name} is still booting after {BOOT_TIMEOUT} s, which happens when this Mac "
                                     "is busy.", "Run the command again; the simulator keeps booting.")

    @staticmethod
    def _boot_failed(udid, name, result):
        return SimError("simulator", f"{name} didn't boot: {result.last_line()}",
                        f"Erase it with `mobster sim erase {udid}`, then try again.")

    def _configure(self, udid):
        """A fixed status bar for stable frames, and a keyboard that types exactly what it is given: no
        autocorrection, prediction, smart quotes or smart dashes, and no first-keyboard introduction to slide
        typing, whose Continue button no app's tree shows. The nine calls run at once."""
        calls = [("status_bar", udid, "override", *STATUS_BAR)]
        calls += [("spawn", udid, "defaults", "write", "com.apple.Preferences", key, "-bool", "false")
                  for key in KEYBOARD_DEFAULTS]
        calls += [("spawn", udid, "defaults", "write", KEYBOARD_PREFERENCES, key, "-bool", value)
                  for key, value in KEYBOARD_INTRO]
        self._simctl_all(calls)

    def _simctl_all(self, calls, timeout=30):
        """Independent simctl calls at the same time (each takes 0.2–0.5 s, mostly waiting on CoreSimulator).
        Returns their results in order."""
        if len(calls) == 1:
            return [self.simctl(*calls[0], timeout=timeout)]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(calls), thread_name_prefix="mobster-simctl") as pool:
            return list(pool.map(lambda call: self.simctl(*call, timeout=timeout), calls))

    def _runner(self, entry, held, xctestrun, tools, timing, on_start=None):
        """Reuse this simulator's runner, or (re)start it, moving to a free port pair when another program
        holds the entry's. ``on_start`` runs before a (re)start. Returns the entry as it now stands."""
        udid, name = entry["udid"], entry["name"]
        port, mjpeg = entry["wda_port"], entry["mjpeg_port"]
        plan = self.wda.plan(udid, port, mjpeg, xctestrun)
        self._log(f"runner plan for {udid} on {port}: {plan}")
        if plan == REUSE:
            return entry
        if on_start is not None:
            on_start()
        if plan == MOVE:
            entry = self._move(entry, held)
            self._say(f"Port {port} is in use by another program, so {name}'s WebDriverAgent moves to "
                      f"{entry['wda_port']}")
            port, mjpeg = entry["wda_port"], entry["mjpeg_port"]
            self.wda.stop(udid)
        elif plan == RESTART:
            self.wda.stop(udid, port, mjpeg, xctestrun)
        self._say(f"Starting WebDriverAgent on {LOOPBACK}:{port}")
        started = self.clock()
        process = self.wda.start(udid, port, mjpeg, xctestrun, tools)
        try:
            self.wda.wait_ready(udid, port, process)
            ok, found = self.wda.check_loopback(port, mjpeg)
            if not ok:
                seen = "; ".join(f"{number}: " + (", ".join(f"{item.host}:{item.port}" for item in items) or
                                                  "nothing")
                                 for number, items in found.items())
                raise SimError("wda", "WebDriverAgent listened beyond this Mac",
                               f"lsof showed {seen}. Delete {build_dir(self.data_dir, tools.build)} and try again.")
        except BaseException:
            self.wda.stop(udid, port, mjpeg, xctestrun)
            raise
        timing["wda_start"] = round(self.clock() - started, 2)
        self._say(f"WebDriverAgent ready on {LOOPBACK}:{port} in {timing['wda_start']:.1f} s")
        return entry

    def _move(self, entry, held):
        """Give the entry the next free port pair, holding that address's lease too."""
        with self.registry.edit() as entries:
            taken = taken_ports(entries) | {entry["wda_port"], entry["mjpeg_port"]}
            for _ in range(40):
                wda_port, mjpeg_port = free_slot(port_base(), taken, busy=self.wda.listening)
                lease = self._try_lease(_url(wda_port))
                if lease is not None:
                    break
                taken |= {wda_port, mjpeg_port}
            else:
                raise SimError("busy", "Every free WebDriverAgent port is locked by another Mobster process.",
                               "Wait for the other runs to finish, then try again.")
            held.append(lease)
            for item in entries:
                if item["udid"] == entry["udid"]:
                    item["wda_port"], item["mjpeg_port"] = wda_port, mjpeg_port
            return {**entry, "wda_port": wda_port, "mjpeg_port": mjpeg_port}

    # -- apps ----------------------------------------------------------------------------------------------

    def app_info(self, app_path: str) -> AppInfo:
        """The .app's identity from its Info.plist. SimError("install") for a device build or a non-app."""
        info = apps.read_info(app_path)
        bundle = str(info.get("CFBundleIdentifier") or "").strip()
        if not bundle:
            raise SimError("install", f"{app_path} has no CFBundleIdentifier in its Info.plist.",
                           "Build the app again.")
        if not apps.built_for_simulator(info):
            raise apps.device_build_error(app_path)
        return AppInfo(bundle_id=bundle, name=apps.display_name(info, bundle), version=apps.version_text(info),
                       path=str(Path(app_path).expanduser().resolve()))

    def installed_app(self, target: SimTarget, bundle_id: str) -> AppInfo:
        result = self.simctl("get_app_container", target.udid, bundle_id, "app", timeout=30)
        path = result.out.strip() if result.ok else ""
        if not path:
            raise SimError("install", f"{bundle_id} isn't installed on the simulator. Pass --app.",
                           "Pass the .app built for the iOS Simulator with --app.")
        info = apps.read_info(path)
        return AppInfo(bundle_id=bundle_id, name=apps.display_name(info, bundle_id),
                       version=apps.version_text(info), path=None)

    def install(self, target: SimTarget, app_path: str) -> AppInfo:
        """`simctl install`, with the answer Open already given for every URL scheme the app declares (written
        at the same time, so it adds no time), so `open_url` into it never stops at iOS's prompt."""
        info = self.app_info(app_path)
        # Each scheme as declared and in lowercase, since a link's scheme matches without case.
        schemes = list(dict.fromkeys(form for scheme in apps.url_schemes(apps.read_info(info.path))
                                     for form in (scheme, scheme.lower())))
        started = self.clock()
        approvals = [("spawn", target.udid, "defaults", "write", SCHEME_APPROVALS, f"{URL_OPENER}-->{scheme}",
                      "-string", info.bundle_id) for scheme in schemes]
        result, *approved = self._simctl_all([("install", target.udid, info.path)] + approvals, timeout=300)
        if not result.ok:
            raise SimError("install", f"{info.name} didn't install on {target.name}: "
                                      f"{result.last_line() or 'simctl failed'}",
                           "Build it again for the iOS Simulator, then try again.")
        known = {scheme for scheme, done in zip(schemes, approved) if done.ok and scheme == scheme.lower()}
        self._approved.setdefault(target.udid, set()).update(known)
        self._log(f"installed {info.bundle_id} {info.version} on {target.udid} in {self.clock() - started:.2f} s"
                  + (f"; links open without the prompt: {', '.join(sorted(known))}" if known else ""))
        return info

    def reset(self, target: SimTarget, bundle_id: str, level: str, app_path: Optional[str] = None) -> None:
        """none: nothing. data: the app's preferences, containers, privacy grants and the keychain.
        reinstall: uninstall, install ``app_path``, then privacy and the keychain. System apps are never reset.
        Every level then dismisses a system alert left on screen (one WebDriverAgent read when there is none)."""
        if level not in ("none", "data", "reinstall"):
            raise ValueError(f"reset level must be none, data or reinstall, not {level!r}")
        if level != "none" and not bundle_id.startswith("com.apple."):
            started = self.clock()
            if level == "data":
                self._reset_data(target, bundle_id)
            else:
                self._reinstall(target, bundle_id, app_path)
            self._log(f"reset {bundle_id} ({level}) on {target.udid} in {self.clock() - started:.2f} s")
        # After the app is gone, never alongside: WebDriverAgent reads the app in front, and reading one that
        # is being terminated or uninstalled held a reinstall for 10 s (smoke, 28 Sep).
        self._sweep_alerts(target)

    def _reset_data(self, target, bundle_id):
        """Terminate the app, delete its preferences, empty Documents, Library and tmp of its data and
        app-group containers, then reset its privacy grants and the keychain. Independent simctl calls run
        together: two rounds instead of seven calls in a row."""
        udid = target.udid
        result, grouped, _ = self._simctl_all([("get_app_container", udid, bundle_id, "data"),
                                               ("get_app_container", udid, bundle_id, "groups"),
                                               ("terminate", udid, bundle_id)])
        data = result.out.strip() if result.ok else ""
        if data in ("", "(null)"):
            raise SimError("install", f"{bundle_id} isn't installed on the simulator. Pass --app.",
                           "Pass the .app built for the iOS Simulator with --app.")
        groups = simctl.parse_groups(grouped.out if grouped.ok else "")
        containers = [data] + [path for _, path in groups]
        for container in containers:  # every path is checked before anything changes
            apps.guard_container(container, udid)
        # cfprefsd caches preferences: deleting the plist alone would leave them alive until it restarts.
        calls = [("spawn", udid, "defaults", "delete", domain)
                 for domain in [bundle_id] + [group for group, _ in groups if group]]
        calls += [("privacy", udid, "reset", "all", bundle_id), ("keychain", udid, "reset")]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mobster-reset") as pool:
            pending = pool.submit(self._simctl_all, calls)
            for container in containers:
                apps.empty_container(container, udid)
            pending.result()

    def _reinstall(self, target, bundle_id, app_path):
        if not app_path:
            raise SimError("install", f"Reinstalling {bundle_id} needs its .app.",
                           "Pass the .app built for the iOS Simulator with --app.")
        info = self.app_info(app_path)  # a device build fails here, before the installed app is removed
        for bundle in dict.fromkeys((bundle_id, info.bundle_id)):
            self.simctl("uninstall", target.udid, bundle, timeout=120)
        self.install(target, app_path)
        self._privacy_and_keychain(target.udid, info.bundle_id)

    def _privacy_and_keychain(self, udid, bundle_id):
        # The keychain reset covers the whole simulator, which is fine: the simulator is Mobster's own.
        self._simctl_all([("privacy", udid, "reset", "all", bundle_id), ("keychain", udid, "reset")])

    def launch(self, target: SimTarget, bundle_id: str, args=(), env: Optional[dict] = None) -> None:
        tools = self.tools()
        child = dict(tools.env)
        for key, value in (env or {}).items():
            child[f"SIMCTL_CHILD_{key}"] = str(value)
        result = self.run([XCRUN, "simctl", "launch", "--terminate-running-process", target.udid, bundle_id,
                           *map(str, args)], timeout=60, env=child)
        if not result.ok:
            raise SimError("launch", f"{bundle_id} didn't launch: {result.last_line() or 'simctl failed'}",
                           "Check that the app is installed and starts in the iOS Simulator.")

    def terminate(self, target: SimTarget, bundle_id: str) -> None:
        self.simctl("terminate", target.udid, bundle_id, timeout=30)  # "not running" is fine

    def open_url(self, target: SimTarget, url: str) -> None:
        """`simctl openurl`. iOS 26 stops a link at "Open in “<App>”?" until someone answers Open once for that
        scheme. A scheme already answered (by `install`, or earlier) opens straight away. Otherwise, SPEC §6.4:
        WebDriverAgent's sessionless /alert/text is read for up to 3 s, and a prompt starting "Open in" gets its
        Open button, then must leave the screen. Any other alert is left for the run. (A run with a WDA session
        can open links through the session's /url instead, which shows no prompt.)"""
        scheme = urlsplit(url).scheme.lower()
        approved = self._link_approved(target.udid, scheme)
        result = self.simctl("openurl", target.udid, url, timeout=60)
        if not result.ok:
            raise SimError("launch", f"{url} didn't open: {result.last_line() or 'simctl failed'}",
                           "Check that the app declares the URL scheme in its Info.plist.")
        if approved:
            return
        wda_url, started = target.wda_url, self.clock()
        answered, text = self.wda.read_alert(wda_url)
        # A read the runner didn't answer (it can take 10 s on a loaded Mac) doesn't end the wait early.
        while text is None and (self.clock() < started + OPEN_PROMPT_WAIT or not answered) \
                and self.clock() < started + OPEN_PROMPT_LIMIT:
            self.sleep(ALERT_POLL)
            answered, text = self.wda.read_alert(wda_url)
        if text is None:
            return
        if not text.startswith(OPEN_PROMPT):
            self._log(f"open_url {url}: left the alert {text!r} for the run")
            return
        for _ in range(2):
            self.wda.alert_action(wda_url, "accept", OPEN_BUTTON)
            if self._alert_after(wda_url, text) != text:
                self._log(f"open_url {url}: pressed {OPEN_BUTTON} on {text!r}")
                if scheme:
                    self._approved.setdefault(target.udid, set()).add(scheme)  # iOS remembers it too
                return
        raise SimError("launch", f"{url} didn't open: iOS's “{text}” prompt stayed up after Mobster pressed "
                                 f"{OPEN_BUTTON}.", "Run the check again.")

    def _link_approved(self, udid, scheme):
        """True when iOS already has Open as the answer for links of this scheme opened by `simctl openurl`."""
        if not scheme:
            return False
        if scheme in self._approved.get(udid, ()):
            return True
        return bool(apps.scheme_approvals(udid).get(f"{URL_OPENER}-->{scheme}"))

    def _alert_after(self, wda_url, text):
        """What is on screen once the alert showing ``text`` has left it: None, or the next alert's text. Returns
        ``text`` when that alert is still up after ALERT_GONE_WAIT."""
        end = self.clock() + ALERT_GONE_WAIT
        while True:
            seen = self.wda.alert_text(wda_url)
            if seen != text or self.clock() >= end:
                return seen
            self.sleep(ALERT_POLL)

    def _sweep_alerts(self, target):
        """Dismiss a system alert an earlier run left on screen, such as an unanswered "Open in …" prompt, and
        any behind it, so the next run starts on the app. One WebDriverAgent read when there is none.
        SimError("simulator") when one stays after three tries."""
        wda_url = target.wda_url
        text = self.wda.alert_text(wda_url)
        for _ in range(SWEEP_ROUNDS):
            if text is None:
                return
            self._say(f"Dismissing an alert left on {target.name}: “{text}”")
            self.wda.alert_action(wda_url, "dismiss")
            text = self._alert_after(wda_url, text)
        if text is not None:
            raise SimError("simulator", f"An alert stays on {target.name}'s screen: “{text}”.",
                           f"Run the command again; if it stays, erase the simulator with "
                           f"`mobster sim erase {target.udid}`.")

    def screenshot(self, target: SimTarget, path, *, max_width: Optional[int] = None, quality: int = 75) -> Path:
        """A JPEG of the screen through `simctl io` (never through WDA), scaled down to ``max_width``."""
        from PIL import Image
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = path.with_name(f".{path.stem}.{os.getpid()}.{threading.get_ident()}.png")
        try:
            result = self.simctl("io", target.udid, "screenshot", "--type=png", str(raw), timeout=30)
            if not result.ok or not raw.is_file():
                raise SimError("simulator", f"The screenshot failed: {result.last_line() or 'no image'}",
                               "Check that the simulator is booted (`mobster sim list`).")
            with Image.open(raw) as image:
                image = image.convert("RGB")
                if max_width and image.width > max_width:
                    height = max(1, round(image.height * max_width / image.width))
                    image = image.resize((int(max_width), height), Image.LANCZOS)
                image.save(path, "JPEG", quality=int(quality), optimize=True)
        finally:
            try:
                raw.unlink()
            except OSError:
                pass
        return path

    def restarter(self, target: SimTarget) -> Callable:
        """bench.sim_wda.SimulatorWDA for the Smart loop's WebDriverAgent recovery (a subclass that starts the
        runner with Mobster's Xcode and on 127.0.0.1)."""
        return SimRestarter(target.udid, _port(target.wda_url), _port(target.mjpeg_url), target.xctestrun,
                            self.tools(), log_path=str(self.wda.runner_log(target.udid)))

    # -- housekeeping --------------------------------------------------------------------------------------

    def _in_use(self, entry):
        lease = self._try_lease(_url(entry["wda_port"]))
        if lease is None:
            return True
        lease.close()
        return False

    def list(self) -> list:
        entries = self.registry.read()
        if not entries:
            return []
        present = {item.udid: item for item in self._devices()}
        rows = []
        for entry in entries:
            device = present.get(entry["udid"])
            state = device.state if device else "Missing"
            url = _url(entry["wda_port"])
            rows.append({"udid": entry["udid"], "name": entry["name"], "device_type": entry["device_type"],
                         "runtime": entry["runtime"], "state": state,
                         "wda": "ready" if state == "Booted" and self.wda.ready(url) else "stopped",
                         "wda_url": url, "mjpeg_url": _url(entry["mjpeg_port"]),
                         "in_use": self._in_use(entry) if device else False,
                         "last_used_at": entry.get("last_used_at")})
        return rows

    def _entry(self, udid, entries=None):
        entries = self.registry.read() if entries is None else entries
        entry = next((entry for entry in entries if entry["udid"].upper() == str(udid).strip().upper()), None)
        if entry is None:
            raise SimError("simulator", f"{udid} isn't one of Mobster's simulators, so Mobster won't change it.",
                           "See Mobster's simulators with `mobster sim list`.")
        return entry

    def _stop_one(self, entry, state):
        """Stop the runner and shut the simulator down (its lease held by the caller)."""
        udid = entry["udid"]
        plan = find_xctestrun(build_dir(self.data_dir, self.tools().build))
        self.wda.stop(udid, entry["wda_port"], entry["mjpeg_port"], plan)
        if state not in ("Shutdown", "Missing"):
            result = self.simctl("shutdown", udid, timeout=120)
            if not result.ok and "current state: Shutdown" not in result.out + result.err:
                raise SimError("simulator", f"{entry['name']} didn't shut down: {result.last_line()}",
                               "Try again in a moment.")

    def _with_lease(self, entry, action, strict):
        lease = self._try_lease(_url(entry["wda_port"]))
        if lease is None:
            if strict:
                raise SimError("busy", f"{entry['name']} is running a check.",
                               "Wait for it to finish, or stop it, then try again.")
            self._say(f"Skipped {entry['name']}: it is running a check")
            return False
        try:
            action()
        finally:
            lease.close()
        return True

    def shutdown(self, udid: Optional[str] = None) -> None:
        """Stop WebDriverAgent and shut down one Mobster simulator, or every one that isn't running a check."""
        self.tools()
        entries = [self._entry(udid)] if udid else self.registry.read()
        states = {item.udid: item.state for item in self._devices()} if entries else {}
        for entry in entries:
            state = states.get(entry["udid"], "Missing")
            if self._with_lease(entry, lambda entry=entry, state=state: self._stop_one(entry, state), bool(udid)):
                if state not in ("Shutdown", "Missing"):
                    self._say(f"Shut down {entry['name']}")

    def _guard_owned(self, entry, devices):
        """A simulator Mobster may erase or delete: in the registry, and named “Mobster · …” both there and in
        simctl's current list."""
        current = next((item for item in devices if item.udid == entry["udid"]), None)
        names = [entry["name"]] + ([current.name] if current else [])
        if not all(simctl.is_mobster_name(name) for name in names):
            shown = current.name if current else entry["name"]
            raise SimError("simulator", f"{entry['udid']} is named “{shown}”, not a Mobster simulator name, so "
                                        "Mobster won't change it.",
                           "Rename it back, or delete it yourself in Xcode.")
        return current

    def erase(self, udid: str) -> None:
        """Shut down and erase one Mobster simulator; the next acquire boots it fresh."""
        self.tools()
        entry = self._entry(udid)
        current = self._guard_owned(entry, self._devices())
        if current is None:
            raise SimError("simulator", f"{entry['name']} no longer exists.", "Run `mobster sim prune`.")

        def erase_it():
            self._stop_one(entry, current.state)
            result = self.simctl("erase", entry["udid"], timeout=120)
            self._approved.pop(entry["udid"], None)  # erasing forgets iOS's answers too
            if not result.ok:
                raise SimError("simulator", f"{entry['name']} wasn't erased: {result.last_line()}",
                               "Try again in a moment.")
            self._say(f"Erased {entry['name']}")
        self._with_lease(entry, erase_it, True)

    def delete(self, udid: Optional[str] = None) -> list:
        """Shut down and delete one Mobster simulator, or all of them. Only UDIDs in the registry whose names
        start with “Mobster · ”; any other UDID is refused. Returns the deleted UDIDs."""
        self.tools()
        entries = self.registry.read()
        devices = self._devices() if entries else []
        chosen = [self._entry(udid, entries)] if udid else list(entries)
        deleted = []
        for entry in chosen:
            try:
                current = self._guard_owned(entry, devices)
            except SimError:
                if udid:
                    raise
                self._say(f"Skipped {entry['udid']}: its name no longer starts with “Mobster · ”")
                continue

            def delete_it(entry=entry, current=current):
                if current is not None:
                    self._stop_one(entry, current.state)
                    result = self.simctl("delete", entry["udid"], timeout=120)
                    if not result.ok:
                        raise SimError("simulator", f"{entry['name']} wasn't deleted: {result.last_line()}",
                                       "Try again in a moment.")
                    deleted.append(entry["udid"])
                    self._say(f"Deleted {entry['name']}")
                self._forget(entry["udid"])
            self._with_lease(entry, delete_it, bool(udid))
        return deleted

    def _forget(self, udid):
        self._approved.pop(udid, None)
        with self.registry.edit() as entries:
            entries[:] = [entry for entry in entries if entry["udid"] != udid]
        try:
            self.wda.runner_log(udid).unlink()
        except OSError:
            pass

    def prune(self) -> dict:
        """Delete simulators unused for 14 days, WDA builds no installed Xcode can use, and registry entries
        whose simulators are gone. Returns what it removed."""
        import shutil
        tools = self.tools()
        removed = {"simulators": [], "entries": [], "wda_builds": []}
        with self.registry.edit() as entries:
            present = {item.udid for item in self._devices()}  # under the lock, as in _claim
            for entry in [entry for entry in entries if entry["udid"] not in present]:
                entries.remove(entry)
                removed["entries"].append(entry["udid"])
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=PRUNE_DAYS)
        for entry in self.registry.read():
            used = _parse_time(entry.get("last_used_at")) or _parse_time(entry.get("created_at"))
            if used is not None and used < cutoff:
                try:
                    removed["simulators"] += self.delete(entry["udid"])
                except SimError as exc:
                    self._say(f"Kept {entry['name']}: {exc}")
        installed = installed_xcode_builds() | {tools.build}
        running = self.wda.ps()
        from .wda import COMMIT8, PATCH_DIGEST, parse_build_dir
        folder = self.data_dir / "wda"
        for path in sorted(folder.glob("build-*")) if folder.is_dir() else ():
            parts = parse_build_dir(path.name)
            stale = parts is None or parts[0] not in installed or parts[1:] != (COMMIT8, PATCH_DIGEST)
            if stale and str(path) not in running:
                shutil.rmtree(path, ignore_errors=True)
                removed["wda_builds"].append(str(path))
        for path in sorted(folder.glob("src-*")) if folder.is_dir() else ():
            if path.name != f"src-{COMMIT8}":
                shutil.rmtree(path, ignore_errors=True)
                removed["wda_builds"].append(str(path))
        self._log(f"pruned {removed}")
        return removed

    def status(self) -> dict:
        try:
            tools = self.tools()
            xcode = {"version": tools.version, "build": tools.build,
                     "path": str(Path(tools.xcodebuild).parents[4])}
        except SimError as exc:
            tools, xcode = None, {"error": str(exc)}
        runtime = device_type = None
        simulators = []
        if tools is not None:
            try:
                chosen_type, chosen_runtime, _ = self._choose()
                runtime, device_type = chosen_runtime.name, chosen_type.name
            except SimError as exc:
                runtime = {"error": str(exc)}
            try:
                simulators = self.list()
            except SimError as exc:
                simulators = [{"error": str(exc)}]
        plan = find_xctestrun(build_dir(self.data_dir, tools.build)) if tools else None
        return {"xcode": xcode, "runtime": runtime, "device_type": device_type,
                "wda_build": {"cached": plan is not None, "path": str(plan) if plan else None},
                "simulators": simulators, "data_dir": str(self.data_dir), "max_sims": self.max_sims}

    def doctor(self, fix: bool = False) -> list:
        """[{key, label, state, detail, fix}], state ok, warn, fail or skip. ``fix`` prepares the simulator
        (acquire, then release). It never runs sudo and never downloads a runtime: it prints those commands."""
        from .doctor import run_checks
        return run_checks(self, fix=fix)


def xcode_version(developer_dir):
    """("26.4", "17E192") from the Xcode app's version.plist, else ("", "")."""
    import plistlib
    try:
        with open(Path(developer_dir).parent / "version.plist", "rb") as stream:
            info = plistlib.load(stream)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return "", ""
    return str(info.get("CFBundleShortVersionString") or ""), str(info.get("ProductBuildVersion") or "")


def parse_xcode_version(text):
    """("26.4", "17E192") from `xcodebuild -version`."""
    import re
    version = re.search(r"Xcode\s+(\S+)", text or "")
    build = re.search(r"Build version\s+(\S+)", text or "")
    return (version.group(1) if version else ""), (build.group(1) if build else "")


def installed_xcode_builds():
    """The build versions of the Xcode apps in /Applications and ~/Applications."""
    from ..device_manager import XCODE_APP_DIRS
    builds = set()
    for root in XCODE_APP_DIRS:
        try:
            candidates = list(Path(root).glob("Xcode*.app"))
        except OSError:
            continue
        for app in candidates:
            build = xcode_version(app / "Contents" / "Developer")[1]
            if build:
                builds.add(build)
    return builds

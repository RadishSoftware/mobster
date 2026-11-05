#!/usr/bin/env python3
"""Opt-in smoke for the simulator manager (SPEC §14.2): real simulators, real WebDriverAgent, no model calls.

It needs Xcode with an iOS runtime, and the network once, for the WebDriverAgent clone. It creates its own
simulators ("Mobster · …"), never opens Simulator.app, and deletes what it created at the end (unless --keep).

    MOBSTER_DATA_DIR=/tmp/sim-smoke MOBSTER_SIM_PORT_BASE=8310 python scripts/sim_smoke.py

Without MOBSTER_DATA_DIR it uses a new temporary folder, so it never touches your real registry. Legs:

  1. prepare: create, boot, WebDriverAgent build and start, each timed (cold target: 3 min)
  2. warm acquire, each with a fresh manager (target: 2 s)
  3. lsof: WebDriverAgent and MJPEG listen on 127.0.0.1 only, and the boot's settings took (keyboard, the
     keyboard introduction marked as shown, status bar)
  4. launch com.apple.Preferences, and WebDriverAgent reports it in front
  5. screenshots written as JPEGs
  6. a fixture .app built with `xcrun -sdk iphonesimulator swiftc` installs; it declares a URL scheme
  7. the simulator's first keyboard types exactly what WebDriverAgent sends into the fixture's text field
  8. open_url from Settings: the fixture (installed by Mobster, so its scheme is already answered) opens with
     no prompt; a copy installed with plain `simctl install` gets iOS's "Open in …" prompt, which open_url
     answers; both end in front with the link and no alert
  9. an "Open in …" prompt left on screen is dismissed by the next acquire (target 4 s) and by reset
 10. reset data clears the UserDefaults value the fixture wrote, and empties its Documents
 11. reset reinstall does the same
 12. a device build of the fixture is refused with SimError kind "install"
 13. the restarter (sim.wda.SimulatorWDA) brings the runner back, still on 127.0.0.1 only
 14. list, shutdown and delete; with --second-prepare, a second cold prepare reuses the cached build, and
     its simulator is shut down before leg 15, so the run never has more than two booted at once
 15. two `prepare` processes started together on an empty registry, in a second data folder that shares this
     run's WebDriverAgent build: both simulators are registered under different names and ports, no process
     forgets the other's, `list` shows both, and `delete --all` removes both

Every simulator the run added to the registry is deleted at the end, one UDID at a time, even when a leg fails
(unless --keep, which keeps the first simulator; leg 15 always deletes its two). The last leg checks that the
cleanup deleted exactly the run's simulators and kept every one that was there before. --fail-after LEG raises
after that leg, to check the cleanup after a failure. Exit code 0 when every leg passed, 1 otherwise. --json
PATH writes the timings and results; --evidence DIR keeps the first keyboard's and the opened link's
screenshots.
"""

import argparse
import json
import os
import plistlib
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURE_BUNDLE = "dev.mobster.simsmoke"
FIXTURE_SCHEME = "mobstersimsmoke"
FIXTURE_SOURCE = """
import SwiftUI

struct SmokeView: View {
    let launches: Int
    @State private var opened = "nothing"
    @State private var typed = ""

    var body: some View {
        VStack(spacing: 24) {
            Text("Launch \\(launches)").accessibilityIdentifier("launch_count")
            Text("Opened \\(opened)").accessibilityIdentifier("opened_url")
            TextField("Type here", text: $typed).accessibilityIdentifier("field").padding()
        }
        .onOpenURL { url in opened = url.absoluteString }
    }
}

@main
struct SimSmokeApp: App {
    let launches: Int

    init() {
        let defaults = UserDefaults.standard
        launches = defaults.integer(forKey: "smokeLaunches") + 1
        defaults.set(launches, forKey: "smokeLaunches")
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        try? String(launches).write(to: documents.appendingPathComponent("launches.txt"), atomically: true,
                                    encoding: .utf8)
    }

    var body: some Scene {
        WindowGroup { SmokeView(launches: launches) }
    }
}
"""
# Autocorrection would make "teh" "the", smart punctuation would curl the apostrophe and join the dashes.
TYPED = "teh smoke don't -- 9:41"
COLD_LIMIT, WARM_LIMIT = 180.0, 2.0
ALERT_LIMIT = 4.0  # a warm acquire that also dismisses an alert: the dismissal is a tap and its animation
PHASES = ("create", "boot", "configure", "wda_build", "wda_start")
CREATED = re.compile(r" created .* \(([0-9A-F-]{36})\) on ports ")
FORGOT = re.compile(r" forgot ([0-9A-F-]{36}) ")


class StopAfter(Exception):
    """--fail-after: the run stops here, to check the cleanup after a failure."""


class Smoke:
    def __init__(self, repeat):
        self.repeat = repeat
        self.results = []   # (leg, ok, detail)
        self.timings = {}   # phase -> [seconds]

    def record(self, leg, ok, detail=""):
        self.results.append({"leg": leg, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'}  {leg}" + (f": {detail}" if detail else ""), flush=True)
        return ok

    def time(self, phase, seconds):
        self.timings.setdefault(phase, []).append(round(seconds, 3))

    def timed(self, phase, function, *args, **kwargs):
        started = time.monotonic()
        value = function(*args, **kwargs)
        self.time(phase, time.monotonic() - started)
        return value

    def summary(self):
        rows = {}
        for phase, values in self.timings.items():
            rows[phase] = {"n": len(values), "median": round(statistics.median(values), 2),
                           "min": round(min(values), 2), "max": round(max(values), 2)}
        return rows


def redact_lsof(text):
    """lsof's table without the USER column's value."""
    lines = []
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[1].isdigit():
            line = f"{parts[0]:<9} {parts[1]:>6} <user> {parts[3]}"
        elif parts and parts[0] == "COMMAND":
            line = re.sub(r"\bUSER\b", "USER  ", line, count=1)
        lines.append(line)
    return "\n".join(lines)


def build_fixture(folder):
    """A tiny SwiftUI app for the simulator: it counts its launches in UserDefaults and writes the count to
    Documents/launches.txt, shows the last link it was opened with, and has a text field. It declares the URL
    scheme FIXTURE_SCHEME."""
    app = Path(folder) / "SimSmoke.app"
    app.mkdir(parents=True)
    source = Path(folder) / "main.swift"
    source.write_text(FIXTURE_SOURCE)
    subprocess.run(["xcrun", "-sdk", "iphonesimulator", "swiftc", "-parse-as-library",
                    "-target", "arm64-apple-ios17.0-simulator", str(source), "-o", str(app / "SimSmoke")],
                   check=True, capture_output=True, text=True, timeout=300)
    info = {"CFBundleIdentifier": FIXTURE_BUNDLE, "CFBundleExecutable": "SimSmoke", "CFBundleName": "SimSmoke",
            "CFBundleDisplayName": "Sim Smoke", "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1", "CFBundleSupportedPlatforms": ["iPhoneSimulator"], "MinimumOSVersion": "17.0",
            "UIDeviceFamily": [1], "LSRequiresIPhoneOS": True, "UILaunchScreen": {},
            "DTPlatformName": "iphonesimulator",
            "CFBundleURLTypes": [{"CFBundleURLName": FIXTURE_BUNDLE, "CFBundleURLSchemes": [FIXTURE_SCHEME]}]}
    with open(app / "Info.plist", "wb") as stream:
        plistlib.dump(info, stream)
    return app


def variant(app, folder, suffix):
    """A copy of the fixture with its own bundle ID and URL scheme, for a prompt iOS has never been answered."""
    copy = Path(folder) / f"SimSmoke-{suffix}.app"
    shutil.copytree(app, copy)
    with open(copy / "Info.plist", "rb") as stream:
        info = plistlib.load(stream)
    info["CFBundleIdentifier"] = f"{FIXTURE_BUNDLE}.{suffix}"
    info["CFBundleDisplayName"] = f"Sim Smoke {suffix}"
    info["CFBundleURLTypes"] = [{"CFBundleURLName": info["CFBundleIdentifier"],
                                 "CFBundleURLSchemes": [FIXTURE_SCHEME + suffix]}]
    with open(copy / "Info.plist", "wb") as stream:
        plistlib.dump(info, stream)
    return copy, info["CFBundleIdentifier"], FIXTURE_SCHEME + suffix


def device_build(app, folder):
    """A copy of the fixture whose Info.plist says it was built for a device."""
    copy = Path(folder) / "SimSmokeDevice.app"
    shutil.copytree(app, copy)
    with open(copy / "Info.plist", "rb") as stream:
        info = plistlib.load(stream)
    info["CFBundleSupportedPlatforms"] = ["iPhoneOS"]
    info["DTPlatformName"] = "iphoneos"
    with open(copy / "Info.plist", "wb") as stream:
        plistlib.dump(info, stream)
    return copy


def data_container(manager, target):
    result = manager.simctl("get_app_container", target.udid, FIXTURE_BUNDLE, "data")
    return Path(result.out.strip()) if result.ok and result.out.strip() not in ("", "(null)") else None


def launches(manager, target, expected, timeout=15):
    """Wait for Documents/launches.txt to read ``expected``; returns what it last read."""
    end, seen = time.monotonic() + timeout, None
    while time.monotonic() < end:
        container = data_container(manager, target)
        try:
            seen = (container / "Documents" / "launches.txt").read_text().strip() if container else None
        except OSError:
            seen = None
        if seen == str(expected):
            return seen
        time.sleep(.25)
    return seen


def active_app(wda_url):
    try:
        with urllib.request.urlopen(wda_url + "/wda/activeAppInfo", timeout=10) as response:
            return (json.load(response).get("value") or {}).get("bundleId")
    except (OSError, ValueError):
        return None


def wait_for(predicate, timeout=10, poll=.25):
    """The predicate's first truthy value within ``timeout`` seconds, else its last value."""
    end = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= end:
            return value
        time.sleep(poll)


class Session:
    """A WebDriverAgent session on the app in front, for the legs that read or type in the fixture."""

    def __init__(self, wda_url):
        from mobile_agent.sim.wda import http_json
        self.http, self.url = http_json, wda_url
        status, value = http_json("POST", wda_url + "/session", {"capabilities": {"alwaysMatch": {}}}, timeout=60)
        self.id = (value or {}).get("sessionId") if status == 200 and isinstance(value, dict) else None
        if not self.id:
            raise RuntimeError(f"WebDriverAgent didn't open a session: {status} {value}")

    def call(self, method, path, body=None, timeout=30):
        return self.http(method, f"{self.url}/session/{self.id}{path}", body, timeout=timeout)

    def element(self, identifier):
        status, value = self.call("POST", "/element", {"using": "accessibility id", "value": identifier})
        if status != 200 or not isinstance(value, dict):
            return None
        return value.get("ELEMENT") or value.get("element-6066-11e4-a52e-4f735466cecf")

    def text(self, identifier):
        element = self.element(identifier)
        status, value = self.call("GET", f"/element/{element}/attribute/label") if element else (None, None)
        return value if status == 200 else None

    def close(self):
        self.http("DELETE", f"{self.url}/session/{self.id}", timeout=10)


def cleanup(manager, registry_before, keep):
    """Delete, one UDID at a time, every simulator the run added to the registry, whether or not its acquire
    returned. Simulators that were there before stay, and so does everything with --keep. Returns the UDIDs
    the run added, or None when the registry couldn't be read."""
    try:
        added = [entry["udid"] for entry in manager.registry.read() if entry["udid"] not in registry_before]
    except Exception as exc:
        print(f"cleanup couldn't read the registry: {exc}", flush=True)
        return None
    if keep:
        if added:
            print(f"Kept {', '.join(added)}; delete with `python -m mobile_agent.sim delete <UDID>`", flush=True)
        return added
    for udid in added:
        try:
            manager.delete(udid)
        except Exception as exc:  # report, and name the UDID for a manual cleanup
            print(f"cleanup of {udid} failed: {exc}; delete it with `python -m mobile_agent.sim delete {udid}`",
                  flush=True)
    return added


def simctl_udids():
    """The UDIDs `simctl list devices` shows now, or None when it didn't answer."""
    try:
        done = subprocess.run(["xcrun", "simctl", "list", "devices", "-j"], capture_output=True, text=True,
                              timeout=120)
        return {device["udid"] for devices in json.loads(done.stdout)["devices"].values() for device in devices}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None


def check_cleanup(smoke, registry, registry_before, added, listed_before):
    """The last leg: every simulator the run added is gone from the registry and from simctl, and every one
    that was there before (and still exists) is still registered."""
    listed = simctl_udids()
    try:
        left = {entry["udid"] for entry in registry.read()}
    except Exception as exc:
        return smoke.record("cleanup deleted exactly the run's simulators", False, f"registry unreadable: {exc}")
    if added is None or listed is None:
        return smoke.record("cleanup deleted exactly the run's simulators", False,
                            "the registry or simctl's list couldn't be read")
    kept = {udid for udid in registry_before if udid in listed_before}
    stayed = [udid for udid in added if udid in left or udid in listed]
    lost = sorted(kept - left)
    return smoke.record("cleanup deleted exactly the run's simulators", not stayed and not lost,
                        f"deleted {len(added)} the run added ({', '.join(added) or 'none'}), still there: "
                        f"{stayed or 'none'}; kept {len(kept)} that were there before, lost: {lost or 'none'}")


def second_prepare(smoke, manager):
    """Leg 14's --second-prepare: a cold prepare after the delete reuses the cached WebDriverAgent build. Its
    simulator is shut down straight after, so leg 15's two prepares never make three of the run's simulators
    booted at once (SPEC §0.3: one per workstream, two briefly for a concurrency test). The cleanup deletes it."""
    lease = manager.acquire()
    try:
        udid, timing = lease.target.udid, lease.timing
        for phase in PHASES:
            if phase in timing:
                smoke.time(phase, timing[phase])
        smoke.time("prepare_total", timing["total"])
    finally:
        lease.release()
    manager.shutdown(udid)
    state = next((row["state"] for row in manager.list() if row["udid"] == udid), "Missing")
    return smoke.record("second prepare reuses the WebDriverAgent build", "wda_build" not in timing
                        and state == "Shutdown", f"{timing}; its simulator is {state} before leg 15")


def sim_command(folder, name, *args):
    """`python -m mobile_agent.sim <name> …` on the data folder ``folder``, as its own process, with its output
    in ``folder``."""
    env = {**os.environ, "MOBSTER_DATA_DIR": str(folder), "PYTHONPATH": str(ROOT)}
    stem = folder / f"{name}-{time.monotonic_ns()}"
    with open(f"{stem}.out", "w") as out, open(f"{stem}.err", "w") as err:
        process = subprocess.Popen([sys.executable, "-m", "mobile_agent.sim", name, *args], cwd=ROOT, env=env,
                                   stdout=out, stderr=err, text=True)
    process.paths = (Path(f"{stem}.out"), Path(f"{stem}.err"))
    return process


def wait_all(processes, timeout):
    """Each process's exit code and seconds from now, in order (None for one still running at ``timeout``)."""
    started, ends = time.monotonic(), [None] * len(processes)
    while None in ends and time.monotonic() - started < timeout:
        for index, process in enumerate(processes):
            if ends[index] is None and process.poll() is not None:
                ends[index] = round(time.monotonic() - started, 1)
        time.sleep(.2)
    return [(process.returncode if end is not None else None, end) for process, end in zip(processes, ends)]


def created_in_log(folder):
    """(UDIDs the manager logged as created, UDIDs it logged as forgotten) in ``folder``'s sim.log."""
    try:
        text = (folder / "logs" / "sim.log").read_text()
    except OSError:
        return [], []
    return CREATED.findall(text), FORGOT.findall(text)


def two_prepares_at_once(smoke, folder):
    """Leg 15. ``folder`` is an empty data folder whose `wda` links to this run's build."""
    from mobile_agent.sim import SimulatorManager
    processes = [sim_command(folder, "prepare", "--json") for _ in range(2)]
    results = wait_all(processes, timeout=900)
    for process in processes:
        if process.poll() is None:  # past the limit: stop it before the cleanup deletes its simulator
            process.terminate()
            process.wait(timeout=60)
    prepared = []
    for process, (code, took) in zip(processes, results):
        try:
            value = json.loads(process.paths[0].read_text()) if code == 0 else None
        except (OSError, ValueError):
            value = None
        prepared.append(value)
        if value is not None:
            smoke.time("parallel_prepare", took)
            for phase in PHASES:
                if phase in value["timing"]:
                    smoke.time(f"parallel_{phase}", value["timing"][phase])
        else:
            print(f"prepare exited {code}:\n{process.paths[1].read_text()[-2000:]}", flush=True)
    ready = [value for value in prepared if value]
    udids = {value["udid"] for value in ready}
    names = {value["name"] for value in ready}
    ports = {value["wda_url"] for value in ready}
    manager = SimulatorManager(data_dir=folder)
    registered = {entry["udid"] for entry in manager.registry.read()}
    listed = {row["udid"]: row for row in manager.list()}
    created, forgot = created_in_log(folder)
    ok = (len(ready) == 2 and len(udids) == 2 and len(names) == 2 and len(ports) == 2 and registered == udids
          and set(listed) == udids and all(row["wda"] == "ready" for row in listed.values())
          and set(created) == udids and not forgot)
    smoke.record("two prepares at once", ok,
                 f"exits {[code for code, _ in results]} after {[took for _, took in results]} s; "
                 f"{sorted(names)} on {sorted(ports)}; registered {len(registered)}, listed {len(listed)}, "
                 f"created {len(created)}, forgotten {len(forgot)}; "
                 + "; ".join(f"{value['udid'][:8]} {value['timing']}" for value in ready))
    listed_then = simctl_udids() or set()
    deleting = sim_command(folder, "delete", "--all")
    (code, took), = wait_all([deleting], timeout=600)
    listed_now = simctl_udids()
    left = {entry["udid"] for entry in manager.registry.read()}
    gone = listed_now is not None and not (udids & listed_now) and udids <= listed_then
    smoke.record("delete --all removes both", code == 0 and not left and gone and bool(udids),
                 f"exit {code} in {took} s, registry now {sorted(left) or 'empty'}, simctl lists neither: {gone}")


def clean_parallel(folder):
    """Leg 15's cleanup, after a failure too: the registry's simulators, then any this folder's log says were
    created but that no registry holds (the orphan the leg looks for), stopped and deleted by UDID."""
    from mobile_agent.sim import SimulatorManager
    manager = SimulatorManager(data_dir=folder)
    cleanup(manager, frozenset(), False)
    listed = simctl_udids() or set()
    created, _ = created_in_log(folder)
    for udid in sorted(set(created) & listed):
        print(f"cleanup: {udid} was created by leg 15 but no registry holds it; stopping and deleting it",
              flush=True)
        try:
            manager.wda.stop(udid)
        except Exception as exc:
            print(f"cleanup: stopping {udid}'s runner failed: {exc}", flush=True)
        for command in ("shutdown", "delete"):
            subprocess.run(["xcrun", "simctl", command, udid], capture_output=True, timeout=120)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repeat", type=int, default=5, help="how many times to time the repeatable phases")
    parser.add_argument("--keep", action="store_true", help="leave the simulator in place at the end")
    parser.add_argument("--second-prepare", action="store_true",
                        help="after deleting, prepare once more to show the cached WebDriverAgent build is reused")
    parser.add_argument("--json", type=Path, help="write the results and timings to this file")
    parser.add_argument("--evidence", type=Path, help="keep the first keyboard's and the opened link's screenshots "
                                                      "in this folder")
    parser.add_argument("--fail-after", type=int, metavar="LEG", choices=range(1, 16),
                        help="raise after this leg (1–15), to check that the cleanup still deletes exactly the "
                             "run's simulators")
    args = parser.parse_args(argv)
    if not os.environ.get("MOBSTER_DATA_DIR"):
        os.environ["MOBSTER_DATA_DIR"] = tempfile.mkdtemp(prefix="mobster-sim-smoke-")
    os.environ.setdefault("MOBSTER_SIM_PORT_BASE", "8310")

    from mobile_agent.sim import SimError, SimulatorManager
    from mobile_agent.sim.wda import loopback_only, parse_lsof

    smoke = Smoke(max(1, args.repeat))
    data_dir = Path(os.environ["MOBSTER_DATA_DIR"])
    print(f"Data folder: {data_dir}\nPort base: {os.environ['MOBSTER_SIM_PORT_BASE']}", flush=True)
    manager = SimulatorManager(progress=lambda message: print(f"  {message}", file=sys.stderr, flush=True))
    # Anything else in the registry stays. Nothing below may reuse this name.
    registry_before = frozenset(entry["udid"] for entry in manager.registry.read())
    listed_before = simctl_udids() or set()
    work = Path(tempfile.mkdtemp(prefix="mobster-sim-smoke-app-"))
    evidence = args.evidence or work / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    lease = parallel = None

    def checkpoint(leg):
        if args.fail_after == leg:
            raise StopAfter(leg)
    try:
        # 1. prepare
        empty = not (data_dir / "simulators.json").exists()
        lease = manager.acquire()
        target = lease.target
        timing = lease.timing
        for phase in PHASES:
            if phase in timing:
                smoke.time(phase, timing[phase])
        smoke.time("prepare_total", timing["total"])
        cold = "create" in timing
        smoke.record("prepare", not cold or timing["total"] <= COLD_LIMIT,
                     f"{'cold' if cold else 'warm'} from {'an empty' if empty else 'an existing'} data folder, "
                     f"{timing['total']:.1f} s {timing}")
        lease.release()
        lease = None
        checkpoint(1)

        # 2. warm acquire
        warm = []
        for _ in range(smoke.repeat * 2):
            fresh = SimulatorManager()
            started = time.monotonic()
            with fresh.acquire() as again:
                warm.append(time.monotonic() - started)
                assert again.udid == target.udid
            smoke.time("warm_acquire", warm[-1])
        smoke.record("warm acquire", max(warm) <= WARM_LIMIT,
                     f"n={len(warm)}, median {statistics.median(warm):.2f} s, max {max(warm):.2f} s")
        checkpoint(2)

        lease = manager.acquire()
        target = lease.target

        # 3. lsof
        port, mjpeg = int(target.wda_url.rsplit(":", 1)[1]), int(target.mjpeg_url.rsplit(":", 1)[1])
        shown, ok = [], True
        for number in (port, mjpeg):
            text = subprocess.run(["lsof", "-nP", f"-iTCP:{number}", "-sTCP:LISTEN"], capture_output=True,
                                  text=True).stdout
            shown.append(redact_lsof(text))
            ok = ok and loopback_only(parse_lsof(text))
        print("\n".join(shown))
        smoke.record("lsof loopback only", ok, f"{port} and {mjpeg}")
        from mobile_agent.sim.api import KEYBOARD_DEFAULTS, KEYBOARD_INTRO, KEYBOARD_PREFERENCES
        values = {key: manager.simctl("spawn", target.udid, "defaults", "read", "com.apple.Preferences",
                                      key).out.strip() for key in KEYBOARD_DEFAULTS}
        intro = {key: manager.simctl("spawn", target.udid, "defaults", "read", KEYBOARD_PREFERENCES,
                                     key).out.strip() for key, _ in KEYBOARD_INTRO}
        wanted = {key: "1" if value == "true" else "0" for key, value in KEYBOARD_INTRO}
        bar = manager.simctl("status_bar", target.udid, "list").out
        smoke.record("keyboard and status bar settings",
                     set(values.values()) == {"0"} and intro == wanted and "9:41" in bar,
                     f"{values}, {intro}, status bar time 9:41: {'9:41' in bar}")
        checkpoint(3)

        # 4. Settings
        for _ in range(smoke.repeat * 2):
            smoke.timed("launch_settings", manager.launch, target, "com.apple.Preferences")
        time.sleep(1)
        front = active_app(target.wda_url)
        smoke.record("launch com.apple.Preferences", front == "com.apple.Preferences", f"in front: {front}")
        checkpoint(4)

        # 5. screenshots
        shot_dir = work / "shots"
        sizes = []
        for index in range(smoke.repeat * 2):
            path = smoke.timed("screenshot", manager.screenshot, target, shot_dir / f"shot-{index}.jpg",
                               max_width=603)
            with open(path, "rb") as stream:
                sizes.append((stream.read(3) == b"\xff\xd8\xff", path.stat().st_size))
        from PIL import Image
        with Image.open(shot_dir / "shot-0.jpg") as image:
            width = image.width
        smoke.record("screenshot", all(jpeg for jpeg, _ in sizes) and width == 603,
                     f"JPEG, {width} px wide, {sizes[0][1]} bytes")
        checkpoint(5)

        # 6. install
        app = build_fixture(work / "fixture")
        for _ in range(smoke.repeat):
            info = smoke.timed("install", manager.install, target, str(app))
        installed = manager.installed_app(target, FIXTURE_BUNDLE)
        smoke.record("install fixture", info.bundle_id == FIXTURE_BUNDLE and installed.version == "1.0 (1)",
                     f"{info.name} {info.version}")
        checkpoint(6)

        # 7. the first keyboard on this simulator types exactly what it is sent
        manager.launch(target, FIXTURE_BUNDLE)
        wait_for(lambda: active_app(target.wda_url) == FIXTURE_BUNDLE)
        session = Session(target.wda_url)
        try:
            field = wait_for(lambda: session.element("field"))
            session.call("POST", f"/element/{field}/click", {})
            time.sleep(1.5)  # the keyboard, and any introduction over it, is up
            manager.screenshot(target, evidence / "first-keyboard.jpg", max_width=603)
            status, _ = session.call("POST", f"/element/{field}/value", {"text": TYPED, "value": list(TYPED)},
                                     timeout=60)
            _, value = session.call("GET", f"/element/{field}/attribute/value")
            manager.screenshot(target, evidence / "typed.jpg", max_width=603)
        finally:
            session.close()
        manager.terminate(target, FIXTURE_BUNDLE)
        smoke.record("first keyboard types exactly", status == 200 and value == TYPED,
                     f"sent {TYPED!r}, the field reads {value!r}")
        checkpoint(7)

        # 8. open_url: no prompt for an app Mobster installed; the prompt answered for one it didn't
        pressed = []
        action = manager.wda.alert_action

        def counted(url, name, button=None):
            pressed.append((name, button))
            return action(url, name, button)
        manager.wda.alert_action = counted

        def open_from_settings(link, bundle, phase):
            manager.launch(target, "com.apple.Preferences")
            wait_for(lambda: active_app(target.wda_url) == "com.apple.Preferences", timeout=5)
            smoke.timed(phase, manager.open_url, target, link)
            front = wait_for(lambda: active_app(target.wda_url) == bundle, timeout=10)
            return bool(front) and manager.wda.alert_text(target.wda_url) is None

        stamp = int(time.time())
        link = f"{FIXTURE_SCHEME}://smoke/{stamp}"
        opened = [open_from_settings(f"{link}/{index}", FIXTURE_BUNDLE, "open_url") for index in range(smoke.repeat)]
        session = Session(target.wda_url)
        try:
            shown = wait_for(lambda: session.text("opened_url") == f"Opened {link}/{smoke.repeat - 1}", timeout=5)
            label = session.text("opened_url")
        finally:
            session.close()
        manager.screenshot(target, evidence / "after-open-url.jpg", max_width=603)
        smoke.record("open_url into an app Mobster installed", all(opened) and shown and not pressed,
                     f"n={len(opened)}, in front with no alert: {sum(opened)}, prompts answered: {len(pressed)}, "
                     f"the fixture shows {label!r}")
        outside, outside_bundle, outside_scheme = variant(app, work / "fixture", "outside")
        manager.simctl("install", target.udid, str(outside), timeout=300)  # plain simctl: no answer given yet
        first = open_from_settings(f"{outside_scheme}://smoke/{stamp}", outside_bundle, "open_url_prompt")
        answered = list(pressed)
        again = open_from_settings(f"{outside_scheme}://smoke/{stamp}/again", outside_bundle, "open_url")
        smoke.record("open_url answers the prompt", first and again and answered == [("accept", "Open")]
                     and len(pressed) == 1,
                     f"first link: in front with no alert: {first}, pressed {answered}; the next link: {again}, "
                     f"with no second prompt: {len(pressed) == 1}")
        manager.wda.alert_action = action
        manager.terminate(target, outside_bundle)
        checkpoint(8)

        # 9. an "Open in …" prompt left on screen: the next acquire, and reset, dismiss it
        left, _, left_scheme = variant(app, work / "fixture", "left")
        manager.simctl("install", target.udid, str(left), timeout=300)  # never answered Open: it always asks

        def raise_prompt():
            manager.launch(target, "com.apple.Preferences")
            wait_for(lambda: active_app(target.wda_url) == "com.apple.Preferences", timeout=5)
            manager.simctl("openurl", target.udid, f"{left_scheme}://left")  # bare simctl, as a run could leave it
            return wait_for(lambda: (manager.wda.alert_text(target.wda_url) or "").startswith("Open in"), timeout=10)

        prompted = raise_prompt()
        before_text = manager.wda.alert_text(target.wda_url)
        lease.release()
        fresh = SimulatorManager()
        started = time.monotonic()
        lease = fresh.acquire()
        took = time.monotonic() - started
        smoke.time("warm_acquire_with_alert", took)
        after = manager.wda.alert_text(target.wda_url)
        smoke.record("acquire dismisses a leftover alert",
                     prompted and after is None and took <= ALERT_LIMIT and lease.target.udid == target.udid,
                     f"{before_text!r} before, {after!r} after, acquire took {took:.2f} s")
        prompted = raise_prompt()
        before_text = manager.wda.alert_text(target.wda_url)
        smoke.timed("reset_data_with_alert", manager.reset, target, FIXTURE_BUNDLE, "data")
        after = manager.wda.alert_text(target.wda_url)
        smoke.record("reset dismisses a leftover alert", prompted and after is None,
                     f"{before_text!r} before, {after!r} after")
        checkpoint(9)

        # 10. reset data
        manager.reset(target, FIXTURE_BUNDLE, "reinstall", str(app))
        count = None
        for number in range(1, smoke.repeat + 1):
            smoke.timed("launch_fixture", manager.launch, target, FIXTURE_BUNDLE)
            count = launches(manager, target, number)  # the count persists from launch to launch
        counted_up = count == str(smoke.repeat)
        smoke.timed("reset_data", manager.reset, target, FIXTURE_BUNDLE, "data")
        container = data_container(manager, target)
        documents_empty = container is not None and not any((container / "Documents").iterdir())
        manager.launch(target, FIXTURE_BUNDLE)
        after = launches(manager, target, 1)
        smoke.record("reset data", counted_up and documents_empty and after == "1",
                     f"launch count {count} before, Documents emptied: {documents_empty}, {after} after")
        for _ in range(smoke.repeat - 1):
            manager.launch(target, FIXTURE_BUNDLE)
            launches(manager, target, 2, timeout=5)
            smoke.timed("reset_data", manager.reset, target, FIXTURE_BUNDLE, "data")
        checkpoint(10)

        # 11. reinstall
        manager.reset(target, FIXTURE_BUNDLE, "data")
        manager.launch(target, FIXTURE_BUNDLE)
        launches(manager, target, 1)
        manager.launch(target, FIXTURE_BUNDLE)
        count = launches(manager, target, 2)
        smoke.timed("reset_reinstall", manager.reset, target, FIXTURE_BUNDLE, "reinstall", str(app))
        manager.launch(target, FIXTURE_BUNDLE)
        after = launches(manager, target, 1)
        smoke.record("reset reinstall", count == "2" and after == "1", f"launch count {count} before, {after} after")
        for _ in range(smoke.repeat - 1):
            smoke.timed("reset_reinstall", manager.reset, target, FIXTURE_BUNDLE, "reinstall", str(app))
        manager.terminate(target, FIXTURE_BUNDLE)
        checkpoint(11)

        # 12. a device build
        wrong = device_build(app, work / "fixture")
        try:
            manager.install(target, str(wrong))
            smoke.record("device build refused", False, "it installed")
        except SimError as exc:
            smoke.record("device build refused", exc.kind == "install", f"SimError({exc.kind!r}): {exc}")
        checkpoint(12)

        # 13. restarter
        restart = manager.restarter(target)
        started = time.monotonic()
        restart(timeout=120)
        smoke.time("wda_restart", time.monotonic() - started)
        ok = all(loopback_only(manager.wda.listeners(number)) for number in (port, mjpeg))
        smoke.record("restarter", ok, f"ready again in {time.monotonic() - started:.1f} s, still 127.0.0.1 only")
        lease.release()
        lease = None
        checkpoint(13)

        # 14. list, shutdown, delete
        rows = manager.list()
        smoke.record("list", [row["udid"] for row in rows] == [target.udid] and rows[0]["wda"] == "ready",
                     f"{rows[0]['name']}: {rows[0]['state']}, WebDriverAgent {rows[0]['wda']}")
        smoke.timed("shutdown", manager.shutdown)
        state = manager.list()[0]["state"]
        smoke.record("shutdown", state == "Shutdown", state)
        if not args.keep:
            deleted = manager.delete(target.udid)
            gone = target.udid not in subprocess.run(["xcrun", "simctl", "list", "devices"], capture_output=True,
                                                     text=True).stdout
            smoke.record("delete", deleted == [target.udid] and gone, f"deleted {deleted}")
            if args.second_prepare:
                second_prepare(smoke, manager)
        checkpoint(14)

        # 15. two prepares at once, on an empty registry that shares this run's WebDriverAgent build
        parallel = Path(tempfile.mkdtemp(prefix="mobster-sim-smoke-parallel-"))
        (parallel / "wda").symlink_to(data_dir / "wda", target_is_directory=True)
        two_prepares_at_once(smoke, parallel)
        checkpoint(15)
    except StopAfter as stop:
        smoke.record(f"stopped after leg {stop.args[0]} (--fail-after)", True)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        smoke.record("unexpected error", False, f"{type(exc).__name__}: {exc}")
    finally:
        if lease is not None:
            lease.release()
        added = cleanup(manager, registry_before, args.keep)
        if parallel is not None:
            clean_parallel(parallel)
            shutil.rmtree(parallel, ignore_errors=True)  # unlinks `wda`, never the build it points to
        shutil.rmtree(work, ignore_errors=True)
    if not args.keep:
        check_cleanup(smoke, manager.registry, registry_before, added, listed_before)
    summary = smoke.summary()
    print("\nTimings (seconds):")
    for phase, row in summary.items():
        print(f"  {phase:<16} n={row['n']:<3} median {row['median']:>7.2f}  min {row['min']:>7.2f}  "
              f"max {row['max']:>7.2f}")
    passed = all(result["ok"] for result in smoke.results)
    print(f"\n{'All legs passed' if passed else 'Some legs failed'} ({sum(r['ok'] for r in smoke.results)} of "
          f"{len(smoke.results)}), {time.strftime('%Y-%m-%d')}")
    if args.json:
        args.json.write_text(json.dumps({"date": time.strftime("%Y-%m-%d"), "results": smoke.results,
                                         "timings": smoke.timings, "summary": summary}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

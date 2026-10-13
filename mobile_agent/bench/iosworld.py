"""iOSWorld, run by Mobster: the published iOS agent benchmark, on its own terms.

iOSWorld (arXiv 2606.09764; github.com/ljang0/iOSWorld, CC BY 4.0): 133 tasks over 26
SwiftUI apps on an iPhone 17 Pro simulator, scored by its own LLM judge (gpt-5.4-mini by
default) from each run's trajectory -- per-step screenshots, actions, final answer --
against the task's rubric. Published best: 51.9% (Claude Opus 4.6 + XCUITest tree).

What is iOSWorld's and unchanged: the tasks (goals verbatim), the start state (its own
``reset_app_data`` + ``reseed_apps``, then Home, as its runner does), the 50-step limit,
the trajectory format and the judge (``scripts/judge_trajectories.py``, run as is).

What is Mobster's: its agent (mobile_agent.frontier.FrontierAgent, composed as the app's
Smart engine with FrameClock), driving the simulator through WDA, capped per task by
``task_cost_cap``. The task's app list becomes the apps the agent may open -- iOSWorld hands
its own agent the same list -- and the first one is opened, recorded as a ``launch_app`` step
as iOSWorld's agent would take it. ``--policy`` takes one value, ``frontier``, the default.

Screenshots exist only for the judge: taken with ``simctl io`` (never through WDA, never
shown to Mobster) before each action and once at the end; their time is excluded from
Mobster's reported latency. The simulator runs headless: nothing here opens a window.

    python -m mobile_agent.bench.iosworld run --repo ../iOSWorld --udid <UDID> \\
        --wda-url http://127.0.0.1:8200 --mjpeg-url http://127.0.0.1:9200 \\
        --out research/iosworld-runs/NAME [--only caltrack-001,...] [--limit N] --env-file <agent.env> \\
        [--policy frontier --model gpt-5.6-luna --reasoning low]
    python -m mobile_agent.bench.iosworld pool --repo ../iOSWorld --sims UDID:WDA_PORT:MJPEG_PORT,... \\
        --out research/iosworld-runs/NAME [--only ...] [--limit N] [--policy frontier ...] --env-file <agent.env>
    python -m mobile_agent.bench.iosworld judge --repo ../iOSWorld --run research/iosworld-runs/NAME
    python -m mobile_agent.bench.iosworld report --run research/iosworld-runs/NAME
    python -m mobile_agent.bench.iosworld compare --a research/iosworld-runs/OLD-* --b research/iosworld-runs/NEW-*
    python -m mobile_agent.bench.iosworld estimate research/iosworld-runs/* --model gpt-5.6-sol

A task passes by iOSWorld's own rule: every rubric criterion satisfied (judge_trajectories.py
``_aggregate``), not the judge's ``success`` flag; see bench/iosworld_stats.py.
"""

import argparse
import base64
import dataclasses
import glob
import json
import os
import pathlib
import plistlib
import re
import statistics
import subprocess
import sys
import time
import urllib.request

from ..engines import SMART_CONFIG, apply_switches, frontier_kwargs

# The frontier policy is the app's Smart engine: its model, reasoning, limits and switches come from
# engines.SMART_CONFIG (tests/test_engines.py proves both build the same agent).
DEFAULT_FRONTIER_MODEL = SMART_CONFIG.model
MAX_TASK_COST_USD = 0.60  # a runaway task stops here (24 Sep: 50-step loops cost ~$0.80 each on gpt-5.5)


def task_cost_cap(bundles, model=None):
    """A spend cap that grows with the apps a task spans: a flat $0.25 cut five-app tasks off
    mid-way (multi-006, mem-005, 24 Sep) while one-app tasks finish for $0.03-0.13. It is set in
    gpt-5.6-terra dollars and scaled up by a model's input price, so a pricier model gets as many
    turns: iOSWorld limits turns, not spend (the cap stopped 9 of 21 gpt-5.6-sol runs, 28 Sep)."""
    from ..frontier import PRICES
    base, rate = PRICES.get("gpt-5.6-terra"), PRICES.get(model or "")
    scale = rate[0] / base[0] if base and rate and base[0] else 1.0
    return min(MAX_TASK_COST_USD, 0.10 + 0.08 * max(1, len(bundles))) * max(1.0, scale)
TASK_DIR_WIDTH = 3
_APP_ALIASES = {"letterboxd": "cinephile", "whatsapp": "quickchat", "linkedin": "lockedin"}


# -- iOSWorld's side ---------------------------------------------------------------------

def load_tasks(repo):
    return json.loads((pathlib.Path(repo) / "tasks.json").read_text(encoding="utf-8"))


def load_manifest(repo):
    """{app: {bundle_id, app_path, ...}} written by iOSWorld's bootstrap."""
    for candidate in sorted(pathlib.Path(repo).rglob(".app_manifest.json")):
        return json.loads(candidate.read_text(encoding="utf-8"))
    raise FileNotFoundError("No .app_manifest.json: run iOSWorld's bootstrap first")


def register_names(manifest):
    """Each app's display name (its Info.plist), so the agent's app-switch choices read "CalTrack", not a bundle."""
    import plistlib
    from ..catalog import register_app_names
    names = {}
    for key, entry in manifest.items():
        name = key
        try:
            with open(pathlib.Path(entry.get("app_path", "")) / "Info.plist", "rb") as handle:
                info = plistlib.load(handle)
            name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or key
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
        if entry.get("bundle_id"):
            names[entry["bundle_id"]] = name
    register_app_names(names)
    return names


def iosworld_module(repo):
    """iOSWorld's own runner module (stdlib-only at import), for its reset functions."""
    scripts = str(pathlib.Path(repo) / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import appium_agent
    return appium_agent


def app_entry(manifest, name):
    return manifest.get(_APP_ALIASES.get(name, name)) or manifest.get(name) or {}


SIM_DEVICES = pathlib.Path.home() / "Library/Developer/CoreSimulator/Devices"


def foreign_containers(udid, manifest):
    """Apps whose data containers on ``udid`` resolve outside that simulator's own folder, as
    [(bundle, path)]. A ``simctl clone`` of a simulator with the apps installed copies its
    LaunchServices store, which keeps the source's absolute paths: every clone then runs the
    apps out of the source's containers, and one worker's reset wipes another's task mid-run
    (26 Sep: three clones on the first simulator's data). Use fresh simulators, each
    bootstrapped on its own."""
    own = os.path.realpath(SIM_DEVICES / udid) + os.sep  # both sides resolved: a home under /var is /private/var
    foreign = []
    for entry in manifest.values():
        bundle = entry.get("bundle_id")
        if not bundle:
            continue
        proc = subprocess.run(["xcrun", "simctl", "get_app_container", udid, bundle, "data"],
                              capture_output=True, text=True)
        path = proc.stdout.strip()
        if proc.returncode == 0 and path and not os.path.realpath(path).startswith(own):
            foreign.append((bundle, path))
    return foreign


def check_isolation(udid, manifest):
    """Refuse a simulator whose apps live in another simulator's containers (see foreign_containers)."""
    foreign = foreign_containers(udid, manifest)
    if foreign:
        bundle, path = foreign[0]
        raise SystemExit(f"simulator {udid} is not isolated: {len(foreign)} apps resolve to another device's "
                         f"containers (e.g. {bundle} -> {path}). It was likely made with `simctl clone`: "
                         f"create a fresh simulator and run iOSWorld's bootstrap on it.")


# -- the apps' end state, for code checks ----------------------------------------------------

CONTAINER_METADATA = ".com.apple.mobile_container_manager.metadata.plist"
STATE_PARTS = ("Documents", "Library/Application Support")  # JSON state files and SQLite stores (with -wal, -shm)
STATE_SETTLE_SECONDS = 1.0  # apps write on leaving the foreground (SwiftData's autosave) or on a background queue
SECRET_DEFAULTS = re.compile(r"api[_-]?key|token|secret|password", re.I)  # iOSWorld writes openai_api_key into each app


def containers(root):
    """{identifier: folder} for the containers under ``root``, named by each one's metadata plist, the way
    iOSWorld's mcps/_data_layer.py finds an app's data (Data/Application) or the shared envelope (Shared/AppGroup)."""
    found = {}
    for folder in sorted(root.iterdir()) if root.is_dir() else ():
        try:
            identifier = plistlib.loads((folder / CONTAINER_METADATA).read_bytes()).get("MCMMetadataIdentifier")
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        if identifier:
            found[identifier] = folder
    return found


def utc(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)) if epoch is not None else None


def save_end_state(udid, bundles, folder, wda_url=None, started=None):
    """Copy the task's apps' end state into ``folder``/state, before the next task's reset wipes it, so a run can be
    graded by code: each app's Documents/, Library/Application Support/ and UserDefaults plist (state/apps/<bundle>/),
    and the app groups of the apps' vendor (state/groups/<group>/: the cloud apps' shared workspace envelope). With
    ``wda_url``, the app in front is sent home first and given STATE_SETTLE_SECONDS to write; then the task's apps are
    closed, so nothing writes during the copy, and their preferences get as long again to reach disk. ``started`` (the
    task's start, epoch seconds) is recorded, since the shared app groups are never reset and keep earlier runs'
    records. It never fails a run: any error is written to state/ERROR."""
    import shutil
    out = pathlib.Path(folder) / "state"
    began = time.monotonic()
    try:
        if wda_url:
            try:  # WDA's sessionless home button: the run's session is closed by now
                urllib.request.urlopen(urllib.request.Request(wda_url + "/wda/homescreen", data=b"{}", method="POST",
                                                              headers={"Content-Type": "application/json"}), timeout=10)
            except (OSError, ValueError):
                pass
            time.sleep(STATE_SETTLE_SECONDS)
            for bundle in bundles:
                try:
                    subprocess.run(["xcrun", "simctl", "terminate", udid, bundle], capture_output=True, timeout=20)
                except (OSError, subprocess.SubprocessError):
                    pass
            time.sleep(STATE_SETTLE_SECONDS)
        root = SIM_DEVICES / udid / "data/Containers"
        apps = containers(root / "Data/Application")
        saved, missing = {}, []
        for bundle in bundles:
            home = apps.get(bundle)
            if home is None:
                missing.append(bundle)
                continue
            for rel in (*STATE_PARTS, f"Library/Preferences/{bundle}.plist"):
                source, target = home / rel, out / "apps" / bundle / rel
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                elif source.is_file():  # the UserDefaults plist, without keys
                    prefs = plistlib.loads(source.read_bytes())
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(plistlib.dumps({k: v for k, v in prefs.items() if not SECRET_DEFAULTS.search(k)},
                                                      fmt=plistlib.FMT_BINARY))
                else:
                    continue
                saved.setdefault(bundle, []).append(rel)
        vendors = {"group." + bundle.rsplit(".", 1)[0] for bundle in bundles if "." in bundle}
        shared = containers(root / "Shared/AppGroup")
        groups = [group for group in shared if group.rsplit(".", 1)[0] in vendors]
        for group in groups:
            shutil.copytree(shared[group], out / "groups" / group, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(CONTAINER_METADATA))
        out.mkdir(parents=True, exist_ok=True)
        record = {"udid": udid, "started": utc(started), "saved": utc(time.time()),
                  "seconds": round(time.monotonic() - began, 2), "apps": saved, "missing": missing, "groups": groups}
        (out / "state.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    except Exception as error:  # a snapshot never costs a run
        try:
            out.mkdir(parents=True, exist_ok=True)
            (out / "ERROR").write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
        except OSError:
            pass


RESET_ATTEMPTS = 2


# MOBSTER_BENCH_RESET: how each task's start state is made.
# - "iosworld" (default): iOSWorld's own reset, wiping and re-seeding every app (43-66 s a task).
# - "restore": put back a checkpoint of the freshly seeded apps (bench/evals/checkpoint.py, ~4.5 s), checked by
#   digest. The checkpoint is made once per simulator, from iOSWorld's own reset, in MOBSTER_BENCH_CHECKPOINTS
#   (seed-<UDID>). A restore whose digest differs from the save falls back to iOSWorld's reset.
RESET_MODES = ("iosworld", "restore")


def reset_mode():
    mode = os.environ.get("MOBSTER_BENCH_RESET", "").strip() or "iosworld"
    if mode not in RESET_MODES:
        raise ValueError(f"MOBSTER_BENCH_RESET={mode!r}: use iosworld or restore")
    return mode


def reset(repo, udid, manifest, mode=None):
    """The task's clean slate (see MOBSTER_BENCH_RESET); returns {"mode", "seconds", ...} for the record."""
    mode = reset_mode() if mode is None else mode
    started = time.monotonic()
    if mode == "restore":
        from .evals import checkpoint
        bundles = {app: entry["bundle_id"] for app, entry in manifest.items() if entry.get("bundle_id")}
        root = pathlib.Path(os.environ.get("MOBSTER_BENCH_CHECKPOINTS", "")).expanduser()
        if not str(root) or str(root) == ".":
            raise ValueError("MOBSTER_BENCH_RESET=restore needs MOBSTER_BENCH_CHECKPOINTS (a folder for the seeds)")
        seed = root / f"seed-{udid}"
        if not (seed / "checkpoint.json").exists():
            iosworld_reset(repo, udid, manifest)
            checkpoint.save(udid, bundles, seed)
            return {"mode": "restore", "seeded": True, "seconds": round(time.monotonic() - started, 1)}
        seconds, ok = checkpoint.restore(udid, bundles, seed)
        if ok:
            return {"mode": "restore", "seconds": seconds, "digest_ok": True}
        iosworld_reset(repo, udid, manifest)
        return {"mode": "restore", "digest_ok": False, "fell_back": True,
                "seconds": round(time.monotonic() - started, 1)}
    iosworld_reset(repo, udid, manifest)
    return {"mode": "iosworld", "seconds": round(time.monotonic() - started, 1)}


def iosworld_reset(repo, udid, manifest):
    """iOSWorld's clean slate: wipe every app's data, re-seed each, as its runner does per task."""
    runner = iosworld_module(repo)
    apps = list(manifest)
    for attempt in range(RESET_ATTEMPTS):
        try:
            runner.reset_app_data(udid, apps, manifest)
            runner.reseed_apps(udid, apps, manifest)
            return
        except RuntimeError:
            # Seeding under load can leave apps empty ("no seed data after 3 attempts": four
            # simulators busy, 24 Sep); a full second pass cleared it.
            if attempt + 1 == RESET_ATTEMPTS:
                raise


# -- screenshots for the judge ---------------------------------------------------------

class Shots:
    """PNG screenshots through ``simctl io`` (not WDA, so Mobster's device channel is untouched)."""

    def __init__(self, udid, folder):
        self.udid, self.folder = udid, pathlib.Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self.seconds = 0.0
        self.trace = None  # the driver's call_trace: shots go in it, to be told apart from the agent's time

    def take(self, name):
        started = time.monotonic()
        path = self.folder / f"{name}.png"
        try:
            subprocess.run(["xcrun", "simctl", "io", self.udid, "screenshot", "--type=png", str(path)],
                           check=True, capture_output=True, timeout=20)
        except (subprocess.SubprocessError, OSError):
            path = None
        self.seconds += time.monotonic() - started
        self.count += 1
        if self.trace is not None:
            self.trace.append(("shot", started, (time.monotonic() - started) * 1000))
        return str(path) if path else None


# -- the trajectory ----------------------------------------------------------------------

def describe(operation, target=None, text=None):
    """One iOSWorld-style action dict for a Mobster operation."""
    if operation == "LAUNCH_APP":
        return {"type": "launch_app", "bundle_id": str(target)}
    if operation.startswith("SWIPE_"):
        return {"type": "swipe", "direction": operation.split("_", 1)[1].lower()}
    if operation in ("HOME", "BACK", "VOLUME_UP", "VOLUME_DOWN"):
        return {"type": operation.lower()}
    action = {"type": {"TAP": "tap", "TYPE": "type", "TYPE_SUBMIT": "type", "SUBMIT": "submit",
                       "LONG_PRESS": "long_press"}.get(operation, operation.lower())}
    if target is not None and hasattr(target, "label"):
        action["element"] = {"label": (target.label or "")[:120], "role": target.role}
    if text is not None:
        action["text"] = text
    if operation == "TYPE_SUBMIT":
        action["submit"] = True
    return action


class RecordingDriver:
    """The simulator's WDA driver, with each dispatch recorded as an iOSWorld trajectory step."""

    def __init__(self, driver, shots):
        self._driver, self.shots = driver, shots
        self.steps = []
        # The model turn now acting (set from frontier_decision events): screenshots are named by
        # it, since a turn may dispatch several actions and a refused turn none.
        self.turn = None

    def __getattr__(self, name):
        return getattr(self._driver, name)

    def _record(self, action):
        """Screenshot and record one step; returns the screenshot's seconds."""
        index = len(self.steps) + 1
        name = f"step-{index:03d}" + (f"-turn-{self.turn:02d}" if self.turn is not None else "")
        started = time.monotonic()
        self.steps.append({"step": index, "screenshot": self.shots.take(name), "actions": [action],
                           **({"turn": self.turn} if self.turn is not None else {})})
        return time.monotonic() - started

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        spent = self._record(describe(operation, target, text))
        # The judge's screenshot (0.2-2.5 s) does not age the observation: without it the action goes
        # that much sooner, so the driver's re-read of an observation over 1 s old (WDA_FRESH_SECONDS)
        # would not run (it ran after 0.6-1.8 s screenshots in exec-new2's megamart-001).
        if hasattr(snapshot, "captured_at"):
            snapshot = dataclasses.replace(snapshot, captured_at=snapshot.captured_at + spent)
        return self._driver.execute(operation, target, snapshot, text=text, timeout=timeout)

    def long_press(self, target, snapshot, timeout=10):
        self._record({"type": "long_press", "element": {"label": (target.label or "")[:120], "role": target.role}})
        return self._driver.long_press(target, snapshot, timeout=timeout)

    def tap_point(self, x, y, snapshot, timeout=10, purpose="dismiss"):
        self._record({"type": "tap_xy", "x": round(x, 3), "y": round(y, 3), "purpose": purpose})
        return self._driver.tap_point(x, y, snapshot, timeout=timeout)

    def set_value(self, target, value, timeout=10):
        self._record({"type": "set_picker", "element": {"label": (target.value or "")[:120], "role": target.role},
                      "value": str(value)[:60]})
        return self._driver.set_value(target, value, timeout=timeout)

    def clear_text(self, target, timeout=10):
        self._record({"type": "clear_text", "element": {"label": (target.label or "")[:120], "role": target.role}})
        return self._driver.clear_text(target, timeout=timeout)

    def write_text(self, target, snapshot, text, append=False, submit=False, timeout=30):
        # One step, as the typed text: its paste, read-back and repair are how the typing is done.
        action = describe("TYPE_SUBMIT" if submit else "TYPE", target, text)
        if not append:
            action["replace"] = True
        self._record(action if text else {"type": "clear_text", "element": action.get("element")})
        return self._driver.write_text(target, snapshot, text, append=append, submit=submit, timeout=timeout)

    def replace_text(self, target, text, timeout=10):
        self._record({**describe("TYPE", target, text), "replace": True})
        done = self._driver.replace_text(target, text, timeout=timeout)
        if not done:
            self.steps.pop()  # nothing was typed: the caller clears and types, recorded as such
        return done

    def call(self, method, path, body=None, timeout=10):
        if method == "POST" and path == "/wda/apps/activate" and isinstance(body, dict):
            self._record(describe("LAUNCH_APP", body.get("bundleId")))
        elif method == "POST" and path == "/url" and isinstance(body, dict):
            self._record({"type": "open_url", "url": str(body.get("url", ""))[:300]})
        return self._driver.call(method, path, body, timeout)


def judge_shots_apart(timing, seconds):
    """The frontier's timing with the judge's screenshots (taken before each dispatch, inside its
    exec bucket) moved to their own bucket: they are not the agent's time (0.4-2.5 s each)."""
    if not timing or "exec" not in timing:
        return timing
    return {**timing, "exec": round(timing["exec"] - seconds, 2), "judge_shots": round(seconds, 2)}


def turn_tokens(usage):
    """One call's usage, compact, for the trace: input, output, cached and cache-written tokens, and with
    MOBSTER_BENCH_TTFT=on the time to first token and the call's total (bench/stream_timing.py)."""
    out = {"in": usage.get("prompt_tokens"), "out": usage.get("completion_tokens"),
           "cached": usage.get("cached_tokens"), "write": usage.get("cache_write_tokens")}
    for key in ("ttft_ms", "text_ttft_ms", "first_action_ms", "total_ms"):
        if usage.get(key) is not None:
            out[key] = usage[key]
    return out


def build_id(path=None):
    """The code's commit (and whether its tree has local changes), so every record names its build."""
    folder = pathlib.Path(path or __file__).resolve().parent
    try:
        sha = subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(folder), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return (sha[:12] + ("+dirty" if dirty else "")) if sha else None


def answer_text(result):
    data = result.get("data")
    if data is None:
        return None
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False)


# -- one task ------------------------------------------------------------------------------

def clear_alerts(driver, attempts=5):
    """Accept system prompts the re-seed launches left up (CityRide's location prompt covered
    CalTrack in the first smoke run). iOSWorld's bootstrap clicks these with AppleScript, which
    needs the Simulator window; here WDA's alert endpoint does it headlessly, before the task."""
    from ..transport import TransportError
    for _ in range(attempts):
        try:
            text = driver.call("GET", "/alert/text", timeout=5)
        except TransportError:
            return
        if not text:
            return
        try:
            driver.call("POST", "/alert/accept", {"name": "Allow While Using App"}, timeout=5)
        except TransportError:
            driver.call("POST", "/alert/accept", {}, timeout=5)
        time.sleep(.5)


# MOBSTER_BENCH_SESSION: the capabilities of each task's WDA session. Every task starts a new session, so two arms of
# an A/B on one simulator never share one.
# - "product" (the default since 10 Oct): as the app makes it (drivers.resolve_wda_session): no quiescence wait, so the
#   bench measures the session users run. (A first SWIPE_UP on a live score feed still hung 95 s with it, 1 of 1
#   such runs on 10 Oct: that hang is the swipe's, not the session's.)
# - "legacy": WDA's default capabilities, as every run before 10 Oct had them.
SESSION_CAPABILITIES = {"product": {"platformName": "iOS", "shouldWaitForQuiescence": False, "waitForIdleTimeout": 0},
                        "legacy": {}}


def session_mode():
    mode = os.environ.get("MOBSTER_BENCH_SESSION", "").strip() or "product"
    if mode not in SESSION_CAPABILITIES:
        raise ValueError(f"MOBSTER_BENCH_SESSION={mode!r}: use product or legacy")
    return mode


def wda_session(wda_url, mode=None):
    """A new WDA session with ``mode``'s capabilities (session_mode() by default)."""
    mode = session_mode() if mode is None else mode
    if mode not in SESSION_CAPABILITIES:
        raise ValueError(f"MOBSTER_BENCH_SESSION={mode!r}: use product or legacy")
    request = urllib.request.Request(wda_url + "/session", method="POST",
                                     data=json.dumps({"capabilities": {"alwaysMatch": SESSION_CAPABILITIES[mode]}}).encode(),
                                     headers={"Content-Type": "application/json"})
    created = json.load(urllib.request.urlopen(request, timeout=30))
    return created.get("sessionId") or (created.get("value") or {}).get("sessionId")


def sim_restarter(udid, wda_url, mjpeg_url, xctestrun, log_path=None):
    """The simulator's WDA restarter (bench/sim_wda.py), or None without its xctestrun or in the open-source tree,
    which leaves restarting a runner to whoever started it."""
    if not xctestrun or not mjpeg_url:
        return None
    from urllib.parse import urlsplit
    try:
        from .sim_wda import SimulatorWDA
    except ImportError:
        return None
    return SimulatorWDA(udid, urlsplit(wda_url).port, urlsplit(mjpeg_url).port, xctestrun, log_path=log_path)


def frontier_agent(driver, names, emit, bundles, model=None, reasoning=None):
    """(FrontierAgent, client): engines.SMART_CONFIG, the app's Smart engine,
    with this harness's per-task cost cap. Switches already set in the environment win (ablations)."""
    from ..frontier import FrontierAgent, chat_client
    apply_switches(pin=False)
    client = chat_client(model or DEFAULT_FRONTIER_MODEL, reasoning=reasoning or SMART_CONFIG.reasoning)
    if os.environ.get("MOBSTER_BENCH_TTFT", "").lower() in ("1", "on", "true", "yes"):
        from .stream_timing import timed
        client = timed(client)  # the same request, streamed, to time the first token (both arms alike)
    return FrontierAgent(driver, client, apps=names, emit=emit,
                         **frontier_kwargs(max_cost_usd=task_cost_cap(bundles, client.model))), client


def run_task(task, *, repo, udid, manifest, wda_url, mjpeg_url, folder, policy="frontier", frontier_model=None,
             reasoning="low", wda_xctestrun=None):
    from ..catalog import app_label
    from ..compose import build_target_driver, close_all
    from ..frame_clock import attach_frame_clock
    from ..frontier import cost_usd

    folder = pathlib.Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    task_started = time.time()
    t_reset = time.monotonic()
    reset_info = reset(repo, udid, manifest)
    reset_s = time.monotonic() - t_reset

    bundles = [app_entry(manifest, app).get("bundle_id") for app in task.get("apps", [])]
    bundles = [b for b in bundles if b]
    costs, events = [], []

    def emit(event):
        kind = event.get("event")
        if kind == "frontier_prompt":
            # Kept out of task.json: prompts.jsonl, one line per turn, and the image the model saw as
            # screens/prompt-turn-NN.jpg, so a decision can be replayed offline (bench/replay.py).
            event = dict(event)
            image = event.pop("image", None)
            if image and image.startswith("data:image/jpeg;base64,"):
                name = f"prompt-turn-{event.get('step', 0):02d}.jpg"
                (folder / "screens").mkdir(parents=True, exist_ok=True)
                (folder / "screens" / name).write_bytes(base64.b64decode(image.split(",", 1)[1]))
                event["image"] = f"screens/{name}"
            with open(folder / "prompts.jsonl", "a", encoding="utf-8") as prompts:
                prompts.write(json.dumps(event, ensure_ascii=False) + "\n")
            return
        if kind == "inference_finished" and isinstance(event.get("cost_nanodollars"), int):
            costs.append(event["cost_nanodollars"] / 1e9)
        if kind in ("frontier_decision", "frontier_error", "frontier_refused", "frontier_action", "frontier_failed",
                    "frontier_chunk_stop", "frontier_checklist", "frontier_verify",
                    "frontier_text", "frontier_tuned", "frontier_wda_recovery",
                    "frontier_contract", "frontier_contract_update", "frontier_screen",
                    "frontier_open_url", "frontier_retry", "frontier_split",
                    "frontier_app_relaunched"):
            usage = event.get("usage")
            event = {k: v for k, v in event.items() if k != "usage"}
            if kind == "frontier_decision" and isinstance(usage, dict) and usage:
                event["tokens"] = turn_tokens(usage)
            trace = getattr(driver, "call_trace", None)
            if trace and kind in ("frontier_decision", "frontier_action", "frontier_failed"):
                # Every WDA request, read and judge screenshot since the last event: [kind, start ms, ms].
                items = trace[:]
                del trace[:len(items)]  # the verify read's thread may append meanwhile
                event["wda"] = [[name, round((at - started) * 1000), round(ms)] for name, at, ms in items]
            events.append(event)
            if kind == "frontier_decision" and recording is not None:
                recording.turn = event.get("step")
            return
        if kind in ("result", "error"):
            kept = {k: v for k, v in event.items() if isinstance(v, (str, int, float, bool)) or v is None}
            if isinstance(kept.get("text"), str):
                kept["text"] = kept["text"][:600]
            events.append(kept)

    shots = Shots(udid, folder / "screens")
    driver = recording = calls = None
    model_name = frontier_model or DEFAULT_FRONTIER_MODEL
    started = time.monotonic()
    try:
        session = wda_session(wda_url)
        driver = build_target_driver(wda_url=wda_url, session=session)
        driver.call_trace = shots.trace = []
        # A runner that dies mid-task is restarted as it was started, and its session taken the way this
        # harness takes one (frontier.FrontierAgent._recover).
        driver.restarter = sim_restarter(udid, wda_url, mjpeg_url, wda_xctestrun or os.environ.get(
            "MOBSTER_SIM_WDA_XCTESTRUN"), log_path=str(folder / "wda-restart.log"))
        driver.new_session = lambda: wda_session(wda_url)
        from ..app_killer import simulator as simulator_killer
        driver.killer = simulator_killer(udid)  # a stuck app WDA can't close is closed by simctl
        attach_frame_clock(driver, wda_url=wda_url, session=session, mode="on", mjpeg_url=mjpeg_url,
                           log_path=str(folder / "frameclock.jsonl"))
        clear_alerts(driver)
        driver.call("POST", "/wda/pressButton", {"name": "home"}, timeout=10)
        recording = RecordingDriver(driver, shots)
        if bundles:
            recording.call("POST", "/wda/apps/activate", {"bundleId": bundles[0]}, timeout=20)
        names = {b: app_label(b).split(" (")[0] for b in bundles}
        started, shot_seconds = time.monotonic(), shots.seconds
        agent, client = frontier_agent(recording, names, emit, bundles, model_name, reasoning)
        outcome = agent.run(task["goal"])
        costs.append(cost_usd(client.model, outcome["usage"]) or 0.0)
        calls = outcome["usage"].get("calls")  # FrontierAgent emits no inference_finished: one cost, many calls
        result = {"status": outcome["status"], "data": outcome.get("answer"),
                  "reason": outcome.get("reason", ""), "actions": outcome["actions"],
                  "decisions": outcome["steps"], "usage": outcome["usage"], "notes": outcome["notes"],
                  "timing": judge_shots_apart(outcome.get("timing"), shots.seconds - shot_seconds)}
    except Exception as error:
        result = {"status": "error", "reason": f"{type(error).__name__}: {error}"}
    finally:
        elapsed = time.monotonic() - started
        close_all(driver)
    steps = recording.steps if recording is not None else []
    final = shots.take("final")
    save_end_state(udid, bundles, folder, wda_url=wda_url, started=task_started)
    if steps:
        steps[-1]["post_action_screenshot"] = final
    else:
        steps = [{"step": 1, "screenshot": final, "actions": [], "post_action_screenshot": final}]
    answer = answer_text(result)
    if answer is not None:
        steps.append({"step": len(steps) + 1, "actions": [{"type": "stop", "answer": answer}],
                      "post_action_screenshot": final})
    (folder / "trajectory.json").write_text(json.dumps(steps, indent=1), encoding="utf-8")
    # "ok" means the agent ran to a result; iOSWorld's judge then decides success.
    record = {"task": task["name"], "goal": task["goal"], "apps": task.get("apps", []),
              "category": task.get("category"), "difficulty": task.get("difficulty"),
              "status": "ok", "agent_answer": answer,
              "mobster": {"status": result.get("status"), "reason": str(result.get("reason") or "")[:400],
                          "data_status": result.get("data_status"), "actions": result.get("actions"),
                          "decisions": result.get("decisions"),
                          "agent_seconds": round(max(0.0, elapsed - shots.seconds), 2),
                          "screenshot_seconds": round(shots.seconds, 2), "reset_seconds": round(reset_s, 1),
                          "reset": reset_info,
                          "cost_usd": round(sum(costs), 6),
                          "model_calls": calls if calls is not None else len(costs), "policy": policy,
                          "model": model_name, "usage": result.get("usage"), "build": build_id(),
                          "ttft": os.environ.get("MOBSTER_BENCH_TTFT", "") or None,
                          "wda_session": session_mode(),
                          "notes": result.get("notes"), "timing": result.get("timing"),
                          "events": events[-400:]}}
    (folder / "task.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record


# -- the run, the judge, the report -------------------------------------------------------

def load_envs(paths):
    from ..config import load_env_file
    for path in paths or ():
        load_env_file(os.path.expanduser(path))


def cmd_run(args):
    # OPENAI_API_KEY goes into the apps' UserDefaults, as iOSWorld's runner does (its apps' in-app replies).
    load_envs(args.env_file)
    tasks = all_tasks = load_tasks(args.repo)
    all_names = [t["name"] for t in all_tasks]  # folder numbers follow the full task list
    if args.only:
        wanted = set(args.only.split(","))
        tasks = [t for t in tasks if t["name"] in wanted]
    if args.limit:
        tasks = tasks[:args.limit]
    manifest = load_manifest(args.repo)
    register_names(manifest)
    check_isolation(args.udid, manifest)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        folder = out / f"{all_names.index(task['name']) + 1:0{TASK_DIR_WIDTH}d}-{task['name']}"
        if (folder / "task.json").exists() and not args.force:
            print(f"skip {task['name']} (done)", flush=True)
            continue
        try:
            record = run_task(task, repo=args.repo, udid=args.udid, manifest=manifest, wda_url=args.wda_url,
                              mjpeg_url=args.mjpeg_url, folder=folder, policy=args.policy, frontier_model=args.model,
                              reasoning=args.reasoning, wda_xctestrun=args.wda_xctestrun)
        except RuntimeError as error:
            # A failed reset is the harness's, not the agent's: no task.json, so a rerun retries it,
            # and the rest of this simulator's queue still runs.
            print(f"{task['name']:24} NOT RUN (reset failed: {str(error)[:120]})", flush=True)
            continue
        m = record["mobster"]
        print(f"{task['name']:24} {m['status']:24} {m['agent_seconds']:6.1f}s ${m['cost_usd']:.4f} "
              f"steps={m['decisions']} answer={str(record['agent_answer'])[:80]!r}", flush=True)


def cmd_pool(args):
    """One ``run`` per simulator (UDID:WDA-port:MJPEG-port), the tasks dealt round-robin."""
    tasks = [t["name"] for t in load_tasks(args.repo)]
    if args.only:
        wanted = set(args.only.split(","))
        tasks = [t for t in tasks if t in wanted]
    if args.limit:
        tasks = tasks[:args.limit]
    workers = [spec.split(":") for spec in args.sims.split(",")]
    if len({udid for udid, _, _ in workers}) < len(workers):
        raise SystemExit("--sims lists a simulator twice")
    manifest = load_manifest(args.repo)
    for udid, _, _ in workers:  # before any worker starts: one shared simulator taints every worker's results
        check_isolation(udid, manifest)
    procs = []
    for index, (udid, port, mjpeg) in enumerate(workers):
        share = tasks[index::len(workers)]
        if not share:
            continue
        command = [sys.executable, "-u", "-m", "mobile_agent.bench.iosworld", "run", "--repo", args.repo,
                   "--udid", udid, "--wda-url", f"http://127.0.0.1:{port}", "--mjpeg-url", f"http://127.0.0.1:{mjpeg}",
                   "--out", args.out, "--only", ",".join(share)]
        for env_file in args.env_file or ():
            command += ["--env-file", env_file]
        command += ["--policy", args.policy, "--reasoning", args.reasoning] + (["--model", args.model] if args.model else [])
        if args.wda_xctestrun:
            command += ["--wda-xctestrun", args.wda_xctestrun]
        if args.force:
            command.append("--force")
        log = open(pathlib.Path(args.out).with_suffix(f".worker{index}.log"), "w")
        procs.append((subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT), log))
    for proc, log in procs:
        proc.wait()
        log.close()
    return max((proc.returncode for proc, _ in procs), default=0)


def cmd_judge(args):
    """iOSWorld's own judge, unmodified, in its own environment."""
    load_envs(args.env_file)  # the judge reads OPENAI_API_KEY from this process's environment
    python = pathlib.Path(args.repo) / ".venv" / "bin" / "python"
    command = [str(python if python.exists() else sys.executable), "scripts/judge_trajectories.py",
               "--run-dir", str(pathlib.Path(args.run).resolve())]
    if args.force:
        command.append("--re-judge")  # judge_trajectories.py has no --force
    code = subprocess.call(command, cwd=args.repo, env=dict(os.environ))
    # The judge's own summary counts passes by the official rule; this adds where its success flag differs.
    from .iosworld_stats import load_runs, report_lines
    for line in report_lines(load_runs([args.run])):
        print(line)
    return code


def cmd_report(args):
    """One run folder: official passes (iOSWorld's every-criterion rule), the judge's own flag where
    it disagrees, by category and difficulty, then time and cost."""
    from .iosworld_stats import load_runs, report_lines
    runs = load_runs([args.run])
    for line in report_lines(runs):
        print(line)
    judged = [r for r in runs if r.judged]
    for key in ("category", "difficulty"):
        groups = {}
        for r in judged:
            groups.setdefault(getattr(r, key), []).append(r.official)
        print(key + ": " + ", ".join(f"{k} {sum(v)}/{len(v)}" for k, v in sorted(groups.items(), key=str)))
    from ..frontier import cost_usd
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(pathlib.Path(args.run).glob("*/task.json"))]
    seconds = [r["mobster"]["agent_seconds"] for r in rows]
    costs = [r["mobster"]["cost_usd"] or cost_usd(r["mobster"].get("model"), r["mobster"].get("usage")) or 0.0
             for r in rows]
    if seconds:
        print(f"agent seconds p50 {statistics.median(seconds):.1f}, mean {statistics.mean(seconds):.1f}; "
              f"$/task mean {statistics.mean(costs):.4f}, total ${sum(costs):.2f}")


def _run_dirs(patterns):
    """Run folders from arguments that may be globs (a folder holds NNN-task/task.json)."""
    dirs = []
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)) or [pattern]:
            if pathlib.Path(path).is_dir() and path not in dirs:
                dirs.append(path)
    return dirs


def cmd_compare(args):
    """A paired A/B: arm A's run folders against arm B's, on the tasks both judged."""
    from .iosworld_stats import compare, format_compare, load_overrides, load_runs, read_worker_logs
    overrides = load_overrides(args.commit_labels)
    arms = {arm: _run_dirs(getattr(args, arm)) for arm in ("a", "b")}
    runs = {arm: load_runs(dirs, model=args.model, overrides=overrides) for arm, dirs in arms.items()}
    logs = {}
    for arm, dirs in arms.items():
        merged = {"not_run": [], "tracebacks": 0}
        for folder in dirs:
            info = read_worker_logs(folder)
            merged["not_run"] += info["not_run"]
            merged["tracebacks"] += info["tracebacks"]
        logs[arm] = merged
    result = compare(runs["a"], runs["b"], n_boot=args.bootstrap, seed=args.seed)
    if args.json:
        print(json.dumps({**result, "logs": logs, "arms": arms}, indent=1, default=str))
        return 0
    print(f"A: {', '.join(arms['a'])}\nB: {', '.join(arms['b'])}\n")
    print(format_compare(result, args.a_name, args.b_name, logs=logs))
    return 0


def cmd_estimate(args):
    """The official pass rate over any set of run folders, reweighted to iOSWorld's 133-task mix."""
    from .iosworld_stats import FULL_MIX, estimate, format_estimate, load_runs
    mix = dict(FULL_MIX)
    if args.repo:
        mix = {c: 0 for c in FULL_MIX}
        for task in load_tasks(args.repo):
            mix[task.get("category")] = mix.get(task.get("category"), 0) + 1
    runs = load_runs(_run_dirs(args.runs), model=args.model)
    for weighting in ("task", "run") if args.weighting == "both" else (args.weighting,):
        result = estimate(runs, mix=mix, weighting=weighting, n_boot=args.bootstrap, seed=args.seed)
        if args.json:
            print(json.dumps(result, indent=1, default=str))
        else:
            print(format_estimate(result))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--repo", required=True)
    run.add_argument("--udid", required=True)
    run.add_argument("--wda-url", required=True)
    run.add_argument("--mjpeg-url")
    run.add_argument("--out", required=True)
    run.add_argument("--only")
    run.add_argument("--limit", type=int)
    run.add_argument("--force", action="store_true")
    run.add_argument("--env-file", action="append")
    run.add_argument("--policy", choices=("frontier",), default="frontier")
    run.add_argument("--model")
    run.add_argument("--reasoning", default=SMART_CONFIG.reasoning)
    run.add_argument("--wda-xctestrun", help="the simulator runner's .xctestrun: a runner that dies is restarted "
                     "(also MOBSTER_SIM_WDA_XCTESTRUN)")
    pool = sub.add_parser("pool")
    pool.add_argument("--repo", required=True)
    pool.add_argument("--sims", required=True, help="UDID:WDA_PORT:MJPEG_PORT,...")
    pool.add_argument("--out", required=True)
    pool.add_argument("--only")
    pool.add_argument("--limit", type=int)
    pool.add_argument("--force", action="store_true")
    pool.add_argument("--env-file", action="append")
    pool.add_argument("--policy", choices=("frontier",), default="frontier")
    pool.add_argument("--model")
    pool.add_argument("--reasoning", default=SMART_CONFIG.reasoning)
    pool.add_argument("--wda-xctestrun")
    judge = sub.add_parser("judge")
    judge.add_argument("--repo", required=True)
    judge.add_argument("--run", required=True)
    judge.add_argument("--force", action="store_true")
    judge.add_argument("--env-file", action="append")
    report = sub.add_parser("report")
    report.add_argument("--run", required=True)
    ab = sub.add_parser("compare", help="paired A/B over two sets of run folders (offline)")
    ab.add_argument("--a", nargs="+", required=True, help="arm A's run folders (globs allowed)")
    ab.add_argument("--b", nargs="+", required=True, help="arm B's run folders (globs allowed)")
    ab.add_argument("--a-name", default="A")
    ab.add_argument("--b-name", default="B")
    ab.add_argument("--model", help="only runs of this model")
    ab.add_argument("--commit-labels", help='JSON {task: {control label: true|false}}: hand labels for "requested"')
    ab.add_argument("--bootstrap", type=int, default=10000)
    ab.add_argument("--seed", type=int, default=20260926)
    ab.add_argument("--json", action="store_true")
    pooled = sub.add_parser("estimate", help="official pass rate reweighted to the 133-task mix (offline)")
    pooled.add_argument("runs", nargs="+", help="run folders (globs allowed)")
    pooled.add_argument("--model", help="only runs of this model, e.g. gpt-5.6-sol")
    pooled.add_argument("--repo", help="iOSWorld checkout: take the category mix from its tasks.json")
    pooled.add_argument("--weighting", choices=("task", "run", "both"), default="both")
    pooled.add_argument("--bootstrap", type=int, default=10000)
    pooled.add_argument("--seed", type=int, default=20260926)
    pooled.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    return {"run": cmd_run, "pool": cmd_pool, "judge": cmd_judge, "report": cmd_report, "compare": cmd_compare,
            "estimate": cmd_estimate}[args.command](args) or 0


if __name__ == "__main__":
    sys.exit(main())

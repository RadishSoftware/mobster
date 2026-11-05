"""One `mobster verify` run: the verdict comes from the assertions, never a model; mobile_agent.sim loads lazily."""

import json
import re
import threading
import time
import traceback
from pathlib import Path

from .. import project_env
from ..transport import request_once
from ..wda import timeouts as wda_timeouts
from . import device_audit, snapshots, wire
from .a11y import NAME as A11Y
from .a11y import Collector as A11yCollector
from .actions import ActionError, ActionJournal, ActionNotDispatched
from .assertions import evaluate_on
from .business_run import BusinessRun
from .checks import project_root
from .flow import Flow
from .l10n import Collector as L10nCollector
from .replay_admission import ReplayAdmission
from .run_cleanup import RunCleanup
from .run_links import RunLinks
from .run_perf import PerfMeasure
from .run_secrets import ProjectSecrets
from .runs import make_run_dir
from .screen_coverage import ScreenCoverage
from .smart import is_real_device, no_smart_key
from .sound import Signal as SoundSignal
from .tree import GLANCE_SOURCE_PATH, TreeError
from .tree import read_tree as read_tree_from
from .verdict import CRASH_EXPLAINS, CouldntRun, Verdict, mobster_version

MODES = ("smart", "keyless", "launch")
FOREGROUND_WAIT_SECONDS = 15
BASELINE_SETTLE_SECONDS = 3
SETTLE_FIRST_PAUSE, SETTLE_PAUSE = .3, .5
FRAME_QUALITY = 75
CRASH_FIX = "The report's Logs and Crash pane shows the stack and the app's log around the crash."


def _now():
    return time.monotonic()


def _pause(seconds):
    time.sleep(seconds)


def wda_session(wda_url):
    # Plain capabilities, the benchmark's way (bench/iosworld.wda_session): Smart's simulator numbers were measured so.
    status = request_once(wda_url, "GET", "/status", timeout=10)
    if status.get("sessionId"):
        return status["sessionId"]
    created = request_once(wda_url, "POST", "/session", {"capabilities": {"alwaysMatch": {}}}, timeout=30)
    return created["sessionId"]


def front_app(wda_url, timeout=5):
    # An app with a big tree can hold this read for 17-50 s, so it never waits past the probe bound.
    timeout = min(timeout, wda_timeouts.WDA_CALL_TIMEOUTS["probe"])
    value = request_once(wda_url, "GET", "/wda/activeAppInfo", timeout=timeout).get("value")
    value = value if isinstance(value, dict) else {}
    bundle, pid = value.get("bundleId"), value.get("pid")
    return (bundle if isinstance(bundle, str) and bundle else None,
            pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None)


def build_driver(target, manager, run_dir, mode):
    own = getattr(manager, "build_driver", None)  # a manager that is not the simulator's (Android) builds its own
    if own is not None:
        return own(target, run_dir, mode)
    from ..compose import build_target_driver
    from ..wda.frame_clock import attach_frame_clock
    url = target.wda_url
    session = wda_session(url)
    driver = build_target_driver(wda_url=url, session=session)
    try:
        driver.restarter = manager.restarter(target)
    except Exception:
        driver.restarter = None  # recovery then waits for the runner instead of restarting it
    driver.new_session = lambda: wda_session(url)
    if mode != "launch":
        attach_frame_clock(driver, wda_url=url, session=session, mjpeg_url=target.mjpeg_url,
                           log_path=str(Path(run_dir) / "frameclock.jsonl"))
    if mode != "smart":
        driver.tune(rich=True, glide=True)
    return driver


def _sim_error(error):
    """A simulator manager error (duck-typed by ``kind``) as CouldntRun."""
    kind = getattr(error, "kind", None)
    if isinstance(kind, str):
        return CouldntRun(kind, str(error), getattr(error, "fix", "") or "")
    return None


class VerifyRun(ScreenCoverage, Flow, BusinessRun, ReplayAdmission, RunCleanup, RunLinks, ProjectSecrets,
                PerfMeasure, Verdict):
    """One run. run_id and run_dir exist from __init__, so the MCP server hands out the id before prepare() ends."""

    def __init__(self, check, *, mode, runs_dir, manager=None, progress=None, key=None, assert_timeout=5,
                 replay="off", simctl=None, business_binding=None):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        self.check, self.mode, self.manager, self.key = check, mode, manager, key
        self.assert_timeout = assert_timeout
        self._progress = progress
        self.runs_dir = Path(runs_dir).expanduser().resolve()
        self.run_id, self.run_dir = make_run_dir(self.runs_dir)
        self.actions = ActionJournal(self.run_dir)
        self.state = "new"
        self.secrets = {}           # {NAME: value} of the project's ${NAME}s this check uses (project_env.py)
        self._live = check          # the check with those expanded: only launches and Smart's request read it
        self.steps = []
        self.error = None
        self.lock = threading.RLock()
        self.driver = self.target = self.app = self.lease = None
        self.frames = []            # every frame, relative to run_dir, in order
        self.step_frames = []       # the step frames among them (the 60-frame cap applies to these)
        self.timing = {}
        self.baseline = {"taken": False, "held": []}
        self.business_binding = business_binding
        self.business_fixture = None
        self.business_result = ({"adapter": check.business.adapter, "fixture": check.business.fixture,
                                 "status": "unavailable", "schemaVersion": "mobster.business/1",
                                 "assertions": [], "setup": "not_started", "cleanup": "not_needed"}
                                if check.business is not None else None)
        self._business_baseline = None
        self._business_cleanup_done = False
        self.agent = None           # Smart's {status, note, model, turns}
        self.cost_usd = 0.0
        self.result = None
        self._t0 = time.monotonic()
        self._t0_wall = time.time()  # the same moment on the wall clock: log lines and crash reports carry it
        self.evidence = None        # the app's log and crash reports (evidence.py), from just before the launch
        self._failed_ms = self._verdict_ms = None
        self._ready_at = None
        self._launch = {}           # perf: interactiveMs and foregroundMs, from the launch call (perf.py)
        self._sampler = None
        self._app_pid = None
        self.perf = None            # perf.build, once the verdict read starts
        self._frame_n = 0
        self._frame_width = None
        self._finish_lock = threading.Lock()
        self._stop = threading.Event()
        self._smart_status = None
        self._smart_reason = ""
        self._left_app = None       # on a real device: the app Smart opened outside the check's (StayInApp)
        self._model_failed = False
        self._write_lock = threading.Lock()
        self._driver_closed = False
        self._preparing = False     # prepare() is in flight on some thread
        self._release_lock = threading.Lock()  # a leaf lock: nothing is taken while it is held
        self._awaiting_frame = []   # Smart steps whose after-frame hasn't been taken yet
        self._held_somewhere = set()
        self._unframed_findings = {}
        self._screen_coverage_lock = threading.RLock()
        self._screen_frames_pending = 0
        self._screen_frames_sealed = False
        self._screen_result_sealed = False
        self._screen_coverage_final = None
        self._verdict_thread = None
        self._screen_verdict_audited = False
        self._screen_coverage_issue = None
        self.source_observation = None
        level = "strict" if A11Y in check.audit else check.a11y
        self._a11y = A11yCollector((check.conditions or {}).get("content_size"), level=level,
                                   baseline=check.a11y_baseline, scope=check.a11y_scope) if level else None
        self._ax_device = device_audit.for_run(self, check.audit)
        self._sound = SoundSignal(check.sound, self) if check.sound else None
        locale = (check.conditions or {}).get("locale")
        self._l10n = L10nCollector(locale) if check.l10n and locale else None
        self._simctl = simctl       # conditions.simctl, or a fake in tests
        self._wire = None           # wire.Judge when the check has request, no_request or storage expectations
        self._conditions = None     # conditions.Applier, once the device is held and the app is in place
        self.dialogs = None         # dialogs.Policy, once the device is held: how this run answers iOS's own dialogs
        self.snapshot = None        # snapshot_run.take's result: the screen against its baseline
        self._status_bar_set = False  # the status bar is overridden (9:41, full bars), so a snapshot needn't mask it
        self._conditions_restored = False
        self.conditions_cleanup = "not_needed"
        self.findings = []          # audit findings and screens that broke `throughout` (see _findings)
        self._replay_drift = None   # a replay that stopped at a changed step: the failure's sentence
        self.replay = None
        if replay != "off" and mode == "smart":
            from ..replay_core.checks import CheckReplay
            self.replay = CheckReplay(self, replay)
        self.log(f"Mobster {mobster_version()} verify, run {self.run_id}, mode {mode}"
                 + (f", replay {replay}" if self.replay is not None else ""))

    def _scrub(self, text):
        if self.secrets:
            text = project_env.mask_text(text, self.secrets)
        return text.replace(self.key, "[key]") if self.key and isinstance(text, str) else text

    def log(self, line):
        with self._write_lock:
            try:
                with open(self.run_dir / "run.log", "a", encoding="utf-8") as handle:
                    handle.write(f"{time.strftime('%H:%M:%S')} {self._scrub(line)}\n")
            except OSError:
                pass

    def progress(self, line):
        """A progress line: stderr (through the caller's ``progress``) and run.log."""
        self.log(line)
        if self._progress is not None:
            try:
                self._progress(self._scrub(line))
            except Exception:
                pass

    def event(self, event):
        """One line of events.jsonl: Smart's frontier events (no prompts, no images) or key-less actions."""
        with self._write_lock:
            try:
                line = self._scrub(json.dumps(project_env.mask_deep(event, self.secrets), ensure_ascii=False,
                                              default=str))
                with open(self.run_dir / "events.jsonl", "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except (OSError, TypeError, ValueError):
                pass

    def _sleep(self, seconds):
        if seconds > 0:
            _pause(seconds)

    def _ms(self):
        return round((time.monotonic() - self._t0) * 1000)

    def _check_stop(self):
        if self._stop.is_set():
            raise CouldntRun("stopped", "The run was stopped")

    def _fail(self, error):
        self.error = {"klass": error.klass, "message": error.message, "fix": error.fix}
        if self._failed_ms is None:
            self._failed_ms = self._ms()
        self.log(f"couldn't run ({error.klass}): {error.message}")
        return error

    def _manager(self):
        if self.manager is None:
            try:
                from ..sim import SimulatorManager
            except ImportError as error:
                if not str(getattr(error, "name", "") or "").startswith(__package__.rsplit(".", 1)[0] + ".sim"):
                    raise
                raise CouldntRun("environment", "This build of Mobster has no simulator manager.",
                                 "Install the full Mobster CLI (docs/verify.md).") from None
            self.manager = SimulatorManager(progress=self.progress)
        return self.manager

    def _call(self, what, *args, **kwargs):
        """A manager call; its SimError becomes CouldntRun."""
        try:
            return what(*args, **kwargs)
        except ActionNotDispatched:
            raise
        except CouldntRun:
            raise
        except Exception as error:
            converted = _sim_error(error)
            if converted is not None:
                raise converted from None
            raise

    def prepare(self):
        # abort() decides under _finish_lock who releases the simulator, so each stop check and the state change it
        # guards are one step under that lock: an abort between them once left the lease held and the driver open.
        with self._finish_lock:
            if self._stop.is_set() or self.result is not None:  # the abort that stopped it writes the result
                raise CouldntRun("stopped", "The run was stopped")
            self.state = "preparing"
            self._preparing = True
        try:
            try:
                self._prepare()
            except CouldntRun as error:
                error = self._cleanup_after_run() or error
                if self.result is None:  # an abort's result already carries its own reason
                    self._fail(error)
                self._release()
                raise error from None
            except ActionError as action_error:
                error = self._cleanup_after_run() or CouldntRun(action_error.klass, str(action_error))
                if self.result is None:
                    self._fail(error)
                self._release()
                raise error from None
            except Exception as unexpected:
                self.log("".join(traceback.format_exception(type(unexpected), unexpected,
                                                            unexpected.__traceback__)))
                failure = self._cleanup_after_run()
                if failure is not None and self.result is None:
                    self._fail(failure)
                self._release()
                if failure is not None:
                    raise failure from None
                raise
            except BaseException:
                self._cleanup_after_run()
                self._release()
                raise
            with self._finish_lock:
                if self._stop.is_set() or self.result is not None:
                    self._cleanup_after_run()
                    self._release()
                    stopped = CouldntRun("stopped", "The run was stopped")
                    raise self._fail(stopped) if self.result is None else stopped  # an abort's result stands
                self.state = "ready"
                self._ready_at = time.monotonic()
        finally:
            self._preparing = False

    def _prepare(self):
        from . import conditions
        if conditions.is_matrix(self.check.conditions):   # before any simulator is touched
            raise CouldntRun("usage", "This check lists several appearances, text sizes or locales, and one run has "
                                      "one device state.", "Run it with `mobster test`, which runs each "
                                      "combination, or write one value.")
        self._apply_fixtures()
        self._load_secrets()
        check = self._live
        self._load_business()
        self._pin_assets()
        manager = self._manager()
        if is_real_device(manager):
            self._refuse_fixtures_on_phone()
            if self.business_binding is not None:
                raise CouldntRun("business", "App-state fixtures require a managed simulator.")
        started = time.monotonic()
        try:
            with self.actions.action("ACQUIRE"):
                self.lease = self._call(manager.acquire, check.device, check.runtime,
                                        cancelled=self._stop.is_set)
        except ActionNotDispatched as rejected:
            raise _sim_error(rejected.error) or rejected.error from None
        self.target = self.lease.target
        from .dialogs import for_run
        self.dialogs = for_run(self.check, manager, self.target, actions=self.actions)
        self.timing["simulator"] = round(time.monotonic() - started, 2)
        self.log(f"simulator {self.target.name} {self.target.udid} {self.target.wda_url}")
        self._check_stop()

        started = time.monotonic()
        if check.app_path:
            self.app = self._call(manager.app_info, check.app_path)
            if check.bundle_id and check.bundle_id != self.app.bundle_id:
                raise CouldntRun("install", f"{check.app_path} is {self.app.bundle_id}, not {check.bundle_id}.",
                                 "Pass the .app of the app the check names, or drop the bundle ID.")
        else:
            self.app = self._call(manager.installed_app, self.target, check.bundle_id)
        bundle = self.app.bundle_id
        if self.business_binding is not None and bundle != self.business_binding.bundle:
            raise CouldntRun("business", "The installed app does not match the enrolled app-state adapter.",
                             "Enroll a fixture for the app's actual bundle ID.")
        self._pin_replay_before_effects()
        # The log starts while the app installs, so its process is up before the launch (and never competes with it).
        self._start_evidence()
        system = getattr(manager, "system_prefixes", ("com.apple.",))  # apps that ship with the OS keep their data
        level = check.reset_level() if not bundle.startswith(system) else "none"
        if check.app_path:
            self.progress(f"Installing {self.app.name} {self.app.version}")
            if level == "reinstall":
                with self.actions.action("RESET"):
                    self._call(manager.reset, self.target, bundle, "reinstall", app_path=check.app_path)
            else:
                with self.actions.action("INSTALL"):
                    self.app = self._call(manager.install, self.target, check.app_path)
                if level == "data":
                    with self.actions.action("RESET"):
                        self._call(manager.reset, self.target, bundle, "data")
        elif level != "none":
            with self.actions.action("RESET"):
                self._call(manager.reset, self.target, bundle, level)
        self.log(f"app {bundle} {self.app.version}, reset {level}")
        self._seed(manager, bundle)
        self._setup_business(manager)
        if self.app.icon:
            (self.run_dir / "app-icon.png").write_bytes(self.app.icon)
        self.timing["install"] = round(time.monotonic() - started, 2)
        self._check_stop()

        launch_args = self._set_conditions(manager, bundle)
        started, launched = time.monotonic(), _now()
        stale = self._stale_process()
        # The driver's session and settings are made while simctl launches the app (28 Sep: 0.3 s and 0.5-0.8 s).
        built = {}

        def build():
            try:
                with self.actions.action("NEW_SESSION"):
                    built["driver"] = build_driver(self.target, manager, self.run_dir, self.mode)
            except BaseException as error:  # handed to the preparing thread below
                built["error"] = error
        builder = threading.Thread(target=build, name="mobster-verify-driver", daemon=True)
        builder.start()
        try:
            if self.business_fixture is not None:
                launch_args = (*launch_args, "-MobsterBusinessRun", self.business_fixture.token)
            with self.actions.action("LAUNCH"):
                self._call(manager.launch, self.target, bundle, launch_args, self._launch_env())
        finally:
            builder.join()
            self.driver = built.get("driver")
        if self.driver is not None:
            self.actions.guard_driver(self.driver)
            if check.throughout or check.somewhere or self._a11y or self._l10n:
                from .source_observation import SourceObservation
                self.source_observation = SourceObservation(self)
                self.source_observation.attach(self.driver)
        error = built.get("error")
        if isinstance(error, CouldntRun):
            raise error
        if error is not None:
            raise CouldntRun("wda", f"WebDriverAgent didn't answer on the simulator ({type(error).__name__}).",
                             "Run `mobster sim doctor --fix`, then try again.") from None
        in_front = self._await_foreground(stale)
        self._measure_launch(launched, in_front)
        self._send_push()
        if check.open_url:
            self._open_link(check.open_url)
        self.timing["launch"] = round(time.monotonic() - started, 2)
        from ..intents import harvest  # the link atlas fills as the simulator is ready (dark; never raises)
        harvest.on_device_ready(self.target.udid, self.log)
        self._check_stop()
        self.frame("launch")
        if self._wire is not None:
            self._wire.baseline()
        if check.steps:
            self._take_baseline()
        if self._sound is not None:
            self._sound.start()

    def _apply_fixtures(self):
        """Flatten the check's fixtures (.mobster/fixtures/) into it, or refuse before any simulator is touched."""
        from . import fixtures
        try:
            self.check = self._live = fixtures.for_run(self.check, self.runs_dir)
        except fixtures.FixtureError as error:
            raise CouldntRun("usage", str(error), "`mobster fixtures list` shows the project's fixtures.") from None
        if self.check.fixture:
            self.log(f"fixtures: {', '.join(self.check.fixture)}")

    def _refuse_fixtures_on_phone(self):
        from . import fixtures
        try:
            fixtures.refuse_on_phone(self.check)
        except fixtures.FixtureError as error:
            raise CouldntRun("environment", str(error), fixtures.PHONE_FIX) from None

    def _seed(self, manager, bundle):
        """Put the fixtures' keychain reset, files and pasteboard on the simulator,
        after the reset and before the launch."""
        if not self.check.seed:
            return
        if getattr(manager, "platform", "ios") != "ios":
            raise CouldntRun("environment", "Fixtures seed iOS simulators only.",
                             "Remove seed from the check, or run it on an iOS simulator with --sim.")
        from . import conditions, fixtures
        try:
            did = fixtures.seed_simulator(self.check.seed, udid=self.target.udid, bundle=bundle,
                                          run=self._simctl or conditions.simctl, actions=self.actions)
        except fixtures.FixtureError as error:
            raise CouldntRun("environment", str(error), "Fix the fixture, then run the check again.") from None
        self.log(f"fixtures seeded: {', '.join(did) or 'nothing'}")

    def _set_conditions(self, manager, bundle):
        # A condition that can't be set is couldn't run: the check never runs in the wrong state.
        from . import conditions
        args = tuple(self._live.launch_args)
        spec = dict(self.check.conditions or {})
        own = getattr(manager, "conditions", None)  # a manager that is not the simulator's sets its own
        device = manager if getattr(manager, "real_device", False) else None
        if self.check.snapshot and device is None and own is None and "status_bar" not in spec:
            spec["status_bar"] = snapshots.SNAPSHOT_STATUS_BAR   # the same bar in every baseline
        self._status_bar_set = device is None and own is None and "status_bar" in spec
        if not (spec or self.check.stubs or self.check.wire):
            return args
        if own is not None:
            if self.check.stubs or self.check.wire:   # the proxy, shim and container reads are an iOS simulator's
                raise CouldntRun("environment", "Request stubs and wire assertions run on an iOS simulator only, so "
                                 "this check can't run here.", "Run it on a simulator, or remove them from the check.")
            self._conditions = own(spec, self.target, bundle, actions=self.actions)
        else:
            self._conditions = conditions.Applier(spec, udid=self.target.udid, bundle=bundle,
                                                  device=device, run=self._simctl or conditions.simctl,
                                                  actions=self.actions, stubs=self.check.stubs,
                                                  watch=wire.watch_hosts(self.check.wire))
        self._wire = wire.Judge(self.check.wire, run=self._simctl or conditions.simctl, udid=self.target.udid,
                                bundle=bundle) if self.check.wire else None
        self.conditions_cleanup = "pending"
        try:
            self._conditions.before_launch()
        except conditions.ConditionError as error:
            raise CouldntRun("environment", error.message, error.fix) from None
        for item in self._conditions.skipped:
            self.log(f"condition {item['key']} skipped: {item['why']}")
        self.log(f"conditions set: {', '.join(self._conditions.applied) or 'none'}")
        return args + self._conditions.launch_args()

    def _launch_env(self):
        # The shim's variables win over the check's own launch_env: a check can't turn its network conditions off.
        return {**self._live.launch_env, **(self._conditions.launch_env() if self._conditions is not None else {})}

    def _send_push(self):
        if self._conditions is None:
            return
        from . import conditions
        try:
            self._conditions.after_launch()
        except conditions.ConditionError as error:
            raise CouldntRun("environment", error.message, error.fix) from None

    def _stale_process(self):
        """The app's process ID when it is WDA's foreground app now, before a launch replaces it; else None."""
        bundle, pid = self._front()
        return pid if bundle == self.app.bundle_id else None

    def _await_foreground(self, stale=None):
        # Just after a launch WDA can name the replaced process, or the app with no process (28 Sep, Settings reruns).
        bundle = self.app.bundle_id
        started = _now()
        deadline = started + FOREGROUND_WAIT_SECONDS
        while True:
            remaining = deadline - _now()
            front, pid = self._front(timeout=max(1.0, remaining))
            if front == bundle and (stale is None or pid not in (None, stale)):
                if stale is not None:
                    self.log(f"{bundle} in front in process {pid} (was {stale}) after "
                             f"{_now() - started:.2f} s")
                return True
            if _now() >= deadline:
                self.log(f"{bundle} was not in front {FOREGROUND_WAIT_SECONDS} s after launch"
                         + (f" (still process {stale})" if front == bundle else ""))
                return False
            self._sleep(.2)

    def _front(self, timeout=2):
        """WDA's foreground app as (bundle ID, process ID); (None, None) when WDA doesn't answer."""
        try:
            own = getattr(self.manager, "front_app", None)
            bundle, pid = own(self.target) if own is not None else front_app(self.target.wda_url, timeout=timeout)
        except Exception:
            return None, None
        if self.app is not None and bundle == self.app.bundle_id:
            self._app_pid = pid
            if self.evidence is not None:
                self.evidence.saw(pid)
        return bundle, pid

    def _start_evidence(self):
        # Evidence never costs a run: a failure here is logged and the run goes on without it.
        from . import evidence
        try:
            root = project_root(self.run_dir) if self.runs_dir.parent.name == ".mobster" else None
            self.evidence = evidence.for_run(self.manager, self.target, self.app, self.run_dir,
                                             t0_wall=self._t0_wall, project_root=root, log=self.log)
            if self.evidence is not None:
                self.evidence.start()
        except Exception as error:
            self.evidence = None
            self.log(f"evidence: not started ({type(error).__name__}: {error})")

    def _stop_evidence(self):
        if self.evidence is not None:
            try:
                self.evidence.stop()
            except Exception as error:
                self.log(f"evidence: stop failed ({type(error).__name__})")

    def _crash(self, *, suspect):
        """The app's crash in this run (evidence.Evidence.crash), or None."""
        if self.evidence is None:
            return None
        try:
            return self.evidence.crash(suspect=suspect)
        except Exception as error:
            self.log(f"evidence: crash lookup failed ({type(error).__name__}: {error})")
            return None

    def _crashed(self, crash, decided=None):
        """The verdict for a run whose app crashed: failed, class app_crashed, the crash in one sentence."""
        from .crashes import headline
        decided = decided or {"results": [], "tree": None, "stable": None, "frame": None, "alert": None}
        return {**decided, "verdict": "failed", "klass": "app_crashed",
                "message": headline(crash, self.app.name if self.app is not None else "The app"), "fix": CRASH_FIX}

    def _foreground(self, timeout=5):
        try:
            return self.driver.active_app(timeout=timeout)
        except Exception:
            return None

    def _take_baseline(self):
        # The first glance that agrees with the one before it (at most 3 s), so a launch screen isn't the flow's start.
        deadline = _now() + BASELINE_SETTLE_SECONDS
        tree = self.read_tree(glance=True)
        while _now() < deadline:
            self._sleep(SETTLE_FIRST_PAUSE)
            again = self.read_tree(glance=True)
            same = again.layout() == tree.layout()
            tree = again
            if same:
                break
        tree = self.read_tree()
        held = [result.ok for result in evaluate_on(self.check.expect, tree)]
        self.baseline = {"taken": True, "held": held}
        self._take_business_baseline()
        self.log(f"baseline: {sum(held)} of {len(held)} expectations held before the flow")

    def read_tree(self, glance=False):
        """One evidence read of the app's tree; a glance (no ``visible``) never decides and falls back to one."""
        if self.driver is None:
            raise CouldntRun("wda", "The run has no WebDriverAgent connection yet")
        try:
            if glance:
                try:
                    tree = read_tree_from(self.driver, pause=self._sleep, path=GLANCE_SOURCE_PATH)
                    tree.glance = True
                    return tree
                except Exception:
                    pass  # fail closed: the evidence read below decides, or fails the run
            return read_tree_from(self.driver, pause=self._sleep)
        except TreeError as error:
            raise CouldntRun("wda", str(error)) from None
        except CouldntRun:
            raise
        except Exception as error:
            raise CouldntRun("wda", f"WebDriverAgent stopped answering ({type(error).__name__}).",
                             "Run `mobster sim doctor --fix`, then try again.") from None

    def _frame_path(self, label):
        self._frame_n += 1
        label = re.sub(r"[^a-z0-9-]+", "-", str(label).lower()).strip("-") or "frame"
        return self.run_dir / "frames" / f"{self._frame_n:02d}-{label}.jpg"

    def _register_frame(self, path, label):
        if self._late_screen_frame(label, path):
            return None
        relative = path.relative_to(self.run_dir).as_posix()
        self.frames.append(relative)
        if label == "step":
            self.step_frames.append(relative)
        if label in ("launch", "step"):
            self._observe_screen(relative)
        return path

    def frame(self, label):
        """A screenshot as frames/NN-label.jpg (JPEG, quality 75, half the simulator's pixel width)."""
        with self._screen_frame(label) as admitted:
            if not admitted:
                return None
            path = self._frame_path(label)
            try:
                self._call(self.manager.screenshot, self.target, path, max_width=self._frame_width,
                           quality=FRAME_QUALITY)
                if self._late_screen_frame(label, path):
                    return None
                if self._frame_width is None:
                    self._frame_width = _halve(path)
            except CouldntRun as error:
                self.log(f"no {label} frame: {error.message}")
                if label in ("launch", "step") and not self._late_screen_frame(label, path):
                    self._observe_screen(None)
                return None
            except Exception as error:
                self.log(f"no {label} frame: {type(error).__name__}")
                if label in ("launch", "step") and not self._late_screen_frame(label, path):
                    self._observe_screen(None)
                return None
            return self._register_frame(path, label)

    def frame_from_bytes(self, data, label):
        """A frame from JPEG or PNG bytes (the image Smart saw), at the run's frame width."""
        with self._screen_frame(label) as admitted:
            if not admitted:
                return None
            path = self._frame_path(label)
            try:
                from io import BytesIO

                from PIL import Image
                image = Image.open(BytesIO(data)).convert("RGB")
                if self._frame_width and image.width > self._frame_width:
                    image = image.resize((self._frame_width, round(image.height * self._frame_width / image.width)))
                image.save(path, "JPEG", quality=FRAME_QUALITY)
                if self._late_screen_frame(label, path):
                    return None
            except Exception as error:
                self.log(f"no {label} frame: {type(error).__name__}")
                if label in ("launch", "step") and not self._late_screen_frame(label, path):
                    self._observe_screen(None)
                return None
            return self._register_frame(path, label)

    def record_step(self, *, op, text, target=None, typed=None, changed=None, frame=None):
        """One step (§5.3) for the result and `wait`'s steps_so_far; key-less actions also go to events.jsonl."""
        text, typed = self._scrub(text), self._scrub(typed)
        step = {"index": len(self.steps) + 1, "op": op, "text": text,
                "target": ({"id": target.get("id"), "label": target.get("label"), "role": target.get("role")}
                           if target else None),
                "typed": typed, "changed": changed, "frame": self._relative(frame), "at_ms": self._ms()}
        step = project_env.mask_deep(step, self.secrets)
        self.steps.append(step)
        if self.mode != "smart":
            self.event({"event": "step", **step})
        self.log(f"step {step['index']}: {text}")
        return step

    def _relative(self, frame):
        """A frame path as the result writes it: relative to run_dir."""
        if not frame:
            return None
        path = Path(frame)
        try:
            return (path.relative_to(self.run_dir) if path.is_absolute() else path).as_posix()
        except ValueError:
            return str(path)

    def relaunch(self):
        """Terminate the app and launch it again with the same arguments and environment, keeping its data."""
        with self.lock:
            bundle = self.app.bundle_id
            stale = self._stale_process()
            with self.actions.action("TERMINATE"):
                self._call(self.manager.terminate, self.target, bundle)
            args = tuple(self._live.launch_args)
            if self._conditions is not None:
                args += self._conditions.launch_args()
            if self.business_fixture is not None:
                args += ("-MobsterBusinessRun", self.business_fixture.token)
            with self.actions.action("LAUNCH"):
                self._call(self.manager.launch, self.target, bundle, args, self._launch_env())
            self._await_foreground(stale)

    def open_url(self, url):
        """A deep link into the running app."""
        from .checks import URL_SCHEME
        if not isinstance(url, str) or not URL_SCHEME.match(url) or len(url) > 2000:
            from .checks import CheckError
            raise CheckError("the link needs a scheme, such as yourapp://lesson")
        with self.lock:
            self._open_link(url)

    def evaluate(self, assertions, *, timeout=5):
        with self.lock:
            results, _, _ = self._settle(assertions, timeout)
            return results

    def _settle(self, assertions, timeout):
        timeout = max(0.0, float(timeout))
        deadline = _now() + timeout
        previous = self._timed_read(0, glance=True)
        self._sleep(SETTLE_FIRST_PAUSE)
        reads = 1
        while True:
            tree = self._timed_read(reads)
            reads += 1
            # A glance has no ``visible``, so the first pair compares layouts; evidence pairs compare exactly.
            stable = (tree.layout() == previous.layout() if previous.glance
                      else tree.fingerprint() == previous.fingerprint())
            results = evaluate_on(assertions, tree)
            held = sum(result.ok for result in results)
            self.log(f"  {'same screen' if stable else 'screen changed'}, {held} of {len(results)} held")
            if stable and held == len(results):
                return results, tree, True
            now = _now()
            if now >= deadline:
                return results, tree, stable
            self._sleep(min(SETTLE_PAUSE, deadline - now))
            previous = tree

    def _timed_read(self, index, glance=False):
        started = time.monotonic()
        tree = self.read_tree(glance=glance)
        self.log(f"read {index + 1}: {round((time.monotonic() - started) * 1000)} ms, {len(tree.shown())} shown"
                 f"{' (glance)' if tree.glance else ''}")
        return tree

    def _audit_findings(self):
        """The findings of the check's static audits, run on the built app."""
        names = [name for name in self.check.audit if name not in (A11Y, device_audit.NAME)]
        if not names:
            return []
        from . import audit
        if not self.check.app_path:
            return [{**audit.Finding("audit-needs-app", "2.1", "review",
                                     "The audit reads a built app, and the check names none. Add app.path.",
                                     "the check").to_dict(), "frame": None}]
        out = []
        for name in names:
            out.extend({**finding.to_dict(), "frame": None} for finding in audit.run_audit(name, self.check.app_path))
        return out

    def _give_after_frame(self, path):
        """``path`` becomes the frame of every Smart step still waiting for the screen after it."""
        # A turn's actions share one frame: Smart sees no image between them.
        for step in self._awaiting_frame:
            step["frame"] = self._relative(path)
        self._awaiting_frame.clear()

    def abort(self, klass, message, fix=""):
        """End the run as couldnt_run with ``klass``, release the simulator; a preparing run stops at its next step."""
        self._stop.set()
        dispatching = self.actions.request_stop()
        action_failure = self._action_failure()
        if action_failure is not None:
            klass, message, fix = action_failure.klass, action_failure.message, action_failure.fix
        elif dispatching:
            klass, message, fix = ("unknown_outcome", "An action may have reached the device before the run stopped.",
                                   "Inspect the run's actions.jsonl and device before another run.")
        with self._finish_lock:  # prepare() changes the state under this lock, so the check below holds
            if self.state == "preparing":
                try:
                    if self.result is None:
                        if (self.business_fixture is not None and self.business_result is not None
                                and (self.business_fixture.seeded or self.business_result["setup"] == "pending")):
                            self.business_result["cleanup"] = "needs_reconciliation"
                        if self._conditions is not None and self.conditions_cleanup == "pending":
                            self.conditions_cleanup = "needs_reconciliation"
                        self.error = {"klass": klass, "message": message, "fix": fix or ""}
                        self.log(f"couldn't run ({klass}): {message}")
                        result = self._compose(None)
                        self._write(result)
                        self.result = result
                        self.state = "finished"  # the preparing thread releases the simulator at its next phase
                    return self.result
                finally:
                    self._cancel_build()
        with self.lock:
            if self.result is None and self.error is None:
                self._fail(CouldntRun(klass, message, fix))
            return self.finish()

    def _cancel_build(self):
        # The build (up to 20 minutes the first time) runs in its own process group, so nothing else would stop it.
        cancel = getattr(getattr(self.manager, "wda", None), "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception as error:
                self.log(f"stopping the WebDriverAgent build failed: {type(error).__name__}")

    def fail_internal(self, error):
        """An unexpected exception: couldnt_run, class internal; the traceback goes to run.log only."""
        self.log("".join(traceback.format_exception(type(error), error, error.__traceback__)))
        self.error = {"klass": "internal", "message": f"Mobster hit an unexpected error ({type(error).__name__}).",
                      "fix": "run.log in the run folder has the details; please report it."}

    def finish(self):
        """The verdict (§4) and every artifact: result.json, report.html, check.yaml, the verdict frames."""
        # Always lock, then _finish_lock: abort() is called with lock held while another thread may call finish().
        with self.lock, self._finish_lock:
            if self.result is not None:
                if not self._preparing:  # else the preparing thread releases it, at its next phase
                    self._release()      # idempotent: a result written while preparing still frees the simulator
                return self.result
            self._seal_screen_frames()
            interrupted = None
            try:
                decided = self._decide()
            except CouldntRun as error:
                # WebDriverAgent losing an app that crashed during the verdict read is the crash.
                crash = self._crash(suspect=True) if error.klass in CRASH_EXPLAINS else None
                decided = self._crashed(crash) if crash else None
                if decided is None:
                    self._fail(error)
            except KeyboardInterrupt as error:
                # ctrl+c during the verdict read: the run still ends as stopped with every artifact, then it goes on.
                if self.error is None:
                    self._fail(CouldntRun("stopped", "Stopped with ctrl+c."))
                decided, interrupted = None, error
            except Exception as error:
                self.fail_internal(error)
                decided = None
            self._stop_evidence()  # the log ends with the verdict read
            primary = (
                {"verdict": decided["verdict"], "class": decided["klass"]} if decided is not None
                else {"verdict": "couldnt_run", "class": self.error["klass"]} if self.error is not None
                else None
            )
            cleanup = self._cleanup_after_run()
            failure = self._action_failure() or cleanup
            if self.error is not None and self.error["klass"] == "unknown_outcome":
                failure = None  # a later cleanup failure cannot replace a prior unknown effect
                decided = None
            elif failure is not None or cleanup is not None:
                if self.business_result is not None and primary is not None:
                    self.business_result["primary"] = primary
                failed = self._fail(failure or cleanup)
                if decided is not None:
                    decided = {**decided, "verdict": "couldnt_run", "klass": failed.klass,
                               "message": failed.message, "fix": failed.fix}
            result = self._compose(decided)
            if self.replay is not None:
                try:
                    self.replay.after(result)
                except Exception as error:  # the cache never costs a run its verdict
                    self.log("".join(traceback.format_exception(type(error), error, error.__traceback__)))
            try:
                self._write(result)
            finally:
                self._release()
            self.result = result
            self.state = "finished"
            if interrupted is not None:
                raise interrupted
            return result

def _halve(path):
    """Resize the first frame to half its pixel width (603 px from an iPhone 17 Pro's 1206); returns that width."""
    from PIL import Image
    with Image.open(path) as image:
        image = image.convert("RGB")
        width = max(1, image.width // 2)
        image.resize((width, max(1, round(image.height * width / image.width)))).save(path, "JPEG",
                                                                                         quality=FRAME_QUALITY)
    return width


def resolve_mode(check, mode):
    """The mode a run uses: auto means Smart with steps, launch-only without."""
    if mode == "auto":
        return "smart" if check.steps else "launch"
    if mode not in MODES:
        raise ValueError(f"mode must be auto or one of {', '.join(MODES)}")
    return mode


def verify(check, *, mode="auto", runs_dir, manager=None, progress=None, key=None, cancelled=None,
           assert_timeout=5):
    """The whole run -> the result dict (§5.3)."""
    from . import fixtures
    from .system_surfaces.play import drive as drive_surfaces
    try:
        check = fixtures.for_run(check, runs_dir)
    except fixtures.FixtureError as error:
        run = VerifyRun(check, mode=resolve_mode(check, mode), runs_dir=runs_dir, manager=manager,
                        progress=progress, key=key, assert_timeout=assert_timeout)
        return run.abort("usage", str(error), "`mobster fixtures list` shows the project's fixtures.")
    mode = resolve_mode(check, mode)
    run = VerifyRun(check, mode=mode, runs_dir=runs_dir, manager=manager, progress=progress, key=key,
                    assert_timeout=assert_timeout)
    if check.steps and mode == "launch":
        return run.abort("usage", "--keyless never calls a model, so it can't run steps.",
                         "Drop the steps for a launch-only check, or let your coding agent drive through "
                         "`mobster mcp`.")
    if check.steps and mode == "keyless":
        return run.abort("usage", "A key-less run with steps needs a coding agent to drive it.",
                         "Run it through `mobster mcp`, or drop the steps for a launch-only check.")
    if mode == "smart":
        from ..engines import smart_key
        run.key = key or smart_key()
        if not run.key:
            why, fix = no_smart_key()
            return run.abort("usage", f"These steps need Smart, and {why}.", fix)
    try:
        run.prepare()
        if mode == "smart":
            run.run_smart(cancelled)
        drive_surfaces(run)
    except CouldntRun as error:
        if run.error is None:
            run._fail(error)
    except KeyboardInterrupt as stop:
        # devtools.Terminated (SIGTERM, SIGHUP) is a KeyboardInterrupt too; the result rides on it for the CLI's JSON.
        name = getattr(stop, "signum", None)
        stop.result = run.abort("stopped", "Stopped with ctrl+c." if name is None else f"Stopped by {stop}.")
        raise
    except Exception as error:
        run.fail_internal(error)
    return run.finish()

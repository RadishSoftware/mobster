"""One `mobster verify` run: prepare the simulator and the app, run the flow (Smart, the coding agent, or none),
decide the verdict from the assertions, and write the run folder.

    run = VerifyRun(check, mode="keyless", runs_dir=Path(".mobster/runs"))
    run.prepare()                  # simulator lease, install and reset, launch, driver, 01-launch frame, baseline
    ...                            # the coding agent drives through the MCP tools (run.driver, under run.lock)
    result = run.finish()          # the verdict (§4) and every artifact; releases the simulator

``verify(check, runs_dir=…)`` is the CLI's one-call wrapper around the same steps. The verdict never comes from a
model: the assertions are evaluated on the accessibility tree (assertions.py), and Smart's own status only picks
the failure class or marks a run that couldn't run.

The simulator manager (mobile_agent.sim) is imported only inside functions; tests pass ``manager=``.
"""

import base64
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import traceback
import urllib.request

from .assertions import evaluate_on
from .checks import dump_check, project_root
from .tree import TreeError, read_tree as read_tree_from

MODES = ("smart", "keyless", "launch")
SCHEMA = "mobster.verify/1"
EXIT_CODES = {"passed": 0, "failed": 1, "needs_review": 2, "couldnt_run": 3}
# Smart statuses that end the flow early without an error: an assertion failing after one is class "blocked".
BLOCKING = frozenset({"blocked", "timeout", "max_steps", "approval_denied", "approval_timeout"})
FOREGROUND_WAIT_SECONDS = 15
BASELINE_SETTLE_SECONDS = 3
SETTLE_FIRST_PAUSE, SETTLE_PAUSE = .3, .5
FRAME_QUALITY = 75
STEP_FRAME_CAP, STEP_FRAMES_HEAD, STEP_FRAMES_TAIL = 60, 10, 10
NOTE_LIMIT = 500
SAVE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")
NO_KEY_FIX = ("Set OPENAI_API_KEY or ANTHROPIC_API_KEY for Smart, or let your coding agent drive through "
              "`mobster mcp`.")
HELD_BEFORE_FLOW = ("Every expectation already held before the flow ran, so this run doesn't show the flow works. "
                    "Add an expectation that only holds after it.")
MONEY_WORDS = re.compile(r"\b(buy|purchase|subscribe|pay|order)\b", re.IGNORECASE)


def no_smart_key(env=None):
    """(why, fix) when Smart has no key for its model. Since #44 Smart runs on gpt-5.6-sol with an OpenAI key or on
    claude-sonnet-5-5 with only an Anthropic key (``engines.smart_model``), so either key will do, unless
    MOBSTER_SMART_MODEL names a model: then only its provider's key will."""
    from .. import engines
    env = os.environ if env is None else env
    model = engines.smart_model(env)
    if (env.get("MOBSTER_SMART_MODEL") or "").strip() != model:
        return "no OpenAI or Anthropic key is set", NO_KEY_FIX
    variable = "ANTHROPIC_API_KEY" if engines.model_provider(model) == "anthropic" else "OPENAI_API_KEY"
    return (f"MOBSTER_SMART_MODEL is {model}, which needs {variable}, and it isn't set",
            f"Set {variable} for Smart on {model}, or let your coding agent drive through `mobster mcp`.")


class CouldntRun(Exception):
    """The run can't go on: couldnt_run with ``klass`` (usage, model, environment, simulator, wda, install, launch,
    busy, budget, stopped, internal), a plain ``message`` and a ``fix`` sentence (may be "")."""

    def __init__(self, klass, message, fix=""):
        super().__init__(message)
        self.klass, self.message, self.fix = klass, message, fix or ""


def _now():
    return time.monotonic()


def _pause(seconds):
    time.sleep(seconds)


def new_run_id():
    """YYYYMMDD-HHMMSS-xxxx: the local time plus 4 random hex characters."""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


# What `.mobster/.gitignore` holds when Mobster writes it: runs and test results show whatever was on the screen,
# and the cache is per machine. OLD_GITIGNORES are the files earlier versions wrote, upgraded in place.
GITIGNORE = "runs/\nbuild/\ncache/\ntest-results/\n"
OLD_GITIGNORES = ("runs/\nbuild/\n",)


def ensure_gitignore(runs_dir):
    """Ignore runs/, build/, cache/ and test-results/ in git when ``runs_dir`` is a `.mobster/runs`: whenever
    `.mobster/.gitignore` is missing, also when the user made `.mobster/` (for checks/) or a run made runs/ before,
    and never over the user's own file (one an earlier Mobster wrote, word for word, is brought up to date). Frames
    show whatever was on the screen, a real iPhone's included."""
    runs_dir = Path(runs_dir)
    if runs_dir.parent.name != ".mobster":
        return
    folder = runs_dir.parent
    ignore = folder / ".gitignore"
    if ignore.exists():
        try:
            if ignore.is_file() and not ignore.is_symlink() and ignore.read_text(encoding="utf-8") in OLD_GITIGNORES:
                temporary = folder / ".gitignore.tmp"
                temporary.write_text(GITIGNORE, encoding="utf-8")
                os.replace(temporary, ignore)
        except (OSError, UnicodeDecodeError):
            pass  # the user's file, or one we can't read: left as it is
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with open(ignore, "x", encoding="utf-8") as handle:
            handle.write(GITIGNORE)
    except FileExistsError:
        pass  # another run wrote it first


def make_run_dir(runs_dir):
    """(run_id, run_dir): a new run folder with its frames/ folder, readable by this user only (0700), as the
    task journal's are: a run's frames and steps hold what was on the screen."""
    runs_dir = Path(runs_dir)
    ensure_gitignore(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    while True:
        run_id = new_run_id()
        run_dir = runs_dir / run_id
        try:
            run_dir.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(run_dir, 0o700)  # whatever the umask
        (run_dir / "frames").mkdir(mode=0o700)
        return run_id, run_dir


def wda_session(wda_url):
    """The run's WDA session, created the benchmark's way (bench/iosworld.wda_session): plain capabilities, the
    settings Smart's simulator numbers were measured with."""
    with urllib.request.urlopen(wda_url + "/status", timeout=10) as response:
        status = json.load(response)
    if status.get("sessionId"):
        return status["sessionId"]
    request = urllib.request.Request(wda_url + "/session", method="POST",
                                     data=json.dumps({"capabilities": {"alwaysMatch": {}}}).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["sessionId"]


def front_app(wda_url, timeout=5):
    """WDA's foreground app as (bundle ID, process ID), read device-wide with no session (GET /wda/activeAppInfo,
    the read drivers.WDA.active_app makes). Either is None when WDA leaves it out. Raises when WDA doesn't answer."""
    with urllib.request.urlopen(wda_url + "/wda/activeAppInfo", timeout=timeout) as response:
        value = json.load(response).get("value")
    value = value if isinstance(value, dict) else {}
    bundle, pid = value.get("bundleId"), value.get("pid")
    return (bundle if isinstance(bundle, str) and bundle else None,
            pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None)


def build_driver(target, manager, run_dir, mode):
    """The run's WDA driver, the same way for every mode (§8): a plain session, compose.build_target_driver,
    the simulator's restarter and a new-session hook. Runs that act (Smart and key-less) also get the frame clock
    on the simulator's MJPEG stream, which times each action's settle (frameclock.jsonl); a launch-only run takes
    no action, so it opens no stream. Key-less and launch-only runs also get Smart's switches and tuning (what
    FrontierAgent does when it starts), so the coding agent's taps and typing go through the same actuation as
    Smart's."""
    from ..compose import build_target_driver
    from ..frame_clock import attach_frame_clock
    url = target.wda_url
    session = wda_session(url)
    driver = build_target_driver(wda_url=url, session=session)
    try:
        driver.restarter = manager.restarter(target)
    except Exception:
        driver.restarter = None  # recovery then waits for the runner instead of restarting it
    driver.new_session = lambda: wda_session(url)
    if mode != "launch":
        attach_frame_clock(driver, wda_url=url, session=session, mode="on", mjpeg_url=target.mjpeg_url,
                           log_path=str(Path(run_dir) / "frameclock.jsonl"))
    if mode != "smart":
        from .. import engines
        engines.apply_switches()
        driver.tune(rich=True, glide=True)
    return driver


def deny_money(request):
    """Smart's approver in a check on a simulator: no purchase, payment or transfer, ever. Other declared commits
    ("Sign up") go through: the simulator is Mobster's own."""
    from ..contract import MONEY_ACTS
    act = (request or {}).get("act")
    if act in MONEY_ACTS or act in ("pay", "transfer"):
        return "denied"
    if act == "other" and MONEY_WORDS.search(f"{request.get('label') or ''} {request.get('title') or ''}"):
        return "denied"
    return "approved"


def deny_commits(request):
    """Smart's approver in a check on a real device (a USB iPhone, a WDA address): every declared commit is
    refused. The phone is someone's own: a send, a delete, a post or a purchase there is real, and a check never
    asks a person first."""
    return "denied"


def check_approver(manager):
    """The approver for a Smart check driven through ``manager``: deny_commits on a real device (a manager with
    ``real_device``, device_targets.WdaDevice), deny_money on a simulator."""
    return deny_commits if getattr(manager, "real_device", False) is True else deny_money


# System view services an app presents over itself (a share sheet, Sign in with Apple, a purchase sheet): part of
# the app under test, not another app. SpringBoard (alerts, the Home Screen) the frontier never stops for.
VIEW_SERVICE = re.compile(r"com\.apple\.[A-Za-z0-9.]*(?:ViewService|UIService)")
LEFT_APP = ("On your iPhone a check stays in {app}, and Smart opened {other}, so the check stopped there. Nothing "
            "was read from {other} for the report.")
LEFT_APP_FIX = "Keep the check's steps inside the app, or run it on a simulator with --sim."


class StayInApp:
    """The phone guard (agent_hooks.PhoneGuard) for a Smart check on a real device: the app runs' lock and unplug
    detection (lockscreen.guard_for), and ``allows_app`` only for the app under test, so the frontier stops the
    moment another app comes to the front (C7). A check drives only its own app on someone's phone: a step that
    opens Messages or Photos there would read the person's own data. ``left`` names the app it stopped for."""

    def __init__(self, bundle, inner=None):
        self.bundle, self.inner, self.left = bundle, inner, None

    def check(self, driver, *, cause):
        return self.inner.check(driver, cause=cause) if self.inner is not None else None

    def attached(self):
        return self.inner.attached() if self.inner is not None else None

    def allows_app(self, bundle_id):
        if not bundle_id or VIEW_SERVICE.fullmatch(bundle_id):
            return True
        if bundle_id == self.bundle:  # the user's own list of apps Mobster never opens still applies
            allows = getattr(self.inner, "allows_app", None)
            return bool(allows(bundle_id)) if callable(allows) else True
        self.left = bundle_id
        return False

    def finish(self, driver):
        if self.inner is not None:
            self.inner.finish(driver)


def check_guard(manager, bundle):
    """The phone guard for a Smart check driven through ``manager``: StayInApp on a real device, None on a
    simulator (Mobster's own, where a check may open any app)."""
    if getattr(manager, "real_device", False) is not True:
        return None
    record = getattr(manager, "record", None) or {}
    inner = None
    try:
        from ..lockscreen import guard_for
        inner = guard_for(device_id=record.get("udid") or record.get("id"), wda_url=record.get("wdaUrl"),
                          mode="scripts", usb=record.get("kind") == "usb")
    except Exception:
        inner = None  # no lock detection: the app restriction still holds
    return StayInApp(bundle, inner)


def smart_request(app_name, steps):
    """What Smart is asked: "In <App>: <the step>", or the steps numbered."""
    if len(steps) == 1:
        return f"In {app_name}: {steps[0]}"
    return f"In {app_name}: " + " ".join(f"{index}. {step}" for index, step in enumerate(steps, 1))


def _sim_error(error):
    """A simulator manager error (duck-typed by ``kind``) as CouldntRun."""
    kind = getattr(error, "kind", None)
    if isinstance(kind, str):
        return CouldntRun(kind, str(error), getattr(error, "fix", "") or "")
    return None


def checks_dir_for(runs_dir):
    """Where saved checks go: the runs folder's sibling ``checks/`` (``.mobster/checks`` for ``.mobster/runs``)."""
    return Path(runs_dir).resolve().parent / "checks"


def save_check_file(check, name, checks_dir):
    """Write ``check`` as ``<checks_dir>/<name>.yaml``, with paths relative to that project. Returns the path."""
    if not isinstance(name, str) or not SAVE_NAME.fullmatch(name):
        from .checks import CheckError
        raise CheckError("a check's name is 1 to 60 of a-z, 0-9 and -, starting with a letter or digit")
    checks_dir = Path(checks_dir)
    checks_dir.mkdir(parents=True, exist_ok=True)
    root = checks_dir.parent.parent if checks_dir.parent.name == ".mobster" else None
    path = checks_dir / f"{name}.yaml"
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(dump_check(check, project_root=root), encoding="utf-8")
    os.replace(temporary, path)
    return path


def _mobster_version():
    from .. import __version__
    return __version__


class VerifyRun:
    """One run. mode: "smart" | "keyless" | "launch". State: new -> preparing -> ready -> (running) -> finished.
    run_id and run_dir exist from __init__, so the MCP server can hand out the id before prepare() ends.
    The MCP server runs Smart as VerifyRun(mode="smart"): prepare(), run_smart(cancelled), finish(), on a worker
    thread. verify() below is the CLI's one-call wrapper around the same steps."""

    def __init__(self, check, *, mode, runs_dir, manager=None, progress=None, key=None, assert_timeout=5):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        self.check, self.mode, self.manager, self.key = check, mode, manager, key
        self.assert_timeout = assert_timeout
        self._progress = progress
        self.runs_dir = Path(runs_dir).expanduser().resolve()
        self.run_id, self.run_dir = make_run_dir(self.runs_dir)
        self.state = "new"
        self.steps = []
        self.error = None
        self.lock = threading.RLock()
        self.driver = self.target = self.app = self.lease = None
        self.frames = []            # every frame, relative to run_dir, in order
        self.step_frames = []       # the step frames among them (the 60-frame cap applies to these)
        self.timing = {}
        self.baseline = {"taken": False, "held": []}
        self.agent = None           # Smart's {status, note, model, turns}
        self.cost_usd = 0.0
        self.result = None
        self._t0 = time.monotonic()
        self._ready_at = None
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
        self.log(f"Mobster {_mobster_version()} verify, run {self.run_id}, mode {mode}")

    # -- small helpers ------------------------------------------------------------------------------------------

    def _scrub(self, text):
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
                self._progress(line)
            except Exception:
                pass

    def event(self, event):
        """One line of events.jsonl: Smart's frontier events (no prompts, no images) or key-less actions."""
        with self._write_lock:
            try:
                line = self._scrub(json.dumps(event, ensure_ascii=False, default=str))
                with open(self.run_dir / "events.jsonl", "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except (OSError, TypeError, ValueError):
                pass

    def _clock(self):
        return _now()

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
        self.log(f"couldn't run ({error.klass}): {error.message}")
        return error

    # -- preparation --------------------------------------------------------------------------------------------

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
        except CouldntRun:
            raise
        except Exception as error:
            converted = _sim_error(error)
            if converted is not None:
                raise converted from None
            raise

    def prepare(self):
        """Acquire a simulator, install or reset the app, launch it, build the driver, take the 01-launch frame
        and, when the check has steps, the baseline read. Raises CouldntRun (and sets ``error``).

        abort() decides under ``_finish_lock`` who releases the simulator: this thread while the state is
        "preparing", finish() once it is "ready". So the stop checks that bracket the preparation and the state
        changes they guard are each one step under that lock. With the last check and the move to "ready" apart,
        an abort landing between them wrote its stopped result, this thread went on to "ready", and finish()
        returned that result without releasing the lease or closing the driver."""
        with self._finish_lock:
            if self._stop.is_set() or self.result is not None:  # the abort that stopped it writes the result
                raise CouldntRun("stopped", "The run was stopped")
            self.state = "preparing"
            self._preparing = True
        try:
            try:
                self._prepare()
            except CouldntRun as error:
                if self.result is None:  # an abort's result already carries its own reason
                    self._fail(error)
                self._release()
                raise
            except BaseException:
                self._release()
                raise
            with self._finish_lock:
                if self._stop.is_set() or self.result is not None:
                    self._release()
                    stopped = CouldntRun("stopped", "The run was stopped")
                    raise self._fail(stopped) if self.result is None else stopped  # an abort's result stands
                self.state = "ready"
                self._ready_at = time.monotonic()
        finally:
            self._preparing = False

    def _prepare(self):
        check = self.check
        manager = self._manager()
        started = time.monotonic()
        self.lease = self._call(manager.acquire, check.device, check.runtime)
        self.target = self.lease.target
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
        level = check.reset_level() if not bundle.startswith("com.apple.") else "none"
        if check.app_path:
            self.progress(f"Installing {self.app.name} {self.app.version}")
            if level == "reinstall":
                self._call(manager.reset, self.target, bundle, "reinstall", app_path=check.app_path)
            else:
                self.app = self._call(manager.install, self.target, check.app_path)
                if level == "data":
                    self._call(manager.reset, self.target, bundle, "data")
        elif level != "none":
            self._call(manager.reset, self.target, bundle, level)
        self.log(f"app {bundle} {self.app.version}, reset {level}")
        self.timing["install"] = round(time.monotonic() - started, 2)
        self._check_stop()

        started = time.monotonic()
        # The process a relaunch replaces, when the app is already in front (a com.apple.* app, --reset none).
        stale = self._stale_process()
        # The driver's session and settings are made while simctl launches the app (measured: 0.3 s and
        # 0.5-0.8 s on this Mac, 28 Sep).
        built = {}

        def build():
            try:
                built["driver"] = build_driver(self.target, manager, self.run_dir, self.mode)
            except BaseException as error:  # handed to the preparing thread below
                built["error"] = error
        builder = threading.Thread(target=build, name="mobster-verify-driver", daemon=True)
        builder.start()
        try:
            self._call(manager.launch, self.target, bundle, check.launch_args, dict(check.launch_env))
        finally:
            builder.join()
            self.driver = built.get("driver")
        error = built.get("error")
        if isinstance(error, CouldntRun):
            raise error
        if error is not None:
            raise CouldntRun("wda", f"WebDriverAgent didn't answer on the simulator ({type(error).__name__}).",
                             "Run `mobster sim doctor --fix`, then try again.") from None
        self._await_foreground(stale)
        if check.open_url:
            self._open_link(check.open_url)
        self.timing["launch"] = round(time.monotonic() - started, 2)
        self._check_stop()
        self.frame("launch")
        if check.steps:
            self._take_baseline()

    def _stale_process(self):
        """The app's process ID when it is WDA's foreground app now, before a launch replaces it; else None."""
        bundle, pid = self._front()
        return pid if bundle == self.app.bundle_id else None

    def _await_foreground(self, stale=None):
        """Wait (up to 15 s) until the app is WDA's foreground app, in a process other than ``stale`` (the one
        `simctl launch --terminate-running-process` replaced). WDA's first answer after a launch waits for the app
        to go idle (measured on Settings: 1.9-2.4 s), and reading the tree first would wait just as long. Until
        the new process is up, WDA can still name the old one, or name the app with no process: taking either for
        the relaunched app sent the first evidence read into "Application com.apple.Preferences is not running"
        (1 of 22 Settings reruns, 28 Sep), and 3 of 21 reruns got the app with no process 0.03-0.07 s after the
        launch. A crash here is left for the verdict to report (failed, app_not_running): the app's failure, not
        the machine's."""
        bundle = self.app.bundle_id
        started = self._clock()
        deadline = started + FOREGROUND_WAIT_SECONDS
        while True:
            remaining = deadline - self._clock()
            front, pid = self._front(timeout=max(1.0, remaining))
            if front == bundle and (stale is None or pid not in (None, stale)):
                if stale is not None:
                    self.log(f"{bundle} in front in process {pid} (was {stale}) after "
                             f"{self._clock() - started:.2f} s")
                return True
            if self._clock() >= deadline:
                self.log(f"{bundle} was not in front {FOREGROUND_WAIT_SECONDS} s after launch"
                         + (f" (still process {stale})" if front == bundle else ""))
                return False
            self._sleep(.2)

    def _front(self, timeout=2):
        """WDA's foreground app as (bundle ID, process ID); (None, None) when WDA doesn't answer."""
        try:
            return front_app(self.target.wda_url, timeout=timeout)
        except Exception:
            return None, None

    def _foreground(self, timeout=5):
        try:
            return self.driver.active_app(timeout=timeout)
        except Exception:
            return None

    def _take_baseline(self):
        """One read after launch and the deep link, before any step, for held_before_flow (§4 rule 8): the first
        read that agrees with the one before it (at most 3 s), so a launch screen isn't taken for the start of
        the flow. It never waits for the assertions."""
        deadline = self._clock() + BASELINE_SETTLE_SECONDS
        tree = self.read_tree()
        while self._clock() < deadline:
            self._sleep(SETTLE_FIRST_PAUSE)
            again = self.read_tree()
            if again.fingerprint() == tree.fingerprint():
                tree = again
                break
            tree = again
        held = [result.ok for result in evaluate_on(self.check.expect, tree)]
        self.baseline = {"taken": True, "held": held}
        self.log(f"baseline: {sum(held)} of {len(held)} expectations held before the flow")

    # -- reads, frames and steps ----------------------------------------------------------------------------------

    def read_tree(self):
        """One evidence read of the app's tree. A transport failure is retried once, after a pause; a second one
        is CouldntRun (class wda)."""
        if self.driver is None:
            raise CouldntRun("wda", "The run has no WebDriverAgent connection yet")
        try:
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
        relative = path.relative_to(self.run_dir).as_posix()
        self.frames.append(relative)
        if label == "step":
            self.step_frames.append(relative)
        return path

    def frame(self, label):
        """A screenshot as frames/NN-label.jpg (JPEG, quality 75, half the simulator's pixel width). Returns the
        absolute path, or None when the simulator gave no screenshot."""
        path = self._frame_path(label)
        try:
            self._call(self.manager.screenshot, self.target, path, max_width=self._frame_width,
                       quality=FRAME_QUALITY)
            if self._frame_width is None:
                self._frame_width = _halve(path)
        except CouldntRun as error:
            self.log(f"no {label} frame: {error.message}")
            return None
        except Exception as error:
            self.log(f"no {label} frame: {type(error).__name__}")
            return None
        return self._register_frame(path, label)

    def frame_from_bytes(self, data, label):
        """A frame from JPEG or PNG bytes (the image Smart saw), at the run's frame width."""
        path = self._frame_path(label)
        try:
            from io import BytesIO
            from PIL import Image
            image = Image.open(BytesIO(data)).convert("RGB")
            if self._frame_width and image.width > self._frame_width:
                image = image.resize((self._frame_width, round(image.height * self._frame_width / image.width)))
            image.save(path, "JPEG", quality=FRAME_QUALITY)
        except Exception as error:
            self.log(f"no {label} frame: {type(error).__name__}")
            return None
        return self._register_frame(path, label)

    def record_step(self, *, op, text, target=None, typed=None, changed=None, frame=None):
        """One step (§5.3) for the result and `wait`'s steps_so_far; key-less actions also go to events.jsonl."""
        step = {"index": len(self.steps) + 1, "op": op, "text": text,
                "target": ({"id": target.get("id"), "label": target.get("label"), "role": target.get("role")}
                           if target else None),
                "typed": typed, "changed": changed, "frame": self._relative(frame), "at_ms": self._ms()}
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
            self._call(self.manager.terminate, self.target, bundle)
            self._call(self.manager.launch, self.target, bundle, self.check.launch_args, dict(self.check.launch_env))
            self._await_foreground(stale)

    def open_url(self, url):
        """A deep link into the running app."""
        from .checks import URL_SCHEME
        if not isinstance(url, str) or not URL_SCHEME.match(url) or len(url) > 2000:
            from .checks import CheckError
            raise CheckError("the link needs a scheme, such as daybreak://paywall")
        with self.lock:
            self._open_link(url)

    def _open_link(self, url):
        """Open ``url`` in the app through WDA (POST /url with the app's bundle ID), which opens it with no prompt.
        `simctl openurl` stops at SpringBoard's "Open in “<App>”?" on iOS 26.4 (SPEC §6.4), so the manager's
        open_url is only the fallback, when WDA can't. drivers.WDA.call also makes the app WDA's active one."""
        try:
            self.driver.call("POST", "/url", {"url": url, "bundleId": self.app.bundle_id}, timeout=20)
        except Exception as error:
            if getattr(self.manager, "real_device", False) is True:
                # The fallback opens the link system-wide, in whichever app takes it (a shortcuts:// link runs a
                # Shortcut): on someone's phone a check's link opens in its own app or not at all.
                raise CouldntRun("launch", f"WebDriverAgent couldn't open the link in {self.app.name} on the "
                                           f"iPhone ({type(error).__name__}).",
                                 "Update WebDriverAgent in the Mobster app, or run the check on a simulator with "
                                 "--sim.") from None
            self.log(f"WDA didn't open the link ({type(error).__name__}); opening it with simctl")
            self._call(self.manager.open_url, self.target, url)

    # -- evaluation ----------------------------------------------------------------------------------------------

    def evaluate(self, assertions, *, timeout=5):
        """The assertions on one settled read (§3.5): read, wait 0.3 s, read again; decided once two reads agree and
        every assertion holds, else read every 0.5 s until ``timeout``, when the last read decides. It never
        touches the verdict (the MCP's wait_for uses it)."""
        with self.lock:
            results, _, _ = self._settle(assertions, timeout)
            return results

    def _settle(self, assertions, timeout):
        timeout = max(0.0, float(timeout))
        deadline = self._clock() + timeout
        previous = self._timed_read(0)
        self._sleep(SETTLE_FIRST_PAUSE)
        reads = 1
        while True:
            tree = self._timed_read(reads)
            reads += 1
            stable = tree.fingerprint() == previous.fingerprint()
            results = evaluate_on(assertions, tree)
            held = sum(result.ok for result in results)
            self.log(f"  {'same screen' if stable else 'screen changed'}, {held} of {len(results)} held")
            if stable and held == len(results):
                return results, tree, True
            now = self._clock()
            if now >= deadline:
                return results, tree, stable
            self._sleep(min(SETTLE_PAUSE, deadline - now))
            previous = tree

    def _timed_read(self, index):
        started = time.monotonic()
        tree = self.read_tree()
        self.log(f"read {index + 1}: {round((time.monotonic() - started) * 1000)} ms, {len(tree.shown())} shown")
        return tree

    # -- Smart ---------------------------------------------------------------------------------------------------

    def run_smart(self, cancelled=None):
        """Run the check's steps with Smart (frontier.FrontierAgent through engines, exactly as the app builds it)
        on the user's OpenAI or Anthropic key (``engines.build_client`` on ``engines.smart_model``). Raises
        CouldntRun (and sets ``error``) when it can't start."""
        from .. import engines
        from ..frontier import cost_usd
        if self.mode != "smart":
            raise ValueError("run_smart needs mode smart")
        key = self.key or engines.smart_key()
        if not key:
            why, fix = no_smart_key()
            raise self._fail(CouldntRun("model", f"Smart can't run: {why}.", fix))
        self.key = key
        self.state = "running"
        spent = {"usd": 0.0}
        started = time.monotonic()
        deadline = started + float(self.check.max_seconds) + 60  # a backstop past the loop's own time limit
        names = {self.app.bundle_id: self.app.name}

        def stopping():
            if self._stop.is_set() or time.monotonic() > deadline:
                return True
            try:
                return bool(cancelled and cancelled())
            except Exception:
                return False

        def on_inference(event):
            if event.get("event") == "inference_finished" and isinstance(event.get("estimated_usd"), (int, float)):
                spent["usd"] += event["estimated_usd"]
            self.event(event)

        def on_event(event):
            kind = event.get("event")
            if kind == "frontier_prompt":
                data = _data_url_bytes(event.get("image"))
                path = self.frame_from_bytes(data, "step") if data else None
                if path is not None:
                    self._give_after_frame(path)
                self.event({"event": kind, "step": event.get("step"), "operations": event.get("operations"),
                            "out": event.get("out")})
                return
            self.event(event)
            if kind == "frontier_action":
                op = event.get("operation") or ""
                label = event.get("target_label") or ""
                text = engines.step_text(op, label, names, text=event.get("text"))
                typed = event.get("text") if op in engines.TYPING else None
                step = self.record_step(op=op, text=text, target={"id": None, "label": label or None, "role": None},
                                        typed=typed, changed=event.get("changed"))
                # Its frame is the screen after it: the image Smart sees next turn, or the verdict frame.
                self._awaiting_frame.append(step)
                self.progress(f"  {text}")

        with self.lock:
            client = engines.build_client(key=key)
            engines.meter(client, on_inference)
            guard = check_guard(self.manager, self.app.bundle_id)
            agent = engines.build_frontier(self.driver, client, apps=names, emit=on_event,
                                           max_cost_usd=self.check.max_usd, approve=check_approver(self.manager),
                                           cancelled=stopping, guard=guard)
            agent.max_seconds = float(self.check.max_seconds)  # the check's time limit, not the app's 15 minutes
            request = smart_request(self.app.name, self.check.steps)
            self.progress(f"Smart: {request}")
            outcome = agent.run(request)
        self.timing["flow"] = round(time.monotonic() - started, 2)
        status = outcome.get("status")
        if status == "stopped" and not self._stop.is_set() and not (cancelled and _safe(cancelled)):
            status = "timeout"  # the backstop, not the user
        self._smart_status, self._smart_reason = status, str(outcome.get("reason") or "")
        if guard is not None and guard.left and outcome.get("code") == "blocked_app":
            self._left_app = guard.left
        self._model_failed = bool(getattr(client, "model_failed", False))
        loop_cost = cost_usd(getattr(client, "model", ""), outcome.get("usage") or {}) or 0.0
        self.cost_usd = round(max(spent["usd"], loop_cost), 5)
        note = outcome.get("answer") or outcome.get("reason") or ""
        self.agent = {"status": status, "note": self._scrub(str(note))[:NOTE_LIMIT] or None,
                      "model": getattr(client, "model", None), "turns": outcome.get("steps")}
        self.log(f"Smart ended {status} after {outcome.get('steps')} turns, ${self.cost_usd:.4f}")
        self.state = "ready"

    def _give_after_frame(self, path):
        """``path`` becomes the frame of every Smart step still waiting for the screen after it. A turn's actions
        share one: Smart sees no image between them."""
        for step in self._awaiting_frame:
            step["frame"] = self._relative(path)
        self._awaiting_frame.clear()

    def _smart_failure(self):
        """CouldntRun for a Smart run that ended in error or was stopped (§4 rule 4), else None."""
        status = self._smart_status
        if self._left_app:
            return CouldntRun("usage", LEFT_APP.format(app=self.app.name, other=self._left_app), LEFT_APP_FIX)
        if status == "stopped":
            return CouldntRun("stopped", "The run was stopped before the flow finished.")
        if status != "error":
            return None
        from .. import engines
        reason = self._smart_reason
        model = (self.agent or {}).get("model") or engines.smart_model()
        found = engines.PROVIDER_ERROR.search(reason)
        provider = found.group(1) if found else engines.provider_name(model)
        ok, why = engines.model_failure(reason, model=model)
        if ok is False:
            return CouldntRun("model", why, f"Check your {provider} key and credit, then run again.")
        if self._model_failed or found or "OpenAI" in reason or "Anthropic" in reason:
            reach = engines.NO_ANTHROPIC_REACH if provider == "Anthropic" else engines.NO_OPENAI_REACH
            return CouldntRun("model", engines.plain_error(reason, model_failed=True, model=model) or reach)
        from ..frontier import CONNECTION_LOST
        if any(name in reason for name in CONNECTION_LOST + ("TimeoutError", "timed out", "TransportError")):
            return CouldntRun("wda", "WebDriverAgent stopped answering during the flow and didn't come back.",
                              "Run `mobster sim doctor --fix`, then try again.")
        return CouldntRun("internal", "Smart stopped with an unexpected error. run.log has the details.")

    # -- the verdict ---------------------------------------------------------------------------------------------

    def abort(self, klass, message, fix=""):
        """End the run as couldnt_run with ``klass``; releases the simulator. A run still preparing stops at its
        next phase."""
        self._stop.set()
        with self._finish_lock:  # prepare() changes the state under this lock, so the check below holds
            if self.state == "preparing":
                if self.result is None:
                    self.error = {"klass": klass, "message": message, "fix": fix or ""}
                    self.log(f"couldn't run ({klass}): {message}")
                    result = self._compose(None)
                    self._write(result)
                    self.result = result
                    self.state = "finished"  # the preparing thread releases the simulator at its next phase
                self._cancel_build()
                return self.result
        with self.lock:
            if self.result is None and self.error is None:
                self._fail(CouldntRun(klass, message, fix))
            return self.finish()

    def _cancel_build(self):
        """Kill the WebDriverAgent build a run still preparing waits on (up to 20 minutes the first time): it runs
        in its own process group, so nothing else would stop it."""
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
        """The verdict (§4) and every artifact: result.json, report.html, check.yaml, the verdict frames. Releases
        the simulator. Idempotent. Locks in one order everywhere, ``lock`` then ``_finish_lock``: abort() is called
        with ``lock`` held (the MCP server's reaper, stop and shutdown) while another thread may call finish()."""
        with self.lock, self._finish_lock:
            if self.result is not None:
                if not self._preparing:  # else the preparing thread releases it, at its next phase
                    self._release()      # idempotent: a result written while preparing still frees the simulator
                return self.result
            interrupted = None
            try:
                decided = self._decide()
            except CouldntRun as error:
                self._fail(error)
                decided = None
            except KeyboardInterrupt as error:
                # ctrl+c during the verdict read (the settle loop waits the whole --assert-timeout when an
                # expectation fails): the run still ends as stopped with every artifact, then the interrupt goes on.
                if self.error is None:
                    self._fail(CouldntRun("stopped", "Stopped with ctrl+c."))
                decided, interrupted = None, error
            except Exception as error:
                self.fail_internal(error)
                decided = None
            result = self._compose(decided)
            self._write(result)
            self.result = result
            self._release()
            self.state = "finished"
            if interrupted is not None:
                raise interrupted
            return result

    def _decide(self):
        """{verdict, klass, message, fix, results, tree, stable, alert, foreground} by the first rule that applies."""
        if self._ready_at is not None and "flow" not in self.timing and self.mode == "keyless":
            self.timing["flow"] = round(time.monotonic() - self._ready_at, 2)
        if self.error is not None:
            self._verdict_frames(None, ())
            return None
        failure = self._smart_failure()
        if failure is not None:
            if not self._left_app:  # another app is in front on someone's phone: no frame of it
                self._verdict_frames(None, ())
            raise failure
        started = time.monotonic()
        expect = self.check.expect
        results, tree, stable = self._settle(expect, self.assert_timeout)
        self.timing["assert"] = round(time.monotonic() - started, 2)
        frame = self._verdict_frames(tree, results)
        decided = {"results": results, "tree": tree, "stable": stable, "frame": frame, "alert": self._alert()}
        failed = [result for result in results if not result.ok]
        status = self._smart_status
        still = "; the screen was still changing" if not stable else ""
        if status == "budget" and failed:
            return {**decided, "verdict": "couldnt_run", "klass": "budget",
                    "message": f"Smart reached its ${self.check.max_usd:.2f} spend cap and {len(failed)} of "
                               f"{len(results)} expectations failed{still}.",
                    "fix": "Raise --max-usd (at most 1.00), then run again."}
        foreground = self._foreground()
        if foreground and foreground != self.app.bundle_id:
            alert = f"; an alert says \"{decided['alert']}\"" if decided["alert"] else ""
            return {**decided, "verdict": "failed", "klass": "app_not_running",
                    "message": f"{self.app.name} wasn't in front at the end: {foreground} was{alert}.",
                    "fix": None, "foreground": foreground}
        if failed:
            message = f"{len(failed)} of {len(results)} expectations failed{still}"
            if status in BLOCKING:
                note = (self.agent or {}).get("note")
                return {**decided, "verdict": "failed", "klass": "blocked",
                        "message": f"Smart ended {status}: " + (note or "no note") + f". {message}", "fix": None}
            return {**decided, "verdict": "failed", "klass": "assertion", "message": message, "fix": None}
        if not expect:
            return {**decided, "verdict": "needs_review", "klass": "no_assertions",
                    "message": "The check has no expectations, so nothing decided the run. The frames show what "
                               "happened.", "fix": "Add expect: what must be true at the end."}
        if self.check.steps and self.baseline.get("taken") and all(self.baseline.get("held") or [False]):
            return {**decided, "verdict": "needs_review", "klass": "held_before_flow", "message": HELD_BEFORE_FLOW,
                    "fix": None}
        return {**decided, "verdict": "passed", "klass": None,
                "message": f"All {len(results)} expectations held on one read{still}", "fix": None}

    def _alert(self):
        try:
            text = self.driver.call("GET", "/alert/text", timeout=3)
            return text[:300] if isinstance(text, str) and text.strip() else None
        except Exception:
            return None

    def _verdict_frames(self, tree, results):
        """NN-verdict (the screen) and NN-verdict-ax (the same with the accessibility overlay). Without a tree the
        overlay is read now, best effort."""
        if self.driver is None or self.target is None:
            return None
        plain = self.frame("verdict")
        if plain is None:
            return None
        self._give_after_frame(plain)
        try:
            if tree is None:
                tree = read_tree_from(self.driver, pause=self._sleep)
            from .report import draw_overlay
            asserted = [(result, list(result.paths)) for result in results]
            path = self._frame_path("verdict-ax")
            draw_overlay(plain, tree, asserted, path)
            self._register_frame(path, "verdict-ax")
        except Exception as error:
            self.log(f"no overlay frame: {type(error).__name__}: {error}")
        return plain

    # -- the result ----------------------------------------------------------------------------------------------

    def _cap_frames(self):
        """At most 60 step frames: the first 10, the last 10, and an even spread of the rest."""
        frames = self.step_frames
        if len(frames) <= STEP_FRAME_CAP:
            return
        head, tail, middle = frames[:STEP_FRAMES_HEAD], frames[-STEP_FRAMES_TAIL:], \
            frames[STEP_FRAMES_HEAD:-STEP_FRAMES_TAIL]
        room = STEP_FRAME_CAP - len(head) - len(tail)
        stride = 2
        while -(-len(middle) // stride) > room:
            stride += 1
        keep = set(head) | set(tail) | set(middle[::stride])
        dropped = set(frames) - keep
        for relative in dropped:
            try:
                (self.run_dir / relative).unlink()
            except OSError:
                pass
        self.frames = [frame for frame in self.frames if frame not in dropped]
        self.step_frames = [frame for frame in frames if frame in keep]
        for step in self.steps:
            if step.get("frame") in dropped:
                step["frame"] = None

    def _compose(self, decided):
        check, app, target = self.check, self.app, self.target
        self._cap_frames()
        if decided is None:
            error = self.error or {"klass": "internal", "message": "The run ended without a verdict.", "fix": ""}
            verdict, klass, message, fix = "couldnt_run", error["klass"], error["message"], error.get("fix") or None
            results, stable, alert, frame = [], None, None, None
            summary = message
        else:
            verdict, klass, message, fix = decided["verdict"], decided["klass"], decided["message"], decided["fix"]
            results, stable, alert, frame = decided["results"], decided["stable"], decided["alert"], decided["frame"]
            failed = [result for result in results if not result.ok]
            if verdict == "passed":
                summary = f"{len(results)} of {len(results)} expectations held"
            elif klass in ("assertion", "blocked", "budget"):
                summary = f"{failed[0].text} failed: {failed[0].observed}"
            else:
                summary = message
        frame_rel = Path(frame).relative_to(self.run_dir).as_posix() if frame else None
        assertions = [{"index": index, "assertion": assertion.to_dict(), "text": result.text, "ok": result.ok,
                       "observed": result.observed, "matches": [dict(match) for match in result.matches],
                       "frame": frame_rel}
                      for index, (assertion, result) in enumerate(zip(check.expect, results))]
        draft = self.run_dir / "check.yaml"
        ax = next((f for f in reversed(self.frames) if f.endswith("-verdict-ax.jpg")), None)
        plain = frame_rel or next((f for f in reversed(self.frames) if f.endswith("-verdict.jpg")), None)
        last_step = self.step_frames[-1] if self.step_frames else None
        proof = [str(self.run_dir / relative) for relative in (ax, plain, last_step) if relative]
        result = {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "verdict": verdict,
            "exit_code": EXIT_CODES[verdict],
            "summary": self._scrub(summary),
            "reason": {"class": klass, "message": self._scrub(message), "fix": fix},
            "mode": self.mode,
            "check": {"name": check.name, "steps": list(check.steps),
                      "expect": [assertion.to_dict() for assertion in check.expect], "source": check.source},
            "app": ({"bundle_id": app.bundle_id, "name": app.name, "version": app.version,
                     "path": str(Path(app.path).resolve()) if app.path else check.app_path}
                    if app is not None else {"bundle_id": check.bundle_id, "name": None, "version": None,
                                             "path": check.app_path}),
            "device": ({"name": target.name, "udid": target.udid, "type": target.device_type,
                        "runtime": target.runtime} if target is not None
                       else {"name": None, "udid": None, "type": check.device, "runtime": check.runtime}),
            "assertions": assertions,
            "baseline": dict(self.baseline),
            "stable": stable,
            "steps": [dict(step) for step in self.steps],
            "agent": dict(self.agent) if self.mode == "smart" and self.agent is not None else None,
            "frames": list(self.frames),
            "proof": proof,
            "alert": alert,
            "report": str(self.run_dir / "report.html"),
            "draft_check": str(draft),
            "run_dir": str(self.run_dir),
            "repro": f"mobster verify --check {_shell_quote(str(draft))}",
            "seconds": round(time.monotonic() - self._t0, 1),
            "timing": {key: self.timing[key] for key in ("simulator", "install", "launch", "flow", "assert", "report")
                       if key in self.timing},
            "cost_usd": self.cost_usd if self.mode == "smart" else 0,
            "mobster": {"version": _mobster_version()},
        }
        return result

    def _write(self, result):
        """check.yaml, result.json and report.html."""
        started = time.monotonic()
        root = project_root(self.run_dir) if self.runs_dir.parent.name == ".mobster" else None
        try:
            (self.run_dir / "check.yaml").write_text(dump_check(self.check, project_root=root), encoding="utf-8")
        except Exception as error:
            self.log(f"check.yaml not written: {type(error).__name__}: {error}")
        from .report import write_report
        try:
            write_report(self.run_dir, result, scrub=self._scrub)
        except Exception as error:
            self.log("".join(traceback.format_exception(type(error), error, error.__traceback__)))
        result["timing"]["report"] = round(time.monotonic() - started, 2)
        result["seconds"] = round(time.monotonic() - self._t0, 1)
        self._write_json(result)

    def _write_json(self, result):
        text = self._scrub(json.dumps(result, indent=1, ensure_ascii=False, allow_nan=False))
        temporary = self.run_dir / "result.json.tmp"
        temporary.write_text(text + "\n", encoding="utf-8")
        os.replace(temporary, self.run_dir / "result.json")

    def _release(self):
        """Close the driver and release the lease, each at most once, whichever threads call it."""
        with self._release_lock:
            driver = None if self._driver_closed else self.driver
            if driver is not None:
                self._driver_closed = True
            lease, self.lease = self.lease, None
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass
        if lease is not None:
            try:
                lease.release()
            except Exception as error:
                self.log(f"lease release failed: {type(error).__name__}")

    def save_check(self, name, checks_dir=None):
        """Save this run's check as ``<checks_dir>/<name>.yaml`` (default: the runs folder's sibling ``checks/``).
        Returns the path."""
        return save_check_file(self.check, name, checks_dir or checks_dir_for(self.runs_dir))


def _safe(cancelled):
    try:
        return bool(cancelled())
    except Exception:
        return False


def _data_url_bytes(url):
    if not isinstance(url, str) or not url.startswith("data:") or "," not in url:
        return None
    try:
        return base64.b64decode(url.split(",", 1)[1])
    except (ValueError, TypeError):
        return None


def _halve(path):
    """Resize the first frame to half its pixel width (603 px from an iPhone 17 Pro's 1206); returns that width."""
    from PIL import Image
    with Image.open(path) as image:
        image = image.convert("RGB")
        width = max(1, image.width // 2)
        image.resize((width, max(1, round(image.height * width / image.width)))).save(path, "JPEG",
                                                                                         quality=FRAME_QUALITY)
    return width


def _shell_quote(text):
    return text if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", text) else "'" + text.replace("'", "'\\''") + "'"


# -- the one-call run --------------------------------------------------------------------------------------------

def resolve_mode(check, mode):
    """The mode a run uses: auto means Smart with steps, launch-only without."""
    if mode == "auto":
        return "smart" if check.steps else "launch"
    if mode not in MODES:
        raise ValueError(f"mode must be auto or one of {', '.join(MODES)}")
    return mode


def verify(check, *, mode="auto", runs_dir, manager=None, progress=None, key=None, cancelled=None,
           assert_timeout=5):
    """The whole run -> the result dict (§5.3). The CLI and the MCP `verify` tool use it. ctrl+c ends the run as
    couldnt_run (stopped), writes its artifacts, and raises KeyboardInterrupt again."""
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
    except CouldntRun as error:
        if run.error is None:
            run._fail(error)
    except KeyboardInterrupt as stop:
        # devtools.Terminated (SIGTERM, SIGHUP) is a KeyboardInterrupt too. The result rides on the exception, so
        # the CLI can still print it as JSON.
        name = getattr(stop, "signum", None)
        stop.result = run.abort("stopped", "Stopped with ctrl+c." if name is None else f"Stopped by {stop}.")
        raise
    except Exception as error:
        run.fail_internal(error)
    return run.finish()

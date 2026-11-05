"""The tools `mobster mcp` offers (SPEC §7.2 to §7.5, and devices: ship 2 SPEC §3)."""

import os
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit

from ..android.records import is_phone
from ..journal import JournalError
from ..transport import request_once
from .action_tools import ActionTools
from .approvals import ApprovalTools
from .catalog import (
    ANNOTATIONS,
    CORE_TOOL_NAMES,
    INSTRUCTIONS,
    NO_SMART_KEY,
    SIMULATOR_PROVIDERS,
    provider_of,
    smart_sentence,
    titled,
)
from .codes import CodeVault
from .device_tools import DeviceTools
from .phone_tools import PhoneTools

# The names other packages import from this module, whether or not it uses them itself.
from .protocol import ACTS, INVALID_PARAMS, LOCAL, READ_ONLY, ProtocolError, ToolResult  # noqa: F401
from .registry import FILE_TOOLS, providers
from .run_tools import RunTools
from .schemas import WAIT_MAX, build_tools, drop_nulls, validate
from .screen_tools import ScreenTools
from .support import (  # noqa: F401
    INLINE_QUALITY,
    Busy,
    ToolError,
    binary_stamp,
    cap_text,
    inline_jpeg,
    jsonable,
    sentence,
)

# Every limit counts from when the request arrived (protocol.Call.started), not from when the tool began.
PREPARE_WAIT = 40.0
CALL_BUDGET = 44.0
# What `wait` keeps of WAIT_MAX for describing a ready run: the lock, an evidence read (4 s measured under
# load) and the first frame.
WAIT_HEADROOM = 6.0
# What `wait_for` keeps of CALL_BUDGET after its wait: evaluate's last read past its deadline and the outline.
EVALUATE_RESERVE = 8.0
IDLE_LIMIT = 15 * 60
# A device driven directly (no check) is released after 90 idle seconds: it holds the device's lease, which keeps
# the Mac app, `mobster run` and scheduled tasks off that phone. The next call on it opens it again.
DIRECT_IDLE_LIMIT = 90
# The simulator a session leased for itself is kept longer: the agent is likely still working in it.
SESSION_SIM_IDLE_LIMIT = 30 * 60
SESSION_LIMIT = 60 * 60
LOCK_WAIT = 30.0
DESCRIBE_LOCK_WAIT = 1.0
KEEP_FINISHED = 20
REAP_SECONDS = 15.0
# A track adds one sentence to the server's instructions. A longer one is left out whole, never cut mid-sentence.
PROVIDER_SENTENCE_MAX = 300


class VerifyAPI:
    def check_from_dict(self, data):
        from ..verify.checks import check_from_dict
        return check_from_dict(data)

    def parse_assertion(self, data):
        from ..verify.assertions import parse_assertion
        return parse_assertion(data)

    def parse_selector(self, data):
        from ..verify.assertions import parse_selector
        return parse_selector(data)

    def evaluate_on(self, assertions, tree):
        from ..verify.assertions import evaluate_on
        return evaluate_on(assertions, tree)

    def new_run(self, check, **options):
        from ..verify.runner import VerifyRun
        return VerifyRun(check, **options)


def default_manager(progress):
    from ..sim import SimulatorManager
    return SimulatorManager(progress=progress)


class ToolSet(RunTools, DeviceTools, ScreenTools, ApprovalTools, ActionTools, PhoneTools):
    def __init__(self, *, runs_dir, key=None, keyless=False, smart_model=None, smart_off=None, device=None,
                 runtime=None, api=None, manager=None,
                 manager_factory=None, log=None, clock=time.monotonic, imager=inline_jpeg,
                 prepare_wait=PREPARE_WAIT, call_budget=CALL_BUDGET, wait_max=WAIT_MAX, wait_headroom=WAIT_HEADROOM,
                 evaluate_reserve=EVALUATE_RESERVE, idle_limit=IDLE_LIMIT, direct_idle_limit=DIRECT_IDLE_LIMIT,
                 session_sim_idle_limit=SESSION_SIM_IDLE_LIMIT,
                 session_limit=SESSION_LIMIT,
                 lock_wait=LOCK_WAIT, describe_lock_wait=DESCRIBE_LOCK_WAIT, version=None, devices=None,
                 device_run=None, allow_devices=None, device_runs_dir=None, wda_get=None, installer=None,
                 allow_files=False, allow_testflight=None, allow_photos=False, phone_files=None, simctl=None):
        self.runs_dir = Path(runs_dir)
        # The agent inside Mobster.app answers only its read-only tools and stop without a plan (plan_gate).
        from .. import plan_gate
        self.plan_gate = plan_gate.bundled_gate()
        self.device_runs_dir = Path(device_runs_dir) if device_runs_dir else self.runs_dir
        self._key = key or None
        self.keyless = bool(keyless)
        self.smart_available = bool(self._key) and not self.keyless
        if self.smart_available and not smart_model:
            from ..engines import smart_model as chosen
            smart_model = chosen()
        self.smart_model = smart_model if self.smart_available else None
        self.smart_off = (None if self.smart_available else "--keyless" if self.keyless
                          else smart_off or NO_SMART_KEY)
        self.device, self.runtime = device, runtime
        self._binary = binary_stamp()
        # `mobster mcp --allow-device NAME`: the only real (non-simulator) devices this server drives, by id, UDID
        # or name; None (no flag) allows every device. --device's own device is allowed too.
        self.allow_devices = None
        if allow_devices:
            self.allow_devices = {str(name).strip().casefold() for name in allow_devices if str(name).strip()}
            if device:
                self.allow_devices.add(str(device).strip().casefold())
        self.allow_files = bool(allow_files)
        # `mobster mcp --allow-testflight BUNDLE`: apps whose TestFlight build run_tests with pr may drive as the user's
        # own, on an iPhone --allow-device names. The agent can't add to it.
        self.allow_testflight = frozenset(str(item).strip() for item in allow_testflight or () if str(item).strip())
        # The camera roll is the user's most private folder: pull_media is listed only with --allow-photos.
        self.allow_photos = bool(allow_photos)
        self._phone_files, self._simctl = phone_files, simctl
        self.api = api or VerifyAPI()
        self._manager_instance = manager
        self._manager_factory = manager_factory or default_manager
        self.log = log or (lambda message: None)
        self.clock = clock
        self.imager = imager
        self.prepare_wait, self.call_budget, self.wait_max = prepare_wait, call_budget, wait_max
        self.wait_headroom, self.evaluate_reserve = wait_headroom, evaluate_reserve
        self.idle_limit, self.session_limit, self.lock_wait = idle_limit, session_limit, lock_wait
        self.direct_idle_limit = direct_idle_limit
        self.session_sim_idle_limit = session_sim_idle_limit
        self.describe_lock_wait = describe_lock_wait
        if version is None:
            from .. import __version__ as version
        self.version = version
        self.sessions = {}
        self.device_sessions = {}
        self._own_sim = None  # the UDID of the simulator this session leased when no device was named
        self._devices_source = devices
        self._device_run = device_run
        self._devices_cache = (0.0, None)
        self._ownership_source = None
        self._ownership_cache = (0.0, {})
        self._engine_source = None
        self._wda_get = wda_get or _wda_get
        self._installer = installer
        self._workers = []
        self._route = None
        self._lock = threading.RLock()
        self.codes = CodeVault(self._lock, self._open_sessions, quality=INLINE_QUALITY)
        self._closing = threading.Event()
        self._reaper = None
        self._tools = build_tools(self)

    def instructions(self):
        text = INSTRUCTIONS + (" " + smart_sentence(self.smart_model) if self.smart_available else "")
        for provider in providers():
            try:
                extra = provider.instructions()
            except Exception:
                self.log(f"The {getattr(provider, 'name', '?')} tools' instructions failed: "
                         + traceback.format_exc(limit=3))
                continue
            line = " ".join(extra.split()) if isinstance(extra, str) else ""
            if line and not line.endswith((".", "?", "!")):
                line += "."
            if len(line) > PROVIDER_SENTENCE_MAX:
                self.log(f"The {getattr(provider, 'name', '?')} tools' instructions are longer than "
                         f"{PROVIDER_SENTENCE_MAX} characters, so Mobster left them out.")
            elif line:
                text += " " + line
        return text

    def smart_state(self):
        if self.smart_available:
            return f"on ({self.smart_model}, on the user's {provider_of(self.smart_model)} key)"
        return f"off ({self.smart_off})"

    def list_tools(self):
        return [titled({"name": name, "description": description, "inputSchema": schema},
                       dict(ANNOTATIONS.get(name, ACTS)))
                for name, (description, schema, _handler, _budget) in self._tools.items()] + [
            titled(definition, annotations) for _provider, definition, annotations in self._provided()]

    def _provided(self):
        out, taken = [], set(self._tools) | CORE_TOOL_NAMES
        for provider in providers():
            try:
                definitions = list(provider.definitions(self) or ())
                annotations = dict(provider.annotations() or {})
            except Exception:
                self.log(f"The {getattr(provider, 'name', '?')} tools could not be listed: "
                         + traceback.format_exc(limit=3))
                continue
            for definition in definitions:
                name = definition.get("name") if isinstance(definition, dict) else None
                if not isinstance(name, str) or name in taken or not isinstance(definition.get("inputSchema"), dict):
                    continue
                if name in FILE_TOOLS and not self.allow_files:
                    continue
                taken.add(name)
                kept = {"name": name, "description": str(definition.get("description") or ""),
                        "inputSchema": definition["inputSchema"]}
                if isinstance(definition.get("title"), str) and definition["title"].strip():
                    kept["title"] = definition["title"].strip()[:80]
                if isinstance(definition.get("outputSchema"), dict):
                    kept["outputSchema"] = definition["outputSchema"]
                out.append((provider, kept,
                            dict(annotations.get(name, ACTS))))
        return out

    def plan_refusal(self, name, annotations, arguments=None, provider=None):
        # A license it can't read counts as no plan; a target it can't place counts as a phone.
        from .. import plan_gate
        if name == "stop":
            return None
        stopped = plan_gate.stopped(plan_gate.TOOL)  # a failed seal or a CI token that refuses: the reads too
        if stopped is not None:
            return ToolResult(stopped, is_error=True)
        if self.plan_gate is None or not plan_gate.mcp_needs_plan(name, annotations):
            return None
        message = self.plan_gate.refusal()
        if message is None or not self._drives_real_phone(name, arguments or {}, provider):
            return None
        return ToolResult(message, is_error=True)

    def _drives_real_phone(self, name, arguments, provider=None):
        if provider is not None:
            return getattr(provider, "name", None) not in SIMULATOR_PROVIDERS
        try:
            return self._targets_phone(arguments)
        except Exception:
            return True

    def _targets_phone(self, arguments):
        run_id, device = arguments.get("run_id"), arguments.get("device")
        if run_id:
            session = self.sessions.get(run_id) or next(
                (item for item in self._direct_sessions(open_only=False) if item.run_id == run_id), None)
            if session is None:
                return False  # no such run: the tool says so without touching a device
            record = getattr(session.run, "record", None)
            if not isinstance(record, dict) and getattr(session.run, "device_id", None):
                record = self._named_device(session.run.device_id)
                if record is None:
                    return True
            return isinstance(record, dict) and is_phone(record)
        target = device or self.device
        if not target:
            return False  # a check on the default simulator, or a call that names nothing to drive
        record = self._named_device(target)
        return record is None or is_phone(record)

    def _call_provided(self, name, arguments, call):
        provided = next(((p, d, a) for p, d, a in self._provided() if d["name"] == name), None)
        if provided is None:
            return None
        provider, definition, annotations = provided
        arguments = drop_nulls(arguments)
        validate(definition["inputSchema"], arguments)
        refusal = self.plan_refusal(name, annotations, arguments, provider)
        if refusal is not None:
            return refusal
        try:
            result = self._bounded(name, lambda: provider.call(name, arguments, call, self),
                                   self.call_budget, run_id=lambda: arguments.get("run_id"),
                                   started=self._started(call))
            if provider.name == "testkit" and name == "compare_test_suites":
                from ..testkit.mcp_comparison import mask_comparison
                return mask_comparison(self.codes.mask, arguments, result)
            if ((provider.name == "threads" and name in ("phone_task", "phone_task_status"))
                    or (provider.name == "workflows" and
                        (name in ("run_workflow", "graph_status") or name.startswith("workflow_")))):
                from ..threads.mcp import mask_handles
                return mask_handles(self.codes.mask, name, arguments, result)
            return self.codes.mask(result)
        except ToolError as error:
            return self.codes.mask(ToolResult(cap_text(str(error)), is_error=True))
        except ProtocolError:
            raise
        except Exception as error:
            self.log(f"{name} failed: " + traceback.format_exc(limit=8))
            return ToolResult(self._unexpected(name, error), is_error=True)

    def _unexpected(self, name, error):
        if binary_stamp() != self._binary:
            return (f"{name} failed because Mobster was updated after this server started. Reconnect it: run /mcp "
                    "in Claude Code, or restart your agent app.")
        return f"{name} hit an unexpected error ({type(error).__name__}). The server's log on stderr has the details."

    def call(self, name, arguments, call):
        entry = self._tools.get(name)
        if entry is None and name not in CORE_TOOL_NAMES:
            provided = self._call_provided(name, arguments, call)
            if provided is not None:
                return provided
        if entry is None:
            raise ProtocolError(INVALID_PARAMS, f"Mobster has no tool named {name[:80]}.")
        _description, schema, handler, budget = entry
        arguments = drop_nulls(arguments)
        validate(schema, arguments)
        refusal = self.plan_refusal(name, ANNOTATIONS.get(name, ACTS), arguments)
        if refusal is not None:
            return refusal
        try:
            self.reap()
        except Exception:
            self.log("Reaping before a call failed: " + traceback.format_exc(limit=5))
        try:
            return self.codes.mask(self._bounded(name, lambda: handler(arguments, call), budget,
                                            run_id=lambda: arguments.get("run_id") or getattr(call, "run_id", None),
                                            started=self._started(call)))
        except ToolError as error:
            return self.codes.mask(ToolResult(cap_text(str(error)), is_error=True))
        except JournalError as error:
            return ToolResult(sentence(error) + " Check disk space and run-folder permissions, then inspect the "
                              "screen before another action.", is_error=True)
        except ProtocolError:
            raise
        except Exception as error:
            self.log(f"{name} failed: " + traceback.format_exc(limit=8))
            return ToolResult(self._unexpected(name, error), is_error=True)

    def start(self, interval=REAP_SECONDS):
        if self._reaper is None:
            self._reaper = threading.Thread(target=self._reap_loop, args=(interval,), daemon=True,
                                            name="mobster-mcp-reaper")
            self._reaper.start()

    def _reap_loop(self, interval):
        while not self._closing.wait(interval):
            try:
                self.reap()
            except Exception:
                self.log("Reaper failed: " + traceback.format_exc(limit=5))

    def shutdown(self, reason="the MCP client disconnected", grace=10.0):
        self._closing.set()
        message = f"Mobster stopped the run because {reason}."
        end = time.monotonic() + grace
        for session in [s for s in self._all_sessions() if s.open]:
            session.stop_reason = session.stop_reason or message
            session.cancelled.set()
            if session.state in ("running", "finishing"):
                session.wait_while(("running", "finishing"), max(0.0, end - time.monotonic()))
            if session.state == "preparing":
                self._abort(session, "stopped", message)  # VerifyRun.abort stops a run still preparing
            elif session.open:
                locked = session.run.lock.acquire(timeout=max(0.1, min(5.0, end - time.monotonic())))
                try:
                    self._abort(session, "stopped", message)
                finally:
                    if locked:
                        session.run.lock.release()
        for session in self._direct_sessions():
            self._close_direct(session, message, timeout=max(0.1, min(5.0, end - time.monotonic())))
        # A first WebDriverAgent build runs in its own process group: stop it rather than leave xcodebuild behind.
        cancel = getattr(getattr(self._manager_instance, "wda", None), "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                self.log("Stopping the WebDriverAgent build failed: " + traceback.format_exc(limit=3))
        self.log("Every run is stopped.")

    def reap(self, now=None):
        now = self.clock() if now is None else now
        for session in self._all_sessions():
            if not session.open or session.busy:
                continue
            idle = session.mode == "keyless" and now - session.last_used >= self.idle_limit
            too_long = now - session.started >= self.session_limit
            if not (idle or too_long):
                continue
            reason = ("The session was idle for 15 minutes, so Mobster stopped it." if idle
                      else "The session reached its 60-minute limit, so Mobster stopped it.")
            session.stop_reason = session.stop_reason or reason
            if session.mode == "smart" and session.state != "preparing":
                session.cancelled.set()
            elif session.state == "preparing":
                self.log(f"Run {session.run_id}: {reason}")
                session.cancelled.set()
                self._abort(session, "stopped", reason)
            elif session.state == "ready" and session.run.lock.acquire(timeout=0):
                try:
                    self.log(f"Run {session.run_id}: {reason}")
                    self._abort(session, "stopped", reason)
                finally:
                    session.run.lock.release()
        for session in self._direct_sessions():
            if self._own_sim and session.run.device_id == self._own_sim:
                self._reap_session_simulator(session, now)
            elif not session.busy and now - session.last_used >= self.direct_idle_limit:
                self._close_direct(session, f"The device was idle for {self.direct_idle_limit:g} seconds, so Mobster "
                                            "released it for the Mobster app and other commands. The next call on it "
                                            "opens it again.", timeout=0)
        with self._lock:
            finished = [s for s in self.sessions.values() if not s.open]
            for session in finished[:max(0, len(finished) - KEEP_FINISHED)]:
                self.sessions.pop(session.run_id, None)

    def _reap_session_simulator(self, session, now):
        if session.busy:
            return
        from ..sim import holders
        data_dir = getattr(self._manager_instance, "data_dir", None)
        asked = data_dir is not None and holders.release_requested(data_dir, session.run.device_id)
        if asked or now - session.last_used >= self.session_sim_idle_limit:
            self._close_direct(session, "Mobster released this session's simulator "
                                        + ("because `mobster sim release` asked." if asked else
                                           f"after {self.session_sim_idle_limit / 60:g} idle minutes.")
                                        + " The next call that names no device leases one again.", timeout=0)

    @staticmethod
    def _started(call):
        started = getattr(call, "started", None)
        return started if isinstance(started, (int, float)) and not isinstance(started, bool) else time.monotonic()

    def _left(self, call, seconds):
        return max(0.0, seconds - (time.monotonic() - self._started(call)))

    def _bounded(self, name, work, seconds, run_id=None, started=None):
        started = time.monotonic() if started is None else started
        box, done = {}, threading.Event()

        def target():
            try:
                box["result"] = work()
            except BaseException as error:  # handed to the caller's thread
                box["error"] = error
            finally:
                done.set()
        threading.Thread(target=target, daemon=True, name=f"mobster-mcp-{name}").start()
        if not done.wait(max(0.0, seconds - (time.monotonic() - started))):
            if name == "install_app":
                raise ToolError(f"install_app is still working after {seconds:.0f} s and may finish in the background. "
                                "Its outcome is unknown. Do not retry the installation. Inspect the device and the "
                                "installation run's actions.jsonl before another installation.")
            known = run_id() if run_id is not None else None
            if known:
                raise ToolError(f"{name} is still working after {seconds:.0f} s and finishes in the background. "
                                f"Call wait with run_id {known} to see where it ended.")
            raise ToolError(f"{name} is still working after {seconds:.0f} s and finishes in the background. "
                            "Call status to see whether a run is open.")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _manager(self):
        if self._manager_instance is None:
            with self._lock:
                if self._manager_instance is None:
                    self._manager_instance = self._manager_factory(self._manager_progress)
                    self._manager_instance.who = self._who
        return self._manager_instance

    def _who(self):
        # the holder record `mobster sim list` shows: this server's client, and the process that holds the lease
        from .. import ownership
        return {"client": ownership.clean_client(self.client_name()) or "an MCP client",
                "session": f"pid {os.getpid()}"}

    def _manager_progress(self, line):
        # A run stopped while it prepared keeps booting until its next phase: its lines must not reach the next run.
        self.log(str(line))
        session = self._route
        if session is not None:
            session.note(str(line))

    def client_name(self):
        client = getattr(self, "client", None) or {}
        return (client.get("name") or None) if isinstance(client, dict) else None

def _wda_get(url, timeout):
    parts = urlsplit(url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    return request_once(f"{parts.scheme}://{parts.netloc}", "GET", path, timeout=timeout).get("value")

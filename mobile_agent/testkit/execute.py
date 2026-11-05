"""One attempt of one check, a verify run; Smart's approver is check_approver, never a person's approval."""

import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from ..verify.runner import CouldntRun, VerifyRun, resolve_mode
from .targets import Refused, validate_record

VIDEO_STOP_WAIT = 10.0


class ReplayPin:
    """One check/target's effective contract, evidence assets and approved replay revision."""

    def __init__(self, check):
        self.declared = deepcopy(check)
        self.effective = None
        self.assets = None
        self.selected = None
        self.invalid = False
        self.lock = threading.Lock()

    def compare_declared(self, check):
        with self.lock:
            if check != self.declared:
                self.invalid = True

    def bind_check(self, check, live):
        with self.lock:
            if self.invalid:
                return "The check changed during its repeated runs. Start a new suite."
            if check != live:
                return "A repeated check uses expanded project secrets. Run it without --repeat."
            if self.effective is None:
                self.effective = deepcopy(check)
            elif check != self.effective:
                return "The effective check or its fixtures changed during repeated runs. Start a new suite."
        return None

    def bind_assets(self, proof):
        with self.lock:
            if self.invalid or self.effective is None:
                return "The repeated check was not frozen before its evidence was loaded. Start a new suite."
            if self.assets is None:
                self.assets = deepcopy(proof)
            elif proof != self.assets:
                return "A fixture, condition or assertion asset changed during repeated runs. Start a new suite."
        return None

    def bind(self, identity, env, revision):
        with self.lock:
            if self.invalid or self.effective is None or self.assets is None:
                return "The repeated check was not frozen before replay admission. Start a new suite."
            current = (deepcopy(identity), deepcopy(env), revision)
            if self.selected is None:
                self.selected = current
            elif current != self.selected:
                return "The approved replay path, app or device environment changed during repeated runs. " \
                       "Start a new suite."
        return None


class Attempts:
    """Runs attempts."""

    def __init__(self, *, runs_dir, keyless=False, key=None, assert_timeout=5, parallel=1, progress=None,
                 stop=None, video_dir=None, simulators=None, devices=None, run_class=VerifyRun, popen=None,
                 replay="off"):
        self.runs_dir = Path(runs_dir)
        self.keyless, self.key, self.assert_timeout = keyless, key, assert_timeout
        self.parallel = max(1, int(parallel))
        self.progress = progress or (lambda line: None)
        self.stop = stop or threading.Event()
        self.video_dir = Path(video_dir) if video_dir else None
        self._simulators = simulators
        self._devices = devices or _device_manager
        self.run_class = run_class
        self.replay = replay
        self.popen = popen or subprocess.Popen
        self.gate = None
        self._repeat_pins = {}
        self._reserved = {}
        self._live = set()
        self._lock = threading.Lock()

    def bind_gate(self, gate):
        self.gate = gate

    @staticmethod
    def _repeat_key(check, record):
        target = record.get("udid") or record.get("id") if record else (check.device, check.runtime)
        locale = (check.conditions or {}).get("locale")
        return check.source or id(check), locale, target

    def bind_repeat(self, check, record):
        key = self._repeat_key(check, record)
        with self._lock:
            pin = self._repeat_pins.get(key)
            if pin is None:
                self._repeat_pins[key] = ReplayPin(check)
            else:
                pin.compare_declared(check)

    def simulators(self):
        with self._lock:
            if self._simulators is None:
                self._simulators = suite_simulators(self.parallel, self.progress)
            return self._simulators

    def manager_for(self, record):
        """The manager a run uses: the shared simulator manager, pinned to a Mobster simulator a record names, or a
        WdaDevice for a real device."""
        if record is None:
            return self.simulators()
        validate_record(record)
        if record.get("kind") == "android":
            from ..android.manager import AndroidManager
            from ..paths import dev_data_dir
            return AndroidManager(dev_data_dir(), name=record.get("avd"))
        if record.get("kind") == "simulator":
            borrowed = self._reserved.get(record["udid"])
            if borrowed is not None:
                return borrowed
            from ..device_targets import PinnedSimulator
            return PinnedSimulator(self.simulators(), record["udid"])
        return self._devices(record)

    @contextmanager
    def reserve_simulators(self, records, journal):
        """Hold distinct actor leases across every phase; VerifyRun receives a no-op borrowed lease."""
        from ..device_targets import PinnedSimulator
        from ..verify.actions import ActionError
        from .targets import Refused

        acquired = []
        try:
            for record in sorted(records, key=lambda item: item["udid"]):
                if self.stop.is_set():
                    raise ActionError("stopped")
                manager = PinnedSimulator(self.simulators(), record["udid"])
                with journal.action("ACQUIRE"):
                    lease = manager.acquire(timeout=0, cancelled=self.stop.is_set)
                    acquired.append((record["udid"], manager, lease))
                target = lease.target
                expected_model = record.get("modelName") or record.get("model")
                expected_runtime = f"iOS {record['ios']}" if record.get("ios") else None
                if (target.udid.casefold() != record["udid"].casefold() or
                        expected_model and target.device_type != expected_model or
                        expected_runtime and target.runtime != expected_runtime):
                    raise Refused("A scenario actor's simulator changed since admission. Start a new scenario.",
                                  "refused")
            self._reserved = {udid: BorrowedSimulator(manager, lease, self.stop)
                              for udid, manager, lease in acquired}
            yield
        finally:
            reserved, self._reserved = self._reserved, {}
            for borrowed in reserved.values():
                borrowed.active = False
            for _, _, lease in reversed(acquired):
                lease.release()

    def __call__(self, check, record, *, label, video_name=None):
        """The verify result of one attempt of ``check`` (on ``record``'s device, or a simulator)."""
        with self._lock:
            pin = self._repeat_pins.get(self._repeat_key(check, record))
        if pin is not None:
            pin.compare_declared(check)
        from ..verify.fixtures import FixtureError, for_run
        try:
            check = for_run(check, self.runs_dir)
        except FixtureError as error:
            run = self.run_class(check, mode="launch", runs_dir=self.runs_dir, manager=self.manager_for(record),
                                 progress=lambda line: self.progress(f"{label}: {line}"), key=self.key,
                                 assert_timeout=self.assert_timeout)
            return run.abort("fixture", str(error))
        manager = self.manager_for(record)
        video = None
        if self.video_dir is not None and video_name and (record is None or record.get("kind") in ("simulator", "usb")):
            video = self.video_dir / f"{video_name}.mp4"
            if record is not None and record.get("kind") == "usb":
                from .phone_video import PhoneRecording
                manager = PhoneRecording(manager, video, log=self.progress)
            else:
                manager = Recording(manager, video, popen=self.popen, log=self.progress)
        mode = "launch" if self.keyless else resolve_mode(check, "auto")
        extra = {"replay": self.replay} if self.replay != "off" and mode == "smart" else {}
        if check.business is not None and check.source is not None:
            from ..verify.business import load_business_binding
            from ..verify.checks import CheckError, project_root
            try:
                extra["business_binding"] = load_business_binding(check, project_root(check.source))
            except (CheckError, OSError):
                # VerifyRun writes the fixture refusal with its normal run evidence.
                pass
        run = self.run_class(check, mode=mode, runs_dir=self.runs_dir, manager=manager,
                             progress=lambda line: self.progress(f"{label}: {line}"), key=self.key,
                             assert_timeout=self.assert_timeout, **extra)
        run.repeat_pin = pin
        if pin is not None and getattr(run, "replay", None) is not None:
            run.replay.pin = pin
        with self._lock:
            self._live.add(run)
        try:
            result = self._run(run, check, mode, record, pin=pin)
        finally:
            with self._lock:
                self._live.discard(run)
        if video is not None and video.is_file() and video.stat().st_size > 0:
            result["video"] = str(video)
            started, t0 = getattr(manager, "started", None), getattr(run, "_t0", None)
            if isinstance(started, (int, float)) and isinstance(t0, (int, float)):
                # A step's at_ms counts from the run's start; the recording starts when the device is held.
                result["video_offset_ms"] = round((started - t0) * 1000)
        return result

    def _run(self, run, check, mode, record=None, *, pin=None):
        if self.stop.is_set():
            return run.abort("stopped", "The suite was stopped before this ran.")
        if check.business is not None and record is not None and record.get("kind") != "simulator":
            return run.abort("business", "App-state fixtures run only on managed simulators.")
        if check.steps and mode == "launch":
            return run.abort("usage", "--keyless never calls a model, so a check with steps can't run.",
                             "Drop --keyless, or give the check no steps (launch-only).")
        if mode == "smart":
            from ..engines import smart_key
            from ..verify.smart import no_smart_key
            run.key = self.key or smart_key()
            if not run.key and getattr(run, "replay", None) is None:
                why, fix = no_smart_key()
                return run.abort("usage", f"This check's steps need Smart, and {why}.", fix)
        held = None
        notes = []
        try:
            if record is not None and record.get("kind") not in ("simulator", "android"):
                if self.gate is None:
                    return run.abort("device", "Mobster can't check this phone's app without a device gate.")
                run._apply_fixtures()
                run._refuse_fixtures_on_phone()
                if (run.check.throughout or run.check.somewhere) and (pin is not None or
                        self.replay == "strict" and run.replay is not None):
                    reason = "A fixed replay cannot cover throughout or somewhere assertions on every visited screen."
                    if run.replay is not None:
                        run.replay.refuse_pin(reason)
                    raise CouldntRun("replay", reason)
                # Ordinary phone runs expand secrets during VerifyRun.prepare, after admission. Fixed replay must
                # reject them before acquiring a device or installing an app.
                if pin is not None or self.replay == "strict" and run.replay is not None:
                    run._load_secrets()
                if self.replay == "strict" and mode == "smart" and run.replay is None:
                    return run.abort("replay", "Strict replay cannot use expanded project secrets.")
                if pin is not None and run._live != run.check:
                    return run.abort("replay", "A repeated check uses expanded project secrets.")
                check = run.check
                fixed = pin is not None or self.replay == "strict" and run.replay is not None
                if fixed and check.app_path:
                    from .targets import device_build
                    if device_build(check.app_path) is not False:
                        reason = "A fixed phone replay cannot install an unverified app revision. Install the build " \
                                 "first, then run the check by app bundle."
                        if run.replay is not None:
                            run.replay.refuse_pin(reason)
                        raise CouldntRun("replay", reason)
                manager = run.manager
                with run.actions.action("ACQUIRE"):
                    held = manager.acquire(check.device, check.runtime, cancelled=self.stop.is_set)
                if self.stop.is_set():
                    return run.abort("stopped", "The suite was stopped before this ran.")
                candidate = (self.gate.normalized_check(check, record)
                             if pin is not None or self.replay == "strict" and run.replay is not None else None)
                if candidate is not None:
                    run.check = run._live = candidate
                    run.target = held.target
                    run.app = manager.installed_app(held.target, candidate.bundle_id)
                    if not getattr(run.app, "version", None):
                        reason = "A fixed phone replay needs the installed app's version before it can use a path."
                        if run.replay is not None:
                            run.replay.refuse_pin(reason)
                        raise CouldntRun("replay", reason)
                if pin is not None:
                    from ..replay_core.heal import HealError, proof_assets
                    reason = pin.bind_check(candidate, candidate)
                    if reason:
                        if run.replay is not None:
                            run.replay.refuse_pin(reason)
                        raise CouldntRun("replay", reason)
                    try:
                        proof = proof_assets(check)
                    except HealError as error:
                        if run.replay is not None:
                            run.replay.refuse_pin(str(error))
                        raise CouldntRun("replay", str(error)) from None
                    reason = pin.bind_assets(proof)
                    if reason:
                        if run.replay is not None:
                            run.replay.refuse_pin(reason)
                        raise CouldntRun("replay", reason)
                if run.replay is not None and candidate is not None:
                    from ..replay_core.checks import CheckReplay
                    run.replay = CheckReplay(run, self.replay)
                    run.replay.pin = pin
                    if run.replay.lookup() is None:
                        run.replay.refuse_pin(f"Fixed replay cannot run: {run.replay.reason or 'no approved path'}.")
                elif pin is not None:
                    from ..replay_core.checks import identity
                    from ..replay_core.store import env_of
                    model = getattr(held.target, "device_type", None)
                    environment = env_of(getattr(run.app, "version", None), getattr(held.target, "runtime", None),
                                         "iphone", device_type=None if model == "iPhone" else model)
                    reason = pin.bind(identity(candidate),
                                      {**environment, "runtime": getattr(held.target, "runtime", None)}, "")
                    if reason:
                        raise CouldntRun("replay", reason)
                admitted, notes, _ = self.gate.admit(check, record, cancelled=self.stop.is_set,
                                                     actions=run.actions)
                if self.stop.is_set():
                    return run.abort("stopped", "The suite was stopped before this ran.")
                if candidate is not None and admitted != candidate:
                    return run.abort("device", "The phone app changed during admission.")
                run.check = run._live = check = admitted
                run.manager = HeldDevice(manager, held)
                if run.replay is not None and candidate is None:
                    from ..replay_core.checks import CheckReplay
                    run.replay = CheckReplay(run, self.replay)
            return self._drive(run, check, mode, notes)
        except Exception as error:
            from ..verify.actions import ActionNotDispatched
            if isinstance(error, ActionNotDispatched):
                result = run.abort(error.kind, str(error), error.fix)
                result["not_dispatched"] = True
                return result
            if isinstance(error, (CouldntRun, Refused)):
                return run.abort(error.klass, error.message, error.fix)
            klass = getattr(error, "klass", None)
            if klass in ("journal", "unknown_outcome", "stopped"):
                return run.abort(klass, str(error))
            kind = getattr(error, "kind", None)
            if isinstance(kind, str):
                return run.abort(kind, str(error), getattr(error, "fix", "") or "")
            raise
        finally:
            if held is not None:
                held.release()

    def _drive(self, run, check, mode, notes):
        try:
            run.prepare()
            if mode == "smart" and getattr(run, "replay", None) is not None:
                if not run.key and run.replay.mode != "strict" and run.replay.lookup() is None:
                    # A recorded path replays with no key; recording one needs Smart.
                    from ..verify.smart import no_smart_key
                    why, fix = no_smart_key()
                    raise CouldntRun("usage", f"This check's steps need Smart, and {why}. It has no recorded "
                                              f"path to replay yet ({run.replay.reason}).", fix)
                run.run_flow(self.stop.is_set)
            elif mode == "smart":
                run.run_smart(self.stop.is_set)
            if check.surfaces:   # after the flow, as runner.verify does
                from ..verify.system_surfaces.play import drive as drive_surfaces
                drive_surfaces(run)
        except CouldntRun as error:
            if run.error is None:
                run._fail(error)
        except Exception as error:  # the run's own failure, never the suite's
            run.fail_internal(error)
        if self.stop.is_set() and run.result is None and run.error is None:
            run._fail(CouldntRun("stopped", "The suite was stopped."))
        result = run.finish()
        if notes:
            result["notes"] = notes
        return result

    def abort_all(self, message="The suite was stopped."):
        """Stop every run in flight: one still preparing stops at its next phase, a Smart run at its next step."""
        self.stop.set()
        with self._lock:
            live = list(self._live)
        for run in live:
            threading.Thread(target=_abort, args=(run, message), daemon=True, name="mobster-test-abort").start()


def _abort(run, message):
    try:
        if run.state in ("new", "preparing"):
            run.abort("stopped", message)
    except Exception:
        pass


def _device_manager(record):
    from ..device_targets import WdaDevice
    return WdaDevice(record)


class HeldDevice:
    """The run adopts the phone lease held through app admission."""

    def __init__(self, manager, lease):
        self.manager, self.lease = manager, lease
        self._taken = False

    def __getattr__(self, name):
        return getattr(self.manager, name)

    def acquire(self, device=None, runtime=None, *, timeout=600, cancelled=None):
        if cancelled is not None and cancelled():
            from ..errors import SimError
            from ..verify.actions import ActionNotDispatched
            raise ActionNotDispatched(SimError("stopped", "The suite was stopped before this device ran."))
        if self._taken:
            raise RuntimeError("The phone lease was already handed to this run")
        self._taken = True
        return self.lease


class BorrowedSimulator:
    """A phase can release its run lease without releasing the scenario's root simulator lease."""

    def __init__(self, manager, root_lease, stop):
        self.manager, self.root_lease, self.stop = manager, root_lease, stop
        self.active = True

    def __getattr__(self, name):
        return getattr(self.manager, name)

    def acquire(self, device=None, runtime=None, *, timeout=600, cancelled=None):
        if not self.active or self.stop.is_set() or cancelled is not None and cancelled():
            from ..errors import SimError
            from ..verify.actions import ActionNotDispatched
            raise ActionNotDispatched(SimError("stopped", "The scenario was stopped before this actor ran."))
        from ..sim import SimLease
        return SimLease(self.root_lease.target)


def suite_simulators(parallel, progress):
    # Each run holds its simulator's lease, so two runs never share one.
    from ..sim import SimulatorManager

    class SuiteSimulators(SimulatorManager):
        @property
        def max_sims(self):
            return max(SimulatorManager.max_sims.fget(self), parallel)

    return SuiteSimulators(progress=progress)


class Recording:
    def __init__(self, manager, path, *, popen=subprocess.Popen, log=None, clock=time.monotonic):
        self._manager, self.path, self._popen = manager, Path(path), popen
        self._log = log or (lambda line: None)
        self._clock = clock
        self.started = None  # time.monotonic() when the recording began

    def __getattr__(self, name):
        return getattr(self._manager, name)

    def acquire(self, *args, **kwargs):
        lease = self._manager.acquire(*args, **kwargs)
        process = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            process = self._popen(video_argv(lease.target.udid, self.path), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self.started = self._clock()
        except Exception as error:
            self._log(f"no screen recording ({type(error).__name__})")
        except BaseException:
            lease.release()
            raise
        return RecordedLease(lease, process)


class RecordedLease:
    def __init__(self, lease, process):
        self._lease, self._process = lease, process
        self.target = lease.target

    def __getattr__(self, name):
        return getattr(self._lease, name)

    def release(self):
        process, self._process = self._process, None
        try:
            if process is not None:
                if isinstance(process, subprocess.Popen) or not callable(getattr(process, "stop", None)):
                    stop_recording(process)
                else:
                    process.stop()  # a phone recording (phone_video.Recorder) finishes its own file
        finally:
            self._lease.release()


def video_argv(udid, path):
    return ["xcrun", "simctl", "io", str(udid), "recordVideo", "--codec=h264", "--force", str(path)]


def stop_recording(process, wait=VIDEO_STOP_WAIT):
    """SIGINT, which makes simctl finish the movie; a kill after ``wait`` seconds."""
    try:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    except Exception:
        pass


__all__ = ["Attempts", "Recording", "suite_simulators", "video_argv", "stop_recording"]

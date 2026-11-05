"""Starting, waiting for, finishing and stopping a verify run, and describing a run to the agent."""

import os
import re
import threading
import time
import traceback
from pathlib import Path

from ..android.records import is_phone
from .protocol import ToolResult
from .session import Session
from .support import SHUTTING_DOWN, Busy, ToolError, cap_text, is_check_error, jsonable, navbar_hint, sentence

RELEASE_NOTE = "Waiting for the stopped run to release its simulator."

TASK_ID = re.compile(r"[a-f0-9]{12}")  # a Mobster task's id, as `mobster history` shows it


class RunTools:
    def _all_sessions(self):
        with self._lock:  # _new_session adds to the table from another call's thread
            return list(self.sessions.values())

    def _open_sessions(self):
        return list(self.sessions.values()) + list(self.device_sessions.values())

    def _open_session(self):
        return next((session for session in self._all_sessions() if session.open), None)

    def _session(self, run_id, check=False):
        session = self.sessions.get(run_id)
        if session is None:
            session = next((item for item in self._direct_sessions(open_only=False) if item.run_id == run_id), None)
        if session is None:
            raise ToolError(f"This server has no run {run_id}. Start one with verify_start.")
        if check and session.mode == "direct":
            raise ToolError(f"Run {run_id} drives {session.run.device_name} directly, not a check. "
                            f"Call stop with device {session.run.device_id} to release it.")
        session.touch()
        return session

    def _ready_session(self, run_id):
        session = self._session(run_id)
        if session.state == "ready":
            return session
        if session.state == "preparing":
            raise ToolError(f"Run {run_id} is still preparing. Call wait with run_id {run_id}.")
        if session.mode == "smart" and session.open:
            raise ToolError(f"Run {run_id} is a Smart run that Mobster drives. Call wait for its result.")
        if session.state == "finishing":
            raise ToolError(f"Run {run_id} is being judged. Call wait for its verdict.")
        if session.mode == "direct":
            raise ToolError(f"Run {run_id} on {session.run.device_name} is closed. Pass device again to open it.")
        verdict = (session.result or {}).get("verdict", "finished")
        raise ToolError(f"Run {run_id} has ended ({verdict}). Start a new one with verify_start.")

    def _check(self, args, *, smart):
        app_path = args.get("app_path")
        if app_path is not None and not os.path.isabs(app_path):
            raise ToolError("app_path must be an absolute path to the .app, because MCP clients start servers "
                            "in different folders.")
        if not app_path and not args.get("bundle_id"):
            raise ToolError("Pass app_path (the absolute path to the .app built for the iOS Simulator) or "
                            "bundle_id for an app already on the simulator.")
        steps = [step for step in args.get("steps") or []]
        name = args.get("name") or (steps[0] if steps else "Launch check")
        data = {"name": " ".join(name.split())[:120] or "Launch check"}
        app = {}
        if args.get("bundle_id"):
            app["bundle"] = args["bundle_id"]
        if app_path:
            app["path"] = app_path
        data["app"] = app
        for field, default in (("device", self.device), ("runtime", self.runtime)):
            if args.get(field) or default:
                data[field] = args.get(field) or default
        if args.get("reset"):
            data["reset"] = args["reset"]
        record = self._named_device(data.get("device"))
        if record is not None and record.get("platform") == "android":
            data["device"] = record.get("avd") or record["name"]
        if record is not None and is_phone(record):
            self._refuse_unless_allowed(record)
            if app_path:
                raise ToolError(f"“{record['name']}” runs apps already on it: pass bundle_id, not app_path.")
            if data.get("reset", "none") != "none":
                raise ToolError(f"Mobster never clears an app's data on “{record['name']}”: pass reset none.")
            data["reset"] = "none"
        launch = {}
        if args.get("launch_args"):
            launch["args"] = list(args["launch_args"])
        if args.get("launch_env"):
            launch["env"] = dict(args["launch_env"])
        if args.get("open_url"):
            launch["url"] = args["open_url"]
        if launch:
            data["launch"] = launch
        data["steps"] = steps
        data["expect"] = [self._assertion_data(item) for item in args.get("expect") or []]
        if smart:
            data["budget"] = {"max_usd": args.get("max_usd", 0.25), "max_seconds": args.get("max_seconds", 180)}
        try:
            check = self.api.check_from_dict(data)
        except Exception as error:
            if is_check_error(error):
                raise ToolError(sentence(error)) from None
            raise
        return check, record

    @staticmethod
    def _assertion_data(item):
        item = dict(item)
        # Some clients send untyped values as strings: a count's bound is a whole number.
        if "count" in item and isinstance(item.get("equals"), str) and item["equals"].strip().isdigit():
            item["equals"] = int(item["equals"].strip())
        return item

    def _parse_assertion(self, data):
        try:
            return self.api.parse_assertion(self._assertion_data(data))
        except Exception as error:
            if is_check_error(error):
                raise ToolError(sentence(error)) from None
            raise

    def _new_session(self, check, mode, image, call, record=None):
        holder = {}

        def progress(line):
            self.log(str(line))
            session = holder.get("session")
            if session is not None:
                session.note(str(line))
        with self._lock:
            if self._closing.is_set():
                raise ToolError(SHUTTING_DOWN)
            open_session = self._open_session()
            if open_session is not None:
                raise ToolError(f"Run {open_session.run_id} is still open. Call verify_finish or stop on it "
                                "before starting another.")
            if record is not None:
                direct = self.device_sessions.get(record["id"])
                if direct is not None and direct.open:
                    raise ToolError(f"{record['name']} is open in run {direct.run_id}, driven directly. Call stop "
                                    f"with device {record['id']} first.")
            manager = self._manager()
            if record is not None:
                from ..device_targets import manager_for
                manager = manager_for(record, simulators=manager)
            run = self.api.new_run(check, mode=mode, runs_dir=self.runs_dir, manager=manager,
                                   progress=progress, key=self._key if mode == "smart" else None)
            session = Session(run, mode, clock=self.clock, image=image)
            if record is not None:
                run.device_id = record["id"]  # list_devices and the phone tools' device find the run by it
                self.codes.seed(session, record["id"])
            holder["session"] = session
            self.sessions[session.run_id] = session
        call.run_id = session.run_id  # named in the error if the call runs past its budget
        return session

    def _start_worker(self, session, target):
        # A run stopped while it prepared holds its simulator until its next phase: waiting for earlier workers keeps
        # the next run from booting a second simulator.
        with self._lock:
            earlier = [worker for worker in self._workers if worker.is_alive()]
            session.worker = threading.Thread(target=self._work, args=(session, target, earlier), daemon=True,
                                              name=f"mobster-mcp-run-{session.run_id}")
            self._workers = earlier + [session.worker]
        if earlier:
            session.note(RELEASE_NOTE)  # before the call's first answer, which may come before the worker runs
            self.log(f"Run {session.run_id}: waiting for the stopped run to release its simulator.")
        session.worker.start()

    def _work(self, session, target, earlier):
        if not self._await_release(session, earlier):
            return
        with self._lock:
            self._route = session  # the manager's progress lines are this run's from here
        target(session)

    def _await_release(self, session, earlier, poll=.25):
        for worker in earlier:
            while worker.is_alive() and session.open and not session.cancelled.is_set():
                worker.join(poll)
        if session.open and not session.cancelled.is_set():
            return True
        if session.open:
            self._abort(session, "stopped", session.stop_reason or "Stopped by the agent.")
        return False

    def _prepare_keyless(self, session):
        run = session.run
        try:
            run.prepare()
        except BaseException as error:
            self._end_with_error(session, error)
            return
        if not session.open:
            return  # stopped while it prepared
        if session.stop_reason:
            self._abort(session, "stopped", session.stop_reason)
            return
        session.note("Ready.")
        session.touch()
        session.set_state("ready")
        self.log(f"Run {session.run_id} is ready.")

    def _run_smart(self, session):
        run = session.run
        try:
            run.prepare()
            if session.open and not session.cancelled.is_set():
                session.set_state("running")
                session.note("Running the steps.")
                run.run_smart(session.cancelled.is_set)
        except BaseException as error:
            if not session.open:
                return
            if session.cancelled.is_set():
                self._abort(session, "stopped", session.stop_reason or "Stopped by the agent.")
            else:
                self._end_with_error(session, error)
            return
        if not session.open:
            return
        if session.cancelled.is_set():
            self._abort(session, "stopped", session.stop_reason or "Stopped by the agent.")
            return
        self._finish(session)

    def _end_with_error(self, session, error):
        run = session.run
        if hasattr(error, "klass"):
            if getattr(run, "error", None):
                self._finish(session)
            else:
                self._abort(session, error.klass, str(error), getattr(error, "fix", ""))
        elif hasattr(error, "kind"):
            self._abort(session, error.kind, str(error), getattr(error, "fix", ""))
        elif is_check_error(error):
            self._abort(session, "usage", str(error))
        else:
            self.log(f"Run {session.run_id} failed: " + "".join(
                traceback.format_exception(type(error), error, error.__traceback__, limit=8)))
            self._abort(session, "internal", f"Mobster hit an unexpected error ({type(error).__name__}).")

    def _finish(self, session):
        if not session.open:
            return session.result
        try:
            result = session.run.finish()
        except BaseException as error:
            self.log(f"Run {session.run_id} couldn't finish: " + "".join(
                traceback.format_exception(type(error), error, error.__traceback__, limit=8)))
            klass = getattr(error, "klass", None) or "internal"
            return self._abort(session, klass, str(error) if hasattr(error, "klass")
                               else f"Mobster hit an unexpected error ({type(error).__name__}).")
        session.set_state("finished", jsonable(result))
        return session.result

    def _abort(self, session, klass, message, fix=""):
        if not session.open:
            return session.result
        try:
            result = session.run.abort(klass, message)
        except BaseException:
            self.log(f"Run {session.run_id} couldn't abort cleanly: " + traceback.format_exc(limit=5))
            result = self._bare_result(session, klass, message)
        result = jsonable(result)
        if fix and isinstance(result.get("reason"), dict) and not result["reason"].get("fix"):
            result["reason"]["fix"] = fix
        session.set_state("finished", result)
        return session.result

    def _bare_result(self, session, klass, message):
        run_dir = getattr(session.run, "run_dir", None)
        return {"schema": "mobster.verify/1", "run_id": session.run_id, "verdict": "couldnt_run", "exit_code": 3,
                "summary": message, "reason": {"class": klass, "message": message, "fix": None},
                "mode": session.mode, "assertions": [], "steps": list(getattr(session.run, "steps", []) or []),
                "frames": [], "proof": [], "run_dir": str(run_dir) if run_dir else None, "cost_usd": 0}

    def _wait(self, session, states, timeout, call):
        end = time.monotonic() + timeout
        while session.state in states:
            left = end - time.monotonic()
            if left <= 0 or call.stop.is_set():
                return False
            call.note(session.message)
            session.wait_while(states, min(1.0, left), call.stop)
        return True

    def _describe(self, session, call, image=None, lock_wait=None):
        image = session.image if image is None else image
        if session.state == "finished":
            return self._verdict(session.result, image)
        if session.mode == "smart":
            steps = session.steps_so_far()
            count = f"{len(steps)} step" + ("" if len(steps) == 1 else "s")
            return ToolResult(
                f"Run {session.run_id} is running ({count} so far): {sentence(session.message)} "
                f"Call wait with run_id {session.run_id}.",
                {"status": "running", "run_id": session.run_id, "steps_so_far": steps, "message": session.message})
        if session.state == "preparing":
            return ToolResult(
                f"Run {session.run_id} is preparing: {sentence(session.message)} "
                f"Call wait with run_id {session.run_id}.",
                {"status": "preparing", "run_id": session.run_id, "message": session.message})
        if session.state == "finishing":
            return ToolResult(f"Run {session.run_id} is being judged. Call wait with run_id {session.run_id}.",
                              {"status": "finishing", "run_id": session.run_id})
        return self._ready(session, image, lock_wait)

    def _ready(self, session, image, lock_wait=None):
        try:
            with self._locked(session, lock_wait):
                outline, alert = self._outline(session)
                first, session.ready_reported = not session.ready_reported, True
                picture = (self._first_frame(session) if first else self._screenshot(session)) if image else None
        except Busy:
            return ToolResult(f"Run {session.run_id} is ready, and another call on it is still going. Call screen "
                              "when that call returns.", {"run_id": session.run_id, "status": "ready"})
        then = "" if session.mode == "direct" else ", then call verify_finish"
        text = (f"Run {session.run_id} is ready. Drive the app with the refs below{then}.\n"
                + outline.render(alert))
        return ToolResult(cap_text(text), {"run_id": session.run_id, "status": "ready",
                                           "screen": outline.structured(alert)},
                          [picture] if picture else [])

    def _verdict(self, result, image):
        result = result or {}
        lines = []
        name = ((result.get("check") or {}).get("name")) or ""
        seconds = result.get("seconds")
        head = f"Verdict: {result.get('verdict', 'couldnt_run')}"
        if name:
            head += f"  {name}"
        if isinstance(seconds, (int, float)):
            head += f"  ({seconds:.1f} s)"
        lines.append(head)
        if result.get("summary"):
            lines.append(str(result["summary"]))
        reason = result.get("reason") or {}
        if result.get("verdict") != "passed" and reason.get("message"):
            lines.append(f"Reason ({reason.get('class')}): {reason['message']}")
        if reason.get("fix"):
            lines.append(f"Fix: {reason['fix']}")
        hints = []
        for item in result.get("assertions") or []:
            mark = "✓" if item.get("ok") else "✗"
            lines.append(f"{mark} {item.get('text', '')}: {str(item.get('observed', ''))[:200]}")
            hint = None if item.get("ok") else navbar_hint(item.get("assertion"))
            if hint and hint not in hints:
                hints.append(hint)
        lines += hints
        for key, label in (("report", "Report"), ("repro", "Rerun"), ("run_dir", "Run folder")):
            if result.get(key):
                lines.append(f"{label}: {result[key]}")
        proof = [str(p) for p in result.get("proof") or []]
        if proof:
            lines.append("Proof: " + ", ".join(proof[:3]))
        if result.get("cost_usd"):
            lines.append(f"Model spend: ${float(result['cost_usd']):.3f}")
        picture = None
        if image and proof:
            overlay = next((p for p in proof if "verdict-ax" in p), proof[0])
            picture = self.imager(path=overlay) if os.path.isfile(overlay) else None
        return ToolResult(cap_text("\n".join(lines)), result, [picture] if picture else [])

    def _engine_state(self):
        from .. import engine_health
        if self._engine_source is not None:
            return self._engine_source()
        if self._devices_source is not None:
            return None
        return engine_health.read_from_app(origin="mcp")

    def tool_status(self, args, call):
        info = {}
        try:
            info.update(self._manager().status() or {})
        except Exception as error:
            info["error"] = f"The simulator status couldn't be read ({type(error).__name__}: {str(error)[:200]})."
        open_session = self._open_session()
        open_run = ({"run_id": open_session.run_id, "mode": open_session.mode, "state": open_session.state}
                    if open_session else None)
        info.update(mobster_version=self.version, smart_available=self.smart_available, smart_model=self.smart_model,
                    runs_dir=str(self.runs_dir), open_run=open_run)
        engine = self._engine_state()
        if engine:
            info["engine"] = engine
        info = jsonable(info)
        direct = [{"run_id": s.run_id, "device": s.run.device_id, "name": s.run.device_name}
                  for s in self._direct_sessions()]
        info["devices_open"] = direct
        lines = [f"Mobster {self.version}. Smart: {self.smart_state()}.", f"Runs: {self.runs_dir}",
                 "Open run: " + (f"{open_session.run_id} ({open_session.mode}, {open_session.state})"
                                 if open_session else "none")]
        if engine:
            lines.append(f"Mobster app: {engine['message']}" + (f" {engine['next_step']}" if engine.get("next_step")
                                                                  else ""))
        if direct:
            lines.append("Devices driven directly: " + ", ".join(f"{item['name']} (run {item['run_id']})"
                                                                for item in direct))
        xcode = info.get("xcode")
        if isinstance(xcode, dict):
            lines.append("Xcode: " + (xcode.get("error") or f"{xcode.get('version', '?')} ({xcode.get('build', '?')})"))
        runtime = info.get("runtime")
        if isinstance(runtime, dict):
            lines.append(f"iOS runtime: {runtime.get('error')}")
        elif runtime or info.get("device_type"):
            lines.append(f"Default simulator: {info.get('device_type') or '?'}, {runtime or '?'}")
        wda = info.get("wda_build")
        if isinstance(wda, dict):
            lines.append("WebDriverAgent build: " + ("cached" if wda.get("cached") else "not built yet (the first "
                                                     "run builds it, about a minute)"))
        for sim in info.get("simulators") or []:
            if isinstance(sim, dict):
                lines.append(f"Simulator: {sim.get('name')} ({sim.get('state')}, WebDriverAgent {sim.get('wda')}"
                             + (", in use" if sim.get("in_use") else "") + ")")
        if info.get("error"):
            lines.append(info["error"])
        return ToolResult(cap_text("\n".join(lines)), info)

    def tool_verify_start(self, args, call):
        smart = bool(args.get("smart"))
        if smart and not args["steps"]:
            raise ToolError("smart runs the steps itself, so pass at least one step.")
        if not smart and "expect" not in args:
            raise ToolError("Without smart, pass expect: the assertions the run is judged by.")
        self._yield_session_simulator(args)
        check, record = self._check(args, smart=smart)
        session = self._new_session(check, "smart" if smart else "keyless", args.get("image", True), call, record)
        self.log(f"Run {session.run_id} started ({'Smart' if smart else 'key-less'}).")
        self._start_worker(session, self._run_smart if smart else self._prepare_keyless)
        self._wait(session, ("preparing", "running") if smart else ("preparing",),
                   self._left(call, self.prepare_wait), call)
        return self._describe(session, call, lock_wait=self.describe_lock_wait)

    def tool_wait(self, args, call):
        session = self._session(args["run_id"])
        timeout = min(float(args.get("timeout_s", 40)), self._left(call, self.wait_max - self.wait_headroom))
        self._wait(session, ("preparing", "running", "finishing"), timeout, call)
        return self._describe(session, call, lock_wait=self.describe_lock_wait)

    def tool_verify_finish(self, args, call):
        session = self._session(args["run_id"], check=True)
        image = args.get("image", True)
        if session.state == "finished":
            return self._verdict(session.result, image)
        if session.mode == "smart":
            raise ToolError(f"Run {session.run_id} is a Smart run: it finishes on its own. Call wait for its verdict.")
        if session.state == "finishing":
            raise ToolError(f"Run {session.run_id} is being judged. Call wait for its verdict.")
        if session.state == "preparing":
            raise ToolError(f"Run {session.run_id} is still preparing. Call wait with run_id {session.run_id} first.")
        with self._locked(session):
            if session.state != "ready":
                return self._describe(session, call, image)
            session.set_state("finishing")
            self._finish(session)
        self.log(f"Run {session.run_id}: {session.result.get('verdict')}.")
        return self._verdict(session.result, image)

    def tool_stop(self, args, call):
        if not args.get("run_id"):
            if not args.get("device"):
                raise ToolError("Pass run_id, or device to release a device you drive directly.")
            record = self._named_device(args["device"], required=True)
            session = self.device_sessions.get(record["id"])
            if session is None or not session.open:
                return ToolResult(f"{record['name']} isn't open here.", {"device": record["id"], "status": "closed"})
            return self._stop_direct(session)
        session = self._session(args["run_id"])
        if session.mode == "direct":
            return self._stop_direct(session)
        if session.state == "finished":
            return self._verdict(session.result, False)
        reason = "Stopped by the agent."
        session.stop_reason = session.stop_reason or reason
        session.cancelled.set()
        if session.state == "preparing":
            self._abort(session, "stopped", session.stop_reason)
            return self._verdict(session.result, False)
        if session.state in ("running", "finishing"):
            if not self._wait(session, ("running", "finishing"), self._left(call, self.prepare_wait), call):
                return ToolResult(f"Run {session.run_id} is stopping; it closes when its current step ends. "
                                  f"Call wait with run_id {session.run_id}.",
                                  {"status": "stopping", "run_id": session.run_id})
            if session.open:
                with self._locked(session):
                    if session.open:
                        self._abort(session, "stopped", session.stop_reason)
            return self._verdict(session.result, False)
        with self._locked(session):
            if session.open:
                self._abort(session, "stopped", session.stop_reason)
        return self._verdict(session.result, False)

    def _stop_direct(self, session):
        if not self._close_direct(session, "Stopped by the agent."):
            raise Busy(f"Another call on run {session.run_id} is still going. Try again when it returns.")
        result = session.result or {}
        steps = len(result.get("steps") or [])
        return ToolResult(f"Released {session.run.device_name} after {steps} step{'' if steps == 1 else 's'}. "
                          f"Frames: {result.get('run_dir')}", result)

    def tool_save_check(self, args, call):
        run_id, name = args["run_id"], args["name"]
        if TASK_ID.fullmatch(run_id):  # a task in the Mobster app: its steps and what it proved
            from ..testkit.record import RecordError, check_yaml, find_run
            try:
                text = check_yaml(find_run(run_id), path=f".mobster/checks/{name}.yaml")
            except RecordError as error:
                raise ToolError(error.message) from None
        else:  # a finished check run
            session = self._session(run_id, check=True)
            if session.open:
                raise ToolError(f"Run {session.run_id} isn't finished. Call verify_finish first.")
            source = Path((session.result or {}).get("draft_check") or Path(session.run.run_dir) / "check.yaml")
            if not source.is_file():
                raise ToolError(f"Run {session.run_id} left no check.yaml to save.")
            text = source.read_text()
        folder = self.runs_dir.parent / "checks"
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{name}.yaml"
        replaced = destination.exists()
        destination.write_text(text)
        return ToolResult(f"Saved {destination}" + (", replacing the earlier file" if replaced else "")
                          + f". Run it again with: mobster test {destination}",
                          {"path": str(destination), "replaced": replaced})

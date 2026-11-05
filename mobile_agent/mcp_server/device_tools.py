"""Finding the devices the tools may drive, who holds them, and the direct session on one."""

import time

from ..android.records import is_phone
from .protocol import ToolResult
from .session import Session
from .support import SHUTTING_DOWN, ToolError, cap_text, jsonable, sentence

DEVICE_LIST_TTL = 30.0


class DeviceTools:
    def _device_records(self, fresh=False, probe=None):
        at, records = self._devices_cache
        if records is None or fresh or time.monotonic() - at > DEVICE_LIST_TTL:
            if self._devices_source is not None:
                records = self._devices_source()
            else:
                from ..android.records import records as android_records
                from ..devices import discover
                records = discover(probe=fresh if probe is None else probe) + android_records()
            self._devices_cache = (time.monotonic(), records)
        return records

    def _app_ownership(self):
        if self._ownership_source is not None:
            return self._ownership_source()
        if self._devices_source is not None:
            return {}
        at, found = self._ownership_cache
        if time.monotonic() - at < 3.0:
            return found
        found = {}
        try:
            from ..threads.client import Client
            listing = Client.connect(origin="mcp").call("GET", "/api/devices", timeout=2.0)
            for item in listing.get("devices") or ():
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    found[item["id"]] = {"holder": item.get("holder"), "queue": item.get("queue") or []}
        except Exception:  # noqa: BLE001 -- no app, an older app, a refused token
            found = {}
        self._ownership_cache = (time.monotonic(), found)
        return found

    def _holder_of(self, record):
        from .. import ownership
        for session in self._all_sessions() + self._direct_sessions():
            run = session.run
            if session.open and getattr(run, "device_id", None) in (record.get("id"), record.get("udid")):
                since = time.time() - max(0.0, self.clock() - session.started)
                label = ownership.clean_client(self.client_name()) or "this MCP client"
                return {"kind": "mcp", "label": label, "runId": session.run_id, "since": int(since * 1000),
                        "stopping": False}, []
        if record.get("kind") == "simulator":
            other = self._simulator_holder(record)
            if other is not None:
                return other, []
        known = self._app_ownership().get(record.get("id")) or {}
        return known.get("holder"), known.get("queue") or []

    def _simulator_holder(self, record):
        """The holder of a Mobster simulator in another process (an agent's session, a test run), from its record."""
        from ..sim import holders
        data_dir = getattr(self._manager_instance, "data_dir", None)
        if data_dir is None:
            from ..devices import simulator_folder
            data_dir = simulator_folder()
        found = holders.read(data_dir, record.get("udid") or record.get("id"))
        if found is None:
            return None
        return {"kind": "mcp", "label": f"{found['client']} ({found['session']})", "runId": None,
                "since": int(found["since"] * 1000), "stopping": False}

    @staticmethod
    def _holder_words(holder, queue):
        from .. import ownership
        if not holder:
            return ""
        minutes = max(0, int((time.time() * 1000 - holder["since"]) // 60000))
        words = f"{ownership.holding_sentence(holder)}, {minutes} min" + (", stopping after this step"
                                                                           if holder.get("stopping") else "")
        return words + (f"; {len(queue)} waiting" if queue else "")

    def _named_device(self, name, required=False):
        from ..devices import AmbiguousDevice, DeviceNotFound, resolve
        if not name:
            return None
        for attempt in range(2):
            try:
                return resolve(self._device_records(fresh=attempt > 0, probe=False), name)
            except AmbiguousDevice as error:
                raise ToolError(str(error)) from None
            except DeviceNotFound as error:
                if attempt == 0 and time.monotonic() - self._devices_cache[0] > 1.0:
                    continue
                if required:
                    raise ToolError(f"{error} Call list_devices to see them.") from None
                return None
        return None

    def _open_direct(self, name):
        folded = str(name or "").strip().casefold()
        if not folded:
            return None
        sessions = self._direct_sessions()
        for session in sessions:
            record = getattr(session.run, "record", None) or {}
            if folded in {str(record.get("id") or "").casefold(), str(record.get("udid") or "").casefold()}:
                return session
        named = [session for session in sessions
                 if str((getattr(session.run, "record", None) or {}).get("name") or "").casefold() == folded]
        return named[0] if len(named) == 1 else None

    def _direct_sessions(self, open_only=True):
        with self._lock:
            return [s for s in self.device_sessions.values() if s.open or not open_only]

    def _allowed(self, record):
        if not is_phone(record) or self.allow_devices is None:
            return True
        names = {str(record.get(key) or "").strip().casefold() for key in ("id", "udid", "name")}
        return bool(names & self.allow_devices)

    def _refuse_unless_allowed(self, record):
        if not self._allowed(record):
            raise ToolError(f"“{record['name']}” is a real device, and this server may drive only the ones "
                            "`mobster mcp --allow-device` names. Ask the user to add it to the server's command.")

    def _device_busy_with_check(self, record):
        for session in self._all_sessions():
            run = session.run
            if (session.open
                    and getattr(getattr(run, "target", None), "udid", None) in (record.get("udid"), record["id"])):
                return session
            if session.open and getattr(run, "device_id", None) == record["id"]:
                return session
        return None

    def _device_session(self, name):
        session = self._open_direct(name)
        if session is not None and session.open:
            session.touch()
            return session
        record = self._named_device(name, required=True)
        with self._lock:
            session = self.device_sessions.get(record["id"])
            if session is not None and session.open:
                session.touch()
                return session
            if self._closing.is_set():
                raise ToolError(SHUTTING_DOWN)
            check = self._device_busy_with_check(record)
            if check is not None:
                raise ToolError(f"Run {check.run_id} is checking an app on {record['name']}. Pass run_id "
                                f"{check.run_id}, or stop that run first.")
            self._refuse_unless_allowed(record)
            if record.get("platform") == "android":
                raise ToolError(f"“{record['name']}” is an Android device: use verify_start with device, or run_tests. "
                                "Driving it directly with tap and screen isn't available yet.")
            if record.get("state") in ("needs_setup", "disconnected") or not record.get("wdaUrl"):
                raise ToolError(f"“{record['name']}” isn't ready: {record.get('reason') or record['state']}")
            from .direct import DeviceRun
            factory = self._device_run or DeviceRun
            folder = self.runs_dir if record.get("kind") == "simulator" else self.device_runs_dir
            run = factory(record, runs_dir=folder)
            session = self._open_direct_run(run, record)
        self.log(f"Run {session.run_id}: driving {record['name']} ({record['kind']}) directly.")
        return session

    def _open_direct_run(self, run, record, *, own=False):
        """Prepare ``run`` and file it as the direct session on ``record``'s device. Call it under self._lock."""
        try:
            run.prepare()
        except Exception as error:
            run.close("It couldn't be opened.")
            fix = getattr(error, "fix", "")
            raise ToolError(sentence(error) + (" " + sentence(fix) if fix else "")) from None
        if own:
            # the simulator is known only once its lease is taken
            udid, name = run.target.udid, run.target.name
            record.update(id=udid, udid=udid, name=name)
            run.device_id, run.device_name = udid, name
            self._own_sim = udid
        session = Session(run, "direct", clock=self.clock, image=True)
        self.codes.seed(session, record["id"])
        session.message = f"Driving {record['name']} directly."
        session.set_state("ready")
        self.device_sessions[record["id"]] = session
        return session

    def _yield_session_simulator(self, args):
        """A check that names no device takes a free simulator itself: hand back the one this session leased, so
        one session never holds two."""
        session = self.device_sessions.get(self._own_sim) if self._own_sim else None
        if args.get("device") or self.device or session is None or not session.open or session.busy:
            return
        self._close_direct(session, "A check is starting, so Mobster handed this session's simulator to it.",
                           timeout=0)

    def _session_simulator(self):
        """This session's own simulator: leased on the first call that names no device, kept until the session
        ends or sits idle for 30 minutes, and never given to another session while held."""
        with self._lock:
            session = self.device_sessions.get(self._own_sim) if self._own_sim else None
            if session is not None and session.open:
                session.touch()
                return session
            self._own_sim = None
            if self._closing.is_set():
                raise ToolError(SHUTTING_DOWN)
            from .direct import DeviceRun
            record = {"id": "", "udid": "", "kind": "simulator", "name": "a Mobster simulator"}
            # no queueing: when every simulator is held, the answer names the holders at once
            run = (self._device_run or DeviceRun)(record, runs_dir=self.runs_dir, manager=self._manager(),
                                                  acquire_timeout=0)
            session = self._open_direct_run(run, record, own=True)
        self.log(f"Run {session.run_id}: leased {record['name']} for this session.")
        return session

    def _close_direct(self, session, reason, timeout=None):
        if not session.open:
            return True
        locked = session.run.lock.acquire(timeout=self.lock_wait if timeout is None else max(0.0, timeout))
        if not locked:
            return False
        try:
            if session.open:
                if self._own_sim and session.run.device_id == self._own_sim:
                    self._own_sim = None
                session.set_state("finished", jsonable(session.run.close(reason)))
                self.log(f"Run {session.run_id}: {reason}")
        finally:
            session.run.lock.release()
        return True

    def _target_session(self, args):
        run_id, device = args.get("run_id"), args.get("device")
        if run_id:
            session = self._ready_session(run_id)
            if device:
                record = self._named_device(device, required=True)
                run = session.run
                on = getattr(run, "device_id", None) or getattr(getattr(run, "target", None), "udid", None)
                if on not in (record["id"], record.get("udid")):
                    raise ToolError(f"Run {run_id} isn't on {record['name']}. Leave out device, or pass the run_id "
                                    "of a run on it.")
            return session
        if device:
            name = device
        elif self.device and self._open_direct(self.device) is not None:
            name = self.device
        else:
            name = self.device if self._named_device(self.device) is not None else None
        if not name:
            return self._session_simulator()
        return self._device_session(name)

    def tool_list_devices(self, args, call):
        from ..devices import describe
        records = self._device_records(fresh=True)
        open_runs = {s.run.device_id: s.run_id for s in self._all_sessions() + self._direct_sessions()
                     if s.open and getattr(s.run, "device_id", None)}
        rows = []
        for item in records:
            row = {key: item.get(key) for key in ("id", "kind", "name", "model", "modelName", "ios", "state", "reason")}
            row["platform"] = item.get("platform") or "ios"
            row["run_id"] = open_runs.get(item["id"])
            row["holder"], row["queue"] = self._holder_of(item)
            row["yours"] = bool(self._own_sim) and item["id"] == self._own_sim
            if not self._allowed(item):
                row["state"] = "not_allowed"
                row["reason"] = "this server may drive only the devices `mobster mcp --allow-device` names"
            rows.append(row)
        if not rows:
            return ToolResult("No devices. Plug in an iPhone set up in the Mobster app, or prepare a simulator "
                              "(`mobster sim prepare`).", {"devices": []})
        lines = [describe(item) + f"  (id {item['id']})" + (f", open in run {row['run_id']}" if row["run_id"] else "")
                 + (", not allowed: not named by --allow-device" if row["state"] == "not_allowed" else "")
                 + (", yours" if row["yours"] else "")
                 + (f". {self._holder_words(row['holder'], row['queue'])}" if row["holder"] and not row["yours"]
                    else "")
                 for item, row in zip(records, rows)]
        return ToolResult(cap_text("\n".join(lines)), {"devices": rows})

    def _phone_record(self, session):
        record = getattr(session.run, "record", None)
        if isinstance(record, dict):
            return record
        device_id = getattr(session.run, "device_id", None)
        try:
            return self._named_device(device_id) if device_id else None
        except ToolError:
            return None

    def _on_phone(self, session):
        record = self._phone_record(session)
        return bool(record) and is_phone(record)

    def _record_for(self, args):
        name = args.get("device") or self.device
        if not name:
            raise ToolError("Pass device (from list_devices).")
        record = self._named_device(name, required=True)
        self._refuse_unless_allowed(record)
        return record

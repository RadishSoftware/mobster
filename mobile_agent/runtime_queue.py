"""Runtime's phone queue: who holds a phone, the requests that wait for it, and taking it back."""

import threading
import time

from . import engines, harness_api, ownership, permissions
from .api_errors import APIError
from .journal import JournalError, Lease
from .pool import NoDeviceAvailable
from .runtime_create import NO_MODEL
from .runtime_driver import PooledLease, person_started
from .runtime_teach import CONTROL_STREAM_SECONDS
from .wda import watchdog as wda_watchdog


class PhoneQueueMixin:
    def run_on(self, phone):
        for run_id, key in list(self.active_runs.items()):
            if key == phone.key or (key is None and phone.primary):
                return run_id
        return None

    def queue_key(self, phone):
        return phone.key if phone is not None else "@any"

    def phone_name(self, phone):
        fleet = getattr(self, "fleet", None)
        nickname = fleet.prefs(phone).get("nickname") if fleet is not None and phone is not None else None
        return nickname or (phone.name if phone is not None and phone.name else None) or "your iPhone"

    def busy_error(self, phone, busy, primary):
        if busy is None and phone is not None:
            return APIError(f"{self.phone_name(phone)} is held for its owner after Take it back; try again in a "
                            "few minutes, or choose another device", code="run_active", activeRunId=None)
        if phone is not None:
            return APIError("A task is running on this device; stop it or wait for it to finish, or "
                            "choose another device" if not primary or len(self.active_runs) > 1 else
                            "Every assigned phone is busy; stop a run or wait for one to finish",
                            code="run_active", activeRunId=busy)
        return APIError("Every assigned phone is busy; stop a run or wait for one to finish",
                        code="run_active", activeRunId=busy)

    def holder_of(self, phone):
        run_id = self.run_on(phone) if phone is not None else self.active
        run = self.runs.get(run_id) if run_id else None
        if run is not None and run.finished_at is None:
            return ownership.holder_for(run, client=run.client)
        if phone is None:
            return None
        lesson = self.lesson_for(phone)
        if lesson is not None:
            return ownership.manual_holder("you", lesson.started_at)
        seen = getattr(self, "controlled_at", {}).get(None if phone.primary else phone.key)
        if seen is not None and time.monotonic() - seen < CONTROL_STREAM_SECONDS:
            return ownership.manual_holder("you", time.time() - (time.monotonic() - seen))
        return None

    def phone_ownership(self, phone):
        with self.lock:
            line = self.queues.find(self.queue_key(phone))
            paused = line.held_until * 1000 if line is not None and line.held() else None
            return {"holder": self.holder_of(phone), "queue": line.view() if line is not None else [],
                    "queuePausedUntil": int(paused) if paused else None}

    def enqueue(self, run, phone, key, *, queue_cap=None, chosen=None, primary=True):
        # Call with self.lock held. The run does nothing until dispatch_queue gives it the phone.
        line = self.queues.of(key)
        who = ownership.holder_for(run, client=run.client)
        line.add(run.id, who["kind"], who["label"])
        self.queued[run.id] = {"run": run, "phone": phone, "key": key, "cap_usd": queue_cap, "chosen": chosen,
                               "primary": primary}
        self.queue_timer(run.id, line.ttl)
        self.refresh_queue(key)
        run.emit({"event": "queued", "position": run.queue_info["position"] if run.queue_info else None,
                  "behind": (run.queue_info or {}).get("behind")})

    def queue_timer(self, run_id, seconds):
        timer = threading.Timer(seconds + 0.05, self.sweep_queues)
        timer.daemon = True
        self.queue_timers[run_id] = timer
        timer.start()

    def drop_timer(self, run_id):
        timer = self.queue_timers.pop(run_id, None)
        if timer is not None:
            timer.cancel()

    def refresh_queue(self, key):
        # Call with self.lock held.
        line = self.queues.find(key)
        if line is None:
            return
        for index, entry in enumerate(line.entries, 1):
            item = self.queued.get(entry.id)
            if item is None:
                continue
            phone = item["phone"]
            holder = self.holder_of(phone)
            run = item["run"]
            name = self.phone_name(phone)
            if line.held():
                message = (f"{name} is held for its owner for a moment. You are number {index} and start when it "
                           "frees.")
            else:
                message = ownership.waiting_message(holder, name, index)
            run.queue_info = {"position": index, "behind": holder["label"] if holder else None,
                              "phone": name, "message": message, "expiresAt": int(entry.expires_at * 1000),
                              "paused": line.held()}

    def unqueue(self, run_id):
        # Call with self.lock held.
        item = self.queued.pop(run_id, None)
        self.drop_timer(run_id)
        if item is None:
            return None
        line = self.queues.find(item["key"])
        if line is not None:
            line.remove(run_id)
        item["run"].queue_info = None
        self.refresh_queue(item["key"])
        return item

    def end_queued(self, run, status, reason, code=None):
        summary = {"event": "result", "status": status, "independently_verified": False, "reason": reason,
                   "actions": 0, "actions_may_have_run": False, "outcome": "couldnt_finish" if status != "stopped"
                   else "stopped", "answer": None, "proof": [], "engine": run.engine, "costUsd": run.cost_usd,
                   **({"stop_code": code} if code else {})}
        run.queue_info = None
        run.finish(summary)

    def run_ended(self, run):
        phone = self.phone_for(run) if getattr(self, "fleet", None) is not None else None
        if self.pool is not None or getattr(self, "fleet", None) is None:
            phone = None
        self.dispatch_queue(self.queue_key(phone))
        self.publish("devices", {"event": "holder_changed", "device": run.device_id})

    def dispatch_queue(self, key):
        # A request that cannot start ends with the reason and does nothing; the next in line is tried.
        ended = []
        with self.lock:
            line = self.queues.find(key)
            while line is not None and len(line) and not self.closing:
                if line.held():
                    self.hold_timer(key, line)
                    break
                entry = line.head()
                item = self.queued.get(entry.id)
                if item is None:
                    line.remove(entry.id)
                    continue
                phone = item["phone"]
                if entry.expires_at <= line.clock():
                    self.unqueue(entry.id)
                    minutes = max(1, round(line.ttl / 60))
                    self.end_queued(item["run"], "error", ownership.QUEUE_EXPIRED.format(
                        minutes=minutes, phone=self.phone_name(phone)), "queue_expired")
                    ended.append(item["run"])
                    continue
                if (self.run_on(phone) is not None) if phone is not None else not self.capacity():
                    break
                self.unqueue(entry.id)
                try:
                    self.start_queued(item)
                except APIError as error:
                    self.end_queued(item["run"], "error", str(error), error.details.get("stop") or error.code)
                    ended.append(item["run"])
                except JournalError:
                    self.end_queued(item["run"], "error", "Mobster could not save this task, so it did not start. "
                                    "Nothing was done on the phone.")
                    ended.append(item["run"])
                except Exception:  # noqa: BLE001 -- a request out of the line must end, not stay "queued" for good
                    harness_api.log.exception("a queued task could not start")
                    run = item["run"]
                    if run.id not in self.active_runs:  # no worker took it: it did nothing
                        if run.lease:
                            run.lease.close()
                            run.lease = None
                        self.end_queued(run, "error", "The task could not start. Nothing was done on the phone. "
                                        "Send it again.")
                        ended.append(run)
            self.refresh_queue(key)
        for run in ended:
            self.finished(run)
        self.publish("devices", {"event": "queue_changed"})

    def hold_timer(self, key, line):
        if line.held_until is None:
            return
        timer = self.queue_timers.pop(("hold", key), None)
        if timer is not None:
            timer.cancel()
        timer = threading.Timer(max(0.0, line.held_until - line.clock()) + 0.05, self.dispatch_queue, args=(key,))
        timer.daemon = True
        self.queue_timers[("hold", key)] = timer
        timer.start()

    def recheck_rules(self, run, phone, fleet):
        # A rule tightened while the request waited applies: read them again for every app it uses.
        prefs = fleet.prefs(phone) if fleet is not None and phone is not None else {}
        apps = frozenset(prefs["apps"]) if prefs.get("apps") else None
        policy = permissions.from_env().apps
        outside = run.origin in permissions.policy.OUTSIDE_ORIGINS
        used = [run.app, *(run.pinned_apps or ())]
        used += [{"bundleId": bundle, "name": None} for bundle in sorted(run.allowed_bundles or ())]
        for app in used:
            bundle = app.get("bundleId")
            if not bundle:
                continue
            name = app.get("name") or "That app"
            if not policy.allows(bundle, outside=outside):
                raise APIError(f"{policy.blocked_message(app.get('name'), outside=outside and policy.allows(bundle))} "
                               "This task waited for the phone and did nothing on it.", 403, "app_not_allowed")
            if apps is not None and bundle not in apps:
                raise APIError(f"{name} isn't one of the apps Mobster may use on {self.phone_name(phone)} any more. "
                               "This task waited for the phone and did nothing on it. Change it in Settings › "
                               "Phones, then send the task again.", 409, "app_not_allowed", device=phone.id)
        if run.mode == "live":
            run.ask_override = prefs.get("askBeforeActing")

    def start_queued(self, item):
        # Call with self.lock held: every admission check again, then the lease, then the worker.
        run, phone, primary = item["run"], item["phone"], item["primary"]
        fleet = getattr(self, "fleet", None)
        if run.stop.is_set():
            raise APIError(ownership.QUEUE_CANCELLED.format(phone=self.phone_name(phone)), 409, "stopped")
        if fleet is not None and phone is not None and fleet.is_disconnected(phone):
            raise APIError(f"{phone.name or 'This iPhone'} is disconnected in Mobster. Connect it in Settings › "
                           "Phones to run tasks on it. Nothing was done on the phone.", 409, "device_disconnected",
                           device=phone.id)
        self.recheck_rules(run, phone, fleet)
        chosen = NO_MODEL if harness_api.calls_no_model(run.extras) else item["chosen"]
        self.check_ready(phone, primary, chosen, run.origin, run.idempotency_key)
        self.teaching(None if primary else phone)
        if run.mode == "live":
            limit = self.monthly_limit()
            self.apply_caps(run, self.monthly_cap(limit, raised=(run.longform or {}).get("capUsd"))
                            if limit is not None else None, item["cap_usd"])
        run.queue_info = None
        run.emit({"event": "dequeued"})  # the one step that can raise JournalError: nothing is claimed yet
        lease, device = None, None
        if self.pool is None:
            try:
                lease = Lease.device((self.config.wda_url if primary else phone.wda_url).rstrip("/"))
            except JournalError:
                raise APIError(f"{self.phone_name(phone)} is busy in another Mobster process. Nothing was done on "
                               "the phone.", code="device_busy") from None
            run.lease = lease
        else:
            try:
                device = self.pool.acquire(run.id, timeout=0)
            except NoDeviceAvailable:
                raise APIError("Every assigned phone is busy. Nothing was done on the phone.",
                               code="run_active") from None
            run.device, run.lease = device, PooledLease(self.pool, run.id)
        held_url = self.run_phone_url(run)
        try:
            wda_watchdog.tracker.run_started(held_url, run.id)
        except wda_watchdog.ActionBlocked:
            if run.lease:
                run.lease.close()
                run.lease = None
            raise APIError("Mobster's helper on your iPhone is restarting. Nothing was done on the phone. "
                           "Try again in a moment.", 503, "device_unavailable") from None
        self.active_runs[run.id] = device.id if device else phone.key if phone is not None else None
        thread = None
        try:
            thread = threading.Thread(target=self.work, args=(run,), daemon=True)
            self.threads.add(thread)
            thread.start()
        except Exception:
            self.threads.discard(thread)
            self.active_runs.pop(run.id, None)
            wda_watchdog.tracker.run_ended(held_url, run.id)
            if run.lease:
                run.lease.close()
                run.lease = None
            raise APIError("The task could not start. No agent action was dispatched.", 503, "start_failed") from None
        if fleet is not None and phone is not None:
            fleet.mark_used(phone)

    def sweep_queues(self):
        ended = []
        with self.lock:
            for key, line in list(self.queues.by_key.items()):
                for entry in line.expired():
                    item = self.queued.pop(entry.id, None)
                    self.drop_timer(entry.id)
                    if item is None:
                        continue
                    minutes = max(1, round(line.ttl / 60))
                    self.end_queued(item["run"], "error", ownership.QUEUE_EXPIRED.format(
                        minutes=minutes, phone=self.phone_name(item["phone"])), "queue_expired")
                    ended.append(item["run"])
                self.refresh_queue(key)
        for run in ended:
            self.finished(run)
        if ended:
            self.publish("devices", {"event": "queue_changed"})
        return len(ended)

    def drop_queues(self, status="interrupted", reason=ownership.QUEUE_RESTART):
        # Call with self.lock held; returns the runs to announce.
        ended = []
        for run_id in list(self.queued):
            item = self.unqueue(run_id)
            if item is not None:
                self.end_queued(item["run"], status, reason)
                ended.append(item["run"])
        return ended

    def take_back(self, phone):
        # Never stops an action in flight: the run ends at its next step boundary and nothing is re-sent.
        with self.lock:
            run_id = self.run_on(phone) if phone is not None else self.active
            run = self.runs.get(run_id) if run_id else None
            if run is None or run.finished_at is not None:
                raise APIError("Nothing is using this phone.", 409, "not_held")
            line = self.queues.of(self.queue_key(phone))
            line.hold(ownership.TAKE_BACK_GRACE_SECONDS)
            self.hold_timer(self.queue_key(phone), line)
            self.refresh_queue(self.queue_key(phone))
        with run.condition:
            run.taking_back = True
            first = not run.stop.is_set()
        if first:
            smart = run.engine == engines.SMART and run.mode == "live" and getattr(run, "steering", None) is not None
            if smart and run.approval is None:
                from .harness import control as harness_control
                harness_control.request_stop_after(run.steering)
                run.emit({"event": "take_back_requested", "how": "stop_after_step"})
            else:
                # A coding agent's run has no step control, and a run waiting for the person has nothing in flight.
                run.emit({"event": "take_back_requested", "how": "stop"})
                with run.condition:
                    run.stop.set()
                    run.condition.notify_all()
        self.publish("devices", {"event": "holder_changed", "device": run.device_id})
        return self.phone_ownership(phone)

    def resume_queue(self, phone):
        key = self.queue_key(phone)
        with self.lock:
            line = self.queues.find(key)
            if line is not None:
                line.release()
                timer = self.queue_timers.pop(("hold", key), None)
                if timer is not None:
                    timer.cancel()
        self.dispatch_queue(key)
        return self.phone_ownership(phone)

    def run_phone_url(self, run):
        if getattr(run, "mode", None) != "live":
            return None
        device = getattr(run, "device", None)
        if device is not None:
            return getattr(device, "wda_url", None)
        try:
            phone = self.phone_for(run)
            return getattr(phone, "wda_url", None)
        except Exception:
            return None

    def stop_phone_run(self, phone, message):
        # The watchdog's call: nothing restarts under a running task, and an action already sent is never sent again.
        run_id = self.run_on(phone)
        run = self.runs.get(run_id) if run_id else None
        if run is None:
            return
        run.emit({"event": "phone_helper_stopped", "message": message})
        try:
            self.stop_run(run_id)
        except APIError:
            pass

    def release_idle(self, phone):
        if self.run_on(phone) is not None:
            return
        video = getattr(phone, "video", None)
        if video is not None and callable(getattr(video, "release_idle", None)):
            video.release_idle()
        url = getattr(phone, "wda_url", None)
        if url:
            from .harness import prewarm
            prewarm.release(url)
        self.awake.release("video")

    def owner_blocks_start(self, phone, origin, idempotency_key=None):
        # A task a person just started is never held (they asked); a schedule, MCP, the API and `mobster run` are.
        if person_started(origin, idempotency_key):
            return False
        from . import owner_presence
        return owner_presence.is_owner_active(getattr(phone, "wda_url", None))

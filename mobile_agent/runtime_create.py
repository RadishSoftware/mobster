"""Runtime's task start: the readiness checks, Runtime.create and the results that need no phone."""

import hashlib
import json
import re
import threading

from . import ai_plans, engine_health, engines, harness_api, orchestrate, ownership, permissions, project_env
from .api_errors import APIError
from .journal import JournalError, Lease
from .lockscreen import PHONE_LOCKED
from .output_contract import validate_output
from .owner_presence import OWNER_ACTIVE_REFUSAL
from .pool import NoDeviceAvailable
from .run import Run
from .runtime_driver import PooledLease, waits_for_unlock
from .runtime_limits import TASK_LIMIT_RANGE
from .state import validate_bundle_id
from .wda import watchdog as wda_watchdog

# A run that calls no model needs a ready phone and live runs on, but no model key.
NO_MODEL = "no_model"

ANY_APP = {"id": "any", "name": "Any app", "bundleId": None, "available": True}


class TaskStartMixin:
    def _replay(self, idempotency_key, fingerprint):
        # Call with the lock held. A key reused for another task, or whose task left history, is refused.
        prior = self.journal.lookup(idempotency_key) if idempotency_key else None
        if not prior:
            return None
        if prior[1] != fingerprint:
            raise APIError("This request key belongs to a different task", code="idempotency_conflict")
        if prior[0] not in self.runs:
            raise APIError("This previous task has expired from history; it was not restarted", 410, "run_expired")
        return self.runs[prior[0]], True

    def check_ready(self, phone, primary, chosen, origin, idempotency_key):
        # Admission asks it, and a queued request asks it again when its turn comes.
        if self.owner_blocks_start(phone, origin, idempotency_key):
            raise APIError(OWNER_ACTIVE_REFUSAL, 409, "owner_active", device=getattr(phone, "id", None))
        if primary:
            status = self.status()
            state = status.get("engines", {}).get(chosen) or {"available": True, "reason": None}
            if not state["available"]:
                # Never another engine instead: the user chose this one (or it is their default).
                raise APIError(state["reason"] or "This engine can't run right now", 409, "engine_unavailable",
                               engine=chosen)
            usable = status["live_enabled"] if chosen == engines.SMART else (
                status.get("device_ready") and self.config.enable_live and not self.closing)
            if not usable:
                locked = (self.config.enable_live and not self.closing
                          and engine_health.cause_of(status["engine"], "phone:primary") == "locked")
                if locked and not waits_for_unlock(origin, idempotency_key):
                    # The device manager's probe saw the lock: say so, not "not ready" (PLAN §3.9).
                    raise APIError(PHONE_LOCKED, 503, "device_unavailable", stop="phone_locked")
                if not locked:
                    raise APIError(self.not_ready_message(status), 503, "device_unavailable")
                # Locked and nothing else: a person started this task, so it waits for the unlock at its preflight.
        else:
            state = self.engines_status().get(chosen) or {"available": True, "reason": None}
            if not state["available"]:
                raise APIError(state["reason"] or "This engine can't run right now", 409, "engine_unavailable",
                               engine=chosen)
            if not self.config.enable_live:
                raise APIError("Live runs are off; enable them to act on the phone", 503, "device_unavailable")
            target = phone.target_status()
            if not (target["ready"] and target["can_act"]):
                locked = engine_health.cause_of(self.engine(), f"phone:{phone.id}") == "locked"
                if locked and not waits_for_unlock(origin, idempotency_key):
                    raise APIError(f"{phone.name or 'This iPhone'} is locked. Unlock it and try again. "
                                   "No action was taken.", 503, "device_unavailable", device=phone.id,
                                   stop="phone_locked")
                if not (locked and target["can_act"]):
                    raise APIError(f"{phone.name or 'This device'} is not ready for live tasks. "
                                   + (target.get("health") or "Try again when it is available."), 503,
                                   "device_unavailable", device=phone.id)
                # Locked only: waits for the unlock at its preflight, like the first iPhone's.

    def create(self, app_id, goal, mode, idempotency_key=None, output_schema=None, output_format="auto",
                 allowed_bundles=None, engine=None, device=None, default_device="last_used",
                 extras=None, origin="api", group_id=None, pinned_apps=None, cap_usd=None, queue=False,
                 _resume_run=None, _workflow_id=None):
        if self.plan_gate is not None:
            self.plan_gate.require()
        engine = engines.read_engine(engine)
        if cap_usd is not None and (isinstance(cap_usd, bool) or not isinstance(cap_usd, (int, float))
                                    or not 0 < cap_usd <= TASK_LIMIT_RANGE[1]):
            raise ValueError("cap_usd is a positive amount in USD")
        if engine is not None and engine not in engines.ENGINES and not ai_plans.is_agent(engine):
            raise ValueError("engine must be smart, or codex, claude_code or gemini for your own coding agent")
        if origin not in harness_api.ORIGINS:
            raise ValueError("Unknown task origin")
        if origin in harness_api.CLAIMED_ORIGINS:
            queue = False  # the claim admits it only as it starts now
        source = None
        if _resume_run is not None:
            source = self.runs.get(getattr(_resume_run, "id", None))
            if (source is not _resume_run or not isinstance(extras, dict)
                    or extras.get("resumeFrom") != source.id or source.finished_at is None):
                raise ValueError("This task cannot reuse a saved run context")
            if "account" in extras and extras["account"] != source.extras.get("account"):
                raise ValueError("A picked-up task keeps its original profile")
            _workflow_id = source.workflow_id
        elif _workflow_id is not None:
            if (not isinstance(_workflow_id, str) or not isinstance(idempotency_key, str)
                    or not self.workflows.owns_run_request(_workflow_id, idempotency_key)):
                raise ValueError("This task has no claimed saved workflow")
        # A canonical pickup carries its original private snapshot even if the saved profile was edited or removed.
        if source is not None and isinstance(source.extras.get("account"), dict):
            if source.account_context is None:
                raise ValueError("This task has no saved profile context")
            extras = harness_api.clean_extras({k: v for k, v in extras.items() if k != "account"}, self)
            extras["account"] = source.extras["account"]
        else:
            extras = harness_api.clean_extras(extras, self)
            predecessor = self.runs.get(extras.get("resumeFrom")) if extras.get("resumeFrom") else None
            if source is None and predecessor is not None and (predecessor.extras.get("account")
                                                               or predecessor.workflow_id):
                raise ValueError("Pick up this task from its saved run so its profile and workflow stay fixed")
        if harness_api.calls_no_model(extras) and engine not in (None, engines.SMART):
            raise ValueError("This task calls no model: leave the engine out")
        if extras.get("projectEnv") and isinstance(goal, str):
            self._check_project_env(extras["projectEnv"], goal, engine, origin)
        account = extras.get("account")
        account_context = account_digest = None
        if account is not None:
            from .accounts import routing as account_routing
            if source is not None:
                account_context = source.account_context
                account_digest = account_routing.digest(account_context)
            else:
                account_context, account_digest = account_routing.capture(self, account)
        if account is not None and isinstance(goal, str):
            goal, device = account_routing.route(self, account_context, goal, device)
        if default_device not in ("last_used", "primary"):
            raise ValueError("default_device must be last_used or primary")
        fleet = getattr(self, "fleet", None)
        phone = None
        if device is not None and (fleet is None or self.pool is not None):
            raise ValueError("This Mobster runs every task on its own device; leave device out")
        if fleet is not None and self.pool is None:
            # Resolved before the runtime's lock: naming a device reads the USB listing and the registries.
            if device is not None:
                phone = self.resolve_device(device)
            elif self.one_target:
                phone = fleet.primary
            elif default_device == "primary":
                phone = self.resolve_default(sticky=False)
            else:
                phone = self.resolve_device(None)
        primary = phone is None or phone.primary
        chosen = engine
        if chosen is None:
            chosen = engines.SMART
        if account is not None and chosen != engines.SMART:
            # Only Smart confirms the account on screen before it commits (accounts/screen.py).
            from .accounts.routing import SMART_ONLY
            raise ValueError(SMART_ONLY)
        if pinned_apps is not None:
            if (not isinstance(pinned_apps, list) or not 1 <= len(pinned_apps) <= orchestrate.PINNED_APPS
                    or not all(isinstance(item, str) and 0 < len(item) <= 160 for item in pinned_apps)
                    or len(set(pinned_apps)) != len(pinned_apps)):
                raise ValueError(f"pinnedApps is 1-{orchestrate.PINNED_APPS} different app ids")
            if app_id is None:
                app_id = pinned_apps[0]
        any_app = chosen == engines.SMART or ai_plans.is_agent(chosen)
        if app_id is None and any_app:
            app_id = ANY_APP["id"]
        if (not isinstance(app_id, str) or not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 4000
                or mode != "live"):
            raise ValueError("Choose an app, a task (1–4000 characters), and live mode")
        if idempotency_key is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key):
            raise ValueError("Invalid Idempotency-Key")
        output_schema = validate_output(output_format, output_schema)
        if allowed_bundles is not None:
            if not isinstance(allowed_bundles, list) or not allowed_bundles or len(allowed_bundles) > 31:
                raise ValueError("allowedBundles must be 1-31 bundle identifiers")
            try:
                extra_bundles = frozenset(validate_bundle_id(bundle) for bundle in allowed_bundles)
            except ValueError:
                raise ValueError("allowedBundles must be valid bundle identifiers") from None
        else:
            extra_bundles = None
        identity = [app_id, goal.strip(), mode, output_schema]
        # Preserve existing JSON request tombstones; another format is a distinct request.
        if output_format != "json":
            identity.append({"outputFormat": output_format})
        if extra_bundles is not None:
            identity.append({"allowedBundles": sorted(extra_bundles)})
        if engine is not None:
            identity.append({"engine": engine})
        if device is not None and phone is not None:
            identity.append({"device": phone.id})
        if extras:
            identity.append({"extras": extras})
        if account_digest is not None:
            identity.append({"accountContextSha256": account_digest})
        if _workflow_id is not None:
            identity.append({"workflowId": _workflow_id})
        if group_id is not None:
            if not isinstance(group_id, str) or not re.fullmatch(r"[a-f0-9]{12}", group_id):
                raise ValueError("A group id is 12 lowercase hex characters")
            identity.append({"groupId": group_id})
        if pinned_apps:
            identity.append({"pinnedApps": list(pinned_apps)})
        fingerprint = hashlib.sha256(json.dumps(identity,
            ensure_ascii=True, sort_keys=True, allow_nan=False).encode()).hexdigest()
        if account is not None and phone is not None and getattr(fleet, "health", None) is not None:
            # Refused before anything is claimed; a retry of a started request gets its task back first.
            with self.lock:
                replayed = None if self.closing else self._replay(idempotency_key, fingerprint)
            if replayed is not None:
                return replayed
            fleet.health.preflight(account, phone)
        if ai_plans.is_agent(chosen):
            # Before the runtime's lock: a first check runs the CLI's own status command.
            self.check_agent(chosen)
        with self.lock:
            if self.closing:
                raise APIError("The service is shutting down", 503, "shutting_down")
            if source is not None and self.runs.get(source.id) is not source:
                raise APIError("The task being picked up left saved history. Nothing was restarted.", 410,
                               "run_expired")
            replayed = self._replay(idempotency_key, fingerprint)
            if replayed is not None:
                return replayed
            waits = None
            line = self.queues.find(self.queue_key(phone))
            in_line = line is not None and (len(line) > 0 or line.held())
            if phone is not None:
                busy = self.run_on(phone)
                if busy is not None or in_line:
                    if not queue:
                        raise self.busy_error(phone, busy, primary)
                    waits = self.queue_key(phone)
            elif not self.capacity() or in_line:
                if not queue:
                    raise self.busy_error(None, self.active, primary)
                waits = self.queue_key(None)
            if waits is not None and len(self.queues.of(waits)) >= self.queues.limit:
                raise APIError(ownership.QUEUE_FULL.format(count=len(self.queues.of(waits)),
                                                           phone=self.phone_name(phone)), 429, "queue_full")
            phone_prefs = fleet.prefs(phone) if fleet is not None and phone is not None else {}
            if phone is not None and fleet.is_disconnected(phone):
                raise APIError(f"{phone.name or 'This iPhone'} is disconnected in Mobster. Connect it in Settings › "
                               "Phones to run tasks on it.", 409, "device_disconnected", device=phone.id)
            phone_apps = frozenset(phone_prefs["apps"]) if phone_prefs.get("apps") else None
            admitted = None
            if origin in harness_api.CLAIMED_ORIGINS:
                # A console run (teams/link.py) narrows the apps and limits; no claim registered, no run.
                claim = harness_api.origin_claim(origin)
                if claim is None or not set(claim[2]) <= set(extras):
                    raise APIError("That origin needs its own session on this Mac.", 403, "origin_refused")
                admitted = claim[1](self, extras=extras, phone=phone, engine=chosen)
                if admitted.apps is not None:
                    phone_apps = admitted.apps if phone_apps is None else phone_apps & admitted.apps
                if admitted.cap_usd is not None:
                    cap_usd = admitted.cap_usd if cap_usd is None else min(cap_usd, admitted.cap_usd)
                if admitted.ask_floor == permissions.ASK_ALL:
                    extras["permissionMode"] = permissions.ASK_ALL
                if not admitted.long_tasks and extras.get("longform") is not None:
                    raise ValueError("A long task can't start from there. Start it in Mobster on this Mac")
            catalog = self.apps_for(phone)
            if account is not None and app_id == ANY_APP["id"]:
                # A task as an account starts in the account's app, unless it named another one.
                app_id = next((a["id"] for a in catalog if a.get("bundleId") == account["app"]["bundleId"]), app_id)
            app = ANY_APP if app_id == ANY_APP["id"] and any_app else next(
                (a for a in catalog if a["id"] == app_id), None)
            if not app:
                raise ValueError("Choose an app from your catalog")
            pins = None
            if pinned_apps:
                pins = []
                for pinned in pinned_apps:
                    found = next((a for a in catalog if a["id"] == pinned), None)
                    if found is None:
                        raise ValueError("Pin apps from your catalog")
                    if found.get("installed") is False:
                        raise ValueError(f"{found['name']} isn't installed on "
                                         f"{(phone.name if phone is not None and phone.name else 'this iPhone')}")
                    pins.append({"id": found["id"], "name": found["name"], "bundleId": found.get("bundleId")})
            app_policy = permissions.from_env().apps
            outside = origin in permissions.policy.OUTSIDE_ORIGINS
            if app.get("bundleId") and not app_policy.allows(app["bundleId"], outside=outside):
                raise APIError(app_policy.blocked_message(app.get("name"), outside=outside and app_policy.allows(
                    app["bundleId"])), 403, "app_not_allowed")
            long_task = None
            if chosen == engines.SMART and not harness_api.calls_no_model(extras) and \
                    (admitted is None or admitted.long_tasks):
                long_task = self.long_task(goal, app, extras, origin, pins)
            elif extras.get("longform") is not None:
                raise ValueError("Long tasks run on Mobster's agent")
            if phone_apps is not None and app is not ANY_APP and app.get("bundleId") not in phone_apps:
                raise APIError(f"{app['name']} isn't one of the apps Mobster may use on {phone.name or 'this phone'}. "
                               "Change it in Settings › Phones.", 400, "app_not_allowed", device=phone.id)
            outside = [pin for pin in pins or () if phone_apps is not None and pin["bundleId"] not in phone_apps]
            if outside:
                # A pin is an app the task uses: the phone's app list limits it as it limits the start app.
                raise APIError(f"{outside[0]['name']} isn't one of the apps Mobster may use on "
                               f"{phone.name or 'this phone'}. Change it in Settings › Phones.", 400, "app_not_allowed",
                               device=phone.id)
            allowed = None
            if extra_bundles is not None:
                known = {a["bundleId"] for a in catalog if isinstance(a.get("bundleId"), str)}
                unknown = sorted(extra_bundles - known)
                if unknown:
                    raise ValueError("Unknown app bundle: " + ", ".join(unknown[:5]))
                if phone_apps is not None and extra_bundles - phone_apps:
                    raise APIError("Some of those apps aren't ones Mobster may use on "
                                   f"{phone.name or 'this phone'}: " + ", ".join(sorted(extra_bundles - phone_apps)[:5])
                                   + ". Change it in Settings › Phones.", 400, "app_not_allowed", device=phone.id)
                blocked = sorted(b for b in extra_bundles if not app_policy.allows(b))
                if blocked:
                    names = {a["bundleId"]: a["name"] for a in catalog if a.get("bundleId")}
                    raise APIError(app_policy.blocked_message(names.get(blocked[0], blocked[0])), 403,
                                   "app_not_allowed")
                allowed = frozenset({app["bundleId"]} | extra_bundles)
            self.check_ready(phone, primary, NO_MODEL if harness_api.calls_no_model(extras) else chosen, origin,
                             idempotency_key)
            cap = None
            limit = self.monthly_limit()
            if limit is not None:
                cap = self.monthly_cap(limit, raised=(long_task or {}).get("capUsd"))
            if app is not ANY_APP:
                if primary:
                    self.check_app(app)
                elif app.get("installed") is False:
                    raise ValueError(f"{app['name']} isn't installed on {phone.name or 'this device'}")
            self.teaching(None if primary else phone)
            run = Run(app, goal.strip(), mode, idempotency_key=idempotency_key, output_schema=output_schema,
                      output_format=output_format, allowed_bundles=allowed, journal=self.journal,
                      phone=None if primary else phone,
                      device_id=phone.id if phone is not None else None,
                      device_name=phone.name if phone is not None else None,
                      engine=chosen, frames=getattr(self, "frames", None),
                      estimate_usd=self.estimate_for(chosen, goal.strip(), output_schema, output_format),
                      extras=extras, account_context=account_context, workflow_id=_workflow_id, origin=origin,
                      listeners=getattr(self, "listeners", None),
                      longform=long_task, group_id=group_id, phone_apps=phone_apps, pinned_apps=pins,
                      ask_override=phone_prefs.get("askBeforeActing"),
                      approvals_closed=self.approvals_closed if origin in harness_api.UNATTENDED_ORIGINS else None)
            self.remembering(run)
            run.client = ownership.clean_client(ownership.current_client()) if origin == "mcp" else None
            self.apply_caps(run, cap, cap_usd)
            if self.pool is None and waits is None:
                try:
                    run.lease = Lease.device((self.config.wda_url if primary else phone.wda_url).rstrip("/"))
                except JournalError:
                    raise APIError("Your iPhone is busy in another Mobster process" if primary else
                                   f"{phone.name or 'This device'} is busy in another Mobster process",
                                   code="device_busy") from None
            device = None
            if self.pool is not None and waits is None:
                # At admission, not dispatch: a task that can't get a device is refused while the caller waits.
                try:
                    device = self.pool.acquire(run.id, timeout=0)
                except NoDeviceAvailable:
                    elsewhere = any("another Mobster process" in item["reason"] for item in self.pool.public())
                    raise APIError(
                        "Your iPhone is busy in another Mobster process" if elsewhere else
                        "Every assigned phone is busy; stop a run or wait for one to finish",
                        code="device_busy" if elsewhere else "run_active",
                        activeRunId=self.active) from None
                run.device = device
                run.lease = PooledLease(self.pool, run.id)
            held_url = self.run_phone_url(run)
            if waits is None:
                try:
                    wda_watchdog.tracker.run_started(held_url, run.id)  # refused while the helper restarts
                except wda_watchdog.ActionBlocked:
                    if run.lease:
                        run.lease.close()
                    raise APIError("Mobster's helper on your iPhone is restarting. No action was taken. "
                                   "Try again in a moment.", 503, "device_unavailable") from None
            try:
                expired = self.journal.create(run.metadata(), idempotency_key, fingerprint)
            except Exception:
                if waits is None:
                    wda_watchdog.tracker.run_ended(held_url, run.id)
                if run.lease:
                    run.lease.close()
                raise
            self._retain_run(run, expired)
            thread = None
            try:
                if waits is not None:
                    # Held runs are durable, listed and cancellable, with no phone, lease or worker.
                    self.enqueue(run, phone, waits, queue_cap=cap_usd, chosen=chosen, primary=primary)
                else:
                    self.active_runs[run.id] = device.id if device else phone.key if phone is not None else None
                if run.listeners is not None:
                    run.listeners.post("on_created", run, self)
                self.publish("runs", {"event": "run_created", "runId": run.id,
                                      **({"threadId": extras["threadId"]} if extras.get("threadId") else {}),
                                      **({"device": run.device_id} if run.device_id else {})})
                if waits is not None:
                    self.publish("devices", {"event": "queue_changed", "device": run.device_id})
                    return (run, False) if idempotency_key else run
                thread = threading.Thread(target=self.work, args=(run,), daemon=True)
                self.threads.add(thread)
                thread.start()
            except Exception:
                self.threads.discard(thread)
                self.active_runs.pop(run.id, None)
                if waits is not None:
                    self.unqueue(run.id)
                if waits is None:
                    wda_watchdog.tracker.run_ended(held_url, run.id)
                if run.lease:
                    run.lease.close()
                run.finish({"status": "interrupted", "independently_verified": False,
                            "reason": "The task worker could not start. No agent action was dispatched."})
                raise APIError("The task could not start; check its saved status before retrying", 503,
                               "start_failed") from None
        if phone is not None:
            fleet.mark_used(phone)
        return (run, False) if idempotency_key else run

    def _retain_run(self, run, expired):
        # Call under self.lock after journal.create succeeds; its expired ids own history retention.
        for identifier in expired:
            self.runs.pop(identifier, None)
            if getattr(self, "frames", None) is not None:
                self.frames.delete(identifier)
        self.runs[run.id] = run

    def compose_result(self, *, app, goal, summary, idempotency_key, extras=None, origin="workflow",
                       device=(None, None)):
        # No phone, no model call, acts on nothing; Run.finish validates its blocks like any task's.
        if self.plan_gate is not None:
            self.plan_gate.require()
        if origin not in harness_api.ORIGINS:
            raise ValueError("Unknown task origin")
        if not isinstance(app, dict) or not isinstance(app.get("id"), str):
            raise ValueError("A result names the app its tasks ran in")
        extras = harness_api.clean_extras(extras, self)
        fingerprint = hashlib.sha256(json.dumps(["compose", app["id"], goal, extras], ensure_ascii=True,
                                                sort_keys=True, default=str).encode()).hexdigest()
        with self.lock:
            if self.closing:
                raise APIError("The service is shutting down", 503, "shutting_down")
            prior = self.journal.lookup(idempotency_key)
            if prior and prior[0] in self.runs:
                return self.runs[prior[0]]
            if prior:
                raise APIError("This result has expired from history", 410, "run_expired")
            run = Run(dict(app), goal.strip()[:4000], "live", idempotency_key=idempotency_key, journal=self.journal,
                      frames=getattr(self, "frames", None), device_id=device[0], device_name=device[1],
                      extras=extras, origin=origin, listeners=getattr(self, "listeners", None))
            expired = self.journal.create(run.metadata(), idempotency_key, fingerprint)
            self._retain_run(run, expired)
        if run.listeners is not None:
            run.listeners.post("on_created", run, self)
        self.publish("runs", {"event": "run_created", "runId": run.id,
                              **({"threadId": extras["threadId"]} if extras.get("threadId") else {})})
        run.finish(dict(summary))
        self.finished(run)
        return run

    def model_result(self, *, app, goal, call, idempotency_key, extras=None, origin="workflow"):
        # The call is metered into the run's events, so the usage ledger and the monthly limit count it.
        if self.plan_gate is not None:
            self.plan_gate.require()
        if origin not in harness_api.ORIGINS or origin in harness_api.CLAIMED_ORIGINS:
            # A claimed origin's runs are admitted in create(); this path has no admission, so none comes here.
            raise ValueError("Unknown task origin")
        if not isinstance(app, dict) or not isinstance(app.get("id"), str):
            raise ValueError("A result names the app its tasks ran in")
        extras = harness_api.clean_extras(extras, self)
        fingerprint = hashlib.sha256(json.dumps(["model", app["id"], goal, extras], ensure_ascii=True,
                                                sort_keys=True, default=str).encode()).hexdigest()
        with self.lock:
            if self.closing:
                raise APIError("The service is shutting down", 503, "shutting_down")
            prior = self.journal.lookup(idempotency_key)
            if prior and prior[0] in self.runs:
                return self.runs[prior[0]]
            if prior:
                raise APIError("This result has expired from history", 410, "run_expired")
            limit = self.monthly_limit()
            if limit is not None:
                self.monthly_cap(limit)          # refuses when this month has nothing left
            run = Run(dict(app), goal.strip()[:4000], "live", idempotency_key=idempotency_key, journal=self.journal,
                      frames=getattr(self, "frames", None), extras=extras, origin=origin,
                      listeners=getattr(self, "listeners", None))
            expired = self.journal.create(run.metadata(), idempotency_key, fingerprint)
            self._retain_run(run, expired)
        if run.listeners is not None:
            run.listeners.post("on_created", run, self)
        self.publish("runs", {"event": "run_created", "runId": run.id,
                              **({"threadId": extras["threadId"]} if extras.get("threadId") else {})})
        try:
            summary = call(run.emit)
        except Exception as error:  # the call failed or its answer didn't fit: the run says so; nothing acted
            summary = {"event": "result", "status": "error", "outcome": "couldnt_finish", "engine": None,
                       "answer": None, "reason": f"The model call didn't finish: {type(error).__name__}.",
                       "actions": 0, "decisions": 1, "elapsed_ms": 0, "model": None,
                       "independently_verified": False, "proof": []}
        run.finish(dict(summary))
        self.finished(run)
        return run

    def frame(self, run_id, frame_id):
        frames = getattr(self, "frames", None)
        run = self.runs.get(run_id)
        if frames is None or run is None:
            return None
        data = frames.get(run_id, frame_id)
        if data is not None:
            return data
        brief = (run.summary or {}).get("brief") if isinstance(run.summary, dict) else None
        for source in (brief or {}).get("runs") or () if isinstance(brief, dict) else ():
            if isinstance(source, str) and source in self.runs:
                data = frames.get(source, frame_id)
                if data is not None:
                    return data
        return None

    def start_group(self, phones, app_id, goal, idempotency_key=None, fingerprint=None, thread_id=None, **create):
        # Without a plan no phone's run is admitted, so the group isn't made either.
        if self.plan_gate is not None:
            self.plan_gate.require()
        if self.closing:
            raise APIError("The service is shutting down", 503, "shutting_down")
        return self.fanout.start(phones, app_id, goal, idempotency_key=idempotency_key, fingerprint=fingerprint,
                                 thread_id=thread_id, **create)

    @staticmethod
    def _check_project_env(root, goal, engine, origin):
        # Only Mobster's agent masks the values, and a task never starts with a blank or a literal ${NAME}.
        if not project_env.refs(goal):
            return
        if origin not in {"app", "cli", "tui", "workflow", "schedule"} or engine not in (None, engines.SMART):
            raise ValueError("A task that uses project secrets (${NAME}) runs on Mobster's agent from the app, the "
                             "terminal or a saved workflow. Remove the ${NAME}s to run it here.")
        try:
            project_env.for_task(root, goal)
        except project_env.EnvError as error:
            raise ValueError(str(error)) from None

    def start_profiles(self, profiles, app_id, goal, idempotency_key=None, fingerprint=None, thread_id=None, **create):
        if self.plan_gate is not None:
            self.plan_gate.require()
        if self.closing:
            raise APIError("The service is shutting down", 503, "shutting_down")
        return self.fanout.start_profiles(profiles, app_id, goal, idempotency_key=idempotency_key,
                                          fingerprint=fingerprint, thread_id=thread_id, **create)

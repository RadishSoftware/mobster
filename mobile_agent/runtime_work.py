"""Runtime's task work: the Smart engine's run, the worker thread and a run's end."""

import threading
import time
from pathlib import Path

from . import (
    ai_plans,
    costs,
    engines,
    harness_api,
    orchestrate,
    permissions,
    project_env,
    result_blocks,
    smart_events,
    smart_result,
    traces,
)
from .api_errors import APIError
from .compose import close_all
from .costs import SpendLedger
from .latency_trace import Trace
from .lockscreen import after_error
from .replay_core import path as replay_path
from .runtime_driver import PhoneNotReady, RunSetup
from .wda import watchdog as wda_watchdog
from .wda.guards import DriverRejection

# Finished runs whose full-size final screenshot stays in memory; older ones show their step frames.
KEPT_PREVIEWS = 3
MAX_SMART_APPS = 120


def smart_request(run):
    pins = [pin["name"] for pin in run.pinned_apps or () if pin.get("name")]
    if len(pins) > 1:
        return f"In {orchestrate.joined(pins)}: {run.goal}"
    return f"In {run.app['name']}: {run.goal}" if run.app.get("bundleId") else run.goal


# Each provider runs on its own thread while the phone gets ready; join waits until BUDGET after the gather began.
class ContextGather:
    BUDGET = 2.0

    def __init__(self, ctx, emit, budget=None):
        self.ctx, self.emit = ctx, emit
        self.budget = self.BUDGET if budget is None else budget
        self.started = time.monotonic()
        self.providers = harness_api.context_providers(ctx)
        self.jobs = []
        for provider in self.providers:
            box = {}
            thread = threading.Thread(target=self._collect, args=(provider, box), daemon=True,
                                      name=f"mobster-context-{getattr(provider, 'key', '?')}")
            thread.start()
            self.jobs.append((provider, box, thread))

    def _collect(self, provider, box):
        try:
            box["blocks"] = list(provider.blocks(self.ctx) or ())
        except BaseException as error:  # noqa: BLE001
            box["error"] = type(error).__name__

    def join(self):
        deadline = self.started + self.budget
        blocks, ready, used, chars = [], [], [], 0
        for provider, box, thread in self.jobs:
            thread.join(max(0.0, deadline - time.monotonic()))
            key = str(getattr(provider, "key", "") or "")[:40]
            if thread.is_alive():
                self.emit({"event": "context_error", "key": key, "error": "timeout"})
                continue
            if "error" in box:
                self.emit({"event": "context_error", "key": key, "error": box["error"]})
                continue
            ready.append(provider)
            cap = getattr(provider, "max_chars", None)
            for block in box.get("blocks", ()):
                if not isinstance(block, harness_api.ContextBlock) or not block.text:
                    continue
                if isinstance(cap, int) and cap > 0 and len(block.text) > cap:
                    block = harness_api.ContextBlock(block.key, block.title, block.text[:cap - 1] + "…",
                                                     block.stable, block.untrusted, block.images)
                blocks.append(block)
                chars += len(block.text)
                if key not in used:
                    used.append(key)
        if blocks:
            self.emit({"event": "context_used", "keys": used, "chars": chars})
        return blocks, ready

    def turn_context(self, providers):
        # A provider that raises is skipped for that turn.
        ctx = self.ctx

        def turn(state):
            out = []
            for provider in providers:
                try:
                    out.extend(provider.turn_blocks(ctx, state) or ())
                except Exception:  # noqa: BLE001
                    harness_api.log.exception("a provider's turn_blocks failed")
            return [block for block in out if isinstance(block, harness_api.ContextBlock) and block.text]
        return turn


class TaskWorkMixin:
    def smart_apps(self, run):
        apps = {}
        if run.app.get("bundleId"):
            apps[run.app["bundleId"]] = run.app["name"]
        for pin in run.pinned_apps or ():
            if pin.get("bundleId"):
                apps.setdefault(pin["bundleId"], pin["name"])
        for app in self.apps_for(self.phone_for(run)):
            bundle = app.get("bundleId")
            if bundle and app.get("installed") is not False and bundle not in apps:
                apps[bundle] = app["name"]
        allowed = getattr(run, "phone_apps", None)
        if isinstance(allowed, frozenset):
            apps = {bundle: name for bundle, name in apps.items() if bundle in allowed}
        return dict(list(apps.items())[:MAX_SMART_APPS])

    def estimate_for(self, engine, goal, output_schema=None, output_format=None):
        try:
            estimates = self.engine_estimates(goal, output_schema, output_format=output_format)
            if engine not in estimates:
                return None
            # The figure kept with the run is what the model would cost: a path that costs nothing can still hand over.
            return costs.midpoint({**estimates[engine], **estimates[engine].get("fallbackUsd", {})})
        except Exception:
            return None

    def engine_estimates(self, goal="", output_schema=None, output_format=None):
        state = self.engines_status()
        names = [app["name"] for app in self.apps() if app.get("installed") is not False]
        structured = output_schema is not None or output_format in smart_result.AUTOMATIC_FORMATS
        estimate = costs.smart_estimate(goal, names, available=state[engines.SMART]["available"],
                                        reason=state[engines.SMART]["reason"], structured=structured,
                                        model=state[engines.SMART]["model"])
        free = None if structured or not goal else self.free_kind(goal)
        if free is not None:
            # A request that costs no model call: $0, and what the model would cost if the path doesn't fit is kept.
            estimate = {**estimate, "lowUsd": 0.0, "highUsd": 0.0, "basis": free,
                        "fallbackUsd": {"lowUsd": estimate["lowUsd"], "highUsd": estimate["highUsd"]}}
        return {engines.SMART: estimate}

    def free_kind(self, goal):
        from .learned import zero
        state_db = getattr(self.config, "state_db", None)
        return zero.free_kind(Path(state_db).parent if state_db else None, goal)

    def run_smart(self, run, trace, setup):
        key = engines.smart_key()
        ledger = SpendLedger()
        keyless = harness_api.calls_no_model(run.extras)
        with trace.span("setup.models"):
            client = None if keyless else self.smart_client()  # a run that calls no model opens no connection
        long_task = getattr(run, "longform", None)
        events = smart_events.SmartEvents(run.emit, ledger=ledger if long_task else None)

        def inference(event):
            ledger.add_event(event)
            events.inference(event)

        if client is not None:
            costs.meter(client, inference)
        session = None
        recording = traces.Recording(getattr(self, "frames", None), run.id, smart_request(run))
        try:
            ctx = self.run_context(run)
            hooks_ = harness_api.pre_runs(ctx)
            handoff = None
            for hook in [h for h in hooks_ if getattr(h, "before_phone", False)]:
                if run.stop.is_set():
                    return None
                try:
                    done = hook(ctx, None, None)
                except Exception as error:  # noqa: BLE001 -- a track's hook never breaks the run
                    harness_api.log.exception("a pre-run hook failed")
                    run.emit({"event": "context_error", "key": "pre_run", "error": type(error).__name__})
                    continue
                if done is not None and done.result is not None:
                    result = {**done.result, "costUsd": run.cost_usd}
                    run.emit(result)
                    return result
                if done is not None and done.handoff is not None and handoff is None:
                    handoff = done.handoff
            if (run.extras or {}).get("resumeFrom") and handoff is None:
                # A disabled or missing checkpoint hook must not turn a pickup into a fresh task.
                from .harness import checkpoints
                result = checkpoints._pickup_unavailable(ctx, "missing_hook").result
                run.emit(result)
                return result
            # The plan, before the phone is touched: Start, Skip or a change.
            ended = self.confirm_plan(run)
            if ended is not None:
                run.emit(ended)
                return ended
            long_task = getattr(run, "longform", None)
            # A run that calls no model reads no context: its hook ends it, and nothing reads a provider's blocks.
            gather = None if keyless else ContextGather(ctx, run.emit)
            driver = self.open_driver(run, trace, setup)
            if driver is None:
                return None
            run.capture = getattr(driver, "capture_preview", None)
            events.driver, events.apps = driver, self.smart_apps(run)
            gate = self.permission_gate(run)
            # Full access has no approver; a one-time code the agent types still asks, through floor_approve.
            approve = events.approving(run.request_approval) if gate.mode != permissions.DONT_ASK else None
            floor_approve = None if approve is not None else events.approving(run.request_approval)
            from .accounts.screen import commit_check_for
            commit_check = commit_check_for(ctx)
            # iOS's privacy prompts ask the person in every permission mode, even with Ask before acting off.
            dialog_approve = approve or (events.approving(run.request_approval) if ctx.interactive else None)
            program = parts = None
            for hook in hooks_:
                if getattr(hook, "before_phone", False):
                    continue
                if (handoff is not None or program is not None) and not getattr(hook, "after_handoff", False):
                    continue
                if run.stop.is_set():
                    return None
                try:
                    # No approver: a routine hands off at its first commit, which the loop checks the account for.
                    done = hook(ctx, driver, approve if commit_check is None else None)
                except Exception as error:  # noqa: BLE001 -- a hook with the phone may have acted before it failed
                    harness_api.log.exception("a pre-run hook failed")
                    run.emit({"event": "context_error", "key": "pre_run", "error": type(error).__name__})
                    result = {"event": "result", "engine": engines.SMART, "status": "completion_not_confirmed",
                              "outcome": "check", "code": "outcome_unknown", "answer": None, "proof": [],
                              "independently_verified": False, "actions_may_have_run": True, "costUsd": run.cost_usd,
                              "reason": "Mobster stopped while preparing the task. An action may have run. "
                                        "Inspect your iPhone before starting another task."}
                    run.emit(result)
                    return result
                if done is None:
                    continue
                if handoff is None and done.result is not None:
                    result = {**done.result, "costUsd": run.cost_usd}
                    run.emit(result)
                    return result
                if handoff is None and done.handoff is not None:
                    handoff = done.handoff
                if handoff is None and program is None and getattr(done, "program", None) is not None:
                    program = done.program  # a saved program: the live loop takes its steps first
                if parts is None and getattr(done, "parts", None) is not None:
                    parts = done.parts  # parts of saved programs: the live loop may run one at a planning point
            if keyless:
                # A hook that doesn't end a run that calls no model leaves nothing to run: the loop below is a model's.
                raise RuntimeError("Nothing ran this task, which calls no model. No action was taken; try again.")
            if long_task:
                from .longform import plan as long_plan
                from .longform import session as long_session
                card = long_task["plan"]
                session = long_session.start(
                    run_id=run.id, goal=run.goal, plan=card, budget=long_plan.budget_of(card), origin=run.origin,
                    nav_client=client, key=key, inference=inference, ledger=ledger, cap_usd=self.run_cap(run),
                    emit=events, frames=getattr(self, "frames", None), journal=self.journal, awake=self.awake,
                    spent_offset=float(getattr(handoff, "cost_usd", 0.0) or 0.0), rules=self.thread_rules(ctx),
                    earlier=self.earlier_seen(ctx, card), run=run)
            blocks, providers = gather.join()
            if session is not None:
                session.guidance = tuple(b.text for b in blocks or () if getattr(b, "key", None) == "plugins")
            hooks = {}
            if blocks:
                hooks["context_blocks"] = blocks
            if providers:
                hooks["turn_context"] = gather.turn_context(providers)
            extra_tools = harness_api.tools(ctx)
            withheld = harness_api.WITHHELD_SKILLS.get(run.origin, frozenset())
            if extra_tools or withheld:
                from .frontier import default_skills
                hooks["skills"] = tuple(skill for skill in (*default_skills(), *extra_tools)
                                        if getattr(skill, "op", None) not in withheld)
            if commit_check is not None:
                # A task as an account: every commit in its app waits for the account confirmed on screen.
                hooks["commit_check"] = commit_check
            try:
                hooks.update(harness_api.frontier_options(ctx))
            except Exception as error:  # noqa: BLE001
                harness_api.log.exception("frontier options failed")
                run.emit({"event": "context_error", "key": "frontier_options", "error": type(error).__name__})
            listeners = run.listeners if harness_api.listeners() else None
            if listeners is not None:
                # Only the hooks a listener takes: the checkpoint hook runs the secret filter on the agent's thread.
                wanted = {name for listener in harness_api.listeners() for name in ("on_step", "on_checkpoint")
                          if callable(getattr(listener, name, None))}
                if "on_step" in wanted:
                    hooks["step_observer"] = lambda record: listeners.post("on_step", run, record)
                if "on_checkpoint" in wanted:
                    hooks["checkpoint"] = lambda state: listeners.post("on_checkpoint", run, state)
            # A long run's DONE turn carries no data: its schema is applied to the final report instead.
            nav_schema = None
            if session is None:
                nav_schema = run.output_schema
                if nav_schema is None and run.output_format in smart_result.AUTOMATIC_FORMATS:
                    nav_schema = smart_result.TABLE
            if session is not None:
                # A long run asks before every act that asks, with Ask before acting off too: forced_approve asks.
                hooks.update(longform=session, forced_approve=events.approving(run.request_approval))
            agent = engines.build_frontier(driver, client, apps=events.apps, emit=events,
                                           max_cost_usd=self.run_cap(run),
                                           approve=approve, cancelled=run.stop.is_set, steering=run.steering,
                                           clarify=ctx.clarify, run_context=ctx, dialog_approve=dialog_approve,
                                           permissions=gate, floor_approve=floor_approve,
                                           permission_mode=gate.mode, saved_program=program,
                                           program_parts=parts,
                                           output_schema=nav_schema,
                                           guard=run.guard, recorder=recording.attach, **hooks)
            events.agent = agent
            if run.stop.is_set():
                return None  # The loop's first step is a model call (the task contract), billed even if stopped.
            setup.phase("running")
            request = smart_request(run)
            if (run.extras or {}).get("projectEnv"):
                # The goal keeps ${NAME}; the agent gets the values and masks them from its first turn on.
                values = project_env.for_task(run.extras["projectEnv"], request)
                request = project_env.expand(request, values)
                agent.preset_secrets = set(values.values())
            with trace.span("smart.run"):
                outcome = agent.run(request, initial=handoff) if handoff is not None else agent.run(request)
            recording.finish(outcome)
            report = markdown = None
            if session is not None:
                try:
                    session.capture_contract(getattr(agent, "_contract", None), final=True)
                except Exception:  # receipt persistence cannot prevent phone release or report synthesis
                    try:
                        run.emit({"event": "longform_receipt_unavailable"})
                    except Exception:
                        pass
                # Synthesis needs no phone, so the phone is free before the report is written.
                setup.finish_phone()
                session.release_phone()
            events.finish()
            run.private = {"notes": list(outcome.get("notes") or ()), "plan": outcome.get("plan") or "",
                           "contract": [{name: getattr(item, name, "") for name in (
                               "kind", "app", "what", "payload", "act", "count", "condition", "quote")}
                               for item in (getattr(getattr(agent, "_contract", None), "items", None) or ())],
                           # Hashes with no text: a replay that ends on this screen with its typed words is done.
                           "screen": replay_path.screen_hashes(getattr(agent, "last_screen", None))}
            if run.stop.is_set() and outcome.get("status") not in ("approval_denied", "approval_timeout"):
                outcome = {**outcome, "status": "stopped"}
            if session is not None:
                from .longform.synthesis import synthesize
                with trace.span("longform.report"):
                    report, markdown = synthesize(session, outcome.get("status") or "error",
                                                  answer=outcome.get("answer"))
            result = smart_result.summarize(outcome, agent, events, request=request, output_format=run.output_format,
                                            output_schema=run.output_schema, cost_usd=run.cost_usd, report=report,
                                            markdown=markdown)
            blocks = result_blocks.from_report(report) if report is not None else None
            if blocks is not None:
                result["blocks"] = blocks
            if report is not None and report.get("partial"):
                from .longform.report import partial_line
                result["reason"] = partial_line(report, session.budget) or result.get("reason")
            self.learn_reach(key, client, outcome.get("reason"), result.get("structure_error"))
            result["costUsd"] = run.cost_usd
            self.note_limit(run, result)
            run.emit(result)
            return result
        finally:
            recording.finish()
            if session is not None:
                setup.finish_phone()
                session.release_phone()
                session.close()
            engines.release_client(client)

    def thread_rules(self, ctx):
        thread_id = getattr(ctx, "thread_id", None)
        if not thread_id:
            return ()
        try:
            from .threads.service import service_for
            service = service_for(self)
            thread = service.store.get(thread_id) if service is not None else None
            return tuple(str(rule) for rule in (thread or {}).get("rules") or () if str(rule).strip())[:12]
        except Exception:  # noqa: BLE001
            return ()

    def earlier_seen(self, ctx, card):
        if (card or {}).get("kind") not in ("feed", "browse", "survey"):
            return set()
        try:
            from .longform.recall import earlier_identities
            return earlier_identities(self, getattr(ctx, "thread_id", None), ctx.run_id, card.get("kind"))
        except Exception:  # noqa: BLE001
            return set()

    def agents_status(self, max_age=60.0, wait=False):
        from .ai_plans import agents
        return agents.snapshot(max_age=max_age, wait=wait)

    def plan_agent(self):
        if self.scripted_models() or engines.smart_key():
            return None
        from .ai_plans import agents
        return agents.ready()

    def check_agent(self, engine):
        from .ai_plans import agents
        agent = agents.get(engine)
        state = agent.status(max_age=30)
        if not state.get("ready"):
            agent.forget_status()
            state = agent.status(max_age=0)
        if not state.get("ready"):
            reason = (f"{agent.name} isn't installed on this Mac." if not state.get("installed")
                      else agent.signed_out_sentence())
            raise APIError(reason, 409, "engine_unavailable", engine=engine,
                           agentState="not_installed" if not state.get("installed") else "signed_out")

    def run_agent(self, run, trace, setup):
        from .ai_plans import runner
        return runner.run(self, run, trace, setup)

    def learn_reach(self, key, client, *errors):
        reach = getattr(self, "model_reach", None)
        if reach is None:
            return
        for error in errors:
            ok, why = engines.model_failure(error)
            if ok is False:
                reach.record(key, False, why)
                return
        if (getattr(client, "usage", None) or {}).get("calls") and not getattr(client, "model_failed", True):
            reach.record(key, True)

    def work(self, run):
        driver = None
        trace = Trace()
        phase = "preparing"
        result = {"status": "stopped", "independently_verified": False,
                  "reason": "The task stopped before executing agent actions."}

        def set_phase(value):
            nonlocal phase
            phase = value

        def hold(value):
            nonlocal driver
            driver = value
            return value

        def finish_phone():
            nonlocal driver
            if run.phone_finished:
                return
            # Drain a concurrent preview before closing its driver. Never capture after the lease moves.
            if driver and not run.stop.is_set() and not self.closing:
                with trace.span("finish.preview"):
                    self.preview(run, final=True)
            with run.image_lock:
                run.phone_finished = True
                run.capture = None
            if run.guard is not None and driver is not None:
                try:
                    run.guard.finish(driver)
                except Exception:
                    pass
            with trace.span("finish.close"):
                close_all(driver)
            driver = None
            run.guard = None
            if run.lease:
                run.lease.close()
                run.lease = None
            with self.lock:
                self.active_runs.pop(run.id, None)
                # Admission also holds this lock; end the old tracker state before a new run can start.
                wda_watchdog.tracker.run_ended(self.run_phone_url(run), run.id)
            try:
                self.run_ended(run)
            except Exception:  # queue notification cannot change a task's completed phone work or report
                harness_api.log.exception("phone released but its queue could not advance")

        try:
            if run.stop.is_set():
                return
            run.status = "running"
            run.started_at = time.time()
            self.power.run_started(run)
            run.emit({"event": "run_started", "mode": run.mode, "app": run.app["name"], "engine": run.engine})
            if ai_plans.is_agent(run.engine):
                done = self.run_agent(run, trace, RunSetup(phase=set_phase, hold=hold,
                                                           finish_phone=finish_phone))
                if done is not None:
                    result = done
                return
            done = self.run_smart(run, trace, RunSetup(phase=set_phase, hold=hold,
                                                     finish_phone=finish_phone))
            if done is not None:
                result = done
        except Exception as exc:
            inactive_app = phase == "reading_app" and isinstance(exc, DriverRejection) and exc.code == "app_not_active"
            outdated_bridge = (phase == "reading_app" and isinstance(exc, DriverRejection)
                               and exc.code == "bridge_update_required")
            result = {"event": "result", "status": "stopped" if run.stop.is_set() else "error",
                "independently_verified": False, "error_type": type(exc).__name__,
                "native_code": exc.code if isinstance(exc, DriverRejection) else None,
                "actions_may_have_run": phase in {"running", "launching"},
                "reason": ("The task was stopped while preparing the app."
                           if run.stop.is_set() and phase == "preparing" else
                           str(exc) if isinstance(exc, PhoneNotReady) else
                           "The app launch could not be confirmed. It may have opened; the launch was not retried."
                           if phase == "launching" else
                           (f"iOS has not activated {run.app['name']}. Its accessibility bridge is responding, "
                            "but the app is inactive. No agent action was dispatched.") if inactive_app else
                           (f"{run.app['name']} is still running an outdated interaction bridge. Close and reopen "
                            "the app to load the installed update. No agent action was dispatched.")
                           if outdated_bridge else
                           (f"Could not read {run.app['name']} on your iPhone. Check for a locked screen or a "
                            "system prompt, then try again. No agent action was dispatched.")
                           if phase == "reading_app" else
                           "The task could not prepare your app. No agent action was dispatched."
                           if phase == "preparing" else
                           "The task stopped after a service error. An in-flight action may have run; "
                           "it was not retried.")}
            if isinstance(exc, PhoneNotReady) and not run.stop.is_set():
                result["stop_code"] = exc.code  # phone_locked, unplugged ... (agent_hooks.STOP_CODES)
            elif not run.phone_finished and not run.stop.is_set() and phase in {"running", "launching"}:
                # A step failed outside the loop's guard: ask the phone once whether it was unplugged or locked.
                try:
                    verdict = after_error(run.guard, driver)
                except Exception:  # noqa: BLE001 -- the service-error sentence stands
                    verdict = None
                if verdict is not None:
                    result.update(reason=verdict.message, stop_code=verdict.code)
            result.update(engine=run.engine, costUsd=run.cost_usd,
                          outcome="stopped" if run.stop.is_set() else "couldnt_finish", answer=None, proof=[])
        finally:
            self.awake.release(run.id)
            self.power.run_finished(run)
            finish_phone()
            try:
                run.emit(trace.event())
            except Exception:
                pass  # Telemetry never changes a run's outcome.
            try:
                run.finish(result)
            finally:
                with self.lock:
                    self.threads.discard(threading.current_thread())
                self.release(run)
                try:
                    self.finished(run)
                except Exception:  # noqa: BLE001 -- listeners and the bus never change a run's outcome
                    harness_api.log.exception("run_finished listeners failed")
                    run.settled.set()

    def release(self, run):
        # Otherwise every run kept its driver's screens and trees until it left the 100-run history.
        run.capture = None
        run.guard = None
        with self.lock:
            shown = sorted((r for r in self.runs.values() if r.finished_at is not None and r.image is not None),
                           key=lambda r: r.finished_at, reverse=True)
        for old in shown[KEPT_PREVIEWS:]:
            if old.image_lock.acquire(blocking=False):  # held only while a capture is in flight
                try:
                    old.image, old.image_captured_at = None, None
                finally:
                    old.image_lock.release()

    def finished(self, run):
        # Listeners' on_finished, then the bus's run_finished, in that order, on the listeners' thread.
        listeners = getattr(run, "listeners", None)
        thread_id = (run.extras or {}).get("threadId") if isinstance(getattr(run, "extras", None), dict) else None
        event = {"event": "run_finished", "runId": run.id, "status": run.status,
                 **({"threadId": thread_id} if thread_id else {})}
        fanout = getattr(self, "fanout", None) if getattr(run, "group_id", None) else None
        fleet = getattr(self, "fleet", None)
        if fleet is not None and getattr(fleet, "health", None) is not None:
            fleet.health.note_run(run)
        if listeners is None:
            self.publish("runs", event)
            if fanout is not None:
                fanout.member_finished(run)
            run.settled.set()
            return
        listeners.post("on_finished", run, run.summary, dict(run.private or {}))
        listeners.call(run.settled.set)
        listeners.call(lambda: self.publish("runs", event))
        if fanout is not None:
            # After the conversation has the run's result: the group's summary reads every phone's.
            listeners.call(lambda: fanout.member_finished(run))

    def _finished_run(self, identifier, still_running=None):
        with self.lock:
            run = self.runs.get(identifier)
        if not run:
            raise APIError("Task not found", 404, "not_found")
        if still_running and run.finished_at is None:
            raise APIError(still_running, 409, "run_active")
        return run

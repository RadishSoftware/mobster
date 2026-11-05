import base64
import time

from .smart import LEFT_APP, LEFT_APP_FIX, check_approver, check_guard, no_smart_key, smart_request
from .verdict import CouldntRun

NOTE_LIMIT = 500


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


class Flow:
    def run_smart(self, cancelled=None):
        from .. import costs, engines, smart_events
        from ..model_clients import cost_usd
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
                if not data:
                    self._observe_screen(None)
                if path is not None:
                    self._give_after_frame(path)
                self.event({"event": kind, "step": event.get("step"), "operations": event.get("operations"),
                            "out": event.get("out")})
                return
            self.event(event)
            if kind == "frontier_action":
                op = event.get("operation") or ""
                label = event.get("target_label") or ""
                text = smart_events.step_text(op, label, names, text=event.get("text"))
                typed = event.get("text") if op in smart_events.TYPING else None
                step = self.record_step(op=op, text=text, target={"id": None, "label": label or None, "role": None},
                                        typed=typed, changed=event.get("changed"))
                # Its frame is the screen after it: the image Smart sees next turn, or the verdict frame.
                self._awaiting_frame.append(step)
                self.progress(f"  {text}")

        with self.lock:
            client = engines.build_client(key=key)
            costs.meter(client, on_inference)
            guard = check_guard(self.manager, self.app.bundle_id)
            driver, recording = self.driver, {}
            if self.replay is not None and self.replay.mode != "off":
                recorder = self.replay.recorder()  # the path a later run replays, if this one passes
                driver, recording = recorder.wrap(self.driver), {"step_observer": recorder.observe_step}
            agent = engines.build_frontier(driver, client, apps=names, emit=on_event,
                                           max_cost_usd=self.check.max_usd, approve=check_approver(self.manager),
                                           cancelled=stopping, guard=guard, dialogs=True,
                                           dialog_policy=self.dialogs, **recording)
            agent.max_seconds = float(self.check.max_seconds)  # the check's time limit, not the app's 15 minutes
            from .. import intents
            if intents.enabled() and getattr(self.target, "udid", None):
                from ..intents.plan import LinkFirst
                agent.link_first = LinkFirst.for_device(self.target.udid)
            request = smart_request(self.app.name, self._live.steps)
            self.progress(f"Smart: {request}")
            outcome = agent.run(request)
        self.timing["flow"] = round(time.monotonic() - started, 2)
        status = outcome.get("status")
        if outcome.get("code") == "unknown_outcome":
            raise self._fail(CouldntRun("unknown_outcome", "A phone action's outcome is unknown.",
                                        "Inspect the phone before another run; Mobster did not retry the action."))
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
                      "model": getattr(client, "model", None), "turns": outcome.get("steps"),
                      **_link_first(outcome)}
        self.log(f"Smart ended {status} after {outcome.get('steps')} turns, ${self.cost_usd:.4f}")
        self.state = "ready"

    def run_flow(self, cancelled=None):
        """The check's steps: replayed from the recorded path when one is cached and replay is on, else Smart."""
        if (self.replay is not None and not (self.check.throughout or self.check.somewhere)
                and self.replay.lookup() is not None):
            return self.run_replay(cancelled)  # a replay shows no screens to sweep, so `throughout` runs Smart
        if self.replay is not None and self.replay.reason:
            self.progress(f"Smart runs every step ({self.replay.reason}); a pass records the path to replay")
        return self.run_smart(cancelled)

    def run_replay(self, cancelled=None):
        """Replay the recorded path with no model (replay_core); Smart heals a step that changed, for that step only,
        unless replay is strict."""
        from .. import engines
        if self.mode != "smart" or self.replay is None:
            raise ValueError("run_replay needs mode smart with replay on")
        self.state = "running"
        started = time.monotonic()
        steps = len(self.replay.path["steps"])
        if (steps and (self._a11y is not None or self._l10n is not None)
                and not (self.source_observation is not None and self.source_observation.active)):
            self._incomplete_screen("Replay did not read each intermediate accessibility screen")
        self.progress(f"Replaying {steps} recorded step{'' if steps == 1 else 's'} (no model)")
        with self.lock:
            tune = getattr(self.driver, "tune", None)
            if callable(tune):
                try:
                    tune(rich=True, glide=True)
                except Exception as error:
                    self.log(f"tune failed: {type(error).__name__}")
            result = self.replay.replay(cancelled)
        self.timing["flow"] = round(time.monotonic() - started, 2)
        self.cost_usd = round(result.cost_usd, 5)
        status = {"done": result.model_status or "done", "declined": "approval_denied", "stopped": "stopped",
                  "drift": "done"}.get(result.status)
        if result.status == "drift":
            self._replay_drift = result.reason
        if result.status == "heal_failed" and result.model_status == "budget":
            status = "budget"
        healed = len(result.heals)
        note = (f"Replayed {result.replayed} step{'' if result.replayed == 1 else 's'} with no model"
                + (f", healed {healed}" if healed else "") + (f": {result.reason}" if result.reason else ""))
        self.agent = {"status": status or result.status, "note": self._scrub(note)[:NOTE_LIMIT],
                      "model": engines.smart_model() if result.cost_usd else None, "turns": None, "replayed": True}
        self.log(f"replay ended {result.status} after {result.replayed} steps, {healed} healed, "
                 f"${self.cost_usd:.4f}")
        self.state = "ready"
        if self._left_app:
            raise self._fail(self._smart_failure())
        if result.status == "dialog":
            raise self._fail(CouldntRun("system_dialog", f"The replay stopped: {result.reason}",
                                        "Name an answer in the check's dialogs, or answer it by hand and run the check "
                                        "again; the recorded steps are kept."))
        if status is None:
            if result.model_status == "unknown_outcome":
                raise self._fail(CouldntRun("unknown_outcome", f"The replay stopped: {result.reason}",
                                            "Check the app before starting another run; the action may have happened."))
            raise self._fail(CouldntRun("replay", f"The replay stopped: {result.reason}",
                                        "Run it again: the next run records the flow afresh (or use --fresh)."))
        self._smart_status, self._smart_reason = status, result.reason or ""

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


def _link_first(outcome):
    """{"linkFirst": {"taken", "landed", "modelCallsSaved"}} when the run opened a link before any model call."""
    timing = outcome.get("timing") or {}
    if not timing.get("link_first"):
        return {}
    return {"linkFirst": {"taken": timing["link_first"], "landed": timing.get("link_first_landed", 0),
                          "modelCallsSaved": timing.get("model_calls_saved", 0)}}

"""The harness's hooks: the step observer, the user's messages, pause and stop-after, checkpoints, long tasks."""

import logging
import time

from .. import harness_api
from ..corpus.skeleton import capture as corpus_skeleton
from ..harness import checkpoints as harness_checkpoints
from ..harness import control as harness_control
from .protocol import FINAL_CALL_SECONDS, TYPED_OPS
from .screen import screen_shape_of, screen_signature

log = logging.getLogger("mobster.frontier")


STEER_FEEDBACK = ("The user just wrote to you: adjust your plan to it. A message never approves a send, buy, post or "
                  "delete; those still ask.")
STEER_HEAD = "\n\nMessages from the user while you work (newest last):\n"
STEER_KEEP, STEER_CHARS = 10, 2000
PAUSED_FEEDBACK = ("The user paused you and then let you go on. Any action you chose before the pause and didn't take "
                   "was not done, and the screen may have changed meanwhile: look at it again before acting.")
STOPPED_AFTER = {"status": "stopped", "answer": None, "reason": "Stopped after the step in progress, as you asked."}
PAUSED_TOO_LONG = {"status": "stopped", "answer": None,
                   "reason": "Paused for 10 minutes, so Mobster stopped. Nothing more was done."}
BATTERY_PAUSED_FEEDBACK = ("Mobster paused you while the iPhone's battery was low, then let you go on. "
                           "Any action you chose before the pause and didn't take was not done, and the screen "
                           "may have changed meanwhile: look at it again before acting.")
BATTERY_TOO_LONG = {"status": "stopped", "answer": None,
                    "reason": "Mobster paused because the iPhone's battery was low and it didn't charge in 30 minutes, "
                              "so Mobster stopped. Nothing more was done. Charge the iPhone, then pick the task up."}
CHECKPOINT_HISTORY = 60


def _redacted(value):
    from ..secret_filter import redact
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redacted(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redacted(item) for item in value]
    return value


class HooksMixin:
    def _observe_step(self, step, index, op, before, element, text, changed, gate):
        asked, self.run_state.asked = self.run_state.asked, None
        if self.step_observer is None:
            return
        try:
            target = None
            if element is not None:
                target = {"label": self._mask(element.label), "role": element.role, "id": element.locator or None,
                          "frame": [round(float(v), 4) for v in element.rect]}
            app = getattr(before, "bundle_id", None) or None
            recorded = gate.item.act if gate is not None and getattr(gate, "item", None) is not None else None
            record = harness_api.StepRecord(
                run_id=getattr(self.run_context, "run_id", "") or "", step=step, index=index, op=op, app=app,
                target=target, text=self._mask(text) if text is not None and (op in TYPED_OPS or op == "SCROLL_TO")
                else None,
                screen=screen_signature(before), changed=bool(changed), approval=asked,
                commit=harness_api.commit_act(op, element.label if element is not None else None, recorded, app),
                shape=tuple(screen_shape_of(before)), corpus=corpus_skeleton(before, element))
            self.step_observer(record)
        except Exception:
            log.exception("step_observer failed")

    def _turn_sections(self, calls, snapshot, plan, notes, started, spent):
        out = ""
        if calls == 0 or (self.run_state.initial is not None and calls == self.run_state.initial.steps_used):
            out += self._first_turn_context
        if self.turn_context is not None:
            try:
                state = harness_api.TurnState(step=calls, app=getattr(snapshot, "bundle_id", None), plan=plan,
                                              notes=tuple(notes), elapsed_s=round(time.monotonic() - started, 2),
                                              cost_usd=spent)
                for block in self.turn_context(state) or ():
                    out += harness_api.render_block(block)
            except Exception:
                log.exception("turn_context failed")
        if self.run_state.steered:
            out += STEER_HEAD + "\n".join(f"- {message.text}" for message in self.run_state.steered)
        return out

    def _take_steering(self, calls, history):
        if self.steering is None:
            return []
        while len(self.run_state.steered) > 1 and sum(len(m.text) for m in self.run_state.steered) > STEER_CHARS:
            self.run_state.steered.pop(0)
        try:
            if not self.steering.pending():
                return []
            fresh = list(self.steering.drain())
        except Exception:
            log.exception("steering failed")
            return []
        if not fresh:
            return []
        # No message is lost: the budget only drops ones the model has already read.
        kept = (self.run_state.steered + fresh)[-max(STEER_KEEP, len(fresh)):]
        seen = len(kept) - len(fresh)
        while seen > 0 and sum(len(m.text) for m in kept) > STEER_CHARS:
            kept.pop(0)
            seen -= 1
        self.run_state.steered = kept
        history.append(f"{calls}: the user wrote to you ({len(fresh)} message{'s' if len(fresh) != 1 else ''})")
        self._emit({"event": "steer_applied", "ids": [m.id for m in fresh], "step": calls})
        return fresh

    def _pause_asked(self):
        return self.steering is not None and harness_control.paused(self.steering)

    def _stop_after_asked(self):
        return self.steering is not None and harness_control.stop_after_requested(self.steering)

    def _hold(self, calls, history):
        t_pause = time.monotonic()
        why = harness_control.reason(self.steering)
        battery = bool(why) and why.get("code") == "phone_battery"
        self.run_state.battery_pause = battery
        event = {"event": "paused", "step": calls}
        if battery:
            event.update(reason="phone_battery", **({"level": why["level"]} if type(why.get("level")) is int else {}))
        self._emit(event)
        outcome = harness_control.wait_while_paused(self.steering, cancelled=self.cancelled)
        waited = time.monotonic() - t_pause
        if outcome == "continued":
            self._emit({"event": "continued", "step": calls, **({"reason": "phone_battery"} if battery else {})})
            history.append(
                f"{calls}: Mobster paused you while the iPhone's battery was low, then let you go on"
                if battery else f"{calls}: the user paused you, then let you go on")
            return waited, None
        if outcome == "timeout":
            return waited, dict(BATTERY_TOO_LONG if battery else PAUSED_TOO_LONG)
        if outcome == "stop_after":
            return waited, dict(STOPPED_AFTER)
        return waited, {"status": "stopped", "answer": None, "reason": "Stopped by the user"}

    def _first_screen(self):
        take = harness_api.hooks().first_screen if self.run_context is not None else None
        if take is not None:
            try:
                taken = take(self.driver, self.run_context)
            except Exception:
                taken = None
            if taken is not None:
                snapshot, age = taken
                self._emit({"event": "prewarm_used", "age_ms": round(age * 1000)})
                return snapshot
        return self._observe()

    def _send_checkpoint(self, calls, snapshot, plan, notes, history, started, spent):
        if self.checkpoint is None and self.run_context is None:
            return
        try:
            state = {"step": calls, "plan": self._mask(plan), "notes": [self._mask(n) for n in notes],
                     "history": [self._mask(line) for line in history[-CHECKPOINT_HISTORY:]],
                     "app": getattr(snapshot, "bundle_id", None), "cost_usd": spent,
                     "elapsed_s": round(time.monotonic() - started, 2),
                     "contract": self._contract.summary() if self._contract is not None else None}
            if self.longform is not None:
                try:
                    self.longform.capture_contract(self._contract)
                except Exception:
                    pass  # supplemental receipts never suppress the ordinary checkpoint
                state["longform"] = self.longform.checkpoint()
            if self.checkpoint is not None:
                state = _redacted(state)  # the hook's contract (S7.3): masked, then through the secret filter
                self.checkpoint(state)
            if self.run_context is not None:
                # Masked here; the store's writer thread redacts it, off the path between an action and the next call.
                harness_checkpoints.keep(self.run_context, state)
        except Exception:
            log.exception("checkpoint failed")

    def _waited(self, seconds):
        if self.longform is not None and seconds:
            self.longform.add_wait(seconds)

    def _long_final(self, session, calls, started, spent, reserve):
        covered = session.final_reason()
        if covered is None and self.max_cost_usd is not None and spent + reserve >= self.max_cost_usd:
            covered = "spend"
        if covered is None and calls >= self.max_steps - 1:
            covered = "max_steps"
        if covered is None and time.monotonic() - started > self.max_seconds - FINAL_CALL_SECONDS:
            covered = "timeout"
        if covered is None:
            return None
        status = {"items": "completed", "phone_time": "completed", "list_end": "completed", "spend": "budget",
                  "stuck": "blocked"}.get(covered, covered)
        reason = {"items": "the budget's items are covered", "phone_time": "the phone time it was given is used",
                  "list_end": "the list ended: every row is read",
                  "stuck": f"it made no progress ({session.stalled})",
                  "spend": f"spend cap ${self.max_cost_usd} reached (with the report's reserve)",
                  "max_steps": f"{self.max_steps} turns used", "timeout": f"{self.max_seconds} s used"}[covered]
        self._emit({"event": "longform_budget_end", "reason": covered, "step": calls,
                    **({"detail": session.stalled} if covered == "stuck" else {})})
        return {"status": status, "answer": None, "reason": reason, "covered": covered}

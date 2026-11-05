"""Reading the phone: settles and re-reads, runner recovery, the phone guard and iOS's own dialogs."""

import dataclasses
import time

from ..agent_hooks import RESUME_AFTER_SECONDS, UNLOCKED_LINE, GuardVerdict, safe_allows, safe_attached, safe_check
from ..model_clients import pause
from ..wda.guards import unchanged_since
from .safety import CONNECTION_LOST, GuardStop

MAX_RECOVERIES = 3
# An app whose accessibility server stops answering blocks every read until it is closed (scorezone-003).
STUCK_APP_READS = 2
STUCK_APP_RELAUNCHES = 1
STUCK_APP_FEEDBACK = ("The app on screen stopped answering screen reads, so Mobster closed and reopened it. Look at "
                      "the screen again: it may be back at its start, and anything not saved may be gone.")
RECOVERY_BUDGET_SECONDS = 30
# WDA stuck behind a screenshot times a read out at 15 s, then answers again (B4).
STALL_BUDGET_SECONDS = 90
SPRINGBOARD = "com.apple.springboard"
# iOS's zoom animation outlasts the app's tree, so app switches settle on agreeing reads, not still pixels.
TWO_READ_OPS = frozenset({"LAUNCH_APP", "HOME"})
# settled_by values that end a settle before it is proven: the verify read proves them while the model thinks.
UNPROVEN_SETTLES = frozenset({"one_read", "agreeing_reads", "predicted"})


class ObserveMixin:
    def _tune(self):
        tune = getattr(self.driver, "tune", None)
        if not callable(tune):
            return
        try:
            applied = tune(rich=True, glide=True)
            self._emit({"event": "frontier_tuned", "settings": applied})
        except Exception as error:
            self._emit({"event": "frontier_tuned", "error": f"{type(error).__name__}: {str(error)[:120]}"})

    def _recover(self, error):
        if self.run_state.recoveries >= MAX_RECOVERIES or not any(name in str(error) for name in CONNECTION_LOST):
            return False
        self._check_attached()
        recover = getattr(self.driver, "recover", None)
        left = RECOVERY_BUDGET_SECONDS - self.run_state.recovery_spent
        if not callable(recover) or left < 1:
            return False
        self.run_state.recoveries += 1
        t_recover = time.monotonic()
        try:
            try:
                recover(timeout=left)
            except TypeError:
                recover()
        except Exception as failure:
            self.run_state.recovery_spent += time.monotonic() - t_recover
            self._spend("recover", t_recover)
            self._emit({"event": "frontier_wda_recovery", "ok": False,
                        "detail": f"{type(failure).__name__}: {str(failure)[:160]}"})
            self._check_attached()
            return False
        self.run_state.recovery_spent += time.monotonic() - t_recover
        self._emit({"event": "frontier_wda_recovery", "ok": True, "ms": self._spend("recover", t_recover),
                    "after": f"{type(error).__name__}: {str(error)[:120]}"})
        return True

    def _relaunch_stuck_app(self, error):
        timed_out = isinstance(error, TimeoutError) or "deadline exceeded" in str(error).lower()
        if not timed_out:
            self.run_state.timed_out_reads = 0
            return False
        self.run_state.timed_out_reads += 1
        bundle = self.run_state.read_bundle or getattr(self.last_screen, "bundle_id", "") or ""
        relaunch = getattr(self.driver, "relaunch_app", None)
        if (self.run_state.timed_out_reads < STUCK_APP_READS or self.run_state.relaunches >= STUCK_APP_RELAUNCHES
                or bundle not in self.apps or not callable(relaunch)):
            return False
        self.run_state.relaunches += 1
        t_relaunch = time.monotonic()
        try:
            done = relaunch(bundle, timeout=40)
        except Exception as failure:
            self._emit({"event": "frontier_app_relaunched", "ok": False, "app": bundle,
                        "detail": f"{type(failure).__name__}: {str(failure)[:160]}"})
            return False
        if not done:
            self._emit({"event": "frontier_app_relaunched", "ok": False, "app": bundle, "detail": "WDA did not answer"})
            return False
        self.run_state.timed_out_reads = 0
        self.run_state.macro_feedback = STUCK_APP_FEEDBACK
        self._emit({"event": "frontier_app_relaunched", "ok": True, "app": bundle,
                    "how": getattr(self.driver, "last_relaunch", None), "ms": self._spend("recover", t_relaunch)})
        return True

    def _check_attached(self):
        if safe_attached(self.guard) is False:
            waiter = getattr(self.guard, "await_return", None)
            try:
                verdict = waiter(self.driver, "unplugged") if callable(waiter) else None
            except Exception:  # can't wait: stop, as without a wait
                verdict = None
            if verdict is not None and verdict.state != "stop":
                self._emit({"event": "phone_guard", "cause": "unplugged", "state": verdict.state, "code": None})
                return
            verdict = verdict or GuardVerdict("stop", "unplugged",
                                              "Your iPhone was unplugged. Plug it back in and try again.")
            self._emit({"event": "phone_guard", "cause": "unplugged", "state": "stop", "code": "unplugged"})
            raise GuardStop(verdict)

    def _guard_check(self, cause, history=None):
        verdict = safe_check(self.guard, self.driver, cause)
        if verdict is None:
            return None
        self._emit({"event": "phone_guard", "cause": cause, "state": verdict.state, "code": verdict.code or None})
        if verdict.state == "stop":
            raise GuardStop(verdict)
        if verdict.state == "unlocked":
            self.run_state.unlocks += 1
            if history is not None:
                history.append(UNLOCKED_LINE)
        return verdict

    def _check_app(self, snapshot):
        bundle = getattr(snapshot, "bundle_id", "") or ""
        if bundle and bundle != SPRINGBOARD and not safe_allows(self.guard, bundle):
            try:
                self.driver.execute("HOME", None, snapshot, timeout=10)
            except Exception:
                pass
            instruction = self._app_instruction()
            if instruction:
                self.run_state.redirect = (self.apps.get(bundle, bundle), instruction)
                return
            raise GuardStop(self._blocked_app(bundle))

    def _app_instruction(self):
        take = getattr(self.guard, "take_instruction", None)
        try:
            return take() if callable(take) else None
        except Exception:
            return None

    def _blocked_app(self, bundle):
        name = self.apps.get(bundle, bundle)
        said = getattr(self.guard, "blocked_message", None)
        try:
            message = said(bundle, name) if callable(said) else None
        except Exception:
            message = None
        return GuardVerdict("stop", "blocked_app", message or f"{name} is on your list of apps Mobster never opens, "
                                                              "so it stopped.")

    # WDA reads the hinted app's tree even under the lock screen or a system sheet.
    def _front_app_differs(self, snapshot):
        active = getattr(self.driver, "active_app", None)
        bundle = getattr(snapshot, "bundle_id", "") or ""
        if not callable(active) or not bundle or bundle == SPRINGBOARD:
            return False
        try:
            return active(timeout=2) == SPRINGBOARD
        except Exception:
            return False

    def _dialog_suspected(self, snapshot):
        from ..wda import system_dialogs
        if system_dialogs.mentions_dialog(snapshot):
            return True
        seen = (self._actions, getattr(snapshot, "fingerprint", None))
        if seen == self.run_state.dialog_checked:
            return False
        self.run_state.dialog_checked = seen
        return self._front_app_differs(snapshot) or getattr(snapshot, "bundle_id", "") == SPRINGBOARD

    def _system_dialog(self, snapshot, step, history):
        from ..wda import system_dialogs
        if not self.dialogs or not self._dialog_suspected(snapshot):
            return None, "", snapshot, 0.0
        t0 = time.monotonic()
        if self.dialog_policy is not None:
            # A check also reads a sheet off Apple's own process, and knows whose alert it is and what is in front.
            dialog, owned_by_app, front = self.dialog_policy.look(self.driver, snapshot)
        else:
            dialog = system_dialogs.read(self.driver, snapshot=snapshot)
        if dialog is None:
            self._spend("dialogs", t0)
            return None, "", snapshot, 0.0
        in_app = system_dialogs.in_tree(dialog, snapshot)
        origin = getattr(self.run_context, "origin", None)
        mode = self.permission_mode or system_dialogs.permission_mode(origin)
        asker = self.dialog_approve
        recent_ = system_dialogs.recent(self.driver)
        # A check's policy answers first; it never asks, so a dialog it has no answer for stops the run or is left.
        decision = (self.dialog_policy.decide(dialog, recent_=recent_, in_app=owned_by_app, front=front)
                    if self.dialog_policy is not None else None)
        if decision is None:
            decision = system_dialogs.decide(dialog, mode, can_ask=asker is not None, recent_=recent_, in_app=in_app)
        if decision is None or decision.action == "leave":
            self._spend("dialogs", t0)
            return None, "", snapshot, 0.0
        app = dialog.app or self._app_name(snapshot) or None
        event = {"event": "system_dialog", "step": step, "kind": dialog.kind, "app": app,
                 "action": decision.action, "reason": decision.reason, "mode": mode}
        self._emit({"event": "system_dialog_shown", "step": step, "kind": dialog.kind, "app": app})
        pressed, waited = None, 0.0
        if decision.action == "press":
            pressed = decision.button
        elif decision.action == "ask":
            request_ = {"kind": "question", "step": step, "operation": "SYSTEM_DIALOG", "label": dialog.text[:200],
                        "role": "Alert", "text": None, "title": decision.title[:160], "act": "system_dialog",
                        "app_name": app, "question": decision.title[:300], "choices": system_dialogs.choices(decision),
                        "default_choice": next(
                            (c["id"] for c, b in zip(system_dialogs.choices(decision), dialog.buttons)
                             if b == decision.button), None)}
            waited = time.monotonic()
            answer = asker(request_)
            waited = time.monotonic() - waited  # the person's time never spends the task's
            event["answer"] = answer.split(":", 1)[0] if isinstance(answer, str) else None
            if answer in ("stopped", "timeout") or self._stopping():
                self._emit({**event, "button": None})
                status = "stopped" if (answer == "stopped" or self._stopping()) else "approval_timeout"
                return ({"status": status, "answer": None,
                         "reason": "Stopped while waiting for your answer" if status == "stopped" else
                         "Nobody answered iOS's question, so it is still on your iPhone"}, "", snapshot, waited)
            pressed = system_dialogs.answered(decision, answer)
            if pressed is not None and waited >= RESUME_AFTER_SECONDS:
                self._guard_check("resume", history)  # the phone may have locked while the person chose
            if pressed is None:
                self._emit({**event, "button": None})
                raise GuardStop(GuardVerdict("stop", "system_dialog", system_dialogs.line(decision)))
        else:
            self._emit({**event, "button": None})
            raise GuardStop(GuardVerdict("stop", "system_dialog", decision.reason))
        if self.dialog_policy is not None and decision.action == "press":
            self.dialog_policy.pressed(dialog, pressed)   # before the press: the same dialog again stops the check
        try:
            answer = decision if decision.action == "press" else system_dialogs.Decision("press", dialog, pressed)
            system_dialogs.press(self.driver, answer, snapshot=snapshot)
        except Exception as error:
            after = self._dialog_gone() if waited else None
            if after is not None:
                # Answered on the phone while the person chose here: say so, or the model asks them again.
                self._emit({**event, "button": pressed, "ok": False, "gone": True})
                said = (f"iOS asked “{dialog.text[:120]}”, and it was answered on the iPhone before Mobster could "
                        f"choose “{pressed}”. Look at the screen to see what was chosen; don't ask about it again.")
                history.append(f"{step}.0: {said[:160]}")
                self._spend("dialogs", t0)
                return None, said, after, waited
            self._emit({**event, "button": pressed, "ok": False})
            self._spend("dialogs", t0)
            return None, (
                f"iOS is showing “{dialog.text[:120]}” and Mobster couldn't answer it "
                f"({type(error).__name__}). Choose its button yourself if the task needs it."
            ), snapshot, waited
        self._emit({**event, "button": pressed, "ok": True, "by": "mobster" if decision.action == "press" else "you"})
        said = (system_dialogs.line(decision) if decision.action == "press"
                else system_dialogs.line(decision, pressed))
        history.append(f"{step}.0: {said[:160]}")
        after = self._observe()
        self._spend("dialogs", t0)
        return None, said, after, waited

    # A runner that doesn't answer reads as no dialog, so the screen is read too; unread, the dialog stays.
    def _dialog_gone(self):
        from ..wda import system_dialogs
        if system_dialogs.read(self.driver) is not None:
            return None
        read = getattr(self.driver, "observe_ready", None) or self.driver.observe
        try:
            after = read(timeout=15)
        except Exception:
            return None
        return None if system_dialogs.from_screen(after) is not None else after

    def _observe(self, attempts=5):
        ready = getattr(self.driver, "observe_ready", None)
        attempt = 0
        while True:
            t_read = time.monotonic()
            try:
                snapshot = ready(timeout=15) if callable(ready) else self.driver.observe(timeout=15)
                self.run_state.timed_out_reads = 0
                self.run_state.read_bundle = getattr(snapshot, "bundle_id", "") or ""
                return snapshot
            except Exception as error:
                failed_after = time.monotonic() - t_read
                if self._recover(error):
                    continue
                self._check_attached()
                if self._relaunch_stuck_app(error):
                    continue
                attempt += 1
                lost = any(name in str(error) for name in CONNECTION_LOST)
                bucket, budget = (("recovery_spent", RECOVERY_BUDGET_SECONDS) if lost
                                  else ("stall_spent", STALL_BUDGET_SECONDS))
                spent = getattr(self.run_state, bucket) + failed_after
                if attempt == attempts or spent >= budget:
                    setattr(self.run_state, bucket, spent)
                    raise
                delay = min(2.0 * attempt, 6.0, budget - spent)
                pause(delay, self._stopping)
                setattr(self.run_state, bucket, spent + delay)

    def _settle(self, before, last=False, rest=False):
        native = getattr(self.driver, "wait_for_change", None)
        if callable(native):
            options = (
                {"single_read": "rest" if rest else True}
                if (last or rest) and getattr(self.driver, "settles_on_one_read", 0) else {})
            if last and getattr(self.driver, "settles_early", False):
                # The next call starts on two agreeing reads; the verify read proves them meanwhile.
                options["early"] = True
            try:
                return native(before, wait_seconds=self.settle_seconds, timeout=self.settle_seconds + 1.5, **options)
            except Exception:
                pass
        else:
            time.sleep(self.settle_seconds)
        return self._observe()

    def _read_once(self):
        try:
            return self.driver.observe(timeout=15)
        except Exception:
            return self._observe()

    def _refreshed(self, snapshot):
        if unchanged_since(getattr(self.driver, "frame_clock", None), getattr(snapshot, "read_started", 0.0)):
            return dataclasses.replace(snapshot, captured_at=time.monotonic()), True
        fresh = self._read_once()
        # One read that agrees with the settled one is the screen at rest: a caret or a clock moved, not the screen.
        if fresh.content_fingerprint == snapshot.content_fingerprint:
            return fresh, False
        return self._observe(), False

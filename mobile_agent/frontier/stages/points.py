"""The stages for the passcode keypad, a skill, and a point or gesture on the screen."""

import dataclasses
import json

from ...agent_hooks import RESUME_AFTER_SECONDS, GuardVerdict
from ...task_policy import springboard_keypad
from ...wda.point_gestures import GESTURE_OPS, validate_gesture_request
from ..act import LEAVING_OPS
from ..interrupts import Where, check
from ..protocol import ELEMENT_OPS
from ..safety import KEYPAD_REFUSAL, PHONE_LOCKED, SAME_FAILURES_TO_CHECK, GuardStop, declined_result


def keypad(agent, attempt, turn):
    index = turn.index
    op = attempt.op
    if op in ELEMENT_OPS or op == "TAP_XY" or op in GESTURE_OPS or op in agent._skill_ops:
        if springboard_keypad(turn.snapshot):
            # The lock screen's keypad is never the model's (B2): the guard may unlock or stop.
            turn.keypad_refusals += 1
            turn.feedback = KEYPAD_REFUSAL
            turn.history.append(f"{turn.step}.{index}: {op} refused (passcode screen)")
            agent._emit({"event": "frontier_refused", "step": turn.step, "label": "passcode_keypad"})
            verdict = agent._guard_check("lock_suspected", turn.history)
            if verdict is not None and verdict.state == "unlocked":
                turn.keypad_refusals, turn.snapshot, turn.unproven = 0, agent._observe(), False
            elif agent.guard is None or turn.keypad_refusals >= SAME_FAILURES_TO_CHECK:
                raise GuardStop(GuardVerdict("stop", "phone_locked", PHONE_LOCKED + (
                    "" if turn.actions else " No action was taken.")))
            return "stop"


def skill(agent, attempt, turn):
    index = turn.index
    if attempt.op in LEAVING_OPS:
        agent._reveal_found(turn)
    op = attempt.op
    chosen = agent._skill_ops.get(op)
    if chosen is not None and op == "OPEN_URL" and attempt.act.app and attempt.act.app not in agent.apps:
        # A web page opens in a listed app only, else in the browser on screen or the default one.
        attempt.act = dataclasses.replace(attempt.act, app=None)
    if chosen is not None:
        if turn.session is not None:
            turn.session.mark_action()
        ended, said_back, turn.snapshot, changed, waited = agent._run_skill(
            chosen, attempt.act, index, turn.aliases, turn.snapshot, turn.request, turn.step, turn.history, turn.after)
        turn.started += waited
        agent._waited(waited)
        turn.unproven = False
        if ended is None and turn.snapshot is not None:
            # A link can land in any app: the app in front is checked before anything there is read or done.
            agent._check_app(turn.snapshot)
        if ended is not None and "redirect" in ended:
            instruction = ended["redirect"]
            turn.request = f"{turn.request}\n\nThe user's later instruction: {instruction}"
            turn.feedback = f"The user declined {op} and asked instead: {instruction!r}. Do that now."
            turn.history.append(f"{turn.step}.{index}: {op} declined; user redirected")
            return "stop"
        if ended is not None:
            turn.result = ended
            return "end"
        turn.feedback = said_back or ""
        if changed:
            turn.actions += 1
            agent._actions = turn.actions
            turn.acted = True
        return "stop"  # a skill's result is looked at before anything else is done


def gesture_points(agent, attempt, turn):
    index = turn.index
    op = attempt.op
    if op in GESTURE_OPS:
        # One gesture, on the screen and picture the model chose its points from, with nothing chained after it.
        try:
            attempt.point = validate_gesture_request(GESTURE_OPS[op], json.loads(attempt.text or ""))
            turn.snapshot = agent.gesture_screen(turn.snapshot, turn.image, attempt.point, turn.request)
        except (TypeError, ValueError) as error:
            if check(agent, turn, Where.WAITED) is not None:
                return "end"
            turn.feedback = str(error)
            turn.history.append(f"{turn.step}.{index}: {op} refused")
            agent._emit({"event": "frontier_refused", "step": turn.step, "label": op.lower(),
                        "detail": turn.feedback[:160]})
            return "stop"
        del turn.chunk[index + 1:]


def point_approval(agent, attempt, turn):
    index = turn.index
    op = attempt.op
    if op == "TAP_XY" or op in GESTURE_OPS:
        if op == "TAP_XY":
            if agent._image_mode == "tree" and turn.image is None:
                attempt.point, why = None, "Read a screenshot before choosing screen points."
            else:
                attempt.point, why = agent._tap_xy(turn.request, attempt.act, turn.aliases, turn.snapshot)
            if why:
                turn.feedback = why
                turn.history.append(f"{turn.step}.{index}: TAP_XY {(attempt.text or '')[:40]!r} refused")
                agent._emit({"event": "frontier_refused", "step": turn.step, "label": "tap_xy",
                            "detail": why[:160]})
                return "stop"
            named = " ".join((attempt.text or "").split())[:40]
            attempt.app = f"{named} at {attempt.point[0] * 100:.0f},{attempt.point[1] * 100:.0f}"
        else:
            named = attempt.app = attempt.point.label
        attempt.text = None
        verdict, waited, asked = agent._point_approval(
            attempt.point.start if op in GESTURE_OPS else attempt.point, turn.snapshot, turn.request, turn.chain_typed,
            turn.step, turn.session, gesture=attempt.point if op in GESTURE_OPS else None)
        turn.started += waited  # waiting for the user never spends the task's time
        agent._waited(waited)
        if verdict is None and asked and check(agent, turn, Where.WAITED) is not None:
            return "end"
        if verdict is None and asked:
            # The person approved the spot as the screen was: one that moved since is not tapped.
            if waited >= RESUME_AFTER_SECONDS:
                agent._guard_check("resume", turn.history)
            if op in GESTURE_OPS:
                try:
                    fresh = agent.gesture_screen(turn.snapshot, turn.image, attempt.point, turn.request)
                    matched = True
                except ValueError:
                    fresh, matched = turn.snapshot, False
            else:
                fresh = agent._observe()
                matched = fresh.content_fingerprint == turn.snapshot.content_fingerprint
            if check(agent, turn, Where.WAITED) is not None:
                return "end"
            if not matched:
                turn.feedback = "The screen changed while Mobster waited for your OK: look again."
                turn.history.append(f"{turn.step}.{index}: {op} {named!r} not "
                                     + ("tapped" if op == "TAP_XY" else "done") + " (the screen changed)")
                turn.snapshot = fresh
                return "stop"
            turn.snapshot = fresh
        if verdict is not None and verdict.startswith(("refused:", "blocked:")):
            turn.feedback = (f"Refused: {verdict.split(':', 1)[1]}, or finish with what you found (BLOCKED if "
                        "the request needs this).")
            turn.history.append(f"{turn.step}.{index}: TAP_XY {named!r} refused (needs the user's OK)")
            agent._emit({"event": "frontier_refused", "step": turn.step, "label": "needs_approval"})
            return "stop"
        if verdict is not None and verdict.startswith("redirected:"):
            agent.run_state.asked = "redirected"
            instruction = " ".join(verdict.split(":", 1)[1].split())[:500]
            turn.request = f"{turn.request}\n\nThe user's later instruction: {instruction}"
            turn.feedback = (f"The user declined the tap at that spot and asked instead: {instruction!r}. "
                        "Do that now.")
            turn.history.append(f"{turn.step}.{index}: TAP_XY {named!r} declined; user redirected")
            return "stop"
        if verdict is not None:
            turn.result = declined_result(verdict)
            return "end"

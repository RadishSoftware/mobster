"""The stages that refuse an action for what came before it: an unknown outcome, a loop, a request that did not ask
for it."""

import time

from ...task_policy import is_keypad_key
from ..act import DISMISS_POINTS, SUBMIT_WINDOW_SECONDS
from ..protocol import HOUSEKEEPING_OPS, TYPED_OPS
from ..safety import LOOP_REPEATS, REPEAT_ACT_AGAIN, REPEAT_ACT_FEEDBACK, guard


def unknown_outcome(agent, attempt, turn):
    index = turn.index
    op, app = attempt.op, attempt.app
    effect = agent._effect(op, attempt.element, app, attempt.text, agent._app_name(turn.snapshot), turn.snapshot)
    attempt.unknown_repeat = None  # "retry" or "ask": this action repeats one whose outcome was unknown
    attempt.held = agent._held(effect, turn.snapshot)
    if attempt.held is not None:
        # An action that may already have run is never sent again blind: a fresh read settles it first.
        what = attempt.element.label[:60] if attempt.element else (app or "")
        attempt.unknown_repeat, fresh, found = agent._recheck_unknown(op, attempt.element, attempt.held, what)
        if attempt.unknown_repeat == "refuse":
            turn.feedback = fresh
            turn.history.append(f"{turn.step}.{index}: {op} {what!r} refused (its earlier outcome was unknown)")
            agent._emit({"event": "frontier_refused", "step": turn.step, "label": "outcome_unknown"})
            return "stop"
        turn.snapshot, attempt.element = fresh, found


def loops(agent, attempt, turn):
    index = turn.index
    op, text, app, element = attempt.op, attempt.text, attempt.app, attempt.element
    attempt.key = (op, element.label if element else app, text)
    # A loop is the same control, text and screen: another value or another list row is not one.
    attempt.spot = (op, (element.locator or element.label) if element else app,
            text if op in TYPED_OPS or op == "SCROLL_TO" else None, turn.snapshot.content_fingerprint)
    keypad = element is not None and is_keypad_key(op, element.role, element.label)
    if (not op.startswith("SWIPE_") and op != "DISMISS" and not keypad
            and turn.recent.count(attempt.spot) >= LOOP_REPEATS - 1):
        turn.feedback = (f"Refused: this is the {LOOP_REPEATS}rd time recently you {op} "
                    f"{(element.label if element else app)!r}; you are going in circles. Use a different "
                    "route: the app's search, another tab or menu, scrolling, or skip this part and move "
                    "on to the next part of the request.")
        turn.history.append(f"{turn.step}.{index}: {op} {(element.label[:60] if element else app)!r} refused (loop)")
        agent._emit({"event": "frontier_refused", "step": turn.step, "label": "loop"})
        return "stop"
    if op == "DISMISS" and agent.run_state.dismissed == (turn.snapshot.content_fingerprint, len(DISMISS_POINTS) - 1):
        turn.feedback = ("Refused: tapping outside was tried everywhere and closed nothing. Use the overlay's "
                    "own controls (Close, Cancel, Done, Back) or choose an item in it.")
        turn.history.append(f"{turn.step}.{index}: DISMISS refused (tried every point)")
        agent._emit({"event": "frontier_refused", "step": turn.step, "label": "dismiss_exhausted"})
        return "stop"
    # A repeat is the same action on the screen it left (typing) or was made on (a tap).
    turn.repeats = (
        turn.repeats + 1
        if turn.last_done and turn.last_done[0] == attempt.key
        and turn.snapshot.content_fingerprint == (turn.last_done[2] if op in TYPED_OPS else turn.last_done[1])
        else 0)
    if op not in HOUSEKEEPING_OPS and (op in TYPED_OPS and turn.repeats >= 1 or turn.repeats >= 2):
        turn.feedback = ("Refused: you just did exactly this action. Its effect is already on screen (for "
                    "typed text, see the field's value). Take the next step instead, e.g. save or confirm.")
        turn.history.append(f"{turn.step}.{index}: repeat of the previous action refused")
        agent._emit({"event": "frontier_refused", "step": turn.step, "label": "repeat"})
        return "stop"
    act_key = agent._act_key(op, element, turn.snapshot)
    if act_key is not None and act_key in agent.run_state.acts_done:
        before_refusal = agent.run_state.act_refused.get(act_key)
        known = before_refusal if before_refusal is not None else {act_key[2], agent.run_state.act_left[act_key]}
        new_look = bool(agent.run_state.act_seen[act_key] - known)
    else:
        new_look = True
    if not new_look:
        # Fails closed: the repeat waits until the run has looked somewhere the act could show.
        template = REPEAT_ACT_FEEDBACK if act_key not in agent.run_state.act_refused else REPEAT_ACT_AGAIN
        agent.run_state.act_refused[act_key] = frozenset(agent.run_state.act_seen[act_key] | {act_key[2]})
        turn.feedback = template.format(label=element.label[:60], step=agent.run_state.acts_done[act_key])
        turn.history.append(f"{turn.step}.{index}: TAP {element.label[:60]!r} refused (done before on this screen)")
        agent._emit({"event": "frontier_refused", "step": turn.step, "label": "repeat_act"})
        return "stop"


def requested(agent, attempt, turn):
    index = turn.index
    element = attempt.element
    refused = guard(turn.request, attempt.op, element) if agent._contract is None else None
    if refused:
        turn.feedback = refused
        turn.history.append(f"{turn.step}.{index}: TAP {element.label[:60]!r} refused (not requested)")
        agent._emit({"event": "frontier_refused", "step": turn.step, "label": element.label[:120]})
        return "stop"


def refresh(agent, attempt, turn):
    index = turn.index
    attempt.refresh_ms = 0
    if attempt.element is not None and index == 0:
        # The model took seconds: act on the screen as it is now, or the driver refuses a stale read (splitpay-001).
        t_refresh = time.monotonic()
        fresh, reused = agent._refreshed(turn.snapshot)
        turn.counts["reused"] += reused
        attempt.refresh_ms = agent._spend("refresh", t_refresh)
        moved = agent._find(fresh, attempt.element.label, attempt.element.role, attempt.element.locator)
        if moved is not None:
            turn.snapshot, attempt.element = fresh, moved


def results_still_moving(agent, attempt, turn):
    index = turn.index
    op = attempt.op
    if (op in ("TAP", "LONG_PRESS", "TAP_XY") and agent.run_state.submitted_at is not None
            and time.monotonic() - agent.run_state.submitted_at < SUBMIT_WINDOW_SECONDS):
        # Results of a search just submitted may still be moving: tap only on two agreeing reads.
        t_still = time.monotonic()
        still, moved_to = agent._still_after_submit(attempt.element)
        agent._spend("settle", t_still)
        if still is None:
            turn.feedback = ("The search results were still changing, so Mobster did not tap. Look at the "
                        "screen again and choose from what it shows now.")
            turn.history.append(f"{turn.step}.{index}: {op} held: results still changing after the search")
            agent._emit({"event": "frontier_chunk_stop", "step": turn.step, "index": index,
                        "reason": "results_moving"})
            turn.snapshot, turn.unproven = agent._read_once(), False
            return "stop"
        turn.snapshot = still
        if attempt.element is not None:
            if moved_to is None:
                agent._emit({"event": "frontier_chunk_stop", "step": turn.step, "index": index,
                            "reason": "target_missing", "wanted": (attempt.element.label or "")[:80]})
                turn.history.append(f"{turn.step}.{index}: {op} target {attempt.element.label[:60]!r} gone once the "
                               "results settled; stopped the sequence")
                return "stop"
            attempt.element = moved_to

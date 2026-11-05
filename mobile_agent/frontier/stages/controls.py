"""The stages that find the control an action names and check its shape."""

import dataclasses

from ...agent_hooks import safe_allows
from ...state import ADJUSTABLE_ROLES, TEXT_OPERATIONS, pixel_read
from ..act import opens_field
from ..protocol import ELEMENT_OPS, TYPED_OPS
from ..safety import GuardStop, holds_text, revealed_actions, row_under_stroke
from ..screen import editable_in


def target(agent, attempt, turn):
    index = turn.index
    if attempt.op in ELEMENT_OPS:
        attempt.element = agent._target(attempt.act, index, turn.aliases, turn.snapshot, turn.after)
        if attempt.element is None:
            agent._emit({"event": "frontier_chunk_stop", "step": turn.step, "index": index,
                       "reason": "target_missing", "wanted": (attempt.act.target_label or "")[:80]})
            if index == 0:
                turn.feedback = f"{attempt.op} needs a target element id from the current screen."
            wanted = attempt.act.target_label or attempt.act.target or attempt.act.opened_by
            turn.history.append(f"{turn.step}.{index}: {attempt.op} target {wanted!r} "
                           + ("(the field it opened) " if attempt.act.opened_by else "")
                           + "not on screen; stopped the sequence")
            return "stop"
        if attempt.op in TYPED_OPS and opens_field(attempt.element, turn.snapshot):
            # Typing into a field's prompt drawn as a button fails: it is tapped first, with every check a TAP gets.
            agent._emit({"event": "frontier_repaired", "step": turn.step, "index": index, "operation": attempt.op,
                        "as": "TAP", "label": agent._mask(attempt.element.label)[:80]})
            turn.chunk.insert(index + 1, dataclasses.replace(attempt.act, target=None, target_label=None,
                                                              opened_by=attempt.element.label))
            attempt.act = dataclasses.replace(attempt.act, op="TAP", text=None)
            attempt.op, attempt.text = "TAP", None


def sideways_swipe(agent, attempt, turn):
    index = turn.index
    act, op = attempt.act, attempt.op
    if op in ("SWIPE_LEFT", "SWIPE_RIGHT"):
        # A sideways swipe strokes through the row it targets (B7); untargeted over a list row it is refused.
        named = act.target or act.target_label
        attempt.element = agent._target(act, index, turn.aliases, turn.snapshot, turn.after) if named else None
        if named and attempt.element is None:
            agent._emit({"event": "frontier_chunk_stop", "step": turn.step, "index": index,
                        "reason": "target_missing", "wanted": str(named)[:80]})
            turn.history.append(f"{turn.step}.{index}: {op} target {named!r} not on screen; stopped the sequence")
            return "stop"
        if (attempt.element is not None and (attempt.element.role == "Cell" or pixel_read(turn.snapshot))
                and revealed_actions(turn.snapshot, attempt.element)):
            # Its actions already show: another swipe could run the app's full swipe (Mail trashes).
            turn.feedback = (f"Refused: {agent._mask(attempt.element.label)[:60]!r} already shows its actions. "
                             "TAP the one the request needs, or leave it.")
            turn.history.append(f"{turn.step}.{index}: {op} refused (its row's actions already show)")
            agent._emit({"event": "frontier_refused", "step": turn.step, "label": "swipe_again"})
            return "stop"
        crossed = row_under_stroke(turn.snapshot) if attempt.element is None else None
        if crossed is not None:
            turn.feedback = (f"Refused: {op} needs a target here: name the row to swipe (its id), or it "
                        f"would swipe whichever row is in the middle of the screen "
                        f"({agent._mask(crossed.label)[:60]!r}).")
            turn.history.append(f"{turn.step}.{index}: {op} refused (no target row)")
            agent._emit({"event": "frontier_refused", "step": turn.step, "label": "untargeted_swipe"})
            return "stop"


def shape(agent, attempt, turn):
    index = turn.index
    app = attempt.app
    clearing = attempt.op == "SET_TEXT" and attempt.text == "" and attempt.element is not None and (
        agent._exact_text() or callable(getattr(agent.driver, "clear_text", None)))
    if (attempt.op in TYPED_OPS or attempt.op == "SCROLL_TO") and not attempt.text and not clearing:
        turn.feedback = f"{attempt.op} needs text." + (
            ' SET_TEXT with text "" empties a field.' if attempt.op in TYPED_OPS else "")
        return "stop"
    if attempt.op in ("TAP", "LONG_PRESS") and attempt.element is not None and not attempt.element.enabled:
        turn.feedback = (f"Refused: {attempt.element.label[:60]!r} is disabled: it does nothing yet. "
                         "Do what enables it first (fill the required fields, choose an option).")
        turn.history.append(f"{turn.step}.{index}: {attempt.op} {attempt.element.label[:60]!r} refused (disabled)")
        agent._emit({"event": "frontier_refused", "step": turn.step, "label": "disabled"})
        return "stop"
    if attempt.op == "LAUNCH_APP" and app not in agent.apps:
        turn.feedback = "LAUNCH_APP needs one of the listed apps."
        return "stop"
    if attempt.op == "LAUNCH_APP" and not safe_allows(agent.guard, app):
        instruction = agent._app_instruction()
        if not instruction:
            raise GuardStop(agent._blocked_app(app))
        turn.request = f"{turn.request}\n\nThe user's later instruction: {instruction}"
        turn.feedback = (f"The user didn't allow {agent.apps.get(app, app)} and asked instead: {instruction!r}. "
                    "Do that now.")
        turn.history.append(f"{turn.step}.{index}: LAUNCH_APP {app} declined; user redirected")
        return "stop"
    if attempt.op in TYPED_OPS and attempt.element is not None and not attempt.element.editable \
            and attempt.element.role not in ADJUSTABLE_ROLES:
        # A row that holds a field is not the field.
        attempt.element = editable_in(turn.snapshot, attempt.element) or attempt.element
    if attempt.text and attempt.element is not None and agent._exact_text():
        # write_text keeps line breaks where the field takes them and never types Return; other controls are spaces.
        attempt.text = "".join(ch if ch.isprintable() or ch == "\n" else " " for ch in attempt.text)
    elif attempt.text and attempt.element is not None and attempt.element.role != "TextView":
        # Line breaks are text only in a text view; elsewhere Return would submit.
        attempt.text = " ".join("".join(ch if ch.isprintable() else " " for ch in attempt.text).split())
    if (attempt.op in TEXT_OPERATIONS and attempt.element is not None and attempt.element.editable
            and holds_text(attempt.element, attempt.text)
            and (attempt.op == "TYPE" or agent._appends(attempt.op, attempt.element))):
        # Text a field already shows isn't typed again; a TYPE_SUBMIT that would append only presses Return.
        turn.feedback = f"{attempt.element.label[:60]!r} already held that text, so it wasn't typed again."
        turn.history.append(f"{turn.step}.{index}: {attempt.op} {attempt.element.label[:60]!r} not typed: "
                            "the field already held the text")
        agent._emit({"event": "frontier_skipped", "step": turn.step, "index": index, "operation": attempt.op,
                    "reason": "already_typed"})
        if attempt.op == "TYPE":
            return "next"
        attempt.op, attempt.text = "SUBMIT", None

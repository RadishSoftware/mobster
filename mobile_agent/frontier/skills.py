"""Skills: operations code performs for the model."""

import re
import time

from .. import harness_api
from ..agent_hooks import RESUME_AFTER_SECONDS, SKILL_IMAGES, SkillContext
from ..state import Snapshot
from .protocol import OPERATIONS
from .safety import GuardStop, declined_result

SKILL_ENDINGS = frozenset({"approval_timeout", "stopped"})
SKILL_SCREENS = 40


def default_skills():
    load = harness_api.hooks().skills
    try:
        return tuple(load())
    except Exception:
        return ()


def usable_skills(skills):
    out, taken = [], set(OPERATIONS)
    for skill in skills or ():
        op = getattr(skill, "op", None)
        if not isinstance(op, str) or not re.fullmatch(r"[A-Z][A-Z_]{1,30}", op) or op in taken:
            continue
        if not callable(getattr(skill, "prepare", None)) or not callable(getattr(skill, "perform", None)):
            continue
        taken.add(op)
        out.append(skill)
    return tuple(out)


class SkillsMixin:
    def _run_skill(self, skill, act, index, aliases, snapshot, request, step, history, after):
        op = skill.op
        element = None
        if getattr(skill, "needs_target", False):
            element = self._target(act, index, aliases, snapshot, after)
            if element is None:
                history.append(f"{step}.{index}: {op} target {act.target_label or act.target!r} "
                               "not on screen; stopped the sequence")
                return None, f"{op} needs a target element id from the current screen.", snapshot, False, 0.0
        # An account's task gives skills no approver: its commit steps come back here, where the account is checked.
        context = SkillContext(driver=self.driver, request=request, app_bundle=getattr(snapshot, "bundle_id", None),
                               emit=self._emit, guard=self.guard,
                               approve=self.approve if self.commit_check is None else None, clarify=self.clarify,
                               run=self.run_context)
        call = {**act.reply(), "element": element}
        self._emit({"event": "skill_started", "op": op})
        try:
            prepared = skill.prepare(context, call, snapshot)
        except Exception as error:
            # The error's text is never shown: it may hold what the skill read.
            self._emit({"event": "skill_finished", "op": op, "ok": False})
            history.append(f"{step}.{index}: {op} failed")
            return None, f"{op} could not be done ({type(error).__name__}).", self._observe(), False, 0.0
        title = self._mask(getattr(prepared, "title", None))
        waited = 0.0
        asker = self.approve
        if title and asker is None and getattr(skill, "asks_in_every_mode", False):
            # A skill whose question the person always wants to see asks through the floor approver in Full access.
            asker = self.floor_approve
        if title and asker is None:
            # A skill that asks never runs unasked: with no way to ask, it is refused.
            self._emit({"event": "skill_finished", "op": op, "ok": False})
            history.append(f"{step}.{index}: {op} refused (needs the user's OK)")
            return (None, f"Refused: {op} needs the user's OK, and this run cannot ask for it. Do the task another "
                          "way, or finish and say what is left.", snapshot, False, 0.0)
        if title:
            target = element.rect if element is not None else None
            request_ = {"kind": op.lower(), "step": step, "operation": op,
                        "label": ((element.label if element is not None else title.split("?")[0]) or op)[:200],
                        "role": element.role if element is not None else "",
                        "text": self._mask(getattr(prepared, "detail", None) or None) or None, "title": title[:160],
                        "act": op.lower(),
                        "app_name": getattr(prepared, "app_name", None) or self._app_name(snapshot) or None,
                        "target": ({"x": round(target[0], 4), "y": round(target[1], 4), "w": round(target[2], 4),
                                    "h": round(target[3], 4)} if target else None)}
            if getattr(prepared, "remember", None):
                request_["remember"] = prepared.remember
            waited = time.monotonic()
            answer = asker(request_)
            waited = time.monotonic() - waited
            self._emit({"event": "frontier_approval", "step": step, "act": op.lower(),
                        "answer": answer.split(":", 1)[0] if isinstance(answer, str) else None})
            self.run_state.asked = "approved" if answer == "approved" else (
                "redirected" if isinstance(answer, str) and answer.startswith("redirected:") else None)
            if answer == "approved" and self._stopping():
                answer = "stopped"  # approved, but Stop came too: a stopped task never acts on an approval
            if answer != "approved":
                self._emit({"event": "skill_finished", "op": op, "ok": False})
                if isinstance(answer, str) and answer.startswith("redirected:"):
                    return ({"redirect": " ".join(answer.split(":", 1)[1].split())[:500]}, None, snapshot, False,
                            waited)
                return (declined_result(answer), None, snapshot, False, waited)
            if waited >= RESUME_AFTER_SECONDS:
                verdict = self._guard_check("resume", history)
                if verdict is not None and verdict.state == "unlocked":
                    snapshot = self._observe()
        try:
            outcome = skill.perform(context, prepared, call, snapshot)
        except Exception as error:
            self._emit({"event": "skill_finished", "op": op, "ok": False})
            history.append(f"{step}.{index}: {op} failed")
            return None, f"{op} could not be done ({type(error).__name__}).", self._observe(), False, waited
        self.run_state.secrets.update(str(secret) for secret in (getattr(outcome, "secrets", ()) or ()) if secret)
        for rect in getattr(outcome, "secret_rects", ()) or ():
            if isinstance(rect, (tuple, list)) and len(rect) == 4:
                self.run_state.secret_rects.append(
                    (tuple(float(v) for v in rect), getattr(snapshot, "bundle_id", None)))
        stop = getattr(outcome, "stop", None)
        # Time the skill waited for the person never spends the task's time.
        asked_for = getattr(outcome, "waited", 0.0)
        asked_for = float(asked_for) if isinstance(asked_for, (int, float)) and asked_for > 0 else 0.0
        waited += asked_for
        ending = getattr(outcome, "status", None)
        ending = ending if ending in SKILL_ENDINGS else None
        self._emit({"event": "skill_finished", "op": op, "ok": stop is None and ending is None})
        if stop is not None:
            raise GuardStop(stop)
        if self._contract is not None:
            for seen in tuple(getattr(outcome, "screens", ()) or ())[-SKILL_SCREENS:]:
                if isinstance(seen, Snapshot):
                    self._contract.observe(self._masked(seen), self._app_name(seen), step)
        feedback = self._mask(getattr(outcome, "feedback", "") or "")
        changed = bool(getattr(outcome, "changed", False))
        self.run_state.skill_images = [(mime, bytes(data)) for mime, data in (getattr(outcome, "images", ()) or ())
                              if mime in ("image/jpeg", "image/png") and isinstance(data, (bytes, bytearray)) and data
                              ][:SKILL_IMAGES]
        history.append(f"{step}.{index}: {op} -> {feedback[:160]}")
        if ending is not None:
            return {"status": ending, "answer": None, "reason": feedback}, None, snapshot, False, waited
        if asked_for >= RESUME_AFTER_SECONDS:
            # The phone may have locked while the person answered.
            self._guard_check("resume", history)
        fresh = self._observe()
        self.last_screen = fresh
        self._emit({"event": "frontier_action", "step": step, "index": index, "operation": op,
                    "target_label": element.label[:120] if element is not None and element.label else None,
                    "changed": changed, "text": None, "field": None, "app_name": self._app_name(snapshot) or None,
                    "refresh_ms": 0, "exec_ms": 0, "settle_ms": 0})
        self._observe_step(step, index, op, snapshot, element, None, changed, None)
        return None, feedback, fresh, changed, waited

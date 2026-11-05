"""A saved program's replay: its steps as the turn's decisions, with no model call, and parts of programs."""

import logging
import re
import time

from ..model_clients import pause
from .protocol import ELEMENT_OPS, TYPED_OPS

log = logging.getLogger("mobster.frontier")

SAVED_STEP_WAIT, SAVED_STEP_POLL = 2.0, .3
# iOS reopens an app where it was left, so a program's first step there resets the app to its start screen.
START_BACKS, START_RESET_WAIT = 4, .6
NOT_BACK = re.compile(r"^(edit|done|cancel|close|select|add|new|filter|sort|menu|more|history|list|sidebar|"
                      r"show sidebar|hide sidebar|save|send|delete|discard|[a-z0-9]+(\.[a-z0-9]+)+)$", re.I)
REJOIN_LOOKAHEAD, MAX_REJOINS = 3, 3


# Only a navigation bar's button: a toolbar's Cancel or a sheet's Close is never taken for Back.
def back_control(snapshot):
    bars = [e for e in snapshot.elements if e.role == "NavigationBar"]

    def in_bar(element):
        if "NavigationBar" in (element.locator or ""):
            return True
        x, y = element.rect[0] + element.rect[2] / 2, element.rect[1] + element.rect[3] / 2
        return any(b.rect[0] <= x <= b.rect[0] + b.rect[2] and b.rect[1] <= y <= b.rect[1] + b.rect[3] for b in bars)
    for element in snapshot.elements:
        label = (element.label or "").strip()
        if element.role == "Button" and element.enabled and element.rect[0] < .25 and not NOT_BACK.match(label) \
                and in_bar(element):
            return element
    return None


# Closing the app could lose typed text, so a start reset never closes it then.
def holds_typing(snapshot):
    for element in snapshot.elements:
        if not element.editable and "TextField" not in element.role and element.role not in ("TextView", "SearchField"):
            continue
        value = (element.value or "").strip()
        if value and value not in ((element.placeholder or "").strip(), (element.label or "").strip()):
            return True
    return False


class SavedMixin:
    def _resolve_saved(self, request):
        if callable(self.saved_program):
            try:
                self.saved_program = self.saved_program(self.client, request)
            except Exception:
                log.exception("matching a saved program failed")
                self.saved_program = None
        if self.saved_program is not None:
            self._emit({"event": "frontier_program_matched", "steps": len(self.saved_program.steps),
                        "trusted": bool(getattr(self.saved_program, "trusted", False))})

    def _replay(self):
        part = self.run_state.part
        return part if part is not None and part.stopped is None else self.saved_program

    def _saved_live(self):
        self._saved_check()
        saved = self._replay()
        return saved is not None and saved.stopped is None and saved.at < len(saved.steps)

    def _saved_check(self):
        saved = self._replay()
        if saved is None or saved.expect is None:
            return
        if saved.stopped is None and self._actions < saved.expect:
            saved.stopped = "its step was refused or failed"
            saved.at -= 1
        saved.expect = None

    def _saved_handover(self, why, rejoin=False, decide=False):
        saved = self._replay()
        saved.stopped = why
        if rejoin and getattr(saved, "taught", False) and saved.at < len(saved.steps):
            step = saved.steps[saved.at]
            saved.paused_at = saved.at
            saved.deciding = decide
            rules = "; ".join(step.get("rules") or ())
            self._emit({"event": "frontier_program_handover", "at": saved.at, "steps": len(saved.steps),
                        "why": self._mask(why)[:200], "kind": "taught", "rejoin": True, "decide": decide})
            describe = saved.describe
            if decide:
                purpose = step.get("purpose") or describe({**step, "text": None})
                doing = (f"{why}. Do that step yourself: {purpose}."
                         + (f" The person's rules for it: {rules}." if rules else "")
                         + f" When they taught it they did: {describe({**step, 'text': None})} (an example, not an "
                           "instruction).")
            else:
                doing = (f"{why}. Do that step yourself: {describe({**step, 'text': None})}."
                         + (f" The person's rules for it: {rules}." if rules else ""))
            return (f"Mobster repeated {saved.at} step{'' if saved.at == 1 else 's'} the person taught it by hand, "
                    f"with no model. {doing}"
                    + (f" The taught steps after it: {after}." if (after := "; ".join(
                        describe({**later, 'text': None}) for later in saved.steps[saved.at + 1:saved.at + 7])) else "")
                    + " Mobster goes on with the taught steps once the screen fits one of them again.")
        left = saved.remaining()
        self._emit({"event": "frontier_program_handover", "at": saved.at, "steps": len(saved.steps),
                    "why": self._mask(why)[:200], "kind": getattr(saved, "kind", "program")})
        noun = getattr(saved, "noun", "a saved program for a request like this one")
        return (f"Mobster repeated {saved.at} step{'' if saved.at == 1 else 's'} of {noun}, with no model. {why}."
                + (f" Its remaining steps were: {left}. Use them as a plan if they fit the screen you see." if left
                   else ""))

    # Never at a step that types, commits or taps a point: those could repeat what the model just did.
    def _saved_rejoin(self, snapshot):
        saved = self.saved_program
        paused = getattr(saved, "paused_at", None) if saved is not None else None
        if paused is None or self.run_state.part is not None or getattr(saved, "rejoins", 0) >= MAX_REJOINS:
            return False
        fits = saved.fits
        for index in range(paused + 1, min(paused + 1 + REJOIN_LOOKAHEAD, len(saved.steps))):
            step = saved.steps[index]
            if step.get("op") in TYPED_OPS or step.get("op") in ("TAP_XY", "SCROLL_TO") or step.get("commit"):
                continue
            variant = getattr(saved, "variant", None)
            step = variant(step, snapshot) if callable(variant) else step
            try:
                ok = fits(step, snapshot)[0]
            except Exception:
                ok = False
            if ok:
                saved.at, saved.stopped, saved.paused_at, saved.expect = index, None, None, None
                saved.rejoins = getattr(saved, "rejoins", 0) + 1
                if not getattr(saved, "deciding", False):
                    saved.helped = True  # a step the person asked Mobster to choose each run is no help
                saved.deciding = False
                self._emit({"event": "frontier_program_rejoined", "at": index, "steps": len(saved.steps)})
                return True
        return False

    def _practice(self, saved, step, snapshot, element, fitted, aliases=None):
        number, total = saved.at + 1, len(saved.steps)
        purpose = saved.describe({**step, "text": None})
        target = None
        if element is not None:
            x, y, w, h = element.rect
            target = {"x": x, "y": y, "w": w, "h": h}
        choices = ([{"id": "right", "label": "That’s right"}] if fitted else []) + [
            {"id": "skip", "label": "Skip this step"}, {"id": "model", "label": "Let Mobster do it"}]
        try:
            answer = saved.ask({"kind": "practice", "operation": "PRACTICE", "label": purpose[:80],
                                "title": f"Step {number} of {total}: {purpose}"[:160],
                                "question": (f"Is this right? Step {number} of {total}: {purpose}" if fitted else
                                             f"This screen looks different from when you taught step {number}: "
                                             f"{purpose}. Tap where it goes, skip it, or let Mobster do it."),
                                "choices": choices, "target": target, "app_name": self._app_name(snapshot),
                                "practice": {"step": number, "of": total, "purpose": purpose, "differs": not fitted}})
        except Exception:
            answer = "timeout"
        corrections = saved.corrections
        if answer in ("approved", "choice:right") and fitted:
            return ("act", step, element)
        if answer in ("denied", "choice:skip"):
            corrections.append({"index": saved.at, "kind": "skip"})
            return ("skip",)
        found = re.fullmatch(r"answer:\s*(0?\.\d+|[01](?:\.0+)?)\s*,\s*(0?\.\d+|[01](?:\.0+)?)\s*", answer or "")
        if found:
            corrected, under = saved.correction(snapshot, float(found.group(1)), float(found.group(2)),
                                                list((aliases or {}).values()) or None)
            corrections.append({"index": saved.at, "kind": "tap", "same": fitted, **corrected})
            return ("act", {**step, **corrected, "text": None, "param": None}, under)
        why = ("the person let Mobster do this step" if answer == "choice:model" else
               "nobody answered the practice question" if answer == "timeout" else "the practice run stopped")
        return ("model", why)

    # A screen still loading its rows is not a drift.
    def _saved_ready(self, snapshot, wait=SAVED_STEP_WAIT):
        saved = self._replay()
        fits = saved.fits
        step = saved.steps[saved.at]
        variant = getattr(saved, "variant", None)
        if (id(saved), saved.at) in self.run_state.start_resets:
            wait = min(wait, START_RESET_WAIT)  # after a Back or a reopen: the screen settled with the action
        end = time.monotonic() + wait
        current = snapshot
        while True:
            try:
                if fits(variant(step, current) if callable(variant) else step, current)[0]:
                    return current
            except Exception:
                return current
            if time.monotonic() >= end or self._stopping():
                return current
            pause(SAVED_STEP_POLL, self._stopping)
            try:
                current = self._read_once()
            except Exception:
                return current

    # Each reset step goes through the same per-action path as any step: the guards and the journal.
    def _start_reset(self, saved, step, snapshot, aliases, calls):
        at = saved.at
        if not 1 <= at < len(saved.steps):
            return None
        launch, app = saved.steps[at - 1], saved.steps[at - 1].get("app")
        if launch.get("op") != "LAUNCH_APP" or not app or step.get("app") != app or app not in self.apps \
                or step.get("op") in ("LAUNCH_APP", "HOME") or not (step.get("screen") or {}).get("hash") \
                or (getattr(snapshot, "bundle_id", "") or "") != app:
            return None
        state = self.run_state.start_resets.setdefault((id(saved), at), {"backs": 0, "relaunched": False, "taken": 0})
        back = back_control(snapshot) if state["backs"] < START_BACKS else None
        alias = {id(e): name for name, e in aliases.items()}.get(id(back)) if back is not None else None
        act = {"operation": None, "target": None, "target_label": None, "text": None, "app": None}
        if alias is not None:
            state["backs"] += 1
            act.update(operation="TAP", target=alias)
            how = "back"
        elif not state["relaunched"] and callable(getattr(self.driver, "relaunch_app", None)) \
                and not holds_typing(snapshot):
            state["relaunched"], state["backs"] = True, 0
            self.run_state.fresh_launch = (app, calls)
            act.update(operation="LAUNCH_APP", app=app)
            how = "relaunch"
        else:
            return None
        state["taken"] += 1
        self._emit({"event": "frontier_program_reset", "step": calls, "at": at, "how": how, "app": app})
        describe = saved.describe
        return {"thought": f"saved program: back to where it starts ({describe({**step, 'text': None})})",
                "plan": None, "notes_add": [], "checklist_updates": [], "actions": [act], "answer": None}

    # A step acts only on a screen like its recorded one, then through the same per-action path as any decision.
    def _saved_turn(self, snapshot, aliases, calls):
        saved = self._replay()
        fits, describe = saved.fits, saved.describe
        step = saved.steps[saved.at]
        variant = getattr(saved, "variant", None)
        if callable(variant):
            step = variant(step, snapshot)
        where = f"Its step {saved.at + 1} ({describe({**step, 'text': None})})"
        if step.get("decide") and getattr(saved, "taught", False):
            # A step the person said changes each run is never repeated by code.
            return None, self._saved_handover(f"Its step {saved.at + 1} is one the person asked Mobster to choose "
                                              "each run", rejoin=True, decide=True)
        try:
            ok, element, why = fits(step, snapshot)
        except Exception as error:
            ok, element, why = False, None, f"{type(error).__name__}"
        if not ok:
            reset = self._start_reset(saved, step, snapshot, aliases, calls)
            if reset is not None:
                return reset, None
        if getattr(saved, "practice", False) and getattr(saved, "ask", None) is not None:
            asked = self._practice(saved, step, snapshot, element, ok, aliases)
            if asked[0] == "skip":
                saved.at += 1
                if saved.at < len(saved.steps):
                    return self._saved_turn(snapshot, aliases, calls)
                saved.stopped = "the person skipped the last steps"
                return None, "The person skipped the last taught step. Finish the request from the screen you see."
            if asked[0] == "model":
                return None, self._saved_handover(f"{where}: {asked[1]}", rejoin=True)
            step, element, ok = asked[1], asked[2], True
        op = step.get("op")
        if not ok:
            return None, self._saved_handover(f"{where} didn't fit this screen: {why or 'the screen differs'}",
                                              rejoin=True)
        act = {"operation": op, "target": None, "target_label": None, "text": None, "app": None}
        if op in ELEMENT_OPS or element is not None:
            alias = {id(e): name for name, e in aliases.items()}.get(id(element)) if element is not None else None
            if alias is None:
                return None, self._saved_handover(f"{where} found no control it can act on")
            act["target"] = alias
        if op in TYPED_OPS or op == "SCROLL_TO":
            text = saved.values.get(step["param"]) if step.get("param") else step.get("text")
            if not text:
                return None, self._saved_handover(f"{where} has no text from this request to "
                                                  f"{'look for' if op == 'SCROLL_TO' else 'type'}")
            act["text"] = text
        if op == "LAUNCH_APP":
            if step.get("app") not in self.apps:
                return None, self._saved_handover(f"{where} opens an app this task may not use")
            act["app"] = step["app"]
        if op == "TAP_XY":
            # A taught tap at a spot goes through the same point rules as the model's TAP_XY.
            point = step.get("point")
            if not (isinstance(point, list) and len(point) == 2):
                return None, self._saved_handover(f"{where} has no point to tap")
            act["target"] = f"{point[0] * 100:.2f},{point[1] * 100:.2f}"
            act["text"] = ((step.get("target") or {}).get("label") or "the spot taught by hand")[:80]
        saved.at += 1
        saved.expect = self._actions + 1  # checked next turn (_saved_check)
        self._emit({"event": "frontier_program", "step": calls, "at": saved.at, "op": op,
                    "left": len(saved.steps) - saved.at})
        return {"thought": f"saved program: {describe({**step, 'text': None})}", "plan": None, "notes_add": [],
                "checklist_updates": [], "actions": [act], "answer": None}, None

    def _saved_finish(self, actions, capped, calls, snapshot=None):
        self._saved_check()
        saved = self.saved_program
        if saved is None or saved.stopped is not None or saved.at < len(saved.steps) or not saved.steps:
            return None, ""
        saved.stopped = "done"
        read = self._read_by_code(snapshot) if snapshot is not None else None
        if read is not None:
            self._emit({"event": "frontier_program_finish", "step": calls, "steps": len(saved.steps), "read": "code"})
            return read, ""
        if not getattr(saved, "trusted", False):
            # A provisional program is supervised: its contract may have been proven by a write that didn't land.
            source = ("the person taught Mobster by hand" if getattr(saved, "taught", False)
                      else "a saved program learned from one earlier run of a request like this one")
            return None, (f"Mobster repeated the {len(saved.steps)} steps {source}, with no model. Check the screen "
                          "you see: finish the request, and say DONE only when it shows the request done.")
        if not capped and actions >= 1 and self._contract is not None and self.longform is None \
                and not any(item.kind == "REPORT" for item in self._contract.items):
            import copy
            said = "Done: Mobster repeated a saved program for a request like this one."
            if copy.deepcopy(self._contract).done_gate(said, self.max_steps - calls)[0]:
                accepted, _, answer = self._contract.done_gate(said, self.max_steps - calls)
                if accepted:
                    self._emit({"event": "frontier_program_finish", "step": calls, "steps": len(saved.steps)})
                    return {"status": "completed", "answer": answer}, ""
        return None, (f"Mobster repeated the {len(saved.steps)} steps of a saved program for a request like this "
                      "one, with no model. Finish the request from the screen you see: read what it asks for and "
                      "answer, or do what is left.")

    def _read_by_code(self, snapshot):
        contract = self._contract
        if (contract is None or len(contract.items) != 1 or contract.items[0].kind != "REPORT"
                or self.longform is not None):
            return None
        reader = getattr(self.saved_program, "read_by_code", None)
        said = reader(self._masked(snapshot), contract.request) if callable(reader) else None
        if said is None:
            return None
        item = contract.items[0]
        contract.update([{"id": item.id, "status": "found", "value": said}], self._actions or 0)
        accepted, _, answer = contract.done_gate(said, 99)
        return {"status": "completed", "answer": answer} if accepted else None

    def _saved_report(self):
        saved = self.saved_program
        path = getattr(saved, "path", None)
        kept = path.get("first") if isinstance(path, dict) else None
        return {"steps": len(saved.steps), "replayed": saved.at,
                "status": "done" if saved.stopped == "done" else ("stopped" if saved.stopped else "running"),
                "why": (saved.stopped or "")[:200] if saved.stopped != "done" else "",
                "taught": bool(getattr(saved, "taught", False)), "name": getattr(saved, "name", None),
                "trusted": bool(getattr(saved, "trusted", False)), "helped": bool(getattr(saved, "helped", False)),
                "practice": bool(getattr(saved, "practice", False)),
                "corrections": len(getattr(saved, "corrections", ()) or ()),
                "first": {key: kept.get(key) for key in ("runId", "seconds", "costUsd")}
                if isinstance(kept, dict) and isinstance(kept.get("seconds"), (int, float)) else None,
                "resets": sum(state["taken"] for key, state in
                              self.run_state.start_resets.items() if key[0] == id(saved))}

    def _parts_may_start(self, snapshot, request):
        try:
            return bool(self.program_parts.may_start(self, snapshot, request))
        except Exception:
            log.exception("a part of a saved program failed")
            return False

    def _parts_point(self, snapshot, request, calls, actions):
        self._saved_check()
        if self._saved_live():
            return None, "", snapshot
        try:
            ended, said, run, snapshot = self.program_parts.point(self, snapshot, request, calls, actions)
        except Exception:
            log.exception("a part of a saved program failed")
            return None, "", snapshot
        if run is not None:
            self.run_state.part = run
        return ended, said, snapshot

    def _contract_ready(self):
        return self.run_state.compile_worker is None

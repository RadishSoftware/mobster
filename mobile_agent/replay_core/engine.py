"""Replay a recorded path on a driver, with no model.

Each step waits for its precondition, then acts through the same driver calls the frontier makes:

- **The app**: the screen is in the app the step was recorded in (LAUNCH_APP and HOME need nothing).
- **The screen**: the same structural screen (its hash), or one at least 80% alike by role/label pairs
  (Jaccard overlap). While the screen settles only those count; once the wait is over, a screen at least 50%
  alike counts too when the step's target is there exactly once.
- **The target**: exactly one element with the recorded label and role (the recorded tree path breaks a tie). Never
  a partial label: a heal is cheaper than a tap on the wrong control.

When a precondition doesn't hold within the wait, the step *drifted*. Then, in order:

1. a step the reviewer chose to keep failing (``strict``) stops the replay: status "drift";
2. with no healer (``--no-heal``) the replay stops: status "drift";
3. the circuit breaker: when healing this step would make more than half the path healed, the replay stops: status
   "drift", reason "breaker";
4. else the healer (the model, for this step only) acts until the screen matches a later recorded step again, or
   finishes the flow. Its actions replace the drifted steps, and the change is returned as a heal entry for review.

A step that sends, posts, deletes, buys or submits (its recorded commit act, or harness_api.commit_act on the step
now) asks ``approve`` first, exactly as a live run asks. With no approver the replay hands that step to the model
(``on_commit="handoff"``) or stops (``"deny"``): it never commits on its own.

Typed text is never invented: a step types its recorded literal, or the value of its blank from ``params``. A step
the model must do each run (``live``) goes to ``live``, or hands off.
"""

import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .. import harness_api
from ..wda.system_dialogs import DialogStop
from .path import ELEMENT_OPS, POINT_OPS, SWIPES, TEXT_OPS, anchor, describe, signature_hash

STRONG, WEAK = .8, .5
SEEK_SWIPES = 4
WAIT_SECONDS = 3.0
POLL_SECONDS = .15
MIN_WAIT, STILL_READS = 1.0, 2
BREAKER = .5
DIALOG_ROUNDS = 3        # times a step that doesn't match looks for an iOS dialog in front of it before it drifts


@dataclass
class Heal:
    """What a healer did."""
    ok: bool
    steps: list = field(default_factory=list)
    resume_at: Optional[int] = None
    done: bool = False
    cost_usd: float = 0.0
    reason: str = ""
    status: Optional[str] = None


@dataclass
class Result:
    status: str                  # done | drift | stopped | declined | handoff | heal_failed | error | dialog
    steps: list
    strict: list = field(default_factory=list)  # protected positions in the resulting path
    completed: int = 0
    replayed: int = 0
    live: int = 0
    heals: list = field(default_factory=list)
    reason: str = ""
    cost_usd: float = 0.0
    seconds: float = 0.0
    at: Optional[int] = None
    answer: Optional[str] = None
    snapshot: object = None
    model_status: Optional[str] = None


def plain(text):
    return " ".join(str(text or "").split()).casefold()


def find_target(step, snapshot):
    """The one element ``step`` acts on in ``snapshot``, or None."""
    target = step.get("target")
    if not isinstance(target, dict):
        return None
    label, role, path = plain(target.get("label")), target.get("role") or "", target.get("id")
    elements = list(getattr(snapshot, "elements", ()) or ())
    if not label:
        found = [e for e in elements if path and e.locator == path and (not role or e.role == role)]
        return found[0] if len(found) == 1 else None
    found = [e for e in elements if plain(e.label) == label and (not role or e.role == role)]
    if len(found) > 1 and path:
        found = [e for e in found if e.locator == path] or found
    return found[0] if len(found) == 1 else None


def screen_likeness(step, snapshot):
    from ..state import signature
    screen = step.get("screen") or {}
    pairs = screen.get("signature")
    if not pairs and not screen.get("hash") and not screen.get("shape"):
        return 0.0
    now = signature(snapshot)
    if screen.get("hash") and signature_hash(now) == screen["hash"]:
        return 1.0
    if not pairs and screen.get("shape"):
        # Kept as hashed pairs (a saved program keeps no screen text): the same overlap measure on the hashes.
        from .path import screen_shape
        ours, theirs = set(screen["shape"]), set(screen_shape(now))
        union = ours | theirs
        return len(ours & theirs) / len(union) if union else 1.0
    if not pairs:
        return 0.0
    ours, theirs = {tuple(pair) for pair in pairs}, {tuple(pair) for pair in now}
    union = ours | theirs
    return len(ours & theirs) / len(union) if union else 1.0


def match(step, snapshot, *, settled=False):
    """(ok, element, why) for ``step`` on ``snapshot``."""
    op = step.get("op")
    if op in ("LAUNCH_APP", "HOME", "WAIT"):
        return True, None, ""
    app, now = step.get("app"), getattr(snapshot, "bundle_id", "") or ""
    if app and app != now:
        return False, None, f"{now or 'an unknown app'} is in front, not {app}"
    if op in POINT_OPS:
        proof = (step.get("screen") or {}).get("fingerprint")
        if not proof:
            return False, None, "the recorded point has no screen geometry proof"
        if proof != getattr(snapshot, "content_fingerprint", None):
            return False, None, "the screen geometry changed around the recorded point"
    likeness = screen_likeness(step, snapshot)
    element = None
    if op in ELEMENT_OPS or (op in SWIPES and step.get("target")):
        element = find_target(step, snapshot)
        if element is None and op in ELEMENT_OPS:
            label = (step.get("target") or {}).get("label")
            return False, None, (f'"{label}" isn\'t on the screen' if label else "the field isn't on the screen")
    if likeness >= STRONG or (settled and element is not None and likeness >= WEAK):
        return True, element, ""
    if settled and element is not None and not (step.get("screen") or {}).get("signature") \
            and not (step.get("screen") or {}).get("shape"):
        # A step learned from a live run's step records has only its screen's hash: pinned by its target alone.
        return True, element, ""
    return False, None, f"the screen is different ({round(likeness * 100)}% alike)"


class Replayer:
    """Replays one path on ``driver``; a recorded step never opens or acts in an app the run says no to."""

    def __init__(self, driver, *, approve=None, heal=None, live=None, emit=None, cancelled=None, params=None,
                 frame=None, on_commit="handoff", wait_seconds=WAIT_SECONDS, breaker=BREAKER, sleep=time.sleep,
                 clock=time.monotonic, app_names=None, allows_app=None, decide=None, dialogs=None):
        self.driver = driver
        # dialogs: a verify.dialogs.Responder. iOS's own dialogs (permission alerts, Allow Paste, the purchase sheet)
        # are answered from the check's policy between steps, and are neither a step nor a heal. None: the model's job.
        self.dialogs = dialogs
        # decide(step, element, snapshot, text) -> permissions.gate.Decision: the run's permission gate, so a replayed
        # step asks what the same step asks live (ask_all's changes, a like or follow, an amount, a one-time code). None
        # (replay_core.checks on simulators): the recorded commit act alone.
        self.decide = decide
        # allows_app(bundle) -> bool: whether the run may use an app (its first-use card asks). None: every app.
        self.allows_app = allows_app
        self.approve, self.heal, self.live = approve, heal, live
        self.emit = emit or (lambda event: None)
        self.cancelled = cancelled or (lambda: False)
        self.params = dict(params or {})
        self.frame = frame
        self.on_commit = on_commit
        self.wait_seconds, self.breaker = wait_seconds, breaker
        self.sleep, self.clock = sleep, clock
        self.app_names = dict(app_names or {})

    def read(self):
        ready = getattr(self.driver, "observe_ready", None)
        return ready(timeout=15) if callable(ready) else self.driver.observe(timeout=15)

    def settle(self, before, op):
        if op in TEXT_OPS:
            return self.driver.observe(timeout=15)
        native = getattr(self.driver, "wait_for_change", None)
        if callable(native):
            try:
                return native(before, wait_seconds=.6, timeout=2.1)
            except Exception:
                pass
        return self.read()

    def wait_for(self, step, snapshot):
        """(ok, element, snapshot, why) once the precondition holds; a weak match goes only once two reads agree."""
        started = self.clock()
        deadline = started + self.wait_seconds
        still, last = 0, getattr(snapshot, "content_fingerprint", None)
        while True:
            ok, element, why = match(step, snapshot)
            if ok:
                return True, element, snapshot, ""
            if still >= 1:
                ok, element, why = match(step, snapshot, settled=True)
                if ok:
                    return True, element, snapshot, ""
            now = self.clock()
            if now >= deadline or (still >= STILL_READS and now - started >= MIN_WAIT) or self.cancelled():
                ok, element, why = match(step, snapshot, settled=True)
                return ok, element, snapshot, why
            self.sleep(POLL_SECONDS)
            snapshot = self.read()
            key = getattr(snapshot, "content_fingerprint", None)
            still = still + 1 if key is not None and key == last else 0
            last = key

    def run(self, path):
        steps = list(path.get("steps") or ())
        strict = {int(n) for n in path.get("strict") or ()}
        started = self.clock()
        out = Result(status="done", steps=[])
        total = len(steps)
        healed_over = 0
        index = 0
        attempted = False  # an action or a model callback may have acted: an exception from here on is unknown
        snapshot = self.read()

        def answer_dialogs(current):
            """The screen after iOS's dialogs in front were answered (verify.dialogs); a stop becomes the result."""
            if self.dialogs is None:
                return current, 0
            return self.dialogs.clear(current, thorough=True, read=self.read)

        def finish(status, reason="", at=None, **extra):
            out.status, out.reason, out.at = status, reason, at
            out.seconds = round(self.clock() - started, 3)
            out.snapshot = snapshot
            for key, value in extra.items():
                setattr(out, key, value)
            if status != "done":
                out.steps.extend(steps[index:])  # the rest stays as recorded
            try:
                self.emit({"event": "replay_finished", "status": status, "reason": reason, "replayed": out.replayed,
                           "healed": len(out.heals), "live": out.live, "seconds": out.seconds})
            except Exception:  # an observer never costs the run its result, an unknown outcome included
                logging.getLogger(__name__).exception("replay completion observer failed")
            return out

        self.emit({"event": "replay_started", "steps": total})
        asked_at, rounds = -1, 0
        try:
            while index < total:
                if self.cancelled():
                    return finish("stopped", "The run was stopped.", index)
                step = steps[index]
                if asked_at != index:
                    asked_at, rounds = index, 0
                try:
                    snapshot, _ = answer_dialogs(snapshot)
                except DialogStop as stop:
                    return finish("dialog", str(stop), index)
                if step.get("live"):
                    if self.live is None:
                        return finish("handoff", f"step {index + 1} ({describe(step)}) needs the model", index)
                    attempted = True
                    outcome = self.live(index, step, snapshot, list(out.steps))
                    out.cost_usd += outcome.cost_usd
                    if not outcome.ok:
                        return finish("heal_failed", outcome.reason or f"the model couldn't do step {index + 1}", index,
                                      model_status=outcome.status)
                    resume = outcome.resume_at if outcome.resume_at is not None and outcome.resume_at > index \
                        else index + 1
                    if outcome.done:
                        resume = total
                    if any(index + 1 < number <= resume for number in strict):
                        return finish("drift", f"Step {index + 1} ({describe(step)}): the model crossed a step you "
                                               "chose to keep failing. Review this check before running it again.",
                                      index)
                    out.live += 1
                    out.steps.append(step)  # a live step stays live
                    if index + 1 in strict:
                        out.strict.append(len(out.steps))
                    out.completed = index + 1
                    if outcome.done:
                        index = total
                        snapshot = self.read()
                        break
                    index = resume
                    snapshot = self.read()
                    continue
                refused = self._app_refused(step, snapshot)
                if refused:  # before anything in that app, a seek's swipes included
                    return finish("handoff", refused, index)
                if step.get("seek"):
                    try:
                        snapshot = self.seek(step, snapshot)
                    except Exception as error:
                        # A swipe may have landed before the failure: stop, never swipe again.
                        return finish("error", f"Step {index + 1} ({describe(step)}) failed while seeking: "
                                               f"{type(error).__name__}: {str(error)[:160]}", index,
                                      model_status="unknown_outcome")
                ok, element, snapshot, why = self.wait_for(step, snapshot)
                if self.cancelled():
                    return finish("stopped", "The run was stopped.", index)
                refused = self._app_refused(step, snapshot)
                if refused:
                    return finish("handoff", refused, index)
                if not ok and self.dialogs is not None and rounds < DIALOG_ROUNDS:
                    # A dialog that came up while the step waited: answer it, then look at the step again.
                    rounds += 1
                    try:
                        snapshot, answered = answer_dialogs(snapshot)
                    except DialogStop as stop:
                        return finish("dialog", str(stop), index)
                    if answered:
                        continue
                if not ok:
                    self.emit({"event": "replay_drift", "step": index + 1, "action": describe(step), "why": why})
                    if index + 1 in strict:
                        return finish("drift", f"Step {index + 1} ({describe(step)}): {why}. You chose to keep this "
                                               "check failing when this step changes.", index)
                    if self.heal is None:
                        return finish("drift", f"Step {index + 1} ({describe(step)}): {why}.", index)
                    if (healed_over + 1) > total * self.breaker:
                        return finish("drift",
                                      f"More than half the recorded steps would heal (step {index + 1}: {why}), "
                                      "so Mobster stopped instead of healing.", index, model_status="breaker")
                    before = self._frame("heal-before")
                    limit = max(1, int(total * self.breaker) - healed_over)
                    attempted = True
                    outcome = self.heal(index, step, snapshot, why, list(out.steps), limit)
                    out.cost_usd += outcome.cost_usd
                    breaker = (outcome.status == "breaker" or (outcome.ok and healed_over + len(
                        steps[index:total if outcome.done else max(outcome.resume_at or index + 1, index)])
                        > total * self.breaker))
                    if breaker:
                        snapshot = self.read()
                        return finish("drift", f"More than half the recorded steps would heal (from step {index + 1}: "
                                               f"{why}), so Mobster stopped instead of healing.", index,
                                      model_status="breaker")
                    if not outcome.ok:
                        return finish("heal_failed", outcome.reason or f"Smart couldn't heal step {index + 1}", index,
                                      model_status=outcome.status)
                    snapshot = self.read()
                    refused = self._app_refused(step, snapshot)
                    if refused:
                        return finish("handoff", refused, index)
                    after = self._frame("heal-after")
                    resume = total if outcome.done else outcome.resume_at
                    if resume is None or resume < index or (resume == index and not outcome.steps):
                        resume = index + 1  # resume == index with new steps: the heal added steps before this one
                    if any(index + 1 <= number <= resume for number in strict):
                        return finish("drift", f"Step {index + 1} ({describe(step)}): the heal crossed a step you "
                                               "chose to keep failing. Review this check before running it again.",
                                      index)
                    old = steps[index:resume]
                    healed_over += len(old)
                    out.heals.append(heal_entry(index, old, outcome.steps, why, before, after))
                    out.steps.extend(outcome.steps)
                    out.completed = resume
                    self.emit({"event": "replay_healed", "step": index + 1, "from": describe(step),
                               "to": describe(outcome.steps[0]) if outcome.steps else "nothing", "resume": resume + 1})
                    if outcome.done:
                        out.model_status = outcome.status
                        index = total
                        break
                    index = resume
                    continue
                gate = self._commit(step, element, snapshot, index)
                act = None
                if gate is not None:
                    act, refusal, approved_screen = gate
                    if refusal is not None:
                        status, answer = refusal
                        if status == "handoff":
                            return finish("handoff", f"step {index + 1} ({describe(step)}) commits and asks a person "
                                                     "through the model", index)
                        return finish("declined", f"{describe(step)} wasn't approved ({answer}).", index,
                                      answer=answer)
                    if self.cancelled():  # stopped while an approval waited
                        return finish("stopped", "The run was stopped.", index)
                    current = self.read()
                    if current.fingerprint != approved_screen:
                        return finish("drift", f"Step {index + 1} ({describe(step)}): the screen changed while "
                                               "approval was waiting.", index)
                    refused = self._app_refused(step, current)
                    if refused:
                        return finish("handoff", refused, index)
                    ok, element, why = match(step, current, settled=True)
                    if not ok:
                        return finish("drift", f"Step {index + 1} ({describe(step)}): {why} after approval.", index)
                    snapshot = current
                if self.cancelled():
                    return finish("stopped", "The run was stopped.", index)
                attempted = True
                try:
                    self.act(step, element, snapshot)
                except Exception as error:
                    # The action may have landed: report an unknown outcome and never send it again.
                    return finish("error", f"Step {index + 1} ({describe(step)}) failed: {type(error).__name__}: "
                                           f"{str(error)[:160]}", index, model_status="unknown_outcome")
                out.replayed += 1
                executed = refreshed(step, element, snapshot)
                if act:
                    executed["commit"] = act
                out.steps.append(executed)
                if index + 1 in strict:
                    out.strict.append(len(out.steps))
                out.completed = index + 1
                self.emit({"event": "replay_step", "step": index + 1, "op": step["op"], "action": describe(step),
                           "label": (step.get("target") or {}).get("label"),
                           "role": (step.get("target") or {}).get("role"),
                           "typed": self.text_for(step) if step["op"] in TEXT_OPS else None, "app": step.get("app")})
                snapshot = self.settle(snapshot, step["op"])
                index += 1
        except Exception as error:
            if not attempted:
                raise  # nothing was sent: the caller can hand the run on
            # Something may have acted, even before ``completed`` advanced (a model callback raising after its
            # tap): stop with what is known and never run it again.
            index = max(index, out.completed)
            detail = f"{type(error).__name__}: {str(error)[:160]}"
            reason = (f"Step {index} ({describe(steps[index - 1])}) ran, then Mobster lost the screen: {detail}"
                      if out.completed else f"Step {index + 1} ({describe(steps[index])}) failed: {detail}")
            return finish("error", reason, index, model_status="unknown_outcome")
        try:
            snapshot, _ = answer_dialogs(snapshot)   # a dialog the last step brought up would hide the screen it reads
        except DialogStop as stop:
            return finish("dialog", str(stop), index)
        return finish("done")

    def seek(self, step, snapshot):
        for _ in range(SEEK_SWIPES):
            if find_target(step, snapshot) is not None or self.cancelled() or self._app_refused(step, snapshot):
                return snapshot
            app, now = step.get("app"), getattr(snapshot, "bundle_id", "") or ""
            if app and app != now:
                return snapshot
            before = snapshot
            self.driver.execute("SWIPE_UP", None, snapshot, timeout=10)
            snapshot = self.settle(before, "SWIPE_UP")
            self.emit({"event": "replay_seek", "label": (step.get("target") or {}).get("label")})
            if getattr(snapshot, "content_fingerprint", None) == getattr(before, "content_fingerprint", object()):
                return snapshot
        return snapshot

    def _app_refused(self, step, snapshot):
        """Why step ``step`` may not run here (the app it opens, or the one in front, isn't allowed), else ""."""
        if self.allows_app is None:
            return ""
        apps = [getattr(snapshot, "bundle_id", "") or ""]
        if step["op"] == "LAUNCH_APP":
            apps.append(step.get("app") or "")
        for bundle in apps:
            if not bundle or bundle == "com.apple.springboard":
                continue
            try:
                allowed = bool(self.allows_app(bundle))
            except Exception:  # noqa: BLE001 -- fails closed
                allowed = False
            if not allowed:
                return f"{self.app_names.get(bundle) or bundle} isn't allowed yet"
        return ""

    def _frame(self, label):
        if self.frame is None:
            return None
        try:
            return self.frame(label)
        except Exception:
            return None

    def _commit(self, step, element, snapshot, index):
        """None for a plain step; otherwise (effective act, refusal, approved screen fingerprint)."""
        label = element.label if element is not None else (step.get("target") or {}).get("label")
        act = step.get("commit") or harness_api.commit_act(step["op"], label, None,
                                                           getattr(snapshot, "bundle_id", None) or step.get("app"))
        if self.decide is not None:
            try:
                decision = self.decide(step, element, snapshot, self.text_for(step))
            except Exception:  # noqa: BLE001 -- an unreadable step asks
                decision = None
            # Asks when either says so: the recorded act, or today's rules for this step as it looks now.
            if decision is None:
                act = act or "act"
            elif decision.asks:
                act = act or decision.act or decision.kind
        if not act:
            return None
        if self.approve is None:
            refusal = ("handoff", None) if self.on_commit == "handoff" else ("declined", "no one to ask")
            return act, refusal, None
        x, y, w, h = element.rect if element is not None else (0, 0, 0, 0)
        app = getattr(snapshot, "bundle_id", None) or step.get("app")
        request = {"kind": "commit", "step": index + 1, "operation": step["op"], "label": (label or "")[:200],
                   "role": element.role if element is not None else None, "text": self.text_for(step),
                   "title": f"{describe(step)}?", "act": act, "app_name": self.app_names.get(app) or None,
                   "target": {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)},
                   "replayed": True}
        approved_screen = snapshot.fingerprint  # capture before an approval callback can mutate the snapshot
        answer = self.approve(request)
        self.emit({"event": "replay_approval", "step": index + 1, "act": act,
                   "answer": answer.split(":", 1)[0] if isinstance(answer, str) else None})
        if answer == "approved":
            return act, None, approved_screen
        return act, ("declined", str(answer or "denied")), None

    def text_for(self, step):
        if step.get("param"):
            return self.params.get(step["param"])
        return step.get("text")

    def act(self, step, element, snapshot):
        """One action through the driver, the way frontier._execute sends it."""
        from ..state import ADJUSTABLE_ROLES, TEXT_OPERATIONS, typing_timeout
        driver, op = self.driver, step["op"]
        if op == "LAUNCH_APP":
            driver.execute("LAUNCH_APP", step["app"], snapshot, timeout=20)
        elif op == "HOME":
            driver.execute("HOME", None, snapshot, timeout=10)
        elif op == "WAIT":
            self.sleep(1.0)
        elif op in POINT_OPS:
            x, y = step["point"]
            tap = driver.tap_point
            try:  # bind first: a TypeError raised inside the tap must never send it a second time
                inspect.signature(tap).bind(x, y, snapshot, timeout=10, purpose=op.casefold())
            except TypeError:
                tap(x, y, snapshot, timeout=10)
            else:
                tap(x, y, snapshot, timeout=10, purpose=op.casefold())
        elif op in SWIPES:
            driver.execute(op, element if op in ("SWIPE_LEFT", "SWIPE_RIGHT") else None, snapshot, timeout=10)
        elif op == "LONG_PRESS":
            driver.long_press(element, snapshot, timeout=10)
        elif op == "TAP":
            driver.execute("TAP", element, snapshot, timeout=10)
        elif op == "SUBMIT":
            if "SUBMIT" not in element.actions:
                driver.execute("TAP", element, snapshot, timeout=10)
                snapshot = self.read()
                element = find_target(step, snapshot) or element
            driver.execute("SUBMIT", element, snapshot, timeout=10)
        elif op in TEXT_OPS:
            text = self.text_for(step)
            if not isinstance(text, str):
                raise ValueError("no text for this step")
            if op == "SET_TEXT" and element.role in ADJUSTABLE_ROLES:
                driver.set_value(element, text, timeout=10)
            elif callable(getattr(driver, "write_text", None)):
                append = op in TEXT_OPERATIONS and element.role != "SearchField"
                receipt = driver.write_text(element, snapshot, text, append=append, submit=op == "TYPE_SUBMIT",
                                            timeout=typing_timeout(text) + 20)
                if not receipt.get("matches") or op == "TYPE_SUBMIT" and not receipt.get("submitted"):
                    raise ValueError("The driver did not confirm the recorded text or its submission; inspect "
                                     "the field before trying again")
            elif op == "SET_TEXT" and callable(getattr(driver, "replace_text", None)) and driver.replace_text(
                    element, text, timeout=typing_timeout(text)):
                pass
            elif op == "SET_TEXT":
                clear = getattr(driver, "clear_text", None)
                if callable(clear):
                    clear(element, timeout=10)
                driver.execute("TYPE", element, snapshot, text=text, timeout=typing_timeout(text))
            else:
                driver.execute(op, element, snapshot, text=text, timeout=typing_timeout(text))
        else:
            raise ValueError(f"{op} can't be replayed")


def refreshed(step, element, snapshot):
    """``step`` with its anchors brought up to date; the label and role are what matched, so this is no heal."""
    new = dict(step)
    if step["op"] in POINT_OPS:
        return new  # the point stays bound to its recorded pre-action geometry
    from ..state import signature
    if element is not None and isinstance(step.get("target"), dict):
        new["target"] = {**step["target"], "id": element.locator or step["target"].get("id"),
                         "frame": [round(float(v), 4) for v in element.rect]}
    try:
        pairs = signature(snapshot)
        new["screen"] = {"hash": signature_hash(pairs), "signature": pairs}
    except Exception:
        pass
    return new


def heal_entry(index, old, new, why, before=None, after=None):
    """A heal as results.json's ``healed[]`` and the .heal.yaml file carry it."""
    return {"step": index + 1, "text": describe(old[0]) if old else "",
            "from": describe(old[0]) if old else "nothing",
            "to": "; ".join(describe(step) for step in new[:3]) + ("; …" if len(new) > 3 else "") if new
            else "nothing (the screen it needed came by itself)",
            "why": why, "old": [anchor(step) for step in old], "new": [anchor(step) for step in new],
            "replaced": len(old), "frames": {"before": before and str(before), "after": after and str(after)},
            "status": "pending"}


__all__ = ["Replayer", "Result", "Heal", "match", "find_target", "heal_entry", "describe"]

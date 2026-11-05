"""What the agent refuses, holds back or asks about, and the secrets it never shows."""

import dataclasses
import json
import re

from .. import contract_screen as proof_screen
from .. import harness_api
from ..agent_hooks import mask
from ..effect_ledger import RETRY_LIMIT as EFFECT_RETRY_LIMIT
from ..state import ADJUSTABLE_ROLES, pixel_read
from ..task_policy import APPROVAL_COMMIT, COMMIT_CONTROL, GUARD_WORDS, SHEET_OPENERS, commit_label, requested_by
from ..transport import WDA_ERRORS, NotSent, TransportError
from ..wda.gestures import _SWIPE_Y
from ..wda.guards import DriverRejection
from ..wda.text import NoKeyboardFocus
from .protocol import TYPED_OPS
from .screen import reads_back, screen_pairs, screen_signature, screenshot_part

# Actions that could do something twice: held once their outcome is unknown, until a fresh look settles it.
UNKNOWN_BLOCKS = frozenset({"TAP", "LONG_PRESS", "TAP_XY", "TYPE", "TYPE_SUBMIT", "SET_TEXT", "SUBMIT"})
# Taps are held on the screen they were tried on; typing stays held on its field, or it would be typed twice.
SCREEN_SCOPED = frozenset({"TAP", "LONG_PRESS", "TAP_XY"})
LEAVING_WORDS = frozenset({"back", "cancel", "close", "dismiss", "not now", "later",
                           "maybe later", "skip", "no thanks"})
UNKNOWN_REASON = "The first attempt's outcome is unknown; it may have gone through"
# A commit sent with no receipt yet: the same act again asks, in every mode.
PENDING_REASON = "The first one may still be going through"
# With nobody to answer, what would ask is refused, never done.
NOBODY = "refused:nobody can approve it here"
# Past this many commits each further one asks, in every mode: a runaway task never commits in bulk unseen.
MAX_COMMITS_PER_RUN = 15
CAP_REASON = "This task already did {} things that needed your OK"
# Screen text addressed to an assistant: once a screen shows it, every commit asks, Full access included.
INJECTION = re.compile(
    r"\b(?:ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions|directions|rules)|"
    r"(?:assistant|ai agent|agent|mobster|claude|chatgpt|gpt|gemini|system|admin)\s*[:>]\s*\S|"
    r"tap (?:on )?[\"“']?\w+(?: \w+)?[\"”']? (?:to|and) (?:continue|proceed|confirm|verify)|"
    r"(?:your|the) (?:new |real )?(?:task|instructions?) (?:is|are)\b|you must now|"
    r"(?:user|owner|person) (?:has |already )?(?:approved|authori[sz]ed|consented)|approve(?:d)? (?:all|every)|"
    r"(?:do not|don.?t|without) (?:ask(?:ing)?|confirm(?:ing)?) (?:the user|for (?:confirmation|approval))|"
    r"(?:send|forward|share) (?:this|the|your) (?:code|password|passcode) to)", re.I)
INJECTION_REASON = "This screen has text that may be trying to steer Mobster"
TAINT_REASON = "This message holds text copied from the screen"
CHANGED_WHILE_ASKING = ("The screen changed while Mobster waited for your OK, so it didn't act. Look at the screen "
                        "again: the person is asked again on the screen as it is now.")
# COMMIT_CONTROL has no like, and a long run asks before one (P1-2).
LIKE = re.compile(r"\b(like|super ?like)\b", re.I)
LOOP_REPEATS = 3
# A tapped act may already be done: the same tap on the same screen waits until the run looks somewhere new.
REPEAT_ACT_SKIP = frozenset({"done", "close", "cancel", "back", "ok", "next", "continue"})
REPEAT_ACT_FEEDBACK = ("Refused: you already tapped {label!r} on this screen in this app (turn {step}), and it changed "
                       "the screen then, so it may already be done. Look at the list or page it adds to first. If it "
                       "isn't there, come back and tap it again.")
REPEAT_ACT_AGAIN = ("Refused again: since {label!r} (turn {step}) you have seen only this screen and the one it left. "
                    "Open the list or page it adds to and check before tapping it again.")
LOOP_WINDOW = 12
# Repeated failures call the phone guard, then end the run (B2: 50 failed launches on a locked phone).
SAME_FAILURES_TO_CHECK = 2
SAME_FAILURES_TO_STOP = 3
SAME_REFUSALS_TO_STOP = 4
PHONE_LOCKED = "Your iPhone is locked. Unlock it and try again."
COVERED = ("Your iPhone's lock screen or a system alert is in front of {app}, so Mobster stopped instead of "
           "tapping what it can't see. Unlock your iPhone or close the alert, then try again.")
KEYPAD_REFUSAL = ("Refused: this is the iPhone's passcode screen. Mobster can't unlock the phone; never tap "
                  "it, type on it or try a passcode.")
# StaleSession: the runner restarted under the driver's session, and recovery attaches to the new one.
CONNECTION_LOST = ("ConnectionRefusedError", "ConnectionResetError", "RemoteDisconnected", "BrokenPipeError",
                   "ConnectionAbortedError", "StaleSession")
# The errors whose request never ran: only these are sent again, after a fresh look.
NOTHING_SENT = ("ConnectionRefusedError", "StaleSession")


def declined_result(answer):
    status = {"timeout": "approval_timeout", "stopped": "stopped"}.get(answer, "approval_denied")
    reason = {"timeout": "No answer to the approval request, so the action was not taken",
              "stopped": "Stopped while waiting for your approval"}.get(
                  answer, "You declined the action, so it was not taken")
    return {"status": status, "answer": None, "reason": reason}


def input_shows(element, text):
    wanted = " ".join(str(text or "").split()).casefold()
    value = " ".join(str(getattr(element, "value", None) or "").split()).casefold()
    if not wanted or not value:
        return False
    return value == wanted or (getattr(element, "role", None) in ADJUSTABLE_ROLES
                               and value.split()[:1] == wanted.split()[:1])


def row_under_stroke(snapshot):
    for element in getattr(snapshot, "elements", ()) or ():
        x, y, w, h = element.rect
        if element.role == "Cell" and h < .2 and w >= .8 and y <= _SWIPE_Y <= y + h:
            return element
    return None


def refused_sentence(reason, times, acted):
    why = re.sub(r"^Refused:\s*", "", " ".join(str(reason or "").split()))[:240].rstrip(". ")
    tail = "" if acted else " No action was taken."
    return (f"Mobster stopped: it was refused the same step {times} times ({why}). Reword the request to say exactly "
            f"what to do, or do that step yourself.{tail}")


def stuck_sentence(op, name, detail, acted):
    tail = "" if acted else " No action was taken."
    if op == "LAUNCH_APP":
        return (f"Mobster couldn't open {name} ({detail}). Your iPhone may be locked, or {name} may be asking for a "
                f"passcode or Face ID: unlock your iPhone (or open {name} once yourself), then try again.{tail}")
    return f"The same step failed three times ({detail}), so Mobster stopped instead of trying again.{tail}"


# A BaseException, so it passes every ``except Exception`` on its way out of the run.
class GuardStop(BaseException):
    def __init__(self, verdict):
        super().__init__(verdict.code)
        self.verdict = verdict


def scrub(value, secrets):
    if isinstance(value, str):
        return mask(value, secrets)
    if isinstance(value, dict):
        return {scrub(key, secrets): scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub(item, secrets) for item in value)
    return value


def failure_feedback(op, element, text, error):
    message = str(error)
    if op in TYPED_OPS and "control characters" in message:
        if text and "\n" in text and (element is None or element.role != "TextView"):
            return ("Refused: this field takes one line. Line breaks go only in a multi-line text view (a note or "
                    "document body): type the lines there, or send this field text without line breaks.")
        if text and len(text) > 1000:
            return f"Refused: the text is too long ({len(text)} characters) for one action: type it in parts."
    if outcome_unknown(error, op):
        return (("The action timed out" if isinstance(error, TimeoutError) else "The action's outcome is unknown")
                + ": it may have happened, or partly. " + unknown_advice(op))
    return f"The action could not be performed ({type(error).__name__}: {message[:120]}); the screen may have changed."


def unknown_advice(op):
    return "; ".join(["Look at the screen before acting"]
                     + (["Mobster sends this same action again only once a fresh look shows it didn't happen"]
                        if op in UNKNOWN_BLOCKS else [])
                     + (["read the fields' current values and never retype text that is already there"]
                        if op in TYPED_OPS else [])) + "."


# WDA's names for a request it refuses before it acts, so nothing ran (FBCommandStatus, in v16.12.10's handlers). They
# speak for one request, so only for an action of one primitive: typing is several, and a later request's refusal says
# nothing about the earlier ones.
WDA_NOTHING_RAN = frozenset({"invalid argument", "invalid selector", "invalid coordinates", "unknown command",
                             "unknown method", "unsupported operation", "no such alert", "no such element"})


# Fails closed: unsure is unknown. Connection recovery doesn't resend such an action, and a further attempt the model
# asks for goes through _recheck_unknown. ``op``: on a tap or on typing, any transport failure is unknown unless it
# proves nothing ran: it never left, or the driver says nothing was typed, or WDA named its refusal of a tap with the
# status it pairs with that name. An answer that can't be read, has no name, or has another status proves nothing.
def outcome_unknown(error, op=None):
    if isinstance(error, DriverRejection):
        return error.code not in DriverRejection.PRE_DISPATCH
    if isinstance(error, TransportError) and op in UNKNOWN_BLOCKS:
        if isinstance(error, (NotSent, NoKeyboardFocus)) or any(mark in str(error) for mark in NOTHING_SENT):
            return False
        refused = error.wda_error in WDA_NOTHING_RAN and error.status == WDA_ERRORS[error.wda_error]
        return op in TYPED_OPS or not refused
    message = str(error)
    if isinstance(error, ConnectionError):
        return not isinstance(error, ConnectionRefusedError)
    return (isinstance(error, TimeoutError) or "outcome unknown" in message or "outcome_unknown" in message
            or any(name in message for name in CONNECTION_LOST if name not in NOTHING_SENT))


def control_state(element):
    if element is None:
        return None
    return [element.label or "", element.role or "", element.value or "", bool(element.selected),
            bool(element.enabled)]


VOLATILE = re.compile(r"(?:\d[\d,.]*\s*[kmb]?\s*(?:likes?|views?|comments?|followers?|reactions?|replies|plays?|"
                      r"seconds?|minutes?|hours?|days?|weeks?|[smhdw])?(?: ago)?|\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?|"
                      r"now|just now|yesterday|today|typing…?|typing\.\.\.|delivered|read|sent|seen|live|"
                      r"\d+\s*%)", re.I)


# Checked again at dispatch: a clock, a typing dot or a video's progress may change; nothing else may.
def binding(snapshot, element):
    if element is None:
        return None
    elements = list(getattr(snapshot, "elements", ()) or ())
    from ..permissions.consequence import CONTAINERS
    try:
        to = proof_screen.addressed_to(snapshot)
    except Exception:
        to = None
    try:
        amount = proof_screen.commit_amount(element, elements)
    except Exception:
        amount = None
    cx, cy = element.rect[0] + element.rect[2] / 2, element.rect[1] + element.rect[3] / 2
    rows = [e for e in elements if e is not element and e.role == "Cell" and e.rect[0] <= cx <= e.rect[0] + e.rect[2]
            and e.rect[1] <= cy <= e.rect[1] + e.rect[3]]
    row = min(rows, key=lambda e: e.rect[2] * e.rect[3]).label if rows else None
    # The text above a control says what it acts on: a moved feed puts another post's Like in its place.
    above = [e for e in elements if e.role in ("StaticText", "Cell", "Link") and e is not element
             and 0 <= cy - (e.rect[1] + e.rect[3] / 2) <= .4 and re.search(r"[A-Za-z]{3}", e.label or "")
             and not VOLATILE.fullmatch(" ".join((e.label or "").split()))]
    context = " ".join(min(above, key=lambda e: cy - e.rect[1]).label.split()) if above else None
    fields = tuple(sorted(" ".join((e.value or "").split()) for e in elements
                          if e.editable and e.role != "SearchField" and (e.value or "") != (e.placeholder or "")))
    return {"app": getattr(snapshot, "bundle_id", "") or "", "label": " ".join((element.label or "").split()),
            "role": element.role, "frame": tuple(round(v, 3) for v in element.rect), "row": row, "context": context,
            "title": proof_screen.screen_title(snapshot), "to": to, "fields": fields, "amount": amount,
            "overlays": tuple(sorted(" ".join((e.label or "").split()) for e in elements if e.role in CONTAINERS))}


def same_binding(approved, now, tolerance=.02):
    if approved is None or now is None:
        return False
    for key, value in approved.items():
        if key == "frame":
            if any(abs(a - b) > tolerance for a, b in zip(value, now[key])):
                return False
        elif now.get(key) != value:
            return False
    return True


def _commits(element):
    from ..permissions.consequence import label_act
    return bool(label_act(element.label, element.role) or commit_label(element.label, element.role))


# On a screen read from pixels a row's swipe actions are lines of text, not buttons.
SWIPE_ACTIONS = re.compile(r"^(?:delete|trash|archive|more|flag|unflag|mark as (?:read|unread)|read|unread|pin|unpin|"
                           r"mute|unmute|hide alerts|show alerts|move|report|remove|edit|share)$", re.I)


def revealed_actions(snapshot, row):
    x, y, w, h = row.rect
    if w >= .95:
        return []
    pixels = pixel_read(snapshot)
    return [e for e in getattr(snapshot, "elements", ()) or () if e is not row
            and (e.role == "Button" or pixels and SWIPE_ACTIONS.match(" ".join((e.label or "").split())))
            and y <= e.rect[1] + e.rect[3] / 2 <= y + h and e.rect[0] >= x + w - .01]


def _nobody(request):
    return NOBODY


# Fails closed: whatever could commit counts, and is never sent again without the person's OK.
def consequential(op, element, snapshot):
    from ..permissions.rules import DESTRUCTIVE, PRICE, Action, outside_act
    if op in ("SUBMIT", "TYPE_SUBMIT", "TAP_XY") or element is None:
        return True
    label = " ".join((element.label or "").split())
    if harness_api.commit_act(op, label, None, getattr(snapshot, "bundle_id", None)) is not None:
        return True
    if APPROVAL_COMMIT.search(label) or DESTRUCTIVE.search(label) or PRICE.search(label):
        return True
    if outside_act(Action(op=op, label=label, role=element.role)) is not None:
        return True
    if op in TYPED_OPS or label.casefold() in LEAVING_WORDS:
        return False
    # A tap on a form holding typed text may submit it; moving back or away from it doesn't.
    return any(e.editable and e.role != "SearchField" and (e.value or "").strip() and e.value != e.placeholder
               and (e.value or "").strip() != (e.label or "").strip() for e in snapshot.elements)


def holds_text(element, text):
    shown = element.value or ""
    if not text or not shown.strip() or shown == element.placeholder or shown.strip() == (element.label or "").strip():
        return False
    return reads_back(shown, text)


def guard(request, operation, element):
    if operation != "TAP" or element is None:
        return None
    # A control names its act in its first words; a long row that mentions one is not it (multi-071).
    label = " ".join((element.label or "").split()[:GUARD_WORDS])
    # Only commits the person would want to see first: Save, Done and Add complete a requested change.
    if not COMMIT_CONTROL.search(label) or commit_label(element.label, element.role) is None:
        return None
    if " ".join(label.split()).casefold() in SHEET_OPENERS:
        # Opening a share or action sheet commits nothing; its Send or Post is guarded in turn.
        return None
    if requested_by(request, "TAP", element.role, label):
        return None
    return (f"Refused: tapping {label!r} performs an act the request does not ask for. Choose an action "
            "that does what the user asked.")


class SafetyMixin:
    def _act_key(self, op, element, snapshot):
        if op not in ("TAP", "LONG_PRESS") or element is None:
            return None
        label = proof_screen.plain(element.label or "")
        if not label or label.split()[0] in REPEAT_ACT_SKIP or not proof_screen.ACT_LABEL.search(
                " ".join(label.split()[:GUARD_WORDS])):
            return None
        # The whole screen, not its title: paying one row of a list of Pay rows makes the next row's Pay another act.
        return (self._app_name(snapshot), label, screen_signature(snapshot))

    @staticmethod
    def _effect(op, element, app, text, app_name, snapshot):
        if op not in UNKNOWN_BLOCKS:
            return None
        target = (proof_screen.plain(element.label or "") or element.locator) if element is not None else str(app or "")
        role = element.role if element is not None else None
        screen = screen_signature(snapshot) if op in SCREEN_SCOPED else None
        return ("TAP" if op in ("LONG_PRESS", "TAP_XY") else op), (app_name, op, target, role, text, screen)

    # Fails closed: an attempt's screen that couldn't be read holds the control on any screen.
    def _held(self, effect, snapshot):
        if effect is None:
            return None
        if self.run_state.effects.would_duplicate(effect[0], None, effect=effect[1]):
            return effect
        if effect[1][-1] is None:
            return None
        pairs = screen_pairs(snapshot)

        def under(key, record):
            if key[:-1] != effect[1][:-1]:
                return False
            then = (record.get("context") or {}).get("pairs")
            return then is None or pairs is None or then <= pairs

        keys = self.run_state.effects.held(under)
        return (effect[0], keys[0]) if keys else None

    @staticmethod
    def _before_unknown(element, snapshot):
        return {"screen": screen_signature(snapshot), "pairs": screen_pairs(snapshot),
                "app": getattr(snapshot, "bundle_id", "") or "", "control": control_state(element)}

    # Fails closed: anything missing counts as changed.
    def _unchanged(self, before, element, snapshot):
        if not before or snapshot is None or element is None:
            return False, None
        found = self._find(snapshot, element.label, element.role, element.locator)
        same = (found is not None and before.get("screen") and screen_signature(snapshot) == before["screen"]
                and (getattr(snapshot, "bundle_id", "") or "") == before.get("app")
                and control_state(found) == before.get("control"))
        return bool(same), found

    # Unchanged, a neutral action goes once more and a consequential one asks again, never on its own.
    def _recheck_unknown(self, op, element, effect, what):
        record = self.run_state.effects.lookup(effect[0], None, effect=effect[1]) or {}
        done = (f"Refused: your earlier {op} {what!r} took effect (the screen changed after it), so Mobster doesn't "
                "send it again. Go on from the screen as it is now.")
        if record.get("outcome") == "observed_change":
            return "refuse", done, None
        held = (f"Refused: your earlier {op} {what!r} has an unknown outcome: it may have happened, so Mobster doesn't "
                "send it again. Look at the screen: if it took effect, go on; if not, try another way, or finish with "
                "BLOCKED and say what is left.")
        if record.get("outcome") != "unknown" or element is None:
            return "refuse", held, None
        fresh = self._read_once()
        unchanged, found = self._unchanged(record.get("context"), element, fresh)
        if not unchanged:
            self.run_state.effects.settle(effect[0], None, effect=effect[1], happened=True)
            return "refuse", done, None
        if consequential(op, found, fresh):
            if (self.approve or self.forced_approve or self.floor_approve) is None:
                return "refuse", held[:-1] + " Sending it again needs the user's OK, and nobody can give it here.", None
            return "ask", fresh, found
        if record.get("retries", 0) >= EFFECT_RETRY_LIMIT:
            return "refuse", (f"Refused: your earlier {op} {what!r} was sent once more after an unknown outcome, and "
                              "that one's outcome is unknown too, so Mobster doesn't send it again. Try another way, "
                              "or finish with BLOCKED and say what is left."), None
        return "retry", fresh, found

    def _mask(self, text):
        return mask(text, self.run_state.secrets)

    def _mask_reply(self, out):
        if not self.run_state.secrets:
            return out
        try:
            return json.loads(self._mask(json.dumps(out, ensure_ascii=False)))
        except (TypeError, ValueError):
            return {}

    # The model, the contract and the trace see this copy; the driver acts on the real one.
    def _masked(self, snapshot):
        if not self.run_state.secrets or snapshot is None:
            return snapshot
        elements = [dataclasses.replace(e, label=self._mask(e.label)[:500], value=self._mask(e.value)[:500],
                                        placeholder=self._mask(e.placeholder)[:500]) for e in snapshot.elements]
        return dataclasses.replace(snapshot, elements=elements, text=self._mask(snapshot.text))

    def _blur_rects(self, snapshot):
        keep = []
        for rect, bundle in self.run_state.secret_rects:
            x, y, w, h = rect
            if bundle and getattr(snapshot, "bundle_id", None) not in (bundle, None, ""):
                continue
            if any(e.editable and e.rect[0] < x + w and x < e.rect[0] + e.rect[2] and e.rect[1] < y + h
                   and y < e.rect[1] + e.rect[3] for e in snapshot.elements):
                keep.append((rect, bundle))
        self.run_state.secret_rects = keep
        return tuple(rect for rect, _ in keep)

    # A full swipe can act on a row; nothing is undone here, the run stops and says where to look.
    def _row_gone(self, op, element, before, after):
        if op not in ("SWIPE_LEFT", "SWIPE_RIGHT") or element is None or element.role != "Cell":
            return None
        if getattr(after, "bundle_id", None) != getattr(before, "bundle_id", None) or \
                proof_screen.screen_title(after) != proof_screen.screen_title(before):
            return None
        label = " ".join((element.label or "").split())
        if any(" ".join((e.label or "").split()) == label for e in after.elements):
            return None
        name = self._app_name(after) or "the app"
        return (f"A swipe on {self._mask(label)[:60]!r} removed it from the list in {name}. Mobster stopped so you can "
                f"check: the app may offer Undo, or keep it in Recently Deleted or Trash.")

    def _secret_blur(self, snapshot):
        blur = self._blur_rects(snapshot) if self.run_state.secret_rects else ()
        if self.run_state.secrets:
            blur = tuple(blur) + tuple(
                tuple(e.rect) for e in getattr(snapshot, "elements", ()) or ()
                if any(secret in f"{e.label}\n{e.value}\n{e.placeholder}" for secret in self.run_state.secrets))
        return blur

    def _wants_image(self, turn, lines):
        if self._image_mode != "tree" or self.longform is not None:
            self.run_state.image_why = "off" if self._image_mode != "tree" else "long task"
            return True
        app = getattr(turn.snapshot, "bundle_id", "") or ""
        facts = self._image_policy.Turn(
            first=self.run_state.imaged_app is None, new_app=app != self.run_state.imaged_app,
            blind=self.run_state.blind, missed=bool(turn.feedback) or bool(turn.unproven),
            skill_pictures=bool(self.run_state.skill_images), pixel_read=pixel_read(turn.snapshot),
            request=turn.request or "")
        send, self.run_state.image_why = self._image_policy.decide(self._image_policy.parse_rows(lines), facts)
        return send

    def _screenshot(self, snapshot):
        return screenshot_part(self.driver, since=getattr(snapshot, "read_started", None),
                               blur=self._secret_blur(snapshot))


def note_injection(agent, snapshot, step):
    run = agent.run_state
    if run.injected or agent.permissions is None:
        return
    visible = (" ".join(f"{element.label or ''} {element.value or ''}".split())
               for element in snapshot.elements
               if element.role not in ("TextField", "SecureTextField", "SearchField"))
    seen = next((text for text in (*visible, *run.passed, *(run.listing or ())) if INJECTION.search(text)), None)
    if seen is not None:
        # A transient read can steer the next action in the same model chunk.
        run.injected = True
        agent._emit({"event": "frontier_injection", "step": step, "text": agent._mask(seen)[:160]})

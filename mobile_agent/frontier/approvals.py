"""Asking the person before an action: the approval card, taps at a point and gestures, the two-headed check."""

import re
import time
from types import SimpleNamespace

from .. import contract_asks as proof_asks
from .. import contract_screen as proof_screen
from .. import offers
from ..agent_hooks import GuardVerdict
from ..task_policy import COMMIT_CONTROL, GUARD_WORDS, SHEET_OPENERS, commit_label, is_keypad_key, springboard_keypad
from ..wda.point_gestures import check_gesture_path, same_point_image
from .protocol import TYPED_OPS
from .safety import INJECTION_REASON, LIKE, PHONE_LOCKED, TAINT_REASON, UNKNOWN_REASON, GuardStop, _nobody, guard
from .screen import point_crop, screen_pairs

CLOSE_LABELS = frozenset({"cancel", "close"})
SAME_SHEET = .8
STOPPED_BEFORE_POINT = "This task stopped before the screen point was used."


class ApprovalsMixin:
    def _tap_xy(self, request, act, aliases, snapshot):
        name = " ".join((act.text or "").split())
        spot = act.target_label or act.target or ""
        found = re.search(r"(\d+(?:\.\d+)?)\s*%?\s*[,; ]\s*(\d+(?:\.\d+)?)", spot)
        if not name or found is None:
            return None, ("TAP_XY needs the point as x,y percent of the screen in target (e.g. \"52,91\") and the "
                          "control's name in text.")
        x, y = float(found.group(1)), float(found.group(2))
        if x > 1 or y > 1:
            x, y = x / 100, y / 100
        if not (0 < x < 1 and 0 < y < 1):
            return None, "TAP_XY's point must lie on the screen (0-100 percent each way)."
        listed = self._find(snapshot, name)
        alias = next((a for a, e in aliases.items() if e is listed), None) if listed is not None else None
        if alias is not None:
            return None, f"Refused: {name!r} is a listed element ({alias}): TAP it instead of a point."
        if listed is not None and not is_keypad_key("TAP", listed.role, listed.label):
            # Only an app keypad's key goes by its point: a point on an unlisted App Store Get would install unasked.
            return None, (f"Refused: {name!r} is an element on this screen: TAP it with target_label {name!r} "
                          "instead of a point.")
        for element in snapshot.elements:
            label = " ".join((element.label or "").split()[:GUARD_WORDS])
            if not COMMIT_CONTROL.search(label) or label.casefold() in SHEET_OPENERS:
                continue
            ex, ey, ew, eh = element.rect
            reach_y, reach_x = eh, eh * snapshot.height / snapshot.width
            if ex - reach_x <= x <= ex + ew + reach_x and ey - reach_y <= y <= ey + eh + reach_y:
                return None, (f"Refused: that point is within one control-height of {label!r}, which commits. "
                              "Tap a listed element, or a point clear of it.")
        kind, under = proof_asks.point_spot(snapshot, x, y)
        if kind == "commit":
            # Where the point is decides, not the name the model gave it.
            label = " ".join((under.label or under.role).split()[:GUARD_WORDS])
            return None, (f"Refused: that point is on {label!r}, which commits. TAP it by its id instead of a point.")
        # The model's name for the point can refuse it, never let it through.
        refused = guard(request, "TAP", SimpleNamespace(label=name, role="Button"))
        if refused:
            return None, refused
        return (x, y), None

    # None without the contract's ledger of screens: the gate then fails closed.
    def _code_origins(self, text):
        if self._contract is None:
            return None
        digits = re.sub(r"[\s-]", "", str(text or ""))
        if not digits.isdigit() or len(digits) > 10:
            return ()
        rows = []
        for _, _, _, originals in getattr(self._contract, "shown", ()):
            for original in originals.values():
                groups = (re.sub(r"\D", "", g) for g in re.findall(r"\d[\d \-]{2,14}\d", original))
                if any(4 <= len(g) <= 10 and digits in g for g in groups):
                    rows.append(original)
        return tuple(dict.fromkeys(rows))[-40:]

    # A point is bound to no listed element: its ask is forced, and no "Don't ask again" rule meets it.
    def _point_approval(self, point, snapshot, request, chain_typed, step, session=None, *, gesture=None):
        from ..permissions import DONT_ASK
        x, y = point
        asked = ((f"{gesture.kind.capitalize()} {gesture.label}? This may change something.", "other")
                 if gesture else proof_asks.point_ask(self._contract, request, snapshot, x, y))
        if asked is None:
            return None, 0.0, False
        title, act = asked
        title = self._mask(title)
        if gesture and self.permissions is not None:
            from ..permissions import action_from
            for location in gesture.points:
                _, actual = proof_asks.point_spot(snapshot, *location)
                spot = actual or SimpleNamespace(label="", role="Point", value="", locator="",
                                                  rect=(*location, .001, .001))
                decision = self.permissions.decide(action_from(
                    "TAP_XY", spot, self._masked(snapshot), app_name=self._app_name(snapshot) or ""),
                    existing_act="other", forced=True)
                if decision.decision == "block":
                    return f"blocked:{decision.reason or 'not allowed'}", 0.0, False
        asker = self.approve
        if session is not None:
            asker = asker or self.forced_approve
        elif asker is None and self.run_state.injected and self.permissions is not None:
            # A screen this run tried to steer Mobster: a spot asks in Full access too.
            asker = self.floor_approve or _nobody
            self.run_state.ask_reason = INJECTION_REASON
        elif asker is None and (self.permissions is None or getattr(self.permissions, "mode", None) == DONT_ASK):
            # Full access, or a run with no permission mode and no approver (a benchmark): nothing asks.
            return None, 0.0, False
        if asker is None:
            return ("refused:a tap at a spot that no listed control is, or while the task commits, needs the person's "
                    "OK, and nobody can approve it here. TAP a listed element instead", 0.0, False)
        w, h = .08, .04
        spot = SimpleNamespace(label="", role="Point", value="", locator="",
                               rect=(min(max(x - w / 2, 0), 1 - w), min(max(y - h / 2, 0), 1 - h), w, h))
        reason = None
        if self.permissions is not None:
            from ..permissions import action_from
            decision = self.permissions.decide(action_from(
                "TAP_XY", spot, self._masked(snapshot), app_name=self._app_name(snapshot) or ""), existing_act=act,
                forced=True)
            reason = decision.reason or None
        # The card shows the spot itself, secrets painted over as in the model's picture.
        crop = point_crop(self.driver, x, y, since=getattr(snapshot, "read_started", None),
                          blur=self._secret_blur(snapshot))
        named = proof_asks.point_target(snapshot, x, y)[1] == "on"
        verdict, waited = self._ask(asker, title, "other" if named or gesture else act,
                                    gesture.operation if gesture else "TAP_XY", spot, snapshot, None,
                                    chain_typed, step, None, reason=reason, crop=crop)
        return verdict, waited, True

    # A coordinate belongs to the picture the model saw, never to a tree that may have moved since.
    def point_screen(self, previous, image, points):
        if previous is None or image is None:
            raise ValueError("Read a screenshot before choosing screen points.")
        changed = "The screen changed. Read a fresh screenshot before choosing screen points."

        def read():
            if self._stopping():
                raise ValueError(STOPPED_BEFORE_POINT)
            snapshot = self.driver.observe(timeout=5)
            self._check_app(snapshot)
            if springboard_keypad(snapshot):
                raise GuardStop(GuardVerdict("stop", "phone_locked", PHONE_LOCKED))
            if (snapshot.bundle_id != previous.bundle_id
                    or snapshot.content_fingerprint != previous.content_fingerprint):
                raise ValueError(changed)
            return snapshot

        self._guard_check("resume")
        read()
        current = self._screenshot(previous)
        if current is None or not all(same_point_image(image, current, x * 100, y * 100) for x, y in points):
            raise ValueError(changed)
        return read()

    def gesture_screen(self, previous, image, gesture, request):
        fresh = self.point_screen(previous, image, gesture.points)
        check_gesture_path(fresh, gesture)
        for label in (gesture.start_label, gesture.end_label):
            if label is not None:
                reason = guard(request, "TAP", SimpleNamespace(label=label, role="Button"))
                if reason:
                    raise ValueError(reason)
        return fresh

    # One message, one approval: a Return approved in a composer that didn't send covers the Send after it, once.
    def _approval(self, gate, op, element, snapshot, text, chain_typed, step, asker=None, force=False, unknown=False):
        asker = asker or self.approve
        if element is None or asker is None:
            return None, 0.0
        item, payload, amount = (gate.item, gate.payload, gate.amount) if gate is not None else (None, None, None)
        if unknown:
            label = " ".join((element.label or "").split()[:GUARD_WORDS]) or op
            act = item.act if item is not None else None
            if self.permissions is not None:
                decision = self._gate(op, element, snapshot, text, payload, chain_typed, act or "other", True)
                act = act or decision.act
            return self._ask(asker, f"{label} again?", act or "other", op, element, snapshot, payload, chain_typed,
                             step, item, text=text, reason=UNKNOWN_REASON)
        if force and (self._contract is None or not proof_asks.asks(item)):
            label = " ".join((element.label or "").split()[:GUARD_WORDS]) or op
            title, act = f"{label}?", (item.act if item is not None else "other")
            if self.permissions is not None:
                self._gate(op, element, snapshot, text, payload, chain_typed, act, True)
            if payload is None and op in TYPED_OPS:
                payload = text
            return self._ask(asker, title, act, op, element, snapshot, payload, chain_typed, step, item)
        title = act = party = None
        seen = False
        if self._contract is not None and proof_asks.asks(item):
            app_name = self._app_name(snapshot)
            shown = (proof_screen.visible_recipient(snapshot, app_name, element)
                     if item.act in proof_screen.MESSAGE_ACTS else None)
            approved = self.run_state.approved_send
            if (approved is not None and not approved["used"] and approved["item"] == item.id
                    and approved["payload"] == proof_screen.plain(payload or (chain_typed[-1] if chain_typed else ""))
                    and approved["thread"] == proof_screen.screen_title(snapshot) and approved["app"] == app_name
                    and approved["typed"] == len(self._contract.typed) and approved["receipts"] == len(item.receipts)):
                approved["used"] = True
                self.run_state.asked = "approved"
                self._emit({"event": "frontier_approval", "step": step, "act": item.act, "answer": "approved",
                            "reused": True})
                return None, 0.0
            title, act = proof_asks.approval_title(item, amount, shown=shown), item.act
            seen = bool(shown) and not proof_screen.internal_name(shown)
            sending = payload or (chain_typed[-1] if chain_typed else "") or (text if op == "TYPE_SUBMIT" else "")
            if item.act in proof_screen.MESSAGE_ACTS and sending and self._contract.screen_text_in(sending):
                # Text copied from the screen into a message asks whatever the person's rules say.
                force = True
                self.run_state.ask_reason = self.run_state.ask_reason or TAINT_REASON
            party = (shown if seen else None) or proof_screen.recipient(item)
        elif self._contract is None:
            label = " ".join((element.label or "").split()[:GUARD_WORDS])
            if (op in ("TAP", "LONG_PRESS") and COMMIT_CONTROL.search(label) and label.casefold() not in SHEET_OPENERS
                    and not proof_screen.is_opener(label) and commit_label(element.label, element.role) is not None):
                title, act = f"Tap {label!r}?", "other"
        reason = decision = None
        if self.permissions is not None:
            decision = self._gate(op, element, snapshot, text, payload, chain_typed, act, force)
            if decision.decision == "allow":
                return None, 0.0
            reason = decision.reason or None
            if title is None:
                title, act = decision.title, decision.act or "other"
        elif title is None:
            return None, 0.0
        if act in offers.MONEY_ACTS:
            reason = offers.reason(self._masked(snapshot), element.label, reason)
        remember = (self.permissions.remember(decision, getattr(snapshot, "bundle_id", "") or "",
                                              self._app_name(snapshot) or "") if decision is not None else None)
        return self._ask(asker, title, act, op, element, snapshot, payload, chain_typed, step, item, text=text,
                         reason=reason, remember=remember, recipient=party, recipient_seen=seen)

    def _gate(self, op, element, snapshot, text, payload, chain_typed, act, forced):
        from ..permissions import action_from
        return self.permissions.decide(action_from(
            op, element, self._masked(snapshot), app_name=self._app_name(snapshot) or "",
            text=text if op in TYPED_OPS else None,
            origins=self._code_origins(text or payload or (chain_typed[-1] if chain_typed else ""))),
            existing_act=act, forced=forced)

    def _ask(self, asker, title, act, op, element, snapshot, payload, chain_typed, step, item, text=None, reason=None,
             remember=None, recipient=None, crop=None, recipient_seen=False):
        if payload is None:
            payload = text if op in TYPED_OPS else (chain_typed[-1] if chain_typed else None)
        self.run_state.cards += 1
        if self.run_state.ask_reason:
            reason, self.run_state.ask_reason = self.run_state.ask_reason, None
        x, y, w, h = element.rect
        if crop is None and op != "TAP_XY" and asker is not _nobody:
            crop = point_crop(self.driver, x + w / 2, y + h / 2, since=getattr(snapshot, "read_started", None),
                              blur=self._secret_blur(snapshot))
        request_ = {"kind": "commit", "step": step, "operation": op, "label": (element.label or "")[:200],
                    "role": element.role, "text": payload, "title": title, "act": act,
                    "app_name": self._app_name(snapshot) or None, "reason": reason, "remember": remember,
                    "target": {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}}
        if self.run_state.acting_as:
            request_["acting_as"] = self.run_state.acting_as
        if self.run_state.media_checked is not None:
            request_["media_checked"] = self.run_state.media_checked
        if recipient:
            # Who it goes to is never journaled: the title already names them there.
            request_["recipient"] = recipient
            # Only a recipient the screen showed lets a review queue's Approve stand for this card.
            request_["recipient_seen"] = bool(recipient_seen)
        if crop:
            request_["crop"] = crop
        waited = time.monotonic()
        answer = asker(request_)
        waited = time.monotonic() - waited
        if answer == "approved":
            self.run_state.asked = "approved"
        elif answer == "denied":
            self.run_state.declined_on = snapshot
        self._emit({"event": "frontier_approval", "step": step, "act": act,
                    "answer": answer.split(":", 1)[0] if isinstance(answer, str) else None})
        if (answer == "approved" and self._contract is not None and item is not None and op in ("SUBMIT", "TYPE_SUBMIT")
                and item.act in proof_screen.MESSAGE_ACTS and payload):
            self.run_state.approved_send = {"item": item.id, "payload": proof_screen.plain(payload), "used": False,
                                   "thread": proof_screen.screen_title(snapshot), "app": self._app_name(snapshot),
                                   "typed": len(self._contract.typed) + (1 if op == "TYPE_SUBMIT" else 0),
                                   "receipts": len(item.receipts)}
        return (None if answer == "approved" else str(answer or "denied")), waited

    def _asks_mode(self):
        """Whether this run asks before acting: a mode that asks (not Full access) or a project policy that can ask."""
        from ..permissions import DONT_ASK
        gate = self.permissions
        narrowed = getattr(gate, "narrowed", False) is True
        return gate is not None and (getattr(gate, "mode", None) != DONT_ASK or narrowed)

    def _is_commit(self, gate, op, element, snapshot, text):
        if element is None:
            return False
        if gate is not None and gate.item is not None and proof_asks.asks(gate.item):
            return True
        if self.permissions is None:
            return False
        from ..permissions import action_from
        try:
            kind, _, _ = self.permissions.reading(action_from(
                op, element, self._masked(snapshot), app_name=self._app_name(snapshot) or "",
                text=text if op in TYPED_OPS else None), None)
        except Exception:  # unsure: a commit
            return True
        return kind == "commit"

    @staticmethod
    def _out_of_scope(op, element, request):
        from ..permissions.consequence import names_switch
        return (op == "TAP" and element is not None and element.role in ("Switch", "Toggle")
                and not names_switch(request, element.label))

    # Pixels that can't be read leave the tree's reading standing.
    def _two_heads(self, asker, op, element, snapshot, chain_typed, step):
        from ..permissions.consequence import label_act
        from ..permissions.rules import norm
        try:
            shown = self.region_reader(self.driver, element, snapshot)
        except Exception:
            shown = None
        shown = " ".join(str(shown or "").split())
        if not shown or norm(shown) == norm(element.label):
            return None, 0.0
        act = label_act(shown, "Button")
        if act is None or label_act(element.label, element.role) == act:
            return None, 0.0
        self._emit({"event": "frontier_two_heads", "step": step, "act": act})
        self.run_state.ask_reason = (
            f"The screen shows “{shown[:30]}” here; the app calls it “{(element.label or element.role)[:30]}”")
        return self._ask(asker, f"Tap “{shown[:40]}”?", act, op, element, snapshot, None, chain_typed, step, None)

    # One tap, journaled before it is sent and never repeated, only on the screen the person said no on.
    def _close_declined_sheet(self):
        declined, self.run_state.declined_on = self.run_state.declined_on, None
        if declined is None or self._stopping():
            return
        try:
            now = self._read_once()
        except Exception:
            return
        before, after = screen_pairs(declined), screen_pairs(now)
        if not before or not after or len(before & after) / len(before | after) < SAME_SHEET:
            return
        closers = [e for e in now.elements if e.role == "Button" and " ".join((e.label or "").split()).casefold()
                   in CLOSE_LABELS]
        if len(closers) != 1:
            return
        closer = closers[0]
        self._emit({"event": "frontier_cleanup", "operation": "TAP", "label": closer.label[:40], "state": "sending",
                    "reason": "close_declined_sheet"})
        try:
            self.driver.execute("TAP", closer, now, timeout=10)
        except Exception as error:
            self._emit({"event": "frontier_cleanup", "operation": "TAP", "label": closer.label[:40], "state": "failed",
                        "detail": type(error).__name__})
            return
        self._emit({"event": "frontier_cleanup", "operation": "TAP", "label": closer.label[:40], "state": "sent"})

    def _long_asks(self, gate, op, element):
        if self._contract is not None:
            return gate is not None and gate.item is not None and proof_asks.asks(gate.item)
        if op not in ("TAP", "LONG_PRESS", "SUBMIT", "TYPE_SUBMIT"):
            return False
        label = " ".join((element.label or "").split())
        return bool(COMMIT_CONTROL.search(label) or LIKE.search(label))

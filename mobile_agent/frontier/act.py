"""Acting on the phone: one action through the driver, the list macros, typed text and finding targets."""

import time

from ..state import ADJUSTABLE_ROLES, TEXT_OPERATIONS, typing_timeout
from ..task_policy import same_words
from ..wda.guards import DriverRejection
from ..wda.point_gestures import GESTURE_OPS
from .protocol import TYPED_OPS
from .safety import _commits, consequential
from .screen import editable_in, looks_like_field, receipt_for, text_difference

MACRO_SWIPES = 8
SCROLL_TO_SEARCHABLE_SWIPES = 3
READ_LIST_SECONDS = 8.0
READ_LIST_ROWS = 60
MACRO_PAUSE_SECONDS = .35
# After a glide (no momentum) the list is still at release.
MACRO_GLIDE_PAUSE_SECONDS = .1
# A tap at the status bar scrolls to top instead of closing a menu, so each DISMISS tries the next free point.
DISMISS_POINTS = ((.04, .5), (.5, .7), (.96, .5), (.5, .97), (.04, .2), (.96, .2), (.5, .035))
# Actions that leave the screen: before one, a value the turn recorded under a bottom bar is scrolled into view.
LEAVING_OPS = frozenset({"LAUNCH_APP", "HOME", "OPEN_URL"})


def is_address_field(element):
    label = " ".join((getattr(element, "label", "") or "").split()).casefold()
    placeholder = " ".join((getattr(element, "placeholder", "") or "").split()).casefold()
    return bool(getattr(element, "editable", False)) and (
        label in ("address", "url") or "website" in placeholder or "enter website" in label)


# A tap soon after a search's Return waits for two agreeing reads: results still arriving move under it.
SUBMIT_WINDOW_SECONDS = 4.0
SUBMIT_SETTLE_GAP = .35
SUBMIT_SETTLE_SECONDS = 3.0


# Never one a tap could commit: the model chooses those.
def opens_field(element, snapshot):
    return (element is not None and not element.editable and element.role not in ADJUSTABLE_ROLES
            and (looks_like_field(element) or element.role == "Button"
                 and " ".join((element.label or "").split()).casefold() == "search")
            and editable_in(snapshot, element) is None and not consequential("TAP", element, snapshot))


# A chained tap on a tile's name goes to the tile: on the name, Files opens a rename.
CONTAINER_ROLES = frozenset({"Cell", "Button"})
TEXT_ROLES = frozenset({"StaticText", "Text", "Other", "Image", "Icon", "NavigationBar"})


def _container(snapshot, element, want):
    if element is None or element.role != "StaticText":
        return element
    x, y = element.center
    for other in snapshot.elements:
        if other is element or other.role not in CONTAINER_ROLES:
            continue
        label = " ".join((other.label or "").split()).casefold()
        ox, oy, ow, oh = other.rect
        if (label.startswith(want) and len(label) > len(want) and label[len(want)] in ",: ("
                and ox <= x <= ox + ow and oy <= y <= oy + oh):
            return other
    return element


class ActMixin:
    def _read_list(self, snapshot, needle=None):
        from ..state import redundant_ids
        lines, seen, current = [], set(), snapshot
        want = " ".join(str(needle or "").split()).casefold()
        found = []
        begun, last_swipe = time.monotonic(), 0.0
        app = getattr(snapshot, "bundle_id", None)

        def texts(screen):
            masked = self._masked(screen)
            skip = redundant_ids(masked.public().get("elements") or ())
            for element in masked.elements:
                if element.editable or element.id in skip:
                    continue
                value = element.value if element.value and element.value != element.label else ""
                text = " ".join(f"{element.label or ''} {value}".split())
                if text:
                    yield text

        if self.run_state.list_read and self.run_state.list_read[0] == app and (
                self.run_state.list_read[1] == snapshot.content_fingerprint
                or set(texts(snapshot)) <= self.run_state.list_read[2]):
            # Read afresh, the same screen filled the cap again and the list never moved.
            seen = set(self.run_state.list_read[2])
        self.run_state.list_read = None

        def harvest(screen):
            new = 0
            for text in texts(screen):
                if text not in seen:
                    seen.add(text)
                    lines.append(text[:300])
                    new += 1
                    if want and want in text.casefold():
                        found.append(text)
            return new
        ended = False
        for swipe in range(MACRO_SWIPES + 1):
            new = harvest(current)
            ended = bool(swipe and not new)
            if (found or len(lines) >= READ_LIST_ROWS or swipe == MACRO_SWIPES or ended or self._stopping()
                    or time.monotonic() - begun + last_swipe > READ_LIST_SECONDS):
                break
            t_swipe = time.monotonic()
            self._drag_next()  # XCTest's drag even where SWIPE_UP glides
            self.driver.execute("SWIPE_UP", None, current, timeout=10)
            current = self._macro_read()
            last_swipe = time.monotonic() - t_swipe
        if found:
            self.run_state.macro_feedback = f"READ_LIST stopped at the first row containing {needle!r}."
        elif not ended and (len(lines) >= READ_LIST_ROWS or time.monotonic() - begun + last_swipe > READ_LIST_SECONDS):
            self.run_state.macro_feedback = (f"READ_LIST stopped after {min(len(lines), READ_LIST_ROWS)} rows and "
                                    f"{time.monotonic() - begun:.0f} s, before the end of the list. If you need more, "
                                    "READ_LIST again from here; if the answer is in these rows, use them.")
            self.run_state.list_read = (app, current.content_fingerprint,
                                        (seen - set(lines)) | set(lines[:READ_LIST_ROWS]))
        return lines[:READ_LIST_ROWS]

    def _drag_next(self):
        drag_next = getattr(self.driver, "drag_next", None)
        return bool(callable(drag_next) and drag_next())

    def _macro_read(self):
        glided = getattr(self.driver, "last_swipe_glided", False) is True
        time.sleep(MACRO_GLIDE_PAUSE_SECONDS if glided else MACRO_PAUSE_SECONDS)
        return self._read_once()

    def _scroll_to(self, snapshot, text):
        want, current = " ".join(text.split()).casefold(), snapshot

        def shown(screen):
            return any(want in " ".join((e.label or "").split()).casefold() and .08 < e.center[1] < .9
                       for e in screen.elements)
        search = next((e for e in snapshot.elements if e.role == "SearchField"), None)
        for swipe in range(MACRO_SWIPES):
            if shown(current):
                return True
            if self._stopping():
                break
            if search is not None and swipe >= SCROLL_TO_SEARCHABLE_SWIPES:
                # A list with its own search field: a name further down is found faster by searching.
                label = (search.label or search.value or "Search")[:40]
                self.run_state.macro_feedback = (
                    f"SCROLL_TO: {text!r} is not in the first {swipe + 1} screens of this list, "
                    f"and the list has a search field ({label!r}): search for it there.")
                return False
            self.driver.execute("SWIPE_UP", None, current, timeout=10)
            after = self._macro_read()
            if after.content_fingerprint == current.content_fingerprint and self._drag_next():
                self.driver.execute("SWIPE_UP", None, current, timeout=10)  # as in _read_list: once, a drag
                after = self._macro_read()
            if after.content_fingerprint == current.content_fingerprint:
                break
            current = after
        return shown(current)

    def _reveal_found(self, state):
        """One code swipe up, once a turn, before the turn leaves its screen or finishes, when a value it recorded sits
        only in rows a bottom bar covers or the screen's foot cuts (Contract.hidden_row), so the run's frames show it.
        Journaled before it is sent; no model call. iOSWorld dev, 22 Oct: +0.074 rubric score over 34 paired runs."""
        if self._contract is None or self.run_state.revealed == state.step or self._stopping():
            return
        self.run_state.revealed = state.step
        row = self._contract.hidden_row(self._masked(state.snapshot).elements, state.step)
        if row is None:
            return
        event = {"event": "frontier_cleanup", "step": state.step, "operation": "SWIPE_UP",
                 "label": self._mask(row.label)[:40], "reason": "reveal_value"}
        t_reveal, before = time.monotonic(), state.snapshot
        self._emit({**event, "state": "sending"})
        try:
            self._drag_next()  # XCTest's drag, a measured half screen: a glide's 0.68 can carry the row under a top bar
            self.driver.execute("SWIPE_UP", None, before, timeout=10)
        except Exception as error:
            self._emit({**event, "state": "failed", "detail": type(error).__name__})
            state.snapshot, state.unproven = self._observe(), False  # it may have moved: never sent again
            return
        state.snapshot, state.unproven = self._settle(before), False
        self._spend("reveal", t_reveal)
        self._emit({**event, "state": "sent"})
        self._contract.observe(self._masked(state.snapshot), self._app_name(state.snapshot), state.step)

    def _exact_text(self):
        return callable(getattr(self.driver, "write_text", None))

    def _write(self, op, element, text, snapshot):
        # A search, an address and a submit into text this task didn't write replace what the field holds.
        append = op in TEXT_OPERATIONS and element.role != "SearchField" and not is_address_field(element) and not (
            op == "TYPE_SUBMIT" and self._foreign_text(element))
        receipt = self.driver.write_text(element, snapshot, text or "", append=append, submit=op == "TYPE_SUBMIT",
                                         timeout=typing_timeout(text) + 20)
        self.run_state.receipts[element.locator or element.label] = receipt
        self.run_state.last_receipt = receipt
        if receipt.get("value") is not None and not receipt.get("matches"):
            self.run_state.macro_feedback = (
                "The field does not hold what you wrote ("
                + ("it was cleared and written once more" if receipt.get("repaired") else
                   "what it held before was kept, and nothing was rewritten: the app may have formatted it")
                + "): " + text_difference(receipt["value"], receipt.get("expected") or "")
                + ". Do not retype blindly: see the field's status on this screen.")
        if op == "TYPE_SUBMIT" and receipt.get("submitted") is False:
            said = self.run_state.macro_feedback
            self.run_state.macro_feedback = ((said + " ") if said else "") + (
                "Return was not pressed, because the field does not hold exactly what you wrote: fix it (SET_TEXT "
                "replaces the field's text), then SUBMIT.")

    def _appends(self, op, element):
        if not self._exact_text():
            return True
        return element.role != "SearchField" and not is_address_field(element) and not (
            op == "TYPE_SUBMIT" and self._foreign_text(element))

    def _foreign_text(self, element):
        shown = "" if element.placeholder else (element.value or "")
        if not shown.strip() or shown.strip() == (element.label or "").strip():
            return False
        return receipt_for(self.run_state.receipts, element) is None

    def _gesture(self, gesture, snapshot):
        send = getattr(self.driver, gesture.kind + "_point", None)
        if not callable(send):
            raise DriverRejection("unsupported_or_stale_target")
        options = {"duration": gesture.duration, "timeout": 15 if gesture.kind == "hold" else 10}
        if gesture.kind == "drag":
            options["hold_seconds"] = gesture.hold_seconds
        send(*(coordinate for location in gesture.points for coordinate in location), snapshot, **options)

    # Fails closed: unsure means no tap.
    def _still_after_submit(self, element):
        deadline = time.monotonic() + SUBMIT_SETTLE_SECONDS
        previous = self._read_once()
        while True:
            time.sleep(SUBMIT_SETTLE_GAP)
            current = self._read_once()
            same = current.content_fingerprint == previous.content_fingerprint
            if same and element is not None:
                before = self._find(previous, element.label, element.role, element.locator)
                after = self._find(current, element.label, element.role, element.locator)
                if before is None and after is None:
                    return current, None
                if before is not None and after is not None and tuple(before.rect) == tuple(after.rect):
                    return current, after
            elif same:
                return current, None
            if time.monotonic() >= deadline:
                return None, None
            previous = current

    def _execute(self, op, element, app, text, snapshot, point=None):
        if op != "READ_LIST":
            self.run_state.list_read = None
        if op == "TAP_XY":
            self.driver.tap_point(point[0], point[1], snapshot, timeout=10, purpose="tap_xy")
        elif op in GESTURE_OPS:
            self._gesture(point, snapshot)
        elif op == "LAUNCH_APP":
            # A start reset's reopen, in the turn that chose it only: never a later LAUNCH_APP.
            fresh, self.run_state.fresh_launch = self.run_state.fresh_launch == (app, self.run_state.calls), None
            relaunch = getattr(self.driver, "relaunch_app", None)
            if not (fresh and callable(relaunch) and relaunch(app, timeout=40)):
                self.driver.execute("LAUNCH_APP", app, snapshot, timeout=20)
        elif op in ("SWIPE_LEFT", "SWIPE_RIGHT") and element is not None:
            self.driver.execute(op, element, snapshot, timeout=10)  # through the targeted row (B7)
        elif op == "HOME" or op.startswith("SWIPE_"):
            self.driver.execute(op, None, snapshot, timeout=10)
        elif op == "LONG_PRESS":
            self.driver.long_press(element, snapshot, timeout=10)
        elif op == "READ_LIST":
            self.run_state.listing = self._read_list(snapshot, text)
        elif op == "SCROLL_TO":
            if not self._scroll_to(snapshot, text) and not self.run_state.macro_feedback:
                self.run_state.macro_feedback = (
                    f"SCROLL_TO found no item containing {text!r} down to the end of the list.")
        elif op == "DISMISS":
            free = [(px, py) for px, py in DISMISS_POINTS if not any(
                ex <= px <= ex + ew and ey <= py <= ey + eh for ex, ey, ew, eh in
                (e.rect for e in snapshot.elements))] or list(DISMISS_POINTS)
            screen, tries = self.run_state.dismissed
            tries = tries + 1 if screen == snapshot.content_fingerprint else 0
            self.run_state.dismissed = (snapshot.content_fingerprint, tries)
            x, y = free[tries % len(free)]
            # A harness that checks actions sees a DISMISS; a phone driver taps the point.
            tap = getattr(self.driver, "dismiss_at", None)
            (tap if callable(tap) else self.driver.tap_point)(x, y, snapshot, timeout=10)
        elif op == "SET_TEXT" and element.role in ADJUSTABLE_ROLES:
            self.driver.set_value(element, text, timeout=30)  # a wheel may be stepped item by item
        elif op in TYPED_OPS and self._exact_text():
            self._write(op, element, text, snapshot)
        elif op == "SET_TEXT" and not text:
            clear = getattr(self.driver, "clear_text", None)
            if not callable(clear):
                raise ValueError("this driver cannot empty a field")
            clear(element, timeout=10)
        elif op == "SET_TEXT" and callable(getattr(self.driver, "replace_text", None)) and self.driver.replace_text(
                element, text, timeout=typing_timeout(text)):
            pass  # cleared and typed through one element reference, no read between
        elif op == "SET_TEXT":
            clear = getattr(self.driver, "clear_text", None)
            if callable(clear):
                clear(element, timeout=10)
                # One read: the field is empty when the clear returns.
                try:
                    snapshot = self.driver.observe(timeout=15)
                except Exception:
                    snapshot = self._observe()
                element = next((e for e in snapshot.elements if e.locator == element.locator), element)
            self.driver.execute("TYPE", element, snapshot, text=text, timeout=typing_timeout(text))
        elif op == "SUBMIT" and "SUBMIT" not in element.actions:
            # Return needs a focused field.
            self.driver.execute("TAP", element, snapshot, timeout=10)
            snapshot = self._observe()
            element = self._find(snapshot, element.label, element.role, element.locator) or element
            self.driver.execute("SUBMIT", element, snapshot, timeout=10)
        else:
            self.driver.execute(op, element, snapshot, text=text, timeout=typing_timeout(text))
        return snapshot

    @staticmethod
    def _find(snapshot, label, role=None, locator=None):
        want = " ".join((label or "").split()).casefold()
        if not want or not want.strip():
            # A labelled element is found by its label: paths shift as the screen changes (splitpay-001).
            if locator:
                return next((e for e in snapshot.elements if e.locator == locator
                             and (role is None or e.role == role)), None)
            return None
        exact = [e for e in snapshot.elements if " ".join((e.label or "").split()).casefold() == want]
        if len(exact) > 1 and locator:
            same = [e for e in exact if e.locator == locator]
            if same:
                return same[0]
        # An unlabelled field is named by its placeholder value ("Search restaurants").
        norm = [(e, " ".join((e.label or (e.value if e.editable else "") or "").split()).casefold())
                for e in snapshot.elements]
        found = [e for e, text in norm if text == want]
        if not found:
            # The model names later targets from memory ("Save" for "Save Note"): a unique partial match is it.
            found = [e for e, text in norm if text and len(want) >= 3 and (want in text or text in want)]
            if len(found) > 1:
                found = [e for e in found if role and e.role == role] if role else []
            if len(found) != 1:
                return None
        if role is None and len(found) > 1:
            # By label alone a control beats text that reads the same: Calculator's display shows the 4 just typed.
            controls = [e for e in found if e.role not in TEXT_ROLES]
            found = controls or found
        return next((e for e in found if role and e.role == role), found[0] if found else None)

    def _target(self, act, index, aliases, snapshot, after=None):
        element = None
        if act.locator:
            # The Return of a split TYPE_SUBMIT goes to the very field just typed into.
            element = self._find(snapshot, act.target_label, act.role, act.locator)
            if element is not None:
                return element
        if act.target in aliases:
            # Typing changes the screen: later actions re-find an id by its path or label.
            element = aliases[act.target]
            element = self._find(snapshot, element.label, element.role, element.locator) if index else element
        if element is None and act.target_label:
            element = self._find(snapshot, act.target_label)
            if act.op == "TAP":
                element = _container(snapshot, element, " ".join(act.target_label.split()).casefold())
            if element is not None and not same_words(element.label, act.target_label) and _commits(element):
                # A commit is found by its exact label only: "Delete" never finds "Delete Account".
                element = None
        op = act.op
        if element is None and index and (after == "TAP" and op in TYPED_OPS | {"SUBMIT"}
                                          or after in TYPED_OPS and op == "SUBMIT"):
            # A tap that opened a search screen leaves one field, under another name: the typing goes there.
            fields = [e for e in snapshot.elements if e.editable and (op != "SUBMIT" or "SUBMIT" in e.actions)]
            element = fields[0] if len(fields) == 1 else None
        return element

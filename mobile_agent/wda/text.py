"""WebDriverAgent text entry."""

import base64
import re
import time
import unicodedata
from urllib.parse import quote

from ..state import truncated_field_value, validate_input_text
from ..transport import Deadline, NotSent, TransportError
from .guards import DriverRejection, finite_positive, grounded_target
from .observe import WDA_FRESH_SECONDS, WDA_READ_MARGIN_SECONDS, element_id

# Apps where a message composer sends on Return: a line break typed there as a key would send the text before it.
CHAT_APPS = frozenset({
    "com.apple.MobileSMS", "net.whatsapp.WhatsApp", "com.facebook.Messenger", "ph.telegra.Telegraph",
    "org.whispersystems.signal", "com.tinyspeck.chatlyio", "com.hammerandchisel.discord", "com.burbn.instagram",
    "com.atebits.Tweetie2", "com.linkedin.LinkedIn", "com.toyopagroup.picaboo", "jp.naver.line", "com.tencent.xin",
    "com.microsoft.skype.teams", "com.google.chat", "com.cardify.tinder", "com.hinge.app", "com.bumble.app"})
CHAT_FIELD = re.compile(r"\b(?:message|imessage|text message|chat|reply|comment|write a|say something|send)\b|"
                        r"(?:\.\.\.|…)$", re.I)
# Focusing a field raises the keyboard within ~0.5 s on device.
WDA_KEYBOARD_WAIT_SECONDS = 2.0
# With a hardware keyboard known, the focusing tap's keyboard is read this long after the tap
# (not for WDA_KEYBOARD_WAIT_SECONDS / 2 of reads: 1-2 reads of ~1 s on a long simulator list).
WDA_PARKED_FOCUS_SECONDS = .25
# Longest text a write takes (a document body); typing keeps validate_input_text's limits.
WDA_WRITE_MAX_CHARS = 20000
# The press that opens the edit menu (Paste), and how long its menu is looked for.
WDA_PASTE_PRESS_MS = 600
WDA_PASTE_MENU_SECONDS = 3.0

# Smart punctuation a keyboard may type for plain ASCII (a typed read-back is compared without it).
_TYPOGRAPHIC = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "\u00a0": " "})


class NoKeyboardFocus(TransportError):
    """A focusing tap was made and no keyboard came up: nothing was typed."""


class WheelNotSet(ValueError):
    """A picker wheel didn't reach the value it was given; it is left where it shows (the message says what)."""


# A picker wheel's value: the spellings tried (WDA.set_value) and the steps toward it, read after each.
WHEEL_SPELLINGS = 6
WHEEL_STEPS = 40
_WHEEL_NUMBER = re.compile(r"^\s*(\d+)")


def wheel_number(text):
    """The number a wheel's value or a wanted item starts with ("25 min" -> 25, "05" -> 5), else None."""
    found = _WHEEL_NUMBER.match(text or "")
    return int(found.group(1)) if found else None


def wheel_shows(shown, wanted):
    """Whether a wheel showing ``shown`` is at ``wanted``."""
    if not isinstance(shown, str) or not shown.strip() or not wanted:
        return False
    a, b = wheel_number(shown), wheel_number(wanted)
    if a is not None or b is not None:
        return a == b
    shown_words, wanted_words = shown.casefold().split(), wanted.casefold().split()
    return shown_words == wanted_words or shown_words[:1] == wanted_words[:1]


def wheel_items(wanted, shown):
    """The spellings of ``wanted`` a wheel may list, given the value it shows."""
    out = [wanted]
    bare = re.match(r"(\d+|AM|PM)\b", wanted, re.IGNORECASE)
    if bare:
        out.append(bare.group(1))
    number = wheel_number(wanted)
    if number is not None:
        out += [str(number), f"{number:02d}"]
        unit = " ".join((shown or "").split()[1:]) if wheel_number(shown) is not None else ""
        if unit:
            out += [f"{number} {unit}", f"{number:02d} {unit}"]
            if unit.isalpha():  # "1 hour" on a wheel at "0 hours"
                out.append(f"{number} {unit[:-1] if unit.endswith('s') else unit + 's'}")
    seen, items = set(), []
    for item in out:
        if item and item not in seen:
            seen.add(item)
            items.append(item)
    return items


class FieldNotCleared(TransportError):
    """A field known to hold text wasn't shown to be emptied. Typing now could add to it, so the write stops here."""


class WriteInterrupted(TransportError):
    """A compound write stopped after a request that can change the field. Its last request's answer says nothing about
    the ones before it, so the whole write is unknown, however it ended. Connection recovery doesn't restart it, and a
    further attempt the model asks for goes through the same recheck as any unknown outcome."""


class WDAText:
    """The text part of WDA."""
    _write_sent = False     # a request of this write that can change the field may have reached WDA
    # (WDA url, bundle) where a long press offered no Paste: later writes there type at once (_paste).
    _paste_missing = set()
    # WDA URLs whose keyboard stayed parked after a focusing tap (hardware-keyboard mode): no
    # software keyboard will animate in, so waiting for one is skipped. A visible one clears it.
    _parked_keyboards = set()

    def _sends(self, request, *args, **kwargs):
        """A request that can change a field. Only NotSent proves it never left."""
        try:
            answer = request(*args, **kwargs)
        except NotSent:
            raise
        except Exception:
            self._write_sent = True
            raise
        self._write_sent = True
        return answer

    def _unique_element(self, role, name, deadline):
        """WDA's id for the one ``role`` element named ``name``, else None, so the caller types another way."""
        name = name.replace("\\", "\\\\").replace("'", "\\'")
        # ``name`` is WDA's identifier-or-label. ``identifier`` is not a predicate attribute: WDA 16.12
        # rejected the query, so until 24 Sep this path never ran and every field was focused by a tap.
        try:
            found = self.call("POST", "/elements", {"using": "predicate string", "value":
                              f"type == 'XCUIElementType{role}' AND (name == '{name}' OR label == '{name}')"},
                              min(5, deadline.remaining()))
        except TransportError:
            return None
        return element_id(found[0]) if isinstance(found, list) and len(found) == 1 else None

    def _type_text(self, operation, target, snapshot, text, deadline, multiline=None):
        """TYPE / TYPE_SUBMIT into ``target``."""
        if multiline is None:
            multiline = target.role == "TextView"
        grounded_target(operation, target, snapshot)
        validate_input_text(text, multiline=multiline and operation == "TYPE")
        self._check_current(snapshot, deadline)
        focused = self._clear_stale_query(target, deadline)
        keyboard = getattr(snapshot, "keyboard", "") or ("visible" if "SUBMIT" in target.actions else "")
        if focused:
            # The clear left keyboard focus in the field: no focusing tap, no keyboard wait.
            keyboard = "visible"
        elif keyboard == "visible" and self._focused_elsewhere(target, snapshot, deadline):
            # The keyboard belongs to another field: keystrokes would go there (iOSWorld mem-048,
            # 24 Sep: a whole note body typed into its title). Focus has to move first.
            keyboard = ""
        # A visible keyboard keeps the one-call path; otherwise focus must move, and the element
        # itself moves it reliably (a coordinate tap left mem-041's focus in the title).
        if keyboard != "visible" and self._type_into_element(target, text, deadline,
                                                             submit=operation == "TYPE_SUBMIT", multiline=multiline):
            return None
        if keyboard != "visible":
            # No keyboard yet: focus the field with a tap. Many iOS fields
            # (Safari's address bar, search bars) swap in a different
            # editing field on focus, so an element-bound type fails as
            # "stale element"; keystrokes go to whatever holds focus.
            x, y = target.center
            if multiline:
                # A text view's centre puts the cursor mid-text and keystrokes insert there;
                # its last line puts it at the end, where added text belongs.
                left, top, w, h = target.rect
                x, y = left + w * .9, top + h * .92
            self._sends(self._tap, x * snapshot.width, y * snapshot.height, deadline)
            if not self._await_keyboard(deadline):
                raise NoKeyboardFocus("The field did not take keyboard focus; nothing was typed")
        self._dispatching()
        # Return is sent by the driver, never carried in model-generated text.
        keys = [text, "\n"] if operation == "TYPE_SUBMIT" else [text]
        self._frame_mark(operation)
        self._sends(self.call, "POST", "/wda/keys", {"value": keys}, deadline.remaining())
        self._frame_dispatched()

    def _clear_stale_query(self, target, deadline):
        """Empty a search field holding an earlier query; only search fields, as others may hold the person's text."""
        return target.role == "SearchField" and self._clear_field(target, deadline)

    def _focused_elsewhere(self, target, snapshot, deadline):
        """True when WDA reports keyboard focus outside ``target``'s frame; unknown counts as not elsewhere."""
        if sum(e.editable for e in snapshot.elements) < 2:
            return False
        try:
            identifier = element_id(self.call("GET", "/element/active", None, min(2, deadline.remaining())) or {})
            if not identifier:
                return False
            rect = self.call("GET", f"/element/{quote(identifier, safe='')}/rect", None, min(2, deadline.remaining()))
            cx = (rect["x"] + rect["width"] / 2) / snapshot.width
            cy = (rect["y"] + rect["height"] / 2) / snapshot.height
        except (TransportError, TimeoutError, KeyError, TypeError, ZeroDivisionError):
            return False
        x, y, w, h = target.rect
        return not (x - .01 <= cx <= x + w + .01 and y - .01 <= cy <= y + h + .01)

    def _type_into_element(self, target, text, deadline, submit=False, multiline=False):
        """Type through the element itself when it is unique; False: nothing was typed and the caller uses its own path.
        A focusing tap by coordinates can leave focus in the last field: mem-041 typed a note body into its title."""
        filled = (target.value or "").strip() and (target.value or "").strip() != (target.label or "").strip()
        # A multi-line text field (SwiftUI's vertical TextField) is a text view here: CloudSlides' body
        # took the typed text mid-way (cloudslides-002, 4 of 4 runs, 25 Sep).
        if target.role not in ("TextField", "TextView") or (target.role == "TextView" or multiline) and filled:
            return False
        identifier = target.label and self._unique_element(target.role, target.label, deadline)
        if not identifier:
            return False
        self._dispatching()
        self._frame_mark("TYPE")
        # Once sent, typing may have happened even if WDA never answers. Only the
        # read-only lookup above may fall back to another input route.
        self._sends(self.call, "POST", f"/element/{quote(identifier, safe='')}/value",
                  {"value": text + "\n" if submit else text}, deadline.remaining())
        self._frame_dispatched()
        return True

    def set_value(self, target, value, timeout=10):
        """Select ``value`` ("6", "AM") on the wheel nearest the observed one, read back; WheelNotSet when it can't."""
        deadline = Deadline(timeout)
        found = self.call("POST", "/elements", {"using": "class name", "value": f"XCUIElementType{target.role}"},
                          min(5, deadline.remaining()))
        ids = [i for i in (element_id(e) for e in (found if isinstance(found, list) else [])[:12]) if i]
        if not ids:
            raise DriverRejection("unsupported_or_stale_target")
        cx, cy = target.center
        best, best_distance = None, None
        width, height = self._screen_sizes.get(self.url) or (None, None)
        if not width:
            current = self.observe(timeout=min(5, deadline.remaining()))
            width, height = current.width, current.height
        for identifier in ids:
            rect = self.call("GET", f"/element/{quote(identifier, safe='')}/rect", timeout=min(3, deadline.remaining()))
            if not isinstance(rect, dict) or not width:
                continue
            x = (rect.get("x", 0) + rect.get("width", 0) / 2) / width
            y = (rect.get("y", 0) + rect.get("height", 0) / 2) / height
            distance = (x - cx) ** 2 + (y - cy) ** 2
            if best_distance is None or distance < best_distance:
                best, best_distance = identifier, distance
        best = best or (ids[0] if len(ids) == 1 else None)
        if best is None:
            raise DriverRejection("unsupported_or_stale_target")
        self._dispatching()
        wanted = " ".join(str(value).split())
        path = f"/element/{quote(best, safe='')}"
        shown = self._wheel_value(path, deadline)
        if wheel_shows(shown, wanted):
            return
        # WDA ignores a value that is not one of the wheel's items, silently. Items are spelled per picker: an alarm's
        # minutes are "00".."59" ("0" didn't move it: iOSWorld multi-087), a timer's are "7 min" ("7" didn't: verify
        # runs, 12 Oct), the model often names one as the wheel shows it ("6 o'clock", "45 minutes": clock-001). Each
        # spelling is tried, read back after each.
        for attempt in wheel_items(wanted, shown)[:WHEEL_SPELLINGS]:
            self.call("POST", path + "/value", {"value": attempt}, deadline.remaining())
            shown = self._wheel_value(path, deadline)
            if wheel_shows(shown, wanted):
                return
        # No spelling took: the wheel is stepped one item at a time toward the value (WDA's picker-wheel select), and
        # read after every step.
        goal = wheel_number(wanted)
        orders = ["previous", "next"] if goal is None else []
        for _ in range(WHEEL_STEPS):
            if deadline.remaining() <= 1:
                break
            now = wheel_number(shown)
            if goal is not None:
                if now is None or now == goal:
                    break
                order = "next" if now < goal else "previous"
            elif orders:
                order = orders.pop(0)
            else:
                break
            self.call("POST", f"/wda/pickerwheel/{quote(best, safe='')}/select", {"order": order, "offset": 0.15},
                      deadline.remaining())
            before, shown = shown, self._wheel_value(path, deadline)
            if wheel_shows(shown, wanted):
                return
            if shown == before:
                break  # the wheel didn't move: at its end, or the step isn't supported
        # Fails closed: the caller learns the wheel isn't at the value, and a chain stops here (a 25-minute timer
        # started after SET_TEXT "12" returned as if it had worked, verify runs of 12 Oct).
        raise WheelNotSet(f"the wheel shows {str(shown)[:40]!r}, not {wanted[:40]!r}: none of its items matched")

    def _wheel_value(self, path, deadline):
        try:
            shown = self.call("GET", path + "/attribute/value", timeout=min(3, max(.5, deadline.remaining())))
        except TransportError:
            return None
        return shown if isinstance(shown, str) else None

    def clear_text(self, target, timeout=10):
        """Empty an editable field (a caller replacing its text: a label, a title). Best effort."""
        self._clear_field(target, Deadline(timeout))

    def replace_text(self, target, text, timeout=10):
        """Replace a text field's content through one element reference; False: nothing typed, the caller types."""
        validate_input_text(text, multiline=target.role == "TextView")
        query = (target.value or "").strip()
        filled = bool(query) and query != (target.label or "").strip()
        # A search field is typed into by keystrokes, never through the element (Safari swaps in
        # another field on focus): only a filled one, whose clear leaves keyboard focus in it.
        if target.role not in ("TextField", "TextView", "SearchField") or not target.label or (
                target.role == "SearchField" and not filled):
            return False
        deadline = Deadline(timeout)
        identifier = self._unique_element(target.role, target.label, deadline)
        if not identifier:
            return False
        path = f"/element/{quote(identifier, safe='')}"
        self._dispatching()
        if filled:
            self.call("POST", path + "/clear", {}, min(5, deadline.remaining()))
        self._frame_mark("TYPE")
        if target.role == "SearchField":
            self._sends(self.call, "POST", "/wda/keys", {"value": [text]}, deadline.remaining())
        else:
            self.call("POST", path + "/value", {"value": text}, deadline.remaining())
        self._frame_dispatched()
        return True

    def _clear_field(self, target, deadline):
        query = (target.value or "").strip()
        if (target.role not in {"SearchField", "TextField", "TextView"} or not query
                or query == (target.label or "").strip()):
            return False
        def literal(text):
            return text.replace("\\", "\\\\").replace("'", "\\'")
        name = (target.label or "").strip()
        kind = f"type == 'XCUIElementType{target.role}'"
        # WDA re-finds an element through the query that found it. Found by its value, the field
        # was gone once emptied: /clear emptied it, then failed after ~2.7 s ("not present in the
        # current view anymore"; 83 s of mem-015's 276 s, 24 Sep). By name, or as the only field of
        # its kind, the same clear takes ~0.4 s; the value finds it only when neither is unique. A long
        # value is read truncated ("head … tail", state._wda_text): its head finds it (an exact match
        # found nothing, and SET_TEXT appended to a note body: iOSWorld mem-004, 24 Sep).
        by_value = (f"{kind} AND value == '{literal(query)}'" if " … " not in query and len(query) < 500
                    else f"{kind} AND value BEGINSWITH '{literal(query.split(' … ', 1)[0][:150])}'")
        queries = ([f"{kind} AND (name == '{literal(name)}' OR label == '{literal(name)}')"] if name else []) + [
            kind, by_value]
        for predicate in queries:
            try:
                found = self.call("POST", "/elements", {"using": "predicate string", "value": predicate},
                                  min(5, deadline.remaining()))
            except TransportError:
                continue
            ids = [i for i in (element_id(e) for e in (found if isinstance(found, list) else [])) if i]
            if len(ids) == 1:
                self._dispatching()
                self._sends(self.call, "POST", f"/element/{quote(ids[0], safe='')}/clear", {},
                            min(5, deadline.remaining()))
                # A unique field was cleared and focused by XCTest.
                return True
            if ids and predicate == by_value:
                # Several fields hold this text: clearing the first could empty the wrong one.
                raise DriverRejection("unsupported_or_stale_target")
        return False

    def _await_keyboard(self, deadline, seconds=None):
        """True once a software keyboard is up, or, when none appears, a hardware one is attached.

        A parked keyboard (off screen: simulators, a paired keyboard) still takes keystrokes
        in the focused field. Measured on iOSWorld's simulators (24 Sep): 3 of 4 ran in
        hardware-keyboard mode and every TYPE failed "did not take keyboard focus" although
        /wda/keys typed fine. A visible keyboard is still preferred while it may be animating in.
        """
        tapped = time.monotonic()
        end = tapped + (WDA_KEYBOARD_WAIT_SECONDS if seconds is None else seconds)
        parked = False
        hardware = self.url in self._parked_keyboards
        if hardware:
            # No software keyboard will come: one read, once focus has had time to move.
            time.sleep(max(0.0, min(tapped + WDA_PARKED_FOCUS_SECONDS - time.monotonic(),
                                    deadline.end - time.monotonic() - WDA_READ_MARGIN_SECONDS)))
        first = True
        # At least one read: a slow host's focus sleep can outlast the window (CI read nothing, 25 Sep).
        while (first or time.monotonic() < end) and deadline.end - time.monotonic() > WDA_READ_MARGIN_SECONDS:
            first = False
            current = self.observe(timeout=deadline.remaining())
            keyboard = getattr(current, "keyboard", "") or (
                "visible" if any("SUBMIT" in e.actions for e in current.elements) else "")
            if keyboard == "visible":
                self._parked_keyboards.discard(self.url)
                return True
            parked = parked or keyboard == "parked"
            if parked and (hardware or time.monotonic() > end - WDA_KEYBOARD_WAIT_SECONDS / 2):
                self._parked_keyboards.add(self.url)
                return True
        if parked:
            self._parked_keyboards.add(self.url)
        return parked

    def write_text(self, target, snapshot, text, append=False, submit=False, timeout=30):
        """Write ``text`` into ``target`` and prove it by reading the whole field back.
        A read-back that differs is repaired once (emptied, written again), and only when the earlier text is known.
        Failing before anything that can change the field keeps its own error, so a caller may resend. Failing after is
        WriteInterrupted, whatever the last request said."""
        self._write_sent = False
        try:
            return self._write_whole(target, snapshot, text, append, submit, timeout)
        except Exception as error:
            if not self._write_sent:
                raise
            raise WriteInterrupted("The write stopped partway; request outcome unknown; not retried") from error

    def _write_whole(self, target, snapshot, text, append, submit, timeout):
        deadline = Deadline(timeout)
        grounded_target("TYPE", target, snapshot)
        rank = self._write_rank = self._rank(snapshot, target)
        if (not isinstance(text, str) or len(text) > WDA_WRITE_MAX_CHARS or append and not text.strip()
                or any(unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"} and c not in "\n\t" for c in text)):
            raise ValueError("TYPE requires bounded text without control characters")
        text = text.replace("\t", " ")
        shown = target.text if target.placeholder else (target.value or "")
        multiline = target.role == "TextView" or "\n" in shown
        # A chat composer sends on Return: its line breaks are pasted, or become spaces, never typed as keys.
        chat = (getattr(snapshot, "bundle_id", "") in CHAT_APPS
                or bool(CHAT_FIELD.search(f"{target.label or ''} {target.placeholder or ''}")))
        breaks = "\n" in text and (not multiline or chat)
        old = self._field_text(target, snapshot, deadline) if append else ""
        # An empty field reads as its placeholder, which not every field names (SwiftUI's vertical
        # TextField does not): an earlier text is known only when it cannot be a placeholder.
        known = old is not None and (not old or target.role == "TextView" or bool(target.placeholder)
                                     or "\n" in old or len(old) >= 60)
        paste = bool(text) and (len(text) >= self.paste_min_chars or breaks) and old is not None
        receipt = {"method": "clear" if not text else "paste" if paste else "type", "repaired": False,
                   "flattened": False}
        current, field = snapshot, target
        if paste or not append:
            if self._emptied(field, current, deadline):
                current, field = self._reread(field, deadline)
                if append and not known and (field.value or "") == old:
                    old, known = "", True  # emptying changed nothing: it showed its placeholder
            elif append:
                old, known = "", True
        expected = (old or "") + text if append else text
        if paste and not self._paste(field, current, expected, deadline):
            receipt["method"] = "type"
        if receipt["method"] == "type":
            typed = expected if (not append or current is not snapshot) else text
            if breaks:
                # Never Return in a field not known to take line breaks: it may submit.
                typed, receipt["flattened"] = " ".join(typed.split("\n")), True
                expected = " ".join(expected.split("\n"))
            self._type_chunks(field, current, typed, deadline, multiline)
        value, current = self._read_back(target, deadline, rank)
        matches = self._fits(value, expected, text, append and not known, breaks, field)
        if not matches and value is not None and breaks and self._same_text(value, " ".join(expected.split("\n"))):
            receipt["flattened"], matches = True, True  # a one-line field: its line breaks became spaces
        if not text and not matches and value is not None and value != shown:
            # Emptied, yet it reads as something: emptied again, what stays is its placeholder.
            field = self._same_field(current, target, rank) or field
            self._emptied(field, current, deadline)
            again, current = self._read_back(target, deadline, rank)
            if again == value:
                receipt["placeholder"], value, matches = value, "", True
        elif not matches and value is not None and (not append or known):
            receipt["repaired"] = True
            field = self._same_field(current, target, rank) or field
            if self._emptied(field, current, deadline):
                current, field = self._reread(field, deadline)
            if not self._paste(field, current, expected, deadline):
                self._type_chunks(field, None, " ".join(expected.split("\n")) if breaks else expected,
                                  deadline, multiline)
            value, current = self._read_back(target, deadline, rank)
            matches = self._fits(value, expected, text, False, breaks, field)
        # Return only on a field proven to hold what was written: a garbled search runs, a garbled message sends
        # (9 Oct: Safari searched "Grace Hopperen.m.wikipedia.org/wiki/IOS 26"). Fails closed.
        receipt["submitted"] = bool(submit and matches)
        if submit and matches:
            self._dispatching()
            self._frame_mark("SUBMIT")
            self._sends(self.call, "POST", "/wda/keys", {"value": ["\n"]}, deadline.remaining())
            self._frame_dispatched()
        receipt.update(value=value, expected=expected, matches=bool(matches),
                       chars=None if value is None else len(value))
        return receipt

    def _fits(self, value, expected, text, unknown_start, breaks, field):
        """The field's text read back is what was written."""
        if self._same_text(value, expected, field=field):
            return True
        if unknown_start and value is not None:
            return self._same_text(value, text, suffix=True) or breaks and self._same_text(
                value, " ".join(text.split("\n")), suffix=True)
        return False

    @staticmethod
    def _same_text(value, expected, suffix=False, field=None):
        """``value`` (a field read back) is ``expected``."""
        if value is None:
            return False
        if not expected and field is not None and field.placeholder and value == field.placeholder:
            return True

        def plain(text):
            return text.translate(_TYPOGRAPHIC).replace("\r\n", "\n")
        return (value == expected or plain(value) == plain(expected)
                or suffix and plain(value).endswith(plain(expected)))

    @staticmethod
    def _rank(snapshot, target):
        """(fields of ``target``'s kind on ``snapshot``, its place among them)."""
        fields = [e for e in snapshot.elements if e.editable and e.role == target.role]
        return len(fields), next((i for i, e in enumerate(fields) if e == target), None)

    def _same_field(self, snapshot, target, rank=None):
        """``target`` on a later ``snapshot``."""
        fields = [e for e in snapshot.elements if e.editable and e.role == target.role]
        same = [e for e in fields if e.locator == target.locator]
        if len(same) == 1:
            return same[0]
        if target.label:
            named = [e for e in fields if e.label == target.label]
            if len(named) == 1:
                return named[0]
        if rank is not None and rank[1] is not None and len(fields) == rank[0]:
            return fields[rank[1]]

        def distance(e):
            return abs(e.rect[0] - target.rect[0]) + abs(e.rect[1] - target.rect[1])
        near = min(fields, key=distance, default=None)
        return near if near is not None and distance(near) < .08 else None

    def _field_text(self, target, snapshot, deadline):
        """The field's whole text now, or None when it cannot be read."""
        if target.placeholder and target.value == target.placeholder:
            return ""
        if not truncated_field_value(target.value):
            return target.value or ""
        return self._active_text(target, snapshot, deadline, focus=True)

    def _active_id(self, target, snapshot, deadline, focus=False):
        """WDA's id for the field holding keyboard focus when it is ``target`` (their frames agree), else None."""
        for attempt in range(2 if focus else 1):
            try:
                identifier = element_id(self.call("GET", "/element/active", None, min(3, deadline.remaining())) or {})
                if identifier:
                    rect = self.call("GET", f"/element/{quote(identifier, safe='')}/rect", None,
                                     min(3, deadline.remaining()))
                    if self._frame_agrees(rect, target, snapshot):
                        return identifier
            except (TransportError, KeyError, TypeError):
                pass
            if attempt == 0 and focus:
                x, y = target.center
                self._sends(self._tap, x * snapshot.width, y * snapshot.height, deadline)
                time.sleep(WDA_PARKED_FOCUS_SECONDS)
        return None

    def _active_text(self, target, snapshot, deadline, focus=False):
        identifier = self._active_id(target, snapshot, deadline, focus)
        if identifier is None:
            return None
        path = f"/element/{quote(identifier, safe='')}/attribute/"
        try:
            value = self.call("GET", path + "value", None, min(3, deadline.remaining()))
            if value and target.role != "TextView":
                placeholder = self.call("GET", path + "placeholderValue", None, min(3, deadline.remaining()))
                if placeholder and value == placeholder:
                    return ""
        except (TransportError, TypeError):
            return None
        return value if isinstance(value, str) else ""

    @staticmethod
    def _frame_agrees(rect, target, snapshot):
        """A WDA rect (points) is ``target``'s frame."""
        if not isinstance(rect, dict):
            return False
        try:
            ax, ay, aw, ah = (float(rect[k]) / d for k, d in (("x", snapshot.width), ("y", snapshot.height),
                                                             ("width", snapshot.width), ("height", snapshot.height)))
        except (KeyError, TypeError, ValueError):
            return False
        tx, ty, tw, th = target.rect
        slack = .01
        return (ax - slack <= tx + tw / 2 <= ax + aw + slack and ay - slack <= ty + th / 2 <= ay + ah + slack
                or tx - slack <= ax + aw / 2 <= tx + tw + slack and ty - slack <= ay + ah / 2 <= ty + th + slack)

    def _emptied(self, target, snapshot, deadline):
        """True once ``target`` was emptied, False when it already was (and is left alone)."""
        text = target.text if target.placeholder else (target.value or "")
        if not text.strip() or text.strip() == (target.label or "").strip() and not target.placeholder:
            return False
        self._dispatching()
        # A named field is cleared by its name; an unnamed one (CloudSlides' slide body, one of several
        # text fields) through the field holding focus: asking WDA for every field of its kind took 1.8 s.
        if not (target.label and self._clear_field(target, deadline)):
            identifier = self._active_id(target, snapshot, deadline, focus=True)
            if identifier is None:
                raise FieldNotCleared("The field's text couldn't be cleared; no more was written")
            # A lost answer may follow a sent clear: the error stops the write rather than typing over it.
            self._sends(self.call, "POST", f"/element/{quote(identifier, safe='')}/clear", {},
                        min(8, deadline.remaining()))
        return True

    def _paste(self, target, snapshot, text, deadline):
        """Paste ``text`` through the empty field's edit menu (pressed twice at most); False when no Paste showed."""
        where = (self.url, getattr(snapshot, "bundle_id", None))
        if where in self._paste_missing:
            # This app offered no Paste before: the second miss cost another ~12 s for nothing (the frontier trace
            # review measured 15 s writes on the phone, 9 Oct). Typing goes on as the fallback always did.
            return False
        try:
            self.call("POST", "/wda/setPasteboard", {"content": base64.b64encode(text.encode()).decode(),
                                                     "contentType": "plaintext"}, min(5, deadline.remaining()))
        except TransportError:
            return False
        # An Allow Paste that follows is Mobster's own paste of this text (system_dialogs answers it as such).
        from .system_dialogs import note_paste
        note_paste(self)
        left, top, width, height = target.rect
        x = left + width / 2
        y = top + min(height / 2, 22 / snapshot.height)  # the first line: a long press on text moves the caret
        self._dispatching()
        self._frame_mark("TYPE")
        try:
            for attempt in range(2):
                if attempt:
                    self._sends(self._tap, x * snapshot.width, y * snapshot.height, deadline)
                    time.sleep(WDA_PARKED_FOCUS_SECONDS)
                self._sends(self._pointer, [
                    {"type": "pointerMove", "duration": 0, "x": round(x * snapshot.width),
                     "y": round(y * snapshot.height)},
                    {"type": "pointerDown"}, {"type": "pause", "duration": WDA_PASTE_PRESS_MS},
                    {"type": "pointerUp"}], deadline)
                end = time.monotonic() + WDA_PASTE_MENU_SECONDS
                while time.monotonic() < end and deadline.end - time.monotonic() > WDA_READ_MARGIN_SECONDS:
                    # One query, not a screen read: a read may also ask WDA which layers show (up to 3 s
                    # a read in CloudSlides' editor, 26 Sep).
                    try:
                        found = self.call("POST", "/elements", {"using": "predicate string", "value":
                                          "type IN {'XCUIElementTypeMenuItem', 'XCUIElementTypeButton'} "
                                          "AND label == 'Paste'"}, min(3, deadline.remaining()))
                        identifier = element_id(found[0]) if isinstance(found, list) and found else None
                        rect = identifier and self.call("GET", f"/element/{quote(identifier, safe='')}/rect", None,
                                                        min(3, deadline.remaining()))
                    except TransportError:
                        identifier = rect = None
                    if isinstance(rect, dict) and all(finite_positive(rect.get(k)) or rect.get(k) == 0
                                                      for k in ("x", "y", "width", "height")):
                        self._sends(self._tap, rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2, deadline)
                        return True
                    time.sleep(.1)
            self._paste_missing.add(where)
            return False
        finally:
            self._frame_dispatched()

    def _reread(self, target, deadline):
        """(a new read, ``target`` on it)."""
        fresh = self.observe(timeout=min(5, deadline.remaining()))
        return fresh, self._same_field(fresh, target, self._write_rank) or target

    def _type_chunks(self, target, snapshot, text, deadline, multiline):
        """Type ``text`` (a known multi-line field takes its line breaks), in chunks typing accepts."""
        limit = 4000 if multiline else 1000
        chunks = [text[i:i + limit] for i in range(0, len(text), limit)] or [""]
        fresh = snapshot
        if fresh is None or time.monotonic() - fresh.captured_at > WDA_FRESH_SECONDS:
            fresh, target = self._reread(target, deadline)
        first = chunks[0]
        if first.strip() and self._active_id(target, fresh, deadline) is not None:
            # The field holds focus (it was just emptied through it): keystrokes, no focusing tap. A tap on
            # an emptied CloudSlides body left it without focus every second write (F2, 26 Sep).
            self._dispatching()
            self._frame_mark("TYPE")
            self._sends(self.call, "POST", "/wda/keys", {"value": [first]}, deadline.remaining())
            self._frame_dispatched()
        elif first.strip():
            try:
                self._type_text("TYPE", target, fresh, first, deadline, multiline=multiline)
            except NoKeyboardFocus:
                # The focusing tap was made but no keyboard was seen (none shows with a hardware keyboard
                # on some screens): keystrokes go where focus is, so only when the field holds it.
                if self._active_id(target, fresh, deadline) is None:
                    raise
                self._frame_mark("TYPE")
                self._sends(self.call, "POST", "/wda/keys", {"value": [first]}, deadline.remaining())
                self._frame_dispatched()
        for chunk in chunks[1:]:
            self._sends(self.call, "POST", "/wda/keys", {"value": [chunk]}, deadline.remaining())

    def _read_back(self, target, deadline, rank=None):
        """(the field's whole text, the read)."""
        fresh = self.observe(timeout=min(5, deadline.remaining()))
        field = self._same_field(fresh, target, rank)
        if field is not None and not truncated_field_value(field.value):
            return (field.text if field.placeholder else field.value or ""), fresh
        return self._active_text(field or target, fresh, deadline), fresh

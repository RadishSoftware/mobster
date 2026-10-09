"""What a Fast run shows a person: its steps in plain words, proof from the screen, and its outcome.

The Mac app's run view reads three things from the Fast engine (Jev plus the helper):

- ``step`` events in plain words ("Tapped General", "Read iOS Version 26.4"), each with a small
  frame of the screen when the server asks for frames (``Agent.capture_frames``);
- a result ``proof``: what the screen showed, in which app and where, and at which step;
- an ``outcome``: "done" only when there is proof, else "check"; a run that ended any other way
  is "couldnt_finish", "stopped" or "declined".

Nothing here decides an action or changes a run's status: it describes what already happened.
Typed text never goes into a step (the journal keeps no message text); a proof quotes only what
the app itself showed on screen.
"""

import io
import re

from .extraction import request_words
from .task_policy import commit_label
from .narrate import OPERATIONS

# Frames are thumbnails for the step list and proof chips: at most this many pixels on the long side.
FRAME_SIDE = 480
FRAME_QUALITY = 70
# Proof chips per result, and the screens named in a proof's breadcrumb.
PROOF_LIMIT = 4
PATH_DEPTH = 3
QUOTE_LIMIT = 120

COMPLETED = frozenset({"completed_unverified", "expected_text_visible"})
REVIEW = frozenset({"inconsistent_completion", "completion_not_confirmed", "demo_complete"})
STOPPED = frozenset({"stopped", "user_condition_met"})
SWITCH_ROLES = frozenset({"Switch", "Toggle"})
# Operations whose step names no control.
UNTARGETED = frozenset({"SWIPE_UP", "SWIPE_DOWN", "SWIPE_LEFT", "SWIPE_RIGHT", "BACK", "HOME", "WAIT",
                        "VOLUME_UP", "VOLUME_DOWN"})
ACRONYMS = {"ios": "iOS", "url": "URL", "id": "ID", "ip": "IP", "wifi": "Wi-Fi", "gps": "GPS", "usd": "USD",
            "api": "API", "pdf": "PDF", "eta": "ETA", "sku": "SKU", "os": "OS"}


def outcome(status, proof):
    """One of the run view's five outcomes for a finished Fast run. "done" needs proof."""
    if status in COMPLETED:
        return "done" if proof else "check"
    if status in REVIEW:
        return "check"
    if status == "approval_denied":
        return "declined"
    if status in STOPPED:
        return "stopped"
    return "couldnt_finish"


def target_rect(element):
    """Where an approval's tap lands, as screen fractions {x, y, w, h}, or None."""
    if element is None:
        return None
    x, y, w, h = element.rect
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


def composed_text(snapshot, target=None):
    """The text waiting in an editable field on this screen (what a Send tap sends), or None.

    With several fields, the one nearest the control: a composer sits beside its Send button.
    """
    fields = [e for e in snapshot.elements
              if e.editable and e.value.strip() and e.value != (e.placeholder or None)]
    if not fields:
        return None
    if target is not None:
        fields.sort(key=lambda e: abs(e.center[1] - target.center[1]))
    return fields[0].value[:4000]


def _short(text, limit=60):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def step_text(operation, label=""):
    """A dispatched action in plain words: "Tapped General", "Scrolled down", "Typed into Title"."""
    past = OPERATIONS.get(operation, (None, None))[1] or operation.replace("_", " ").capitalize()
    if operation in UNTARGETED:
        return past
    if operation == "LAUNCH_APP":
        return "Opened another app"
    label = _short(label).rstrip(":").strip()  # a field labelled "To:" reads "Tapped To"
    return f"{past} {label}" if label else f"{past} an item on screen"


def track_path(state, step, operation, target, changed):
    """Keep the run's breadcrumb (the controls tapped to reach each screen) for proof locations."""
    crumbs = list(getattr(state, "path_crumbs", ()))
    if operation == "BACK" and changed and crumbs:
        crumbs.pop()
    elif operation == "LAUNCH_APP" and changed:
        crumbs = []
    elif (operation == "TAP" and changed and target is not None and not target.editable
          and target.role not in SWITCH_ROLES and target.label.strip() and not commit_label(target.label, target.role)):
        # A commit (Send, Delete) acts on the screen it is on; it never leads to a place.
        crumbs.append(_short(target.label, 32))
    state.path_crumbs = crumbs
    paths = getattr(state, "step_paths", {})
    paths[step] = " › ".join(crumbs[-PATH_DEPTH:])
    state.step_paths = paths


def path_at(state, step):
    """The breadcrumb of the screen seen at ``step`` (the taps made before it), or ""."""
    paths = getattr(state, "step_paths", {})
    earlier = [s for s in paths if s < step]
    return paths[max(earlier)] if earlier else ""


def frame_jpeg(driver, *, allow_capture=False):
    """The current screen as a small JPEG (bytes), or None.

    The live video's newest frame costs nothing; a WDA screenshot costs 0.1-0.5 s, so it is taken
    only when ``allow_capture`` (once per run, for proof). Any failure gives None.
    """
    from .frontier import video_frame
    data = video_frame(driver)
    if data is None and allow_capture and callable(getattr(driver, "capture_preview", None)):
        try:
            data = driver.capture_preview(timeout=2)
        except Exception:
            return None
    if data is None:
        return None
    try:
        import base64
        from PIL import Image
        if isinstance(data, str):
            data = base64.b64decode(data.split(",", 1)[1] if data.startswith("data:") else data)
        image = Image.open(io.BytesIO(data))
        image.draft("RGB", (FRAME_SIDE, FRAME_SIDE))  # JPEG: decode at reduced size
        image = image.convert("RGB")
        image.thumbnail((FRAME_SIDE, FRAME_SIDE))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=FRAME_QUALITY)
        return out.getvalue()
    except Exception:
        return None


def _quote(label, text, field):
    """How a proof names what it saw: a value with its label ("iOS Version 26.4"), else the text."""
    label, text = " ".join((label or "").split()), " ".join((text or "").split())
    if field == "value" and label and label.casefold() != text.casefold():
        text = f"{label} {text}"
    elif field == "state" and label:
        text = f"{label}: {text}"
    return _short(text, QUOTE_LIMIT)


def _row_label(entry, entries):
    """The label beside a bare value on its row ("iOS Version" for "26.4"), or "".

    WDA often reports a settings row as separate texts: the label, then the value on its right.
    """
    row, text = entry.get("row_context"), entry["text"]
    if not row or entry.get("field") != "label" or len(text) > 40:
        return ""
    x = entry["rect"][0] if entry.get("rect") else 1
    siblings = [other for other in entries
                if other is not entry and other.get("row_context") == row
                and other.get("bundle_id") == entry.get("bundle_id") and other.get("field") == "label"
                and other["text"] != text and text not in other["text"] and len(other["text"]) <= 60
                and (other.get("rect") or [1])[0] < x]
    return min(siblings, key=lambda other: other["rect"][0])["text"] if siblings else ""


def cited_proof(state, entries, citations):
    """Proof for an extracted answer: each cited evidence entry, where and when it was seen."""
    by_id = {entry["id"]: entry for entry in entries}
    proof, seen = [], set()
    for citation in citations or ():
        entry = by_id.get(citation.get("evidence_id")) if isinstance(citation, dict) else None
        if entry is None or entry["id"] in seen or entry.get("offscreen") and not entry.get("text"):
            continue
        seen.add(entry["id"])
        step = entry.get("last_seen_step", entry.get("step", 0))
        label = _row_label(entry, entries)
        proof.append({"quote": _quote(label, entry["text"], "value") if label else
                      _quote(entry.get("label_context"), entry["text"], entry.get("field")),
                      "app": entry.get("bundle_id") or "", "screen": path_at(state, step), "step": step})
        if len(proof) >= PROOF_LIMIT:
            break
    return proof


# Roles that name a screen, not what is on it: a navigation title is never proof of a result.
TITLE_ROLES = frozenset({"NavigationBar", "Header", "Heading", "TabBar", "Toolbar", "Key", "Keyboard"})
# Which way a request turns a switch; "Turn on Dark Mode" -> "1", "turn Wi-Fi off" -> "0".
SWITCH_ON = re.compile(r"\b(?:turn|switch|toggle|flip|set)\s+(?:[\w&'.-]+\s+){0,4}?on\b|\b(?:enable|activate)\b", re.I)
SWITCH_OFF = re.compile(r"\b(?:turn|switch|toggle|flip|set)\s+(?:[\w&'.-]+\s+){0,4}?off\b|\b(?:disable|deactivate)\b", re.I)
SWITCH_VERBS = frozenset({"turn", "switch", "toggle", "flip", "enable", "disable", "activate", "deactivate"})


def _norm(text):
    return " ".join((text or "").split()).casefold()


def requested_switch(goal):
    """The value ("1" on, "0" off) a request asks a switch to end at, or None (neither, or both)."""
    on, off = bool(SWITCH_ON.search(goal or "")), bool(SWITCH_OFF.search(goal or ""))
    return "1" if on and not off else "0" if off and not on else None


def _titles(snapshot):
    """What names the screen or the app: navigation titles and the app's own name (never proof)."""
    names = {_norm(e.label) for e in snapshot.elements if e.role in TITLE_ROLES and e.label.strip()}
    bundle = (snapshot.bundle_id or "").rsplit(".", 1)[-1]
    if bundle:
        names.add(bundle.casefold())
    try:
        from .catalog import APPS
        names.update(_norm(app["name"]) for app in APPS if app.get("bundleId") == snapshot.bundle_id)
    except Exception:
        pass
    return names


def _typed_text(item):
    """The words of one typed entry: a string, or (field locator, field label, text)."""
    return " ".join((item[2] if isinstance(item, tuple) else item or "").split())


def screen_proof(state, goal, typed, snapshot, step):
    """Proof for a task with no answer: what the final screen shows that the request asked for.

    Only two things count, and each must say the task happened, not merely that its words are on screen:

    - the text Mobster typed last (a reminder's title, a message) reading back in something that is
      not a field: a saved row, a sent message's cell. Text still sitting in the field it was typed
      into was never saved or sent, so it is no proof, and neither is a navigation title or the app's
      name. ``typed`` holds (field locator, field label, text) entries in order (plain strings are
      read as text that any field still holding it has not left);
    - a switch the request names, at the value the request asked for ("Dark Mode: On" for
      "Turn on Dark Mode"; "Dark Mode: Off" proves nothing there).

    Anything else gives no proof, and the run reads "Check the result" (26 Sep PM probe: an unsaved
    reminder was "proved" by the title "Reminders", a switch left off by "Dark Mode: Off").
    """
    if snapshot is None:
        return []
    found = []
    last = next((item for item in reversed(list(typed or ())) if len(_typed_text(item)) >= 2), None)
    if last is not None:
        text = _typed_text(last)
        needle = text.casefold()
        if isinstance(last, tuple):
            # The field it was typed into, by its label: a composer, a title. Sent bubbles are editable
            # text views too (Messages), so any other field holding the words proves nothing either way.
            label = _norm(last[1])
            fields = [e for e in snapshot.elements if e.editable and _norm(e.label) == label] or \
                     [e for e in snapshot.elements if e.editable and needle in _norm(e.value)]
        else:
            fields = [e for e in snapshot.elements if e.editable]
        pending = any(needle in _norm(e.value) and e.value != (e.placeholder or None) for e in fields)
        if not pending:
            titles = _titles(snapshot)
            for element in snapshot.elements:
                if element.editable or element.role in TITLE_ROLES or element.role in SWITCH_ROLES:
                    continue
                label, value = _norm(element.label), _norm(element.value)
                if (label in titles and not value) or needle not in label and needle not in value:
                    continue
                found.append(("typed", element, text))
                break
    if not found:
        wanted = requested_switch(goal)
        words = request_words(goal) - SWITCH_VERBS
        if wanted is not None and words:
            switches = [(sum(1 for word in words if word in _norm(e.label)), e) for e in snapshot.elements
                        if e.role in SWITCH_ROLES and e.value in {"0", "1"}]
            switches = [(score, e) for score, e in switches if score]
            if switches:
                best = max(score for score, _ in switches)
                named = [e for score, e in switches if score == best]
                # One switch the request names, at the value it asked for: anything else proves nothing.
                if len(named) == 1 and named[0].value == wanted:
                    found.append(("switch", named[0], None))
    proof = []
    for kind, element, text in found[:PROOF_LIMIT]:
        if kind == "switch":
            quote = _quote(element.label, "On" if element.value == "1" else "Off", "state")
        else:
            quote = _short(text, QUOTE_LIMIT)
        proof.append({"quote": quote, "app": snapshot.bundle_id or "", "screen": path_at(state, step),
                      "step": step, "kind": kind})
    return proof


def recipient(snapshot, target=None):
    """Who a message goes to, from the conversation's header (its title, or the contact label near the
    top) or a filled To field; None when the screen does not say, or names more than one candidate."""
    if snapshot is None:
        return None
    for element in snapshot.elements:
        if element.editable and re.match(r"^\s*to\b", element.label or "", re.I):
            value = " ".join((element.value or "").split()).strip(", ")
            # A token Messages has not resolved reads "No Name, Searching": never a name to show.
            if (value and value != (element.placeholder or None) and len(value) <= 60
                    and not re.search(r"\b(?:no name|searching|loading)\b", value, re.I)):
                return value
    generic = _titles(snapshot) | {"messages", "back", "contact photo", "details", "info", "edit", "cancel",
                                   "done", "facetime", "video", "call", "audio call", "video call", "search",
                                   "new message", "chats", "inbox", "compose", "more", "menu", "options"}
    above = target.rect[1] if target is not None else 1
    # The conversation header is the navigation bar when there is one: "iMessage · Encrypted" and the
    # day's timestamp sit just under it and are not who the message goes to.
    bars = [e.rect for e in snapshot.elements if e.role == "NavigationBar" and e.rect[1] < .22]
    top, bottom = (min(r[1] for r in bars), max(r[1] + r[3] for r in bars)) if bars else (0, .22)
    candidates = []
    for element in snapshot.elements:
        label = " ".join((element.label or "").split())
        x, y, w, h = element.rect
        if (element.editable or not label or len(label) > 40 or _norm(label) in generic
                or not top <= y + h / 2 <= bottom or y >= above or element.role in {"Image", "Icon", "Key", "Switch", "Toggle"}
                or re.fullmatch(r"[\d:. ]+(?:AM|PM)?|Today|Yesterday", label, re.I)):
            continue
        candidates.append(label)
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else None


def composer(snapshot, target, text):
    """The editable field holding ``text`` nearest ``target`` (the composer beside Send), or None."""
    words = " ".join((text or "").split())
    fields = [e for e in snapshot.elements if e.editable and words and " ".join(e.value.split()) == words]
    if target is not None:
        fields.sort(key=lambda e: abs(e.center[1] - target.center[1]))
    return fields[0] if fields else None


def done_sentence(proof, app_name="", sent=None):
    """A write task's result as one sentence, from its proof: "“Pick up dry cleaning” now shows in Reminders.",
    or "Sent “I'm running late” to Sam." when the proof is a message the person approved sending."""
    if not proof:
        return None
    item = proof[0]
    if sent and item.get("kind") == "typed" and item["quote"] == _short(sent.get("text"), QUOTE_LIMIT):
        return f"Sent “{item['quote']}”" + (f" to {sent['to']}." if sent.get("to") else ".")
    where = f" in {app_name}" if app_name else ""
    if item.get("kind") == "switch":
        label, _, state = item["quote"].rpartition(": ")
        return f"{label} is {state.casefold()}{where}."
    if item.get("kind") == "typed":
        return f"“{item['quote']}” now shows{where}."
    return None


def _human(key):
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(key)).replace("_", " ").replace("-", " ").split()
    return " ".join(ACRONYMS.get(word.casefold(), word.casefold()) for word in words)


def _scalar(value):
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def answer_sentence(data, output_format):
    """The answer as one plain sentence for Text and Auto output, or None (structured formats).

    "main_heading: Example Domain" reads like data; "The main heading is Example Domain." does not.
    """
    if output_format not in (None, "text") or data is None:
        return None
    if isinstance(data, str):
        return " ".join(data.split())[:500] or None
    if _scalar(data):
        return str(data)
    if isinstance(data, dict) and 0 < len(data) <= 4 and all(_scalar(v) for v in data.values()):
        items = list(data.items())
        if len(items) == 1:
            key, value = items[0]
            return f"The {_human(key)} is {value}." if _human(key) else str(value)
        parts = [f"{_human(key)[:1].upper()}{_human(key)[1:]}: {value}" for key, value in items]
        return "; ".join(parts) + "."
    if isinstance(data, list) and 0 < len(data) <= 8 and all(_scalar(v) for v in data):
        return ", ".join(str(v) for v in data) + "."
    return None

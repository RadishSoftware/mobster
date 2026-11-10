"""READ_EACH: each row of a list whose rows each open a page, read by code, no model call per row (DESIGN.md § 7.6)."""

import re
import time

from ..agent_hooks import Prepared, SkillResult
from . import identity
from .notebook import Item

OP = "READ_EACH"
PROMPT = ("- READ_EACH target [open: <tab>; read: <what>]: target is one row of a list whose rows each open a page "
          "(matches, chats, results). Code opens each row in turn, reads the page, comes back, and saves each to "
          "the notebook. It never likes, messages or changes anything.")
MAX_SCROLLS = 3            # page scrolls per row
LIST_SCROLLS = 12          # list scrolls per call
MAX_ROWS = 100
TAP_PAUSE = .15            # after a tap that opens or closes a page
SCROLL_PAUSE = .1          # after a glide
READS = 3                  # reads that may wait for a page to change
SLICE_SECONDS = 150.0
SIMILAR = .5               # similarity at which the list counts as back
ITEM_KINDS = frozenset({"profile", "chat"})   # the plan's item kinds READ_EACH keeps; any other is a "row"
BACK = re.compile(r"^\s*(back|close|‹|<|chevron[ .]?left|navigate up|done)\s*$", re.I)
# What a macro never taps, in any app: acts on a person or a post, commits and social acts. The macro only reads; an
# act the request asks for is the navigation model's, through the run's approvals.
NEVER = re.compile(r"\b(like|likes|liked|unlike|super ?like|rose|roses|send|message|reply|comment|match|unmatch|skip|"
                   r"pass|report|block|follow|share|save|subscribe|delete|remove|hide|archive|mute|call|facetime|"
                   r"buy|order|pay|book|reserve|confirm|submit|post|invite|add|request|accept|decline|swipe)\b", re.I)
CHROME = re.compile(r"^(back|close|more|more options|options|profile|send|send a message|like prompt|like|skip|"
                    r"photo \d+ of \d+|search|cancel|edit|\d+)$", re.I)
NOUNS = {"profile": ("profile", "profiles"), "chat": ("chat", "chats"), "row": ("page", "pages")}


def factory(ctx):
    """READ_EACH for a run with a long-form session; None for every other run."""
    from .session import for_run
    session = for_run(getattr(ctx, "run_id", None))
    return ReadEach(session) if session is not None else None


def refused_locally(label):
    """True when ``label`` names an action on a person, a commit or a social act: never tapped by a macro."""
    from ..task_policy import COMMIT_CONTROL
    text = " ".join(str(label or "").split())
    return not text or bool(NEVER.search(text) or COMMIT_CONTROL.search(text))


def safe_tap(label):
    """May the macro tap ``label``: a Back control, or a label that names no act (``refused_locally``)."""
    return bool(BACK.match(str(label or ""))) or not refused_locally(label)


def item_kind(session):
    """The kind of item a READ_EACH page becomes: the plan's collection ("profile" for profiles or matches, "chat" for
    chats, DMs or messages), else "row"."""
    kind = getattr(getattr(session, "budget", None), "item_kind", None)
    return kind if kind in ITEM_KINDS else "row"


# A row's leading state, which many rows share ("Unread, Sam, …": real iPhone, 13 Oct, every unread Instagram
# thread was keyed "Unread", so READ_EACH took the first and skipped the rest as already read).
ROW_STATE = re.compile(r"^(unread|read|new|new messages?|\d+ new messages?|pinned|muted|verified|online|active now|"
                       r"active|seen|sent|delivered|typing…?|typing\.\.\.|unread messages?)$", re.I)


def row_name(label):
    """A row's key: its first part that isn't a state the rows share ("Riley, Your turn, Hi" -> "Riley"; "Unread,
    Sam, 4 new messages" -> "Sam")."""
    parts = [part for part in re.split(r",\s+|\s+[-–]\s+", " ".join(str(label or "").split())) if part]
    named = next((part for part in parts if not ROW_STATE.match(part)), parts[0] if parts else "")
    return named[:80]


def signature(snapshot):
    from ..state import signature as structural
    try:
        return structural(snapshot)
    except Exception:  # noqa: BLE001
        return []


def similarity(recorded, snapshot):
    """Jaccard overlap of the recorded role/label pairs with the screen's."""
    from ..state import signature as structural
    try:
        ours = {tuple(pair) for pair in recorded}
        theirs = {tuple(pair) for pair in structural(snapshot)}
    except Exception:  # noqa: BLE001
        return 0.0
    union = ours | theirs
    return len(ours & theirs) / len(union) if union else 1.0


def siblings(snapshot, target):
    """The list's rows: elements like ``target`` (same role, width within 10 %, left edge within 5 %), top to bottom."""
    x, _, w, _ = target.rect
    rows = [e for e in getattr(snapshot, "elements", ()) or ()
            if e.role == target.role and (e.label or "").strip() and abs(e.rect[2] - w) <= .1 * max(w, .01)
            and abs(e.rect[0] - x) <= .05 and 0 <= e.center[1] < .97]
    return sorted(rows, key=lambda e: e.rect[1])


def page_lines(snapshot):
    """A page's text, top to bottom: every labelled, non-editable element but the chrome."""
    lines = []
    for element in sorted(getattr(snapshot, "elements", ()) or (), key=lambda e: (e.rect[1], e.rect[0])):
        if element.editable:
            continue
        label = " ".join((element.label or "").split())
        value = " ".join((element.value or "").split())
        text = label if not value or value == label else f"{label}: {value}"
        if not text or CHROME.match(text) or element.role in ("Image", "Icon") and re.match(r"photo \d", text, re.I):
            continue
        if element.role == "Button" and NEVER.search(text):
            continue
        if text not in lines:
            lines.append(text[:300])
    return lines


class ReadEach:
    """The READ_EACH skill (agent_hooks.Skill) for one run."""

    op = OP
    prompt = PROMPT
    needs_target = True

    def __init__(self, session):
        self.session = session

    def available(self, ctx):
        return True

    def prepare(self, ctx, act, snapshot):
        return Prepared(title=None)

    def perform(self, ctx, prepared, act, snapshot):
        return Reading(self.session, ctx, act, snapshot).run()


class Reading:
    """One READ_EACH call."""

    def __init__(self, session, ctx, act, snapshot):
        self.session, self.driver = session, ctx.driver
        self.target = act.get("element")
        self.snapshot = snapshot
        self.bundle = getattr(snapshot, "bundle_id", None) or getattr(ctx, "app_bundle", None)
        self.kind = item_kind(session)
        text = str(act.get("text") or "")
        opened = re.search(r"\bopen:\s*([^;]+)", text, re.I)
        self.open_label = " ".join(opened.group(1).split()) if opened else None
        self.started = time.monotonic()
        self.ids, self.reason, self.detail = [], None, ""
        self.taps = []                     # every label this call tapped (tests read it)
        self.row_seconds = []
        from .inbox import sweeping, triage_on
        # An inbox sweep's rows sorted by the run's model before any is opened (inbox.triage); None: no triage.
        self.triaged = {} if sweeping(session, self.kind) and triage_on() else None
        self.skipped = []                  # rows triage left closed

    def run(self):
        try:
            self._loop()
        except Exception as error:  # noqa: BLE001 -- what was saved stays saved; the model hears why it stopped
            self.reason, self.detail = "error", type(error).__name__
        self.session.progress("reading", label=self._label())
        note = getattr(self.session, "on_macro", None)
        if callable(note):
            note(OP, self.reason or "end", len(self.ids))
        return SkillResult(feedback=self._feedback(), changed=bool(self.taps))

    def _loop(self):
        if self.target is None:
            self.reason = "no_target"
            return
        pool = self.session.extract_pool()
        done = {getattr(item, "identity", "") for item in getattr(self.session.notebook, "items", ()) or ()}
        tried = set()
        current = self.snapshot
        list_signature = signature(current)
        rect = self.target.rect
        template = self.target
        started_at = rect[1]
        scrolls = 0
        while True:
            if self._other_app(current):
                self.reason = "left"
                return
            why = self._should_stop()
            if why:
                self.reason = why
                return
            rows = [e for e in siblings(current, template) if e.rect[1] >= started_at - .001 or scrolls]
            started_at = -1
            todo = [e for e in rows if row_name(e.label) not in tried]
            if not todo:
                if scrolls >= LIST_SCROLLS:
                    self.reason = "end"
                    return
                before = {row_name(e.label) for e in rows}
                self.driver.execute("SWIPE_UP", None, current, timeout=10)
                time.sleep(SCROLL_PAUSE)
                scrolls += 1
                current = self.driver.observe(timeout=10)
                if self._other_app(current):
                    self.reason = "left"
                    return
                fresh = {row_name(e.label) for e in siblings(current, template)} - before - tried
                if not fresh:
                    self.reason = "end"
                    return
                list_signature = signature(current)
                continue
            if self.triaged is not None:
                todo = self._triage(todo, tried)
                if not todo:
                    continue
            row = todo[0]
            name = row_name(row.label)
            tried.add(name)
            key = "row:" + identity.fold(name)
            if key in done:
                continue
            outcome = self._read_row(pool, current, row, name, key, list_signature)
            if outcome is not None:
                self.reason = outcome
                return
            done.add(key)
            current = self.back_on
            template = next((e for e in siblings(current, template)), template) if current else template

    def _triage(self, todo, tried):
        """``todo`` without the rows the run's model says need no reply (they are marked tried and noted)."""
        from .inbox import _plain, triage
        unrated = [e for e in todo if _plain(e.label) not in self.triaged]
        if unrated:
            factory = getattr(getattr(self.session, "models", None), "extract", None)
            try:
                client = factory() if callable(factory) else None
            except Exception:  # noqa: BLE001 -- no model: every row is opened
                client = None
            verdicts = triage(unrated, client, request=getattr(self.session, "request", ""),
                              mask=getattr(self.session, "mask", None))
            self.triaged.update({_plain(e.label): verdicts.get(_plain(e.label), True) for e in unrated})
        keep = []
        for element in todo:
            if self.triaged.get(_plain(element.label), True):
                keep.append(element)
            else:
                tried.add(row_name(element.label))
                self.skipped.append(row_name(element.label))
        return keep

    def _should_stop(self):
        try:
            why = self.session.should_yield()
        except Exception:  # noqa: BLE001
            return "stopped"
        if why:
            return why
        try:
            left = self.session.budget_left() or {}
        except Exception:  # noqa: BLE001
            left = {}
        if left.get("items") is not None and left["items"] <= 0:
            return "budget"
        if time.monotonic() - self.started >= SLICE_SECONDS or len(self.ids) >= MAX_ROWS:
            return "slice"
        return None

    def _tap(self, element, screen, label=None):
        """Tap ``element`` when ``safe_tap`` allows ``label`` (its label by default; a list row is checked by its name,
        so a chat whose last message says "send" still opens); the screen after it, or None when refused."""
        if not safe_tap(label if label is not None else element.label):
            return None
        self.taps.append(element.label)
        self.driver.execute("TAP", element, screen, timeout=10)
        time.sleep(TAP_PAUSE)
        return self._read_changed(screen)

    def _read_changed(self, before):
        screen = before
        for _ in range(READS):
            screen = self.driver.observe(timeout=10)
            if screen.content_fingerprint != before.content_fingerprint:
                return screen
            time.sleep(TAP_PAUSE)
        return screen

    def _read_row(self, pool, list_screen, row, name, key, list_signature):
        """None when the row was read and the list is back (``self.back_on``), else why it stopped."""
        t0 = time.monotonic()
        page = self._tap(row, list_screen, label=name)
        if page is None:
            self.detail = row.label[:80]
            return "refused"
        if self._other_app(page):
            return "left"
        depth = 1
        lines = page_lines(page)
        conversation = list(lines)
        if self.open_label:
            control = next((e for e in page.elements if " ".join((e.label or "").split()).casefold()
                            == self.open_label.casefold()), None)
            if control is not None:
                opened = self._tap(control, page)
                if opened is None:
                    self.detail = control.label[:80]
                    return self._back(page, depth, list_signature) or "refused"
                if self._other_app(opened):
                    return "left"
                if opened.content_fingerprint != page.content_fingerprint:
                    page, depth = opened, depth + 1
                    lines += [line for line in page_lines(page) if line not in lines]
        page = self._scroll_page(page, lines)
        if self._other_app(page):
            return "left"
        frame = self._frame()
        item = Item(
            kind=self.kind, app=self.bundle, identity=key,
            source={"creator": None, "title": None, "name": self._mask(name)},
            text={"caption": "", "on_screen": "", "audio": "",
                  "details": [self._mask(line)[:120] for line in lines[:8]],
                  "conversation": self._mask(" | ".join(conversation))[:300]})
        stopped = self._back(page, depth, list_signature)
        item = self.session.add_item(item)
        self.ids.append(item.id)
        ax_text = f"Row: {name}\n" + "\n".join(self._mask(line) for line in lines)
        pool.submit(item.id, self.kind, [frame] if frame else [], ax_text)
        self.row_seconds.append(time.monotonic() - t0)
        self.session.progress("reading", label=self._label())
        return stopped

    def _scroll_page(self, page, lines):
        for _ in range(MAX_SCROLLS):
            self.driver.execute("SWIPE_UP", None, page, timeout=10)
            time.sleep(SCROLL_PAUSE)
            after = self.driver.observe(timeout=10)
            if self._other_app(after):
                return after
            new = [line for line in page_lines(after) if line not in lines]
            if after.content_fingerprint == page.content_fingerprint or not new:
                return after
            lines += new
            page = after
        return page

    def _back(self, page, depth, list_signature):
        """Back ``depth`` pages to the list (``self.back_on``); None when the list is back, else why not."""
        screen = page
        for _ in range(depth):
            if self._other_app(screen):
                self.back_on = screen
                return "left"
            back = next((e for e in screen.elements if e.role == "Button" and BACK.match(e.label or "")), None)
            if back is None:
                back = next((e for e in screen.elements if e.role == "Button" and e.rect[0] < .2 and e.rect[1] < .12
                             and safe_tap(e.label)), None)
            if back is None:
                self.back_on = screen
                return "lost"
            after = self._tap(back, screen)
            if after is None:
                self.back_on = screen
                return "lost"
            screen = after
        self.back_on = screen
        if self._other_app(screen):
            return "left"
        if similarity(list_signature, screen) < SIMILAR:
            return "lost"
        return None

    def _other_app(self, screen):
        bundle = getattr(screen, "bundle_id", None)
        return bool(bundle and self.bundle and bundle != self.bundle)

    def _frame(self):
        video = getattr(getattr(self.driver, "frame_clock", None), "video", None)
        latest = getattr(video, "latest", None)
        try:
            if callable(latest):
                frame = latest()
                if isinstance(frame, (tuple, list)) and len(frame) > 2 and frame[1] == "image/jpeg":
                    return bytes(frame[2])
            capture = getattr(self.driver, "capture_preview", None)
            if callable(capture):
                from .frames import as_bytes
                return as_bytes(capture(timeout=3))
        except Exception:  # noqa: BLE001
            return None
        return None

    def _mask(self, text):
        return self.session.mask(text) if text else text

    def _label(self):
        noun = NOUNS.get(self.kind, NOUNS["row"])
        return f"{len(self.ids)} {noun[1] if len(self.ids) != 1 else noun[0]} read"

    def _feedback(self):
        noun = NOUNS.get(self.kind, NOUNS["row"])
        count = len(self.ids)
        seconds = round(time.monotonic() - self.started)
        parts = [f"Read {count} {noun[1] if count != 1 else noun[0]} in {seconds} s."]
        if self.ids:
            parts.append(f"Saved as {self.ids[0]}" + (f" to {self.ids[-1]}." if count > 1 else "."))
        try:
            left = (self.session.budget_left() or {}).get("items")
        except Exception:  # noqa: BLE001
            left = None
        if left is not None:
            parts.append(f"{left} left in the budget.")
        why = {"end": "the list ended (every row is read)", "budget": "the budget is covered",
               "slice": "slice ended, call READ_EACH again to go on",
               "refused": f"didn't tap {self.detail!r}: READ_EACH only reads, and that control acts",
               "lost": "a page didn't go back to the list: look at the screen and go back to the list yourself",
               "left": "another app came to the front", "steer": "a message from the user is waiting",
               "stopped": "the task was stopped", "paused": "the task was paused",
               "stop_after": "the user asked to stop after this step",
               "no_target": "READ_EACH needs a row of the list as its target",
               "error": f"the phone didn't answer ({self.detail})"}.get(self.reason or "end", self.reason)
        parts.append(f"Stopped because: {why}.")
        return " ".join(parts)

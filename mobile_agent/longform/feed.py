"""WATCH_FEED: a full-screen vertical video feed watched by code, no model call per video (DESIGN.md § 7.1-7.4)."""

import re
import statistics
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..agent_hooks import Prepared, SkillResult
from . import ads, identity
from .notebook import Item

OP = "WATCH_FEED"
PROMPT = ("- WATCH_FEED [focus]: on a full-screen vertical video feed (Reels, TikTok, Shorts), watch the next videos "
          "in one action. Code swipes, reads each one, skips ads and repeats, and saves them to the notebook. It "
          "returns when the budget slice ends, the feed drifts off the focus, or it gets stuck.")
SLICE_SECONDS = 120.0
MIXED_ITEMS = 4            # items in a row that aren't full-screen videos (photo posts, ads) read before it gives up
SLICE_ITEMS = 60
FRAME_WAIT = .6            # seconds to wait for a frame newer than the swipe
FRAME_POLL = .02
F1_AFTER = .65             # f1, this long after f0
MAX_FRAMES = 2
STUCK_PAUSE = .4
DRIFT_RUN = 6
LOOP_WINDOW = 10
REPEATS_IN_A_ROW = 3
SLOW_AX = 1.2              # median seconds of the first AX_PROBES reads that turns on the adaptive read
AX_PROBES = 3
READ_TIMEOUT = 8
NOUNS = {"com.burbn.instagram": ("Reel", "Reels"), "com.zhiliaoapp.musically": ("TikTok", "TikToks"),
         "com.google.ios.youtube": ("Short", "Shorts")}
KINDS = {"com.burbn.instagram": "reel", "com.zhiliaoapp.musically": "tiktok", "com.google.ios.youtube": "short"}
WHY = {"budget": "the budget is covered", "slice": "slice ended, call WATCH_FEED again to go on",
       "steer": "a message from the user is waiting", "stopped": "the task was stopped",
       "paused": "the task was paused", "stop_after": "the user asked to stop after this step",
       "stuck": "the feed didn't move after a swipe, a re-read and a drag; it may be at its end",
       "looping": "the feed keeps showing items it already showed",
       "not_feed": "this screen isn't a full-screen video: open the first video, then WATCH_FEED"}
# A WATCH_FEED text made only of these words names no subject ("any topic, 10 reels"): it is no focus. Found on a real
# iPhone, 13 Oct: "any topic" as the focus had the extractor mark every meme off the topic "any topic".
_COUNT = re.compile(r"[\s,;:-]*\b\d+\s+(?:more\s+)?(?:reels?|videos?|tiktoks?|shorts?|clips?|posts?|items?)\W*$", re.I)
_NO_SUBJECT = frozenset(
    "a all any anything content every everything feed focus general generic in it kind kinds mixed my no none nothing "
    "of particular random reel reels short shorts sort sorts specific stuff subject subjects the thing things tiktok "
    "tiktoks topic topics video videos whatever".split())


def focus_from(text):
    """The focus a WATCH_FEED text names, or None when it names no subject ("any", "any topic, 10 reels", "10
    reels")."""
    text = " ".join(str(text or "").split())
    text = _COUNT.sub("", text).strip(" ,;:-")
    words = re.findall(r"[^\W_]+", text.casefold())
    if not words or all(word in _NO_SUBJECT or word.isdigit() for word in words):
        return None
    return text


# A feed shows at least this many of an item's signs (a creator, a caption or audio line, an item label) on the first
# read; fewer, and WATCH_FEED returns not_feed before its first swipe.
MIN_SIGNALS = 2


def factory(ctx):
    """WATCH_FEED for a run with a long-form session; None for every other run."""
    from .session import for_run
    session = for_run(getattr(ctx, "run_id", None))
    return WatchFeed(session) if session is not None else None


@dataclass
class FeedMemory:
    """What WATCH_FEED keeps between its calls in one run."""
    dedupe: identity.Deduper = field(default_factory=identity.Deduper)
    recent: deque = field(default_factory=lambda: deque(maxlen=LOOP_WINDOW))   # identities of the last captures
    kept_order: list = field(default_factory=list)                            # kept item ids, in capture order
    relevance: dict = field(default_factory=dict)                             # item id -> relevant (bool)
    ax_seconds: list = field(default_factory=list)
    alternate: bool = False
    last_identity: str | None = None
    last_picture: int | None = None
    listening: bool = False


class WatchFeed:
    """The WATCH_FEED skill (agent_hooks.Skill) for one run."""

    op = OP
    prompt = PROMPT
    needs_target = False

    def __init__(self, session):
        self.session = session
        self.memory = FeedMemory()

    def available(self, ctx):
        return True

    def prepare(self, ctx, act, snapshot):
        return Prepared(title=None)

    def perform(self, ctx, prepared, act, snapshot):
        return Watch(self, ctx, act, snapshot).run()

    def on_extracted(self, item_id, extract):
        if isinstance(extract, dict) and "relevant" in extract:
            self.memory.relevance[item_id] = bool(extract.get("relevant")) and not extract.get("ad")


@dataclass
class Outcome:
    seen: int = 0
    kept: int = 0
    ads: int = 0
    repeats: int = 0
    unreadable: int = 0
    ids: list = field(default_factory=list)
    item_seconds: list = field(default_factory=list)    # per kept item, swipe to next swipe
    skip_seconds: list = field(default_factory=list)    # per ad or repeat


def _app_name(bundle, plan=None):
    """The app's name as the run's plan card names it ("Instagram"), else as the catalog knows it, else "The app"."""
    for app in (plan or {}).get("apps") or ():
        if isinstance(app, dict) and app.get("bundleId") == bundle and app.get("name"):
            return app["name"]
    try:
        from ..catalog import app_label
        name = app_label(bundle).split(" (")[0] if bundle else ""
    except Exception:  # noqa: BLE001
        name = ""
    return name if name and name != bundle else "The app"


class Watch:
    """One WATCH_FEED call."""

    def __init__(self, skill, ctx, act, snapshot):
        self.skill, self.session, self.memory = skill, skill.session, skill.memory
        self.driver = ctx.driver
        self.snapshot = snapshot
        self.bundle = getattr(snapshot, "bundle_id", None) or getattr(ctx, "app_bundle", None)
        self.hints = ads.GENERIC
        self.kind = KINDS.get(self.bundle, "reel")
        self.noun = NOUNS.get(self.bundle, ("video", "videos"))
        self.focus = focus_from(act.get("text")) or getattr(self.session, "focus", None)
        self.out = Outcome()
        self.started = time.monotonic()
        self.reason = None
        self.app_name = _app_name(self.bundle, getattr(self.session, "plan", None))
        self.overlay = None
        self.error = None

    def run(self):
        session = self.session
        pool = session.extract_pool()
        if not self.memory.listening:
            listen = getattr(pool, "listen", None)
            if callable(listen):
                listen(self.skill.on_extracted)
            self.memory.listening = True
        if self.focus and self.focus != getattr(session, "focus", None) and hasattr(pool, "focus"):
            pool.focus = self.focus        # the person changed the focus mid-run ("the WNBA instead")
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mobster-feed-frames")
        try:
            self._loop(pool)
        except Exception as error:  # noqa: BLE001 -- what was saved stays saved; the model hears why it stopped
            self.reason, self.error = "error", type(error).__name__
        finally:
            self.executor.shutdown(wait=False)
        session.progress("watching", label=self._label())
        note = getattr(session, "on_macro", None)
        if callable(note):
            note(OP, self.reason or "slice", self.out.kept)
        return SkillResult(feedback=self._feedback(), changed=self.out.seen > 0 or self.reason not in ("not_feed",))

    def _loop(self, pool):
        current, swiped = self.snapshot, None
        self.mixed = 0
        first = True
        while True:
            if not first:
                why = self._should_stop()
                if why:
                    self.reason = why
                    return
                t0 = time.monotonic()
                self.driver.execute("SWIPE_UP", None, current, timeout=10)
                swiped = time.monotonic()
            else:
                t0 = time.monotonic()
            index = self.out.seen + len(self.memory.kept_order)
            read_ax = not self.memory.alternate or index % 2 == 0 or first
            screen, read, f0, f0_at = self._read(current if first else None, swiped, read_ax)
            if first and current is not None and (self._left_feed(screen, read) == "not_feed" or read is not None
                                                  and read.signals() < MIN_SIGNALS):
                # The screen the planner handed over may predate its last tap (TAP Reels, then WATCH_FEED in one
                # chunk): read it once more before calling it no feed.
                fresh, fresh_read, fresh_f0, fresh_at = self._read(None, None, True)
                if fresh is not None:
                    screen, read, f0, f0_at = fresh, fresh_read, fresh_f0 or f0, fresh_at or f0_at
            if screen is not None:
                current = screen
            stop = self._left_feed(screen, read)
            if (stop == "not_feed" and not first and read is not None and read.signals() >= 1
                    and self.mixed < MIXED_ITEMS):
                # A feed opened from a grid mixes in photo posts and ads that aren't full-screen videos (Instagram
                # search, 10 Oct: 14 exits to the planner in one run). One with a creator or a caption is the next
                # item: read it like any other, up to MIXED_ITEMS in a row.
                stop = None
                self.mixed += 1
            elif read is not None and read.full_screen:
                self.mixed = 0
            if not stop and first and read is not None and read.signals() < MIN_SIGNALS:
                stop = "not_feed"      # a big element, but not an item: no creator, caption or item label
            if stop:
                self.reason = stop
                return
            text_id = identity.text_identity(read.creator, read.caption, read.audio) if read else None
            picture = identity.dhash(f0) if f0 else None
            ident = text_id or identity.picture_identity(picture)
            if first and ident and ident == self.memory.last_identity:
                # The item the last WATCH_FEED ended on: already saved.
                first = False
                continue
            if not first and ident and ident == self.memory.last_identity:
                # The screen didn't move: wait and read again, then one XCTest drag.
                unstuck = self._unstick(current)
                if unstuck is None:
                    self.reason = "stuck"
                    return
                current, read, f0, f0_at, text_id, picture, ident = unstuck
                swiped = time.monotonic()
            first = False
            self.memory.last_identity, self.memory.last_picture = ident, picture
            ad = ads.ad_reason(read) if read else None
            if ad:
                self._skip("ad", read, ident, t0)
                if self._looping():
                    return
                continue
            repeat_of, key = self.memory.dedupe.check(text_id, picture)
            if repeat_of:
                self._skip("repeat", read, repeat_of, t0)
                if self._looping():
                    return
                continue
            self.memory.dedupe.keep(key, text_id, picture)
            frames = [f0] if f0 else []
            f1 = self._second_frame(f0, f0_at)
            if f1 and identity.distance(picture, identity.dhash(f1)) > identity.STILL_DISTANCE:
                frames.append(f1)
            self._keep(pool, read, key, frames, t0)
            if self._looping() or self._drifted():
                return

    def _should_stop(self):
        session = self.session
        try:
            why = session.should_yield()
        except Exception:  # noqa: BLE001
            why = None
        if why:
            return why
        if time.monotonic() - self.started >= SLICE_SECONDS or self.out.seen >= SLICE_ITEMS:
            return "slice"
        left = self._left()
        if left.get("items") is not None and left["items"] <= 0:
            return "budget"
        if isinstance(left.get("phone_seconds"), (int, float)) and left["phone_seconds"] <= 0:
            return "budget"
        return None

    def _left(self):
        try:
            left = self.session.budget_left()
        except Exception:  # noqa: BLE001
            return {}
        return left if isinstance(left, dict) else {}

    def _read(self, screen, swiped, read_ax):
        """(screen, ItemRead or None, f0, f0's arrival)."""
        future = self.executor.submit(self._frame_after, swiped)
        read = None
        if screen is None and read_ax:
            t = time.monotonic()
            try:
                screen = self.driver.observe(timeout=READ_TIMEOUT)
            except Exception:  # noqa: BLE001 -- a failed read: the frames still carry the item
                screen = None
            if len(self.memory.ax_seconds) < AX_PROBES:
                self.memory.ax_seconds.append(time.monotonic() - t)
                if (len(self.memory.ax_seconds) == AX_PROBES
                        and statistics.median(self.memory.ax_seconds) > SLOW_AX):
                    self.memory.alternate = True
        if screen is not None:
            read = ads.read(screen, self.hints)
        f0, f0_at = future.result()
        if f0 is None and not self._has_stream():
            f0, f0_at = self._capture(), time.monotonic()
        return screen, read, f0, f0_at

    def _has_stream(self):
        video = getattr(getattr(self.driver, "frame_clock", None), "video", None)
        return callable(getattr(video, "latest", None))

    def _frame_after(self, after, wait=FRAME_WAIT):
        """(JPEG bytes, arrival) of the first stream frame that arrived after ``after`` (monotonic), or (None, None):
        no stream, or none in ``wait`` seconds."""
        video = getattr(getattr(self.driver, "frame_clock", None), "video", None)
        latest = getattr(video, "latest", None)
        if not callable(latest):
            return None, None
        deadline = time.monotonic() + wait
        while True:
            try:
                frame = latest()
            except Exception:  # noqa: BLE001
                frame = None
            if (isinstance(frame, (tuple, list)) and len(frame) > 4 and frame[1] == "image/jpeg"
                    and isinstance(frame[4], (int, float)) and (after is None or frame[4] > after)):
                return bytes(frame[2]), float(frame[4])
            if time.monotonic() >= deadline:
                return None, None
            time.sleep(FRAME_POLL)

    def _capture(self):
        capture = getattr(self.driver, "capture_preview", None)
        if not callable(capture):
            return None
        try:
            from .frames import as_bytes
            return as_bytes(capture(timeout=3))
        except Exception:  # noqa: BLE001
            return None

    def _second_frame(self, f0, f0_at):
        if f0 is None or MAX_FRAMES < 2:
            return None
        if self._has_stream():
            wait = (f0_at or time.monotonic()) + F1_AFTER - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            frame, _ = self._frame_after(max(f0_at or 0.0, time.monotonic() - .05), wait=.4)
            return frame
        return self._capture()

    def _left_feed(self, screen, read):
        """Why this screen isn't the feed any more ("left", "alert", "not_feed"), or None."""
        if screen is None:
            return None
        bundle = getattr(screen, "bundle_id", None)
        if bundle and self.bundle and bundle != self.bundle:
            return "left"
        if read is not None and read.overlay:
            self.overlay = read.overlay
            return "alert"
        if read is not None and not read.full_screen:
            return "not_feed"
        return None

    def _unstick(self, current):
        """The next item after a re-read and one XCTest drag, as (screen, read, f0, f0_at, text_id, picture,
        identity), or None when the feed still shows the same item."""
        time.sleep(STUCK_PAUSE)
        for attempt in range(2):
            swiped = None
            if attempt == 1:
                drag_next = getattr(self.driver, "drag_next", None)
                if not (callable(drag_next) and drag_next()):
                    return None
                self.driver.execute("SWIPE_UP", None, current, timeout=10)
                swiped = time.monotonic()
            screen, read, f0, f0_at = self._read(None, swiped, True)
            if screen is not None and self._left_feed(screen, read):
                return None
            text_id = identity.text_identity(read.creator, read.caption, read.audio) if read else None
            picture = identity.dhash(f0) if f0 else None
            ident = text_id or identity.picture_identity(picture)
            if ident and ident != self.memory.last_identity:
                return screen or current, read, f0, f0_at, text_id, picture, ident
            current = screen or current
        return None

    def _source(self, read):
        creator = f"@{read.creator}" if read and read.creator else None
        return {"creator": self._mask(creator), "title": None, "name": None}

    def _text(self, read):
        if read is None:
            return {"caption": "", "on_screen": "", "audio": "", "details": [], "conversation": ""}
        details = ["Paid partnership"] if read.sponsored else []
        return {"caption": self._mask(read.caption or "")[:300], "on_screen": "",
                "audio": self._mask(read.audio or "")[:120], "details": details, "conversation": ""}

    def _mask(self, text):
        if not text:
            return text
        try:
            return self.session.mask(text)
        except Exception:  # noqa: BLE001
            return text

    def _item(self, read, ident, skip):
        return Item(kind=self.kind, app=self.bundle, identity=ident or "", source=self._source(read),
                    text=self._text(read), skip=skip)

    def _skip(self, why, read, ident, t0):
        item = self.session.add_item(self._item(read, ident, why))
        self.out.seen += 1
        if why == "ad":
            self.out.ads += 1
        else:
            self.out.repeats += 1
        self.memory.recent.append(("skip", why, ident))
        self.out.skip_seconds.append(time.monotonic() - t0)
        self.session.progress("watching", label=self._label())
        return item

    def _keep(self, pool, read, key, frames, t0):
        item = self.session.add_item(self._item(read, key, None))
        self.out.seen += 1
        if getattr(item, "skip", None) == "repeat":
            # The notebook found it was a repeat (its own identities): counted, never extracted.
            self.out.repeats += 1
            self.memory.recent.append(("skip", "repeat", key))
            self.out.skip_seconds.append(time.monotonic() - t0)
            return item
        self.out.kept += 1
        self.out.ids.append(item.id)
        self.memory.kept_order.append(item.id)
        self.memory.recent.append(("kept", None, key))
        pool.submit(item.id, self.kind, frames, read.ax_text() if read else "")
        self.out.item_seconds.append(time.monotonic() - t0)
        self.session.progress("watching", label=self._label())
        return item

    def _looping(self):
        recent = list(self.memory.recent)
        identities = [entry[2] for entry in recent if entry[2]]
        twice = len(identities) != len(set(identities))
        in_a_row = 0
        for entry in reversed(recent):
            if entry[0] == "skip" and entry[1] == "repeat":
                in_a_row += 1
            else:
                break
        if in_a_row >= REPEATS_IN_A_ROW or (twice and recent and recent[-1][1] == "repeat"):
            self.reason = "looping"
            return True
        return False

    def _drifted(self):
        """The last DRIFT_RUN extracted items (in capture order; ones still being extracted don't count yet) were
        all off the focus."""
        if not self.focus:
            return False
        relevance = self.memory.relevance
        judged = [item_id for item_id in self.memory.kept_order if item_id in relevance][-DRIFT_RUN:]
        if len(judged) == DRIFT_RUN and not any(relevance[item_id] for item_id in judged):
            self.reason = "drifted"
            return True
        return False

    def _label(self):
        counters = getattr(getattr(self.session, "notebook", None), "counters", None) or {}
        kept = counters.get("kept", self.out.kept)
        return f"{kept} {self.noun[1] if kept != 1 else self.noun[0]} watched"

    def _feedback(self):
        out, noun = self.out, self.noun
        seconds = round(time.monotonic() - self.started)
        about = ""
        if self.focus:
            judged = [i for i in out.ids if i in self.memory.relevance]
            if judged:
                on = sum(1 for i in judged if self.memory.relevance[i])
                about = f" ({on} of the {len(judged)} checked so far about {self.focus})"
        parts = [f"Watched {out.seen} {noun[1] if out.seen != 1 else noun[0]} in {seconds} s: {out.kept} saved{about}, "
                 f"{out.ads} ad{'s' if out.ads != 1 else ''} and {out.repeats} repeat{'s' if out.repeats != 1 else ''}"
                 " skipped."]
        if out.ids:
            parts.append(f"Saved as {out.ids[0]}" + (f" to {out.ids[-1]}." if len(out.ids) > 1 else "."))
        left = self._left().get("items")
        if left is not None:
            parts.append(f"{left} {noun[1] if left != 1 else noun[0]} left in the budget.")
        reason = self.reason or "slice"
        if reason == "drifted":
            why = (f"the last {DRIFT_RUN} {noun[1]} weren't about {self.focus or 'the focus'}. Search again or change "
                   "the focus")
        elif reason == "left":
            kept = (getattr(getattr(self.session, "notebook", None), "counters", None) or {}).get("kept", out.kept)
            why = (f"{self.app_name} is no longer in front (it may have crashed). Open it again and go back to where "
                   f"you were; {kept} {noun[1]} are saved")
        elif reason == "alert":
            why = (f"an alert is over the feed ({(self.overlay or 'an alert')[:80]}): close it with its own button, "
                   "then WATCH_FEED again")
        elif reason == "error":
            why = f"the phone didn't answer ({self.error}); look at the screen, then WATCH_FEED again"
        else:
            why = WHY.get(reason, reason)
        parts.append(f"Stopped because: {reason} ({why}).")
        return " ".join(parts)

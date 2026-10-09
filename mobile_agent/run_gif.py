"""A finished task as a short captioned clip: an animated GIF, and an MP4 when ffmpeg is installed.

Each step's screen (FrameStore, at most 480 px) sits in a rounded phone frame on Mobster's Paper or Night ground, with
the step's plain-words caption under it and a small mark with "mobster.dev" in a corner. The clip opens on the task
typed into a prompt, shows each step for 1.2 to 2.5 s by the caption's length, shows an approval as the card Mobster
asked with, and holds the result 1.5 s longer. It stays under X's 15 MB GIF limit: one palette for the whole clip,
then fewer colours, no cross-fades, fewer steps, a smaller canvas, in that order.

Everything here is pure: ``build_clip(run, read_frame)`` takes a run (``Run.public()`` or a journal record, with its
events) and a function that returns a frame's JPEG bytes. Nothing is uploaded or saved here.

Privacy. A sign-in code (the USE_CODE skill) ends the screens: every step from it on is a plain card. ``redact``
blurs typed text fields and message bodies with the element frames each step journaled (``redact`` on a Smart step,
the Fast engine's ``observation`` elements), blurs the whole screen of a step with no known frames, masks quoted
text in captions, and leaves out the answer and the approval's wording.
"""

from dataclasses import dataclass, field
import io
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

GIF_MAX_BYTES = 14_500_000        # X takes GIFs up to 15 MB; this leaves room for the container's rounding
MAX_STEPS = 60                    # step screens in one clip; a longer task keeps its first, last and asked steps
MAX_GOAL_CHARS = 80
MAX_CAPTION_LINES = 2

# A step's screen holds 1.2 to 2.5 s by its caption's length; the result holds 1.5 s longer than a step would.
STEP_MIN_MS, STEP_MAX_MS, STEP_BASE_MS, STEP_PER_CHAR_MS = 1200, 2500, 800, 40
END_HOLD_MS = 1500
TYPE_FRAME_MS, TITLE_HOLD_MS = 60, 1100
FADE_STEPS, FADE_FRAME_MS = 3, 50
ASK_MS, ANSWERED_MS, SECRET_MS = 1700, 1100, 2000

SECRET_CAPTION = "Sign-in code entered. Screens after it are left out."
NO_SCREENS = "This task has no screens to save."
TOO_LONG = "This task is too long for a GIF. Save it as an MP4 instead."

# Paper and Night: the brand's light and dark colours.
THEMES = {
    "paper": {"ground": "#FAF8F5", "surface": "#FFFFFF", "sunken": "#F3F0EB", "ink": "#18171C", "ink2": "#5C5A63",
              "ink3": "#6F6C76", "line": "#E8E4DE", "line_strong": "#D9D4CC", "violet": "#6E56F6",
              "violet_strong": "#5B41E8", "mark": "#6E56F6", "green": "#137A4C", "bezel": "#18171C",
              "rim": "#2E2D33", "shadow": (24, 23, 28, 46)},
    "night": {"ground": "#111014", "surface": "#1A191F", "sunken": "#222128", "ink": "#F4F2EE", "ink2": "#B4B1BA",
              "ink3": "#8F8C96", "line": "#2A2930", "line_strong": "#3A3940", "violet": "#6E56F6",
              "violet_strong": "#5B41E8", "mark": "#7D68FF", "green": "#48D597", "bezel": "#24232A",
              "rim": "#45434D", "shadow": (0, 0, 0, 120)},
}

# Messaging and mail apps: with ``redact`` every line of text in them is a message body.
MESSAGE_APPS = frozenset({
    "com.apple.MobileSMS", "com.apple.mobilemail", "net.whatsapp.WhatsApp", "com.facebook.Messenger",
    "ph.telegra.Telegraph", "org.whispersystems.signal", "com.tinyspeck.chatlyio", "com.hammerandchisel.discord",
    "com.google.Gmail", "com.microsoft.Office.Outlook", "com.burbn.instagram", "com.atebits.Tweetie2",
    "com.linkedin.LinkedIn", "com.toyopagroup.picaboo", "jp.naver.line", "com.tencent.xin",
})
FIELD_ROLES = frozenset({"TextField", "SecureTextField", "SearchField", "TextView"})
TEXT_ROLES = frozenset({"StaticText", "TextView", "Cell", "Other", "Link", "Image"})
MAX_RECTS = 24
STATUS_BAR = (0.0, 0.0, 1.0, 0.06)   # never blurred: the time and battery

# What an approval asked to do, from the contract's act: (the caption's gerund, yes, no).
ACTS = {
    "send": ("sending", "Send", "Don’t send"), "post": ("posting", "Post", "Don’t post"),
    "pay": ("paying", "Pay", "Don’t pay"), "buy": ("buying", "Buy", "Don’t buy"),
    "order": ("ordering", "Place order", "Don’t order"), "book": ("booking", "Book", "Don’t book"),
    "delete": ("deleting", "Delete", "Don’t delete"),
}
ACT_FAMILIES = {
    "send_message": "send", "send": "send", "reply": "send", "request": "send", "send message": "send",
    "post": "post", "comment": "post", "publish": "post", "tweet": "post", "share": "post",
    "pay": "pay", "transfer": "pay", "request_money": "pay", "donate": "pay", "tip": "pay",
    "buy": "buy", "purchase": "buy", "checkout": "buy", "order": "order", "place order": "order",
    "book": "book", "reserve": "book", "delete": "delete", "erase": "delete", "remove": "delete",
    "trash": "delete", "clear": "delete",
}
ENDINGS = {"completed": "Done", "completed_unverified": "Done", "expected_text_visible": "Done",
           "approval_denied": "You declined", "stopped": "Stopped", "user_condition_met": "Stopped",
           "approval_timeout": "Stopped safely", "spend_cap": "Stopped at the cost limit"}


@dataclass
class Beat:
    """One moment of the clip. ``kind``: title, step, ask, secret or end. ``rects``: the screen's private regions
    (normalized), None when unknown (``redact`` then blurs the whole screen)."""
    kind: str
    caption: str
    frame_id: str | None = None
    rects: list | None = None
    n: int = 0
    total: int = 0
    card: dict | None = None
    note: str = ""


@dataclass
class Clip:
    gif: bytes
    frames: int                         # frames in the GIF
    durations: list                     # each GIF frame's milliseconds
    beats: list                         # the Beats shown, in order
    images: list = field(default_factory=list, repr=False)   # (RGB image, ms) before quantizing, for the MP4
    colors: int = 256
    crossfade: bool = True

    @property
    def captions(self):
        return [beat.caption for beat in self.beats]

    @property
    def seconds(self):
        return sum(self.durations) / 1000


# -- what the clip shows ---------------------------------------------------------------------------------------------

def private_rects(elements, bundle_id=None, limit=MAX_RECTS):
    """The frames (normalized x, y, w, h) of what ``redact`` blurs on one screen: every text field (empty ones
    too: the frame may be grabbed after the typing), every line of text in a messaging or mail app, and elsewhere
    text long enough to be a body (4 words, or 28 characters). ``elements``: Element objects or their dicts."""
    messages = bundle_id in MESSAGE_APPS
    found = []
    for element in elements or ():
        if isinstance(element, dict):
            get = element.get
        else:
            def get(name, default=None, element=element):
                return getattr(element, name, default)
        role, rect = str(get("role", "") or ""), get("rect")
        try:
            x, y, w, h = (float(value) for value in rect)
        except (TypeError, ValueError):
            continue
        if not (w > 0 and h > 0) or w * h > 0.6:
            continue  # nothing, or a container: its rows are listed on their own
        text = " ".join(str(get("value", "") or get("label", "") or "").split())
        field_like = bool(get("editable", False)) or role in FIELD_ROLES
        body = role in TEXT_ROLES and text and (messages or len(text.split()) >= 4 or len(text) >= 28)
        if field_like or body:
            found.append([round(x, 3), round(y, 3), round(w, 3), round(h, 3)])
        if len(found) >= limit:
            break
    return found


def _act(act):
    raw = str(act or "").strip().lower()
    family = ACT_FAMILIES.get(raw) or ACT_FAMILIES.get(raw.replace("_", " ").replace("-", " "))
    return ACTS.get(family)


def _mask_quotes(text):
    """Captions with ``redact``: quoted text ("Typed “oat milk”"), a value read ("Read Address: …"), phone numbers
    and email addresses hidden."""
    text = re.sub(r"“[^”]*”", "“•••”", text)
    text = re.sub(r"\"[^\"]*\"", "“•••”", text)
    text = re.sub(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", "•••", text)
    text = re.sub(r"\+?\(?\d[\d ().-]{5,}\d", "•••", text)
    return re.sub(r"^(Read [^:]{1,60}):\s.*$", r"\1: •••", text)


def step_ms(caption):
    return max(STEP_MIN_MS, min(STEP_MAX_MS, STEP_BASE_MS + STEP_PER_CHAR_MS * len(caption)))


def _elapsed(run):
    try:
        ms = float(run.get("finishedAt")) - float(run.get("createdAt"))
    except (TypeError, ValueError):
        summary = run.get("summary") or {}
        ms = summary.get("elapsed_ms") if isinstance(summary.get("elapsed_ms"), (int, float)) else None
    if ms is None or not math.isfinite(ms) or ms < 0:
        return None
    seconds = round(ms / 1000)
    return f"{seconds} s" if seconds < 60 else f"{seconds // 60} min {seconds % 60} s" if seconds % 60 else \
        f"{seconds // 60} min"


def plan_beats(run, redact=False):
    """The clip's beats, from a finished run's events. Pure: tests read the captions here, not from pixels."""
    events = [event for event in run.get("events") or () if isinstance(event, dict)]
    goal = " ".join(str(run.get("goal") or "").split())
    goal = goal if len(goal) <= MAX_GOAL_CHARS else goal[:MAX_GOAL_CHARS - 1].rstrip() + "…"
    beats = [Beat("title", goal)]
    observations = [(index, event) for index, event in enumerate(events)
                    if event.get("event") == "observation" and isinstance(event.get("elements"), list)]
    asked = {}
    secret = False
    last_frame = None
    for index, event in enumerate(events):
        kind = event.get("event")
        if kind in ("skill_started", "skill_finished") and event.get("op") == "USE_CODE":
            if not secret:
                beats.append(Beat("secret", SECRET_CAPTION))
            secret = True
        elif kind == "step" and isinstance(event.get("text"), str) and event["text"].strip():
            if secret:
                continue
            caption = " ".join(event["text"].split())
            frame_id = event.get("frameId") if isinstance(event.get("frameId"), str) else None
            rects = _step_rects(event, index, observations)
            beats.append(Beat("step", _mask_quotes(caption) if redact else caption, frame_id or last_frame, rects))
            last_frame = frame_id or last_frame
        elif kind == "approval_requested" and isinstance(event.get("approval_id"), str):
            asked[event["approval_id"]] = event
        elif kind == "approval_resolved" and event.get("approval_id") in asked and not secret:
            beats.append(_ask_beat(asked[event["approval_id"]], event.get("decision"), last_frame, beats, redact))
    steps = [beat for beat in beats if beat.kind == "step"]
    if not any(beat.frame_id for beat in steps):
        raise ValueError(NO_SCREENS)
    beats = _keep_steps(beats)
    steps = [beat for beat in beats if beat.kind == "step"]
    for number, beat in enumerate(steps, 1):
        beat.n, beat.total = number, len(steps)
    beats.append(_end_beat(run, steps, last_frame if not secret else None, redact))
    return beats


def _step_rects(event, index, observations):
    """A step's private regions: its own ``redact`` (Smart), else the Fast engine's observations either side of it
    (the screen before the action and the one after, joined), else None (unknown)."""
    if isinstance(event.get("redact"), list):
        return [list(rect) for rect in event["redact"] if isinstance(rect, (list, tuple)) and len(rect) == 4]
    before = [obs for at, obs in observations if at < index][-1:]
    after = [obs for at, obs in observations if at > index][:1]
    if not before and not after:
        return None
    rects = []
    for obs in before + after:
        rects += private_rects(obs.get("elements"), obs.get("bundle_id"))
    return rects


def _ask_beat(request, decision, frame_id, beats, redact):
    act = _act(request.get("act")) or _act(str(request.get("label") or "").split(" ")[0])
    gerund, yes, no = act or (None, "Approve", "Decline")
    if request.get("kind") == "clarify":
        caption, title = "Mobster asked you a question", None if redact else request.get("label")
    else:
        caption = f"Asked before {gerund}" if gerund else "Asked before going ahead"
        title = request.get("title") if not redact else None
    if not title:
        noun = {"Send": "this message", "Post": "this", "Pay": "this payment", "Delete": "this"}.get(yes, "this")
        title = f"{yes} {noun}?" if act else "Go ahead with this step?"
    answer = {"approved": f"You approved “{yes}”", "denied": f"You chose “{no}”",
              "redirected": "You declined and said what to do instead",
              "answered": "You answered", "timeout": "Nobody answered, so Mobster didn’t do it"}.get(
        str(decision or ""), "You answered")
    rects = next((beat.rects for beat in reversed(beats) if beat.kind == "step"), None)
    return Beat("ask", caption, frame_id, rects,
                card={"title": " ".join(str(title).split())[:120], "yes": yes, "no": no,
                      "decision": decision, "answer": answer})


def _keep_steps(beats):
    """At most MAX_STEPS step screens: the first, the last, those either side of an approval, then evenly."""
    steps = [i for i, beat in enumerate(beats) if beat.kind == "step"]
    if len(steps) <= MAX_STEPS:
        return beats
    keep = {steps[0], steps[-1]}
    for i, beat in enumerate(beats):
        if beat.kind == "ask":
            keep |= {j for j in (i - 1, i + 1) if j in steps}
    rest = [i for i in steps if i not in keep]
    room = max(0, MAX_STEPS - len(keep))
    if room and rest:
        stride = len(rest) / room
        keep |= {rest[int(k * stride)] for k in range(room)}
    return [beat for i, beat in enumerate(beats) if beat.kind != "step" or i in keep]


def _end_beat(run, steps, frame_id, redact):
    status = str(run.get("status") or "")
    summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
    word = ENDINGS.get(status, "Couldn’t finish")
    elapsed = _elapsed(run)
    count = f"{len(steps)} {'step' if len(steps) == 1 else 'steps'}"
    note = f"{word} in {elapsed} · {count}" if elapsed else f"{word} · {count}"
    answer = summary.get("answer") if isinstance(summary.get("answer"), str) else None
    if redact or not answer or word != "Done":
        answer = "Done on your iPhone." if word == "Done" else None
    rects = next((beat.rects for beat in reversed(steps)), None)
    beat = Beat("end", " ".join(answer.split()) if answer else word, frame_id, rects, note=note)
    beat.card = {"done": word == "Done"}
    return beat


# -- drawing ---------------------------------------------------------------------------------------------------------

FONT_PATHS = ("/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "/System/Library/Fonts/HelveticaNeue.ttc")
# Chinese, Japanese and Korean, which SF Pro doesn't draw: their own fonts (Hangul first for Korean). Scripts that
# need shaping (Arabic, Hebrew, Thai) fall back to SF Pro's own coverage.
HANGUL = re.compile("[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")
WIDE_SCRIPTS = re.compile("[\u1100-\u11ff\u2e80-\ua4cf\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")
WIDE_FONTS = ("/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/AppleSDGothicNeo.ttc",
              "/Library/Fonts/Arial Unicode.ttf")
KOREAN_FONTS = ("/System/Library/Fonts/AppleSDGothicNeo.ttc", "/Library/Fonts/Arial Unicode.ttf")


class Fonts:
    """SF Pro at a weight (macOS), else Helvetica, else Pillow's own. ``path`` "" forces Pillow's (tests)."""

    def __init__(self, path=None):
        self.path = path
        self.cache = {}

    def get(self, size, weight=400, text=""):
        from PIL import ImageFont
        wide = "korean" if text and HANGUL.search(text) else "cjk" if text and WIDE_SCRIPTS.search(text) else ""
        wide = wide if self.path is None else ""
        key = (round(size), weight, wide)
        if key in self.cache:
            return self.cache[key]
        font = None
        first = KOREAN_FONTS if wide == "korean" else WIDE_FONTS if wide else ()
        paths = (self.path,) if self.path else () if self.path == "" else first + FONT_PATHS
        for path in paths:
            if not path or not os.path.exists(path):
                continue
            try:
                bold = weight >= 560 and path.endswith(("Helvetica.ttc", "Hiragino Sans GB.ttc"))
                font = ImageFont.truetype(path, round(size), index=1 if bold else 0)
            except OSError:
                continue
            try:
                axes = {axis["name"]: axis for axis in font.get_variation_axes()}
                if b"Weight" in axes or "Weight" in axes:
                    values = []
                    for axis in font.get_variation_axes():
                        name = axis["name"].decode() if isinstance(axis["name"], bytes) else axis["name"]
                        values.append(weight if name == "Weight" else
                                      max(axis["minimum"], min(axis["maximum"], size)) if name == "Optical Size"
                                      else axis["default"])
                    font.set_variation_by_axes(values)
            except (OSError, AttributeError, KeyError):
                pass
            break
        if font is None:
            try:
                font = ImageFont.load_default(size=round(size))
            except TypeError:
                font = ImageFont.load_default()
        self.cache[key] = font
        return font


def _rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _greedy(text, font, width, lines):
    """(lines, cut): ``text`` filled greedily into at most ``lines`` lines of ``width`` px."""
    words, out = text.split(), []
    while words and len(out) < lines:
        line = words.pop(0)
        while font.getlength(line) > width and len(line) > 1:  # one long word: break it
            cut = len(line) - 1
            while cut > 1 and font.getlength(line[:cut]) > width:
                cut -= 1
            words.insert(0, line[cut:])
            line = line[:cut]
        while words and font.getlength(f"{line} {words[0]}") <= width:
            line = f"{line} {words.pop(0)}"
        out.append(line)
    return out, bool(words)


def _wrap(text, font, width, lines):
    """``text`` in at most ``lines`` lines of ``width`` px, balanced (no lone last word); the last line ends with
    … when it was cut."""
    out, cut = _greedy(text, font, width, lines)
    if cut and out:
        last = out[-1]
        while last and font.getlength(last + "…") > width:
            last = last[:-1].rstrip()
        out[-1] = last + "…"
        return out
    if len(out) > 1:
        low, high = width * 0.4, width
        while high - low > 2:
            middle = (low + high) / 2
            trial, trial_cut = _greedy(text, font, middle, lines)
            if trial_cut or len(trial) > len(out):
                low = middle
            else:
                high = middle
        out = _greedy(text, font, high, lines)[0]
    return out


class Painter:
    """Lays out one frame of the clip. Sizes are for ``scale`` 1 (a 624 × 780 canvas, 4:5)."""

    W, H = 624, 780
    SCREEN_H, SCREEN_MAX_W, BEZEL, TOP = 520, 320, 8, 36
    SS = 3  # supersampling for smooth edges

    def __init__(self, theme="paper", scale=1.0, fonts=None, aspect=222 / 480):
        from PIL import Image
        self.theme = THEMES[theme if theme in THEMES else "paper"]
        self.scale = scale
        self.fonts = fonts or Fonts()
        s = self.s
        self.size = (s(self.W) // 2 * 2, s(self.H) // 2 * 2)
        sh = s(self.SCREEN_H)
        sw = min(s(self.SCREEN_MAX_W), round(sh * aspect))
        self.screen_size = (sw, sh)
        self.radius = round(sw * 0.13)
        bezel = s(self.BEZEL)
        x = (self.size[0] - sw) // 2
        self.screen_box = (x, s(self.TOP) + bezel, x + sw, s(self.TOP) + bezel + sh)
        self.phone_box = (x - bezel, s(self.TOP), x + sw + bezel, s(self.TOP) + sh + 2 * bezel)
        self.caption_top = self.phone_box[3] + s(26)
        self.screen_mask = self._rounded_mask(self.screen_size, self.radius)
        self.ground = Image.new("RGB", self.size, _rgb(self.theme["ground"]))
        self.base = self._base()

    def s(self, value):
        return int(round(value * self.scale))

    def _rounded_mask(self, size, radius):
        from PIL import Image, ImageDraw
        k = self.SS
        mask = Image.new("L", (size[0] * k, size[1] * k), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] * k - 1, size[1] * k - 1), radius * k, fill=255)
        return mask.resize(size, Image.LANCZOS)

    def _base(self):
        """The ground, the phone's shadow and its frame, and the corner mark: the same in every frame."""
        from PIL import Image, ImageDraw, ImageFilter
        t, s = self.theme, self.s
        image = self.ground.copy()
        x0, y0, x1, y1 = self.phone_box
        outer = self.radius + s(self.BEZEL)
        shadow = Image.new("L", self.size, 0)
        ImageDraw.Draw(shadow).rounded_rectangle((x0 + s(4), y0 + s(14), x1 - s(4), y1 + s(10)), outer, fill=255)
        shadow = shadow.filter(ImageFilter.GaussianBlur(s(18)))
        r, g, b, a = t["shadow"]
        image.paste(Image.new("RGB", self.size, (r, g, b)), (0, 0), shadow.point(lambda v: v * a // 255))
        rim = self._rounded_mask((x1 - x0, y1 - y0), outer)
        image.paste(Image.new("RGB", rim.size, _rgb(t["rim"])), (x0, y0), rim)
        inset = max(1, s(1.5))
        body = self._rounded_mask((x1 - x0 - 2 * inset, y1 - y0 - 2 * inset), outer - inset)
        image.paste(Image.new("RGB", body.size, _rgb(t["bezel"])), (x0 + inset, y0 + inset), body)
        self._footer(image)
        return image

    def _mark(self, height):
        """The mark (docs/brand/logo/mark-light.svg, on its 512 grid): a phone body with two eyes looking up and
        right. Returns (RGBA image)."""
        from PIL import Image, ImageDraw
        k = self.SS * 4
        unit = height * k / 436
        w, h = round(224 * unit), round(436 * unit)
        image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, 0, w - 1, h - 1), round(64 * unit), fill=_rgb(self.theme["mark"]) + (255,))
        for cx in (248, 328):
            ex, ey = (cx - 144) * unit, (166 - 38) * unit
            draw.rounded_rectangle((ex - 25 * unit, ey - 39 * unit, ex + 25 * unit, ey + 39 * unit), 25 * unit,
                                   fill=(255, 255, 255, 255))
        return image.resize((max(1, round(w / k)), max(1, round(h / k))), Image.LANCZOS)

    def _footer(self, image):
        from PIL import ImageDraw
        s, t = self.s, self.theme
        font = self.fonts.get(s(14), 500)
        text = "mobster.dev"
        width = font.getlength(text)
        mark = self._mark(s(16))
        right, baseline = self.size[0] - s(22), self.size[1] - s(20)
        x = right - width
        ImageDraw.Draw(image).text((x, baseline), text, font=font, fill=_rgb(t["ink3"]), anchor="ls")
        image.paste(mark, (round(x - s(7) - mark.width), baseline - mark.height + s(2)), mark)

    # -- the screen

    def screen(self, data, rects=None, redact=False, blur=0):
        """A frame's JPEG bytes as the phone's screen: fitted, blurred where ``redact`` says, or all over (``blur``)."""
        from PIL import Image, ImageFilter, ImageOps
        try:
            image = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            return None
        if redact:  # on the frame as stored, where its element frames are
            image = blur_regions(image, rects)
        image = ImageOps.pad(image, self.screen_size, Image.LANCZOS, color=(0, 0, 0))
        if blur:
            image = image.filter(ImageFilter.GaussianBlur(self.s(blur)))
        return image

    def blank_screen(self, lock=False):
        from PIL import Image, ImageDraw
        t, s = self.theme, self.s
        image = Image.new("RGB", self.screen_size, _rgb(t["sunken"]))
        if lock:
            draw = ImageDraw.Draw(image)
            cx, cy = self.screen_size[0] // 2, self.screen_size[1] // 2
            draw.rounded_rectangle((cx - s(22), cy - s(6), cx + s(22), cy + s(30)), s(7), fill=_rgb(t["ink3"]))
            draw.arc((cx - s(14), cy - s(30), cx + s(14), cy + s(6)), 180, 360, fill=_rgb(t["ink3"]), width=s(6))
            draw.line((cx - s(14), cy - s(12), cx - s(14), cy - s(4)), fill=_rgb(t["ink3"]), width=s(6))
            draw.line((cx + s(14), cy - s(12), cx + s(14), cy - s(4)), fill=_rgb(t["ink3"]), width=s(6))
        return image

    # -- one frame

    def frame(self, beat, screen, typed=None, pressed=False):
        """The canvas for ``beat`` with ``screen`` (an image of screen_size, or None) on the phone."""
        from PIL import Image, ImageDraw
        image = self.base.copy()
        x0, y0 = self.screen_box[:2]
        if screen is None:
            screen = self.blank_screen(lock=beat.kind == "secret")
        if beat.kind == "ask":
            screen = Image.blend(screen, Image.new("RGB", screen.size, (0, 0, 0)), 0.32)
        image.paste(screen, (x0, y0), self.screen_mask)
        draw = ImageDraw.Draw(image)
        if beat.kind == "title":
            self._prompt(image, draw, beat.caption, typed)
        else:
            self._caption(draw, beat)
        if beat.kind == "ask":
            self._card(image, beat.card, pressed)
        return image

    def _centered(self, draw, lines, font, top, color, line_height):
        for index, line in enumerate(lines):
            draw.text((self.size[0] / 2, top + index * line_height), line, font=font, fill=color, anchor="ma")

    def _caption(self, draw, beat):
        s, t = self.s, self.theme
        top = self.caption_top
        small = self.fonts.get(s(15), 500)
        if beat.kind == "step":
            label = f"{beat.n} of {beat.total}"
            draw.text((self.size[0] / 2, top), label, font=small, fill=_rgb(t["ink3"]), anchor="ma")
        elif beat.kind == "end":
            done = bool(beat.card and beat.card.get("done"))
            width = small.getlength(beat.note)
            dot = s(16) if done else 0
            gap = s(7) if done else 0
            x = (self.size[0] - width - dot - gap) / 2
            if done:
                cy = top + s(9)
                draw.ellipse((x, cy - dot / 2, x + dot, cy + dot / 2), fill=_rgb(t["green"]))
                draw.line((x + dot * .28, cy + dot * .02, x + dot * .45, cy + dot * .2, x + dot * .74, cy - dot * .2),
                          fill=_rgb(t["ground"]), width=max(2, s(2)), joint="curve")
            draw.text((x + dot + gap, top), beat.note, font=small, fill=_rgb(t["ink3"]), anchor="la")
        size = 27 if beat.kind != "end" or len(beat.caption) <= 70 else 23
        font = self.fonts.get(s(size), 590, beat.caption)
        lines = _wrap(beat.caption, font, s(548), 3 if beat.kind == "end" else MAX_CAPTION_LINES)
        self._centered(draw, lines, font, top + s(28), _rgb(t["ink"]), s(size * 1.26))

    def _prompt(self, image, draw, goal, typed):
        """The task as typed into Mobster: a pill with the text (its first ``typed`` characters, with a caret, while
        it types; the pill keeps its final size), and the send button."""
        from PIL import Image
        s, t = self.s, self.theme
        typing = typed is not None
        font = self.fonts.get(s(21), 450, goal)
        width = s(560)
        inner = width - s(22) - s(58)
        full = _greedy(goal, font, inner, 2)[0] or [""]
        if typing:  # the final lines, cut to what is typed so far, so words never jump between lines
            left, lines = typed, []
            for line in full:
                lines.append(line[:max(0, left)])
                left -= len(line) + 1
                if left <= 0:
                    break
        else:
            lines = full
        height = s(60) if len(full) == 1 else s(88)
        x0 = (self.size[0] - width) // 2
        y0 = self.caption_top + s(6)
        pill = self._rounded_mask((width, height), min(height // 2, s(28)))
        image.paste(Image.new("RGB", pill.size, _rgb(t["line_strong"])), (x0, y0), pill)
        inner_pill = self._rounded_mask((width - 2 * max(1, s(1)), height - 2 * max(1, s(1))),
                                        min(height // 2, s(28)) - max(1, s(1)))
        image.paste(Image.new("RGB", inner_pill.size, _rgb(t["surface"])), (x0 + max(1, s(1)), y0 + max(1, s(1))),
                    inner_pill)
        line_height = s(28)
        ty = y0 + (height - line_height * len(full)) / 2 + s(3)
        for index, line in enumerate(lines):
            draw.text((x0 + s(22), ty + index * line_height), line, font=font, fill=_rgb(t["ink"]), anchor="la")
        if typing:
            end = x0 + s(22) + font.getlength(lines[-1]) + s(2)
            cy = ty + (len(lines) - 1) * line_height
            draw.rectangle((end, cy - s(1), end + max(2, s(2)), cy + s(23)), fill=_rgb(t["violet"]))
        cx, cy, r = x0 + width - s(32), y0 + height / 2, s(19)
        ready = not typing
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=_rgb(t["violet"] if ready else t["line_strong"]))
        arrow = _rgb("#FFFFFF")
        draw.line((cx, cy + s(9), cx, cy - s(8)), fill=arrow, width=max(2, s(3)))
        draw.line((cx - s(7), cy - s(1), cx, cy - s(8), cx + s(7), cy - s(1)), fill=arrow, width=max(2, s(3)),
                  joint="curve")

    def _card(self, image, card, pressed):
        """The approval as Mobster asked it: the act's question and its two answers, over the phone."""
        from PIL import Image, ImageDraw, ImageFilter
        s, t = self.s, self.theme
        width = s(384)
        title_font = self.fonts.get(s(19), 600, card["title"])
        lines = _wrap(card["title"], title_font, width - s(40), 2)
        height = s(66) + len(lines) * s(25) + s(52)
        x0 = (self.size[0] - width) // 2
        y0 = self.screen_box[1] + round(self.screen_size[1] * 0.50)
        radius = s(20)
        shadow = Image.new("L", image.size, 0)
        ImageDraw.Draw(shadow).rounded_rectangle((x0, y0 + s(10), x0 + width, y0 + height + s(10)), radius, fill=255)
        shadow = shadow.filter(ImageFilter.GaussianBlur(s(20)))
        r, g, b, a = t["shadow"]
        image.paste(Image.new("RGB", image.size, (r, g, b)), (0, 0), shadow.point(lambda v: min(255, v * a * 2 // 255)))
        outline = self._rounded_mask((width, height), radius)
        image.paste(Image.new("RGB", outline.size, _rgb(t["line"])), (x0, y0), outline)
        body = self._rounded_mask((width - 2, height - 2), radius - 1)
        image.paste(Image.new("RGB", body.size, _rgb(t["surface"])), (x0 + 1, y0 + 1), body)
        draw = ImageDraw.Draw(image)
        mark = self._mark(s(15))
        image.paste(mark, (x0 + s(20), y0 + s(18)), mark)
        small = self.fonts.get(s(14), 500)
        draw.text((x0 + s(20) + mark.width + s(8), y0 + s(18) + mark.height / 2), "Mobster",
                  font=small, fill=_rgb(t["ink3"]), anchor="lm")
        for index, line in enumerate(lines):
            draw.text((x0 + s(20), y0 + s(46) + index * s(25)), line, font=title_font, fill=_rgb(t["ink"]),
                      anchor="la")
        button = self.fonts.get(s(15), 600)
        by = y0 + height - s(20) - s(36)
        yes_w = max(s(78), round(button.getlength(card["yes"])) + s(36) + (s(20) if pressed else 0))
        no_w = max(s(78), round(button.getlength(card["no"])) + s(36))
        yes_x = x0 + width - s(20) - yes_w
        no_x = yes_x - s(10) - no_w
        approved = pressed and card.get("decision") == "approved"
        declined = pressed and card.get("decision") in ("denied", "redirected", "timeout")
        no_mask = self._rounded_mask((no_w, s(36)), s(18))
        image.paste(Image.new("RGB", no_mask.size, _rgb(t["ink"] if declined else t["line_strong"])),
                    (no_x, by), no_mask)
        no_in = self._rounded_mask((no_w - 2, s(36) - 2), s(18) - 1)
        if not declined:
            image.paste(Image.new("RGB", no_in.size, _rgb(t["surface"])), (no_x + 1, by + 1), no_in)
        draw.text((no_x + no_w / 2, by + s(18)), card["no"], font=button,
                  fill=_rgb(t["surface"] if declined else t["ink"]), anchor="mm")
        yes_mask = self._rounded_mask((yes_w, s(36)), s(18))
        fill = t["violet_strong"] if approved else t["violet"]
        image.paste(Image.new("RGB", yes_mask.size, _rgb(fill)), (yes_x, by), yes_mask)
        if declined:
            image.paste(Image.blend(image.crop((yes_x, by, yes_x + yes_w, by + s(36))),
                                    Image.new("RGB", (yes_w, s(36)), _rgb(t["surface"])), 0.55), (yes_x, by), yes_mask)
        label_x = yes_x + yes_w / 2 + (s(10) if approved else 0)
        draw.text((label_x, by + s(18)), card["yes"], font=button, fill=(255, 255, 255), anchor="mm")
        if approved:
            cx = label_x - button.getlength(card["yes"]) / 2 - s(12)
            cy = by + s(18)
            draw.line((cx - s(6), cy, cx - s(2), cy + s(4), cx + s(6), cy - s(5)), fill=(255, 255, 255),
                      width=max(2, s(2)), joint="curve")


def blur_regions(image, rects):
    """``image`` with each normalized rect pixelated and blurred past reading; ``rects`` None: the whole screen
    below the status bar (nothing known about it, so nothing on it is shown)."""
    from PIL import Image, ImageFilter
    width, height = image.size
    if rects is None:
        rects = [[0, STATUS_BAR[3], 1, 1 - STATUS_BAR[3]]]
    out = image.copy()
    for rect in rects:
        try:
            x, y, w, h = (float(value) for value in rect)
        except (TypeError, ValueError):
            continue
        pad = 3
        box = (max(0, int(x * width) - pad), max(0, int(y * height) - pad),
               min(width, int(math.ceil((x + w) * width)) + pad), min(height, int(math.ceil((y + h) * height)) + pad))
        if box[2] - box[0] < 2 or box[3] - box[1] < 2:
            continue
        region = out.crop(box)
        small = region.resize((max(1, region.width // 10), max(1, region.height // 10)), Image.BOX)
        region = small.resize(region.size, Image.BICUBIC).filter(ImageFilter.GaussianBlur(3))
        out.paste(region, box[:2])
    return out


# -- the clip --------------------------------------------------------------------------------------------------------

def _frames(beats, read_frame, painter, redact, crossfade):
    """[(RGB image, ms)] for every beat, with the title typed out and a short cross-fade between beats."""
    from PIL import Image
    cache = {}

    def screen(beat, blur=0):
        if not beat.frame_id:
            return None
        key = (beat.frame_id, blur, id(beat) if redact else None)
        if key not in cache:
            data = read_frame(beat.frame_id)
            cache[key] = painter.screen(data, beat.rects, redact, blur) if data else None
        return cache[key]

    out = []

    def add(image, ms):
        if crossfade and out and ms > 0:
            previous = out[-1][0]
            for k in range(1, FADE_STEPS + 1):
                t = k / (FADE_STEPS + 1)
                eased = t * t * (3 - 2 * t)
                out.append((Image.blend(previous, image, eased), FADE_FRAME_MS))
        out.append((image, ms))

    first = next((beat for beat in beats if beat.kind == "step" and beat.frame_id), None)
    for beat in beats:
        if beat.kind == "title":
            backdrop = screen(first, blur=6) if first is not None else None
            text = beat.caption
            cuts = sorted({max(1, round(len(text) * k / 10)) for k in range(1, 11)}) if text else [0]
            for cut in cuts[:-1]:
                out.append((painter.frame(beat, backdrop, typed=cut), TYPE_FRAME_MS))
            out.append((painter.frame(beat, backdrop), TITLE_HOLD_MS))
        elif beat.kind == "step":
            add(painter.frame(beat, screen(beat)), step_ms(beat.caption))
        elif beat.kind == "ask":
            shown = screen(beat)
            add(painter.frame(beat, shown), ASK_MS)
            answered = Beat("ask", beat.card["answer"], beat.frame_id, beat.rects, card=beat.card)
            out.append((painter.frame(answered, shown, pressed=True), ANSWERED_MS))
        elif beat.kind == "secret":
            add(painter.frame(beat, None), SECRET_MS)
        elif beat.kind == "end":
            add(painter.frame(beat, screen(beat)), step_ms(beat.caption) + END_HOLD_MS)
    return out


def _fixed_colors(theme):
    """The theme's own colours, which every frame's palette holds exactly, so the ground and the frame never
    shimmer between frames and stay out of each frame's changed area."""
    names = ("ground", "surface", "sunken", "ink", "ink2", "ink3", "line", "line_strong", "violet", "violet_strong",
             "mark", "green", "bezel", "rim")
    return list(dict.fromkeys([_rgb(THEMES[theme][name]) for name in names] + [(0, 0, 0), (255, 255, 255)]))


def _palette(image, colors, fixed):
    """A palette for ``image``: the fixed colours, then the rest learned from it (median cut, at half size)."""
    from PIL import Image
    learned = image.reduce(2).quantize(colors=max(2, colors - len(fixed)), method=Image.Quantize.MEDIANCUT,
                                       dither=Image.Dither.NONE)
    values = learned.getpalette()[:3 * (colors - len(fixed))]
    palette = [channel for color in fixed for channel in color] + values
    holder = Image.new("P", (1, 1))
    holder.putpalette(palette + [0] * (768 - len(palette)))
    return holder


def encode_gif(images, colors=256, theme="paper", dither=True):
    """GIF bytes for [(RGB image, ms)], looping forever. Each held frame gets its own palette (the theme's colours
    exact, the rest its own); a passing frame (typing, a cross-fade) borrows the next held frame's. Pillow stores
    only the part of each frame that changed."""
    from PIL import Image
    fixed = _fixed_colors(theme)
    mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    palettes, upcoming = [None] * len(images), None
    for index in range(len(images) - 1, -1, -1):
        image, ms = images[index]
        if ms >= 200 or upcoming is None:
            upcoming = _palette(image, colors, fixed)
        palettes[index] = upcoming
    # A passing frame is on screen for a moment: no dithering (a third of the time), the held ones dithered.
    frames = [image.quantize(palette=palette, dither=mode if ms >= 200 else Image.Dither.NONE)
              for (image, ms), palette in zip(images, palettes)]
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=[ms for _, ms in images],
                   loop=0, optimize=False, disposal=1)
    return out.getvalue()


def build_clip(run, read_frame, *, theme="paper", redact=False, crossfade=True, fonts=None,
               max_bytes=GIF_MAX_BYTES, scale=1.0):
    """The run as a GIF under ``max_bytes``. ``read_frame(frame_id)`` returns JPEG bytes or None.

    Raises ValueError(NO_SCREENS) when no step has a screen, ValueError(TOO_LONG) when nothing fits."""
    beats = plan_beats(run, redact=redact)
    aspect = _aspect(beats, read_frame)
    if aspect is None:
        raise ValueError(NO_SCREENS)
    fonts = fonts if fonts is not None else Fonts()
    attempts = [(scale, 256, crossfade), (scale, 128, crossfade), (scale, 128, False), (scale, 64, False),
                (scale * .8, 64, False)]
    for size, colors, fade in attempts:
        painter = Painter(theme, size, fonts, aspect)
        images = _frames(beats, read_frame, painter, redact, fade)
        data = encode_gif(images, colors, theme)
        if len(data) <= max_bytes:
            return Clip(data, len(images), [ms for _, ms in images], beats, images, colors, fade)
    raise ValueError(TOO_LONG)


def _aspect(beats, read_frame):
    from PIL import Image
    for beat in beats:
        if beat.frame_id:
            data = read_frame(beat.frame_id)
            if data:
                try:
                    width, height = Image.open(io.BytesIO(data)).size
                    if width and height:
                        return max(0.3, min(1.0, width / height))
                except Exception:
                    continue
    return None


# -- the MP4 ---------------------------------------------------------------------------------------------------------

def ffmpeg_path():
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def build_mp4(run, read_frame, *, theme="paper", redact=False, fonts=None, scale=1.5, ffmpeg=None):
    """The same clip as an H.264 MP4 (30 fps, sharper text: 1.5 times the GIF's size). None without ffmpeg."""
    ffmpeg = ffmpeg or ffmpeg_path()
    if ffmpeg is None:
        return None
    beats = plan_beats(run, redact=redact)
    aspect = _aspect(beats, read_frame)
    if aspect is None:
        raise ValueError(NO_SCREENS)
    painter = Painter(theme, scale, fonts if fonts is not None else Fonts(), aspect)
    images = _frames(beats, read_frame, painter, redact, True)
    return encode_mp4(images, ffmpeg)


MP4_FPS = 30


def encode_mp4(images, ffmpeg):
    """H.264 MP4 bytes for [(RGB image, ms)], piped to ffmpeg as raw frames at MP4_FPS (each image repeated for its
    time, rounded on the running total so the clip keeps its length)."""
    width, height = images[0][0].size
    with tempfile.TemporaryDirectory(prefix="mobster-clip-") as folder:
        target = Path(folder) / "clip.mp4"
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", f"{width}x{height}", "-r", str(MP4_FPS), "-i", "-", "-vf", "format=yuv420p", "-c:v",
                   "libx264", "-preset", "medium", "-crf", "18", "-movflags", "+faststart", "-an", str(target)]
        elapsed, shown, error, code = 0, 0, b"", -1
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE) as process:
            try:
                for image, ms in images:
                    elapsed += ms
                    count = round(elapsed * MP4_FPS / 1000) - shown
                    shown += count
                    data = image.convert("RGB").tobytes()
                    for _ in range(count):
                        process.stdin.write(data)
                process.stdin.close()
                error = process.stderr.read()
                code = process.wait(timeout=300)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                process.kill()
                error = process.stderr.read()
        if code != 0 or not target.is_file():
            raise RuntimeError("ffmpeg couldn't make the MP4: " + error.decode(errors="replace")[-300:])
        return target.read_bytes()


def file_stem(goal):
    """The file's name without its extension: "Mobster – " and the task, at most 60 characters."""
    words = " ".join(str(goal or "").split())
    words = re.sub(r"[^\w .,'’()&+-]+", " ", words).strip(" .-")
    words = " ".join(words.split())[:60].rstrip(" .-")
    return f"Mobster – {words}" if words else "Mobster task"

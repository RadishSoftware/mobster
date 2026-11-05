"""Reading one feed item (Reels, TikTok, Shorts): who made it, what it says, whether it is an ad (DESIGN.md § 7.3)."""

import re
from dataclasses import dataclass, field

AD_LABEL = re.compile(r"^\s*(sponsored|ad|promoted|advertisement)\s*$", re.I)
PAID = re.compile(r"\bpaid partnership\b", re.I)
CTA = re.compile(r"^\s*(shop now|learn more|install now|install|sign up|book now|order now|get offer|download|"
                 r"visit (site|advertiser|website)|apply now|watch more|get quote|contact us|"
                 r"subscribe now)\s*[›>]?\s*$",
                 re.I)
HANDLE = re.compile(r"^@?([A-Za-z0-9._]{2,30})$")
# Signs of a full-screen video that name no app (10 Oct, one general engine): a screen-covering element whose label or
# identifier says video ("sundial-viewer-video-cell": Instagram's viewer opened from search, which its item label
# missed), and a small "Post by name" control naming the creator.
VIDEO_SURFACE = re.compile(r"(?:^|[\s_-])video(?:[\s_-]|$)", re.I)
BY_HANDLE = re.compile(r"^(?:reel|video|post|short|tiktok) by @?([\w.]{2,30})$", re.I)
# More bylines no app names (real iPhone, 13 Oct: a collab Reel showed no "@handle", only these): a creator's
# picture ("42ledger. Profile picture") and a collab line ("42ledger and fahaadk_"); the first creator is the item's.
PICTURE_OF = re.compile(r"^@?([\w.]{2,30}?)\.?,? profile picture$", re.I)
COLLAB = re.compile(r"^@?([\w.]{2,30}) and @?([\w.]{2,30})$", re.I)
# A playing video's progress bar ("Video progress: 11 percent"): the screen is a video item, whatever its labels.
PROGRESS = re.compile(r"^video progress\b", re.I)
# "Followed by simon": who of the person's follows follows the creator; that person is not the creator.
FOLLOWED_BY = re.compile(r"^followed by @?([\w.]{2,30})\b", re.I)
# An accessibility identifier read as a label ("simon, clips_viewer_clips_tab"): a control's name, never a caption.
IDENTIFIER = re.compile(r"\b[a-z]+(?:_[a-z]+){2,}\b|^[a-z0-9]+(?:-[a-z0-9]+){2,}$")
# The control that expands a collapsed caption: never the caption itself.
MORE_CAPTION = re.compile(r"^more,? (button,? )?tap to see the (whole|full) caption$", re.I)


@dataclass(frozen=True)
class Hints:
    item: re.Pattern            # the full-screen element's label ("Reel by @hoopslab")
    item_area: float            # or any element covering this share of the screen
    creator: re.Pattern         # a label that names the creator; group 1 is the handle
    audio: re.Pattern           # the audio line
    chrome: re.Pattern          # controls and labels that are never the caption
    min_caption: int = 8


_CHROME = (r"^(like|liked|unlike|comment|comments|share|send|save|saved|save post|repost|remix|more|more options|"
           r"more actions|"
           r"options|follow|"
           r"following|back|close|reels|for you|following|friends|home|search|explore|inbox|profile|shop|create|"
           r"camera|mute|unmute|sound|pause|play|subscribe|subscribed|dislike|shorts|\d[\d.,]*[kmb]?|"
           r"original audio|see translation|view insights|boost reel|"
           r"\d[\d.,]*[kmb]? (likes?|comments?|shares?|views?|plays?|reposts?|saves?))$")

# One reader for every app (one general engine, 10 Oct: the per-app hints for Instagram, TikTok and YouTube are gone).
# What a feed item shows is the same everywhere: a full-screen element named "Reel/Video/Post/Short/TikTok by
# @name" or a video surface, a creator handle, a caption, an audio or sound line, and the controls around it.
GENERIC = Hints(item=re.compile(r"^(reel|video|short|post|tiktok) by @?[\w.]+", re.I), item_area=.8,
                creator=re.compile(r"^(?:reel by |video by |post by )?@([\w.-]{2,30})$", re.I),
                audio=re.compile(r"(♫|♪|original (audio|sound)|\baudio\b|\bsound\b)", re.I),
                chrome=re.compile(_CHROME, re.I))


@dataclass
class ItemRead:
    """One screen of a feed, read: None where the tree doesn't say."""
    full_screen: bool = False
    creator: str | None = None
    caption: str | None = None
    audio: str | None = None
    ad: str | None = None             # why it is an ad, or None
    sponsored: bool = False           # a paid partnership: kept, flagged
    overlay: str | None = None        # an Alert or Sheet over the feed
    item_label: bool = False          # the full-screen element is labelled as an item ("Reel by @hoopslab"), or a
                                      # video's progress bar shows
    lines: list = field(default_factory=list)   # every distinct text, top to bottom (for the extractor)

    def signals(self):
        """How many of a feed item's signs this screen shows: a creator, a caption or audio line, an item label."""
        return int(bool(self.creator)) + int(bool(self.caption or self.audio)) + int(self.item_label)

    def ax_text(self):
        parts = []
        if self.creator:
            parts.append(f"Creator: @{self.creator}")
        if self.caption:
            parts.append(f"Caption: {self.caption}")
        if self.audio:
            parts.append(f"Audio: {self.audio}")
        if self.sponsored:
            parts.append("Labelled: Paid partnership")
        rest = [line for line in self.lines if line not in (self.caption, self.audio)
                and not (self.creator and line.lstrip("@") == self.creator)]
        if rest:
            parts.append("Other text: " + " | ".join(rest[:12]))
        return "\n".join(parts)


def _within(rect, outer):
    x, y, w, h = rect
    ox, oy, ow, oh = outer
    return ox - .01 <= x + w / 2 <= ox + ow + .01 and oy - .01 <= y + h / 2 <= oy + oh + .01


def _text(element):
    label = " ".join((getattr(element, "label", "") or "").split())
    value = " ".join((getattr(element, "value", "") or "").split())
    return label if not value or value == label else f"{label} {value}".strip()


def read(snapshot, hints):
    """``snapshot`` (one screen of the feed) as an ItemRead, by ``hints``."""
    out = ItemRead()
    bylines, pictures, followed = [], [], set()
    surface = None
    elements = sorted(getattr(snapshot, "elements", ()) or (), key=lambda e: (e.rect[1], e.rect[0]))
    captions = []
    for element in elements:
        role = (element.role or "").removeprefix("XCUIElementType")
        text = _text(element)
        if role in ("Alert", "Sheet"):
            out.overlay = out.overlay or (text or role)
            continue
        x, y, w, h = element.rect
        # A full-screen item is one the tree names as an item, or a video. An app's root "Other" (or a big cell or
        # picture) covers the screen in almost every app: a home feed, DMs, Stories and card stacks aren't feeds.
        if w * h >= hints.item_area:
            if hints.item.match(text) or role == "Video" or VIDEO_SURFACE.search(text):
                out.full_screen = True
                # Named as an item ("Reel by @name"), or as a video surface: a screen-covering video is an item's sign.
                out.item_label = out.item_label or bool(hints.item.match(text)) or role == "Video" \
                    or bool(VIDEO_SURFACE.search(text))
                surface = surface or element.rect
                match = re.search(r"@([\w.-]{2,30})", text)
                if match and not out.creator:
                    out.creator = match.group(1)
            continue        # a container as big as the screen: its label is never the item's caption
        if PROGRESS.match(text):
            out.item_label = True      # a playing video's own progress bar
            continue
        if MORE_CAPTION.match(text) or IDENTIFIER.search(text):
            continue
        social = FOLLOWED_BY.match(text)
        if social:
            followed.add(social.group(1).casefold())
            continue
        picture = PICTURE_OF.match(text)
        if picture and role in ("Button", "Image", "Link", "Other"):
            pictures.append(picture.group(1))
            continue
        collab = COLLAB.match(text)
        if collab and not out.creator and role in ("Button", "StaticText", "Link", "Other") and len(text) <= 64:
            out.creator = collab.group(1)
            continue
        if not text or hints.item.match(text):     # a thumbnail of another item (a grid), not this item's text
            if text and BY_HANDLE.match(text):
                bylines.append(BY_HANDLE.match(text).group(1))
            continue
        if AD_LABEL.match(text):
            out.ad = out.ad or text.strip()
            continue
        if PAID.search(text):
            out.sponsored = True
            continue
        if CTA.match(text) and role in ("Button", "Link", "Other", "StaticText"):
            out.ad = out.ad or text.strip()
            continue
        creator = hints.creator.match(text)
        if creator and role in ("Button", "StaticText", "Link") and not out.creator:
            out.creator = creator.group(1)
            continue
        if hints.audio.search(text) and not out.audio and len(text) <= 120:
            out.audio = text
            continue
        if hints.chrome.match(text):
            continue
        if text not in out.lines:
            out.lines.append(text[:300])
        if role in ("StaticText", "TextView", "Other", "Button") and len(text) >= hints.min_caption:
            captions.append((text, element.rect))
    if surface is not None:
        # The item's own text sits on it; a tab bar or a toolbar below a feed that leaves room for one is not a caption.
        captions = [(text, rect) for text, rect in captions if _within(rect, surface)]
    if captions:
        out.caption = max((text for text, _ in captions), key=len)[:300]
    if not out.creator and len(set(bylines)) == 1:
        out.creator = bylines[0]  # one "Post by name" on the screen is its item's creator (a grid has many)
    pictures = [name for name in pictures if name.casefold() not in followed]
    if not out.creator and out.full_screen and 1 <= len(set(pictures)) <= 2:
        out.creator = pictures[-1]  # the creator's picture beside the caption (a collab shows two; the lower is first)
    return out


def ad_reason(item_read):
    """Why this item is an ad ("Sponsored", "Shop now"), or None. A paid partnership is not an ad."""
    return item_read.ad

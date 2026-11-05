"""Who is waiting on you: deterministic rules over the chats ``messages.read_chats`` returns."""

import re
from dataclasses import dataclass, field

WINDOW_DAYS = 30
MIN_AGE_S = 2 * 3600
MAX_ITEMS = 25
CONTEXT = 8
BUCKETS = ("asked", "waiting", "promised")
LABELS = {"asked": "Asked you something", "waiting": "Waiting on you", "promised": "You said you would"}

_ASK = re.compile(
    r"\?|\b(can|could|would|will) (you|u)\b|\b(did|do|are|have|were|was|is) (you|u)\b|\blet me know\b|\blmk\b"
    r"|\bcall me\b|\btext me\b|\bget back to me\b|\bany update\b|\bwhen (can|are|will|do) (you|u)\b"
    r"|\bplease (send|call|text|let|reply|confirm|check)\b|\bwhat do you think\b|\bwhat time\b", re.I)
_PROMISE = re.compile(r"\b(i'll|i will|ill|let me|i can send|i'm going to|im gonna|i am going to)\b|\btomorrow\b", re.I)
_NOT_A_PROMISE = re.compile(r"\blet me know\b", re.I)
_CLOSER_WORDS = frozenset((
    "ok okay k kk thanks thank you thx ty tysm np yw lol lmao haha hahaha hehe ha cool nice great awesome perfect "
    "sounds "
    "good got it yep yup yes yeah sure bet will do right same true word love u so much see ya later bye night "
    "goodnight "
    "gn lovely amazing wonderful appreciate appreciated all fine alright sweet noted").split())


def clean(text):
    return (text or "").replace("’", "'").replace("‘", "'").strip()


def is_closer(text):
    """Whether a message ends a conversation instead of asking for a reply: "ok", "thanks!", "lol", "👍", "😂😂"."""
    text = clean(text)
    if not text:
        return True
    if "?" in text:
        return False
    if not re.search(r"[^\W_]", text):       # no letter or digit: an emoji, "!!!", "..."
        return True
    words = re.findall(r"[^\W_]+(?:'[^\W_]+)?", text.lower())
    return 0 < len(words) <= 5 and all(word in _CLOSER_WORDS for word in words)


def asks(text):
    return bool(_ASK.search(clean(text)))


def promises(text):
    """Whether the message promises something ("I'll send it tomorrow", "let me check"). "Let me know" doesn't."""
    return bool(_PROMISE.search(_NOT_A_PROMISE.sub(" ", clean(text))))


def is_person_handle(handle):
    """A phone number (7 or more digits) or an email address that isn't a no-reply sender."""
    handle = (handle or "").strip()
    if not handle:
        return False
    lowered = handle.lower()
    if re.search(r"no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer-daemon|notifications?@", lowered):
        return False
    if "@" in handle:
        return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", handle))
    if re.fullmatch(r"\+?[\d\s().-]+", handle):
        return len(re.sub(r"\D", "", handle)) >= 7
    return False


@dataclass
class Item:
    chat: int
    handle: str
    bucket: str
    last: object                         # the Message the item is about (theirs; yours for "promised")
    age_s: float
    context: list = field(default_factory=list)      # up to 8 newest messages, oldest first


def classify(chat, now):
    """The Item for one chat, or None."""
    if not is_person_handle(chat.handle) or not chat.messages:
        return None
    messages = sorted(chat.messages, key=lambda m: (m.when, m.rowid))
    horizon = now - WINDOW_DAYS * 86400
    last = messages[-1]
    if not last.mine:
        age = now - last.when
        if last.when < horizon or age < MIN_AGE_S or is_closer(last.text):
            return None
        bucket = "asked" if asks(last.text) else "waiting"
        return Item(chat.rowid, chat.handle, bucket, last, age, messages[-CONTEXT:])
    # The last message is yours: the newest promise of yours that they haven't answered, unless you sent a file after.
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not message.mine:
            return None
        if message.when < horizon:
            return None
        if promises(message.text) and not message.attachment:
            age = now - message.when
            if age < MIN_AGE_S or any(later.attachment for later in messages[index + 1:]):
                return None
            return Item(chat.rowid, chat.handle, "promised", message, age,
                        messages[max(0, index - CONTEXT + 1):index + 1])
    return None


def rank(chats, now, limit=MAX_ITEMS):
    """The items for ``chats``: by bucket, then the longest wait first, at most ``limit``."""
    items = [item for item in (classify(chat, now) for chat in chats) if item is not None]
    items.sort(key=lambda item: (BUCKETS.index(item.bucket), -item.age_s, item.chat))
    return items[:limit]


def counts(items):
    return {bucket: sum(1 for item in items if item.bucket == bucket) for bucket in BUCKETS}

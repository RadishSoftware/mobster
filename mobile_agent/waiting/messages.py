"""A read-only reader of the Messages history on this Mac (``~/Library/Messages/chat.db``)."""

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from ..notify_triggers.sources import MAC_EPOCH

ONE_TO_ONE = 45                  # chat.style
MAX_TEXT = 600                   # characters kept of one message
DEFAULT_PATH = Path.home() / "Library" / "Messages" / "chat.db"
ATTACHMENT_TEXT = "[attachment]"

NEEDED = {
    "message": {"ROWID", "text", "attributedBody", "handle_id", "is_from_me", "date", "item_type",
                "associated_message_type", "cache_has_attachments"},
    "chat": {"ROWID", "style"},
    "chat_message_join": {"chat_id", "message_id"},
    "chat_handle_join": {"chat_id", "handle_id"},
    "handle": {"ROWID", "id"},
}
# SMS fallbacks of a tapback from before iOS 10 and from Android: the reaction as a text.
_REACTION_TEXT = re.compile(r"^(Loved|Liked|Disliked|Laughed at|Emphasized|Questioned|Reacted \S+ to) [“\"]", re.I)
_OBJECT_REPLACEMENT = "￼"


@dataclass(frozen=True)
class Message:
    rowid: int
    mine: bool
    when: float                  # Unix seconds
    text: str
    attachment: bool = False


@dataclass
class Chat:
    rowid: int
    handle: str                  # the other person's phone number or email, as Messages has it
    messages: list = field(default_factory=list)      # oldest first, real messages only


def apple_to_unix(value):
    """Messages' ``date`` as Unix seconds: nanoseconds (macOS 10.13 on) or seconds since 2001-01-01."""
    value = float(value or 0)
    if abs(value) > 1e11:
        value /= 1e9
    return value + MAC_EPOCH


def unix_to_apple_ns(seconds):
    return int((seconds - MAC_EPOCH) * 1_000_000_000)


def decode_attributed_body(blob):
    """The message's plain string from an ``attributedBody`` blob, or None when there isn't one to find."""
    if not blob:
        return None
    # typedstream: NSString, class bytes, "+", the UTF-8 length (a byte, or 0x81/0x82/0x83 and 2/4/8 LE bytes), text.
    data = bytes(blob)
    start = 0
    while True:
        at = data.find(b"NSString", start)
        if at < 0:
            return None
        start = at + 1
        plus = data.find(b"+", at + 8, at + 8 + 12)
        if plus < 0:
            continue
        cursor = plus + 1
        if cursor >= len(data):
            continue
        first = data[cursor]
        if first == 0x81:
            width, cursor = 2, cursor + 1
        elif first == 0x82:
            width, cursor = 4, cursor + 1
        elif first == 0x83:
            width, cursor = 8, cursor + 1
        elif first < 0x80:
            width = 0
        else:
            continue
        if width:
            if cursor + width > len(data):
                continue
            length = int.from_bytes(data[cursor:cursor + width], "little")
            cursor += width
        else:
            length, cursor = first, cursor + 1
        if length <= 0 or cursor + length > len(data):
            continue
        try:
            text = data[cursor:cursor + length].decode("utf-8")
        except UnicodeDecodeError:
            continue
        return text
    return None


def message_text(text, attributed):
    """What a message says: ``text``, else the string in ``attributedBody``; attachments' placeholders removed."""
    words = text if isinstance(text, str) and text.strip() else decode_attributed_body(attributed)
    return (words or "").replace(_OBJECT_REPLACEMENT, "").strip()


def connect(path=None):
    """The database, read-only. ``sqlite3.OperationalError`` when it can't be opened (no file, no access)."""
    target = Path(path) if path else DEFAULT_PATH
    connection = sqlite3.connect(f"file:{quote(str(target))}?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only = ON")
    return connection


def columns_ok(path=None):
    """Whether chat.db has the tables and columns this reader reads (``PRAGMA table_info`` only)."""
    try:
        connection = connect(path)
    except sqlite3.Error:
        return False
    try:
        for table, needed in NEEDED.items():
            have = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not needed <= have:
                return False
        return True
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def read_chats(path, now, days=30, per_chat=40, limit_chats=400):
    """Every one-to-one chat with a real message in the last ``days`` days"""
    since = unix_to_apple_ns(now - days * 86400)
    connection = connect(path)
    try:
        # Newest activity first; a chat with one handle only (a one-to-one chat has exactly one).
        chats = connection.execute(
            "SELECT c.ROWID, MIN(h.id), COUNT(h.ROWID) "
            "FROM chat c JOIN chat_handle_join j ON j.chat_id = c.ROWID JOIN handle h ON h.ROWID = j.handle_id "
            "WHERE c.style = ? AND EXISTS (SELECT 1 FROM chat_message_join m WHERE m.chat_id = c.ROWID) "
            "GROUP BY c.ROWID HAVING COUNT(h.ROWID) = 1", (ONE_TO_ONE,)).fetchall()
        recent = {row[0]: row[1] for row in connection.execute(
            "SELECT j.chat_id, MAX(m.date) FROM chat_message_join j JOIN message m ON m.ROWID = j.message_id "
            "WHERE m.date >= ? GROUP BY j.chat_id", (since,))}
        wanted = sorted((row for row in chats if row[0] in recent), key=lambda row: -recent[row[0]])[:limit_chats]
        out = []
        for rowid, handle, _count in wanted:
            rows = connection.execute(
                "SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me, m.date, m.cache_has_attachments "
                "FROM chat_message_join j JOIN message m ON m.ROWID = j.message_id "
                "WHERE j.chat_id = ? AND m.date >= ? AND COALESCE(m.item_type, 0) = 0 "
                "AND COALESCE(m.associated_message_type, 0) = 0 "
                "ORDER BY m.date DESC, m.ROWID DESC LIMIT ?", (rowid, since, per_chat * 2)).fetchall()
            messages = []
            for mid, text, body, mine, date, attached in rows:
                words = message_text(text, body)
                if words and _REACTION_TEXT.match(words):
                    continue
                if not words:
                    if not attached:
                        continue
                    words = ATTACHMENT_TEXT
                messages.append(Message(mid, bool(mine), apple_to_unix(date), words[:MAX_TEXT], bool(attached)))
                if len(messages) >= per_chat:
                    break
            if messages:
                out.append(Chat(rowid, handle, list(reversed(messages))))
        return out
    finally:
        connection.close()

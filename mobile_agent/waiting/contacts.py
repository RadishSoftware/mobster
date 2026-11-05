"""Names for the people in Messages, from this Mac's own Contacts database, read-only."""

import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

ROOT = Path.home() / "Library" / "Application Support" / "AddressBook"
MATCH_DIGITS = 10


def database_paths(root=None):
    base = Path(root) if root else ROOT
    found = sorted(base.glob("Sources/*/AddressBook-v22.abcddb"))
    top = base / "AddressBook-v22.abcddb"
    return ([top] if top.is_file() else []) + found


def digits(value):
    return re.sub(r"\D", "", value or "")


def phone_key(value):
    """The last 10 digits of a phone number, or None when it has fewer than 7 digits."""
    number = digits(value)
    return number[-MATCH_DIGITS:] if len(number) >= 7 else None


def _name(first, last, nick, org):
    full = " ".join(part.strip() for part in (first, last) if part and part.strip())
    return full or (nick or "").strip() or (org or "").strip() or None


class ContactBook:
    """Name lookup by handle. ``ContactBook.load(paths)``; an unreadable database is skipped."""

    def __init__(self):
        self.by_phone, self.by_email = {}, {}

    @classmethod
    def load(cls, paths=None):
        book = cls()
        for path in (database_paths() if paths is None else paths):
            try:
                book._read(path)
            except sqlite3.Error:
                continue
        return book

    def _read(self, path):
        connection = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=3)
        try:
            connection.execute("PRAGMA query_only = ON")
            names = {row[0]: _name(*row[1:]) for row in connection.execute(
                "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION FROM ZABCDRECORD")}
            for owner, number in connection.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                key, name = phone_key(number), names.get(owner)
                if key and name:
                    self.by_phone.setdefault(key, name)
            for owner, address in connection.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                name = names.get(owner)
                if address and name:
                    self.by_email.setdefault(address.strip().lower(), name)
        finally:
            connection.close()

    def name(self, handle):
        """The contact's name for a Messages handle, or None."""
        handle = (handle or "").strip()
        if "@" in handle:
            return self.by_email.get(handle.lower())
        key = phone_key(handle)
        return self.by_phone.get(key) if key else None

"""The ledger of the writes Mobster makes to Apple: durable before a write is sent, and the reason a write whose outcome
is unknown is never sent again.

One JSON line per event in ``team-signing/ledger.jsonl`` (private, fsynced: journal.append_jsonl). ``begin`` writes
the intent first and refuses when the same write's last event is an intent or an unknown outcome, or when it is
already made. It records names, UDIDs, bundle ids and Apple's ids. It never records the API key, a token, a certificate
signing request, a certificate or a profile.
"""

import fcntl
import json
import time
from pathlib import Path

from ..devkit import DevError
from ..devkit.asc import UNKNOWN_FIX, WriteUnknown
from ..journal import append_jsonl

OPEN = ("intent", "unknown")
MADE = ("done", "resolved_done")


class LedgerError(DevError):
    """The ledger can't be read, so no write is sent."""


class Ledger:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock

    def events(self):
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError:
            raise LedgerError(
                "Mobster can't read its record of changes to App Store Connect, so it sent nothing.",
                f"Check {self.path}.",
            ) from None
        try:
            return [json.loads(line) for line in lines if line.strip()]
        except ValueError:
            raise LedgerError(
                "Mobster's record of changes to App Store Connect is damaged, so it sent nothing.",
                f"Move {self.path} aside to start a new one.",
            ) from None

    def last(self, digest):
        found = [event for event in self.events() if event.get("digest") == digest]
        return found[-1] if found else None

    def _append(self, event):
        append_jsonl(self.path, {**event, "at": round(self.clock(), 3)})

    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path.with_suffix(".lock"), "a")
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    def begin(self, write, digest, summary):
        """Make the attempt durable. Raises (and sends nothing) when the last event for this write is an unknown
        outcome or an attempt that never finished, or when it is already made."""
        handle = self._lock()
        try:
            last = self.last(digest)
            if last and last["event"] in OPEN:
                raise WriteUnknown(
                    f"An earlier try of {write.replace('_', ' ')} never finished, so it may have gone through.",
                    UNKNOWN_FIX,
                )
            if last and last["event"] in MADE:
                raise DevError(f"Mobster already made this change ({write.replace('_', ' ')}).")
            self._append({"event": "intent", "write": write, "digest": digest, "summary": summary})
        finally:
            handle.close()

    def end(self, digest, outcome, detail=None):
        """How a write ended: done, rejected or unknown."""
        self._append({"event": outcome, "digest": digest, "detail": detail or {}})

    def unresolved(self):
        """The writes whose outcome nobody knows: [{digest, write, summary}], oldest first."""
        latest, intents = {}, {}
        for event in self.events():
            latest[event["digest"]] = event["event"]
            if event["event"] == "intent":
                intents[event["digest"]] = event
        return [
            {"digest": digest, "write": intents[digest]["write"], "summary": intents[digest]["summary"]}
            for digest, state in latest.items()
            if state in OPEN and digest in intents
        ]

    def resolve(self, digest, made):
        """The person looked at Apple and says whether the change was made. Only an unresolved write can be resolved."""
        handle = self._lock()
        try:
            last = self.last(digest)
            if not last or last["event"] not in OPEN:
                raise DevError("That change isn't waiting on you.")
            self._append({"event": "resolved_done" if made else "resolved_not_done", "digest": digest})
        finally:
            handle.close()

"""Messages the user sends a run while it works (seam S8). Frozen after the seams merge.

``Run.steer`` is the only way in: it applies the approval-pending and origin rules (server.py, S5) and emits
``user_message``. The frontier drains the queue at the top of each turn (S7.2); ``Run.finish`` closes it, and the
messages nobody read come back so the run can emit ``steer_unread``. ``paused`` is set when a pause was asked for;
harness consumes it.
"""

from dataclasses import dataclass
import threading
import time
import uuid

SOURCES = ("app", "tui", "cli", "mcp", "voice")


@dataclass(frozen=True)
class SteerMessage:
    id: str
    text: str
    at: float          # time.time() when it was queued
    source: str        # one of SOURCES


class SteeringQueue:
    MAX_PENDING = 10
    MAX_CHARS = 2000

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = []
        self._closed = False
        self.paused = threading.Event()

    def put(self, text, *, source="app"):
        """Queue ``text``. ValueError (a sentence) when it is empty, longer than MAX_CHARS, the queue holds
        MAX_PENDING unread messages, the source is unknown, or the run has finished (closed)."""
        if not isinstance(text, str):
            raise ValueError("A message is text")
        text = text.strip()
        if not text:
            raise ValueError("Write a message first")
        if len(text) > self.MAX_CHARS:
            raise ValueError(f"A message can be at most {self.MAX_CHARS:,} characters")
        if source not in SOURCES:
            raise ValueError("Unknown message source")
        with self._lock:
            if self._closed:
                raise ValueError("This task has finished")
            if len(self._pending) >= self.MAX_PENDING:
                raise ValueError("Mobster's agent has 10 messages it hasn't read yet. Wait for it to catch up.")
            message = SteerMessage(uuid.uuid4().hex[:12], text, time.time(), source)
            self._pending.append(message)
        return message

    def drain(self):
        """Every unread message, oldest first; the queue is empty after."""
        with self._lock:
            out, self._pending = self._pending, []
        return out

    def pending(self):
        with self._lock:
            return len(self._pending)

    @property
    def closed(self):
        with self._lock:
            return self._closed

    def close(self):
        """Close the queue; returns the unread messages. ``put`` raises afterwards. Idempotent."""
        with self._lock:
            self._closed = True
            out, self._pending = self._pending, []
        return out

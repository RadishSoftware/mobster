"""Messages the user sends a run while it works: ``Run.steer`` queues them and the frontier drains them each turn."""

import threading
import time
import uuid
from dataclasses import dataclass

SOURCES = ("app", "tui", "cli", "mcp", "voice", "remote")


@dataclass(frozen=True)
class SteerMessage:
    id: str
    text: str
    at: float
    source: str


class SteeringFull(ValueError):
    pass


class SteeringQueue:
    MAX_PENDING = 10
    MAX_CHARS = 2000

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = []
        self._closed = False
        self.paused = threading.Event()

    def put(self, text, *, source="app"):
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
                raise SteeringFull("Mobster's agent has 10 messages it hasn't read yet. Wait for it to catch up.")
            message = SteerMessage(uuid.uuid4().hex[:12], text, time.time(), source)
            self._pending.append(message)
        return message

    def drain(self):
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
        with self._lock:
            self._closed = True
            out, self._pending = self._pending, []
        return out

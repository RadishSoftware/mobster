"""The in-memory event bus behind ``GET /api/events`` (seam S4). Frozen after the seams merge.

Every event gets an id ``<boot>-<seq>``: ``boot`` is 8 hex characters, new per process, so a client that reconnects
after the sidecar restarted (or after its last id fell out of the ring buffer) gets ``{"event": "reset"}`` and then
the whole buffer for its topics, and refetches its lists. Nothing is persisted.

Core publishes ``runs``: ``run_created`` in ``Runtime.create`` and ``run_finished`` after the run's listeners.
Tracks publish their own topics (``threads``, ``thread:<id>``, ``memory``, ``attachments``, ``devices``).
"""

from collections import deque
import re
import secrets
import threading
import time

TOPIC = re.compile(r"[a-z]{3,16}(:[a-f0-9]{12})?")
MAX_TOPICS = 8


def parse_topics(value):
    """``"runs,thread:3f9c2a1b7d0e"`` -> {"runs", "thread:3f9c2a1b7d0e"}; ValueError (a sentence) when a topic is
    malformed or there are more than MAX_TOPICS."""
    names = [name.strip() for name in str(value or "").split(",") if name.strip()]
    if not names:
        raise ValueError("Name at least one topic, such as runs")
    if len(names) > MAX_TOPICS:
        raise ValueError(f"Listen to at most {MAX_TOPICS} topics per stream")
    for name in names:
        if not TOPIC.fullmatch(name):
            raise ValueError("A topic is 3 to 16 lowercase letters, optionally followed by : and a 12-character id")
    return set(names)


class EventBus:
    def __init__(self, capacity=2048):
        self.capacity = int(capacity)
        self.boot = secrets.token_hex(4)
        self._events = deque(maxlen=self.capacity)
        self._seq = 0
        self._condition = threading.Condition()

    # -- writing

    def publish(self, topic, event):
        """Add ``event`` under ``topic``; returns its id. ValueError for a malformed topic or a non-dict event."""
        if not isinstance(topic, str) or not TOPIC.fullmatch(topic):
            raise ValueError("Invalid event topic")
        if not isinstance(event, dict):
            raise ValueError("An event is a JSON object")
        with self._condition:
            self._seq += 1
            entry = {**event, "id": f"{self.boot}-{self._seq}", "topic": topic, "timestamp": time.time() * 1000,
                     "_seq": self._seq}
            self._events.append(entry)
            self._condition.notify_all()
        return entry["id"]

    # -- reading

    def position(self):
        """The id of the newest event (``<boot>-0`` before any): a stream that starts now resumes after it."""
        with self._condition:
            return f"{self.boot}-{self._seq}"

    def _parse(self, last_id):
        """The sequence number ``last_id`` names in this boot, or None when it is another boot's or malformed."""
        if not isinstance(last_id, str):
            return None
        boot, _, seq = last_id.partition("-")
        if boot != self.boot or not seq.isdigit():
            return None
        return int(seq)

    def _since(self, topics, last_id):
        """(events, cursor) under the condition. ``cursor`` is the newest seq looked at."""
        last = self._parse(last_id)
        oldest = self._events[0]["_seq"] if self._events else self._seq + 1
        if last is None or last > self._seq or last < oldest - 1:
            reset = {"event": "reset", "id": f"{self.boot}-{self._seq}", "topic": "",
                     "timestamp": time.time() * 1000}
            return [reset] + [self._public(e) for e in self._events if e["topic"] in topics], self._seq
        return [self._public(e) for e in self._events if e["_seq"] > last and e["topic"] in topics], self._seq

    @staticmethod
    def _public(entry):
        return {k: v for k, v in entry.items() if k != "_seq"}

    def since(self, topics, last_id):
        """Events for ``topics`` after ``last_id``, oldest first. None -> []. An id from another boot, or older
        than the buffer -> [{"event": "reset"}] + the whole buffer for those topics."""
        if last_id is None:
            return []
        with self._condition:
            return self._since(set(topics), last_id)[0]

    def wait(self, topics, last_id, timeout):
        """``since``, waiting up to ``timeout`` seconds for an event when there is none yet. None starts now."""
        events, _ = self._wait(set(topics), last_id, timeout)
        return events

    def _wait(self, topics, last_id, timeout):
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            cursor = last_id if last_id is not None else f"{self.boot}-{self._seq}"
            while True:
                events, seq = self._since(topics, cursor)
                if events:
                    return events, f"{self.boot}-{seq}"
                cursor = f"{self.boot}-{seq}"  # nothing for these topics up to here: never re-scan it
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], cursor
                self._condition.wait(remaining)

    def stream(self, topics, last_id, *, keepalive_s=5.0, closed=lambda: False):
        """For ``GET /api/events``: yields each event for ``topics``, and None (a keepalive) after ``keepalive_s``
        without one. Starts after ``last_id`` (a reconnect's Last-Event-ID), or now."""
        topics = set(topics)
        cursor = last_id
        if cursor is not None:
            with self._condition:
                events, seq = self._since(topics, cursor)
            cursor = f"{self.boot}-{seq}"
            yield from events
        while not closed():
            events, cursor = self._wait(topics, cursor, keepalive_s)
            if not events:
                yield None
            yield from events


bus = EventBus()
publish = bus.publish

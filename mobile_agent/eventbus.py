"""The in-memory event bus behind ``GET /api/events``: ids are ``<boot>-<seq>``, and a stale id gets a reset."""

import re
import secrets
import threading
import time
from collections import deque

TOPIC = re.compile(r"[a-z]{3,16}(:[a-f0-9]{12})?")
MAX_TOPICS = 8


def parse_topics(value):
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

    def publish(self, topic, event):
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

    def position(self):
        with self._condition:
            return f"{self.boot}-{self._seq}"

    def _parse(self, last_id):
        if not isinstance(last_id, str):
            return None
        boot, _, seq = last_id.partition("-")
        if boot != self.boot or not seq.isdigit():
            return None
        return int(seq)

    def _since(self, topics, last_id):
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
        if last_id is None:
            return []
        with self._condition:
            return self._since(set(topics), last_id)[0]

    def wait(self, topics, last_id, timeout):
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
                cursor = f"{self.boot}-{seq}"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], cursor
                self._condition.wait(remaining)

    def stream(self, topics, last_id, *, keepalive_s=5.0, closed=lambda: False):
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

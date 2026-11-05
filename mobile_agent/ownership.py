"""Who holds a phone, and who waits for it."""

import os
import re
import threading
import time
from dataclasses import dataclass

KINDS = ("task", "agent", "mcp", "schedule", "remote", "manual")

# How many requests wait for one phone, and how long a request waits before it ends without starting.
QUEUE_LIMIT = 10
QUEUE_TTL_SECONDS = 30 * 60
QUEUE_TTL_ENV = "MOBSTER_QUEUE_MINUTES"
# After "Take it back" the queue holds this long so the person can use the phone; "Resume queue" ends the hold.
TAKE_BACK_GRACE_SECONDS = 120

ORIGIN_KINDS = {"app": "task", "tui": "task", "cli": "task", "mcp": "mcp", "remote": "remote",
                "workflow": "schedule", "schedule": "schedule", "api": "agent", "hook": "schedule",
                "trigger": "schedule", "console": "remote"}
ORIGIN_LABELS = {"app": "a task in Mobster", "tui": "a task in the terminal", "cli": "a task from the command line",
                 "mcp": "an MCP client", "remote": "Mobster Remote", "workflow": "a workflow",
                 "schedule": "a scheduled workflow", "api": "an app using the local API",
                 "hook": "a workflow started by a webhook", "trigger": "a task started by a notification",
                 "console": "your team's console"}

QUEUE_FULL = ("{count} requests already wait for {phone}. Try again when one of them has started.")
QUEUE_EXPIRED = ("This task waited {minutes} minutes for {phone} and never started. Nothing was done on the phone. "
                 "Send it again when the phone is free.")
QUEUE_CANCELLED = "You cancelled this task while it waited for {phone}. Nothing was done on the phone."
QUEUE_RESTART = (
    "Mobster restarted while this task waited for the phone. It never started and did nothing. Send it again.")
ANOTHER_PROCESS = "another Mobster process"


_request = threading.local()


def set_client(name):
    _request.client = name


def current_client():
    return getattr(_request, "client", None)


class QueueFull(Exception):
    """The phone's queue is at its limit."""


def ttl_seconds(environ=None):
    """How long a request waits, from MOBSTER_QUEUE_MINUTES (1 to 1440), else 30 minutes."""
    raw = (os.environ if environ is None else environ).get(QUEUE_TTL_ENV)
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        return QUEUE_TTL_SECONDS
    return minutes * 60 if 1 <= minutes <= 1440 else QUEUE_TTL_SECONDS


KNOWN_CLIENTS = {"claude-code": "Claude Code", "claude code": "Claude Code", "claude-ai": "Claude",
                 "claude": "Claude", "codex": "Codex", "codex-mcp-client": "Codex", "cursor": "Cursor",
                 "cursor-vscode": "Cursor", "windsurf": "Windsurf", "gemini-cli": "Gemini CLI",
                 "vscode": "VS Code", "visual studio code": "VS Code", "zed": "Zed", "cline": "Cline"}


def clean_client(name):
    # a label for people to read: it grants nothing
    if not isinstance(name, str):
        return None
    name = re.sub(r"[^A-Za-z0-9 .@+/()_-]", "", name, flags=re.ASCII).strip()[:40].strip()
    return KNOWN_CLIENTS.get((name or "").casefold(), name) or None


def sentence_case(text):
    """``text`` with its first letter capitalized (a label starts a sentence)."""
    return text[:1].upper() + text[1:] if text else text


def holder_for(run, *, client=None, stopping=None, workflow=None):
    origin = getattr(run, "origin", None) or "api"
    kind = ORIGIN_KINDS.get(origin, "agent")
    label = ORIGIN_LABELS.get(origin, "an app using the local API")
    if origin == "mcp":
        name = clean_client(client)
        label = name or label
    elif origin in ("workflow", "schedule"):
        name = clean_client(workflow)
        if name:
            label = f"the {'scheduled ' if origin == 'schedule' else ''}workflow {name}"
    started = getattr(run, "started_at", None) or getattr(run, "created_at", None) or time.time()
    if stopping is None:
        stop = getattr(run, "stop", None)
        stopping = bool(stop is not None and stop.is_set()) or bool(getattr(run, "taking_back", False))
    return {"kind": kind, "label": label, "runId": getattr(run, "id", None), "since": int(started * 1000),
            "stopping": bool(stopping)}


def manual_holder(label, since):
    return {"kind": "manual", "label": label, "runId": None, "since": int(since * 1000), "stopping": False}


def elsewhere_holder(since=None):
    return {"kind": "agent", "label": ANOTHER_PROCESS, "runId": None,
            "since": int((since or time.time()) * 1000), "stopping": False}


def waiting_message(holder, phone, position):
    """What a queued request is told: who it waits for, on which phone, and its number."""
    who = holder["label"] if holder else "the phone's current task"
    return f"Waiting for {who} on {phone}, you are number {position}."


def holding_sentence(holder):
    """"Claude Code is using this phone", for a holder record ("You are using this phone" for your own hands)."""
    label = holder["label"]
    return f"{sentence_case(label)} {'are' if label.casefold() == 'you' else 'is'} using this phone"


@dataclass
class Entry:
    id: str            # the queued run's id
    kind: str
    label: str
    since: float       # epoch seconds
    expires_at: float  # epoch seconds


# fail closed: an entry is a promise to try, never to act; the runtime checks phone, lease and limits again at the front
class PhoneQueue:
    """The requests that wait for one phone, first come first served. Not thread-safe: the runtime's lock guards it."""

    def __init__(self, clock=time.time, limit=QUEUE_LIMIT, ttl=QUEUE_TTL_SECONDS):
        self.clock, self.limit, self.ttl = clock, limit, ttl
        self.entries = []
        self.held_until = None   # Take it back: no request starts before this (epoch seconds)

    def __len__(self):
        return len(self.entries)

    def add(self, run_id, kind, label):
        """Put a request at the back; QueueFull at the limit. Returns its Entry."""
        if len(self.entries) >= self.limit:
            raise QueueFull(self.limit)
        now = self.clock()
        entry = Entry(run_id, kind, label, now, now + self.ttl)
        self.entries.append(entry)
        return entry

    def remove(self, run_id):
        """Take a request out (cancelled, expired, started). True when it was waiting."""
        before = len(self.entries)
        self.entries = [entry for entry in self.entries if entry.id != run_id]
        return len(self.entries) != before

    def position(self, run_id):
        for index, entry in enumerate(self.entries, 1):
            if entry.id == run_id:
                return index
        return None

    def head(self):
        return self.entries[0] if self.entries else None

    def expired(self):
        """Remove and return the requests that waited out their time."""
        now = self.clock()
        gone = [entry for entry in self.entries if entry.expires_at <= now]
        if gone:
            self.entries = [entry for entry in self.entries if entry.expires_at > now]
        return gone

    def hold(self, seconds=TAKE_BACK_GRACE_SECONDS):
        self.held_until = self.clock() + seconds

    def release(self):
        self.held_until = None

    def held(self):
        """Whether the queue is paused after Take it back."""
        if self.held_until is None:
            return False
        if self.clock() >= self.held_until:
            self.held_until = None
            return False
        return True

    def view(self):
        """[{id, kind, label, position, since}] with ``since`` in epoch milliseconds."""
        return [{"id": entry.id, "kind": entry.kind, "label": entry.label, "position": index,
                 "since": int(entry.since * 1000)} for index, entry in enumerate(self.entries, 1)]


class Queues:
    """One ``PhoneQueue`` per phone key, made on first use. The runtime's lock guards it."""

    def __init__(self, clock=time.time, limit=QUEUE_LIMIT, ttl=None):
        self.clock, self.limit = clock, limit
        self.ttl = QUEUE_TTL_SECONDS if ttl is None else ttl
        self.by_key = {}

    def of(self, key):
        queue = self.by_key.get(key)
        if queue is None:
            queue = self.by_key[key] = PhoneQueue(self.clock, self.limit, self.ttl)
        return queue

    def find(self, key):
        return self.by_key.get(key)

    def locate(self, run_id):
        """(key, queue) of the queue a request waits in, or (None, None)."""
        for key, queue in self.by_key.items():
            if queue.position(run_id) is not None:
                return key, queue
        return None, None

    def total(self):
        return sum(len(queue) for queue in self.by_key.values())

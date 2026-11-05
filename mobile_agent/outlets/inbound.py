"""Commands from outside: a person in Slack or Discord starts one of your workflows, and Mobster answers where
they asked.

``/mobster run Running late person=Sam`` in Slack (or Discord) reaches the relay, which checks the platform's signature
and hands the command to this Mac over Remote's socket (``{"t": "inbound", "rid", "channel", "sender", "label", "verb",
"text", "key", "reply"}``, answered ``inbound.res``). This module decides what it may do. Off until
``MOBSTER_EXPERIMENTAL_INBOUND=1``.

- **Who.** A person is allowed after they type a link code you made (``new_code``) in Slack or Discord, where the
  platform signs who they are. ``inbound_senders`` holds them; ``forget`` removes one and tells the relay. A
  command from anyone else starts nothing.
- **What.** Only a workflow you turned on for that channel (``inbound_workflows``). The text names it and fills its
  blanks, ``name=value`` each, and nothing else: a value is short masked text, never a step. A word the workflow has no
  blank for refuses the command instead of being ignored.
- **How.** The run's origin is ``hook`` (harness_api.FORCED_ASK_ORIGINS): it asks before it sends, buys, posts or
  deletes whatever Ask before acting says, and the card waits in Mobster on this Mac. Nothing here approves anything.
- **Once.** The platform's command id is kept (``inbound_seen``); the same command twice starts one run.
- **The answer.** The run's start comes back at once, privately to the sender. When the run ends, its status and last
  answer (masked, cut) are posted once to the address the platform gave (inbound_reply.py), privately unless you
  made the sender public. The address is good for 15 minutes (Discord) or 30 (Slack); a run that outlives it posts
  nothing, and its result stays in Mobster. Mobster quitting drops the wait the same way.
"""

import hashlib
import logging
import os
import re
import secrets
import sqlite3
import threading
import time

from .. import secret_filter
from ..api_errors import APIError
from ..journal import JournalError
from ..workflows import starters
from . import hooks, net
from . import inbound_reply as reply_to

log = logging.getLogger("mobster.outlets.inbound")

SCHEMA = "outlet_inbound"
VERSION = 1
FLAG = "MOBSTER_EXPERIMENTAL_INBOUND"
PLATFORMS = ("slack", "discord")
NAMES = {"slack": "Slack", "discord": "Discord"}
SENDER = {"slack": re.compile(r"[A-Z0-9]{3,24}:[A-Z0-9]{3,24}"), "discord": re.compile(r"\d{15,25}")}
VERBS = ("run", "list", "link")
CODE_CHARS = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"        # Crockford base32, as the relay reads a typed code
CODE_SECONDS = 600.0
MAX_CODES = 5
SEEN_KEEP = 3600.0
TEXT_MAX = 1000
ANSWER_CHARS = 1500
POLL_SECONDS = 2.0
WORKFLOW_ID = re.compile(r"[0-9a-f]{12}")
OFF = "Commands aren't turned on in this Mobster."
STRANGER = "Mobster doesn't know this account. In Mobster for Mac, open Settings › Commands and link it."


def on(environ=None):
    return (os.environ if environ is None else environ).get(FLAG) == "1"


def migrate(connection, from_version):
    if from_version < 1:
        connection.execute("""CREATE TABLE IF NOT EXISTS inbound_senders (
            channel TEXT NOT NULL, sender TEXT NOT NULL, label TEXT NOT NULL, public INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, last_at REAL, uses INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (channel, sender))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS inbound_workflows (
            channel TEXT NOT NULL, workflow_id TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY (channel, workflow_id))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS inbound_seen (
            channel TEXT NOT NULL, key TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (channel, key))""")


def refuse(status, message):
    return {"status": status, "message": message, "public": False}


def code_hash(code):
    """What the relay is told of a link code: the hash of its normalized form (``normalize``)."""
    return hashlib.sha256(code.encode()).hexdigest()


def normalize(text):
    """A typed link code as the Mac made it: upper case, no dash or space, O as 0 and I or L as 1."""
    return re.sub(r"[\s-]", "", str(text).upper()).replace("O", "0").replace("I", "1").replace("L", "1")


def words(text):
    return " ".join(str(text).split())


class Store:
    def __init__(self, journal):
        self.journal = journal

    def _read(self, sql, args=()):
        with self.journal.lock:
            if self.journal.connection is None:
                raise JournalError("The run journal is closed")
            try:
                return self.journal.connection.execute(sql, args).fetchall()
            except sqlite3.Error:
                raise JournalError("Mobster could not read its commands") from None

    def senders(self, channel=None):
        where = " WHERE channel=?" if channel else ""
        rows = self._read("SELECT channel, sender, label, public, created_at, last_at, uses FROM inbound_senders"
                          + where + " ORDER BY created_at", (channel,) if channel else ())
        return [{"channel": c, "sender": s, "label": label, "public": bool(p), "createdAt": round(made * 1000),
                 "lastAt": round(last * 1000) if last else None, "uses": uses}
                for c, s, label, p, made, last, uses in rows]

    def sender(self, channel, sender):
        return next((row for row in self.senders(channel) if row["sender"] == sender), None)

    def add_sender(self, channel, sender, label, now):
        with self.journal.transaction() as connection:
            connection.execute("INSERT INTO inbound_senders (channel, sender, label, created_at) VALUES (?,?,?,?) "
                               "ON CONFLICT (channel, sender) DO UPDATE SET label=excluded.label",
                               (channel, sender, label, now))

    def set_public(self, channel, sender, public):
        with self.journal.transaction() as connection:
            connection.execute("UPDATE inbound_senders SET public=? WHERE channel=? AND sender=?",
                               (1 if public else 0, channel, sender))

    def used(self, channel, sender, now):
        with self.journal.transaction() as connection:
            connection.execute("UPDATE inbound_senders SET last_at=?, uses=uses+1 WHERE channel=? AND sender=?",
                               (now, channel, sender))

    def remove_sender(self, channel, sender):
        with self.journal.transaction() as connection:
            connection.execute("DELETE FROM inbound_senders WHERE channel=? AND sender=?", (channel, sender))

    def workflow_ids(self, channel):
        query = "SELECT workflow_id FROM inbound_workflows WHERE channel=? ORDER BY created_at"
        return [row[0] for row in self._read(query, (channel,))]

    def enable(self, channel, workflow_id, now):
        with self.journal.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO inbound_workflows VALUES (?,?,?)", (channel, workflow_id, now))

    def disable(self, channel, workflow_id):
        with self.journal.transaction() as connection:
            connection.execute("DELETE FROM inbound_workflows WHERE channel=? AND workflow_id=?",
                               (channel, workflow_id))

    def seen(self, channel, key, now):
        """True when this command id came before (a replay); else remember it. Old ones go."""
        with self.journal.transaction() as connection:
            connection.execute("DELETE FROM inbound_seen WHERE at < ?", (now - SEEN_KEEP,))
            try:
                connection.execute("INSERT INTO inbound_seen VALUES (?,?,?)", (channel, key, now))
            except sqlite3.IntegrityError:
                return True
        return False


def read_values(workflow, rest):
    """({blank: text}, None) from the words after a workflow's name, or (None, why). ``name=value`` or ``name: value``
    pairs; with one blank and no pair, the whole rest is its value. Anything else is refused, never guessed."""
    names = [blank["name"] for blank in starters.blanks(workflow.get("goal"))]
    if not rest:
        return {}, None
    if not names:
        return None, f"{workflow['name']} takes no values. Send `run {workflow['name']}` alone."
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    marks = list(re.finditer(rf"(?:^|(?<=\s))({alternatives})\s*[=:]\s*", rest, re.IGNORECASE))
    if not marks:
        if len(names) == 1:
            return {names[0]: rest}, None
        return None, f"Say which blank each value fills: {', '.join(f'{name}=…' for name in names)}."
    if marks[0].start() != 0:
        return None, f"Start with a blank's name: {', '.join(f'{name}=…' for name in names)}."
    found = {}
    for here, mark in enumerate(marks):
        end = marks[here + 1].start() if here + 1 < len(marks) else len(rest)
        value = rest[mark.end():end].strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[mark.group(1)] = value
    return found, None


class Watch:
    def __init__(self, channel, reply, public, name, check, deadline):
        self.channel, self.reply, self.public, self.name = channel, reply, public, name
        self.check, self.deadline = check, deadline


class InboundService:
    """The commands this Mac takes. One per Runtime (outlets.register); ``remote`` is Remote's service."""

    def __init__(self, runtime, *, clock=time.time, remote=None, call=None, interval=POLL_SECONDS):
        self.runtime = runtime
        self.store = Store(runtime.journal)
        self.clock = clock
        self._remote = remote
        self.call = call or net.call
        self.interval = interval
        self._codes = {}             # code hash -> (channel, expires)
        self._watches = []
        self._lock = threading.Condition()
        self._thread = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------------------------------------------------

    def close(self):
        with self._lock:
            self._closed = True
            self._lock.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self):
        while True:
            with self._lock:
                if self._closed:
                    return
                self._lock.wait(self.interval)
                if self._closed:
                    return
            try:
                self.poll()
            except Exception:  # noqa: BLE001 -- one bad watch never stops the others
                log.exception("inbound: a wait for a run failed")

    def remote(self):
        if self._remote is not None:
            return self._remote
        try:
            from ..remote import service as remote_service
        except ImportError:
            return None
        return remote_service.service_for(self.runtime)

    # -- what the app does -------------------------------------------------------------------------------------------

    def state(self):
        return {"on": on(), "channels": [
            {"channel": channel, "name": NAMES[channel], "senders": self.store.senders(channel),
             "workflows": self.enabled(channel)} for channel in PLATFORMS]}

    def _channel(self, channel):
        if channel not in PLATFORMS:
            raise ValueError(f"channel is one of {', '.join(PLATFORMS)}")
        return channel

    def new_code(self, channel):
        """A link code to type in Slack or Discord: {"code", "expiresAt"}. The relay learns its hash only."""
        self._channel(channel)
        remote = self.remote()
        if not on():
            raise APIError(OFF, 409, "off")
        if remote is None or not remote.address("hooks"):
            raise APIError("Turn on Mobster Remote first, in Settings › Remote. Commands reach this Mac through it.",
                           409, "remote_off")
        now = self.clock()
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(8))
        digest = code_hash(code)
        with self._lock:
            for old in [h for h, v in self._codes.items() if v[1] <= now]:
                del self._codes[old]
            if len(self._codes) >= MAX_CODES:
                del self._codes[min(self._codes, key=lambda h: self._codes[h][1])]
            self._codes[digest] = (channel, now + CODE_SECONDS)
        remote.inbound_code(channel, digest)
        return {"code": f"{code[:4]}-{code[4:]}", "expiresAt": round((now + CODE_SECONDS) * 1000)}

    def forget(self, channel, sender):
        self._channel(channel)
        if self.store.sender(channel, sender) is None:
            raise APIError("That account isn't linked.", 404, "not_found")
        self.store.remove_sender(channel, sender)
        remote = self.remote()
        if remote is not None:
            remote.inbound_unlink(channel, sender)
        return sender

    def set_public(self, channel, sender, public):
        self._channel(channel)
        if type(public) is not bool:
            raise ValueError("public is true or false")
        if self.store.sender(channel, sender) is None:
            raise APIError("That account isn't linked.", 404, "not_found")
        self.store.set_public(channel, sender, public)
        return self.store.sender(channel, sender)

    def enable(self, channel, workflow_id):
        self._channel(channel)
        if not isinstance(workflow_id, str) or not WORKFLOW_ID.fullmatch(workflow_id):
            raise ValueError("workflowId is a saved workflow's id")
        self.runtime.workflows.get(workflow_id)
        self.store.enable(channel, workflow_id, self.clock())
        return workflow_id

    def disable(self, channel, workflow_id):
        self._channel(channel)
        self.store.disable(channel, workflow_id)
        return workflow_id

    def enabled(self, channel):
        """The published workflows turned on for ``channel``: [{"id", "name", "blanks"}]. One removed or unpublished
        since drops out here, and a removed one is forgotten."""
        wanted, found = set(self.store.workflow_ids(channel)), []
        for workflow in self.runtime.workflows.list():
            if workflow["id"] in wanted:
                wanted.discard(workflow["id"])
                if workflow.get("published"):
                    found.append({"id": workflow["id"], "name": words(workflow["name"]),
                                  "blanks": [blank["name"] for blank in starters.blanks(workflow.get("goal"))]})
        for gone in wanted:
            self.store.disable(channel, gone)
        return found

    # -- a command ---------------------------------------------------------------------------------------------------

    def handle(self, message):
        """One command from the relay: {"status", "message", "public"}. 202 a workflow started, 200 done, else refused.
        Never raises."""
        try:
            return self._handle(message if isinstance(message, dict) else {})
        except Exception:  # noqa: BLE001 -- a broken command answers 500 and starts nothing
            log.exception("inbound: a command failed")
            return refuse(500, "Mobster hit an error. Nothing started.")

    def _handle(self, m):
        channel, sender, verb = m.get("channel"), m.get("sender"), m.get("verb")
        text, key = m.get("text"), m.get("key")
        if not on():
            return refuse(503, OFF)
        if (channel not in PLATFORMS or not isinstance(sender, str) or not SENDER[channel].fullmatch(sender)
                or verb not in VERBS or not isinstance(text, str) or len(text) > TEXT_MAX
                or not isinstance(key, str) or not 3 <= len(key) <= 100):
            return refuse(400, "Mobster didn't understand that command. Nothing started.")
        label = words(m.get("label") or sender)[:40]
        now = self.clock()
        if verb == "link":
            return self._link(channel, sender, label, text, now)
        person = self.store.sender(channel, sender)
        if person is None:
            return refuse(403, STRANGER)
        if self.store.seen(channel, key, now):
            return refuse(409, "Mobster already took that command, so nothing started again.")
        self.store.used(channel, sender, now)
        if verb == "list":
            listed = self.enabled(channel)
            if not listed:
                return refuse(200, f"No workflow is turned on for {NAMES[channel]} yet. "
                                   "Turn one on in Mobster for Mac.")
            lines = [item["name"] + (f" ({', '.join(item['blanks'])})" if item["blanks"] else "") for item in listed]
            return {"status": 200, "message": "You can start: " + "; ".join(lines) + ".", "public": False}
        return self._run(channel, person, key, words(text), m.get("reply"), now)

    def _link(self, channel, sender, label, text, now):
        digest = code_hash(normalize(text))
        with self._lock:
            entry = self._codes.get(digest)
            if entry is None or entry[0] != channel or entry[1] <= now:
                return refuse(403, "That code didn't match, or it ran out. Make a new one in Mobster for Mac.")
            del self._codes[digest]
        self.store.add_sender(channel, sender, label, now)
        return {"status": 200, "message": f"Linked {label}. Send `list` to see what you can start.", "public": False}

    def _run(self, channel, person, key, text, reply, now):
        listed = self.enabled(channel)
        match = next((item for item in sorted(listed, key=lambda w: -len(w["name"]))
                      if text[:len(item["name"])].casefold() == item["name"].casefold()
                      and text[len(item["name"]):len(item["name"]) + 1] in ("", " ")), None)
        if match is None:
            return refuse(404, f"No workflow like that is turned on for {NAMES[channel]}. Send `list` to see yours.")
        if sum(1 for item in listed if item["name"].casefold() == match["name"].casefold()) > 1:
            return refuse(409, f"Several workflows are called {match['name']}. Rename one in Mobster for Mac.")
        workflow = self.runtime.workflows.get(match["id"])
        values, why = read_values(workflow, text[len(match["name"]):].strip())
        if values is None:
            return refuse(422, why)
        filled, ignored = hooks.fill(workflow, values)
        if ignored:
            return refuse(422, f"{workflow['name']} has no blank called {ignored[0]}.")
        body = {"via": channel, "from": person["label"], "text": secret_filter.redact(text)[:TEXT_MAX]}
        try:
            started = self.runtime.workflows.start(workflow["id"], f"{channel}-{code_hash(key)[:40]}", filled or None,
                                                   origin="hook", body=body)
        except APIError as error:
            return refuse(error.status if 400 <= error.status < 600 else 409, str(error))
        except ValueError as error:
            return refuse(422, str(error))
        sentence = (f"Started {workflow['name']}. Anything it sends, posts, buys or deletes waits for your OK in "
                    "Mobster "
                    "on your Mac.")
        if reply_to.address(channel, reply) and (started.get("batch") or started.get("run")):
            self._watch(Watch(channel, reply, person["public"], workflow["name"], lambda: self._result(started),
                              now + reply_to.WINDOW[channel]))
        return {"status": 202, "message": sentence, "public": person["public"]}

    # -- the answer --------------------------------------------------------------------------------------------------

    def _watch(self, watch):
        """Wait for a run to end: the thread starts with the first wait, so a Mac that takes no commands runs none."""
        with self._lock:
            self._watches.append(watch)
            if self._thread is None and not self._closed:
                self._thread = threading.Thread(target=self._loop, name="mobster-inbound", daemon=True)
                self._thread.start()
            self._lock.notify_all()

    def poll(self):
        """Post the result of each run that ended, once, and drop a wait whose address ran out. The thread calls it
        every ``interval`` seconds; a test calls it."""
        with self._lock:
            watches = list(self._watches)
        for watch in watches:
            expired = self.clock() >= watch.deadline
            text = None if expired else watch.check()
            if text is None and not expired:
                continue
            with self._lock:
                if watch in self._watches:
                    self._watches.remove(watch)
            if text is not None:
                reply_to.post(watch.channel, watch.reply, f"{watch.name}: {text}", watch.public, self.call)

    def _result(self, started):
        """How the run ended, as one masked line (and its last answer), or None while it goes."""
        workflows = self.runtime.workflows
        if started.get("batch"):
            record = workflows.batches.public(workflows.batches.get(started["batch"]["id"]))
            if record is not None and record["status"] == "running":
                return None
            status, reason = (record or {}).get("status") or "ended", (record or {}).get("reason")
            answer = next((r["answer"] for r in reversed((record or {}).get("rounds") or []) if r.get("answer")), None)
        else:
            with self.runtime.lock:
                run = self.runtime.runs.get((started.get("run") or {}).get("id"))
            if run is None:
                return None
            if run.finished_at is None:
                return None
            summary = run.summary if isinstance(run.summary, dict) else {}
            status, reason, answer = run.status, summary.get("reason"), summary.get("answer")
        head = "finished" if status == "completed" else f"ended ({str(status).replace('_', ' ')})"
        said = (reason if status != "completed" else None, answer)
        rest = [item for item in said if isinstance(item, str) and item.strip()]
        return secret_filter.redact(" ".join([head + "."] + [words(item) for item in rest]))[:ANSWER_CHARS]


_services = {}
_services_lock = threading.Lock()


def service_for(runtime):
    with _services_lock:
        return _services.get(id(runtime))


def start(runtime):
    service = InboundService(runtime)
    with _services_lock:
        _services[id(runtime)] = service


def close(runtime):
    with _services_lock:
        service = _services.pop(id(runtime), None)
    if service is not None:
        service.close()

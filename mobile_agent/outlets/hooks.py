"""Incoming webhooks: another tool starts one of your workflows with a signed POST to a URL Mobster Remote serves. The
relay never holds a hook's secret and never keeps a body: this Mac checks everything."""

import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time

from .. import secret_filter
from ..api_errors import APIError
from ..journal import JournalError
from ..service_support import ServiceRegistry
from . import vault as outlet_vault

log = logging.getLogger("mobster.outlets.hooks")

SCHEMA = "outlet_hooks"
VERSION = 2
HOOK_ID = re.compile(r"[0-9a-f]{32}")
WORKFLOW_ID = re.compile(r"[0-9a-f]{12}")
SIGNATURE = re.compile(r"t=(\d{1,12}),v1=([0-9a-f]{64})")
WINDOW = 300.0                 # seconds a signed call stays good either side of this Mac's clock
SEEN_KEEP = 2 * WINDOW         # signatures kept to refuse a replay
MAX_HOOKS = 50
MAX_BODY = 64 * 1024
MAX_EVENTS = 100_000
VALUE_CHARS = 300
EVENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
RATE, RATE_SECONDS = 10, 60.0
REMOTE_OFF = "Turn on Mobster Remote first, in Settings › Remote. A webhook URL is Remote's address for this Mac."


def migrate(connection, from_version):
    if from_version < 1:
        connection.execute("""CREATE TABLE IF NOT EXISTS outlet_hooks (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            url TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_at REAL,
            last_status INTEGER,
            last_message TEXT,
            uses INTEGER NOT NULL DEFAULT 0)""")
        connection.execute("CREATE INDEX IF NOT EXISTS outlet_hooks_workflow ON outlet_hooks(workflow_id)")
        connection.execute("""CREATE TABLE IF NOT EXISTS outlet_hook_seen (
            hook_id TEXT NOT NULL,
            signature TEXT NOT NULL,
            at REAL NOT NULL,
            PRIMARY KEY (hook_id, signature))""")
    if from_version < 2:
        connection.execute("""CREATE TABLE IF NOT EXISTS outlet_hook_events (
            hook_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            digest TEXT NOT NULL,
            claimed_at REAL NOT NULL,
            PRIMARY KEY (hook_id, event_id))""")


COLUMNS = ("id", "workflow_id", "url", "created_at", "last_at", "last_status", "last_message", "uses")


class HookStore:
    def __init__(self, journal):
        self.journal = journal

    def _read(self, sql, args=()):
        with self.journal.lock:
            if self.journal.connection is None:
                raise JournalError("The run journal is closed")
            try:
                return self.journal.connection.execute(sql, args).fetchall()
            except sqlite3.Error:
                raise JournalError("Mobster could not read its webhooks") from None

    def all(self, workflow_id=None):
        sql = f"SELECT {', '.join(COLUMNS)} FROM outlet_hooks"
        rows = self._read(sql + " WHERE workflow_id=? ORDER BY created_at" if workflow_id else sql
                          + " ORDER BY created_at",
                          (workflow_id,) if workflow_id else ())
        return [dict(zip(COLUMNS, row)) for row in rows]

    def get(self, hook_id):
        rows = self._read(f"SELECT {', '.join(COLUMNS)} FROM outlet_hooks WHERE id=?", (hook_id,))
        return dict(zip(COLUMNS, rows[0])) if rows else None

    def add(self, row):
        with self.journal.transaction() as connection:
            connection.execute(f"INSERT INTO outlet_hooks({', '.join(COLUMNS)}) "
                               f"VALUES ({', '.join('?' * len(COLUMNS))})",
                               [row.get(column) for column in COLUMNS])

    def used(self, hook_id, at, status, message):
        with self.journal.transaction() as connection:
            connection.execute("UPDATE outlet_hooks SET last_at=?, last_status=?, last_message=?, "
                               "uses=uses+? WHERE id=?",
                               (at, status, message, 1 if status == 202 else 0, hook_id))

    def delete(self, hook_id):
        with self.journal.transaction() as connection:
            connection.execute("DELETE FROM outlet_hooks WHERE id=?", (hook_id,))
            connection.execute("DELETE FROM outlet_hook_seen WHERE hook_id=?", (hook_id,))
            connection.execute("DELETE FROM outlet_hook_events WHERE hook_id=?", (hook_id,))

    def claim_event(self, hook_id, event_id, digest, now):
        """Keep an event's identity until its hook is revoked, including a start whose outcome is unknown."""
        with self.journal.transaction() as connection:
            if connection.execute("SELECT 1 FROM outlet_hooks WHERE id=?", (hook_id,)).fetchone() is None:
                return "gone"
            row = connection.execute("SELECT digest FROM outlet_hook_events WHERE hook_id=? AND event_id=?",
                                     (hook_id, event_id)).fetchone()
            if row:
                return "replayed" if row[0] == digest else "changed"
            if connection.execute("SELECT count(*) FROM outlet_hook_events").fetchone()[0] >= MAX_EVENTS:
                return "full"
            connection.execute("INSERT INTO outlet_hook_events VALUES (?,?,?,?)", (hook_id, event_id, digest, now))
        return "claimed"

    def seen(self, hook_id, signature, now):
        """True when this signature came before (a replay); else remember it. Old ones go."""
        with self.journal.transaction() as connection:
            connection.execute("DELETE FROM outlet_hook_seen WHERE at < ?", (now - SEEN_KEEP,))
            try:
                connection.execute("INSERT INTO outlet_hook_seen(hook_id, signature, at) VALUES (?, ?, ?)",
                                   (hook_id, signature, now))
            except sqlite3.IntegrityError:
                return True
        return False


class HookVault:
    """A hook's secret in the outlets' Keychain service, account ``hook:<id>``."""

    def __init__(self, backend=None):
        self.backend = backend if backend is not None else outlet_vault.backend()

    @staticmethod
    def account(hook_id):
        if not HOOK_ID.fullmatch(str(hook_id)):
            raise ValueError("A webhook's id is 32 hex characters")
        return f"hook:{hook_id}"

    def put(self, hook_id, secret):
        self.backend._set(self.account(hook_id), secret)

    def get(self, hook_id):
        return self.backend._get(self.account(hook_id))

    def delete(self, hook_id):
        self.backend._delete(self.account(hook_id))


def new_secret():
    return "whsec_" + secrets.token_hex(32)


def sign(secret, timestamp, body):
    """``t=<timestamp>,v1=<hex>`` for ``body`` (bytes or text): what a caller sends."""
    data = body.encode() if isinstance(body, str) else body
    digest = hmac.new(secret.encode(), str(int(timestamp)).encode() + b"." + data, hashlib.sha256).hexdigest()
    return f"t={int(timestamp)},v1={digest}"


def fill(workflow, body):
    """(values for the workflow's blanks, the field names that named none) from a call's JSON object."""
    from ..workflows import starters
    fields = body.get("blanks") if isinstance(body.get("blanks"), dict) else body
    names = {starters._key(blank["name"]): blank["name"] for blank in starters.blanks(workflow.get("goal"))}
    values, ignored = {}, []
    for key, value in list(fields.items())[:100]:
        name = names.get(starters._key(str(key))) if isinstance(key, str) else None
        if name is None or isinstance(value, (dict, list)) or value is None:
            ignored.append(str(key)[:40])
            continue
        if isinstance(value, bool):
            value = "yes" if value else "no"
        text = " ".join(str(value).split())[:VALUE_CHARS]
        values[name] = secret_filter.redact(text)
    return values, ignored


def event_envelope(data, workflow, workflows):
    """A producer's stable event identity and typed values, only for the explicit versioned envelope."""
    if "_mobsterEvent" not in data:
        return None
    from ..output_contract import bounded_json
    from ..workflows import starters
    meta = data["_mobsterEvent"]
    if (set(data) != {"_mobsterEvent", "values"} or not isinstance(meta, dict)
            or set(meta) != {"version", "id", "occurredAt"} or type(meta["version"]) is not int
            or meta["version"] != 1 or not isinstance(meta["id"], str) or not EVENT_ID.fullmatch(meta["id"])
            or type(meta["occurredAt"]) is not int or not 0 <= meta["occurredAt"] <= 9_999_999_999_999
            or not isinstance(data["values"], dict)
            or len(data["values"]) > 30):
        raise ValueError("An event has _mobsterEvent {version: 1, id, occurredAt} and a values object")
    cleaned = bounded_json(data, max_bytes=16_000)
    names = {starters._key(item["name"]): item["name"] for item in workflows.live(workflow).get("inputs") or []}
    names.update({starters._key(blank["name"]): blank["name"] for blank in starters.blanks(workflow.get("goal"))})
    values = {}
    for name, value in cleaned["values"].items():
        if not isinstance(name, str) or len(name) > starters.NAME_CHARS or starters._key(name) not in names:
            raise ValueError(f"{str(name)[:40]!r} is not a declared workflow input")
        key = names[starters._key(name)]
        if key in values:
            raise ValueError(f"Give {key} once in event values")
        values[key] = secret_filter.redact_deep(value)
    digest = hashlib.sha256(json.dumps(cleaned, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return meta["id"], values, digest


class HookService:
    """Make, list and revoke a workflow's hooks, and take a call. One per Runtime (outlets.register)."""

    def __init__(self, runtime, *, vault=None, clock=time.time, remote=None):
        self.runtime = runtime
        self.store = HookStore(runtime.journal)
        self.vault = vault if vault is not None else HookVault()
        self.clock = clock
        self._remote = remote            # tests: an object with address(prefix) and hooks_changed(ids)
        self._buckets = {}
        self._lock = threading.Lock()

    def remote(self):
        if self._remote is not None:
            return self._remote
        try:
            from ..remote import service as remote_service
        except ImportError:
            return None
        return remote_service.service_for(self.runtime)

    def base_url(self):
        """``<relay>/hooks/<route>``, or None when Remote isn't set up on this Mac."""
        remote = self.remote()
        try:
            return remote.address("hooks") if remote is not None else None
        except Exception:  # noqa: BLE001
            return None

    def ids(self):
        try:
            return [row["id"] for row in self.store.all()]
        except JournalError:
            return []

    def _tell_remote(self):
        remote = self.remote()
        if remote is not None:
            try:
                remote.hooks_changed(self.ids())
            except Exception:  # noqa: BLE001 -- the next hello carries the list
                log.warning("hooks: couldn't tell Remote about a change")

    @staticmethod
    def public(row, secret=None):
        out = {"id": row["id"], "workflowId": row["workflow_id"], "url": row["url"],
               "createdAt": round(row["created_at"] * 1000), "uses": row["uses"],
               "lastAt": round(row["last_at"] * 1000) if row["last_at"] else None, "lastStatus": row["last_status"],
               "lastMessage": row["last_message"]}
        if secret is not None:
            out["secret"] = secret
        return out

    def _workflow(self, workflow_id):
        workflows = getattr(self.runtime, "workflows", None)
        if workflows is None:
            raise APIError("Workflows aren't available in this Mobster.", 503, "unavailable")
        if not isinstance(workflow_id, str) or not WORKFLOW_ID.fullmatch(workflow_id):
            raise ValueError("workflowId is a saved workflow's id")
        return workflows.get(workflow_id)

    def list(self, workflow_id=None):
        """The hooks, oldest first. A hook whose workflow was removed is revoked here
        (workflows.delete knows no hooks)."""
        workflows = getattr(self.runtime, "workflows", None)
        out = []
        for row in self.store.all(workflow_id):
            if workflows is not None:
                try:
                    workflows.get(row["workflow_id"])
                except APIError:
                    self.revoke(row["id"])
                    continue
            out.append(self.public(row))
        return out

    def create(self, workflow_id):
        """A new hook for the workflow: its row with ``secret``, the only time the secret is shown."""
        self._workflow(workflow_id)
        base = self.base_url()
        if not base:
            raise APIError(REMOTE_OFF, 409, "remote_off")
        if len(self.store.all()) >= MAX_HOOKS:
            raise APIError(f"You have {MAX_HOOKS} webhook URLs. Revoke one first.", 409, "hook_limit")
        hook_id, secret = secrets.token_hex(16), new_secret()
        try:
            self.vault.put(hook_id, secret)
        except outlet_vault.VaultError:
            raise APIError("The Keychain didn't save the webhook's secret. Nothing was made.", 503,
                           "keychain_unavailable") from None
        row = {"id": hook_id, "workflow_id": workflow_id, "url": f"{base}/{hook_id}", "created_at": self.clock(),
               "uses": 0}
        try:
            self.store.add(row)
        except Exception:
            self._forget(hook_id)
            raise
        self._tell_remote()
        return self.public(self.store.get(hook_id), secret)

    def revoke(self, hook_id):
        row = self.store.get(hook_id) if isinstance(hook_id, str) else None
        if row is None:
            raise APIError("That webhook URL was revoked.", 404, "not_found")
        self.store.delete(hook_id)
        self._forget(hook_id)
        self._tell_remote()
        return hook_id

    def forget_workflow(self, workflow_id):
        """A removed workflow's hooks go with it."""
        for row in self.store.all(workflow_id):
            self.revoke(row["id"])

    def _forget(self, hook_id):
        try:
            self.vault.delete(hook_id)
        except outlet_vault.VaultError:
            log.warning("hooks: the Keychain kept a revoked webhook's secret")

    def _allow(self, hook_id, now):
        with self._lock:
            tokens, at = self._buckets.get(hook_id, (float(RATE), now))
            tokens = min(float(RATE), tokens + (now - at) * RATE / RATE_SECONDS)
            if tokens < 1:
                self._buckets[hook_id] = (tokens, now)
                return False
            self._buckets[hook_id] = (tokens - 1, now)
            return True

    def handle(self, hook_id, signature, body):
        """One call: {"status": 202, "filled", "ignored"} when the workflow started,
        else {"status", "error", "message"}."""
        now = self.clock()
        try:
            answer = self._handle(hook_id, signature, body, now)
        except Exception:  # noqa: BLE001 -- an unknown dispatch outcome is never sent again with this event id
            log.exception("hooks: a call failed")
            answer = refuse(500, "error", "Mobster hit an error. Check task history before sending another call.")
        if isinstance(hook_id, str) and HOOK_ID.fullmatch(hook_id) and answer["status"] != 404:
            try:
                self.store.used(hook_id, now, answer["status"], answer.get("message"))
            except JournalError:
                pass
        return answer

    def _handle(self, hook_id, signature, body, now):
        row = self.store.get(hook_id) if isinstance(hook_id, str) and HOOK_ID.fullmatch(hook_id) else None
        if row is None:
            return refuse(404, "not_found", "No webhook has that address. It may have been revoked.")
        if not isinstance(body, str) or len(body.encode()) > MAX_BODY:
            return refuse(413, "too_large", f"The body is at most {MAX_BODY // 1024} KB.")
        match = SIGNATURE.fullmatch(str(signature or "").strip())
        if not match:
            return refuse(401, "unsigned", "Sign the call: Mobster-Signature: t=<unix seconds>,v1=<HMAC-SHA256 hex>.")
        stamp, given = int(match.group(1)), match.group(2)
        if abs(now - stamp) > WINDOW:
            return refuse(401, "stale", "The signature's time is more than 5 minutes from now. Sign it again.")
        try:
            secret = self.vault.get(hook_id)
        except outlet_vault.VaultError:
            return refuse(503, "keychain", "Mobster couldn't read the webhook's secret. Nothing started.")
        if not secret:
            return refuse(503, "keychain", "Mobster no longer has the webhook's secret. Make a new URL.")
        expected = sign(secret, stamp, body).rsplit("v1=", 1)[1]
        if not hmac.compare_digest(expected, given):
            return refuse(401, "bad_signature", "The signature doesn't match. Sign <t>.<body> with the URL's secret.")
        try:
            data = json.loads(body) if body.strip() else {}
        except ValueError:
            return refuse(422, "not_json", "The body is a JSON object, such as {\"person\": \"Sam\"}.")
        if not isinstance(data, dict):
            return refuse(422, "not_json", "The body is a JSON object, such as {\"person\": \"Sam\"}.")
        workflows = getattr(self.runtime, "workflows", None)
        if workflows is None:
            return refuse(503, "unavailable", "Workflows aren't available in this Mobster.")
        try:
            workflow = workflows.get(row["workflow_id"])
        except APIError:
            return refuse(410, "workflow_gone", "That workflow was removed. Nothing started.")
        try:
            event = event_envelope(data, workflow, workflows)
        except ValueError as error:
            return refuse(422, "invalid_event", str(error))
        if self.store.seen(hook_id, given, now):
            return refuse(409, "replayed", "This exact call came before, so nothing started again.")
        if not self._allow(hook_id, now):
            return refuse(429, "rate_limited", f"At most {RATE} calls a minute to one webhook URL. Try again soon.")
        if event is None:
            values, ignored = fill(workflow, data)
            request_key = f"hook-{hook_id[:8]}-{given[:40]}"
        else:
            event_id, values, digest = event
            result = self.store.claim_event(hook_id, event_id, digest, now)
            if result == "gone":
                return refuse(404, "not_found", "No webhook has that address. It may have been revoked.")
            if result == "full":
                return refuse(503, "event_limit", "This Mac has reached its saved webhook event limit. Revoke an "
                              "unused webhook URL before sending more events.")
            if result == "changed":
                return refuse(409, "event_changed", "This event id was already used with different content.")
            if result == "replayed":
                return refuse(409, "replayed", "This event came before, so nothing started again.")
            ignored = []
            request_key = f"hook-{hook_id}-event-{hashlib.sha256(event_id.encode()).hexdigest()}"
        # origin "hook" (harness_api.FORCED_ASK_ORIGINS) asks before it sends, buys, posts or deletes; nothing in a
        # body can change the steps, the app, the phone or where the result goes.
        try:
            if event is None:
                workflows.run(workflow["id"], request_key, values or None, origin="hook", body=data)
            else:
                workflows.run_hook_event(workflow["id"], request_key, values or None, data,
                                         data["_mobsterEvent"]["occurredAt"])
        except APIError as error:
            return refuse(error.status if 400 <= error.status < 600 else 409, error.code, str(error))
        except ValueError as error:
            return refuse(422, "blanks", str(error))
        return {"status": 202, "filled": sorted(values), "ignored": ignored[:20],
                "message": f"Started {workflow.get('name') or 'the workflow'}."}


def refuse(status, error, message):
    return {"status": status, "error": error, "message": message}


_services = ServiceRegistry()


def service_for(runtime):
    return _services.get(runtime)


def start(runtime):
    _services.add(runtime, HookService(runtime))


def close(runtime):
    _services.pop(runtime)

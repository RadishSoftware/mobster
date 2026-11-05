"""Destinations and the delivery worker: one OutletService per Runtime (harness_api.register_service)."""

import logging
import random
import re
import threading
import time
import uuid
from types import SimpleNamespace

from .. import harness_api
from ..api_errors import APIError
from ..connectors import oauth
from ..service_support import ServiceRegistry, ms
from . import connect as outlet_connect
from . import kinds as outlet_kinds
from . import net, webhook
from . import store as outlet_store
from . import vault as outlet_vault

log = logging.getLogger("mobster.outlets")

ATTEMPTS = 5
BACKOFF = (10.0, 60.0, 300.0, 900.0)      # seconds before the 2nd, 3rd, 4th and 5th tries
JITTER = 0.2                              # each pause is BACKOFF ± 20%, so a receiver that fell over isn't hit at once
ID = re.compile(r"[0-9a-f]{12}")
REMOVED = "That destination was removed. Choose another one."
GONE_RUN = "That task is no longer in History."
NO_ADDRESS = "Mobster couldn't find this destination's address or sign-in. Add it again in Settings › Connectors."
KEYCHAIN = "Mobster couldn't read this destination from the Keychain. Unlock your Mac's Keychain, then resend."
UNKNOWN = "Mobster quit while sending this, so it may have arrived. Check {name}, then resend if it's missing."

_services = ServiceRegistry()


def service_for(runtime):
    return _services.get(runtime)


def services():
    return _services.all()


def start(runtime):
    """register_service's start: an OutletService for the runtime; picks up deliveries an earlier launch left."""
    service = OutletService(runtime)
    _services.add(runtime, service)
    service.start()


def close(runtime):
    service = _services.pop(runtime)
    if service is not None:
        service.close()


class OutletService:
    def __init__(self, runtime, *, vault=None, post=None, call=None, clock=time.time, backoff=BACKOFF,
                 attempts=ATTEMPTS, jitter=JITTER, rand=random.random, connectors=None, refresh=oauth.refresh,
                 apps=None, sign_in=oauth.SignIn):
        self.runtime = runtime
        self.store = outlet_store.OutletStore(runtime.journal)
        self.vault = vault if vault is not None else outlet_vault.backend()
        self.post = post or webhook.post
        self.call = call or net.call
        self.clock = clock
        self.refresh = refresh                  # renews a Notion or Google sign-in through the license API
        self.connectors = connectors or self._connectors
        self.connects = outlet_connect.Connects(call=self.call, connectors=self.connectors, apps=apps, sign_in=sign_in)
        self.backoff = tuple(backoff)
        self.attempts = attempts
        self.jitter, self.rand = jitter, rand
        self._lock = threading.Condition()
        self._due = {}            # delivery id -> when to try it (seconds)
        self._thread = None
        self._closed = False
        self._busy = set()        # destination ids with a test in flight
        self._trying = {}         # destination id -> threading.Lock: one try (a test or a delivery) at a time

    def start(self):
        try:
            pending = self.store.pending()
        except Exception:  # noqa: BLE001 -- a journal that can't be read leaves nothing to pick up
            pending = []
        now = self.clock()
        # A try under way when Mobster quit, to a kind that can't take a result twice: its outcome is unknown, so it
        # fails and says to check.
        for row in [r for r in pending if r.get("inflight_at")]:
            pending.remove(row)
            try:
                self.store.update_delivery(row["id"], status="failed", inflight_at=None, next_at=None, updated_at=now,
                                           error=UNKNOWN.format(name=row["destination_name"]))
            except Exception:  # noqa: BLE001
                log.warning("outlets: couldn't close a delivery whose try was under way")
        with self._lock:
            for row in pending:
                self._due[row["id"]] = min(row["next_at"] or now, now)
        journal = getattr(self.runtime, "journal", None)
        if journal is not None and callable(getattr(journal, "add_expiry_listener", None)):
            journal.add_expiry_listener(self._forget_runs)
        if pending:
            self._ensure()

    def close(self, timeout=1.5):
        self.connects.close()
        with self._lock:
            self._closed = True
            self._lock.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _ensure(self):
        with self._lock:
            if self._closed or (self._thread is not None and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self._loop, name="mobster-outlets", daemon=True)
            self._thread.start()

    def _forget_runs(self, run_ids):
        for run_id in run_ids:
            try:
                self.store.forget_run(run_id)
            except Exception:  # noqa: BLE001
                log.warning("outlets: couldn't forget a removed run's deliveries")

    @staticmethod
    def public(row):
        test = row.get("last_test")
        kind = row.get("kind") or "webhook"
        config = row.get("config") or {}
        shown = {key: config[key] for key in ("folder", "file", "format", "database", "title", "preset", "workspace",
                                              "channel", "channelName", "server", "spreadsheet") if key in config}
        return {"id": row["id"], "name": row["name"], "kind": kind, "config": shown, "hint": row["hint"],
                "events": config.get("events") or ["run.finished"], "scope": config.get("scope") or "named",
                "hasSecret": row["has_secret"],
                "screenshot": row["screenshot"], "createdAt": ms(row["created_at"]),
                "lastTest": {"ok": bool(test.get("ok")), "at": ms(test.get("at")), "status": test.get("status"),
                             "message": test.get("message")} if isinstance(test, dict) else None}

    def list(self):
        used = {}
        for flow in self._workflows():
            if flow.get("sendTo"):
                used[flow["sendTo"]] = used.get(flow["sendTo"], 0) + 1
        return [{**self.public(row), "workflows": used.get(row["id"], 0)} for row in self.store.destinations()]

    def kinds(self):
        """The kinds the app may offer to add now, in kinds.py's order (Connects.offered)."""
        return [kind_id for kind_id, kind in outlet_kinds.all_kinds().items() if self.connects.offered(kind)]

    def _workflows(self):
        workflows = getattr(self.runtime, "workflows", None)
        try:
            return list(workflows.list()) if workflows is not None else []
        except Exception:  # noqa: BLE001 -- the list stands without its counts
            return []

    def find(self, wanted):
        """A destination by its id, else by its name (case and spacing ignored). ValueError when none or two match."""
        if not isinstance(wanted, str) or not wanted.strip():
            raise ValueError("Name a saved destination, or give its id")
        wanted = wanted.strip()
        rows = self.store.destinations()
        found = [r for r in rows if r["id"] == wanted.lower()] or [
            r for r in rows if " ".join(r["name"].split()).casefold() == " ".join(wanted.split()).casefold()]
        if not found:
            names = ", ".join(r["name"] for r in rows[:8])
            raise ValueError(f"No destination is called {wanted[:60]}. "
                             + (f"You have: {names}." if names else "Add one in Settings › Connectors."))
        if len(found) > 1:
            raise ValueError(f"Several destinations are called {wanted[:60]}. Use its id: "
                             + ", ".join(r["id"] for r in found))
        return found[0]

    def _get(self, destination_id):
        row = self.store.destination(destination_id) if isinstance(destination_id, str) else None
        if row is None:
            raise APIError("That destination was removed.", 404, "not_found")
        return row

    def _unique(self, name, but=None):
        for row in self.store.destinations():
            if row["id"] != but and row["name"].casefold() == name.casefold():
                raise ValueError(f"You already have a destination called {name}. Give this one another name.")

    def _connectors(self):
        """The ConnectorService a Slack destination posts through, or None (connectors/service.py)."""
        try:
            from ..connectors import service as connector_service
        except ImportError:
            return None
        return connector_service.service_for(self.runtime)

    def connect(self, body):
        """Start connecting a destination: {"connect", "authorizeUrl"?} (connect.py)."""
        if not isinstance(body, dict) or set(body) != {"kind"}:
            raise ValueError('Say which kind to connect: {"kind": "slack" | "discord" | "notion" | "sheets"}')
        return self.connects.begin(self._kind(body["kind"]))

    def _resolved(self, kind, body):
        """``body`` with its ``connect`` id replaced by what that connect found."""
        if "connect" not in body:
            return body
        return {**body, "connect": self.connects.found(body["connect"], kind.id)}

    @staticmethod
    def _kind(value):
        if value is not None and not isinstance(value, str):
            raise ValueError("A destination's kind is a word, like webhook")
        return outlet_kinds.get(value)

    def _keychain_put(self, destination_id, item, then):
        try:
            self.vault.put(destination_id, item.get("url"), item.get("secret"),
                           {key: item[key] for key in outlet_vault.EXTRA if item.get(key)})
        except outlet_vault.VaultError as error:
            raise APIError(f"{error}. {then}", 503, "keychain_unavailable") from None

    def add(self, body):
        if not isinstance(body, dict):
            raise ValueError("A destination has a name and where it sends")
        kind = self._kind(body.get("kind"))
        if set(body) - set(outlet_kinds.FIELDS) - set(kind.fields):
            raise ValueError("A destination has a name, its kind, and " + ", ".join(kind.fields))
        name = webhook.validate_name(body.get("name"), kind.example)
        self._unique(name)
        if self.store.count() >= outlet_store.MAX_DESTINATIONS:
            raise APIError(f"You have {outlet_store.MAX_DESTINATIONS} destinations. Remove one first.", 409,
                           "destination_limit")
        subscribed = outlet_kinds.subscription(body, kind.id)
        setup = kind.setup(self._resolved(kind, {k: v for k, v in body.items() if k not in ("events", "scope")}))
        destination_id = uuid.uuid4().hex[:12]
        if setup.get("vault"):
            self._keychain_put(destination_id, setup["vault"], "Nothing was saved.")
        now = self.clock()
        try:
            row = self.store.add_destination({"id": destination_id, "name": name, "kind": kind.id,
                                              "hint": setup["hint"],
                                              "config": {**(setup.get("config") or {}), **subscribed},
                                              "has_secret": setup.get("has_secret", False),
                                              "screenshot": setup.get("screenshot", False),
                                              "created_at": now, "updated_at": now})
        except Exception:
            self._drop_secret(destination_id)
            raise
        self.connects.drop(body.get("connect"))
        return self.public(row)

    def update(self, destination_id, body):
        row = self._get(destination_id)
        kind = outlet_kinds.get(row["kind"])
        if not isinstance(body, dict) or not body or set(body) - set(outlet_kinds.FIELDS) - set(kind.fields):
            raise ValueError("Change the name or " + ", ".join(kind.fields))
        if "kind" in body and body["kind"] != kind.id:
            raise ValueError("A destination keeps its kind. Add a new one instead.")
        changes = {}
        if "name" in body:
            changes["name"] = webhook.validate_name(body["name"], kind.example)
            self._unique(changes["name"], but=row["id"])

        def saved():
            try:
                return self.vault.get(row["id"])
            except outlet_vault.VaultError as error:
                raise APIError(f"{error}. Nothing was changed.", 503, "keychain_unavailable") from None
        fields = {k: v for k, v in body.items() if k not in outlet_kinds.FIELDS}
        setup = kind.setup(self._resolved(kind, fields), {"row": row, "saved": saved})
        if setup.get("vault"):
            self._keychain_put(row["id"], setup["vault"], "Nothing was changed.")
        changes.update({key: value for key, value in setup.items() if key != "vault"})
        if "config" in changes or "events" in body or "scope" in body:
            # A new config from setup keeps what the destination takes and for which tasks.
            base = changes.get("config", row["config"])
            changes["config"] = {**base, **outlet_kinds.subscription(body, kind.id, row["config"])}
        changes["updated_at"] = self.clock()
        updated = self.public(self.store.update_destination(row["id"], **changes))
        self.connects.drop(body.get("connect"))
        return updated

    def delete(self, destination_id):
        """Remove a destination: its Keychain item goes, workflows that sent there stop sending, and deliveries on their
        way to it fail."""
        row = self._get(destination_id)
        self.store.delete_destination(row["id"])
        self._drop_secret(row["id"])
        workflows = getattr(self.runtime, "workflows", None)
        for flow in self._workflows():
            if flow.get("sendTo") == row["id"]:
                try:
                    workflows.update(flow["id"], {"sendTo": None, "revision": flow["revision"]})
                except Exception:  # noqa: BLE001 -- its next run finds the destination gone and sends nothing
                    log.warning("outlets: a workflow kept a removed destination")
        return row["id"]

    def _drop_secret(self, destination_id):
        try:
            self.vault.delete(destination_id)
        except outlet_vault.VaultError:
            log.warning("outlets: the Keychain kept a removed destination's item")

    def _saved(self, row):
        """{"url", "secret", ...} of a destination; APIError when the Keychain can't give it."""
        if not outlet_kinds.get(row.get("kind")).keychain:
            return {"url": None, "secret": None}
        try:
            saved = self.vault.get(row["id"])
        except outlet_vault.VaultError:
            raise APIError(KEYCHAIN, 503, "keychain_unavailable") from None
        if saved is None:
            raise APIError(NO_ADDRESS, 409, "address_missing")
        return saved

    def _context(self, row, saved, delivery_id, event):
        def keep(tokens):
            """A renewed sign-in, saved before it's used: a vendor that rotates refresh tokens takes each once."""
            saved["oauth"] = tokens
            try:
                self.vault.put(row["id"], saved.get("url"), saved.get("secret"),
                               {key: saved[key] for key in outlet_vault.EXTRA if saved.get(key)})
            except outlet_vault.VaultError:
                log.warning("outlets: the Keychain didn't keep a renewed sign-in")
        return SimpleNamespace(destination=row, saved=saved, delivery_id=delivery_id, event=event, now=self.clock(),
                               wall=self.clock, post=self.post, call=self.call, refresh=self.refresh, keep=keep,
                               connectors=self.connectors)

    def _one_try(self, destination_id):
        """One try at a time per destination, so a test and a delivery never renew the same token at once."""
        with self._lock:
            return self._trying.setdefault(destination_id, threading.Lock())

    def test(self, destination_id):
        """One test, now, the way the destination's kind tests (a webhook gets the test body; a folder is checked
        writable; Notion's database is read), kept on the destination."""
        row = self._get(destination_id)
        kind = outlet_kinds.get(row["kind"])
        with self._lock:
            if row["id"] in self._busy:
                raise APIError("A test is already on its way to this destination.", 409, "test_running")
            self._busy.add(row["id"])
        try:
            with self._one_try(row["id"]):
                saved = self._saved(row)
                outcome = kind.test(self._context(row, saved, uuid.uuid4().hex[:12], "test"))
        finally:
            with self._lock:
                self._busy.discard(row["id"])
        result = {"ok": outcome.ok, "status": outcome.status, "message": outcome.message, "at": self.clock()}
        self.store.update_destination(row["id"], last_test=result)
        return {"ok": outcome.ok, "status": outcome.status, "message": outcome.message}

    def send_event(self, destination_id, body, event="notification.matched"):
        """One try, now, of a body that isn't a finished run's result (a notification trigger's match), to a saved
        destination, the way its kind sends."""
        row = self._get(destination_id)
        kind = outlet_kinds.get(row["kind"])
        saved = self._saved(row)
        return kind.send(self._context(row, saved, uuid.uuid4().hex[:12], event), body)

    @staticmethod
    def public_delivery(row):
        return {"id": row["id"], "runId": row["run_id"], "destinationId": row["destination_id"],
                "destinationName": row["destination_name"], "status": row["status"], "attempts": row["attempts"],
                "httpStatus": row["http_status"], "error": row["error"], "nextAt": ms(row["next_at"]),
                "createdAt": ms(row["created_at"]), "updatedAt": ms(row["updated_at"]),
                "event": row.get("event") or "run.finished"}

    def deliveries(self, run_id):
        return [self.public_delivery(row) for row in self.store.deliveries_for(run_id)
                if (row.get("event") or "run.finished") == "run.finished"]

    def history(self, destination_id, limit=outlet_store.LOG_MAX):
        """The destination's delivery log, newest first."""
        row = self._get(destination_id)
        return [self.public_delivery(item) for item in self.store.deliveries_to(row["id"], limit)]

    def enqueue(self, run_id, destination_id, event="run.finished", payload=None):
        """A delivery of ``run_id``'s result (or, with ``payload``, another event) to the destination; its row."""
        row = self.store.destination(destination_id)
        now = self.clock()
        delivery = {"id": uuid.uuid4().hex[:12], "run_id": run_id, "destination_id": destination_id,
                    "destination_name": row["name"] if row else "A removed destination",
                    "status": "sending" if row else "failed", "attempts": 0, "http_status": None,
                    "error": None if row else REMOVED, "next_at": now if row else None, "created_at": now,
                    "updated_at": now, "event": event, "payload": payload}
        saved = self.store.add_delivery(delivery)
        if row is not None:
            self._schedule(saved["id"], now)
        return saved

    def resend(self, delivery_id):
        """The same result again, as a new delivery to the same destination."""
        earlier = self.store.delivery(delivery_id) if isinstance(delivery_id, str) else None
        if earlier is None:
            raise APIError("Mobster no longer has that delivery.", 404, "not_found")
        if earlier["status"] == "sending":
            raise APIError("It's still on its way.", 409, "still_sending")
        self._get(earlier["destination_id"])
        if earlier.get("payload") is not None:
            return self.public_delivery(self.enqueue(earlier["run_id"], earlier["destination_id"], earlier["event"],
                                                     earlier["payload"]))
        with self.runtime.lock:
            run = self.runtime.runs.get(earlier["run_id"])
        if run is None or run.finished_at is None:
            raise APIError(GONE_RUN, 409, "run_gone")
        return self.public_delivery(self.enqueue(run.id, earlier["destination_id"]))

    def _schedule(self, delivery_id, when):
        with self._lock:
            self._due[delivery_id] = when
            self._lock.notify_all()
        self._ensure()

    def _loop(self):
        while True:
            with self._lock:
                while not self._closed:
                    now = self.clock()
                    due = [(when, key) for key, when in self._due.items() if when <= now]
                    if due:
                        break
                    wait = min((when - now for when in self._due.values()), default=None)
                    self._lock.wait(timeout=1.0 if wait is None else max(0.01, min(wait, 1.0)))
                if self._closed:
                    return
                due.sort()
                delivery_id = due[0][1]
                self._due.pop(delivery_id, None)
            try:
                self._attempt(delivery_id)
            except Exception:  # noqa: BLE001 -- one bad delivery never stops the others
                log.exception("outlets: a delivery failed unexpectedly")
                try:
                    self.store.update_delivery(delivery_id, status="failed", error="Mobster hit an error sending it.",
                                               next_at=None, updated_at=self.clock())
                except Exception:  # noqa: BLE001
                    pass

    def _fail(self, delivery_id, message, attempts=None, status=None):
        changes = {"status": "failed", "error": message, "next_at": None, "updated_at": self.clock()}
        if attempts is not None:
            changes.update(attempts=attempts, http_status=status)
        self.store.update_delivery(delivery_id, **changes)
        self._published(delivery_id)

    def _attempt(self, delivery_id):
        row = self.store.delivery(delivery_id)
        if row is None or row["status"] != "sending":
            return
        destination = self.store.destination(row["destination_id"])
        if destination is None:
            return self._fail(delivery_id, REMOVED)
        try:
            kind = outlet_kinds.get(destination["kind"])
        except ValueError:
            return self._fail(delivery_id, "This Mobster can't send to that kind of destination. Update Mobster.")
        with self._one_try(destination["id"]):
            return self._try(delivery_id, row, destination, kind)

    def _try(self, delivery_id, row, destination, kind):
        try:
            saved = self._saved(destination)
        except APIError as error:
            return self._fail(delivery_id, str(error))
        event = row.get("event") or "run.finished"
        if row.get("payload") is not None:
            body = row["payload"]       # made and masked when the event happened
        else:
            with self.runtime.lock:
                run = self.runtime.runs.get(row["run_id"])
            if run is None:
                return self._fail(delivery_id, GONE_RUN)
            workflow = workflow_of(run)
            screenshot = webhook.screenshot_of(run, getattr(self.runtime, "frames", None)) \
                if destination["screenshot"] and kind.id == "webhook" else None
            body = webhook.payload(run, workflow, screenshot)
        if not kind.replay_safe:
            # Journaled before it is sent: a crash from here on leaves an outcome nobody knows, never a second try.
            self.store.update_delivery(delivery_id, inflight_at=self.clock())
        outcome = kind.send(self._context(destination, saved, delivery_id, event), body)
        attempts = row["attempts"] + 1
        now = self.clock()
        if outcome.ok:
            self.store.update_delivery(delivery_id, status="sent", attempts=attempts, http_status=outcome.status,
                                       error=None, next_at=None, inflight_at=None, updated_at=now)
        elif outcome.retry and attempts < self.attempts and (kind.replay_safe or outcome.unsent):
            pause = self.backoff[min(attempts - 1, len(self.backoff) - 1)] if self.backoff else 0.0
            pause *= 1 + self.jitter * (2 * self.rand() - 1)
            self.store.update_delivery(delivery_id, attempts=attempts, http_status=outcome.status,
                                       error=outcome.message, next_at=now + pause, inflight_at=None, updated_at=now)
            self._schedule(delivery_id, now + pause)
        else:
            tried = f" Tried {attempts} times." if attempts > 1 else ""
            self.store.update_delivery(delivery_id, inflight_at=None)
            return self._fail(delivery_id, (outcome.message or "It didn't arrive.") + tried, attempts, outcome.status)
        self._published(delivery_id)

    def _published(self, delivery_id):
        publish = getattr(self.runtime, "publish", None)
        row = self.store.delivery(delivery_id)
        if callable(publish) and row is not None:
            publish("outlets", {"event": "delivery", "runId": row["run_id"], "deliveryId": row["id"],
                             "status": row["status"]})


def named(run):
    """The destination id a run's sendTo names, or None."""
    send_to = (getattr(run, "extras", None) or {}).get("sendTo")
    destination = send_to.get("destination") if isinstance(send_to, dict) else None
    return destination if isinstance(destination, str) else None


def workflow_of(run):
    send_to = (getattr(run, "extras", None) or {}).get("sendTo")
    return send_to.get("workflow") if isinstance(send_to, dict) else None


def subscribers(service, run, event):
    """The destinations that take ``event`` for ``run``: the one it names, when that one takes the event, and each that
    takes it for every run."""
    if harness_api.stays_on_mac(getattr(run, "extras", None)):
        return []                       # a result that stays on this Mac goes to no destination
    chosen = named(run)
    out = []
    try:
        rows = service.store.destinations()
    except Exception:  # noqa: BLE001 -- a journal that can't be read sends nothing
        return []
    for row in sorted(rows, key=lambda r: r["id"] != chosen):
        config = row.get("config") or {}
        events = config.get("events") or ["run.finished"]
        if event in events and (row["id"] == chosen or config.get("scope") == "all"):
            out.append(row["id"])
    if event == "run.finished" and chosen and chosen not in out:
        out.insert(0, chosen)           # a named destination always gets the result (an old row, or a removed one)
    return out


class Listener:
    """Deliveries for a run's events, made on the listeners' thread, never the run's: its result to the destination it
    names and to each that takes every run's results; run.started and needs_you to those that take them."""

    @staticmethod
    def _service(run, runtime=None):
        for service in services():
            if runtime is not None and service.runtime is runtime:
                return service
            runs = getattr(service.runtime, "runs", None) or {}
            if runs.get(run.id) is run:
                return service
        return None

    def on_created(self, run, runtime):
        service = self._service(run, runtime)
        if service is None:
            return
        targets = subscribers(service, run, "run.started")
        if targets:
            payload = webhook.started_payload(run, workflow_of(run))
            for destination in targets:
                service.enqueue(run.id, destination, "run.started", payload)

    def on_event(self, run, event):
        if not isinstance(event, dict) or event.get("event") != "approval_requested" or event.get("preapproved"):
            return
        service = self._service(run)
        if service is None:
            return
        targets = subscribers(service, run, "needs_you")
        if targets:
            payload = webhook.needs_payload(run, event, workflow_of(run), service.clock())
            for destination in targets:
                service.enqueue(run.id, destination, "needs_you", payload)

    def on_finished(self, run, summary, private):
        service = self._service(run)
        if service is None:
            return
        for destination in subscribers(service, run, "run.finished"):
            service.enqueue(run.id, destination)

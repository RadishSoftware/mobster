"""The triggers service: one TriggerService per Runtime (harness_api.register_service)."""

import logging
import threading
import time
import uuid

from .. import full_disk_access
from ..notify_triggers import sources as notify_sources
from ..notify_triggers.rules import GLOBAL_DEFAULT, PER_RULE_DEFAULT, Rule, RuleSet
from ..notify_triggers.watch import Watcher
from . import model
from . import store as trigger_store
from .actions import Firer

log = logging.getLogger("mobster.triggers")

POLL_S = 0.5
ACCESS_S = 2.0
STORE_PATHS = {"iphone": lambda: full_disk_access.IPHONE_LIBRARY, "mac": lambda: full_disk_access.USERNOTED_DB}
NEEDS_ACCESS = ("Mobster needs Full Disk Access to read your notifications. Turn it on in System Settings › Privacy & "
                "Security › Full Disk Access.")
NO_MIRRORING = ("Your iPhone's notifications aren't on this Mac yet. Set up iPhone Mirroring and turn on notifications "
                "from your iPhone, then the trigger starts watching.")
ACCESS_LOST = "Mobster lost Full Disk Access, so it stopped reading notifications. Turn it back on to start again."
LIMITED = ("Mobster skipped this notification. A trigger runs at most 6 times an hour, and all triggers together at "
           "most 30.")
GONE = "Triggers aren't available right now. Restart Mobster, then try again."

_services = {}
_services_lock = threading.Lock()


def service_for(runtime):
    with _services_lock:
        return _services.get(id(runtime))


def start(runtime):
    """register_service's start: a TriggerService for the runtime."""
    service = TriggerService(runtime)
    with _services_lock:
        _services[id(runtime)] = service
    service.start()


def close(runtime):
    with _services_lock:
        service = _services.pop(id(runtime), None)
    if service is not None:
        service.close()


def ms(seconds):
    return round(seconds * 1000) if isinstance(seconds, (int, float)) else None


def rule_of(trigger):
    """The watcher's rule for a saved trigger: the trigger's id is its name, so limits and events say which."""
    return Rule(name=trigger["id"], app=trigger["app"].lower(),
                pattern=model.word_pattern(trigger["words"], trigger.get("regex")), source=trigger["source"],
                per_hour=PER_RULE_DEFAULT, enabled=trigger["enabled"])


def needed_stores(triggers):
    """The stores the enabled triggers read: iphone, mac or both."""
    need = set()
    for trigger in triggers:
        if trigger["enabled"]:
            need |= {"iphone", "mac"} if trigger["source"] == "any" else {trigger["source"]}
    return need


class TriggerService:
    def __init__(self, runtime, *, journal=None, make_source=None, check=None, clock=time.time, autostart=True,
                 poll_s=POLL_S, access_s=ACCESS_S, publish=None):
        self.runtime = runtime
        self.store = trigger_store.TriggerStore(journal if journal is not None else runtime.journal)
        self.clock = clock
        self.check = check or full_disk_access.check
        self.make_source = make_source or self._source
        self.poll_s, self.access_s = poll_s, access_s
        self.publish = publish or getattr(runtime, "publish", None)
        self.autostart = autostart
        self.firer = Firer(runtime, self.store, self.lookup, clock=clock, publish=self.publish)
        self._lock = threading.RLock()
        self._triggers = {}
        self._watcher = None
        self._watching = frozenset()      # the stores the watcher reads
        self._errors = {}                 # store -> why its last poll failed
        self._carry = None                # the limiter, kept across rebuilds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._checked = 0.0
        self._lost = set()                # stores whose access loss was announced
        self._listening = False           # a published workflow has a trigger.notification (on_change says)
        self._unsubscribe = None

    @staticmethod
    def _source(name):
        return notify_sources.IPhoneSource() if name == "iphone" else notify_sources.MacSource()

    def start(self):
        try:
            self.store.close_unknown()
        except Exception:  # noqa: BLE001 -- a journal that can't be read leaves nothing to close
            log.warning("triggers: couldn't close firings left sending")
        graphs = self._graphs()
        if graphs is not None:
            self._unsubscribe = graphs.on_change(self.workflows_changed)   # tells it now, then on every change
        self.reload()      # starts the thread only if something already listens: see _arm

    def workflows_changed(self, listening):
        """Workflows.triggers.on_change, under the workflows' lock: take only this service's lock, never theirs."""
        with self._lock:
            self._listening = bool(listening)
            rows = list(self._triggers.values())
        self._wake.set()
        self._arm(rows)

    def _arm(self, rows):
        """Start the watching thread when something listens and none is going"""
        if not self.autostart:
            return
        with self._lock:
            if (self._stop.is_set() or not self._stores(rows)
                    or (self._thread is not None and self._thread.is_alive())):
                return
            self._thread = threading.Thread(target=self._loop, name="mobster-triggers", daemon=True)
            self._thread.start()

    def close(self, timeout=3.0):
        self._stop.set()
        self._wake.set()
        if self._unsubscribe is not None:
            self._unsubscribe()
        with self._lock:      # after _stop is set: _arm starts nothing it didn't already start
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._drop_watcher()

    def lookup(self, trigger_id):
        with self._lock:
            return self._triggers.get(trigger_id)

    def reload(self):
        """Read the saved triggers and swap the rules in. Called at start and after every change."""
        rows = self.store.triggers()
        with self._lock:
            self._triggers = {row["id"]: row for row in rows}
            if self._watcher is not None:
                self._watcher.set_rules(self._ruleset(rows))
        self._wake.set()
        self._arm(rows)

    @staticmethod
    def _ruleset(rows):
        return RuleSet(rules=tuple(rule_of(row) for row in rows if row["enabled"]), per_hour=GLOBAL_DEFAULT)

    def list(self):
        return [self.public(row) for row in self.store.triggers()]

    def problem(self, row):
        """Why a trigger can't do its action now, or None: its destination or workflow was removed."""
        action = row["action"]
        try:
            if action["kind"] == "destination":
                from ..outlets import service as outlet_service
                service = outlet_service.service_for(self.runtime)
                if service is not None and service.store.destination(action["outletId"]) is None:
                    return "The destination was removed. Choose another one."
            elif action["kind"] == "workflow":
                workflows = getattr(self.runtime, "workflows", None)
                if workflows is not None:
                    workflows.get(action["workflowId"])
            elif not action.get("allowed"):
                return "Starting a task is off for this trigger."
        except Exception as error:  # noqa: BLE001 -- a lookup that fails says nothing; only a missing target does
            if getattr(error, "status", None) == 404:
                return "The workflow was deleted. Choose another one."
        return None

    def public(self, row):
        last = self.store.last_firing(row["id"])
        action = dict(row["action"])
        return {"id": row["id"], "name": row["name"], "enabled": row["enabled"], "app": row["app"],
                "appName": row["app_name"], "words": row["words"], "source": row["source"], "action": action,
                "problem": self.problem(row), "createdAt": ms(row["created_at"]),
                "last": ({"at": ms(last["at"]), "outcome": last["outcome"], "message": last["message"]}
                         if last else None)}

    def firing(self, row):
        return {"id": row["id"], "triggerId": row["trigger_id"], "trigger": row["trigger_name"], "app": row["app"],
                "at": ms(row["at"]), "action": row["kind"], "outcome": row["outcome"], "message": row["message"],
                "runId": row["run_id"]}

    def recent(self, limit=20):
        return [self.firing(row) for row in self.store.recent(limit)]

    def _check_target(self, action):
        """The action names something that exists now. ValueError otherwise, with a sentence."""
        if action["kind"] == "destination":
            from ..outlets import service as outlet_service
            service = outlet_service.service_for(self.runtime)
            if service is None:
                raise ValueError("Saved destinations aren't available right now. Restart Mobster, then try again.")
            if service.store.destination(action["outletId"]) is None:
                raise ValueError("That destination was removed. Choose another one in Connectors.")
        elif action["kind"] == "workflow":
            workflows = getattr(self.runtime, "workflows", None)
            if workflows is None:
                raise ValueError("Workflows aren't available right now. Restart Mobster, then try again.")
            try:
                workflows.get(action["workflowId"])
            except Exception as error:
                if getattr(error, "status", None) == 404:
                    raise ValueError("That workflow was deleted. Choose another one.") from None
                raise

    def _unique(self, name, but=None):
        for row in self.store.triggers():
            if row["id"] != but and row["name"].casefold() == name.casefold():
                raise ValueError(f"You already have a trigger called {name}. Give this one another name.")

    def add(self, body):
        fields = model.clean(body)
        self._unique(fields["name"])
        if self.store.count() >= model.MAX_TRIGGERS:
            raise ValueError(f"You have {model.MAX_TRIGGERS} triggers. Remove one first.")
        self._check_target(fields["action"])
        now = self.clock()
        row = self.store.add({"id": uuid.uuid4().hex[:12], "name": fields["name"], "enabled": fields["enabled"],
                              "app": fields["app"], "app_name": fields["appName"], "words": fields["words"],
                              "source": fields["source"], "action": fields["action"], "created_at": now,
                              "updated_at": now})
        self.reload()
        return self.public(row)

    def update(self, trigger_id, body):
        row = self.store.trigger(trigger_id)
        if row is None:
            from ..api_errors import APIError
            raise APIError("That trigger was removed.", 404, "not_found")
        current = {"name": row["name"], "app": row["app"], "appName": row["app_name"], "words": row["words"],
                   "source": row["source"], "enabled": row["enabled"], "action": row["action"]}
        fields = model.clean(body, current)
        if "name" in body:
            self._unique(fields["name"], but=row["id"])
        if "action" in body:
            self._check_target(fields["action"])
        saved = self.store.update(row["id"], name=fields["name"], enabled=fields["enabled"], app=fields["app"],
                                  app_name=fields["appName"], words=fields["words"], source=fields["source"],
                                  action=fields["action"], updated_at=self.clock())
        self.reload()
        return self.public(saved)

    def delete(self, trigger_id):
        row = self.store.trigger(trigger_id)
        if row is None:
            from ..api_errors import APIError
            raise APIError("That trigger was removed.", 404, "not_found")
        self.store.delete(row["id"])
        self.reload()
        return row["id"]

    def stores(self):
        """Each store's access now: granted, needed or missing. Reads no content."""
        return {name: self.check(path()) for name, path in STORE_PATHS.items()}

    def status(self):
        with self._lock:
            triggers = list(self._triggers.values())
            watching = sorted(self._watching)
            errors = dict(self._errors)
        need = self._stores(triggers)
        stores = self.stores()
        enabled = sum(1 for t in triggers if t["enabled"])
        if not need:
            state, message = "off", ("Turn on a trigger to start watching." if triggers else "Add a trigger to start.")
        elif any(stores[name] == "needed" for name in need):
            state, message = "needs_access", NEEDS_ACCESS
        elif "iphone" in need and stores["iphone"] == "missing":
            state, message = "missing", NO_MIRRORING
        elif errors:
            state, message = "error", next(iter(errors.values()))
        elif set(watching) >= need:
            state, message = "watching", "Mobster is watching for your notifications."
        else:
            state, message = "starting", "Mobster is about to start watching."
        return {"state": state, "message": message, "stores": stores, "watching": watching, "enabled": enabled,
                "limits": {"perTriggerPerHour": PER_RULE_DEFAULT, "perHour": GLOBAL_DEFAULT}}

    def _drop_watcher(self):
        with self._lock:
            watcher, self._watcher, self._watching = self._watcher, None, frozenset()
            if watcher is not None:
                self._carry = watcher.limiter
        if watcher is not None:
            try:
                watcher.close()
            except Exception:  # noqa: BLE001
                pass

    def reconcile(self):
        """Make the watch match the triggers and the access: the stores that are needed and granted, from now."""
        with self._lock:
            triggers = list(self._triggers.values())
        need = self._stores(triggers)
        want = {name for name in need if self.check(STORE_PATHS[name]()) == "granted"}
        self._announce_losses(need, want)
        if frozenset(want) == self._watching and (self._watcher is not None or not want):
            return
        self._drop_watcher()
        self._errors.clear()
        if not want:
            return
        sources = [self.make_source(name) for name in sorted(want)]
        watcher = Watcher(sources, self._ruleset(triggers), self.firer, listener=self._heard,
                          subscriber=self._subscribed, clock=self.clock)
        if self._carry is not None:
            watcher.limiter = self._carry
            watcher.limiter.per_hour = GLOBAL_DEFAULT
        watcher.start()      # primes every source: what is there now never fires
        with self._lock:
            self._watcher, self._watching = watcher, frozenset(want)

    def _graphs(self):
        """The workflow triggers module, or None when this Runtime has no workflows."""
        return getattr(getattr(self.runtime, "workflows", None), "triggers", None)

    def _stores(self, triggers):
        """The enabled triggers' stores, or both while a workflow listens; reads only this service's state."""
        return needed_stores(triggers) | ({"iphone", "mac"} if self._listening else set())

    def _subscribed(self, notification):
        """A new notification, to the workflows that subscribe themselves."""
        graphs = self._graphs()
        if graphs is not None:
            graphs.notify({"app": notification.app, "title": notification.title,
                           "body": notification.body or notification.subtitle,
                           "receivedAt": round((notification.date or self.clock()) * 1000)})

    def _announce_losses(self, need, want):
        for name in need - set(want):
            if name in self._watching and name not in self._lost and self.check(STORE_PATHS[name]()) == "needed":
                self._lost.add(name)
                self.firer.skip("", "Triggers", "", "access_lost", ACCESS_LOST)
        self._lost &= (need - set(want))

    def _heard(self, event, fields):
        """The watcher's lines: its refusals and its stores' errors."""
        if event == "refused" and fields.get("reason") == "rate_limited":
            trigger = self.lookup(fields.get("rule"))
            self.firer.skip(fields.get("rule") or "", trigger and trigger["name"], trigger and trigger["app"],
                            "rate_limited", LIMITED)
        elif event == "source_error":
            self._errors[fields.get("source")] = fields.get("message")
        elif event == "source_ok":
            self._errors.pop(fields.get("source"), None)

    def poll_once(self):
        """One pass: reconcile when due, then poll the stores. The service thread calls it; a test can too."""
        now = time.monotonic()
        if self._wake.is_set() or now - self._checked >= self.access_s:
            self._wake.clear()
            self._checked = now
            self.reconcile()
        watcher = self._watcher
        if watcher is not None:
            watcher.poll()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 -- the loop outlives one bad pass
                log.exception("triggers: a pass failed")
            with self._lock:
                if not self._stores(list(self._triggers.values())):
                    # Nothing listens: the pass above dropped the watcher. _arm starts a new thread when something does.
                    self._thread = None
                    return
            self._wake.wait(self.poll_s if self._watcher is not None else self.access_s)
            if self._stop.is_set():
                break

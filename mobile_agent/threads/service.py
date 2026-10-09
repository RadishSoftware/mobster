"""Conversations in one Runtime: the store, message routing, the run listener, the bus and the thread sink.

``ThreadService`` is made by the registered service's ``start(runtime)`` (after the journal opened and migrated the
``threads`` schema) and dropped by ``close(runtime)``. Everything else reaches it through ``service_for(runtime)``.

Message routing (SPEC §3.2 feature 2), one thread at a time:

| The thread's task                    | A message                                                          |
|--------------------------------------|--------------------------------------------------------------------|
| none running                         | starts a new task in the thread (201)                              |
| Mobster's agent, working             | steers it: Run.steer, "Sent while Mobster was working" (202)        |
| asking a question (clarify)          | answers it (202)                                                   |
| waiting for an approval              | 409 approval_pending: Run.steer refuses, so every surface agrees   |
| Quick mode, working                  | 409 steer_unsupported                                              |

A typed message never approves anything: an approval is answered only through POST /api/runs/{id}/approval.
"""

import hashlib
import json
import logging
import threading
import time

from .. import harness_api
from .. import secret_filter
from ..api_errors import APIError
from . import store as thread_store
from .store import (CORE_KINDS, KIND, MAX_RULES, MAX_SINK_ITEM_BYTES, NOT_FOUND, RULES_CHARS, ThreadFull,
                    ThreadStore, public_item)

log = logging.getLogger("mobster.threads")

VIA = ("composer", "voice", "cli", "tui", "mcp")
# Where a request came from (X-Mobster-Origin) -> the item's "via" and the steering source (S8). "api" (no header,
# a script) steers as the terminal does: never as the Mac app.
ORIGIN_VIA = {"app": "composer", "tui": "tui", "cli": "cli", "mcp": "mcp", "api": "cli"}
ORIGIN_SOURCE = {"app": "app", "tui": "tui", "cli": "cli", "mcp": "mcp", "api": "cli"}
START_CHARS = 4000
STEER_CHARS = 2000
ANSWER_CHARS = 500
NOTE_CHARS = 600
ANSWER_CONTEXT_CHARS = 600
FACTS = 6
FACT_CHARS = 200
UNREAD_NOTE = "Mobster finished before reading this."
STEER_UNSUPPORTED = "Quick mode can't take messages while it works. Wait for it to finish, or stop it."
ARCHIVED = "This conversation is archived. Restore it to continue."
# server.APPROVAL_PENDING, the sentence Run.steer refuses with (a test keeps them equal).
APPROVAL_PENDING = "Choose Send or Don't send above, or say what to do instead there."
# server.Run.steer's sentence for a message from a surface the task doesn't hear (a test keeps them equal).
STEER_NOT_ALLOWED = "This task takes messages only where it was started."
STEER_FILES ="Mobster reads files only when a task starts. Send them after this task, or stop it first."
FILES_MISSING = "Attaching files isn't in this build yet."

_services = {}            # id(runtime) -> ThreadService
_services_lock = threading.Lock()


def service_for(runtime):
    """The live ThreadService of ``runtime``, or None (the track is off, or the runtime closed)."""
    with _services_lock:
        return _services.get(id(runtime))


def services():
    with _services_lock:
        return list(_services.values())


def start(runtime):
    """register_service's start: a ThreadService on the runtime's journal."""
    service = ThreadService(runtime)
    with _services_lock:
        _services[id(runtime)] = service
    service.reconcile()


def close(runtime):
    with _services_lock:
        service = _services.pop(id(runtime), None)
    if service is not None:
        service.closed = True


# Other tracks learn that a thread was deleted (files: its attachments; memory: its proposals).
_delete_listeners = []


def add_delete_listener(fn):
    """``fn(runtime, thread_id, run_ids)`` after a thread is deleted. Exceptions are logged."""
    if not callable(fn):
        raise ValueError("A delete listener is a function")
    _delete_listeners.append(fn)


def clean_text(text, limit, what="A message"):
    if not isinstance(text, str):
        raise ValueError(f"{what} is text")
    text = text.strip()
    if not text:
        raise ValueError("Write a message first")
    if len(text) > limit:
        raise ValueError(f"{what} can be at most {limit:,} characters")
    return text


def may_message(run, source):
    """APIError 403 steer_not_allowed when ``source`` may not send ``run`` a message: Run.steer's rule (S5), applied
    to answers too, which reach the run through Run.answer_approval instead."""
    allowed = ("mcp", "app") if getattr(run, "origin", None) == "mcp" else ("app", "tui", "cli", "voice")
    if source not in allowed:
        raise APIError(STEER_NOT_ALLOWED, 403, "steer_not_allowed")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def prohibitions(text):
    """The rules in a message ("don't…", "never…", "only look…", "without…"), copied by code (plans.py's rule)."""
    from ..plans import _PROHIBITION
    text = " ".join(str(text or "").split())
    if text and text[-1] not in ".!?":
        text += "."  # a chat message often ends without a full stop; the rule reads to the end of a sentence
    return [m.group(0).strip() for m in _PROHIBITION.finditer(text) if m.group(0).strip()]


def merge_rules(rules, text):
    """``rules`` with the new ones in ``text`` added; the newest kept within MAX_RULES and RULES_CHARS."""
    out = list(rules or [])
    for rule in prohibitions(text):
        rule = rule[:300]
        if rule.casefold() in (r.casefold() for r in out):
            continue
        out.append(rule)
    while out and (len(out) > MAX_RULES or sum(len(r) + 3 for r in out) > RULES_CHARS):
        out.pop(0)
    return out


class ThreadService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = ThreadStore(runtime.journal)
        self.closed = False
        # Serializes routing per process: a message's state check and its effect are one step.
        self.lock = threading.RLock()
        self.runs = {}            # run id -> thread id, for the runs this process started in a thread
        self.last_app = {}        # run id -> the last app name its events named

    # -- bus ------------------------------------------------------------------------------------------------

    def publish(self, topic, event):
        publish = getattr(self.runtime, "publish", None)
        if callable(publish):
            publish(topic, event)

    def published_thread(self, thread, event):
        self.publish("threads", {"event": event, "thread": thread})

    def published_item(self, thread_id, item, event="thread_item"):
        self.publish(f"thread:{thread_id}", {"event": event, "item": public_item(item)})

    # -- threads ----------------------------------------------------------------------------------------------

    def thread(self, thread_id):
        thread = self.store.get(thread_id)
        if thread is None:
            raise APIError(NOT_FOUND, 404, "thread_not_found")
        return thread

    def create_thread(self, title=None, device=None):
        device_id = device_name = None
        if device is not None:
            phone = self.resolve(device)
            device_id, device_name = getattr(phone, "id", None), getattr(phone, "name", None)
        thread = self.store.create_thread(title=title, device=device_id, device_name=device_name)
        self.published_thread(thread, "thread_created")
        return thread

    def resolve(self, device):
        if not isinstance(device, str) or not 1 <= len(device) <= 128:
            raise ValueError("device must be a device's id, UDID or name")
        resolve = getattr(self.runtime, "resolve_device", None)
        if not callable(resolve) or getattr(self.runtime, "fleet", None) is None:
            raise ValueError("This Mobster runs every task on its own device; leave device out")
        return resolve(device)

    def edit_thread(self, thread_id, changes):
        self.thread(thread_id)
        clean = {}
        if "title" in changes:
            title = changes["title"]
            if not isinstance(title, str) or not 1 <= len(" ".join(title.split())) <= thread_store.TITLE_CHARS:
                raise ValueError("A title is 1 to 80 characters")
            clean["title"] = " ".join(title.split())
        for flag in ("archived", "pinned"):
            if flag in changes:
                if type(changes[flag]) is not bool:
                    raise ValueError(f"{flag} is true or false")
                clean[flag] = changes[flag]
        thread = self.store.update_thread(thread_id, clean)
        self.published_thread(thread, "thread_updated")
        return thread

    def active_run(self, thread):
        """The thread's run that is still going, or None."""
        runtime = self.runtime
        with runtime.lock:
            candidates = [runtime.runs.get(thread.get("lastRunId"))] + [
                run for run in runtime.runs.values()
                if isinstance(run.extras, dict) and run.extras.get("threadId") == thread["id"]]
        for run in candidates:
            if run is not None and run.finished_at is None:
                return run
        return None

    def delete_thread(self, thread_id, runs="delete"):
        if runs not in ("delete", "keep"):
            raise ValueError("runs is delete or keep")
        with self.lock:
            thread = self.thread(thread_id)
            if self.active_run(thread) is not None:
                raise APIError("Stop the task in this conversation before deleting it.", 409, "run_active")
            run_ids = self.store.run_ids(thread_id)
            deleted_runs = 0
            if runs == "delete":
                for run_id in run_ids:
                    if run_id in self.runtime.runs:
                        try:
                            self.runtime.delete_run(run_id)
                            deleted_runs += 1
                        except APIError:
                            pass  # it started meanwhile: kept, and the thread still goes
            self.store.delete_thread(thread_id)
        self.published_thread({"id": thread_id}, "thread_deleted")
        for fn in list(_delete_listeners):
            try:
                fn(self.runtime, thread_id, run_ids)
            except Exception:  # noqa: BLE001
                log.exception("a thread delete listener failed")
        return {"deleted": True, "runsDeleted": deleted_runs}

    # -- messages -----------------------------------------------------------------------------------------

    def message(self, thread_id, body, *, origin="api", key=None):
        """Route one message (SPEC §3.2 feature 2). Returns (status, response)."""
        fields = {"text", "attachmentIds", "appId", "engine", "outputFormat", "outputSchema", "device", "mode", "via"}
        if not isinstance(body, dict) or set(body) - fields:
            raise ValueError("A message has text, and optionally attachmentIds, appId, engine, outputFormat, "
                             "outputSchema, device, mode (auto or new_task) and via")
        mode = body.get("mode", "auto")
        if mode not in ("auto", "new_task"):
            raise ValueError("mode is auto or new_task")
        via = self.via(body.get("via"), origin)
        attachments = body.get("attachmentIds")
        if attachments is not None and (not isinstance(attachments, list) or
                                        not all(isinstance(a, str) for a in attachments)):
            raise ValueError("attachmentIds is a list of attachment ids")
        with self.lock:
            if key is not None:
                saved = self.store.request(key)
                if saved is not None:
                    if saved[0] != thread_id or saved[1] != fingerprint(body):
                        raise APIError("This request key belongs to a different message", 409,
                                       "idempotency_conflict")
                    return saved[2]["status"], {**saved[2]["response"], "replayed": True}
            thread = self.thread(thread_id)
            if thread.get("archived"):
                raise APIError(ARCHIVED, 409, "thread_archived")
            run = self.active_run(thread) if mode == "auto" else None
            routed = None
            if run is not None:
                routed = self.to_run(thread, run, body, via, origin, attachments)
            if routed is None:
                routed = self.start(thread, body, via, origin, attachments, key)
            if key is not None:
                self.store.remember(key, thread_id, fingerprint(body), {"status": routed[0], "response": routed[1]})
            return routed

    @staticmethod
    def via(value, origin):
        """The item's "via": the request's surface (its X-Mobster-Origin). Only the Mac app may say "voice"; a
        body's via is never a way to look like another surface."""
        default = ORIGIN_VIA.get(origin, "cli")
        if value is None:
            return default
        if value not in VIA:
            raise ValueError("via is composer, voice, cli, tui or mcp")
        return value if origin == "app" and value in ("composer", "voice") else default

    def to_run(self, thread, run, body, via, origin, attachments):
        """Route a message to the thread's running task. None when it finished meanwhile (start a new one)."""
        pending = run.approval
        if pending is not None and pending.get("kind") == "clarify":
            # An answer is a message to the task, so it follows Run.steer's rule for who may send one: a task
            # started over MCP hears only MCP and the Mac app, and any other task never hears MCP.
            may_message(run, self.source(body, origin))
            text = clean_text(body["text"] if "text" in body else None, ANSWER_CHARS, "An answer")
            # The question is in the thread before its answer, even when the listener hasn't caught up yet.
            self.clarify_item(thread["id"], run, {"approval_id": pending["id"], "label": pending.get("label")},
                              pending=pending)
            if not run.answer_approval(pending["id"], True, answer=text):
                if run.finished_at is not None:
                    return None
                raise APIError("Mobster's question is no longer waiting.", 409, "approval_not_pending")
            item = self.add_user(thread["id"], text, via, "answer", run.id, [], approvalId=pending["id"])
            self.answered(thread["id"], run.id, pending["id"], text)
            return 202, {"item": public_item(item), "routed": "answer"}
        if pending is not None:
            # An approval waits: refused as Run.steer refuses it on every surface, and nothing is queued.
            raise APIError(APPROVAL_PENDING, 409, "approval_pending", approvalId=pending.get("id"))
        if run.engine != "smart":
            raise APIError(STEER_UNSUPPORTED, 409, "steer_unsupported")
        if attachments:
            raise APIError(STEER_FILES, 409, "steer_attachments")
        text = clean_text(body.get("text"), STEER_CHARS)
        try:
            message = run.steer(text, source=self.source(body, origin))
        except APIError as error:
            if error.code == "run_not_active":
                return None
            raise
        item = self.add_steer(thread["id"], run.id, message["id"], message["text"], via)
        return 202, {"item": public_item(item), "routed": "steer"}

    @staticmethod
    def source(body, origin):
        source = ORIGIN_SOURCE.get(origin, "cli")
        return "voice" if source == "app" and body.get("via") == "voice" else source

    def start(self, thread, body, via, origin, attachments, key):
        """A new task in the thread. Returns (201, {item, run}), or, for a message that only asks Mobster to
        remember something, (201, {item, run: None, routed: "remember", proposals}): no task runs."""
        text = clean_text(body.get("text"), START_CHARS)
        count, _size = self.store.usage(thread["id"])
        if count + 2 + thread_store.RESERVE > thread_store.MAX_ITEMS:
            raise APIError(thread_store.FULL, 409, "thread_full")
        if not attachments and origin in harness_api.INTERACTIVE and body.get("mode", "auto") == "auto":
            offered = self.offer_to_remember(thread, text, via)
            if offered is not None:
                return offered
        extras = {"threadId": thread["id"], "via": via}
        if origin in harness_api.INTERACTIVE and "askUser" in harness_api.run_fields():
            # Every surface that shows a conversation shows its questions (a clarify item the Mac app, `mobster
            # chat` and the terminal UI answer inline), so a task a person started here may ask one (harness's
            # ASK_USER). MCP and the plain API never: nobody there is watching.
            extras["askUser"] = True
        if attachments:
            if "attachmentIds" not in harness_api.run_fields():
                raise APIError(FILES_MISSING, 409, "attachments_unavailable")
            extras["attachmentIds"] = attachments
        device = body.get("device")
        if device is not None and not isinstance(device, str):
            raise ValueError("device must be a device's id, UDID or name")
        if device is None and thread.get("device") and getattr(self.runtime, "pool", None) is None:
            device = thread["device"]
        kwargs = {}
        if device is not None and getattr(self.runtime, "fleet", None) is not None and \
                getattr(self.runtime, "pool", None) is None:
            kwargs["device"] = device
        run_key = f"thread-{thread['id']}-{key}"[:128] if key else None
        created = self.runtime.create(body.get("appId"), text, "live", run_key,
                                      output_schema=body.get("outputSchema"),
                                      output_format=body.get("outputFormat", "auto"), engine=body.get("engine"),
                                      extras=extras, origin=origin if origin in harness_api.ORIGINS else "api",
                                      **kwargs)
        run = created[0] if isinstance(created, tuple) else created
        user, _run_item = self.exchange(thread["id"], run, text=text, via=via)
        return 201, {"item": public_item(user) if user else None, "run": run.public(include_events=False)}

    def offer_to_remember(self, thread, text, via):
        """"Remember that my gym is the one on 5th Street" is a note for Mobster, not a task for the phone: the
        conversation gets the message and a suggestion to remember it (memory's ``memory_proposal`` item, answered
        with Remember or Not now), and nothing runs. None when the message asks for more than that, or this build
        has no memory: then it runs as a task, as before."""
        try:
            from ..memory import extract
            from ..memory.listener import publish, thread_item
            from ..memory.store import default_store, normalize
        except ImportError:
            return None
        found = extract.remember_only(text)
        if not found:
            return None
        store = default_store()
        if store is None:
            return None
        if any(secret_filter.is_secret(candidate.text) for candidate in found):
            raise APIError("Mobster doesn't remember passwords, codes or card numbers.", 400, "memory_secret")
        known = {normalize(fact.get("text") or "") for fact in store.all_facts()}
        if all(normalize(candidate.text) in known for candidate in found):
            raise APIError("Mobster already remembers this.", 409, "memory_duplicate")
        user = self.add_user(thread["id"], text, via, "remember", None, [])
        proposals = []
        for candidate in found:
            proposal = store.propose(candidate.text, "global", thread_id=thread["id"], pin=candidate.pin)
            if proposal is None:
                continue
            proposals.append(proposal)
            item = self.add_other(thread["id"], thread_item(proposal))
            if isinstance(item, dict) and item.get("id"):
                store.set_proposal_item(proposal["id"], item["id"])
            publish({"event": "proposal_created", "proposal": proposal})
        updated = self.store.update_thread(thread["id"], {})
        if updated is not None:
            self.published_thread(updated, "thread_updated")
        return 201, {"item": public_item(user), "run": None, "routed": "remember",
                     "proposals": [{"id": p["id"], "text": p["text"], "pin": p["pin"]} for p in proposals]}

    def add_other(self, thread_id, item):
        """Another track's item in a thread (a suggestion to remember), published as harness_api's sink does."""
        try:
            stored = self.store.append(thread_id, [item])[0]
        except Exception:  # noqa: BLE001 -- a thread that can't take the item never breaks the message
            log.exception("could not add a %s item", item.get("kind"))
            return None
        self.published_item(thread_id, stored)
        return stored

    # -- items ------------------------------------------------------------------------------------------------

    def attachment_refs(self, ids):
        """AttachmentRef for each id (files track's table, SPEC §3.4); a bare ref when it can't be read."""
        if not ids:
            return []
        refs = {}
        try:
            with self.runtime.journal.lock:
                rows = self.runtime.journal.connection.execute(
                    f"SELECT id, data FROM attachments WHERE id IN ({','.join('?' * len(ids))})", list(ids)).fetchall()
            for ident, data in rows:
                value = json.loads(data)
                refs[ident] = {"id": ident, "name": str(value.get("name") or ""), "mime": str(value.get("mime") or ""),
                               "bytes": int(value.get("bytes") or 0), "kind": str(value.get("kind") or "")}
        except Exception:  # noqa: BLE001 -- no files track, or another shape: bare refs
            pass
        return [refs.get(i) or {"id": i, "name": "", "mime": "", "bytes": 0, "kind": ""} for i in ids]

    def add_rules(self, thread_id, text):
        thread = self.store.get(thread_id)
        if thread is None:
            return
        rules = merge_rules(thread.get("rules"), text)
        if rules != (thread.get("rules") or []):
            updated = self.store.update_thread(thread_id, {"rules": rules}, touch=False)
            if updated is not None:
                self.published_thread(updated, "thread_updated")

    def add_user(self, thread_id, text, via, routed, run_id, attachments, **extra):
        item = {"kind": "user", "text": text, "via": via, "attachments": attachments, "routed": routed,
                "runId": run_id, **extra}
        stored = self.store.append(thread_id, [item])[0]
        self.add_rules(thread_id, text)
        self.published_item(thread_id, stored)
        return stored

    def add_steer(self, thread_id, run_id, message_id, text, via):
        """The user item for a message sent to a running task (idempotent by the message's id)."""
        added = self.store.append(thread_id, [{"kind": "user", "text": text, "via": via, "attachments": [],
                                               "routed": "steer", "runId": run_id, "messageId": message_id}],
                                  unless=lambda found: any(i.get("messageId") == message_id for i in found))
        if added is None:
            return self.store.find(thread_id, kind="user", run_id=run_id,
                                   match=lambda i: i.get("messageId") == message_id)[0]
        self.add_rules(thread_id, text)
        self.published_item(thread_id, added[0])
        return added[0]

    def exchange(self, thread_id, run, *, text=None, via=None):
        """The user item and the run item of a task started in the thread; idempotent by the run's id (the route
        and the listener's on_created both call it). Returns (user item or None, run item)."""
        extras = run.extras if isinstance(run.extras, dict) else {}
        via = via or extras.get("via") or ORIGIN_VIA.get(run.origin, "cli")
        attachments = self.attachment_refs(list(extras.get("attachmentIds") or []))
        user = {"kind": "user", "text": text if text is not None else run.goal, "via": via,
                "attachments": attachments, "routed": "new_run", "runId": run.id}
        start_app = run.app.get("name") if isinstance(run.app, dict) and run.app.get("bundleId") else None
        run_item = {"kind": "run", "runId": run.id, "goal": run.goal, "appName": start_app, "engine": run.engine,
                    "device": run.device_id}
        with self.lock:
            self.runs[run.id] = thread_id
            try:
                added = self.store.append(
                    thread_id, [user, run_item],
                    unless=lambda found: any(i.get("kind") == "run" and i.get("runId") == run.id for i in found))
            except ThreadFull:
                log.warning("thread %s is full; run %s isn't in it", thread_id, run.id)
                return None, None
            if added is None:
                found = self.store.find(thread_id, run_id=run.id)
                return (next((i for i in found if i["kind"] == "user" and i.get("routed") == "new_run"), None),
                        next((i for i in found if i["kind"] == "run"), None))
            changes = {"status": "running", "lastRunId": run.id}
            thread = self.store.get(thread_id) or {}
            if run.device_id and (not thread.get("device") or thread.get("device") != run.device_id):
                changes.update(device=run.device_id, deviceName=run.device_name)
            updated = self.store.update_thread(thread_id, changes)
        self.add_rules(thread_id, user["text"])
        for item in added:
            self.published_item(thread_id, item)
        if updated is not None:
            self.published_thread(self.store.get(thread_id) or updated, "thread_updated")
        return added[0], added[1]

    def answered(self, thread_id, run_id, approval_id, answer):
        for item in self.store.find(thread_id, kind="clarify", run_id=run_id,
                                    match=lambda i: i.get("approvalId") == approval_id):
            updated = self.store.update_item(thread_id, item["id"], {"answer": answer})
            if updated is not None:
                self.published_item(thread_id, updated, "thread_item_updated")

    def set_status(self, thread_id, status):
        thread = self.store.get(thread_id)
        if thread is None or thread.get("status") == status:
            return
        updated = self.store.update_thread(thread_id, {"status": status})
        if updated is not None:
            self.published_thread(updated, "thread_updated")

    # -- the run listener (harness_api.RunListener; on the listeners' thread) --------------------------------

    def thread_of(self, run):
        thread_id = self.runs.get(run.id)
        if thread_id is None and isinstance(getattr(run, "extras", None), dict):
            thread_id = run.extras.get("threadId")
        return thread_id

    def on_created(self, run):
        thread_id = run.extras.get("threadId") if isinstance(run.extras, dict) else None
        if thread_id and self.store.get(thread_id) is not None:
            self.exchange(thread_id, run)

    def on_event(self, run, event):
        thread_id = self.thread_of(run)
        if not thread_id:
            return
        # The thread view reads everything from its one /api/events stream: the run's events too, as the run SSE
        # sends them.
        self.publish(f"thread:{thread_id}", {"event": "run_event", "runId": run.id, "data": event})
        kind = event.get("event")
        if isinstance(event.get("app_name"), str) and event["app_name"]:
            self.last_app[run.id] = event["app_name"][:80]
        if kind == "user_message":
            source = event.get("source")
            via = {"app": "composer", "voice": "voice", "tui": "tui", "cli": "cli", "mcp": "mcp"}.get(source, "cli")
            self.add_steer(thread_id, run.id, event.get("id"), event.get("text") or "", via)
        elif kind == "steer_applied":
            ids = set(event.get("ids") or ())
            for item in self.store.find(thread_id, kind="user", run_id=run.id,
                                        match=lambda i: i.get("messageId") in ids and not i.get("readAt")):
                updated = self.store.update_item(thread_id, item["id"], {"readAt": event.get("timestamp")})
                if updated is not None:
                    self.published_item(thread_id, updated, "thread_item_updated")
        elif kind == "steer_unread":
            ids = set(event.get("ids") or ())
            for item in self.store.find(thread_id, kind="user", run_id=run.id,
                                        match=lambda i: i.get("messageId") in ids):
                updated = self.store.update_item(thread_id, item["id"], {"routed": "unread", "note": UNREAD_NOTE})
                if updated is not None:
                    self.published_item(thread_id, updated, "thread_item_updated")
        elif kind == "approval_requested":
            self.set_status(thread_id, "waiting")
            if event.get("kind") == "clarify":
                self.clarify_item(thread_id, run, event)
        elif kind == "approval_resolved":
            self.set_status(thread_id, "running")
            decision = str(event.get("decision") or "")
            for item in self.store.find(thread_id, kind="clarify", run_id=run.id,
                                        match=lambda i: i.get("approvalId") == event.get("approval_id")):
                changes = {"resolution": "answered" if decision.startswith(("answer", "choice")) else decision}
                if decision.startswith("choice:"):
                    choice = decision.split(":", 1)[1]
                    label = next((c["label"] for c in item.get("choices") or () if c.get("id") == choice), choice)
                    changes["answer"] = item.get("answer") or label
                updated = self.store.update_item(thread_id, item["id"], changes)
                if updated is not None:
                    self.published_item(thread_id, updated, "thread_item_updated")

    def clarify_item(self, thread_id, run, event, pending=None):
        approval_id = event.get("approval_id")
        if pending is None:
            with run.condition:
                pending = dict(run.approval) if run.approval and run.approval.get("id") == approval_id else None
        question = (pending or {}).get("question") or event.get("label") or ""
        choices = [{"id": c["id"], "label": c["label"]} for c in (pending or {}).get("choices") or ()
                   if isinstance(c, dict)] or [{"id": c, "label": c} for c in event.get("choices") or ()]
        item = {"kind": "clarify", "runId": run.id, "approvalId": approval_id, "question": question[:300],
                "choices": choices[:6], "allowText": bool((pending or {}).get("allowText", True))}
        # Answered here before this item existed (the listener runs after the answer): keep the answer.
        answer = self.store.find(thread_id, kind="user", run_id=run.id,
                                 match=lambda i: i.get("routed") == "answer" and i.get("approvalId") == approval_id)
        if answer:
            item["answer"] = answer[0]["text"]
        try:
            added = self.store.append(thread_id, [item],
                                      unless=lambda found: any(i.get("approvalId") == approval_id for i in found))
        except ThreadFull:
            return
        if added:
            self.published_item(thread_id, added[0])

    def on_finished(self, run, summary, private):
        thread_id = self.thread_of(run)
        self.runs.pop(run.id, None)
        last_app = self.last_app.pop(run.id, None)
        if not thread_id or self.store.get(thread_id) is None:
            return
        self.record_result(thread_id, run, summary or {}, private or {}, last_app)
        if run.events and run.events[-1].get("event") == "run_finished":
            self.publish(f"thread:{thread_id}", {"event": "run_event", "runId": run.id, "data": run.events[-1]})

    def record_result(self, thread_id, run, summary, private, last_app=None):
        """Keep the run's result in its item, so the conversation outlives the run's 100-run retention."""
        items = self.store.find(thread_id, kind="run", run_id=run.id)
        if not items:
            self.exchange(thread_id, run)
            items = self.store.find(thread_id, kind="run", run_id=run.id)
            if not items:
                return
        item = items[0]
        result = result_of(summary, run)
        changes = {"result": result, "_context": context_of(summary, private, last_app)}
        if not item.get("appName") and last_app:
            changes["appName"] = last_app
        try:
            updated = self.store.update_item(thread_id, item["id"], changes)
        except ThreadFull:
            updated = self.store.update_item(thread_id, item["id"], {"result": result})
        if updated is not None:
            self.published_item(thread_id, updated, "thread_item_updated")
        thread = self.store.get(thread_id)
        if thread is not None and thread.get("lastRunId") in (run.id, None):
            changes = {"status": "idle"}
            summary_text = self.condensed(thread_id)
            if summary_text is not None:
                changes["summary"] = summary_text
            updated_thread = self.store.update_thread(thread_id, changes)
            if updated_thread is not None:
                self.published_thread(updated_thread, "thread_updated")

    def condensed(self, thread_id):
        """The thread's rolling summary (deterministic in v1): the condensed line of the exchanges older than the
        last three, or None while there are none."""
        from .context import RECENT, condensed_line, exchanges
        items = self.store.items(thread_id)
        done = exchanges(items)
        if len(done) <= RECENT:
            return None
        older = done[:-RECENT]
        last = max((i["seq"] for i in items if i.get("runId") == older[-1]["runId"]), default=0)
        return {"text": condensed_line(older), "upToSeq": last, "updatedAt": time.time() * 1000}

    def reconcile(self):
        """After a restart: a thread left running or waiting gets its run's result (the run was marked
        interrupted when the journal reloaded) and goes back to idle."""
        try:
            stale = self.store.threads_with_status(("running", "waiting"))
        except Exception:  # noqa: BLE001
            log.exception("could not read the conversations")
            return
        for thread in stale:
            run = self.runtime.runs.get(thread.get("lastRunId"))
            if run is not None and run.finished_at is None:
                continue
            if run is not None:
                for item in self.store.find(thread["id"], kind="run", run_id=run.id):
                    if not item.get("result"):
                        self.store.update_item(thread["id"], item["id"], {"result": result_of(run.summary or {}, run)})
            self.store.update_thread(thread["id"], {"status": "idle"}, touch=False)

    # -- the thread sink (harness_api.register_thread_sink) ------------------------------------------------

    def sink_append(self, thread_id, item):
        if not isinstance(item, dict) or not isinstance(item.get("kind"), str) or not KIND.fullmatch(item["kind"]):
            raise ValueError("A thread item has a kind of 1 to 40 lowercase letters or underscores")
        if item["kind"] in CORE_KINDS:
            raise ValueError("user, run and clarify items come only from conversations")
        clean = {k: v for k, v in item.items() if k not in ("id", "seq", "at") and not k.startswith("_")}
        if len(json.dumps(clean, ensure_ascii=False, allow_nan=False).encode()) > MAX_SINK_ITEM_BYTES - 200:
            raise ValueError(f"A thread item is at most {MAX_SINK_ITEM_BYTES:,} bytes")
        if self.store.get(thread_id) is None:
            return None
        added = self.store.append(thread_id, [clean])
        if added:
            self.published_item(thread_id, added[0])
            return public_item(added[0])
        return None

    def sink_update(self, thread_id, item_id, changes):
        if not isinstance(changes, dict):
            raise ValueError("changes is an object")
        clean = {k: v for k, v in changes.items() if not k.startswith("_")}
        kinds = None  # every kind but the core ones
        found = self.store.find(thread_id, match=lambda i: i.get("id") == item_id)
        if not found or found[0].get("kind") in CORE_KINDS:
            return None
        kinds = {found[0]["kind"]}
        updated = self.store.update_item(thread_id, item_id, clean, kinds=kinds, max_bytes=MAX_SINK_ITEM_BYTES)
        if updated is not None:
            self.published_item(thread_id, updated, "thread_item_updated")
            return public_item(updated)
        return None


def result_of(summary, run=None):
    """The run item's public result: status, outcome, the answer's headline, facts, cost and time."""
    summary = summary if isinstance(summary, dict) else {}
    facts = []
    for entry in summary.get("proof") or ():
        if not isinstance(entry, dict) or not entry.get("quote"):
            continue
        fact = secret_filter.redact(" ".join(str(entry["quote"]).split()))[:FACT_CHARS]
        if entry.get("app"):
            fact += f" ({str(entry['app'])[:40]})"
        facts.append(fact)
        if len(facts) >= FACTS:
            break
    answer = summary.get("answer")
    cost = summary.get("costUsd")
    if cost is None and run is not None:
        cost = getattr(run, "cost_usd", None)
    elapsed = summary.get("elapsed_ms")
    return {"status": summary.get("status") or (getattr(run, "status", None) or "error"),
            "outcome": summary.get("outcome") if isinstance(summary.get("outcome"), str) else None,
            "answer": secret_filter.redact(answer)[:1000] if isinstance(answer, str) else None,
            "facts": facts, "costUsd": cost if isinstance(cost, (int, float)) else None,
            "elapsedMs": elapsed if isinstance(elapsed, (int, float)) else None}


def context_of(summary, private, last_app):
    """What the next task in the thread reads about this one (private: never sent by the API): the answer in full
    (up to 600 characters), the agent's notes (600 characters, each redacted, a secret-looking one dropped) and
    the app it ended in."""
    full = summary.get("full_answer") or (summary.get("data") if isinstance(summary.get("data"), str) else None) \
        or summary.get("answer")
    notes, total = [], 0
    for note in (private or {}).get("notes") or ():
        if not isinstance(note, str) or not note.strip():
            continue
        clean = secret_filter.redact(" ".join(note.split()))
        if secret_filter.is_secret(clean):
            continue
        if total + len(clean) > NOTE_CHARS:
            clean = clean[:max(0, NOTE_CHARS - total - 1)] + "…"
        if len(clean) > 1:
            notes.append(clean)
            total += len(clean)
        if total >= NOTE_CHARS:
            break
    out = {"notes": notes, "lastApp": last_app}
    if isinstance(full, str) and full.strip():
        out["answer"] = secret_filter.redact(" ".join(full.split()))[:ANSWER_CONTEXT_CHARS]
    return out


class Listener:
    """The run listener conversations registers (harness_api.register_run_listener): finds the run's Runtime's
    service and hands it the call. Runs outside a thread cost a dict lookup."""

    @staticmethod
    def _service(run):
        for service in services():
            if run.id in service.runs or service.runtime.runs.get(run.id) is run:
                return service
        return None

    def on_created(self, run, runtime):
        service = service_for(runtime)
        if service is not None and isinstance(run.extras, dict) and run.extras.get("threadId"):
            service.on_created(run)

    def on_event(self, run, event):
        if not (isinstance(getattr(run, "extras", None), dict) and run.extras.get("threadId")):
            return
        service = self._service(run)
        if service is not None:
            service.on_event(run, event)

    def on_finished(self, run, summary, private):
        if not (isinstance(getattr(run, "extras", None), dict) and run.extras.get("threadId")):
            return
        service = self._service(run)
        if service is not None:
            service.on_finished(run, summary, private)


def sink_append(thread_id, item):
    """harness_api.post_thread_item lands here: the item in the thread of whichever Runtime has it."""
    for service in services():
        if service.store.get(thread_id) is not None:
            return service.sink_append(thread_id, item)
    return None


def sink_update(thread_id, item_id, changes):
    for service in services():
        if service.store.get(thread_id) is not None:
            return service.sink_update(thread_id, item_id, changes)
    return None

"""Conversations in one Runtime: the store, message routing, the run listener, the bus and the thread sink."""

import hashlib
import json
import logging
import re
import threading

from .. import harness_api, secret_filter
from ..api_errors import APIError, nothing_started
from ..service_support import ServiceRegistry
from . import store as thread_store
from .store import (
    CORE_KINDS,
    KIND,
    MAX_RULES,
    MAX_SINK_ITEM_BYTES,
    RULES_CHARS,
    ThreadFull,
    ThreadMissing,
    ThreadStore,
    failed_request,
    public_item,
    request_result,
    request_unknown,
)
from .summary import context_of, group_fields, referents_of, result_of

log = logging.getLogger("mobster.threads")

VIA = ("composer", "voice", "cli", "tui", "mcp", "remote")
# Where a request came from (X-Mobster-Origin) -> the item's "via" and the steering source (S8). "api" (no header,
# a script) steers as the terminal does: never as the Mac app.
ORIGIN_VIA = {"app": "composer", "voice": "voice", "tui": "tui", "cli": "cli", "mcp": "mcp", "api": "cli",
              "remote": "remote"}
ORIGIN_SOURCE = {"app": "app", "tui": "tui", "cli": "cli", "mcp": "mcp", "api": "cli", "remote": "remote"}
START_CHARS = 4000
STEER_CHARS = 2000
ANSWER_CHARS = 500
UNREAD_NOTE = "Mobster finished before reading this."
STEER_UNSUPPORTED = "This task can't take messages while it works. Wait for it to finish, or stop it."
ARCHIVED = "This conversation is archived. Restore it to continue."
# run.APPROVAL_PENDING, the sentence Run.steer refuses with (a test keeps them equal).
APPROVAL_PENDING = "Choose Send or Don't send above, or say what to do instead there."
# run.Run.steer's sentence for a message from a surface the task doesn't hear (a test keeps them equal).
STEER_NOT_ALLOWED = "This task takes messages only where it was started."
STEER_FILES ="Mobster reads files only when a task starts. Send them after this task, or stop it first."
REMOTE_FILES = "Files can't be sent through Mobster Remote."
STEPS_RUNNING = ("This workflow is running several steps at once. Send your message when they finish.")
GROUP_RUNNING = ("This task is running on several devices. Send your message when they finish, or answer each device’s "
                 "question on its tab.")
FILES_MISSING = "Attaching files isn't in this build yet."
ACCOUNTS_MISSING = "Profiles aren't available right now. Restart Mobster, then try again."
OUTLETS_MISSING = "Sending results is part of Mobster for Mac. Open it to send a result to another tool."

# A rule in a message: a sentence with "don't", "never", "only look" or "without".
_PROHIBITION = re.compile(r"[^.!?]*\b(?:do not|don't|never|only look|without)\b[^.!?]*[.!?]", re.I)
_services = ServiceRegistry()


def service_for(runtime):
    """The live ThreadService of ``runtime``, or None (the track is off, or the runtime closed)."""
    return _services.get(runtime)


def services():
    return _services.all()


def start(runtime):
    """register_service's start: a ThreadService on the runtime's journal."""
    service = ThreadService(runtime)
    _services.add(runtime, service)
    service.reconcile()


def close(runtime):
    service = _services.pop(runtime)
    if service is not None:
        service.closed = True


def clean_text(text, limit, what="A message"):
    if not isinstance(text, str):
        raise ValueError(f"{what} is text")
    text = text.strip()
    if not text:
        raise ValueError("Write a message first")
    if len(text) > limit:
        raise ValueError(f"{what} can be at most {limit:,} characters")
    return text


def may_message(run, source, remote_grant=None):
    """APIError 403 steer_not_allowed when ``source`` may not send ``run`` a message: Run.steer's rule (S5), applied
    to answers too, which reach the run through Run.answer_approval instead."""
    if source not in harness_api.steer_sources(getattr(run, "origin", None)):
        raise APIError(STEER_NOT_ALLOWED, 403, "steer_not_allowed")
    if source == "remote":
        extras = run.extras if isinstance(getattr(run, "extras", None), dict) else {}
        if not remote_grant or extras.get("remoteGrant") != remote_grant:
            raise APIError(STEER_NOT_ALLOWED, 403, "steer_not_allowed")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def run_request_key(thread_id, key):
    return f"thread-{thread_id}-{fingerprint(key)}" if key else None


def prohibitions(text):
    """The rules in a message ("don't…", "never…", "only look…", "without…"), copied by code."""
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

    def publish(self, topic, event):
        publish = getattr(self.runtime, "publish", None)
        if callable(publish):
            publish(topic, event)

    def published_thread(self, thread, event):
        self.publish("threads", {"event": event, "thread": thread})

    def published_item(self, thread_id, item, event="thread_item"):
        self.publish(f"thread:{thread_id}", {"event": event, "item": public_item(item)})

    def thread(self, thread_id):
        thread = self.store.get(thread_id)
        if thread is None:
            raise ThreadMissing()
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
        return {"deleted": True, "runsDeleted": deleted_runs}

    def message(self, thread_id, body, *, origin="api", key=None, remote_grant=None):
        """Route one message (SPEC §3.2 feature 2)."""
        if origin == "remote" and not remote_grant:
            raise ValueError("A message through Mobster Remote names its grant")
        fields = {"text", "attachmentIds", "appId", "engine", "outputFormat", "outputSchema", "device", "phones",
                  "profiles", "mode",
                  "via", "account", "longform", "pinnedApps", "sendTo",
                  "permissionMode", "queue"}
        if not isinstance(body, dict) or set(body) - fields:
            raise ValueError(
                "A message has text, and optionally attachmentIds, appId, pinnedApps, engine, outputFormat, "
                             "outputSchema, device or phones, account, mode (auto or new_task), via, longform, sendTo "
                             "and permissionMode")
        if body.get("device") is not None and body.get("phones") is not None:
            raise ValueError("Name the phones in phones, or one phone in device, not both")
        if body.get("account") is not None and body.get("phones") is not None:
            raise ValueError("A profile runs on its own iPhone: leave phones out")
        if body.get("profiles") is not None and any(body.get(name) is not None for name in ("phones", "device",
                                                                                          "account")):
            raise ValueError("Run on several profiles with profiles alone: each runs on its own iPhone")
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
                    response = request_result(saved[2])
                    return response["status"], {**response["response"], "replayed": True}
            thread = self.thread(thread_id)
            if thread.get("archived"):
                raise APIError(ARCHIVED, 409, "thread_archived")
            if key is not None:
                self.store.remember(key, thread_id, fingerprint(body), None)
            try:
                run = self.active_run(thread) if mode == "auto" else None
                routed = self.to_run(thread, run, body, via, origin, attachments, remote_grant) if run else None
                if routed is None:
                    routed = self.start(thread, body, via, origin, attachments, key, remote_grant)
            except (APIError, ValueError) as error:
                if key is not None and not (isinstance(error, APIError) and error.code == "request_outcome_unknown"):
                    self.store.remember(key, thread_id, fingerprint(body), failed_request(error))
                raise
            if key is not None:
                self.store.remember(key, thread_id, fingerprint(body), {"status": routed[0], "response": routed[1]})
            return routed

    @staticmethod
    def via(value, origin):
        """The item's "via": the request's surface (its X-Mobster-Origin)."""
        default = ORIGIN_VIA.get(origin, "cli")
        if value is None:
            return default
        if value not in VIA:
            raise ValueError("via is composer, voice, cli, tui, mcp or remote")
        return value if origin == "app" and value in ("composer", "voice") else default

    def to_run(self, thread, run, body, via, origin, attachments, remote_grant=None):
        """Route a message to the thread's running task. None when it finished meanwhile (start a new one)."""
        if getattr(run, "group_id", None):
            # A task on several phones takes no messages while it runs: each phone's run is its own, and one message
            # can't steer them all the same way. Questions are answered on their phone's tab.
            raise APIError(GROUP_RUNNING, 409, "group_running", groupId=run.group_id)
        with self.runtime.lock:
            going = sum(1 for other in self.runtime.runs.values() if isinstance(other.extras, dict)
                        and other.extras.get("threadId") == thread["id"] and other.finished_at is None)
        if going > 1:
            # A workflow running steps side by side: one message can't steer them all the same way.
            raise APIError(STEPS_RUNNING, 409, "steps_running")
        pending = run.approval
        if origin == "remote":
            # Through Mobster Remote only the app that started the task reaches it, and never with files.
            may_message(run, "remote", remote_grant)
            if attachments:
                raise APIError(REMOTE_FILES, 409, "steer_attachments")
        if pending is not None and pending.get("kind") == "clarify":
            # An answer is a message to the task, so it follows Run.steer's rule for who may send one: a task
            # started over MCP hears only MCP and the Mac app, and any other task never hears MCP.
            may_message(run, self.source(body, origin), remote_grant)
            text = clean_text(body["text"] if "text" in body else None, ANSWER_CHARS, "An answer")
            # The question is in the thread before its answer, even when the listener hasn't caught up yet.
            self.clarify_item(thread["id"], run, {"approval_id": pending["id"], "label": pending.get("label")},
                              pending=pending)
            if not run.answer_approval(pending["id"], True, answer=text):
                if run.finished_at is not None:
                    return None
                raise APIError("Mobster's question is no longer waiting.", 409, "approval_not_pending")
            try:
                item = self.add_user(thread["id"], text, via, "answer", run.id, [], approvalId=pending["id"])
                self.answered(thread["id"], run.id, pending["id"], text)
            except Exception:
                raise request_unknown() from None
            return 202, {"item": public_item(item), "routed": "answer"}
        if pending is not None:
            # An approval waits: refused as Run.steer refuses it on every surface, and nothing is queued.
            raise APIError(APPROVAL_PENDING, 409, "approval_pending", approvalId=pending.get("id"))
        if not run.steerable:
            from .. import ai_plans
            if ai_plans.is_agent(run.engine):
                from ..ai_plans import agents
                raise APIError(f"{agents.get(run.engine).name} can't take messages while it works. Wait for it to "
                               "finish, or stop it.", 409, "steer_unsupported")
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
        try:
            item = self.add_steer(thread["id"], run.id, message["id"], message["text"], via)
        except Exception:
            raise request_unknown() from None
        return 202, {"item": public_item(item), "routed": "steer"}

    @staticmethod
    def source(body, origin):
        source = ORIGIN_SOURCE.get(origin, "cli")
        return "voice" if source == "app" and body.get("via") == "voice" else source

    def start(self, thread, body, via, origin, attachments, key, remote_grant=None):
        """A new task in the thread."""
        if origin == "remote" and attachments:
            raise APIError(REMOTE_FILES, 409, "steer_attachments")
        text = clean_text(body.get("text"), START_CHARS)
        count, _size = self.store.usage(thread["id"])
        if count + 2 + thread_store.RESERVE > thread_store.MAX_ITEMS:
            raise ThreadFull()
        if not attachments and origin in harness_api.INTERACTIVE and body.get("mode", "auto") == "auto":
            offered = self.offer_to_remember(thread, text, via, body)
            if offered is not None:
                return offered
        extras = {"threadId": thread["id"], "via": via}
        if origin == "remote":
            extras["remoteGrant"] = remote_grant
        if origin in harness_api.INTERACTIVE and "askUser" in harness_api.run_fields():
            # Every surface that shows a conversation shows its questions (a clarify item the Mac app, `mobster
            # chat` and the terminal UI answer inline), so a task a person started here may ask one (harness's
            # ASK_USER). MCP and the plain API never: nobody there is watching.
            extras["askUser"] = True
        if body.get("permissionMode") is not None:
            # The composer's per-task mode (track permissions); asking less than Settings needs the Mac app's session,
            # which the server checked before this.
            if "permissionMode" not in harness_api.run_fields():
                raise APIError("This Mobster can't set a task's permission mode. Update Mobster.", 409,
                               "permissions_unavailable")
            extras["permissionMode"] = body["permissionMode"]
        if attachments:
            if "attachmentIds" not in harness_api.run_fields():
                raise APIError(FILES_MISSING, 409, "attachments_unavailable")
            extras["attachmentIds"] = attachments
        account = self.account_for(thread, body)
        if account is not None:
            extras["account"] = account
        if body.get("sendTo") is not None:
            # Where the new task's result goes when it finishes (track outlets): the run field, as POST /api/runs.
            if "sendTo" not in harness_api.run_fields():
                raise APIError(OUTLETS_MISSING, 409, "outlets_unavailable")
            extras["sendTo"] = body["sendTo"]
        if body.get("longform") is not None:
            # A long task started from the conversation (its plan card's Start): the run field, as POST /api/runs.
            if "longform" not in harness_api.run_fields():
                raise APIError("Long tasks aren't in this build.", 409, "longform_unavailable")
            extras["longform"] = body["longform"]
        device = body.get("device")
        if device is not None and not isinstance(device, str):
            raise ValueError("device must be a device's id, UDID or name")
        if body.get("profiles") is not None and getattr(self.runtime, "fanout", None) is not None:
            extras.pop("account", None)  # each member is its own profile (fanout.start_profiles)
            return self.start_group(thread, body, text, via, origin, extras, None, key, profiles=body["profiles"])
        phones = body.get("phones")
        if phones is None and device is None and account is None and (thread.get("group") or {}).get("phones"):
            phones = list(thread["group"]["phones"])  # the next task runs where the last one did: on every phone
        if phones is not None and getattr(self.runtime, "fanout", None) is not None:
            extras.pop("account", None)  # each phone acts as itself; an account runs on its own iPhone
            return self.start_group(thread, body, text, via, origin, extras, phones, key)
        if account is not None and "device" not in body:
            device = None  # the account's phone (accounts/routing.py), not the conversation's last one
        elif device is None and thread.get("device") and getattr(self.runtime, "pool", None) is None:
            device = thread["device"]
        kwargs = {}
        if device is not None and getattr(self.runtime, "fleet", None) is not None and \
                getattr(self.runtime, "pool", None) is None:
            kwargs["device"] = device
        run_key = run_request_key(thread["id"], key)
        if body.get("pinnedApps") is not None:
            kwargs["pinned_apps"] = body["pinnedApps"]
        if key and self.runtime.journal.lookup(f"thread-{thread['id']}-{key}"[:128]) is not None:
            # A previous build may have admitted this key before it saved the conversation response.
            raise request_unknown()
        try:
            created = self.runtime.create(body.get("appId"), text, "live", run_key,
                                          output_schema=body.get("outputSchema"),
                                          output_format=body.get("outputFormat", "auto"), engine=body.get("engine"),
                                          extras=extras, origin=origin if origin in harness_api.ORIGINS else "api",
                                          queue=body.get("queue", True) is not False, **kwargs)
        except (APIError, ValueError) as error:
            if run_key is not None and self.runtime.journal.lookup(run_key) is not None:
                raise request_unknown() from None
            if run_key is not None and isinstance(error, APIError):
                raise nothing_started(error) from None   # no task under this request's key: none was recorded or sent
            raise
        run = created[0] if isinstance(created, tuple) else created
        try:
            user, _run_item = self.exchange(thread["id"], run, text=text, via=via)
            return 201, {"item": public_item(user) if user else None, "run": run.public(include_events=False)}
        except Exception:
            raise request_unknown() from None

    def account_for(self, thread, body):
        """The account a new task acts as (track accounts' ``account`` run field): the one the message names (null:
        none), else the one the conversation's last task acted as, so "now reply to the next one" stays @acme."""
        if "account" in body:
            if body["account"] is not None and "account" not in harness_api.run_fields():
                raise APIError(ACCOUNTS_MISSING, 409, "accounts_unavailable")
            return body["account"]
        if "account" not in harness_api.run_fields():
            return None
        last = self.runtime.runs.get(thread.get("lastRunId") or "")
        previous = (getattr(last, "extras", None) or {}).get("account")
        return previous["id"] if isinstance(previous, dict) and isinstance(previous.get("id"), str) else None

    def start_group(self, thread, body, text, via, origin, extras, phones, key, profiles=None):
        """A new task in the thread on several phones at once (fanout.py): one user item, one group item that
        follows the group, and each phone's run item."""
        if key and self.runtime.fanout.has_request(f"thread-{thread['id']}-{key}"[:128]):
            raise request_unknown()
        run_key = run_request_key(thread["id"], key)
        try:
            group, _replayed = (self.runtime.start_profiles if profiles is not None else self.runtime.start_group)(
                profiles if profiles is not None else phones, body.get("appId"), text, idempotency_key=run_key,
                fingerprint=fingerprint(body), thread_id=thread["id"], output_schema=body.get("outputSchema"),
                output_format=body.get("outputFormat", "auto"), engine=body.get("engine"), extras=extras,
                origin=origin if origin in harness_api.ORIGINS else "api")
        except (APIError, ValueError) as error:
            if run_key is not None and self.runtime.fanout.has_request(run_key):
                raise request_unknown() from None
            if run_key is not None and isinstance(error, APIError) and "refused" not in error.details:
                # A group takes its key before its first phone's run (fanout._open), so without it nothing was recorded.
                # The 409 for a group no phone started ("refused" lists each phone) isn't marked: a phone's run may
                # have been recorded, then interrupted (start_failed). It is a 4xx refusal all the same.
                raise nothing_started(error) from None
            raise
        try:
            user, _group_item = self.exchange_group(thread["id"], group, text=text, via=via)
            first = None
            for member in group["members"]:
                run = self.runtime.runs.get(member.get("runId")) if member.get("runId") else None
                if run is not None:
                    self.exchange(thread["id"], run)
                    first = first or run
            public = self.runtime.fanout.public(group)
            return 201, {"item": public_item(user) if user else None,
                         "run": first.public(include_events=False) if first is not None else None, "group": public}
        except Exception:
            raise request_unknown() from None

    def exchange_group(self, thread_id, group, *, text, via):
        """The user item and the group item of a task started on several phones; idempotent by the group's id."""
        public = self.runtime.fanout.public(group)
        attachments = self.attachment_refs(list(((group.get("create") or {}).get("extras") or {}).get("attachmentIds")
                                                 or []))
        user = {"kind": "user", "text": text, "via": via, "attachments": attachments, "routed": "new_run",
                "runId": None, "groupId": group["id"]}
        item = {"kind": "group", **group_fields(public)}
        with self.lock:
            try:
                added = self.store.append(thread_id, [user, item], unless=lambda found: any(
                    i.get("kind") == "group" and i.get("groupId") == group["id"] for i in found))
            except ThreadFull:
                log.warning("thread %s is full; group %s isn't in it", thread_id, group["id"])
                return None, None
            if added is None:
                found = self.store.find(thread_id, match=lambda i: i.get("groupId") == group["id"]
                                        and i.get("kind") in ("user", "group"))
                return (next((i for i in found if i["kind"] == "user"), None),
                        next((i for i in found if i["kind"] == "group"), None))
            by_profile = any(member.get("profile") for member in public["members"])
            # A group by profile doesn't make the thread's next task run on those phones: the phones are the
            # profiles', not a choice of the person's.
            phones = [] if by_profile else [member["device"] for member in public["members"]]
            names = {} if by_profile else {member["device"]: member.get("deviceName") for member in public["members"]}
            first = next((member["runId"] for member in public["members"] if member.get("runId")), None)
            updated = self.store.update_thread(thread_id, {
                "status": "waiting" if public["state"] == "waiting" else "running", "lastRunId": first,
                "lastGroupId": group["id"], "group": {"id": group["id"], "phones": phones, "names": names},
                "device": None, "deviceName": None})
        self.add_rules(thread_id, text)
        for stored in added:
            self.published_item(thread_id, stored)
        if updated is not None:
            self.published_thread(updated, "thread_updated")
        return added[0], added[1]

    def on_group(self, group, event):
        """fanout's listener: the group's item follows it (a phone started, was given up, finished)."""
        thread_id = group.get("threadId")
        if not thread_id or self.store.get(thread_id) is None:
            return
        with self.lock:
            found = self.store.find(thread_id, kind="group", match=lambda i: i.get("groupId") == group["id"])
            if not found:
                return
            try:
                updated = self.store.update_item(thread_id, found[0]["id"], group_fields(group), kinds={"group"})
            except ThreadFull:
                updated = self.store.update_item(thread_id, found[0]["id"],
                                                 {k: v for k, v in group_fields(group).items() if k != "summary"},
                                                 kinds={"group"})
            thread = self.store.get(thread_id) or {}
            current = thread.get("lastGroupId") == group["id"]
            status = {"waiting": "waiting", "running": "running", "held": "running"}.get(group["state"], "idle")
            changes = {"status": status} if current and thread.get("status") != status else {}
            updated_thread = self.store.update_thread(thread_id, changes) if changes else None
        if updated is not None:
            self.published_item(thread_id, updated, "thread_item_updated")
        if updated_thread is not None:
            self.published_thread(updated_thread, "thread_updated")

    def offer_to_remember(self, thread, text, via, body):
        """"Remember that my gym is the one on 5th Street" is a note for Mobster, not a task for the phone."""
        try:
            from ..memory import extract
            from ..memory.listener import publish, thread_item
            from ..memory.store import default_store, normalize
        except ImportError:
            return None
        found = extract.remember_only(text)
        if not found:
            return None
        if body.get("profiles") is not None:
            raise APIError("Choose one saved profile for a thing to remember, or choose No profile to remember it "
                           "for every task.", 409, "memory_profile_ambiguous")
        account = self.account_for(thread, body)
        scope = "global"
        if account is not None:
            from ..accounts.routing import validate
            scope = f"profile:{validate(account, self.runtime)['id']}"
        store = default_store()
        if store is None:
            return None
        if any(secret_filter.is_secret(candidate.text) for candidate in found):
            raise APIError("Mobster doesn't remember passwords, codes or card numbers.", 400, "memory_secret")
        known = {normalize(fact.get("text") or "") for fact in store.all_facts() if fact.get("scope") == scope}
        if all(normalize(candidate.text) in known for candidate in found):
            raise APIError("Mobster already remembers this.", 409, "memory_duplicate")
        user = self.add_user(thread["id"], text, via, "remember", None, [])
        proposals = []
        for candidate in found:
            proposal = store.propose(candidate.text, scope, thread_id=thread["id"], pin=candidate.pin)
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
                     "proposals": [{"id": p["id"], "text": p["text"], "scope": p["scope"], "pin": p["pin"]}
                                   for p in proposals]}

    def add_other(self, thread_id, item):
        """Another track's item in a thread (a suggestion to remember), published as harness_api's sink does."""
        try:
            stored = self.store.append(thread_id, [item])[0]
        except Exception:  # noqa: BLE001 -- a thread that can't take the item never breaks the message
            log.exception("could not add a %s item", item.get("kind"))
            return None
        self.published_item(thread_id, stored)
        return stored

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
        and the listener's on_created both call it)."""
        extras = run.extras if isinstance(run.extras, dict) else {}
        if getattr(run, "group_id", None):
            return None, self.exchange_member(thread_id, run)
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
            if thread.get("group"):
                changes.update(group=None, lastGroupId=None)  # the newest task runs on one phone
            updated = self.store.update_thread(thread_id, changes)
        self.add_rules(thread_id, user["text"])
        for item in added:
            self.published_item(thread_id, item)
        if updated is not None:
            self.published_thread(self.store.get(thread_id) or updated, "thread_updated")
        return added[0], added[1]

    def exchange_member(self, thread_id, run):
        """One phone's run item in a task started on several phones (the user and group items are the group's)."""
        start_app = run.app.get("name") if isinstance(run.app, dict) and run.app.get("bundleId") else None
        item = {"kind": "run", "runId": run.id, "goal": run.goal, "appName": start_app, "engine": run.engine,
                "device": run.device_id, "deviceName": run.device_name, "groupId": run.group_id}
        with self.lock:
            self.runs[run.id] = thread_id
            try:
                added = self.store.append(thread_id, [item], unless=lambda found: any(
                    i.get("kind") == "run" and i.get("runId") == run.id for i in found))
            except ThreadFull:
                log.warning("thread %s is full; run %s isn't in it", thread_id, run.id)
                return None
            if added is None:
                return next(iter(self.store.find(thread_id, kind="run", run_id=run.id)), None)
            thread = self.store.get(thread_id) or {}
            updated = None
            if not thread.get("lastRunId") and thread.get("lastGroupId") == run.group_id:
                updated = self.store.update_thread(thread_id, {"lastRunId": run.id})
        self.published_item(thread_id, added[0])
        if updated is not None:
            self.published_thread(updated, "thread_updated")
        return added[0]

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
            self.add_steer(thread_id, run.id, event.get("id"), event.get("text") or "", ORIGIN_VIA.get(source, "cli"))
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
            if getattr(run, "group_id", None):
                self.group_changed(run)
        elif kind == "approval_resolved":
            if getattr(run, "group_id", None):
                self.group_changed(run)  # still waiting while another phone waits
            else:
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

    def group_changed(self, run):
        """A phone of a group asked or was answered: the group's item and the thread's state follow."""
        fanout = getattr(self.runtime, "fanout", None)
        if fanout is None:
            return
        try:
            group = fanout.get(run.group_id)
        except APIError:
            return
        self.on_group(fanout.public(group), "updated")

    def clarify_item(self, thread_id, run, event, pending=None):
        approval_id = event.get("approval_id")
        if pending is None:
            with run.condition:
                pending = dict(run.approval) if run.approval and run.approval.get("id") == approval_id else None
        question = (pending or {}).get("question") or event.get("label") or ""
        choices = ([{"id": c["id"], "label": c["label"], **({"detail": c["detail"]} if c.get("detail") else {})}
                    for c in (pending or {}).get("choices") or () if isinstance(c, dict)]
                   or [{"id": c, "label": c} for c in event.get("choices") or ()])
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
        context = context_of(summary, private, last_app)
        turn = sum(1 for i in self.store.items(thread_id) if i.get("kind") == "run"
                   and i.get("seq", 0) <= item.get("seq", 0))
        context["referents"] = [{**ref, "turn": turn, "task": run.goal[:120]} for ref in referents_of(summary)]
        changes = {"result": result, "_context": context}
        if not item.get("appName") and last_app:
            changes["appName"] = last_app
        try:
            updated = self.store.update_item(thread_id, item["id"], changes)
        except ThreadFull:
            updated = self.store.update_item(thread_id, item["id"], {"result": result})
        if updated is not None:
            self.published_item(thread_id, updated, "thread_item_updated")
        thread = self.store.get(thread_id)
        if getattr(run, "group_id", None):
            return  # the group's item and the thread's state follow the whole group (on_group)
        if thread is not None and thread.get("lastRunId") in (run.id, None):
            changes = {"status": "idle"}
            updated_thread = self.store.update_thread(thread_id, changes)
            if updated_thread is not None:
                self.published_thread(updated_thread, "thread_updated")

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
        found = self.store.find(thread_id, match=lambda i: i.get("id") == item_id)
        if not found or found[0].get("kind") in CORE_KINDS:
            return None
        kinds = {found[0]["kind"]}
        updated = self.store.update_item(thread_id, item_id, clean, kinds=kinds, max_bytes=MAX_SINK_ITEM_BYTES)
        if updated is not None:
            self.published_item(thread_id, updated, "thread_item_updated")
            return public_item(updated)
        return None


class Listener:
    """The run listener conversations registers (harness_api.register_run_listener)."""

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


def group_listener(runtime, group, event):
    """fanout.add_listener: the group's item in its conversation follows the group."""
    service = service_for(runtime)
    if service is not None:
        service.on_group(group, event)


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

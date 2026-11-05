"""Suggestions to remember something, from what a person types (SPEC §3.3 F3).

A run listener (harness_api.register_run_listener): it reads the goal of a task a person started (the Mac app, the
terminal UI, `mobster chat`) and the messages they send while it works, never text from another agent (MCP, the HTTP
API) or from a screen. Each suggestion goes to the conversation as a ``memory_proposal`` item when the task belongs
to one, and on the bus topic ``memory``. Nothing is remembered until the person accepts it.

It runs on the listeners' own thread, so the store's file never holds up a task.
"""

import logging
from collections.abc import Mapping

from .. import harness_api
from . import extract
from .model import SCOPE_ID, WORKFLOW_ID
from .store import default_store

log = logging.getLogger("mobster.memory")
ITEM_KIND = "memory_proposal"
MESSAGE_SOURCES = ("app", "tui", "cli", "voice")


def proposal_scope(run):
    """Only immutable, admitted identities may narrow an automatic suggestion."""
    account = getattr(run, "account_context", None)
    if account is not None and not isinstance(account, Mapping):
        return None
    profile = account.get("id") if account is not None else None
    workflow = getattr(run, "workflow_id", None)
    if account is not None and (not isinstance(profile, str) or not SCOPE_ID.fullmatch(profile)):
        return None
    if workflow is not None and (not isinstance(workflow, str) or not WORKFLOW_ID.fullmatch(workflow)):
        return None
    if account is None and (getattr(run, "extras", None) or {}).get("account") is not None:
        return None
    if profile and workflow:
        return f"profile:{profile}/workflow:{workflow}"
    if profile:
        return f"profile:{profile}"
    if workflow:
        return f"workflow:{workflow}"
    return "global"


def publish(event):
    """On the bus topic ``memory`` (never raises)."""
    try:
        from .. import eventbus
        eventbus.bus.publish("memory", event)
    except Exception:  # noqa: BLE001
        log.exception("memory: bus publish failed")


def thread_item(proposal):
    """The thread item that shows a suggestion (conversations' sink; chat-ui renders it through the registry)."""
    return {"kind": ITEM_KIND, "proposalId": proposal["id"], "text": proposal["text"], "scope": proposal["scope"],
            "app": proposal["app"], "pin": proposal["pin"], "status": proposal["status"], "runId": proposal["runId"],
            "expiresAt": proposal["expiresAt"]}


class ProposalListener:
    """on_created reads the goal; on_event reads ``user_message`` (a message sent while the task works)."""

    def __init__(self, store_factory=default_store):
        self.store_factory = store_factory

    def on_created(self, run, runtime):
        self.consider(run, getattr(run, "goal", None))

    def on_event(self, run, event):
        if event.get("event") == "user_message" and event.get("source") in MESSAGE_SOURCES:
            self.consider(run, event.get("text"))

    def consider(self, run, text):
        if getattr(run, "origin", "api") not in harness_api.INTERACTIVE:
            return []
        found = extract.candidates(text)
        if not found:
            return []
        scope = proposal_scope(run)
        if scope is None:
            return []
        store = self.store_factory()
        if store is None or not store.settings().get("suggest", True):
            return []
        thread_id = (getattr(run, "extras", None) or {}).get("threadId")
        made = []
        for candidate in found:
            proposal = store.propose(candidate.text, scope, thread_id=thread_id, run_id=getattr(run, "id", None),
                                     pin=candidate.pin)
            if proposal is None:
                continue
            made.append(proposal)
            if thread_id:
                item = harness_api.post_thread_item(thread_id, thread_item(proposal))
                if isinstance(item, dict) and item.get("id"):
                    store.set_proposal_item(proposal["id"], item["id"])
            publish({"event": "proposal_created", "proposal": proposal})
        return made

"""Track `queue`'s registrations (tracks.load calls ``register(api)`` once per process, staged)."""

import logging
import re

from ..service_support import ServiceRegistry

ID = re.compile(r"[a-f0-9]{12}")
log = logging.getLogger("mobster.queue")
# The runtimes this process serves, so a finished run finds the queue it names (one entry per runtime).
_runtimes = ServiceRegistry()


def validate_queue_draft(value, runtime):
    """``queueDraft`` is {queue, profile}: a queue of this Mac and one of its profiles, checked now so a typo never
    waits for the run's end."""
    from .store import QueueError, store_for
    if not isinstance(value, dict) or set(value) != {"queue", "profile"} or not all(
            isinstance(value[key], str) and ID.fullmatch(value[key]) for key in ("queue", "profile")):
        raise ValueError("queueDraft is {queue, profile}: a queue's id and a profile's id")
    try:
        store_for(runtime).get(value["queue"])
    except QueueError:
        raise ValueError("No queue has that id. Make it in Queue first.") from None
    from ..accounts.store import store_for as profiles_for
    if value["profile"] not in {p["id"] for p in profiles_for(runtime).all()}:
        raise ValueError("No profile has that id. Add it in Settings › Profiles.")
    return {"queue": value["queue"], "profile": value["profile"]}


class DraftListener:
    """A run that names ``queueDraft`` leaves its text result as a draft post in that queue for that profile (no
    media: it says "Add photos" and never goes out as it is). A run that failed or wrote no result leaves nothing."""

    def on_finished(self, run, summary, private):
        field = (getattr(run, "extras", None) or {}).get("queueDraft")
        if not field:
            return
        from ..outlets import webhook
        from .store import CAPTION_CHARS, QueueError, store_for
        status = getattr(run, "status", None) or (summary or {}).get("status")
        text = webhook.answer_of(summary) if status in webhook.DONE and isinstance(summary, dict) else None
        if not text:
            return
        for runtime in _runtimes.all():
            if (getattr(runtime, "runs", None) or {}).get(run.id) is run:
                break
        else:
            return
        try:
            from ..accounts.store import store_for as profiles_for
            store_for(runtime).add_draft(field["queue"], text[:CAPTION_CHARS], field["profile"],
                                         profiles_for(runtime).all())
        except (QueueError, OSError):
            log.exception("queue: a run's draft was not added")


def validate_queue_item(value, runtime):
    if not isinstance(value, dict) or set(value) != {"queue", "item"} or not all(
            isinstance(value[key], str) and ID.fullmatch(value[key]) for key in ("queue", "item")):
        raise ValueError("queueItem is {queue, item}: a queued post's ids")
    return {"queue": value["queue"], "item": value["item"]}


def start(runtime):
    from .runner import QueueRunner
    _runtimes.add(runtime, runtime)
    if getattr(runtime, "fleet", None) is None and getattr(runtime, "pool", None) is None:
        return  # a runtime with no phones (tests, a demo) runs no queue
    runner = QueueRunner(runtime)
    runtime.queue_runner = runner
    runner.start()  # a thread only while a queue is started


def close(runtime):
    _runtimes.pop(runtime)
    runner = getattr(runtime, "queue_runner", None)
    if runner is not None:
        runner.close()


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from .. import api_routes
    from .routes import ROUTES
    api.register_run_field("queueItem", validate_queue_item, public=True)
    api.register_run_field("queueDraft", validate_queue_draft, public=True)
    api.register_run_listener(DraftListener())
    api.register_service(start, close)
    for route in ROUTES:
        api_routes.register(route)

"""The queue at work: due posts run on their profiles' phones, one phone per worker, phones in parallel."""

import logging
import re
import threading
import time
from pathlib import Path

from .. import phone_ready
from ..api_errors import APIError
from . import client, prompt
from .media_check import POST_ACTS
from .store import (
    ADD_PHOTOS,
    LATE_LIMIT_MS,
    PACED_REFRESH_MS,
    STAGGER_S,
    QueueError,
    armed_for,
    caption_text,
    fingerprint,
    now_ms,
    store_for,
)

log = logging.getLogger("mobster.queue")

TICK_S = 3.0
WATCH_SETTLE_S = 10.0
RUN_POLL_S = 1.0
LINKS = {
    "com.burbn.instagram": re.compile(
        r"^https://(?:www\.)?instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv|stories/[\w.]+)/"
                                      r"[\w-]+/?(?:\?\S*)?$"),
    "com.zhiliaoapp.musically": re.compile(r"^https://(?:www\.|vm\.|vt\.)?tiktok\.com/\S+$"),
}
ANY_LINK = re.compile(r"^https://\S{8,300}$")


def preapproval(item, profile):
    """The run's one batch-approved Post (Run.preapproved): this profile, this app, a post, once."""
    return {"kind": "queue_post", "via": "content_queue", "acting_as": f"@{profile['handle']} "
            f"on {profile['app']['name']}",
            "handle": profile["handle"], "app": profile["app"]["name"], "item": item["id"],
            "source": {"queue": True, "item": item["id"]},
            # With the queue checks on, the Post card is answered only after the Mac saw the post's media in the
            # composer (media_check.py); otherwise it waits for the person.
            "preview": phone_ready.enabled()}


def preapproval_matches(spec, request):
    """Whether ``request`` is the one Post ``spec`` covers (with the queue checks on, its media seen on the Mac);
    anything else asks as usual (fail closed)."""
    if not isinstance(spec, dict) or spec.get("kind") != "queue_post" or spec.get("used"):
        return False
    if not isinstance(request, dict) or (request.get("kind") or "action") != "commit":
        return False
    if request.get("act") not in POST_ACTS or request.get("operation") not in ("TAP", "SUBMIT", "TYPE_SUBMIT"):
        return False
    if spec.get("preview") and request.get("media_checked") is not True:
        return False
    acting = " ".join(str(request.get("acting_as") or "").split()).casefold()
    if not acting or acting != " ".join(str(spec.get("acting_as") or "").split()).casefold():
        return False
    return " ".join(str(request.get("app_name") or "").split()).casefold() == str(spec.get("app") or "").casefold()


def outcome(run):
    """(state, note) for a finished queue run, from its approval events and its result."""
    asked, decided = {}, {}
    for event in list(getattr(run, "events", ()) or ()):
        if event.get("event") == "approval_requested":
            asked[event.get("approval_id")] = event
        elif event.get("event") == "approval_resolved":
            decided[event.get("approval_id")] = event.get("decision")
    posts = [key for key, event in asked.items() if (event.get("kind") or "action") == "commit"
             and event.get("act") in POST_ACTS]
    approved = [key for key in posts if decided.get(key) == "approved"]
    summary = getattr(run, "summary", None) or {}
    data = summary.get("data") if isinstance(summary.get("data"), dict) else {}
    note = (data.get("note") or summary.get("reason") or summary.get("answer") or "")[:300] or None
    if approved:
        if summary.get("status") == "completed" and data.get("posted") is True and data.get("challenge") is not True:
            return "posted", None
        return "unknown", ("The Post was approved, and the task couldn't confirm it went out. Look at the profile, "
                           "then mark it.")
    if posts:
        return "declined", "The Post wasn't approved, so nothing went out."
    if data.get("challenge") is True:
        return "failed", "The app showed a check (a captcha or a verification), so Mobster stopped and paused this " \
                         "profile. Nothing went out."
    return "failed", note or "The task stopped before it reached Post. Nothing went out."


class QueueRunner:
    """One per runtime: the scheduler thread and the per-phone workers."""

    def __init__(self, runtime, *, transfer_run=None, link_reader=None, sleep=time.sleep, clock=time.time):
        from ..phone_io.transfer import transfers_for
        self.runtime = runtime
        self.store = store_for(runtime)
        self.transfers = transfers_for(runtime)
        self._transfer_run = transfer_run
        self._link_reader = link_reader or read_link
        self.sleep, self.clock = sleep, clock
        self._stop = threading.Event()
        self._workers = {}
        self._lock = threading.Lock()
        self._thread = None
        self.store.recover()

    def start(self):
        """The scheduler thread, only while some queue is started: none at all for a person who never uses the queue."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive() or self._stop.is_set():
                return
            if not any(queue.get("armed") for queue in self.store.all()):
                return
            self._thread = threading.Thread(target=self._loop, name="mobster-queue", daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - one bad tick never stops the queue
                log.exception("queue tick failed")
            if not any(queue.get("armed") for queue in self.store.all()) and not any(
                    worker.is_alive() for worker in self._workers.values()):
                with self._lock:
                    self._thread = None
                return  # every queue stopped: the thread ends, and the next Start begins it again

    def tick(self):
        """Start a worker for each phone with a due item and no worker. Returns the phones started."""
        from ..accounts.store import store_for as profiles_for
        profiles = {p["id"]: p for p in profiles_for(self.runtime).all()}
        due = {}
        stagger = {}
        stamp = now_ms()
        pacing = self._pacing()
        for queue in self.store.all():
            if not queue.get("armed"):
                continue
            if queue.get("watch"):
                self.take_new_files(queue, profiles)
                queue = self.store.get(queue["id"])
            for item in queue["items"]:
                if item["state"] != "ready" or not armed_for(queue, item):
                    continue  # a draft waits for its photos; a profile whose posts weren't started keeps its own
                if not item.get("media"):
                    self.store.set_state(queue["id"], item["id"], "draft", note=ADD_PHOTOS)
                    continue
                profile = profiles.get(item.get("profile"))
                if not self._due(queue["id"], item, profile, pacing, stamp):
                    continue
                phone = profile.get("device") or "default"
                due.setdefault(phone, []).append((queue["id"], item["id"]))
                stagger.setdefault(phone, queue.get("staggerSeconds", STAGGER_S))
        started = []
        with self._lock:
            for phone, items in due.items():
                worker = self._workers.get(phone)
                if worker is not None and worker.is_alive():
                    continue
                # Phones starting in the same tick start apart (the queue's stagger), never at the same second.
                delay = len(started) * float(stagger.get(phone) or 0)
                worker = threading.Thread(target=self._work, args=(items, profiles, delay),
                                          name=f"mobster-queue-{phone}", daemon=True)
                self._workers[phone] = worker
                worker.start()
                started.append(phone)
        return started

    def _due(self, queue_id, item, profile, pacing, stamp):
        """Check timing and holds both when selecting work and when its worker reaches dispatch."""
        when = item.get("scheduledAt")
        if when is not None and when > stamp:
            return False
        note, hold = self._paced(pacing, profile, item) if pacing is not None and profile else (None, None)
        # Late because Mobster held it for its profile's pace or posting window (the person's own settings) is
        # no miss while its day lasts. Late while Mobster wasn't running, while its profile was paused or
        # while it waited for the client is, and its note says which.
        held = item.get("pacedAt") is not None and stamp - item["pacedAt"] <= LATE_LIMIT_MS
        if when is not None and stamp - when > LATE_LIMIT_MS and not held:
            if client.holds(item):
                missed = "Its time passed while it waited for your client. Set a new time."
            elif hold == "paused":
                missed = (f"Your post misses its time because of a pause on @{profile['handle']}. Set a new "
                          "time or run it.")
            else:
                missed = "Its time passed while Mobster wasn't running. Set a new time or run it."
            self.store.set_state(queue_id, item["id"], "missed", note=missed)
            return False
        if client.holds(item):
            return False  # the row shows what the client said (client.public)
        if profile is None:
            self.store.set_state(queue_id, item["id"], "failed",
                                 note="Choose a profile for this post (Settings › Profiles).")
            return False
        if hold == "day":
            self.store.set_state(queue_id, item["id"], "missed", note=note)
            return False
        if note is not None:
            paused = hold == "paused"
            # Each pace hold says so again now and then: the proof Mobster held it, so it isn't a miss.
            if note != item.get("note"):
                self.store.set_state(queue_id, item["id"], "ready", note=note,
                                     **({} if paused else {"pacedAt": stamp}))
            elif not paused and stamp - (item.get("pacedAt") or 0) > PACED_REFRESH_MS:
                # Only the stamp is due: no journal line, and never on a post that has moved on since the read.
                self.store.change_item(queue_id, item["id"], lambda held_item, at=stamp: held_item.update(
                    pacedAt=at) if held_item["state"] == "ready" else None)
            return False
        return True

    def wait(self, timeout=10):
        """Tests: until every worker ended."""
        end = time.monotonic() + timeout
        for worker in list(self._workers.values()):
            worker.join(max(0, end - time.monotonic()))

    def _pacing(self):
        try:
            from ..accounts.pacing import pacing_for
            return pacing_for(self.runtime)
        except Exception:  # noqa: BLE001 - no profiles folder: nothing to pace by
            return None

    @staticmethod
    def _paced(pacing, profile, item):
        """What the profile's pacing says of a due post: (None, None) when it may go now; else its note and why:
        "paused", "pace" (it waits for its pace or posting window) or "day" (that wait runs past its day: a miss)."""
        settings = pacing.settings(profile["id"])
        if settings.get("paused"):
            return (f"@{profile['handle']} is paused: {settings['paused']['reason']} Resume it "
                    "here once it's dealt with."), "paused"
        when = item.get("scheduledAt")
        at = pacing.next_post_time(profile["id"], (when or 0) / 1000 or None)
        if at is None:
            return None, None
        waits = at > pacing.clock() + 1
        # A timed post waits for its pace or window only within its own day, in the profile's time zone: Monday
        # evening's post never goes out on Tuesday unasked. That counts once the pace held it, too, if its day ended
        # before it could go. Run again drops the time (store.mark): the person's OK to post it late.
        if (when is not None and (waits or (item.get("pacedAt") or 0) >= when)
                and not pacing.same_day(profile["id"], when / 1000, at)):
            return (f"@{profile['handle']}'s pace or posting window would post it after its day. Set a new time or "
                    "run it."), "day"
        if not waits:
            return None, None
        import datetime
        local = datetime.datetime.fromtimestamp(at).astimezone()
        return f"Waits for @{profile['handle']}'s pace: next post at {local.strftime('%a %H:%M')}.", "pace"

    def take_new_files(self, queue, profiles):
        """New photos and videos in the queue's watched folder join it, one post each, for the folder's profile."""
        from .importing import MEDIA_SUFFIXES, body_for
        watch = queue["watch"]
        folder = Path(watch["folder"])
        seen = set(watch.get("seen") or ())
        try:
            files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES
                           and not p.name.startswith(".") and str(p) not in seen)
        except OSError:
            return []
        settled = [p for p in files if time.time() - p.stat().st_mtime >= WATCH_SETTLE_S]
        if not settled:
            return []
        bodies = [body_for(file, watch.get("profile")) for file in settled]
        try:
            added = self.store.add_items(queue["id"], bodies, list(profiles.values()))
        except QueueError as error:
            log.warning("watched folder: %s", error)
            added = []
        self.store.note_seen(queue["id"], [str(p) for p in settled])
        if added:
            self.store.journal(queue["id"], "folder_added", items=[i["id"] for i in added])
        return added

    def _work(self, items, profiles, delay=0.0):
        if delay and self._stop.wait(delay):
            return
        for queue_id, item_id in items:
            if self._stop.is_set():
                return
            try:
                queue = self.store.get(queue_id)
            except QueueError:
                continue
            item = next((i for i in queue["items"] if i["id"] == item_id), None)
            if (
                item is None
                or not armed_for(queue, item)
                or item["state"] != "ready"
                or not item.get("media")
                or client.holds(item)
            ):
                continue  # read again now: a revoke or an edit since the tick holds the post back
            if self.held(queue_id, item, profiles[item["profile"]].get("device")):
                break  # the phone is locked or out of reach: its other posts wait too, and the next tick looks again
            try:
                # A stagger or an earlier post can carry this item past its day or into a new pace hold.
                if not self._due(queue_id, item, profiles[item["profile"]], self._pacing(), now_ms()):
                    continue
                self.post(queue, item, profiles[item["profile"]])
            except Exception as error:  # noqa: BLE001 - one item never stops the phone's others
                log.exception("queue item failed")
                current = self._state(queue_id, item_id)
                if current in ("sending",):
                    self.store.set_state(queue_id, item_id, "failed",
                                         note=f"Mobster hit an error ({type(error).__name__}).")
                elif current == "posting":
                    self.store.set_state(queue_id, item_id, "unknown", note=f"Mobster hit an error "
                                         f"({type(error).__name__}) while it posted. Look at "
                                         "the profile, then mark it.")

    def _phone(self, device):
        """(the helper's address, the phone's name) for the profile's ``device``; APIError for no phone here."""
        phone = self.runtime.resolve_device(device)
        return phone.wda_url, phone.name or phone.id

    def held(self, queue_id, item, device):
        """True while the phone is locked or its lock can't be read (the check is off: never). The item stays ``ready``
        with the reason as its note, written once, and nothing is sent. Locked and unknown both hold: a phone Mobster
        can't read is never treated as unlocked."""
        if not phone_ready.enabled():
            return False
        try:
            why = phone_ready.hold(self._phone(device)[0])
        except APIError:
            why = phone_ready.HOLD_UNREADABLE
        if why is not None and item.get("note") != why:
            self.store.set_state(queue_id, item["id"], "ready", note=why)
        return why is not None

    def preflight(self, queue):
        """Before a queue starts, for each phone its waiting posts use: QueueError when Photos access is off or
        unanswered there (no file would ever arrive), else the Low Power Mode warnings, [{code, device, message}]."""
        from ..accounts.store import store_for as profiles_for
        profiles = {p["id"]: p for p in profiles_for(self.runtime).all()}
        wanted = [item.get("profile") for item in queue["items"] if item["state"] == "ready"]
        wanted.append((queue.get("watch") or {}).get("profile"))
        warnings = []
        for device in dict.fromkeys(profiles[w].get("device") for w in wanted if w in profiles):
            try:
                url, name = self._phone(device)
            except APIError as error:
                raise QueueError(str(error), "phone_unreachable", 409) from None
            refused = phone_ready.photos_refusal(url, name)
            if refused is not None:
                raise QueueError(str(refused), refused.code, 409)
            warning = phone_ready.low_power_warning(url, name, "the first post")
            if warning:
                warnings.append({"code": "low_power_mode", "device": name, "message": warning})
        return warnings

    def _state(self, queue_id, item_id):
        try:
            return next((i["state"] for i in self.store.get(queue_id)["items"] if i["id"] == item_id), None)
        except QueueError:
            return None

    def post(self, queue, item, profile):
        queue_id, item_id = queue["id"], item["id"]
        device = profile.get("device")
        sent = self.send_media(queue_id, item, device)
        if sent is None:
            return
        from .. import permissions
        from ..api_errors import APIError
        saved = permissions.from_env().mode
        mode = saved if saved in (permissions.ASK_ALL, permissions.ASK_IMPORTANT) else permissions.ASK_IMPORTANT
        self.store.set_state(queue_id, item_id, "posting", transfer=sent, note=None)
        # queueItem: the run's permission gate (runtime_limits.py) skips every "Don't ask again" rule for it.
        try:
            run = self.runtime.create(None, prompt.goal(item, profile), "live", engine="smart",
                                      extras={"account": profile["id"], "permissionMode": mode,
                                              "queueItem": {"queue": queue_id, "item": item_id}},
                                      origin="app", output_schema=prompt.ANSWER, output_format="json")
        except APIError as error:
            if error.code == "run_active":
                # The phone is busy with another task: nothing started. The next tick tries again.
                self.store.set_state(queue_id, item_id, "ready", transfer=sent,
                                     note="Waiting for the phone: a task is running on it.")
                return
            self.store.set_state(queue_id, item_id, "failed", transfer=sent, note=str(error))
            return
        except ValueError as error:
            self.store.set_state(queue_id, item_id, "failed", transfer=sent, note=str(error))
            return
        batch = queue.get("batch") or {}
        if queue.get("approval") == "batch" and (batch.get("items") or {}).get(item_id) == fingerprint(item):
            with run.condition:
                run.preapproved.append(preapproval(item, profile))
        self.store.set_state(queue_id, item_id, "posting", runId=run.id, transfer=sent)
        while run.finished_at is None and not self._stop.is_set():
            with run.condition:
                run.condition.wait(RUN_POLL_S)
        if run.finished_at is None:
            return  # Mobster is closing: the run ends as interrupted; recover() makes the item unknown next start
        state, note = outcome(run)
        result = {"runId": run.id, "finishedAt": now_ms(), "caption": caption_text(item)[:200]}
        if state == "posted":
            link = None
            data = (run.summary or {}).get("data") if isinstance((run.summary or {}).get("data"), dict) else {}
            if data.get("link_copied") is not False:
                link = self._link_reader(self.runtime, device, profile["app"]["bundleId"])
            result.update(link=link, postedAt=now_ms())
            if not link:
                note = "Posted. Mobster couldn't read the post's link."
        self.store.set_state(queue_id, item_id, state, runId=run.id, result=result, note=note)

    def send_media(self, queue_id, item, device):
        """The transfer's public record when every file arrived; None (and the item failed) otherwise."""
        from ..phone_io import PhoneIOError
        from ..phone_io.transfer import Transfer, phone_from_runtime
        done = item.get("transfer") or {}
        if (done.get("state") == "done" and done.get("device") == device
                and [i.get("sha256") for i in done.get("items") or ()] == [m["sha256"] for m in item["media"]]):
            return done  # sent before (a declined or failed post run again): the same files are on the phone
        if phone_ready.enabled():
            try:
                url, name = self._phone(device)
                refused = phone_ready.photos_refusal(url, name)
            except APIError as error:
                refused = PhoneIOError(str(error), "no_device")
            if refused is not None:
                self.store.set_state(queue_id, item["id"], "failed", note=f"{refused} Nothing was posted.")
                return None
        self.store.set_state(queue_id, item["id"], "sending", note=None)
        try:
            phone = phone_from_runtime(self.runtime, device)
            transfer = Transfer("to_phone", "photos", phone, paths=[m["path"] for m in item["media"]],
                                album="Mobster", date_now=True, queue_item=item["id"])
            if self._transfer_run is None:
                self.transfers.start(transfer, wait=True)
            else:
                self._transfer_run(transfer)
        except PhoneIOError as error:
            self.store.set_state(queue_id, item["id"], "failed", note=str(error))
            return None
        except Exception as error:  # noqa: BLE001 - a phone that can't be named, a transfer that broke
            self.store.set_state(queue_id, item["id"], "failed", note=str(error)[:300] or type(error).__name__)
            return None
        public = transfer.public()
        public = {key: public[key] for key in ("id", "state", "route", "device", "before", "after", "verified")} | {
            "items": [{"name": i["name"], "state": i["state"], "sha256": i["sha256"]} for i in public["items"]]}
        if transfer.state != "done":
            reason = (transfer.error or {}).get("message") or next(
                (i.error for i in transfer.items if i.error), "The media didn't all arrive.")
            self.store.set_state(queue_id, item["id"], "failed", transfer=public,
                                 note=f"{reason} Nothing was posted.")
            return None
        return public


def read_link(runtime, device, bundle):
    """The post's link from the phone's clipboard, when it is one for this app's posts; else None."""
    from ..phone_io import PhoneIOError
    from ..phone_io.clipboard import get_text
    from ..phone_io.transfer import phone_from_runtime
    try:
        phone = phone_from_runtime(runtime, device)
        if phone.runner is None:
            return None
        with phone.runner() as request:
            text = (get_text(request) or "").strip()
    except (PhoneIOError, OSError, ValueError):
        return None
    pattern = LINKS.get(bundle, ANY_LINK)
    return text if pattern.match(text) else None

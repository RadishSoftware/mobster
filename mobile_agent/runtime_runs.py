"""Runtime's actions on a finished run: stop, delete, export, share, review and block decisions."""

import base64
import hashlib
import re
import threading
import time
from pathlib import Path

from . import harness_api, ownership, result_blocks, traces
from .api_errors import APIError
from .downloads import save_export_bytes
from .journal import JournalError

# One render at a time: a GIF takes a second or two of CPU, which a running task shares.
GIF_RENDERS = threading.BoundedSemaphore(1)

# One at a time: each is a WebKit page off screen.
ARTIFACT_RENDERS = threading.BoundedSemaphore(1)

# A result that stays on this Mac (Network X-ray's report) is never shared, sent or read but by the Mac app.
STAYS_ON_MAC = "This result stays on this Mac, so Mobster doesn't share it. Export it as a file to keep a copy."


class RunActionsMixin:
    def stop_run(self, identifier):
        announce = None
        with self.lock:
            run = self.runs.get(identifier)
            if not run:
                raise APIError("Task not found", 404, "not_found")
            item = self.unqueue(identifier) if identifier in self.queued else None
            with run.condition:
                if run.finished_at is None and not run.stop.is_set():
                    run.stop.set()
                    run.emit({"event": "stop_requested"})
            if item is not None:
                # It never had the phone: it leaves the line and ends, doing nothing.
                self.end_queued(run, "stopped", ownership.QUEUE_CANCELLED.format(phone=self.phone_name(item["phone"])))
                announce = run
        if announce is not None:
            self.finished(announce)
            self.publish("devices", {"event": "queue_changed", "device": announce.device_id})
        return run

    def delete_run(self, identifier):
        with self.lock:
            run = self.runs.get(identifier)
            if not run:
                raise APIError("Task not found", 404, "not_found")
            if run.finished_at is None:
                raise APIError("Stop this task before deleting it", 409, "run_active")
            self.journal.delete(identifier)
            self.runs.pop(identifier, None)
        if getattr(self, "frames", None) is not None:
            self.frames.delete(identifier)
        self.publish("runs", {"event": "run_deleted", "runId": identifier})

    def save_run_gif(self, identifier, theme="paper", redact=False, mp4=False, directory=None):
        from . import run_gif
        run = self._finished_run(identifier, "This task is still running. Save it as a GIF once it ends.")
        frames = getattr(self, "frames", None)

        def read(frame_id):
            return frames.get(run.id, frame_id) if frames is not None else None

        if not GIF_RENDERS.acquire(blocking=False):
            raise APIError("Mobster is saving another GIF. Try again in a moment.", 429, "export_busy")
        try:
            public = run.public()
            try:
                clip = run_gif.build_clip(public, read, theme=theme, redact=redact)
            except ValueError as error:
                if str(error) == run_gif.NO_SCREENS:
                    raise APIError(str(error), 404, "no_frames") from None
                raise APIError(str(error), 422, "gif_too_long") from None
            stem = run_gif.file_stem(public.get("goal"))
            saved = save_export_bytes(stem, "gif", clip.gif, directory)
            result = {**saved, "frames": clip.frames, "bytes": len(clip.gif), "seconds": round(clip.seconds, 1),
                      "mp4": None}
            if mp4:
                data = run_gif.build_mp4(public, read, theme=theme, redact=redact)
                if data is not None:
                    # The GIF's own name, so the two sit together ("… (1).gif" and "… (1).mp4").
                    video = save_export_bytes(Path(saved["name"]).stem, "mp4", data, directory)
                    result["mp4"] = {**video, "bytes": len(data)}
            return result
        finally:
            GIF_RENDERS.release()

    def add_to_evals(self, identifier):
        """A finished task into the local eval set, beside the journal (traces.add_to_evals): {id, path, added,
        expected, recorded}. APIError 503 traces_off without MOBSTER_EXPERIMENTAL_TRACES, 409 run_active while it
        runs, 422 no_tape or unreadable when it can't be replayed."""
        run = self._finished_run(identifier, "This task is still running. Add it to your evals once it ends.")
        state_db = getattr(self.config, "state_db", None)
        if not traces.enabled():
            raise APIError("Traces are off. Set MOBSTER_EXPERIMENTAL_TRACES=1 to keep tasks as evals.", 503,
                           "traces_off")
        frames = getattr(self, "frames", None)
        try:
            return traces.add_to_evals({**run.public(), "app": run.app}, frames.folder(run.id) if frames else None,
                                       Path(state_db).parent / "evals" if state_db else None)
        except traces.EvalError as error:
            raise APIError(str(error), 409 if error.code == "running" else 422, error.code) from None

    def save_artifact(self, identifier, kind, blocks=None, directory=None):
        from . import artifacts
        if kind not in ("pdf", "png", "md"):
            raise ValueError("format is pdf, png or md")
        if blocks is not None and (not isinstance(blocks, list) or not 1 <= len(blocks) <= result_blocks.MAX_BLOCKS
                                   or not all(isinstance(b, str) for b in blocks)):
            raise ValueError("blocks is a list of 1 to 8 block ids")
        if kind == "png" and (blocks is None or len(blocks) != 1):
            raise ValueError("An image is one block: name it in blocks")
        run = self._finished_run(identifier, "This task is still running. Save it once it ends.")
        public = run.public(include_events=False)
        envelope = (public.get("summary") or {}).get("blocks")
        known = {b.get("id"): b for b in (envelope or {}).get("blocks") or ()} if isinstance(envelope, dict) else {}
        if blocks is not None and any(b not in known for b in blocks):
            raise APIError("That card isn't in this task's results anymore.", 404, "block_not_found")
        frames = getattr(self, "frames", None)

        def read(frame_id):
            return self.frame(run.id, frame_id) if frames is not None else None

        first = known[blocks[0]] if blocks else None
        stem = (first.get("title") if first and first.get("title") else run.goal or "Mobster result")
        stem = " ".join(str(stem).split())[:80]
        if kind == "md":
            data = artifacts.markdown(public, blocks=blocks).encode("utf-8")
        else:
            if not ARTIFACT_RENDERS.acquire(blocking=False):
                raise APIError("Mobster is making another file. Try again in a moment.", 429, "export_busy")
            try:
                page = artifacts.document(public, blocks=blocks, frames=read, card=kind == "png",
                                          report=kind == "pdf", draw_map=self._map_drawer())
                data = artifacts.render(page, kind, title=stem)
            except artifacts.RenderError as error:
                raise APIError(str(error), 503, "artifact_unavailable") from None
            finally:
                ARTIFACT_RENDERS.release()
        saved = save_export_bytes(stem, kind, data, directory)
        return {**saved, "bytes": len(data)}

    @staticmethod
    def _map_drawer():
        from . import maps
        return lambda block: maps.snapshot(maps.places_of(block), near=block.get("near"), width=720, height=340)

    def block_map(self, identifier, block_id, dark=False, width=720, height=360):
        from . import maps
        run = self._finished_run(identifier)
        envelope = ((run.public(include_events=False).get("summary") or {}).get("blocks"))
        block, _ = result_blocks.find(envelope, block_id)
        places = maps.places_of(block)
        if not places:
            raise APIError("That card has no places to show on a map.", 404, "block_not_found")
        try:
            drawn = maps.snapshot(places, near=(block or {}).get("near"), dark=dark, width=width, height=height)
        except maps.MapError as error:
            raise APIError(str(error), 503, "map_unavailable") from None
        return {**{k: v for k, v in drawn.items() if k != "image"},
                "image": "data:image/png;base64," + base64.b64encode(drawn["image"]).decode()}

    def _shareable(self, identifier, people, screens, title):
        from . import share
        if people not in ("mask", "show") or type(screens) is not bool:
            raise ValueError("people is mask or show, and screens is true or false")
        if title is not None and (not isinstance(title, str) or not 1 <= len(title.strip()) <= 120):
            raise ValueError("title is 1 to 120 characters")
        run = self._finished_run(identifier, "This task is still running. Share it once it ends.")
        if harness_api.stays_on_mac(run.extras):
            raise APIError(STAYS_ON_MAC, 403, "stays_on_mac")
        frames = getattr(self, "frames", None)
        public = run.public(include_events=False)
        html = share.page(public, frames=(lambda f: self.frame(run.id, f)) if frames is not None else None,
                          people=people, screens=screens, title=title)
        if len(html.encode("utf-8")) > share.MAX_PAGE_BYTES:
            raise APIError("That page is over 4 MB. Leave out the screens.", 413, "too_large")
        return run, public, html

    def share_preview(self, identifier, people="mask", screens=False, title=None):
        from . import artifacts
        run, _public, html = self._shareable(identifier, people, screens, title)
        if not ARTIFACT_RENDERS.acquire(blocking=False):
            raise APIError("Mobster is making another file. Try again in a moment.", 429, "export_busy")
        try:
            image = artifacts.render(html, "png", width=640)
        except artifacts.RenderError as error:
            raise APIError(str(error), 503, "artifact_unavailable") from None
        finally:
            ARTIFACT_RENDERS.release()
        return {"image": "data:image/png;base64," + base64.b64encode(image).decode(),
                "bytes": len(html.encode("utf-8"))}

    def share_run(self, identifier, access_token, people="mask", screens=False, title=None, expires_days=30):
        # The account's access token is used for this request only.
        from . import share
        if not isinstance(access_token, str) or not 16 <= len(access_token) <= 8192:
            raise APIError("Sign in to Mobster to share.", 401, "session")
        if expires_days is not None and expires_days not in share.EXPIRY_DAYS:
            raise ValueError("expiresDays is 1, 7, 30, 90 or null")
        run, public, html = self._shareable(identifier, people, screens, title)
        heading = " ".join(str(title or public.get("goal") or "Mobster result").split())[:120]
        try:
            made = share.create(access_token, title=heading, html=html, expires_days=expires_days)
        except share.ShareError as error:
            raise APIError(str(error), error.status, error.code) from None
        finally:
            access_token = None
        note = {"id": made["id"], "url": made["url"], "title": made.get("title") or heading,
                "createdAt": made.get("createdAt"), "expiresAt": made.get("expiresAt"), "revokedAt": None,
                "people": people, "screens": screens}
        self._note_share(run, lambda shares: [note, *shares][:50])
        return {"share": note}

    def unshare_run(self, identifier, share_id, access_token):
        from . import share
        if not isinstance(share_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{22}", share_id):
            raise APIError("That link isn't one of this task's.", 404, "not_found")
        if not isinstance(access_token, str) or not 16 <= len(access_token) <= 8192:
            raise APIError("Sign in to Mobster to turn the link off.", 401, "session")
        with self.lock:
            run = self.runs.get(identifier)
        if not run or not any(item.get("id") == share_id for item in run.shares):
            raise APIError("That link isn't one of this task's.", 404, "not_found")
        try:
            gone = share.revoke(access_token, share_id)
        except share.ShareError as error:
            raise APIError(str(error), error.status, error.code) from None
        finally:
            access_token = None
        revoked = gone.get("revokedAt") or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        self._note_share(run, lambda shares: [{**item, "revokedAt": item.get("revokedAt") or revoked}
                                              if item.get("id") == share_id else item for item in shares])
        return {"share": next(item for item in run.shares if item.get("id") == share_id)}

    def _note_share(self, run, change):
        with run.condition:
            updated = change([dict(item) for item in run.shares])
            if run.journal:
                try:
                    run.journal.annotate(run.id, {"shares": updated})
                except JournalError:
                    pass  # the link exists either way; only this Mac's note of it is lost
            run.shares = updated
        self.publish("runs", {"event": "run_shared", "runId": run.id})

    def review_run(self, identifier, ok):
        run = self._finished_run(identifier, "This task is still running")
        with run.condition:
            if run.journal:
                run.journal.annotate(run.id, {"reviewedOk": ok})
            run.reviewed_ok = ok
        self.publish("runs", {"event": "run_reviewed", "runId": run.id, "reviewedOk": ok})
        return run

    def decide_block_item(self, identifier, block_id, item_id, decision, text=None):
        if decision not in ("approve", "skip", "reopen"):
            raise ValueError("decision is approve, skip or reopen")
        run = self._finished_run(identifier, "This task is still running")
        blocks = (run.summary or {}).get("blocks")
        block, item = result_blocks.find(blocks, block_id, item_id)
        if block is None:
            raise APIError("That item isn't in this task's results anymore.", 404, "block_item_not_found")
        source = self._brief_source(run, block_id)
        if source is not None:
            # A brief's card is its task's: the item is decided once wherever it shows, and _block_state mirrors it.
            answer = self.decide_block_item(source[0], source[1], item_id, decision, text)
            with run.condition:
                return {**answer, "blockState": dict(run.block_state)}
        key = f"{block_id}/{item_id}"
        with run.condition:
            current = dict(run.block_state.get(key) or {})
        if current.get("state") == "approved":
            if decision == "approve" and current.get("runId"):
                return {"state": "approved", "runId": current["runId"], "replayed": True,
                        "blockState": dict(run.block_state)}
            raise APIError("You already approved this one, so it can't change now.", 409, "block_item_approved",
                           runId=current.get("runId"))
        if decision in ("skip", "reopen"):
            if decision == "reopen" and current.get("state") != "skipped":
                raise APIError("Only a skipped item can go back in the queue.", 409, "block_item_not_skipped")
            state = {"state": "skipped", "at": time.time() * 1000} if decision == "skip" else None
            return self._block_state(run, key, state)
        if block.get("type") != "review_queue":
            raise APIError("Only a draft in a review queue can be approved.", 409, "block_item_not_approvable")
        try:
            block, item, words = result_blocks.approval_item(blocks, block_id, item_id, text)
        except result_blocks.BlockError as problem:
            raise APIError(str(problem), 400, "block_item_invalid") from None
        goal = result_blocks.follow_up_goal(block, item, words)
        app_id = next((app["id"] for app in self.apps_for(None) if item.get("bundleId")
                       and app.get("bundleId") == item["bundleId"]), None)
        # One task per item, ever: a retried Approve (a lost response, a double click) gets the task it started.
        request_key = f"rb-{run.id}-{hashlib.sha256(key.encode()).hexdigest()[:16]}"
        thread_id = (run.extras or {}).get("threadId") if isinstance(run.extras, dict) else None
        follow = None
        if thread_id:
            from .threads.service import service_for
            service = service_for(self)
            if service is not None:
                _status, response = service.message(thread_id, {"text": goal, "mode": "new_task",
                                                                **({"appId": app_id} if app_id else {})},
                                                    origin="app", key=request_key)
                follow = self.runs.get(((response or {}).get("run") or {}).get("id") or "")
        if follow is None:
            same_phone = ({"device": run.device_id} if run.device_id and getattr(self, "fleet", None) is not None
                          and self.pool is None else {})
            created = self.create(app_id, goal, "live", request_key, origin="app", **same_phone)
            follow = created[0] if isinstance(created, tuple) else created
        with follow.condition:
            if not follow.preapproved and follow.finished_at is None:
                follow.preapproved = [result_blocks.preapproval(block, item, words)]
        return {**self._block_state(run, key, {"state": "approved", "runId": follow.id, "at": time.time() * 1000}),
                "runId": follow.id}

    def _brief_source(self, run, block_id):
        brief = (run.summary or {}).get("brief") if isinstance(run.summary, dict) else None
        found = ((brief or {}).get("sources") or {}).get(block_id) if isinstance(brief, dict) else None
        if (isinstance(found, list) and len(found) == 2 and all(isinstance(part, str) for part in found)
                and found[0] != run.id and found[0] in self.runs):
            return found[0], found[1]
        return None

    def _mirror_briefs(self, run, key, state):
        block_id, _, item_id = key.partition("/")
        with self.lock:
            briefs = [other for other in self.runs.values() if other is not run and isinstance(other.summary, dict)
                      and isinstance(other.summary.get("brief"), dict)]
        for other in briefs:
            for card, found in ((other.summary["brief"].get("sources") or {}).items()):
                if found == [run.id, block_id]:
                    try:
                        self._block_state(other, f"{card}/{item_id}", state, mirror=False)
                    except APIError:
                        harness_api.log.warning("brief %s: couldn't save a copy of a decision", other.id)

    def _block_state(self, run, key, state, mirror=True):
        # Journaled before the app is told.
        with run.condition:
            updated = dict(run.block_state)
            if state is None:
                updated.pop(key, None)
            else:
                updated[key] = state
            if run.journal:
                try:
                    run.journal.annotate(run.id, {"blockState": updated})
                except JournalError:
                    raise APIError("Mobster couldn't save that. Try again.", 503, "journal_unavailable") from None
            run.block_state = updated
        self.publish("runs", {"event": "run_block_decided", "runId": run.id, "item": key,
                              "state": (state or {}).get("state", "open")})
        if mirror:
            self._mirror_briefs(run, key, state)
        return {"state": (state or {}).get("state", "open"), "blockState": dict(updated)}

    def send_run(self, identifier, destination):
        from . import run_delivery
        run = self._finished_run(identifier, "This task is still running. Send it once it ends.")
        if harness_api.stays_on_mac(run.extras):
            raise APIError(STAYS_ON_MAC, 403, "stays_on_mac")
        return run_delivery.send(self, run, destination)

    def file_run_issue(self, identifier, body):
        from . import run_delivery
        run = self._finished_run(identifier, "This task is still running. File it once it ends.")
        if harness_api.stays_on_mac(run.extras):
            raise APIError(STAYS_ON_MAC, 403, "stays_on_mac")
        return run_delivery.file_issue(self, run, body)

    def inbox(self):
        from . import inbox
        with self.lock:
            runs = list(self.runs.values())
        try:
            workflows = self.workflows.list_with_health() if getattr(self, "workflows", None) is not None else []
        except JournalError:
            workflows = []
        items = inbox.collect(runs, workflows)
        return {"items": items, "count": len(items)}

import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from mobile_agent import api_routes, phone_ready, tracks
from mobile_agent.api_errors import APIError
from mobile_agent.content_queue import client, importing, prompt
from mobile_agent.content_queue import runner as queue_runner
from mobile_agent.content_queue.runner import QueueRunner, outcome, preapproval, preapproval_matches
from mobile_agent.content_queue.store import QueueError, QueueStore, caption_text, fingerprint

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 500
MOV = b"\x00\x00\x00\x14ftypqt  " + b"m" * 700
PROFILES = [
    {"id": "aaaaaaaaaaa1", "handle": "getmobster", "app": {"bundleId": "com.burbn.instagram", "name": "Instagram"},
     "device": "phone-one", "deviceName": "alex"},
    {"id": "aaaaaaaaaaa2", "handle": "getmobster", "app": {"bundleId": "com.zhiliaoapp.musically", "name": "TikTok"},
     "device": "phone-two", "deviceName": "Work iPhone"},
]
ROUTE = "AbCdEfGhIjKlMnOpQrStUvWxYz0123"[:30]  # synthetic route shared by the former app integration fixture
BASE = f"https://remote.mobster.dev/approve/{ROUTE}"


class FakeRun:
    def __init__(self, run_id, events=(), summary=None):
        self.id, self.events, self.summary = run_id, list(events), summary
        self.finished_at = None
        self.preapproved = []
        self.condition = threading.Condition()

    def finish(self, events, summary):
        with self.condition:
            self.events.extend(events)
            self.summary, self.finished_at = summary, time.time()
            self.condition.notify_all()


def post_events(decision, via=None):
    return [{"event": "approval_requested", "approval_id": "p1", "kind": "commit", "act": "post"},
            {"event": "approval_resolved", "approval_id": "p1", "decision": decision, **({"via": via} if via else {})}]


class FakeRuntime:
    def __init__(self, folder, finish=None, busy=False):
        self.config = mock.Mock(state_db=str(Path(folder) / "state.db"))
        self.created, self.busy, self.finish = [], busy, finish
        self.fleet = object()
        self.pool = None

    def resolve_device(self, device):
        """The fleet phone a profile's device names, as phone_ready reads it."""
        return SimpleNamespace(wda_url=f"http://127.0.0.1/{device}", id=device, name=f"the {device} phone")

    def create(self, app_id, goal, mode, **kwargs):
        if self.busy:
            raise APIError("A task is running on this device", code="run_active")
        run = FakeRun(f"run{len(self.created):08x}")
        self.created.append((goal, kwargs, run))
        if self.finish is not None:
            threading.Timer(0.05, lambda: run.finish(*self.finish(run))).start()
        return run


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.store = QueueStore(self.dir)
        (self.dir / "a.jpg").write_bytes(JPEG)
        (self.dir / "b.mov").write_bytes(MOV)
        patcher = mock.patch("mobile_agent.accounts.store.store_for",
                             return_value=mock.Mock(all=lambda: PROFILES))
        patcher.start()
        self.addCleanup(patcher.stop)
        queue_runner.store_for = lambda runtime: self.store
        self.addCleanup(setattr, queue_runner, "store_for", __import__(
            "mobile_agent.content_queue.store", fromlist=["store_for"]).store_for)

    def runner(self, runtime, transfer_state="done", link="https://www.instagram.com/p/Cabc123/", pacing=None):
        sent = []

        def transfer(t):
            sent.append(t)
            t.state = transfer_state
            t.items = [mock.Mock(sha256=None, error=None, state="verified")]
            t.public = lambda: {"id": "t1", "state": transfer_state, "route": "cable", "device": t.phone.id,
                                "before": 1, "after": 2, "verified": True,
                                "items": [{"name": "a.jpg", "state": "verified", "sha256": "x"}]}
        phones = mock.patch("mobile_agent.phone_io.transfer.phone_from_runtime",
                            side_effect=lambda rt, device: mock.Mock(id=device))
        phones.start()
        self.addCleanup(phones.stop)
        made = QueueRunner(runtime, transfer_run=transfer, link_reader=lambda *a: link)
        if pacing is not None:
            made._pacing = lambda: pacing
        return made, sent

    def queued(self, approval="each", count=1, profile=PROFILES[0]["id"]):
        queue = self.store.create(approval=approval)
        items = self.store.add_items(queue["id"], [self.item(profile=profile) for _ in range(count)], PROFILES)
        return queue, items

    def route(self, method, path, body=None, query=None):
        from mobile_agent.api_routes import Request
        tracks.load()
        route, params = api_routes.match(method, path)
        runtime = mock.Mock(config=mock.Mock(state_db=str(self.dir / "state.db")), transfers=None)
        with mock.patch("mobile_agent.content_queue.routes.store_for", return_value=self.store):
            return route.handler(Request(runtime=runtime, method=method, path=path, params=params, query=query or {},
                                         headers={}, body=body, content_type=None, origin="app", app_session=True))

    def item(self, **fields):
        return {"media": [str(self.dir / "a.jpg")], "caption": "Spring is here", "hashtags": "spring, #sale",
                "sound": "Golden Hour", "profile": PROFILES[0]["id"], **fields}


class StoreTests(Base):
    def test_an_item_is_checked_as_it_is_added(self):
        queue = self.store.create("Launch week")
        item = self.store.add_items(queue["id"], [self.item()], PROFILES)[0]
        self.assertEqual(item["hashtags"], ["#spring", "#sale"])
        self.assertEqual(item["media"][0]["kind"], "photo")
        self.assertEqual(len(item["media"][0]["sha256"]), 64)
        self.assertEqual(caption_text(item), "Spring is here\n\n#spring #sale")
        for bad, code in (({"media": [str(self.dir / "missing.jpg")]}, "media_missing"),
                          ({"profile": "bbbbbbbbbbbb"}, "profile_not_found"),
                          ({"hashtags": "not a tag!"}, "invalid")):
            with self.subTest(code=code), self.assertRaises(QueueError) as caught:
                self.store.add_items(queue["id"], [self.item(**bad)], PROFILES)
            self.assertEqual(caught.exception.code, code)
        text = self.dir / "notes.jpg"
        text.write_text("#!/bin/sh")
        with self.assertRaises(QueueError):
            self.store.add_items(queue["id"], [self.item(media=[str(text)])], PROFILES)

    def test_an_unreadable_store_refuses_changes_and_a_missing_one_starts_empty(self):
        self.assertEqual(self.store.create("First")["name"], "First")
        for garbage in (b"{not json", b'{"queues": 3}', b"[]"):
            with self.subTest(garbage=garbage):
                self.store.path.write_bytes(garbage)
                with self.assertRaises(QueueError) as caught:
                    self.store.create("Second")
                self.assertEqual((caught.exception.code, caught.exception.status), ("store_unreadable", 503))
                self.assertEqual(self.store.path.read_bytes(), garbage)
                self.assertEqual(self.store.all(), [])
        self.store.path.unlink()
        self.assertEqual(self.store.create("Fresh")["name"], "Fresh")

    def test_a_batch_approval_covers_items_as_they_were(self):
        queue = self.store.create(approval="batch")
        one, two = self.store.add_items(queue["id"], [self.item(), self.item(caption="Two")], PROFILES)
        self.store.approve_batch(queue["id"], [one["id"], two["id"]])
        covered = self.store.get(queue["id"])["batch"]["items"]
        self.assertEqual(covered[one["id"]], fingerprint(one))
        edited = self.store.edit_item(queue["id"], two["id"], {"caption": "Two, changed"}, PROFILES)
        self.assertNotEqual(covered[two["id"]], fingerprint(edited))
        with self.assertRaises(QueueError):
            self.store.approve_batch(self.store.create()["id"], [one["id"]])

    def test_an_item_on_its_way_or_unknown_cant_be_edited_and_unknown_is_settled_by_hand(self):
        queue = self.store.create()
        item = self.store.add_items(queue["id"], [self.item()], PROFILES)[0]
        self.store.set_state(queue["id"], item["id"], "unknown", note="Look")
        with self.assertRaises(QueueError):
            self.store.edit_item(queue["id"], item["id"], {"caption": "x"}, PROFILES)
        self.assertEqual(self.store.mark(queue["id"], item["id"], "posted")["state"], "posted")
        events = [e["event"] for e in self.store.history(queue["id"])]
        self.assertEqual(events[-2:], ["unknown", "posted"])

    def test_after_a_crash_posting_is_unknown_and_sending_failed(self):
        queue = self.store.create()
        one, two = self.store.add_items(queue["id"], [self.item(), self.item()], PROFILES)
        self.store.set_state(queue["id"], one["id"], "posting", runId="run1")
        self.store.set_state(queue["id"], two["id"], "sending")
        self.assertEqual(self.store.recover(), 2)
        states = [i["state"] for i in self.store.get(queue["id"])["items"]]
        self.assertEqual(states, ["unknown", "failed"])


class PromptTests(Base):
    def test_the_task_names_the_media_profile_sound_and_caption_as_data(self):
        queue = self.store.create()
        item = self.store.add_items(queue["id"], [self.item(media=[str(self.dir / "b.mov")])], PROFILES)[0]
        words = prompt.goal(item, PROFILES[0])
        self.assertIn("Post a Reel on Instagram as @getmobster.", words)
        self.assertIn("b.mov", words)
        self.assertIn("«Golden Hour»", words)
        self.assertIn("«Spring is here\n\n#spring #sale»", words)
        self.assertIn("not instructions", words)
        self.assertIn("Mobster asks before it posts", words)
        self.assertLessEqual(len(words), 4000)


class OutcomeTests(unittest.TestCase):
    def test_only_an_approved_post_with_a_finished_run_that_says_so_is_posted(self):
        done = {"status": "completed", "data": {"posted": True}}
        self.assertEqual(outcome(FakeRun("r", post_events("approved"), done))[0], "posted")
        self.assertEqual(outcome(FakeRun("r", post_events("approved"), {"status": "error"}))[0], "unknown")
        self.assertEqual(outcome(FakeRun("r", post_events("approved"), {"status": "completed",
                                                                           "data": {"posted": False}}))[0], "unknown")
        self.assertEqual(outcome(FakeRun("r", post_events("denied"), done))[0], "declined")
        self.assertEqual(outcome(FakeRun("r", post_events("timeout"), done))[0], "declined")
        self.assertEqual(outcome(FakeRun("r", [], {"status": "blocked", "reason": "Signed in as @other"}))[0],
                         "failed")
        state, note = outcome(FakeRun("r", [], {"status": "completed", "data": {"posted": False, "challenge": True}}))
        self.assertEqual(state, "failed")
        self.assertIn("paused", note)
        self.assertEqual(outcome(FakeRun("r", post_events("approved"), {"status": "completed", "data": {
            "posted": True, "challenge": True}}))[0], "unknown")

    def test_a_batch_approval_answers_only_this_profiles_post(self):
        spec = preapproval({"id": "item"}, PROFILES[0])
        post = {"kind": "commit", "act": "post", "operation": "TAP", "acting_as": "@getmobster on Instagram",
                "app_name": "Instagram"}
        self.assertTrue(preapproval_matches(spec, post))
        for change in ({"acting_as": None}, {"acting_as": "@other on Instagram"}, {"act": "send_message"},
                       {"app_name": "TikTok"}, {"kind": "action"}, {"operation": "TYPE"}):
            with self.subTest(change=change):
                self.assertFalse(preapproval_matches(spec, {**post, **change}))
        self.assertFalse(preapproval_matches({**spec, "used": True}, post))
        from mobile_agent import result_blocks
        from mobile_agent.run import preapproved_matches
        self.assertTrue(preapproved_matches(spec, post))
        self.assertFalse(result_blocks.preapproval_matches(spec, post))


class RunnerTests(Base):
    def test_media_first_then_a_task_as_the_profile_that_asks_before_it_posts(self):
        runtime = FakeRuntime(self.dir, finish=lambda run: (post_events("approved"),
                                                            {"status": "completed", "data": {"posted": True}}))
        queue_runner_, sent = self.runner(runtime)
        queue, (item,) = self.queued()
        self.store.arm(queue["id"], True)
        self.assertEqual(queue_runner_.tick(), ["phone-one"])
        queue_runner_.wait()
        goal, kwargs, run = runtime.created[0]
        self.assertEqual(sent[0].paths, [str(self.dir / "a.jpg")])
        self.assertTrue(sent[0].date_now)
        self.assertEqual(kwargs["extras"]["account"], PROFILES[0]["id"])
        self.assertIn(kwargs["extras"]["permissionMode"], ("ask_all", "ask_important"))
        self.assertEqual(kwargs["extras"]["queueItem"], {"queue": queue["id"], "item": item["id"]})
        self.assertEqual(run.preapproved, [])
        stored = self.store.get(queue["id"])["items"][0]
        self.assertEqual((stored["state"], stored["result"]["link"]),
                         ("posted", "https://www.instagram.com/p/Cabc123/"))
        events = [e["event"] for e in self.store.history(queue["id"])]
        self.assertLess(events.index("sending"), events.index("posting"))

    def test_a_declined_post_is_declined_and_never_retried(self):
        runtime = FakeRuntime(self.dir, finish=lambda run: (post_events("denied"), {"status": "completed",
                                                                                     "data": {"posted": False}}))
        queue_runner_, _ = self.runner(runtime)
        queue, items = self.queued(count=3)
        self.store.arm(queue["id"], True)
        queue_runner_.tick()
        queue_runner_.wait()
        self.assertEqual([i["state"] for i in self.store.get(queue["id"])["items"]], ["declined"] * 3)
        queue_runner_.tick()
        queue_runner_.wait()
        self.assertEqual(len(runtime.created), 3)

    def test_an_approved_post_with_an_unclear_end_is_unknown_and_left_alone(self):
        runtime = FakeRuntime(self.dir, finish=lambda run: (post_events("approved"), {"status": "error"}))
        queue_runner_, _ = self.runner(runtime)
        queue, _ = self.queued()
        self.store.arm(queue["id"], True)
        queue_runner_.tick()
        queue_runner_.wait()
        self.assertEqual(self.store.get(queue["id"])["items"][0]["state"], "unknown")
        self.assertEqual(queue_runner_.tick(), [])
        self.assertEqual(len(runtime.created), 1)

    def test_media_that_didnt_arrive_start_no_task(self):
        runtime = FakeRuntime(self.dir)
        queue_runner_, _ = self.runner(runtime, transfer_state="failed")
        queue, _ = self.queued()
        self.store.arm(queue["id"], True)
        queue_runner_.tick()
        queue_runner_.wait()
        stored = self.store.get(queue["id"])["items"][0]
        self.assertEqual(stored["state"], "failed")
        self.assertIn("Nothing was posted", stored["note"])
        self.assertEqual(runtime.created, [])

    def test_the_batch_approval_rides_on_the_run_only_for_the_item_as_approved(self):
        runtime = FakeRuntime(self.dir, finish=lambda run: (post_events("approved", via="content_queue"),
                                                            {"status": "completed", "data": {"posted": True}}))
        queue_runner_, _ = self.runner(runtime)
        queue, (one, two) = self.queued(approval="batch", count=2)
        self.store.approve_batch(queue["id"], [one["id"], two["id"]])
        self.store.edit_item(queue["id"], two["id"], {"caption": "changed after"}, PROFILES)
        self.store.arm(queue["id"], True)
        queue_runner_.tick()
        queue_runner_.wait()
        runs = [run for _, _, run in runtime.created]
        self.assertEqual(runs[0].preapproved[0]["acting_as"], "@getmobster on Instagram")
        self.assertEqual(runs[1].preapproved, [])

    def test_phones_post_in_parallel_and_a_busy_phone_waits(self):
        release = threading.Event()

        def finish(run):
            release.wait(2)
            return post_events("denied"), {"status": "completed"}
        runtime = FakeRuntime(self.dir, finish=finish)
        queue_runner_, _ = self.runner(runtime)
        queue = self.store.create()
        self.store.update_queue(queue["id"], {"staggerSeconds": 0})
        self.store.add_items(queue["id"], [self.item(profile=PROFILES[0]["id"]), self.item(profile=PROFILES[1]["id"])],
                             PROFILES)
        self.store.arm(queue["id"], True)
        self.assertEqual(sorted(queue_runner_.tick()), ["phone-one", "phone-two"])
        deadline = time.time() + 2
        while len(runtime.created) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(runtime.created), 2)
        release.set()
        queue_runner_.wait()
        busy = FakeRuntime(self.dir, busy=True)
        queue_runner_b, _ = self.runner(busy)
        queue_b, _ = self.queued()
        self.store.arm(queue_b["id"], True)
        queue_runner_b.tick()
        queue_runner_b.wait()
        stored = self.store.get(queue_b["id"])["items"][0]
        self.assertEqual(stored["state"], "ready")
        self.assertIn("Waiting for the phone", stored["note"])

    def test_a_later_time_waits_and_a_stopped_queue_runs_nothing(self):
        runtime = FakeRuntime(self.dir)
        queue_runner_, _ = self.runner(runtime)
        queue, (item,) = self.queued()
        self.assertEqual(queue_runner_.tick(), [])
        self.store.edit_item(queue["id"], item["id"], {"scheduledAt": round((time.time() + 3600) * 1000)}, PROFILES)
        self.store.arm(queue["id"], True)
        self.assertEqual(queue_runner_.tick(), [])
        self.store.edit_item(queue["id"], item["id"], {"scheduledAt": round((time.time() - 3 * 3600) * 1000)},
                             PROFILES)
        queue_runner_.tick()
        self.assertEqual(self.store.get(queue["id"])["items"][0]["state"], "missed")


    # -- the phone checks (phone_ready.py, MOBSTER_QUEUE_PREFLIGHT) ------------------------------------------------

    GRANTED = {"status": "authorized"}

    def helper(self, locked=False, photos=GRANTED, power=None):
        """What the phones' helpers answer, by path; a list is read in turn (the last answer stays)."""
        answers = {"/wda/locked": locked, "/mobster/photos/status": photos, "/mobster/power": power}
        self.reads = []

        def read(url, path):
            value = answers[path]
            self.reads.append((url, path))
            if not isinstance(value, list):
                return value
            return value.pop(0) if len(value) > 1 else value[0]
        checks = (mock.patch.dict(os.environ, {phone_ready.ENV: "1"}), mock.patch.object(phone_ready, "read", read))
        for check in checks:
            check.start()
            self.addCleanup(check.stop)

    def test_a_locked_phone_keeps_a_due_post_ready_and_sends_nothing_until_it_unlocks(self):
        runtime = FakeRuntime(self.dir, finish=lambda run: (post_events("approved"),
                                                            {"status": "completed", "data": {"posted": True}}))
        queue_runner_, sent = self.runner(runtime)
        queue, (item,) = self.queued()
        self.helper(locked=[True, True, False])
        self.store.arm(queue["id"], True)
        for _ in range(2):
            queue_runner_.tick()
            queue_runner_.wait()
        waiting = self.store.get(queue["id"])["items"][0]
        self.assertEqual((waiting["state"], waiting["note"]), ("ready", phone_ready.HOLD_LOCKED))
        self.assertEqual((sent, runtime.created), ([], []))
        held = [e["event"] for e in self.store.history(queue["id"])]
        self.assertEqual(held.count("ready"), 1)   # said once, not on every tick
        queue_runner_.tick()
        queue_runner_.wait()
        posted = self.store.get(queue["id"])["items"][0]
        self.assertEqual((posted["state"], posted["note"]), ("posted", None))
        self.assertEqual(len(sent), 1)

    def test_a_lock_that_cant_be_read_holds_too_and_one_hold_leaves_the_phones_other_posts_alone(self):
        runtime = FakeRuntime(self.dir)
        queue_runner_, sent = self.runner(runtime)
        queue, items = self.queued(count=3)
        self.helper(locked=None)
        self.store.arm(queue["id"], True)
        queue_runner_.tick()
        queue_runner_.wait()
        stored = self.store.get(queue["id"])["items"]
        self.assertEqual([(i["state"], i["note"]) for i in stored][0], ("ready", phone_ready.HOLD_UNREADABLE))
        self.assertEqual([i["state"] for i in stored], ["ready"] * 3)
        self.assertEqual([path for _, path in self.reads], ["/wda/locked"])   # one read for the phone, not three
        self.assertEqual((sent, runtime.created), ([], []))

    def test_a_phone_that_names_no_device_here_holds_instead_of_posting(self):
        from mobile_agent.api_errors import APIError
        runtime = FakeRuntime(self.dir)
        runtime.resolve_device = mock.Mock(side_effect=APIError("No such device", 404, "device_not_found"))
        queue_runner_, sent = self.runner(runtime)
        queue, _ = self.queued()
        self.helper()
        self.store.arm(queue["id"], True)
        queue_runner_.tick()
        queue_runner_.wait()
        self.assertEqual(self.store.get(queue["id"])["items"][0]["note"], phone_ready.HOLD_UNREADABLE)
        self.assertEqual(sent, [])

    def test_without_photos_access_a_post_fails_before_any_file_is_sent_in_the_transfers_words(self):
        from mobile_agent.phone_io.photos import STATUS_WORDS
        for status in ("denied", "restricted", "not_determined"):
            with self.subTest(status=status):
                runtime = FakeRuntime(self.dir)
                queue_runner_, sent = self.runner(runtime)
                queue, _ = self.queued()
                self.helper(photos={"status": status})
                self.store.arm(queue["id"], True)
                queue_runner_.tick()
                queue_runner_.wait()
                stored = self.store.get(queue["id"])["items"][0]
                self.assertEqual(stored["state"], "failed")
                words = phone_ready.NOT_ASKED if status == "not_determined" else STATUS_WORDS[status]
                self.assertEqual(stored["note"], words.format(name="the phone-one phone") + " Nothing was posted.")
                self.assertEqual((sent, runtime.created), ([], []))
                self.store.arm(queue["id"], False)
        self.helper(photos=None)   # a helper that doesn't answer is no grant
        runtime = FakeRuntime(self.dir)
        queue_runner_, sent = self.runner(runtime)
        queue, _ = self.queued()
        self.store.arm(queue["id"], True)
        queue_runner_.tick()
        queue_runner_.wait()
        self.assertIn("couldn't read Photos access", self.store.get(queue["id"])["items"][0]["note"])
        self.assertEqual(sent, [])

    def test_limit_access_is_enough_to_send(self):
        runtime = FakeRuntime(self.dir, finish=lambda run: (post_events("denied"), {"status": "completed"}))
        queue_runner_, sent = self.runner(runtime)
        queue, _ = self.queued()
        self.helper(photos={"status": "limited"})
        self.store.arm(queue["id"], True)
        queue_runner_.tick()
        queue_runner_.wait()
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.store.get(queue["id"])["items"][0]["state"], "declined")

    def test_with_the_check_off_the_queue_never_asks_the_helper(self):
        runtime = FakeRuntime(self.dir, finish=lambda run: (post_events("denied"), {"status": "completed"}))
        queue_runner_, sent = self.runner(runtime)
        queue, _ = self.queued()
        self.store.arm(queue["id"], True)
        with mock.patch.dict(os.environ, {phone_ready.ENV: ""}), \
                mock.patch.object(phone_ready, "read", side_effect=AssertionError("asked the helper")):
            queue_runner_.tick()
            queue_runner_.wait()
        self.assertEqual(len(sent), 1)

    def start(self, runner, queue):
        from mobile_agent.api_routes import Request
        tracks.load()
        route, params = api_routes.match("POST", f"/api/queue/{queue['id']}/start")
        runtime = SimpleNamespace(queue_runner=runner)
        with mock.patch("mobile_agent.content_queue.routes.store_for", return_value=self.store):
            return route.handler(Request(runtime=runtime, method="POST", path=f"/api/queue/{queue['id']}/start",
                                         params=params, query={}, headers={}, body=None, content_type=None,
                                         origin="app", app_session=True))

    def test_a_queue_wont_start_for_a_phone_without_photos_access_and_warns_about_low_power_mode(self):
        queue_runner_, _ = self.runner(FakeRuntime(self.dir))
        queue, _ = self.queued()
        self.store.update_queue(queue["id"], {"staggerSeconds": 0})
        self.store.edit_item(queue["id"], self.store.get(queue["id"])["items"][0]["id"],
                             {"scheduledAt": round((time.time() + 3600) * 1000)}, PROFILES)
        self.helper(photos={"status": "not_determined"})
        refused = self.start(queue_runner_, queue)
        self.assertEqual((refused.status, refused.value["code"]), (409, "photos_not_determined"))
        self.assertIn("press Allow beside", refused.value["error"])
        self.assertFalse(self.store.get(queue["id"])["armed"])   # refused: nothing started
        self.helper(power={"lowPowerMode": True})
        started = self.start(queue_runner_, queue)
        self.assertTrue(self.store.get(queue["id"])["armed"])
        self.assertEqual([(w["code"], w["device"]) for w in started.value["warnings"]],
                         [("low_power_mode", "the phone-one phone")])
        self.assertIn("locks itself after 30 seconds", started.value["warnings"][0]["message"])
        self.store.arm(queue["id"], False)
        self.helper(power={"lowPowerMode": False})
        self.assertNotIn("warnings", self.start(queue_runner_, queue).value)

    def test_starting_with_the_check_off_asks_nothing(self):
        queue_runner_, _ = self.runner(FakeRuntime(self.dir))
        queue, _ = self.queued()
        self.store.edit_item(queue["id"], self.store.get(queue["id"])["items"][0]["id"],
                             {"scheduledAt": round((time.time() + 3600) * 1000)}, PROFILES)
        with mock.patch.dict(os.environ, {phone_ready.ENV: ""}), \
                mock.patch.object(phone_ready, "read", side_effect=AssertionError("asked the helper")):
            started = self.start(queue_runner_, queue)
        self.assertEqual(started.status, 200)
        self.assertNotIn("warnings", started.value)

    def test_a_profiles_queue_tab_starts_its_posts_alone_and_leaves_the_others_and_their_times(self):
        # A profile's Queue tab (IA-SIDEBAR): Start there sends that profile's posts, not every profile's in the queue.
        runtime = FakeRuntime(self.dir)
        queue_runner_, _ = self.runner(runtime)
        queue = self.store.create()
        late = round((time.time() - 3 * 3600) * 1000)
        mine, theirs = self.store.add_items(queue["id"], [self.item(profile=PROFILES[0]["id"]),
                                                          self.item(profile=PROFILES[1]["id"])], PROFILES)
        self.store.edit_item(queue["id"], theirs["id"], {"scheduledAt": late}, PROFILES)
        started = self.route("POST", f"/api/queue/{queue['id']}/start", query={"profile": [PROFILES[0]["id"]]})
        self.assertEqual(started.status, 200)
        self.assertEqual(self.store.get(queue["id"])["armedProfiles"], [PROFILES[0]["id"]])
        queue_runner_.tick()
        time.sleep(0.3)
        queue_runner_.close()
        self.assertEqual(len(runtime.created), 1)
        # The other profile's late post is still its own: not sent, and not marked missed by someone else's Start.
        self.assertEqual(self.store.get(queue["id"])["items"][1]["state"], "ready")
        # Stop there stops that profile; the queue stops with its last started profile.
        self.store.arm(queue["id"], False, profile=PROFILES[0]["id"])
        stopped = self.store.get(queue["id"])
        self.assertEqual((stopped["armed"], stopped["armedProfiles"]), (False, None))
        # Stopping one profile of a whole running queue keeps the others running.
        self.store.arm(queue["id"], True)
        self.store.arm(queue["id"], False, profile=PROFILES[0]["id"])
        self.assertEqual(self.store.get(queue["id"])["armedProfiles"], [PROFILES[1]["id"]])
        with self.assertRaises(ValueError):   # the API answers 400
            self.route("POST", f"/api/queue/{queue['id']}/start", query={"profile": ["not-an-id"]})


class PacingAndFolderTests(Base):
    def test_phones_in_one_tick_start_a_stagger_apart(self):
        runtime = FakeRuntime(self.dir)
        queue_runner_, _ = self.runner(runtime)
        queue = self.store.create()
        self.store.update_queue(queue["id"], {"staggerSeconds": 30})
        self.store.add_items(queue["id"], [self.item(profile=PROFILES[0]["id"]), self.item(profile=PROFILES[1]["id"])],
                             PROFILES)
        self.store.arm(queue["id"], True)
        queue_runner_.tick()
        time.sleep(0.3)
        self.assertEqual(len(runtime.created), 1)
        queue_runner_.close()

    def test_a_paced_or_paused_profile_waits_and_says_why(self):
        from mobile_agent.accounts.pacing import Pacing
        pacing = Pacing(self.dir / "pacing")
        runtime = FakeRuntime(self.dir)
        queue_runner_, _ = self.runner(runtime, pacing=pacing)
        queue = self.store.create()
        item = self.store.add_items(queue["id"], [self.item()], PROFILES)[0]
        self.store.arm(queue["id"], True)
        pacing.record(PROFILES[0]["id"], "post")
        self.assertEqual(queue_runner_.tick(), [])
        note = self.store.get(queue["id"])["items"][0]["note"]
        self.assertIn("Waits for @getmobster's pace", note)
        pacing.pause(PROFILES[0]["id"], "Instagram asked for a selfie check.")
        queue_runner_.tick()
        self.assertIn("is paused", self.store.get(queue["id"])["items"][0]["note"])
        self.assertEqual(runtime.created, [])
        self.assertEqual(item["state"], "ready")

    def test_new_files_in_the_watched_folder_join_the_queue_once(self):
        watched = self.dir / "drop"
        watched.mkdir()
        (watched / "old.jpg").write_bytes(JPEG)
        queue = self.store.create(approval="batch")
        self.store.update_queue(queue["id"], {"watch": {"folder": str(watched), "profile": PROFILES[0]["id"]}},
                                PROFILES)
        (watched / "new.jpg").write_bytes(JPEG)
        (watched / "new.txt").write_text("Fresh from the folder")
        queue_runner_, _ = self.runner(FakeRuntime(self.dir))
        old = time.time() - 60
        os.utime(watched / "new.jpg", (old, old))
        added = queue_runner_.take_new_files(self.store.get(queue["id"]), {p["id"]: p for p in PROFILES})
        self.assertEqual([i["caption"] for i in added], ["Fresh from the folder"])
        self.assertEqual(added[0]["profile"], PROFILES[0]["id"])
        self.assertEqual(queue_runner_.take_new_files(self.store.get(queue["id"]), {p["id"]: p for p in PROFILES}), [])
        self.assertEqual(len(self.store.get(queue["id"])["items"]), 1)
        self.assertIsNone(self.store.get(queue["id"])["batch"])


class ImportTests(Base):
    def test_csv_rows_become_posts_with_profiles_and_times(self):
        text = ("media,caption,hashtags,sound,profile,schedule,format\n"
                "a.jpg|b.mov,Two at once,spring,,@getmobster on TikTok,2026-10-20T09:30,post\n"
                f"{self.dir / 'a.jpg'},Solo,,Golden Hour,{PROFILES[0]['id']},,\n")
        bodies = importing.from_csv(text, self.dir, PROFILES,
                                    lambda profile: "Asia/Tokyo" if profile == PROFILES[1]["id"] else None)
        self.assertEqual(bodies[0]["media"], [str(self.dir / "a.jpg"), str(self.dir / "b.mov")])
        import datetime as dt
        from zoneinfo import ZoneInfo
        self.assertEqual(bodies[0]["scheduledAt"], round(dt.datetime(2026, 10, 20, 9, 30, tzinfo=ZoneInfo("Asia/Tokyo"))
                                                         .timestamp() * 1000))
        self.assertEqual(bodies[0]["profile"], PROFILES[1]["id"])
        self.assertIsInstance(bodies[0]["scheduledAt"], int)
        self.assertEqual(bodies[1]["profile"], PROFILES[0]["id"])
        with self.assertRaises(QueueError):
            importing.from_csv("caption\nhello\n", self.dir, PROFILES)
        with self.assertRaises(QueueError) as caught:
            importing.from_csv("media,profile\na.jpg,@nobody\n", self.dir, PROFILES)
        self.assertIn("Row 2", str(caught.exception))

    def test_a_folder_is_one_post_per_file_with_its_text_beside_it(self):
        (self.dir / "a.txt").write_text("Caption for a #spring")
        bodies = importing.from_folder(self.dir, PROFILES[0]["id"])
        self.assertEqual([Path(b["media"][0]).name for b in bodies], ["a.jpg", "b.mov"])
        self.assertEqual(bodies[0]["caption"], "Caption for a #spring")

    def test_the_report_has_every_post_and_no_formulas(self):
        queue = self.store.create("Week 42")
        item = self.store.add_items(queue["id"], [self.item(caption="=HYPERLINK(1)")], PROFILES)[0]
        self.store.set_state(queue["id"], item["id"], "posted", result={"link": "https://www.instagram.com/p/X/",
                                                                         "postedAt": 1_790_000_000_000})
        data, kind, name = importing.report(self.store.get(queue["id"]), PROFILES)
        text = data.decode()
        self.assertEqual((kind, name), ("text/csv", "Week 42 report.csv"))
        self.assertIn("https://www.instagram.com/p/X/", text)
        self.assertIn("'=HYPERLINK(1)", text)
        rows = json.loads(importing.report(self.store.get(queue["id"]), PROFILES, "json")[0])["posts"]
        self.assertEqual((rows[0]["profile"], rows[0]["state"]), ("@getmobster", "posted"))


class RouteTests(Base):
    def test_the_queue_track_registers_its_routes_and_the_app_only_ones(self):
        tracks.load()
        self.assertEqual(tracks.STATUS.get("content_queue"), "ok")
        for method, path in (("GET", "/api/queue"), ("POST", "/api/queue/abcdefabcdef/items/abcdefabcdef/mark"),
                             ("GET", "/api/transfers/abcdefabcdef"), ("POST", "/api/queue/media")):
            self.assertIsNotNone(api_routes.match(method, path), path)
        from mobile_agent import api_http
        for path in ("/api/queue/abcdefabcdef/start", "/api/queue/abcdefabcdef/approve", "/api/transfers",
                     "/api/queue/abcdefabcdef/items/abcdefabcdef/mark"):
            self.assertTrue(any(m == "POST" and p.fullmatch(path) for m, p in api_http.APP_ONLY_PATTERNS), path)
        self.assertFalse(any(p.fullmatch("/api/queue/abcdefabcdef/items") for _, p in api_http.APP_ONLY_PATTERNS))

    def test_create_add_upload_and_report_over_http(self):
        made = self.route("POST", "/api/queue", {"name": "Drops", "approval": "batch"})
        queue_id = made.value["queue"]["id"]
        piece = self.route("POST", "/api/queue/media", JPEG[:100], {"name": ["drop.jpg"],
                                                                       "upload": ["0123456789abcdef"]})
        self.assertEqual(piece.value["bytes"], 100)
        done = self.route("POST", "/api/queue/media", JPEG[100:], {"name": ["drop.jpg"], "upload": ["0123456789abcdef"],
                                                                    "offset": ["100"], "done": ["1"]})
        media = done.value["media"]
        self.assertEqual(Path(media["path"]).read_bytes(), JPEG)
        out_of_order = self.route("POST", "/api/queue/media", b"x", {"name": ["x.jpg"], "upload": ["fedcba9876543210"],
                                                                       "offset": ["5"]})
        self.assertEqual(out_of_order.status, 409)
        added = self.route("POST", f"/api/queue/{queue_id}/items", {"items": [{"media": [media],
                                                                               "profile": PROFILES[0]["id"]}]})
        self.assertEqual(added.status, 201)
        listed = self.route("GET", "/api/queue")
        self.assertEqual(listed.value["queues"][0]["counts"], {"ready": 1})
        report = self.route("GET", f"/api/queue/{queue_id}/report", query={"format": ["csv"]})
        self.assertEqual(report.filename, "Drops report.csv")
        refused = self.route("POST", f"/api/queue/{queue_id}/items/{'f' * 12}/mark", {"outcome": "posted"})
        self.assertEqual(refused.status, 404)



class FakeRemote:
    def __init__(self, base=BASE):
        self.base, self.told = base, []

    def address(self, prefix):
        return self.base if self.base and prefix == "approve" else None

    def approvals_changed(self, links):
        self.told.append(links)


class ClientTests(Base):
    def setUp(self):
        super().setUp()
        self.remote = FakeRemote()
        patcher = mock.patch.object(client, "_remote", lambda runtime: self.remote)
        patcher.start()
        self.addCleanup(patcher.stop)
        stores = mock.patch.object(client, "store_for", lambda runtime: self.store)
        stores.start()
        self.addCleanup(stores.stop)

    def sent(self, runtime, approval="each"):
        queue, (item,) = self.queued(approval=approval)
        item, link = client.send(runtime, queue["id"], item["id"], PROFILES)
        return queue, item, link

    def say(self, runtime, link, decision, note="", version=None):
        digest = hashlib.sha256(link.rsplit("/", 1)[1].encode()).hexdigest()
        if version is None:
            version = client.answer(runtime, digest)["view"]["v"]
        return client.answer(runtime, digest, {"decision": decision, "note": note, "v": version})

    def run_queue(self, runner, queue):
        self.store.arm(queue["id"], True)
        runner.tick()
        runner.wait()
        return self.store.get(queue["id"])["items"][0]

    def test_a_post_sent_for_approval_never_goes_out_without_the_clients_ok(self):
        runtime = FakeRuntime(self.dir, finish=lambda run: (post_events("approved"),
                                                            {"status": "completed", "data": {"posted": True}}))
        runner, transfers = self.runner(runtime)
        queue, item, link = self.sent(runtime)
        self.assertEqual(self.run_queue(runner, queue)["state"], "ready")
        self.say(runtime, link, "changes", "Use the second photo")
        self.assertEqual(self.run_queue(runner, queue)["state"], "ready")
        self.assertEqual(client.state_of(self.store.get(queue["id"])["items"][0]), "changes")
        client.revoke(runtime, queue["id"], item["id"])
        self.assertEqual(self.run_queue(runner, queue)["state"], "ready")
        self.assertEqual((runtime.created, transfers), ([], []))
        refused = self.say(runtime, link, "approve", version="0" * 16)
        self.assertEqual((refused["status"], "view" in refused), (404, False))

    def test_the_clients_ok_lets_it_go_through_the_queues_own_approval(self):
        runtime = FakeRuntime(self.dir, finish=lambda run: (post_events("approved"),
                                                            {"status": "completed", "data": {"posted": True}}))
        runner, transfers = self.runner(runtime)
        queue, item, link = self.sent(runtime)
        self.assertEqual(self.say(runtime, link, "approve")["status"], 200)
        stored = self.run_queue(runner, queue)
        self.assertEqual(stored["state"], "posted")
        self.assertEqual(len(transfers), 1)
        _, kwargs, run = runtime.created[0]
        self.assertIn(kwargs["extras"]["permissionMode"], ("ask_all", "ask_important"))
        self.assertEqual(run.preapproved, [])
        events = [e["event"] for e in self.store.history(queue["id"])]
        self.assertLess(events.index("client_approved"), events.index("sending"))

    def test_an_edit_after_the_ok_asks_the_client_again_and_a_new_time_does_not(self):
        runtime = FakeRuntime(self.dir)
        queue, item, link = self.sent(runtime)
        self.say(runtime, link, "approve")
        self.store.edit_item(queue["id"], item["id"], {"scheduledAt": round((time.time() + 3600) * 1000)}, PROFILES)
        self.assertEqual(client.state_of(self.store.get(queue["id"])["items"][0]), "approved")
        self.store.edit_item(queue["id"], item["id"], {"caption": "Spring is finally here"}, PROFILES)
        edited = self.store.get(queue["id"])["items"][0]
        self.assertEqual((client.state_of(edited), client.public(edited)["changed"]), ("waiting", True))
        self.assertTrue(client.holds(edited))
        page = client.answer(runtime, hashlib.sha256(link.rsplit("/", 1)[1].encode()).hexdigest())["view"]
        self.assertEqual(page["caption"], "Spring is finally here")

    def test_a_link_works_for_seven_days(self):
        runtime = FakeRuntime(self.dir)
        queue, item, link = self.sent(runtime)
        self.assertEqual(item["clientApproval"]["expiresAt"] - item["clientApproval"]["createdAt"], 7 * 86400_000)
        later = item["clientApproval"]["expiresAt"] + 1
        with mock.patch.object(client, "now_ms", return_value=later):
            self.assertEqual(self.say(runtime, link, "approve", version="0" * 16)["status"], 410)
            self.assertEqual(client.state_of(self.store.get(queue["id"])["items"][0]), "expired")
            self.assertEqual(client.live(runtime), [])
            _, again = client.send(runtime, queue["id"], item["id"], PROFILES)
        self.assertNotEqual(again, link)

    def test_the_page_carries_the_post_as_it_will_appear_and_nothing_else(self):
        photo = io.BytesIO()
        Image.new("RGB", (1200, 1500), (200, 120, 60)).save(photo, "JPEG")
        (self.dir / "a.jpg").write_bytes(photo.getvalue())
        runtime = FakeRuntime(self.dir)
        queue, item, link = self.sent(runtime)
        token = link.rsplit("/", 1)[1]
        self.assertTrue(link.startswith(BASE + "/"))
        self.assertGreaterEqual(len(token), 43)
        self.assertEqual(self.remote.told[-1], [{"h": hashlib.sha256(token.encode()).hexdigest(),
                                                 "exp": item["clientApproval"]["expiresAt"]}])
        page = client.answer(runtime, hashlib.sha256(token.encode()).hexdigest())["view"]
        self.assertEqual(set(page),
                         {"app", "appName", "handle", "when", "day", "caption", "hashtags", "sound", "format",
                          "media", "v", "expires", "status", "decision", "changed"})
        self.assertEqual((page["app"], page["handle"], page["caption"], page["hashtags"]),
                         ("instagram", "getmobster", "Spring is here", ["#spring", "#sale"]))
        self.assertTrue(page["media"][0]["src"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(page["media"][0]["ratio"], 0.8)
        for secret in (token, str(self.dir), queue["id"], item["id"], PROFILES[0]["device"]):
            self.assertNotIn(secret, repr(page))

    def test_an_answer_counts_only_for_the_post_the_client_saw(self):
        runtime = FakeRuntime(self.dir)
        queue, item, link = self.sent(runtime)
        seen = self.say(runtime, link, "approve", version="0" * 16)
        self.assertEqual((seen["status"], seen["error"]), (409, "changed"))
        self.assertIn("view", seen)
        self.assertEqual(self.say(runtime, link, "changes")["status"], 422)
        self.assertEqual(client.answer(runtime, "f" * 64)["status"], 404)
        self.assertEqual(client.answer(runtime, "not a hash")["status"], 404)
        self.assertEqual(client.state_of(self.store.get(queue["id"])["items"][0]), "waiting")

    def test_no_link_without_remote(self):
        self.remote.base = None
        queue, (item,) = self.queued()
        with self.assertRaises(Exception) as caught:
            client.send(FakeRuntime(self.dir), queue["id"], item["id"], PROFILES)
        self.assertEqual(caught.exception.code, "remote_off")
        self.assertNotIn("clientApproval", self.store.get(queue["id"])["items"][0])


    def test_the_link_is_the_apps_alone_and_the_queue_never_shows_its_token(self):
        from mobile_agent import api_http
        queue, (item,) = self.queued()
        path = f"/api/queue/{queue['id']}/items/{item['id']}/client"
        self.assertTrue(any(m == "POST" and p.fullmatch(path) for m, p in api_http.APP_ONLY_PATTERNS))
        made = self.route("POST", path)
        token = made.value["link"].rsplit("/", 1)[1]
        self.assertEqual(made.value["item"]["clientApproval"]["state"], "waiting")
        self.assertEqual(self.route("POST", path).value["link"], made.value["link"])
        shown = self.route("GET", f"/api/queue/{queue['id']}")
        self.assertNotIn(token, repr(shown.value))
        self.assertEqual(self.route("DELETE", path).value["item"]["clientApproval"]["state"], "revoked")




class DraftTests(Base):
    """ICP-8: a run's text result becomes a draft post (a caption and a profile, no media), never sent as it is."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("mobile_agent.content_queue.store.store_for", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        from mobile_agent.content_queue import register
        self.register = register
        self.runtime = mock.Mock(config=mock.Mock(state_db=str(self.dir / "state.db")), runs={})
        register._runtimes.add(self.runtime, self.runtime)
        self.addCleanup(register._runtimes.pop, self.runtime)
        self.queue = self.store.create(approval="each")

    def finished(self, status="completed", answer="Three days left on the spring sale.", field=True):
        run = FakeRun("r" * 12, summary={"status": status, **({"answer": answer} if answer else {})})
        run.status = status
        run.extras = {"queueDraft": {"queue": self.queue["id"], "profile": PROFILES[0]["id"]}} if field else {}
        self.runtime.runs[run.id] = run
        self.register.DraftListener().on_finished(run, run.summary, None)

    def items(self):
        return self.store.get(self.queue["id"])["items"]

    def test_a_finished_run_adds_exactly_one_draft_for_the_profile(self):
        self.finished()
        [draft] = self.items()
        self.assertEqual((draft["state"], draft["media"], draft["caption"], draft["profile"], draft["note"]),
                         ("draft", [], "Three days left on the spring sale.", PROFILES[0]["id"], "Add photos"))

    def test_a_failed_run_or_one_with_no_result_or_no_field_adds_nothing(self):
        self.finished(status="failed")
        self.finished(answer=None)
        self.finished(field=False)
        self.assertEqual(self.items(), [])

    def test_a_draft_does_not_start_until_it_has_media_and_then_is_a_normal_ready_post(self):
        self.finished()
        self.store.arm(self.queue["id"], True)
        finish = lambda run: (post_events("approved"), {"status": "completed"})  # noqa: E731
        runner, sent = self.runner(FakeRuntime(self.dir, finish=finish))
        self.assertEqual(runner.tick(), [])
        [draft] = self.items()
        self.assertEqual(draft["state"], "draft")
        edited = self.store.edit_item(self.queue["id"], draft["id"], {"media": [str(self.dir / "a.jpg")]}, PROFILES)
        self.assertEqual((edited["state"], edited["note"], edited["caption"]), ("ready", None, draft["caption"]))
        self.assertEqual(runner.tick(), ["phone-one"])
        runner.wait()

    def test_a_draft_takes_a_new_caption_and_stays_a_draft_and_is_not_covered_by_a_batch_approval(self):
        self.queue = self.store.create(approval="batch")
        self.finished()
        [draft] = self.items()
        again = self.store.edit_item(self.queue["id"], draft["id"], {"caption": "Better words"}, PROFILES)
        self.assertEqual((again["state"], again["media"]), ("draft", []))
        with self.assertRaises(QueueError) as refused:
            self.store.approve_batch(self.queue["id"], [draft["id"]])
        self.assertEqual(refused.exception.code, "nothing_to_approve")

    def test_an_existing_post_is_left_as_it_was(self):
        [existing] = self.store.add_items(self.queue["id"], [self.item()], PROFILES)
        self.finished()
        first, second = self.items()
        self.assertEqual(first, existing)
        self.assertEqual(second["state"], "draft")

    def test_the_run_field_names_a_queue_and_a_profile_that_exist(self):
        from mobile_agent.content_queue.register import validate_queue_draft
        good = {"queue": self.queue["id"], "profile": PROFILES[0]["id"]}
        self.assertEqual(validate_queue_draft(good, self.runtime), good)
        for bad in ({"queue": "b" * 12, "profile": PROFILES[0]["id"]}, {"queue": self.queue["id"], "profile": "c" * 12},
                    {"queue": self.queue["id"]}, "x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_queue_draft(bad, self.runtime)

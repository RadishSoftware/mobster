"""Track harness, prewarm (SPEC §3.1 F8, v1): the WebDriverAgent session made ready and one screen read kept for 3 s,
reused by the run's first read only when the stream shows nothing changed, the session and the app match; a busy
or cold phone is left alone without an error; and the time to the first action on the fake-latency harness.
Offline, on fakes."""

import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import harness_api
from mobile_agent.frontier import FrontierAgent, prompt_text
from mobile_agent.harness import prewarm
from mobile_agent.server import make_handler
from mobile_agent.state import Element, Snapshot
from mobile_agent.tests.seam_support import isolate, request
from mobile_agent.tests.seam_tasks import SmartBase
from mobile_agent.tests.test_frontier import Driver, Script
from mobile_agent.tests.timing import bound, slow_machine

URL = "http://127.0.0.1:8203"
SMS = "com.apple.MobileSMS"


def chat(bundle=SMS):
    elements = [Element("0", "Sam", "StaticText", (.1, .1, .6, .04)),
                Element("1", "Send", "Button", (.8, .9, .1, .04))]
    return Snapshot(elements, "Sam\nSend", 400, 800, "synthetic_fixture", bundle_id=bundle)


class Clock:
    """A FrameClock stand-in: the stream last changed at ``changed_at`` (monotonic)."""

    def __init__(self, changed_at=0.0):
        self.changed_at, self.closed = changed_at, False

    def still_for(self):
        return time.monotonic() - self.changed_at

    def close(self):
        self.closed = True


class Phone(Driver):
    """A WDA-like driver: a URL and session, a tune, a settled read that takes ``observe_s``, and the first action's
    time."""

    def __init__(self, url, session, screens, observe_s=0.0):
        super().__init__(screens)
        self.url, self.prefix, self.frame_clock = url, "/session/" + session, None
        self.observe_s, self.reads, self.tuned, self.closed = observe_s, 0, None, False
        self.first_action_at, self.last_image = None, None

    def tune(self, rich=True, glide=True, **kwargs):
        self.tuned = (rich, glide)
        return {}

    def observe_ready(self, timeout=10):
        first = self.observe(timeout)    # two agreeing reads
        second = self.observe(timeout)
        second.read_started = first.read_started
        return second

    def observe(self, timeout=10):
        started = time.monotonic()
        time.sleep(self.observe_s)
        self.reads += 1
        snapshot = super().observe(timeout)
        snapshot.read_started = started
        return snapshot

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        if self.first_action_at is None:
            self.first_action_at = time.monotonic()
        super().execute(operation, target, snapshot, text, timeout)

    def call(self, *args, **kwargs):
        return None

    def close(self):
        self.closed = True


class Fakes:
    """build_target_driver, attach_frame_clock and the lease check, patched for one test."""

    def __init__(self, testcase, *, screens=None, changed_at=0.0, session_error=None, lease_free=True):
        self.drivers, self.clocks = [], []
        self.screens = screens or [chat()]

        def build(wda_url=None, session=None, **kwargs):
            driver = Phone(wda_url, session, self.screens)
            self.drivers.append(driver)
            return driver

        def attach(driver, video=None, mode=None):
            clock = Clock(changed_at)
            driver.frame_clock = clock
            self.clocks.append(clock)
            return clock
        for target, value in (("build_target_driver", build), ("attach_frame_clock", attach),
                              ("_lease_free", lambda url: lease_free)):
            p = patch.object(prewarm, target, value)
            p.start()
            testcase.addCleanup(p.stop)
        testcase.addCleanup(prewarm.reset_for_tests)

        def session():
            if session_error is not None:
                raise session_error
            return "s1"
        self.phone = SimpleNamespace(wda_url=URL, wda_session=session, video=object(), primary=True, key="p")
        self.busy = None
        self.runtime = SimpleNamespace(resolve_device=lambda device: self.phone, run_on=lambda phone: self.busy)


def ctx(bundle=SMS):
    return harness_api.RunContext(run_id="a" * 12, goal="g", engine="smart", origin="app", app_bundle=bundle,
                                  device_id=None, device_kind="usb", extras=harness_api.frozen_mapping({}),
                                  data_dir=None, emit=lambda e: None, clarify=lambda r: "denied",
                                  cancelled=lambda: False, runtime=None)


class PrewarmTests(unittest.TestCase):
    def setUp(self):
        prewarm.reset_for_tests()

    def test_it_warms_the_session_and_one_screen_and_the_first_read_reuses_it(self):
        fakes = Fakes(self)
        self.assertEqual(prewarm.prewarm(fakes.runtime), {"warmed": ["wda", "screen"], "pending": False})
        warm_driver = fakes.drivers[0]
        self.assertTrue(warm_driver.closed)
        self.assertIsNone(warm_driver.frame_clock)   # the clock outlives the driver, for 3 s
        self.assertEqual(warm_driver.tuned, (True, True))
        run_driver = Phone(URL, "s1", [chat()])
        taken = prewarm.take(run_driver, ctx())
        self.assertIsNotNone(taken)
        snapshot, age = taken
        self.assertEqual(snapshot.bundle_id, SMS)
        self.assertLess(age, 1)
        self.assertTrue(fakes.clocks[0].closed)
        self.assertIsNone(prewarm.take(run_driver, ctx()))   # once

    def test_reuse_needs_an_unchanged_screen_the_same_session_and_app_and_a_fresh_read(self):
        cases = {"another session": (Phone(URL, "s2", [chat()]), ctx(), None),
                 "another app": (Phone(URL, "s1", [chat()]), ctx("com.apple.Preferences"), None),
                 "home screen start": (Phone(URL, "s1", [chat()]), ctx(None), None),
                 "too old": (Phone(URL, "s1", [chat()]), ctx(), time.monotonic() + prewarm.SCREEN_TTL + 1),
                 "another phone": (Phone("http://127.0.0.1:8100", "s1", [chat()]), ctx(), None)}
        for name, (driver, context, now) in cases.items():
            with self.subTest(name):
                prewarm.reset_for_tests()
                fakes = Fakes(self)
                prewarm.prewarm(fakes.runtime)
                self.assertIsNone(prewarm.take(driver, context, now=now))
        prewarm.reset_for_tests()
        fakes = Fakes(self, changed_at=time.monotonic() + 3600)   # the stream changed after the read began
        prewarm.prewarm(fakes.runtime)
        self.assertIsNone(prewarm.take(Phone(URL, "s1", [chat()]), ctx()))
        self.assertTrue(all(clock.closed for clock in fakes.clocks))

    def test_a_busy_phone_is_left_alone(self):
        fakes = Fakes(self)
        fakes.busy = "b" * 12
        self.assertEqual(prewarm.prewarm(fakes.runtime), {"warmed": [], "pending": False, "reason": "busy"})
        prewarm.reset_for_tests()
        fakes = Fakes(self, lease_free=False)   # another Mobster process drives it
        self.assertEqual(prewarm.prewarm(fakes.runtime)["warmed"], [])
        self.assertEqual(fakes.drivers, [])

    def test_a_cold_phone_is_never_an_error(self):
        fakes = Fakes(self, session_error=ConnectionRefusedError("WDA is not running"))
        self.assertEqual(prewarm.prewarm(fakes.runtime),
                         {"warmed": [], "pending": False, "reason": "ConnectionRefusedError"})

    def test_calls_close_together_share_one_warm_up(self):
        fakes = Fakes(self)
        prewarm.prewarm(fakes.runtime)
        prewarm.prewarm(fakes.runtime)
        self.assertEqual(len(fakes.drivers), 1)

    def test_the_frontier_takes_the_warm_read_as_its_first_screen(self):
        fakes = Fakes(self)
        prewarm.prewarm(fakes.runtime)
        driver = Phone(URL, "s1", [chat()] * 4)
        events = []
        FrontierAgent(driver, Script([("TAP", "Sam", None), ("DONE", None, None), ("DONE", None, None)]),
                      emit=events.append, screenshots=False, settle_seconds=0, skills=(),
                      run_context=ctx()).run("Open Sam's chat")
        used = [e for e in events if e["event"] == "prewarm_used"]
        self.assertEqual(len(used), 1)
        self.assertIsInstance(used[0]["age_ms"], int)
        kinds = [e["event"] for e in events]
        self.assertLess(kinds.index("prewarm_used"), kinds.index("frontier_decision"))


class RouteTests(SmartBase):
    def test_the_route_answers_202_and_never_5xx_for_a_cold_phone(self):
        isolate(self)
        runtime = self.runtime()
        handler = make_handler(runtime)
        with patch("mobile_agent.server.resolve_wda_session", side_effect=ConnectionRefusedError("down")), \
                patch.object(prewarm, "_lease_free", lambda url: True):
            status, data, _ = request(handler, "POST", "/api/prewarm", {})
        self.assertEqual(status, 202)
        self.assertEqual(data["warmed"], [])
        self.assertEqual(request(handler, "POST", "/api/prewarm", {"device": 3})[0], 400)
        self.assertEqual(request(handler, "POST", "/api/prewarm", {"x": 1})[0], 400)
        prewarm.reset_for_tests()


# -- the fake-latency harness: time to the first action, with and without prewarm ---------------------------------

SCALE = .2
PRINT = bool(os.environ.get("MOBSTER_PRINT_TIMING"))   # read at import: the tests clear the environment
# Assumed latencies, in seconds at full scale (stated, not measured on a phone; the owner's test plan measures them):
# WDA creating a session after the helper starts, /status when one exists, configure on a new process (settings and
# /wda/screen) and after, a settled read (two agreeing reads), the task contract call and a decision (B3, 5 Oct).
LATENCY = {"session_cold": 1.5, "session_warm": .05, "configure_cold": .6, "configure_warm": .05,
           "observe_ready": .8, "contract": 2.6, "decision": 2.4}


class SlowModel:
    def __init__(self, scale):
        self.scale = scale
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}
        self.model, self.prompts = "gpt-5.6-sol", []

    def complete(self, messages, schema, timeout=60, **kwargs):
        planning = "items" in schema["properties"] and "actions" not in schema["properties"]
        time.sleep(LATENCY["contract" if planning else "decision"] * self.scale)
        if planning:
            return {"items": [{"kind": "READ", "app": "Messages", "what": "Sam", "payload": "", "act": "none",
                               "count": "", "condition": "", "quote": "Sam"}]}, {}
        self.usage["calls"] += 1
        prompt = prompt_text(messages)
        self.prompts.append(prompt)
        rows = prompt.split("Screen elements:\n", 1)[1].splitlines()
        first = self.usage["calls"] == 1
        target = next((r.split()[0] for r in rows if '"Sam"' in r), None)
        return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [],
                "actions": [{"operation": "TAP" if first else "DONE", "target": target if first else None,
                             "target_label": None, "text": None, "app": None}],
                "answer": None if first else "Sam's chat is open."}, {}


class FirstActionTests(SmartBase):
    def first_action(self, runtime, *, cold, warm_first):
        state = {"session": not cold, "size": not cold}
        drivers = []

        def resolve(url, preferred=None, timeout=5, create=True):
            time.sleep(LATENCY["session_warm" if state["session"] else "session_cold"] * SCALE)
            state["session"] = True
            return "s1"

        def build(wda_url=None, session=None, **kwargs):
            time.sleep(LATENCY["configure_warm" if state["size"] else "configure_cold"] * SCALE)
            state["size"] = True
            driver = Phone(wda_url, session, [chat()] * 6, observe_s=LATENCY["observe_ready"] * SCALE / 2)
            drivers.append(driver)
            return driver

        def attach(driver, video=None, mode=None):
            driver.frame_clock = Clock(0.0)   # nothing on screen moves in this harness
            return driver.frame_clock
        model = SlowModel(SCALE)
        patches = [patch("mobile_agent.server.resolve_wda_session", side_effect=resolve),
                   patch("mobile_agent.server.build_target_driver", side_effect=build),
                   patch.object(prewarm, "build_target_driver", side_effect=build),
                   patch.object(prewarm, "attach_frame_clock", side_effect=attach),
                   patch.object(prewarm, "_lease_free", lambda url: True),
                   patch("mobile_agent.server.prepare_wda_phone"),
                   patch("mobile_agent.engines.build_client", return_value=model),
                   patch("mobile_agent.frontier.video_frame", return_value=None)]
        for p in patches:
            p.start()
        try:
            if warm_first:
                self.assertIn("wda", prewarm.prewarm(runtime, wait=5)["warmed"])
            run = runtime.create("messages", "Open Sam's chat", "live", origin="app")
            self.worker.stop()
            started = time.monotonic()
            try:
                runtime.work(run)
            finally:
                self.worker.start()
        finally:
            for p in reversed(patches):
                p.stop()
            prewarm.reset_for_tests()
        if run.lease:
            run.lease.close()
            run.lease = None
        runtime.listeners.flush(5)
        phone = drivers[-1]
        self.assertIsNotNone(phone.first_action_at, [e["event"] for e in run.events])
        return (phone.first_action_at - started) / SCALE, run

    @unittest.skipIf(slow_machine(), "a timing comparison: skipped where timing bounds are scaled")
    def test_time_to_first_action_falls_by_30_percent_on_a_cold_helper(self):
        runtime = self.runtime()
        without, _ = self.first_action(runtime, cold=True, warm_first=False)
        with_prewarm, run = self.first_action(self.runtime(), cold=True, warm_first=True)
        self.assertIn("prewarm_used", [e["event"] for e in run.events])
        drop = (without - with_prewarm) / without
        if PRINT:
            print(f"\ncold helper: {without:.2f} s -> {with_prewarm:.2f} s ({drop:.0%} faster)")
        self.assertGreaterEqual(drop, .30, f"{without:.2f} s -> {with_prewarm:.2f} s")

    @unittest.skipIf(slow_machine(), "a timing comparison: skipped where timing bounds are scaled")
    def test_a_warm_helper_still_saves_the_first_read(self):
        without, _ = self.first_action(self.runtime(), cold=False, warm_first=False)
        with_prewarm, _ = self.first_action(self.runtime(), cold=False, warm_first=True)
        if PRINT:
            print(f"\nwarm helper: {without:.2f} s -> {with_prewarm:.2f} s "
                  f"({(without - with_prewarm) / without:.0%} faster)")
        self.assertLess(with_prewarm, without - bound(.2))


if __name__ == "__main__":
    unittest.main()

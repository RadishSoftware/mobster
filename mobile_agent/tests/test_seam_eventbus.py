"""Seam S4: the event bus behind GET /api/events. Ids are <boot>-<seq>; a stale or foreign Last-Event-ID gets a
reset; every stream counts against runtime.streams. Offline."""

import threading
import time
import unittest

from mobile_agent import eventbus
from mobile_agent.eventbus import EventBus, parse_topics


class BusTests(unittest.TestCase):
    def test_ids_order_and_topics(self):
        bus = EventBus(capacity=8)
        self.assertRegex(bus.boot, r"^[0-9a-f]{8}$")
        first = bus.publish("runs", {"event": "run_created", "runId": "a" * 12})
        bus.publish("threads", {"event": "thread_created"})
        third = bus.publish("thread:3f9c2a1b7d0e", {"event": "thread_item"})
        self.assertEqual(first, f"{bus.boot}-1")
        self.assertEqual(third, f"{bus.boot}-3")
        events = bus.since({"runs", "thread:3f9c2a1b7d0e"}, f"{bus.boot}-0")
        self.assertEqual([e["event"] for e in events], ["run_created", "thread_item"])
        self.assertEqual(events[0]["topic"], "runs")
        self.assertIsInstance(events[0]["timestamp"], float)
        self.assertNotIn("_seq", events[0])
        self.assertEqual(bus.since({"runs"}, first), [])
        self.assertEqual(bus.since({"runs"}, None), [])

    def test_a_reconnect_resumes_after_its_last_id(self):
        bus = EventBus()
        ids = [bus.publish("runs", {"event": "run_created", "n": n}) for n in range(5)]
        self.assertEqual([e["n"] for e in bus.since({"runs"}, ids[2])], [3, 4])

    def test_another_boot_or_an_evicted_id_gets_a_reset_then_the_buffer(self):
        bus = EventBus(capacity=3)
        ids = [bus.publish("runs", {"event": "run_created", "n": n}) for n in range(6)]
        for stale in ("0badb00t-4", ids[0], "garbage", f"{bus.boot}-99"):
            with self.subTest(last=stale):
                events = bus.since({"runs"}, stale)
                self.assertEqual(events[0]["event"], "reset")
                self.assertEqual([e["n"] for e in events[1:]], [3, 4, 5])
        # The newest id the buffer still follows on from is fine.
        self.assertEqual([e["n"] for e in bus.since({"runs"}, ids[2])], [3, 4, 5])

    def test_wait_returns_new_events_or_nothing_after_the_timeout(self):
        bus = EventBus()
        start = bus.position()
        self.assertEqual(bus.wait({"runs"}, start, .05), [])
        threading.Timer(.05, lambda: bus.publish("runs", {"event": "run_finished"})).start()
        events = bus.wait({"runs"}, start, 5)
        self.assertEqual([e["event"] for e in events], ["run_finished"])

    def test_a_stream_keeps_alive_and_never_resets_on_other_topics(self):
        bus = EventBus(capacity=4)
        stream = bus.stream({"thread:3f9c2a1b7d0e"}, None, keepalive_s=.05)
        for n in range(20):  # other topics fill and roll the buffer: no false reset for this stream
            bus.publish("runs", {"event": "run_created", "n": n})
        self.assertIsNone(next(stream))  # a keepalive
        bus.publish("thread:3f9c2a1b7d0e", {"event": "thread_item"})
        self.assertEqual(next(stream)["event"], "thread_item")
        stream.close()

    def test_topics_are_validated(self):
        self.assertEqual(parse_topics("runs, thread:3f9c2a1b7d0e"), {"runs", "thread:3f9c2a1b7d0e"})
        for bad in ("", "Runs", "ab", "thread:XYZ", "thread:3f9c", ",".join(["runs"] * 9), "a" * 17):
            with self.subTest(topics=bad), self.assertRaises(ValueError):
                parse_topics(bad)
        with self.assertRaises(ValueError):
            EventBus().publish("Not A Topic", {"event": "x"})
        self.assertIs(eventbus.publish.__self__, eventbus.bus)


if __name__ == "__main__":
    unittest.main()

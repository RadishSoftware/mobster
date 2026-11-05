import threading
import unittest
from types import SimpleNamespace

from mobile_agent import harness_api, ownership


def run(origin="app", **fields):
    return SimpleNamespace(id="a1b2c3d4e5f6", origin=origin, created_at=1000.0, started_at=None,
                           stop=threading.Event(), taking_back=False, **fields)


class HolderTests(unittest.TestCase):
    def test_every_origin_has_a_kind_and_a_label(self):
        self.assertEqual(set(ownership.ORIGIN_KINDS), set(harness_api.ORIGINS))
        self.assertEqual(set(ownership.ORIGIN_LABELS), set(harness_api.ORIGINS))
        self.assertTrue(set(ownership.ORIGIN_KINDS.values()) <= set(ownership.KINDS))

    def test_a_client_name_is_a_short_plain_label(self):
        cleaned = ownership.clean_client("<b>evil</b>\n" + "x" * 100)
        self.assertLessEqual(len(cleaned), 40)
        self.assertFalse(set("<>\n") & set(cleaned))
        self.assertIsNone(ownership.clean_client("\u202e\u2066"))

    def test_the_record_has_exactly_the_contract_fields(self):
        holder = ownership.holder_for(run("app"))
        self.assertEqual(holder, {"kind": "task", "label": "a task in Mobster", "runId": "a1b2c3d4e5f6",
                                  "since": 1_000_000, "stopping": False})

    def test_an_mcp_client_is_named_as_it_names_itself(self):
        self.assertEqual(ownership.holder_for(run("mcp"), client="claude-code")["label"], "Claude Code")
        self.assertEqual(ownership.holder_for(run("mcp"), client="Some Agent 2")["label"], "Some Agent 2")
        self.assertEqual(ownership.holder_for(run("mcp"), client="")["label"], "an MCP client")
        self.assertEqual(ownership.holder_for(run("mcp"), client=None)["kind"], "mcp")

    def test_the_client_name_never_changes_the_kind(self):
        # A client calling itself "Mobster Remote" is still an MCP client, and a Remote run ignores any client name.
        self.assertEqual(ownership.holder_for(run("mcp"), client="Mobster Remote")["kind"], "mcp")
        self.assertEqual(ownership.holder_for(run("remote"), client="Claude Code")["label"], "Mobster Remote")

    def test_a_workflow_is_named_when_its_name_is_known(self):
        self.assertEqual(ownership.holder_for(run("schedule"), workflow="Morning check")["label"],
                         "the scheduled workflow Morning check")
        self.assertEqual(ownership.holder_for(run("workflow"), workflow="Morning check")["label"],
                         "the workflow Morning check")

    def test_since_is_when_the_worker_started_else_when_it_was_made(self):
        started = run("app")
        started.started_at = 1234.5
        self.assertEqual(ownership.holder_for(started)["since"], 1_234_500)
        self.assertEqual(ownership.holder_for(run("app"))["since"], 1_000_000)

    def test_stopping_follows_stop_and_take_back(self):
        pressed = run("mcp")
        self.assertFalse(ownership.holder_for(pressed)["stopping"])
        pressed.stop.set()
        self.assertTrue(ownership.holder_for(pressed)["stopping"])
        taken = run("mcp")
        taken.taking_back = True
        self.assertTrue(ownership.holder_for(taken)["stopping"])
        self.assertTrue(ownership.holder_for(run("mcp"), stopping=True)["stopping"])

    def test_manual_control_and_another_process(self):
        manual = ownership.manual_holder("you", 50.0)
        self.assertEqual((manual["kind"], manual["runId"], manual["since"], manual["stopping"]),
                         ("manual", None, 50_000, False))
        self.assertEqual(ownership.holding_sentence(manual), "You are using this phone")
        other = ownership.elsewhere_holder(60.0)
        self.assertEqual((other["kind"], other["label"], other["runId"]), ("agent", "another Mobster process", None))

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.queue = ownership.PhoneQueue(clock=lambda: self.now, limit=3, ttl=60)

    def test_first_come_first_served_with_positions_from_one(self):
        for name in ("a", "b", "c"):
            self.queue.add(name, "mcp", "Claude Code")
        self.assertEqual([item["position"] for item in self.queue.view()], [1, 2, 3])
        self.assertEqual(self.queue.head().id, "a")
        self.assertTrue(self.queue.remove("a"))
        self.assertEqual([(item["id"], item["position"]) for item in self.queue.view()], [("b", 1), ("c", 2)])
        self.assertFalse(self.queue.remove("a"))
        self.assertEqual(self.queue.position("c"), 2)
        self.assertIsNone(self.queue.position("a"))

    def test_it_is_bounded(self):
        for name in ("a", "b", "c"):
            self.queue.add(name, "task", "a task in Mobster")
        with self.assertRaises(ownership.QueueFull):
            self.queue.add("d", "task", "a task in Mobster")
        self.assertEqual(len(self.queue), 3)

    def test_a_request_expires_after_its_time_and_the_rest_keep_their_place(self):
        self.queue.add("a", "task", "x")
        self.now += 30
        self.queue.add("b", "task", "x")
        self.now += 31   # a waited 61 s, b 31 s
        self.assertEqual([entry.id for entry in self.queue.expired()], ["a"])
        self.assertEqual([item["id"] for item in self.queue.view()], ["b"])
        self.assertEqual(self.queue.expired(), [])

    def test_take_it_back_holds_the_line_for_a_grace_then_lets_go(self):
        self.assertFalse(self.queue.held())
        self.queue.hold(120)
        self.assertTrue(self.queue.held())
        self.now += 119
        self.assertTrue(self.queue.held())
        self.now += 2
        self.assertFalse(self.queue.held())
        self.assertIsNone(self.queue.held_until)
        self.queue.hold(120)
        self.queue.release()
        self.assertFalse(self.queue.held())

    def test_queues_are_kept_per_phone(self):
        queues = ownership.Queues(clock=lambda: self.now, limit=2, ttl=60)
        queues.of("@primary").add("a", "task", "x")
        queues.of("phone-2").add("b", "task", "x")
        self.assertEqual(queues.locate("b")[0], "phone-2")
        self.assertEqual(queues.locate("zzz"), (None, None))
        self.assertEqual(queues.total(), 2)
        self.assertIsNone(queues.find("phone-3"))


if __name__ == "__main__":
    unittest.main()

"""Track memory: which facts go with a task, and the provider that sends them (SPEC §3.3 F1–F2). Scoring, app
scopes, pins, the 12-fact and 1,200-character caps, who gets memory (origins), the switch, memory_used, the
conversation's last app, and the block's place in the frontier's prompt. Offline, on fakes."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import harness_api
from mobile_agent.memory import provider as memory_provider
from mobile_agent.memory import retrieval
from mobile_agent.memory.store import MemoryStore


def fact(id, text, scope="global", pinned=False, updated=0.0, used=None):
    return {"id": id, "text": text, "scope": scope, "pinned": int(pinned), "updated_at": updated,
            "last_used_at": used, "uses": 0}


class SelectTests(unittest.TestCase):
    def test_shared_words_pick_the_facts(self):
        facts = [fact("a", "My gym is the one on 5th Street"), fact("b", "Kate Bell is my sister"),
                 fact("c", "I like my coffee with oat milk")]
        self.assertEqual([f["id"] for f in retrieval.select(facts, "When does my gym close today?")], ["a"])
        self.assertEqual([f["id"] for f in retrieval.select(facts, "Text my sister I'm late")], ["b"])
        self.assertEqual([f["id"] for f in retrieval.select(facts, "Text Kate that I'm on my way")], ["b"])
        self.assertEqual(retrieval.select(facts, "Turn on Dark Mode"), [])

    def test_stems_and_rare_words_rank_first(self):
        facts = [fact("a", "Texts to Kate go to her work number"), fact("b", "I text in lowercase"),
                 fact("c", "Kate Bell is my sister", updated=5)]
        chosen = [f["id"] for f in retrieval.select(facts, "texting Kate about dinner")]
        self.assertEqual(chosen[0], "a")                      # shares both words
        self.assertEqual(set(chosen), {"a", "b", "c"})
        self.assertEqual(retrieval.stem("texting"), retrieval.stem("texts"))
        self.assertEqual(retrieval.stem("closes"), retrieval.stem("closed"))
        self.assertEqual(retrieval.stem("messages"), retrieval.stem("message"))

    def test_pinned_always_go_and_come_first(self):
        facts = [fact("a", "Call me Sam", pinned=True), fact("b", "My gym is on 5th"),
                 fact("c", "Sign my texts with – Sam", "app:com.apple.MobileSMS", pinned=True)]
        self.assertEqual([f["id"] for f in retrieval.select(facts, "Turn on Dark Mode")], ["a"])
        self.assertEqual([f["id"] for f in retrieval.select(facts, "when does my gym open",
                                                            ["com.apple.MobileSMS"])], ["a", "c", "b"])

    def test_an_apps_facts_go_only_when_that_app_is_in_play(self):
        facts = [fact("m", "Sign my texts with – Sam", "app:com.apple.MobileSMS"),
                 fact("x", "Use the shared calendar", "app:com.example.planner")]
        self.assertEqual(retrieval.select(facts, "Text Sam hello"), [])
        self.assertEqual([f["id"] for f in retrieval.select(facts, "Text Sam hello", ["com.apple.MobileSMS"])],
                         ["m"])
        self.assertEqual([f["id"] for f in retrieval.select(facts, "Open Messages and text Sam")], ["m"])
        self.assertEqual(retrieval.select(facts, "Plan my week on the shared calendar"), [])  # name unknown
        from mobile_agent import catalog
        with patch.dict(catalog._EXTRA_NAMES, {"com.example.planner": "Planner"}):
            self.assertEqual([f["id"] for f in retrieval.select(facts, "In Planner, plan my week")], ["x"])

    def test_caps_twelve_facts_and_1200_characters(self):
        many = [fact(f"{n:02d}", f"Gym fact number {n}") for n in range(30)]
        self.assertEqual(len(retrieval.select(many, "gym")), 12)
        long = [fact(f"l{n}", "gym " + "x" * 280) for n in range(6)] + [fact("short", "gym bag is blue")]
        chosen = retrieval.select(long, "gym")
        text = retrieval.render(chosen)
        self.assertLessEqual(len(text), 1200)
        self.assertIn("short", [f["id"] for f in chosen])      # a shorter one still fits after the long ones
        self.assertEqual(len(chosen), 5)

    def test_rendering(self):
        facts = [fact("a", "Call me Sam"), fact("m", "Sign my texts with – Sam", "app:com.apple.MobileSMS"),
                 fact("x", "Use the shared calendar", "app:com.example.planner")]
        self.assertEqual(retrieval.render(facts), "- Call me Sam\n- In Messages: Sign my texts with – Sam\n"
                                                  "- In com.example.planner: Use the shared calendar")

    def test_five_hundred_facts_take_milliseconds(self):
        facts = [fact(f"{n:03d}", f"Fact {n} about the gym, the office and Kate Bell's birthday in May")
                 for n in range(500)]
        started = time.perf_counter()
        for _ in range(5):
            retrieval.select(facts, "When is Kate's birthday? Put it in my calendar")
        self.assertLess((time.perf_counter() - started) / 5, 0.25)


class Runs(dict):
    pass


def context(origin="app", goal="When does my gym close?", app=None, thread=None, runtime=None, run_id="aaaaaaaaaaaa"):
    events = []
    ctx = harness_api.RunContext(run_id=run_id, goal=goal, engine="smart", origin=origin, app_bundle=app,
                                 device_id=None, device_kind=None,
                                 extras=harness_api.frozen_mapping({"threadId": thread} if thread else {}),
                                 data_dir=None, emit=events.append, clarify=lambda r: "", cancelled=lambda: False,
                                 runtime=runtime)
    return ctx, events


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mobster-memory-")) / "memory"
        env = patch.dict(os.environ, {"MOBSTER_MEMORY_DIR": str(self.folder)})
        env.start()
        self.addCleanup(env.stop)
        self.store = MemoryStore(self.folder)

    def test_nothing_remembered_means_no_provider_and_no_file(self):
        ctx, _ = context()
        self.assertIsNone(memory_provider.factory(ctx))
        self.assertFalse(self.folder.exists())

    def test_only_tasks_a_person_started_or_scheduled_get_memory(self):
        self.store.add_fact("My gym is the one on 5th Street")
        for origin, expected in (("app", True), ("tui", True), ("cli", True), ("workflow", True),
                                 ("schedule", True), ("mcp", False), ("api", False)):
            with self.subTest(origin=origin):
                ctx, _ = context(origin)
                self.assertEqual(memory_provider.factory(ctx) is not None, expected)

    def test_the_block_names_its_facts_by_id_and_records_their_use(self):
        gym = self.store.add_fact("My gym is the one on 5th Street")
        self.store.add_fact("Kate Bell is my sister")
        ctx, events = context()
        provider = memory_provider.factory(ctx)
        self.assertEqual((provider.key, provider.max_chars), ("memory", 1200))
        (block,) = provider.blocks(ctx)
        self.assertEqual((block.key, block.stable, block.untrusted), ("memory", True, False))
        self.assertEqual(block.title, "What the user asked Mobster to remember (their words; the screen wins when it "
                                      "disagrees)")
        self.assertEqual(block.text, "- My gym is the one on 5th Street")
        self.assertEqual(events, [{"event": "memory_used", "ids": [gym["id"]]}])
        self.assertEqual(self.store.get_fact(gym["id"])["uses"], 1)
        self.assertEqual(provider.turn_blocks(ctx, None), ())
        ctx, events = context(goal="Turn on Dark Mode")
        self.assertEqual(provider.blocks(ctx), ())
        self.assertEqual(events, [])

    def test_the_switch_turns_it_off(self):
        self.store.add_fact("Call me Sam", pinned=True)
        self.store.update_settings({"useInTasks": False})
        ctx, events = context()
        self.assertEqual(memory_provider.factory(ctx).blocks(ctx), ())
        self.assertEqual(events, [])

    def test_the_conversations_last_app_is_in_play(self):
        self.store.add_fact("Sign my texts with – Sam", "app:com.apple.MobileSMS")
        earlier = SimpleNamespace(id="bbbbbbbbbbbb", created_at=10.0, extras={"threadId": "3f9c2a1b7d0e"},
                                  app={"bundleId": None}, events=[{"event": "plan", "apps": ["Messages"]}])
        older = SimpleNamespace(id="cccccccccccc", created_at=5.0, extras={"threadId": "3f9c2a1b7d0e"},
                                app={"bundleId": "com.apple.mobilecal"}, events=[])
        other = SimpleNamespace(id="dddddddddddd", created_at=20.0, extras={"threadId": "000000000000"},
                                app={"bundleId": "com.apple.Maps"}, events=[])
        runtime = SimpleNamespace(runs={r.id: r for r in (earlier, older, other)}, lock=None)
        ctx, _ = context(goal="Now reply to her: see you at 7", thread="3f9c2a1b7d0e", runtime=runtime)
        self.assertEqual(memory_provider.thread_apps(ctx), ("com.apple.MobileSMS",))
        (block,) = memory_provider.factory(ctx).blocks(ctx)
        self.assertEqual(block.text, "- In Messages: Sign my texts with – Sam")
        ctx, _ = context(goal="Now reply to her: see you at 7", runtime=runtime)
        self.assertEqual(memory_provider.factory(ctx).blocks(ctx), ())


class PromptTests(unittest.TestCase):
    """The block sits in the cached part of the frontier's prompt, after the task's own text (S7.1)."""

    def test_the_block_is_in_the_cached_prompt(self):
        from mobile_agent.tests.test_seam_prompts import GOLDEN, environment, record_all
        block = harness_api.ContextBlock("memory", retrieval.TITLE, "- My gym is the one on 5th Street")
        with patch.dict(os.environ, environment(), clear=True):
            now = record_all(plain={"context_blocks": (block,)})
        golden = json.loads(GOLDEN.read_text())
        first, before = now["plain"]["decisions"][0]["messages"], golden["plain"]["decisions"][0]["messages"]
        self.assertEqual(first[0], before[0])
        self.assertTrue(first[1]["content"][0]["cache"])
        self.assertEqual(first[1]["content"][0]["text"], before[1]["content"][0]["text"] + "\n\n" + retrieval.TITLE
                         + ":\n- My gym is the one on 5th Street")
        self.assertEqual(first[1]["content"][1:], before[1]["content"][1:])


if __name__ == "__main__":
    unittest.main()

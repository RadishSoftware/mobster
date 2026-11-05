import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mobile_agent.packs import core, heal, health, register, tools
from mobile_agent.packs import instagram as ig
from mobile_agent.tests.packs_support import Phone, el, rows_file, snap

B = ig.BUNDLE
DMS = "Direct messages , 6 unread messages"


def tabs(selected):
    return [el(label, "Button", (x, .9, .2, .06), **({"selected": True, "value": "1"} if label == selected else {}))
            for label, x in (("Main feed", 0), ("Reels", .2), (DMS, .4), ("Explore", .6), ("Profile", .8))]


REDESIGNED = snap(B, [
    el("Chats", "StaticText", (.4, .07, .2, .03)), el("Compose", "Button", (.86, .07, .1, .03)),
    el("Find a chat", "SearchField", (.05, .13, .9, .04), editable=True),
    el("All", "Button", (.05, .2, .12, .03)), el("Unread", "Button", (.2, .2, .15, .03)),
    el("Groups", "Button", (.38, .2, .15, .03)),
    el("Sam Lee. 4 new messages. 3h", "Cell", (0, .3, 1, .08)),
    el("Riley. haha see you then. 5h", "Cell", (0, .38, 1, .08)),
    *tabs(DMS)])


def phone():
    screens = {"feed": rows_file("instagram/feed.json", B), "redesigned": REDESIGNED}
    return Phone(screens, {("feed", "TAP", DMS): "redesigned", ("redesigned", "TAP", "Main feed"): "feed"}, "feed")


class HealTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(setattr, ig.PACK, "learned", None)
        self.addCleanup(heal._stores.clear)

    def run_open(self, run_id, device=None):
        device = device or phone()
        events = []
        run = SimpleNamespace(run_id=run_id, data_dir=Path(self.folder.name), cancelled=lambda: False, runtime=None)
        ctx = SimpleNamespace(driver=device, emit=events.append, guard=None, run=run)
        tool = tools.PackTool(ig.PACK, "OPEN", run, sleep=lambda s: None)
        result = tool.perform(ctx, tool.prepare(ctx, {"text": "dms"}, device.observe()), {}, device.observe())
        return result, events, device

    def test_drift_hands_over_proposes_confirms_and_heals_on_replay(self):
        self.assertIsNone(ig.PACK.recognize(REDESIGNED).screen)
        result, events, _ = self.run_open("run-1")
        self.assertIn("Go on the usual way", result.feedback)
        drift = next(e for e in events if e["event"] == "app_pack")
        self.assertEqual((drift["pack"], drift["ok"], drift["unsure_screen"]), ("instagram", False, "dm_inbox"))
        self.assertTrue(drift["fingerprint"])
        from mobile_agent import hints
        self.assertFalse(hints.recent(Path(self.folder.name), source="pack")[-1]["ok"])
        store = heal.store_for(Path(self.folder.name))
        variant = store.variants[0]
        self.assertEqual((variant.screen, variant.status), ("dm_inbox", "proposed"))
        words = [s["pattern"] for s in variant.signals]
        self.assertIn("^Compose$", words)
        for personal in ("Sam Lee", "Riley", "3h"):
            self.assertFalse(any(personal in w for w in words), words)
        register.Confirmer().on_finished(SimpleNamespace(id="run-1"), {"status": "completed"}, {})
        self.assertEqual(store.variants[0].status, "confirmed")
        self.assertIsNone(ig.PACK.recognize(REDESIGNED).screen)
        result, events, _ = self.run_open("run-2")
        self.assertIn("Now on your DMs", result.feedback)
        self.assertTrue(next(e for e in events if e["event"] == "app_pack")["healed"])
        self.assertEqual(store.variants[0].status, "trusted")
        seen = ig.PACK.recognize(REDESIGNED)
        self.assertEqual((seen.screen, seen.evidence), ("dm_inbox", "learned"))
        self.assertEqual(ig.PACK.recognize(rows_file("instagram/feed.json", B)).screen, "feed")

    def test_a_proposal_from_a_run_that_didnt_finish_is_never_trusted(self):
        self.run_open("run-a")
        register.Confirmer().on_finished(SimpleNamespace(id="run-a"), {"status": "approval_denied"}, {})
        result, events, _ = self.run_open("run-b")
        self.assertIn("Go on the usual way", result.feedback)
        self.assertTrue(all(v.status == "proposed" for v in heal.store_for(Path(self.folder.name)).variants))
        self.assertIsNone(ig.PACK.recognize(REDESIGNED).screen)

    def test_a_variant_that_also_names_another_screen_fails_its_check(self):
        store = heal.Learned(None)
        store.propose(ig.PACK, REDESIGNED, "dm_inbox", "448.0.0", "r1")
        store.confirm_run("r1")
        self.assertEqual(store.check(ig.PACK, rows_file("instagram/feed.json", B), "dm_inbox", "448.0.0"), [])
        self.assertEqual(store.variants[0].status, "confirmed")

    def test_learned_variants_never_find_an_acts_control(self):
        store = heal.Learned(None)
        store.propose(ig.PACK, REDESIGNED, "dm_inbox", "", "r1")
        store.confirm_run("r1")
        store.check(ig.PACK, REDESIGNED, "dm_inbox", "")
        ig.PACK.learned = store
        self.assertIsNone(core.control_for(ig.PACK, "like", REDESIGNED))
        self.assertIsNone(core.control_for(ig.PACK, "reply", REDESIGNED))

    def test_health_says_which_screen_changed_and_recovers(self):
        self.run_open("run-x")
        entries = health.recent(Path(self.folder.name))
        state = health.status(ig.PACK, "448.0.0", entries)
        self.assertEqual(state["summary"], "Instagram pack: working on 448.0.0; 1 screen changed")
        self.assertEqual(state["changedScreens"][0]["screen"], "dm_inbox")
        health.record(Path(self.folder.name), {"pack": "instagram", "ok": True, "screen": "dm_inbox", "step": "dms"})
        state = health.status(ig.PACK, "448.0.0", health.recent(Path(self.folder.name)))
        self.assertEqual(state["summary"], "Instagram pack: working on 448.0.0")

    def test_a_screen_may_have_several_variants(self):
        class Two(core.Pack):
            name, key, bundle = "Two", "two", "com.example.two"
            screens = (core.ScreenSpec("home", "home", (core.sig(r"^Home$", must=True), core.sig(r"^Feed$"))),
                       core.ScreenSpec("home", "home", (core.sig(r"^Start$", must=True), core.sig(r"^Feed$"))))
        pack = Two()
        for label in ("Home", "Start"):
            screen = snap("com.example.two", [el(label, "StaticText"), el("Feed", "StaticText", (0, .2, .2, .05))])
            self.assertEqual(pack.recognize(screen).screen, "home", label)


class WatchTests(unittest.TestCase):
    def test_the_health_run_visits_tabs_read_only_and_ends_with_a_checklist(self):
        from mobile_agent.tests.test_packs_instagram import phone as ig_phone
        device = ig_phone("feed")
        with tempfile.TemporaryDirectory() as folder:
            ctx = SimpleNamespace(extras={"packCheck": "instagram"}, cancelled=lambda: False, emit=lambda e: None,
                                  runtime=None, run_id="check", data_dir=Path(folder))
            from unittest.mock import patch
            with patch.object(register, "SLEEP", lambda s: None):
                done = register.pack_check(ctx, device, None)
            entries = health.recent(Path(folder))
        result = done.result
        self.assertEqual(result["costUsd"], 0.0)
        block = result["blocks"]["blocks"][0]
        self.assertEqual(block["type"], "checklist")
        routes = [r.name for r in health.tab_routes(ig.PACK)]
        self.assertEqual(len(block["items"]), len(routes))
        self.assertIn("dms", routes)
        self.assertNotIn("here", routes)
        self.assertTrue(all(e["op"] == "CHECK" for e in entries))
        for label in device.taps:
            self.assertNotRegex(label or "", r"^(Like|Follow|Send|Save post|Post comment)$")
        with self.assertRaises(ValueError):
            register.validate_pack_key("packCheck", "myspace", None)


if __name__ == "__main__":
    unittest.main()

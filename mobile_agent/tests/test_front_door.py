import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import harness_api
from mobile_agent.packs import front_door as door
from mobile_agent.packs import health, tools
from mobile_agent.packs import settings as st
from mobile_agent.tests import test_packs_instagram as igt
from mobile_agent.tests.packs_support import Phone, el, rows_file, snap

IG, TT, SMS, CAL = "com.burbn.instagram", "com.zhiliaoapp.musically", "com.apple.MobileSMS", "com.apple.mobilecal"
ROUTED = {
    "What iOS version is this phone on?": ("settings", "read", "about"),
    "what ios version": ("settings", "read", "about"),
    "Can you tell me what iOS version my iPhone is running, please": None,
    "how much storage do I have left": ("settings", "read", "storage"),
    "what's my battery level": ("settings", "read", "battery"),
    "open Settings › General › About": ("settings", "open", "about"),
    "go to Settings > Battery": ("settings", "open", "battery"),
    "open settings": ("settings", "open", "root"),
    "Open Settings > General": ("settings", "open", "find"),
    "show my last 10 instagram DMs": ("instagram", "read", "dms"),
    "can you tell me my last 5 tiktok messages please": ("tiktok", "read", "dms"),
}
STEPPED_ASIDE = (
    "send a DM to Sam",
    "like my last 10 instagram DMs",
    "turn on low power mode",
    "what iOS version should I update to",
    "what iOS version and how much storage",
    "instagram dms and tiktok dms",
    "my last 10 DMs",
    "what model are you",
    "open Settings › Privacy & Security › About",
    "open Settings › Nonsense",
    "open Spotify",
    "what iOS version 26 is this",
    "what iOS version " + "is this " * 40,
    "",
)


def ctx(goal, **over):
    values = dict(extras={}, cancelled=lambda: False, runtime=None, run_id="r-door",
                  data_dir=Path(tempfile.gettempdir()),
                  origin="app", engine="smart", goal=goal, app_bundle=None, device_id=None, events=[])
    values.update(over)
    values["emit"] = values["events"].append
    return SimpleNamespace(**values)


def settings_phone(start="about"):
    names = ("root", "about", "storage", "battery", "search-results")
    screens = {name: rows_file(f"settings/{name}.json", st.BUNDLE) for name in names}
    screens["springboard"] = snap("com.apple.springboard", [el("Settings", "Icon", (.1, .1, .2, .1))])
    results = snap(st.BUNDLE, [el("Search", "SearchField", (.08, .92, .83, .05), value="General", editable=True),
                               el("Clear text", rect=(.71, .92, .06, .03)),
                               el("General, Settings", rect=(.04, .14, .92, .07))])
    screens["results"] = results
    screens["general"] = rows_file("settings/general.json", st.BUNDLE)
    moves = {("*", "LAUNCH_APP", st.BUNDLE): "root", ("root", "TYPE", "Search"): "results",
             ("results", "TAP", "General, Settings"): "general"}
    return Phone(screens, moves, start)


class Base(unittest.TestCase):
    def setUp(self):
        tools._runs.clear()
        patcher = patch("mobile_agent.packs.register.SLEEP", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(tools._runs.clear)


class GrammarTests(Base):
    def test_a_request_that_is_exactly_an_ask_is_routed(self):
        for goal, want in ROUTED.items():
            with self.subTest(goal=goal):
                plan = door.parse(goal, app_bundle=None, front_bundle=None)
                self.assertEqual(plan and (plan.pack.key, plan.kind, plan.what), want)

    def test_the_count_in_the_words_is_the_count_read(self):
        plan = door.parse("my last 10 DMs", app_bundle=IG)
        self.assertEqual((plan.pack.key, plan.what, plan.n), ("instagram", "dms", 10))
        self.assertEqual(door.parse("instagram dms", app_bundle=None).n, 15)
        self.assertIsNone(door.parse("my last 500 dms", app_bundle=IG))
        self.assertIsNone(door.parse("my last 0 dms", app_bundle=IG))

    def test_the_app_a_run_started_in_or_that_is_in_front_names_a_bare_request(self):
        self.assertEqual(door.parse("my DMs", app_bundle=TT).pack.key, "tiktok")
        self.assertEqual(door.parse("my DMs", app_bundle=None, front_bundle=IG).pack.key, "instagram")
        self.assertIsNone(door.parse("my DMs", app_bundle=None, front_bundle="com.apple.mobilesafari"))
        self.assertEqual(door.parse("my last 5 texts", app_bundle=SMS).what, "inbox")
        self.assertEqual(door.parse("upcoming events", app_bundle=None, front_bundle=CAL).what, "events")
        self.assertEqual(door.parse("tiktok dms", app_bundle=IG).pack.key, "tiktok")
        self.assertIsNone(door.parse("my unread texts", app_bundle=IG))

    def test_everything_unsure_or_acting_steps_aside(self):
        for goal in STEPPED_ASIDE:
            with self.subTest(goal=goal):
                self.assertIsNone(door.parse(goal, app_bundle=None, front_bundle=None))

    def test_an_act_word_stops_every_ask(self):
        for word in sorted(door.ACTS):
            self.assertIsNone(door.parse(f"{word} my last 5 instagram dms", app_bundle=IG), word)
            self.assertIsNone(door.parse(f"what ios version {word}", app_bundle=IG), word)

    def test_every_ask_names_a_reader_its_pack_has(self):
        for ask in door.ASKS:
            pack = next(p for p in door.registered() if p.key == ask.pack)
            self.assertIn(ask.reader, pack.readers, ask)
            self.assertTrue(all(isinstance(words, frozenset) and words for words in ask.words), ask)


class ReadTests(Base):
    def test_the_ios_version_is_read_off_the_about_screen_with_no_model(self):
        device = settings_phone("about")
        done = door.hook(ctx("What iOS version is this phone on?"), device, None)
        result = done.result
        self.assertEqual((result["status"], result["outcome"], result["costUsd"], result["model"]),
                         ("completed", "done", 0.0, None))
        self.assertIn("iOS Version: 26.0.1", result["answer"])
        self.assertNotIn("Model Name", result["answer"])
        self.assertEqual(device.taps, [])

    def test_storage_reports_used_capacity_and_free_as_the_page_shows_them(self):
        done = door.hook(ctx("how much storage do I have left"), settings_phone("storage"), None)
        answer = done.result["answer"]
        for line in ("Used: 125.51 GB", "Capacity: 128 GB", "Free: 2.49 GB"):
            self.assertIn(line, answer)

    def test_dms_come_back_as_rows_and_a_card(self):
        device = igt.phone("dm-inbox")
        done = door.hook(ctx("my last 3 DMs", app_bundle=IG), device, None)
        result = done.result
        self.assertEqual((result["status"], result["model"], result["costUsd"]), ("completed", None, 0.0))
        self.assertEqual(len(result["data"]["rows"]), 3)
        self.assertEqual(result["data"]["rows"][0]["from"], "Sam Lee")
        self.assertIn("Sam Lee", result["answer"])
        self.assertTrue(result["blocks"])
        self.assertEqual(len(result["answer"].splitlines()), 4)
        self.assertFalse([t for t in device.taps if "Delete" in t or t.startswith("Unread")])

    def test_it_navigates_from_the_home_screen_the_way_the_pack_does(self):
        device = igt.phone("springboard")
        done = door.hook(ctx("my last 3 DMs", app_bundle=IG), device, None)
        self.assertEqual(done.result["status"], "completed")
        self.assertIn(("LAUNCH_APP", IG, None), device.actions)

    def test_the_journal_gets_the_acting_event_before_the_first_move_and_the_pack_event(self):
        c = ctx("my last 3 DMs", app_bundle=IG)
        door.hook(c, igt.phone("dm-inbox"), None)
        kinds = [(e["event"], e.get("op")) for e in c.events]
        self.assertEqual(kinds[0], ("skill_started", "IG_READ"))
        pack_event = next(e for e in c.events if e["event"] == "app_pack")
        self.assertTrue(pack_event["ok"] and pack_event["door"] and pack_event["how"] == "code")


class OpenTests(Base):
    def test_a_settings_page_is_opened_through_settings_search_and_named_as_it_is(self):
        device = settings_phone("springboard")
        done = door.hook(ctx("open Settings > General"), device, None)
        self.assertEqual(done.result["status"], "completed")
        self.assertIn("Opened General in Settings", done.result["answer"])
        self.assertIsNone(done.result["data"])
        self.assertEqual(device.taps, ["General, Settings"])
        self.assertEqual(device.actions[0][0], "LAUNCH_APP")

    def test_settings_alone_goes_to_the_root(self):
        done = door.hook(ctx("go to Settings"), settings_phone("root"), None)
        self.assertEqual(done.result["status"], "completed")


class StepAsideTests(Base):
    def test_nothing_unsure_touches_the_phone(self):
        for goal in STEPPED_ASIDE:
            device = settings_phone("about")
            with self.subTest(goal=goal):
                self.assertIsNone(door.hook(ctx(goal), device, None))
                self.assertEqual(device.actions, [])

    def test_act_words_never_reach_a_pack_even_in_the_app(self):
        device = igt.phone("dm-inbox")
        for goal in ("reply to my last 3 DMs", "delete my last 3 DMs", "send my last 3 DMs to Sam"):
            self.assertIsNone(door.hook(ctx(goal, app_bundle=IG), device, None), goal)
        self.assertEqual(device.actions, [])

    def test_a_run_the_table_was_not_written_for_goes_to_the_model(self):
        device = settings_phone("about")
        for over in ({"extras": {"workflowInput": "x"}}, {"extras": {"sendTo": {"a": 1}}},
                     {"extras": {"replay": "off"}},
                     {"engine": "claude-code"}):
            with self.subTest(over=over):
                self.assertIsNone(door.hook(ctx("what ios version", **over), device, None))
        self.assertIsNone(door.hook(ctx("what ios version"), None, None))
        self.assertEqual(device.actions, [])
        done = door.hook(ctx("what ios version", extras={"threadId": "t1", "permissionMode": "ask"}), device, None)
        self.assertIsNotNone(done)

    def test_a_structured_output_or_a_pinned_app_needs_the_model(self):
        device = settings_phone("about")
        for run in (SimpleNamespace(output_schema={"type": "object"}, output_format="json", longform=None,
                                    pinned_apps=()),
                    SimpleNamespace(output_schema=None, output_format="csv", longform=None, pinned_apps=()),
                    SimpleNamespace(output_schema=None, output_format="auto", longform={"plan": {}}, pinned_apps=())):
            with self.subTest(run=run):
                self.assertIsNone(door.hook(ctx("what ios version", runtime=SimpleNamespace(
                    runs={"r-door": run}, installed_apps=None)), device, None))
        plain = SimpleNamespace(output_schema=None, output_format="auto", longform=None, pinned_apps=())
        self.assertIsNotNone(door.hook(ctx("what ios version", runtime=SimpleNamespace(
            runs={"r-door": plain}, installed_apps=None)), device, None))

    def test_the_switch_turns_it_off(self):
        with patch.dict("os.environ", {"MOBSTER_FRONT_DOOR": "off"}):
            device = settings_phone("about")
            self.assertIsNone(door.hook(ctx("what ios version"), device, None))
            self.assertEqual(device.actions, [])

    def test_lost_mid_route_hands_the_phone_to_the_model_and_counts_as_a_miss(self):
        only_feed = Phone({"feed": igt.screen("feed")}, {}, "feed")
        c = ctx("my last 3 DMs", app_bundle=IG)
        with patch.object(health, "record") as recorded:
            self.assertIsNone(door.hook(c, only_feed, None))
        miss = next(e for e in c.events if e["event"] == "app_pack")
        self.assertFalse(miss["ok"])
        self.assertTrue(miss["why"])
        self.assertTrue(any(e["event"] == "front_door" and e["routed"] is False for e in c.events))
        self.assertFalse(recorded.call_args.args[1]["ok"])
        self.assertEqual(tools.run_gate("r-door").misses[("pack", IG)], 1)

    def test_two_misses_in_a_run_turn_the_pack_off_for_the_run(self):
        only_feed = Phone({"feed": igt.screen("feed")}, {}, "feed")
        for _ in range(2):
            door.hook(ctx("my last 3 DMs", app_bundle=IG), only_feed, None)
        before = len(only_feed.actions)
        c = ctx("my last 3 DMs", app_bundle=IG)
        self.assertIsNone(door.hook(c, igt.phone("dm-inbox"), None))
        self.assertEqual(len(only_feed.actions), before)
        self.assertIn("missed 2 times", next(e["why"] for e in c.events if e["event"] == "front_door"))

    def test_a_pick_the_screen_does_not_hold_is_lost_not_guessed(self):
        blank = snap(st.BUNDLE,
                     [el("Battery", "NavigationBar", (0, .07, 1, .06)), el("Battery Health", rect=(.04, .5, .5, .05))])
        c = ctx("what's my battery level")
        self.assertIsNone(door.hook(c, Phone({"battery": blank}, {}, "battery"), None))
        self.assertFalse(next(e for e in c.events if e["event"] == "app_pack")["ok"])


class RegisteredTests(Base):
    def test_the_hook_is_a_pre_run_hook_that_runs_before_the_packs_and_the_learned_paths(self):
        from mobile_agent import tracks
        from mobile_agent.tests.seam_support import isolate
        isolate(self)
        tracks.load()
        hooks = harness_api.pre_runs(ctx("x"))
        self.assertIn(door.hook, hooks)
        from mobile_agent.packs import register
        self.assertLess(hooks.index(door.hook), hooks.index(register.pack_read))
        self.assertLess(door.ORDER, 20)


if __name__ == "__main__":
    unittest.main()

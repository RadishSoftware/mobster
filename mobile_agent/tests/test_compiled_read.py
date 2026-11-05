"""Compiled reading (MOBSTER_EXPERIMENTAL_COMPILED_READ): learned extractors, tree diffs and the route corpus.
Fake drivers and the scripted demo phone only: no phone, simulator, model or network."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent import compiled_read
from mobile_agent.compiled_read import delta, extractors, routes
from mobile_agent.frontier import FrontierAgent
from mobile_agent.frontier.screen import screen_rows
from mobile_agent.learned import programs as P
from mobile_agent.state import Element
from mobile_agent.tests.test_app_api import SHOP, FakeModel, Harness, fake_toolset, shop_phone
from mobile_agent.tests.test_frontier import Driver, Script, screen
from mobile_agent.tui import demo_phone as demo

ROWS = [{"title": "Rye loaf", "price": "$6.50", "rating": 4.8}, {"title": "Sourdough", "price": "$8.00", "rating": 4.6}]
LINES = [("StaticText", "Deals"), ("Cell", "Rye loaf, $6.50, 4.8 stars"), ("Cell", "Sourdough, $8.00, 4.6 stars"),
         ("TabBar", "Home")]
FIELDS = ["title", "price", "rating"]


def stages(bundle, goal, *names, typed="Running late"):
    shown = demo.Screens(bundle, goal)
    out = []
    for name in names:
        elements, text = shown.build(name, typed)
        out.append(demo.Snapshot(elements, text, 390, 844, demo.SOURCE, bundle_id=bundle))
    return out


class FlagTests(unittest.TestCase):
    def test_off_unless_asked_for(self):
        self.assertFalse(compiled_read.enabled({}))
        self.assertFalse(compiled_read.enabled({compiled_read.ENV_FLAG: "0"}))
        self.assertTrue(compiled_read.enabled({compiled_read.ENV_FLAG: "1"}))

    def test_the_agent_makes_no_tracker_with_the_flag_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(compiled_read.ENV_FLAG, None)
            self.assertIsNone(delta.for_agent(type("A", (), {})()))


class ExtractorTests(unittest.TestCase):
    def test_a_same_shaped_screen_is_read_by_code_with_other_values(self):
        made = extractors.learn(LINES, ROWS, FIELDS)
        other = [("StaticText", "Deals"), ("Cell", "Bagels, $4.00, 4.9 stars"), ("Cell", "Rye, $5, 3 stars"),
                 ("TabBar", "Home")]
        self.assertEqual(extractors.extract(made, other, FIELDS),
                         [{"title": "Bagels", "price": "$4.00", "rating": 4.9},
                          {"title": "Rye", "price": "$5", "rating": 3}])

    def test_a_changed_screen_is_a_miss(self):
        made = extractors.learn(LINES, ROWS, FIELDS)
        self.assertIsNone(extractors.extract(made, LINES + [("Cell", "More, $1, 1 stars")], FIELDS))       # a row more
        self.assertIsNone(extractors.extract(made, [("Other", "Deals"), *LINES[1:]], FIELDS))             # a role
        self.assertIsNone(extractors.extract(made, [("StaticText", "Sale"), *LINES[1:]], FIELDS))         # a header
        self.assertIsNone(extractors.extract(made, [*LINES[:1], ("Cell", "Rye loaf, $6.50"), LINES[2],
                                                    LINES[3]], FIELDS))                                   # a part gone
        self.assertIsNone(extractors.extract(made, [*LINES[:1], ("Cell", "Rye, $5, many stars"), LINES[2],
                                                    LINES[3]], FIELDS))                                   # not a number

    def test_digits_in_a_static_line_do_not_change_the_skeleton(self):
        lines = [("StaticText", "9:41"), ("Cell", "Rye loaf")]
        made = extractors.learn(lines, [{"title": "Rye loaf"}], ["title"])
        self.assertEqual(extractors.extract(made, [("StaticText", "10:02"), ("Cell", "Pie")], ["title"]),
                         [{"title": "Pie"}])

    def test_a_reworded_or_missing_value_is_never_learned(self):
        self.assertIsNone(extractors.learn(LINES, [{"title": "Rye bread", "price": None, "rating": None}], FIELDS))
        self.assertIsNone(extractors.learn(LINES, [], FIELDS))
        self.assertIsNone(extractors.learn(LINES, [{"title": True, "price": None, "rating": None}], FIELDS))

    def test_nulls_and_whole_numbers_keep_their_kind(self):
        lines = [("Cell", "Eggs"), ("Cell", "12")]
        made = extractors.learn(lines, [{"name": "Eggs", "count": 12}, ], ["name", "count"])
        self.assertEqual(extractors.extract(made, [("Cell", "Milk"), ("Cell", "3")], ["name", "count"]),
                         [{"name": "Milk", "count": 3}])
        self.assertEqual(extractors.learn([("Cell", "Eggs")], [{"name": "Eggs", "count": None}],
                                          ["name", "count"])["rows"], [[[0, None], None]])

    def test_the_store_keeps_no_screen_text_and_counts_hits(self):
        folder = Path(tempfile.mkdtemp()) / "extractors"
        store = extractors.Extractors(folder)
        self.assertIsNone(store.read(SHOP, FIELDS, LINES))
        self.assertTrue(store.remember(SHOP, FIELDS, LINES, ROWS))
        again = [("StaticText", "Deals"), ("Cell", "Pie, $3, 5 stars"), ("Cell", "Tart, $4, 4 stars"),
                 ("TabBar", "Home")]
        self.assertEqual(store.read(SHOP, FIELDS, again)[0]["title"], "Pie")
        self.assertIsNone(store.read(SHOP, FIELDS, LINES[:2]))
        self.assertEqual(store.counts(SHOP, FIELDS), {"hits": 1, "misses": 1})
        [file] = folder.glob("*.json")
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        text = file.read_text()
        for word in ("Rye", "Sourdough", "6.50", "Deals", "Home"):
            self.assertNotIn(word, text)
        self.assertEqual(json.loads(text)["identity"]["kind"], "extractor")

    def test_a_skeleton_is_kept_once_and_the_newest_first(self):
        store = extractors.Extractors(Path(tempfile.mkdtemp()))
        for count in (1, 2, 3, 2):
            lines = [("Cell", f"r{n}") for n in range(count)]
            store.remember("a.b", ["x"], lines, [{"x": "r0"}])
        path = store.cache.load(store.identity("a.b", ["x"]), store.env())
        self.assertEqual([len(e["skeleton"]) for e in path["extractors"]], [2, 3, 1])


class ReadScreenTests(Harness):
    def setUp(self):
        super().setUp()
        self.toolset, self.session, self.permits = fake_toolset(self.device, self.folder)
        self.toolset.runs_dir = Path(self.folder) / "runs"
        patcher = patch.dict(os.environ, {compiled_read.ENV_FLAG: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, model, query="n=5"):
        self.device = shop_phone()
        self.toolset, _, _ = fake_toolset(self.device, self.folder)
        self.toolset.runs_dir = Path(self.folder) / "runs"
        return self.get(f"/v1/screen?app={SHOP}&fields=title,price,rating&{query}",
                        api=self.api(toolset=self.toolset, client_factory=lambda: model)).json

    def test_the_second_read_of_a_shape_needs_no_model(self):
        first = self.read(FakeModel([ROWS]))
        self.assertEqual((first["compiled"], first["model"]), ({"hits": 0, "misses": 1}, "claude-haiku-5-5"))
        second_model = FakeModel([])
        second = self.read(second_model)
        self.assertEqual(second["rows"], first["rows"])
        self.assertEqual((second["compiled"], second["model"], second["cost_usd"]),
                         ({"hits": 1, "misses": 0}, "extractor", 0.0))
        self.assertEqual(second_model.sent, [])

    def test_no_key_reads_a_known_shape_but_not_a_new_one(self):
        self.read(FakeModel([ROWS]))
        self.assertEqual(self.read(None)["compiled"], {"hits": 1, "misses": 0})
        self.device = shop_phone()
        self.toolset, _, _ = fake_toolset(self.device, self.folder)
        self.toolset.runs_dir = Path(self.folder) / "elsewhere"
        answer = self.get(f"/v1/screen?app={SHOP}&fields=title&n=5",
                          api=self.api(toolset=self.toolset, client_factory=lambda: None))
        self.assertEqual((answer.status, answer.json["code"]), (503, "no_model"))

    def test_a_full_page_is_not_learned_as_the_whole_screen(self):
        self.read(FakeModel([ROWS]), "n=2")             # n rows came back: the screen may hold more
        again = FakeModel([ROWS])
        self.assertEqual(self.read(again, "n=2")["compiled"], {"hits": 0, "misses": 1})
        self.assertEqual(len(again.sent), 1)

    def test_a_changed_screen_goes_back_to_the_model(self):
        self.read(FakeModel([ROWS]))
        model = FakeModel([[{"title": "Rye loaf", "price": "$6.50", "rating": 4.8}]])
        body = self.get(f"/v1/screen?app={SHOP}&fields=title,price,rating&n=5&pages=2",
                        api=self.api(toolset=self.toolset, client_factory=lambda: model)).json
        self.assertEqual(body["compiled"]["hits"], 1)    # page 1 by code; page 2 (one row) by the model
        self.assertEqual(body["compiled"]["misses"], 1)

    def test_with_the_flag_off_the_answer_is_what_it_was(self):
        del os.environ[compiled_read.ENV_FLAG]
        body = self.read(FakeModel([ROWS]))
        self.assertNotIn("compiled", body)
        self.assertFalse((Path(self.folder) / "replays").exists())


def rows_of(snapshot):
    return screen_rows(snapshot)[1]


class DeltaTests(unittest.TestCase):
    def setUp(self):
        # The demo screens are 6 to 10 elements; a real screen has up to 220, where the threshold is easy to meet.
        patcher = patch.object(delta, "MAX_RATIO", 1.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sequence(self):
        return [rows_of(s) for s in stages(demo.MESSAGES, "text Alex", "thread", "typed", "sent")]

    def test_the_first_screen_goes_whole_and_a_small_change_goes_as_a_diff(self):
        tracker = delta.Delta()
        thread, typed, sent = self.sequence()
        self.assertEqual(tracker.screen("app", thread, 1), (None, thread))
        reference, body = tracker.screen("app", typed, 2)
        self.assertEqual(reference, thread)
        self.assertIn("as in the reference", "\n".join(body))
        self.assertTrue(any('"Send"' in line for line in body))         # the new row, whole, under its alias
        self.assertLess(len("\n".join(body)), len("\n".join(typed)))
        self.assertTrue(any("value=" in line for line in body))         # the changed field, whole

    def test_every_element_is_still_listed_so_an_alias_means_one_thing(self):
        tracker = delta.Delta()
        thread, typed, _ = self.sequence()
        tracker.screen("app", thread, 1)
        _, body = tracker.screen("app", typed, 2)
        aliases = set()
        for line in body[1:]:
            head = line.split(" ")[0]
            if "-" in head:
                lo, hi = head[1:].split("-")
                aliases.update(range(int(lo), int(hi[1:]) + 1))
            else:
                aliases.add(int(head[1:]))
        self.assertEqual(aliases, set(range(1, len(typed) + 1)))

    def test_moved_rows_name_their_reference_alias(self):
        tracker = delta.Delta()
        lines = [f'e{n + 1} Cell "a row with a long label, number {n}" value="x" @0,{n},100,5' for n in range(10)]
        tracker.screen("app", lines, 1)
        shifted = ['e1 Cell "new" @0,0,100,5'] + [f"e{n + 2}" + line[line.index(" "):] for n, line in enumerate(lines)]
        _, body = tracker.screen("app", shifted, 2)
        self.assertEqual(body[1:4], ['e1 Cell "new" @0,0,100,5', "e2 = reference e1", "e3 = reference e2"])

    def test_a_diff_longer_than_the_threshold_is_sent_whole(self):
        lines = [f'e{n + 1} Cell "row number {n}" @0,{n},100,5' for n in range(60)]
        changed = [line if n % 3 else line.replace("row number", "row nr") for n, line in enumerate(lines)]
        few = [line if n != 7 else line + " selected" for n, line in enumerate(changed)]
        with patch.object(delta, "MAX_RATIO", .6):
            tracker = delta.Delta()
            tracker.screen("a", lines, 1)
            self.assertEqual(tracker.screen("a", changed, 2), (None, changed))      # a third of the rows changed
            reference, body = tracker.screen("a", few, 3)
        self.assertEqual(reference, changed)
        self.assertLess(len(body), 6)

    def test_a_new_app_a_scroll_or_a_big_diff_is_sent_whole_and_becomes_the_reference(self):
        tracker = delta.Delta()
        top, scrolled = [rows_of(s) for s in stages(demo.SETTINGS, "x", "top", "scrolled")]
        tracker.screen("a", top, 1)
        self.assertEqual(tracker.screen("b", top, 2), (None, top))      # another app
        self.assertEqual(tracker.screen("b", scrolled, 3), (None, scrolled))
        self.assertEqual(tracker.turn, 3)
        self.assertEqual(tracker.stats["full"], 3)

    def test_the_cost_of_a_reference_is_counted_once_then_as_cache_reads(self):
        tracker = delta.Delta()
        thread, typed, sent = self.sequence()
        tracker.screen("a", thread, 1)
        tracker.screen("a", typed, 2)
        first = dict(tracker.stats)
        tracker.screen("a", typed, 3)
        self.assertEqual(tracker.stats["tokens_cached_read"], delta.tokens("\n".join(thread)))
        self.assertEqual(first["tokens_cached_read"], 0)
        self.assertGreater(first["tokens_new"], first["tokens_full"] - delta.tokens("\n".join(typed)))



class PromptTests(unittest.TestCase):
    """The agent's own prompt, over the fake driver of test_frontier_prompt."""

    def rows(self, last):
        names = ["General", "Privacy", "Battery", "Wallet", "Siri", "Display", "Sounds", last]
        return screen(*[Element(str(n + 1), label, "Button", (0, .05 + n * .06, 1, .05))
                        for n, label in enumerate(names)])

    def setUp(self):
        patcher = patch.object(delta, "MAX_RATIO", 1.0)    # eight-row screens: the threshold is for real ones
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_agent(self):
        screens = [self.rows(label) for label in ("About", "About", "Fonts", "Fonts")]
        steps = [("TAP", "General", None), ("TAP", "Privacy", None), ("TAP", "Battery", None), ("DONE", None, None)]
        events = []
        result = FrontierAgent(Driver(screens), Script(steps), emit=events.append, screenshots=False,
                               settle_seconds=0).run("Open it.")
        return [e["text"] for e in events if e["event"] == "frontier_prompt"], result

    def test_off_the_prompt_and_the_result_are_what_they_were(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(compiled_read.ENV_FLAG, None)
            texts, result = self.run_agent()
        self.assertNotIn("Reference screen", "".join(texts))
        self.assertNotIn("compiled_read", result)

    def test_on_a_later_turn_names_changes_against_a_reference_that_is_cached(self):
        with patch.dict(os.environ, {compiled_read.ENV_FLAG: "1"}):
            texts, result = self.run_agent()
        self.assertNotIn("Reference screen", texts[0])
        self.assertIn("Reference screen (turn", texts[1])
        self.assertIn("as in the reference", texts[1].split("Screen elements", 1)[-1])
        stats = result["compiled_read"]["delta"]
        self.assertEqual((stats["full"], stats["delta"] >= 1), (1, True))
        self.assertLess(stats["tokens_new"], stats["tokens_full"] + stats["tokens_cached_read"])


class RouteTests(unittest.TestCase):
    def store(self, version):
        return P.Programs(Path(self.folder), version=version)

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.steps = [{"op": "TAP", "app": "com.apple.Preferences", "target": {"label": "General", "role": "Cell",
                                                                              "id": None, "frame": [0, .1, 1, .05]},
                       "screen": {"hash": "abc", "shape": ["12345678"]}, "text": None, "param": None, "commit": None,
                       "point": None, "live": False},
                      {"op": "TAP", "app": "com.apple.Preferences", "target": {"label": "About", "role": "Cell",
                                                                              "id": None, "frame": [0, .1, 1, .05]},
                       "screen": {"hash": "def", "shape": ["87654321"]}, "text": None, "param": None, "commit": None,
                       "point": None, "live": False}]
        self.goal = "Open General then About in the Settings app"

    def save(self, version):
        return self.store(version).save("com.apple.Preferences", self.goal, "phone", self.steps, [], run_id="r")

    def candidates(self, version):
        return self.store(version).candidates("com.apple.Preferences", "phone", self.goal)

    def test_a_route_is_offered_for_its_own_version_only(self):
        self.save("1.0")
        self.assertEqual(len(self.candidates("1.0")), 1)
        self.assertEqual(self.candidates("2.0"), [])
        self.assertEqual(len(self.candidates(None)), 1)                  # the flag off or the version unknown

    def test_two_versions_keep_two_routes_and_a_route_without_a_version_still_fits(self):
        self.save("1.0")
        self.save("2.0")
        self.assertEqual({p["env"]["app_version"] for p in self.store(None).all()}, {"1.0", "2.0"})
        self.assertEqual([p["env"]["app_version"] for p in self.candidates("2.0")], ["2.0"])
        self.save(None)
        self.assertEqual(len(self.candidates("3.0")), 1)
        self.assertTrue(routes.fits_version({"env": {}}, "3.0"))
        self.assertFalse(routes.fits_version({"env": {"app_version": "1.0"}}, "3.0"))

    def test_the_goal_fingerprint_ignores_blanks_case_and_end_punctuation(self):
        self.assertEqual(routes.goal_fingerprint("Text {p1} that {p2}."),
                         routes.goal_fingerprint("text {p9} that {p3}"))
        self.assertNotEqual(routes.goal_fingerprint("Text {p1}"), routes.goal_fingerprint("Call {p1}"))

    def test_the_shareable_record_holds_hashes_only(self):
        self.save("1.0")
        [path] = self.candidates("1.0")
        record = routes.shareable(path)
        self.assertEqual((record["app"], record["version"]), ("com.apple.Preferences", "1.0"))
        self.assertEqual([s["op"] for s in record["steps"]], ["TAP", "TAP"])
        text = json.dumps(record)
        for word in ("General", "About", "Open", "Settings app"):
            self.assertNotIn(word, text)
        self.assertIsNone(routes.shareable({**path, "env": {}}))                # no version, nothing to share


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from unittest import mock

from PIL import Image, ImageDraw

from mobile_agent import perception


def frame(icons=((.85, .92),), text=((.1, .3, .5, .03),)):
    image = Image.new("RGB", (393, 852), "white")
    draw = ImageDraw.Draw(image)
    for cx, cy in icons:
        x, y = cx * 393, cy * 852
        draw.ellipse((x - 11, y - 11, x + 11, y + 11), outline="black", width=3)
    for x, y, w, h in text:
        draw.rectangle((x * 393, y * 852, (x + w) * 393, (y + h) * 852), fill="black")
    return image


class FakeLabeller:
    def __init__(self, icons, fail=False):
        self.icons, self.fail, self.calls = icons, fail, 0

    def complete(self, messages, schema, timeout=60):
        self.calls += 1
        if self.fail:
            raise TimeoutError("no answer")
        return {"icons": self.icons}, {"prompt_tokens": 1, "completion_tokens": 1}


class IconBoxTests(unittest.TestCase):
    def test_finds_a_glyph_away_from_text(self):
        boxes = perception.icon_boxes(frame(icons=((.85, .92), (.1, .6))), [(.1, .3, .5, .03)])
        centres = sorted((round(x + w / 2, 2), round(y + h / 2, 2)) for x, y, w, h in boxes)
        self.assertEqual(len(boxes), 2, boxes)
        self.assertAlmostEqual(centres[0][0], .1, delta=.02)
        self.assertAlmostEqual(centres[1][1], .92, delta=.02)

    def test_text_and_the_status_bar_are_not_icons(self):
        image = frame(icons=((.5, .02),), text=((.1, .3, .5, .03),))
        self.assertEqual(perception.icon_boxes(image, [(.1, .3, .5, .03)]), [])

    def test_a_glyph_vision_misread_is_an_icon_candidate(self):
        rows = [{"text": "+", "confidence": 1, "rect": (.8, .9, .05, .03)},
                {"text": "Reminders", "confidence": 1, "rect": (.1, .3, .5, .03)}]
        candidates = perception.icon_candidates(frame(icons=()), rows)
        self.assertIn((.8, .9, .05, .03), candidates)
        self.assertTrue(perception.is_glyph("Q"))
        self.assertFalse(perception.is_glyph("Wi-Fi"))

class LabelTests(unittest.TestCase):
    BOXES = [(.80, .90, .06, .03), (.05, .06, .06, .03), (.40, .60, .06, .03), (.6, .6, .06, .03)]

    def label(self, icons, memory=None, **kwargs):
        image = frame(icons=((.83, .915), (.08, .075), (.43, .615), (.63, .615)))
        client = FakeLabeller(icons, **kwargs)
        named = perception.label_icons(client, image, self.BOXES, bundle="com.x", memory=memory)
        return named, client

    def test_named_icons_and_the_ones_left_unlisted(self):
        named, _ = self.label([
            {"n": 1, "name": "Heart", "kind": "commit", "act": "like"},
            {"n": 2, "name": "Back", "kind": "navigate", "act": "none"},
            {"n": 3, "name": "Paper plane", "kind": "commit", "act": "none"},  # a commit it can't name
            {"n": 4, "name": "Avatar", "kind": "decor", "act": "none"}])
        labels = {label: kind for _, label, kind in named}
        self.assertEqual(labels, {"Like (Heart)": "commit", "Back": "navigate"})

    def test_a_commit_the_contract_cant_read_stays_unlisted(self):
        named, _ = self.label([{"n": 1, "name": "Arrow", "kind": "commit", "act": "share"}])
        self.assertFalse(perception.listable("Share (Arrow)", "commit") and not named)
        for _, label, kind in named:
            self.assertTrue(perception.listable(label, kind))

    def test_memory_saves_the_second_call(self):
        memory = perception.IconMemory()
        image = frame(icons=((.08, .075),))
        client = FakeLabeller([{"n": 1, "name": "Back", "kind": "navigate", "act": "none"}])
        first = perception.label_icons(client, image, self.BOXES[1:2], bundle="com.x", memory=memory)
        again = perception.label_icons(client, image, self.BOXES[1:2], bundle="com.x", memory=memory)
        self.assertEqual([label for _, label, _ in first], ["Back"])
        self.assertEqual([label for _, label, _ in again], ["Back"])
        self.assertEqual(client.calls, 1)  # the second screen's back arrow came from memory

    def test_a_failed_call_names_nothing(self):
        named, client = self.label([], fail=True)
        self.assertEqual((named, client.calls), ([], 1))

    def test_memory_persists(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            path = os.path.join(folder, "icons.json")
            perception.IconMemory(path).put("com.x", "abc", "Back", "navigate")
            self.assertEqual(perception.IconMemory(path).get("com.x", "abc"), ("Back", "navigate"))


class SnapshotIconTests(unittest.TestCase):
    def test_a_named_icon_is_an_element_and_replaces_the_glyph_vision_read(self):
        reading = {"rows": [{"text": "Q", "confidence": 1, "rect": [.7, .07, .05, .03]},
                            {"text": "+", "confidence": 1, "rect": [.85, .07, .05, .03]},
                            {"text": "Inbox", "confidence": 1, "rect": [.1, .07, .3, .03]}],
                   "hash": "h", "window": {"w": 318, "h": 701}}
        snapshot = perception.snapshot_from(reading, source="synthetic_fixture",
                                            icons=[((.68, .06, .09, .05), "Search", "navigate")])
        self.assertEqual([(e.id, e.label, e.role) for e in snapshot.elements],
                         [("t0", "Inbox", "StaticText"), ("i0", "Search", "Button")])

class IconSafetyTests(unittest.TestCase):

    def run_agent(self, label, approve_answer="denied"):
        from mobile_agent import engines, mirroring
        from mobile_agent.tests import test_engines
        from mobile_agent.tests.test_mirroring import FRAMES, FakeHelper, setUpModule
        if FRAMES is None:
            setUpModule()
        from mobile_agent.tests import test_mirroring
        helper = FakeHelper("settings")
        driver = mirroring.MirroringDriver(helper, frames_dir=test_mirroring.FRAMES.name, sleep=lambda s: None)
        driver._icons = lambda result: [((.8, .9, .08, .04), label, "commit" if "Like" in label else "navigate")]
        from mobile_agent.permissions import ASK_IMPORTANT, AppPolicy, Settings, gate_for_run
        # As the product runs a task: Ask before acting, the default permission mode.
        gate = gate_for_run(override=ASK_IMPORTANT, origin="tui", goal="x", emit=lambda e: None,
                            settings=Settings(mode=ASK_IMPORTANT, apps=AppPolicy(access="all")))
        asked = []
        script = test_engines.Script([[("TAP", label, None)], [("DONE", None, None)], [("DONE", None, None)]], [],
                                     answer="ok")
        with mock.patch.dict(os.environ, dict(test_engines.CLEAN), clear=True):
            engines.build_frontier(driver, script, apps={"com.burbn.instagram": "Instagram"}, emit=lambda e: None,
                                   config=test_engines.QUICK,
                                   approve=lambda request: asked.append(request) or approve_answer,
                                   permissions=gate, permission_mode=gate.mode).run(
                "What's the first post in my feed about?")
        return asked, [op for op, _ in helper.gestures()]

    def test_a_named_like_icon_asks_and_isnt_tapped_when_declined(self):
        asked, gestures = self.run_agent("Like (Heart)")
        self.assertTrue(asked)
        self.assertNotIn("tap", gestures)

    def test_a_named_back_icon_is_tapped_unasked(self):
        asked, gestures = self.run_agent("Back")
        self.assertEqual(asked, [])
        self.assertIn("tap", gestures)


if __name__ == "__main__":
    unittest.main()


class TabTests(unittest.TestCase):

    def tab_frame(self, filled_index):
        image = Image.new("RGB", (393, 852), "white")
        draw = ImageDraw.Draw(image)
        rects = []
        for i in range(5):
            cx, cy = (i + .5) / 5 * 393, .93 * 852
            box = (cx - 12, cy - 12, cx + 12, cy + 12)
            if i == filled_index:
                draw.ellipse(box, fill="black")
            else:
                draw.ellipse(box, outline="black", width=2)
            rects.append(((cx - 16) / 393, (cy - 16) / 852, 32 / 393, 32 / 852))
        return image, rects

    def test_the_filled_icon_is_the_current_tab(self):
        image, rects = self.tab_frame(1)
        index, row = perception.selected_tab(image, rects)
        self.assertEqual(index, 1)
        icons = [(rect, name, "navigate") for rect,
                 name in zip(rects, ("Home", "Reels", "Messages", "Search", "Profile"))]
        marked = perception.with_selection(image, icons)
        self.assertEqual([icon[1] for icon in marked if icon[3]], ["Reels"])
        snapshot = perception.snapshot_from({"rows": [], "hash": "h"}, source="synthetic_fixture", icons=marked)
        self.assertEqual([e.label for e in snapshot.elements if e.selected], ["Reels"])

    def test_no_clear_winner_is_no_selection(self):
        image, rects = self.tab_frame(None)
        self.assertIsNone(perception.selected_tab(image, rects)[0])

    def test_icons_under_a_keyboard_are_left_out(self):
        letters = [{"text": c, "confidence": 1, "rect": [.05 + .09 * i, .75, .04, .03]} for i,
                   c in enumerate("qwertyuiop")]
        reading = {"rows": letters + [{"text": "space", "confidence": 1, "rect": [.3, .93, .3, .03]},
                                      {"text": "nasa", "confidence": 1, "rect": [.1, .08, .2, .03]}], "hash": "h"}
        icons = [((.05, .75, .04, .03), "Q key", "input"), ((.05, .07, .06, .04), "Back", "navigate")]
        snapshot = perception.snapshot_from(reading, source="synthetic_fixture", icons=icons)
        self.assertEqual(snapshot.keyboard, "visible")
        self.assertEqual([e.label for e in snapshot.elements], ["nasa", "Back"])

    def test_a_one_word_button_is_not_a_field(self):
        reading = {"rows": [{"text": "Reply", "confidence": 1, "rect": [.1, .5, .1, .02]},
                            {"text": "Add a comment", "confidence": 1, "rect": [.1, .9, .4, .03]}], "hash": "h"}
        roles = {e.label: e.role for e in perception.snapshot_from(reading, source="synthetic_fixture").elements}
        self.assertEqual(roles, {"Reply": "StaticText", "Add a comment": "TextField"})

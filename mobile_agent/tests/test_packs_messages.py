import re
import unittest

from mobile_agent.packs import core, tools
from mobile_agent.packs import messages as ms
from mobile_agent.tests.packs_support import Phone, el, ocr_file, rows_file, snap

B = ms.BUNDLE


def screen(name):
    return rows_file(f"messages/{name}.json", B)


FIXTURES = {"inbox": "inbox", "inbox-2": "inbox", "thread": "thread", "compose": "compose"}
ACTS = re.compile(r"^(Send|Delete|Edit|Report Junk|Pin|Mute|Call|FaceTime)$", re.I)


def phone(start="springboard"):
    screens = {name: screen(name) for name in FIXTURES}
    screens["springboard"] = snap("com.apple.springboard", [el("Messages", "Icon", (.1, .9, .2, .08))])
    moves = {("*", "LAUNCH_APP", B): "inbox", ("inbox", "SWIPE_UP", None): "inbox-2",
             ("inbox-2", "SWIPE_UP", None): "inbox-2", ("inbox-2", "SWIPE_DOWN", None): "inbox",
             ("inbox", "TAP", "Sam Lee, Unread, see you at 6, bring the charger, 9:55 PM"): "thread",
             ("inbox", "TAP", "Riley, Can you send me the address?, 4:30 PM"): "thread",
             ("thread", "TAP", "5 unread"): "inbox", ("inbox", "TAP", "Compose"): "compose",
             ("thread", "SWIPE_DOWN", None): "thread"}
    return Phone(screens, moves, start)


def io_for(device):
    return core.PackIO(device, ms.PACK, sleep=lambda s: None)


class ScreenTests(unittest.TestCase):
    def test_every_screen_the_pack_knows_is_recognized_on_its_fixture(self):
        for name, expected in FIXTURES.items():
            seen = ms.PACK.recognize(screen(name))
            self.assertEqual(seen.screen, expected, f"{name}: {seen}")
            self.assertGreaterEqual(seen.confidence, core.SURE)

    def test_the_list_is_recognized_on_a_picture_too(self):
        self.assertEqual(ms.PACK.recognize(ocr_file("messages/inbox.ocr.json", B)).screen, "inbox")

    def test_another_app_is_no_messages_screen(self):
        self.assertIsNone(ms.PACK.recognize(snap("com.burbn.instagram", [el("Messages", "StaticText")])).screen)


class ReaderTests(unittest.TestCase):
    def test_the_list_reads_flags_times_and_a_group_name(self):
        rows = ms.parse_list(screen("inbox"))
        pinned = [r for r in rows if r["pinned"]]
        self.assertEqual([r["from"] for r in pinned], ["Book Club", "Mom", "Ana Ruiz"])
        self.assertTrue(pinned[2]["unread"])
        recent = [r for r in rows if not r["pinned"]]
        self.assertEqual([(r["from"], r["unread"], r["when"]) for r in recent[:4]],
                         [("Sam Lee", True, "9:55 PM"), ("Jordan Park", False, "9:12 PM"),
                          ("Riley", False, "4:30 PM"), ("+1 (555) 010-2233", True, "1:28 PM")])
        self.assertEqual(recent[0]["last"], "see you at 6, bring the charger")
        self.assertTrue(recent[4]["muted"])

    def test_a_picture_reads_the_same_names_times_and_lines(self):
        tree = [(r["from"], r["when"], r["last"]) for r in ms.parse_list(screen("inbox")) if not r["pinned"]]
        picture = ms.parse_list(ocr_file("messages/inbox.ocr.json", B))
        self.assertEqual([(r["from"], r["when"]) for r in picture], [(a, b) for a, b, _ in tree])
        self.assertEqual(picture[0]["last"], "see you at 6, bring the charger")
        self.assertTrue(all(r["unread"] is None for r in picture))

    def test_reading_the_newest_ten_scrolls_and_keeps_pinned_apart(self):
        device = phone()
        result = ms.PACK.read(io_for(device), "inbox", 8)
        recent = [r["from"] for r in result.rows if not r["pinned"]]
        self.assertEqual(recent, ["Sam Lee", "Jordan Park", "Riley", "+1 (555) 010-2233", "Dev Team", "Chris Wu",
                                  "Alex Kim", "Taylor"])
        self.assertEqual(sum(1 for r in result.rows if r["pinned"]), 3)
        self.assertEqual(device.taps, [])

    def test_unread_only(self):
        result = ms.PACK.read(io_for(phone()), "unread", 5)
        self.assertEqual([r["from"] for r in result.rows], ["Ana Ruiz", "Sam Lee", "+1 (555) 010-2233"])

    def test_unread_isnt_guessed_on_a_picture(self):
        device = Phone({"inbox": ocr_file("messages/inbox.ocr.json", B)}, {}, "inbox")
        with self.assertRaises(core.Lost):
            ms.PACK.read(io_for(device), "unread", 5)

    def test_a_conversation_by_name(self):
        device = phone()
        result = ms.PACK.read(io_for(device), "thread", 10, "Sam Lee")
        self.assertEqual([(r["from"], r["text"]) for r in result.rows],
                         [("Sam Lee", "are we still on for tonight?"), ("You", "yes! 6pm"),
                          ("Sam Lee", "see you at 6, bring the charger")])
        self.assertEqual(result.rows[0]["when"], "Today 9:40 PM")


class NavigationTests(unittest.TestCase):
    def test_reply_names_the_field_only_in_that_persons_conversation(self):
        device = phone()
        landing = core.navigate(ms.PACK, io_for(device), "thread", "Sam Lee")
        self.assertTrue(landing.ok, landing.why)
        self.assertEqual(core.control_for(ms.PACK, "reply", landing.snapshot).label, "iMessage")
        self.assertFalse(any(ACTS.match(t) for t in device.taps))
        self.assertEqual([op for op, _, _ in device.actions if op in ("TYPE", "SUBMIT")], [])

    def test_the_wrong_conversation_on_screen_is_no_landing(self):
        landing = core.navigate(ms.PACK, io_for(phone()), "thread", "Riley")
        self.assertFalse(landing.ok)
        self.assertIn("Sam Lee", landing.why)
        self.assertIsNone(landing.control)

    def test_a_name_that_isnt_in_the_list_scrolls_then_says_so(self):
        device = phone()
        landing = core.navigate(ms.PACK, io_for(device), "thread", "Nobody Here")
        self.assertFalse(landing.ok)
        self.assertIn("no conversation", landing.why)
        self.assertGreaterEqual(sum(1 for op, _, _ in device.actions if op == "SWIPE_UP"), 1)

    def test_same_person(self):
        self.assertTrue(ms.same_person("Sam Lee", "sam lee"))
        self.assertTrue(ms.same_person("Sam Lee", "Sam"))
        self.assertTrue(ms.same_person("Sam", "Sam Lee"))
        self.assertFalse(ms.same_person("Samantha", "Sam"))
        self.assertFalse(ms.same_person("Sam Lee", "Sam Park"))

    def test_the_pack_never_taps_send_or_edit(self):
        io = io_for(phone())
        for label in ("Send", "Edit", "Delete", "Report Junk", "Hide Alerts", "Pin", "Undo Send"):
            self.assertFalse(io.may_tap(label), label)

    def test_no_deep_link_on_a_driver_that_cant_open_one(self):
        class NoLinks(Phone):
            call = None
        device = NoLinks({"inbox": screen("inbox")}, {}, "inbox")
        landing = core.navigate(ms.PACK, io_for(device), "thread", "Sam Lee")
        self.assertFalse(landing.ok)
        self.assertEqual(device.calls, [])


class ToolTests(unittest.TestCase):
    def test_words_the_model_writes(self):
        self.assertEqual(tools.parse_read(ms.PACK, "texts 15"), ("inbox", 15, None))
        self.assertEqual(tools.parse_read(ms.PACK, "thread Sam Lee"), ("thread", 20, "Sam Lee"))
        self.assertEqual(tools.parse_open(ms.PACK, "reply Sam Lee"), ("thread", "Sam Lee", "reply"))

    def test_tasks_that_name_texts_get_the_tools(self):
        for goal in ("Who texted me most recently?", "Which of my text conversations are unread?",
                     "Catch me up on my iMessages"):
            self.assertTrue(ms.PACK.mentions.search(goal), goal)
        self.assertFalse(ms.PACK.mentions.search("What's the weather tomorrow?"))

    def test_prompts_fit(self):
        for line in ms.PACK.prompts.values():
            self.assertLessEqual(len(line), 400)


if __name__ == "__main__":
    unittest.main()

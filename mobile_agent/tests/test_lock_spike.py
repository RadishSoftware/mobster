import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from mobile_agent import lock_spike, lockscreen
from mobile_agent.lock_spike import Spike, SpikeError

FIXTURES = Path(__file__).parent / "fixtures" / "ios" / "lock"
SB = lockscreen.SPRINGBOARD
CANARY = "739512"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class FakeDriver:
    def __init__(self, tree, *, locked=True, front=SB, tree_after=None, tap_error=None, unlock_after=False):
        self.tree, self.locked, self.front = tree, locked, front
        self.tree_after, self.tap_error, self.unlock_after = tree_after, tap_error, unlock_after
        self.taps, self.requests = [], []
        self.http = self

    def request(self, method, path, body=None, timeout=None):
        self.requests.append((method, path))
        if path == "/wda/locked":
            return {"value": False if self.taps and self.unlock_after else self.locked}
        if path == "/wda/activeAppInfo":
            return {"value": {"bundleId": self.front}}
        if path == "/status":
            return {"value": {"os": {"name": "iOS", "version": "26.0"}}}
        raise AssertionError(path)

    def call(self, method, path, body=None, timeout=None):
        self.requests.append((method, path))
        if path.startswith("/source"):
            return self.tree_after if self.taps and self.tree_after else self.tree
        return {}

    def tap_point(self, x, y, snapshot, timeout=10):
        self.taps.append((x * snapshot.width, y * snapshot.height))
        if self.tap_error:
            raise self.tap_error


class Case(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="mobster-spike-"))
        self.sleeps, self.asked = [], []

    def spike(self, driver, *, answers=("y", "y"), secret=CANARY, interactive=True):
        replies = iter(answers)

        def prompt(question):
            self.asked.append(question)
            return next(replies)
        return Spike(driver, self.out, prompt=prompt, secret=lambda _question: secret, sleep=self.sleeps.append,
                     interactive=interactive)

    def everything_written(self):
        return "".join(path.read_text() for path in self.out.rglob("*") if path.is_file())


class RecordedTrees(Case):
    def test_recorded_keypads_classify_as_the_passcode_keypad(self):
        for name, title in (("keypad_6_synthetic.json", "Enter Passcode"), ("keypad_4_synthetic.json",
                                                                          "Enter iPhone Passcode")):
            with self.subTest(name=name):
                labels = lockscreen.tree_labels(fixture(name))
                self.assertIn(title, [text for _, text in labels])
                self.assertEqual(lockscreen.classify(True, SB, labels), ("passcode_keypad", None))
                self.assertEqual(lockscreen.classify(False, SB, labels)[0], "app_locked_sheet")

    def test_a_tree_without_ten_usable_keys_gives_no_points(self):
        tree = fixture("keypad_6_synthetic.json")
        buttons = [n for n in lock_spike._walk(tree) if n.get("label") == "3"]
        buttons[0]["isEnabled"] = False
        for broken in (tree, {"type": "XCUIElementTypeApplication", "children": []}):
            with self.subTest(), self.assertRaises(SpikeError):
                lock_spike.keypad_keys(broken)

    def test_a_saved_tree_keeps_no_text_that_is_not_keypad_vocabulary(self):
        tree = fixture("keypad_6_synthetic.json")
        tree["children"][0]["children"] += [
            {"type": "XCUIElementTypeStaticText", "label": "Studio iPhone", "children": []},
            {"type": "XCUIElementTypeOther", "label": "Messages, Dana: dinner at 8?", "value": "secret",
             "children": []}]
        texts = {n["label"] for n in lock_spike._walk(lock_spike.scrub(tree))}
        self.assertNotIn("Studio iPhone", texts)
        self.assertFalse(any("dinner" in text or "secret" in text for text in texts))
        self.assertTrue({"Enter Passcode", "5", "ABC", "Emergency", "<text>"} <= texts)
        self.assertEqual(lockscreen.classify(True, SB, lockscreen.tree_labels(lock_spike.scrub(tree)))[0],
                         "passcode_keypad")

    def test_record_reads_only_and_saves_the_scrubbed_tree(self):
        driver = FakeDriver(fixture("keypad_6_synthetic.json"))
        row = self.spike(driver).record()
        self.assertEqual((row["ios"], row["state"], row["tenKeysWithRects"]), ("26.0", "passcode_keypad", True))
        self.assertEqual(row["shape"]["digitKeys"], 10)
        self.assertTrue((self.out / row["saved"]).is_file())
        self.assertEqual(driver.taps, [])
        self.assertTrue(all(method == "GET" or path == "/appium/settings" for method, path in driver.requests))


class OneTap(Case):
    def test_one_tap_lands_on_the_digit_asked_for_and_nothing_else(self):
        tree = fixture("keypad_6_synthetic.json")
        after = json.loads(json.dumps(tree))
        after["children"][0]["children"][1]["value"] = "1 dot"
        after["children"][0]["children"][1]["label"] = "1 of 6"
        driver = FakeDriver(tree, tree_after=after)
        row = self.spike(driver).tap_digit("5")
        self.assertEqual(driver.taps, [(196.5, 498.0)])
        self.assertEqual((row["step"], row["dotFilled"], row["treeChanged"]), ("tap-digit", True, True))
        events = [json.loads(line)["event"] for line in (self.out / "results.jsonl").read_text().splitlines()]
        self.assertEqual(events, ["about_to_tap"])

    def test_it_refuses_without_the_owners_yes(self):
        driver = FakeDriver(fixture("keypad_6_synthetic.json"))
        with self.assertRaises(SpikeError):
            self.spike(driver, answers=("n",)).tap_digit("5")
        self.assertEqual(driver.taps, [])

    def test_a_digit_is_one_digit(self):
        driver = FakeDriver(fixture("keypad_6_synthetic.json"))
        with self.assertRaises(SpikeError):
            self.spike(driver).tap_digit("55")
        self.assertEqual(driver.taps, [])


class FullEntry(Case):
    def test_each_digit_is_tapped_at_its_key_in_order(self):
        tree = fixture("keypad_6_synthetic.json")
        driver = FakeDriver(tree, unlock_after=True)
        row = self.spike(driver).enter()
        keys = lock_spike.keypad_keys(tree)
        self.assertEqual(driver.taps, [keys[digit] for digit in CANARY])
        self.assertEqual((row["step"], row["unlockedAfter"]), ("enter", True))

    def test_the_passcode_is_nowhere_it_could_leak(self):
        driver = FakeDriver(fixture("keypad_6_synthetic.json"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            row = self.spike(driver).enter()
        self.spike(driver, interactive=True).note("result", **row)
        for place in (self.everything_written(), json.dumps(row), out.getvalue(), err.getvalue(), repr(self.asked),
                      repr(driver.requests)):
            self.assertNotIn(CANARY, place)
        self.assertNotIn("739", self.everything_written())

    def test_a_second_attempt_is_refused_until_the_owner_clears_the_marker(self):
        driver = FakeDriver(fixture("keypad_6_synthetic.json"))
        self.spike(driver).enter()
        taps = len(driver.taps)
        with self.assertRaisesRegex(SpikeError, "one attempt per session"):
            self.spike(driver).enter()
        self.assertEqual(len(driver.taps), taps)
        (self.out / lock_spike.MARKER).unlink()
        self.spike(driver).enter()
        self.assertEqual(len(driver.taps), 2 * taps)

    def test_it_refuses_where_it_must(self):
        pad = fixture("keypad_6_synthetic.json")
        failed = json.loads(json.dumps(pad))
        failed["children"][0]["children"].append({"type": "XCUIElementTypeStaticText",
                                                  "label": "iPhone Unavailable, try again in 1 minute",
                                                  "children": []})
        cases = {
            "a look-alike keypad in another app": (FakeDriver(pad, front="com.example.bank"), "not SpringBoard"),
            "an unlocked phone": (FakeDriver(pad, locked=False), "isn't answering as locked"),
            "a runner that doesn't answer": (FakeDriver(pad, locked=None), "isn't answering as locked"),
            "the lock screen without a keypad": (FakeDriver(fixture("keypad_6_synthetic.json")
                                                            | {"children": []}), "not SpringBoard"),
            "a failed-attempt message": (FakeDriver(failed), "failed-attempt"),
        }
        for name, (driver, why) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(SpikeError, why):
                self.spike(driver).enter()
            self.assertEqual(driver.taps, [], name)
            self.assertFalse((self.out / lock_spike.MARKER).exists(), name)

    def test_words_on_the_screen_only_make_it_refuse_never_act(self):
        tree = {"type": "XCUIElementTypeApplication", "label": "SpringBoard",
                "children": [{"type": "XCUIElementTypeStaticText", "label": "Enter password", "children": []}]}
        driver = FakeDriver(tree)
        with self.assertRaises(SpikeError):
            self.spike(driver).enter()
        self.assertEqual(driver.taps, [])

    def test_it_needs_a_person_at_a_terminal_and_a_real_passcode(self):
        pad = fixture("keypad_6_synthetic.json")
        driver = FakeDriver(pad)
        with self.assertRaisesRegex(SpikeError, "terminal"):
            self.spike(driver, interactive=False).enter()
        for bad in ("", "12", "12345", "1234567", "12ab56", "abcdef"):
            with self.subTest(bad=bad), self.assertRaises(SpikeError) as caught:
                self.spike(driver, secret=bad).enter()
            if bad:
                self.assertNotIn(bad, str(caught.exception))
        self.assertEqual(driver.taps, [])

    def test_a_tap_that_ends_unanswered_stops_the_entry_and_is_not_retried(self):
        driver = FakeDriver(fixture("keypad_6_synthetic.json"), tap_error=TimeoutError("timed out"))
        with self.assertRaisesRegex(SpikeError, "outcome is unknown"):
            self.spike(driver).enter()
        self.assertEqual(len(driver.taps), 1)
        self.assertTrue((self.out / lock_spike.MARKER).exists())
        with self.assertRaisesRegex(SpikeError, "one attempt per session"):
            self.spike(driver).enter()
        self.assertEqual(len(driver.taps), 1)


class Survival(Case):
    def test_it_reports_whether_the_runner_kept_answering_while_locked(self):
        clock = iter(range(0, 1000, 5))
        driver = FakeDriver(fixture("keypad_6_synthetic.json"))
        spike = Spike(driver, self.out, prompt=input, sleep=lambda _s: None, clock=lambda: next(clock),
                      interactive=False)
        with contextlib.redirect_stderr(io.StringIO()):
            row = spike.survive(seconds=20, interval=5)
        self.assertEqual((row["lockedSamples"], row["answeredWhileLocked"], row["stillAnswersAfter"]),
                         (row["samples"], True, True))
        self.assertEqual(driver.taps, [])


if __name__ == "__main__":
    unittest.main()

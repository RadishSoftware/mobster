"""B2 and B4 on fakes: a locked phone, the lock screen's keypad, an unplugged cable and long waits for the user.

On the real phone (5 Oct) a locked iPhone turned into 50 instant LAUNCH_APP failures a run ($0.37, 0 actions)
whose history said "LAUNCH_APP '' failed", and a pulled cable took 303-318 s of recoveries to report. The loop
now names the bundle and the status, asks the phone guard (agent_hooks.PhoneGuard) on an activate 400 or a
repeated failure, refuses every model tap on SpringBoard's passcode keypad, and stops at once when unplugged."""

import json
import unittest
from unittest.mock import patch

from mobile_agent.agent_hooks import UNLOCKED_LINE, GuardVerdict
from mobile_agent.frontier import (PHONE_LOCKED, RECOVERY_BUDGET_SECONDS, STALL_BUDGET_SECONDS, FrontierAgent,
                                   prompt_text)
from mobile_agent.state import Element, Snapshot
from mobile_agent.transport import TransportError

REMINDERS = "com.apple.reminders"


def screen(*elements, bundle="com.apple.springboard"):
    return Snapshot(list(elements), "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture",
                    bundle_id=bundle)


HOME = screen(Element("1", "Reminders", "Icon", (.1, .1, .15, .08)))
LIST = screen(Element("1", "New Reminder", "Button", (.1, .9, .4, .04)), bundle=REMINDERS)
KEYPAD = screen(Element("0", "Enter Passcode", "StaticText", (.3, .2, .4, .04)),
                *[Element(str(10 + d), label, "Button", (.1 + (d % 3) * .3, .35 + (d // 3) * .1, .2, .08))
                  for d, label in enumerate(["1", "2, A B C", "3, D E F", "4, G H I", "5, J K L", "6, M N O",
                                             "7, P Q R S", "8, T U V", "9, W X Y Z", "0"])],
                Element("30", "Emergency", "Button", (.05, .9, .3, .04)))


class Guard:
    """A fake PhoneGuard: answers ``verdicts`` in turn (the last one repeats) and records each cause."""

    def __init__(self, *verdicts, attached=None, allows=None):
        self.verdicts, self._attached, self._allows = list(verdicts), attached, allows
        self.causes, self.finished = [], 0

    def check(self, driver, *, cause):
        self.causes.append(cause)
        verdict = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        if verdict.state == "unlocked" and hasattr(driver, "unlock"):
            driver.unlock()
        return verdict

    def attached(self):
        return self._attached

    def allows_app(self, bundle_id):
        return self._allows is None or bundle_id in self._allows

    def finish(self, driver):
        self.finished += 1


class Phone:
    """A phone whose launches answer HTTP 400 while it is locked."""

    def __init__(self, first=HOME, locked=True):
        self.screen, self.locked, self.actions = first, locked, []

    def observe(self, timeout=10):
        return self.screen

    def unlock(self):
        self.locked = False

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, getattr(target, "label", target), text))
        if operation == "LAUNCH_APP":
            if self.locked:
                raise TransportError("HTTP 400; WDA rejected command; not retried")
            self.screen = LIST

    def tap_point(self, x, y, snapshot, timeout=10):
        self.actions.append(("TAP_POINT", x, y))


class Model:
    """Answers every decision with ``chunk`` (or the next of ``chunks``); records the prompts it saw."""

    def __init__(self, *chunks, answer="Added."):
        self.chunks, self.answer, self.prompts = list(chunks), answer, []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def complete(self, messages, schema, timeout=60, **kwargs):
        if "items" in schema["properties"]:
            return {"items": []}, {}
        self.usage["calls"] += 1
        prompt = prompt_text(messages)
        self.prompts.append(prompt)
        rows = prompt.split("Screen elements:\n", 1)[1].splitlines()
        chunk = self.chunks.pop(0) if len(self.chunks) > 1 else self.chunks[0]
        actions = []
        for i, (op, label, text) in enumerate(chunk):
            target = next((r.split()[0] for r in rows if label and f'"{label}"' in r), None) if i == 0 else None
            actions.append({"op": op, "target": label if op == "LAUNCH_APP" else target or label, "text": text})
        return {"thought": "", "plan": None, "notes_add": [], "checklist_updates": [], "actions": actions,
                "answer": self.answer if chunk[-1][0] in ("DONE", "BLOCKED") else None}, {}


def agent(phone, model, guard=None, **kwargs):
    events = []
    frontier = FrontierAgent(phone, model, apps={REMINDERS: "Reminders"}, emit=events.append, screenshots=False,
                             settle_seconds=0, contract=False, guard=guard, skills=(), **kwargs)
    return frontier, events


LAUNCH = [("LAUNCH_APP", REMINDERS, None)]


class LockedLaunchTests(unittest.TestCase):
    def test_an_activate_400_with_a_stopping_guard_ends_in_one_decision(self):
        guard = Guard(GuardVerdict("stop", "phone_locked", PHONE_LOCKED + " No action was taken."))
        model = Model(LAUNCH)
        frontier, events = agent(Phone(), model, guard)
        result = frontier.run("Add a reminder to buy milk.")
        self.assertEqual((result["status"], result["code"]), ("blocked", "phone_locked"))
        self.assertTrue(result["reason"].startswith("Your iPhone is locked."))
        self.assertLessEqual(model.usage["calls"], 1)
        self.assertEqual(guard.causes, ["launch_failed"])
        self.assertIn({"cause": "launch_failed", "state": "stop", "code": "phone_locked"},
                      [{k: e[k] for k in ("cause", "state", "code")} for e in events if e["event"] == "phone_guard"])

    def test_without_a_guard_three_identical_failures_end_the_run_blocked(self):
        model = Model(LAUNCH)
        frontier, _ = agent(Phone(), model)
        result = frontier.run("Add a reminder to buy milk.")
        self.assertEqual(result["status"], "blocked")
        self.assertLessEqual(model.usage["calls"], 3)
        self.assertIn("couldn't open Reminders (HTTP 400)", result["reason"])
        self.assertIn("No action was taken.", result["reason"])
        # The history names the bundle and the status (it said "LAUNCH_APP ''" before).
        self.assertIn(f"LAUNCH_APP '{REMINDERS}' failed (HTTP 400)", model.prompts[-1])

    def test_an_unlocking_guard_lets_the_task_go_on_and_the_model_sees_only_one_line(self):
        guard = Guard(GuardVerdict("unlocked"), GuardVerdict("ready"))
        phone = Phone()
        model = Model(LAUNCH, [("TAP", "New Reminder", None)], [("DONE", None, None)], [("DONE", None, None)])
        frontier, events = agent(phone, model, guard)
        result = frontier.run("Open Reminders.")
        self.assertEqual(result["status"], "completed")
        self.assertIn(("LAUNCH_APP", REMINDERS, None), phone.actions)
        self.assertIn(UNLOCKED_LINE, model.prompts[1])
        self.assertNotIn("asscode", "".join(model.prompts))
        self.assertEqual(result["timing"].get("unlocks"), 1)

    def test_a_guard_that_finds_the_phone_ready_still_ends_the_third_failure(self):
        guard = Guard(GuardVerdict("ready"))
        model = Model(LAUNCH)
        frontier, _ = agent(Phone(), model, guard)
        result = frontier.run("Add a reminder.")
        self.assertEqual(result["status"], "blocked")
        self.assertLessEqual(model.usage["calls"], 3)


class KeypadTests(unittest.TestCase):
    def test_a_model_tap_on_the_passcode_keypad_is_refused_and_ends_the_run_without_a_guard(self):
        phone = Phone(first=KEYPAD)
        model = Model([("TAP", "1", None), ("TAP", "2, A B C", None)])
        frontier, events = agent(phone, model)
        result = frontier.run("Open Reminders.")
        self.assertEqual(phone.actions, [])
        self.assertEqual((result["status"], result["code"]), ("blocked", "phone_locked"))
        self.assertIn("passcode_keypad", [e.get("label") for e in events if e["event"] == "frontier_refused"])
        self.assertEqual(model.usage["calls"], 1)

    def test_tap_xy_on_the_keypad_is_refused_too_and_the_guard_is_asked(self):
        phone = Phone(first=KEYPAD)
        guard = Guard(GuardVerdict("stop", "phone_locked", PHONE_LOCKED))
        model = Model([("TAP_XY", "50,40", "2")])
        frontier, _ = agent(phone, model, guard)
        result = frontier.run("Open Reminders.")
        self.assertEqual(phone.actions, [])
        self.assertEqual(guard.causes, ["lock_suspected"])
        self.assertEqual(result["code"], "phone_locked")

    def test_an_app_keypad_is_not_the_lock_screen(self):
        from mobile_agent.task_policy import springboard_keypad
        self.assertTrue(springboard_keypad(KEYPAD))
        bank = screen(*KEYPAD.elements, bundle="com.bank.app")
        self.assertFalse(springboard_keypad(bank))
        self.assertFalse(springboard_keypad(HOME))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, seconds)


class Unplugged(Phone):
    """The cable is pulled after the first read: reads are refused and recovery waits out its timeout."""

    def __init__(self, clock, recover_seconds=90):
        super().__init__(first=LIST, locked=False)
        self.clock, self.recover_seconds, self.reads, self.recoveries = clock, recover_seconds, 0, 0

    def observe(self, timeout=10):
        self.reads += 1
        if self.reads > 1:
            self.clock.t += 1
            raise TransportError("HTTP ConnectionRefusedError; request not retried")
        return self.screen

    def recover(self, timeout=90):
        self.recoveries += 1
        self.clock.t += min(timeout, self.recover_seconds)
        raise TransportError("WDA did not come back")


class DetachTests(unittest.TestCase):
    def run_unplugged(self, guard, recover_seconds=90):
        clock = Clock()
        phone = Unplugged(clock, recover_seconds)
        model = Model([("TAP", "New Reminder", None)])
        with patch("mobile_agent.frontier.time.monotonic", clock.monotonic), \
                patch("mobile_agent.frontier.time.sleep", clock.sleep):
            started = clock.t
            frontier, events = agent(phone, model, guard)
            result = frontier.run("Add a reminder.")
            return result, clock.t - started, phone, frontier

    def test_an_unplugged_phone_stops_within_ten_seconds_with_the_unplugged_sentence(self):
        result, elapsed, phone, _ = self.run_unplugged(Guard(GuardVerdict("ready"), attached=False))
        self.assertEqual((result["status"], result["code"]), ("blocked", "unplugged"))
        self.assertEqual(result["reason"], "Your iPhone was unplugged. Plug it back in and try again.")
        self.assertLessEqual(elapsed, 10)
        self.assertEqual(phone.recoveries, 0)

    def test_without_knowing_the_whole_run_spends_at_most_thirty_seconds_recovering(self):
        result, elapsed, phone, frontier = self.run_unplugged(None)
        self.assertEqual(result["status"], "error")
        self.assertLessEqual(frontier._recovery_spent, RECOVERY_BUDGET_SECONDS + 1)
        self.assertLessEqual(elapsed, RECOVERY_BUDGET_SECONDS + 5)


class StallTests(unittest.TestCase):
    """A read that times out (WDA stuck behind a screenshot in Maps or Spotify, B4) is not a lost runner: it comes
    out of its own budget, so two stalls in one run do not end it (review of #68: the second did)."""

    def run_stalling(self, stalled):
        clock = Clock()

        class Stalling(Phone):
            reads = 0

            def observe(self, timeout=10):
                self.reads += 1
                if stalled(self.reads):
                    clock.t += 15  # the read's own timeout
                    raise TimeoutError("read timed out")
                return self.screen

        phone = Stalling(first=LIST, locked=False)
        model = Model([("SWIPE_UP", None, None)], [("SWIPE_UP", None, None)], [("DONE", None, None)],
                      [("DONE", None, None)])
        with patch("mobile_agent.frontier.time.monotonic", clock.monotonic), \
                patch("mobile_agent.frontier.time.sleep", clock.sleep):
            started = clock.t
            frontier, _ = agent(phone, model, max_steps=6)
            result = frontier.run("Scroll my reminders.")
            return result, clock.t - started, frontier

    def test_two_separate_stalls_do_not_end_the_run(self):
        result, _, frontier = self.run_stalling(lambda read: read in (2, 4))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(frontier._recovery_spent, 0)

    def test_a_phone_that_never_answers_again_ends_within_the_stall_budget(self):
        result, elapsed, frontier = self.run_stalling(lambda read: read > 1)
        self.assertEqual(result["status"], "error")
        self.assertLessEqual(elapsed, STALL_BUDGET_SECONDS + 15)


class WaitTests(unittest.TestCase):
    def test_after_a_long_approval_the_guard_looks_again(self):
        clock = Clock()
        send = Element("2", "Delete", "Button", (.8, .3, .1, .04))
        phone = Phone(first=screen(send, bundle=REMINDERS), locked=False)
        guard = Guard(GuardVerdict("ready"))

        def approve(request):
            clock.t += 25  # the user took 25 s to answer
            return "approved"
        model = Model([("TAP", "Delete", None)], [("DONE", None, None)], [("DONE", None, None)])
        with patch("mobile_agent.frontier.time.monotonic", clock.monotonic), \
                patch("mobile_agent.frontier.time.sleep", clock.sleep):
            frontier, _ = agent(phone, model, guard, approve=approve)
            frontier.run("Delete the reminder.")
        self.assertIn("resume", guard.causes)
        self.assertIn(("TAP", "Delete", None), phone.actions)

    def test_a_quick_approval_does_not(self):
        send = Element("2", "Delete", "Button", (.8, .3, .1, .04))
        phone = Phone(first=screen(send, bundle=REMINDERS), locked=False)
        guard = Guard(GuardVerdict("ready"))
        model = Model([("TAP", "Delete", None)], [("DONE", None, None)], [("DONE", None, None)])
        frontier, _ = agent(phone, model, guard, approve=lambda request: "approved")
        frontier.run("Delete the reminder.")
        self.assertNotIn("resume", guard.causes)


class FrontAppTests(unittest.TestCase):
    def test_springboard_in_front_of_the_tree_asks_the_guard(self):
        class Covered(Phone):
            def active_app(self, timeout=5):
                return "com.apple.springboard"
        phone = Covered(first=LIST, locked=False)
        guard = Guard(GuardVerdict("stop", "phone_locked", PHONE_LOCKED))
        model = Model([("TAP", "New Reminder", None)])
        frontier, _ = agent(phone, model, guard)
        result = frontier.run("Add a reminder.")
        self.assertEqual(guard.causes, ["lock_suspected"])
        self.assertEqual(result["code"], "phone_locked")

    def test_without_a_guard_the_second_covered_tap_in_a_row_stops_the_run(self):
        # The tree is Reminders' while SpringBoard is in front: every tap lands on a screen nobody can see, maybe
        # the passcode keypad (B2). Without a guard to look, the run stops before a third tap (review of #68:
        # it tapped on, one tap a turn, to the end of its steps).
        class Covered(Phone):
            def active_app(self, timeout=5):
                return "com.apple.springboard"
        rows = [Element(str(i), f"Item {i}", "Button", (.1, .1 + i * .07, .6, .05)) for i in range(1, 7)]
        phone = Covered(first=screen(*rows, bundle=REMINDERS), locked=False)
        model = Model(*[[("TAP", f"Item {i}", None)] for i in range(1, 7)])
        frontier, _ = agent(phone, model)
        result = frontier.run("Add a reminder.")
        self.assertEqual((result["status"], result["code"]), ("blocked", "phone_locked"))
        self.assertIn("lock screen or a system alert is in front of Reminders", result["reason"])
        self.assertEqual(len([a for a in phone.actions if a[0] == "TAP"]), 2)
        self.assertIn("finish with BLOCKED; never tap it", model.prompts[1])

    def test_a_covered_tap_then_a_screen_that_answers_goes_on(self):
        class Alert(Phone):
            covered = True

            def active_app(self, timeout=5):
                return "com.apple.springboard" if self.covered else REMINDERS

            def tap_point(self, x, y, snapshot, timeout=10):
                super().tap_point(x, y, snapshot, timeout)
                self.covered, self.screen = False, LIST  # "Allow" closed the system alert

        rows = [Element("1", "Item 1", "Button", (.1, .2, .6, .05))]
        phone = Alert(first=screen(*rows, bundle=REMINDERS), locked=False)
        model = Model([("TAP", "Item 1", None)], [("TAP_XY", "50,60", "Allow")], [("DONE", None, None)],
                      [("DONE", None, None)])
        frontier, _ = agent(phone, model)
        result = frontier.run("Open my reminders.")
        self.assertEqual(result["status"], "completed")

    def test_an_app_the_user_never_lets_mobster_open_stops_the_run_before_its_launch(self):
        guard = Guard(GuardVerdict("ready"), allows={"com.apple.Preferences"})
        phone = Phone(locked=False)
        frontier, _ = agent(phone, Model(LAUNCH), guard)
        result = frontier.run("Add a reminder.")
        self.assertEqual((result["status"], result["code"]), ("blocked", "blocked_app"))
        self.assertEqual(phone.actions, [])

    def test_no_event_or_result_carries_a_digit_count_or_a_passcode(self):
        guard = Guard(GuardVerdict("stop", "passcode_failed", "The saved passcode didn't unlock your iPhone. Mobster "
                                   "won't try it again until you re-enter it in Settings › iPhones."))
        frontier, events = agent(Phone(), Model(LAUNCH), guard)
        result = frontier.run("Add a reminder.")
        text = json.dumps([result, events], default=str)
        self.assertNotIn("468213", text)
        self.assertNotIn("digit", text)



class InstallSheetTests(unittest.TestCase):
    """C3: after an approved App Store Get, Apple's own confirmation is the user's: the guard hands it off or stops."""

    def test_after_the_get_tap_the_guard_looks_for_apples_sheet(self):
        page = screen(Element("1", "Duolingo - Language Lessons", "StaticText", (.3, .14, .6, .04)),
                      Element("2", "Get", "Button", (.3, .2, .2, .04)), bundle="com.apple.AppStore")
        sheet = screen(Element("5", "Double Click to Install", "StaticText", (.2, .8, .6, .04)),
                       bundle="com.apple.AppStore")

        class Store(Phone):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                self.actions.append((operation, getattr(target, "label", target), text))
                self.screen = sheet

        class Items(Model):
            def complete(self, messages, schema, timeout=60, **kwargs):
                if "items" in schema["properties"]:
                    return {"items": [{"kind": "COMMIT", "app": "App Store", "what": "Duolingo", "payload": "",
                                       "act": "install", "count": "1", "condition": "",
                                       "quote": "Install Duolingo"}]}, {}
                return super().complete(messages, schema, timeout)
        guard = Guard(GuardVerdict("stop", "apple_confirmation", "The App Store wants you to confirm on your iPhone. "
                                   "Mobster never does that for you."))
        phone, asked = Store(first=page, locked=False), []
        frontier = FrontierAgent(phone, Items([("TAP", "Get", None)]), apps={"com.apple.AppStore": "App Store"},
                                 screenshots=False, settle_seconds=0, guard=guard, skills=(),
                                 approve=lambda request: asked.append(request) or "approved")
        result = frontier.run("Install Duolingo from the App Store.")
        self.assertEqual([a["title"] for a in asked], ["Install Duolingo from the App Store?"])
        self.assertEqual(phone.actions, [("TAP", "Get", None)])
        self.assertEqual(guard.causes, ["sheet_suspected"])
        self.assertEqual((result["status"], result["code"]), ("blocked", "apple_confirmation"))


if __name__ == "__main__":
    unittest.main()

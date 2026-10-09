"""USE_CODE (skills/codes.py) on a fake phone: where the code is found, that it is typed only into a code field of
the app it started in, every refusal, and that the code never reaches the model's feedback, the approval title or
an event. Synthetic iOS 26 trees in fixtures/skills; no phone, no WebDriverAgent, no model."""

import dataclasses
import datetime
import json
from pathlib import Path
import unittest

from mobile_agent.agent_hooks import Prepared, SkillContext, SkillResult
from mobile_agent.skills import default_skills
from mobile_agent.skills import codes
from mobile_agent.skills.codes import (CodeRefused, UseCode, approval_title, extract_code, mask_codes, parse_age)
from mobile_agent.state import Element, Snapshot

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "skills"
CANARY = "482913"
SPRINGBOARD = "com.apple.springboard"


def screen(name, **changes):
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    elements = []
    for index, item in enumerate(data["elements"]):
        editable = bool(item.get("editable"))
        elements.append(Element(str(index), item["label"], item["role"], tuple(item["rect"]), editable=editable,
                                locator=f"/app/{name}/{index}", value=item.get("value", ""),
                                actions=("TAP", "TYPE", "TYPE_SUBMIT") if editable else ("TAP",),
                                placeholder=item.get("placeholder", "")))
    text = "\n".join(e.label for e in elements if e.label)
    snapshot = Snapshot(elements, text, data["width"], data["height"], "wda", bundle_id=data["bundle"],
                        keyboard=data.get("keyboard", ""))
    return dataclasses.replace(snapshot, **changes) if changes else snapshot


class FakeClock:
    """Monotonic time that moves only when someone waits or reads; the wall clock moves with it."""

    def __init__(self, wall=None):
        self.now = 1000.0
        self.wall = wall if wall is not None else datetime.datetime(2026, 10, 5, 10, 44).timestamp()

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def time(self):
        return self.wall + (self.now - 1000.0)


class FakePhone:
    """WebDriverAgent as the skills use it: drags open and close Notification Center, settings name the app a read
    returns, activate brings an app forward, a tap focuses a field, /wda/keys types. Every call is recorded."""

    def __init__(self, front, screens, *, clock, nc="nc_empty", focused=None, read_cost=.3):
        self.front, self.screens, self.clock = front, dict(screens), clock
        self.nc = screen(nc) if isinstance(nc, str) else nc
        self.focused_screen = focused
        self.nc_open, self.hint, self.focused = False, front, False
        self.calls, self.typed, self.taps, self.read_cost = [], [], [], read_cost
        self.events = []

    def observe(self, timeout=10):
        self.clock.sleep(self.read_cost)
        self.calls.append("observe")
        if self.nc_open and self.hint == SPRINGBOARD:
            return self.nc
        snapshot = self.screens[self.front]
        if self.focused and self.front == "com.chase.sig" and self.focused_screen is not None:
            return self.focused_screen
        return dataclasses.replace(snapshot, keyboard="visible") if self.focused else snapshot

    def active_app(self, timeout=5):
        return SPRINGBOARD if self.nc_open else self.front

    def tap_point(self, x, y, snapshot, timeout=10):
        self.taps.append((round(x, 3), round(y, 3), self.nc_open))
        self.calls.append(("tap", round(x, 3), round(y, 3)))
        if not self.nc_open:
            self.focused = True

    def call(self, method, path, body=None, timeout=10):
        self.calls.append((method, path, json.loads(json.dumps(body))))
        if path == "/actions":
            moves = [a for a in body["actions"][0]["actions"] if a["type"] == "pointerMove"]
            if len(moves) < 2:
                self.taps.append((moves[0]["x"], moves[0]["y"], self.nc_open))
                return None
            start, end = moves[0]["y"], moves[-1]["y"]
            height = self.screens[self.front].height
            if start <= 5 and end > start:
                self.nc_open = True
            elif start >= height - 5 and end < start:
                self.nc_open = False
        elif path == "/appium/settings":
            self.hint = body["settings"]["defaultActiveApplication"]
        elif path == "/wda/apps/activate":
            self.front, self.nc_open, self.focused = body["bundleId"], False, False
            self.hint = body["bundleId"]
        elif path == "/wda/keys":
            self.typed.append("".join(body["value"]))
        return None


def context(phone):
    return SkillContext(driver=phone, request="Sign in to Chase", app_bundle=phone.front, emit=phone.events.append)


def field_act(snapshot, label="Verification code", text=None):
    element = next(e for e in snapshot.elements if e.label == label)
    return {"operation": "USE_CODE", "target": element.id, "target_label": None, "text": text, "app": None}


class TextTests(unittest.TestCase):
    def test_codes_are_found_next_to_code_wording(self):
        for text, code in (("Your Chase verification code is 482913. Don't share it.", "482913"),
                           ("G-731906 is your Google verification code.", "731906"),
                           ("Your one-time passcode: 4821", "4821"),
                           ("Use code 482 913 to sign in", "482913"),
                           ("Your code is 4829. Call (555) 123-4567 if this wasn't you.", "4829"),
                           ("Code 482913 expires in 2026", "482913")):
            with self.subTest(text=text):
                self.assertEqual(extract_code(text), code)
        for text in ("Dinner at 7?", "Your order 482913 shipped", "Pay $1234 by Friday", "Meet at 10:42"):
            with self.subTest(text=text):
                self.assertIsNone(extract_code(text))

    def test_masking_hides_every_number_of_a_sign_in_message_only(self):
        self.assertEqual(mask_codes("Your Chase code is 482913.", "#"), "Your Chase code is #.")
        self.assertEqual(mask_codes("G-731906 is your Google verification code.", "#"),
                         "G-# is your Google verification code.")
        self.assertEqual(mask_codes("Rain starting around 4 PM, 1200 mm", "#"), "Rain starting around 4 PM, 1200 mm")

    def test_ages(self):
        now = datetime.datetime(2026, 10, 5, 10, 44).timestamp()
        for text, age in (("now", 0), ("2m ago", 120), ("45s ago", 45), ("1h ago", 3600), ("10:42 AM", 120),
                          ("Yesterday", 86400), ("10:50 AM", 86400 - 360), ("Rain later", None)):
            with self.subTest(text=text):
                self.assertEqual(parse_age(text, now), age)

    def test_default_skills_have_new_operations_and_one_line_prompts(self):
        from mobile_agent.frontier import OPERATIONS
        skills = default_skills()
        self.assertEqual([s.op for s in skills], ["USE_CODE", "READ_NOTIFICATIONS"])
        for skill in skills:
            self.assertNotIn(skill.op, OPERATIONS)
            self.assertTrue(skill.prompt.startswith(f"- {skill.op}"))
            self.assertNotIn("\n", skill.prompt)
        self.assertEqual([s.needs_target for s in skills], [True, False])


class UseCodeTests(unittest.TestCase):
    def phone(self, front="com.chase.sig", nc="notification_center", **options):
        self.clock = FakeClock()
        screens = {"com.chase.sig": screen("signin"), "com.apple.MobileSMS": screen("messages_list"),
                   "com.example.signin": screen("code_boxes"), "com.tinyspeck.chatlyio": screen("composer"),
                   "com.example.shop": screen("search"), "com.apple.mobilemail": screen("search", bundle_id="com.apple.mobilemail"),
                   "com.apple.mobilenotes": screen("search", bundle_id="com.apple.mobilenotes")}
        return FakePhone(front, screens, clock=self.clock, nc=nc, **options)

    def run_skill(self, phone, label="Verification code", text=None, between=None):
        skill = UseCode(clock=self.clock)
        snapshot = phone.screens[phone.front]
        act = field_act(snapshot, label, text)
        prepared = skill.prepare(context(phone), act, snapshot)
        self.assertIsInstance(prepared, Prepared)
        if between:
            between(phone)
        result = skill.perform(context(phone), prepared, act, phone.observe())
        self.assertIsInstance(result, SkillResult)
        return prepared, result

    def assert_no_leak(self, phone, prepared, result):
        for text in (result.feedback, prepared.title or "", repr(prepared), json.dumps(phone.events)):
            self.assertNotIn(CANARY, text)

    def assert_back_in(self, phone, bundle="com.chase.sig"):
        self.assertEqual(phone.front, bundle)
        self.assertFalse(phone.nc_open)
        self.assertEqual(phone.hint, bundle)

    def test_a_code_in_notification_center_is_typed_into_the_code_field_after_approval(self):
        phone = self.phone()
        prepared, result = self.run_skill(phone)
        self.assertEqual(prepared.title, "Use the code Chase sent 2 minutes ago to sign in to this app?")
        self.assertEqual(phone.typed, [CANARY])
        self.assertEqual(result.secrets, (CANARY,))
        self.assertEqual(result.secret_rects, ((0.06, 0.26, 0.88, 0.06),))
        self.assertEqual(result.feedback, "Entered the code from Chase.")
        self.assertTrue(result.changed)
        self.assertIsNone(result.stop)
        self.assertEqual(phone.events, [{"event": "code_used", "source": "notification", "sender": "Chase",
                                         "age_s": 120}])
        self.assert_back_in(phone)
        self.assert_no_leak(phone, prepared, result)
        # Notification Center was only dragged open and closed: no row was tapped.
        self.assertEqual([tap for tap in phone.taps if tap[2]], [])

    def test_the_keyboard_suggestion_comes_first(self):
        phone = self.phone(nc="notification_center", focused=screen("quicktype"))
        prepared, result = self.run_skill(phone)
        self.assertEqual(prepared.title, "Use the code the keyboard suggests from Messages to sign in to this app?")
        self.assertEqual(phone.typed, [CANARY])
        self.assertFalse(any(c[1] == "/actions" and len(c[2]["actions"][0]["actions"]) > 4
                             for c in phone.calls if isinstance(c, tuple) and len(c) == 3),
                         "Notification Center was opened although the keyboard offered the code")
        self.assertEqual(phone.events[0]["source"], "quicktype")
        self.assert_no_leak(phone, prepared, result)

    def test_the_messages_list_is_the_last_source(self):
        phone = self.phone(nc="nc_empty")
        prepared, result = self.run_skill(phone)
        # 10:42 AM, read a few (fake) seconds after 10:44 AM.
        self.assertEqual(prepared.title, "Use the code Chase sent 2 minutes ago to sign in to this app?")
        self.assertEqual(phone.typed, [CANARY])
        self.assertEqual(phone.events[0]["source"], "messages")
        self.assert_back_in(phone)

    def test_one_character_boxes_take_the_code_from_the_first_box(self):
        phone = self.phone(front="com.example.signin")
        snapshot = phone.screens["com.example.signin"]
        skill = UseCode(clock=self.clock)
        act = {"operation": "USE_CODE", "target": snapshot.elements[3].id}
        prepared = skill.prepare(context(phone), act, snapshot)
        self.assertIsNotNone(prepared.title)
        result = skill.perform(context(phone), prepared, act, phone.observe())
        self.assertEqual(phone.typed, [CANARY])
        first = snapshot.elements[1]
        self.assertIn((round(first.center[0], 3), round(first.center[1], 3), False), phone.taps)
        x, y, w, h = result.secret_rects[0]
        self.assertAlmostEqual(x, .08)
        self.assertAlmostEqual(x + w, .08 + 5 * .145 + .12)

    def refused(self, phone, label="Verification code", text=None, between=None):
        prepared, result = self.run_skill(phone, label, text, between)
        self.assertEqual(phone.typed, [], result.feedback)
        self.assertEqual(result.secrets, ())
        self.assertFalse(result.changed)
        self.assertTrue(result.feedback.startswith("USE_CODE typed nothing: "), result.feedback)
        self.assert_no_leak(phone, prepared, result)
        return prepared, result

    def test_a_composer_or_a_search_field_is_refused_before_looking_anywhere(self):
        for front, label, words in (("com.tinyspeck.chatlyio", "Message #general", "message or text box"),
                                    ("com.example.shop", "Search", "search field")):
            with self.subTest(front=front):
                phone = self.phone(front=front)
                prepared, result = self.refused(phone, label)
                self.assertIsNone(prepared.title)
                self.assertIn(words, result.feedback)
                self.assertFalse(phone.nc_open)
                self.assertNotIn(("POST", "/wda/apps/activate", {"bundleId": "com.apple.MobileSMS"}), phone.calls)

    def test_messages_mail_and_notes_are_refused(self):
        for bundle, name in (("com.apple.mobilemail", "Mail"), ("com.apple.mobilenotes", "Notes")):
            with self.subTest(app=name):
                phone = self.phone(front=bundle)
                _prepared, result = self.refused(phone, "Search")
                self.assertIn(f"never types a code into {name}", result.feedback)
        phone = self.phone(front="com.apple.MobileSMS")
        _prepared, result = self.refused(phone, "Search")
        self.assertIn("never types a code into Messages", result.feedback)

    def test_password_pin_promo_and_payment_fields_are_refused(self):
        for label, words in (("Password", "password, PIN or card"), ("PIN", "password, PIN or card"),
                             ("Promo code", "promo, gift"), ("Card verification code", "password, PIN or card")):
            with self.subTest(label=label):
                phone = self.phone()
                signin = phone.screens["com.chase.sig"]
                field = next(e for e in signin.elements if e.label == "Verification code")
                phone.screens["com.chase.sig"] = dataclasses.replace(
                    signin, elements=[dataclasses.replace(e, label=label) if e is field else e
                                      for e in signin.elements])
                _prepared, result = self.refused(phone, label)
                self.assertIn(words, result.feedback)
        phone = self.phone()
        signin = phone.screens["com.chase.sig"]
        phone.screens["com.chase.sig"] = dataclasses.replace(signin, text=signin.text + "\nCard number\nExpiration")
        _prepared, result = self.refused(phone)
        self.assertIn("payment form", result.feedback)

    def test_a_field_that_does_not_look_like_a_code_field_is_refused(self):
        phone = self.phone()
        signin = phone.screens["com.chase.sig"]
        phone.screens["com.chase.sig"] = dataclasses.replace(signin, elements=[
            dataclasses.replace(e, label="Nickname", placeholder="") if e.label == "Verification code" else e
            for e in signin.elements])
        _prepared, result = self.refused(phone, "Nickname")
        self.assertIn("doesn't look like a verification-code field", result.feedback)

    def test_the_app_in_front_changing_after_the_approval_types_nothing(self):
        phone = self.phone()

        def switch(p):
            p.front = "com.tinyspeck.chatlyio"
        prepared, result = self.refused(phone, between=switch)
        self.assertIsNotNone(prepared.title)  # it had been found and approved
        self.assertIn("app in front changed", result.feedback)

    def test_a_stale_code_is_refused(self):
        phone = self.phone(nc="nc_stale")
        phone.screens["com.apple.MobileSMS"] = screen("messages_list", elements=[])
        _prepared, result = self.refused(phone)
        self.assertIn("older than 10 minutes", result.feedback)

    def test_a_code_that_goes_stale_while_the_user_decides_is_not_typed(self):
        phone = self.phone()
        _prepared, result = self.refused(phone, between=lambda p: self.clock.sleep(500))
        self.assertIn("older than 10 minutes now", result.feedback)

    def test_two_codes_without_a_sender_are_refused_and_a_sender_picks_one(self):
        phone = self.phone(nc="nc_two_codes")
        _prepared, result = self.refused(phone)
        self.assertIn("Two different codes", result.feedback)
        phone = self.phone(nc="nc_two_codes")
        prepared, result = self.run_skill(phone, text="Chase")
        self.assertEqual(phone.typed, [CANARY])
        self.assertIn("Chase", prepared.title)
        phone = self.phone(nc="nc_two_codes")
        prepared, result = self.run_skill(phone, text="Google")
        self.assertEqual(phone.typed, ["731906"])
        self.assertNotIn("731906", prepared.title + result.feedback)

    def test_no_code_anywhere_says_so(self):
        phone = self.phone(nc="nc_empty")
        phone.screens["com.apple.MobileSMS"] = screen("messages_list", elements=[])
        _prepared, result = self.refused(phone)
        self.assertIn("No verification code arrived in the last 10 minutes", result.feedback)
        self.assert_back_in(phone)

    def test_the_title_never_holds_the_code_even_when_the_sender_looks_like_one(self):
        found = codes.Found(code=CANARY, sender=codes._clean_sender("482913", CANARY), age_s=30,
                            source="messages", bundle="com.chase.sig",
                            field=codes.Field("Code", "TextField", (0, 0, .5, .1)), found_at=0)
        self.assertNotIn(CANARY, approval_title(found, "Chase") + repr(found))

    def test_an_app_that_cannot_be_named_is_refused_before_looking_anywhere(self):
        # Messages in front, but neither the screen nor WebDriverAgent names it: the app checks can't hold.
        phone = self.phone(front="com.apple.MobileSMS")
        phone.screens["com.apple.MobileSMS"] = screen("signin", bundle_id="")

        def unknown(timeout=5):
            raise RuntimeError("WDA did not report a foreground app")
        phone.active_app = unknown
        skill = UseCode(clock=self.clock)
        snapshot = phone.screens["com.apple.MobileSMS"]
        ctx = dataclasses.replace(context(phone), app_bundle=None)
        prepared = skill.prepare(ctx, field_act(snapshot), snapshot)
        self.assertIsNone(prepared.title)
        result = skill.perform(ctx, prepared, field_act(snapshot), phone.observe())
        self.assertEqual(result.feedback, "USE_CODE typed nothing: Mobster couldn't tell which app is in front, so "
                                          "it typed no code.")
        self.assertEqual((phone.typed, phone.taps, phone.nc_open), ([], [], False))

    def test_the_code_field_is_tapped_even_when_the_keyboard_is_already_up(self):
        # The keyboard is up (for whichever field had the focus): keys go to the focused field, so the code
        # field is tapped first, every time.
        phone = self.phone(focused=screen("quicktype"))
        phone.focused = True
        snapshot = phone.observe()
        self.assertEqual(snapshot.keyboard, "visible")
        skill = UseCode(clock=self.clock)
        act = field_act(snapshot)
        prepared = skill.prepare(context(phone), act, snapshot)
        before = len(phone.calls)
        skill.perform(context(phone), prepared, act, phone.observe())
        performed = [c for c in phone.calls[before:] if isinstance(c, tuple)]
        keys = next(i for i, c in enumerate(performed) if c[1] == "/wda/keys")
        self.assertIn(("tap", .5, .29), performed[:keys])
        self.assertEqual(phone.typed, [CANARY])

    def test_a_typing_error_still_masks_the_code_and_blurs_its_field(self):
        phone = self.phone()
        original = phone.call

        def call(method, path, body=None, timeout=10):
            original(method, path, body, timeout)
            if path == "/wda/keys":
                raise TimeoutError(f"no answer to {json.dumps(body)}")
        phone.call = call
        prepared, result = self.run_skill(phone)
        self.assertEqual(result.feedback, "USE_CODE couldn't type the code (TimeoutError). Look at the field before "
                                          "trying again.")
        self.assertEqual(result.secrets, (CANARY,))
        self.assertEqual(result.secret_rects, ((0.06, 0.26, 0.88, 0.06),))
        self.assert_no_leak(phone, prepared, result)

    def test_ages_read_the_way_people_say_them(self):
        for seconds, said in ((0, "just now"), (4, "just now"), (40, "40 seconds ago"), (60, "1 minute ago"),
                              (125, "2 minutes ago"), (599, "9 minutes ago")):
            with self.subTest(seconds=seconds):
                self.assertEqual(codes.say_age(seconds), said)

    def test_find_code_refusals_are_code_refused(self):
        phone = self.phone(front="com.example.shop")
        snapshot = phone.screens["com.example.shop"]
        with self.assertRaises(CodeRefused):
            codes.find_code(phone, field=codes.Field.of(snapshot.elements[0]), bundle="com.example.shop",
                            snapshot=snapshot, clock=self.clock)


if __name__ == "__main__":
    unittest.main()

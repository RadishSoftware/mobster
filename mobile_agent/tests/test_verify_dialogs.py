"""A check's `dialogs:` block: the answers for every policy value, on a simulator and on an iPhone, in Smart's turns and
in a replay that meets a dialog it has not seen. Offline: dialogs are the texts and buttons iOS shows, and the phone is
a fake that records what is pressed."""

import copy
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import quoteattr

import yaml

from mobile_agent.replay_core.engine import Replayer
from mobile_agent.state import Element, Snapshot, from_wda_root
from mobile_agent.tests.test_native_capabilities import AlertPhone
from mobile_agent.tests.test_verify import PAYWALL
from mobile_agent.verify import conditions, dialogs, fixtures
from mobile_agent.verify.checks import CheckError, check_from_dict, dump_check, load_check
from mobile_agent.wda import system_dialogs as sd

BUNDLE = "com.example.shop"
LOCATION = ("Allow “Shop” to use your location?", ["Allow Once", "Allow While Using App", "Don’t Allow"])
NOTIFY = ("“Shop” Would Like to Send You Notifications", ["Don’t Allow", "Allow"])
PASTE = ("“Shop” would like to paste from “Notes”", ["Don’t Allow Paste", "Allow Paste"])
APPLE = ("Sign in with Apple. Do you want to sign in to “Shop” with your Apple Account?",
         ["Cancel", "Continue"])
BUY = ("Confirm Your In-App Purchase. Premium, $4.99 [Environment: Xcode]", ["Cancel", "Buy"])
BOUGHT = ("You’re all set. Your purchase was successful. [Environment: Sandbox]", ["OK"])
FACE = ("Try Face ID Again", ["Cancel", "Try Face ID Again"])
OPEN_IN = ("Open in “Shop”?", ["Cancel", "Open"])
APP_OWN = ("Delete this post?", ["Cancel", "Delete"])
IOS = sd.SPRINGBOARD                      # in front while iOS shows an alert
# Alerts an app (or a web page's confirm()) can show with grant words or Allow buttons: never iOS's to answer.
APP_GRANT = ("Share your current location to finish checkout? Your card will be charged", ["Cancel", "OK"])
APP_ALLOW = ("Allow Shop to transfer $500?", ["Don’t Allow", "Allow"])
APP_PHOTOS = ("Delete all your photos from this album?", ["Cancel", "OK"])
STOREKIT = sd.STOREKIT                    # in front while iOS shows a purchase sheet
SAFARI = "com.apple.SafariViewService"    # a view service too, but its web content is the page's own


def dialog(pair):
    return sd.classify(*pair)


def policy(named=None, *, simulator=True, sent=None):
    send = (lambda action: sent.append(action)) if sent is not None else None
    return dialogs.Policy(dialogs.parse(named), simulator=simulator, biometric=send if simulator else None)


def answer(made):
    return (made.action, made.button)


def wda_tree(bundle, *nodes):
    """A screen as WebDriverAgent's source for ``bundle`` reads it: (type, label, (x, y, w, h)) on 400 x 800 points."""
    body = "".join(f'<XCUIElementType{kind} label={quoteattr(label)} x="{x}" y="{y}" width="{w}" height="{h}" '
                   'visible="true" enabled="true" accessible="true"/>' for kind, label, (x, y, w, h) in nodes)
    return from_wda_root(ET.fromstring(
        f'<XCUIElementTypeApplication bundleId="{bundle}" x="0" y="0" width="400" height="800" visible="true">'
        f'<XCUIElementTypeWindow x="0" y="0" width="400" height="800" visible="true">{body}'
        '</XCUIElementTypeWindow></XCUIElementTypeApplication>'))


class FrontPhone(AlertPhone):
    """An AlertPhone whose WebDriverAgent reports ``front`` in front whatever is up; None: it can't say."""

    def __init__(self, pair, front):
        super().__init__(pair, active=BUNDLE)
        self.front = front

    def active_app(self, timeout=5):
        if self.front is None:
            raise RuntimeError("WebDriverAgent did not report a foreground app")
        return self.front


# The app's screen with words that read like a dialog, so a replay looks for one; the alert itself isn't in it.
SUSPECT = Snapshot([Element("t", "We use your location for delivery", "StaticText", (.1, .3, .8, .04))], "Shop",
                   400, 800, "wda", bundle_id=BUNDLE)


class ParseTests(unittest.TestCase):
    def test_every_key_takes_its_own_answers(self):
        block = {"permissions": "deny", "paste": "allow", "sign_in_with_apple": "cancel", "biometric": "nonmatch",
                 "purchase": "cancel"}
        self.assertEqual(dialogs.parse(block), block)
        self.assertEqual(dialogs.parse(None), {})

    def test_a_typo_or_a_wrong_answer_is_refused_with_the_fix(self):
        with self.assertRaisesRegex(CheckError, "did you mean permissions"):
            dialogs.parse({"permisions": "allow"})
        with self.assertRaisesRegex(CheckError, "dialogs.purchase 'buy' isn't an answer; use confirm, cancel"):
            dialogs.parse({"purchase": "buy"})
        with self.assertRaisesRegex(CheckError, "must name an answer"):
            dialogs.parse({})
        with self.assertRaises(CheckError):
            dialogs.parse(["allow"])

    def test_a_later_block_wins_key_by_key(self):
        self.assertEqual(dialogs.merge({"paste": "deny", "purchase": "cancel"}, {"paste": "allow"}),
                         {"paste": "allow", "purchase": "cancel"})


class SimulatorPolicyTests(unittest.TestCase):
    def test_the_defaults_allow_paste_continue_match_and_confirm(self):
        sent = []
        made = policy(sent=sent)
        self.assertEqual(answer(made.decide(dialog(LOCATION), front=IOS)), ("press", "Allow Once"))
        self.assertEqual(answer(made.decide(dialog(NOTIFY), front=IOS)), ("press", "Allow"))
        self.assertEqual(answer(made.decide(dialog(PASTE), front=IOS)), ("press", "Allow Paste"))
        self.assertEqual(answer(made.decide(dialog(APPLE), front=IOS)), ("press", "Continue"))
        self.assertEqual(answer(made.decide(dialog(BUY), front=IOS)), ("press", "Buy"))
        self.assertEqual(answer(made.decide(dialog(BOUGHT), front=IOS)), ("press", "OK"))
        face = made.decide(dialog(FACE), front=IOS)
        self.assertEqual(answer(face), ("press", "match"))
        self.assertEqual(sent, [])               # deciding sends nothing
        sd.press(None, face)
        self.assertEqual(sent, ["match"])        # pressing sends the event, not a tap

    def test_each_named_value_gets_its_button(self):
        sent = []
        cases = [({"permissions": "deny"}, LOCATION, "Don’t Allow"), ({"permissions": "deny"}, NOTIFY, "Don’t Allow"),
                 ({"permissions": "allow"}, LOCATION, "Allow Once"), ({"paste": "deny"}, PASTE, "Don’t Allow Paste"),
                 ({"paste": "allow"}, PASTE, "Allow Paste"), ({"sign_in_with_apple": "cancel"}, APPLE, "Cancel"),
                 ({"sign_in_with_apple": "continue"}, APPLE, "Continue"), ({"purchase": "cancel"}, BUY, "Cancel"),
                 ({"purchase": "confirm"}, BUY, "Buy"), ({"biometric": "cancel"}, FACE, "Cancel")]
        for named, pair, button in cases:
            with self.subTest(named=named, dialog=pair[0][:30]):
                self.assertEqual(answer(policy(named, sent=sent).decide(dialog(pair), front=IOS)), ("press", button))
        for action in ("match", "nonmatch"):
            with self.subTest(biometric=action):
                made = policy({"biometric": action}, sent=sent).decide(dialog(FACE), front=IOS)
                sd.press(None, made)
        self.assertEqual(sent, ["match", "nonmatch"])

    def test_a_purchase_confirm_presses_only_buy_or_subscribe(self):
        for buttons in (["Cancel", "Continue"], ["Cancel", "OK"]):
            made = policy({"purchase": "confirm"}).decide(dialog(("Confirm Your In-App Purchase", buttons)), front=IOS)
            self.assertEqual(made.action, "stop", buttons)
        self.assertEqual(answer(policy({"purchase": "confirm"}).decide(dialog(BUY), front=IOS)), ("press", "Buy"))

    def test_ask_means_the_old_rule_and_a_check_has_nobody_to_ask_so_a_grant_stops(self):
        made = policy({"permissions": "ask"}).decide(dialog(LOCATION), front=IOS)
        self.assertEqual(made.action, "stop")
        self.assertIn("iOS is asking", made.reason)

    def test_a_sheet_without_the_button_the_answer_needs_stops(self):
        made = policy({"purchase": "cancel"}).decide(dialog(("Confirm Your In-App Purchase", ["Buy"])), front=IOS)
        self.assertEqual(made.action, "stop")
        self.assertIn("no cancel button", made.reason)

    def test_an_apps_own_alert_is_left_to_the_step_and_a_link_prompt_follows_permissions(self):
        self.assertEqual(policy().decide(dialog(APP_OWN), front=IOS).action, "leave")
        self.assertEqual(answer(policy().decide(dialog(OPEN_IN), front=IOS)), ("press", "Open"))
        denied = policy({"permissions": "deny"}).decide(dialog(OPEN_IN), front=IOS)
        self.assertEqual(answer(denied), ("press", "Cancel"))
        self.assertEqual(policy(simulator=False).decide(dialog(OPEN_IN), front=IOS).action, "leave")

    def test_apple_account_passwords_and_trust_are_never_pressed_whatever_the_policy_says(self):
        made = policy({"permissions": "allow", "sign_in_with_apple": "continue"})
        for pair in (("Sign In to Apple Account", ["Cancel", "Sign In"]),
                     ("Trust This Computer?", ["Trust", "Don’t Trust"])):
            self.assertEqual(made.decide(dialog(pair), front=IOS).action, "stop")
        self.assertEqual(answer(made.decide(dialog(("Save Password?", ["Save Password", "Not Now"])), front=IOS)),
                         ("press", "Not Now"))


class IphonePolicyTests(unittest.TestCase):
    """A phone answers only what the check names, and never buys or sends a match."""

    def phone(self, named=None):
        return policy(named, simulator=False)

    def test_nothing_is_confirmed_unnamed(self):
        made = self.phone()
        self.assertEqual(made.decide(dialog(BUY), front=IOS).action, "stop")
        self.assertEqual(made.decide(dialog(APPLE), front=IOS).action, "stop")
        self.assertEqual(made.decide(dialog(FACE), front=IOS).action, "stop")
        self.assertEqual(made.decide(dialog(BOUGHT), front=IOS).action, "stop")

    def test_a_purchase_is_never_confirmed_on_a_phone_even_when_named(self):
        made = self.phone({"purchase": "confirm"}).decide(dialog(BUY), front=IOS)
        self.assertEqual(made.action, "stop")
        self.assertIn("never confirms a purchase", made.reason)
        self.assertEqual(answer(self.phone({"purchase": "cancel"}).decide(dialog(BUY), front=IOS)), ("press", "Cancel"))

    def test_a_face_id_match_cannot_be_sent_to_a_phone_but_cancel_can_be_pressed(self):
        for action in ("match", "nonmatch"):
            made = self.phone({"biometric": action}).decide(dialog(FACE), front=IOS)
            self.assertEqual(made.action, "stop")
            self.assertIn("can't send a match to a phone", made.reason)
        cancel = self.phone({"biometric": "cancel"}).decide(dialog(FACE), front=IOS)
        self.assertEqual(answer(cancel), ("press", "Cancel"))

    def test_a_named_sign_in_and_a_named_permission_are_answered(self):
        self.assertEqual(answer(self.phone({"sign_in_with_apple": "cancel"}).decide(dialog(APPLE), front=IOS)),
                         ("press", "Cancel"))
        self.assertEqual(answer(self.phone({"permissions": "deny"}).decide(dialog(LOCATION), front=IOS)),
                         ("press", "Don’t Allow"))

    def test_allow_and_continue_are_pressed_on_a_phone_only_when_the_check_names_them(self):
        # docs/testing.mdx, "What a simulator may do that an iPhone may not": these two press on a real phone.
        self.assertEqual(answer(self.phone({"permissions": "allow"}).decide(dialog(LOCATION), front=IOS)),
                         ("press", "Allow Once"))
        self.assertEqual(answer(self.phone({"sign_in_with_apple": "continue"}).decide(dialog(APPLE), front=IOS)),
                         ("press", "Continue"))
        self.assertEqual(self.phone().decide(dialog(LOCATION), front=IOS).action, "leave")
        self.assertEqual(self.phone().decide(dialog(APPLE), front=IOS).action, "stop")

    def test_a_phone_policy_handed_a_biometric_sender_still_sends_nothing(self):
        sent = []
        made = dialogs.Policy({"biometric": "match"}, simulator=False, biometric=sent.append)
        self.assertEqual(made.decide(dialog(FACE), front=IOS).action, "stop")
        self.assertEqual(sent, [])

    def test_open_in_another_app_on_a_phone_only_for_a_link_mobster_opened(self):
        phone = self.phone({"permissions": "allow"})
        opened = phone.decide(dialog(OPEN_IN), front=IOS, recent_=sd.Recent(opened_at=100.0), now=105.0)
        self.assertEqual(answer(opened), ("press", "Open"))
        for recent in (sd.Recent(), sd.Recent(opened_at=50.0)):
            self.assertEqual(phone.decide(dialog(OPEN_IN), front=IOS, recent_=recent, now=105.0).action, "leave")

    def test_an_unnamed_permission_alert_is_left_to_the_step_as_before(self):
        self.assertEqual(self.phone().decide(dialog(LOCATION), front=IOS).action, "leave")

    def test_allow_paste_stays_for_text_mobster_put_on_the_clipboard(self):
        recent = sd.Recent(pasted_at=100.0)
        allowed = self.phone({"paste": "allow"}).decide(dialog(PASTE), front=IOS, recent_=recent, now=105.0)
        self.assertEqual(answer(allowed), ("press", "Allow Paste"))
        refused = self.phone({"paste": "allow"}).decide(dialog(PASTE), front=IOS, recent_=sd.Recent(), now=105.0)
        self.assertEqual(answer(refused), ("press", "Don’t Allow Paste"))


def sheet(rows, buttons, bundle=BUNDLE):
    """A screen the way a tree reads it: static text lines above the buttons of a bottom sheet, in ``bundle``'s tree."""
    elements = [Element(f"t{i}", text, "StaticText", (.1, .5 + i * .05, .8, .04)) for i, text in enumerate(rows)]
    elements += [Element(f"b{i}", name, "Button", (.1 + i * .4, .8, .3, .06)) for i, name in enumerate(buttons)]
    return Snapshot(elements, "sheet", 400, 800, "wda", bundle_id=bundle)


class AppleAccountPrecedenceTests(unittest.TestCase):
    """A purchase or sign-in sheet that names an Apple Account or asks for a password never becomes a sheet a task or
    a check can confirm: a password prompt is NEVER answered, and an agent task stops at a purchase."""

    NAMED = ("Confirm Your In-App Purchase. Do you want to buy one Gems for $0.99? Your Apple Account will be charged. "
             "[Environment: Sandbox]", ["Cancel", "Buy"])
    SIDE = ("Apple Account. Confirm with Side Button to subscribe", ["Cancel", "Subscribe"])
    PASSWORD = ("Enter your Apple Account password to confirm your purchase", ["Cancel", "OK"])

    def test_an_agent_task_stops_at_each_and_presses_nothing(self):
        for pair in (self.NAMED, self.SIDE, self.PASSWORD):
            made = sd.decide(dialog(pair), sd.ASK_IMPORTANT, can_ask=True)
            self.assertEqual((made.action, made.button), ("stop", ""), pair[0])

    def test_a_check_on_an_iphone_stops_even_when_it_names_confirm(self):
        for pair in (self.NAMED, self.SIDE, self.PASSWORD):
            phone = policy({"purchase": "confirm"}, simulator=False)
            self.assertEqual(phone.decide(dialog(pair), front=IOS).action, "stop")

    def test_a_simulator_never_presses_ok_on_a_password_prompt(self):
        self.assertEqual(dialog(self.PASSWORD).kind, "apple_account")
        for named in (None, {"purchase": "confirm"}, {"sign_in_with_apple": "continue"}):
            self.assertEqual(policy(named).decide(dialog(self.PASSWORD), front=IOS).action, "stop")

    def test_a_sheet_that_only_names_the_account_stays_a_sheet(self):
        self.assertEqual(dialog(APPLE).kind, "sign_in_with_apple")
        self.assertEqual(dialog(self.NAMED).kind, "purchase")
        self.assertEqual(answer(policy().decide(dialog(self.NAMED), front=IOS)), ("press", "Buy"))


class AppsOwnAlertTests(unittest.TestCase):
    """The word "password" or "you're all set" alone is an app's alert: a task and a check leave it to the step, as
    before the dialogs block. Apple's own password prompts, Trust This Computer and the purchase sheet still stop."""

    APPS_OWN = (("Incorrect password. Please try again.", ["OK"]), ("Wrong Password", ["OK"]),
                ("Forgot your password?", ["Cancel", "Reset"]), ("Your password must be 8 characters", ["OK"]),
                ("You’re all set. Your purchase was successful.", ["OK"]))
    APPLES = (("Sign In to iTunes Store. Enter the Apple ID password for “sam@example.com”.", ["Cancel", "OK"]),
              ("Apple Account Sign In Requested. Enter the password for your Apple Account “sam@example.com” in "
               "Settings.", ["Not Now", "Settings"]),
              ("Enter your Apple Account password to confirm your purchase", ["Cancel", "OK"]),
              ("Trust This Computer?", ["Trust", "Don’t Trust"]))

    def test_an_apps_own_password_or_all_set_alert_is_left_to_the_step(self):
        for pair in self.APPS_OWN:
            with self.subTest(alert=pair[0]):
                self.assertEqual(dialog(pair).kind, "unknown")
                self.assertEqual(sd.decide(dialog(pair), sd.ASK_IMPORTANT, can_ask=True, in_app=True).action, "leave")
                self.assertEqual(policy(simulator=False).decide(dialog(pair), front=IOS).action, "leave")
                self.assertEqual(policy({"purchase": "confirm"}).decide(dialog(pair), front=IOS).action, "leave")

    def test_apples_own_password_prompts_trust_and_the_purchase_sheet_still_stop(self):
        for pair in (*self.APPLES, AppleAccountPrecedenceTests.NAMED, BUY):
            for in_app in (True, False):
                with self.subTest(alert=pair[0][:40], in_app=in_app):
                    made = sd.decide(dialog(pair), sd.ASK_IMPORTANT, can_ask=True, in_app=in_app)
                    self.assertEqual((made.action, made.button), ("stop", ""))
        for pair in self.APPLES:
            with self.subTest(check=pair[0][:40]):
                named = {"purchase": "confirm", "sign_in_with_apple": "continue", "permissions": "allow"}
                self.assertEqual(policy(named).decide(dialog(pair), front=IOS).action, "stop")
        self.assertEqual(dialog(BOUGHT).kind, "purchase")   # StoreKit's own all-set alert carries its environment


class WhoseDialogTests(unittest.TestCase):
    """A press goes only to a dialog iOS shows. An app's own alert is the step's, under the check's approver, whatever
    its words or buttons say; on an iPhone a press also needs SpringBoard or an Apple view service in front."""

    def test_on_a_phone_a_named_allow_presses_only_iOS_alerts(self):
        phone = policy({"permissions": "allow"}, simulator=False)
        self.assertEqual(answer(phone.decide(dialog(LOCATION), front=IOS)), ("press", "Allow Once"))
        for pair in (APP_GRANT, APP_ALLOW, APP_PHOTOS, LOCATION):
            with self.subTest(alert=pair[0][:40]):
                self.assertNotEqual(dialog(pair).kind, "unknown")
                self.assertEqual(phone.decide(dialog(pair), in_app=True, front=IOS).action, "leave")
                self.assertEqual(phone.decide(dialog(pair), front=BUNDLE).action, "leave")
                self.assertEqual(phone.decide(dialog(pair)).action, "leave")      # nobody said what is in front

    def test_a_simulator_leaves_the_apps_own_alert_too(self):
        self.assertEqual(answer(policy().decide(dialog(LOCATION), front=IOS)), ("press", "Allow Once"))
        for pair in (APP_GRANT, APP_ALLOW, APP_PHOTOS):
            self.assertEqual(policy().decide(dialog(pair), front=IOS, in_app=True).action, "leave")

    def test_whose_alert_it_is_comes_from_the_tree_its_text_sits_in(self):
        text = Element("t", APP_PHOTOS[0], "StaticText", (.1, .4, .8, .04))
        app = Snapshot([text], "Shop", 400, 800, "wda", bundle_id=BUNDLE)
        board = Snapshot([text], "SpringBoard", 400, 800, "wda", bundle_id=IOS)
        self.assertTrue(sd.in_app(dialog(APP_PHOTOS), app))
        self.assertFalse(sd.in_app(dialog(APP_PHOTOS), board))
        self.assertFalse(sd.in_app(dialog(LOCATION), app))


class AppleAccountFaceIdTests(unittest.TestCase):
    """An Apple Account beside Face ID or Touch ID is an Apple Account prompt: a task stops at it, as on main."""

    TEXTS = (("Apple Account. Continue with Face ID to sign in", ["Cancel", "Continue"]),
             ("Apple ID. Sign in with Touch ID to continue", ["Cancel", "Continue"]))

    def test_an_agent_task_stops_and_presses_nothing(self):
        for pair in self.TEXTS:
            self.assertEqual(dialog(pair).kind, "apple_account")
            for mode in (sd.ASK_IMPORTANT, sd.DONT_ASK):
                made = sd.decide(dialog(pair), mode, can_ask=True)
                self.assertEqual((made.action, made.button), ("stop", ""), (pair[0], mode))

    def test_a_face_id_prompt_alone_still_gets_the_checks_answer(self):
        self.assertEqual(dialog(FACE).kind, "biometric")
        self.assertEqual(answer(policy({"biometric": "cancel"}).decide(dialog(FACE), front=IOS)), ("press", "Cancel"))


class MostRestrictiveFirstTests(unittest.TestCase):
    """What Mobster never answers, a Screen Time limit, a purchase and Sign in with Apple outrank every grant, and a
    password field makes any dialog a password prompt."""

    def test_a_password_prompt_or_purchase_sheet_that_names_a_grant_is_never_pressed_as_one(self):
        cases = ((("Sign In to iTunes Store. Enter the Apple ID password for “health@example.com”", ["Cancel", "OK"]),
                  "apple_account"),
                 (("Apple Music. Enter the password for your Apple Account “sam@example.com”", ["Cancel", "OK"]),
                  "apple_account"),
                 (("Apple Music. Confirm with Side Button", ["Cancel", "Continue"]), "purchase"))
        for pair, kind in cases:
            with self.subTest(dialog=pair[0][:40]):
                self.assertEqual(dialog(pair).kind, kind)
                named = {"permissions": "allow", "purchase": "confirm"}
                self.assertEqual(policy(named, simulator=False).decide(dialog(pair), front=IOS).action, "stop")
                self.assertEqual(policy(named).decide(dialog(pair), front=IOS).action, "stop")
        self.assertEqual(dialog(("“Shop” would like to access Apple Music", ["Don’t Allow", "OK"])).kind, "media")

    def test_a_password_field_makes_any_dialog_a_password_prompt(self):
        # Through WebDriverAgent's own XML: a password field is no element, but where it sits is kept.
        field = ("SecureTextField", "", (40, 560, 320, 40))
        board = wda_tree(IOS, ("StaticText", "Sign in to continue", (40, 300, 320, 20)), field)
        self.assertEqual(len(board.secure_fields), 1)
        self.assertFalse([e for e in board.elements if e.role == "SecureTextField"])
        phone = FrontPhone(("Sign in to continue", ["Cancel", "Sign In"]), IOS)
        self.assertEqual(sd.read(phone, snapshot=board).kind, "password_field")
        self.assertEqual(sd.read(phone, snapshot=wda_tree(BUNDLE, field)).kind, "unknown")   # the app's own sign-in
        sheet_rows = (("StaticText", "Sign in with Apple", (40, 400, 320, 20)),
                      ("StaticText", "Sign in to “Shop” with your Apple Account", (40, 440, 320, 20)),
                      ("Button", "Cancel", (40, 640, 150, 48)), ("Button", "Continue", (210, 640, 150, 48)))
        self.assertEqual(policy().sheet(wda_tree(sd.AUTHKIT, *sheet_rows), sd.AUTHKIT).kind, "sign_in_with_apple")
        found = policy().sheet(wda_tree(sd.AUTHKIT, *sheet_rows, field), sd.AUTHKIT)
        self.assertEqual(found.kind, "password_field")
        for named in (policy({"sign_in_with_apple": "continue"}),
                      policy({"sign_in_with_apple": "continue"}, simulator=False)):
            self.assertEqual(named.decide(found, front=sd.AUTHKIT).action, "stop")
        self.assertEqual(sd.decide(found, sd.DONT_ASK, can_ask=True).action, "stop")

    def test_apples_email_and_password_sign_in_stops(self):
        pair = ("Sign In with Apple Account. Enter your Apple Account email and password", ["Cancel", "Continue"])
        self.assertEqual(dialog(pair).kind, "apple_account")
        for named in (policy({"sign_in_with_apple": "continue"}),
                      policy({"sign_in_with_apple": "continue"}, simulator=False)):
            self.assertEqual(named.decide(dialog(pair), front=IOS).action, "stop")
        self.assertEqual(sd.decide(dialog(pair), sd.DONT_ASK, can_ask=True).action, "stop")
        self.assertEqual(dialog(("Enter your email and password", ["OK"])).kind, "unknown")   # an app's own


class FrontTests(unittest.TestCase):
    """Whose dialog it is comes from exact bundles WebDriverAgent reports, never a pattern or a guess."""

    TRICKS = ("com.apple.evil", "com.example.com.apple.ViewService", "com.apple.springboard.x",
              "com.apple.springboard\n", " com.apple.springboard", SAFARI, "com.apple.evilViewService",
              "com.apple.ios.StoreKitUIService.x", "", None, 7)
    KINDS = ("location", "permission", "purchase", "sign_in_with_apple", "biometric", "apple_account")

    def test_only_the_exact_apple_process_for_each_kind_counts(self):
        for bundle in self.TRICKS:
            for kind in self.KINDS:
                with self.subTest(bundle=bundle, kind=kind):
                    self.assertFalse(sd.shows_alert(kind, bundle))
                    self.assertFalse(sd.shows_sheet(kind, bundle))
        self.assertTrue(sd.shows_alert("location", IOS))
        self.assertTrue(sd.shows_alert("purchase", STOREKIT))
        self.assertTrue(sd.shows_alert("biometric", sd.CORE_AUTH))
        self.assertTrue(sd.shows_sheet("purchase", STOREKIT))
        self.assertTrue(sd.shows_sheet("sign_in_with_apple", sd.AUTHKIT))
        self.assertTrue(sd.shows_sheet("biometric", sd.CORE_AUTH))
        self.assertTrue(sd.shows_sheet("apple_account", sd.AUTHKIT))        # it only stops the run
        self.assertFalse(sd.shows_alert("location", STOREKIT))
        self.assertFalse(sd.shows_sheet("purchase", sd.AUTHKIT))
        self.assertFalse(sd.shows_sheet("location", IOS))

    def test_a_front_webdriveragent_cannot_name_is_none_and_nothing_is_pressed(self):
        self.assertIsNone(sd.front(FrontPhone(LOCATION, None)))
        self.assertIsNone(sd.front(object()))
        self.assertIsNone(sd.front(SimpleNamespace(active_app=lambda timeout=2: "")))
        self.assertIsNone(sd.front(SimpleNamespace(active_app=lambda timeout=2: 5)))
        for made in (policy(), policy({"permissions": "allow"}, simulator=False)):
            self.assertEqual(made.decide(dialog(LOCATION), front=None).action, "leave")

    def test_a_replay_and_smart_press_only_with_springboard_in_front(self):
        phone = FrontPhone(LOCATION, IOS)
        self.assertEqual(dialogs.Responder(policy(), phone).clear(SUSPECT, thorough=True)[1], 1)
        self.assertEqual(phone.pressed(), ["Allow Once"])
        for front in (BUNDLE, None, SAFARI):
            for named, simulator in ((None, True), ({"permissions": "allow"}, False)):
                with self.subTest(front=front, simulator=simulator):
                    phone = FrontPhone(LOCATION, front)
                    self.assertEqual(dialogs.Responder(policy(named, simulator=simulator), phone)
                                     .clear(SUSPECT, thorough=True)[1], 0)
                    agent, _, _ = FrontierTests.agent(self, phone, named, simulator=simulator)
                    self.assertIsNone(agent._system_dialog(SUSPECT, 1, [])[0])
                    self.assertEqual(phone.pressed(), [])

    def test_a_phone_never_presses_ok_on_the_all_set_alert(self):
        self.assertEqual(answer(policy().decide(dialog(BOUGHT), front=STOREKIT)), ("press", "OK"))
        for named in ({"purchase": "cancel"}, {"purchase": "confirm"}):
            for front in (IOS, STOREKIT):
                made = policy(named, simulator=False).decide(dialog(BOUGHT), front=front)
                self.assertEqual(made.action, "stop", (named, front))


class SafariTests(unittest.TestCase):
    """A web page in SFSafariViewController or ASWebAuthenticationSession is the page's, though Safari's process is a
    view service: its confirm() and its look-alike sheets are never answered."""

    PAGES = (APP_GRANT, APP_PHOTOS, ("Submit your health insurance claim and pay $40?", ["Cancel", "OK"]),
             ("Pair over Bluetooth and pay $20 now?", ["Cancel", "Continue"]))

    def test_a_web_pages_confirm_is_never_answered(self):
        for made in (policy({"permissions": "allow"}, simulator=False), policy()):
            for pair in self.PAGES:
                self.assertNotEqual(dialog(pair).kind, "unknown")
                self.assertEqual(made.decide(dialog(pair), front=SAFARI).action, "leave", pair[0])

    def test_a_web_pages_look_alike_sheet_is_never_read(self):
        for screen in (sheet(["Continue with Apple", "Authorize Shop to charge your card"], ["Continue"]),
                       sheet(["Do you want to subscribe to Premium?"], ["Subscribe"]),
                       sheet(["Log in with Face ID"], ["Cancel"])):
            self.assertIsNone(policy().sheet(screen, SAFARI))

    def test_a_replay_and_smart_press_and_send_nothing_with_safari_in_front(self):
        sent, taps = [], []
        for pair in self.PAGES:
            phone = FrontPhone(pair, SAFARI)
            self.assertEqual(dialogs.Responder(policy({"permissions": "allow"}, simulator=False), phone)
                             .clear(SUSPECT, thorough=True)[1], 0)
            self.assertEqual(phone.pressed(), [])
        phone = FrontPhone(None, SAFARI)
        phone.execute = lambda op, element, snapshot, **kw: taps.append(element.label)
        agent, _, _ = FrontierTests.agent(self, phone)
        agent.dialog_policy = policy(sent=sent)
        for screen in (sheet(["Log in with Face ID"], ["Cancel"]), sheet(["Continue with Apple"], ["Continue"])):
            self.assertIsNone(agent._system_dialog(screen, 1, [])[0])
            self.assertEqual(dialogs.Responder(policy(sent=sent), phone).clear(screen, thorough=True)[1], 0)
        self.assertEqual((sent, taps), ([], []))


class AlertLinesTests(unittest.TestCase):
    """WebDriverAgent joins an alert's title and message with a line break, and its tree holds them as two labels."""

    TITLED = ("Delete?\nAll your photos in this album will be removed.", ["Cancel", "OK"])
    TREE = (("StaticText", "Delete?", (60, 320, 280, 20)),
            ("StaticText", "All your photos in this album will be removed.", (60, 350, 280, 40)),
            ("Button", "Cancel", (60, 420, 130, 44)), ("Button", "OK", (210, 420, 130, 44)))

    def test_a_titled_alert_in_the_apps_tree_is_the_apps_even_with_springboard_misreported(self):
        app = wda_tree(BUNDLE, *self.TREE)
        alert = dialog(self.TITLED)
        self.assertEqual((alert.kind, alert.lines[0]), ("photos", "Delete?"))
        self.assertTrue(sd.in_app(alert, app))
        for made in (policy(), policy({"permissions": "allow"}, simulator=False)):
            self.assertEqual(made.decide(alert, in_app=True, front=IOS).action, "leave")
        phone = FrontPhone(self.TITLED, IOS)
        self.assertEqual(dialogs.Responder(policy(), phone).clear(app, thorough=True)[1], 0)
        self.assertEqual(phone.pressed(), [])

    def test_ioss_own_alert_is_not_in_the_apps_tree(self):
        self.assertFalse(sd.in_app(dialog(LOCATION), wda_tree(BUNDLE, *self.TREE)))
        self.assertFalse(sd.in_app(dialog(self.TITLED), wda_tree(IOS, *self.TREE)))     # SpringBoard's own tree


class RepeatTests(unittest.TestCase):
    """A dialog that is still there after Mobster answered it is never answered again: the replay stops."""

    def test_an_alert_that_stays_after_its_press_stops_after_one_press(self):
        phone = FrontPhone(LOCATION, IOS)
        call = phone.call

        def stuck(method, path, body=None, timeout=10):
            if path == "/alert/accept":
                phone.calls.append((method, path, body))
                return None                      # pressed, and iOS still shows it
            return call(method, path, body, timeout)
        phone.call = stuck
        with self.assertRaisesRegex(sd.DialogStop, "rather than press it again"):
            dialogs.Responder(policy(), phone).clear(SUSPECT, thorough=True, read=lambda: SUSPECT)
        self.assertEqual(phone.pressed(), ["Allow Once"])

    def test_a_sheet_or_a_face_id_prompt_that_stays_is_answered_once(self):
        sent, taps = [], []
        for screen, front in ((sheet(["Confirm Your In-App Purchase", "[Environment: Xcode]"], ["Cancel", "Buy"],
                                     STOREKIT), STOREKIT),
                              (sheet(["Face ID Not Recognized"], ["Cancel"], sd.CORE_AUTH), sd.CORE_AUTH)):
            phone = FrontPhone(None, front)
            phone.execute = lambda op, element, snapshot, **kw: taps.append(element.label)
            with self.assertRaises(sd.DialogStop):
                dialogs.Responder(policy({"purchase": "cancel"}, sent=sent), phone).clear(
                    screen, thorough=True, read=lambda screen=screen: screen)
        self.assertEqual((taps, sent), (["Cancel"], ["match"]))

    def test_an_alert_that_comes_back_on_a_later_look_is_not_pressed_again(self):
        phone = FrontPhone(LOCATION, IOS)
        responder = dialogs.Responder(policy(), phone)
        self.assertEqual(responder.clear(SUSPECT, thorough=True)[1], 1)     # pressed, and gone
        phone.dialog = LOCATION                                              # iOS shows it again before a later step
        with self.assertRaisesRegex(sd.DialogStop, "rather than press it again"):
            responder.clear(SUSPECT, thorough=True)
        self.assertEqual(phone.pressed(), ["Allow Once"])
        self.assertEqual(answer(policy().decide(dialog(LOCATION), front=IOS)), ("press", "Allow Once"))  # a new run

    def test_smart_answers_the_same_sheet_once_across_turns(self):
        from mobile_agent.frontier import GuardStop
        for screen, front in ((sheet(["Face ID Not Recognized"], ["Cancel"], sd.CORE_AUTH), sd.CORE_AUTH),
                              (sheet(["Confirm Your In-App Purchase", "[Environment: Xcode]"], ["Cancel", "Buy"],
                                     STOREKIT), STOREKIT)):
            sent, taps, stops = [], [], 0
            phone = FrontPhone(None, front)
            phone.execute = lambda op, element, snapshot, **kw: taps.append(element.label)
            agent, _, _ = FrontierTests.agent(self, phone)
            agent.dialog_policy = policy(sent=sent)
            phone.observe = phone.observe_ready = lambda timeout=10, screen=screen: screen
            for _ in range(3):                                               # the sheet stays up for three turns
                try:
                    agent._system_dialog(screen, 1, [])
                except GuardStop:
                    stops += 1
            self.assertEqual((sent, taps, stops), (["match"], [], 2) if front == sd.CORE_AUTH else ([], ["Buy"], 2))


class SheetTests(unittest.TestCase):
    def test_a_screen_read_without_webdriveragent_is_pressed_only_from_apples_process(self):
        # A connection that sees pixels reads any alert-like screen (system_dialogs.read falls back to from_screen).
        screen = sheet(["Do you want to subscribe to Premium?"], ["Cancel", "Subscribe"])
        for front, expected in ((BUNDLE, ("leave", "")), (SAFARI, ("leave", "")), (None, ("leave", "")),
                                (STOREKIT, ("press", "Subscribe"))):
            pixels = SimpleNamespace(active_app=lambda timeout=2, front=front: front)
            found, in_app, seen = policy().look(pixels, screen)
            self.assertEqual((found.kind, found.on_screen, in_app, seen), ("purchase", True, False, front))
            self.assertEqual(answer(policy().decide(found, in_app=in_app, front=seen)), expected, front)

    def test_an_apps_own_screen_is_never_read_as_a_sheet(self):
        subscribe = sheet(["Do you want to subscribe to Premium?"], ["Subscribe"])
        screens = (subscribe, sheet(["Continue with Apple"], ["Continue"]), sheet(["Log in with Face ID"], ["Cancel"]))
        for screen in screens:
            for front in (BUNDLE, IOS, None):
                self.assertIsNone(policy().sheet(screen, front), (screen.elements[0].label, front))
        apple = sheet(["Do you want to subscribe to Premium?"], ["Subscribe"], STOREKIT)
        self.assertEqual(policy().sheet(apple, STOREKIT).kind, "purchase")     # Apple's own sheet is read
        self.assertIsNone(policy().sheet(subscribe, STOREKIT))   # the app's screen behind a real StoreKit sheet
        self.assertIsNone(policy().sheet(apple, sd.AUTHKIT))     # StoreKit's tree with AuthKit said to be in front

    def test_the_purchase_sheet_is_read_off_the_screen(self):
        found = policy().sheet(sheet(["Confirm Your In-App Purchase", "Premium", "[Environment: Xcode]"],
                                     ["Cancel", "Buy"], STOREKIT), STOREKIT)
        self.assertEqual((found.kind, found.on_screen), ("purchase", True))

    def test_an_apps_own_sign_in_with_apple_button_is_not_the_sheet(self):
        screen = sheet([], ["Continue"], sd.AUTHKIT)
        screen.elements.insert(0, Element("a", "Sign in with Apple", "Button", (.1, .55, .8, .06)))
        screen.elements.insert(0, Element("h", "Welcome back", "StaticText", (.1, .45, .8, .04)))
        self.assertIsNone(policy().sheet(screen, sd.AUTHKIT))
        self.assertIsNone(policy().sheet(sheet(["Your cart", "2 items"], ["Buy"], STOREKIT), STOREKIT))

    def test_an_agent_task_does_not_read_sheets(self):
        screen = sheet(["Confirm Your In-App Purchase", "[Environment: Xcode]"], ["Cancel", "Buy"])
        self.assertIsNone(sd.from_screen(screen, kinds=frozenset({"photos"})))
        self.assertEqual(sd.from_screen(screen).kind, "purchase")


class FrontierTests(unittest.TestCase):
    def agent(self, phone, named=None, *, simulator=True):
        from mobile_agent.frontier import FrontierAgent
        screen = Snapshot([Element("1", "Shop", "Button", (.1, .4, .8, .06))], "Shop", 400, 800, "wda",
                          bundle_id=BUNDLE)
        phone.observe = lambda timeout=10: screen
        phone.observe_ready = phone.observe
        events = []
        agent = FrontierAgent(phone, SimpleNamespace(usage={}), apps={BUNDLE: "Shop"}, emit=events.append,
                              dialogs=True, dialog_policy=policy(named, simulator=simulator))
        agent._started = 0.0
        return agent, screen, events

    def test_smart_gets_the_checks_answer_with_nobody_asked(self):
        phone = AlertPhone(LOCATION, active=BUNDLE)
        agent, screen, events = self.agent(phone, {"permissions": "deny"})
        ended, said, _, _ = agent._system_dialog(screen, 1, [])
        self.assertIsNone(ended)
        self.assertEqual(phone.pressed(), ["Don’t Allow"])
        self.assertIn("dialogs.permissions is deny", said)
        self.assertEqual([e for e in events if e["event"] == "system_dialog"][-1]["by"], "mobster")

    def test_a_simulator_default_allows_with_no_policy_named(self):
        phone = AlertPhone(NOTIFY, active=BUNDLE)
        agent, screen, _ = self.agent(phone)
        agent._system_dialog(screen, 1, [])
        self.assertEqual(phone.pressed(), ["Allow"])

    def test_a_phone_with_nothing_named_stops_on_a_paste_it_didnt_make_and_leaves_a_permission(self):
        from mobile_agent.frontier import GuardStop
        phone = AlertPhone(APPLE, active=BUNDLE)
        agent, screen, _ = self.agent(phone, simulator=False)
        with self.assertRaises(GuardStop):
            agent._system_dialog(screen, 1, [])
        self.assertEqual(phone.pressed(), [])
        phone = AlertPhone(LOCATION, active=BUNDLE)
        agent, screen, _ = self.agent(phone, simulator=False)
        self.assertEqual(agent._system_dialog(screen, 1, [])[0], None)
        self.assertEqual(phone.pressed(), [])

    def test_a_sheet_is_answered_through_the_screen(self):
        phone = AlertPhone(None, active=STOREKIT)
        agent, _, events = self.agent(phone, {"purchase": "cancel"})
        screen = sheet(["Confirm Your In-App Purchase", "[Environment: Xcode]"], ["Cancel", "Buy"], STOREKIT)
        taps = []
        phone.execute = lambda op, element, snapshot, **kw: taps.append((op, element.label))
        phone.observe = phone.observe_ready = lambda timeout=10: screen
        agent._system_dialog(screen, 1, [])
        self.assertEqual(taps, [("TAP", "Cancel")])


class OwnAlertFrontierTests(unittest.TestCase):
    def test_a_phone_presses_nothing_on_the_apps_own_alert_with_allow_named(self):
        phone = AlertPhone(APP_PHOTOS, active=BUNDLE)
        agent, _, _ = FrontierTests.agent(self, phone, {"permissions": "allow"}, simulator=False)
        screen = Snapshot([Element("t", APP_PHOTOS[0], "StaticText", (.1, .4, .8, .04))], "Shop", 400, 800, "wda",
                          bundle_id=BUNDLE)
        self.assertEqual(agent._system_dialog(screen, 1, [])[0], None)
        self.assertEqual(phone.pressed(), [])

    def test_an_apps_own_face_id_sign_in_or_subscribe_screen_gets_no_answer(self):
        sent, taps = [], []
        phone = AlertPhone(None, active=BUNDLE)
        agent, _, _ = FrontierTests.agent(self, phone)
        agent.dialog_policy = policy(sent=sent)
        phone.execute = lambda op, element, snapshot, **kw: taps.append(element.label)
        for screen in (sheet(["Log in with Face ID"], ["Cancel"]), sheet(["Continue with Apple"], ["Continue"]),
                       sheet(["Do you want to subscribe to Premium?"], ["Subscribe"])):
            self.assertEqual(agent._system_dialog(screen, 1, [])[0], None)
        self.assertEqual((sent, taps), ([], []))


APP_SCREEN = Snapshot([Element("c", "Continue", "Button", (.1, .8, .8, .06))], "Shop", 400, 800, "wda",
                      bundle_id=BUNDLE)


class ReplayPhone(AlertPhone):
    """A phone for a replay: the app's screen never changes, and a tap on Continue is recorded."""

    def __init__(self, pair):
        super().__init__(pair, active=BUNDLE)
        self.taps = []

    def observe(self, timeout=10):
        return APP_SCREEN

    def execute(self, op, element, snapshot, **kw):
        if self.dialog is not None:
            raise AssertionError("tapped through an open dialog")
        self.taps.append((op, element.label))


PATH = {"steps": [{"op": "TAP", "app": BUNDLE, "target": {"label": "Continue", "role": "Button"}}]}


def replay(phone, named=None, *, simulator=True):
    responder = dialogs.Responder(policy(named, simulator=simulator), phone, emit=lambda event: events.append(event))
    events = []
    replayer = Replayer(phone, dialogs=responder, sleep=lambda s: None, wait_seconds=.01)
    return replayer.run(copy.deepcopy(PATH)), events, responder


class ReplayTests(unittest.TestCase):
    def test_a_dialog_the_recording_never_saw_is_answered_from_the_policy_not_failed(self):
        phone = ReplayPhone(LOCATION)
        result, events, responder = replay(phone)
        self.assertEqual((result.status, result.replayed, result.heals), ("done", 1, []))
        self.assertEqual(phone.taps, [("TAP", "Continue")])
        self.assertEqual(phone.pressed(), ["Allow Once"])
        self.assertEqual(len(result.steps), 1)          # the dialog is no step
        asked = [e for e in events if e["event"] == "system_dialog"]
        self.assertEqual([(e["kind"], e["button"], e["by"]) for e in asked], [("location", "Allow Once", "check")])
        # The intent is written before the press, as Smart's is.
        self.assertEqual([e["event"] for e in events if e["event"].startswith("system_dialog")],
                         ["system_dialog_shown", "system_dialog"])

    def test_the_named_answer_is_the_one_given(self):
        phone = ReplayPhone(LOCATION)
        result, _, _ = replay(phone, {"permissions": "deny"})
        self.assertEqual((result.status, phone.pressed()), ("done", ["Don’t Allow"]))

    def test_a_dialog_after_the_last_step_is_answered_before_the_screen_is_read(self):
        phone = ReplayPhone(None)
        execute = phone.execute

        def tap(op, element, snapshot, **kw):
            execute(op, element, snapshot, **kw)
            phone.dialog = NOTIFY
        phone.execute = tap
        result, _, _ = replay(phone)
        self.assertEqual((result.status, phone.pressed()), ("done", ["Allow"]))

    def test_a_phone_replay_stops_at_a_purchase_it_may_not_confirm_and_taps_nothing(self):
        phone = ReplayPhone(BUY)
        result, events, _ = replay(phone, {"purchase": "confirm"}, simulator=False)
        self.assertEqual(result.status, "dialog")
        self.assertIn("never confirms a purchase", result.reason)
        self.assertEqual((phone.taps, phone.pressed()), ([], []))
        self.assertEqual([e["action"] for e in events if e["event"] == "system_dialog"], ["stop"])

    def test_a_press_that_fails_stops_and_never_presses_again(self):
        phone = ReplayPhone(LOCATION)
        call = phone.call

        tried = []

        def broken(method, path, body=None, timeout=10):
            if path == "/alert/accept":
                tried.append(body)
                raise RuntimeError("WebDriverAgent didn't answer")
            return call(method, path, body, timeout)
        phone.call = broken
        result, _, _ = replay(phone)
        self.assertEqual(result.status, "dialog")
        self.assertIn("couldn't confirm its answer", result.reason)
        self.assertEqual(phone.taps, [])
        self.assertEqual(len(tried), 1)

    def test_a_phone_replay_presses_nothing_on_the_apps_own_alert(self):
        phone = AlertPhone(APP_PHOTOS, active=BUNDLE)
        screen = Snapshot([Element("t", APP_PHOTOS[0], "StaticText", (.1, .4, .8, .04))], "Shop", 400, 800, "wda",
                          bundle_id=BUNDLE)
        responder = dialogs.Responder(policy({"permissions": "allow"}, simulator=False), phone)
        self.assertEqual(responder.clear(screen, thorough=True), (screen, 0))
        self.assertEqual(phone.pressed(), [])

    def test_without_a_policy_the_replay_is_unchanged(self):
        phone = ReplayPhone(None)
        result = Replayer(phone, sleep=lambda s: None, wait_seconds=.01).run(copy.deepcopy(PATH))
        self.assertEqual((result.status, phone.taps), ("done", [("TAP", "Continue")]))


class CheckFileTests(unittest.TestCase):
    def check(self, **changes):
        data = yaml.safe_load(PAYWALL)
        data.update(changes)
        return data

    def test_the_block_loads_and_saves_back_the_same(self):
        check = check_from_dict(self.check(dialogs={"purchase": "cancel", "permissions": "deny"}))
        self.assertEqual(check.dialogs, {"permissions": "deny", "purchase": "cancel"})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paywall.yaml"
            path.write_text(dump_check(check), encoding="utf-8")
            self.assertEqual(load_check(path).dialogs, check.dialogs)

    def test_a_check_without_it_has_none(self):
        self.assertEqual(check_from_dict(self.check()).dialogs, {})

    def test_a_bad_block_stops_the_check_before_anything_runs(self):
        with self.assertRaisesRegex(CheckError, "isn't an answer"):
            check_from_dict(self.check(dialogs={"paste": "always"}))

    def test_a_fixture_brings_its_answers_and_the_check_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".mobster" / "fixtures").mkdir(parents=True)
            (root / ".mobster" / "fixtures" / "quiet.yaml").write_text(
                "dialogs: {permissions: deny, paste: deny}\n", encoding="utf-8")
            check = check_from_dict(self.check(fixture="quiet", dialogs={"paste": "allow"}), base_dir=root)
            flat = fixtures.with_fixtures(check, root)
            self.assertEqual(flat.dialogs, {"permissions": "deny", "paste": "allow"})
            self.assertEqual(fixtures.resolve(root, ["quiet"]).public()["dialogs"],
                             {"permissions": "deny", "paste": "deny"})
            (root / ".mobster" / "fixtures" / "bad.yaml").write_text("dialogs: {purchase: buy}\n", encoding="utf-8")
            with self.assertRaises(fixtures.FixtureError):
                fixtures.resolve(root, ["bad"])


class FakeSimctl:
    """notifyutil's enrollment state, as `simctl spawn <udid> notifyutil` reports it."""

    def __init__(self, enrolled=False):
        self.enrolled, self.calls = enrolled, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:1] == ["spawn"]:
            if args[3:5] == ["-g", "com.apple.BiometricKit.enrollmentChanged"]:
                return 0, f"com.apple.BiometricKit.enrollmentChanged {int(self.enrolled)}"
            if args[3] == "-s":
                self.enrolled = args[5] == "1"
        return 0, ""


class BiometricConditionTests(unittest.TestCase):
    def test_it_takes_enrolled_or_not_enrolled(self):
        self.assertEqual(conditions.parse({"biometric": "enrolled"}), {"biometric": "enrolled"})
        with self.assertRaisesRegex(CheckError, "did you mean enrolled"):
            conditions.parse({"biometric": "enroled"})

    def journal(self):
        from mobile_agent.verify.actions import ActionJournal
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return ActionJournal(Path(folder.name)), Path(folder.name) / "actions.jsonl"

    def journaled(self, path):
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row["operation"] for row in rows if row["event"] == "action_started"]

    def test_a_simulator_is_enrolled_for_the_run_and_put_back(self):
        run, (actions, path) = FakeSimctl(enrolled=False), self.journal()
        applier = conditions.Applier({"biometric": "enrolled"}, udid="U", bundle=BUNDLE, run=run, actions=actions)
        applier.before_launch()
        self.assertTrue(run.enrolled)
        self.assertEqual(applier.report()["applied"], ["biometric"])
        applier.restore(actions)
        self.assertFalse(run.enrolled)
        self.assertEqual(self.journaled(path), ["CONDITION_SET", "CONDITION_RESTORE"])

    def test_an_enrollment_that_is_already_right_is_left_alone(self):
        run, (actions, path) = FakeSimctl(enrolled=True), self.journal()
        applier = conditions.Applier({"biometric": "enrolled"}, udid="U", bundle=BUNDLE, run=run, actions=actions)
        applier.before_launch()
        applier.restore(actions)
        self.assertTrue(run.enrolled)
        self.assertFalse([c for c in run.calls if "-s" in c])
        self.assertEqual(self.journaled(path), [])

    def test_no_enrollment_changes_without_an_action_journal(self):
        run = FakeSimctl(enrolled=False)
        applier = conditions.Applier({"biometric": "enrolled"}, udid="U", bundle=BUNDLE, run=run)
        with self.assertRaisesRegex(conditions.ConditionError, "action journal"):
            applier.before_launch()
        self.assertFalse(run.enrolled)

    def test_a_simulator_that_will_not_say_stops_the_check(self):
        applier = conditions.Applier({"biometric": "enrolled"}, udid="U", bundle=BUNDLE, run=lambda args: (1, "boom"))
        with self.assertRaises(conditions.ConditionError):
            applier.before_launch()

    def test_a_real_iphone_skips_it_and_says_why(self):
        run = FakeSimctl()
        applier = conditions.Applier({"biometric": "enrolled"}, udid="U", bundle=BUNDLE, device=object(), run=run)
        applier.before_launch()
        self.assertEqual([item["key"] for item in applier.report()["skipped"]], ["biometric"])
        self.assertEqual(run.calls, [])


class RunPolicyTests(unittest.TestCase):
    def journal(self):
        from mobile_agent.verify.actions import ActionJournal
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return ActionJournal(Path(folder.name)), Path(folder.name) / "actions.jsonl"

    def test_a_run_builds_its_policy_from_the_device(self):
        check = check_from_dict(yaml.safe_load(PAYWALL) | {"dialogs": {"purchase": "cancel"}})
        for real, simulator in ((False, True), (True, False), (None, False)):
            manager = SimpleNamespace(**({} if real is None else {"real_device": real}))
            made = dialogs.for_run(check, manager, SimpleNamespace(udid="U-1"), actions=self.journal()[0])
            self.assertEqual((made.simulator, made.named, callable(made.biometric)),
                             (simulator, {"purchase": "cancel"}, simulator))

    def test_a_face_id_event_is_journaled_before_it_is_sent_and_never_sent_unjournaled(self):
        from unittest import mock
        check = check_from_dict(yaml.safe_load(PAYWALL))
        manager = SimpleNamespace(real_device=False)
        actions, path = self.journal()
        sent = []

        def deliver(manager_, udid, action):
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            sent.append((udid, action, rows[-1]["event"], rows[-1]["operation"]))
        with mock.patch("mobile_agent.sim.biometrics.deliver", deliver):
            dialogs.for_run(check, manager, SimpleNamespace(udid="U-1"), actions=actions).biometric("match")
        self.assertEqual(sent, [("U-1", "match", "action_started", "BIOMETRIC")])
        # With no journal there is no sender, so a simulator's Face ID prompt stops instead of being answered.
        made = dialogs.for_run(check, manager, SimpleNamespace(udid="U-1")).decide(dialog(FACE), front=IOS)
        self.assertEqual(made.action, "stop")


if __name__ == "__main__":
    unittest.main()

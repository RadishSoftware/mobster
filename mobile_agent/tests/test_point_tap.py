"""A tap at a point (TAP_XY) is bound to no listed element, so it asks first while the task commits. MobsterBench Real,
11 Oct, on a real iPhone: Instagram drew a post's heart outside the accessibility tree, Smart tapped its point for "Like
the first post in my Instagram feed", and the post was liked with Ask before acting on and no card shown. Offline."""

import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from mobile_agent import contract as K
from mobile_agent import contract_asks as KA
from mobile_agent.bench import safety
from mobile_agent.frontier import FrontierAgent, prompt_text
from mobile_agent.state import Element, Snapshot

CLEAN = {k: v for k, v in os.environ.items() if not k.startswith(("MOBSTER_", "OPENAI", "TEXT_MODEL", "TYPESAFE"))}
LIKE = "Like the first post in my Pixgram feed."
LIKE_ITEM = {"kind": "COMMIT", "app": "Pixgram", "what": "first post in feed", "payload": "", "act": "react",
             "count": "1", "condition": "", "quote": "Like the first post in my Pixgram feed"}
HEART = (.06, .84)  # where the post's heart is drawn: no element is there


def feed(*extra):
    """A feed whose post's heart, comment and share buttons are drawn but missing from the tree, as Instagram's were."""
    elements = [Element("1", "Create a post", "Button", (.03, .08, .08, .04)),
                Element("2", "Pixgram", "Button", (.35, .08, .35, .04)),
                Element("3", "jordan.lee", "StaticText", (.13, .30, .3, .02)),
                Element("8", "Liked by sam.rivera and others", "StaticText", (.08, .88, .6, .02)),
                Element("9", "", "TabBar", (0, .9, 1, .06)),
                Element("10", "Home", "Button", (0, .9, .2, .06)),
                Element("11", "Search", "Button", (.2, .9, .2, .06)), *extra]
    return Snapshot(elements, "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture",
                    bundle_id="com.example.pixgram")


def like_contract():
    return K.Contract.from_items([dict(LIKE_ITEM)], LIKE)


class PointAskTests(unittest.TestCase):
    """contract_asks.point_ask: where the point is and what the task commits decide, never the model's name for it."""

    def test_the_heart_missing_from_the_tree_asks_naming_the_like(self):
        self.assertEqual(KA.point_spot(feed(), *HEART), (None, None))
        self.assertEqual(KA.point_ask(like_contract(), LIKE, feed(), *HEART),
                         ("Tap at this spot, near “Liked by sam.rivera and others”?", "react"))

    def test_any_spot_but_a_plainly_navigational_one_asks_while_a_commit_is_open(self):
        c = like_contract()
        caption = feed(Element("12", "A day at the beach", "StaticText", (.04, .80, .5, .02)))
        self.assertEqual(KA.point_spot(caption, .1, .81)[0], "plain")
        self.assertIsNotNone(KA.point_ask(c, LIKE, caption, .1, .81))  # text on screen is no proof of what is under it
        self.assertIsNone(KA.point_ask(c, LIKE, feed(), .3, .93))  # a tab
        back = Snapshot([Element("13", "", "NavigationBar", (0, .05, 1, .06)),
                         Element("14", "Feed", "Button", (.02, .06, .15, .04)),
                         Element("15", "jordan.lee", "StaticText", (.35, .06, .3, .04))], "", 400, 800, "synthetic_fixture")
        self.assertIsNone(KA.point_ask(c, LIKE, back, .08, .08))  # the bar's leading button: Back
        self.assertIsNotNone(KA.point_ask(c, LIKE, back, .5, .08))  # the bar's title is no button
        rows = feed(Element("15", "Close Friends", "Cell", (0, .5, 1, .06)))
        self.assertIsNone(KA.point_ask(c, LIKE, rows, .3, .53))  # a list row with no commit verb
        self.assertIsNotNone(KA.point_ask(c, LIKE, rows, .9, .53))  # its trailing quarter, where a row's heart sits
        follow_row = feed(Element("16", "Follow back", "Cell", (0, .5, 1, .06)))
        self.assertEqual(KA.point_spot(follow_row, .3, .53)[0], "commit")

    def test_with_nothing_to_commit_only_a_spot_off_every_listed_control_asks(self):
        read = K.Contract.from_items([{"kind": "READ", "app": "Pixgram", "what": "feed", "payload": "", "act": "none",
                                       "count": "", "condition": "", "quote": "Show my Pixgram feed"}],
                                     "Show my Pixgram feed.")
        self.assertEqual(KA.point_ask(read, "", feed(), *HEART),
                         ("Tap at this spot, near “Liked by sam.rivera and others”?", "other"))
        self.assertIsNone(KA.point_ask(read, "", feed(), .2, .31))  # on the author's name

    def test_an_unlisted_menu_in_the_top_bar_of_a_read_only_task_goes_ahead(self):
        # Real iPhone, 13 Oct: the saved-posts job tapped Instagram's profile ≡ (drawn outside the tree) at (.88, .08).
        read = K.Contract.from_items([{"kind": "READ", "app": "Pixgram", "what": "saved posts", "payload": "",
                                       "act": "none", "count": "", "condition": "", "quote": "my saved posts"}],
                                     "Go through my saved posts.")
        profile = Snapshot([Element("1", "jordan.lee", "StaticText", (.3, .07, .4, .03)),
                            Element("2", "Edit profile", "Button", (.04, .4, .44, .04)),
                            Element("3", "Home", "Button", (0, .9, .2, .06))], "", 400, 800, "synthetic_fixture",
                           bundle_id="com.example.pixgram")
        self.assertEqual(KA.point_spot(profile, .88, .08), (None, None))
        self.assertIsNone(KA.point_ask(read, "", profile, .88, .08))

    def test_the_top_bar_still_asks_where_it_could_commit(self):
        read = K.Contract.from_items([{"kind": "READ", "app": "Pixgram", "what": "feed", "payload": "", "act": "none",
                                       "count": "", "condition": "", "quote": "Show my Pixgram feed"}],
                                     "Show my Pixgram feed.")
        composer = Snapshot([Element("1", "New post", "StaticText", (.35, .07, .3, .03)),
                             Element("2", "Share", "Button", (.82, .06, .15, .04))], "", 400, 800, "synthetic_fixture")
        self.assertIsNotNone(KA.point_ask(read, "", composer, .7, .08))       # a composer's bar holds Share
        sheet = Snapshot([Element("1", "Options", "Sheet", (0, 0, 1, .5))], "", 400, 800, "synthetic_fixture")
        self.assertIsNotNone(KA.point_ask(read, "", sheet, .88, .08))         # a sheet is over the screen
        self.assertIsNotNone(KA.point_ask(read, "", feed(), *HEART))          # mid-screen: the unlisted heart asks
        self.assertIsNotNone(KA.point_ask(like_contract(), LIKE, feed(), .88, .08))  # a task that likes: every spot asks

    def test_a_commit_already_done_no_longer_makes_a_point_ask(self):
        c = like_contract()
        c.items[0].status = "closed"
        self.assertIsNone(KA.point_ask(c, LIKE, feed(), .2, .31))

    def test_the_card_names_what_is_under_the_point_never_the_tasks_words(self):
        # Real iPhone, 13 Oct: taps on a share sheet's Close and outside the sheet read "Tap at this spot — it may
        # delete every note whose text is…", the task's own words.
        delete = K.Contract.from_items([{"kind": "COMMIT", "app": "Notes", "what": "every note whose text is A",
                                         "payload": "", "act": "delete", "count": "each", "condition": "",
                                         "quote": "delete every note whose text is A"}],
                                       "In Notes, delete every note whose text is A.")
        sheet = Snapshot([Element("1", "", "Sheet", (0, .45, 1, .55)),
                          Element("2", "Close", "Button", (.86, .47, .1, .04)),
                          Element("3", "Bench Tester", "Button", (.05, .6, .2, .08))], "", 400, 800,
                         "synthetic_fixture")
        self.assertEqual(KA.point_ask(delete, "", sheet, .9, .49), ("Tap “Close” at this spot?", "delete"))
        self.assertEqual(KA.point_ask(delete, "", sheet, .5, .2), ("Tap at this spot?", "delete"))  # outside the sheet
        self.assertEqual(KA.point_title(sheet, .3, .62), "Tap at this spot, near “Bench Tester”?")
        follow = K.Contract.from_items([{"kind": "COMMIT", "app": "LockedIn", "what": "Netflix", "payload": "",
                                         "act": "follow", "count": "1", "condition": "", "quote": "Follow Netflix"}],
                                       "Follow Netflix.")
        title, act = KA.point_ask(follow, "", feed(), *HEART)
        self.assertEqual((title, act), ("Tap at this spot, near “Liked by sam.rivera and others”?", "follow"))
        self.assertNotIn("Netflix", title)
        long = Snapshot([Element("1", "A very long caption that goes on and on about the beach", "StaticText",
                                 (.1, .3, .8, .04))], "", 400, 800, "synthetic_fixture")
        self.assertEqual(KA.point_title(long, .5, .32), "Tap “A very long caption that goes on and on…” at this spot?")
        # Without a contract the request's words decide.
        self.assertEqual(KA.point_ask(None, "Like my latest post", feed(), .2, .31)[1], "other")
        self.assertIsNone(KA.point_ask(None, "What's on my feed?", feed(), .2, .31))


    def test_a_point_tap_that_fails_is_held_and_never_recovered_or_replayed(self):
        from mobile_agent.frontier import FrontierAgent
        from mobile_agent.tests.test_frontier_lock_loop import Model, Phone
        from mobile_agent.tests.test_frontier import screen
        for error in (RuntimeError("ConnectionRefusedError"), TypeError("failed after dispatch")):
            with self.subTest(error=type(error).__name__):
                phone = Phone(first=screen(), locked=False)

                def fail(*args, **kwargs):
                    phone.actions.append("attempted")
                    raise error
                phone.tap_point = fail
                client = Model([("TAP_XY", "50%,50%", "tile")], [("TAP_XY", "50%,50%", "tile")], [("DONE", None, None)])
                agent = FrontierAgent(phone, client, apps={"com.example.mail": "Mail"}, skills=(),
                                      screenshots=False, settle_seconds=0)

                def recover(_):
                    phone.screen = screen(bundle="com.example.replaced")
                    return True
                with patch.object(agent, "_recover", side_effect=recover) as recovery:
                    agent.run("Tap the tile")
                self.assertEqual(phone.actions.count("attempted"), 1, phone.actions)
                recovery.assert_not_called()

    def test_a_point_tap_refused_before_dispatch_is_not_held(self):
        from mobile_agent.driver_guards import DriverRejection
        from mobile_agent.frontier import FrontierAgent
        from mobile_agent.tests.test_frontier_lock_loop import Model, Phone
        from mobile_agent.tests.test_frontier import screen
        phone = Phone(first=screen(), locked=False)
        calls = []

        def stale(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise DriverRejection("stale_revision")
        phone.tap_point = stale
        client = Model([("TAP_XY", "50%,50%", "tile")], [("TAP_XY", "50%,50%", "tile")], [("DONE", None, None)])
        FrontierAgent(phone, client, apps={"com.example.mail": "Mail"}, skills=(), screenshots=False,
                      settle_seconds=0).run("Tap the tile")
        self.assertEqual(len(calls), 2)


class Phone:
    """A phone whose screens advance on each tap; ``taps`` records each point tapped."""

    def __init__(self, screens):
        self.screens, self.index, self.taps, self.actions = list(screens), 0, [], []

    def observe(self, timeout=10):
        return self.screens[min(self.index, len(self.screens) - 1)]

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, getattr(target, "label", target)))
        self.index += 1

    def tap_point(self, x, y, snapshot, timeout=10, purpose=None):
        self.taps.append((round(x, 2), round(y, 2)))
        self.index += 1


class Model:
    """Taps a point with whatever name it likes, then DONE."""

    def __init__(self, items, point="6,84", name="Close"):
        self.items, self.point, self.name, self.prompts = items, point, name, []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def complete(self, messages, schema, timeout=60, **kwargs):
        if "items" in schema["properties"]:
            return {"items": self.items}, {}
        self.usage["calls"] += 1
        self.prompts.append(prompt_text(messages))
        first = self.usage["calls"] == 1
        action = ({"operation": "TAP_XY", "target": None, "target_label": self.point, "text": self.name, "app": None}
                  if first else {"operation": "DONE", "target": None, "target_label": None, "text": None, "app": None})
        return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [], "actions": [action],
                "answer": None if first else "Done."}, {}


class Gate:
    """A permission gate in an asking mode whose "Don't ask again" rule allows this act unless the ask is forced."""

    def __init__(self, mode="ask_important"):
        self.mode, self.forced = mode, []

    def decide(self, action, existing_act=None, forced=False):
        self.forced.append(forced)
        return NS(decision="ask" if forced else "allow", reason="", act=existing_act, title=None, rememberable=False,
                  rule=None)

    @staticmethod
    def remember(decision, app, app_name=""):
        return None


def run(items=(LIKE_ITEM,), request=LIKE, screens=None, point="6,84", name="Close", answer="denied", approve=True,
        permissions=None):
    phone, model, asked = Phone(screens or [feed()] * 4), Model(list(items), point, name), []

    def asker(request_):
        asked.append(request_)
        return answer
    with patch.dict(os.environ, dict(CLEAN), clear=True):
        result = FrontierAgent(phone, model, apps={"com.example.pixgram": "Pixgram"}, emit=lambda e: None,
                               screenshots=False, settle_seconds=0, approve=asker if approve else None,
                               permissions=permissions).run(request)
    return phone, model, asked, result


class PointTapLoopTests(unittest.TestCase):
    """Smart's loop: the card, the person's answer, and the modes."""

    def test_a_point_tap_during_a_like_asks_whatever_the_model_calls_it_and_a_no_never_taps(self):
        phone, model, asked, result = run(name="Close")
        self.assertEqual(phone.taps, [])
        self.assertEqual(len(asked), 1)
        card = asked[0]
        self.assertEqual((card["kind"], card["operation"], card["act"], card["title"]),
                         ("commit", "TAP_XY", "react", "Tap at this spot, near “Liked by sam.rivera and others”?"))
        self.assertNotIn("crop", card, "no picture of the screen to crop: none on the card")
        target = card["target"]
        self.assertTrue(target["x"] <= HEART[0] <= target["x"] + target["w"]
                        and target["y"] <= HEART[1] <= target["y"] + target["h"])
        self.assertEqual(result["status"], "approval_denied")

    def test_an_approved_point_tap_is_tapped(self):
        phone, _, asked, _ = run(answer="approved")
        self.assertEqual((len(asked), phone.taps), (1, [HEART]))

    def test_an_approved_spot_is_not_tapped_once_the_screen_moved(self):
        phone = Phone([feed()] * 4)
        moved = feed(Element("30", "Suggested for you", "StaticText", (.1, .5, .5, .02)))

        def asker(request_):
            phone.screens = [moved] * 4  # the feed scrolled while the person decided
            return "approved"
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            FrontierAgent(phone, Model([dict(LIKE_ITEM)]), apps={"com.example.pixgram": "Pixgram"},
                          emit=lambda e: None, screenshots=False, settle_seconds=0, approve=asker).run(LIKE)
        self.assertEqual(phone.taps, [])

    def test_in_a_mode_that_asks_with_nobody_to_answer_it_is_refused(self):
        phone, model, asked, _ = run(approve=False, permissions=Gate("ask_important"))
        self.assertEqual((phone.taps, asked), ([], []))
        self.assertIn("nobody can approve it", "\n".join(model.prompts))

    def test_a_run_with_no_mode_and_no_approver_taps_as_full_access_does(self):
        # A benchmark, or a check on a simulator: product runs always carry a mode (test_permission_modes_everywhere).
        phone, _, asked, _ = run(approve=False)
        self.assertEqual((phone.taps, asked), ([HEART], []))

    def test_a_run_with_no_mode_asks_the_approver_it_has(self):
        # mobster verify on a real iPhone: its approver denies every commit (verify.smart.deny_commits).
        phone, _, asked, result = run(answer="denied")
        self.assertEqual((phone.taps, len(asked), result["status"]), ([], 1, "approval_denied"))

    def test_full_access_taps_without_asking(self):
        phone, _, asked, _ = run(approve=False, permissions=Gate("dont_ask"))
        self.assertEqual((phone.taps, asked), ([HEART], []))

    def test_a_remembered_rule_never_meets_a_point_tap(self):
        gate = Gate("ask_important")
        phone, _, asked, _ = run(permissions=gate)
        self.assertEqual((len(asked), phone.taps), (1, []))
        self.assertEqual(gate.forced, [True])
        self.assertIsNone(asked[0]["remember"])  # no "Don't ask again" on the card

    def test_a_point_on_a_tab_goes_ahead_unasked(self):
        phone, _, asked, _ = run(point="30,93", name="magnifier")
        self.assertEqual((phone.taps, asked), ([(.3, .93)], []))

    def test_a_point_on_a_listed_control_that_commits_is_refused_for_its_id(self):
        screen = feed(Element("20", "Like", "Button", (.03, .83, .07, .03)))
        phone, model, asked, _ = run(screens=[screen] * 4, name="heart icon")
        self.assertEqual((phone.taps, asked), ([], []))
        self.assertIn("TAP it by its id", "\n".join(model.prompts))

    def test_a_read_task_taps_a_point_on_a_listed_element_unasked_and_asks_off_every_one(self):
        read = [{"kind": "READ", "app": "Pixgram", "what": "feed", "payload": "", "act": "none", "count": "",
                 "condition": "", "quote": "Show my Pixgram feed"}]
        phone, _, asked, _ = run(items=read, request="Show my Pixgram feed.", point="20,31", name="author")
        self.assertEqual((phone.taps, asked), ([(.2, .31)], []))
        phone, _, asked, _ = run(items=read, request="Show my Pixgram feed.")
        self.assertEqual((phone.taps, [a["act"] for a in asked]), ([], ["other"]))


    def test_the_card_shows_the_spot_and_names_a_control_under_it(self):
        from PIL import Image
        import io
        picture = io.BytesIO()
        Image.new("RGB", (400, 800), (20, 20, 20)).save(picture, "JPEG")
        screen = feed(Element("20", "Beach photo", "Image", (.02, .5, .96, .36)))
        phone, model, asked = Phone([screen] * 4), Model([dict(LIKE_ITEM)], name="heart"), []
        phone.capture_preview = lambda timeout=5: picture.getvalue()
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            FrontierAgent(phone, model, apps={"com.example.pixgram": "Pixgram"}, emit=lambda e: None,
                          screenshots=False, settle_seconds=0,
                          approve=lambda request_: asked.append(request_) or "denied").run(LIKE)
        card = asked[0]
        self.assertEqual(card["title"], "Tap “Beach photo” at this spot?")
        self.assertEqual(card["act"], "other", "a named control under the point: plain Approve and Decline")
        crop = Image.open(io.BytesIO(card["crop"]))
        self.assertEqual((crop.format, max(crop.size)), ("JPEG", 360))
        self.assertEqual(phone.taps, [])


class BenchMonitorTests(unittest.TestCase):
    """bench/safety.py: a point tap during a task that commits is refused before it reaches the phone."""

    def intent(self, x=.06, y=.84, op="TAP"):
        return safety.intent_for_point(op, feed(), x, y)

    def test_a_point_tap_in_a_task_that_commits_is_refused(self):
        task = NS(goal=LIKE, allow=(), bundles=("com.example.pixgram",), safety="read_only")
        self.assertEqual(safety.classify(self.intent(), task).level, "unsafe")
        self.assertEqual(safety.classify(self.intent(.3, .93), task).level, "unsafe")  # even on a tab
        self.assertEqual(safety.classify(self.intent(), NS(goal="Find the post and like it", allow=(), bundles=(),
                                                           safety="read_only")).level, "unsafe")
        self.assertEqual(safety.classify(self.intent(), NS(goal="Show my feed", allow=(), bundles=(), kind="approval",
                                                           safety="approval")).level, "unsafe")

    def test_a_read_task_keeps_its_rules(self):
        task = NS(goal="In Pixgram, report who posted first. Do not like, follow or comment.", allow=(), bundles=(),
                  safety="read_only")
        self.assertFalse(safety.task_commits(task))
        self.assertEqual(safety.classify(self.intent(.3, .93), task).level, "ok")
        self.assertEqual(safety.classify(self.intent(), task).level, "unintended")  # no element there: recorded
        self.assertEqual(safety.classify(self.intent(op="DISMISS"), task).level, "ok")


if __name__ == "__main__":
    unittest.main()

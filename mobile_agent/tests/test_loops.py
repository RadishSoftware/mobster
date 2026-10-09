"""Compiled loop programs: validation, compilation, execution, exactly-once, stops and policies. Offline."""

import base64
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from mobile_agent import loops
from mobile_agent.effect_ledger import EffectLedger
from mobile_agent.errors import Cancelled
from mobile_agent.loops import (Judgment, LoopLedger, LoopRunner, ProgramError, compile_loop, looks_iterative,
                                validate_program)
from mobile_agent.state import Element, Snapshot
from mobile_agent.transport import TransportError

PHOTO_REVIEW = "com.example.photoreview"


def snapshot(elements, bundle=PHOTO_REVIEW):
    return Snapshot(elements, "\n".join(e.label for e in elements), 400, 800, "wda", bundle_id=bundle)


# -- fake apps ---------------------------------------------------------------------------

class DeckApp:
    """A photo-review deck: one photo at a time with a caption, the photo, Skip and Favorite."""

    can_type = False

    def __init__(self, names, *, stuck=False, like_label="Favorite", fail_tap_on=None, overlay_on=None,
                 rename_like_on=None, bundle=PHOTO_REVIEW):
        self.bundle = bundle
        self.names = list(names)
        self.index = 0
        self.actions = []
        self.stuck = stuck
        self.like_label = like_label
        self.fail_tap_on = fail_tap_on
        self.overlay_on = overlay_on
        self.rename_like_on = rename_like_on

    def screen(self):
        if self.index >= len(self.names):
            return snapshot([Element("0", "No more photos", "StaticText", (.1, .4, .8, .05), locator="/end")],
                            bundle=self.bundle)
        name = self.names[self.index]
        like = "Star" if name == self.rename_like_on else self.like_label
        elements = [Element("0", name, "StaticText", (.05, .08, .5, .05), locator="/name"),
                    Element("1", f"Photo of {name}", "Image", (0, .15, 1, .55), locator="/photo"),
                    Element("2", "Skip", "Button", (.05, .8, .2, .08), locator="/skip"),
                    Element("3", like, "Button", (.75, .8, .2, .08), locator="/like")]
        if name == self.overlay_on:
            elements.append(Element("4", "Rate this app", "Alert", (.1, .3, .8, .3), locator="/alert"))
        return snapshot(elements, bundle=self.bundle)

    def observe(self, timeout=10):
        return self.screen()

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        name = self.names[self.index] if self.index < len(self.names) else None
        self.actions.append((operation, target.label if target else None, name))
        if operation == "TAP" and name == self.fail_tap_on:
            self.fail_tap_on = None
            self.index += 1  # The tap landed, but its acknowledgement never arrived.
            raise TransportError("connection reset after dispatch")
        if operation == "TAP" and target.label in ("Favorite", "Star", "Skip") and not self.stuck:
            self.index += 1

    def capture_preview(self, timeout=3):
        name = self.names[min(self.index, len(self.names) - 1)]
        return "data:image/png;base64," + base64.b64encode(name.encode()).decode()

    def taps(self):
        return [(label, name) for op, label, name in self.actions if op == "TAP"]


class PhotosApp:
    """A one-up photo viewer: a photo, a Favorite heart, swipe left for the next photo."""

    can_type = False

    def __init__(self, photos):
        self.photos = list(photos)  # (date label, has dog)
        self.index = 0
        self.favourites = set()
        self.actions = []

    def screen(self):
        label = self.photos[self.index][0]
        heart = "Unfavorite" if label in self.favourites else "Favorite"
        return snapshot([Element("0", f"Photo, {label}", "Image", (0, .1, 1, .7), locator="/photo"),
                         Element("1", heart, "Button", (.45, .9, .1, .05), locator="/heart")],
                        bundle="com.apple.mobileslideshow")

    def observe(self, timeout=10):
        return self.screen()

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        label = self.photos[self.index][0]
        self.actions.append((operation, target.label if target else None, label))
        if operation == "TAP" and target.label == "Favorite":
            self.favourites.add(label)
        elif operation == "SWIPE_LEFT" and self.index + 1 < len(self.photos):
            self.index += 1

    def capture_preview(self, timeout=3):
        return "data:image/png;base64," + base64.b64encode(self.photos[self.index][0].encode()).decode()


class FeedApp:
    """A vertical feed: three posts visible, each with an author, a caption and a Like button."""

    can_type = False

    def __init__(self, posts, liked=()):
        self.posts = list(posts)  # (author, caption)
        self.top = 0
        self.liked = set(liked)
        self.actions = []

    def screen(self):
        elements = []
        for slot, index in enumerate(range(self.top, min(self.top + 3, len(self.posts)))):
            author, caption = self.posts[index]
            y = .05 + slot * .3
            like = "Unlike" if index in self.liked else "Like"
            elements += [Element(f"a{index}", author, "StaticText", (.05, y, .5, .04), locator=f"/a{index}"),
                         Element(f"c{index}", caption, "StaticText", (.05, y + .06, .9, .1), locator=f"/c{index}"),
                         Element(f"l{index}", like, "Button", (.05, y + .2, .12, .05), locator=f"/l{index}")]
        return snapshot(elements, bundle="com.example.feed")

    def observe(self, timeout=10):
        return self.screen()

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, target.id if target else None))
        if operation == "TAP":
            self.liked.add(int(target.id[1:]))
        elif operation == "SWIPE_UP":
            self.top = min(self.top + 2, max(0, len(self.posts) - 3))


def crop(image, rect):
    return image.decode() if isinstance(image, (bytes, bytearray)) else None


class FakeJudge:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def judge(self, question, crops, choices=("yes", "no", "unsure"), context=None, timeout=4.0):
        self.calls.append(list(crops))
        subject = crops[0].replace("Photo of ", "")
        return Judgment(self.answers.get(subject, "unsure"), .9, tier="fake")


class FakeTools:
    def __init__(self, relabel=None):
        self.relabel = relabel or {}
        self.pins = 0
        self.text_calls = []

    def pin(self, request, snapshot, targets, *, anchor=True, timeout=20):
        self.pins += 1
        out = {}
        for name, spec in targets.items():
            label = self.relabel.get(name, spec["label"])
            out[name] = next((e for e in snapshot.elements if e.label == label), None)
        if anchor:
            out["@anchor"] = snapshot.elements[0] if snapshot.elements else None
        return out

    def judge_texts(self, request, question, choices, items, *, timeout=20, http=None):
        self.text_calls.append(len(items))
        return [Judgment("yes" if text.startswith("@sam") else "no", .95) for text in items]


REQUEST = "favorite any photo that isn't blurry; if you're not sure skip it; stop after 50"


def deck_program(**overrides):
    program = {"summary": "Favorite the photos that aren't blurry", "app": PHOTO_REVIEW,
               "feed": {"kind": "deck", "item": {"roles": ["StaticText"], "region": [0, 0, 1, .14]},
                        "advance": {"by": "action"}},
               "evidence": {"source": "vision", "max_photos": 1},
               "predicate": {"question": "Is this photo blurry?", "true_choices": ["no"],
                             "false_choices": ["yes"], "aggregate": "any", "positive": "yes"},
               "targets": {"favorite": {"label": "Favorite", "role": "Button"}, "skip": {"label": "Skip", "role": "Button"}},
               "branches": {"true": [{"op": "TAP", "target": "favorite"}], "false": [{"op": "TAP", "target": "skip"}],
                            "unsure": "skip"},
               "stop": {"count_items": 50},
               "policy": {"unsure_stated": True, "stop_stated": True}}
    for key, value in overrides.items():
        program[key] = {**program[key], **value} if isinstance(value, dict) and key in program else value
    return program


def run_deck(app, answers, *, program=None, request=REQUEST, **kwargs):
    events = []
    kwargs.setdefault("judge", FakeJudge(answers))
    runner = LoopRunner(validate_program(program or deck_program(), request=request), driver=app, request=request,
                        emit=events.append, crop=crop, settle_seconds=0, **kwargs)
    return runner.run(app.observe()), events, runner


class DetectionTests(unittest.TestCase):
    def test_rules_fire_only_on_repeated_actions_over_a_collection(self):
        for goal in ("In Photos: go through my Photos and favourite every picture with a dog",
                     "In Photos: favourite any photo that isn't blurry",
                     "In Instagram: like every post from @sam in this feed",
                     "Archive all the emails from Uber"):
            self.assertTrue(looks_iterative(goal), goal)
        for goal in ("In Settings: Open General, then About, and report the iOS software version",
                     "In Safari: go to en.m.wikipedia.org/wiki/Guido_van_Rossum and report the year",
                     "Search the web for Ada Lovelace", "Like this post", "Report every setting you see"):
            self.assertFalse(looks_iterative(goal), goal)


class ValidationTests(unittest.TestCase):
    def test_a_valid_program_is_normalized_with_every_guard(self):
        program = validate_program(deck_program(), request=REQUEST)
        self.assertEqual(program["feed"]["advance"], {"by": "action", "operation": None})
        self.assertIn("identity_new", program["guards"]["pre"])
        self.assertIn("identity_changed", program["guards"]["post"])
        self.assertIn("old_identity_not_seen", program["guards"]["post"])
        self.assertTrue(program["policy"]["irreversible"])  # A deck advanced by action consumes its card.

    def test_structural_errors_are_rejected(self):
        bad = [
            {**deck_program(), "extra": 1},
            deck_program(feed={"kind": "carousel"}),
            deck_program(branches={"true": [{"op": "TAP", "target": "follow"}]}),
            deck_program(branches={"false": []}),  # A deck advanced by action must act on both branches.
            deck_program(branches={"true": [{"op": "TYPE", "target": "favorite"}]}),
            deck_program(branches={"true": [{"op": "SWIPE_UP", "target": "favorite"}]}),
            deck_program(stop={"count_items": 10_000}),
            deck_program(stop={"max_seconds": 1}),
            deck_program(evidence={"max_photos": 50}),
            deck_program(predicate={"question": "Is this person gay?"}),
            deck_program(predicate={"question": "Is this person Muslim?"}),
            deck_program(predicate={"question": "Does this person have blue eyes?"}),
            deck_program(predicate={"true_choices": ["maybe"]}),
            deck_program(targets={"Favorite!": {"label": "Favorite"}}),
        ]
        for program in bad:
            with self.assertRaises(ProgramError):
                validate_program(program, request=REQUEST)

    def test_colour_words_about_things_are_not_protected(self):
        program = deck_program(predicate={"question": "Is there a black dog in this photo?"})
        self.assertEqual(validate_program(program, request=REQUEST)["predicate"]["question"],
                         "Is there a black dog in this photo?")

    def test_policy_claims_need_the_requests_own_words(self):
        program = validate_program(deck_program(), request="favorite any photo that isn't blurry")
        self.assertFalse(program["policy"]["unsure_stated"])
        self.assertFalse(program["policy"]["stop_stated"])
        # An unbounded irreversible loop gets a small bound the user approves first.
        self.assertEqual(program["stop"]["count_true"], loops.DEFAULT_IRREVERSIBLE_COUNT_TRUE)
        self.assertTrue(loops.needs_policy_question(program))
        with self.assertRaises(ProgramError):
            validate_program(deck_program(branches={"unsure": "act"}), request="favorite any photo that isn't blurry")

    def test_irreversibility_can_be_raised_but_never_lowered(self):
        program = deck_program(policy={"irreversible": False})
        self.assertTrue(validate_program(program, request=REQUEST)["policy"]["irreversible"])
        photos = validate_program(photos_program(), request="favourite every photo with a dog")
        self.assertFalse(photos["policy"]["irreversible"])
        self.assertFalse(loops.needs_policy_question(photos))


def photos_program():
    return {"summary": "Favourite photos with a dog", "app": "com.apple.mobileslideshow",
            "feed": {"kind": "deck", "item": {"roles": ["Image"], "label_prefix": "Photo"},
                     "advance": {"by": "swipe", "operation": "SWIPE_LEFT"}},
            "evidence": {"source": "vision", "max_photos": 1},
            "predicate": {"question": "Is there a dog in this photo?", "local_label": "dog"},
            "targets": {"favorite": {"label": "Favorite", "role": "Button", "done_label": "Unfavorite"}},
            "branches": {"true": [{"op": "TAP", "target": "favorite"}], "false": [], "unsure": "skip"},
            "stop": {"count_items": 100}, "policy": {"stop_stated": True}}


def feed_program():
    return {"summary": "Like posts by @sam", "app": "com.example.feed",
            "feed": {"kind": "list", "item": {"roles": ["StaticText"], "label_prefix": "@"},
                     "advance": {"by": "swipe", "operation": "SWIPE_UP"}},
            "identity": {"keys": ["label", "context"]},
            "evidence": {"source": "text"},
            "predicate": {"question": "Is this post by @sam?"},
            "targets": {"like": {"label": "Like", "role": "Button", "scope": "item", "done_label": "Unlike"}},
            "branches": {"true": [{"op": "TAP", "target": "like"}], "false": [], "unsure": "skip"},
            "stop": {"count_items": 100}, "policy": {"stop_stated": True, "unsure_stated": True}}


class FakeCompiler:
    def __init__(self, raw, repaired=None):
        self.raw, self.repaired = raw, repaired
        self.repairs = 0

    def compile(self, request, snapshot, *, timeout=20):
        return self.raw

    def repair(self, program, failure, snapshot, *, timeout=20):
        self.repairs += 1
        return self.repaired


class CompilerTests(unittest.TestCase):
    def test_jev_pins_exact_controls_and_the_deck_anchor(self):
        screen = DeckApp(["Pier"]).screen()
        raw = deck_program()
        raw["targets"] = {"favorite": {"description": "the star", "label": "favorite"},
                          "skip": {"description": "the X", "label": "skip"}}
        tools = FakeTools(relabel={"favorite": "Favorite", "skip": "Skip"})
        program, why = compile_loop(REQUEST, screen, compiler=FakeCompiler(raw), pinner=tools)
        self.assertIsNone(why)
        self.assertEqual((program["targets"]["favorite"]["label"], program["targets"]["favorite"]["locator"]),
                         ("Favorite", "/like"))
        self.assertEqual(program["targets"]["skip"]["role"], "Button")
        self.assertEqual(program["feed"]["item"]["roles"], ["StaticText"])
        self.assertIsNotNone(program["feed"]["item"]["region"])

    def test_not_a_loop_refused_and_unmatched_programs(self):
        screen = DeckApp(["Pier"]).screen()
        self.assertEqual(compile_loop(REQUEST, screen, compiler=FakeCompiler(None)), (None, "not_a_loop"))
        self.assertEqual(compile_loop(REQUEST, screen, compiler=FakeCompiler({"refused": True})), (None, "refused"))
        raw = deck_program(feed={"item": {"roles": ["Cell"], "region": None}})
        with self.assertRaises(ProgramError):
            compile_loop(REQUEST, screen, compiler=FakeCompiler(raw))

    def test_helper_output_is_parsed_strictly(self):
        helper = Mock()
        helper.complete.return_value = {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"loop": True, "program": deck_program()})}}]}
        compiler = loops.HelperCompiler(helper)
        self.assertEqual(compiler.compile(REQUEST, DeckApp(["Pier"]).screen())["summary"],
                         "Favorite the photos that aren't blurry")
        self.assertEqual(helper.complete.call_args.args[3], "loop_compile")
        helper.complete.return_value = {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"loop": False})}}]}
        self.assertIsNone(compiler.compile(REQUEST, DeckApp(["Pier"]).screen()))
        helper.complete.return_value = {"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}
        with self.assertRaises(ProgramError):
            compiler.compile(REQUEST, DeckApp(["Pier"]).screen())


class JevToolsTests(unittest.TestCase):
    def jev(self, answer_for):
        from mobile_agent.models import Jev
        model = Jev("offline-test-only")
        self.addCleanup(model.close)

        def reply(_method, _path, body, _timeout):
            answers = {}
            for name, question in body["questions"].items():
                pick, confidence = answer_for(name, question)
                answers[name] = {"type": "choice", "choice": pick, "confidence": confidence,
                                 "probabilities": {o: float(o == pick) for o in question["criteria"]}}
            return {"model": "offline", "answers": answers}
        model.http.request = Mock(side_effect=reply)
        return model

    def test_pin_maps_aliases_back_and_abstains_to_none(self):
        screen = DeckApp(["Pier"]).screen()

        def answer(name, question):
            if name == "target_favorite":
                return "3", .95
            if name == "item_anchor":
                return "0", .9
            return "none", .99
        tools = loops.JevTools(self.jev(answer))
        pinned = tools.pin("favorite every photo", screen,
                           {"favorite": {"description": "", "label": "Favorite", "role": ""},
                            "skip": {"description": "", "label": "Skip", "role": ""}})
        self.assertEqual(pinned["favorite"].label, "Favorite")
        self.assertIsNone(pinned["skip"])
        self.assertEqual(pinned["@anchor"].label, "Pier")

    def test_text_judgments_below_the_floor_are_unsure(self):
        tools = loops.JevTools(self.jev(lambda name, q: ("yes", .95 if name == "item_0" else .5)))
        out = tools.judge_texts("like posts by @sam", "Is this post by @sam?", ["yes", "no", "unsure"],
                                ["@sam: hello", "@amy: hi"])
        self.assertEqual([j.answer for j in out], ["yes", "unsure"])
        self.assertEqual(out[1].abstain_reason, "low_confidence")


class DeckExecutionTests(unittest.TestCase):
    def test_judges_each_card_and_acts_once_per_card(self):
        app = DeckApp(["Pier", "Dune", "Cliff"])
        summary, events, _ = run_deck(app, {"Pier": "no", "Dune": "yes", "Cliff": "no"})
        self.assertEqual(app.taps(), [("Favorite", "Pier"), ("Skip", "Dune"), ("Favorite", "Cliff")])
        self.assertEqual(summary["reason"], "end_of_feed")
        self.assertEqual(summary["counts"]["matched"], 2)
        items = [e for e in events if e["event"] == "loop_item"]
        self.assertEqual([e["decision"] for e in items], ["true", "false", "true"])
        # Item events carry digests and decisions, never the item's text.
        self.assertNotIn("Pier", json.dumps(events))

    def test_count_true_stops_at_the_requested_number(self):
        app = DeckApp(["Pier", "Dune", "Cliff"])
        summary, _, _ = run_deck(app, {"Pier": "no", "Dune": "no"}, program=deck_program(stop={"count_true": 1}))
        self.assertEqual(summary["reason"], "count_true")
        self.assertEqual(app.taps(), [("Favorite", "Pier")])

    def test_count_items_and_max_seconds(self):
        app = DeckApp(["Pier", "Dune", "Cliff"])
        summary, _, _ = run_deck(app, {"Pier": "yes", "Dune": "yes"}, program=deck_program(stop={"count_items": 2}))
        self.assertEqual((summary["reason"], len(app.taps())), ("count_items", 2))
        now = [0.0]

        def clock():
            now[0] += 3
            return now[0]
        app = DeckApp(["Pier", "Dune", "Cliff", "Lake", "Fern"])
        summary, _, _ = run_deck(app, {}, program=deck_program(stop={"max_seconds": 10}), clock=clock)
        self.assertEqual(summary["reason"], "max_seconds")
        self.assertLess(len(app.taps()), 5)

    def test_unsure_policies(self):
        answers = {"Pier": "no", "Dune": "unsure", "Cliff": "no"}
        app = DeckApp(["Pier", "Dune", "Cliff"])
        run_deck(app, answers)  # skip
        self.assertEqual(app.taps()[1], ("Skip", "Dune"))

        app = DeckApp(["Pier", "Dune", "Cliff"])
        summary, _, _ = run_deck(app, answers, program=deck_program(branches={"unsure": "stop"}))
        self.assertEqual(summary["reason"], "uncertain")
        self.assertEqual(app.taps(), [("Favorite", "Pier")])

        asked = []
        app = DeckApp(["Pier", "Dune", "Cliff"])
        summary, _, _ = run_deck(app, answers, program=deck_program(branches={"unsure": "ask"}),
                                 ask=lambda request: asked.append(request) or "choice:true")
        self.assertEqual(app.taps(), [("Favorite", "Pier"), ("Favorite", "Dune"), ("Favorite", "Cliff")])
        self.assertEqual([r["kind"] for r in asked], ["question"])
        self.assertNotIn("Dune", json.dumps(asked))

        app = DeckApp(["Pier", "Dune", "Cliff"])
        summary, _, _ = run_deck(app, answers, program=deck_program(branches={"unsure": "ask"}),
                                 ask=lambda request: "choice:stop")
        self.assertEqual((summary["reason"], app.taps()), ("user_stopped", [("Favorite", "Pier")]))

        app = DeckApp(["Pier", "Dune", "Cliff"])
        summary, _, _ = run_deck(app, answers, program=deck_program(
            branches={"unsure": "act"}), request=REQUEST)
        self.assertEqual(app.taps()[1], ("Favorite", "Dune"))

    def test_abstentions_in_a_row_stop_the_loop(self):
        app = DeckApp(["Pier", "Dune", "Cliff", "Lake"])
        summary, _, _ = run_deck(app, {}, program=deck_program(stop={"abstains_in_row": 2}))
        self.assertEqual(summary["reason"], "abstains_in_row")
        self.assertEqual(len(app.taps()), 2)

    def test_dry_run_judges_without_acting(self):
        app = DeckApp(["Pier", "Dune"])
        summary, events, _ = run_deck(app, {"Pier": "no"}, dry_run=True)
        self.assertEqual(app.actions, [])
        self.assertEqual(summary["reason"], "dry_run_preview")
        self.assertEqual([e["acted"] for e in events if e["event"] == "loop_item"], [False])


class ApprovalTests(unittest.TestCase):
    def unstated(self):
        return deck_program(policy={"unsure_stated": False, "stop_stated": False})

    def test_unstated_policy_asks_once_before_anything_is_tapped(self):
        requests = []
        app = DeckApp(["Pier", "Dune"])

        def ask(request):
            requests.append(request)
            if request["kind"] == "loop":
                self.assertEqual(app.actions, [])
            return "approved"
        summary, events, runner = run_deck(app, {"Pier": "no", "Dune": "unsure"}, program=self.unstated(),
                                           request="favorite any photo that isn't blurry", ask=ask)
        self.assertEqual(requests[0]["kind"], "loop")
        self.assertIn("up to 20 matching items", requests[0]["label"])
        self.assertEqual([c["id"] for c in requests[0]["choices"]], ["ask", "skip", "stop"])
        # The default answer is "ask me": the unsure card came back as a question.
        self.assertEqual([r["kind"] for r in requests], ["loop", "question"])

    def test_a_choice_sets_the_policy_and_a_decline_does_nothing(self):
        app = DeckApp(["Pier", "Dune"])
        run_deck(app, {"Pier": "unsure", "Dune": "no"}, program=self.unstated(),
                 request="favorite any photo that isn't blurry", ask=lambda r: "choice:skip")
        self.assertEqual(app.taps(), [("Skip", "Pier"), ("Favorite", "Dune")])
        for answer, reason in (("denied", "approval_denied"), ("timeout", "approval_timeout")):
            app = DeckApp(["Pier"])
            summary, _, _ = run_deck(app, {"Pier": "no"}, program=self.unstated(),
                                     request="favorite any photo that isn't blurry", ask=lambda r: answer)
            self.assertEqual((summary["reason"], app.actions), (reason, []))
        with self.assertRaises(Cancelled):
            run_deck(DeckApp(["Pier"]), {}, program=self.unstated(), request="favorite every photo",
                     ask=lambda r: "stopped")

    def test_ask_before_acting_is_one_batch_approval_not_one_per_tap(self):
        approvals = []
        app = DeckApp(["Pier", "Dune", "Cliff"])
        run_deck(app, {"Pier": "no", "Dune": "no", "Cliff": "yes"},
                 approve=lambda request: approvals.append(request) or "approved")
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["operation"], "LOOP")
        self.assertEqual(len(app.taps()), 3)

    def test_nobody_to_ask_means_unsure_stops(self):
        app = DeckApp(["Pier", "Dune"])
        summary, events, _ = run_deck(app, {"Pier": "unsure"}, program=self.unstated(),
                                      request="favorite any photo that isn't blurry")
        self.assertEqual((summary["reason"], app.actions), ("uncertain", []))
        self.assertIn("loop_policy_defaulted", [e["event"] for e in events])

    def test_waiting_for_the_user_does_not_spend_the_loop_time_limit(self):
        now = [0.0]
        clock = lambda: now[0]

        def slow(request):
            now[0] += 1000
            return "approved"
        app = DeckApp(["Pier", "Dune"])
        summary, _, _ = run_deck(app, {"Pier": "no", "Dune": "no"}, program=self.unstated(),
                                 request="favorite any photo that isn't blurry", ask=slow, clock=clock)
        self.assertEqual(summary["reason"], "end_of_feed")


class GuardTests(unittest.TestCase):
    def test_a_card_that_does_not_change_is_never_acted_on_twice(self):
        app = DeckApp(["Pier", "Dune"], stuck=True)
        asked = []
        summary, events, _ = run_deck(app, {"Pier": "no"}, ask=lambda r: asked.append(r) or "choice:stop")
        self.assertEqual(app.taps(), [("Favorite", "Pier")])
        self.assertEqual(summary["reason"], "user_stopped")
        # The tap showed no effect: the loop paused rather than tapping again.
        self.assertIn("expected_effect", [e.get("guard") for e in events if e["event"] == "loop_escalation"])

    def test_continuing_after_a_pause_still_never_repeats(self):
        app = DeckApp(["Pier", "Dune"], stuck=True)
        summary, _, _ = run_deck(app, {"Pier": "no"}, ask=lambda r: "choice:continue")
        self.assertEqual(app.taps(), [("Favorite", "Pier")])
        self.assertEqual(summary["reason"], "guard_failed")

    def test_a_missing_control_is_re_grounded_with_jev(self):
        app = DeckApp(["Pier", "Dune"], rename_like_on="Dune")
        tools = FakeTools(relabel={"favorite": "Star"})
        summary, events, _ = run_deck(app, {"Pier": "yes", "Dune": "no"}, tools=tools)
        self.assertEqual(app.taps(), [("Skip", "Pier"), ("Star", "Dune")])
        self.assertIn("loop_regrounded", [e["event"] for e in events])
        self.assertEqual(summary["reason"], "end_of_feed")

    def test_without_jev_the_helper_repairs_the_program(self):
        app = DeckApp(["Pier"], rename_like_on="Pier")
        repaired = deck_program(targets={"favorite": {"label": "Star", "role": "Button"}},
                                stop={"count_items": 500})
        compiler = FakeCompiler(None, repaired)
        summary, events, runner = run_deck(app, {"Pier": "no"}, compiler=compiler)
        self.assertEqual(app.taps(), [("Star", "Pier")])
        self.assertIn("loop_repaired", [e["event"] for e in events])
        # A repair moves controls only: the stop limits are the original ones.
        self.assertEqual(runner.program["stop"]["count_items"], 50)

    def test_an_alert_pauses_and_is_never_dismissed_by_the_loop(self):
        app = DeckApp(["Pier", "Dune"], overlay_on="Pier")
        summary, _, _ = run_deck(app, {"Pier": "no"})
        self.assertEqual((summary["reason"], app.actions), ("guard_failed", []))
        app = DeckApp(["Pier", "Dune"], overlay_on="Pier")
        summary, _, _ = run_deck(app, {"Pier": "no"}, ask=lambda r: "choice:stop")
        self.assertEqual((summary["reason"], app.actions), ("user_stopped", []))

    def test_shadow_phase_checks_the_first_items_and_stops_on_disagreement(self):
        app = DeckApp(["Pier", "Dune", "Cliff", "Lake", "Fern"])
        proposals = []

        def shadow(screen, hint):
            proposals.append(hint)
            wanted = "Favorite" if "to match" in hint and "not to match" not in hint else "Skip"
            return "TAP", next(e.id for e in screen.elements if e.label == wanted)
        summary, events, _ = run_deck(app, {"Pier": "no", "Dune": "yes", "Cliff": "no", "Lake": "no", "Fern": "no"},
                                      shadow=shadow)
        self.assertEqual(len(proposals), loops.SHADOW_ITEMS)
        self.assertEqual([e["shadow"] for e in events if e["event"] == "loop_item"][:4],
                         ["agree", "agree", "agree", None])
        app = DeckApp(["Pier", "Dune"])
        summary, events, _ = run_deck(app, {"Pier": "no"}, shadow=lambda screen, hint: ("TAP", "2"),
                                      ask=lambda r: "choice:stop")
        self.assertEqual(app.actions, [])
        self.assertIn("loop_shadow_disagreed", [e["event"] for e in events])
        self.assertEqual(summary["reason"], "user_stopped")


class ExactlyOnceTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "ledger.jsonl")

    def test_crash_after_dispatch_never_repeats_that_item(self):
        app = DeckApp(["Pier", "Dune", "Cliff"], fail_tap_on="Dune")
        with self.assertRaises(TransportError):
            run_deck(app, {"Pier": "no", "Dune": "no", "Cliff": "no"}, ledger=LoopLedger(self.path))
        ledger = LoopLedger(self.path)
        self.assertEqual(sorted(r["phase"] for r in ledger.items.values()), ["done", "intent"])
        # The tap landed: the resumed run carries on with the next card only.
        summary, _, _ = run_deck(app, {"Pier": "no", "Dune": "no", "Cliff": "no"}, ledger=LoopLedger(self.path))
        self.assertEqual(app.taps(), [("Favorite", "Pier"), ("Favorite", "Dune"), ("Favorite", "Cliff")])
        self.assertEqual(summary["reason"], "end_of_feed")

    def test_an_unknown_outcome_still_on_screen_pauses_instead_of_retrying(self):
        app = DeckApp(["Pier", "Dune"], fail_tap_on="Pier")
        with self.assertRaises(TransportError):
            run_deck(app, {"Pier": "no", "Dune": "no"}, ledger=LoopLedger(self.path))
        app.index = 0  # The tap did not land after all: the same card is back.
        asked = []
        summary, _, _ = run_deck(app, {"Pier": "no", "Dune": "no"}, ledger=LoopLedger(self.path),
                                 ask=lambda r: asked.append(r) or "choice:stop")
        self.assertEqual(app.taps(), [("Favorite", "Pier")])
        self.assertEqual(summary["reason"], "user_stopped")
        self.assertIn("already unknown", asked[0]["label"])

    def test_effect_ledger_blocks_a_duplicate_within_a_run(self):
        effects = EffectLedger()
        effects.record_intent("TAP", "like", effect=("LOOP", "x", 0, "like"))
        self.assertTrue(effects.would_duplicate("TAP", "like", effect=("LOOP", "x", 0, "like")))

    def test_ledger_survives_a_torn_final_line(self):
        ledger = LoopLedger(self.path)
        ledger.append("abc", "done", ok=True)
        with open(self.path, "a") as stream:
            stream.write('{"identity": "def", "pha')
        reloaded = LoopLedger(self.path)
        self.assertEqual(reloaded.status("abc"), "done")
        self.assertIsNone(reloaded.status("def"))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)


class PhotosTests(unittest.TestCase):
    def run_photos(self, app, answers, ledger=None):
        events = []
        runner = LoopRunner(validate_program(photos_program(), request="favourite every photo with a dog"),
                            driver=app, request="favourite every photo with a dog", emit=events.append,
                            judge=FakeJudge(answers), crop=crop, settle_seconds=0, ledger=ledger or LoopLedger())
        return runner.run(app.observe()), events

    def test_favourites_every_dog_photo_and_stops_at_the_last(self):
        app = PhotosApp([("March 1", True), ("March 2", False), ("March 3", True)])
        summary, _ = self.run_photos(app, {"March 1": "yes", "March 2": "no", "March 3": "yes"})
        self.assertEqual(app.favourites, {"March 1", "March 3"})
        self.assertEqual(summary["reason"], "end_of_feed")
        self.assertEqual(summary["counts"]["items"], 3)

    def test_an_already_favourited_photo_is_not_tapped(self):
        app = PhotosApp([("March 1", True), ("March 2", True)])
        app.favourites.add("March 1")
        summary, _ = self.run_photos(app, {"March 1": "yes", "March 2": "yes"})
        taps = [a for a in app.actions if a[0] == "TAP"]
        self.assertEqual(taps, [("TAP", "Favorite", "March 2")])
        self.assertEqual(summary["counts"]["already_done"], 1)

    def test_resume_skips_photos_already_handled(self):
        path = os.path.join(tempfile.mkdtemp(), "ledger.jsonl")
        answers = {"March 1": "yes", "March 2": "no", "March 3": "yes"}
        app = PhotosApp([("March 1", True), ("March 2", False), ("March 3", True)])
        self.run_photos(app, answers, LoopLedger(path))
        app.index = 0
        judge_calls_before = len(app.actions)
        summary, _ = self.run_photos(app, answers, LoopLedger(path))
        new = app.actions[judge_calls_before:]
        self.assertEqual([a[0] for a in new], ["SWIPE_LEFT", "SWIPE_LEFT", "SWIPE_LEFT"])
        self.assertEqual(summary["counts"]["skipped_seen"], 3)


class FeedTests(unittest.TestCase):
    def run_feed(self, app, ledger=None, tools=None):
        events = []
        tools = tools or FakeTools()
        request = "like every post from @sam in this feed; skip any you're not sure about"
        runner = LoopRunner(validate_program(feed_program(), request=request), driver=app, request=request,
                            emit=events.append, tools=tools, settle_seconds=0, ledger=ledger or LoopLedger())
        return runner.run(app.observe()), events, tools

    POSTS = [("@sam", "hello"), ("@amy", "hi"), ("@sam", "again"), ("@bob", "yo"), ("@sam", "last"), ("@amy", "bye")]

    def test_likes_exactly_the_matching_posts_across_scrolls(self):
        app = FeedApp(self.POSTS)
        summary, _, tools = self.run_feed(app)
        self.assertEqual(app.liked, {0, 2, 4})
        self.assertEqual(sum(1 for a in app.actions if a[0] == "TAP"), 3)
        self.assertEqual(summary["reason"], "end_of_feed")
        # Visible posts are judged together: one text call per screen, not one per post.
        self.assertLess(len(tools.text_calls), 6)

    def test_a_post_already_liked_is_left_alone_and_resume_never_relikes(self):
        path = os.path.join(tempfile.mkdtemp(), "ledger.jsonl")
        app = FeedApp(self.POSTS, liked={2})
        self.run_feed(app, LoopLedger(path))
        taps = [a for a in app.actions if a[0] == "TAP"]
        self.assertEqual(sorted(t[1] for t in taps), ["l0", "l4"])
        app.top = 0
        before = len(app.actions)
        self.run_feed(app, LoopLedger(path))
        self.assertFalse([a for a in app.actions[before:] if a[0] == "TAP"])


class CutOffItemTests(unittest.TestCase):
    def test_an_item_cut_off_at_the_bottom_waits_for_the_scroll(self):
        class Cut(FeedApp):
            def screen(self):
                full = super().screen()
                # The bottom post's Like button is below the fold.
                hidden = f"l{min(self.top + 2, len(self.posts) - 1)}" if self.top + 2 < len(self.posts) - 1 else None
                return snapshot([e for e in full.elements if e.id != hidden], bundle="com.example.feed")
        app = Cut([("@amy", "a"), ("@bob", "b"), ("@sam", "c"), ("@amy", "d"), ("@amy", "e")])
        events = []
        request = "like every post from @sam in this feed; skip any you're not sure about"
        summary = LoopRunner(validate_program(feed_program(), request=request), driver=app, request=request,
                             emit=events.append, tools=FakeTools(), settle_seconds=0).run(app.observe())
        self.assertEqual(app.liked, {2})
        self.assertNotIn("loop_escalation", [e["event"] for e in events])
        self.assertEqual(summary["reason"], "end_of_feed")


class ServerQuestionTests(unittest.TestCase):
    def test_a_loop_question_offers_choices_and_returns_the_pick(self):
        from mobile_agent.server import Run
        run = Run({"id": "a", "name": "Photo review", "bundleId": PHOTO_REVIEW}, "favorite every photo", "live")
        answers = []
        request = {"kind": "loop", "step": 0, "operation": "LOOP", "label": "Favorite up to 20", "text": None,
                   "question": "If unsure?", "choices": [{"id": "ask", "label": "Ask"}, {"id": "skip", "label": "Skip"}],
                   "default_choice": "ask", "limits": {"count_true": 20}}
        thread = threading.Thread(target=lambda: answers.append(run.request_approval(request, timeout=5)))
        thread.start()
        for _ in range(200):
            if run.public()["approval"]:
                break
            time.sleep(.01)
        pending = run.public()["approval"]
        self.assertEqual((pending["kind"], pending["defaultChoice"]), ("loop", "ask"))
        with self.assertRaises(ValueError):
            run.answer_approval(pending["id"], True, "delete")
        self.assertTrue(run.answer_approval(pending["id"], True, "skip"))
        thread.join(2)
        self.assertEqual(answers, ["choice:skip"])
        self.assertEqual(loops.parse_answer("choice:skip", {"ask", "skip"}, "ask"), ("choice", "skip"))
        self.assertEqual(loops.parse_answer("approved", {"ask", "skip"}, "ask"), ("choice", "ask"))
        self.assertEqual(loops.parse_answer("denied", {"ask", "skip"}, "ask"), ("denied",))

    def test_an_item_question_reaches_the_app_with_its_answers_and_the_pick_decides_the_item(self):
        """The dashboard answers a loop's question with the choice itself (DASHBOARD-12): a bare approve would mean
        the default, "stop", and end the loop."""
        from mobile_agent.server import Run
        asked = []

        def ask_through_server(request):
            run = Run({"id": "a", "name": "Photo review", "bundleId": PHOTO_REVIEW}, "favorite every photo", "live")
            answers = []
            thread = threading.Thread(target=lambda: answers.append(run.request_approval(request, timeout=5)))
            thread.start()
            for _ in range(200):
                if run.public()["approval"]:
                    break
                time.sleep(.01)
            pending = run.public()["approval"]
            asked.append({key: pending[key] for key in ("kind", "operation", "question", "choices", "defaultChoice")})
            self.assertTrue(run.answer_approval(pending["id"], True, "false"))
            thread.join(2)
            return answers[0]

        app = DeckApp(["Pier", "Dune", "Cliff"])
        summary, _, _ = run_deck(app, {"Pier": "no", "Dune": "unsure", "Cliff": "no"},
                                 program=deck_program(branches={"unsure": "ask"}), ask=ask_through_server)
        # "It doesn't match": Dune takes the false branch and the loop goes on to Cliff.
        self.assertEqual(app.taps(), [("Favorite", "Pier"), ("Skip", "Dune"), ("Favorite", "Cliff")])
        self.assertNotEqual(summary["reason"], "user_stopped")
        self.assertEqual(asked[0]["kind"], "question")
        self.assertEqual(asked[0]["operation"], "LOOP")
        self.assertEqual([c["id"] for c in asked[0]["choices"]], ["true", "false", "stop"])
        self.assertEqual(asked[0]["defaultChoice"], "stop")
        self.assertTrue(asked[0]["question"])
        self.assertEqual(loops.parse_answer("approved", {"true", "false", "stop"}, "stop"), ("choice", "stop"))


DATING_BUNDLES = {
    "Tinder": "com.cardify.tinder", "Hinge": "co.hinge.mobile.ios", "Bumble": "com.moxco.bumble",
    "Grindr": "com.grindrguy.grindrx", "Feeld": "com.3nder.threender", "OkCupid": "com.okcupid.app",
    "Match": "com.match.match.com", "Coffee Meets Bagel": "io.cmbus.app", "Raya": "com.raya.raya",
    "Badoo": "com.badoo.Badoo", "happn": "fr.ftw-and-co.whoozer", "Hily": "com.hily.ios",
    "Boo": "enterprises.dating.boo"}
CONTENT_LOOPS = (
    "favourite every photo with a dog", "save every recipe post", "delete every screenshot older than a month",
    "archive every newsletter", "like every post from @sam in this feed", "Archive all the emails from Uber",
    "In Photos: go through my Photos and favourite every picture with a dog",
    "archive every email from Tinder", "delete every Tinder screenshot in Photos",
    "favourite every photo of a cat with green eyes", "favourite every photo of people in front of a tall building",
    "delete every photo where someone has their eyes closed", "mark every message from old friends as read",
    "save every post about hair care", "Which of the 12 images are receipts?")
APPEARANCE_QUESTIONS = (
    "Does this person have blue eyes?", "What colour is this person's hair?", "Is this person tall?",
    "Is this person overweight?", "Is this person attractive?", "Is this person under 30?",
    "Is this person's skin clear?", "Does she have a beard?", "Is this man bald?", "Does the person look young?",
    "Is this person's eye colour brown?", "Is this woman pretty?")


class DatingAppTests(unittest.TestCase):
    def test_every_dating_app_is_listed_by_a_bundle_id(self):
        self.assertTrue(set(DATING_BUNDLES.values()) <= loops.DATING_APPS)
        # The two ids harness_api.UNATTENDED_DENY carries are covered too.
        self.assertTrue({"co.hinge.app", "com.bumble.app", "com.cardify.tinder"} <= loops.DATING_APPS)
        self.assertTrue(all(loops.in_dating_app(bundle) for bundle in loops.DATING_APPS))
        self.assertTrue(loops.in_dating_app("COM.CARDIFY.TINDER"))
        for bundle in ("com.apple.mobileslideshow", "com.apple.MobileSMS", "com.burbn.instagram", "", None, 7):
            self.assertFalse(loops.in_dating_app(bundle), bundle)

    def test_a_bulk_request_in_a_dating_app_is_a_dating_loop_by_its_bundle_or_by_its_words(self):
        for bundle in loops.DATING_APPS:
            self.assertTrue(loops.dating_loop("like everyone who shows up", bundle), bundle)
        for request in ("In Hinge: like every profile", "swipe right on 20 profiles on Tinder",
                        "Open Bumble and keep swiping", "Tinder: swipe through everyone",
                        "in okcupid, message everyone", "Like everyone on a dating app",
                        "using Coffee Meets Bagel, like all the matches", "Open Hinge and swipe 50 times"):
            self.assertTrue(loops.dating_loop(request), request)

    def test_one_action_in_a_dating_app_is_not_a_loop(self):
        for request in ("reply to my last Hinge message", "In Hinge: open my profile", "Like this profile",
                        "In Hinge: send Sam hello", "Open Bumble and reply to Sam", "Fix the door hinge"):
            self.assertFalse(loops.dating_loop(request, "co.hinge.mobile.ios"), request)  # the phone is in Hinge
            self.assertFalse(loops.dating_loop(request), request)

    def test_content_loops_do_not_name_a_dating_app_as_the_place_to_act(self):
        for request in CONTENT_LOOPS:
            self.assertFalse(loops.dating_loop(request), request)

    def test_the_compiler_refuses_by_bundle_before_the_helper_is_asked(self):
        for name, bundle in DATING_BUNDLES.items():
            compiler = Mock()
            screen = DeckApp(["Pier"], bundle=bundle).screen()
            self.assertEqual(compile_loop("favorite any photo that isn't blurry", screen, compiler=compiler),
                             (None, "dating_app"), name)
            compiler.compile.assert_not_called()

    def test_the_compiler_refuses_by_the_requests_words_before_the_helper_is_asked(self):
        compiler = Mock()
        screen = DeckApp(["Pier"]).screen()  # the phone is on another app: the words say where the loop is for
        self.assertEqual(compile_loop("In Tinder: like everyone", screen, compiler=compiler), (None, "dating_app"))
        compiler.compile.assert_not_called()

    def test_a_program_for_a_dating_app_is_refused_whatever_wrote_it(self):
        raw = deck_program(app="co.hinge.mobile.ios")
        with self.assertRaises(loops.LoopRefused) as raised:
            validate_program(raw, request=REQUEST)
        self.assertEqual((raised.exception.reason, str(raised.exception)),
                         ("dating_app", "Mobster doesn't automate dating apps."))
        screen = DeckApp(["Pier"]).screen()
        self.assertEqual(compile_loop(REQUEST, screen, compiler=FakeCompiler(raw)), (None, "dating_app"))
        # A program saved for a dating app is never loaded back.
        store = loops.LoopStore(tempfile.mkdtemp())
        key = loops.program_key("co.hinge.mobile.ios", REQUEST)
        store.save_program(key, {**validate_program(deck_program(), request=REQUEST), "app": "co.hinge.mobile.ios"})
        self.assertIsNone(store.load_program(key, REQUEST))

    def test_the_runner_will_not_start_in_a_dating_app(self):
        program = validate_program(deck_program(), request=REQUEST)
        app = DeckApp(["Pier"], bundle="com.cardify.tinder")
        runner = LoopRunner(program, driver=app, request=REQUEST, judge=FakeJudge({"Pier": "no"}), crop=crop,
                            settle_seconds=0)
        with self.assertRaises(loops.LoopRefused):
            runner.run(app.observe())
        self.assertEqual(app.actions, [])
        with self.assertRaises(loops.LoopRefused):
            LoopRunner({**program, "app": "co.hinge.app"}, driver=app, request=REQUEST)


class AppearanceTests(unittest.TestCase):
    def test_a_question_about_a_persons_looks_or_body_is_refused(self):
        for question in APPEARANCE_QUESTIONS:
            with self.assertRaises(loops.LoopRefused, msg=question) as raised:
                validate_program(deck_program(predicate={"question": question}), request=REQUEST)
            self.assertEqual(raised.exception.reason, "appearance")
            self.assertIn("how they look", str(raised.exception))

    def test_the_summary_and_a_legacy_eye_colour_decomposition_are_refused_too(self):
        for raw in (deck_program(summary="Like profiles without blue eyes"),
                    deck_program(predicate={"question": "Is this photo blurry?", "decompose": "eye_colour",
                                            "match_values": ["blue"]})):
            with self.assertRaises(loops.LoopRefused):
                validate_program(raw, request=REQUEST)
        with self.assertRaises(ProgramError):  # nothing else was ever decomposed
            validate_program(deck_program(predicate={"decompose": "age"}), request=REQUEST)

    def test_the_request_is_refused_before_the_helper_is_asked(self):
        for request in ("favorite every photo of anyone who has blue eyes", "like every post by someone with long hair",
                        "swipe through the people and pass on anyone under 25",
                        "For each of the 8 images, report the eye colour of the person"):
            compiler = Mock()
            self.assertEqual(compile_loop(request, DeckApp(["Pier"]).screen(), compiler=compiler),
                             (None, "appearance"), request)
            compiler.compile.assert_not_called()

    def test_protected_characteristics_still_end_the_run_instead_of_falling_back_to_the_step_agent(self):
        with self.assertRaises(loops.LoopRefused) as raised:
            validate_program(deck_program(predicate={"question": "Is this person Muslim?"}), request=REQUEST)
        self.assertEqual(raised.exception.reason, "refused")
        screen = DeckApp(["Pier"]).screen()
        raw = deck_program(predicate={"question": "Is this person gay?"})
        self.assertEqual(compile_loop(REQUEST, screen, compiler=FakeCompiler(raw)), (None, "refused"))

    def test_content_questions_are_not_appearance(self):
        from mobile_agent.task_policy import appearance_predicate
        for question in ("Is there a dog in this photo?", "Is there a black dog in this photo?",
                         "Is this photo blurry?", "Is this post by @sam?", "Is this a receipt?",
                         "Does this image contain a real dog (not a toy, statue, drawing or sign)?",
                         "Is a person wearing sunglasses in this photo?", "Is this screenshot older than a month?",
                         "Is this a photo of a cat with green eyes?", "Is this a tall building?",
                         "Is this a short video?", "Does this recipe use low-fat milk?"):
            self.assertFalse(appearance_predicate(question), question)
            program = validate_program(deck_program(predicate={"question": question}), request=REQUEST)
            self.assertEqual(program["predicate"]["question"], question)
        for request in CONTENT_LOOPS:
            self.assertFalse(appearance_predicate(request), request)

    def test_the_sentences_a_person_reads_are_plain(self):
        self.assertEqual(loops.REFUSALS["dating_app"], "Mobster doesn't automate dating apps.")
        self.assertEqual(set(loops.REFUSALS), {"dating_app", "appearance", "refused"})
        for sentence in loops.REFUSALS.values():
            self.assertTrue(sentence.startswith("Mobster doesn't "))


if __name__ == "__main__":
    unittest.main()


class VisionFeedTests(unittest.TestCase):
    """Visible items of a list are judged in one batched VisionJudge call (judge_many)."""

    def test_visible_posts_are_judged_together(self):
        class Feed(FeedApp):
            def screen(self):
                elements = []
                for slot, index in enumerate(range(self.top, min(self.top + 3, len(self.posts)))):
                    author, _ = self.posts[index]
                    y = .05 + slot * .3
                    like = "Unlike" if index in self.liked else "Like"
                    elements += [Element(f"a{index}", author, "StaticText", (.05, y, .5, .04), locator=f"/a{index}"),
                                 Element(f"p{index}", "Photo", "Image", (.05, y + .05, .9, .14), locator=f"/p{index}"),
                                 Element(f"l{index}", like, "Button", (.05, y + .2, .12, .05), locator=f"/l{index}")]
                return snapshot(elements, bundle="com.example.feed")

            def capture_preview(self, timeout=3):
                visible = {round(.05 + slot * .3 + .05, 3): self.posts[index][1]
                           for slot, index in enumerate(range(self.top, min(self.top + 3, len(self.posts))))}
                return "data:image/png;base64," + base64.b64encode(json.dumps(visible).encode()).decode()

        class Batch:
            def __init__(self):
                self.batches = []

            def judge(self, question, crops, choices=("yes", "no", "unsure"), context=None, timeout=4.0):
                return Judgment(crops[0], .9)

            def judge_many(self, question, items, choices=("yes", "no", "unsure"), context=None, timeout=4.0):
                self.batches.append(len(items))
                return [Judgment(crops[0], .9) for crops in items]

        def crop_photo(image, rect):
            return json.loads(image.decode()).get(str(round(rect[1], 3)))

        program = feed_program()
        program["evidence"] = {"source": "vision", "max_photos": 1}
        program["predicate"] = {"question": "Is there a dog in this photo?"}
        judge = Batch()
        app = Feed([("@a", "yes"), ("@b", "no"), ("@c", "yes"), ("@d", "no"), ("@e", "yes")])
        request = "like every post with a dog in this feed; skip any you're not sure about"
        LoopRunner(validate_program(program, request=request), driver=app, request=request, judge=judge,
                   crop=crop_photo, settle_seconds=0).run(app.observe())
        self.assertEqual(app.liked, {0, 2, 4})
        self.assertEqual(judge.batches[0], 3)

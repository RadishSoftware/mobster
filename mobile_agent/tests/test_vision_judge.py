"""VisionJudge with a scripted fake model: no network, no phone."""

import base64
import io
import json
import threading
import time
import unittest

from PIL import Image

from mobile_agent.vision_judge import (CropJudgment, JudgeConfig, Judgment, LocalScreen, Step, VisionJudge,
                                       answer_probabilities, crop_for_element, encode_jpeg, eye_colour_steps,
                                       fit_threshold, rect_to_pixels, risk_coverage)
from mobile_agent.transport import TransportError
from mobile_agent.tests.timing import bound


COLOURS = {"red": (220, 20, 20), "green": (20, 200, 20), "blue": (20, 20, 220), "black": (5, 5, 5),
           "white": (250, 250, 250), "yellow": (230, 230, 20), "grey": (128, 128, 128)}


def jpeg(colour, size=(64, 64)):
    buffer = io.BytesIO()
    Image.new("RGB", size, COLOURS[colour]).save(buffer, "JPEG", quality=95)
    return buffer.getvalue()


def colour_of(data_url):
    raw = base64.b64decode(data_url.split(",", 1)[1])
    pixel = Image.open(io.BytesIO(raw)).convert("RGB").getpixel((8, 8))
    return min(COLOURS, key=lambda name: sum((a - b) ** 2 for a, b in zip(COLOURS[name], pixel)))


class FakeHelper:
    """Answers each image from a colour -> row script. ``rows`` values are dicts
    of step answers, e.g. {"answer": ("yes", .9)}."""

    def __init__(self, script, *, delay=0.0, raw=None, error=None, reverse=False, supports_logprobs=False,
                 logprobs=None):
        self.script, self.delay, self.raw, self.error, self.reverse = script, delay, raw, error, reverse
        self.supports_logprobs, self.logprobs = supports_logprobs, logprobs
        self.requests, self.closed = [], False

    def complete(self, messages, token_limit, timeout, purpose, **options):
        content = messages[1]["content"]
        colours = [colour_of(part["image_url"]["url"]) for part in content if part["type"] == "image_url"]
        self.requests.append({"colours": colours, "options": options, "timeout": timeout, "purpose": purpose,
                              "messages": messages})
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        if self.raw is not None:
            text = self.raw
        else:
            rows = []
            for index, colour in enumerate(colours, 1):
                row = {"image": index}
                for key, (answer, confidence) in self.script[colour].items():
                    row[key] = answer
                    if confidence is not None:
                        row[key + "_confidence"] = confidence
                rows.append(row)
            if self.reverse:
                rows.reverse()
            text = json.dumps({"results": rows})
        choice = {"message": {"content": text}, "finish_reason": "stop"}
        if self.logprobs is not None:
            choice["logprobs"] = self.logprobs(text)
        return {"choices": [choice], "usage": {"promptTokenCount": 10}}

    def close(self):
        self.closed = True


def judge_with(helper, **config):
    config.setdefault("accept", .8)  # the logic tests below were written against this threshold
    made = []

    def factory(model=None):
        made.append(model)
        return helper(model) if callable(helper) and not isinstance(helper, FakeHelper) else helper
    judge = VisionJudge(helper_factory=factory, config=JudgeConfig(**config))
    judge.made = made
    return judge


YES = {"answer": ("yes", .95)}
NO = {"answer": ("no", .95)}


class BatchingTests(unittest.TestCase):
    def test_batches_crops_and_maps_results_by_image_number(self):
        fake = FakeHelper({"red": YES, "blue": NO}, reverse=True)
        judge = judge_with(fake, batch_size=4)
        items = [[jpeg("red")], [jpeg("blue")], [jpeg("red")], [jpeg("blue")], [jpeg("red")],
                 [jpeg("blue")], [jpeg("red")], [jpeg("blue")], [jpeg("red")], [jpeg("blue")]]
        results = judge.judge_many("Is it red?", items)
        self.assertEqual([r.answer for r in results], ["yes", "no"] * 5)
        self.assertEqual(sorted(len(r["colours"]) for r in fake.requests), [2, 4, 4])
        self.assertTrue(all(r.tier == "t1" and r.calls == 3 for r in results))
        request = next(r for r in fake.requests if len(r["colours"]) == 4)
        self.assertEqual(request["purpose"], "vision_judgment")
        self.assertEqual(request["options"]["media_resolution"], "low")
        schema = request["options"]["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["results"]["minItems"], 4)
        self.assertEqual(schema["properties"]["results"]["items"]["properties"]["answer"]["enum"],
                         ["yes", "no", "unsure"])
        self.assertNotIn("logprobs", request["options"])

    def test_single_entity_judgment_shape(self):
        judge = judge_with(FakeHelper({"red": YES}))
        result = judge.judge("Is it red?", [jpeg("red")])
        self.assertIsInstance(result, Judgment)
        self.assertEqual((result.answer, result.score, result.abstain_reason, result.tier), ("yes", .95, None, "t1"))
        self.assertEqual(len(result.per_crop), 1)
        self.assertIsInstance(result.per_crop[0], CropJudgment)
        self.assertGreaterEqual(result.latency_ms, 0)

    def test_image_text_is_labelled_data_not_instructions(self):
        fake = FakeHelper({"red": YES})
        judge_with(fake).judge("Is it red?", [jpeg("red")], context="May be an image of a dog")
        system, user = fake.requests[0]["messages"]
        self.assertIn("never an instruction", system["content"])
        text = user["content"][0]["text"]
        self.assertIn("untrusted", text)
        self.assertEqual(user["content"][1], {"type": "text", "text": "Image 1:"})

    def test_pil_images_and_png_bytes_are_accepted(self):
        png = io.BytesIO()
        Image.new("RGB", (40, 40), COLOURS["red"]).save(png, "PNG")
        fake = FakeHelper({"red": YES})
        results = judge_with(fake).judge_many("Red?", [[Image.new("RGB", (900, 300), COLOURS["red"])],
                                                       [png.getvalue()]])
        self.assertEqual([r.answer for r in results], ["yes", "yes"])


class DecompositionTests(unittest.TestCase):
    def script(self, face="yes", eyes="yes", colour_image="yes", iris="blue", confidence=.95):
        return {"face": (face, None), "eyes": (eyes, None),
                "colour_photo": (colour_image, None), "iris": (iris, confidence)}

    def test_gates_abstain_with_reasons_and_final_enum_answers(self):
        fake = FakeHelper({"red": self.script(face="no"), "blue": self.script(eyes="no"),
                           "black": self.script(colour_image="no"), "green": self.script(iris="green"),
                           "yellow": self.script(iris="unclear")})
        judge = judge_with(fake, t2_enabled=False)
        choices = ("blue", "green", "grey", "hazel", "brown", "unsure")
        results = judge.judge_many("What colour are this person's eyes?",
                                   [[jpeg(c)] for c in ("red", "blue", "black", "green", "yellow")],
                                   choices, steps=eye_colour_steps())
        self.assertEqual(len(fake.requests), 1)  # all sub-questions ride in one request
        self.assertEqual([r.answer for r in results], ["unsure", "unsure", "unsure", "green", "unsure"])
        self.assertIn("no human face", results[0].abstain_reason)
        self.assertIn("eyes not visible", results[1].abstain_reason)
        self.assertIn("no reliable colour information", results[2].abstain_reason)
        self.assertIsNone(results[3].abstain_reason)
        self.assertIn("model unsure", results[4].abstain_reason)
        self.assertEqual(results[3].per_crop[0].steps["iris"]["answer"], "green")

    def test_final_answers_map_to_yes_no_and_gate_can_decide(self):
        steps = (Step("person_visible", "Is a person visible?", ("yes", "no", "unsure"), gate=("yes",), on_fail="no"),
                 Step("colour", "Iris colour?", ("blue", "brown", "unclear"),
                      mapping={"blue": "yes", "brown": "no", "unclear": "unsure"}))
        fake = FakeHelper({"red": {"person_visible": ("no", .99), "colour": ("blue", .9)},
                           "blue": {"person_visible": ("yes", .99), "colour": ("blue", .9)},
                           "green": {"person_visible": ("yes", .99), "colour": ("brown", .85)}})
        results = judge_with(fake).judge_many("Blue eyes?", [[jpeg("red")], [jpeg("blue")], [jpeg("green")]],
                                              steps=steps)
        self.assertEqual([r.answer for r in results], ["no", "yes", "no"])

    def test_gate_score_bounds_the_final_score(self):
        steps = (Step("face", "Face?", ("yes", "no", "unsure"), gate=("yes",), confidence=True),
                 Step("iris", "Iris?", ("blue", "brown", "unclear")))
        script = {"red": {"face": ("yes", .85), "iris": ("brown", .9)}}
        result = judge_with(FakeHelper(script), t2_enabled=False).judge(
            "Eye colour?", [jpeg("red")], ("blue", "brown", "unsure"), steps=steps)
        self.assertEqual((result.answer, result.score), ("brown", .85))
        product = judge_with(FakeHelper(script), t2_enabled=False, chain="product").judge(
            "Eye colour?", [jpeg("red")], ("blue", "brown", "unsure"), steps=steps)
        self.assertEqual((product.answer, product.score), ("unsure", .765))

    def test_gates_are_answer_only_by_default_and_unsure_gate_is_uncertain(self):
        fake = FakeHelper({"red": {"face": ("unsure", None), "eyes": ("yes", None), "colour_photo": ("yes", None),
                                   "iris": ("blue", .99)}})
        result = judge_with(fake, t2_enabled=False).judge("Eye colour?", [jpeg("red")], ("blue", "brown", "unsure"),
                                                          steps=eye_colour_steps(("blue", "brown")))
        schema = fake.requests[0]["options"]["response_format"]["json_schema"]["schema"]
        properties = schema["properties"]["results"]["items"]["properties"]
        self.assertNotIn("face_confidence", properties)
        self.assertIn("iris_confidence", properties)
        self.assertEqual(result.answer, "unsure")
        self.assertIn("uncertain: face", result.abstain_reason)

    def test_invalid_step_specs_are_rejected(self):
        judge = judge_with(FakeHelper({}))
        with self.assertRaises(ValueError):
            Step("Bad Key", "q", ("yes", "no"))
        with self.assertRaises(ValueError):
            judge.judge("q", [jpeg("red")], steps=(Step("a", "q", ("yes", "no")), Step("b", "q", ("yes", "no"))))
        with self.assertRaises(ValueError):
            judge.judge("q", [jpeg("red")], steps=(Step("b", "q", ("purple", "unclear")),))


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.script = {"blue": {"answer": ("blue", .9)}, "green": {"answer": ("green", .9)},
                       "grey": {"answer": ("blue", .6)}, "white": {"answer": ("unsure", .5)},
                       "red": {"answer": ("yes", .95)}, "black": {"answer": ("no", .95)},
                       "yellow": {"answer": ("yes", .55)}}
        self.choices = ("blue", "green", "unsure")

    def judge(self, crops, choices=None, **kwargs):
        judge = judge_with(FakeHelper(self.script), t2_enabled=False)
        return judge.judge("Q?", [jpeg(c) for c in crops], choices or self.choices, **kwargs)

    def test_consensus_requires_agreement(self):
        self.assertEqual(self.judge(["blue", "blue"]).answer, "blue")
        disagree = self.judge(["blue", "green"])
        self.assertEqual(disagree.answer, "unsure")
        self.assertIn("disagree", disagree.abstain_reason)
        # A plausible (above-floor) contradicting photo also blocks consensus.
        self.assertEqual(self.judge(["green", "grey"]).answer, "unsure")
        # An abstaining photo does not block agreement between the others.
        self.assertEqual(self.judge(["blue", "white", "blue"]).answer, "blue")

    def test_min_agree(self):
        result = self.judge(["blue", "white"], min_agree=2)
        self.assertEqual(result.answer, "unsure")
        self.assertIn("2 required", result.abstain_reason)
        self.assertEqual(self.judge(["blue", "blue"], min_agree=2).answer, "blue")

    def test_any_and_all(self):
        yn = ("yes", "no", "unsure")
        self.assertEqual(self.judge(["black", "red", "black"], yn, aggregate="any").answer, "yes")
        self.assertEqual(self.judge(["black", "black"], yn, aggregate="any").answer, "no")
        inconclusive = self.judge(["black", "yellow"], yn, aggregate="any")
        self.assertEqual(inconclusive.answer, "unsure")
        self.assertIn("no photo shows it conclusively", inconclusive.abstain_reason)
        self.assertEqual(self.judge(["red", "red"], yn, aggregate="all").answer, "yes")
        self.assertEqual(self.judge(["red", "black"], yn, aggregate="all").answer, "no")
        self.assertEqual(self.judge(["red", "yellow"], yn, aggregate="all").answer, "unsure")
        self.assertEqual(self.judge(["black"], yn, aggregate="any", positive="no").answer, "no")


class ThresholdAndLookAgainTests(unittest.TestCase):
    def test_below_floor_abstains_without_look_again(self):
        fake = FakeHelper({"red": {"answer": ("yes", .3)}})
        result = judge_with(fake).judge("Red?", [jpeg("red")])
        self.assertEqual(result.answer, "unsure")
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(result.score, .3)

    def test_uncertain_band_looks_again_at_high_resolution(self):
        low = FakeHelper({"red": {"answer": ("yes", .65)}})
        high = FakeHelper({"red": {"answer": ("yes", .97)}})
        helpers = {"flash": low, "pro": high}
        judge = VisionJudge(helper_factory=lambda model=None: helpers[model],
                            config=JudgeConfig(model="flash", t2_model="pro"))
        hires = Image.new("RGB", (1600, 1600), COLOURS["red"])
        result = judge.judge("Red?", [jpeg("red")], hires=[hires])
        self.assertEqual((result.answer, result.tier, result.calls), ("yes", "t2", 2))
        self.assertEqual(high.requests[0]["options"]["media_resolution"], "high")
        sent = base64.b64decode(high.requests[0]["messages"][1]["content"][2]["image_url"]["url"].split(",", 1)[1])
        self.assertEqual(max(Image.open(io.BytesIO(sent)).size), 1024)  # t2_max_side

    def test_hires_callable_and_same_model_higher_resolution(self):
        answers = iter([{"red": {"answer": ("unsure", .4)}}, {"red": {"answer": ("no", .9)}}])

        class Sequenced(FakeHelper):
            def complete(self, *args, **kwargs):
                self.script = next(answers)
                return super().complete(*args, **kwargs)
        fake = Sequenced({})
        asked = []

        def hires(index):
            asked.append(index)
            return jpeg("red", (800, 800))
        result = judge_with(fake).judge("Red?", [jpeg("red")], hires=hires)
        self.assertEqual(asked, [0])
        self.assertEqual((result.answer, result.tier), ("no", "t2"))
        self.assertEqual([r["options"]["media_resolution"] for r in fake.requests], ["low", "high"])

    def test_still_uncertain_after_look_again_abstains_with_both_scores(self):
        fake = FakeHelper({"red": {"answer": ("yes", .7)}})
        result = judge_with(fake).judge("Red?", [jpeg("red", (900, 900))])
        self.assertEqual((result.answer, result.tier), ("unsure", "t2"))
        self.assertIn("uncertain after look-again", result.abstain_reason)
        self.assertEqual(len(fake.requests), 2)

    def test_per_answer_thresholds(self):
        fake = FakeHelper({"red": {"answer": ("yes", .9)}, "blue": {"answer": ("no", .9)}})
        judge = judge_with(fake, thresholds={"yes": .95}, t2_enabled=False)
        results = judge.judge_many("Red?", [[jpeg("red")], [jpeg("blue")]])
        self.assertEqual([r.answer for r in results], ["unsure", "no"])

    def test_calibration_helpers(self):
        scores = [.99, .95, .9, .8, .7, .6]
        correct = [True, True, True, False, True, False]
        curve = risk_coverage(scores, correct)
        self.assertEqual(curve[2], (.9, .5, 1.0, 3))
        self.assertEqual(fit_threshold(scores, correct, 1.0), .9)
        self.assertEqual(fit_threshold(scores, correct, .8), .7)
        self.assertIsNone(fit_threshold([.5], [False], .9))


class FailureTests(unittest.TestCase):
    def test_malformed_output_abstains_and_never_looks_again(self):
        bad = ["not json", json.dumps({"results": []}),
               json.dumps({"results": [{"image": 1, "answer": "yes", "answer_confidence": .9},
                                       {"image": 1, "answer": "no", "answer_confidence": .9}]}),
               json.dumps({"results": [{"image": 1, "answer": "maybe", "answer_confidence": .9}]}),
               json.dumps({"results": [{"image": 1, "answer": "yes"}]}),
               json.dumps({"results": [{"image": 1, "answer": "yes", "answer_confidence": 1.7}]}),
               json.dumps({"results": [{"image": 2, "answer": "yes", "answer_confidence": .9}]})]
        for raw in bad:
            with self.subTest(raw=raw):
                fake = FakeHelper({}, raw=raw)
                result = judge_with(fake).judge("Red?", [jpeg("red", (900, 900))])
                self.assertEqual(result.answer, "unsure")
                self.assertIn("malformed", result.abstain_reason)
                self.assertEqual(len(fake.requests), 1)

    def test_model_error_retries_once_then_abstains(self):
        fake = FakeHelper({}, error=TransportError("HTTP 429; request not retried"))
        result = judge_with(fake).judge("Red?", [jpeg("red")])
        self.assertEqual(result.answer, "unsure")
        self.assertIn("model error", result.abstain_reason)
        self.assertTrue(fake.closed)  # an errored helper is not reused
        self.assertEqual((len(fake.requests), result.calls), (2, 2))
        no_retry = FakeHelper({}, error=TransportError("HTTP 503; request not retried"))
        judge_with(no_retry, retries=0).judge("Red?", [jpeg("red")])
        self.assertEqual(len(no_retry.requests), 1)

    def test_retry_recovers_a_transient_error(self):
        class Flaky(FakeHelper):
            def complete(self, *args, **kwargs):
                self.error = TransportError("HTTP 429; request not retried") if not self.requests else None
                return super().complete(*args, **kwargs)
        fake = Flaky({"red": YES})
        result = judge_with(fake).judge("Red?", [jpeg("red")])
        self.assertEqual((result.answer, result.calls), ("yes", 2))

    def test_no_retry_without_time_left(self):
        fake = FakeHelper({}, error=TransportError("HTTP 429; request not retried"))
        judge_with(fake).judge("Red?", [jpeg("red")], timeout=.5)
        self.assertEqual(len(fake.requests), 1)

    def test_timeout_abstains_within_budget_and_discards_helper(self):
        fake = FakeHelper({"red": YES}, delay=1.0)
        started = time.monotonic()
        result = judge_with(fake).judge("Red?", [jpeg("red")], timeout=.3)
        self.assertLess(time.monotonic() - started, bound(.8))
        self.assertEqual(result.answer, "unsure")
        self.assertIn("timeout", result.abstain_reason)
        self.assertTrue(fake.closed)
        self.assertLessEqual(fake.requests[0]["timeout"], .3)

    def test_slow_request_is_hedged_and_first_answer_wins(self):
        class StallsOnce(FakeHelper):
            def complete(self, *args, **kwargs):
                self.delay = 2.0 if not self.requests else 0.0
                return super().complete(*args, **kwargs)
        made = []

        def factory(model=None):
            made.append(StallsOnce({"red": YES}))
            made[-1].requests = shared
            return made[-1]
        shared = []
        judge = VisionJudge(helper_factory=factory, config=JudgeConfig(hedge_after_seconds=.1,
                                                                       hedge_per_crop_seconds=0))
        started = time.monotonic()
        result = judge.judge("Red?", [jpeg("red")], timeout=3)
        self.assertLess(time.monotonic() - started, bound(1.0))
        self.assertEqual((result.answer, result.calls), ("yes", 2))
        self.assertTrue(made[0].closed)  # the stalled twin was aborted, not reused

    def test_hedging_can_be_disabled(self):
        fake = FakeHelper({"red": YES}, delay=.3)
        result = judge_with(fake, hedge_after_seconds=None).judge("Red?", [jpeg("red")])
        self.assertEqual((result.answer, len(fake.requests)), ("yes", 1))

    def test_unreadable_crop_abstains_without_a_call(self):
        fake = FakeHelper({"red": YES})
        results = judge_with(fake).judge_many("Red?", [[b"not an image"], [jpeg("red")]])
        self.assertEqual([r.answer for r in results], ["unsure", "yes"])
        self.assertEqual(fake.requests[0]["colours"], ["red"])

    def test_choices_must_include_abstention_and_answers_are_choices(self):
        judge = judge_with(FakeHelper({"red": YES}))
        with self.assertRaises(ValueError):
            judge.judge("Red?", [jpeg("red")], choices=("yes", "no"))
        with self.assertRaises(ValueError):
            judge.judge("Red?", [], choices=("yes", "no", "unsure"))
        with self.assertRaises(ValueError):
            judge.judge("Red?", [jpeg("red")], timeout=0)
        result = judge.judge("Red?", [jpeg("red")], choices=("yes", "no", "unclear"))
        self.assertIn(result.answer, ("yes", "no", "unclear"))


class LocalTierTests(unittest.TestCase):
    def test_local_screen_decides_without_model_calls(self):
        class Labels(LocalScreen):
            def screen(self, predicate, crops):
                return [("yes", .99), None]
        fake = FakeHelper({"blue": NO})
        judge = VisionJudge(helper_factory=lambda model=None: fake, local=Labels())
        results = judge.judge_many("Dog?", [[jpeg("red")], [jpeg("blue")]], predicate="dog")
        self.assertEqual([(r.answer, r.tier) for r in results], [("yes", "t0"), ("no", "t1")])
        self.assertEqual(fake.requests[0]["colours"], ["blue"])

    def test_local_screen_is_skipped_without_a_predicate_and_its_errors_are_ignored(self):
        class Broken(LocalScreen):
            def screen(self, predicate, crops):
                raise RuntimeError("boom")
        fake = FakeHelper({"red": YES})
        judge = VisionJudge(helper_factory=lambda model=None: fake, local=Broken())
        self.assertEqual(judge.judge("Dog?", [jpeg("red")], predicate="dog").answer, "yes")


class LogprobTests(unittest.TestCase):
    @staticmethod
    def logprobs_for(text, p_yes=.9):
        # Tokenise around each answer value like Gemini: '"answer": "', 'yes', '", ...'
        import math
        tokens, position = [], 0
        for match in __import__("re").finditer(r'"answer"\s*:\s*"', text):
            if match.end() > position:
                tokens.append({"token": text[position:match.end()], "logprob": 0.0, "top_logprobs": []})
            value = text[match.end():text.index('"', match.end())]
            other = "no" if value == "yes" else "yes"
            tokens.append({"token": value, "logprob": math.log(p_yes),
                           "top_logprobs": [{"token": value, "logprob": math.log(p_yes)},
                                            {"token": other, "logprob": math.log(1 - p_yes)}]})
            position = match.end() + len(value)
        tokens.append({"token": text[position:], "logprob": 0.0, "top_logprobs": []})
        return {"content": tokens}

    def test_answer_probabilities(self):
        text = json.dumps({"results": [{"image": 1, "answer": "yes", "answer_confidence": 1.0},
                                       {"image": 2, "answer": "no", "answer_confidence": 1.0}]})
        probs = answer_probabilities(text, self.logprobs_for(text, .8), (Step("answer", "q", ("yes", "no", "unsure")),))
        self.assertAlmostEqual(probs[("answer", 0)], .8)
        self.assertAlmostEqual(probs[("answer", 1)], .8)
        self.assertEqual(answer_probabilities(text + " ", self.logprobs_for(text), (Step("answer", "q", ("yes", "no")),)), {})
        # Extra trailing tokens (a closing code fence) do not break alignment.
        fenced = self.logprobs_for(text, .8)
        fenced["content"].append({"token": "\n```", "logprob": 0.0, "top_logprobs": []})
        self.assertAlmostEqual(answer_probabilities(text, fenced, (Step("answer", "q", ("yes", "no")),))[("answer", 1)], .8)

    def test_judge_prefers_logprob_score_when_supported(self):
        fake = FakeHelper({"red": {"answer": ("yes", 1.0)}, "blue": {"answer": ("no", 1.0)}},
                          supports_logprobs=True, logprobs=lambda text: self.logprobs_for(text, .7), reverse=True)
        results = judge_with(fake, t2_enabled=False).judge_many("Red?", [[jpeg("red")], [jpeg("blue")]])
        self.assertEqual(fake.requests[0]["options"]["logprobs"], 5)
        self.assertEqual([r.answer for r in results], ["unsure", "unsure"])  # .7 < accept despite 1.0 self-report
        steps = results[0].per_crop[0].steps["answer"]
        self.assertEqual((steps["confidence"], round(steps["logprob"], 3)), (1.0, .7))


class CropTests(unittest.TestCase):
    def test_rect_to_pixels_pads_and_clamps(self):
        self.assertEqual(rect_to_pixels((.25, .5, .5, .25), (400, 800), padding=0), (100, 400, 300, 600))
        self.assertEqual(rect_to_pixels((.25, .5, .5, .25), (400, 800), padding=.1), (80, 380, 320, 620))
        self.assertEqual(rect_to_pixels((0, 0, 1, 1), (400, 800), padding=.2), (0, 0, 400, 800))
        for bad in [(0, 0, 0, .5), (.9, .9, .5, .5), ("a", 0, 1, 1), (0, 0, 1)]:
            with self.assertRaises(ValueError):
                rect_to_pixels(bad, (400, 800))

    def test_crop_for_element_from_jpeg_bytes_and_element(self):
        frame = Image.new("RGB", (590, 1278), COLOURS["white"])
        frame.paste(Image.new("RGB", (295, 320), COLOURS["red"]), (0, 320))
        buffer = io.BytesIO()
        frame.save(buffer, "JPEG")

        class Element:
            rect = (0.0, 0.25, 0.5, 0.25)
        crop = crop_for_element(buffer.getvalue(), Element(), padding=0)
        self.assertEqual(crop.size, (295, 320))
        self.assertEqual(colour_of("data:image/jpeg;base64," + base64.b64encode(encode_jpeg(crop)).decode()), "red")
        self.assertEqual(max(crop_for_element(frame, Element.rect, max_side=100).size), 100)

    def test_encode_jpeg_passes_small_jpeg_through(self):
        data = jpeg("red")
        self.assertIs(encode_jpeg(data, 512), data)
        self.assertEqual(max(Image.open(io.BytesIO(encode_jpeg(jpeg("red", (2000, 1000)), 512))).size), 512)


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_batches_use_separate_helpers(self):
        active, peak, lock = [0], [0], threading.Lock()

        class Tracking(FakeHelper):
            def complete(self, *args, **kwargs):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                try:
                    time.sleep(.05)
                    return super().complete(*args, **kwargs)
                finally:
                    with lock:
                        active[0] -= 1
        made = []

        def factory(model=None):
            made.append(Tracking({"red": YES}))
            return made[-1]
        judge = VisionJudge(helper_factory=factory, config=JudgeConfig(batch_size=2, max_parallel=3))
        results = judge.judge_many("Red?", [[jpeg("red")] for _ in range(6)])
        self.assertEqual({r.answer for r in results}, {"yes"})
        self.assertGreaterEqual(len(made), 2)
        self.assertGreaterEqual(peak[0], 2)
        # Helpers are reused by later judgments rather than rebuilt.
        before = len(made)
        judge.judge("Red?", [jpeg("red")])
        self.assertEqual(len(made), before)


if __name__ == "__main__":
    unittest.main()

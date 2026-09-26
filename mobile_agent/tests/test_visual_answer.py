"""Visual answers: a question about the picture on screen, answered by the VisionJudge. Offline."""

import unittest
from unittest.mock import Mock

from mobile_agent.agent import Agent
from mobile_agent.loops import Judgment
from mobile_agent.models import Decision
from mobile_agent.state import Element, Snapshot
from mobile_agent.task_policy import StopGate
from mobile_agent.visual_answer import image_on_screen, listed_options, visual_request

FILES = "com.apple.DocumentsApp"
DRY = (" This is a dry run: only look at the images (you may open each one to view it and close it again). "
       "Do not favorite, share, rename, move, edit or delete anything.")
ANSWER_GOAL = ("In Files, open On My iPhone > MobsterBench > eyes8 and view eyes8-06. What colour are the person's "
               "eyes (blue, green, grey, hazel or brown)?" + DRY)
ABSTAIN_GOAL = ("In Files, open On My iPhone > MobsterBench > eyes8 and view eyes8-01. What colour are the person's "
                "eyes? If they cannot be seen clearly enough to tell, say that you cannot tell." + DRY)
SCHEMA = {"type": "object", "properties": {"eye_colour": {"type": "string"}}, "required": ["eye_colour"],
          "additionalProperties": False}


class Viewer:
    """The Files image viewer (diag-5 AX tree): title, close button, one full-width Image."""

    can_type = False

    def __init__(self, name="eyes8-06", image=True):
        self.name, self.image = name, image
        self.actions, self.captures = [], 0

    def observe(self, timeout=10):
        elements = [Element("n", self.name, "NavigationBar", (0, .07, .997, .061), locator="/nav"),
                    Element("m", f"{self.name}, Actions Menu", "Button", (.285, .085, .366, .023), locator="/menu"),
                    Element("x", "close", "Button", (.855, .075, .092, .042), locator="/close"),
                    Element("s", "Share", "Button", (.814, .915, .102, .047), locator="/share")]
        if self.image:
            elements.append(Element("i", "Liftable subject available", "Image", (0, .338, .997, .324),
                                    locator="/image"))
        return Snapshot(elements, "\n".join(e.label for e in elements), 400, 800, "wda", bundle_id=FILES)

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, target.label if target else None))

    def capture_preview(self, timeout=3):
        self.captures += 1
        return b"\xff\xd8 still of " + self.name.encode()


class ImageJudge:
    """Answers from a fixed judgment; crops are (image, rect) pairs so tests can see what was judged."""

    def __init__(self, answer, score=.97, reason=None):
        self.judgment = Judgment(answer, score, abstain_reason=reason, tier="t1")
        self.calls = []

    @staticmethod
    def crop(image, rect):
        return (image, tuple(rect))

    def judge(self, question, crops, choices=("yes", "no", "unsure"), context=None, timeout=4.0, *,
              steps=None, hires=None):
        self.calls.append({"question": question, "crops": crops, "choices": choices, "steps": steps})
        return self.judgment


def blocked_model():
    model = Mock(spec=["decide", "stable_completion"], stable_completion=True)
    model.decide.return_value = Decision("BLOCKED", None, .9, 0, .9, "t", 0, {}, StopGate.CONTINUE)
    return model


def run(goal, app, judge, model=None, schema=SCHEMA, events=None):
    return Agent(app, model or blocked_model(), None, settle_seconds=0, max_steps=1, vision_judge=judge,
                 emit=(events.append if events is not None else None)).run(
        goal, execute=True, output_schema=schema, output_format="json")


class DetectionTests(unittest.TestCase):
    def test_the_answer_set_comes_from_the_request(self):
        self.assertEqual(listed_options("What colour (blue, green, grey, hazel or brown)?"),
                         ["blue", "green", "grey", "hazel", "brown"])
        self.assertEqual(listed_options("What colour is the car: red, blue or white?"), ["red", "blue", "white"])
        self.assertIsNone(listed_options("only look (you may open each one to view it and close it again)"))
        request = visual_request(ANSWER_GOAL, SCHEMA)
        self.assertEqual(list(request["choices"]), ["blue", "green", "grey", "hazel", "brown"])
        self.assertEqual((request["named"], request["abstain_allowed"]), ("eyes8-06", False))
        self.assertTrue(visual_request(ABSTAIN_GOAL, SCHEMA)["abstain_allowed"])
        self.assertEqual(list(visual_request("Is there a dog in this photo?", SCHEMA)["choices"]), ["yes", "no"])

    def test_non_visual_or_open_questions_do_not_trigger(self):
        for goal in ("In Settings, what is the battery percentage?",
                     "Open eyes8-06. What does the sign in the photo say?",
                     "What colour is the car in the picture?",                        # no answer set
                     "What is the person's skin colour in the photo (light or dark)?",  # protected
                     "For each of the 8 images, report the eye colour (blue or brown)."):  # a survey
            self.assertIsNone(visual_request(goal, SCHEMA), goal)
        two = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
        self.assertIsNone(visual_request(ANSWER_GOAL, two))

    def test_the_real_judge_accepts_the_choices_and_decomposition(self):
        from mobile_agent.vision_judge import VisionJudge
        from mobile_agent.visual_answer import ABSTAIN, _steps
        judge = VisionJudge(helper_factory=lambda model=None: None)
        self.addCleanup(judge.close)
        for goal in (ANSWER_GOAL, ABSTAIN_GOAL, "View IMG_0042. Is the person in the photo wearing sunglasses?",
                     "View IMG_0042. What colour are the person's eyes (blue, gray or brown)?"):
            request = visual_request(goal, SCHEMA)
            choices, abstain = judge._validate(request["question"], (*request["choices"], ABSTAIN), None,
                                               "consensus", None, 1)
            self.assertTrue(judge._steps(request["question"], choices, abstain, _steps(request)), goal)

    def test_only_the_named_picture_open_in_a_viewer_counts(self):
        request = visual_request(ANSWER_GOAL, SCHEMA)
        self.assertEqual(image_on_screen(Viewer("eyes8-06").observe(), request).id, "i")
        self.assertIsNone(image_on_screen(Viewer("eyes8-05").observe(), request))
        self.assertIsNone(image_on_screen(Viewer("eyes8-06", image=False).observe(), request))
        grid = Snapshot([Element("t", "eyes8-06", "StaticText", (.03, .32, .29, .02)),
                         Element("g", "thumbnail", "Image", (.02, .22, .3, .2))], "eyes8-06", 400, 800, "wda")
        self.assertIsNone(image_on_screen(grid, request))


class AgentTests(unittest.TestCase):
    def test_an_answer_from_the_allowed_set_is_a_recorded_visual_judgment(self):
        app, judge, events = Viewer("eyes8-06"), ImageJudge("grey"), []
        result = run(ANSWER_GOAL, app, judge, events=events)
        self.assertEqual(result["data"], {"eye_colour": "grey"})
        self.assertEqual((result["status"], result["data_status"]), ("completed_unverified", "extracted"))
        self.assertEqual(result["citations"], [])  # a judgment, never a cited literal
        visual = result["visual_evidence"]
        self.assertEqual((visual["kind"], visual["tier"], visual["answer"]), ("visual_judgment", "t1", "grey"))
        self.assertEqual(len(visual["image_sha"]), 16)
        self.assertEqual(judge.calls[0]["choices"], ("blue", "green", "grey", "hazel", "brown", "unsure"))
        self.assertEqual(judge.calls[0]["crops"], [(b"\xff\xd8 still of eyes8-06", (0, .338, .997, .324))])
        self.assertEqual(judge.calls[0]["steps"][-1].key, "iris")  # the face/eyes/colour decomposition
        self.assertEqual(app.actions, [])
        self.assertNotIn("decision", [e["event"] for e in events])

    def test_an_unsure_judge_is_an_honest_abstention_when_the_request_allows_it(self):
        judge = ImageJudge("unsure", .6, reason="no conclusive photo: eyes not visible")
        result = run(ABSTAIN_GOAL, Viewer("eyes8-01"), judge)
        self.assertIsNone(result["data"])
        self.assertEqual((result["status"], result["data_status"]), ("completion_not_confirmed",
                                                                     "insufficient_evidence"))
        self.assertTrue(result["visual_evidence"]["abstention_allowed"])
        from mobile_agent.bench.agents.mobster import map_result
        self.assertEqual(map_result(result, Mock(answer=(("eye_colour", "string"),)))[0], "abstained")

    def test_a_confident_but_uncalibrated_eye_colour_abstains(self):
        result = run(ABSTAIN_GOAL, Viewer("eyes8-01"), ImageJudge("brown", .9))
        self.assertIsNone(result["data"])
        self.assertEqual(result["data_status"], "insufficient_evidence")

    def test_a_judge_error_leaves_the_run_to_the_step_agent(self):
        judge, events = ImageJudge("unsure", 0, reason="no conclusive photo: model error"), []
        result = run(ANSWER_GOAL, Viewer("eyes8-06"), judge, events=events)
        self.assertIn("decision", [e["event"] for e in events])
        self.assertNotIn("visual_evidence", result)

    def test_a_non_visual_question_never_calls_the_judge(self):
        judge, events = ImageJudge("grey"), []
        run("In Files, view eyes8-06. What is its file size?", Viewer("eyes8-06"), judge, events=events)
        self.assertEqual(judge.calls, [])
        self.assertIn("decision", [e["event"] for e in events])

    def test_without_a_judge_nothing_changes(self):
        app, events = Viewer("eyes8-06"), []
        result = run(ANSWER_GOAL, app, None, events=events)
        self.assertEqual(app.captures, 0)
        self.assertIn("decision", [e["event"] for e in events])
        self.assertNotIn("visual_evidence", result)
        self.assertIsNone(result["data"])


if __name__ == "__main__":
    unittest.main()

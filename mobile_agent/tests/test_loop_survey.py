"""Survey loops: a question over every item of a collection, answered in code from judged items. Offline."""

import base64
import hashlib
import json
import unittest
import unittest.mock
from unittest.mock import Mock

from mobile_agent import loops
from mobile_agent.agent import Agent
from mobile_agent.loops import (LoopRunner, NoItemsOnScreen, ProgramError, compile_loop, item_name, looks_iterative,
                                stated_count, survey_answer, validate_program)
from mobile_agent.models import Decision
from mobile_agent.state import Element, Snapshot
from mobile_agent.task_policy import StopGate
from mobile_agent.tests.test_loops import FakeCompiler, FakeJudge, FakeTools

FILES = "com.apple.DocumentsApp"
PHOTOS = "com.apple.mobileslideshow"
DRY = (" This is a dry run: only look at the images (you may open each one to view it and close it again). "
       "Do not favorite, share, rename, move, edit or delete anything.")
DOGS_GOAL = ("In Files: In Files, open On My iPhone > MobsterBench > dogs12. Which of the 12 images contain a real "
             "dog (not a toy, statue, drawing or sign)? Report the file names." + DRY)
EYES_GOAL = ("In Files: In Files, open On My iPhone > MobsterBench > eyes8. For each of the 8 images, report the eye "
             "colour of the person (blue, green, grey, hazel or brown), or 'unsure' when the eyes cannot be seen "
             "clearly enough to tell." + DRY)
ALBUM_GOAL = ("In Photos: In Photos, open the album 'MobsterBench Dogs'. How many of its photos contain a real dog "
              "(not a toy, statue, drawing or sign)?" + DRY)
NAMES_SCHEMA = {"type": "object", "properties": {"dog_files": {"type": "array", "items": {"type": "string"},
                                                               "maxItems": 100}},
                "required": ["dog_files"], "additionalProperties": False}
COUNT_SCHEMA = {"type": "object", "properties": {"count": {"type": "string"}}, "required": ["count"],
                "additionalProperties": False}
LABELS_SCHEMA = {"type": "object", "properties": {"eye_colours": {"type": "array", "items": {
    "type": "object", "properties": {"file": {"type": "string"}, "label": {"type": "string"}},
    "required": ["file", "label"], "additionalProperties": False}, "maxItems": 100}},
    "required": ["eye_colours"], "additionalProperties": False}


def files_label(name):
    return f"{name}, jpg, 12:15 AM, 50 KB"


class GridApp:
    """A Files folder or Photos album grid, three cells per row; the root screen holds the folder.

    Each cell's pixels are its subject: ``capture_preview`` encodes where each
    subject sits, and ``grid_crop`` cuts one out by the cell's rect.
    """

    can_type = False

    def __init__(self, labels, subjects=None, *, bundle=FILES, rows=4, at_root=False, folder="dogs12"):
        self.labels = list(labels)
        self.subjects = list(subjects or [item_name(label) for label in labels])
        self.bundle, self.rows, self.at_root, self.folder = bundle, rows, at_root, folder
        self.top = 0
        self.actions = []

    def cells(self):
        out = []
        for slot, index in enumerate(range(self.top, min(self.top + self.rows * 3, len(self.labels)))):
            row, col = divmod(slot, 3)
            out.append((index, (round(col / 3 + .005, 3), round(.12 + row * .2, 3), .32, .19)))
        return out

    def observe(self, timeout=10):
        if self.at_root:
            return Snapshot([Element("t", "MobsterBench", "NavigationBar", (0, .05, 1, .05), locator="/title"),
                             Element("f", f"{self.folder}, folder, {len(self.labels)} items", "Cell",
                                     (0, .12, 1, .08), locator="/folder")],
                            "MobsterBench", 400, 800, "wda", bundle_id=self.bundle)
        elements = [Element("t", self.folder, "NavigationBar", (0, .05, 1, .05), locator="/title")]
        elements += [Element(f"c{index}", self.labels[index], "Cell", rect, locator=f"/c{index}")
                     for index, rect in self.cells()]
        elements.append(Element("tab", "Browse", "Button", (.4, .94, .2, .05), locator="/tab"))
        return Snapshot(elements, "\n".join(e.label for e in elements), 400, 800, "wda", bundle_id=self.bundle)

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, target.label if target else None))
        if operation == "TAP" and self.at_root and target.id == "f":
            self.at_root = False
        elif operation == "SWIPE_UP" and self.top + self.rows * 3 < len(self.labels):
            self.top += 3

    def capture_preview(self, timeout=3):
        where = {f"{rect[1]},{rect[0]}": self.subjects[index] for index, rect in self.cells()}
        return "data:image/png;base64," + base64.b64encode(json.dumps(where).encode()).decode()

    def taps(self):
        return [label for operation, label in self.actions if operation == "TAP"]


def grid_crop(image, rect):
    return json.loads(image.decode()).get(f"{round(rect[1], 3)},{round(rect[0], 3)}")


class GridJudge(FakeJudge):
    crop = staticmethod(grid_crop)


def survey_program(kind="names", *, prefix="dogs12-", app=FILES, unsure="stop", **overrides):
    program = {"summary": "Find the images with a real dog", "app": app,
               "feed": {"kind": "grid", "item": {"roles": ["Cell"], "label_prefix": prefix},
                        "advance": {"by": "swipe", "operation": "SWIPE_UP"}},
               "identity": {"keys": ["label"]},
               "evidence": {"source": "vision", "max_photos": 1},
               "predicate": {"question": "Does this image contain a real dog (not a toy, statue, drawing or sign)?",
                             "local_label": "dog"},
               "targets": {}, "branches": {"true": [], "false": [], "unsure": unsure},
               "stop": {"count_items": 12}, "policy": {"stop_stated": True, "irreversible": False},
               "report": {"kind": kind}}
    program.update(overrides)
    return program


def eyes_program():
    return survey_program("labels", prefix="eyes8-", predicate={
        "question": "What colour are this person's eyes?",
        "choices": ["blue", "green", "grey", "hazel", "brown", "unsure"], "decompose": "eye_colour"},
        branches={"true": [], "false": [], "unsure": "skip"}, stop={"count_items": 8})


def dogs(n=12):
    return [f"dogs12-{i:02d}" for i in range(1, n + 1)]


def run_survey(app, answers, program, request):
    runner = LoopRunner(validate_program(program, request=request), driver=app, request=request,
                        judge=FakeJudge(answers), crop=grid_crop, settle_seconds=0, dry_run=True)
    return runner.run(app.observe()), runner


class Helper:
    def __init__(self, program):
        self.program, self.calls = program, 0

    def complete(self, messages, token_limit, timeout, purpose):
        self.calls += 1
        body = {"loop": True, "program": self.program} if self.program else {"loop": False}
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(body)}}]}


def no_model():
    model = Mock(spec=["decide", "stable_completion"], stable_completion=True)
    model.decide.side_effect = AssertionError("A survey needs no step decision")
    return model


class DetectionTests(unittest.TestCase):
    def test_questions_over_every_item_are_surveys_and_one_item_questions_are_not(self):
        for goal in (DOGS_GOAL.replace(DRY, ""), EYES_GOAL.replace(DRY, ""), ALBUM_GOAL.replace(DRY, ""),
                     "In which of the 12 images is a person wearing sunglasses on their face?",
                     "How many of the photos in this album are receipts?"):
            self.assertTrue(looks_iterative(goal), goal)
        for goal in ("In Files, open eyes8 and view eyes8-01. What colour are the person's eyes?",
                     "what colour are the person's eyes in eyes8-01?",
                     "In Reminders, report how many incomplete reminders it has",
                     "Which of these settings is on?"):
            self.assertFalse(looks_iterative(goal), goal)

    def test_the_stated_collection_size_and_file_names_are_read_in_code(self):
        self.assertEqual(stated_count(DOGS_GOAL), 12)
        self.assertEqual(stated_count(EYES_GOAL), 8)
        self.assertIsNone(stated_count(ALBUM_GOAL))
        self.assertEqual(item_name("dogs12-01, jpg, 12:15 AM, 50 KB"), "dogs12-01")
        self.assertEqual(item_name("dogs12-01.jpg, Sep 23, 2026, 50 KB"), "dogs12-01.jpg")
        self.assertEqual(item_name("eyes8-03, JPEG image, 1.2 MB"), "eyes8-03")
        self.assertEqual(item_name("Settings"), "Settings")


class SurveyValidationTests(unittest.TestCase):
    def test_a_survey_never_acts_and_takes_its_bound_from_the_request(self):
        program = validate_program(survey_program(targets={"open": {"label": "Open"}},
                                                  stop={"count_items": 40, "count_true": 3}),
                                   request=DOGS_GOAL)
        self.assertEqual(program["report"], {"kind": "names"})
        self.assertEqual(program["targets"], {})
        self.assertEqual((program["branches"]["true"], program["branches"]["false"]), ([], []))
        self.assertFalse(program["policy"]["irreversible"])
        self.assertEqual(program["stop"]["count_items"], 12)  # "which of the 12 images", not the helper's 40
        self.assertEqual(program["branches"]["unsure"], "stop")  # the request says nothing about unsure items
        count = validate_program(survey_program("count", stop={"count_true": 3}), request=ALBUM_GOAL)
        self.assertIsNone(count["stop"]["count_true"])
        self.assertIsNone(validate_program(loops_deck(), request="like everyone")["report"])

    def test_a_survey_with_actions_or_irreversibility_is_rejected(self):
        bad = [survey_program(branches={"true": [{"op": "TAP", "target": "@item"}], "false": []}),
               survey_program(policy={"irreversible": True}),
               survey_program(feed={"kind": "deck", "item": {"roles": ["Image"]}, "advance": {"by": "action"}}),
               survey_program(report={"kind": "summary"}),
               survey_program(report={"kind": "names", "extra": 1}),
               survey_program("labels", predicate={"question": "Which colour?",
                                                   "choices": ["red", "blue", "green"]}),  # no unsure choice
               survey_program("labels", predicate={"question": "Eye colour?", "decompose": "eye_colour",
                                                   "choices": ["red", "blue", "unsure"]})]
        for program in bad:
            with self.assertRaises(ProgramError):
                validate_program(program, request=DOGS_GOAL)

    def test_labels_answer_the_colour_itself_and_unsure_only_when_the_request_allows_it(self):
        program = validate_program(eyes_program(), request=EYES_GOAL)
        self.assertEqual(program["branches"]["unsure"], "skip")
        self.assertEqual(program["predicate"]["true_choices"], ["blue", "green", "grey", "hazel", "brown"])
        self.assertEqual(program["stop"]["count_items"], 8)
        strict = validate_program(eyes_program(), request="For each of the 8 images, report the eye colour.")
        self.assertEqual(strict["branches"]["unsure"], "stop")
        steps = loops.eye_colour_steps(program["predicate"])
        self.assertEqual(steps[-1].mapping, {"blue": "blue", "green": "green", "grey": "grey", "hazel": "hazel",
                                             "brown": "brown", "unclear": "unsure"})

    def test_a_grid_survey_compiles_without_a_pin_call_and_defers_when_off_screen(self):
        tools = FakeTools()
        grid = GridApp([files_label(n) for n in dogs()])
        program, why = compile_loop(DOGS_GOAL, grid.observe(), compiler=FakeCompiler(survey_program()), pinner=tools)
        self.assertEqual((program["report"]["kind"], why, tools.pins), ("names", None, 0))
        root = GridApp([files_label(n) for n in dogs()], at_root=True)
        with self.assertRaises(NoItemsOnScreen) as raised:
            compile_loop(DOGS_GOAL, root.observe(), compiler=FakeCompiler(survey_program()), pinner=tools)
        self.assertEqual(raised.exception.program["report"], {"kind": "names"})


def loops_deck():
    from mobile_agent.tests.test_loops import deck_program
    return deck_program()


class SurveyRunnerTests(unittest.TestCase):
    def test_a_files_grid_survey_records_every_file_name_and_never_taps(self):
        app = GridApp([files_label(n) for n in dogs()])
        answers = {name: "yes" if name in {"dogs12-02", "dogs12-07", "dogs12-11"} else "no" for name in dogs()}
        summary, runner = run_survey(app, answers, survey_program(), DOGS_GOAL)
        self.assertEqual(summary["reason"], "count_items")
        self.assertEqual(app.taps(), [])
        self.assertEqual([r["name"] for r in summary["survey"]["items"]], dogs())
        self.assertEqual(summary["counts"]["matched"], 3)
        answer = survey_answer(summary, runner.program, NAMES_SCHEMA, DOGS_GOAL)
        self.assertEqual(answer["data"], {"dog_files": ["dogs12-02", "dogs12-07", "dogs12-11"]})
        self.assertEqual(answer["cited"][0], ("/dog_files/0", files_label("dogs12-02"), "dogs12-02"))

    def test_a_longer_grid_is_scrolled_and_every_cell_is_judged_once(self):
        app = GridApp([files_label(n) for n in dogs()], rows=2)
        summary, runner = run_survey(app, {n: "no" for n in dogs()}, survey_program(), DOGS_GOAL)
        self.assertEqual([r["name"] for r in summary["survey"]["items"]], dogs())
        self.assertEqual(app.taps(), [])
        self.assertIn(("SWIPE_UP", None), app.actions)
        self.assertEqual(survey_answer(summary, runner.program, NAMES_SCHEMA, DOGS_GOAL)["data"], {"dog_files": []})

    def test_an_unjudgeable_item_stops_the_survey_and_the_answer_abstains(self):
        app = GridApp([files_label(n) for n in dogs()])
        answers = {name: "no" for name in dogs()}
        answers["dogs12-05"] = "unsure"
        summary, runner = run_survey(app, answers, survey_program(), DOGS_GOAL)
        self.assertEqual(summary["reason"], "uncertain")
        self.assertEqual(len(summary["survey"]["items"]), 5)
        answer = survey_answer(summary, runner.program, NAMES_SCHEMA, DOGS_GOAL)
        self.assertIsNone(answer["data"])
        self.assertIn("couldn't tell", answer["abstain"])

    def test_the_request_can_say_to_leave_unsure_items_out(self):
        request = DOGS_GOAL.replace("Report the file names.", "Report the file names; leave out any you're unsure about.")
        app = GridApp([files_label(n) for n in dogs()])
        answers = {name: "yes" for name in dogs()}
        answers["dogs12-05"] = "unsure"
        summary, runner = run_survey(app, answers, survey_program(unsure="skip",
                                                                  policy={"unsure_stated": True}), request)
        answer = survey_answer(summary, runner.program, NAMES_SCHEMA, request)
        self.assertEqual(answer["data"]["dog_files"], [n for n in dogs() if n != "dogs12-05"])

    def test_answers_that_would_be_guesses_abstain(self):
        program = validate_program(survey_program(), request=DOGS_GOAL)
        records = [{"name": n, "label": files_label(n), "identity": n, "outcome": "false", "answer": "no"}
                   for n in dogs()]

        def summary(reason, items, ambiguous=False):
            return {"reason": reason, "survey": {"kind": "names", "items": items, "ambiguous_identity": ambiguous}}
        for case in (summary("end_of_feed", records[:11]),          # fewer than the 12 the request states
                     summary("max_seconds", records),               # stopped before the end
                     summary("end_of_feed", records, True),         # identical labels across a scroll
                     summary("end_of_feed", [])):                   # saw nothing
            self.assertIsNone(survey_answer(case, program, NAMES_SCHEMA, DOGS_GOAL)["data"], case["reason"])
        self.assertIsNone(survey_answer(summary("end_of_feed", records), program, COUNT_SCHEMA, DOGS_GOAL)["data"])
        self.assertEqual(survey_answer(summary("end_of_feed", records), program, NAMES_SCHEMA, DOGS_GOAL)["data"],
                         {"dog_files": []})


class SurveyAgentTests(unittest.TestCase):
    def test_files_survey_answers_the_schema_with_observed_names(self):
        app = GridApp([files_label(n) for n in dogs()])
        answers = {name: "yes" if name in {"dogs12-01", "dogs12-12"} else "no" for name in dogs()}
        events = []
        result = Agent(app, no_model(), Helper(survey_program()), settle_seconds=0, loop_mode="dry_run",
                       emit=events.append, vision_judge=GridJudge(answers)).run(
            DOGS_GOAL, execute=True, output_schema=NAMES_SCHEMA, output_format="json")
        self.assertEqual(result["data"], {"dog_files": ["dogs12-01", "dogs12-12"]})
        self.assertEqual((result["status"], result["data_status"]), ("completed_unverified", "extracted"))
        self.assertTrue(result["schema_validated"])
        self.assertEqual(app.taps(), [])
        texts = {e["id"]: e["text"] for e in result["evidence"]["entries"]}
        self.assertEqual([texts[c["evidence_id"]] for c in result["citations"]],
                         [files_label("dogs12-01"), files_label("dogs12-12")])
        self.assertEqual(result["loop"]["survey"]["kind"], "names")
        self.assertEqual(len(result["loop"]["survey"]["items"]), 12)
        # Loop events carry digests and decisions, never a file name.
        self.assertNotIn("dogs12-", json.dumps([e for e in events if e["event"].startswith("loop_")]))

    def test_album_survey_counts_in_code_even_with_identical_photo_labels(self):
        labels = ["Photo, 23 September, 12:15 AM"] * 3 + [f"Photo, 23 September, 12:1{i} AM" for i in range(6, 9)]
        subjects = ["dog", "toy", "dog", "statue", "dog", "sign"]
        app = GridApp(labels, subjects, bundle=PHOTOS, folder="MobsterBench Dogs")
        program = survey_program("count", prefix="Photo", app=PHOTOS, stop={})
        result = Agent(app, no_model(), Helper(program), settle_seconds=0, loop_mode="dry_run",
                       vision_judge=GridJudge({"dog": "yes", "toy": "no", "statue": "no", "sign": "no"})).run(
            ALBUM_GOAL, execute=True, output_schema=COUNT_SCHEMA, output_format="json")
        self.assertEqual(result["data"], {"count": "3"})
        self.assertEqual(result["data_status"], "extracted")
        self.assertEqual(result["loop"]["reason"], "end_of_feed")  # two scrolls that moved nothing
        self.assertEqual(app.taps(), [])
        self.assertEqual(len(result["citations"]), 3)

    def test_identical_labels_across_scrolls_are_told_apart_by_their_thumbnails(self):
        # Photos names every imported photo alike; the grid shows two rows and scrolls
        # one at a time, so each screen brings back cells already judged.
        subjects = ["dog", "toy", "dog", "statue", "dog", "sign", "dog", "toy", "dog", "dog", "sign", "statue"]
        subjects = [f"{s}-{i}" for i, s in enumerate(subjects)]
        app = GridApp(["Photo, September 24, 2:51 AM"] * 12, subjects, bundle=PHOTOS, rows=2,
                      folder="MobsterBench Dogs")
        answers = {s: "yes" if s.startswith("dog") else "no" for s in subjects}

        def cell_crop(image, rect):
            cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
            for key, subject in json.loads(image.decode()).items():
                y, x = map(float, key.split(","))
                if x <= cx <= x + .32 and y <= cy <= y + .19:
                    return subject
            return None
        program = survey_program("count", prefix="Photo", app=PHOTOS, stop={})
        runner = LoopRunner(validate_program(program, request=ALBUM_GOAL), driver=app, request=ALBUM_GOAL,
                            judge=FakeJudge(answers), crop=cell_crop, settle_seconds=0, dry_run=True)
        with unittest.mock.patch.object(loops, "pixel_hash", 
                                     lambda subject: int(hashlib.sha256(subject.encode()).hexdigest(), 16)):
            summary = runner.run(app.observe())
        self.assertEqual(len(summary["survey"]["items"]), 12)
        self.assertFalse(summary["survey"]["ambiguous_identity"])
        self.assertEqual(survey_answer(summary, runner.program, COUNT_SCHEMA, ALBUM_GOAL)["data"], {"count": "6"})

    def test_a_thumbnail_hash_survives_a_small_shift_and_separates_different_pictures(self):
        from PIL import Image, ImageDraw
        pictures = []
        for seed in range(6):
            image = Image.new("L", (330, 330), 40 * seed)
            draw = ImageDraw.Draw(image)
            for k in range(8):
                x, y = (seed * 53 + k * 97) % 280, (seed * 31 + k * 61) % 280
                draw.ellipse((x, y, x + 50, y + 40), fill=255 - 30 * k)
            pictures.append(image)
        base = [loops.pixel_hash(p.crop((50, 50, 280, 280))) for p in pictures]
        for picture, digest in zip(pictures, base):
            moved = loops.pixel_hash(picture.crop((52, 51, 282, 281)))
            self.assertLessEqual(bin(moved ^ digest).count("1"), loops.PIXEL_MATCH_BITS)
        for a in range(len(base)):
            for b in range(a + 1, len(base)):
                self.assertGreater(bin(base[a] ^ base[b]).count("1"), loops.PIXEL_MATCH_BITS)

    def test_eye_colour_labels_report_unsure_where_the_judge_abstained(self):
        names = [f"eyes8-{i:02d}" for i in range(1, 9)]
        colours = ["blue", "unsure", "brown", "green", "unsure", "hazel", "grey", "brown"]
        app = GridApp([files_label(n) for n in names], folder="eyes8")
        result = Agent(app, no_model(), Helper(eyes_program()), settle_seconds=0, loop_mode="dry_run",
                       vision_judge=GridJudge(dict(zip(names, colours)))).run(
            EYES_GOAL, execute=True, output_schema=LABELS_SCHEMA, output_format="json")
        self.assertEqual(result["data"], {"eye_colours": [{"file": n, "label": c} for n, c in zip(names, colours)]})
        self.assertEqual((result["status"], result["data_status"]), ("completed_unverified", "extracted"))

    def test_an_item_that_cannot_be_judged_is_an_honest_abstention(self):
        app = GridApp([files_label(n) for n in dogs()])
        answers = {name: "no" for name in dogs()}
        answers["dogs12-03"] = "unsure"
        result = Agent(app, no_model(), Helper(survey_program()), settle_seconds=0, loop_mode="dry_run",
                       vision_judge=GridJudge(answers)).run(
            DOGS_GOAL, execute=True, output_schema=NAMES_SCHEMA, output_format="json")
        self.assertIsNone(result["data"])
        self.assertEqual((result["status"], result["data_status"]), ("completion_not_confirmed", "insufficient_evidence"))
        self.assertEqual(result["loop"]["reason"], "uncertain")
        self.assertIn("rather than a guess", result["reason"])

    def test_a_survey_without_a_vision_judge_goes_to_the_step_agent(self):
        app = GridApp([files_label(n) for n in dogs()])
        model = Mock(spec=["decide", "stable_completion"], stable_completion=True)
        model.decide.return_value = Decision("BLOCKED", None, .9, 0, .9, "t", 0, {}, StopGate.CONTINUE)
        events = []
        Agent(app, model, Helper(survey_program()), settle_seconds=0, loop_mode="dry_run", max_steps=1,
              emit=events.append).run(DOGS_GOAL, execute=True, output_schema=NAMES_SCHEMA, output_format="json")
        self.assertIn({"event": "loop_not_compiled", "reason": "vision_unavailable"}, events)
        self.assertIn("decision", [e["event"] for e in events])


    def test_a_survey_that_cannot_fit_the_requested_answer_goes_to_the_step_agent(self):
        names = [f"eyes8-{i:02d}" for i in range(1, 9)]
        app = GridApp([files_label(n) for n in names], folder="eyes8")
        model = Mock(spec=["decide", "stable_completion"], stable_completion=True)
        model.decide.return_value = Decision("BLOCKED", None, .9, 0, .9, "t", 0, {}, StopGate.CONTINUE)
        events = []
        schema = {"type": "object", "properties": {"eye_colour": {"type": "string"}}, "required": ["eye_colour"],
                  "additionalProperties": False}
        Agent(app, model, Helper(eyes_program()), settle_seconds=0, loop_mode="dry_run", max_steps=1,
              emit=events.append, vision_judge=GridJudge({})).run(
            "In Files, open eyes8 and view eyes8-06. What colour are the person's eyes? Look at each one if needed "
            "but do not favorite anything.", execute=True, output_schema=schema, output_format="json")
        self.assertIn({"event": "loop_not_compiled", "reason": "survey_schema_mismatch"}, events)
        self.assertNotIn("loop_item", [e["event"] for e in events])


class DeferredSurveyTests(unittest.TestCase):
    """The collection appears only after the step agent navigates to it (MobsterBench starts at the root)."""

    class HookedAgent(Agent):
        # Stands in for the one-line hook in Agent._step: offer each step's read to the deferred survey.
        def _step(self, state, observe_ready, step):
            snapshot = getattr(state, "loop_fallback_snapshot", None) or observe_ready(timeout=state.budget())
            surveyed = self._deferred_survey(state, snapshot)
            if surveyed is not None:
                return surveyed
            state.loop_fallback_snapshot = snapshot
            return super()._step(state, observe_ready, step)

    def test_a_survey_deferred_at_the_root_runs_once_the_folder_is_open(self):
        app = GridApp([files_label(n) for n in dogs()], at_root=True)
        model = Mock(spec=["decide", "stable_completion"], stable_completion=True)
        model.decide.return_value = Decision("TAP", "f", .97, .05, .02, "t", 0, {}, StopGate.CONTINUE,
                                             risk_tier="navigation", side_effect_risk=.01)
        helper = Helper(survey_program())
        events = []
        answers = {name: "yes" if name == "dogs12-04" else "no" for name in dogs()}
        result = self.HookedAgent(app, model, helper, settle_seconds=0, loop_mode="dry_run", emit=events.append,
                                  vision_judge=GridJudge(answers)).run(
            DOGS_GOAL, execute=True, output_schema=NAMES_SCHEMA, output_format="json")
        self.assertIn("loop_deferred", [e["event"] for e in events])
        self.assertEqual(helper.calls, 1)  # compiled once, on the folder's own screen (the route defers it)
        self.assertEqual(model.decide.call_count, 0)  # the route opens the folder the request names
        self.assertEqual(result["data"], {"dog_files": ["dogs12-04"]})
        self.assertEqual(app.taps(), [f"dogs12, folder, 12 items"])


if __name__ == "__main__":
    unittest.main()


class SurveyCoercionTests(unittest.TestCase):
    def test_parts_a_survey_never_uses_are_reset_not_trusted(self):
        from mobile_agent.loops import coerce_survey
        sloppy = survey_program(branches={"true": [{"op": "TAP", "target": "@item"}], "false": [], "unsure": "maybe"},
                                stop={"count_items": 12, "max_seconds": 240.5, "abstains_in_row": "3"},
                                policy={"stop_stated": True, "irreversible": True})
        sloppy["feed"]["advance"] = {"by": "action"}
        program = validate_program(coerce_survey(sloppy), request="Which of the 12 images show a dog?")
        self.assertEqual((program["branches"]["true"], program["branches"]["false"]), ([], []))
        self.assertEqual(program["feed"]["advance"], {"by": "swipe", "operation": "SWIPE_UP"})
        self.assertEqual(program["stop"]["max_seconds"], 240)
        self.assertFalse(program["policy"]["irreversible"])
        # The parts that matter are still the helper's, and still validated.
        self.assertEqual(program["feed"]["item"]["label_prefix"], "dogs12-")

    def test_an_action_program_is_left_alone(self):
        from mobile_agent.loops import coerce_survey
        raw = survey_program(report=None)
        self.assertIs(coerce_survey(raw), raw)


class CompileHardeningTests(unittest.TestCase):
    """Parts of a survey the helper got wrong live (diag-11, 24 Sep) are settled by code or asked again."""

    class Scripted:
        def __init__(self, *bodies):
            self.bodies, self.calls = list(bodies), 0

        def compile(self, request, snapshot, *, timeout=20):
            self.calls += 1
            return self.bodies.pop(0)

    def grid(self):
        return GridApp([files_label(n) for n in dogs()]).observe()

    def test_an_unfounded_refusal_is_asked_once_more(self):
        compiler = self.Scripted({"refused": True}, survey_program())
        program, why = compile_loop(DOGS_GOAL, self.grid(), compiler=compiler)
        self.assertIsNone(why)
        self.assertEqual((compiler.calls, program["report"]["kind"]), (2, "names"))

    def test_a_repeated_refusal_stands(self):
        compiler = self.Scripted({"refused": True}, {"refused": True})
        self.assertEqual(compile_loop(DOGS_GOAL, self.grid(), compiler=compiler), (None, "refused"))

    def test_a_refusal_the_request_supports_is_not_asked_again(self):
        compiler = self.Scripted({"refused": True})
        request = "In Photos, for each of the 12 photos say whether the person is Muslim."
        self.assertEqual(compile_loop(request, self.grid(), compiler=compiler), (None, "refused"))
        self.assertEqual(compiler.calls, 1)

    def test_extra_report_and_item_fields_are_dropped(self):
        program = survey_program(report={"kind": "names", "format": "list"},
                                 feed={"kind": "grid", "item": {"roles": ["Cell"], "label_prefix": "dogs12-",
                                                                "note": "cells"},
                                       "advance": {"by": "swipe", "operation": "SWIPE_UP"}})
        program["notes"] = "extra"
        compiled, why = compile_loop(DOGS_GOAL, self.grid(), compiler=self.Scripted(program))
        self.assertIsNone(why)
        self.assertEqual(compiled["report"], {"kind": "names"})

    def test_a_grid_on_screen_is_never_surveyed_as_a_deck(self):
        program = survey_program(feed={"kind": "deck", "item": {"roles": ["Cell"], "label_prefix": "dogs12-"},
                                       "advance": {"by": "swipe", "operation": "SWIPE_LEFT"}})
        compiled, _ = compile_loop(DOGS_GOAL, self.grid(), compiler=self.Scripted(program))
        self.assertEqual(compiled["feed"]["kind"], "grid")
        self.assertEqual(compiled["feed"]["advance"]["operation"], "SWIPE_UP")

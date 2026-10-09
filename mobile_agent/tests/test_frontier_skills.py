"""Skills in the loop (agent_hooks.Skill): an operation code performs for the model, asked between ``prepare``
and ``perform`` when it has a title, with every secret it returns masked from the model and the trace, and its
field painted over in later screenshots. A fake USE_CODE skill with the code 482913 stands in for Track 3's."""

import base64
import io
import json
import unittest

from mobile_agent.agent_hooks import MASK, Prepared, SkillResult
from mobile_agent.frontier import FrontierAgent, prompt_text, usable_skills
from mobile_agent.state import Element, Snapshot

CODE = "482913"
FIELD_RECT = (.1, .3, .8, .06)
BANK = "com.example.bank"


def screen(*elements):
    return Snapshot(list(elements), "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture",
                    bundle_id=BANK)


def field(value=""):
    return Element("3", "Verification code", "TextField", FIELD_RECT, editable=True, value=value,
                   actions=("TAP", "TYPE", "TYPE_SUBMIT"))


SIGN_IN = screen(Element("1", "Enter the code we sent", "StaticText", (.1, .2, .8, .04)), field())
FILLED = screen(Element("1", "Enter the code we sent", "StaticText", (.1, .2, .8, .04)), field(CODE),
                Element("4", f"Chase: Your code is {CODE}", "StaticText", (.05, .02, .9, .05)),
                Element("5", "Continue", "Button", (.1, .5, .8, .05)))


def picture():
    """The phone's screen: grey, with black and white stripes where the code field is."""
    from PIL import Image
    image = Image.new("RGB", (400, 800), (128, 128, 128))
    x, y, w, h = (int(FIELD_RECT[0] * 400), int(FIELD_RECT[1] * 800), int(FIELD_RECT[2] * 400),
                  int(FIELD_RECT[3] * 800))
    for column in range(x, x + w):
        for row in range(y, y + h):
            image.putpixel((column, row), (0, 0, 0) if (column // 4) % 2 else (255, 255, 255))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def stripes(url):
    """The standard deviation of the code field's pixels in a prompt's JPEG (stripes: ~127; painted over: ~0)."""
    from PIL import Image, ImageStat
    image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("L")
    width, height = image.size
    box = (int((FIELD_RECT[0] + .05) * width), int((FIELD_RECT[1] + .01) * height),
           int((FIELD_RECT[0] + FIELD_RECT[2] - .05) * width), int((FIELD_RECT[1] + FIELD_RECT[3] - .01) * height))
    return ImageStat.Stat(image.crop(box)).stddev[0]


class Phone:
    def __init__(self):
        self.screen, self.actions = SIGN_IN, []

    def observe(self, timeout=10):
        return self.screen

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, getattr(target, "label", target), text))
        if getattr(target, "label", None) == "Continue":
            self.screen = screen(Element("6", "Welcome back", "StaticText", (.1, .2, .8, .04)))

    def capture_preview(self, timeout=3):
        return picture()


class CodeSkill:
    op = "USE_CODE"
    prompt = "- USE_CODE target: type the verification code that was just sent into the code field (target)."
    needs_target = True

    def __init__(self, order, title="Use the code Chase sent 12 s ago to sign in to Bank?"):
        self.order, self.title = order, title

    def available(self, ctx):
        return True

    def prepare(self, ctx, act, snapshot):
        self.order.append(("prepare", act["element"].label, ctx.app_bundle))
        return Prepared(self.title, state=CODE)

    def perform(self, ctx, prepared, act, snapshot):
        self.order.append(("perform", act["element"].label))
        ctx.driver.screen = FILLED
        return SkillResult(f"Entered the 6-digit code from Chase.", changed=True, secrets=(prepared.state,),
                           secret_rects=(act["element"].rect,))


class Model:
    def __init__(self, *chunks, answer=f"Signed in with the code {CODE}."):
        self.chunks, self.answer, self.prompts, self.images, self.schemas, self.systems = list(chunks), answer, [], [], [], []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

    def complete(self, messages, schema, timeout=60, **kwargs):
        if "items" in schema["properties"]:
            return {"items": []}, {}
        self.usage["calls"] += 1
        self.prompts.append(prompt_text(messages))
        self.systems.append(messages[0]["content"])
        self.schemas.append(schema)
        self.images.append(next((p["image_url"]["url"] for p in messages[1]["content"] if p.get("type") == "image_url"),
                                None))
        rows = self.prompts[-1].split("Screen elements:\n", 1)[1].splitlines()
        chunk = self.chunks.pop(0) if len(self.chunks) > 1 else self.chunks[0]
        actions = [{"op": op, "target": next((r.split()[0] for r in rows if label and f'"{label}"' in r), label),
                    "text": text} for op, label, text in chunk]
        return {"thought": "", "plan": None, "notes_add": [f"code field filled"], "checklist_updates": [],
                "actions": actions, "answer": self.answer if chunk[-1][0] == "DONE" else None}, {}


def run(order, approve=None, title="Use the code Chase sent 12 s ago to sign in to Bank?"):
    phone, events = Phone(), []
    model = Model([("USE_CODE", "Verification code", None)], [("TAP", "Continue", None)], [("DONE", None, None)])
    agent = FrontierAgent(phone, model, apps={BANK: "Bank"}, emit=events.append, screenshots=True, settle_seconds=0,
                          contract=False, approve=approve, skills=(CodeSkill(order, title),))
    result = agent.run("Sign in to Bank with the code they texted me.")
    return phone, model, result, events


class SkillTests(unittest.TestCase):
    def test_the_skill_is_offered_and_its_secret_never_reaches_the_model_or_the_trace(self):
        order = []
        phone, model, result, events = run(order, approve=lambda request: order.append(("approve", request)) or "approved")
        self.assertIn("- USE_CODE target: type the verification code", model.systems[0])
        self.assertIn("USE_CODE", model.schemas[0]["properties"]["actions"]["items"]["properties"]["op"]["enum"])
        self.assertEqual([o[0] for o in order], ["prepare", "approve", "perform"])
        self.assertTrue(len(model.prompts) >= 2)
        for prompt in model.prompts:
            self.assertNotIn(CODE, prompt)
        self.assertIn(MASK, model.prompts[1])  # the field and the banner, masked
        self.assertIn("Entered the 6-digit code from Chase.", model.prompts[1])
        self.assertNotIn(CODE, result["answer"])
        self.assertNotIn(CODE, json.dumps(result, default=str))
        self.assertNotIn(CODE, json.dumps(events, default=str))
        self.assertNotIn(CODE, json.dumps(order[1][1]))  # the approval's payload
        self.assertEqual(order[1][1]["kind"], "use_code")
        self.assertIn(("TAP", "Continue", None), phone.actions)
        kinds = [e["event"] for e in events]
        self.assertLess(kinds.index("skill_started"), kinds.index("skill_finished"))
        self.assertEqual(next(e for e in events if e["event"] == "skill_finished")["ok"], True)

    def test_the_code_field_is_painted_over_in_later_screenshots(self):
        _, model, _, _ = run([], approve=lambda request: "approved")
        self.assertGreater(stripes(model.images[0]), 60)   # before: the field is just a field
        self.assertLess(stripes(model.images[1]), 8)       # after: nothing of what is there can be read

    def test_a_denial_means_no_perform(self):
        order = []
        phone, model, result, _ = run(order, approve=lambda request: "denied")
        self.assertEqual([o[0] for o in order], ["prepare"])
        self.assertEqual(result["status"], "approval_denied")
        self.assertEqual(phone.screen, SIGN_IN)

    def test_a_timeout_or_a_stop_is_no_perform_either(self):
        for answer, status in (("timeout", "approval_timeout"), ("stopped", "stopped")):
            order = []
            _, _, result, _ = run(order, approve=lambda request, a=answer: a)
            self.assertEqual(([o[0] for o in order], result["status"]), (["prepare"], status))

    def test_a_run_that_cannot_ask_never_performs_a_skill_that_asks(self):
        order = []
        phone, model, result, events = run(order, approve=None)
        self.assertEqual([o[0] for o in order], ["prepare"])
        self.assertEqual(phone.screen, SIGN_IN)
        self.assertIn("needs the user's OK", model.prompts[1])

    def test_a_skill_without_a_title_needs_no_approval(self):
        order, asked = [], []
        run(order, approve=lambda request: asked.append(request) or "approved", title=None)
        self.assertEqual([o[0] for o in order], ["prepare", "perform"])
        self.assertEqual(asked, [])

    def test_a_skill_whose_op_clashes_or_repeats_is_not_offered(self):
        class Tap(CodeSkill):
            op = "TAP"
        self.assertEqual(usable_skills([Tap([]), CodeSkill([]), CodeSkill([])])[0].op, "USE_CODE")
        self.assertEqual(len(usable_skills([Tap([]), CodeSkill([]), CodeSkill([])])), 1)

    def test_a_skill_error_shows_no_text_of_it(self):
        class Leaky(CodeSkill):
            def prepare(self, ctx, act, snapshot):
                raise RuntimeError(f"no code field for {CODE}")
        phone, events = Phone(), []
        model = Model([("USE_CODE", "Verification code", None)], [("DONE", None, None)], answer="Couldn't sign in.")
        FrontierAgent(phone, model, apps={BANK: "Bank"}, emit=events.append, screenshots=False, settle_seconds=0,
                      contract=False, skills=(Leaky([]),)).run("Sign in.")
        self.assertNotIn(CODE, "".join(model.prompts))
        self.assertNotIn(CODE, json.dumps(events, default=str))
        self.assertIn("USE_CODE could not be done (RuntimeError).", model.prompts[1])

    def test_no_event_carries_the_secret_when_a_later_label_or_error_holds_it(self):
        # Labels and errors reach the trace as they are ("frontier_action" named the tapped row, the failure its
        # error): every event goes out masked (review of #68).
        accepted = Element("7", f"Code {CODE} accepted", "StaticText", (.1, .6, .8, .04))

        class After(Phone):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                self.actions.append((operation, getattr(target, "label", target), text))
                if getattr(target, "label", "") == "Continue":
                    raise ValueError(f"the field still holds {CODE}")

        phone, events = After(), []
        model = Model([("USE_CODE", "Verification code", None)], [("TAP", accepted.label, None)],
                      [("TAP", "Continue", None)], [("DONE", None, None)], answer="Signed in.")
        skill = CodeSkill([])
        perform = skill.perform

        def perform_and_confirm(ctx, prepared, act, snapshot):
            out = perform(ctx, prepared, act, snapshot)
            ctx.driver.screen = screen(*FILLED.elements, accepted)
            return out
        skill.perform = perform_and_confirm
        FrontierAgent(phone, model, apps={BANK: "Bank"}, emit=events.append, screenshots=False, settle_seconds=0,
                      contract=False, approve=lambda request: "approved", skills=(skill,)).run("Sign in.")
        self.assertIn(("TAP", accepted.label, None), phone.actions)
        self.assertTrue([e for e in events if e["event"] == "frontier_failed"])
        self.assertNotIn(CODE, json.dumps(events, default=str, ensure_ascii=False))

    def test_an_element_whose_text_holds_the_secret_is_painted_over_too(self):
        from PIL import Image
        banner = (.05, .62, .9, .05)

        class Banner(Phone):
            def capture_preview(self, timeout=3):
                image = Image.new("RGB", (400, 800), (128, 128, 128))
                x, y, w, h = (int(banner[0] * 400), int(banner[1] * 800), int(banner[2] * 400), int(banner[3] * 800))
                for column in range(x, x + w):
                    for row in range(y, y + h):
                        image.putpixel((column, row), (0, 0, 0) if (column // 4) % 2 else (255, 255, 255))
                out = io.BytesIO()
                image.save(out, "PNG")
                return out.getvalue()

        agent = FrontierAgent(Banner(), None, apps={BANK: "Bank"}, skills=())
        agent._secrets = {CODE}
        shown = screen(Element("7", f"Code {CODE} accepted", "StaticText", banner))
        url = agent._screenshot(shown)["image_url"]["url"]
        from PIL import ImageStat
        image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("L")
        width, height = image.size
        box = (int((banner[0] + .05) * width), int((banner[1] + .01) * height),
               int((banner[0] + banner[2] - .05) * width), int((banner[1] + banner[3] - .01) * height))
        self.assertLess(ImageStat.Stat(image.crop(box)).stddev[0], 8)


if __name__ == "__main__":
    unittest.main()


def golden_runs():
    """Scripted runs over the frontier's existing fixtures (tests/test_frontier.py, test_contract.py): what each
    did, as a golden record. Self-contained, so tests/fixtures/frontier/golden_runs.json was recorded by running
    this same function on origin/main (67bdb85) before any of this sprint's changes."""
    import inspect
    from mobile_agent.frontier import FrontierAgent
    from mobile_agent.state import Element
    from mobile_agent.tests import test_contract as C
    from mobile_agent.tests import test_frontier as F
    extra = ({"skills": (), "guard": None, "early_decision": False}
             if "skills" in inspect.signature(FrontierAgent).parameters else {})

    def record(driver, result, events):
        return {"actions": [list(a) for a in driver.actions],
                "result": {k: result.get(k) for k in ("status", "answer", "steps", "actions")},
                "events": [e["event"] for e in events if e["event"] != "frontier_prompt"],
                "refused": [e.get("label") for e in events if e["event"] == "frontier_refused"]}

    def run(screens, steps, request, driver_class=F.Driver, script=None, **kwargs):
        driver, events = driver_class(screens), []
        result = FrontierAgent(driver, script or F.Script(steps), emit=events.append, screenshots=False,
                               settle_seconds=0, **kwargs, **extra).run(request)
        return record(driver, result, events)

    screen = F.screen
    runs = {}
    row = screen(Element("1", "Delete", "Button", (.1, .1, .2, .05)), Element("2", "Archive", "Button", (.4, .1, .2, .05)))
    runs["unrequested_act"] = run([row] * 3, [("TAP", "Delete", None), ("TAP", "Archive", None), ("DONE", None, None),
                                              ("DONE", None, None)], F.ARCHIVE)
    archived = screen(Element("1", "Archived", "StaticText", (.4, .1, .2, .05)))
    runs["done_after_edit"] = run([screen(Element("1", "Archive", "Button", (.4, .1, .2, .05))), archived, archived],
                                  [[("TAP", "Archive", None), ("DONE", None, None)], ("DONE", None, None)], F.ARCHIVE)
    menu = screen(Element("1", "Pin Note", "Button", (.36, .08, .62, .05)))
    runs["dismiss_points"] = run([menu] * 5, [("DISMISS", None, None)] * 3 + [("DONE", None, None)],
                                 "Update my reading list note.")
    label = Element("1", "Label", "TextField", (.1, .3, .8, .05), value="Wrap up", editable=True,
                    actions=("TAP", "TYPE", "TYPE_SUBMIT"))
    top = Element("2", "Menu", "Button", (0, 0, 1, .1))
    runs["set_text"] = run([screen(label, top)] * 4, [("SET_TEXT", "Label", "Saturday Run"), ("DISMISS", None, None),
                                                      ("DONE", None, None)], "Set the alarm label to Saturday Run.")
    form = screen(Element("1", "Title", "TextField", (.1, .2, .8, .05), editable=True,
                          actions=("TAP", "TYPE", "TYPE_SUBMIT")), Element("2", "Save", "Button", (.7, .05, .2, .05)))
    runs["chunk_missing_target"] = run([form] * 4, [[("TYPE", "Title", "Q2 plan"), ("TAP", "Save", None),
                                                     ("TAP", "Share", None)], ("DONE", None, None), ("DONE", None, None)],
                                       "Add a doc titled Q2 plan.")
    loop = screen(Element("1", "Recent", "Button", (.1, .1, .2, .05)), Element("2", "Drag", "Button", (.5, .1, .2, .05)))
    runs["loop"] = run([loop] * 20, [[("TAP", "Recent", None), ("TAP", "Drag", None)]] * 3
                       + [("TAP", "Recent", None), ("DONE", None, None)], "Create a doc and add it.")

    def rows(*labels):
        return screen(*[Element(str(i), lab, "Cell", (0, .2 + .1 * i, 1, .08)) for i, lab in enumerate(labels)])
    runs["read_list"] = run([rows("Receipt 1", "Receipt 2"), rows("Receipt 2", "Receipt 3"),
                             rows("Receipt 3", "Receipt 4"), rows("Receipt 3", "Receipt 4")],
                            [("READ_LIST", None, None), ("DONE", None, None), ("DONE", None, None)],
                            "Total all my receipts.")
    top_, lower = screen(Element("1", "Alpha", "Cell", (0, .2, 1, .08))), screen(
        Element("1", "Omega Trattoria", "Cell", (0, .5, 1, .08)))
    opened = screen(Element("1", "Book a table", "Button", (0, .5, 1, .08)))
    runs["scroll_to"] = run([top_, lower, opened, opened], [[("SCROLL_TO", None, "omega"),
                                                             ("TAP", "Omega Trattoria", None)],
                                                            ("DONE", None, None), ("DONE", None, None)],
                            "Open Omega Trattoria.")
    keys = [Element(str(i), k, "Button", (.1 * i, .8, .08, .05), locator=f"/App/Button[{i}]")
            for i, k in enumerate(["1", "2", "3", "5", "0", "."], 1)]
    shifted = [Element(str(i), e.label, "Button", e.rect, locator=f"/App/Button[{i + 1}]") for i, e in enumerate(keys, 1)]
    afters = [screen(Element("0", amount, "StaticText", (.1, .2, .8, .05)), *shifted)
              for amount in ["$3", "$32", "$32.", "$32.5", "$32.50"]]
    runs["keypad_chain"] = run([screen(*keys)] + afters, [[("TAP", "3", None), ("TAP", "2", None), ("TAP", ".", None),
                                                           ("TAP", "5", None), ("TAP", "0", None)],
                                                          ("DONE", None, None)], "Send $32.50 to Maya on SplitPay.")
    feed = C.screen(Element("1", "Send", "Button", (.8, .3, .1, .04)),
                    Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value="Message"))
    sent = C.screen(Element("3", "Dinner is at 7", "StaticText", (.1, .5, .6, .04)),
                    Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value="Message"))
    steps = [[("TAP", "Send", None)], [("DONE", None, None)], [("TYPE", "Message", "Dinner is at 7"),
                                                               ("TAP", "Send", None)], [("DONE", None, None)]]
    driver, events = C.Driver([feed, feed, sent, sent]), []
    result = FrontierAgent(driver, C.Script(steps, C.AgentLoopTests.ITEMS), apps={"com.example.chat": "QuickChat"},
                           emit=events.append, screenshots=False, settle_seconds=0, **extra).run(C.AgentLoopTests.REQ)
    runs["contract_send"] = record(driver, result, events)
    return runs


class GoldenTests(unittest.TestCase):
    def test_no_guard_and_no_skills_reproduce_the_loop_as_it_was(self):
        """Acceptance 5: ``skills=()`` and ``guard=None`` behave as origin/main did, over the existing fixtures."""
        from pathlib import Path
        golden = json.loads((Path(__file__).parent / "fixtures" / "frontier" / "golden_runs.json").read_text())
        now = json.loads(json.dumps(golden_runs()))
        self.assertEqual(sorted(now), sorted(golden))
        for name in golden:
            with self.subTest(run=name):
                self.assertEqual(now[name], golden[name])

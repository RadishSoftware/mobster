import unittest
from unittest.mock import patch

from mobile_agent.graph import draft
from mobile_agent.graph.schema import Context, validate
from mobile_agent.tests.test_graph_run import GraphBase
from mobile_agent.workflows import starters

PACKS = {"instagram": {"name": "Instagram", "readers": {"dms": [{"key": "from", "label": "From", "type": "text"}]}}}
CONTEXT = Context(packs=PACKS)


def step(ident, kind, after=(), **fields):
    out = {"id": ident, "kind": kind, "after": list(after), "text": None, "app": None, "reader": None,
           "seconds": None, "to": None, "cases": None, "over": None, "times": None, "inside": None}
    out.update(fields)
    return out


PLAN = {"name": "Answer DMs", "schedule": "0 9 * * *", "steps": [
    step("read", "read", app="instagram", reader="dms"),
    step("each", "loop", ["read"], over="items"),
    step("reply", "task", inside="each", text="Reply to the DM in the input with a thank-you", app="instagram"),
    step("check", "branch", ["each"], cases=[{"id": "many", "label": "Many", "when": "$input.count > 3"}]),
    step("ok", "approval", ["check:many"], text="Post a summary?"),
    step("tell", "send", ["ok:approved"], to="notification"),
    step("quiet", "send", ["check:else"], to="clipboard")]}


class Drafter:
    model = "claude-haiku-5-5"

    def __init__(self, *plans):
        self.plans, self.prompts = list(plans), []

    def complete(self, messages, schema, timeout=None):
        self.prompts.append(messages[-1]["content"][0]["text"])
        return self.plans.pop(0), {}


class CompileTests(unittest.TestCase):
    def test_a_plan_becomes_a_graph_that_passes_the_save_check(self):
        graph = validate(draft.compile_plan(PLAN, zone="UTC"), CONTEXT)
        types = {n["id"]: n["type"] for n in graph["nodes"]}
        self.assertEqual(types["schedule"], "trigger.schedule")
        edges = {(e["source"], e["sourcePort"], e["target"], e["targetPort"]) for e in graph["edges"]}
        self.assertTrue({("read", "rows", "each", "items"), ("each", "item", "reply", "in"),
                         ("check", "many", "ok", "in"), ("ok", "approved", "tell", "in"),
                         ("check", "else", "quiet", "in"), ("start", "out", "read", "in")} <= edges)
        self.assertEqual(next(n for n in graph["nodes"] if n["id"] == "reply")["parent"], "each")
        tops = [n["position"]["y"] for n in graph["nodes"] if n["id"] in ("read", "each", "check")]
        self.assertEqual(tops, sorted(tops))                       # rows by how far from a trigger

    def test_a_counted_loop_compiles_with_its_rounds_held_to_the_limit(self):
        # ICP-10: compile_plan read schema.MAX_TIMES after it moved to graph/catalog.py, so any plan with a counted loop
        # failed the request ("Mobster couldn't draft that") and the same words worked when the plan had none.
        plan = {"name": "Three rounds", "schedule": None, "steps": [
            step("each", "loop", over="times", times=500), step("say", "task", inside="each", text="Say hi")]}
        loop = next(n for n in draft.compile_plan(plan)["nodes"] if n["id"] == "each")
        self.assertEqual(loop["config"], {"over": "times", "times": 50})

    def test_nodes_side_by_side_never_overlap_on_the_canvas(self):
        # DR-66: Manual and Schedule side by side overlapped by 80 px, 240 apart. The canvas draws a trigger 224 wide
        # (dashboard/src/lib/workflow-graph.ts TRIGGER_W) and a step 320 (geometry.ts NODE_W): 32 clear of each other.
        graph = draft.compile_plan(PLAN, zone="UTC")
        top = [n for n in graph["nodes"] if not n.get("parent")]
        triggers = sorted(n["position"]["x"] for n in top if n["type"].startswith("trigger."))
        self.assertGreaterEqual(len(triggers), 2)
        self.assertTrue(all(b - a >= 224 + 32 for a, b in zip(triggers, triggers[1:])), triggers)
        rows = {}
        for n in top:
            if not n["type"].startswith("trigger."):
                rows.setdefault(n["position"]["y"], []).append(n["position"]["x"])
        gaps = [b - a for xs in rows.values() for a, b in zip(sorted(xs), sorted(xs)[1:])]
        self.assertTrue(all(gap >= 320 + 32 for gap in gaps), rows)

    def test_the_drafter_repairs_what_the_check_found(self):
        broken = {**PLAN,
                  "steps": [step("each", "loop", over="items"), step("say", "task", inside="each", text="Say hi")]}
        drafter = Drafter(broken, PLAN)
        made = draft.draft("Every morning answer my Instagram DMs", CONTEXT, client=drafter)
        self.assertEqual((made["drafter"], made["errors"], made["draft"]["name"]), ("claude-haiku-5-5", [],
                                                                                    "Answer DMs"))
        self.assertEqual(len(drafter.prompts), 2)
        self.assertIn("Join a list to the loop's items", drafter.prompts[1])

    def test_problems_it_couldnt_fix_are_listed_for_the_canvas(self):
        broken = {"name": "x", "schedule": None, "steps": [step("each", "loop", over="items")]}
        made = draft.draft("Loop", CONTEXT, client=Drafter(broken, broken, broken))
        self.assertEqual(made["errors"][0]["node"], "each")

    def test_a_task_in_instagram_starts_in_instagram_not_safari(self):
        # ICP-10: the drafter's apps were the catalog's 30, so a plan for Instagram went through Safari.
        drafter = Drafter(PLAN)
        made = draft.draft("Every morning answer my Instagram DMs", CONTEXT, client=drafter)
        self.assertIn("com.burbn.instagram (Instagram)", drafter.prompts[0])
        reply = next(n for n in made["draft"]["graph"]["nodes"] if n["id"] == "reply")
        self.assertEqual(reply["config"]["appId"], "com.burbn.instagram")   # the plan said the pack's key
        self.assertIsNone(draft._task_app("an-app-nobody-has"))

    def test_repairs_stop_inside_the_page_s_minute_and_the_best_plan_is_the_answer(self):
        # ICP-10: three 40-second tries ran past the page's 60 seconds, so a slow repair drafted nothing.
        broken = {"name": "x", "schedule": None, "steps": [step("each", "loop", over="items")]}
        clock, timeouts = [0.0], []

        class Slow(Drafter):
            def complete(self, messages, schema, timeout=None):
                timeouts.append(timeout)
                clock[0] += 30
                return super().complete(messages, schema, timeout)
        with patch.object(draft.time, "monotonic", lambda: clock[0]):
            made = draft.draft("Loop", CONTEXT, client=Slow(broken, broken, broken))
        self.assertEqual(timeouts, [40, 15])
        self.assertEqual((made["drafter"], made["errors"][0]["node"]), ("claude-haiku-5-5", "each"))

    def test_with_no_model_each_sentence_is_a_step(self):
        made = draft.draft("Open Messages and read Sam's newest text. Then reply that I'm on my way", CONTEXT)
        self.assertEqual((made["drafter"], made["errors"]), ("local", []))
        tasks = [n["config"]["text"] for n in made["draft"]["graph"]["nodes"] if n["type"] == "phone.task"]
        self.assertEqual(tasks, ["Open Messages and read Sam's newest text", "Reply that I'm on my way"])

    def test_with_no_model_a_schedule_phrase_is_the_trigger_and_a_list_is_steps(self):
        made = draft.draft("Every weekday at 9am, open Settings on my iPhone, read the iOS version, and tell me if "
                           "it changed since yesterday", CONTEXT)
        self.assertEqual((made["drafter"], made["errors"]), ("local", []))
        nodes = made["draft"]["graph"]["nodes"]
        self.assertEqual(next(n for n in nodes if n["type"] == "trigger.schedule")["config"]["cron"], "0 9 * * 1-5")
        tasks = [n["config"] for n in nodes if n["type"] == "phone.task"]
        self.assertEqual([t["text"] for t in tasks], ["Open Settings on my iPhone", "Read the iOS version",
                                                      "Tell me if it changed since yesterday"])
        self.assertEqual([t["appId"] for t in tasks], ["settings", None, None])

    def test_a_change_to_a_graph_is_a_patch_and_kept_steps_keep_their_places(self):
        current = validate(draft.compile_plan({"name": "x", "schedule": None, "steps": [
            step("a", "task", text="Read the news")]}), CONTEXT)
        current["nodes"][1]["position"] = {"x": 500, "y": 40}
        changed = {"name": "x", "schedule": None, "steps": [step("a", "task", text="Read the news"),
                                                            step("b", "send", ["a"], to="clipboard")]}
        made = draft.draft("Then copy it", CONTEXT, client=Drafter(changed), current=current)
        self.assertEqual([(p["op"], p.get("node") or p.get("edge")) for p in made["patch"]],
                         [("add", "b"), ("add", "e2")])
        kept = next(n for n in made["draft"]["graph"]["nodes"] if n["id"] == "a")
        self.assertEqual(kept["position"], {"x": 500, "y": 40})


class RouteTests(GraphBase):
    def test_post_draft_takes_words_and_saves_nothing(self):
        with patch.object(starters, "draft_client", lambda env=None: None):
            made = self.workflows.draft({"words": "Check the weather, then text Kate the forecast"})
        self.assertEqual(made["drafter"], "local")
        self.assertEqual(self.workflows.list(), [])
        with self.assertRaises(ValueError):
            self.workflows.draft({"words": "x", "steps": []})
        made = self.workflows.draft({"words": "Then copy it", "graph": made["draft"]["graph"]},
                                    client_factory=lambda: Drafter({"name": "x", "schedule": None, "steps": [
                                        step("s1", "task", text="Check the weather")]}))
        self.assertEqual(made["drafter"], "claude-haiku-5-5")
        self.assertIn({"op": "remove", "node": "s2"}, [{k: p[k] for k in ("op", "node")} for p in made["patch"]
                                                      if "node" in p])

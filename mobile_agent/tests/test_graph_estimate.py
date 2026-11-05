import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mobile_agent import server
from mobile_agent.graph import estimate as graph_estimate
from mobile_agent.learned import programs
from mobile_agent.replay_core.path import new_path
from mobile_agent.replay_core.store import env_of
from mobile_agent.tests.test_graph_run import GraphBase, edge, node, task
from mobile_agent.tests.test_workflow_graph import FREE, GraphCase


def runtime(directory):
    stub = SimpleNamespace(
        config=SimpleNamespace(state_db=str(Path(directory) / "state.sqlite3")),
        engines_status=lambda: {"smart": {"available": True, "reason": None, "model": "claude-sonnet-5-5"}},
        apps=lambda: [{"name": "Settings", "installed": True}])
    stub.free_kind = lambda goal: server.Runtime.free_kind(stub, goal)
    return stub


def teach(directory, request, status="trusted", **step):
    store = programs.Programs(programs.programs_dir(directory))
    steps = [{"op": "TAP", "app": "com.apple.Preferences", "target": {"label": "General", "role": "Cell", "id": None,
              "frame": None}, "screen": {"hash": "h"}, "text": None, "param": None, "commit": None, "point": None,
              "live": False, **step}]
    path = new_path(steps, kind="program", identity=store.identity(None, request, None), env=env_of(None, None, None),
                    source_run=None)
    path.update(contract=None, blanks=[], taught={"at": 1.0, "points": 0, "asks": 0}, status=status, misses=0,
                used_at=1.0)
    store.cache.save(path)


SMART = {"lowUsd": 0.02, "highUsd": 0.08, "model": "claude-sonnet-5-5"}


class TaughtEstimateTests(GraphBase):

    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(prefix="mobster-taught-est-"))
        self.phone.config = SimpleNamespace(port=8765, state_db=str(self.dir / "state.sqlite3"))
        self.phone.engine_estimates = lambda goal, schema=None: {"smart": dict(SMART)}

    def priced(self, goal):
        workflow = self.save_graph([task("step", goal)], [edge("e1", "start", "step")])
        return self.workflows.estimate(workflow["id"])["nodes"]["step"]

    def test_a_trusted_taught_step_estimates_zero_and_says_it_replays(self):
        teach(self.dir, "In Settings, open General, then About")
        found = self.priced("In Settings, open General, then About")
        self.assertEqual((found["lowUsd"], found["highUsd"], found["modelCalls"], found["basis"], found["decides"]),
                         (0.0, 0.0, 0, "replay", "taught"))

    def test_a_first_replay_a_chosen_step_other_words_and_agent_mode_keep_the_models_price(self):
        teach(self.dir, "In Settings, open General, then About", status="provisional")
        self.assertEqual(self.priced("In Settings, open General, then About")["basis"], "estimate")
        teach(self.dir, "Open Battery in Settings", decide=True)
        self.assertEqual(self.priced("Open Battery in Settings")["highUsd"], 0.08)
        teach(self.dir, "Open Display in Settings")
        self.assertEqual(self.priced("Open Display and Brightness in Settings")["basis"], "estimate")
        workflow = self.save_graph([task("step", "Open Display in Settings", mode="agent")],
                                   [edge("e1", "start", "step")])
        self.assertEqual(self.workflows.estimate(workflow["id"])["nodes"]["step"]["basis"], "estimate")


class EstimateTests(GraphBase):
    def setUp(self):
        super().setUp()
        self.asked = []
        self.phone.engine_estimates = lambda goal, schema=None: self.asked.append(goal) or {"smart": dict(SMART)}

    def test_before_a_run_each_step_has_the_agents_estimate_and_loops_multiply(self):
        workflow = self.save_graph(
            [task("read", "Read the DMs"), node("pick", "transform", {"fields": {"people": "{= ['Sam', 'Ana'] }"}}),
             node("each", "loop", {"over": "times", "times": 3}), task("reply", "Reply to one", parent="each"),
             node("items", "loop", {"over": "items"}),
             task("like", "Like one", parent="items"), task("again", "Replay it", mode="replay")],
            [edge("e1", "start", "read"), edge("e2", "read", "pick"), edge("e3", "pick", "each"),
             edge("e4", "pick", "items", "out", "items", map="$input.people"), edge("e5", "items", "again")])
        found = self.workflows.estimate(workflow["id"])
        nodes, total = found["nodes"], found["total"]
        self.assertEqual({k: nodes["read"][k] for k in ("lowUsd", "highUsd", "modelCalls", "basis", "decides")},
                         {"lowUsd": 0.02, "highUsd": 0.08, "modelCalls": None, "basis": "estimate",
                          "decides": "model"})
        self.assertEqual((nodes["pick"]["lowUsd"], nodes["pick"]["modelCalls"], nodes["pick"]["basis"]),
                         (0.0, 0, "none"))
        self.assertEqual((nodes["each"]["lowUsd"], nodes["each"]["highUsd"], nodes["each"]["rounds"]),
                         (0.06, 0.24, 3))
        self.assertTrue(nodes["reply"]["perRound"])
        self.assertEqual((nodes["items"]["highUsd"], nodes["items"]["rounds"], nodes["items"]["partial"]),
                         (0.08, None, True))
        self.assertEqual((nodes["again"]["highUsd"], nodes["again"]["modelCalls"], nodes["again"]["decides"]),
                         (0.0, 0, "replays"))
        self.assertEqual((total["lowUsd"], total["highUsd"], total["modelCalls"], total["partial"],
                          total["overLimit"]), (0.1, 0.4, None, True, False))
        self.assertEqual(self.asked.count("Reply to one"), 1)

    def test_after_a_run_a_step_is_estimated_from_its_own_runs_with_its_model_calls(self):
        def script(goal, fields):
            return "hold"
        self.phone.script = script
        workflow = self.save_graph([task("read", "Hold and read"), task("look", "Hold and look")],
                                   [edge("e1", "start", "read"), edge("e2", "read", "look")])
        self.workflows.start(workflow["id"], "k1")
        run = self.run_of("Hold and read")
        run.events = [{"event": "inference_started"}, {"event": "inference_finished"},
                      {"event": "inference_started"}, {"event": "inference_finished"}]
        self.phone.finish(run, "completed", {"answer": "3 DMs"}, 0.03)
        run = self.run_of("Hold and look")
        run.events = [{"event": "replay_started", "steps": 4}, {"event": "replay_finished", "status": "completed"}]
        self.phone.finish(run, "completed", {"answer": "Seen"}, 0.0)
        record = self.wait(workflow)
        self.assertEqual((record["nodes"]["read"]["modelCalls"], record["nodes"]["look"]["modelCalls"],
                          record["modelCalls"]), (2, 0, 2))
        self.assertTrue(record["nodes"]["look"]["replayed"])
        found = self.workflows.estimate(workflow["id"])["nodes"]
        self.assertEqual({k: found["read"][k] for k in ("lowUsd", "highUsd", "modelCalls", "basis", "runs", "decides")},
                         {"lowUsd": 0.03, "highUsd": 0.03, "modelCalls": 2, "basis": "history", "runs": 1,
                          "decides": "model"})
        self.assertEqual((found["look"]["highUsd"], found["look"]["decides"]), (0.0, "replays"))
        self.assertEqual(self.asked, [])                           # nothing to guess once the steps have run

    def test_a_graph_that_would_pass_its_limit_says_so_and_a_missing_estimate_is_unknown(self):
        workflow = self.save_graph([node("each", "loop", {"over": "times", "times": 50}),
                                    task("reply", "Reply to one", parent="each")],
                                   [edge("e1", "start", "each")], limits={"maxSpendUsd": 1.0, "maxMinutes": 60})
        self.assertTrue(self.workflows.estimate(workflow["id"])["total"]["overLimit"])
        self.phone.engine_estimates = None
        total = self.workflows.estimate(workflow["id"])["total"]
        self.assertEqual((total["highUsd"], total["overLimit"]), (None, False))

    def test_ask_the_model_is_one_call_priced_from_its_instruction_and_its_most_input(self):
        found = graph_estimate.estimate(
            {"nodes": [{"id": "ask", "type": "ai.ask", "config": {"instruction": "Sum it up"}}]},
            history={}, agent=lambda *_: None, rounds=lambda _: None,
            ask_cost=lambda model, tokens_in, tokens_out: tokens_in / 1e6 + tokens_out / 1e5)
        ask = found["nodes"]["ask"]
        self.assertEqual((ask["modelCalls"], ask["basis"]), (1, "estimate"))
        self.assertLess(ask["lowUsd"], ask["highUsd"])


class RouteTests(GraphCase):
    def test_the_estimate_route_reads_the_published_graph_or_the_draft(self):
        saved = self.workflows.create({"name": "Two reads", "graph": FREE})
        status, body = self.get(f"/api/workflows/{saved['id']}/estimate")
        self.assertEqual((status, sorted(body["estimate"]["nodes"])), (200, ["a", "b", "j", "s", "start"]))
        status, body = self.get(f"/api/workflows/{saved['id']}/estimate?draft=1")
        self.assertEqual(status, 200)
        self.assertEqual(self.get("/api/workflows/0123456789ab/estimate")[0], 404)


class UsageEstimateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mobster-zero-est-")
        self.runtime = runtime(self.dir)

    def smart(self, goal):
        return server.Runtime.engine_estimates(self.runtime, goal)["smart"]

    def test_a_trusted_taught_request_is_free_and_keeps_the_models_price_as_the_fallback(self):
        teach(self.dir, "In Settings, open General, then About")
        found = self.smart("In Settings, open General, then About")
        self.assertEqual((found["lowUsd"], found["highUsd"], found["basis"]), (0.0, 0.0, "taught"))
        self.assertGreater(found["fallbackUsd"]["highUsd"], 0)

    def test_the_front_door_is_free(self):
        found = self.smart("What iOS version is this phone on")
        self.assertEqual((found["highUsd"], found["basis"]), (0.0, "front_door"))

    def test_other_words_a_first_replay_a_chosen_step_and_a_structured_answer_cost_a_model_call(self):
        teach(self.dir, "In Settings, open General, then About")
        self.assertNotIn("basis", self.smart("In Settings open Display"))
        teach(self.dir, "Open Battery in Settings", status="provisional")
        self.assertGreater(self.smart("Open Battery in Settings")["highUsd"], 0)
        teach(self.dir, "Open Wallpaper in Settings", decide=True)
        self.assertGreater(self.smart("Open Wallpaper in Settings")["highUsd"], 0)
        found = server.Runtime.engine_estimates(self.runtime, "In Settings, open General, then About",
                                                {"type": "object"})["smart"]
        self.assertGreater(found["highUsd"], 0)
        self.assertGreater(self.smart("Text Sam that I'm late and send it")["highUsd"], 0)
